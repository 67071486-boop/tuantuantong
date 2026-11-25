import base64
import json
from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.business import peer_worker
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_sync import hash_token
from apps.business.shared_peer_sync import chat_cutover_blocker
from apps.core.error_codes import ErrorCode
from apps.internal_chat import peer_chat
from apps.internal_chat.models import Conversation, Message, OrderChatIntent, OrderChatPeerLink, OrderGroup
from apps.platform.provisioning import provision_tenant
from chat_service.protocol import sign_request
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client
from tests.test_chat_service_bridge import ServiceClient


class CorePeerBridgeTests(TenantTestCase):
    inbox = "/api/business/peer-sync/inbound/events/"
    callback = "/api/internal-chat/service/peer-chat/"

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def callback_request(self, domain, key, payload, path=callback):
        body = json.dumps(payload).encode()
        headers = sign_request(f"{key}-synthetic-secret", key, "POST", path, body)
        return ServiceClient(SERVER_NAME=domain).post(
            path, body, content_type="application/json", headers=headers,
        )

    def peer_request(self, domain, token, *events):
        return Client(SERVER_NAME=domain, HTTP_AUTHORIZATION=f"Bearer {token}").post(
            self.inbox, {"events": list(events)}, content_type="application/json",
        )

    def test_due_jobs_wait_for_chat_head_without_blocking_other_groups_or_orders(self):
        now = timezone.now()

        def event(group, *, due=None, target_type="chat_core"):
            return PeerSyncEvent.objects.create(
                direction="out", target_type=target_type, binding_role="downstream", binding_id="25",
                kind="chat.order_message" if target_type == "chat_core" else "order.assign",
                target_ref=group, idempotency_key=uuid4().hex, next_attempt_at=due,
            )

        first_group, second_group = str(uuid4()), str(uuid4())
        head = event(first_group, due=now + timedelta(minutes=2))
        following = event(first_group)
        other = event(second_group)
        order = event("20261121000000000030", target_type="order")
        self.assertEqual(peer_worker.claim_due_events(now=now), [other.pk, order.pk])
        self.assertEqual(peer_worker.deliver_one(following.pk, now=now), "noop")
        head.status = PeerSyncEvent.Status.DONE
        head.save(update_fields=["status"])
        self.assertEqual(peer_worker.claim_due_events(now=now), [following.pk])

    def test_orders_only_does_not_claim_or_modify_either_chat_protocol(self):
        now = timezone.now()
        queued = [
            PeerSyncEvent.objects.create(
                direction="out", target_type=target, binding_role="downstream", binding_id="0",
                kind=kind, target_ref=uuid4().hex, idempotency_key=uuid4().hex,
            )
            for target, kind in (("chat", "chat.order_message"), ("chat_core", "chat.order_message"),
                                 ("order", "order.assign"))
        ]
        before = [PeerSyncEvent.objects.values().get(pk=row.pk) for row in queued[:2]]
        result = peer_worker.run_delivery_batch(
            now=now, window=peer_worker.DeliveryWindow(), orders_only=True,
        )
        self.assertEqual(result["claimed"], 1)
        self.assertEqual([PeerSyncEvent.objects.values().get(pk=row.pk) for row in queued[:2]], before)
        queued[-1].refresh_from_db()
        self.assertEqual(queued[-1].status, PeerSyncEvent.Status.FAILED)

    def test_pending_chat_and_still_connected_groups_block_the_orders_only_cutover(self):
        self.assertEqual(chat_cutover_blocker(), "")
        group = OrderGroup.objects.create(
            conversation=Conversation.objects.create(), wire_id=uuid4(),
            order_no="SYN-PEER-CHAT", binding_role="downstream", binding_id=1,
        )
        self.assertEqual(chat_cutover_blocker(), "有仍相连的旧跨工作室订单群")
        OrderGroup.objects.filter(pk=group.pk).update(binding_id=None)

        for target in ("chat", "chat_core"):
            event = PeerSyncEvent.objects.create(
                direction="out", target_type=target, binding_role="downstream", binding_id="0",
                kind="chat.order_message", target_ref=uuid4().hex, idempotency_key=uuid4().hex,
            )
            self.assertEqual(chat_cutover_blocker(), "有跨工作室聊天待投递")
            PeerSyncEvent.objects.filter(pk=event.pk).update(status=PeerSyncEvent.Status.DONE)
        self.assertEqual(chat_cutover_blocker(), "")

    def test_claim_and_durable_peer_delivery_stay_out_of_legacy_chat(self):
        source_domain = self.domain.domain
        target_domain = "core-peer-target.test.local"
        target, _ = provision_tenant(
            schema_name="core_peer_target", name="独立群验收下家", domain=target_domain,
        )

        def remove_target():
            with tenant_context(target):
                target.delete(force_drop=True)

        self.addCleanup(remove_target)
        folder = Path(self.enterContext(TemporaryDirectory()))
        config = folder / "chat.json"
        config.write_text(json.dumps({"service_url": "http://127.0.0.1:9850", "tenants": {
            self.tenant.schema_name: {"key_id": "peer-a", "secret": "peer-a-synthetic-secret"},
            target.schema_name: {"key_id": "peer-b", "secret": "peer-b-synthetic-secret"},
        }}))
        config.chmod(0o600)
        self.enterContext(self.settings(ALLOWED_HOSTS=[source_domain, target_domain],
                                        TT_CHAT_ORDER_GROUPS_ENABLED=True, DEPOSIT_ENFORCED=False,
                                        TT_CHAT_CORE_BACKEND="service",
                                        TT_CHAT_SERVICE_CONFIG_FILE=str(config)))
        publisher = make_logged_in_client(domain=source_domain, username="core-peer-publisher", is_superuser=True)
        make_logged_in_client(domain=source_domain, username="core-peer-account", groups=("代练员",))
        make_logged_in_client(domain=source_domain, username="core-peer-unrelated")
        User = get_user_model()
        account = User.objects.get(username="core-peer-account")
        unrelated = User.objects.get(username="core-peer-unrelated")
        grant_nodes(account, "grab_hall.take_order", "my_orders.send_to_my_system")
        outbound, inbound = "core-peer-outbound", "core-peer-inbound"
        source_binding = PeerDownstreamBinding.objects.create(
            account=account, peer_base_url=f"https://{target_domain}",
            token_in_hash=hash_token(inbound), token_out=outbound,
        )
        replacement_binding = PeerDownstreamBinding.objects.create(
            account=unrelated, peer_base_url="https://core-peer-unrelated.test.invalid",
            token_in_hash=hash_token("independent-binding"), token_out="unrelated-outbound",
            hall_push_enabled=False,
        )
        source_number = "20261121000000000029"
        Order.objects.create(
            game=Game.objects.create(name="独立跨工作室聊天游戏"),
            order_no=source_number, status=OrderStatus.UNASSIGNED, total_price="100.00", hall_price="50.00",
        )
        with tenant_context(target):
            Game.objects.create(name="独立跨工作室聊天游戏")
            acceptor = make_logged_in_client(domain=target_domain, username="core-peer-acceptor", is_superuser=True)
            make_logged_in_client(domain=target_domain, username="core-peer-binder", is_superuser=True)
            PeerUpstreamBinding.objects.create(
                peer_name="独立群验收上家", peer_base_url=f"https://{source_domain}", peer_account=account.username,
                token_in_hash=hash_token(outbound), token_out=inbound, local_base_url=f"https://{target_domain}",
                bound_by=User.objects.get(username="core-peer-binder"), hall_sync_enabled=True,
            )

        routed, interrupted = [], set()

        @contextmanager
        def transport(method, url, *, json, headers, timeout):
            dest = urlsplit(url).netloc
            self.assertIn(dest, (source_domain, target_domain))
            for event in json["events"]:
                routed.append((dest, event))
                if event["idempotency_key"] in interrupted:
                    interrupted.remove(event["idempotency_key"])
                    yield httpx.Response(503, json={"code": 503})
                    return
            with tenant_context(self.tenant if dest == source_domain else target):
                response = Client(SERVER_NAME=dest).post(
                    urlsplit(url).path, json, content_type="application/json",
                    HTTP_AUTHORIZATION=headers["Authorization"],
                )
            yield httpx.Response(response.status_code, content=response.content)

        self.data(publisher.post(f"/api/business/orders/{source_number}/throw-to-hall/", {}))
        with patch("apps.business.peer_client.httpx.stream", side_effect=transport):
            throw = PeerSyncEvent.objects.get(kind="hall.throw")
            self.assertEqual(peer_worker.deliver_one(throw.pk, now=timezone.now()), "done")
            with tenant_context(target):
                mirror = PeerHallMirror.objects.get(peer_order_no=source_number)
                self.assertEqual(self.data(acceptor.post(
                    f"/api/business/hall/peer/{mirror.pk}/claim/", {},
                ))["status"], "promoted")
                mirror.refresh_from_db()
                number = mirror.promoted_order.order_no
                link = OrderChatPeerLink.objects.get(order_id=mirror.promoted_order_id)
                group_id = str(link.group_id)
                self.assertNotEqual(number, source_number)
                self.assertEqual(link.publisher_name, "core-peer-publisher")
                self.assertTrue(OrderChatIntent.objects.get(order_no=number).ensure_group)
                self.assertFalse(OrderGroup.objects.exists())
            self.assertEqual(str(OrderChatPeerLink.objects.get(order_no=source_number).group_id), group_id)
            self.assertEqual(chat_cutover_blocker(), "有仍相连的聊天微服务订单群")
            intents = self.data(self.callback_request(
                source_domain, "peer-a", {"operation": "pull"},
                "/api/internal-chat/service/order-intents/",
            ))
            self.assertIs(intents["peer_chat"], True)
            fact = intents["intents"][0]["order"]
            self.assertEqual(fact["peer_link"]["group_id"], group_id)
            self.assertEqual(fact["peer_link"]["side"], "downstream")
            self.assertIs(fact["peer_link"]["active"], True)
            self.assertNotIn(account.pk, fact["default_join_user_ids"])
            owner_id = User.objects.get(username="core-peer-publisher").pk
            authority = self.data(self.callback_request(
                source_domain, "peer-a", {"checks": [{"conversation_id": 1_000_000_123,
                    "order_no": source_number, "user_ids": [owner_id, account.pk]}]},
                "/api/internal-chat/service/order-authorizations/",
            ))["groups"][0]
            self.assertEqual(authority["read_user_ids"], [owner_id])
            self.assertEqual(authority["room"]["can_pin_user_ids"], [owner_id])

            image = BytesIO()
            Image.new("RGB", (980, 900), "blue").save(image, format="PNG", compress_level=0)
            message = {
                "message_id": str(uuid4()), "sender": "a", "sender_name": "core-peer-publisher",
                "sender_left_at": None, "text": "上家合成图", "created_at": timezone.now().isoformat(),
                "revoked": False, "reply_to": None,
                "image": {"mime": "image/png", "data": base64.b64encode(image.getvalue()).decode()},
            }
            command = {"operation": "push", "group_id": group_id, "kind": "chat.order_message",
                       "payload": message, "event_key": uuid4().hex}
            for _ in range(2):
                self.assertEqual(self.data(self.callback_request(source_domain, "peer-a", command)),
                                 {"queued": True})
            with tenant_context(target):
                self.assertEqual(self.callback_request(target_domain, "peer-a", command).status_code, 403)
                forged_sender = self.callback_request(target_domain, "peer-b", command)
                self.assertEqual(forged_sender.json()["code"], ErrorCode.VALIDATION)
            sent = PeerSyncEvent.objects.get(target_type="chat_core", direction="out")
            sent.attempts = len(peer_worker.RETRY_BACKOFF_SECONDS)
            sent.save(update_fields=["attempts"])
            interrupted.add(sent.idempotency_key)
            self.assertEqual(peer_worker.deliver_one(sent.pk, now=timezone.now()), "retry")
            sent.refresh_from_db()
            self.assertEqual(sent.status, PeerSyncEvent.Status.PENDING)
            source_binding.refresh_from_db()
            self.assertEqual(source_binding.consecutive_failures, 0)
            self.assertEqual(peer_worker.deliver_one(sent.pk, now=sent.next_attempt_at), "done")
            dest, delivered = routed[-1]
            self.assertEqual(dest, target_domain)
            self.assertEqual(delivered["payload"]["text"], "上家合成图")
            with tenant_context(target):
                valid = self.data(self.peer_request(target_domain, outbound, delivered))
                self.assertEqual((valid["applied"], valid["skipped"]), (1, 0))
                pull = {"operation": "pull", "limit": 5}
                incoming = self.data(self.callback_request(target_domain, "peer-b", pull))["events"]
                self.assertEqual(len(incoming), 1)
                self.assertEqual(incoming[0]["order_no"], number)
                self.assertEqual(base64.b64decode(incoming[0]["payload"]["image"]["data"]), image.getvalue())
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", pull))["events"], incoming)
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", {
                    "operation": "defer", "events": [incoming[0]["id"]],
                })), {"deferred": 1})
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", pull))["events"], [])
                PeerSyncEvent.objects.filter(pk=incoming[0]["id"]).update(next_attempt_at=timezone.now())
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", pull))["events"], incoming)
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", {
                    "operation": "ack", "events": [incoming[0]["id"]],
                })), {"acked": 1})
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", pull))["events"], [])
                self.assertFalse(Message.objects.exists())

            reverse = {**delivered, "idempotency_key": uuid4().hex,
                       "payload": {**message, "message_id": str(uuid4()), "sender": f"b:{link.acceptor_id}",
                                   "sender_name": "core-peer-acceptor", "text": "下家合成回信"}}
            rejected = self.data(self.peer_request(source_domain, "independent-binding", reverse))
            self.assertEqual((rejected["applied"], rejected["skipped"]), (0, 1))
            with tenant_context(target):
                self.assertEqual(self.data(self.callback_request(target_domain, "peer-b", {
                    **command, "payload": reverse["payload"], "event_key": reverse["idempotency_key"],
                })), {"queued": True})
                reply = PeerSyncEvent.objects.get(target_type="chat_core", direction="out")
                self.assertEqual(peer_worker.deliver_one(reply.pk, now=timezone.now()), "done")
            pull = {"operation": "pull", "limit": 5}
            reverse_rows = self.data(self.callback_request(source_domain, "peer-a", pull))["events"]
            self.assertEqual([row["payload"]["text"] for row in reverse_rows], ["下家合成回信"])
            source_order = Order.objects.get(order_no=source_number)
            ordinary = Order.objects.create(
                game=source_order.game, order_no="20261121000000000078", status=OrderStatus.IN_PROGRESS,
            )
            Performance.objects.create(order=ordinary, user=account, emp_type=EmpType.MAIN, temp_amount=0)
            unbound = Client(SERVER_NAME=source_domain, HTTP_AUTHORIZATION=f"Bearer {inbound}").post(
                "/api/business/peer-sync/unbind/", {}, content_type="application/json",
            )
            self.data(unbound)
            self.assertTrue(Performance.objects.filter(order=source_order, user=account).exists())
            checked = self.data(self.callback_request(
                source_domain, "peer-a", {"checks": [
                    {"conversation_id": 1_000_000_123, "order_no": source_number,
                     "user_ids": [owner_id, account.pk]},
                    {"conversation_id": 1_000_000_124, "order_no": ordinary.order_no,
                     "user_ids": [account.pk]},
                ]}, "/api/internal-chat/service/order-authorizations/",
            ))["groups"]
            self.assertEqual(checked[0]["read_user_ids"], [owner_id])
            self.assertNotIn(account.pk, checked[0]["default_join_user_ids"])
            self.assertEqual(checked[1]["read_user_ids"], [account.pk])

            source_binding.state = PeerDownstreamBinding.State.ACTIVE
            source_binding.revoked_at = None
            source_binding.token_in_hash = hash_token("rotated-inbound")
            source_binding.save(update_fields=["state", "revoked_at", "token_in_hash"])
            wrong_generation = self.data(self.peer_request(source_domain, "rotated-inbound", {
                **reverse, "idempotency_key": uuid4().hex, "payload": {**reverse["payload"], "text": "换绑后陌生消息"},
            }))
            self.assertEqual((wrong_generation["applied"], wrong_generation["skipped"]), (0, 1))
            self.assertEqual(self.data(self.callback_request(source_domain, "peer-a", pull))["events"], reverse_rows)
            Performance.objects.filter(order=source_order, user=account).update(user=unrelated)
            next_invite = peer_chat.authorize_claim(source_order, replacement_binding)
            self.assertNotEqual(next_invite["group_id"], group_id)
            generations = list(OrderChatPeerLink.objects.filter(order=source_order).order_by("generation"))
            self.assertEqual([row.generation for row in generations], [1, 2])
            self.assertIsNotNone(generations[0].detached_at)
            self.assertIsNone(generations[1].detached_at)
            stale = self.data(self.peer_request(source_domain, "independent-binding", reverse))
            self.assertEqual((stale["applied"], stale["skipped"]), (0, 1))
            self.assertEqual(self.data(self.callback_request(source_domain, "peer-a", pull))["events"], reverse_rows)
            self.assertFalse(Message.objects.exists())
            self.assertFalse(OrderGroup.objects.exists())
