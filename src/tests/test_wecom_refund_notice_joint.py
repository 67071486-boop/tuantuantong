from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.core.error_codes import ErrorCode
from apps.wecom import refund_notices, zone_events
from apps.wecom.management.commands import sync_wecom_group_chats
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomGroupChat, WecomRefundNotice
from tests.p5_auth_helpers import make_logged_in_client


class RefundSystemChatIntegrationTests(TenantTestCase):
    @staticmethod
    def get_test_schema_name():
        return "kexing"

    def test_refund_event_binds_and_delivers_one_readable_notification_after_summary(self):
        sent_at = timezone.now() - timedelta(seconds=10)
        self.enterContext(self.settings(
            WECOM_ARCHIVE_TENANT_SCHEMA="kexing", WECOM_ARCHIVE_PROGRAM_ID="synth-joint-program",
            WECOM_ZONE_ENABLED=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_CORE_BACKEND="legacy",
            WECOM_REFUND_NOTICE_RULE_ID="synth-joint-rule", WECOM_REFUND_NOTICE_CHAT_ID="synth-joint-chat",
            WECOM_REFUND_NOTICE_START_AT=(sent_at - timedelta(minutes=1)).isoformat(),
        ))
        config = WecomAppConfig.objects.create(
            tenant_schema="kexing", enabled=True, corp_id="synth-joint-corp", agent_id=7,
            secret="synth-joint-secret", identity_revision=2,
        )
        game = Game.objects.create(name="退款提醒集成测试游戏")
        reader = make_logged_in_client(
            domain=self.domain.domain, username="synth-sales-owner", groups=("客服",), nodes=("order.view_detail",),
        )
        colleague = make_logged_in_client(
            domain=self.domain.domain, username="synth-sales-colleague", groups=("客服",), nodes=("order.view_detail",),
        )
        order = Order.objects.create(
            game=game, order_no="20261122000000002711", source_order_no="SYN-REFUND-2711",
            sales_user_id=int(reader.session["_auth_user_id"]),
        )
        WecomGroupChat.objects.create(chat_id="synth-joint-chat", name=order.source_order_no, identity_revision=2)
        # The normal sync command consumes the notification after this message is archived.
        WecomArchiveMessage.objects.create(
            identity_revision=2, msg_id="synth-joint-message", chat_id="synth-joint-chat", sent_at=sent_at,
            encrypted_secret_key="synth-joint-encrypted-key", public_key_version=1,
        )
        callback = {
            "ToUserName": config.corp_id, "MsgType": "event", "Event": "program_notify", "NotifyScene": "94",
        }
        vendor_event = {"event_type": "hit_keyword", "timestamp": int(sent_at.timestamp()),
                        "hit_keyword": {"token": "synth-joint-token"}}
        vendor_hits = {"msg_list": [{"msgid": "synth-joint-message", "hit_rule_list": [
            {"rule_id": "synth-joint-rule", "has_hit_keyword": True},
        ]}], "next_cursor": "x" * 67, "has_more": 0}
        path = "/api/internal-chat/"

        def conversations(client):
            response = client.get(path + "conversations/")
            self.assertEqual(response.status_code, 200)
            return response.json()["data"]["conversations"]

        now = timezone.now()
        processor_times = iter(now + timedelta(minutes=minute) for minute in (0, 2, 4, 6))
        summary = "客户提出退款，原因仍待确认。"
        with (
            patch.object(sync_wecom_group_chats, "run_due", return_value=0),
            patch.object(sync_wecom_group_chats, "cleanup_due", return_value=0),
            patch.object(sync_wecom_group_chats, "sync_archive", return_value=0),
            patch.object(sync_wecom_group_chats, "send_refund_notices",
                         side_effect=lambda: refund_notices.run_due(now=next(processor_times))),
            patch.object(zone_events, "read_zone_event", return_value=vendor_event),
            patch.object(zone_events, "call_zone_operation", return_value=vendor_hits),
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-joint-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-joint-job"}, {"status": 0}, {"status": 1, "response_data": summary},
            ]),
        ):
            zone_events.queue_notification(config, {**callback, "NotifyId": "synth-joint-notice"})
            call_command("sync_wecom_group_chats", stdout=StringIO())
            delivery = WecomRefundNotice.objects.get()
            self.assertEqual(delivery.order_id, order.pk)
            self.assertIsNone(delivery.sent_at)
            self.assertEqual(conversations(reader), [])
            call_command("sync_wecom_group_chats", stdout=StringIO())
            self.assertEqual(conversations(reader), [])

            call_command("sync_wecom_group_chats", stdout=StringIO())
            zone_events.queue_notification(config, {**callback, "NotifyId": "synth-joint-duplicate"})
            call_command("sync_wecom_group_chats", stdout=StringIO())

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.recipient_id, order.sales_user_id)
        row, = conversations(reader)
        self.assertEqual((row["kind"], row["peer"]["name"], row["unread_count"]), ("system", "系统通知", 1))
        history_path = f"{path}conversations/{row['id']}/messages/"
        message, = reader.get(history_path).json()["data"]["messages"]
        self.assertIn("我要退款", message["text"])
        self.assertIn(summary, message["text"])
        self.assertEqual(message["system_order_no"], order.order_no)
        self.assertEqual(conversations(colleague), [])
        denied = colleague.get(history_path).json()
        self.assertEqual(denied["code"], ErrorCode.BUSINESS)
        self.assertIsNone(denied["data"])
