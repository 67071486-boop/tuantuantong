import base64
from datetime import timedelta
from html import escape
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_config import update_wxpusher_config
from apps.accounts.wxpusher_notifications import send_notification_batch
from apps.business.roles import SERVICE, WORKER
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

APP_ID = "8442"
DOMAIN = "suqing.suqing.chat"
BASE = "/api/internal-chat/conversations/"


class WxPusherDirectChatTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            TT_CHAT_ORDER_GROUPS_ENABLED=True,
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_APP_TOKEN="synthetic-direct-chat-token",
            TT_CHAT_REALTIME_NOTIFY_URL="",
        ))
        self.sender, self.sender_user = self.make_user("private-sender", SERVICE)
        self.worker, self.worker_user = self.make_user("private-worker", WORKER)
        self.conversation = self.start(self.sender, self.worker_user)

    def make_user(self, username, role):
        client = make_logged_in_client(username=username, groups=(role,), nodes=("my_profile",))
        user = get_user_model().objects.get(username=username)
        WxPusherBinding.objects.create(user=user, app_id=APP_ID, uid=f"UID_{username}")
        return client, user

    def post(self, client, path, data=None):
        response = client.post(path, data or {}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def start(self, client, peer):
        return self.post(client, BASE, {"peer_id": peer.pk})["id"]

    def send(self, client, client_id, *, conversation=None, **extras):
        return self.post(client, f"{BASE}{conversation or self.conversation}/messages/", {
            "text": "一段私密文本", "client_id": client_id, **extras,
        })

    def age_notices(self):
        WxPusherNotice.objects.update(created_at=timezone.now() - timedelta(seconds=46))

    def photo(self):
        buffer = BytesIO()
        Image.new("RGB", (3, 2), color="green").save(buffer, format="PNG")
        return {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}

    def test_unread_direct_messages_merge_by_conversation_and_only_notify_the_recipient(self):
        first = self.send(self.sender, "first")
        self.assertEqual(self.send(self.sender, "first"), first)
        second = self.send(self.sender, "second", reply_to_id=first["id"])
        self.assertEqual(list(WxPusherNotice.objects.values_list("user_id", "kind", "status")), [
            (self.worker_user.pk, "direct_message", "pending"),
            (self.worker_user.pk, "direct_message", "pending"),
        ])
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            send_notification_batch(DOMAIN)
            remote.send.assert_not_called()
            self.age_notices()
            self.assertEqual(send_notification_batch(DOMAIN), {"sent": 2})
            send_notification_batch(DOMAIN)
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        entry = f"https://{DOMAIN}/m/wxpusher/chat/{self.conversation}?message={second['id']}&entry=wxpusher"
        self.assertEqual(args["uid"], "UID_private-worker")
        self.assertEqual(args["summary"], "单聊有新消息")
        self.assertEqual(args["url"], entry)
        self.assertIn(f'<a href="{escape(entry)}">打开单聊</a>', args["content"])
        self.assertIn("2 条", args["content"])
        self.assertNotIn("一段私密文本", args["content"])

    def test_different_conversations_do_not_merge_and_staff_keeps_its_web_entry(self):
        other, _ = self.make_user("private-second-sender", SERVICE)
        another_conversation = self.start(other, self.worker_user)
        self.send(self.sender, "introduce")
        self.send(other, "other-chat", conversation=another_conversation)
        reply = self.send(self.worker, "to-staff")
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            self.assertEqual(send_notification_batch(DOMAIN), {"sent": 3})
            calls = provider.return_value.__enter__.return_value.send.call_args_list
        self.assertCountEqual([(call.kwargs["uid"], call.kwargs["url"].split("?")[0]) for call in calls], [
            ("UID_private-worker", f"https://{DOMAIN}/m/wxpusher/chat/{self.conversation}"),
            ("UID_private-worker", f"https://{DOMAIN}/m/wxpusher/chat/{another_conversation}"),
            ("UID_private-sender", f"https://{DOMAIN}/m/wxpusher/chat/{self.conversation}"),
        ])
        staff_url = next(call.kwargs["url"] for call in calls if call.kwargs["uid"] == "UID_private-sender")
        self.assertTrue(staff_url.endswith(f"?message={reply['id']}&entry=wxpusher&open=web"))

    def test_delivery_rechecks_read_and_revoked_messages_and_opens_the_remaining_album_part(self):
        read = self.send(self.sender, "read")
        self.post(self.worker, f"{BASE}{self.conversation}/read/", {"seq": read["seq"]})
        revoked = self.send(self.sender, "revoked")
        self.post(self.sender, f"{BASE}{self.conversation}/messages/{revoked['id']}/revoke/")
        photo = self.photo()
        album = self.send(self.sender, "album", images=[photo, photo])["messages"]
        self.post(self.sender, f"{BASE}{self.conversation}/messages/{album[0]['id']}/revoke/")
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            self.assertEqual(send_notification_batch(DOMAIN), {"sent": 1, "skipped": 2})
            remote = provider.return_value.__enter__.return_value
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        self.assertTrue(args["url"].endswith(f"?message={album[1]['id']}&entry=wxpusher"))
        self.assertIn("1 条", args["content"])
        self.assertNotIn(photo["data"], args["content"])
        self.assertCountEqual(WxPusherNotice.objects.values_list("status", "skip_reason"), [
            ("sent", ""), ("skipped", "group_already_read"), ("skipped", "group_unavailable"),
        ])

    def test_direct_delivery_rechecks_the_workspace_switch_and_binding(self):
        _, other_user = self.make_user("private-unbound", WORKER)
        other_conversation = self.start(self.sender, other_user)
        self.send(self.sender, "later-unbound", conversation=other_conversation)
        WxPusherBinding.objects.filter(user=other_user).delete()
        self.send(self.sender, "initially-unbound", conversation=other_conversation)
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            self.assertEqual(send_notification_batch(DOMAIN), {"skipped": 1})
            provider.assert_not_called()
        self.assertEqual(list(WxPusherNotice.objects.filter(user=other_user).values_list("skip_reason", flat=True)), [
            "not_bound", "not_bound",
        ])

        self.send(self.sender, "will-disable")
        self.age_notices()
        update_wxpusher_config({"push_enabled": False})
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            self.assertEqual(send_notification_batch(DOMAIN), {"skipped": 1})
            provider.assert_not_called()
        self.assertEqual(WxPusherNotice.objects.get(user=self.worker_user).skip_reason, "workspace_disabled")

    def test_another_workspaces_credentials_cannot_read_a_direct_message_or_send_to_its_recipient(self):
        domain = "wxpusher-direct-other.test.local"
        self.send(self.sender, "local-only", text="本工作室单聊专用正文")
        other, _ = provision_tenant(schema_name="wxpusher_direct_other", name="示例乙", domain=domain)
        try:
            with tenant_context(other):
                foreign = make_logged_in_client(domain=domain, username="foreign-direct-sender")
                messages = f"{BASE}{self.conversation}/messages/"
                read = foreign.get(messages, HTTP_HOST=self.domain.domain)
                self.assertEqual(read.status_code, 401, read.content)
                self.assertNotIn("本工作室单聊专用正文".encode(), read.content)
                sent = foreign.post(
                    messages, {"text": "越界正文", "client_id": "foreign-message"},
                    content_type="application/json", HTTP_HOST=self.domain.domain,
                )
                self.assertEqual(sent.status_code, 401, sent.content)
        finally:
            with tenant_context(other):
                other.delete(force_drop=True)
        self.assertEqual(WxPusherNotice.objects.count(), 1)
