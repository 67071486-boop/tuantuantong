import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.accounts.models import (
    StaffProfile, WxPusherBinding, WxPusherHallSubscription, WxPusherNotice, WxPusherPreference,
)
from apps.accounts.wxpusher_config import load_wxpusher_config
from apps.accounts.wxpusher_hall import process_hall_publications, queue_hall_publication
from apps.accounts.wxpusher_notifications import send_notification_batch
from apps.business.api.hall_claim_target import hall_notice_card
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherClient
from tests.p5_auth_helpers import grant_nodes


@override_settings(
    WXPUSHER_SUBSCRIBE_TENANT_SCHEMA='wxpusher_hall_race',
    WXPUSHER_APP_ID='993421',
    WXPUSHER_APP_TOKEN='AT_synthetic_hall_race',
)
class WxPusherHallBatchConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model().objects.create(schema_name='wxpusher_hall_race')

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def _prepare_pending_batch(self, case):
        self.order_no = case.zfill(20)
        self.uids = [f'UID_SYNTHETIC_RACE_{case}_1', f'UID_SYNTHETIC_RACE_{case}_2']
        with schema_context(self.tenant.schema_name):
            game = Game.objects.create(name=f'合成大厅竞态游戏{case}')
            group, _ = Group.objects.get_or_create(name=WORKER)
            for index, uid in enumerate(self.uids):
                user = get_user_model().objects.create_user(username=f'synthetic-hall-race-{case}-{index}')
                user.groups.add(group)
                if index == 0:
                    grant_nodes(user, 'grab_hall.take_order')
                StaffProfile.objects.create(user=user).games.add(game)
                WxPusherBinding.objects.create(user=user, app_id='993421', uid=uid)
                now = timezone.now()
                preference = WxPusherPreference.objects.create(user=user, hall_enabled_since=now)
                WxPusherHallSubscription.objects.create(preference=preference, game=game, selected_at=now)
            order = Order.objects.create(
                order_no=self.order_no, game=game, status=OrderStatus.WAITING_GRAB, hall_price='87.00',
            )
            Performance.objects.create(order=order, emp_type=EmpType.MAIN)
            queue_hall_publication('local', order)
            process_hall_publications(load_wxpusher_config(), limit=1, should_stop=None)
            self.assertEqual(WxPusherNotice.objects.filter(status='pending', order_no=self.order_no).count(), 2)

        self.requests = []

        def provider(request):
            payload = json.loads(request.content)
            self.requests.append(payload['uids'])
            return httpx.Response(200, json={'code': 1000, 'data': [
                {'uid': uid, 'code': 1000, 'messageContentId': index} for index, uid in
                enumerate(payload['uids'], start=1)
            ]})

        self.enterContext(patch(
            'apps.accounts.wxpusher_notifications.WxPusherClient',
            side_effect=lambda **kwargs: WxPusherClient(transport=httpx.MockTransport(provider), **kwargs),
        ))

    def test_two_drains_do_not_split_the_same_order_batch(self):
        self._prepare_pending_batch('855')
        both_found_pending = Barrier(2)

        def drain():
            selected_first_reference = False

            def synchronize_selection(execute, sql, params, many, context):
                nonlocal selected_first_reference
                result = execute(sql, params, many, context)
                if (not selected_first_reference and sql.startswith('SELECT "accounts_wxpushernotice"."reference"')
                        and 'LIMIT 1' in sql):
                    selected_first_reference = True
                    both_found_pending.wait(timeout=10)
                return result

            try:
                with schema_context(self.tenant.schema_name), connection.execute_wrapper(synchronize_selection):
                    return send_notification_batch('hall-race.example.invalid', limit=1)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            result_a = executor.submit(drain)
            result_b = executor.submit(drain)
            sent = result_a.result(timeout=15)['sent'] + result_b.result(timeout=15)['sent']

        self.assertEqual(sent, 2)
        self.assertEqual(len(self.requests), 1)
        self.assertCountEqual(self.requests[0], self.uids)

    def test_order_withdrawn_during_recipient_checks_does_not_send_the_batch(self):
        self._prepare_pending_batch('856')
        checked = 0

        def check(source, ref, game_id):
            nonlocal checked
            visible = hall_notice_card(source, ref, game_id)
            self.assertIsNotNone(visible)
            checked += 1
            if checked == 1:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    self.assertEqual(executor.submit(withdraw).result(timeout=10), 1)
            return visible

        def withdraw():
            try:
                with schema_context(self.tenant.schema_name):
                    return Order.objects.filter(order_no=self.order_no).update(status=OrderStatus.UNASSIGNED)
            finally:
                connections.close_all()

        with schema_context(self.tenant.schema_name), patch(
            'apps.business.api.hall_claim_target.hall_notice_card', side_effect=check,
        ):
            result = send_notification_batch('hall-race.example.invalid', limit=1)
            self.assertEqual(result['skipped'], 2)
            notices = WxPusherNotice.objects.filter(order_no=self.order_no, status='skipped')
            self.assertCountEqual(notices.values_list('skip_reason', flat=True), ['hall_unavailable'] * 2)
            self.assertFalse(notices.exclude(attempted_at=None).exists())
        self.assertEqual(checked, 1)
        self.assertEqual(self.requests, [])
