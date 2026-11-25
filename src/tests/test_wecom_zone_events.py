from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.platform.provisioning import provision_tenant
from apps.wecom import archive, zone_events
from apps.wecom.models import (
    WecomAppConfig, WecomArchiveNotification, WecomZoneKeywordHit, WecomZoneKeywordState, WecomZoneNotification,
)
from tests._wecom_helpers import AES_KEY, NONCE, TIMESTAMP, TOKEN, encrypt_message, signature, wrap_encrypted_body


class ZoneEventTests(TenantTestCase):
    def setUp(self):
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-zone-corp", agent_id=42,
            secret="synth-zone-secret", callback_token=TOKEN, encoding_aes_key=AES_KEY,
        )
        self.enterContext(override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name,
            WECOM_ARCHIVE_PROGRAM_ID="test-zone-program", WECOM_ZONE_ENABLED=True,
        ))
        self.notification = {
            "ToUserName": self.config.corp_id, "MsgType": "event", "Event": "program_notify",
            "NotifyScene": "94", "NotifyId": "synth-zone-notice",
        }

    def test_new_events_need_opt_in_and_do_not_consume_the_legacy_queue(self):
        with override_settings(WECOM_ZONE_ENABLED=False):
            zone_events.queue_notification(self.config, self.notification)
            self.assertFalse(WecomZoneNotification.objects.exists())
            archive.queue_notification(self.config, {**self.notification, "NotifyScene": "93"})
        self.assertEqual(WecomArchiveNotification.objects.get().notify_id, "synth-zone-notice")
        zone_events.queue_notification(self.config, {**self.notification, "NotifyScene": "93"})
        zone_events.queue_notification(self.config, {**self.notification, "ToUserName": "other-corp"})
        with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA="different-tenant"):
            zone_events.queue_notification(self.config, self.notification)
            self.assertEqual(zone_events.run_due(), 0)
        self.assertFalse(WecomZoneNotification.objects.exists())
        zone_events.queue_notification(self.config, self.notification)
        zone_events.queue_notification(self.config, self.notification)
        self.assertEqual(WecomZoneNotification.objects.count(), 1)

    def test_encrypted_notification_only_enters_the_configured_tenant_queue(self):
        other, domain = provision_tenant(schema_name="zone_event_b", name="合成专区租户乙", domain="zone-event-b.test.com")
        with tenant_context(other):
            WecomAppConfig.objects.create(
                tenant_schema=other.schema_name, enabled=True, corp_id="synth-zone-other", agent_id=42,
                secret="synth-zone-other-secret", callback_token=TOKEN, encoding_aes_key=AES_KEY,
            )
        xml = ("<xml><ToUserName>synth-zone-corp</ToUserName><MsgType>event</MsgType>"
               "<Event>program_notify</Event><NotifyScene>94</NotifyScene>"
               "<NotifyId>synth-signed-zone-notice</NotifyId><AgentID>42</AgentID></xml>")
        ciphertext = encrypt_message(aes_key=AES_KEY, corp_id=self.config.corp_id, xml=xml)
        query = urlencode({"timestamp": TIMESTAMP, "nonce": NONCE,
                           "msg_signature": signature([TOKEN, TIMESTAMP, NONCE, ciphertext])})
        path = f"/api/wecom/callback?{query}"
        body = wrap_encrypted_body(ciphertext)
        with self.settings(ALLOWED_HOSTS=[domain.domain, "tenant.test.com"]), tenant_context(self.tenant):
            response = Client(SERVER_NAME=domain.domain).post(path, body, content_type="text/xml")
            self.assertEqual(response.status_code, 403)
            response = Client(SERVER_NAME="tenant.test.com").post(path, body, content_type="text/xml")
            self.assertEqual(response.content, b"success")
        self.assertTrue(WecomZoneNotification.objects.filter(notify_id="synth-signed-zone-notice").exists())
        with tenant_context(other):
            self.assertFalse(WecomZoneNotification.objects.filter(notify_id="synth-signed-zone-notice").exists())

    def test_known_events_store_only_fields_from_official_shape(self):
        zone_events.queue_notification(self.config, self.notification)
        event = {
            "event_type": "knowledge_base_learn_done", "timestamp": 1_700_000_020,
            "knowledge_base_learn_done": {
                "knowledge_base_id": "test-kb", "doc_id": 12, "learn_status": 1, "body": "test-content",
            },
        }
        with mock.patch.object(zone_events, "read_zone_event", return_value=event) as read:
            self.assertEqual(zone_events.run_due(), 1)
            self.assertEqual(zone_events.run_due(), 0)
        self.assertEqual(read.call_args.args[1], "synth-zone-notice")
        stored = WecomZoneNotification.objects.get()
        self.assertEqual(stored.event_type, "knowledge_base_learn_done")
        self.assertEqual(stored.details, {"timestamp": 1_700_000_020, "knowledge_base_id": "test-kb",
                                          "doc_id": 12, "learn_status": 1})
        self.assertIsNotNone(stored.completed_at)

    def test_keyword_result_and_cursor_commit_without_saving_token(self):
        zone_events.queue_notification(self.config, self.notification)
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        first = {"msg_list": [
            {"msgid": "test-message", "hit_rule_list": [
                {"rule_id": "test-rule", "has_hit_keyword": True, "semantics_list": [9], "content": "ignored"},
            ]},
        ], "has_more": 1, "next_cursor": "test-hit-page"}
        second = {"msg_list": [{"msgid": "test-message", "hit_rule_list": [
            {"rule_id": "test-rule", "has_hit_keyword": True, "semantics_list": [9]},
        ]}], "has_more": 0, "next_cursor": "test-hit-last"}
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=[first, second],
        ) as call:
            self.assertEqual(zone_events.run_due(), 1)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token"},
            {"limit": 100, "token": "test-hit-token", "cursor": "test-hit-page"},
        ])
        stored = WecomZoneNotification.objects.get()
        self.assertEqual(stored.event_type, "hit_keyword")
        self.assertEqual(stored.details, {"timestamp": 1_700_000_050})
        self.assertIsNotNone(stored.completed_at)
        matched = WecomZoneKeywordHit.objects.get()
        self.assertEqual((matched.msg_id, matched.rule_id, matched.semantics), ("test-message", "test-rule", [9]))
        self.assertEqual(WecomZoneKeywordState.objects.get().cursor, "test-hit-last")

        zone_events.queue_notification(self.config, {**self.notification, "NotifyId": "synth-next-zone-notice"})
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", return_value={"msg_list": [], "next_cursor": "test-next", "has_more": 0},
        ) as call:
            self.assertEqual(zone_events.run_due(), 1)
        self.assertEqual(call.call_args.args[2], {"limit": 100, "token": "test-hit-token", "cursor": "test-hit-last"})

    @staticmethod
    def _keyword_message(msg_id):
        return {"msgid": msg_id, "hit_rule_list": [{"rule_id": "test-rule", "has_hit_keyword": True}]}

    def test_two_overlong_terminal_pages_store_hits_and_replay_the_last_usable_cursor(self):
        old_cursor = "last-usable-position"
        WecomZoneKeywordState.objects.create(
            identity_revision=self.config.identity_revision, program_id="test-zone-program", cursor=old_cursor,
        )
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        first = self._keyword_message("synth-hit-first")
        second = self._keyword_message("synth-hit-second")
        long_cursor = "游" * 22 + "x"
        pages = [
            {"msg_list": [first], "next_cursor": long_cursor, "has_more": 0},
            {"msg_list": [first, second], "next_cursor": long_cursor, "has_more": 0},
            {"msg_list": [], "has_more": 0},
        ]
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=pages,
        ) as call:
            for seq in range(3):
                zone_events.queue_notification(self.config, {**self.notification, "NotifyId": f"synth-keyword-{seq}"})
                self.assertEqual(zone_events.run_due(), 1)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token", "cursor": old_cursor},
        ] * 3)
        self.assertCountEqual(WecomZoneKeywordHit.objects.values_list("msg_id", flat=True), [
            "synth-hit-first", "synth-hit-second",
        ])
        self.assertEqual(WecomZoneNotification.objects.filter(completed_at__isnull=False).count(), 3)
        state = WecomZoneKeywordState.objects.get()
        self.assertEqual((state.cursor, state.needs_pull, state.retry_at), (old_cursor, False, None))

    def test_keyword_pager_accepts_terminal_wider_read_with_the_same_messages(self):
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        zone_events.queue_notification(self.config, self.notification)
        rows = [self._keyword_message(f"synth-hit-{i}") for i in range(100)]
        long_cursor = "x" * 67
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=[
                {"msg_list": rows, "next_cursor": long_cursor, "has_more": 1},
                {"msg_list": rows, "next_cursor": long_cursor, "has_more": 0},
            ],
        ) as call:
            self.assertEqual(zone_events.run_due(), 1)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token"},
            {"limit": 1000, "token": "test-hit-token"},
        ])
        self.assertCountEqual(WecomZoneKeywordHit.objects.values_list("msg_id", flat=True), [row["msgid"] for row in rows])
        state = WecomZoneKeywordState.objects.get()
        self.assertEqual((state.cursor, state.needs_pull), ("", False))
        self.assertIsNotNone(WecomZoneNotification.objects.get().completed_at)

    def test_keyword_pager_continues_with_a_sendable_cursor_from_the_wider_read(self):
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        zone_events.queue_notification(self.config, self.notification)
        rows = [self._keyword_message(f"synth-hit-{i}") for i in range(110)]
        short_cursor = "界" * 21 + "x"
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=[
                {"msg_list": rows[:100], "next_cursor": "x" * 67, "has_more": 1},
                {"msg_list": rows, "next_cursor": short_cursor, "has_more": 1},
                {"msg_list": [self._keyword_message("synth-last")], "has_more": 0},
            ],
        ) as call:
            self.assertEqual(zone_events.run_due(), 1)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token"},
            {"limit": 1000, "token": "test-hit-token"},
            {"limit": 100, "token": "test-hit-token", "cursor": short_cursor},
        ])
        self.assertCountEqual(WecomZoneKeywordHit.objects.values_list("msg_id", flat=True), [
            *(row["msgid"] for row in rows), "synth-last",
        ])
        state = WecomZoneKeywordState.objects.get()
        self.assertEqual((state.cursor, state.needs_pull), (short_cursor, False))
        self.assertIsNotNone(WecomZoneNotification.objects.get().completed_at)

    def test_keyword_pager_stores_hits_without_following_the_same_cursor_again(self):
        old_cursor = "last-usable-position"
        state = WecomZoneKeywordState.objects.create(
            identity_revision=self.config.identity_revision, program_id="test-zone-program", cursor=old_cursor,
        )
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        zone_events.queue_notification(self.config, self.notification)
        first, second = (self._keyword_message(msg_id) for msg_id in ("synth-first", "synth-second"))
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=[
                {"msg_list": [first], "next_cursor": "x" * 67, "has_more": 1},
                {"msg_list": [first, second], "next_cursor": old_cursor, "has_more": 1},
            ],
        ) as call:
            self.assertEqual(zone_events.run_due(), 0)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token", "cursor": old_cursor},
            {"limit": 1000, "token": "test-hit-token", "cursor": old_cursor},
        ])
        self.assertCountEqual(WecomZoneKeywordHit.objects.values_list("msg_id", flat=True), ["synth-first", "synth-second"])
        state.refresh_from_db()
        self.assertEqual((state.cursor, state.needs_pull), (old_cursor, True))
        self.assertGreater(state.retry_at, timezone.now())

    def test_keyword_pager_keeps_a_full_widest_page_pending_and_retries_from_the_same_start(self):
        old_cursor = "last-usable-position"
        state = WecomZoneKeywordState.objects.create(
            identity_revision=self.config.identity_revision, program_id="test-zone-program", cursor=old_cursor,
        )
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        zone_events.queue_notification(self.config, self.notification)
        rows = [self._keyword_message(f"synth-hit-{i}") for i in range(1000)]
        long_cursor = "x" * 67
        pages = [
            {"msg_list": rows[:100], "next_cursor": long_cursor, "has_more": 1},
            {"msg_list": rows, "next_cursor": long_cursor, "has_more": 1},
        ]
        with mock.patch.object(zone_events, "read_zone_event", return_value=event), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=pages * 2,
        ) as call:
            self.assertEqual(zone_events.run_due(), 0)
            self.assertEqual(zone_events.run_due(), 0)
            self.assertEqual(call.call_count, 2)
            expired_retry = timezone.now() - timedelta(seconds=1)
            WecomZoneKeywordState.objects.filter(pk=state.pk).update(retry_at=expired_retry)
            WecomZoneNotification.objects.filter(notify_id=self.notification["NotifyId"]).update(retry_at=expired_retry)
            self.assertEqual(zone_events.run_due(), 0)
        self.assertEqual([request.args[2] for request in call.call_args_list], [
            {"limit": 100, "token": "test-hit-token", "cursor": old_cursor},
            {"limit": 1000, "token": "test-hit-token", "cursor": old_cursor},
        ] * 2)
        self.assertCountEqual(WecomZoneKeywordHit.objects.values_list("msg_id", flat=True), [row["msgid"] for row in rows])
        state.refresh_from_db()
        self.assertEqual((state.cursor, state.needs_pull), (old_cursor, True))
        self.assertGreater(state.retry_at, timezone.now())
        notice = WecomZoneNotification.objects.get()
        self.assertIsNone(notice.completed_at)
        self.assertGreater(notice.retry_at, timezone.now())

    def test_keyword_pager_recovers_from_expired_notification_on_saved_cursor(self):
        zone_events.queue_notification(self.config, self.notification)
        event = {"event_type": "hit_keyword", "timestamp": 1_700_000_050, "hit_keyword": {"token": "test-hit-token"}}
        with mock.patch.object(zone_events, "MAX_ITEMS_PER_RUN", 1), mock.patch.object(
            zone_events, "read_zone_event", return_value=event,
        ), mock.patch.object(zone_events, "call_zone_operation", side_effect=[
            {"msg_list": [], "has_more": 1, "next_cursor": "test-saved-page"}, {"errcode": 0},
        ]):
            self.assertEqual(zone_events.run_due(), 0)
        state = WecomZoneKeywordState.objects.get()
        self.assertTrue(state.needs_pull)
        self.assertEqual(state.cursor, "test-saved-page")

        WecomZoneNotification.objects.update(received_at=timezone.now() - timedelta(minutes=11))
        WecomZoneKeywordState.objects.update(retry_at=timezone.now() - timedelta(minutes=1))
        with mock.patch.object(zone_events, "read_zone_event") as read, mock.patch.object(
            zone_events, "call_zone_operation", return_value={"msg_list": [], "next_cursor": "test-recovered", "has_more": 0},
        ) as call:
            self.assertEqual(zone_events.run_due(), 0)
        read.assert_not_called()
        self.assertEqual(call.call_args.args[2], {"limit": 100, "cursor": "test-saved-page"})
        state.refresh_from_db()
        self.assertEqual((state.cursor, state.needs_pull), ("test-recovered", False))

    def test_export_and_consent_check_real_result_before_completing_notification(self):
        zone_events.queue_notification(self.config, self.notification)
        zone_events.queue_notification(self.config, {**self.notification, "NotifyId": "synth-zone-export-notice"})
        events = [
            {"event_type": "chat_archive_audit_approved_room", "timestamp": 1_700_000_050,
             "chat_archive_audit_approved": {"chatid": "test-chat", "external_userid": "test-guest"}},
            {"event_type": "chat_archive_export_finished", "timestamp": 1_700_000_080,
             "chat_archive_export_finished": {"jobid": "test-export"}},
        ]
        responses = [
            {"agreeinfo": [{"external_userid": "different-guest", "agree_status": "Disagree"},
                           {"external_userid": "test-guest", "agree_status": "Agree", "status_change_time": 99}]},
            {"status": 3, "result_errcode": 0, "result_id": "test-download"},
        ]
        with mock.patch.object(zone_events, "read_zone_event", side_effect=events), mock.patch.object(
            zone_events, "call_zone_operation", side_effect=responses,
        ) as call:
            self.assertEqual(zone_events.run_due(), 2)
        self.assertEqual([request.args[1:3] for request in call.call_args_list], [
            ("get_agree_status_room", {"chatid": "test-chat"}),
            ("get_chatdata_export_job_status", {"jobid": "test-export"}),
        ])
        self.assertEqual(WecomZoneNotification.objects.get(notify_id="synth-zone-notice").details["agree_status"], "Agree")
        self.assertEqual(WecomZoneNotification.objects.get(notify_id="synth-zone-export-notice").details["result_id"], "test-download")

    def test_outage_retries_and_expired_notification_is_visible_as_expired(self):
        zone_events.queue_notification(self.config, self.notification)
        with mock.patch.object(zone_events, "read_zone_event", side_effect=archive.ArchiveError("fail")) as read:
            self.assertEqual(zone_events.run_due(), 0)
            self.assertEqual(zone_events.run_due(), 0)
        self.assertEqual(read.call_count, 1)
        WecomZoneNotification.objects.update(received_at=timezone.now() - timedelta(minutes=11))
        with mock.patch.object(zone_events, "read_zone_event") as read:
            self.assertEqual(zone_events.run_due(), 0)
        read.assert_not_called()
        self.assertIsNotNone(WecomZoneNotification.objects.get().expired_at)
