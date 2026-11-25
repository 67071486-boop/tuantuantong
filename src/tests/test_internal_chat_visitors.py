import json
from base64 import b64encode
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image
from rest_framework.exceptions import PermissionDenied

from apps.audit.models import OperationLog
from apps.business.roles import SERVICE, TENANT_ADMIN, WORKER
from apps.internal_chat import visitor_seats, visitor_service
from apps.internal_chat.models import (
    VisitorConversation, VisitorDeskPresence, VisitorIdentity, VisitorMessage, VisitorReceptionSettings,
)
from apps.internal_chat.visitor_service import COOKIE_NAME, credential_digest, mark_read
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/internal-chat/visitor/"
GUEST = BASE + "guest/"
DESK = BASE + "desk/"
PHONE = "13000000000"


class InternalChatVisitorTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(TT_INTERNAL_CHAT_VISITORS_ENABLED=True, DEBUG=True))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="visitor_other", name="对照工作室", domain="visitor-other.test.local",
        )

    def account(self, username, role):
        client = make_logged_in_client(domain=self.domain.domain, username=username, groups=(role,), games=None)
        return client, get_user_model().objects.get(username=username).pk

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def assert_missing(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "记录不存在或已被删除")
        self.assertIsNone(response.json()["data"])

    def guest(self, *, seat_mode=False):
        client = Client(SERVER_NAME=self.domain.domain, enforce_csrf_checks=True)
        expected = {"registered": False}
        if seat_mode:
            expected["seat_mode"] = True
        self.assertEqual(self.data(client.get(GUEST + "bootstrap/")), expected)
        return client

    def write(self, client, path, data, **kwargs):
        return client.post(path, data, content_type="application/json", **kwargs)

    def register(self, client, phone=PHONE):
        return self.data(self.write(
            client, GUEST + "register/", {"phone": phone},
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        ))

    def configure(self, client, contacts, sender=None, greeting=""):
        return client.put(BASE + "settings/", {
            "contact_ids": contacts, "greeting_sender_id": sender, "greeting_text": greeting,
        }, content_type="application/json")

    def configure_greetings(self, client, contacts, greetings, **extra):
        return client.put(BASE + "settings/", {
            "contact_ids": contacts, "greetings": greetings, **extra,
        }, content_type="application/json")

    def set_seats(self, admin, *seats):
        return admin.put(BASE + "settings/", {"seats": seats}, content_type="application/json")

    def guest_post(self, guest, path, data):
        return self.write(guest, GUEST + path, data, HTTP_X_CSRFTOKEN=guest.cookies["csrftoken"].value)

    def start_seat(self, guest, seat_kind):
        directory = self.data(guest.get(GUEST + "contacts/"))["contacts"]
        peer = next(contact for contact in directory if contact["seat_kind"] == seat_kind)
        return self.data(self.guest_post(guest, "conversations/", {"peer_id": peer["id"]}))

    @staticmethod
    def image_upload(color, format="PNG"):
        file = BytesIO()
        Image.new("RGB", (12, 8), color).save(file, format=format)
        return {"mime": f"image/{format.lower()}", "data": b64encode(file.getvalue()).decode("ascii")}

    def test_visitor_and_reception_can_exchange_images_and_reload_them_from_history(self):
        admin, _ = self.account("image-admin", TENANT_ADMIN)
        desk, desk_id = self.account("image-desk", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [desk_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        self.data(self.write(desk, DESK + "availability/", {"enabled": True}))
        guest = self.guest(seat_mode=True)
        self.register(guest)
        room = self.start_seat(guest, "hr")
        path = f"conversations/{room['id']}/messages/"
        guest_image = self.image_upload("blue")
        requested = {"client_id": "first-image", "image": guest_image}
        sent = self.data(self.guest_post(guest, path, requested))
        self.assertEqual((sent["text"], sent["image"]), ("", {"mime": "image/png", "width": 12, "height": 8}))
        self.assertEqual(self.data(self.guest_post(guest, path, requested))["id"], sent["id"])
        reply_image = self.image_upload("red", "JPEG")
        reply = self.data(self.write(desk, DESK + path, {
            "text": "图片说明", "client_id": "image-reply", "reply_to_id": sent["id"], "image": reply_image,
        }))
        self.assertEqual((reply["reply_to_id"], reply["text"]), (sent["id"], "图片说明"))
        for recipient, prefix, message, image in ((desk, DESK, sent, guest_image), (guest, GUEST, reply, reply_image)):
            summary = self.data(recipient.get(prefix + f"conversations/{room['id']}/"))
            self.assertEqual(summary["last_message"]["image"], reply["image"])
            history = self.data(recipient.get(prefix + path))["messages"]
            self.assertEqual([item["id"] for item in history], [sent["id"], reply["id"]])
            updates = self.data(recipient.get(prefix + "sync/", {"after": 0}))["changes"]
            update = next(change["message"] for change in updates if change.get("message", {}).get("id") == message["id"])
            self.assertEqual(update["image"], message["image"])
            self.assertNotIn("data", update["image"])
            media = recipient.get(prefix + path + f"{message['id']}/image/")
            self.assertIn("no-store", media.headers["Cache-Control"])
            self.assertIn("private", media.headers["Cache-Control"])
            self.assertEqual(self.data(media), image)

    def test_visitor_album_is_idempotent_and_keeps_parts_together_in_history(self):
        admin, _ = self.account("album-admin", TENANT_ADMIN)
        desk, desk_id = self.account("album-desk", SERVICE)
        self.data(self.configure(admin, [desk_id], desk_id, "欢迎"))
        guest = self.guest()
        room = self.register(guest)["conversation_id"]
        path = f"conversations/{room}/messages/"
        welcome, = self.data(guest.get(GUEST + path))["messages"]
        pictures = [self.image_upload("blue"), self.image_upload("green", "WEBP")]
        payload = {"client_id": "album", "images": pictures, "text": "两张图", "reply_to_id": welcome["id"]}
        album = self.data(self.guest_post(guest, path, payload))["messages"]
        self.assertEqual(self.data(self.guest_post(guest, path, payload))["messages"], album)
        self.assertTrue(album[0]["album_id"])
        self.assertEqual([message["album_id"] for message in album], [album[0]["album_id"]] * 2)
        self.assertEqual([message["text"] for message in album], ["两张图", ""])
        self.assertEqual([message["reply_to_id"] for message in album], [welcome["id"], None])
        self.assertEqual(self.data(desk.get(DESK + "unread/"))["unread_count"], 1)
        with patch.object(visitor_service, "PAGE_SIZE", 1):
            page = self.data(desk.get(DESK + path))
        self.assertEqual([message["id"] for message in page["messages"]], [message["id"] for message in album])
        self.assertTrue(page["has_more"])
        for message, image in zip(album, pictures):
            self.assertEqual(self.data(desk.get(DESK + path + f"{message['id']}/image/")), image)
        wrong_type = self.guest_post(guest, path, {"client_id": "album", "image": pictures[0]})
        self.assertNotEqual(wrong_type.json()["code"], 0)
        invalid = {**pictures[0], "mime": "image/jpeg"}
        malformed = self.guest_post(guest, path, {"client_id": "bad-photo", "image": invalid})
        self.assertNotEqual(malformed.json()["code"], 0)
        self.assertEqual(len(self.data(desk.get(DESK + path))["messages"]), 3)

    def test_busy_guest_cannot_decode_more_uploads_and_can_retry_an_accepted_image(self):
        admin, _ = self.account("image-limit-admin", TENANT_ADMIN)
        _, desk_id = self.account("image-limit-desk", SERVICE)
        self.data(self.configure(admin, [desk_id], desk_id, "欢迎"))
        guest = self.guest()
        room = self.register(guest)["conversation_id"]
        path = f"conversations/{room}/messages/"
        payload = {"client_id": "first-picture", "image": self.image_upload("blue")}
        with patch.object(visitor_service, "MESSAGE_LIMIT", 1):
            sent = self.data(self.guest_post(guest, path, payload))
            with patch.object(Image, "open", wraps=Image.open) as decode:
                refused = self.guest_post(guest, path, {**payload, "client_id": "second-picture"})
                self.assertEqual(refused.status_code, 429)
                self.assertEqual(self.data(self.guest_post(guest, path, payload)), sent)
            decode.assert_not_called()
        self.assertEqual([message["id"] for message in self.data(guest.get(GUEST + path))["messages"]][-1:], [sent["id"]])

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_CORE_BACKEND="legacy")
    def test_configured_reception_staff_can_contact_the_regular_workspace_directory(self):
        admin, _ = self.account("reception-directory-admin", TENANT_ADMIN)
        receptionist, receptionist_id = self.account("reception-directory-staff", SERVICE)
        colleague, colleague_id = self.account("reception-directory-unlisted-colleague", SERVICE)
        worker, worker_id = self.account("reception-directory-worker", WORKER)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [receptionist_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        self.assertTrue(self.data(receptionist.get(DESK + "me/"))["can_receive"])

        guest = self.guest(seat_mode=True)
        self.register(guest)
        self.assertEqual(self.data(guest.get(GUEST + "contacts/", {"q": "unlisted-colleague"}))["contacts"], [])
        self.assertEqual(self.guest_post(guest, "conversations/", {"peer_id": colleague_id}).status_code, 403)
        internal = "/api/internal-chat/"
        self.assertEqual(self.data(worker.get(internal + "contacts/", {"q": "unlisted-colleague"}))["contacts"], [])
        self.assertEqual(self.write(worker, internal + "conversations/", {"peer_id": colleague_id}).status_code, 403)

        for target, target_id, search in ((colleague, colleague_id, "unlisted-colleague"), (worker, worker_id, "directory-worker")):
            directory = self.data(receptionist.get(internal + "contacts/", {"q": search}))
            self.assertFalse(directory["worker_directory_only"])
            self.assertEqual([entry["id"] for entry in directory["contacts"]], [target_id])
            conversation = self.data(self.write(receptionist, internal + "conversations/", {"peer_id": target_id}))
            path = internal + f"conversations/{conversation['id']}/messages/"
            self.data(self.write(receptionist, path, {"text": "工作室内部联络", "client_id": search}))
            self.assertEqual([message["text"] for message in self.data(target.get(path))["messages"]], ["工作室内部联络"])

    def test_manual_desk_actions_record_real_actors_and_leave_automatic_changes_unlogged(self):
        admin, _ = self.account("audit-seat-admin", TENANT_ADMIN)
        first, first_id = self.account("audit-seat-first", SERVICE)
        second, second_id = self.account("audit-seat-second", SERVICE)
        other, other_id = self.account("audit-seat-other", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, second_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [other_id], "capacity": 1, "shifts": []},
        ))
        logs = OperationLog.objects.filter(action__startswith="system.internal_chat_visitor_").order_by("pk")
        self.data(self.write(first, DESK + "availability/", {"enabled": False}))
        self.assertFalse(logs.exists())
        self.data(self.write(first, DESK + "availability/", {"enabled": True}))
        self.data(self.write(first, DESK + "availability/", {"enabled": True}))
        self.data(self.write(second, DESK + "availability/", {"enabled": True}))
        guest = self.guest(seat_mode=True)
        self.register(guest)
        room = self.start_seat(guest, "hr")
        path = f"conversations/{room['id']}/"
        self.data(self.guest_post(guest, path + "messages/", {"text": "咨询内容留在会话内", "client_id": "audit-msg"}))
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=20)):
            self.data(first.get(DESK + "sync/", {"after": 0}))
        self.assertNotEqual(self.write(first, DESK + path + "transfer/", {"target_id": other_id}).json()["code"], 0)
        self.assert_missing(self.write(other, DESK + path + "close/", {}))
        self.data(self.write(first, DESK + path + "transfer/", {"target_id": second_id}))
        self.data(self.write(second, DESK + path + "close/", {}))
        self.data(self.write(second, DESK + path + "close/", {}))
        self.data(self.write(first, DESK + "availability/", {"enabled": False}))
        self.data(self.write(first, DESK + "availability/", {"enabled": False}))
        self.assertEqual(list(logs.values_list("action", "actor_id", "actor", "target_kind", "target_id", "to_value")), [
            ("system.internal_chat_visitor_availability", first_id, "audit-seat-first", "visitor_desk", str(first_id), "接待中"),
            ("system.internal_chat_visitor_availability", second_id, "audit-seat-second", "visitor_desk", str(second_id), "接待中"),
            ("system.internal_chat_visitor_transfer", first_id, "audit-seat-first", "chat_visitor", str(room["id"]), str(second_id)),
            ("system.internal_chat_visitor_close", second_id, "audit-seat-second", "chat_visitor", str(room["id"]), ""),
            ("system.internal_chat_visitor_availability", first_id, "audit-seat-first", "visitor_desk", str(first_id), "暂停接待"),
        ])
        for row in logs:
            self.assertEqual(row.log_type, 40)
            self.assertNotIn(PHONE, row.summary)
            self.assertNotIn("咨询内容留在会话内", row.summary)
            self.assertEqual((row.target_name, row.reason, row.order_no), ("", "", ""))

    def test_visitor_seat_assigns_first_message_to_available_staff_with_least_work(self):
        admin, _ = self.account("seat-admin", TENANT_ADMIN)
        first, first_id = self.account("seat-first", SERVICE)
        second, second_id = self.account("seat-second", SERVICE)
        orders, order_id = self.account("seat-orders", SERVICE)
        self.data(self.configure(admin, [first_id], first_id, "旧欢迎语"))
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, second_id], "capacity": 2, "shifts": []},
            {"kind": "orders", "contact_ids": [first_id, order_id], "capacity": 1, "shifts": []},
        ))
        for client in (first, second, orders):
            self.data(self.write(client, DESK + "availability/", {"enabled": True}))

        guest = self.guest(seat_mode=True)
        registration = self.register(guest)
        self.assertIsNone(registration["conversation_id"])
        self.assertTrue(registration["seat_mode"])
        self.assertEqual(registration["user"]["display_name"], "访客 130****0000")
        self.assertEqual(self.data(guest.get(GUEST + "conversations/"))["conversations"], [])
        directory = self.data(guest.get(GUEST + "contacts/"))["contacts"]
        self.assertEqual([(contact["name"], contact["presence"]) for contact in directory], [
            ("人事客服", "online"), ("订单客服", "online"),
        ])
        first_room = self.start_seat(guest, "hr")
        self.assertEqual(first_room["service_status"], "new")
        self.assertEqual(self.data(first.get(DESK + "conversations/"))["conversations"], [])
        self.data(self.guest_post(guest, f"conversations/{first_room['id']}/messages/", {
            "text": "第一次咨询人事", "client_id": "first",
        }))
        another = self.guest(seat_mode=True)
        self.register(another, "13100000000")
        next_room = self.start_seat(another, "hr")
        self.data(self.guest_post(another, f"conversations/{next_room['id']}/messages/", {
            "text": "另一位访客", "client_id": "next",
        }))
        for staff, staff_id, expected, name in (
            (first, first_id, first_room, "访客 13000000000"), (second, second_id, next_room, "访客 13100000000"),
        ):
            [room] = self.data(staff.get(DESK + "conversations/"))["conversations"]
            self.assertEqual((room["id"], room["service_status"], room["assignee_id"]), (
                expected["id"], "assigned", staff_id,
            ))
            self.assertEqual(room["peer"]["name"], name)
            self.assertEqual(self.data(staff.get(DESK + f"conversations/{room['id']}/"))["peer"]["name"], name)
        self.assertEqual(self.data(orders.get(DESK + "conversations/"))["conversations"], [])
        self.assert_missing(orders.get(DESK + f"conversations/{first_room['id']}/"))
        self.assertEqual(self.data(guest.get(GUEST + f"conversations/{first_room['id']}/"))["peer"]["name"], "人事客服")
        self.assertEqual(self.start_seat(guest, "hr")["id"], first_room["id"])

        order_room = self.start_seat(guest, "orders")
        self.data(self.guest_post(guest, f"conversations/{order_room['id']}/messages/", {
            "text": "同一访客也可咨询订单", "client_id": "order",
        }))
        desk = self.data(first.get(DESK + "conversations/"))["conversations"]
        self.assertEqual({row["seat_kind"] for row in desk}, {"hr", "orders"})
        self.assertEqual(len({row["peer"]["id"] for row in desk}), 2)
        self.assertTrue(all(row["last_message"]["sender_id"] == row["peer"]["id"] for row in desk))

    def test_availability_poll_refreshes_receiving_staff_and_assigns_a_waiting_visitor(self):
        admin, _ = self.account("heartbeat-admin", TENANT_ADMIN)
        staff, staff_id = self.account("heartbeat-staff", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [staff_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        availability = DESK + "availability/"
        started = timezone.now()
        with patch("django.utils.timezone.now", return_value=started):
            self.data(self.write(staff, availability, {"enabled": True}))
        presence = VisitorDeskPresence.objects.get(staff_id=staff_id)
        with patch("django.utils.timezone.now", return_value=started + visitor_seats.HEARTBEAT_INTERVAL / 2):
            self.assertEqual(self.data(staff.get(availability)), {"enabled": True})
        presence.refresh_from_db()
        self.assertEqual(presence.last_seen_at, started)

        expired = started + visitor_seats.ONLINE_WINDOW + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=expired):
            guest = self.guest(seat_mode=True)
            self.register(guest)
            room = self.start_seat(guest, "hr")
            path = f"conversations/{room['id']}/"
            self.data(self.guest_post(guest, path + "messages/", {"text": "咨询", "client_id": "waiting"}))
            self.assertEqual(self.data(guest.get(GUEST + path))["service_status"], "waiting")
            self.assertEqual(self.data(staff.get(availability)), {"enabled": True})
            summary = self.data(staff.get(DESK + path))
            self.assertEqual((summary["assignee_id"], summary["service_status"]), (staff_id, "assigned"))
        presence.refresh_from_db()
        self.assertEqual(presence.last_seen_at, expired)
        self.assertEqual(OperationLog.objects.filter(action="system.internal_chat_visitor_availability").count(), 1)

        with patch("django.utils.timezone.now", return_value=expired):
            self.data(self.write(staff, availability, {"enabled": False}))
        with patch("django.utils.timezone.now", return_value=expired + visitor_seats.ONLINE_WINDOW):
            self.assertEqual(self.data(staff.get(availability)), {"enabled": False})
        presence.refresh_from_db()
        self.assertFalse(presence.enabled)
        self.assertEqual(presence.last_seen_at, expired)

    def test_visitor_seat_preserves_offline_messages_and_drains_oldest_first(self):
        admin, _ = self.account("queue-admin", TENANT_ADMIN)
        staff, staff_id = self.account("queue-staff", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [staff_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        cases = []
        for phone, text in ((PHONE, "最早的留言"), ("13100000000", "后来的留言")):
            guest = self.guest(seat_mode=True)
            self.register(guest, phone)
            room = self.start_seat(guest, "hr")
            self.assertEqual(room["peer"]["presence"], "offline")
            url = f"conversations/{room['id']}/"
            self.data(self.guest_post(guest, url + "messages/", {"text": text, "client_id": "note"}))
            [waiting] = self.data(guest.get(GUEST + "conversations/"))["conversations"]
            self.assertEqual(waiting["service_status"], "waiting")
            cases.append((room["id"], url, text))

        self.data(self.write(staff, DESK + "availability/", {"enabled": True}))
        [assigned] = self.data(staff.get(DESK + "conversations/"))["conversations"]
        self.assertEqual((assigned["id"], assigned["last_message"]["text"]), (cases[0][0], cases[0][2]))
        directory = self.data(guest.get(GUEST + "contacts/"))["contacts"]
        self.assertEqual([peer["presence"] for peer in directory], ["busy", "offline"])
        closed = self.data(self.write(staff, DESK + cases[0][1] + "close/", {}))
        self.assertEqual(closed["service_status"], "closed")
        [next_room, previous] = self.data(staff.get(DESK + "conversations/"))["conversations"]
        self.assertEqual((next_room["id"], next_room["last_message"]["text"]), (cases[1][0], cases[1][2]))
        self.assertEqual(previous["service_status"], "closed")

        self.data(self.write(staff, DESK + cases[1][1] + "close/", {}))
        self.data(self.guest_post(guest, cases[1][1] + "messages/", {"text": "补充说明", "client_id": "reopen"}))
        reopened = self.start_seat(guest, "hr")
        self.assertEqual((reopened["id"], reopened["service_status"]), (cases[1][0], "assigned"))
        history = self.data(guest.get(GUEST + cases[1][1] + "messages/"))["messages"]
        self.assertEqual([row["text"] for row in history], [cases[1][2], "补充说明"])

    def test_stopping_reception_hands_off_to_the_same_seat_or_queues(self):
        admin, _ = self.account("stop-admin", TENANT_ADMIN)
        first, first_id = self.account("stop-first", SERVICE)
        next_desk, next_id = self.account("stop-next", SERVICE)
        off_shift, off_shift_id = self.account("stop-off-shift", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, next_id, off_shift_id], "capacity": 1, "shifts": [
                {"contact_id": first_id, "weekday": 0, "start": "00:00", "end": "24:00"},
                {"contact_id": next_id, "weekday": 0, "start": "00:00", "end": "24:00"},
                {"contact_id": off_shift_id, "weekday": 1, "start": "00:00", "end": "24:00"},
            ]},
            {"kind": "orders", "contact_ids": [first_id], "capacity": 1, "shifts": []},
        ))
        now = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        availability = DESK + "availability/"
        with patch("django.utils.timezone.now", return_value=now):
            self.data(self.write(first, availability, {"enabled": True}))
            guest = self.guest(seat_mode=True)
            self.register(guest)
            hr_room = self.start_seat(guest, "hr")["id"]
            orders_room = self.start_seat(guest, "orders")["id"]
            hr_path = f"conversations/{hr_room}/"
            orders_path = f"conversations/{orders_room}/"
            self.data(self.guest_post(guest, hr_path + "messages/", {"text": "人事留言", "client_id": "hr"}))
            self.data(self.guest_post(guest, orders_path + "messages/", {"text": "订单留言", "client_id": "order"}))
            self.data(self.write(first, DESK + hr_path + "messages/", {"text": "转接前的回复", "client_id": "first"}))
            cursor = self.data(first.get(DESK + "conversations/"))["cursor"]
            for staff in (next_desk, off_shift):
                self.data(self.write(staff, availability, {"enabled": True}))

            for _ in range(2):
                self.assertEqual(self.data(self.write(first, availability, {"enabled": False})), {"enabled": False})
            self.assertEqual(self.data(first.get(DESK + "conversations/"))["conversations"], [])
            self.assertEqual(self.data(first.get(DESK + "unread/"))["unread_count"], 0)
            self.assertEqual(
                self.data(first.get(DESK + "sync/", {"after": cursor}))["revoked_conversation_ids"],
                sorted([hr_room, orders_room]),
            )
            [received] = self.data(next_desk.get(DESK + "conversations/"))["conversations"]
            self.assertEqual((received["id"], received["seat_kind"], received["assignee_id"]), (hr_room, "hr", next_id))
            self.assertEqual(self.data(off_shift.get(DESK + "conversations/"))["conversations"], [])
            messages = self.data(next_desk.get(DESK + hr_path + "messages/"))["messages"]
            self.assertEqual([row["text"] for row in messages], ["人事留言", "转接前的回复"])
            history = self.data(next_desk.get(DESK + hr_path + "transfer/"))["history"]
            self.assertEqual([(row["from_id"], row["to_id"]) for row in history], [(first_id, next_id)])
            self.assert_missing(self.write(first, DESK + hr_path + "messages/", {"text": "旧客服发言", "client_id": "stale"}))

            self.data(self.guest_post(guest, orders_path + "messages/", {"text": "订单补充", "client_id": "more"}))
            self.assertEqual(self.data(guest.get(GUEST + orders_path))["service_status"], "waiting")
            self.assert_missing(first.get(DESK + orders_path))
            self.assert_missing(next_desk.get(DESK + orders_path))
            self.data(self.write(first, availability, {"enabled": True}))
            [recovered] = self.data(first.get(DESK + "conversations/"))["conversations"]
            self.assertEqual((recovered["id"], recovered["last_message"]["text"]), (orders_room, "订单补充"))

            # Older releases could stop a desk without releasing its conversation.
            VisitorDeskPresence.objects.filter(staff_id=first_id).update(enabled=False)
            self.data(self.guest_post(guest, orders_path + "messages/", {"text": "再补充一条", "client_id": "legacy-stop"}))
            self.assertEqual(self.data(guest.get(GUEST + orders_path))["service_status"], "waiting")
            self.assert_missing(first.get(DESK + orders_path))

    @override_settings(
        TT_CHAT_REALTIME_NOTIFY_URL="http://127.0.0.1:4191/notify", TT_CHAT_REALTIME_SECRET="visitor-test-secret",
        TT_CHAT_REALTIME_TENANT_KEYS=False,
    )
    def test_visitor_inbox_notifies_only_assigned_desk_once_without_message_contents(self):
        admin, _ = self.account("wake-admin", TENANT_ADMIN)
        first, first_id = self.account("wake-first", SERVICE)
        second, second_id = self.account("wake-second", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, second_id], "capacity": 2, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        availability = DESK + "availability/"
        self.data(self.write(first, availability, {"enabled": True}))
        guests = []

        def incoming(recipient_id, conversation_id, message_id):
            return {
                "tenant": self.tenant.schema_name, "users": [recipient_id],
                "visitor_incoming": {"id": message_id, "conversation_id": conversation_id},
            }

        with patch("apps.internal_chat.realtime.urlopen") as network:
            with self.captureOnCommitCallbacks(execute=True):
                for phone in (PHONE, "13100000000"):
                    guest = self.guest(seat_mode=True)
                    self.register(guest, phone)
                    room = self.start_seat(guest, "hr")
                    path = f"conversations/{room['id']}/"
                    message = {"text": "仅会话成员可见", "client_id": "message"}
                    sent = self.data(self.guest_post(guest, path + "messages/", message))
                    self.assertEqual(self.data(self.guest_post(guest, path + "messages/", message))["id"], sent["id"])
                    guests.append((guest, path, incoming(first_id, room["id"], sent["id"])))
                network.assert_not_called()
            self.assertEqual([json.loads(call.args[0].data) for call in network.call_args_list], [
                notice for _, _, notice in guests
            ])
            self.assertEqual(network.call_args.args[0].get_header("Authorization"), "Bearer visitor-test-secret")
            self.data(self.write(second, availability, {"enabled": True}))
            third = self.guest(seat_mode=True)
            self.register(third, "13200000000")
            full_room = self.start_seat(third, "hr")["id"]
            self.data(self.guest_post(third, f"conversations/{full_room}/messages/", {"text": "另一条会话", "client_id": "third"}))
            network.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.data(self.write(first, availability, {"enabled": False}))
                network.assert_not_called()
            network.assert_called_once()
            self.assertEqual(json.loads(network.call_args.args[0].data), {
                "tenant": self.tenant.schema_name, "users": sorted([first_id, second_id]),
            })

            waiting_guest, waiting_path, _ = guests[1]
            self.assertEqual(self.data(waiting_guest.get(GUEST + waiting_path))["service_status"], "waiting")
            network.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.data(self.guest_post(waiting_guest, waiting_path + "messages/", {"text": "还在等", "client_id": "wait"}))
            network.assert_not_called()

            guest, path, previous = guests[0]
            network.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                album = {
                    "text": "补充", "client_id": "next",
                    "images": [self.image_upload("red"), self.image_upload("blue")],
                }
                sent = self.data(self.guest_post(guest, path + "messages/", album))
                self.assertEqual(self.data(self.guest_post(guest, path + "messages/", album)), sent)
                self.data(self.write(second, DESK + path + "read/", {"seq": 3}))
            self.assertEqual([json.loads(call.args[0].data) for call in network.call_args_list], [
                incoming(second_id, previous["visitor_incoming"]["conversation_id"], sent["messages"][0]["id"]),
                {"tenant": self.tenant.schema_name, "users": [second_id]},
            ])

    def test_visitor_seat_handoff_preserves_sender_receipts_and_blocks_old_owner(self):
        admin, _ = self.account("transfer-admin", TENANT_ADMIN)
        first, first_id = self.account("transfer-first", SERVICE)
        second, second_id = self.account("transfer-second", SERVICE)
        orders, orders_id = self.account("transfer-orders", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, second_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [orders_id], "capacity": 1, "shifts": []},
        ))
        self.data(self.write(first, DESK + "availability/", {"enabled": True}))
        guest = self.guest(seat_mode=True)
        self.register(guest)
        room = self.start_seat(guest, "hr")
        url = f"conversations/{room['id']}/"
        picture = self.image_upload("blue")
        first_message = self.data(self.guest_post(guest, url + "messages/", {
            "text": "访客的问题", "client_id": "question", "image": picture,
        }))
        attachment = url + f"messages/{first_message['id']}/image/"
        self.assertEqual(self.data(first.get(DESK + attachment)), picture)
        staff_room = self.data(first.get(DESK + url))
        self.assertEqual((staff_room["seat_kind"], staff_room["assignee_id"]), ("hr", first_id))
        self.data(self.write(first, DESK + url + "read/", {"seq": 1}))
        self.data(self.write(first, DESK + url + "messages/", {"text": "首位回复", "client_id": "first"}))
        cursor = self.data(first.get(DESK + "conversations/"))["cursor"]
        for staff in (second, orders):
            self.data(self.write(staff, DESK + "availability/", {"enabled": True}))
        transfer = DESK + url + "transfer/"
        choices = self.data(first.get(transfer))["candidates"]
        self.assertEqual([contact["id"] for contact in choices], [second_id])
        self.assertNotEqual(self.write(first, transfer, {"target_id": orders_id}).json()["code"], 0)
        self.assertEqual(self.data(self.write(first, transfer, {"target_id": second_id}))["assignee_id"], second_id)
        self.assert_missing(first.get(DESK + url))
        self.assert_missing(orders.get(DESK + url))
        self.assert_missing(first.get(DESK + attachment))
        self.assert_missing(orders.get(DESK + attachment))
        self.assertEqual(self.data(second.get(DESK + attachment)), picture)
        self.assertEqual(self.data(guest.get(GUEST + attachment)), picture)
        reassigned = self.data(second.get(DESK + url))
        self.assertEqual((reassigned["id"], reassigned["service_status"], reassigned["assignee_id"]), (
            room["id"], "assigned", second_id,
        ))
        guest_room = self.data(guest.get(GUEST + url))
        self.assertEqual((guest_room["id"], guest_room["peer"]["name"]), (room["id"], "人事客服"))
        self.assertNotIn("assignee_id", guest_room)

        history = self.data(second.get(transfer))["history"]
        self.assertEqual([(row["from_id"], row["to_id"]) for row in history], [(first_id, second_id)])
        old_desk = self.data(first.get(DESK + "conversations/"))
        self.assertEqual(old_desk["conversations"], [])
        sync = self.data(first.get(DESK + "sync/", {"after": cursor}))
        self.assertIn({"kind": "conversation.read", "conversation_id": room["id"]}, sync["changes"])
        self.assertEqual(sync["revoked_conversation_ids"], [room["id"]])
        self.assertGreaterEqual(old_desk["cursor"], sync["cursor"])
        self.assertNotIn("revoked_conversation_ids", self.data(first.get(DESK + "sync/", {"after": old_desk["cursor"]})))
        self.assertNotIn("revoked_conversation_ids", self.data(second.get(DESK + "sync/", {"after": cursor})))
        self.assertNotIn("revoked_conversation_ids", self.data(guest.get(GUEST + "sync/", {"after": cursor})))
        self.assert_missing(self.write(first, DESK + url + "messages/", {"text": "迟到的回复", "client_id": "old"}))
        self.assert_missing(orders.get(DESK + url + "messages/"))

        self.data(self.write(second, DESK + url + "messages/", {"text": "下一位回复", "client_id": "second"}))
        desk_history = self.data(second.get(DESK + url + "messages/"))["messages"]
        self.assertEqual([row["sender_id"] for row in desk_history[1:]], [first_id, second_id])
        self.assertTrue(all(row["sender_name"] for row in desk_history[1:]))
        guest_history = self.data(guest.get(GUEST + url + "messages/"))["messages"]
        self.assertEqual([row["sender_id"] for row in guest_history[1:]], [room["peer"]["id"]] * 2)
        self.assertTrue(all("sender_name" not in row for row in guest_history))
        self.assertEqual(self.start_seat(guest, "hr")["peer_read_seq"], 1)

    def test_visitor_seat_overnight_shift_hands_off_on_next_guest_message(self):
        admin, _ = self.account("shift-admin", TENANT_ADMIN)
        first, first_id = self.account("shift-night", SERVICE)
        second, second_id = self.account("shift-morning", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [first_id, second_id], "capacity": 1, "shifts": [
                {"contact_id": first_id, "weekday": 0, "start": "22:00", "end": "01:00"},
                {"contact_id": second_id, "weekday": 1, "start": "01:00", "end": "08:00"},
            ]},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        before = datetime(2026, 11, 24, 0, 59, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("django.utils.timezone.now", return_value=before):
            for staff in (first, second):
                self.data(self.write(staff, DESK + "availability/", {"enabled": True}))
            guest = self.guest(seat_mode=True)
            self.register(guest)
            room = self.start_seat(guest, "hr")
            url = f"conversations/{room['id']}/messages/"
            self.data(self.guest_post(guest, url, {"text": "夜班咨询", "client_id": "night"}))
            self.assertEqual(len(self.data(first.get(DESK + "conversations/"))["conversations"]), 1)
            self.assertEqual(self.data(second.get(DESK + "conversations/"))["conversations"], [])

        with patch("django.utils.timezone.now", return_value=before + timedelta(seconds=10)):
            self.assertEqual(len(self.data(first.get(DESK + "conversations/"))["conversations"]), 1)
            history = visitor_service.history

            def receive_next_shift_message(conversation, **params):
                self.data(self.guest_post(guest, url, {"text": "换班后追问", "client_id": "morning"}))
                return history(conversation, **params)

            with patch.object(visitor_service, "history", side_effect=receive_next_shift_message):
                self.assertEqual(first.get(DESK + url).status_code, 403)
            self.assertEqual(self.data(first.get(DESK + "conversations/"))["conversations"], [])
            [assigned] = self.data(second.get(DESK + "conversations/"))["conversations"]
            self.assertEqual((assigned["id"], assigned["assignee_id"]), (room["id"], second_id))
            self.assertEqual(self.data(second.get(DESK + url))["messages"][-1]["text"], "换班后追问")

    def test_visitor_seat_membership_does_not_restore_removed_legacy_access(self):
        admin, _ = self.account("legacy-admin", TENANT_ADMIN)
        staff, staff_id = self.account("legacy-reception", SERVICE)
        self.data(self.configure(admin, [staff_id], staff_id, "旧会话欢迎语"))
        guest = self.guest()
        conversation_id = self.register(guest)["conversation_id"]
        messages_path = f"conversations/{conversation_id}/messages/"
        self.data(self.guest_post(guest, messages_path, {"text": "旧会话留言", "client_id": "legacy"}))
        self.assertEqual(self.data(staff.get(DESK + "unread/"))["unread_count"], 1)

        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [staff_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        self.data(self.configure(admin, []))
        self.assertEqual(self.data(staff.get(DESK + "me/"))["user"]["id"], staff_id)
        self.assertEqual(self.data(staff.get(DESK + "conversations/"))["conversations"], [])
        self.assertEqual(self.data(staff.get(DESK + "unread/"))["unread_count"], 0)
        self.assertEqual(self.data(staff.get(DESK + "sync/", {"after": 0}))["changes"], [
            {"kind": "conversation.read", "conversation_id": conversation_id},
        ])
        self.assert_missing(staff.get(DESK + messages_path))
        self.assertEqual(self.data(guest.get(GUEST + messages_path))["messages"][-1]["text"], "旧会话留言")

    def test_removed_seat_access_evicts_closed_room_from_desk(self):
        admin, _ = self.account("removed-seat-admin", TENANT_ADMIN)
        staff, staff_id = self.account("removed-seat-staff", SERVICE)
        _, unavailable_id = self.account("unavailable-seat-staff", SERVICE)
        hr = {"kind": "hr", "contact_ids": [staff_id], "capacity": 1, "shifts": []}
        orders = {"kind": "orders", "contact_ids": [staff_id], "capacity": 1, "shifts": []}
        self.data(self.set_seats(admin, hr, {**orders, "contact_ids": [staff_id, unavailable_id]}))
        get_user_model().objects.filter(pk=unavailable_id).update(is_active=False)
        settings = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(settings["unavailable_contacts"], [{"id": unavailable_id, "name": "unavailable-seat-staff"}])
        self.data(self.set_seats(admin, hr, orders))
        self.data(self.write(staff, DESK + "availability/", {"enabled": True}))
        guest = self.guest(seat_mode=True)
        self.register(guest)
        room = self.start_seat(guest, "hr")
        path = f"conversations/{room['id']}/"
        self.data(self.guest_post(guest, path + "messages/", {"text": "之前的问题", "client_id": "closed"}))
        self.data(self.write(staff, DESK + path + "close/", {}))
        cursor = self.data(staff.get(DESK + "conversations/"))["cursor"]

        self.data(self.set_seats(admin, {**hr, "contact_ids": []}, orders))
        self.assertEqual(self.data(staff.get(DESK + "conversations/"))["conversations"], [])
        self.assert_missing(staff.get(DESK + path + "messages/"))
        changes = self.data(staff.get(DESK + "sync/", {"after": cursor}))["changes"]
        self.assertEqual(changes, [{"kind": "conversation.read", "conversation_id": room["id"]}])
        guest_history = self.data(guest.get(GUEST + path + "messages/"))["messages"]
        self.assertEqual([message["text"] for message in guest_history], ["之前的问题"])

    def test_visitor_seat_settings_and_messages_are_scoped_to_the_workspace(self):
        admin, _ = self.account("isolation-admin", TENANT_ADMIN)
        staff, staff_id = self.account("isolation-staff", SERVICE)
        self.data(self.set_seats(
            admin, {"kind": "hr", "contact_ids": [staff_id], "capacity": 1, "shifts": []},
            {"kind": "orders", "contact_ids": [], "capacity": 1, "shifts": []},
        ))
        self.data(self.write(staff, DESK + "availability/", {"enabled": True}))
        guest = self.guest(seat_mode=True)
        self.register(guest)
        room = self.start_seat(guest, "hr")
        url = f"conversations/{room['id']}/messages/"
        own_image = self.image_upload("blue")
        own_message = self.data(self.guest_post(guest, url, {
            "text": "本工作室的人事咨询", "client_id": "own", "image": own_image,
        }))
        own_image_path = url + f"{own_message['id']}/image/"
        self.assertEqual(self.data(staff.get(DESK + own_image_path)), own_image)
        [summary] = self.data(staff.get(DESK + "conversations/"))["conversations"]
        self.assertEqual(summary["last_message"]["text"], "本工作室的人事咨询")
        self.assertEqual(summary["peer"]["name"], "访客 13000000000")
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["seats"][0]["capacity"], 1)
        other_domain = "visitor-other.test.local"
        with tenant_context(self.other_tenant):
            other_admin = make_logged_in_client(
                domain=other_domain, username="isolation-other-admin", groups=(TENANT_ADMIN,), games=None,
            )
            other_staff = make_logged_in_client(
                domain=other_domain, username="isolation-other-staff", groups=(SERVICE,), games=None,
            )
            other_id = get_user_model().objects.get(username="isolation-other-staff").pk
            self.data(self.set_seats(
                other_admin, {"kind": "hr", "contact_ids": [], "capacity": 7, "shifts": []},
                {"kind": "orders", "contact_ids": [other_id], "capacity": 2, "shifts": []},
            ))
            self.data(self.write(other_staff, DESK + "availability/", {"enabled": True}))
            other_guest = Client(SERVER_NAME=other_domain, enforce_csrf_checks=True)
            self.data(other_guest.get(GUEST + "bootstrap/"))
            self.register(other_guest, "13100000000")
            other_room = self.start_seat(other_guest, "orders")
            other_url = f"conversations/{other_room['id']}/messages/"
            other_image = self.image_upload("red")
            other_message = self.data(self.guest_post(other_guest, other_url, {
                "text": "另一工作室的订单咨询", "client_id": "other", "image": other_image,
            }))
            other_image_path = other_url + f"{other_message['id']}/image/"
            self.assertEqual(self.data(other_staff.get(DESK + other_image_path)), other_image)
            [other_summary] = self.data(other_staff.get(DESK + "conversations/"))["conversations"]
            self.assertEqual(other_summary["last_message"]["text"], "另一工作室的订单咨询")
            self.assertEqual(other_summary["peer"]["name"], "访客 13100000000")
            self.assertEqual(self.data(other_admin.get(BASE + "settings/"))["seats"][0]["capacity"], 7)
        for desk, expected in ((staff, "本工作室的人事咨询"), (other_staff, "另一工作室的订单咨询")):
            changes = self.data(desk.get(DESK + "sync/", {"after": 0}))["changes"]
            self.assertEqual([row["message"]["text"] for row in changes if "message" in row], [expected])
        self.data(self.write(staff, DESK + "availability/", {"enabled": False}))
        self.assertEqual(self.data(staff.get(DESK + "conversations/"))["conversations"], [])
        [untouched] = self.data(other_staff.get(DESK + "conversations/"))["conversations"]
        self.assertEqual(untouched["last_message"]["text"], "另一工作室的订单咨询")
        with tenant_context(self.other_tenant):
            self.assertEqual(staff.get(DESK + other_url, SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(staff.get(DESK + other_image_path, SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(guest.get(GUEST + other_image_path, SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(staff.get(DESK + f"conversations/{other_room['id']}/", SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(admin.get(BASE + "settings/", SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(guest.get(GUEST + other_url, SERVER_NAME=other_domain).status_code, 401)
            self.assertEqual(self.write(staff, DESK + "availability/", {"enabled": False}, SERVER_NAME=other_domain).status_code, 401)

    def test_new_visitor_receives_one_welcome_per_enabled_contact(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, first_id = self.account("visitor-first-staff", SERVICE)
        _, second_id = self.account("visitor-second-staff", SERVICE)
        _, quiet_id = self.account("visitor-quiet-staff", SERVICE)
        rules = [
            {"contact_id": first_id, "enabled": True, "text": "你好，我是首位接待。"},
            {"contact_id": quiet_id, "enabled": False, "text": "已关闭的欢迎语"},
            {"contact_id": second_id, "enabled": True, "text": "你好，我是第二位接待。"},
        ]
        self.assertEqual(self.data(self.configure_greetings(
            admin, [first_id, quiet_id, second_id], list(reversed(rules)),
        ))["greetings"], rules)
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], rules)

        guest = self.guest()
        initial = self.register(guest)
        self.assertEqual(self.register(guest), initial)
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 2)
        conversations = self.data(guest.get(GUEST + "conversations/"))["conversations"]
        by_peer = {room["peer"]["id"]: room["id"] for room in conversations}
        self.assertEqual(set(by_peer), {first_id, second_id})
        self.assertEqual(initial["conversation_id"], by_peer[first_id])
        for sender_id, text in [(first_id, "你好，我是首位接待。"), (second_id, "你好，我是第二位接待。")]:
            path = f"conversations/{by_peer[sender_id]}/messages/"
            history = self.data(guest.get(GUEST + path))["messages"]
            self.assertEqual([(row["sender_id"], row["text"]) for row in history], [(sender_id, text)])
        contacts = self.data(guest.get(GUEST + "contacts/"))["contacts"]
        self.assertEqual([contact["id"] for contact in contacts], [first_id, quiet_id, second_id])
        manual = self.data(self.write(
            guest, GUEST + "conversations/", {"peer_id": quiet_id}, HTTP_X_CSRFTOKEN=guest.cookies["csrftoken"].value,
        ))
        self.assertEqual(manual["peer"]["id"], quiet_id)

    def test_old_welcome_is_preserved_and_settings_change_does_not_backfill_existing_guest(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, original_id = self.account("visitor-original-staff", SERVICE)
        _, added_id = self.account("visitor-added-staff", SERVICE)
        VisitorReceptionSettings.objects.create(
            pk=1, contact_ids=[added_id, original_id], greeting_sender_id=original_id, greeting_text="原来的欢迎语",
        )
        initial_settings = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(initial_settings["greetings"], [
            {"contact_id": added_id, "enabled": False, "text": ""},
            {"contact_id": original_id, "enabled": True, "text": "原来的欢迎语"},
        ])
        guest = self.guest()
        registered = self.register(guest)
        room = f"conversations/{registered['conversation_id']}/messages/"
        history = self.data(guest.get(GUEST + room))["messages"]
        self.assertEqual([(row["sender_id"], row["text"]) for row in history], [(original_id, "原来的欢迎语")])

        first_rule = {"contact_id": added_id, "enabled": True, "text": "新的欢迎语"}
        updated_rules = [first_rule, initial_settings["greetings"][1]]
        saved = self.data(self.configure_greetings(admin, [added_id, original_id], updated_rules))
        self.assertEqual((saved["greeting_sender_id"], saved["greeting_text"]), (added_id, "新的欢迎语"))
        self.assertEqual(self.register(guest), registered)
        existing_rooms = self.data(guest.get(GUEST + "conversations/"))["conversations"]
        self.assertEqual([room["peer"]["id"] for room in existing_rooms], [original_id])
        self.assertEqual(self.data(guest.get(GUEST + room))["messages"], history)

        legacy_save = self.configure(admin, [added_id, original_id], added_id, "误覆盖")
        self.assertNotEqual(legacy_save.json()["code"], 0)
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], updated_rules)

    def test_greeting_config_rejects_invalid_rules_and_does_not_use_ineligible_sender(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, active_id = self.account("visitor-active-staff", SERVICE)
        _, inactive_id = self.account("visitor-inactive-staff", SERVICE)
        active = {"contact_id": active_id, "enabled": True, "text": "你好，\n欢迎。"}
        inactive = {"contact_id": inactive_id, "enabled": True, "text": "不会发送"}
        for invalid_rules in ([inactive], [active, active], [{**active, "text": "   "}]):
            with self.subTest(invalid_rules=invalid_rules):
                response = self.configure_greetings(admin, [active_id], invalid_rules)
                self.assertNotEqual(response.json()["code"], 0)
        self.data(self.configure_greetings(admin, [inactive_id, active_id], [inactive, active]))
        get_user_model().objects.filter(pk=inactive_id).update(is_active=False)
        guest = self.guest()
        registration = self.register(guest)
        history = self.data(guest.get(GUEST + f"conversations/{registration['conversation_id']}/messages/"))["messages"]
        self.assertEqual([(row["sender_id"], row["text"]) for row in history], [(active_id, "你好，\n欢迎。")])
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 1)
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], [active])

    def test_saving_active_contacts_preserves_temporarily_ineligible_contact_and_greeting(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, active_id = self.account("visitor-active-staff", SERVICE)
        _, dormant_id = self.account("visitor-dormant-staff", SERVICE)
        _, removed_id = self.account("visitor-removed-staff", SERVICE)
        users = get_user_model().objects
        active = {"contact_id": active_id, "enabled": True, "text": "原欢迎语"}
        dormant = {"contact_id": dormant_id, "enabled": True, "text": "保留欢迎语"}
        self.data(self.configure_greetings(admin, [dormant_id, active_id, removed_id], [dormant, active]))
        users.filter(pk=dormant_id).update(is_active=False)
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["contact_ids"], [active_id, removed_id])

        active["text"] = "新欢迎语"
        saved = self.data(self.configure_greetings(admin, [active_id], [active]))
        self.assertEqual(saved["contact_ids"], [active_id])
        self.assertEqual(saved["greeting_sender_id"], active_id)
        guest = self.guest()
        self.register(guest)
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 1)
        self.assertEqual([row["id"] for row in self.data(guest.get(GUEST + "contacts/"))["contacts"]], [active_id])

        users.filter(pk=dormant_id).update(is_active=True)
        restored = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(restored["contact_ids"], [active_id, dormant_id])
        self.assertEqual(restored["greetings"], [active, dormant])
        next_guest = self.guest()
        self.register(next_guest)
        messages = self.data(next_guest.get(GUEST + "conversations/"))["conversations"]
        self.assertEqual(
            {row["peer"]["id"]: row["last_message"]["text"] for row in messages},
            {active_id: "新欢迎语", dormant_id: "保留欢迎语"},
        )

    def test_explicitly_removing_unavailable_contact_does_not_restore_their_greeting(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, active_id = self.account("visitor-active-staff", SERVICE)
        _, removed_id = self.account("visitor-removable-staff", SERVICE)
        active = {"contact_id": active_id, "enabled": True, "text": "保持启用"}
        removed = {"contact_id": removed_id, "enabled": True, "text": "待移除"}
        self.data(self.configure_greetings(admin, [active_id, removed_id], [active, removed]))
        get_user_model().objects.filter(pk=removed_id).update(is_active=False)

        shown = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(shown["contact_ids"], [active_id])
        self.assertEqual(shown["unavailable_contacts"], [{"id": removed_id, "name": "visitor-removable-staff"}])
        saved = self.data(self.configure_greetings(admin, [active_id], [active], removed_contact_ids=[removed_id]))
        self.assertEqual(saved["unavailable_contacts"], [])
        audit_count = OperationLog.objects.filter(target_kind="chat_visitor").count()
        self.assertEqual(self.data(self.configure_greetings(
            admin, [active_id], [active], removed_contact_ids=[removed_id],
        )), saved)
        self.assertEqual(OperationLog.objects.filter(target_kind="chat_visitor").count(), audit_count)

        get_user_model().objects.filter(pk=removed_id).update(is_active=True)
        restored = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(restored["contact_ids"], [active_id])
        self.assertEqual(restored["greetings"], [active])
        guest = self.guest()
        self.register(guest)
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 1)
        self.assertEqual([row["id"] for row in self.data(guest.get(GUEST + "contacts/"))["contacts"]], [active_id])

    def test_unrelated_explicit_save_preserves_contact_reactivated_after_loading_settings(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, active_id = self.account("visitor-active-staff", SERVICE)
        _, reactivated_id = self.account("visitor-reactivated-staff", SERVICE)
        active = {"contact_id": active_id, "enabled": True, "text": "原欢迎语"}
        reactivated = {"contact_id": reactivated_id, "enabled": True, "text": "恢复后仍使用此欢迎语"}
        contacts = [active_id, reactivated_id]
        self.data(self.configure_greetings(admin, contacts, [active, reactivated]))
        users = get_user_model().objects
        users.filter(pk=reactivated_id).update(is_active=False)
        initial = self.data(admin.get(BASE + "settings/"))
        self.assertEqual(initial["contact_ids"], [active_id])
        self.assertEqual([row["id"] for row in initial["unavailable_contacts"]], [reactivated_id])

        users.filter(pk=reactivated_id).update(is_active=True)
        edited = {**initial["greetings"][0], "text": "改好的欢迎语"}
        saved = self.data(self.configure_greetings(
            admin, initial["contact_ids"], [edited], removed_contact_ids=[],
        ))
        self.assertEqual(saved["contact_ids"], contacts)
        self.assertEqual(saved["greetings"], [edited, reactivated])
        self.assertEqual(saved["unavailable_contacts"], [])

    def test_removed_contacts_must_be_distinct_positive_ids_in_new_format(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, staff_id = self.account("visitor-staff", SERVICE)
        for invalid in ([staff_id], [staff_id + 1, staff_id + 1], [0]):
            with self.subTest(removed_contact_ids=invalid):
                response = self.configure_greetings(admin, [staff_id], [], removed_contact_ids=invalid)
                self.assertNotEqual(response.json()["code"], 0)
        legacy = admin.put(BASE + "settings/", {
            "contact_ids": [staff_id], "greeting_sender_id": None, "greeting_text": "",
            "removed_contact_ids": [],
        }, content_type="application/json")
        self.assertNotEqual(legacy.json()["code"], 0)

    def test_returning_from_old_version_uses_legacy_greeting_edits(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, first_id = self.account("visitor-first-staff", SERVICE)
        _, second_id = self.account("visitor-second-staff", SERVICE)
        contacts = [first_id, second_id]
        rules = [
            {"contact_id": first_id, "enabled": True, "text": "新版第一条"},
            {"contact_id": second_id, "enabled": True, "text": "新版第二条"},
        ]
        self.data(self.configure_greetings(admin, contacts, rules))

        # Old application versions only update the single-sender fields.
        VisitorReceptionSettings.objects.update(greeting_sender_id=None, greeting_text="")
        cleared = self.data(admin.get(BASE + "settings/"))["greetings"]
        self.assertEqual(cleared, [
            {"contact_id": first_id, "enabled": False, "text": ""},
            {"contact_id": second_id, "enabled": False, "text": ""},
        ])
        guest = self.guest()
        self.assertIsNone(self.register(guest)["conversation_id"])

        VisitorReceptionSettings.objects.update(greeting_sender_id=second_id, greeting_text="旧页面新设置")
        resolved = self.data(admin.get(BASE + "settings/"))["greetings"]
        self.assertEqual(resolved, [cleared[0], {"contact_id": second_id, "enabled": True, "text": "旧页面新设置"}])
        guest = self.guest()
        registration = self.register(guest)
        history = self.data(guest.get(GUEST + f"conversations/{registration['conversation_id']}/messages/"))["messages"]
        self.assertEqual([(row["sender_id"], row["text"]) for row in history], [(second_id, "旧页面新设置")])
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 1)
        self.data(self.configure_greetings(admin, contacts, resolved))
        VisitorReceptionSettings.objects.update(greeting_sender_id=first_id, greeting_text="新版第一条")
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], [rules[0], cleared[1]])

    def test_deleting_the_projected_sender_keeps_other_welcomes(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, deleted_id = self.account("visitor-deleted-staff", SERVICE)
        _, remaining_id = self.account("visitor-remaining-staff", SERVICE)
        deleted = {"contact_id": deleted_id, "enabled": True, "text": "原首位欢迎语"}
        remaining = {"contact_id": remaining_id, "enabled": True, "text": "保留另一位欢迎语"}
        self.data(self.configure_greetings(admin, [deleted_id, remaining_id], [deleted, remaining]))
        get_user_model().objects.filter(pk=deleted_id).delete()
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], [remaining])
        guest = self.guest()
        registration = self.register(guest)
        history = self.data(guest.get(GUEST + f"conversations/{registration['conversation_id']}/messages/"))["messages"]
        self.assertEqual([(row["sender_id"], row["text"]) for row in history], [(remaining_id, "保留另一位欢迎语")])
        self.data(self.configure_greetings(admin, [remaining_id], [remaining]))
        self.assertEqual(VisitorReceptionSettings.objects.get().contact_ids, [remaining_id])

    def test_per_person_welcome_settings_are_scoped_to_the_workspace(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, staff_id = self.account("visitor-staff", SERVICE)
        one = {"contact_id": staff_id, "enabled": True, "text": "本工作室的欢迎语"}
        self.data(self.configure_greetings(admin, [staff_id], [one]))
        self.assertEqual(self.data(admin.get(BASE + "settings/"))["greetings"], [one])
        other_domain = "visitor-other.test.local"
        with tenant_context(self.other_tenant):
            other_admin = make_logged_in_client(
                domain=other_domain, username="visitor-other-admin", groups=(TENANT_ADMIN,), games=None,
            )
            make_logged_in_client(domain=other_domain, username="visitor-other-staff", groups=(SERVICE,), games=None)
            other_staff_id = get_user_model().objects.get(username="visitor-other-staff").pk
            two = {"contact_id": other_staff_id, "enabled": True, "text": "另一工作室的欢迎语"}
            self.data(self.configure_greetings(other_admin, [other_staff_id], [two]))
            self.assertEqual(self.data(other_admin.get(BASE + "settings/"))["greetings"], [two])
            self.assertEqual(admin.get(BASE + "settings/", SERVER_NAME=other_domain).status_code, 401)

    def test_registration_uses_same_browser_identity_and_sends_one_welcome(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        staff, staff_id = self.account("visitor-greeter", SERVICE)
        worker, worker_id = self.account("visitor-worker", WORKER)
        self.assertEqual(self.configure(worker, [staff_id]).status_code, 403)
        self.assertNotEqual(self.configure(admin, [worker_id]).json()["code"], 0)
        self.assertNotEqual(self.configure(admin, [staff_id], worker_id, "你好").json()["code"], 0)
        self.assertEqual(self.data(self.configure(admin, [staff_id], staff_id, "你好，欢迎来联系"))["contact_ids"], [staff_id])

        self.data(self.configure(admin, [staff_id], staff_id, "你好，欢迎来联系"))
        self.assertEqual(OperationLog.objects.filter(target_kind="chat_visitor").count(), 1)
        guest = self.guest()
        cookie = guest.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], GUEST)
        valid = {"phone": PHONE}
        self.assertEqual(self.write(guest, GUEST + "register/", valid).status_code, 403)
        self.assertEqual(self.write(guest, GUEST + "register/", valid, HTTP_ORIGIN="https://wrong.test",
                                     HTTP_X_CSRFTOKEN=guest.cookies["csrftoken"].value).status_code, 403)
        self.assertFalse(VisitorIdentity.objects.exists())
        registration = self.register(guest)
        self.assertEqual(self.register(guest), registration)
        self.assertEqual(self.register(guest, phone="13100000000"), registration)
        self.assertEqual(VisitorIdentity.objects.count(), 1)
        self.assertFalse(VisitorIdentity.objects.filter(token_digest=cookie.value).exists())
        self.assertEqual(self.data(guest.get(GUEST + "bootstrap/")), {"registered": True})
        self.assertEqual(self.data(guest.get(GUEST + "me/")), {"user": registration["user"]})
        self.assertEqual(registration["user"]["display_name"], "访客 130****0000")
        self.assertEqual(VisitorMessage.objects.count(), 1)

        conversation_id = registration["conversation_id"]
        history_url = f"conversations/{conversation_id}/messages/"
        history = self.data(guest.get(GUEST + history_url))["messages"]
        self.assertEqual([(row["text"], row["sender_id"]) for row in history], [("你好，欢迎来联系", staff_id)])
        desk = self.data(staff.get(DESK + "conversations/"))["conversations"]
        self.assertEqual([(row["id"], row["peer"]["name"], row["unread_count"]) for row in desk], [
            (conversation_id, "访客 13000000000", 0),
        ])
        self.assertEqual(
            self.data(guest.get(GUEST + f"conversations/{conversation_id}/"))["peer"]["name"], "visitor-greeter",
        )

        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 1)
        self.assertEqual(self.data(self.write(
            guest, GUEST + f"conversations/{conversation_id}/read/", {"seq": history[0]["seq"]},
            HTTP_X_CSRFTOKEN=guest.cookies["csrftoken"].value,
        )), {"read_seq": 1})
        self.assertEqual(self.data(guest.get(GUEST + "unread/"))["unread_count"], 0)
        self.assertEqual(self.data(guest.get(GUEST + history_url))["messages"], history)

    def test_guest_identity_and_desk_conversations_are_isolated(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        staff, staff_id = self.account("visitor-greeter", SERVICE)
        other_staff, other_id = self.account("visitor-other-staff", SERVICE)
        unlisted, _ = self.account("visitor-unlisted-staff", SERVICE)
        self.data(self.configure(admin, [staff_id, other_id], staff_id, "欢迎"))
        guest_a = self.guest()
        a = self.register(guest_a)
        guest_b = self.guest()
        b = self.register(guest_b)
        self.assertNotEqual(a["user"]["id"], b["user"]["id"])
        history_a = f"conversations/{a['conversation_id']}/messages/"
        self.assert_missing(guest_b.get(GUEST + history_a))
        self.assert_missing(other_staff.get(DESK + history_a))
        self.assertEqual(unlisted.get(DESK + "me/").status_code, 403)
        self.assertEqual([row["id"] for row in self.data(staff.get(DESK + "conversations/"))["conversations"]],
                         [b["conversation_id"], a["conversation_id"]])

        wrong_tenant = Client(SERVER_NAME="visitor-other.test.local")
        wrong_tenant.cookies[COOKIE_NAME] = guest_a.cookies[COOKIE_NAME].value
        self.assertEqual(wrong_tenant.get(GUEST + "me/").status_code, 401)
        self.assertEqual(wrong_tenant.get(GUEST + history_a).status_code, 401)
        self.assertEqual(self.write(wrong_tenant, GUEST + "register/", {"phone": PHONE}).status_code, 401)
        old_value = wrong_tenant.cookies[COOKIE_NAME].value
        self.assertEqual(self.data(wrong_tenant.get(GUEST + "bootstrap/")), {"registered": False})
        self.assertNotEqual(wrong_tenant.cookies[COOKIE_NAME].value, old_value)

        with tenant_context(self.tenant):
            digest = credential_digest(guest_a.cookies[COOKIE_NAME].value)
            expires_at = timezone.now() - timedelta(seconds=1)
            VisitorIdentity.objects.filter(token_digest=digest).update(expires_at=expires_at)
        self.assertEqual(guest_a.get(GUEST + "me/").status_code, 401)
        self.assertEqual(self.data(guest_a.get(GUEST + "bootstrap/")), {"registered": False})
        self.assertEqual(self.data(guest_b.get(GUEST + "me/"))["user"], b["user"])

    def test_stale_page_identity_cannot_follow_changed_browser_credentials(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        original_staff, original_staff_id = self.account("visitor-original-staff", SERVICE)
        staff, staff_id = self.account("visitor-next-staff", SERVICE)
        self.data(self.configure(admin, [staff_id, original_staff_id], staff_id, "欢迎"))
        first = self.register(self.guest())
        guest = self.guest()
        second = self.register(guest)
        room = f"conversations/{second['conversation_id']}/messages/"
        tokens = {"HTTP_X_CSRFTOKEN": guest.cookies["csrftoken"].value}
        old_guest = {"HTTP_X_CHAT_PAGE_USER": str(first["user"]["id"])}
        old_staff = {"HTTP_X_CHAT_PAGE_USER": str(original_staff_id)}
        self.assertEqual(guest.get(GUEST + room, **old_guest).status_code, 403)
        self.assertEqual(staff.get(DESK + "sync/?after=0", **old_staff).status_code, 403)
        self.assertEqual(staff.get(DESK + room, **old_staff).status_code, 403)
        self.assertEqual(self.write(
            guest, GUEST + room, {"text": "勿写到另一人的会话", "client_id": "expired"},
            **tokens, **old_guest,
        ).status_code, 403)
        self.assertEqual(self.write(
            staff, DESK + room, {"text": "勿写到另一位员工的会话", "client_id": "expired"}, **old_staff,
        ).status_code, 403)
        response = self.write(
            guest, GUEST + room, {"text": "附件说明", "client_id": "unsupported", "image": "attachment"}, **tokens,
        )
        self.assertNotEqual(response.json()["code"], 0)
        history = self.data(guest.get(GUEST + room, HTTP_X_CHAT_PAGE_USER=str(second["user"]["id"])))
        self.assertEqual([message["text"] for message in history["messages"]], ["欢迎"])
        self.data(staff.get(DESK + "sync/?after=0", HTTP_X_CHAT_PAGE_USER=str(staff_id)))
        self.data(self.configure(admin, [staff_id]))
        self.assertEqual(
            original_staff.get(DESK + "sync/?after=0", **old_staff).status_code, 403,
        )

    def test_search_reply_page_and_sync_keep_correct_messages_and_reads(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        staff, staff_id = self.account("张前台", SERVICE)
        other_staff, other_id = self.account("另一个接待", SERVICE)
        self.data(self.configure(admin, [staff_id, other_id]))
        guest = self.guest()
        self.assertIsNone(self.register(guest)["conversation_id"])
        csrf = {"HTTP_X_CSRFTOKEN": guest.cookies["csrftoken"].value}
        contacts = self.data(guest.get(GUEST + "contacts/?q=前台"))["contacts"]
        self.assertEqual([row["id"] for row in contacts], [staff_id])
        conversation = self.data(self.write(guest, GUEST + "conversations/", {"peer_id": staff_id}, **csrf))
        room_url = f"conversations/{conversation['id']}/"
        messages = room_url + "messages/"
        self.assert_missing(other_staff.get(DESK + messages))
        self.assertEqual(self.data(staff.get(DESK + "conversations/"))["conversations"], [])
        payload = {"text": "有事想问 🐱", "client_id": "guest-chat-1"}
        guest_message = self.data(self.write(guest, GUEST + messages, payload, **csrf))
        self.assertEqual(self.data(self.write(guest, GUEST + messages, payload, **csrf)), guest_message)
        self.assertEqual(self.data(staff.get(DESK + "unread/")), {"unread_count": 1})
        staff_inbox = self.data(staff.get(DESK + "conversations/"))
        summary = staff_inbox["conversations"][0]
        cursor = staff_inbox["cursor"]
        self.assertEqual(summary["first_unread_message_id"], guest_message["id"])
        self.assertEqual(summary["last_message"], guest_message)
        starts_at = self.data(staff.get(DESK + messages + f"?around={guest_message['id']}"))
        self.assertEqual(starts_at["messages"], [guest_message])
        self.assertEqual(self.data(self.write(
            staff, DESK + room_url + "read/", {"seq": guest_message["seq"]},
        )), {"read_seq": 1})
        self.assertEqual(self.data(staff.get(DESK + "unread/")), {"unread_count": 0})
        reply = self.data(self.write(
            staff, DESK + messages, {"text": "请说", "client_id": "desk-chat-1", "reply_to_id": guest_message["id"]},
        ))
        self.assertEqual(reply["reply_to_id"], guest_message["id"])
        self.assertEqual(reply["sender_id"], staff_id)
        self.assertEqual(self.data(guest.get(GUEST + messages + "?before=2"))["messages"], [guest_message])
        updates = self.data(guest.get(GUEST + f"sync/?after={cursor}"))
        self.assertEqual(updates["changes"], [
            {"kind": "conversation.read", "conversation_id": conversation["id"]},
            {"kind": "message.created", "conversation_id": conversation["id"], "message": reply},
        ])
        self.assertEqual(updates["cursor"], self.data(staff.get(DESK + "conversations/"))["cursor"])
        self.assertEqual(self.data(other_staff.get(DESK + "sync/?after=0"))["changes"], [])
        stale_room = VisitorConversation.objects.get(pk=conversation["id"])
        self.data(self.configure(admin, [other_id]))
        self.assertEqual(staff.get(DESK + "me/").status_code, 403)
        with self.assertRaises(PermissionDenied):
            mark_read(stale_room, as_guest=False, seq=reply["seq"], staff_id=stale_room.staff_id)
        self.assertEqual(self.write(guest, GUEST + messages, {"text": "在吗", "client_id": "new"}, **csrf).status_code, 403)

    def test_registration_and_sending_are_throttled_without_blocking_idempotent_retries(self):
        admin, _ = self.account("visitor-admin", TENANT_ADMIN)
        _, staff_id = self.account("visitor-greeter", SERVICE)
        self.data(self.configure(admin, [staff_id], staff_id, "欢迎"))
        first = self.guest()
        first_result = self.register(first)
        csrf = {"HTTP_X_CSRFTOKEN": first.cookies["csrftoken"].value}
        second = self.guest()
        with patch("apps.internal_chat.visitor_service.REGISTRATION_LIMIT", 1):
            self.assertEqual(self.register(first), first_result)
            self.assertEqual(self.write(
                second, GUEST + "register/", {"phone": PHONE}, HTTP_X_CSRFTOKEN=second.cookies["csrftoken"].value,
            ).status_code, 429)
        path = GUEST + f"conversations/{first_result['conversation_id']}/messages/"
        with patch("apps.internal_chat.visitor_service.MESSAGE_LIMIT", 1):
            msg = {"text": "测试", "client_id": "first"}
            original = self.data(self.write(first, path, msg, **csrf))
            self.assertEqual(self.data(self.write(first, path, msg, **csrf)), original)
            self.assertEqual(self.write(first, path, {**msg, "client_id": "second"}, **csrf).status_code, 429)

    @override_settings(TT_INTERNAL_CHAT_VISITORS_ENABLED=False)
    def test_opt_in_is_required(self):
        client = Client(SERVER_NAME=self.domain.domain)
        self.assertEqual(client.get(GUEST + "bootstrap/").status_code, 404)
