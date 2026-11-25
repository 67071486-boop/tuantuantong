from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name

from apps.wecom import archive
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomArchiveState


class ArchiveSenderRepairTests(TenantTestCase):
    def setUp(self):
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-corp",
            agent_id=2, secret="synth-secret",
        )
        self.enterContext(override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name, WECOM_ARCHIVE_PROGRAM_ID="synth-program",
        ))
        self.sent_at = timezone.now().replace(microsecond=0) - timedelta(hours=2)
        self.scope = {
            "tenant": connection.schema_name, "chat_id": ["synth-group"],
            "start": (self.sent_at - timedelta(hours=1)).isoformat(),
            "end": (self.sent_at + timedelta(hours=1)).isoformat(),
        }
        self.state = WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program", cursor="live-cursor",
        )

    def row(self, msg_id, *, chat_id="synth-group", sender_id="", sender_type=None, sent_at=None):
        return WecomArchiveMessage.objects.create(
            identity_revision=self.config.identity_revision, msg_id=msg_id, chat_id=chat_id,
            sent_at=sent_at or self.sent_at, encrypted_secret_key="stored-key", public_key_version=1,
            sender_id=sender_id, sender_type=sender_type,
        )

    def message(self, msg_id, *, chat_id="synth-group", sender_id="recovered", sender_type=2, sent_at=None):
        return {
            "msgid": msg_id, "chatid": chat_id, "send_time": int((sent_at or self.sent_at).timestamp()),
            "sender": {"id": sender_id, "type": sender_type},
            "service_encrypt_info": {"encrypted_secret_key": "response-key", "public_key_ver": 5},
        }

    def sender(self, row):
        row.refresh_from_db()
        return row.sender_id, row.sender_type

    def test_live_sync_repairs_missing_sender_without_changing_other_existing_fields(self):
        missing = self.row("missing")
        complete = self.row("complete", sender_id="existing", sender_type=1)
        other_group = self.row("different-group")
        other_time = self.row("different-time")
        incomplete = self.row("partial", sender_id="recovered")
        conflict = self.row("partial-conflict", sender_id="existing")
        invalid = self.row("invalid")
        with mock.patch.object(archive, "_call_program", return_value={
            "msg_list": [
                self.message("missing"), self.message("complete"),
                self.message("different-group", chat_id="another-group"),
                self.message("different-time", sent_at=self.sent_at + timedelta(seconds=1)),
                self.message("partial"), self.message("partial-conflict"),
                self.message("invalid", sender_type=True),
                self.message("new"),
            ],
            "next_cursor": "live-cursor-next", "has_more": 0,
        }):
            self.assertEqual(archive.run_due(), 8)
        self.assertEqual(self.sender(missing), ("recovered", 2))
        self.assertEqual((missing.encrypted_secret_key, missing.public_key_version), ("stored-key", 1))
        self.assertEqual(self.sender(complete), ("existing", 1))
        self.assertEqual(self.sender(other_group), ("", None))
        self.assertEqual(self.sender(other_time), ("", None))
        self.assertEqual(self.sender(incomplete), ("recovered", 2))
        self.assertEqual(self.sender(conflict), ("existing", None))
        self.assertEqual(self.sender(invalid), ("", None))
        self.assertEqual(self.sender(WecomArchiveMessage.objects.get(msg_id="new")), ("recovered", 2))
        self.state.refresh_from_db()
        self.assertEqual(self.state.cursor, "live-cursor-next")

    def test_historical_preview_and_apply_use_existing_program_with_a_separate_cursor(self):
        recover = self.row("recover")
        missing = self.row("still-missing", sent_at=self.sent_at + timedelta(seconds=1))
        wrong_group = self.row("wrong-group")
        other_group = self.row("out-of-scope-group", chat_id="outside-group")
        other_window = self.row("out-of-window", sent_at=self.sent_at - timedelta(hours=3))
        complete = self.row("complete", sender_id="known", sender_type=1)
        first = {"errcode": 0, "page_id_list": ["page-1"], "next_cursor": "repair-cursor", "has_more": 1}
        first_records = {"errcode": 0, "msg_list": [
            self.message("wrong-group", chat_id="other-group"), self.message("out-of-scope-group", chat_id="outside-group"),
            self.message("out-of-window", sent_at=other_window.sent_at), self.message("complete"),
            self.message("newer-unrelated", chat_id="outside-group", sent_at=self.sent_at + timedelta(hours=2)),
        ]}
        second = {"errcode": 0, "page_id_list": ["page-2"], "has_more": 0}
        recovered_record = self.message("recover")
        recovered_record.pop("service_encrypt_info")
        second_records = {"errcode": 0, "msg_list": [recovered_record, self.message("missing-not-local")]}
        for applying in (False, True):
            output = StringIO()
            with mock.patch.object(archive, "_call_program_ability", side_effect=[
                first, first_records, second, second_records,
            ]) as fetch:
                call_command("backfill_wecom_archive_senders", **self.scope, apply=applying, stdout=output)
            kinds = [call.args[3] for call in fetch.call_args_list]
            self.assertEqual([arg["op"] for arg in kinds], [
                "sync_msg", "get_msg_list_by_page_id", "sync_msg", "get_msg_list_by_page_id",
            ])
            self.assertEqual(kinds[0]["params"]["mode"], 1)
            self.assertNotIn("cursor", kinds[0]["params"])
            self.assertEqual(kinds[2]["params"]["cursor"], "repair-cursor")
            self.assertNotIn("begin_time", kinds[2]["params"])
            self.assertTrue(all(call.args[1:3] == ("synth-program", "tt_zone_v1") for call in fetch.call_args_list))
            self.state.refresh_from_db()
            self.assertEqual(self.state.cursor, "live-cursor")
            self.assertEqual(self.sender(recover), ("recovered", 2) if applying else ("", None))
            self.assertEqual(self.sender(missing), ("", None))
            self.assertEqual(self.sender(wrong_group), ("", None))
            self.assertEqual(self.sender(other_group), ("", None))
            self.assertEqual(self.sender(other_window), ("", None))
            self.assertEqual(self.sender(complete), ("known", 1))
            self.assertFalse(WecomArchiveMessage.objects.filter(msg_id="missing-not-local").exists())
            self.assertNotIn("recovered", output.getvalue())

    def test_historical_repair_rejects_unconfigured_tenants_without_calling_wecom(self):
        self.row("missing")
        with mock.patch.object(archive, "_call_program_ability") as fetch:
            for tenant in (get_public_schema_name(), "archive_workspace_b"):
                with self.subTest(tenant=tenant), self.assertRaisesRegex(CommandError, "工作室"):
                    call_command("backfill_wecom_archive_senders", **{**self.scope, "tenant": tenant}, apply=True)
        fetch.assert_not_called()
        self.assertEqual(WecomArchiveMessage.objects.get().sender_id, "")

    def test_historical_repair_leaves_sender_already_written_by_another_worker(self):
        row = self.row("missing")

        def fetched(*args, **kwargs):
            WecomArchiveMessage.objects.filter(pk=row.pk).update(sender_id="synced-in-flight", sender_type=1)
            return {"errcode": 0, "msg_list": [self.message("missing")], "has_more": 0}

        output = StringIO()
        with mock.patch.object(archive, "_call_program_ability", side_effect=fetched):
            call_command("backfill_wecom_archive_senders", **self.scope, apply=True, stdout=output)
        self.assertEqual(self.sender(row), ("synced-in-flight", 1))
        self.assertIn("已写回 0 条", output.getvalue())

    def test_historical_repair_reports_page_limit_and_applies_verified_subset(self):
        first = self.row("recover")
        missing = self.row("still-missing", sent_at=self.sent_at + timedelta(seconds=1))
        output = StringIO()
        with mock.patch.object(archive, "_call_program_ability", return_value={
            "errcode": 0, "msg_list": [self.message("recover")], "next_cursor": "next", "has_more": 1,
        }) as fetch:
            call_command(
                "backfill_wecom_archive_senders", **self.scope, max_batches=1, apply=True, stdout=output,
            )
        fetch.assert_called_once()
        self.assertEqual(self.sender(first), ("recovered", 2))
        self.assertEqual(self.sender(missing), ("", None))
        self.assertIn("已达到本次拉取批数上限", output.getvalue())

    def test_historical_repair_stops_fetching_pages_when_all_target_senders_are_found(self):
        self.row("recover")
        with mock.patch.object(archive, "_call_program_ability", side_effect=[
            {"errcode": 0, "page_id_list": ["needed", "surplus"], "next_cursor": "more", "has_more": 1},
            {"errcode": 0, "msg_list": [self.message("recover")]},
        ]) as fetch:
            call_command("backfill_wecom_archive_senders", **self.scope, stdout=StringIO())
        self.assertEqual(fetch.call_count, 2)

    def test_historical_repair_refuses_write_when_app_configuration_changes_in_flight(self):
        row = self.row("missing")
        def fetched(*args, **kwargs):
            WecomAppConfig.objects.filter(pk=self.config.pk).update(agent_id=3)
            return {"errcode": 0, "msg_list": [self.message("missing")], "has_more": 0}
        with mock.patch.object(archive, "_call_program_ability", side_effect=fetched), self.assertRaises(CommandError):
            call_command("backfill_wecom_archive_senders", **self.scope, apply=True)
        self.assertEqual(self.sender(row), ("", None))

    def test_old_window_outside_available_history_does_not_call_wecom(self):
        sent_at = self.sent_at - timedelta(days=6)
        row = self.row("expired", sent_at=sent_at)
        scope = {**self.scope, "start": (sent_at - timedelta(hours=1)).isoformat(),
                 "end": (sent_at + timedelta(hours=1)).isoformat()}
        output = StringIO()
        with mock.patch.object(archive, "_call_program_ability") as fetch:
            call_command("backfill_wecom_archive_senders", **scope, apply=True, stdout=output)
        fetch.assert_not_called()
        self.assertEqual(self.sender(row), ("", None))
        self.assertIn("已超过企微可重取窗口 1 条", output.getvalue())
