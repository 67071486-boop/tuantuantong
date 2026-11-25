from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event, current_thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.business import hall_shadow, peer_inbound
from apps.business.models import (
    EmpType, Game, Order, OrderSource, OrderStatus, PeerHallMirror, PeerOrderLink,
    PeerUpstreamBinding, Performance,
)
from apps.business.peer_sync import hash_token, new_token, source_name_for
from tests.p5_auth_helpers import make_logged_in_client


class PeerFirstLinkRaceTests(TransactionTestCase):
    domain = 'peer-first-link.test.local'
    peer_ref = '99991234000000000101'

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        self.tenant = get_tenant_model()(schema_name='peer_first_link_race')
        self.tenant.save(verbosity=0)
        self.addCleanup(self._remove_test_tenant)
        get_tenant_domain_model().objects.create(tenant=self.tenant, domain=self.domain)

    def _remove_test_tenant(self):
        connection.set_schema_to_public()
        self.tenant.delete(force_drop=True)

    def test_confirmation_first_without_shadow(self):
        self._race(first='claim', with_shadow=False)

    def test_update_first_without_shadow(self):
        self._race(first='update', with_shadow=False)

    def test_confirmation_first_with_shadow(self):
        self._race(first='claim', with_shadow=True)

    def test_update_first_with_shadow(self):
        self._race(first='update', with_shadow=True)

    def test_rule_changes_do_not_block_confirmation_first(self):
        self._race(first='claim', with_shadow=True, snapshot_percent=63)

    def test_rule_changes_do_not_block_update_first(self):
        self._race(first='update', with_shadow=False, snapshot_percent=63)

    def _race(self, *, first, with_shadow, snapshot_percent=None):
        token = new_token()
        moment = timezone.now()
        with schema_context(self.tenant.schema_name):
            game = Game.objects.create(name='合成并发抢单游戏')
            users = get_user_model()
            operator = users.objects.create_user(username='syn-peer-first-link-cs')
            binding = PeerUpstreamBinding.objects.create(
                peer_name='合成上家', peer_base_url='https://first-link-up.test.local',
                peer_account='synthetic', token_in_hash=hash_token(token), token_out=new_token(),
                local_base_url=f'https://{self.domain}', bound_by=operator, hall_sync_enabled=True,
                game_rules=[] if snapshot_percent is not None else None,
            )
            OrderSource.objects.create(name=source_name_for(binding.peer_name))
            claim_client = make_logged_in_client(
                self.domain, username='syn-peer-first-link-taker',
                groups=('合成打手',), nodes=('grab_hall.take_order',), games=[game],
            )
            taker = users.objects.get(username='syn-peer-first-link-taker')
            payload = {
                'order_no': self.peer_ref, 'source_order_no': self.peer_ref,
                'game_name': game.name, 'hall_price': '50.00', 'status': OrderStatus.WAITING_GRAB,
            }
            mirror = PeerHallMirror.objects.create(
                peer_upstream_binding=binding, peer_base_url=binding.peer_base_url,
                peer_order_no=self.peer_ref, game=game, remote_hall_price=Decimal('50.00'),
                first_seen_at=moment, payload=payload, hall_price_percent_at_sync=snapshot_percent,
            )
            shadow = hall_shadow.ensure_for_card(mirror) if with_shadow else None

        assign_time = moment + timedelta(seconds=1)
        update = self._event('order.update', moment, {
            **payload, 'status': OrderStatus.IN_PROGRESS, 'remark': '较早更新',
        })
        assign = self._event('order.assign', assign_time, {**payload, 'remark': '较新确认'})
        claim_ok = self._event('hall.claim_ok', assign_time, {})
        verdict = {'claim_replies': [{
            'target_ref': self.peer_ref, 'outcome': 'accepted', 'reply_events': [assign, claim_ok],
        }]}

        claim_sent = Event()
        race = Barrier(2)
        first_finished = Event()
        entry_point = '_apply_peer_order_fields' if with_shadow else 'resolve_game'
        proceed = getattr(peer_inbound, entry_point)

        def upstream_reply(url, sent_token, events, *, timeout):
            self.assertEqual((url, sent_token), (binding.peer_base_url, binding.token_out))
            self.assertEqual([(e['kind'], e['target_ref']) for e in events], [('hall.claim', self.peer_ref)])
            claim_sent.set()
            return verdict

        def wait_after_missing_link(*args, **kwargs):
            # Start both writes with no link, then let the selected request commit first.
            if not first_finished.is_set():
                race.wait(10)
                if not current_thread().name.startswith(first):
                    self.assertTrue(first_finished.wait(10))
            return proceed(*args, **kwargs)

        def request(name, post):
            try:
                return post()
            finally:
                connections.close_all()
                if name == first:
                    first_finished.set()

        def claim():
            return claim_client.post(
                f'/api/business/hall/peer/{mirror.pk}/claim/', {}, content_type='application/json',
            )

        def callback():
            self.assertTrue(claim_sent.wait(10))
            return Client(SERVER_NAME=self.domain, raise_request_exception=False).post(
                '/api/business/peer-sync/inbound/events/', {'events': [update]},
                content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token}',
            )

        with (
            patch('apps.business.peer_client.push_events', side_effect=upstream_reply),
            patch.object(peer_inbound, entry_point, side_effect=wait_after_missing_link),
            ThreadPoolExecutor(max_workers=1, thread_name_prefix='claim') as claimant,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix='update') as worker,
        ):
            claim_pending = claimant.submit(request, 'claim', claim)
            update_pending = worker.submit(request, 'update', callback)
            claim_response = claim_pending.result(timeout=25)
            update_response = update_pending.result(timeout=25)

        self.assertEqual(update_response.status_code, 200)
        update_body = update_response.json()
        self.assertEqual(update_body['code'], 0)
        self.assertEqual(update_body['data']['applied'], int(first == 'update'))
        expected_skips = [] if first == 'update' else ['stale']
        self.assertEqual([detail['code'] for detail in update_body['data']['skipped_details']], expected_skips)
        self.assertEqual(claim_response.status_code, 200)
        self.assertEqual(claim_response.json()['code'], 0, claim_response.json())
        self.assertEqual(claim_response.json()['data']['status'], PeerHallMirror.Status.PROMOTED)
        with schema_context(self.tenant.schema_name):
            link = PeerOrderLink.objects.get(upstream_binding=binding, peer_order_no=self.peer_ref)
            order = Order.all_objects.get(source_order_no=self.peer_ref)
            self.assertEqual(link.order_id, order.pk)
            self.assertEqual(link.hall_price_percent_at_sync, snapshot_percent)
            if snapshot_percent is not None:
                self.assertEqual(order.hall_price, Decimal('31.50'))
            self.assertEqual(link.last_applied_at, assign_time)
            self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
            self.assertEqual(order.remark, '较新确认')
            self.assertIsNone(order.hall_card_id)
            if shadow:
                self.assertEqual(order.pk, shadow.pk)
            mirror.refresh_from_db()
            self.assertEqual(mirror.promoted_order_id, order.pk)
            self.assertEqual(mirror.status, PeerHallMirror.Status.PROMOTED)
            main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
            self.assertEqual(main.user_id, taker.pk)
            self.assertIsNotNone(main.claimed_at)

    def _event(self, kind, moment, payload):
        return {
            'kind': kind, 'target_ref': self.peer_ref, 'idempotency_key': f'syn-first-{kind}',
            'occurred_at': moment.isoformat(), 'payload': payload,
        }
