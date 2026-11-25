import json
from contextlib import closing, contextmanager
from io import StringIO
from queue import Queue
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.accounts.management.commands import send_wxpusher_notices
from apps.accounts.models import (
    WxPusherBinding, WxPusherHallPublication, WxPusherHallSubscription, WxPusherNotice, WxPusherPreference,
)
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.accounts.wxpusher_wakeup import open_listener, wait_for_hall_wakeup
from apps.business.jidanpu_upstream import sync_hall
from apps.business.models import Game, JidanpuUpstreamBinding, Order, OrderStatus, PeerUpstreamBinding
from apps.business.peer_inbound import apply_events, apply_hall_pull_rows
from apps.business.peer_sync import hash_token
from apps.business.roles import TENANT_ADMIN, WORKER
from apps.core.wxpusher import WxPusherClient
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_jidanpu_upstream_pull import _StubClient
from tests.test_peer_hall_inbound import hall_event


SCHEMA = 'wxpusher_hall_wakeup'
DOMAIN = 'wxpusher-hall-wakeup.test.local'
APP_ID = '993421'


@override_settings(
    WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=SCHEMA,
    WXPUSHER_APP_ID=APP_ID,
    WXPUSHER_APP_TOKEN='AT_synthetic_hall_wakeup',
)
class WxPusherHallWakeupTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = get_tenant_model().objects.get_or_create(schema_name=SCHEMA)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        get_tenant_domain_model().objects.get_or_create(tenant=self.tenant, domain=DOMAIN)
        self.case = self._testMethodName.removeprefix('test_')
        with schema_context(SCHEMA):
            self.game = Game.objects.create(name='合成唤醒游戏' + self.case[:18])
            self.admin = make_logged_in_client(
                username='wake-admin-' + self.case, groups=(TENANT_ADMIN,), domain=DOMAIN,
            )

    def _subscribe_workers(self, count):
        user_model = get_user_model()
        role, _ = Group.objects.get_or_create(name=WORKER)
        users = user_model.objects.bulk_create([
            user_model(username=f'wake-{self.case}-{index}', password='!') for index in range(count)
        ])
        user_model.groups.through.objects.bulk_create([
            user_model.groups.through(user_id=user.pk, group_id=role.pk) for user in users
        ])
        uid_prefix = f'UID_WAKE_{self.game.pk}_'
        uids = [uid_prefix + str(index) for index in range(count)]
        WxPusherBinding.objects.bulk_create([
            WxPusherBinding(user=user, app_id=APP_ID, uid=uid) for user, uid in zip(users, uids, strict=True)
        ])
        now = timezone.now()
        WxPusherPreference.objects.bulk_create([
            WxPusherPreference(user=user, hall_enabled_since=now) for user in users
        ])
        WxPusherHallSubscription.objects.bulk_create([
            WxPusherHallSubscription(preference_id=user.pk, game=self.game, selected_at=now) for user in users
        ])
        return uids

    def _make_order(self, number):
        return Order.objects.create(
            order_no=str(number).zfill(20), game=self.game, status=OrderStatus.UNASSIGNED,
            hall_price='87.00', project_text='合成唤醒抢单任务',
        )

    def _publish(self, order):
        result = self.admin.post(f'/api/business/orders/{order.order_no}/throw-to-hall/')
        self.assertEqual((result.status_code, result.json()['code']), (200, 0), result.content)

    @contextmanager
    def _watch(self, *, listener_factory=None, limit=50, before_response=None):
        idle = Event()
        stop = Event()
        requests = Queue()
        errors = Queue()
        diagnostics = StringIO()

        def observe_wait(listener, timeout):
            idle.set()
            return wait_for_hall_wakeup(listener, timeout)

        def provider(request):
            called_at = monotonic()
            payload = json.loads(request.content)
            requests.put((called_at, payload))
            if before_response is not None:
                before_response(payload)
            rows = [
                {'uid': uid, 'code': 1000, 'messageContentId': index}
                for index, uid in enumerate(payload['uids'], start=1)
            ]
            return httpx.Response(200, json={'code': 1000, 'data': rows})

        def run():
            try:
                command = send_wxpusher_notices.Command(stdout=StringIO(), stderr=diagnostics)
                command._watch('public', limit, send_wxpusher_notices.QueuedSendPacer(), stop.is_set)
            except Exception as exc:
                errors.put(exc)
            finally:
                connections.close_all()

        with (
            patch.object(send_wxpusher_notices, 'open_listener', side_effect=listener_factory or open_listener),
            patch.object(send_wxpusher_notices, 'wait_for_hall_wakeup', side_effect=observe_wait),
            patch.object(send_wxpusher_notices, '_REGULAR_INTERVAL', 60),
            patch.object(send_wxpusher_notices, '_HALL_RECOVERY_INTERVAL', 60),
            patch.object(send_wxpusher_notices, '_LISTENER_RETRY_INTERVAL', 0.05),
            patch('apps.accounts.wxpusher_notifications.WxPusherClient', side_effect=lambda **kwargs:
                  WxPusherClient(transport=httpx.MockTransport(provider), **kwargs)),
        ):
            thread = Thread(target=run, daemon=True)
            thread.start()
            try:
                yield idle, requests, diagnostics
            finally:
                stop.set()
                thread.join(8)
                self.assertFalse(thread.is_alive(), '发送进程未停止')
                self.assertTrue(errors.empty(), (list(errors.queue), diagnostics.getvalue()))

    def _assert_live_latency(self, size):
        with schema_context(SCHEMA):
            expected_uids = self._subscribe_workers(size)
            order = self._make_order(6200100000 + size)
        with self._watch() as (idle, requests, diagnostics):
            self.assertTrue(idle.wait(5), diagnostics.getvalue())
            started_at = monotonic()
            self._publish(order)
            sent_at, payload = requests.get(timeout=max(0, started_at + 8 - monotonic()))
            self.assertLess(sent_at - started_at, 8)
            self.assertCountEqual(payload['uids'], expected_uids)
            self.assertEqual(parse_qs(urlsplit(payload['url']).query), {
                'entry': ['wxpusher'], 'shared': ['1'], 'source': ['local'], 'ref': [order.order_no],
            })
            self.assertTrue(requests.empty())
        with schema_context(SCHEMA):
            self.assertEqual(WxPusherNotice.objects.filter(
                kind='hall_new_order', order_no=order.order_no, status='sent',
            ).count(), size)
        print(f'大厅入厅请求到推送 HTTP：{size} 人、1 次、{sent_at - started_at:.3f}s')

    def test_live_300_workers(self):
        self._assert_live_latency(300)

    def test_live_1000_workers(self):
        self._assert_live_latency(1000)

    def test_mirror_sources_wake_once_and_rollback_does_not_signal(self):
        with schema_context(SCHEMA):
            self._subscribe_workers(1)
            user = get_user_model().objects.get(username='wake-admin-' + self.case)
            upstream = JidanpuUpstreamBinding.objects.create(
                user=user, jidanpu_base_url='https://wake-upstream.test.local', account='synthetic-upstream',
                password='synthetic-password', enabled=True,
            )
            peer = PeerUpstreamBinding.objects.create(
                peer_name='合成上家', peer_base_url='https://wake-peer.test.local', peer_account='synthetic-peer',
                token_in_hash=hash_token('synthetic-peer-wake'), token_out='synthetic-out',
                local_base_url=f'https://{DOMAIN}', bound_by=user, hall_sync_enabled=True,
            )
            upstream_rows = [{
                'orderId': 'synthetic-6203', 'orderIdView': 'synthetic-6203', 'gameName': self.game.name,
                'project': '合成鸡蛋铺任务', 'recFee': '70.00',
            }]
            peer_row = hall_event('00000000000006200004', game_name=self.game.name, price='80.00')
            second_peer_row = hall_event('00000000000006200005', game_name=self.game.name, price='90.00')
            order = self._make_order(6200106000)
            with closing(open_listener()) as listener:
                with transaction.atomic():
                    self._publish(order)
                    self.assertFalse(wait_for_hall_wakeup(listener, 0.05))
                    transaction.set_rollback(True)
                self.assertFalse(wait_for_hall_wakeup(listener, 0.05))
                self.assertFalse(WxPusherHallPublication.objects.filter(ref=order.order_no).exists())
                for index in range(2):
                    now = timezone.now()
                    sync_hall(binding=upstream, client=_StubClient(rows=upstream_rows), now=now)
                    if index == 0:
                        self.assertTrue(wait_for_hall_wakeup(listener, 2))
                    else:
                        self.assertFalse(wait_for_hall_wakeup(listener, 0.05))
                apply_hall_pull_rows(
                    peer, [{'target_ref': peer_row['target_ref'], 'payload': peer_row['payload']}],
                    now=timezone.now(), round_started_at=timezone.now(),
                )
                self.assertTrue(wait_for_hall_wakeup(listener, 2))
                apply_events([second_peer_row], binding=peer)
                self.assertTrue(wait_for_hall_wakeup(listener, 2))
                apply_events([second_peer_row], binding=peer)
                self.assertFalse(wait_for_hall_wakeup(listener, 0.05))

    def test_startup_drains_work_when_notify_fails(self):
        def reject_notify(execute, sql, params, many, context):
            if 'pg_notify' in sql:
                raise DatabaseError('synthetic wake failure')
            return execute(sql, params, many, context)

        with schema_context(SCHEMA):
            uids = self._subscribe_workers(1)
            order = self._make_order(6200107000)
            with connection.execute_wrapper(reject_notify), self.assertLogs(
                'apps.accounts.wxpusher_wakeup', level='WARNING',
            ):
                self._publish(order)
        with self._watch() as (_, requests, _):
            _, payload = requests.get(timeout=8)
            self.assertEqual(payload['uids'], uids)

    def test_reconnected_listener_drains_publication_committed_while_disconnected(self):
        reconnecting = Event()
        allow_reconnect = Event()
        first_backend = Queue()
        opened = False

        def listener_factory():
            nonlocal opened
            if not opened:
                opened = True
                listener = open_listener()
                first_backend.put(listener.info.backend_pid)
                return listener
            reconnecting.set()
            if not allow_reconnect.wait(5):
                raise AssertionError('未释放测试监听')
            return open_listener()

        with schema_context(SCHEMA):
            uids = self._subscribe_workers(1)
            order = self._make_order(6200108000)
        with self._watch(listener_factory=listener_factory) as (idle, requests, diagnostics):
            try:
                self.assertTrue(idle.wait(5), diagnostics.getvalue())
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_terminate_backend(%s)', [first_backend.get(timeout=2)])
                    self.assertTrue(cursor.fetchone()[0])
                self.assertTrue(reconnecting.wait(5), diagnostics.getvalue())
                self._publish(order)
                self.assertTrue(requests.empty())
            finally:
                allow_reconnect.set()
            _, payload = requests.get(timeout=8)
            self.assertEqual(payload['uids'], uids)
            self.assertIn('监听断开', diagnostics.getvalue())

    def test_single_signal_keeps_draining_across_limited_rounds(self):
        notified = 0

        def permit_one_notify(execute, sql, params, many, context):
            nonlocal notified
            if 'pg_notify' in sql:
                notified += 1
                if notified > 1:
                    return execute('SELECT 1', None, False, context)
            return execute(sql, params, many, context)

        with schema_context(SCHEMA):
            uids = self._subscribe_workers(1)
            orders = [self._make_order(6200109000 + index) for index in range(3)]
        with self._watch(limit=1) as (idle, requests, diagnostics):
            self.assertTrue(idle.wait(5), diagnostics.getvalue())
            with connection.execute_wrapper(permit_one_notify), transaction.atomic():
                for order in orders:
                    self._publish(order)
                self.assertTrue(requests.empty())
            refs = []
            for _order in orders:
                _, payload = requests.get(timeout=8)
                self.assertEqual(payload['uids'], uids)
                refs.extend(parse_qs(urlsplit(payload['url']).query)['ref'])
            self.assertEqual(refs, [order.order_no for order in orders])
        self.assertEqual(notified, len(orders))

    def test_hall_signal_preempts_reserved_business_backlog_after_current_request(self):
        release_business = Event()

        def hold_first_send(payload):
            if payload['summary'] == '收到新的订单指派' and not release_business.wait(8):
                raise AssertionError('未释放测试请求')

        with schema_context(SCHEMA):
            uids = self._subscribe_workers(1)
            worker_id = WxPusherBinding.objects.get(uid=uids[0]).user_id
            order = self._make_order(6200110000)
            for index in range(5):
                enqueue_worker_notice(user_id=worker_id, kind='assigned', reference=f'preempt-ordinary-{index}')
        with self._watch(before_response=hold_first_send) as (_, requests, _):
            try:
                business_at, business = requests.get(timeout=8)
                self.assertEqual(business['summary'], '收到新的订单指派')
                self._publish(order)
                self.assertTrue(requests.empty())
            finally:
                release_business.set()
            hall_at, hall = requests.get(timeout=8)
            self.assertEqual(hall['summary'], '接单大厅有新订单')
            self.assertEqual(hall['uids'], uids)
            self.assertGreaterEqual(hall_at - business_at, 0.98)
        with schema_context(SCHEMA):
            self.assertEqual(WxPusherNotice.objects.filter(kind='assigned', status='pending',
                                                          reference__startswith='preempt-ordinary-').count(), 4)

    def test_sustained_hall_backlog_leaves_a_send_slot_for_business_notices(self):
        with schema_context(SCHEMA):
            uids = self._subscribe_workers(1)
            worker_id = WxPusherBinding.objects.get(uid=uids[0]).user_id
            orders = [self._make_order(6200111000 + index) for index in range(3)]
            for order in orders:
                self._publish(order)
            enqueue_worker_notice(user_id=worker_id, kind='assigned', reference='hall-backlog-business')
        with self._watch(limit=1) as (_, requests, _):
            observed = [requests.get(timeout=8)[1] for _ in range(4)]
        self.assertEqual([payload['summary'] for payload in observed], [
            '接单大厅有新订单', '收到新的订单指派', '接单大厅有新订单', '接单大厅有新订单',
        ])
