import base64
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context
from requests import Response
from requests.exceptions import Timeout
from wechatpy.exceptions import WeChatClientException

from apps.platform.provisioning import provision_tenant
from apps.wecom import archive
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomArchiveNotification, WecomArchiveState


def sdk_page(cursor, messages=(), *, more=False):
    return {"errcode": 0, "msg_list": list(messages), "has_more": int(more), "next_cursor": cursor}


def message(msg_id, *, chat_id="synth-customer-group"):
    return {
        "msgid": msg_id,
        "chatid": chat_id,
        "send_time": int(timezone.now().timestamp()),
        "service_encrypt_info": {"encrypted_secret_key": "synth-encrypted", "public_key_ver": 5},
    }


def program_reply(payload):
    return {"errcode": 0, "response_data": json.dumps({"output": {"payload": json.dumps(payload)}})}


def sent_payload(body):
    return json.loads(json.loads(body["request_data"])["input"]["payload"])


class ArchiveTenantBoundaryTests(TenantTestCase):
    def test_each_workspace_uses_its_own_program_and_cannot_use_another_apps_credentials(self):
        other, _ = provision_tenant(
            schema_name="archive_workspace_b", name="合成工作室乙", domain="archive-workspace-b.test.com",
        )
        credentials = {
            "corp_id": "synth-matching-corp", "enabled": True,
            "agent_id": 2, "secret": "synth-matching-secret",
        }
        first = WecomAppConfig.objects.create(tenant_schema=self.tenant.schema_name, **credentials)
        with tenant_context(other):
            second = WecomAppConfig.objects.create(tenant_schema=other.schema_name, **credentials)
        notice = {
            "ToUserName": first.corp_id, "MsgType": "event", "Event": "program_notify",
            "NotifyScene": "93", "NotifyId": "synth-cross-workspace-notice",
        }
        with (
            override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=self.tenant.schema_name,
                              WECOM_ARCHIVE_PROGRAM_ID="synth-workspace-a-program"),
            mock.patch.object(archive, "_call_program", return_value={
                "chat_list": [{"chatid": "synth-workspace-a-group"}],
            }) as call,
        ):
            self.assertEqual(archive.search_chat_ids(first, "SYNTH-GROUP"), (["synth-workspace-a-group"], True))
            self.assertEqual(call.call_args.args[1], "synth-workspace-a-program")
            call.reset_mock()
            with tenant_context(other):
                self.assertEqual(archive.run_due(), 0)
                with self.assertRaises(archive.ArchiveError):
                    archive.search_chat_ids(second, "SYNTH-GROUP")
            call.assert_not_called()
        with (
            tenant_context(other),
            override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=other.schema_name,
                              WECOM_ARCHIVE_PROGRAM_ID="synth-workspace-b-program"),
            mock.patch.object(archive, "_call_program", return_value={
                "chat_list": [{"chatid": "synth-workspace-b-group"}],
            }) as call,
        ):
            with self.assertRaises(archive.ArchiveError):
                archive.search_chat_ids(first, "SYNTH-GROUP")
            archive.queue_notification(first, notice)
            self.assertFalse(WecomArchiveNotification.objects.filter(notify_id=notice["NotifyId"]).exists())
            call.assert_not_called()
            self.assertEqual(archive.search_chat_ids(second, "SYNTH-GROUP"), (["synth-workspace-b-group"], True))
            self.assertEqual(call.call_args.args[1], "synth-workspace-b-program")
            call.return_value = sdk_page("synth-workspace-b-cursor", [
                message("synth-workspace-b-message", chat_id="synth-workspace-b-group"),
            ])
            self.assertEqual(archive.run_due(), 1)
            self.assertEqual(call.call_args.args[:3], (second, "synth-workspace-b-program", "sync"))
            self.assertTrue(WecomArchiveMessage.objects.filter(msg_id="synth-workspace-b-message").exists())
        self.assertFalse(WecomArchiveMessage.objects.filter(msg_id="synth-workspace-b-message").exists())

    def test_platform_cannot_be_configured_as_a_workspace(self):
        public = get_public_schema_name()
        with (
            schema_context(public),
            override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=public, WECOM_ARCHIVE_PROGRAM_ID="synth-program"),
        ):
            self.assertEqual(archive.archive_program(), "")
            self.assertEqual(archive.run_due(), 0)


class ArchiveSyncTests(TenantTestCase):
    def setUp(self):
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-corp", agent_id=2, secret="synth-secret",
        )
        self.enterContext(override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name, WECOM_ARCHIVE_PROGRAM_ID="synth-program",
        ))

    def test_unconfigured_workspace_cannot_use_index_or_trigger_remote_search(self):
        self.assertEqual(archive.archive_program(), "synth-program")
        with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA="different-tenant"):
            with mock.patch.object(archive.sdk, "get_client") as factory:
                self.assertEqual(archive.archive_program(), "")
                self.assertEqual(archive.run_due(), 0)
                with self.assertRaises(archive.ArchiveError):
                    archive.search_chat_ids(self.config, "SYNTH-ORDER")
                with self.assertRaises(archive.ArchiveError):
                    archive.search_message_ids(self.config, "SYNTH-ORDER", start_time=100, end_time=200)
                factory.assert_not_called()
        self.assertFalse(WecomArchiveState.objects.exists())

    def test_sync_advances_cursor_while_discarding_direct_chats_and_plaintext(self):
        indexed = message("group-msg")
        indexed["content"] = "synthetic plaintext"
        with mock.patch.object(archive, "_call_program") as call:
            call.side_effect = [sdk_page("cursor-a", [indexed, message("private-msg", chat_id="")], more=True),
                                sdk_page("cursor-b", [indexed])]
            self.assertEqual(archive.run_due(), 2)
        self.assertEqual(WecomArchiveState.objects.get().cursor, "cursor-b")
        row = WecomArchiveMessage.objects.get()
        self.assertEqual((row.msg_id, row.chat_id, row.public_key_version), ("group-msg", "synth-customer-group", 5))
        self.assertNotIn("synthetic plaintext", str(row.__dict__))
        self.assertEqual(call.call_args_list[1].args[3]["cursor"], "cursor-a")

    def test_pre_sender_worker_can_still_insert_after_migration(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO wecom_wecomarchivemessage
                   (identity_revision, msg_id, chat_id, sent_at, encrypted_secret_key, public_key_version)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [self.config.identity_revision, "old-worker", "synth-group", timezone.now(), "encrypted-key", 1],
            )
        row = WecomArchiveMessage.objects.get(msg_id="old-worker")
        self.assertEqual(row.sender_id, "")
        self.assertIsNone(row.sender_type)

    def test_bad_page_leaves_cursor_at_last_successfully_indexed_page(self):
        broken = message("not-saved")
        broken["service_encrypt_info"]["public_key_ver"] = "invalid"
        with mock.patch.object(archive, "_call_program") as call:
            call.side_effect = [sdk_page("cursor-a", [message("saved")], more=True),
                                sdk_page("cursor-b", [message("also-not-saved"), broken])]
            with self.assertLogs("apps.wecom.archive", "WARNING"):
                archive.run_due()
        state = WecomArchiveState.objects.get()
        self.assertEqual(state.cursor, "cursor-a")
        self.assertIsNone(state.lease_until)
        self.assertIsNotNone(state.retry_at)
        self.assertEqual(list(WecomArchiveMessage.objects.values_list("msg_id", flat=True)), ["saved"])

    def test_signed_program_notification_supplies_the_sync_token(self):
        WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program", cursor="existing-cursor",
        )
        raw = {
            "ToUserName": self.config.corp_id, "MsgType": "event", "Event": "program_notify",
            "NotifyScene": "93", "NotifyId": "synth-notify",
        }
        archive.queue_notification(self.config, {**raw, "NotifyScene": "92"})
        self.assertFalse(WecomArchiveNotification.objects.exists())
        archive.queue_notification(self.config, raw)
        archive.queue_notification(self.config, raw)
        self.assertEqual(WecomArchiveNotification.objects.count(), 1)

        indexed = {**message("group-msg"), "sender": {"id": "synth-member", "type": 1}}
        mixed = [
            message("private-msg", chat_id=""),
            {**message("no-key"), "service_encrypt_info": {"public_key_ver": 5}},
            indexed,
        ]
        with mock.patch.object(archive.sdk, "get_client") as factory:
            post = factory.return_value.post
            post.side_effect = [
                program_reply({"errcode": 0, "token": "synth-token"}),
                program_reply(sdk_page("after-mixed", mixed, more=True)),
                program_reply(sdk_page("next", [indexed])),
            ]
            with override_settings(WECOM_ZONE_ENABLED=False):
                self.assertEqual(archive.run_due(), 2)
        self.assertEqual(post.call_count, 3)
        self.assertTrue(all(call.args == ("chatdata/sync_call_program",) for call in post.call_args_list))
        token_body, *sync_bodies = (call.kwargs["json"] for call in post.call_args_list)
        self.assertEqual(token_body["ability_id"], "tt_archive_token")
        self.assertEqual(token_body["notify_id"], "synth-notify")
        self.assertEqual(sent_payload(token_body), {})
        for body, cursor in zip(sync_bodies, ("existing-cursor", "after-mixed"), strict=True):
            self.assertEqual(body["ability_id"], "tt_zone_v1")
            self.assertEqual(body["program_id"], token_body["program_id"])
            self.assertNotIn("notify_id", body)
            self.assertEqual(sent_payload(body), {
                "op": "sync_msg", "params": {"cursor": cursor, "token": "synth-token", "limit": 100, "mode": 0},
            })
        self.assertEqual(WecomArchiveState.objects.get().cursor, "next")
        self.assertEqual(list(WecomArchiveMessage.objects.values_list("msg_id", "sender_id", "sender_type")), [
            ("group-msg", "synth-member", 1),
        ])
        self.assertFalse(WecomArchiveNotification.objects.exists())

    def test_filtered_last_page_can_finish_a_notification_without_a_new_cursor(self):
        state = WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program", cursor="existing-cursor",
        )
        filtered = [
            message("private-msg", chat_id=""),
            {**message("no-key"), "service_encrypt_info": {"public_key_ver": 5}},
        ]
        for returned_cursor in ({"next_cursor": "existing-cursor"}, {}):
            with self.subTest(returned_cursor=returned_cursor), mock.patch.object(archive.sdk, "get_client") as factory:
                state.retry_at = None
                state.save(update_fields=["retry_at"])
                archive.queue_notification(self.config, {
                    "ToUserName": self.config.corp_id, "MsgType": "event", "Event": "program_notify",
                    "NotifyScene": "93", "NotifyId": "synth-notify",
                })
                post = factory.return_value.post
                post.side_effect = [
                    program_reply({"errcode": 0, "token": "synth-token"}),
                    program_reply({"errcode": 0, "msg_list": filtered, "has_more": 0, **returned_cursor}),
                ]
                self.assertEqual(archive.run_due(), 0)
                self.assertEqual(post.call_count, 2)
                self.assertFalse(WecomArchiveNotification.objects.exists())
                self.assertFalse(WecomArchiveMessage.objects.exists())
                state.refresh_from_db()
                self.assertEqual(state.cursor, "existing-cursor")
                self.assertIsNone(state.retry_at)
                self.assertGreater(state.next_poll_at, timezone.now())

    def test_valid_group_row_or_next_page_with_repeated_cursor_is_rejected(self):
        state = WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program", cursor="existing-cursor",
        )
        for records, more in (([message("group-msg")], False), ([message("private-msg", chat_id="")], True)):
            with self.subTest(more=more), mock.patch.object(archive.sdk, "get_client") as factory:
                state.retry_at = None
                state.save(update_fields=["retry_at"])
                factory.return_value.post.return_value = program_reply(sdk_page("existing-cursor", records, more=more))
                with self.assertLogs("apps.wecom.archive", "WARNING"):
                    self.assertEqual(archive.run_due(), 0)
                factory.return_value.post.assert_called_once()
                state.refresh_from_db()
                self.assertEqual(state.cursor, "existing-cursor")
                self.assertIsNotNone(state.retry_at)
                self.assertFalse(WecomArchiveMessage.objects.exists())

    def test_projection_failure_only_retries_that_sync_page_with_legacy_ability(self):
        state = WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program", cursor="existing-cursor",
        )
        archive.queue_notification(self.config, {
            "ToUserName": self.config.corp_id, "MsgType": "event", "Event": "program_notify",
            "NotifyScene": "93", "NotifyId": "synth-notify",
        })
        with mock.patch.object(archive.sdk, "get_client") as factory:
            post = factory.return_value.post
            post.side_effect = [
                program_reply({"errcode": 0, "token": "synth-token"}),
                program_reply({"errcode": -1}),
                program_reply(sdk_page("next", [message("legacy-msg")], more=True)),
                program_reply(sdk_page("done", [message("v1-msg")])),
            ]
            with self.assertLogs("apps.wecom.archive", "WARNING"):
                self.assertEqual(archive.run_due(), 2)
        outbound = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual([body["ability_id"] for body in outbound], [
            "tt_archive_token", "tt_zone_v1", "tt_archive_sync", "tt_zone_v1",
        ])
        params = {"cursor": "existing-cursor", "token": "synth-token", "limit": 100, "mode": 0}
        self.assertEqual(sent_payload(outbound[1]), {"op": "sync_msg", "params": params})
        self.assertEqual(sent_payload(outbound[2]), params)
        self.assertEqual(sent_payload(outbound[3]), {"op": "sync_msg", "params": {**params, "cursor": "next"}})
        state.refresh_from_db()
        self.assertEqual(state.cursor, "done")
        self.assertIsNone(state.retry_at)
        self.assertEqual(set(WecomArchiveMessage.objects.values_list("msg_id", flat=True)), {"legacy-msg", "v1-msg"})
        self.assertFalse(WecomArchiveNotification.objects.exists())

    def test_current_poller_lease_prevents_duplicate_remote_call(self):
        WecomArchiveState.objects.create(
            identity_revision=self.config.identity_revision, program_id="synth-program",
            lease_id="active-lease", lease_until=timezone.now() + timedelta(minutes=1),
        )
        with mock.patch.object(archive, "_call_program") as call:
            self.assertEqual(archive.run_due(), 0)
        call.assert_not_called()

    def test_stale_application_configuration_cannot_commit_messages(self):
        def replace_config(*args):
            WecomAppConfig.objects.filter(pk=self.config.pk).update(identity_revision=1)
            return sdk_page("obsolete-cursor", [message("obsolete")])

        with mock.patch.object(archive, "_call_program", side_effect=replace_config):
            with self.assertLogs("apps.wecom.archive", "WARNING"):
                archive.run_due()
        self.assertFalse(WecomArchiveMessage.objects.exists())
        self.assertEqual(WecomArchiveState.objects.get().cursor, "")

    def test_search_only_uses_group_text_and_reports_truncation(self):
        with mock.patch.object(archive.sdk, "get_client") as factory:
            post = factory.return_value.post
            post.side_effect = [
                program_reply(sdk_page(f"cursor-{i}", [{"msgid": f"msg-{i}"}], more=True)) for i in range(2)
            ]
            ids, complete = archive.search_message_ids(self.config, "SYNTH-ORDER", start_time=100, end_time=200)
        self.assertEqual(ids, ["msg-0", "msg-1"])
        self.assertFalse(complete)
        self.assertEqual(post.call_count, 2)
        for index, call in enumerate(post.call_args_list):
            self.assertEqual(call.args, ("chatdata/sync_call_program",))
            body = call.kwargs["json"]
            self.assertEqual(body["ability_id"], "tt_zone_v1")
            params = {
                "chat_info": {"chat_type": 2, "msg_type_list": [1]},
                "query_word": "SYNTH-ORDER", "start_time": 100, "end_time": 200,
                "limit": 100, "skip_stop_words": False,
            }
            if index == 1:
                params["cursor"] = "cursor-0"
            self.assertEqual(sent_payload(body), {"op": "search_msg", "params": params})

    def test_search_group_names_passes_official_pagination_and_deduplicates_ids(self):
        with mock.patch.object(archive.sdk, "get_client") as factory, override_settings(WECOM_ZONE_ENABLED=False):
            post = factory.return_value.post
            post.side_effect = [
                program_reply({"errcode": 0, "chat_list": [{"chatid": "first"}], "next_cursor": "page-2", "has_more": 1}),
                program_reply({"errcode": 0, "chat_list": [{"chatid": "first"}, {"chatid": "second"}], "has_more": 0}),
            ]
            ids, complete = archive.search_chat_ids(self.config, "SYNTH-ORDER")
        self.assertEqual((ids, complete), (["first", "second"], True))
        self.assertEqual(post.call_count, 2)
        for index, call in enumerate(post.call_args_list):
            body = call.kwargs["json"]
            self.assertEqual(call.args, ("chatdata/sync_call_program",))
            self.assertEqual(body["program_id"], "synth-program")
            self.assertEqual(body["ability_id"], "tt_zone_v1")
            params = {"query_word": "SYNTH-ORDER", "limit": 100}
            if index == 1:
                params["cursor"] = "page-2"
            self.assertEqual(sent_payload(body), {"op": "search_chat", "params": params})

    def test_rejected_cached_token_is_replaced_in_retry_request_and_in_database(self):
        self.config.access_token = "synth-rejected-token"
        self.config.access_token_expires_at = timezone.now() + timedelta(hours=1)
        self.config.save(update_fields=["access_token", "access_token_expires_at"])

        def response(payload):
            result = Response()
            result.status_code = 200
            result._content = json.dumps(payload).encode()
            return result

        with mock.patch("requests.sessions.Session.request") as http:
            http.side_effect = [
                response({"errcode": 40014, "errmsg": "invalid access_token"}),
                response({"errcode": 0, "access_token": "synth-fresh-token", "expires_in": 7200}),
                response({"errcode": 0}),
            ]
            archive.register_archive_callback(self.config, "synth-program")
        first, refresh, second = http.call_args_list
        self.assertEqual(first.kwargs["params"]["access_token"], "synth-rejected-token")
        self.assertEqual(refresh.args, ("GET", "https://qyapi.weixin.qq.com/cgi-bin/gettoken"))
        self.assertEqual(second.kwargs["params"]["access_token"], "synth-fresh-token")
        self.assertEqual(first.kwargs["json"], second.kwargs["json"])
        self.config.refresh_from_db()
        self.assertEqual(self.config.access_token, "synth-fresh-token")

    def test_register_callback_only_accepts_current_pilot_program_and_verifies_wecom_result(self):
        with mock.patch.object(archive.sdk, "get_client") as factory:
            client = factory.return_value
            for foreign_program in ("", "another-program"):
                with self.assertRaises(archive.ArchiveError):
                    archive.register_archive_callback(self.config, foreign_program)
            with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA="another-tenant"):
                with self.assertRaises(archive.ArchiveError):
                    archive.register_archive_callback(self.config, "synth-program")
            factory.assert_not_called()
            client.post.return_value = {"errcode": 60011}
            with self.assertRaisesMessage(archive.ArchiveError, "60011"):
                archive.register_archive_callback(self.config, "synth-program")
            client.post.return_value = {"errcode": 0}
            archive.register_archive_callback(self.config, "synth-program")
            client.post.assert_has_calls([
                mock.call("chatdata/set_receive_callback", json={"program_id": "synth-program"}),
                mock.call("chatdata/set_receive_callback", json={"program_id": "synth-program"}),
            ])


class ArchiveTransportTests(SimpleTestCase):
    def test_sync_fallback_requires_outer_success_and_exact_inner_minus_one(self):
        cases = (
            ("sync", {"errcode": -1}),
            ("sync", {**program_reply({"errcode": -1}), "errcode": 48002}),
            ("sync", program_reply({"errcode": "-1"})),
            ("sync", program_reply({"errcode": 48002})),
            ("sync", Timeout()),
            ("search", program_reply({"errcode": -1})),
        )
        for operation, failure in cases:
            with self.subTest(operation=operation, failure=failure), mock.patch.object(archive.sdk, "get_client") as factory:
                post = factory.return_value.post
                post.side_effect = [failure]
                with self.assertRaises(archive.ArchiveError):
                    archive._call_program(object(), "synth-program", operation, {})
                post.assert_called_once()

    def test_zone_sync_projects_sender_id_and_type_without_profile_or_plaintext(self):
        script = Path(__file__).resolve().parents[2] / "deploy" / "wecom-archive" / "zone_program.py"
        spec = importlib.util.spec_from_file_location("archive_zone_under_test", script)
        program = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"wwspecapisdk": mock.MagicMock()}):
            spec.loader.exec_module(program)

        sdk = mock.Mock()
        group_message = message("group-message")
        legacy_message = message("no-sender")
        sdk.sync_msg.return_value = json.dumps(sdk_page("next", [
            {
                **group_message, "content": "private text", "receiver_list": [{"id": "other-person"}],
                "sender": {"id": "sender-id", "type": 2, "name": "private profile"},
            },
            message("private-message", chat_id=""),
            legacy_message,
        ]))

        self.assertEqual(program.sync_messages(sdk, {}), sdk_page("next", [
            {**group_message, "sender": {"id": "sender-id", "type": 2}}, legacy_message,
        ]))

    def test_upstream_success_requires_an_inner_wecom_success_code(self):
        config = object()
        with mock.patch.object(archive.sdk, "get_client") as factory, mock.patch.object(archive, "_ArchiveSession") as session:
            client = factory.return_value
            client.post.return_value = {"errcode": 0, "response_data": json.dumps({"output": {"payload": '{"errcode":84542}'}})}
            with self.assertRaisesMessage(archive.ArchiveError, "84542"):
                archive._call_program(config, "synth-program", "search", {"query_word": "SYNTH-ORDER"})
            session.return_value.close.assert_called_once()
            self.assertEqual(client.post.call_args.args, ("chatdata/sync_call_program",))
            outbound = client.post.call_args.kwargs["json"]
            self.assertEqual(outbound["ability_id"], "tt_zone_v1")
            self.assertEqual(sent_payload(outbound), {"op": "search_msg", "params": {"query_word": "SYNTH-ORDER"}})

    def test_transport_accepts_direct_json_and_the_documented_output_wrapper(self):
        payload = {"payload": '{"errcode":0,"msg_list":[{"msgid":"synthetic"}]}'}
        config = object()
        with mock.patch.object(archive.sdk, "get_client") as factory:
            client = factory.return_value
            for output in (payload, {"output": payload}):
                with self.subTest(output=output):
                    client.post.return_value = {"errcode": 0, "response_data": json.dumps(output)}
                    result = archive._call_program(config, "synth-program", "search", {})
                    self.assertEqual(result["msg_list"], [{"msgid": "synthetic"}])

    def test_search_returns_partial_page_after_deadline(self):
        config = mock.Mock(tenant_schema=connection.schema_name)
        with (
            mock.patch.object(archive, "archive_program", return_value="synth-program"),
            mock.patch.object(archive, "_call_program") as call,
            mock.patch.object(archive, "monotonic", side_effect=(11, 15)),
        ):
            call.return_value = sdk_page("next", [{"msgid": "found"}], more=True)
            found, complete = archive.search_message_ids(config, "SYNTH-ORDER", start_time=100, end_time=200, deadline=15)
        self.assertEqual((found, complete), (["found"], False))
        call.assert_called_once()

    def test_refresh_rejected_token_before_retrying_with_the_same_deadline(self):
        deadline = 100
        with mock.patch.object(archive.sdk, "get_client") as factory, mock.patch.object(archive, "_ArchiveSession") as session:
            client = factory.return_value
            client.post.side_effect = [
                WeChatClientException(40014, "stale token"),
                {"errcode": 0, "response_data": json.dumps({"payload": '{"errcode":0}'})},
            ]
            result = archive._call_program(object(), "synth-program", "search", {}, deadline=deadline)
        self.assertEqual(result, {"errcode": 0})
        session.assert_called_once_with(deadline)
        session.return_value.close.assert_called_once()
        self.assertEqual(
            [call[0] for call in client.mock_calls if call[0] in {"post", "fetch_access_token"}],
            ["post", "fetch_access_token", "post"],
        )
        self.assertEqual(client.post.call_args_list[0], client.post.call_args_list[1])

    def test_retry_stops_after_second_token_rejection_and_never_retries_permission_error(self):
        for errcode, expected_calls in ((40001, 2), (42001, 2), (60011, 1)):
            with self.subTest(errcode=errcode), mock.patch.object(archive.sdk, "get_client") as factory:
                client = factory.return_value
                client.post.side_effect = WeChatClientException(errcode, "synthetic failure")
                with self.assertRaisesMessage(archive.ArchiveError, str(errcode)):
                    archive._call_program(object(), "synth-program", "search", {})
                self.assertEqual(client.post.call_count, expected_calls)
                self.assertEqual(client.fetch_access_token.call_count, expected_calls - 1)

    def test_versioned_private_key_decrypts_only_valid_rsa_ciphertext(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ciphertext = private_key.public_key().encrypt(b"synthetic-secret", padding.PKCS1v15())
        row = WecomArchiveMessage(public_key_version=5, encrypted_secret_key=base64.b64encode(ciphertext).decode())
        with TemporaryDirectory() as temp_dir, override_settings(WECOM_ARCHIVE_PRIVATE_KEY_DIR=temp_dir):
            (Path(temp_dir) / "5.pem").write_bytes(private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            self.assertEqual(archive.decrypt_message_key(row), "synthetic-secret")
            row.public_key_version = 6
            with self.assertRaises(archive.ArchiveError):
                archive.decrypt_message_key(row)
