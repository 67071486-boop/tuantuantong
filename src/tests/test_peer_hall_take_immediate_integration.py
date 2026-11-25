from io import StringIO
from math import ceil
from queue import Queue
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.business import peer_dispatch, peer_wakeup, peer_worker
from apps.business.management.commands import peer_sync
from apps.business.models import (
    Game, Order, OrderStatus, PeerDownstreamBinding, PeerHallMirror, PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_sync import hash_token, new_token
from tests._peer_stream_fakes import stream_response


class PeerHallTakeImmediateIntegrationTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sender = get_tenant_model().objects.create(schema_name="peer_wake_sender")
        cls.receiver = get_tenant_model().objects.create(schema_name="peer_wake_receiver")
        for tenant in (cls.sender, cls.receiver):
            get_tenant_domain_model().objects.create(
                tenant=tenant, domain=tenant.schema_name.replace("_", "-") + ".test.local",
            )

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.receiver.delete(force_drop=True)
        cls.sender.delete(force_drop=True)
        super().tearDownClass()

    def test_committed_takes_wake_the_real_loop_and_reach_the_receiver(self):
        token = new_token()
        sample_size = 30
        with schema_context(self.sender.schema_name):
            sender_account = get_user_model().objects.create_user(username="wake-sender")
            down = PeerDownstreamBinding.objects.create(
                account=sender_account, token_out=token, token_in_hash=hash_token(new_token()),
                peer_base_url="https://peer-wake-receiver.test.local",
            )
            game = Game.objects.create(name="合成即时摘牌游戏")
            orders = [
                Order.objects.create(
                    order_no=f"9999073400000000{i:04d}", game=game,
                    status=OrderStatus.IN_PROGRESS,
                )
                for i in range(sample_size)
            ]
        with schema_context(self.receiver.schema_name):
            receiver_account = get_user_model().objects.create_user(username="wake-receiver")
            up = PeerUpstreamBinding.objects.create(
                peer_name="合成上家", peer_base_url="https://peer-wake-sender.test.local",
                peer_account="synthetic", token_in_hash=hash_token(token),
                token_out=new_token(), local_base_url="https://peer-wake-receiver.test.local",
                bound_by=receiver_account, hall_sync_enabled=True,
            )
            PeerHallMirror.objects.bulk_create([
                PeerHallMirror(
                    peer_upstream_binding=up, peer_base_url=up.peer_base_url,
                    peer_order_no=order.order_no, first_seen_at=timezone.now(),
                )
                for order in orders
            ])
            other_pending = PeerSyncEvent.objects.create(
                binding_role="upstream", binding_id=str(up.pk), direction="out",
                kind="hall.claim", target_type="order", target_ref="SYNTHETIC-OTHER-TENANT",
                idempotency_key="synthetic-other-tenant-event",
            )

        denied = Client(SERVER_NAME="peer-wake-sender.test.local").post(
            "/api/business/peer-sync/inbound/events/", {"events": []},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(denied.status_code, 401)
        connection.set_schema_to_public()

        ready = Event()
        stop = Event()
        pull_started = Event()
        release_pull = Event()
        received = Queue()
        errors = Queue()
        diagnostics = StringIO()
        real_open = peer_wakeup.open_listener
        client = Client(SERVER_NAME="peer-wake-receiver.test.local")

        def observe_listener(*args, **kwargs):
            listener = real_open(*args, **kwargs)
            ready.set()
            return listener

        def transport(method, target, **kwargs):
            started = monotonic()
            with schema_context(self.sender.schema_name):
                response = client.post(
                    "/api/business/peer-sync/inbound/events/", kwargs["json"],
                    content_type="application/json",
                    HTTP_AUTHORIZATION=kwargs["headers"]["Authorization"],
                )
            body = response.json()
            received.put((kwargs["json"]["events"][0], started, monotonic(), body["code"]))
            return stream_response(body, status_code=response.status_code)

        def slow_pull():
            pull_started.set()
            if not release_pull.wait(15):
                errors.put(AssertionError("大厅拉取未及时释放"))
            return {"pages": 0, "rows": 0, "gone": 0, "complete": False}

        def run():
            try:
                peer_sync.Command(stdout=StringIO(), stderr=diagnostics)._loop(self.sender.schema_name)
            except Exception as exc:
                errors.put(exc)
            finally:
                connections.close_all()

        with (
            patch.object(peer_sync, "_STOP", stop),
            patch.object(peer_wakeup, "open_listener", side_effect=observe_listener),
            patch.object(peer_worker, "run_hall_pull", side_effect=slow_pull),
            patch("apps.business.peer_client.httpx.stream", side_effect=transport),
        ):
            thread = Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(4), ("监听连接未准备好", list(errors.queue), diagnostics.getvalue()))
                self.assertTrue(pull_started.wait(4), "独立维护循环未启动")
                with schema_context(self.sender.schema_name):
                    with transaction.atomic():
                        peer_dispatch.enqueue_hall_event(orders[0], kind="hall.take", bindings=[down])
                        self.assertTrue(received.empty())
                        transaction.set_rollback(True)
                    self.assertFalse(PeerSyncEvent.objects.exists())

                    send_latencies = []
                    leave_latencies = []
                    for order in orders:
                        with transaction.atomic():
                            peer_dispatch.enqueue_hall_event(order, kind="hall.take", bindings=[down])
                            queued_at = monotonic()
                        wire, sent_at, received_at, code = received.get(timeout=4)
                        self.assertEqual((wire["kind"], wire["target_ref"], code), (
                            "hall.take", order.order_no, 0,
                        ))
                        send_latencies.append(sent_at - queued_at)
                        leave_latencies.append(received_at - queued_at)
                with schema_context(self.receiver.schema_name):
                    self.assertEqual(
                        PeerHallMirror.objects.filter(status="gone", gone_at__isnull=False).count(),
                        sample_size,
                    )
                    other_pending.refresh_from_db()
                    self.assertEqual(other_pending.status, PeerSyncEvent.Status.PENDING)
                    self.assertIsNone(other_pending.next_attempt_at)
                p95 = ceil(sample_size * 0.95) - 1
                send_p95, leave_p95 = sorted(send_latencies)[p95], sorted(leave_latencies)[p95]
                print(
                    f"摘牌 30 笔：发出 P95 {send_p95:.3f}s / 最慢 {max(send_latencies):.3f}s，"
                    f"离厅 P95 {leave_p95:.3f}s / 最慢 {max(leave_latencies):.3f}s",
                )
                self.assertLess(send_p95, 1)
                self.assertLess(leave_p95, 2)
            finally:
                stop.set()
                release_pull.set()
                thread.join(8)
        self.assertFalse(thread.is_alive(), "投递循环未退出")
        self.assertTrue(errors.empty(), list(errors.queue))
        self.assertEqual(diagnostics.getvalue(), "")
