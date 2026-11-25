import io
import json
from datetime import timedelta
from time import perf_counter
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import AccountSource
from apps.accounts.models import (
    AppToken, StaffProfile, TakerWallet, WxPusherBinding, WxPusherHallPublication,
    WxPusherHallSubscription, WxPusherNotice, WxPusherPreference,
)
from apps.accounts.wxpusher_config import load_wxpusher_config
from apps.accounts.wxpusher_hall import process_hall_publications
from apps.accounts.wxpusher_notice_templates import HALL_NEW_ORDER
from apps.accounts.wxpusher_notifications import enqueue_worker_notice, send_notification_batch
from apps.business.jidanpu_upstream import sync_hall
from apps.business.models import Game, JidanpuUpstreamBinding, Order, OrderStatus, PeerUpstreamBinding
from apps.business.order_flags import RESTRICT_VIEW_OFFER_PRICE
from apps.business.peer_inbound import apply_events, apply_hall_pull_rows
from apps.business.peer_sync import hash_token
from apps.business.roles import TENANT_ADMIN, WORKER
from apps.core.wxpusher import WxPusherClient
from apps.internal_chat.models import Conversation, GroupMember, Message, OrderGroup
from apps.internal_chat.wxpusher import queue_group_notice
from apps.openapi import tokens
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client
from tests.test_jidanpu_upstream_pull import _StubClient
from tests.test_peer_hall_inbound import hall_event

PATH = '/api/auth/wxpusher/hall-subscription'
ADMIN_CONFIG = '/api/business/wxpusher-config/'
UID = 'UID_SYNTHETIC_HALL'


@override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA='')
class HallSubscriptionTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name='合成大厅测试游戏')
        self.other_game = Game.objects.create(name='另一款合成游戏')
        self.worker = self._worker('hall-push-worker', games=[self.game])
        self.recipient = get_user_model().objects.get(username='hall-push-worker')

    def _worker(self, name, *, games):
        return make_logged_in_client(
            username=name, groups=(WORKER,), nodes=('my_profile', 'grab_hall.take_order'), games=games,
        )

    def _data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['code'], 0, response.content)
        return response.json()['data']

    def _patch(self, payload, client=None):
        return (client or self.worker).patch(PATH, payload, content_type='application/json')

    def _configure_push(self, response_data=None):
        self.admin = make_logged_in_client(username='hall-push-admin', groups=(TENANT_ADMIN,))
        self._data(self.admin.patch(ADMIN_CONFIG, {
            'app_id': '993421', 'app_token': 'AT_synthetic_hall', 'push_enabled': True,
            'order_enabled': False,
        }, content_type='application/json'))
        WxPusherBinding.objects.create(user=self.recipient, app_id='993421', uid=UID)
        self.received = []

        def provider(request):
            data = json.loads(request.content)
            self.received.append(data)
            rows = (
                response_data(data) if response_data is not None else [
                    {'uid': uid, 'code': 1000, 'messageContentId': index}
                    for index, uid in enumerate(data['uids'], start=1)
                ]
            )
            return httpx.Response(200, json={'code': 1000, 'data': rows})

        self.enterContext(patch('apps.accounts.wxpusher_notifications.WxPusherClient', side_effect=lambda **kwargs:
                                WxPusherClient(transport=httpx.MockTransport(provider), **kwargs)))

    def _publish(self, order):
        self._data(self.admin.post(f'/api/business/orders/{order.order_no}/throw-to-hall/'))

    def _make_order(self, suffix, text, price='87.00'):
        return Order.objects.create(
            order_no=f'{suffix:020}', source_order_no=f'synthetic-{suffix}', game=self.game,
            project_text=text, status=OrderStatus.UNASSIGNED, hall_price=price,
        )

    def _subscribe_unfiltered(self, *, enabled=True):
        self._data(self._patch({'enabled': enabled, 'games': [{'game_id': self.game.pk}]}))

    def _drain(self):
        call_command('send_wxpusher_notices', stdout=io.StringIO())

    def _subscribe_synthetic_workers(self, count):
        user_model = get_user_model()
        users = user_model.objects.bulk_create([
            user_model(username=f'batch-worker-{index}', password='!') for index in range(count)
        ])
        worker_role = self.recipient.groups.get(name=WORKER)
        user_model.groups.through.objects.bulk_create([
            user_model.groups.through(user_id=user.pk, group_id=worker_role.pk) for user in users
        ])
        profiles = StaffProfile.objects.bulk_create([StaffProfile(user=user) for user in users])
        StaffProfile.games.through.objects.bulk_create([
            StaffProfile.games.through(staffprofile_id=profile.pk, game_id=self.game.pk) for profile in profiles
        ])
        uids = [f'UID_SYNTHETIC_HALL_BATCH_{index}' for index in range(count)]
        WxPusherBinding.objects.bulk_create([
            WxPusherBinding(user=user, app_id='993421', uid=uid) for user, uid in zip(users, uids, strict=True)
        ])
        now = timezone.now()
        WxPusherPreference.objects.bulk_create([
            WxPusherPreference(user=user, hall_enabled_since=now) for user in users
        ])
        WxPusherHallSubscription.objects.bulk_create([
            WxPusherHallSubscription(preference_id=user.pk, game=self.game, selected_at=now) for user in users
        ])
        return users, uids

    def _assert_hall_batch_at_scale(self, size):
        self._configure_push()
        users, uids = self._subscribe_synthetic_workers(size + 1)
        WxPusherHallSubscription.objects.filter(preference_id=users[-1].pk).update(price_min='88.00')
        order = self._make_order(856, '合成批量抢单')
        self._publish(order)

        started = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            self._drain()
        elapsed = perf_counter() - started
        self.assertFalse(any(
            'JOIN "auth_user_groups"' in query['sql'] and 'JOIN "rbac_role"' in query['sql']
            for query in queries.captured_queries
        ), '大厅批量通知不能把收件人所属职位与权限目录直接联表')
        self.assertEqual([len(item['uids']) for item in self.received], [size])
        item, = self.received
        self.assertCountEqual(item['uids'], uids[:-1])
        self.assertIn('synthetic-856', item['content'])
        self.assertIn(item['url'], item['content'].replace('&amp;', '&'))
        entry = urlsplit(item['url'])
        self.assertEqual(entry.path, '/m/wxpusher/hall-entry')
        self.assertEqual(parse_qs(entry.query), {
            'entry': ['wxpusher'], 'shared': ['1'], 'source': ['local'], 'ref': [order.order_no],
        })
        self.assertEqual(entry.fragment, '')
        sent = WxPusherNotice.objects.filter(kind=HALL_NEW_ORDER, status='sent', order_no=order.order_no)
        self.assertCountEqual(sent.values_list('user_id', flat=True), [user.pk for user in users[:-1]])
        self._drain()
        self.assertEqual(len(self.received), 1)
        print(f'WxPusher mock: {size} recipients, 1 HTTP request, {elapsed:.2f}s (publication and delivery)')

    def test_300_selected_workers_share_one_hall_order_request(self):
        self._assert_hall_batch_at_scale(300)

    def test_1000_selected_workers_share_one_hall_order_request(self):
        self._assert_hall_batch_at_scale(1000)

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_manual_round_shares_send_interval_and_orders_business_hall_then_group_chat(self):
        clock = [0.0]
        requests = []

        def record_request(data):
            requests.append((clock[0], data['summary']))
            return [{'uid': uid, 'code': 1000, 'messageContentId': 1} for uid in data['uids']]

        self._configure_push(record_request)
        self._data(self.admin.patch(ADMIN_CONFIG, {'order_enabled': True}, content_type='application/json'))
        order = self._make_order(821, '合成群聊订单')
        conversation = Conversation.objects.create()
        group = OrderGroup.objects.create(order=order, order_no=order.order_no, conversation=conversation, wire_id=uuid4())
        GroupMember.objects.create(group=group, wire_key='a:2', local_user=self.recipient, manual_joined=True)
        sender = GroupMember.objects.create(group=group, wire_key='b:2', name='合成外部客服')
        message = Message.objects.create(
            conversation=conversation, group_sender=sender, seq=1, client_id='synthetic-pace', wire_id=uuid4(),
        )
        queue_group_notice(group, message)
        WxPusherNotice.objects.filter(kind='group_message').update(created_at=timezone.now() - timedelta(seconds=46))
        self._subscribe_unfiltered()
        self._publish(self._make_order(822, '合成大厅订单'))
        enqueue_worker_notice(user_id=self.recipient.pk, kind='assigned', reference='pace-business', order_no=order.order_no)

        def advance(seconds):
            self.assertFalse(any(block.durable for block in connection.atomic_blocks))
            clock[0] += seconds

        notifications = 'apps.accounts.wxpusher_notifications'
        with (
            patch(f'{notifications}.monotonic', side_effect=lambda: clock[0]),
            patch(f'{notifications}.sleep', side_effect=advance),
        ):
            self._drain()
        self.assertEqual(requests, [
            (0.0, '收到新的订单指派'), (1.0, '接单大厅有新订单'), (2.0, '订单群有新消息'),
        ])

    def test_hall_batch_does_not_resend_or_mark_missing_and_failed_recipients_as_sent(self):
        self._configure_push(lambda payload: [
            {'uid': uids[1], 'code': 1001, 'messageContentId': 123},
            {'uid': uids[0], 'code': 1000, 'messageContentId': 321},
        ])
        users, uids = self._subscribe_synthetic_workers(3)
        order = self._make_order(857, '合成回执测试')
        self._publish(order)
        self._drain()
        item, = self.received
        self.assertCountEqual(item['uids'], uids)
        self.assertEqual(dict(WxPusherNotice.objects.filter(
            kind=HALL_NEW_ORDER, order_no=order.order_no,
        ).values_list('user_id', 'status')), {
            users[0].pk: 'sent', users[1].pk: 'unconfirmed', users[2].pk: 'unconfirmed',
        })
        self._drain()
        self.assertEqual(len(self.received), 1)

    def test_hall_batch_rechecks_queued_binding_preferences_and_order_before_sending(self):
        self._configure_push()
        users, uids = self._subscribe_synthetic_workers(4)
        order = self._make_order(858, '合成待发新单')
        withdrawn = self._make_order(859, '合成发送前撤回订单')
        self._publish(order)
        self._publish(withdrawn)
        process_hall_publications(load_wxpusher_config(), limit=2, should_stop=None)
        self.assertEqual(WxPusherNotice.objects.filter(kind=HALL_NEW_ORDER, status='pending').count(), 8)

        WxPusherBinding.objects.filter(user=users[1]).update(last_start_at=timezone.now())
        WxPusherPreference.objects.filter(user=users[2]).update(hall_enabled_since=None)
        WxPusherHallSubscription.objects.filter(preference_id=users[3].pk).update(price_min='88.00')
        self._data(self.admin.post(f'/api/business/orders/{withdrawn.order_no}/withdraw/'))

        self._drain()
        item, = self.received
        self.assertEqual(item['uids'], uids[:1])
        self.assertEqual(dict(WxPusherNotice.objects.filter(
            kind=HALL_NEW_ORDER, order_no=order.order_no,
        ).values_list('user_id', 'skip_reason')), {
            users[0].pk: '',
            users[1].pk: 'binding_changed',
            users[2].pk: 'personal_disabled',
            users[3].pk: 'hall_rule_changed',
        })
        withdrawn_rows = WxPusherNotice.objects.filter(kind=HALL_NEW_ORDER, order_no=withdrawn.order_no)
        self.assertEqual(withdrawn_rows.filter(status='skipped').count(), 4)
        self.assertEqual(withdrawn_rows.get(user=users[0]).skip_reason, 'hall_unavailable')

    def test_default_and_per_game_rules_saved_for_own_account_only(self):
        self.assertEqual(self._data(self.worker.get(PATH)), {'enabled': False, 'games': []})
        selection = {
            'game_id': self.game.pk, 'price_min': '80.00', 'price_max': '100.00',
            'include_keywords': ['排位', '上分'], 'exclude_keywords': ['急单'],
        }
        expected = {'enabled': True, 'games': [selection]}
        self.assertEqual(self._data(self._patch(expected)), expected)
        self.assertEqual(self._data(self.worker.get(PATH)), expected)
        other = self._worker('hall-push-other', games=[self.game])
        self.assertEqual(self._data(other.get(PATH)), {'enabled': False, 'games': []})
        self.assertNotEqual(self._patch({'enabled': True, 'games': [{'game_id': self.other_game.pk}]}).json()['code'], 0)
        self.assertEqual(self._data(self.worker.get(PATH)), expected)

    def test_only_a_new_matching_hall_order_reaches_the_subscribed_worker_once(self):
        self._configure_push()
        self._publish(self._make_order(861, '周末排位'))
        self._data(self._patch({'enabled': True, 'games': [{
            'game_id': self.game.pk, 'price_min': '80.00', 'price_max': '90.00',
            'include_keywords': ['不命中的词', '排位'], 'exclude_keywords': ['另一个排除词', '急单'],
        }]}))
        matches = self._make_order(862, '周末排位')
        self._publish(matches)
        self._publish(self._make_order(863, '深夜排位急单'))
        self._publish(self._make_order(864, '周末活动'))
        self._publish(self._make_order(865, '周末排位', price='79.99'))
        self._publish(self._make_order(866, '周末排位', price='90.01'))
        self._drain()
        self._data(self.admin.post(f'/api/business/orders/{matches.order_no}/withdraw/'))
        self._publish(matches)
        self._drain()

        item, = self.received
        self.assertEqual(item['uids'], [UID])
        self.assertIn('synthetic-862', item['content'])
        self.assertIn('87.00', item['content'])
        self.assertIn('<strong>游戏</strong>　合成大厅测试游戏', item['content'])
        self.assertIn('<strong>代练内容</strong>　周末排位', item['content'])
        self.assertIn(item['url'], item['content'].replace('&amp;', '&'))
        self.assertEqual(urlsplit(item['url']).path, '/m/wxpusher/hall-entry')

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_hall_top_and_body_share_the_order_link_regardless_of_app_deposit(self):
        self._configure_push()
        self._subscribe_unfiltered()
        domain = self.tenant.get_primary_domain().domain
        for suffix, project in (
            (867, '可在网页接单'), (868, '也可在 App 接单'),
        ):
            order = self._make_order(suffix, project)
            self._publish(order)
            self._drain()
            item = self.received[-1]
            entry = urlsplit(item['url'])
            self.assertEqual((entry.scheme, entry.netloc, entry.path), ('https', domain, '/m/wxpusher/hall-entry'))
            self.assertEqual(parse_qs(entry.query), {
                'entry': ['wxpusher'], 'shared': ['1'], 'source': ['local'], 'ref': [order.order_no],
            })
            self.assertEqual(entry.fragment, '')
            self.assertIn(item['url'], item['content'].replace('&amp;', '&'))
            self.assertIn('前往抢单', item['content'])
            self.assertIn(project, item['content'])
            if suffix == 867:
                TakerWallet.objects.create(user=self.recipient, balance='80.00', frozen='0.00')

    def test_disabling_then_reenabling_does_not_release_old_events(self):
        self._configure_push()
        self._subscribe_unfiltered()
        self._publish(self._make_order(871, '第一张曾订阅订单'))
        self._subscribe_unfiltered(enabled=False)
        self._subscribe_unfiltered()
        withdrawn = self._make_order(872, '即刻撤回订单')
        self._publish(withdrawn)
        self._data(self.admin.post(f'/api/business/orders/{withdrawn.order_no}/withdraw/'))
        self._publish(self._make_order(873, '含各类操作需求'))
        self._drain()
        item, = self.received
        self.assertIn('synthetic-873', item['content'])

    def test_openapi_publication_without_a_staff_sender_notifies_the_worker(self):
        self._configure_push()
        self._subscribe_unfiltered()
        app = ApiApp.objects.create(
            name='合成开放平台', app_key='synthetic-hall-openapi-key', app_secret='synthetic-hall-openapi-secret',
            created_by=get_user_model().objects.get(username='hall-push-admin'),
        )
        order = self._make_order(878, '开放平台发单')
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        self._data(client.post(f'/api/open/v1/orders/{order.order_no}/status/', {
            'status': OrderStatus.WAITING_GRAB,
        }, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {tokens.issue(app.app_key, app.app_secret)}',
            HTTP_IDEMPOTENCY_KEY='synthetic-openapi-hall-publication',
        ))
        order.refresh_from_db()
        self.assertIsNone(order.hall_publisher_id)
        self._drain()
        item, = self.received
        self.assertIn('synthetic-878', item['content'])

    def test_sensitive_hall_project_text_stays_inside_the_hall(self):
        self._configure_push()
        self._subscribe_unfiltered()
        self._publish(self._make_order(880, '微信号 synthetic-chat\n请拨 13800000000 或 customer@example.invalid'))
        self._drain()
        item, = self.received
        self.assertNotIn('synthetic-chat', item['content'])
        self.assertNotIn('13800000000', item['content'])
        self.assertNotIn('customer@example.invalid', item['content'])
        self.assertIn('<strong>代练内容</strong>　请在接单大厅查看', item['content'])

    def test_hall_push_preserves_public_instructions_before_private_details(self):
        self._configure_push()
        self._subscribe_unfiltered()
        public = '手机游戏，打排位 5 把，完成后联系工作室验收'
        self._publish(self._make_order(918, public))
        self._publish(self._make_order(919, public + '，微信: synthetic-handle\nsynthetic-continuation'))
        self._publish(self._make_order(941, public + '，138\n0000\n0000，密码: synthetic-secret'))

        self._drain()

        for number in (918, 919, 941):
            with self.subTest(order=number):
                content = next(item['content'] for item in self.received if f'synthetic-{number}' in item['content'])
                self.assertIn('<strong>代练内容</strong>　' + public, content)
                self.assertNotIn('synthetic-handle', content)
                self.assertNotIn('synthetic-continuation', content)
                self.assertNotIn('138\n0000', content)
                self.assertNotIn('synthetic-secret', content)

    def test_hall_push_shows_explicit_tasks_around_private_fields(self):
        self._configure_push()
        self._subscribe_unfiltered()
        samples = (
            ('打排位 5 把，synthetic-secret，以上是密码', '请在接单大厅查看'),
            ('打排位 5 把；登录名: synthetic-player；密码: synthetic-secret', '打排位 5 把'),
            ('打排位 5 把；手机游戏 ID: synthetic-player', '打排位 5 把'),
            ('密码: synthetic-secret。打排位 5 把', '打排位 5 把'),
            ('账号: synthetic-player；任务: 打排位 5 把', '任务: 打排位 5 把'),
            ('完成后联系工作室验收！', '完成后联系工作室验收！'),
            ('完成后联系工作室验收即可', '完成后联系工作室验收即可'),
            ('完成后联系工作室验收，wxid_synthetic_contact', '完成后联系工作室验收'),
            ('完成后联系工作室验收，@synthetic_contact', '完成后联系工作室验收'),
            ('完成后联系工作室验收: synthetic-secret', '请在接单大厅查看'),
            ('打排位 5 把，以上是密码', '请在接单大厅查看'),
            ('打排位 5 把，以上是登录密码', '请在接单大厅查看'),
            ('打排位 5 把，完成 2 场对局，以上都是密码', '请在接单大厅查看'),
            ('密码: synthetic-secret。打排位 5 把，以上其实是密码', '请在接单大厅查看'),
            ('手机游戏，ID：synthetic-login-id', '手机游戏'),
            ('手机游戏，138\u200b0000\u200b0000', '手机游戏'),
        )
        for number, (project, _) in enumerate(samples, 950):
            self._publish(self._make_order(number, project))

        self._drain()
        self._drain()

        for number, (_, public) in enumerate(samples, 950):
            with self.subTest(order=number):
                content = next(item['content'] for item in self.received if f'synthetic-{number}' in item['content'])
                self.assertIn('<strong>代练内容</strong>　' + public + '</p>', content)
                self.assertNotIn('synthetic-secret', content)
                self.assertNotIn('synthetic-player', content)
                self.assertNotIn('synthetic_contact', content)
                self.assertNotIn('synthetic-login-id', content)
                self.assertNotIn('138\u200b0000\u200b0000', content)
                if public == '请在接单大厅查看':
                    self.assertNotIn('打排位 5 把', content)
                    self.assertNotIn('完成 2 场对局', content)

    def test_hall_push_keeps_ambiguous_password_instructions_in_hall(self):
        self._configure_push()
        self._subscribe_unfiltered()
        task = '打排位 5 把'
        for number, instruction in enumerate((
            '密码就是上面那句',
            '上面这句话就是我的密码',
            '上面的内容用作密码',
            '密码：上面那句',
        ), 970):
            self._publish(self._make_order(number, f'{task}，{instruction}'))

        self._drain()

        self.assertEqual(len(self.received), 4)
        for item in self.received:
            self.assertIn('<strong>代练内容</strong>　请在接单大厅查看</p>', item['content'])
            self.assertNotIn(task, item['content'])

    def test_hall_push_filters_traditional_chinese_private_fields(self):
        self._configure_push()
        self._subscribe_unfiltered()
        samples = (
            ('完成后联系工作室验收。密碼：mock-secret', '完成后联系工作室验收'),
            ('帳號：mock-player；任務：打排位 ５ 場', '任務：打排位 ５ 場'),
            ('完成後聯繫工作室驗收，驗證碼：mock-code', '完成後聯繫工作室驗收'),
            ('聯繫方式：mock-contact。手機遊戲', '手機遊戲'),
            ('打排位 ５ 場，密碼就是上面那句', '请在接单大厅查看'),
            ('完成後聯繫工作室驗收。遊戲賬密：mock-secret', '完成後聯繫工作室驗收'),
            ('登入憑證：mock-token；任務：打排位 ５ 場', '任務：打排位 ５ 場'),
            ('打排位 ５ 場，拿上面這句當登陸憑據', '请在接单大厅查看'),
            ('打排位 ５ 場，請提供驗收憑證', '打排位 ５ 場，請提供驗收憑證'),
        )
        for number, (project, _) in enumerate(samples, 980):
            self._publish(self._make_order(number, project))

        self._drain()

        for number, (_, expected) in enumerate(samples, 980):
            with self.subTest(order=number):
                content = next(item['content'] for item in self.received if f'synthetic-{number}' in item['content'])
                self.assertIn(f'<strong>代练内容</strong>　{expected}</p>', content)
                self.assertNotIn('mock-', content)
                if expected == '请在接单大厅查看':
                    self.assertNotIn('打排位 ５ 場', content)

    def test_hall_push_filters_labeled_contact_information_and_landlines(self):
        self._configure_push()
        self._subscribe_unfiltered()
        samples = [
            ('周末排位 Account: player-demo', 'player-demo'),
            ('周末排位 Discord: teammate-demo', 'teammate-demo'),
            ('周末排位 Telegram: team-demo', 'team-demo'),
            ('周末排位 Phone: synthetic-telephone', 'synthetic-telephone'),
            ('周末排位 Contact: synthetic-contact', 'synthetic-contact'),
            ('周末排位 Line ID: synthetic-line', 'synthetic-line'),
            ('周末排位 010-12345678', '010-12345678'),
            ('周末排位 V信 synthetic-vchat', 'synthetic-vchat'),
            ('周末排位 Discord @synthetic-discord', '@synthetic-discord'),
            ('周末排位 Telegram @synthetic-telegram', '@synthetic-telegram'),
            ('周末排位 Line: @synthetic-player', '@synthetic-player'),
            ('周末排位 Steam ID: synthetic-steam', 'synthetic-steam'),
            ('周末排位 ＋８６ １３８ ００００ ００００', '＋８６ １３８ ００００ ００００'),
        ]
        for number, (project, _secret) in enumerate(samples, 920):
            self._publish(self._make_order(number, project))
        safe_project = 'Line: 中路任务，完成五场对局 Ｌｉｎｅ：保留原文'
        self._publish(self._make_order(940, safe_project))

        self._drain()
        self._drain()

        for number, (_, secret) in enumerate(samples, 920):
            with self.subTest(secret=secret):
                content = next(item['content'] for item in self.received if f'synthetic-{number}' in item['content'])
                self.assertNotIn(secret, content)
                self.assertIn('<strong>代练内容</strong>　请在接单大厅查看', content)
        safe = next(item['content'] for item in self.received if 'synthetic-940' in item['content'])
        self.assertIn(safe_project, safe)

    def test_hall_order_with_no_project_never_uses_private_order_data(self):
        self._configure_push()
        self._subscribe_unfiltered()
        order = self._make_order(881, '')
        order.demand = '合成打手需求，接单后可见'
        order.content = '合成客户原始需求'
        order.save(update_fields=['demand', 'content'])
        self._publish(order)

        self._drain()

        item, = self.received
        self.assertIn('<strong>代练内容</strong>　未填写', item['content'])
        self.assertNotIn('合成打手需求', item['content'])
        self.assertNotIn('合成客户原始需求', item['content'])

    def test_publication_registration_failure_keeps_a_successful_hall_listing(self):
        self._configure_push()
        self._subscribe_unfiltered()
        order = self._make_order(882, '报名失败仍上大厅')
        with patch.object(WxPusherHallPublication.objects, 'get_or_create', side_effect=DatabaseError):
            self._publish(order)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self._drain()
        self.assertEqual(self.received, [])

    def test_a_failed_new_order_does_not_delay_existing_business_pushes_or_other_new_orders(self):
        from apps.business.api.hall_claim_target import hall_notice_card

        self._configure_push()
        self._data(self.admin.patch(ADMIN_CONFIG, {'order_enabled': True}, content_type='application/json'))
        self._subscribe_unfiltered()
        failed = self._make_order(883, '首单读取出错')
        self._publish(failed)
        self._publish(self._make_order(884, '正常新单'))
        enqueue_worker_notice(user_id=self.recipient.pk, kind='paused', reference='synthetic-business-priority')

        def load_card(source, ref, game_id):
            if ref == failed.order_no:
                raise RuntimeError('synthetic hall snapshot failure')
            return hall_notice_card(source, ref, game_id)

        with patch('apps.business.api.hall_claim_target.hall_notice_card', side_effect=load_card):
            self._drain()
        self.assertEqual([item['summary'] for item in self.received], ['订单已暂停', '接单大厅有新订单'])
        self.assertIn('synthetic-884', self.received[-1]['content'])
        WxPusherHallPublication.objects.filter(ref=failed.order_no).update(
            retry_at=timezone.now() - timedelta(seconds=1),
        )
        self._drain()
        self.assertIn('synthetic-883', self.received[-1]['content'])

    def test_mirror_sources_can_share_display_number_and_still_only_push_once_each(self):
        self._configure_push()
        self._subscribe_unfiltered()
        account = get_user_model().objects.get(username='hall-push-admin')
        upstream = JidanpuUpstreamBinding.objects.create(
            user=account, jidanpu_base_url='https://upstream-hall-test.invalid', account='synthetic-upstream',
            password='synthetic-password', enabled=True, hall_price_percent=50,
        )
        peer = PeerUpstreamBinding.objects.create(
            peer_name='合成上家', peer_base_url='https://peer-hall-test.invalid',
            peer_account='synthetic-peer', token_in_hash=hash_token('synthetic-hall-token'),
            token_out='synthetic-hall-outbound', local_base_url='https://local-hall-test.invalid',
            bound_by=account, hall_sync_enabled=True,
        )
        shared = '00000000000000000887'
        rows = [{
            'orderId': shared, 'orderIdView': shared, 'gameName': self.game.name,
            'gameArea': '安卓', 'gameServer': '微信区', 'project': '合成上家任务', 'recFee': '88.00',
        }]
        peer_event = hall_event(shared, game_name=self.game.name, price='55.00')
        for index in range(2):
            sync_hall(binding=upstream, client=_StubClient(rows=rows), now=timezone.now())
            if index == 0:
                now = timezone.now()
                apply_hall_pull_rows(
                    peer, [{'target_ref': shared, 'payload': peer_event['payload']}], now=now, round_started_at=now,
                )
            apply_events([peer_event, hall_event('00000000000000000888', game_name=self.game.name, price='66.00')],
                         binding=peer)
            self._drain()

        self.assertEqual(len(self.received), 3)
        self.assertEqual(sum(shared in item['content'] for item in self.received), 2)
        self.assertEqual(sum('44.00' in item['content'] for item in self.received), 1)
        self.assertEqual(sum('55.00' in item['content'] for item in self.received), 1)
        self.assertEqual(sum('66.00' in item['content'] for item in self.received), 1)
        self.assertEqual(len({item['url'] for item in self.received}), 3)

    def test_upstream_card_notifies_when_its_first_game_mapping_makes_it_visible(self):
        self._configure_push()
        self._subscribe_unfiltered()
        upstream = JidanpuUpstreamBinding.objects.create(
            user=get_user_model().objects.get(username='hall-push-admin'),
            jidanpu_base_url='https://upstream-unmapped-hall.test.invalid', account='synthetic-unmapped',
            password='synthetic-password', enabled=True,
        )
        row = {
            'orderId': 'synthetic-unmapped-order', 'orderIdView': 'synthetic-mapped-card',
            'gameName': '未知海外副本', 'project': '合成映射任务', 'recFee': '88.00',
        }
        sync_hall(binding=upstream, client=_StubClient(per_game_rows={self.game.name: [row]}), now=timezone.now())
        self._drain()
        self.assertEqual(self.received, [])
        row['gameName'] = self.game.name
        for _ in range(2):
            sync_hall(binding=upstream, client=_StubClient(rows=[row]), now=timezone.now())
            self._drain()
        item, = self.received
        self.assertIn('synthetic-mapped-card', item['content'])

    def test_app_and_h5_share_subscriptions_and_app_only_accounts_can_edit(self):
        token = 'synthetic-hall-bearer-token'
        AppToken.objects.create(
            user=self.recipient, key_hash=hash_token(token), expires_at=timezone.now() + timedelta(hours=1),
        )
        app_client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain, HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(self._data(app_client.get(PATH)), {'enabled': False, 'games': []})
        self._subscribe_unfiltered()
        self.assertEqual(self._data(app_client.get(PATH)), self._data(self.worker.get(PATH)))
        self.recipient.staff_profile.account_source = AccountSource.APP
        self.recipient.staff_profile.save(update_fields=['account_source'])
        self._configure_push()
        self._publish(self._make_order(890, 'App 专属打手能接到的新单'))
        self._drain()
        item, = self.received
        self.assertIn('synthetic-890', item['content'])
        self.assertEqual(self._data(self._patch({'enabled': False, 'games': []}, client=app_client)),
                         {'enabled': False, 'games': []})

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_wallet_and_preferred_claim_restrictions_do_not_filter_subscribed_workers(self):
        self._configure_push()
        self._subscribe_unfiltered()
        self._publish(self._make_order(891, '普通单，押金不足'))
        self._drain()
        item, = self.received
        self.assertIn('synthetic-891', item['content'])
        wallet = TakerWallet.objects.create(user=self.recipient, balance='80.00', frozen='0.00')
        self._publish(self._make_order(892, '普通单，押金足够'))
        self._publish(self._make_order(893, '新手不能接的优选单', price='300.00'))
        self._drain()
        self.assertEqual(len(self.received), 3)
        self.assertIn('synthetic-892', self.received[1]['content'])
        self.assertIn('synthetic-893', self.received[2]['content'])
        wallet.refresh_from_db()
        self.assertEqual(str(wallet.balance), '80.00')
        self.assertEqual(str(wallet.frozen), '0.00')

    def test_subscribers_keep_price_redaction_without_learning_price_through_filters(self):
        self._configure_push()
        users, uids = self._subscribe_synthetic_workers(5)
        restriction = PermissionNode.objects.get(code=RESTRICT_VIEW_OFFER_PRICE)
        hidden_role_group = Group.objects.create(name='隐价')
        hidden_role = Role.objects.create(name=hidden_role_group.name, group=hidden_role_group)
        hidden_role.permissions.add(restriction)
        other_hidden_group = Group.objects.create(name='也隐价')
        other_hidden_role = Role.objects.create(name=other_hidden_group.name, group=other_hidden_group)
        other_hidden_role.permissions.add(restriction)
        users[1].groups.add(hidden_role_group, other_hidden_group)
        users[2].groups.add(other_hidden_group)
        users[3].groups.add(hidden_role_group, other_hidden_group, Group.objects.get(name=TENANT_ADMIN))
        WxPusherHallSubscription.objects.filter(preference_id=users[2].pk).update(price_min='80.00')
        order = self._make_order(894, '合成公开代练任务')
        self._publish(order)
        process_hall_publications(load_wxpusher_config(), limit=1, should_stop=None)
        users[4].groups.add(other_hidden_group)
        self._drain()
        self.assertEqual(len(self.received), 2)
        self.assertCountEqual(self.received[0]['uids'], [uids[0], uids[3]])
        self.assertIn('87.00', self.received[0]['content'])
        self.assertCountEqual(self.received[1]['uids'], [uids[1], uids[4]])
        self.assertNotIn('87.00', self.received[1]['content'])
        self.assertEqual(self.received[0]['url'], self.received[1]['url'])
        records = WxPusherNotice.objects.filter(kind=HALL_NEW_ORDER, order_no=order.order_no)
        self.assertEqual(records.get(user=users[1]).details['amount'], '')
        self.assertFalse(records.filter(user=users[2]).exists())

    def test_subscription_keywords_use_full_public_project_without_private_fields(self):
        self._configure_push()
        users, uids = self._subscribe_synthetic_workers(3)
        WxPusherHallSubscription.objects.filter(preference_id=users[0].pk).update(
            include_keywords=['synthetic-secret'],
        )
        WxPusherHallSubscription.objects.filter(preference_id=users[1].pk).update(
            exclude_keywords=['synthetic-secret'],
        )
        WxPusherHallSubscription.objects.filter(preference_id=users[2].pk).update(
            include_keywords=['末尾公开任务'],
        )
        self._publish(self._make_order(895, '打排位 5 把；登录名: synthetic-player；密码: synthetic-secret'))
        self._publish(self._make_order(896, '公开任务' * 100 + '末尾公开任务'))
        self._drain()
        first, second = self.received
        self.assertEqual(first['uids'], [uids[1]])
        self.assertIn('打排位 5 把', first['content'])
        self.assertNotIn('synthetic-secret', first['content'])
        self.assertCountEqual(second['uids'], [uids[1], uids[2]])
        self.assertNotIn('末尾公开任务', second['content'])

    def test_subscription_cannot_be_read_or_updated_from_another_tenant_session(self):
        second, _ = provision_tenant(
            schema_name='hall_subscription_other', name='合成抢单工作室乙', domain='hall-subscription-other.test.local',
        )
        self._subscribe_unfiltered()
        token = 'synthetic-hall-tenant-token'
        AppToken.objects.create(
            user=self.recipient, key_hash=hash_token(token), expires_at=timezone.now() + timedelta(hours=1),
        )
        other_app = Client(SERVER_NAME='hall-subscription-other.test.local', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(other_app.get(PATH).status_code, 401)
        other = Client(SERVER_NAME='hall-subscription-other.test.local')
        other.cookies.update(self.worker.cookies)
        self.assertEqual(other.get(PATH).status_code, 401)
        self.assertEqual(self._patch({'enabled': False, 'games': []}, client=other).status_code, 401)
        connection.set_tenant(self.tenant)
        own = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        self._data(own.post('/api/auth/login', {
            'username': self.recipient.username, 'password': P2_TEST_PASSWORD,
        }, content_type='application/json'))
        self.assertTrue(self._data(own.get(PATH))['enabled'])
        self._configure_push()
        hidden_group = Group.objects.create(name='甲隐价')
        hidden_role = Role.objects.create(group=hidden_group, name=hidden_group.name)
        hidden_role.permissions.add(PermissionNode.objects.get(code=RESTRICT_VIEW_OFFER_PRICE))
        self.recipient.groups.add(hidden_group)
        local_order = self._make_order(897, '工作室甲订单')
        self._publish(local_order)
        with tenant_context(second):
            game = Game.objects.create(name='隔离验证游戏')
            second_worker = make_logged_in_client(
                domain='hall-subscription-other.test.local', username='hall-push-other-worker',
                groups=(WORKER,), nodes=('my_profile', 'grab_hall.take_order'), games=[game],
            )
            self.assertEqual(self._data(second_worker.get(PATH)), {'enabled': False, 'games': []})
            self._data(self._patch({'enabled': True, 'games': [{'game_id': game.pk}]}, client=second_worker))
            second_admin = make_logged_in_client(
                domain='hall-subscription-other.test.local', username='synthetic-second-admin', groups=(TENANT_ADMIN,),
            )
            self._data(second_admin.patch(ADMIN_CONFIG, {
                'app_id': '993421', 'app_token': 'AT_synthetic_other_hall', 'push_enabled': True,
            }, content_type='application/json'))
            second_account = get_user_model().objects.get(username='hall-push-other-worker')
            WxPusherBinding.objects.create(user=second_account, app_id='993421', uid='UID_OTHER_TENANT_HALL')
            other_order = Order.objects.create(
                order_no='00000000000000000898', game=game, status=OrderStatus.UNASSIGNED,
                hall_price='87.00', project_text='工作室乙订单',
            )
            self._data(second_admin.post(f'/api/business/orders/{other_order.order_no}/throw-to-hall/'))

        with tenant_context(self.tenant):
            send_notification_batch(self.tenant.get_primary_domain().domain, mode='hall')
            self.assertFalse(WxPusherHallPublication.objects.filter(ref=other_order.order_no).exists())
        item, = self.received
        self.assertEqual(item['uids'], [UID])
        self.assertIn(local_order.order_no, item['url'])
        self.assertNotIn(other_order.order_no, item['url'])
        self.assertNotIn('87.00', item['content'])
        with tenant_context(second):
            self.assertIsNone(WxPusherHallPublication.objects.get(ref=other_order.order_no).processed_at)
            send_notification_batch(second.get_primary_domain().domain, mode='hall')
        self.assertEqual(self.received[1]['uids'], ['UID_OTHER_TENANT_HALL'])
        self.assertIn('87.00', self.received[1]['content'])
