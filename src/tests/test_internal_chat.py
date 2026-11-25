import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from tempfile import NamedTemporaryFile
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import signing
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.accounts.models import AppToken
from apps.business.peer_sync import hash_token
from apps.business.roles import WORKER
from apps.internal_chat.models import Conversation, GroupMember, Message, OrderGroup
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

BASE = "/api/internal-chat/"


class InternalChatTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="chat_other", name="另一个工作室", domain="chat-other.test.local",
        )

    def setUp(self):
        self.enterContext(override_settings(DEBUG=True))
        self.alice, self.alice_id = self.make_user("demo-alice")
        self.bob, self.bob_id = self.make_user("demo-bob")
        self.charlie, self.charlie_id = self.make_user("demo-charlie")
        self.conversation = self.post(self.alice, "conversations/", {"peer_id": self.bob_id})
        self.path = f"conversations/{self.conversation['id']}/"

    def make_user(self, username, domain=None):
        client = make_logged_in_client(
            domain=domain or self.domain.domain, username=username, games=None,
        )
        user_id = get_user_model().objects.get(username=username).pk
        return client, user_id

    def read(self, client, path):
        return self.data(client.get(BASE + path))

    def post(self, client, path, payload):
        return self.data(client.post(BASE + path, payload, content_type="application/json"))

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def send(self, client, text, client_id, **kwargs):
        return self.post(client, self.path + "messages/", {
            "text": text, "client_id": client_id, **kwargs,
        })

    @override_settings(DEBUG=False, TT_CHAT_ORDER_GROUPS_ENABLED=False)
    def test_production_chat_endpoints_require_the_rollout_switch(self):
        path = BASE + self.path + "messages/"
        payload = {"text": "生产开关未开启时不可发送", "client_id": "rollout-protected"}
        contacts = self.alice.get(BASE + "contacts/?q=demo-bob")
        message = self.alice.post(path, payload, content_type="application/json")
        for response in (contacts, message):
            body = response.json()
            self.assertEqual((response.status_code, body["code"], body["data"]), (200, 1002, None))
        self.assertFalse(Message.objects.filter(conversation_id=self.conversation["id"]).exists())

        with override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True):
            self.assertEqual(self.read(self.alice, "contacts/?q=demo-bob")["contacts"][0]["id"], self.bob_id)
            sent = self.post(self.alice, self.path + "messages/", payload)
            self.assertEqual(sent["text"], payload["text"])

    def test_contacts_and_direct_conversation_are_scoped_to_the_two_members(self):
        contacts = self.read(self.alice, "contacts/?q=demo-bob")["contacts"]
        self.assertEqual(contacts, [{"id": self.bob_id, "name": "demo-bob", "avatar_url": None}])
        from_bob = self.post(self.bob, "conversations/", {"peer_id": self.alice_id})
        self.assertEqual(self.conversation["id"], from_bob["id"])
        self.assertEqual(from_bob["peer"]["id"], self.alice_id)
        self.assertEqual(self.read(self.charlie, "conversations/")["conversations"], [])
        denied = self.charlie.get(BASE + self.path + "messages/")
        self.assertEqual((denied.status_code, denied.json()["code"], denied.json()["data"]), (200, 1002, None))
        denied = self.charlie.post(BASE + self.path + "messages/", {
            "text": "不应被保存", "client_id": "foreign-send",
        }, content_type="application/json")
        self.assertEqual((denied.status_code, denied.json()["code"]), (200, 1002))
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [])

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_notice_link_opens_its_direct_conversation_for_the_recipient_only(self):
        text = "请查看这条单聊消息"
        message = self.send(self.alice, text, "direct-notice-open")
        path = f"notification-conversations/{self.conversation['id']}/"
        endpoint = BASE + path + "messages/"
        anchor = {"around": message["id"]}

        opened = self.data(self.bob.get(endpoint, anchor))
        self.assertEqual((opened["kind"], opened["peer_name"], opened["self_member_id"]),
                         ("direct", "demo-alice", self.bob_id))
        self.assertEqual(opened["messages"], [message])
        self.assertTrue(opened["can_reply"])
        self.assertEqual(self.post(self.bob, path + "read/", {"seq": message["seq"]}), {"read_seq": message["seq"]})

        outsider = self.charlie.get(endpoint, anchor)
        self.assertEqual((outsider.json()["code"], outsider.json()["data"]), (1002, None))
        with tenant_context(self.other_tenant):
            other, _ = self.make_user("other-notice-viewer", "chat-other.test.local")
            denied = other.get(endpoint, anchor, HTTP_HOST=self.domain.domain)
        self.assertEqual((denied.status_code, denied.json()["data"]), (401, None))

        worker, _ = Group.objects.get_or_create(name=WORKER)
        worker.user_set.add(self.bob_id)
        raw = "direct-notice-native-token"
        AppToken.objects.create(
            user_id=self.bob_id, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5),
        )
        app_url = BASE + "app/" + path + "messages/"
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(self.data(app.get(app_url, anchor))["messages"], [message])
        with tenant_context(self.other_tenant):
            refused = app.get(app_url, anchor, HTTP_HOST=self.other_tenant.get_primary_domain().domain)
        self.assertEqual(refused.status_code, 401)

    @override_settings(
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4191/notify",
        TT_CHAT_REALTIME_SECRET="unit-test-chat",
    )
    def test_realtime_distinguishes_new_private_message_from_read_and_duplicate_send(self):
        with patch("apps.internal_chat.realtime._notify") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                message = self.send(self.alice, "内容不进入提醒", "notify-1")
                self.send(self.alice, "内容不进入提醒", "notify-1")
                self.post(self.bob, self.path + "read/", {"seq": message["seq"]})

        self.assertEqual(notify.call_count, 2)
        sender_change, read_change = (call.args for call in notify.call_args_list)
        self.assertEqual(sender_change[1], [self.alice_id, self.bob_id])
        self.assertEqual(sender_change[4], {
            "id": message["id"], "conversation_id": self.conversation["id"], "author": self.alice_id,
        })
        self.assertEqual(read_change[1], [self.alice_id, self.bob_id])
        self.assertIsNone(read_change[4])

    def test_send_retry_reply_and_history_use_original_message_id(self):
        original = self.send(self.alice, "第一条消息", "send-a")
        retried = self.send(self.alice, "第一条消息", "send-a")
        reply = self.send(self.bob, "第二条，引用第一条", "send-b", reply_to_id=original["id"])
        self.assertEqual(retried, original)
        self.assertEqual(reply["reply_to_id"], original["id"])
        ending = self.read(self.bob, self.path + "messages/")["messages"]
        self.assertEqual([(row["seq"], row["text"]) for row in ending], [
            (1, "第一条消息"), (2, "第二条，引用第一条"),
        ])
        located = self.read(self.bob, self.path + f"messages/?around={original['id']}")["messages"]
        self.assertEqual([row["id"] for row in located], [original["id"], reply["id"]])
        before = self.read(self.bob, self.path + "messages/?before=2")["messages"]
        self.assertEqual(before, [original])

    def test_image_message_has_private_content_and_keeps_only_metadata_in_history(self):
        buffer = BytesIO()
        Image.new("RGB", (12, 7), color="red").save(buffer, format="PNG")
        image = {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
        message = self.send(self.alice, "", "private-image", image=image)
        self.assertEqual(message["image"], {"mime": "image/png", "width": 12, "height": 7})
        self.assertEqual(self.send(self.alice, "", "private-image", image=image), message)
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [message])
        self.assertEqual(self.read(self.bob, "conversations/")["conversations"][0]["last_message"], message)
        self.assertEqual(self.read(self.bob, "sync/")["changes"][-1]["message"], message)
        path = self.path + f"messages/{message['id']}/image/"
        allowed = self.bob.get(BASE + path)
        self.assertEqual(self.data(allowed), image)
        self.assertIn("no-store", allowed["Cache-Control"])
        self.assertIn("private", allowed["Cache-Control"])
        denied = self.charlie.get(BASE + path)
        self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))

        with tenant_context(self.other_tenant):
            foreign, _ = self.make_user("foreign-image-reader", "chat-other.test.local")
            cross_domain = foreign.get(BASE + path, HTTP_HOST=self.domain.domain)
            self.assertEqual((cross_domain.status_code, cross_domain.json()["code"]), (401, 401))

        revoked = self.post(self.alice, self.path + f"messages/{message['id']}/revoke/", {})
        self.assertNotIn("image", revoked)
        denied = self.bob.get(BASE + path)
        self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))

    def test_unsupported_or_invalid_images_do_not_create_messages(self):
        image = Image.new("RGB", (2, 2), color="red")
        still = BytesIO()
        image.save(still, format="PNG")
        animated = BytesIO()
        image.save(
            animated, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (2, 2), color="blue")],
        )
        invalid = [
            ("video/mp4", b"\x00\x00\x00\x18ftypmp42"),
            ("image/gif", b"GIF89a"),
            ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
            ("image/jpeg", still.getvalue()),
            ("image/png", still.getvalue()[:32]),
            ("image/png", animated.getvalue()),
            ("image/png", b"x" * (1024 * 1024 + 1)),
        ]
        for mime, data in invalid:
            with self.subTest(mime=mime, size=len(data)):
                response = self.alice.post(BASE + self.path + "messages/", {
                    "text": "", "client_id": "invalid-image",
                    "image": {"mime": mime, "data": base64.b64encode(data).decode("ascii")},
                }, content_type="application/json")
                self.assertEqual((response.json()["code"], response.json()["data"]), (1000, None))
        malformed = self.alice.post(BASE + self.path + "messages/", {
            "text": "", "client_id": "data-url",
            "image": {"mime": "image/png", "data": "data:image/png;base64,AAAA"},
        }, content_type="application/json")
        self.assertEqual((malformed.json()["code"], malformed.json()["data"]), (1000, None))
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [])

    def test_jpeg_and_webp_send_with_a_caption(self):
        for format_name in ("JPEG", "WEBP"):
            with self.subTest(format_name=format_name):
                mime = f"image/{format_name.lower()}"
                buffer = BytesIO()
                Image.new("RGB", (4, 2), color="blue").save(buffer, format=format_name)
                image = {"mime": mime, "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
                message = self.send(self.alice, "交接图片", f"image-{format_name}", image=image)
                self.assertEqual(message["text"], "交接图片")
                self.assertEqual(message["image"], {"mime": mime, "width": 4, "height": 2})
                self.assertEqual(self.read(self.bob, self.path + f"messages/{message['id']}/image/"), image)

    def test_album_posts_atomically_and_retries_without_changing_the_messages(self):
        images = []
        for size in ((4, 3), (6, 5)):
            buffer = BytesIO()
            Image.new("RGB", size, color="blue").save(buffer, format="PNG")
            images.append({"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")})
        url = BASE + self.path + "messages/"
        payload = {"client_id": "album-B", "text": "一组交接截图", "images": images}
        bad = self.alice.post(url, {**payload, "images": [images[0], {**images[1], "data": "invalid"}]},
                              content_type="application/json")
        self.assertEqual((bad.json()["code"], bad.json()["data"]), (1000, None))
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [])
        album = self.data(self.alice.post(url, payload, content_type="application/json"))["messages"]
        self.assertEqual([message["text"] for message in album], ["一组交接截图", ""])
        self.assertEqual(2, len(album))
        self.assertEqual(album[0]["album_id"], album[1]["album_id"])
        self.assertEqual(self.data(self.alice.post(url, payload, content_type="application/json"))["messages"], album)
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], album)
        for message, photo in zip(album, images, strict=True):
            self.assertEqual(self.read(self.bob, self.path + f"messages/{message['id']}/image/"), photo)
        for invalid in ({**payload, "image": images[0]}, {"client_id": "album-B", "text": "普通消息"}):
            failed = self.alice.post(url, invalid, content_type="application/json")
            self.assertEqual((failed.json()["code"], failed.json()["data"]), (1000, None))
        child_retry = self.alice.post(url, {**payload, "client_id": f"album:{album[0]['album_id'].replace('-', '')}:1"},
                                      content_type="application/json")
        self.assertEqual((child_retry.json()["code"], child_retry.json()["data"]), (1000, None))
        followup = self.send(self.alice, "普通消息", "after-album")
        self.assertEqual(self.read(self.alice, "conversations/")["conversations"][0]["unread_count"], 0)
        self.assertEqual(self.read(self.bob, "conversations/")["conversations"][0]["unread_count"], 2)
        for seq, unread in ((album[0]["seq"], 2), (album[1]["seq"], 1), (followup["seq"], 0)):
            self.assertEqual(self.post(self.bob, self.path + "read/", {"seq": seq}), {"read_seq": seq})
            self.assertEqual(self.read(self.bob, "conversations/")["conversations"][0]["unread_count"], unread)

    def test_history_page_keeps_album_together_at_both_page_edges(self):
        from unittest.mock import patch

        buffer = BytesIO()
        Image.new("RGB", (3, 2), color="green").save(buffer, format="PNG")
        photo = {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
        earliest = self.send(self.alice, "相册前", "before-album")
        album = self.send(self.alice, "交接图", "paged-album", images=[photo] * 3)["messages"]
        latest = self.send(self.alice, "相册后", "after-album")
        path = self.path + "messages/"
        with patch("apps.internal_chat.service.PAGE_SIZE", 3):
            newest_page = self.read(self.bob, path)
            self.assertEqual(newest_page, {"messages": [*album, latest], "has_more": True, "has_newer": False})
            previous_page = self.read(self.bob, path + f"?before={album[0]['seq']}")
            self.assertEqual(previous_page, {"messages": [earliest], "has_more": False, "has_newer": True})
            older_side = self.read(self.bob, path + f"?around={latest['id']}")
            self.assertEqual(older_side, {"messages": [*album, latest], "has_more": True, "has_newer": False})
            newer_side = self.read(self.bob, path + f"?around={earliest['id']}")
            self.assertEqual(newer_side, {"messages": [earliest, *album], "has_more": False, "has_newer": True})

    def test_read_cursor_and_peer_receipt_advance_monotonically(self):
        self.send(self.alice, "待读消息", "send-a")
        before = self.read(self.bob, "conversations/")["conversations"][0]
        self.assertEqual((before["unread_count"], before["read_seq"]), (1, 0))
        self.assertEqual(self.post(self.bob, self.path + "read/", {"seq": 1}), {"read_seq": 1})
        self.assertEqual(self.post(self.bob, self.path + "read/", {"seq": 0}), {"read_seq": 1})
        after = self.read(self.bob, "conversations/")["conversations"][0]
        self.assertEqual((after["unread_count"], after["read_seq"]), (0, 1))
        self.assertEqual(self.read(self.alice, "conversations/")["conversations"][0]["peer_read_seq"], 1)

    def test_manual_unread_is_a_private_reminder_and_rereading_clears_it(self):
        message = self.send(self.alice, "已经读过的消息", "read-before-reminder")
        seq = message["seq"]
        self.post(self.bob, self.path + "read/", {"seq": seq})
        cursor = self.read(self.bob, "conversations/")["cursor"]
        prefs = self.path + "preferences/"
        self.assertEqual(self.post(self.bob, prefs, {"marked_unread": True}), {
            "marked_unread": True, "pinned": False, "remark": "",
        })
        sync = self.read(self.bob, f"sync/?after={cursor}")
        self.assertEqual([change["kind"] for change in sync["changes"]], ["conversation.preferences_changed"])
        self.assertEqual(self.read(self.alice, f"sync/?after={cursor}")["changes"], [])

        marked = self.read(self.bob, "conversations/")["conversations"][0]
        sender = self.read(self.alice, "conversations/")["conversations"][0]
        self.assertEqual((marked["marked_unread"], marked["unread_count"], marked["read_seq"]), (True, 0, seq))
        self.assertEqual((sender["marked_unread"], sender["peer_read_seq"]), (False, seq))

        self.assertEqual(self.post(self.bob, self.path + "read/", {"seq": seq}), {"read_seq": seq})
        reopened = self.read(self.bob, "conversations/")["conversations"][0]
        self.assertEqual((reopened["marked_unread"], reopened["unread_count"], reopened["read_seq"]), (False, 0, seq))
        cleared = self.read(self.bob, f"sync/?after={sync['cursor']}")["changes"]
        self.assertEqual([change["kind"] for change in cleared], ["conversation.preferences_changed"])

    def test_empty_conversation_can_be_marked_unread_and_cleared_at_sequence_zero(self):
        prefs = self.path + "preferences/"
        self.assertTrue(self.post(self.bob, prefs, {"marked_unread": True})["marked_unread"])
        self.assertEqual(self.post(self.bob, self.path + "read/", {"seq": 0}), {"read_seq": 0})
        summary = self.read(self.bob, "conversations/")["conversations"][0]
        self.assertEqual((summary["marked_unread"], summary["read_seq"], summary["last_message"]), (False, 0, None))

    def test_manual_unread_uses_authenticated_workspace_and_conversation(self):
        self.send(self.alice, "当前工作室待处理的会话", "reminder-home")
        prefs = BASE + self.path + "preferences/"
        invalid = self.bob.post(prefs, {"marked_unread": "无效标记"}, content_type="application/json")
        self.assertEqual((invalid.json()["code"], invalid.json()["data"]), (1000, None))
        denied = self.charlie.post(prefs, {"marked_unread": True}, content_type="application/json")
        self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))

        with tenant_context(self.other_tenant):
            foreign, _ = self.make_user("other-reminder-owner", "chat-other.test.local")
            _, other_peer_id = self.make_user("other-reminder-peer", "chat-other.test.local")
            other_conversation = self.post(foreign, "conversations/", {"peer_id": other_peer_id})
            other_path = f"conversations/{other_conversation['id']}/"
            self.post(foreign, other_path + "messages/", {"text": "另一个工作室消息", "client_id": "reminder-other"})
            denied = foreign.post(
                prefs, {"marked_unread": True}, content_type="application/json", HTTP_HOST=self.domain.domain,
            )
            self.assertEqual((denied.status_code, denied.json()["code"]), (401, 401))
            self.data(foreign.post("/api/auth/login", {
                "username": "other-reminder-owner", "password": P2_TEST_PASSWORD,
            }, content_type="application/json"))
            owner_summary = self.read(foreign, "conversations/")["conversations"][0]
            self.assertEqual(owner_summary["last_message"]["text"], "另一个工作室消息")
            self.assertIs(owner_summary["marked_unread"], False)

        summary = self.read(self.bob, "conversations/")["conversations"][0]
        self.assertEqual(summary["last_message"]["text"], "当前工作室待处理的会话")
        self.assertEqual((summary["marked_unread"], summary["unread_count"]), (False, 1))

    def test_search_matches_literal_text_in_the_current_conversation(self):
        term = "工单abc%_"
        older = self.send(self.alice, "跟进工单ABC%_：已取号", "matching-old")
        self.send(self.bob, "跟进工单ABC-：已取号", "not-a-literal-match")
        latest = self.send(self.bob, "工单aBc%_已完成", "matching-new")
        revoked = self.send(self.alice, "工单abc%_待撤回", "matching-revoked")
        self.post(self.alice, self.path + f"messages/{revoked['id']}/revoke/", {})
        other = self.post(self.charlie, "conversations/", {"peer_id": self.alice_id})
        self.post(self.alice, f"conversations/{other['id']}/messages/", {
            "text": "别的会话工单abc%_", "client_id": "matching-elsewhere",
        })

        endpoint = BASE + self.path + "messages/search/"
        result = self.data(self.bob.get(endpoint, {"q": f"  {term}  "}))
        self.assertEqual(result, {"messages": [latest, older], "count": 2, "has_more": False})
        denied = self.charlie.get(endpoint, {"q": term})
        self.assertEqual((denied.status_code, denied.json()["code"], denied.json()["data"]), (200, 1002, None))

    def test_search_pages_by_message_sequence_with_a_total_count(self):
        for seq in range(1, 33):
            self.send(self.alice, f"验收工单 {seq}", f"search-{seq}")
        endpoint = BASE + self.path + "messages/search/"
        first = self.data(self.bob.get(endpoint, {"q": "验收"}))
        self.assertEqual([row["seq"] for row in first["messages"]], list(range(32, 2, -1)))
        self.assertEqual((first["count"], first["has_more"]), (32, True))
        second = self.data(self.bob.get(endpoint, {"q": "验收", "before": first["messages"][-1]["seq"]}))
        self.assertEqual([row["seq"] for row in second["messages"]], [2, 1])
        self.assertEqual((second["count"], second["has_more"]), (32, False))
        self.assertEqual(
            self.data(self.bob.get(endpoint, {"q": "验收", "before": 1})),
            {"messages": [], "count": 32, "has_more": False},
        )

    def test_search_rejects_invalid_parameters(self):
        endpoint = BASE + self.path + "messages/search/"
        invalid = (
            {}, {"q": "  "}, {"q": "字" * 101}, {"q": "验收", "before": 0},
            {"before": 1}, {"image_only": False}, {"sender_id": 0},
            {"date_from": 12, "date_to": 11}, {"date_to": 2**63 - 1},
        )
        for params in invalid:
            with self.subTest(params=params):
                response = self.bob.get(endpoint, params)
                self.assertEqual(
                    (response.status_code, response.json()["code"], response.json()["data"]),
                    (200, 1000, None),
                )

    def test_photo_search_includes_old_single_images_and_combines_search_filters(self):
        buffer = BytesIO()
        Image.new("RGB", (3, 2), color="blue").save(buffer, format="PNG")
        image = {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
        day_end = datetime(2025, 4, 2, 23, 59, 59, tzinfo=UTC)
        older = self.send(self.alice, "截图 已交接", "old-image", image=image)
        keep = self.send(self.alice, "截图 已验收", "keep-image", image=image)
        other_author = self.send(self.bob, "截图 补充说明", "bob-image", image=image)
        next_day = self.send(self.alice, "截图 次日补充", "next-image", image=image)
        text_only = self.send(self.alice, "截图稍后发", "plain-text")
        revoked = self.send(self.alice, "截图 已撤回", "revoked-image", image=image)
        self.post(self.alice, self.path + f"messages/{revoked['id']}/revoke/", {})

        Message.objects.filter(pk__in=[keep["id"], other_author["id"], text_only["id"], revoked["id"]]).update(
            created_at=day_end + timedelta(microseconds=999999),
        )
        Message.objects.filter(pk=older["id"]).update(created_at=day_end - timedelta(days=2))
        Message.objects.filter(pk=next_day["id"]).update(created_at=day_end + timedelta(seconds=1))

        endpoint = BASE + self.path + "messages/search/"
        gallery = self.data(self.bob.get(endpoint, {"image_only": "1"}))
        self.assertEqual([row["id"] for row in gallery["messages"]], [
            next_day["id"], other_author["id"], keep["id"], older["id"],
        ])
        self.assertTrue(all(row["image"] == keep["image"] for row in gallery["messages"]))
        self.assertNotIn(image["data"], json.dumps(gallery))
        params = {
            "q": "截图", "image_only": "1", "sender_id": self.alice_id,
            "date_from": int(day_end.timestamp()), "date_to": int(day_end.timestamp()),
        }
        result = self.data(self.bob.get(endpoint, params))
        self.assertEqual([row["id"] for row in result["messages"]], [keep["id"]])
        self.assertEqual((result["count"], result["has_more"]), (1, False))
        self.assertEqual(self.data(self.bob.get(endpoint, {**params, "before": keep["seq"]})), {
            "messages": [], "count": 1, "has_more": False,
        })

        with tenant_context(self.other_tenant):
            foreign_user, _ = self.make_user("other-photo-searcher", "chat-other.test.local")
            denied = foreign_user.get(endpoint, {"image_only": "1"}, HTTP_HOST=self.domain.domain)
            self.assertEqual((denied.status_code, denied.json()["code"], denied.json()["data"]), (401, 401, None))

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_group_search_uses_group_member_ids_and_excludes_nonmembers(self):
        conversation = Conversation.objects.create()
        group = OrderGroup.objects.create(
            conversation=conversation, wire_id=uuid4(), order_no="20261116000100009001",
        )
        author = GroupMember.objects.create(
            pk=801, group=group, wire_key="test-alice", name="测试作者", local_user_id=self.alice_id,
        )
        GroupMember.objects.create(
            pk=802, group=group, wire_key="test-bob", name="测试查看人", local_user_id=self.bob_id,
        )
        path = f"conversations/{conversation.id}/messages/"
        sent = self.post(self.alice, path, {"text": "测试群消息", "client_id": "group-search"})
        endpoint = BASE + path + "search/"
        result = self.data(self.bob.get(endpoint, {"sender_id": author.pk}))
        self.assertEqual(result, {"messages": [sent], "count": 1, "has_more": False})
        self.assertEqual(self.data(self.bob.get(endpoint, {"sender_id": self.alice_id})), {
            "messages": [], "count": 0, "has_more": False,
        })
        denied = self.charlie.get(endpoint, {"sender_id": author.pk})
        self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_SECRET="local-test-signing-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4181/notify",
    )
    def test_app_link_opens_only_the_workers_own_group_and_message(self):
        conversation = Conversation.objects.create()
        group = OrderGroup.objects.create(
            conversation=conversation, wire_id=uuid4(), order_no="20261116000100009002",
        )
        GroupMember.objects.create(group=group, wire_key="sender", name="消息作者", local_user_id=self.alice_id)
        member = GroupMember.objects.create(group=group, wire_key="worker", name="代练员", local_user_id=self.bob_id)
        worker_role, _ = Group.objects.get_or_create(name=WORKER)
        worker_role.user_set.add(self.bob_id, self.charlie_id)
        web_path = f"conversations/{conversation.pk}/messages/"
        message = self.post(self.alice, web_path, {"text": "请看这条消息", "client_id": "native-open"})
        endpoint = BASE + f"app/order-groups/{conversation.pk}/messages/"
        url = endpoint + f"?around={message['id']}"

        def bearer_for(user_id, raw):
            AppToken.objects.create(
                user_id=user_id, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(days=1),
            )
            return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

        app = Client(SERVER_NAME=self.domain.domain)
        bearer = bearer_for(self.bob_id, "native-order-group-member")
        opened = self.data(app.get(url, **bearer))
        self.assertEqual(opened["order_no"], "20261116000100009002")
        self.assertEqual(opened["messages"], [message])
        self.assertEqual(opened["self_member_id"], member.pk)
        self.assertEqual(self.bob.get(url).status_code, 401)
        outsider = bearer_for(self.charlie_id, "native-non-member")
        self.assertEqual(app.get(url, **outsider).json()["code"], 1002)
        self.assertEqual(app.get(url, **bearer_for(self.alice_id, "native-non-worker")).json()["code"], 1002)
        other_domain = self.other_tenant.get_primary_domain().domain
        self.assertEqual(app.get(url, HTTP_HOST=other_domain, **bearer).status_code, 401)
        self.assertEqual(app.get(endpoint + "?around=9223372036854775808", **bearer).json()["code"], 1000)
        read_url = endpoint.removesuffix("messages/") + "read/"
        self.assertEqual(self.data(app.post(
            read_url, {"seq": message["seq"]}, content_type="application/json", **bearer,
        )), {"read_seq": message["seq"]})

        replied = self.data(app.post(endpoint, {
            "text": "我收到了", "client_id": "native-order-reply", "reply_to_id": message["id"],
        }, content_type="application/json", **bearer))
        self.assertEqual(replied["reply_to_id"], message["id"])
        self.assertEqual(self.read(self.alice, web_path)["messages"], [message, replied])

        extension = endpoint.replace("/app/order-groups/", "/order-groups/")
        self.assertEqual(self.data(self.alice.get(extension + f"?around={message['id']}"))["messages"], [message, replied])
        self.assertEqual(self.charlie.get(extension).json()["code"], 1002)
        self.assertEqual(app.get(extension, **bearer).status_code, 401)
        badge = BASE + "app/unread/"
        self.assertEqual(self.data(app.get(badge, **bearer)), {"unread_count": 0})
        thumbnail = BytesIO()
        Image.new("RGB", (4, 3), color="blue").save(thumbnail, format="PNG")
        image = {"mime": "image/png", "data": base64.b64encode(thumbnail.getvalue()).decode("ascii")}
        album = self.post(self.alice, web_path, {
            "client_id": "native-group-unread-album", "text": "两张截图", "images": [image, image],
        })["messages"]
        self.assertEqual(self.data(app.get(badge, **bearer)), {"unread_count": 1})
        self.assertEqual(self.data(app.get(badge, **outsider)), {"unread_count": 0})
        for index, picture in enumerate(album):
            self.post(self.alice, web_path + f"{picture['id']}/revoke/", {})
            self.assertEqual(self.data(app.get(badge, **bearer)), {"unread_count": 1 - index})
        self.post(self.alice, web_path, {"text": "离群前未读", "client_id": "native-group-before-leaving"})
        self.assertEqual(self.data(app.get(badge, **bearer)), {"unread_count": 1})

        group.binding_role = "upstream"
        group.save(update_fields=["binding_role"])
        self.assertFalse(self.data(self.alice.get(extension))["can_reply"])
        reply = {"text": "这里不再允许回复", "client_id": "closed-group-reply"}
        refused = self.alice.post(extension, reply, content_type="application/json")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(app.post(endpoint, reply, content_type="application/json", **bearer).status_code, 403)

        member.left_at = timezone.now()
        member.save(update_fields=["left_at"])
        self.assertEqual(app.get(url, **bearer).json()["code"], 1002)
        self.assertEqual(self.data(app.get(badge, **bearer)), {"unread_count": 0})

    def test_offline_sync_returns_old_revoke_by_id_without_original_text(self):
        original = self.send(self.alice, "待撤回的原文", "send-a")
        reply = self.send(self.bob, "引用后还可以读本消息", "send-b", reply_to_id=original["id"])
        cursor = self.read(self.bob, "conversations/")["cursor"]
        path = self.path + f"messages/{original['id']}/revoke/"
        denied = self.bob.post(BASE + path, {}, content_type="application/json")
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, 403))
        revoked = self.post(self.alice, path, {})
        self.assertEqual((revoked["id"], revoked["revoked"], revoked["text"]), (original["id"], True, ""))
        self.assertEqual(self.post(self.alice, path, {}), revoked)
        recovered = self.read(self.bob, f"sync/?after={cursor}")
        self.assertEqual(recovered["changes"], [{
            "kind": "message.revoked", "conversation_id": self.conversation["id"], "message": revoked,
        }])
        self.assertGreater(recovered["cursor"], cursor)
        self.assertNotIn("待撤回的原文", json.dumps(self.read(self.bob, "sync/"), ensure_ascii=False))
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [revoked, reply])
        self.assertEqual(self.read(self.charlie, "sync/")["changes"], [])

    def test_another_workspace_has_its_own_contacts_messages_and_change_log(self):
        self.send(self.alice, "当前工作室验收消息", "send-a")
        with tenant_context(self.other_tenant):
            foreign_alice, _ = self.make_user("other-demo-alice", "chat-other.test.local")
            foreign_bob, foreign_id = self.make_user("other-demo-bob", "chat-other.test.local")
            started = self.post(foreign_alice, "conversations/", {"peer_id": foreign_id})
            foreign_path = f"conversations/{started['id']}/messages/"
            self.post(foreign_alice, foreign_path, {"text": "另一个工作室消息", "client_id": "send-a"})
            self.assertEqual(
                [row["text"] for row in self.read(foreign_bob, foreign_path)["messages"]],
                ["另一个工作室消息"],
            )
            self.assertNotIn("当前工作室验收消息", json.dumps(self.read(foreign_bob, "sync/"), ensure_ascii=False))
            self.assertEqual(self.read(foreign_bob, "contacts/?q=demo-bob")["contacts"], [])
            search_endpoint = BASE + foreign_path + "search/"
            search = self.data(foreign_bob.get(search_endpoint, {"q": "工作室"}))
            self.assertEqual([row["text"] for row in search["messages"]], ["另一个工作室消息"])
            wrong_host = foreign_alice.get(BASE + self.path + "messages/", HTTP_HOST=self.domain.domain)
            self.assertEqual((wrong_host.status_code, wrong_host.json()["code"]), (401, 401))
            wrong_host = foreign_bob.get(BASE + self.path + "messages/search/", {"q": "工作室"}, HTTP_HOST=self.domain.domain)
            self.assertEqual((wrong_host.status_code, wrong_host.json()["code"]), (401, 401))
        self.assertNotIn("另一个工作室消息", json.dumps(self.read(self.bob, "sync/"), ensure_ascii=False))
        search = self.data(self.bob.get(BASE + self.path + "messages/search/", {"q": "工作室"}))
        self.assertEqual([row["text"] for row in search["messages"]], ["当前工作室验收消息"])

    def test_existing_deliverer_app_bearer_cannot_enter_web_chat(self):
        raw = "local-test-deliverer-app-token"
        AppToken.objects.create(
            user_id=self.alice_id, key_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        client = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.data(client.get("/api/auth/me"))
        denied = client.get(BASE + "contacts/")
        self.assertEqual((denied.status_code, denied.json()["code"]), (401, 401))

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_SECRET="local-test-signing-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4181/notify",
    )
    @patch("apps.internal_chat.realtime._notify")
    def test_native_grant_only_authorizes_chat_for_the_current_worker_workspace(self, _notify):
        user = get_user_model().objects.get(pk=self.alice_id)
        user.groups.add(Group.objects.get_or_create(name=WORKER)[0])
        raw = "local-app-token-for-chat"
        token = AppToken.objects.create(
            user=user, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5),
        )
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertTrue(self.data(app.get("/api/app/auth/me/"))["user"]["internal_chat_enabled"])
        self.assertTrue(self.data(self.alice.get("/api/auth/me"))["user"]["internal_chat_enabled"])
        with override_settings(TT_CHAT_REALTIME_NOTIFY_URL=""):
            self.assertNotIn("internal_chat_enabled", self.data(app.get("/api/app/auth/me/"))["user"])
            self.assertEqual(app.post(BASE + "app/access/").json()["code"], 1002)

        exchange = app.post(BASE + "app/access/")
        self.assertIn("no-store", exchange.headers["Cache-Control"])
        access = self.data(exchange)
        self.assertIs(access["close_in_webview"], True)
        self.assertIs(access["retain_webview"], True)
        grant = access["grant"]
        chat = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Chat {grant}")
        me = self.read(chat, "app/me/")["user"]
        self.assertEqual(me["id"], self.alice_id)
        self.assertEqual(me["identity_plane"], self.tenant.schema_name)
        self.assertEqual(chat.get("/api/auth/me").status_code, 401)
        self.assertEqual(chat.post("/api/app/auth/logout/").status_code, 401)
        self.assertEqual(chat.get(BASE + "settings/").status_code, 401)
        self.assertEqual(self.read(chat, "conversations/")["conversations"][0]["id"], self.conversation["id"])
        self.assertEqual(self.post(chat, "realtime-ticket/", {})["user"]["id"], self.alice_id)
        received = self.send(self.bob, "电脑端聊天消息", "browser-chat")
        mine = self.send(chat, "App 聊天消息", "app-chat")
        self.assertEqual(self.read(self.bob, self.path + "messages/")["messages"], [received, mine])

        other_host = self.other_tenant.get_primary_domain().domain
        self.assertEqual(chat.get(BASE + "conversations/", HTTP_HOST=other_host).status_code, 401)
        self.assertEqual(chat.get(BASE + "app/me/", HTTP_HOST=other_host).status_code, 401)
        with patch("apps.internal_chat.app_access._GRANT_LIFETIME", -1):
            self.assertEqual(chat.get(BASE + "conversations/").status_code, 401)
        user.groups.remove(Group.objects.get(name=WORKER))
        self.assertEqual(chat.get(BASE + "conversations/").status_code, 401)
        user.groups.add(Group.objects.get(name=WORKER))
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        self.assertEqual(chat.get(BASE + "conversations/").status_code, 401)
        self.assertEqual(chat.post(BASE + "app/refresh/").status_code, 401)

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_SECRET="local-test-signing-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4181/notify",
        APP_ANDROID_MIN_VERSION_CODE=0,
    )
    def test_minimum_version_follows_app_chat_grants_into_the_embedded_browser(self):
        user = get_user_model().objects.get(pk=self.alice_id)
        user.groups.add(Group.objects.get_or_create(name=WORKER)[0])
        raw = "local-app-version-test-token"
        token = AppToken.objects.create(
            user=user, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5),
        )
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        android = {"HTTP_X_TT_APP_PLATFORM": "android", "HTTP_USER_AGENT": "uni-app-x/5.26"}
        older = self.data(app.post(BASE + "app/access/", HTTP_X_TT_APP_VERSION_CODE="110", **android))["grant"]
        current = self.data(app.post(BASE + "app/access/", HTTP_X_TT_APP_VERSION_CODE="111", **android))["grant"]
        ios = self.data(app.post(BASE + "app/access/", HTTP_X_TT_APP_PLATFORM="ios"))["grant"]
        legacy = signing.dumps(
            [self.tenant.schema_name, token.pk, token.created_at.isoformat(), int(timezone.now().timestamp()) + 3600],
            salt="internal-chat-app-access",
        )

        webview = Client(SERVER_NAME=self.domain.domain, HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14; wv)")
        self.assertEqual(self.data(webview.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {legacy}"))["user"]["id"], user.pk)
        with override_settings(APP_ANDROID_MIN_VERSION_CODE=111):
            self.assertEqual(app.get(BASE + "app/unread/", HTTP_USER_AGENT="uni-app-x/5.26").status_code, 426)
            for grant in (older, legacy):
                blocked = webview.get(BASE + "conversations/", HTTP_AUTHORIZATION=f"Chat {grant}",
                                      HTTP_X_TT_APP_VERSION_CODE="999")
                self.assertEqual(blocked.status_code, 426)
                self.assertEqual(blocked.json()["data"]["minimum_version_code"], 111)

            refreshed = self.data(webview.post(BASE + "app/refresh/", HTTP_AUTHORIZATION=f"Chat {current}"))["grant"]
            self.assertEqual(self.data(webview.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {refreshed}"))["user"]["id"], user.pk)
            ios_chat = webview.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {ios}", HTTP_USER_AGENT="uni-app-x/5.26")
            self.assertEqual(self.data(ios_chat)["user"]["id"], user.pk)
            self.assertEqual(self.read(self.alice, "conversations/")["conversations"][0]["id"], self.conversation["id"])

            other_host = self.other_tenant.get_primary_domain().domain
            with tenant_context(self.other_tenant):
                foreign_grant = webview.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {refreshed}", HTTP_HOST=other_host)
                self.assertEqual(foreign_grant.status_code, 401)
                foreign_token = app.get("/api/app/auth/me/", HTTP_X_TT_APP_VERSION_CODE="111", HTTP_HOST=other_host, **android)
                self.assertEqual(foreign_token.status_code, 401)

        with override_settings(APP_ANDROID_MIN_VERSION_CODE=112):
            self.assertEqual(webview.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {refreshed}").status_code, 426)

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_SECRET="local-test-signing-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4181/notify",
    )
    def test_native_grant_can_refresh_only_while_app_session_is_valid(self):
        from apps.internal_chat.app_access import _GRANT_LIFETIME, _GRANT_MAX_SESSION

        user = get_user_model().objects.get(pk=self.alice_id)
        user.groups.add(Group.objects.get_or_create(name=WORKER)[0])
        raw = "local-app-token-to-refresh-chat"
        AppToken.objects.create(
            user=user, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5),
        )
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        start = 1_900_000_000
        with patch("django.core.signing.time.time", return_value=start):
            grant = self.post(app, "app/access/", {})["grant"]
        chat = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Chat {grant}")
        with patch("django.core.signing.time.time", return_value=start + _GRANT_LIFETIME - 1):
            refreshed = self.post(chat, "app/refresh/", {})["grant"]
        with patch("django.core.signing.time.time", return_value=start + _GRANT_LIFETIME + 1):
            self.assertEqual(chat.post(BASE + "app/refresh/").status_code, 401)
            response = chat.get(BASE + "app/me/", HTTP_AUTHORIZATION=f"Chat {refreshed}")
            self.assertEqual(self.data(response)["user"]["id"], user.pk)
        # Refreshing every half hour cannot move the original handoff's deadline.
        for step in range(_GRANT_LIFETIME + 2, _GRANT_MAX_SESSION, _GRANT_LIFETIME // 2):
            with patch("django.core.signing.time.time", return_value=start + step):
                response = chat.post(BASE + "app/refresh/", HTTP_AUTHORIZATION=f"Chat {refreshed}")
                refreshed = self.data(response)["grant"]
        with patch("django.core.signing.time.time", return_value=start + _GRANT_MAX_SESSION):
            response = chat.post(BASE + "app/refresh/", HTTP_AUTHORIZATION=f"Chat {refreshed}")
            self.assertEqual(response.status_code, 401)

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_SECRET="local-test-signing-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4181/notify",
    )
    def test_app_badge_counts_unread_and_keeps_the_subscription_tenant_bound(self):
        user = get_user_model().objects.get(pk=self.alice_id)
        role = Group.objects.get_or_create(name=WORKER)[0]
        user.groups.add(role)
        raw = "local-app-unread-token"
        AppToken.objects.create(
            user=user, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5),
        )
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        path = "app/unread/"
        self.assertEqual(self.read(app, path), {"unread_count": 0})
        ticket = self.post(app, path, {})["token"]
        encoded, _ = ticket.split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual((claims["tenant"], claims["user"]), (self.tenant.schema_name, self.alice_id))

        self.send(self.bob, "第一条", "app-unread-first")
        self.send(self.alice, "自己的消息", "app-unread-self")
        self.assertEqual(self.read(app, path), {"unread_count": 1})
        latest = self.send(self.bob, "第二条", "app-unread-second")
        self.assertEqual(self.read(app, path), {"unread_count": 2})
        self.post(self.alice, self.path + "read/", {"seq": latest["seq"]})
        self.assertEqual(self.read(app, path), {"unread_count": 0})
        self.post(self.alice, self.path + "preferences/", {"marked_unread": True})
        self.assertEqual(self.read(app, path), {"unread_count": 1})
        second = self.post(self.charlie, "conversations/", {"peer_id": self.alice_id})
        self.post(self.charlie, f"conversations/{second['id']}/messages/", {
            "text": "另一段会话", "client_id": "app-unread-other-thread",
        })
        self.assertEqual(self.read(app, path), {"unread_count": 2})

        other_host = self.other_tenant.get_primary_domain().domain
        self.assertEqual(app.get(BASE + path, HTTP_HOST=other_host).status_code, 401)
        self.assertEqual(app.post(BASE + path, HTTP_HOST=other_host).status_code, 401)
        with tenant_context(self.tenant):
            user.groups.remove(role)
        self.assertEqual(app.get(BASE + path).json()["code"], 1002)
        with tenant_context(self.tenant):
            user.groups.add(role)
        with override_settings(TT_CHAT_REALTIME_NOTIFY_URL=""):
            self.assertEqual(app.post(BASE + path).json()["code"], 1002)

    @override_settings(TT_CHAT_REALTIME_SECRET="local-test-signing-key")
    def test_realtime_ticket_signs_current_workspace_and_user(self):
        ticket = self.post(self.alice, "realtime-ticket/", {})
        self.assertEqual(ticket["user"], {"id": self.alice_id, "identity_plane": self.tenant.schema_name})
        payload, signature = ticket["token"].encode().split(b".")
        digest = hmac.digest(b"local-test-signing-key", payload, hashlib.sha256)
        self.assertEqual(signature, base64.urlsafe_b64encode(digest).rstrip(b"="))
        claims = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
        self.assertEqual((claims["tenant"], claims["user"]), (self.tenant.schema_name, self.alice_id))
        self.assertIsInstance(claims["exp"], int)

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_TENANT_KEYS=True,
        TT_CHAT_REALTIME_SECRET="legacy-global-key",
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4191/notify",
    )
    def test_realtime_ticket_and_notification_use_the_current_workspace_key(self):
        from apps.internal_chat.authority_signals import invalidate_users

        studio_a = self.tenant.schema_name
        studio_b = self.other_tenant.schema_name
        keys = {
            studio_a: "synthetic-realtime-credential-studio-a",
            studio_b: "synthetic-realtime-credential-studio-b",
        }
        other_host = self.other_tenant.get_primary_domain().domain
        with tenant_context(self.other_tenant):
            bob, bob_id = self.make_user("realtime-foreign-user", other_host)

        with NamedTemporaryFile(mode="w+", encoding="utf-8") as config:
            tenants = {schema: {"key_id": schema, "secret": f"bridge-{schema}", "realtime_secret": key}
                       for schema, key in keys.items()}
            json.dump({"service_url": "https://bridge.test", "tenants": tenants}, config)
            config.flush()
            with override_settings(TT_CHAT_SERVICE_CONFIG_FILE=config.name):
                for current, actor, user_id in ((self.tenant, self.alice, self.alice_id),
                                                (self.other_tenant, bob, bob_id)):
                    token = self.post(actor, "realtime-ticket/", {})["token"]
                    payload, signature = token.encode().split(b".")
                    key = keys[current.schema_name]
                    digest = hmac.digest(key.encode(), payload, hashlib.sha256)
                    self.assertEqual(signature, base64.urlsafe_b64encode(digest).rstrip(b"="))
                    with (tenant_context(current), patch("apps.internal_chat.realtime._notify") as notify,
                          self.captureOnCommitCallbacks(execute=True)):
                        invalidate_users([user_id])
                    notify.assert_called_once_with(current.schema_name, [user_id],
                                                   "http://127.0.0.1:4191/notify", key)

                tenants[studio_a].pop("realtime_secret")
                config.seek(0)
                config.truncate()
                json.dump({"service_url": "https://bridge.test", "tenants": tenants}, config)
                config.flush()
                response = self.alice.post(BASE + "realtime-ticket/", {})
                self.assertEqual(response.status_code, 503)
                with (patch("apps.internal_chat.realtime._notify") as notify,
                      self.captureOnCommitCallbacks(execute=True)):
                    invalidate_users([self.alice_id])
                notify.assert_not_called()

    @override_settings(
        TT_CHAT_ORDER_GROUPS_ENABLED=True,
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4191/notify",
        TT_CHAT_REALTIME_SECRET="local-test-presence-key",
    )
    def test_presence_ticket_is_signed_for_an_existing_private_conversation_only(self):
        path = self.path + "presence-ticket/"
        response = self.alice.post(BASE + path, {}, content_type="application/json")
        payload, signature = self.data(response)["token"].encode().split(b".")
        self.assertIn("no-store", response.headers["Cache-Control"])
        claims = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
        self.assertEqual(
            {key: value for key, value in claims.items() if key != "exp"},
            {"tenant": self.tenant.schema_name, "user": self.alice_id, "purpose": "presence",
             "peer": self.bob_id, "conversation": self.conversation["id"]},
        )
        digest = hmac.digest(b"local-test-presence-key", payload, hashlib.sha256)
        self.assertEqual(signature, base64.urlsafe_b64encode(digest).rstrip(b"="))

        self.assertEqual(self.charlie.post(BASE + path, {}).json()["code"], 1002)
        group_conversation = Conversation.objects.create()
        order_group = OrderGroup.objects.create(
            conversation=group_conversation, wire_id=uuid4(), order_no="20261118000000000002",
        )
        GroupMember.objects.create(
            group=order_group, local_user_id=self.alice_id, wire_key="test-presence-member", name="合成成员",
        )
        group_path = f"conversations/{group_conversation.pk}/presence-ticket/"
        self.assertEqual(self.alice.post(BASE + group_path, {}).json()["code"], 1002)

        other_host = self.other_tenant.get_primary_domain().domain
        with tenant_context(self.other_tenant):
            foreign, foreign_id = self.make_user("presence-foreign-viewer", other_host)
            _, foreign_peer = self.make_user("presence-foreign-peer", other_host)
            other = self.post(foreign, "conversations/", {"peer_id": foreign_peer})
            self.assertEqual(other["peer"]["name"], "presence-foreign-peer")
            other_path = BASE + f"conversations/{other['id']}/presence-ticket/"
            other_ticket = self.data(foreign.post(other_path, {}))["token"].split(".")[0]
            other_claims = json.loads(base64.urlsafe_b64decode(other_ticket + "=" * (-len(other_ticket) % 4)))
            self.assertEqual(
                (other_claims["tenant"], other_claims["user"], other_claims["peer"]),
                (self.other_tenant.schema_name, foreign_id, foreign_peer),
            )
            self.assertEqual(self.alice.post(other_path, {}, HTTP_HOST=other_host).status_code, 401)
            self.assertEqual(foreign.post(BASE + path, {}, HTTP_HOST=self.domain.domain).status_code, 401)
