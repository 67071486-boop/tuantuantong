import json
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.internal_chat.models import Conversation, Message
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


BASE = "/api/internal-chat/"
ORDER_NO = "20260101010101000001"
TEXT = "客人提出退款，暂未说明原因。"


class InternalChatSystemNotificationsTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="notice_other", name="其他工作室", domain="notice-other.test.local",
        )

    def setUp(self):
        self.enterContext(override_settings(
            DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_CORE_BACKEND="legacy", TT_CHAT_MAINTENANCE=False,
        ))

    def login(self, name, domain=None):
        return make_logged_in_client(domain=domain or self.domain.domain, username=name, games=None)

    def payload(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def post(self, client, path, values):
        return client.post(BASE + path, values, content_type="application/json")

    def test_order_notice_is_a_private_read_only_chat_message_and_only_sent_once(self):
        from apps.internal_chat.system_notifications import send_order_notification

        recipient_client = self.login("refund-owner")
        other_client = self.login("refund-non-owner")
        with tenant_context(self.tenant):
            recipient = get_user_model().objects.get(username="refund-owner")
        with patch("apps.internal_chat.realtime._notify") as push:
            with self.captureOnCommitCallbacks(execute=True):
                with tenant_context(self.tenant), override_settings(
                    TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4191/notify",
                    TT_CHAT_REALTIME_SECRET="x" * 40,
                ):
                    msg = send_order_notification(recipient, order_no=ORDER_NO, text=TEXT, client_id="refund-one")
                    repeated = send_order_notification(
                        recipient, order_no=ORDER_NO, text=TEXT, client_id="refund-one",
                    )
            self.assertEqual(msg, repeated)
        with tenant_context(self.tenant):
            self.assertEqual(Message.objects.filter(pk=msg["id"], text=TEXT).count(), 1)
        self.assertEqual(push.call_count, 2)
        self.assertTrue(all(call.args[1] == [recipient.pk] for call in push.call_args_list))
        self.assertEqual(push.call_args.args[4], {
            "id": msg["id"], "conversation_id": msg["conversation_id"], "author": None,
        })

        chosen = self.payload(recipient_client.get(BASE + "conversations/"))["conversations"]
        self.assertEqual(len(chosen), 1)
        room = chosen[0]
        self.assertEqual((room["kind"], room["peer"]["name"], room["unread_count"]), ("system", "系统通知", 1))
        self.assertEqual((room["last_message"]["text"], room["last_message"]["system_order_no"]), (TEXT, ORDER_NO))
        path = f"conversations/{room['id']}/"
        self.assertEqual(self.payload(recipient_client.get(BASE + path + "messages/"))["messages"], [msg])
        self.assertEqual(self.payload(recipient_client.get(BASE + "unread/"))["unread_count"], 1)
        changes = self.payload(recipient_client.get(BASE + "sync/", {"after": 0}))["changes"]
        self.assertEqual([row["message"] for row in changes if row["message"]], [msg])
        self.assertEqual(self.payload(other_client.get(BASE + "conversations/"))["conversations"], [])
        self.assertEqual(other_client.get(BASE + path + "messages/").json()["code"], 1002)
        self.assertEqual(self.post(recipient_client, path + "messages/", {
            "text": "伪造一条系统通知", "client_id": "fake", "system_order_no": ORDER_NO,
        }).json()["code"], 403)
        self.assertEqual(self.post(recipient_client, f"{path}messages/{msg['id']}/revoke/", {}).json()["code"], 403)
        self.assertEqual(self.payload(self.post(recipient_client, path + "read/", {"seq": 1})), {"read_seq": 1})
        self.assertEqual(self.payload(recipient_client.get(BASE + "unread/"))["unread_count"], 0)
        contacts = self.payload(recipient_client.get(BASE + "contacts/"))["contacts"]
        self.assertNotIn("系统通知", [person["name"] for person in contacts])
        self.assertEqual(self.post(recipient_client, "conversations/", {"peer_id": room["peer"]["id"]}).json()["code"], 1002)

        with tenant_context(self.other_tenant):
            foreign = self.login("refund-other-tenant", domain="notice-other.test.local")
            self.assertEqual(self.payload(foreign.get(BASE + "conversations/"))["conversations"], [])
            self.assertEqual(foreign.get(BASE + path + "messages/").json()["code"], 1002)

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=False)
    def test_disabled_chat_does_not_create_a_system_conversation(self):
        from apps.internal_chat.matrix_bridge import ChatServiceUnavailable
        from apps.internal_chat.system_notifications import send_order_notification

        self.login("refund-owner")
        with tenant_context(self.tenant):
            recipient = get_user_model().objects.get(username="refund-owner")
            with self.assertRaises(ChatServiceUnavailable):
                send_order_notification(recipient, order_no=ORDER_NO, text=TEXT, client_id="refund-one")
            self.assertFalse(Conversation.objects.exists())

    def test_plain_system_notice_is_private_unread_and_idempotent(self):
        from apps.internal_chat.system_notifications import send_order_notification, send_system_notification

        recipient_client = self.login("approval-recipient")
        outsider = self.login("approval-outsider")
        notice_text = "罚款审批已提交，请到审批中心处理。"
        with tenant_context(self.tenant):
            recipient = get_user_model().objects.get(username="approval-recipient")
            order_message = send_order_notification(recipient, order_no=ORDER_NO, text=TEXT, client_id="order-notice")
            notice = send_system_notification(recipient, text=notice_text, client_id="approval-submitted")
            repeated = send_system_notification(recipient, text=notice_text, client_id="approval-submitted")
            self.assertEqual(repeated, notice)
            self.assertEqual(Message.objects.filter(text=notice_text).count(), 1)

        room = self.payload(recipient_client.get(BASE + "conversations/"))["conversations"][0]
        self.assertEqual((room["kind"], room["peer"]["name"], room["unread_count"]), ("system", "系统通知", 2))
        self.assertEqual(room["last_message"], notice)
        self.assertEqual(notice["sender_id"], room["peer"]["id"])
        self.assertEqual(notice["text"], notice_text)
        self.assertNotIn("system_order_no", notice)
        path = f"conversations/{room['id']}/"
        history = self.payload(recipient_client.get(BASE + path + "messages/"))
        self.assertEqual(history["messages"], [order_message, notice])
        older = self.payload(recipient_client.get(BASE + path + "messages/", {"before": notice["seq"]}))
        self.assertEqual(older["messages"], [order_message])
        self.assertEqual(older["messages"][0]["system_order_no"], ORDER_NO)
        search = self.payload(recipient_client.get(BASE + path + "messages/search/", {
            "q": "罚款", "sender_id": room["peer"]["id"],
        }))
        self.assertEqual(search["messages"], [notice])
        displayed = self.payload(recipient_client.get(BASE + f"notification-conversations/{room['id']}/messages/"))
        self.assertEqual((displayed["kind"], displayed["can_reply"], displayed["messages"]), (
            "system", False, [order_message, notice],
        ))
        changes = self.payload(recipient_client.get(BASE + "sync/", {"after": 0}))["changes"]
        self.assertEqual([row["message"] for row in changes if row["message"]], [order_message, notice])
        self.assertEqual(self.payload(outsider.get(BASE + "conversations/"))["conversations"], [])
        self.assertEqual(outsider.get(BASE + path + "messages/").json()["code"], 1002)
        self.assertEqual(self.post(recipient_client, path + "messages/", {
            "text": "回复系统通知", "client_id": "forged-reply",
        }).json()["code"], 403)
        self.assertEqual(self.payload(self.post(recipient_client, path + "read/", {"seq": notice["seq"]})), {"read_seq": 2})
        self.assertEqual(self.payload(recipient_client.get(BASE + "unread/"))["unread_count"], 0)

        foreign_text = "另一工作室的审批回执。"
        with tenant_context(self.other_tenant):
            foreign = self.login("approval-foreign", domain="notice-other.test.local")
            foreign_user = get_user_model().objects.get(username="approval-foreign")
            send_system_notification(foreign_user, text=foreign_text, client_id="approval-submitted")
        foreign_rooms = self.payload(foreign.get(BASE + "conversations/"))["conversations"]
        self.assertEqual([row["last_message"]["text"] for row in foreign_rooms], [foreign_text])

    def test_core_tenant_cannot_write_an_invisible_legacy_notification(self):
        from apps.internal_chat.matrix_bridge import ChatServiceUnavailable
        from apps.internal_chat.system_notifications import send_system_notification

        self.login("approval-core-recipient")
        with NamedTemporaryFile(mode="w") as config, tenant_context(self.tenant):
            json.dump({"service_url": "http://127.0.0.1:9050", "tenants": {
                self.tenant.schema_name: {"key_id": "notice-core", "secret": "local-fixture-secret"},
            }}, config)
            config.flush()
            recipient = get_user_model().objects.get(username="approval-core-recipient")
            with override_settings(TT_CHAT_CORE_BACKEND="service", TT_CHAT_SERVICE_CONFIG_FILE=config.name):
                with self.assertRaises(ChatServiceUnavailable):
                    send_system_notification(recipient, text="审批已通过。", client_id="approval-approved")
            self.assertFalse(Conversation.objects.filter(system_recipient=recipient).exists())
