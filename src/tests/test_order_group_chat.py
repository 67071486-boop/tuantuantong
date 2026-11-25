import base64
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from PIL import Image

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import make_logged_in_client


class OrderGroupChatTests(TenantTestCase):
    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def follow_up_available(self, client, order_no, *, grouped=False):
        response = client.get("/api/business/orders/", {
            "keyword": order_no, "group_penalty_children": "true" if grouped else "false",
        })
        rows = self.data(response)["items"]
        return next(row["chat_follow_up_available"] for row in rows if row["order_no"] == order_no)

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_order_detail_viewer_can_preview_and_join_without_marking_history_read(self):
        from unittest.mock import patch

        from apps.internal_chat.service import _lock_cursor

        number = "20261116000100000008"
        order = Order.objects.create(
            game=Game.objects.create(name="订单群详情权限测试"),
            order_no=number, status=OrderStatus.UNASSIGNED,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
        )
        publisher = make_logged_in_client(
            domain=self.domain.domain, username="chat-tab-publisher", is_superuser=True,
        )
        viewer = make_logged_in_client(
            domain=self.domain.domain, username="chat-tab-viewer", groups=("群查看",),
            nodes=("order.view_content",),
        )
        denied = make_logged_in_client(domain=self.domain.domain, username="chat-tab-denied")
        out_of_game = make_logged_in_client(
            domain=self.domain.domain, username="chat-tab-out-of-game", groups=("群查看",), games=[],
        )
        worker = make_logged_in_client(
            domain=self.domain.domain, username="chat-tab-own-worker", groups=("简版详情",),
            nodes=("my_orders.send_to_my_system",), games=[],
        )
        detail = f"/api/business/orders/{number}/"
        order_chat = f"/api/internal-chat/orders/{number}/"
        self.data(publisher.post(detail + "throw-to-hall/", {}, content_type="application/json"))
        Performance.objects.update_or_create(
            order=order, emp_type=EmpType.MAIN,
            defaults={
                "user": get_user_model().objects.get(username="chat-tab-own-worker"),
                "temp_amount": Decimal("12.00"),
            },
        )
        self.data(viewer.get(detail))
        self.assertNotEqual(denied.get(detail).json()["code"], 0)
        self.assertNotEqual(out_of_game.get(detail).json()["code"], 0)
        self.assertNotEqual(out_of_game.get(order_chat).json()["code"], 0)
        self.assertNotEqual(worker.get(detail).json()["code"], 0)
        self.data(worker.get(f"/api/business/my-orders/{number}/detail/"))
        group = self.data(publisher.get(order_chat))
        messages = f"/api/internal-chat/conversations/{group['id']}/messages/"
        sent = self.data(publisher.post(messages, {
            "text": "入群前可预览", "client_id": "chat-tab-first",
        }, content_type="application/json"))
        preview = self.data(viewer.get(order_chat))
        self.assertFalse(preview["is_member"])
        self.assertFalse(self.data(worker.get(order_chat))["is_member"])
        self.assertIsNone(preview["self_member_id"])
        self.assertEqual(preview["local_order_no"], number)
        self.assertEqual(self.data(viewer.get(order_chat + "messages/"))["messages"], [sent])
        self.assertEqual(self.data(worker.get(order_chat + "messages/"))["messages"], [sent])
        self.assertNotEqual(viewer.get(messages).json()["code"], 0)
        self.assertNotEqual(denied.get(order_chat + "messages/").json()["code"], 0)
        self.assertNotEqual(denied.post(order_chat + "join/").json()["code"], 0)

        def lose_game_access_while_waiting_for_chat_lock():
            cursor = _lock_cursor()
            get_user_model().objects.get(username="chat-tab-viewer").staff_profile.games.clear()
            return cursor

        with patch("apps.internal_chat.order_groups._lock_cursor",
                   side_effect=lose_game_access_while_waiting_for_chat_lock):
            self.assertEqual(viewer.post(order_chat + "join/").json()["code"], 1002)
        self.assertFalse(self.data(viewer.get(order_chat))["is_member"])
        joined = self.data(viewer.post(order_chat + "join/"))
        self.assertTrue(joined["is_member"])
        self.assertEqual(self.data(viewer.post(order_chat + "join/"))["self_member_id"],
                         joined["self_member_id"])
        self.assertEqual(self.data(viewer.get(order_chat))["read_seq"], 0)
        self.assertEqual(self.data(viewer.get(messages))["messages"], [sent])
        self.assertEqual(joined["first_unread_message_id"], sent["id"])
        mention = {
            "text": "@chat-tab-viewer 请看", "client_id": "chat-tab-mention",
            "mention_member_ids": [joined["self_member_id"]],
        }
        second = self.data(publisher.post(messages, mention, content_type="application/json"))
        retry = self.data(publisher.post(messages, mention, content_type="application/json"))
        self.assertEqual(retry["id"], second["id"])
        self.assertEqual(second["mention_member_ids"], [joined["self_member_id"]])
        latest = self.data(viewer.get(order_chat))
        self.assertEqual(latest["first_unread_message_id"], sent["id"])
        self.assertEqual(latest["first_unread_mention_id"], second["id"])
        self.assertEqual(latest["unread_mentions_count"], 1)
        self.assertEqual(self.data(publisher.get(order_chat))["unread_mentions_count"], 0)
        prefs = f"/api/internal-chat/conversations/{group['id']}/preferences/"
        self.assertEqual(self.data(viewer.post(prefs, {
            "pinned": True, "remark": "我负责的订单",
        }, content_type="application/json")), {"pinned": True, "remark": "我负责的订单", "marked_unread": False})
        self.assertEqual(self.data(viewer.post(prefs, {"remark": "只改备注"})),
                         {"pinned": True, "remark": "只改备注", "marked_unread": False})
        self.assertTrue(self.data(viewer.get(order_chat))["pinned"])
        self.assertEqual(self.data(publisher.get(order_chat))["remark"], "")
        read = f"/api/internal-chat/conversations/{group['id']}/read/"
        publisher_cursor = self.data(publisher.get("/api/internal-chat/conversations/"))["cursor"]
        viewer_cursor = self.data(viewer.get("/api/internal-chat/conversations/"))["cursor"]
        self.data(viewer.post(read, {"seq": sent["seq"]}, content_type="application/json"))
        self.assertEqual(self.data(viewer.get(order_chat))["first_unread_message_id"], second["id"])
        self.assertEqual(self.data(publisher.get("/api/internal-chat/sync/", {
            "after": publisher_cursor,
        }))["changes"], [])
        self.assertEqual(self.data(viewer.get("/api/internal-chat/sync/", {
            "after": viewer_cursor,
        }))["changes"][0]["kind"], "conversation.read")
        self.data(viewer.post(read, {"seq": second["seq"]}, content_type="application/json"))
        after_read = self.data(viewer.get(order_chat))
        self.assertEqual(after_read["unread_mentions_count"], 0)
        self.assertIsNone(after_read["first_unread_mention_id"])
        self.assertEqual(self.data(viewer.post(prefs, {"marked_unread": True})), {
            "pinned": True, "remark": "只改备注", "marked_unread": True,
        })
        marked = self.data(viewer.get(order_chat))
        self.assertEqual((marked["marked_unread"], marked["unread_count"], marked["read_seq"]), (True, 0, second["seq"]))
        self.assertFalse(self.data(publisher.get(order_chat))["marked_unread"])
        self.data(viewer.get(order_chat + "messages/"))
        self.assertTrue(self.data(viewer.get(order_chat))["marked_unread"])
        self.data(viewer.post(read, {"seq": second["seq"]}, content_type="application/json"))
        self.assertFalse(self.data(viewer.get(order_chat))["marked_unread"])

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_publishing_creates_group_for_actual_publisher(self):
        game = Game.objects.create(name="订单群测试游戏")
        order = Order.objects.create(
            game=game, order_no="20261116000100000001", status=OrderStatus.UNASSIGNED,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
        )
        publisher = make_logged_in_client(
            domain=self.domain.domain, username="chat-order-publisher", is_superuser=True,
        )
        other = make_logged_in_client(
            domain=self.domain.domain, username="chat-order-unrelated", is_superuser=True,
        )
        self.data(publisher.post(
            f"/api/business/orders/{order.order_no}/throw-to-hall/", {},
            content_type="application/json",
        ))
        groups = self.data(publisher.get("/api/internal-chat/conversations/"))["conversations"]
        self.assertEqual([(row["kind"], row["title"]) for row in groups], [
            ("order", "订单-20261116000100000001"),
        ])
        self.assertEqual([p["name"] for p in groups[0]["members"]], ["chat-order-publisher"])
        publisher_member = groups[0]["members"][0]
        self.assertEqual(publisher_member["studio_name"], self.tenant.name)
        self.assertEqual(publisher_member["order_role"], "publisher")
        self.assertEqual(self.data(other.get("/api/internal-chat/conversations/"))["conversations"], [])

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_bound_sync_account_cannot_join_source_group_as_worker_or_follower(self):
        from django.contrib.auth import get_user_model

        from apps.business.models import PeerDownstreamBinding
        from apps.business.peer_sync import hash_token, new_token

        game = Game.objects.create(name="同步账号隔离测试游戏")
        number = "20261116000100000003"
        Order.objects.create(
            game=game, order_no=number, status=OrderStatus.UNASSIGNED,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
        )
        publisher = make_logged_in_client(
            domain=self.domain.domain, username="chat-assignment-publisher", is_superuser=True,
        )
        binding_account = make_logged_in_client(
            domain=self.domain.domain, username="chat-bound-account",
            groups=("代练员",), is_superuser=True,
        )
        make_logged_in_client(
            domain=self.domain.domain, username="chat-source-worker", groups=("代练员",),
        )
        account = get_user_model().objects.get(username="chat-bound-account")
        worker = get_user_model().objects.get(username="chat-source-worker")
        PeerDownstreamBinding.objects.create(
            account=account, peer_base_url="https://chat-machine-binding.test.invalid",
            token_in_hash=hash_token(new_token()), token_out=new_token(),
        )

        def post(client, endpoint, data):
            return self.data(client.post(f"/api/business/orders/{number}/{endpoint}/", data,
                                         content_type="application/json"))

        post(publisher, "throw-to-hall", {})
        group = self.data(publisher.get("/api/internal-chat/conversations/"))["conversations"][0]
        post(publisher, "assign", {"user_id": account.pk})
        rejected = binding_account.get(f"/api/internal-chat/conversations/{group['id']}/messages/")
        self.assertEqual(rejected.json()["code"], 1002)
        self.assertFalse(self.follow_up_available(binding_account, number))
        post(binding_account, "assign", {"user_id": worker.pk, "chat_follow_up": True})
        group = self.data(publisher.get("/api/internal-chat/conversations/"))["conversations"][0]
        self.assertEqual({member["name"] for member in group["members"]},
                         {"chat-assignment-publisher", "chat-source-worker"})
        self.assertEqual(self.data(binding_account.get("/api/internal-chat/conversations/"))["conversations"], [])
        self.data(binding_account.get(f"/api/business/orders/{number}/"))
        joined = self.data(binding_account.post(f"/api/internal-chat/orders/{number}/join/"))
        self.assertTrue(joined["is_member"])
        self.assertEqual(
            self.data(binding_account.get("/api/internal-chat/conversations/"))["conversations"][0]["id"],
            group["id"],
        )

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True, DEPOSIT_ENFORCED=False)
    def test_linked_workspaces_share_history_and_revoke_replaced_worker_access(self):
        from contextlib import contextmanager
        from copy import deepcopy
        from urllib.parse import urlparse
        from unittest.mock import patch
        from uuid import uuid4

        import httpx
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.utils import timezone
        from django_tenants.utils import tenant_context

        from apps.business import peer_worker
        from apps.business.models import PeerDownstreamBinding, PeerHallMirror, PeerSyncEvent, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token
        from apps.internal_chat.models import OrderGroup
        from apps.platform.provisioning import provision_tenant
        from tests.p5_auth_helpers import grant_nodes

        original_number = "20261116000100000002"
        source_domain = self.domain.domain
        second_domain = "order-chat-second.test.local"
        source_url = f"https://{source_domain}"
        second_url = f"https://{second_domain}"
        with self.settings(ALLOWED_HOSTS=[source_domain, second_domain]):
            second, _ = provision_tenant(
                schema_name="order_chat_second", name="订单群第二工作室", domain=second_domain,
            )
            def clean_second():
                with tenant_context(second):
                    second.delete(force_drop=True)

            self.addCleanup(clean_second)

            def user(domain, name, *, admin=False, groups=(), nodes=()):
                client = make_logged_in_client(
                    domain=domain, username=name, is_superuser=admin, groups=groups, nodes=nodes,
                )
                return client, get_user_model().objects.get(username=name)

            def post(client, path, body):
                return client.post(path, body, content_type="application/json")

            def chat(client, path="conversations/"):
                return self.data(client.get("/api/internal-chat/" + path))

            def messages(client, conversation):
                return chat(client, f"conversations/{conversation}/messages/")["messages"]

            def send(client, group, text):
                return self.data(post(client, f"/api/internal-chat/conversations/{group}/messages/",
                                      {"text": text, "client_id": text}))

            routed_chat_messages = []
            routed_albums = []

            def resend(event, payload, key):
                return self.data(Client(SERVER_NAME=second_domain).post(
                    "/api/business/peer-sync/inbound/events/", {"events": [{
                        "kind": event.kind, "target_ref": event.target_ref,
                        "payload": payload, "idempotency_key": key,
                    }]}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {outgoing}",
                ))

            @contextmanager
            def transport(method, url, *, json, headers, timeout):
                current_domain = urlparse(url)
                for event in json.get("events", ()):
                    if event["kind"] == "chat.order_message":
                        payload = event["payload"]
                        routed_chat_messages.append((
                            payload["message_id"], payload["revoked"],
                            "image" in payload, "image_pending" in payload,
                        ))
                    if event["kind"] == "chat.order_album":
                        routed_albums.append(deepcopy(event["payload"]))
                with tenant_context(self.tenant if current_domain.netloc == source_domain else second):
                    response = Client(SERVER_NAME=current_domain.netloc).post(
                        current_domain.path, json, content_type="application/json",
                        HTTP_AUTHORIZATION=headers["Authorization"],
                    )
                    yield httpx.Response(response.status_code, content=response.content)

            def drain(tenant, kind):
                with tenant_context(tenant):
                    for pk in PeerSyncEvent.objects.filter(
                        direction="out", status=PeerSyncEvent.Status.PENDING, kind=kind,
                    ).order_by("pk").values_list("pk", flat=True):
                        self.assertEqual(peer_worker.deliver_one(pk, now=timezone.now()), "done")

            outgoing, incoming, unrelated_token = new_token(), new_token(), new_token()
            game = Game.objects.create(name="订单群共享游戏")
            publisher, _ = user(source_domain, "chat-source-publisher", admin=True)
            source_viewer, _ = user(source_domain, "chat-source-viewer", admin=True)
            source_worker, source_worker_user = user(source_domain, "chat-new-source-worker", groups=("代练员",))
            _, account = user(source_domain, "chat-peer-account", groups=("代练员",))
            grant_nodes(account, "grab_hall.take_order")
            PeerDownstreamBinding.objects.create(
                account=account, peer_base_url=second_url, token_in_hash=hash_token(incoming), token_out=outgoing,
            )
            _, unrelated = user(source_domain, "chat-unrelated-workspace-binding")
            PeerDownstreamBinding.objects.create(
                account=unrelated, peer_base_url="https://chat-unrelated.test.invalid",
                token_in_hash=hash_token(unrelated_token), token_out=new_token(), hall_push_enabled=False,
            )
            Order.objects.create(
                game=game, order_no=original_number, status=OrderStatus.UNASSIGNED,
                total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
            )
            with tenant_context(second):
                Game.objects.create(name=game.name)
                acceptor, _ = user(second_domain, "chat-second-acceptor", admin=True)
                dispatcher, binder = user(second_domain, "chat-second-dispatcher", admin=True)
                limited, _ = user(
                    second_domain, "chat-list-dispatcher", groups=("派单组",),
                    nodes=("order.list", "order.assign_taker"),
                )
                old_worker, old_user = user(second_domain, "chat-old-worker", groups=("代练员",))
                new_worker, new_user = user(second_domain, "chat-new-worker", groups=("代练员",))
                second_outsider, _ = user(second_domain, "chat-second-outsider", admin=True)
                PeerUpstreamBinding.objects.create(
                    peer_name="订单群第一工作室", peer_base_url=source_url,
                    peer_account=account.username, token_in_hash=hash_token(outgoing),
                    token_out=incoming, local_base_url=second_url, bound_by=binder,
                    hall_sync_enabled=True,
                )

            self.data(post(publisher, f"/api/business/orders/{original_number}/throw-to-hall/", {}))
            source_group = chat(publisher)["conversations"][0]
            send(publisher, source_group["id"], "上家的入群前消息")

            with patch("apps.business.peer_client.httpx.stream", side_effect=transport):
                drain(self.tenant, "hall.throw")
                with tenant_context(second):
                    mirror = PeerHallMirror.objects.get(peer_order_no=original_number)
                    claimed = self.data(post(acceptor, f"/api/business/hall/peer/{mirror.pk}/claim/", {}))
                    self.assertEqual(claimed["status"], "promoted")
                    mirror.refresh_from_db()
                    local_number = mirror.promoted_order.order_no
                    group = chat(acceptor)["conversations"][0]["id"]
                    self.assertEqual(chat(dispatcher)["conversations"], [])
                    self.assertEqual(chat(second_outsider)["conversations"], [])
                    self.assertNotEqual(local_number, original_number)
                    self.assertEqual(second_outsider.get(
                        f"/api/internal-chat/orders/{original_number}/messages/",
                    ).json()["code"], 1002)
                    self.assertFalse(chat(second_outsider, f"orders/{local_number}/")["is_member"])
                    roster_event = PeerSyncEvent.objects.get(direction="out", kind="chat.order_roster")
                    wire = {
                        "kind": roster_event.kind, "target_ref": roster_event.target_ref,
                        "payload": roster_event.payload, "idempotency_key": roster_event.idempotency_key,
                    }

                with tenant_context(self.tenant):
                    self.assertFalse(self.follow_up_available(publisher, original_number))
                    wrong_binding = self.data(Client(SERVER_NAME=source_domain).post(
                        "/api/business/peer-sync/inbound/events/", {"events": [wire]},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {unrelated_token}",
                    ))
                    self.assertEqual((wrong_binding["applied"], wrong_binding["skipped"]), (0, 1))
                    self.assertEqual([row["name"] for row in chat(publisher)["conversations"][0]["members"]],
                                     ["chat-source-publisher"])
                    legacy_wire = {
                        **wire, "idempotency_key": "legacy-roster-without-studio",
                        "payload": {
                            **wire["payload"], "members": [
                                {key: value for key, value in member.items() if key not in {"studio_name", "order_role"}}
                                for member in wire["payload"]["members"]
                            ],
                        },
                    }
                    legacy_result = self.data(Client(SERVER_NAME=source_domain).post(
                        "/api/business/peer-sync/inbound/events/", {"events": [legacy_wire]},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {incoming}",
                    ))
                    self.assertEqual((legacy_result["applied"], legacy_result["skipped"]), (1, 0))
                    acceptor_member = next(
                        row for row in chat(publisher)["conversations"][0]["members"]
                        if row["name"] == "chat-second-acceptor"
                    )
                    self.assertEqual(acceptor_member["studio_name"], "")
                drain(second, "chat.order_roster")
                with tenant_context(self.tenant):
                    acceptor_member = next(
                        row for row in chat(publisher)["conversations"][0]["members"]
                        if row["name"] == "chat-second-acceptor"
                    )
                    self.assertEqual(acceptor_member["studio_name"], second.name)
                    self.assertIn(acceptor_member["order_role"], ("acceptor", "worker"))
                    self.assertIsNone(acceptor_member["left_at"])
                drain(self.tenant, "chat.order_message")

                with tenant_context(second):
                    assign = f"/api/business/orders/{local_number}/assign/"
                    self.assertEqual(limited.get(f"/api/business/orders/{local_number}/").status_code, 403)
                    self.assertTrue(self.follow_up_available(limited, local_number, grouped=True))
                    self.assertTrue(self.follow_up_available(dispatcher, local_number))
                    self.data(post(dispatcher, assign, {"user_id": old_user.pk}))
                    self.assertEqual(chat(dispatcher)["conversations"], [])
                    self.assertEqual([m["text"] for m in messages(old_worker, group)], ["上家的入群前消息"])
                    send(old_worker, group, "旧打手的订单记录")
                    old_cursor = chat(old_worker)["cursor"]
                    assigned = self.data(post(dispatcher, "/api/business/orders/batch/assign/", {
                        "order_nos": [local_number], "dry_run": False,
                        "user_id": new_user.pk, "chat_follow_up": True,
                    }))
                    self.assertEqual(assigned["applied"], [local_number])
                    self.assertEqual(chat(old_worker)["conversations"], [])
                    prefix = f"/api/internal-chat/conversations/{group}/messages/"
                    denied = [
                        old_worker.get(prefix),
                        old_worker.get(prefix + "search/", {"q": "旧打手"}),
                        post(old_worker, prefix, {"text": "退群后发言", "client_id": "denied"}),
                        second_outsider.get(prefix),
                    ]
                    self.assertEqual([response.json()["code"] for response in denied], [1002] * 4)
                    sync = chat(old_worker, f"sync/?after={old_cursor}")["changes"]
                    self.assertEqual([(row["kind"], row["message"]) for row in sync], [
                        ("conversation.removed", None),
                    ])
                    new_group = chat(new_worker)["conversations"][0]
                    self.assertFalse(new_group["can_pin_messages"])
                    self.assertEqual(post(new_worker, f"/api/internal-chat/conversations/{group}/pin/", {
                        "message_id": messages(new_worker, group)[0]["id"],
                    }).json()["code"], 403)
                    self.assertEqual(new_group["unread_count"], 2)
                    roster = {row["name"]: row for row in new_group["members"]}
                    self.assertIsNotNone(roster["chat-old-worker"]["left_at"])
                    self.assertEqual(roster["chat-old-worker"]["studio_name"], second.name)
                    self.assertIsNone(roster["chat-new-worker"]["left_at"])
                    self.assertEqual(roster["chat-new-worker"]["studio_name"], second.name)
                    self.assertEqual(roster["chat-new-worker"]["order_role"], "worker")
                    self.assertIsNone(roster["chat-second-dispatcher"]["left_at"])
                    self.assertEqual(roster["chat-second-dispatcher"]["order_role"], "follower")
                    self.assertEqual(roster["chat-source-publisher"]["studio_name"], self.tenant.name)
                    self.assertEqual(roster["chat-source-publisher"]["order_role"], "publisher")
                    self.assertEqual([m["text"] for m in messages(new_worker, group)], [
                        "上家的入群前消息", "旧打手的订单记录",
                    ])
                    older_peer_payload = PeerSyncEvent.objects.filter(
                        direction="out", kind="chat.order_roster", status=PeerSyncEvent.Status.PENDING,
                    ).latest("pk")
                    legacy_member = next(
                        item for item in older_peer_payload.payload["members"] if item["key"] == f"b:{old_user.pk}"
                    )
                    legacy_member.pop("studio_name")
                    legacy_member.pop("order_role")
                    older_peer_payload.save(update_fields=["payload"])

                drain(second, "chat.order_roster")
                drain(second, "chat.order_message")
                with tenant_context(self.tenant):
                    roster = {row["name"]: row for row in chat(publisher)["conversations"][0]["members"]}
                    self.assertIsNotNone(roster["chat-old-worker"]["left_at"])
                    self.assertEqual(roster["chat-old-worker"]["studio_name"], second.name)
                    self.assertIsNone(roster["chat-new-worker"]["left_at"])
                    self.assertEqual(roster["chat-new-worker"]["studio_name"], second.name)
                    self.assertEqual(roster["chat-new-worker"]["order_role"], "worker")
                    self.assertEqual([m["text"] for m in messages(publisher, source_group["id"])], [
                        "上家的入群前消息", "旧打手的订单记录",
                    ])
                    existing_assignment = PeerSyncEvent.objects.filter(
                        kind="order.assign", target_ref=original_number,
                    ).first()
                    replay = {
                        "kind": "order.assign", "target_ref": original_number,
                        "occurred_at": timezone.now().isoformat(), "idempotency_key": "synthetic-pool-return",
                        "payload": {**existing_assignment.payload, "origin_order_no": "20261116000100001300"},
                    }
                    replay["payload"].pop("chat_group", None)

                with tenant_context(second):
                    pool_returned = self.data(Client(SERVER_NAME=second_domain).post(
                        "/api/business/peer-sync/inbound/events/", {"events": [replay]},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {outgoing}",
                    ))
                    self.assertEqual((pool_returned["applied"], pool_returned["skipped"]), (1, 0))
                    self.assertEqual(Order.objects.get(order_no=local_number).status, OrderStatus.UNASSIGNED)
                    self.assertEqual(chat(new_worker)["conversations"], [])
                    self.data(post(dispatcher, assign, {"user_id": new_user.pk}))
                    self.data(post(dispatcher, f"/api/business/orders/{local_number}/clear-taker/", {}))
                    self.assertEqual(chat(new_worker)["conversations"], [])
                    roster = {row["name"]: row["left_at"] for row in chat(acceptor)["conversations"][0]["members"]}
                    self.assertIsNotNone(roster["chat-old-worker"])
                    self.assertIsNotNone(roster["chat-new-worker"])
                    self.assertEqual([m["text"] for m in messages(dispatcher, group)], [
                        "上家的入群前消息", "旧打手的订单记录",
                    ])

                drain(second, "chat.order_roster")
                images = []
                with tenant_context(self.tenant):
                    image_send_path = f"/api/internal-chat/conversations/{source_group['id']}/messages/"
                    for color in ("red", "blue"):
                        buffer = BytesIO()
                        Image.new("RGB", (590, 590), color=color).save(buffer, format="PNG", compress_level=0)
                        image = {"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
                        images.append(image)
                        self.data(post(publisher, image_send_path, {
                            "text": f"同步图片-{color}", "client_id": f"image-{color}", "image": image,
                        }))
                    pending = PeerSyncEvent.objects.filter(
                        direction="out", kind="chat.order_message", status=PeerSyncEvent.Status.PENDING,
                    )
                    self.assertEqual(pending.count(), 2)
                    for payload in pending.values_list("payload", flat=True):
                        self.assertFalse("image" in payload, "待投递台账不应存图片本体")
                        self.assertIs(payload["image_pending"], True)
                    offline = self.data(post(publisher, image_send_path, {
                        "text": "离线期间撤回的图", "client_id": "image-revoke-offline", "image": images[0],
                    }))
                    offline_wire_id = pending.get(payload__text="离线期间撤回的图").payload["message_id"]
                    self.data(post(publisher, image_send_path + f"{offline['id']}/revoke/", {}))
                drain(self.tenant, "chat.order_message")
                self.assertEqual(
                    [details for details in routed_chat_messages if details[0] == offline_wire_id],
                    [(offline_wire_id, True, False, False)] * 2,
                )
                with tenant_context(second):
                    history = messages(acceptor, group)
                    [offline_copy] = [message for message in history if message["revoked"]]
                    self.assertEqual(offline_copy["text"], "")
                    self.assertNotIn("image", offline_copy)
                    received = [message for message in history if message.get("image")]
                    self.assertEqual([row["text"] for row in received], ["同步图片-red", "同步图片-blue"])
                    for message, image in zip(received, images, strict=True):
                        image_path = f"conversations/{group}/messages/{message['id']}/image/"
                        self.assertEqual(message["image"], {"mime": "image/png", "width": 590, "height": 590})
                        actual = chat(acceptor, image_path)
                        self.assertEqual(actual["mime"], image["mime"])
                        self.assertTrue(actual["data"] == image["data"], "同步后的图片内容不同")
                        preview_image = chat(second_outsider, f"orders/{local_number}/messages/{message['id']}/image/")
                        self.assertTrue(preview_image == image, "详情权限的预览图与会话图不同")
                        for client in (old_worker, second_outsider):
                            denied = client.get("/api/internal-chat/" + image_path)
                            self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))
                with tenant_context(self.tenant):
                    sent = next(message for message in messages(publisher, source_group["id"])
                                if message["text"] == "同步图片-red")
                    self.data(post(publisher, image_send_path + f"{sent['id']}/revoke/", {}))
                drain(self.tenant, "chat.order_message")
                with tenant_context(second):
                    first_id = received[0]["id"]
                    revoked = next(message for message in messages(acceptor, group) if message["id"] == first_id)
                    self.assertTrue(revoked["revoked"])
                    self.assertNotIn("image", revoked)
                    denied = acceptor.get(f"/api/internal-chat/conversations/{group}/messages/{first_id}/image/")
                    self.assertEqual((denied.json()["code"], denied.json()["data"]), (1002, None))

                with tenant_context(self.tenant):
                    source_preview = f"orders/{original_number}/"
                    self.assertFalse(chat(source_viewer, source_preview)["is_member"])
                    source_member = self.data(post(
                        source_viewer, "/api/internal-chat/" + source_preview + "join/", {},
                    ))["self_member_id"]
                drain(self.tenant, "chat.order_roster")
                with tenant_context(second):
                    remote_view = chat(acceptor, f"orders/{local_number}/")
                    self.assertEqual(remote_view["local_order_no"], local_number)
                    source_replica = next(
                        row for row in remote_view["members"] if row["name"] == "chat-source-viewer"
                    )
                    self.assertIsNone(source_replica["left_at"])
                    self.data(post(acceptor, f"/api/internal-chat/conversations/{group}/messages/", {
                        "text": "@chat-source-viewer 可查进度", "client_id": "cross-order-mention",
                        "mention_member_ids": [source_replica["id"]],
                    }))
                drain(second, "chat.order_message")
                with tenant_context(self.tenant):
                    source_view = chat(source_viewer, source_preview)
                    self.assertEqual(source_view["unread_mentions_count"], 1)
                    self.assertEqual(source_view["last_message"]["mention_member_ids"], [source_member])

                with tenant_context(self.tenant):
                    source_pin = image_send_path.replace("messages/", "pin/")
                    album_photos = []
                    for color in ("pink", "green"):
                        buffer = BytesIO()
                        Image.new("RGB", (13, 10), color=color).save(buffer, format="PNG")
                        album_photos.append({"mime": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")})
                    album = self.data(post(publisher, image_send_path, {
                        "text": "同时发两张交接图", "client_id": "cross-order-album", "images": album_photos,
                    }))["messages"]
                    self.assertFalse(source_view["can_pin_messages"])
                    self.assertEqual(post(source_viewer, source_pin, {"message_id": album[0]["id"]}).json()["code"], 403)
                    self.assertEqual(self.data(post(publisher, source_pin, {"message_id": album[0]["id"]})), {
                        "pinned_message_id": album[0]["id"],
                    })
                    old_pin = PeerSyncEvent.objects.get(direction="out", kind="chat.order_pin")
                    album_event = PeerSyncEvent.objects.get(direction="out", kind="chat.order_album")
                    self.assertEqual(len(album_event.payload["messages"]), 2)
                    self.assertTrue(all(part.get("image_pending") and "image" not in part
                                        for part in album_event.payload["messages"]))

                drain(self.tenant, "chat.order_pin")
                with tenant_context(second):
                    summary = chat(acceptor, f"orders/{local_number}/")
                    self.assertIsNone(summary["pinned_message_id"])
                    unread_before_album = summary["unread_count"]
                    before_album = chat(acceptor)["cursor"]
                    cross_client = Client(SERVER_NAME=second_domain)
                    cross_client.cookies = deepcopy(source_viewer.cookies)
                    wrong_session_pin = post(cross_client, f"/api/internal-chat/conversations/{group}/pin/",
                                             {"message_id": None})
                    self.assertEqual((wrong_session_pin.status_code, wrong_session_pin.json()["code"]), (401, 401))
                drain(self.tenant, "chat.order_album")
                with tenant_context(second):
                    received_album = [m for m in messages(acceptor, group) if m.get("album_id") == album[0]["album_id"]]
                    self.assertEqual([m["text"] for m in received_album], ["同时发两张交接图", ""])
                    for message, image in zip(received_album, album_photos, strict=True):
                        self.assertEqual(chat(acceptor, f"conversations/{group}/messages/{message['id']}/image/"), image)
                    summary = chat(acceptor, f"orders/{local_number}/")
                    self.assertEqual(summary["pinned_message_id"], received_album[0]["id"])
                    self.assertEqual(summary["unread_count"], unread_before_album + 1)
                    self.assertIn("conversation.pin_changed", [change["kind"] for change in
                                                               chat(acceptor, f"sync/?after={before_album}")["changes"]])
                    self.assertTrue(chat(dispatcher, f"orders/{local_number}/")["can_pin_messages"])
                    remote_pin = f"/api/internal-chat/conversations/{group}/pin/"
                    self.assertEqual(self.data(post(dispatcher, remote_pin, {"message_id": None})), {"pinned_message_id": None})
                    late_pin = resend(old_pin, old_pin.payload, "delayed-original-pin")
                    self.assertEqual((late_pin["applied"], late_pin["skipped"]), (1, 0))
                    self.assertIsNone(chat(acceptor, f"orders/{local_number}/")["pinned_message_id"])
                drain(second, "chat.order_pin")
                with tenant_context(self.tenant):
                    self.assertIsNone(chat(publisher, source_preview)["pinned_message_id"])
                    self.data(post(publisher, source_pin, {"message_id": album[0]["id"]}))
                drain(self.tenant, "chat.order_pin")
                with tenant_context(self.tenant):
                    self.data(post(publisher, image_send_path + f"{album[0]['id']}/revoke/", {}))
                    self.assertIsNone(chat(publisher, source_preview)["pinned_message_id"])
                drain(self.tenant, "chat.order_album")
                with tenant_context(second):
                    self.assertIsNone(chat(acceptor, f"orders/{local_number}/")["pinned_message_id"])
                    self.assertEqual(post(acceptor, remote_pin, {"message_id": received_album[0]["id"]}).json()["code"], 1002)
                    current = messages(acceptor, group)
                    replay = resend(album_event, routed_albums[0], "delayed-album-before-revoke")
                    self.assertEqual((replay["applied"], replay["skipped"]), (1, 0))
                    self.assertEqual(messages(acceptor, group), current)
                    conflicting = deepcopy(routed_albums[0])
                    conflicting["album_id"] = str(uuid4())
                    conflicting["messages"][0]["message_id"] = str(uuid4())
                    rejected = resend(album_event, conflicting, "album-with-conflicting-second-photo")
                    self.assertEqual((rejected["applied"], rejected["skipped"]), (0, 1))
                    self.assertEqual(messages(acceptor, group), current)
                    for revision in (2**63 - 1, 2**63):
                        rejected = resend(old_pin, {**old_pin.payload, "revision": revision}, f"pin-overflow-{revision}")
                        self.assertEqual((rejected["applied"], rejected["skipped"]), (0, 1))

                with tenant_context(self.tenant):
                    blue_image = next(row for row in messages(publisher, source_group["id"])
                                      if row["text"] == "同步图片-blue")
                    self.data(post(publisher, source_pin, {"message_id": album[1]["id"]}))
                    source_operation = PeerSyncEvent.objects.get(
                        direction="out", kind="chat.order_pin", status=PeerSyncEvent.Status.PENDING,
                    ).payload
                with tenant_context(second):
                    self.data(post(acceptor, remote_pin, {"message_id": received[1]["id"]}))
                    receiver_operation = PeerSyncEvent.objects.get(
                        direction="out", kind="chat.order_pin", status=PeerSyncEvent.Status.PENDING,
                    ).payload
                self.assertEqual(source_operation["revision"], receiver_operation["revision"])
                album_wins = source_operation["operation_id"] > receiver_operation["operation_id"]
                drain(self.tenant, "chat.order_pin")
                drain(second, "chat.order_pin")
                with tenant_context(self.tenant):
                    self.assertEqual(chat(publisher, source_preview)["pinned_message_id"],
                                     album[1]["id"] if album_wins else blue_image["id"])
                with tenant_context(second):
                    self.assertEqual(chat(acceptor, f"orders/{local_number}/")["pinned_message_id"],
                                     received_album[1]["id"] if album_wins else received[1]["id"])
                    persisted = OrderGroup.objects.get(conversation_id=group)
                    OrderGroup.objects.filter(pk=persisted.pk).update(pin_revision=2**63 - 1)
                    at_ceiling = post(acceptor, remote_pin, {"message_id": None})
                    self.assertEqual((at_ceiling.status_code, at_ceiling.json()["code"]), (200, 1000))
                    self.assertEqual(OrderGroup.objects.get(pk=persisted.pk).pinned_message_wire_id,
                                     persisted.pinned_message_wire_id)

                with tenant_context(self.tenant):
                    send(publisher, source_group["id"], "断开前尚未发出的消息")
                    self.data(post(publisher, f"/api/business/orders/{original_number}/clear-taker/", {}))
                    queued = PeerSyncEvent.objects.get(
                        kind="chat.order_message", status=PeerSyncEvent.Status.PENDING,
                    )
                    self.assertEqual(peer_worker.deliver_one(queued.pk, now=timezone.now()), "stale-void")
                    send(publisher, source_group["id"], "断开后的上家本地消息")
                    self.assertFalse(PeerSyncEvent.objects.filter(
                        kind="chat.order_message", status=PeerSyncEvent.Status.PENDING,
                    ).exists())
                    old_claim = self.data(Client(SERVER_NAME=source_domain).post(
                        "/api/business/peer-sync/inbound/events/", {"events": [wire]},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {incoming}",
                    ))
                    self.assertEqual((old_claim["applied"], old_claim["skipped"]), (0, 1))
                    active = [row["name"] for row in chat(publisher)["conversations"][0]["members"]
                              if row["left_at"] is None]
                    self.assertEqual(active, ["chat-source-publisher", "chat-source-viewer"])
                    self.assertTrue(chat(publisher, f"orders/{original_number}/")["can_join"])
                    self.assertTrue(self.follow_up_available(publisher, original_number))
                    self.data(post(publisher, f"/api/business/orders/{original_number}/assign/", {
                        "user_id": source_worker_user.pk,
                    }))
                    self.assertEqual(chat(source_worker)["conversations"][0]["id"], source_group["id"])

                drain(self.tenant, "chat.order_detached")
                with tenant_context(second):
                    self.assertEqual(chat(acceptor)["conversations"], [])
                    self.assertEqual(chat(dispatcher)["conversations"], [])
                    self.assertFalse(self.follow_up_available(dispatcher, local_number))
                    archived_path = f"orders/{local_number}/"
                    self.data(dispatcher.get(f"/api/business/orders/{local_number}/"))
                    archived = chat(dispatcher, archived_path)
                    self.assertFalse(archived["is_member"])
                    self.assertFalse(archived["can_join"])
                    self.assertFalse(archived["can_pin_messages"])
                    self.assertIn("同步图片-blue", [
                        row["text"] for row in chat(dispatcher, archived_path + "messages/")["messages"]
                    ])
                    refused = post(dispatcher, "/api/internal-chat/" + archived_path + "join/", {})
                    self.assertEqual((refused.status_code, refused.json()["code"]), (403, 403))
                    self.assertEqual(refused.json()["msg"], "群聊已结束，仅可查看历史消息")
                    self.assertEqual(chat(dispatcher)["conversations"], [])
                drain(self.tenant, "order.withdraw")
