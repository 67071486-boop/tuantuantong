import base64
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_config import update_wxpusher_config
from apps.accounts.wxpusher_notifications import send_notification_batch
from apps.business.models import Game, Order, PeerDownstreamBinding, PeerUpstreamBinding
from apps.business.peer_sync import hash_token, new_token
from apps.business.roles import SERVICE, WORKER
from apps.core.wxpusher import WxPusherError
from apps.internal_chat.models import Conversation, GroupMember, OrderGroup
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

APP_ID = "8442"
ORDER_NO = "20261117000000000075"
WORKER_PREFS = "/api/auth/wxpusher/preferences"
STAFF_PREFS = "/api/auth/wxpusher/staff/preferences"


class WxPusherGroupChatTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            TT_CHAT_ORDER_GROUPS_ENABLED=True,
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_APP_TOKEN="synthetic-group-chat-token",
        ))
        conversation = Conversation.objects.create()
        self.order_group = OrderGroup.objects.create(
            order=Order.objects.create(order_no=ORDER_NO, game=Game.objects.create(name="合成游戏")),
            conversation=conversation, order_no=ORDER_NO, wire_id=uuid4(),
        )
        self.base = f"/api/internal-chat/conversations/{conversation.pk}"
        self.sender, self.sender_member = self.make_member("group-notice-sender", SERVICE)
        self.worker, self.worker_member = self.make_member("group-notice-worker", WORKER)

    def make_member(self, username, role):
        client = make_logged_in_client(username=username, groups=(role,), nodes=("my_profile",))
        user = get_user_model().objects.get(username=username)
        WxPusherBinding.objects.create(user=user, app_id=APP_ID, uid=f"UID_{username}")
        member = GroupMember.objects.create(
            group=self.order_group, wire_key=f"a:{user.pk}", local_user=user,
            name=username, manual_joined=True,
        )
        return client, member

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def send(self, client, client_id, *, base=None, **extras):
        return self.data(client.post(
            (base or self.base) + "/messages/", {"text": "测试私密内容", "client_id": client_id, **extras},
            content_type="application/json",
        ))

    def photo(self):
        buffer = BytesIO()
        Image.new("RGB", (3, 2), color="green").save(buffer, format="PNG")
        return {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}

    def age_notices(self):
        WxPusherNotice.objects.update(created_at=timezone.now() - timedelta(seconds=46))

    def test_ordinary_group_message_notifies_local_members_except_sender(self):
        _, staff_member = self.make_member("group-notice-staff", SERVICE)
        _, departed = self.make_member("group-notice-departed", WORKER)
        departed.left_at = timezone.now()
        departed.save(update_fields=["left_at"])
        GroupMember.objects.create(group=self.order_group, wire_key="b:9", name="对方工作室成员")
        message = self.send(self.sender, "ordinary")
        self.assertEqual(self.send(self.sender, "ordinary"), message)
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            calls = [call.kwargs for call in provider.return_value.__enter__.return_value.send.call_args_list]
        self.assertCountEqual([args["uid"] for args in calls], [
            "UID_group-notice-worker", "UID_group-notice-staff",
        ])
        self.assertCountEqual(WxPusherNotice.objects.values_list("user_id", "kind", "status"), [
            (self.worker_member.local_user_id, "group_message", "sent"),
            (staff_member.local_user_id, "group_message", "sent"),
        ])
        for args in calls:
            self.assertEqual(args["summary"], "订单群有新消息")
            self.assertIn(f"?message={message['id']}&entry=wxpusher", args["url"])
            self.assertIn(ORDER_NO, args["content"])
            self.assertIn("打开群聊", args["content"])
            self.assertNotIn("测试私密内容", args["content"])

    def test_delayed_group_delivery_counts_only_unread_messages_and_opens_the_mention(self):
        first = self.send(self.sender, "already-read")
        self.data(self.worker.post(
            self.base + "/read/", {"seq": first["seq"]}, content_type="application/json",
        ))
        mention = self.send(self.sender, "mention", mention_member_ids=[self.worker_member.pk])
        revoked = self.send(self.sender, "revoked")
        self.data(self.sender.post(self.base + f"/messages/{revoked['id']}/revoke/"))
        self.send(self.sender, "more-recent-ordinary")
        now = timezone.now()
        WxPusherNotice.objects.update(created_at=now - timedelta(seconds=30))
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            send_notification_batch("suqing.suqing.chat")
            remote.send.assert_not_called()
            WxPusherNotice.objects.filter(details__message_id=first["id"]).update(
                created_at=now - timedelta(seconds=46),
            )
            counts = send_notification_batch("suqing.suqing.chat")
            send_notification_batch("suqing.suqing.chat")
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        self.assertEqual(counts, {"sent": 2, "skipped": 2})
        self.assertEqual(args["summary"], "订单群里有人 @ 你")
        self.assertTrue(args["url"].endswith(f"?message={mention['id']}&entry=wxpusher"))
        self.assertIn("2 条", args["content"])
        self.assertNotIn("测试私密内容", args["content"])
        self.assertCountEqual(WxPusherNotice.objects.values_list("status", "skip_reason"), [
            ("skipped", "group_already_read"), ("sent", ""), ("skipped", "group_unavailable"), ("sent", ""),
        ])

    def test_group_reply_batch_does_not_retry_an_unconfirmed_send_or_consume_later_messages(self):
        original = self.send(self.worker, "reply-target")
        self.data(self.sender.post(
            self.base + "/read/", {"seq": original["seq"]}, content_type="application/json",
        ))
        reply = self.send(self.sender, "reply", reply_to_id=original["id"])
        ordinary = self.send(self.sender, "ordinary-with-reply")
        later = self.send(self.sender, "above-the-window")
        now = timezone.now()
        WxPusherNotice.objects.filter(details__message_id=reply["id"]).update(created_at=now - timedelta(seconds=50))
        WxPusherNotice.objects.filter(details__message_id=ordinary["id"]).update(created_at=now - timedelta(seconds=15))
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            remote.send.side_effect = WxPusherError("synthetic timeout")
            counts = send_notification_batch("suqing.suqing.chat")
            self.assertEqual(send_notification_batch("suqing.suqing.chat"), {})
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        self.assertEqual(counts, {"unconfirmed": 2})
        self.assertEqual(args["summary"], "订单群里有人回复你")
        self.assertTrue(args["url"].endswith(f"?message={reply['id']}&entry=wxpusher"))
        self.assertEqual(WxPusherNotice.objects.get(details__message_id=later["id"]).status, "pending")

    def test_group_notice_rechecks_recipient_permissions_before_delivering(self):
        self.send(self.sender, "before-permission-removal", mention_member_ids=[self.worker_member.pk])
        self.worker_member.local_user.groups.clear()
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            counts = send_notification_batch("suqing.suqing.chat")
            provider.assert_not_called()
        self.assertEqual(counts, {"skipped": 1})
        self.assertEqual(WxPusherNotice.objects.get().skip_reason, "recipient_unavailable")

    def test_same_recipient_gets_separate_notifications_for_separate_order_groups(self):
        conversation = Conversation.objects.create()
        second = OrderGroup.objects.create(
            conversation=conversation, order_no="20261117000000000076", wire_id=uuid4(),
            order=Order.objects.create(order_no="20261117000000000076", game=self.order_group.order.game),
        )
        GroupMember.objects.bulk_create([
            GroupMember(group=second, wire_key=member.wire_key, name=member.name,
                        local_user_id=member.local_user_id, manual_joined=True)
            for member in (self.sender_member, self.worker_member)
        ])
        sent_here = self.send(self.sender, "first-group")
        sent_there = self.send(self.sender, "second-group", base=f"/api/internal-chat/conversations/{conversation.pk}")
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            calls = provider.return_value.__enter__.return_value.send.call_args_list
        self.assertCountEqual([call.kwargs["url"] for call in calls], [
            f"https://suqing.suqing.chat/m/wxpusher/chat/{self.order_group.conversation_id}?message={sent_here['id']}&entry=wxpusher",
            f"https://suqing.suqing.chat/m/wxpusher/chat/{conversation.pk}?message={sent_there['id']}&entry=wxpusher",
        ])

    def test_default_group_notice_delivers_once_for_mention_and_reply_without_chat_content(self):
        original = self.send(self.worker, "original")
        self.data(self.sender.post(
            self.base + "/read/", {"seq": original["seq"]}, content_type="application/json",
        ))
        ordinary = self.send(self.sender, "ordinary")
        self.data(self.worker.post(
            self.base + "/read/", {"seq": ordinary["seq"]}, content_type="application/json",
        ))
        params = {"reply_to_id": original["id"], "mention_member_ids": [self.worker_member.pk]}
        message = self.send(self.sender, "reply-with-mention", **params)
        self.assertEqual(self.send(self.sender, "reply-with-mention", **params), message)
        self.assertCountEqual(WxPusherNotice.objects.values_list("user_id", "kind", "status"), [
            (self.sender_member.local_user_id, "group_message", "pending"),
            (self.worker_member.local_user_id, "group_message", "pending"),
            (self.worker_member.local_user_id, "group_mention", "pending"),
        ])
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            provider.assert_not_called()
            self.age_notices()
            send_notification_batch("suqing.suqing.chat")
            send_notification_batch("suqing.suqing.chat")
            remote = provider.return_value.__enter__.return_value
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        self.assertEqual(args["uid"], "UID_group-notice-worker")
        path = f"/m/wxpusher/chat/{self.order_group.conversation_id}?message={message['id']}"
        self.assertEqual(args["url"], "https://suqing.suqing.chat" + path + "&entry=wxpusher")
        self.assertIn("https://suqing.suqing.chat" + path + "&amp;entry=wxpusher", args["content"])
        self.assertIn(ORDER_NO, args["content"])
        self.assertIn("group-notice-sender", args["content"])
        self.assertNotIn("测试私密内容", args["content"])

    def test_group_album_notifies_once_even_after_an_old_client_tries_to_disable_it(self):
        updated = self.data(self.worker.patch(
            WORKER_PREFS, {"group_chat_enabled": False, "order_enabled": False}, content_type="application/json",
        ))
        self.assertIs(updated["group_chat_enabled"], True)
        self.assertIs(updated["order_enabled"], False)
        photo = self.photo()
        album = self.send(self.sender, "ordinary-album", images=[photo, photo])["messages"]
        notice = WxPusherNotice.objects.get()
        self.assertEqual(notice.kind, "group_message")
        self.assertEqual(notice.details["message_id"], album[0]["id"])
        self.assertEqual(notice.status, "pending")
        self.assertNotIn("image", notice.details)
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            provider.return_value.__enter__.return_value.send.assert_called_once()

    def test_album_notice_opens_first_unread_part_for_each_recipient(self):
        self.make_member("group-notice-unread", WORKER)
        reader, _ = self.make_member("group-notice-read", WORKER)
        album = self.send(self.sender, "partly-read-album", images=[self.photo()] * 2)["messages"]
        for client, part in ((self.worker, album[0]), (reader, album[1])):
            self.data(client.post(self.base + "/read/", {"seq": part["seq"]}, content_type="application/json"))
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            calls = [call.kwargs for call in provider.return_value.__enter__.return_value.send.call_args_list]
        expected = {"UID_group-notice-worker": album[1]["id"], "UID_group-notice-unread": album[0]["id"]}
        self.assertCountEqual([args["uid"] for args in calls], expected.keys())
        for args in calls:
            self.assert_album_notice_links(args, expected[args["uid"]])
        self.assertEqual(WxPusherNotice.objects.get(user__username="group-notice-read").skip_reason, "group_already_read")
        self.assertEqual(
            WxPusherNotice.objects.get(user_id=self.worker_member.local_user_id).details["message_id"], album[1]["id"],
        )

    def test_album_notice_uses_surviving_part_and_skips_fully_revoked_album(self):
        partial = self.send(self.sender, "partly-revoked-album", images=[self.photo()] * 2)["messages"]
        revoked = self.send(self.sender, "fully-revoked-album", images=[self.photo()] * 2)["messages"]
        for part in (partial[0], *revoked):
            self.data(self.sender.post(self.base + f"/messages/{part['id']}/revoke/"))
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            remote = provider.return_value.__enter__.return_value
            remote.send.assert_called_once()
            self.assert_album_notice_links(remote.send.call_args.kwargs, partial[1]["id"])
        self.assertEqual(WxPusherNotice.objects.get(status="sent").details["message_id"], partial[1]["id"])
        self.assertEqual(WxPusherNotice.objects.get(status="skipped").skip_reason, "group_unavailable")

    def assert_album_notice_links(self, args, message_id):
        path = f"https://suqing.suqing.chat/m/wxpusher/chat/{self.order_group.conversation_id}?message={message_id}"
        self.assertEqual(args["url"], path + "&entry=wxpusher")
        self.assertIn(path + "&amp;entry=wxpusher", args["content"])

    def test_workspace_push_switch_still_applies_to_group_notices(self):
        mentions = {"mention_member_ids": [self.worker_member.pk]}
        self.send(self.sender, "before-workspace-disabled", **mentions)
        update_wxpusher_config({"push_enabled": False})
        self.send(self.sender, "after-workspace-disabled", **mentions)
        self.assertEqual(list(WxPusherNotice.objects.order_by("pk").values_list("status", "skip_reason")), [
            ("pending", ""), ("skipped", "workspace_disabled"),
        ])
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            provider.assert_not_called()
        self.assertEqual(list(WxPusherNotice.objects.order_by("pk").values_list("status", "skip_reason")), [
            ("skipped", "workspace_disabled"), ("skipped", "workspace_disabled"),
        ])

    def test_staff_reply_and_worker_skip_conditions(self):
        updated = self.data(self.sender.patch(
            STAFF_PREFS, {"group_chat_enabled": False, "enabled": False}, content_type="application/json",
        ))
        self.assertIs(updated["group_chat_enabled"], True)
        self.assertIs(updated["enabled"], False)
        original = self.send(self.sender, "staff-original")
        self_reply = self.send(self.sender, "self-reply", reply_to_id=original["id"])
        self.assertNotEqual(self_reply["id"], original["id"])
        staff_reply = self.send(self.worker, "worker-reply", reply_to_id=original["id"])
        mentions = {"mention_member_ids": [self.worker_member.pk]}
        already_seen = self.send(self.sender, "read-mention", **mentions)
        self.data(self.worker.post(
            self.base + "/read/", {"seq": already_seen["seq"]}, content_type="application/json",
        ))
        revoked = self.send(self.sender, "revoked-mention", **mentions)
        self.data(self.sender.post(self.base + f"/messages/{revoked['id']}/revoke/"))
        departing, departing_member = self.make_member("group-notice-departing", WORKER)
        departing_message = self.send(self.sender, "departing-mention", mention_member_ids=[departing_member.pk])
        self.data(self.worker.post(
            self.base + "/read/", {"seq": departing_message["seq"]}, content_type="application/json",
        ))
        departing_member.left_at = timezone.now()
        departing_member.save(update_fields=["left_at"])
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            remote = provider.return_value.__enter__.return_value
            remote.send.assert_called_once()
            args = remote.send.call_args.kwargs
        self.assertEqual(args["uid"], "UID_group-notice-sender")
        self.assertEqual(args["summary"], "订单群里有人回复你")
        self.assertTrue(args["url"].endswith(f"?message={staff_reply['id']}&entry=wxpusher&open=web"))
        self.assertCountEqual(WxPusherNotice.objects.values_list("kind", "skip_reason"), [
            ("group_reply", ""), ("group_mention", "group_already_read"),
            ("group_mention", "group_unavailable"), ("group_mention", "group_unavailable"),
            ("group_message", "group_already_read"), ("group_message", "group_already_read"),
            ("group_message", "group_already_read"),
        ])

    def test_peer_message_creates_local_notice_only_once_without_replay(self):
        account = get_user_model().objects.create_user(username="synthetic-peer-binding")
        token = new_token()
        binding = PeerDownstreamBinding.objects.create(
            account=account, peer_base_url="https://other.test.invalid",
            token_in_hash=hash_token(token), token_out=new_token(),
        )
        self.order_group.binding_id = binding.pk
        self.order_group.binding_role = "downstream"
        self.order_group.save(update_fields=["binding_id", "binding_role"])
        GroupMember.objects.create(group=self.order_group, wire_key="b:5", name="对方工作室客服")
        client = Client(SERVER_NAME=self.domain.domain)
        payload = {
            "message_id": str(uuid4()), "sender": "b:5", "sender_name": "对方工作室客服",
            "sender_left_at": None, "text": "跨工作室私密聊天", "created_at": timezone.now().isoformat(),
            "revoked": False, "reply_to": None, "mention_keys": [self.worker_member.wire_key],
        }

        def deliver(data, kind="chat.order_message"):
            response = self.data(client.post("/api/business/peer-sync/inbound/events/", {"events": [{
                "kind": kind, "target_ref": str(self.order_group.wire_id),
                "payload": data, "idempotency_key": uuid4().hex,
            }]}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}"))
            self.assertEqual(response["applied"], 1, response)

        deliver({**payload, "message_id": str(uuid4()), "history": True})
        self.assertFalse(WxPusherNotice.objects.exists())
        deliver({**payload, "message_id": str(uuid4()), "revoked": True, "text": ""})
        self.assertFalse(WxPusherNotice.objects.exists())
        deliver(payload)
        deliver(payload)
        self.assertCountEqual(WxPusherNotice.objects.values_list("user_id", "kind"), [
            (self.worker_member.local_user_id, "group_mention"),
            (self.sender_member.local_user_id, "group_message"),
        ])
        notice = WxPusherNotice.objects.get(kind="group_mention")
        self.assertEqual(notice.binding.effective_uid, "UID_group-notice-worker")
        self.assertNotIn("text", notice.details)
        photo = self.photo()
        for head_revoked in (False, True):
            with self.subTest(head_revoked=head_revoked):
                parts = [{**payload, "message_id": str(uuid4()), "text": "", "mention_keys": [], "image": photo}
                         for _ in range(2)]
                if head_revoked:
                    parts[0]["revoked"] = True
                    del parts[0]["image"]
                album = {"album_id": str(uuid4()), "messages": parts}
                deliver(album, "chat.order_album")
                deliver(album, "chat.order_album")
                anchor = self.order_group.conversation.messages.get(wire_id=parts[1 if head_revoked else 0]["message_id"])
                notices = list(WxPusherNotice.objects.filter(reference=f"group-chat:{parts[0]['message_id']}"))
                self.assertCountEqual([(row.user_id, row.kind) for row in notices], [
                    (self.worker_member.local_user_id, "group_message"),
                    (self.sender_member.local_user_id, "group_message"),
                ])
                for row in notices:
                    self.assertEqual(row.details["message_id"], anchor.pk)
                    self.assertNotIn("text", row.details)
                    self.assertNotIn("image", row.details)
        self.assertEqual(WxPusherNotice.objects.count(), 6)

    def test_other_tenant_credential_cannot_send_to_local_group(self):
        domain = "wxpusher-group-other.test.local"
        other, _ = provision_tenant(schema_name="wxpusher_group_other", name="示例乙", domain=domain)
        try:
            with tenant_context(other):
                foreign = make_logged_in_client(domain=domain, username="foreign-group-sender")
                response = foreign.post(self.base + "/messages/", {
                    "text": "不应发送", "client_id": "foreign-message",
                    "mention_member_ids": [self.worker_member.pk],
                }, content_type="application/json", HTTP_HOST=self.domain.domain)
                self.assertEqual(response.status_code, 401, response.content)
        finally:
            with tenant_context(other):
                other.delete(force_drop=True)
        self.assertFalse(WxPusherNotice.objects.exists())

    def test_many_mentions_use_a_bounded_batch_and_preserve_skip_reasons(self):
        with CaptureQueriesContext(connection) as single:
            self.send(self.sender, "single-mention", mention_member_ids=[self.worker_member.pk])
        user_model = get_user_model()
        recipients = user_model.objects.bulk_create([
            user_model(username=f"batch-recipient-{index}") for index in range(49)
        ])
        worker_role = Group.objects.get(name=WORKER)
        group_link = user_model.groups.through
        group_link.objects.bulk_create([group_link(user=user, group=worker_role) for user in recipients])
        members = GroupMember.objects.bulk_create([
            GroupMember(group=self.order_group, wire_key=f"a:{user.pk}", local_user=user, name=user.username)
            for user in recipients
        ])
        WxPusherBinding.objects.bulk_create([
            WxPusherBinding(user=user, app_id=APP_ID, uid=f"UID_{user.username}") for user in recipients[:-2]
        ])
        with CaptureQueriesContext(connection) as batch:
            self.send(
                self.sender, "fifty-mentions",
                mention_member_ids=[self.worker_member.pk, *(member.pk for member in members)],
            )
        self.assertLessEqual(len(batch), len(single) + 2)
        outcomes = WxPusherNotice.objects.exclude(user=self.worker_member.local_user)
        self.assertEqual(outcomes.filter(status="pending").count(), 47)
        self.assertEqual(outcomes.get(user=recipients[-2]).skip_reason, "not_bound")
        self.assertEqual(outcomes.get(user=recipients[-1]).skip_reason, "not_bound")

    def test_push_persistence_error_does_not_undo_chat_message(self):
        with patch.object(WxPusherNotice.objects, "bulk_create", side_effect=DatabaseError("synthetic write error")):
            with self.assertLogs("apps.internal_chat.wxpusher", level="WARNING"):
                message = self.send(self.sender, "push-unavailable", mention_member_ids=[self.worker_member.pk])
        history = self.data(self.worker.get(self.base + "/messages/"))
        self.assertIn(message, history["messages"])
        self.assertFalse(WxPusherNotice.objects.exists())

    def test_closed_upstream_group_is_skipped_but_source_group_stays_local(self):
        binding = PeerUpstreamBinding.objects.create(
            bound_by_id=self.sender_member.local_user_id, peer_name="示例上家",
            peer_base_url="https://upstream.test.invalid", local_base_url=f"https://{self.domain.domain}",
            token_in_hash=hash_token(new_token()), token_out=new_token(),
        )
        self.order_group.binding_role = "upstream"
        self.order_group.binding_id = binding.pk
        self.order_group.save(update_fields=["binding_role", "binding_id"])
        mentions = {"mention_member_ids": [self.worker_member.pk]}
        self.send(self.sender, "will-close-upstream", **mentions)
        binding.enabled = False
        binding.save(update_fields=["enabled"])
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            provider.assert_not_called()
        self.assertEqual(WxPusherNotice.objects.get().skip_reason, "group_unavailable")

        self.order_group.binding_role = "downstream"
        self.order_group.binding_id = None
        self.order_group.save(update_fields=["binding_role", "binding_id"])
        self.send(self.sender, "detached-source-still-local", **mentions)
        self.age_notices()
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch("suqing.suqing.chat")
            provider.return_value.__enter__.return_value.send.assert_called_once()
