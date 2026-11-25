import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from unittest.mock import patch
import httpx
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, Performance
from apps.internal_chat.models import Conversation, OrderGroup
from chat_service.protocol import sign_request
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_chat_service_bridge import ServiceClient

PATH = '/api/internal-chat/service/order-authorizations/'


class ChatOrderAuthorityTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant
        cls.other_tenant,_ = provision_tenant(schema_name='chat_authority_other',name='权限工作室乙',domain='authority-b.test.local')
        cls.added_host = 'authority-b.test.local' not in settings.ALLOWED_HOSTS
        if cls.added_host:
            settings.ALLOWED_HOSTS.append('authority-b.test.local')

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.other_tenant.delete(force_drop=True)
        if cls.added_host:
            settings.ALLOWED_HOSTS.remove('authority-b.test.local')
        super().tearDownClass()

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.enterContext(override_settings(DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, DEPOSIT_ENFORCED=False))
        self.folder = self.enterContext(TemporaryDirectory())
        self.config = Path(self.folder) / 'service.json'
        self.config.write_text(json.dumps({'service_url': 'http://127.0.0.1:9050', 'tenants': {
            connection.schema_name: {'key_id': 'authority-a', 'secret': 'synthetic-authority-a-key'},
            'chat_authority_other':{'key_id':'authority-b','secret':'synthetic-authority-b-key'},
        }}))
        self.config.chmod(0o600)
        self.enterContext(override_settings(TT_CHAT_SERVICE_CONFIG_FILE=str(self.config)))
        self.game = Game.objects.create(name='权限合成游戏')
        self.staff = self.user('authority-staff', nodes=('order.view_detail',))
        self.worker = self.user('authority-worker', nodes=('my_orders.send_to_my_system',))
        self.other = self.user('authority-other', nodes=('my_orders.send_to_my_system',))
        self.order = Order.objects.create(order_no='20261120000000000111', game=self.game, hall_publisher=self.staff)
        self.performance = Performance.objects.create(order=self.order, user=self.worker, emp_type=EmpType.MAIN)
        self.group = OrderGroup.objects.create(conversation=Conversation.objects.create(), order=self.order,
                                               order_no=self.order.order_no, wire_id=uuid4())
        self.group.members.create(wire_key='a', local_user=self.staff, name='合成客服', publisher=True)
        self.group.members.create(wire_key='b:1', local_user=self.worker, name='合成打手', worker=True)

    def user(self, name, **values):
        make_logged_in_client(domain=self.domain.domain, username=name, groups=(str(get_user_model().objects.count())+'权限',), **values)
        return get_user_model().objects.get(username=name)

    def signed(self, payload, *, key='authority-a', secret='synthetic-authority-a-key', **headers):
        body = json.dumps(payload, separators=(',', ':')).encode()
        signing = sign_request(secret, key, 'POST', PATH, body)
        return ServiceClient(SERVER_NAME=self.domain.domain).post(PATH, body, content_type='application/json',
            **{'HTTP_' + k.upper().replace('-', '_'): v for k,v in signing.items()}, **headers)

    def checks(self):
        return {'checks': [{'conversation_id': self.group.conversation_id,
                            'user_ids': [self.staff.pk, self.worker.pk, self.other.pk]}]}

    def test_authority_uses_current_order_permissions_for_entire_group(self):
        response = self.signed(self.checks())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['code'], 0, response.content)
        row = response.json()['data']['groups'][0]
        self.assertEqual(set(row['read_user_ids']), {self.staff.pk, self.worker.pk})
        self.assertEqual(row['worker_id'], self.worker.pk)
        self.assertEqual(row['default_join_user_ids'], [self.staff.pk, self.worker.pk])
        self.assertEqual(row['reopen_user_ids'], [])

    def group_data(self, params=None):
        response = self.signed(params or self.checks())
        self.assertEqual((response.status_code, response.json()['code']), (200, 0), response.content)
        return response.json()['data']

    def test_account_game_and_node_revocation_are_fresh_without_member_changes(self):
        from apps.rbac.models import Role
        before = list(self.group.members.values())
        self.staff.staff_profile.games.clear()
        self.assertEqual(self.group_data()['groups'][0]['read_user_ids'], [self.worker.pk])
        self.staff.staff_profile.games.add(self.game)
        role = Role.objects.get(group__user=self.staff)
        role.permissions.clear()
        self.assertEqual(self.group_data()['groups'][0]['read_user_ids'], [self.worker.pk])
        get_user_model().objects.filter(pk=self.worker.pk).update(is_active=False)
        self.assertEqual(self.group_data()['groups'][0]['read_user_ids'], [])
        self.assertEqual(list(self.group.members.values()), before)

    def test_reopen_requires_backend_detail_and_add_edit_not_only_worker_access(self):
        from tests.p5_auth_helpers import grant_nodes
        grant_nodes(self.worker, 'order.add_edit')
        self.assertEqual(self.group_data()['groups'][0]['reopen_user_ids'], [])
        grant_nodes(self.staff, 'order.add_edit')
        self.assertEqual(self.group_data()['groups'][0]['reopen_user_ids'], [self.staff.pk])
        self.staff.staff_profile.games.clear()
        self.assertEqual(self.group_data()['groups'][0]['reopen_user_ids'], [])

    def test_current_worker_reassignment_retains_group_membership_and_history_facts(self):
        from apps.internal_chat.models import Change, Message
        from apps.internal_chat.order_groups import assignment_changed
        old_member = self.group.members.get(local_user=self.worker)
        with override_settings(TT_CHAT_CORE_BACKEND='service'), transaction.atomic():
            self.performance.user = self.other
            self.performance.save(update_fields=['user'])
            assignment_changed(self.order, self.other, former_worker=self.worker)
        self.assertEqual(self.group_data()['groups'][0]['read_user_ids'], [self.staff.pk, self.other.pk])
        self.assertEqual(OrderGroup.objects.get(order=self.order).conversation_id, self.group.conversation_id)
        old_member.refresh_from_db()
        self.assertIsNone(old_member.left_at)
        self.assertTrue(old_member.worker)
        self.assertFalse(self.group.members.filter(local_user=self.other).exists())
        self.assertIn(self.other.pk,[p['local_user_id'] for p in self.group_data()['groups'][0]['participants']])
        self.assertEqual((Message.objects.count(), Change.objects.count()), (0, 0))
        self.performance.user = self.worker
        self.performance.save(update_fields=['user'])
        self.assertIn(self.worker.pk, self.group_data()['groups'][0]['read_user_ids'])

    def test_discovery_returns_default_join_groups_without_chat_core_membership(self):
        for user in (self.staff, self.worker):
            result = self.group_data({'checks': [], 'viewer_id': user.pk, 'discover': True})
            self.assertTrue(result['has_order_access'])
            self.assertEqual([r['conversation_id'] for r in result['groups']], [self.group.conversation_id])
            self.assertIn(user.pk, result['groups'][0]['default_join_user_ids'])
        self.assertEqual(self.group_data({'checks': [], 'viewer_id': self.other.pk, 'discover': True})['groups'], [])

    def test_members_are_permanent_facts_including_legacy_exit_marker(self):
        member = self.group.members.get(local_user=self.worker)
        member.left_at = timezone.now()
        member.save(update_fields=['left_at'])
        saved = member.left_at
        row = self.group_data()['groups'][0]
        self.assertIn(self.worker.pk, row['read_user_ids'])
        self.assertTrue(all(m['left_at'] is None for m in row['room']['members']))
        member.refresh_from_db()
        self.assertEqual(member.left_at, saved)

    def test_completed_is_authoritative_but_other_statuses_do_not_add_lifecycle_rules(self):
        from apps.business.models import OrderStatus
        self.assertFalse(self.group_data()['groups'][0]['completed'])
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=['status'])
        row = self.group_data()['groups'][0]
        self.assertTrue(row['completed'])
        self.assertIn(self.worker.pk, row['read_user_ids'])

    def test_dependency_failure_does_not_rewrite_participation_or_grant_permissions(self):
        before = list(self.group.members.values())
        with patch('apps.internal_chat.order_authority._operator_orders', side_effect=DatabaseError('authority unavailable')):
            response = self.signed(self.checks())
        self.assertEqual((response.status_code, response.json()['code']), (503, 503))
        self.assertEqual(response.json()['msg'], '暂时无法确认订单权限，请稍后重试')
        self.assertEqual(list(self.group.members.values()), before)

    def test_authority_rejects_user_credentials_wrong_key_and_unsigned_overrides(self):
        for extra in ({'HTTP_COOKIE':'sessionid=synthetic'}, {'HTTP_AUTHORIZATION':'Bearer synthetic'}):
            response = self.signed(self.checks(), **extra)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.signed(self.checks(), key='authority-b', secret='synthetic-b-key').status_code, 403)
        for payload in ({**self.checks(), 'tenant':'another'},
                        {'checks':[{'conversation_id':self.group.conversation_id,'user_ids':[self.worker.pk], 'readonly':True}]},
                        {'checks':[], 'discover':True}):
            self.assertNotEqual(self.signed(payload).json()['code'], 0)

    def test_reopen_endpoint_uses_authenticated_viewer_and_same_order_group(self):
        captured = []
        def remote(url, **kwargs):
            body = json.loads(kwargs['content']);captured.append(body)
            return httpx.Response(200, json={'code':0, 'data':{'id':self.group.conversation_id, 'last_message':None,
                                    'archived':False, 'has_conversation':True, 'can_reopen':False, 'can_send':True}}, request=httpx.Request('POST',url))
        client = make_logged_in_client(domain=self.domain.domain, username='reopener', groups=('重开岗',), nodes=('order.view_detail','order.add_edit'))
        uid = get_user_model().objects.get(username='reopener').pk
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=remote):
            response = client.post('/api/internal-chat/orders/'+self.order.order_no+'/reopen/', {'viewer_id':self.worker.pk},content_type='application/json')
        self.assertEqual(response.json()['code'],0,response.content)
        self.assertEqual([(c['operation'],c['viewer_id'],c['query']) for c in captured],
                         [('order_preview',uid,{'order_no':self.order.order_no}),('reopen',uid,{'conversation_id':self.group.conversation_id})])

    def test_scope_invalidation_notifies_affected_users_without_reading_group_history(self):
        import re
        from apps.rbac.models import Role
        def reject_group_history(execute, sql, params, many, context):
            self.assertFalse(re.search(r'internal_chat_(groupmember|ordergroup|message)',sql), sql)
            return execute(sql,params,many,context)
        role = Role.objects.get(group__user=self.staff)
        with override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify'), patch('apps.internal_chat.realtime._notify') as notify, connection.execute_wrapper(reject_group_history), self.captureOnCommitCallbacks(execute=True):
            role.permissions.clear()
        notify.assert_called_once_with(connection.schema_name,[self.staff.pk],'http://127.0.0.1:4194/notify','synthetic')

    def test_source_batch_authorizes_one_thousand_and_ten_thousand_groups_without_per_group_queries(self):
        orders = Order.objects.bulk_create([Order(order_no=f'20261121{i:012d}',game=self.game,hall_publisher=self.staff) for i in range(10000)])
        conversations = Conversation.objects.bulk_create([Conversation() for _ in orders])
        groups = OrderGroup.objects.bulk_create([OrderGroup(order=order,conversation=conv,order_no=order.order_no,wire_id=uuid4()) for order,conv in zip(orders,conversations)])
        counts = []
        for size in (1000,10000):
            payload={'checks':[{'conversation_id':g.conversation_id,'user_ids':[self.staff.pk]} for g in groups[:size]],'viewer_id':self.staff.pk}
            with CaptureQueriesContext(connection) as queries:
                result=self.group_data(payload)
            counts.append(sum(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
            print('AUTHORITY_SCALE_SELECTS',size,counts[-1],flush=True)
            self.assertEqual(len(result['groups']),size)
            self.assertTrue(all(row['read_user_ids']==[self.staff.pk] for row in result['groups']))
        self.assertLessEqual(counts[1],counts[0]+2)
        self.assertLessEqual(counts[1],20)

    def test_worker_fallback_does_not_grant_reopen_outside_operator_game_scope(self):
        from tests.p5_auth_helpers import grant_nodes
        grant_nodes(self.worker, 'order.view_detail', 'order.add_edit')
        self.worker.staff_profile.games.clear()
        row=self.group_data()['groups'][0]
        self.assertIn(self.worker.pk,row['read_user_ids'])
        self.assertNotIn(self.worker.pk,row['reopen_user_ids'])
        client=Client(SERVER_NAME=self.domain.domain)
        response=client.post('/api/auth/login', {'username':self.worker.username,'password':'pw-p2-api'},content_type='application/json')
        self.assertEqual(response.json()['code'],0)
        detail=client.get('/api/business/my-orders/'+self.order.order_no+'/detail/')
        self.assertEqual(detail.json()['code'],0,detail.content)

    def test_discovery_checks_other_member_permissions_on_first_response(self):
        result=self.group_data({'checks':[],'viewer_id':self.staff.pk,'discover':True})
        self.assertEqual(set(result['groups'][0]['read_user_ids']),{self.staff.pk,self.worker.pk})

    def test_own_tenant_key_is_rejected_on_other_tenant_host(self):
        body=json.dumps(self.checks(),separators=(',',':')).encode()
        headers=sign_request('synthetic-authority-a-key','authority-a','POST',PATH,body)
        other=ServiceClient(SERVER_NAME='authority-b.test.local')
        response=other.post(PATH,body,content_type='application/json',**{'HTTP_'+k.upper().replace('-','_'):v for k,v in headers.items()})
        self.assertEqual(response.status_code,403)
        headers=sign_request('synthetic-authority-b-key','authority-b','POST',PATH,body)
        control=other.post(PATH,body,content_type='application/json',**{'HTTP_'+k.upper().replace('-','_'):v for k,v in headers.items()})
        self.assertEqual(control.json()['code'],0,control.content)
        self.assertEqual(control.json()['data']['groups'],[])

    def test_account_games_and_group_invalidation_use_user_notifications_only(self):
        from apps.rbac.models import Role
        url='http://127.0.0.1:4194/notify'
        with override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL=url), patch('apps.internal_chat.realtime._notify') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.worker.is_active=False
                self.worker.save(update_fields=['is_active'])
            notify.assert_called_once_with(connection.schema_name,[self.worker.pk],url,'synthetic')
            notify.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.staff.staff_profile.games.clear()
            notify.assert_called_once_with(connection.schema_name,[self.staff.pk],url,'synthetic')
            notify.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                self.staff.groups.clear()
            notify.assert_called_once_with(connection.schema_name,[self.staff.pk],url,'synthetic')

    def test_bridge_preserves_only_exact_trusted_order_authority_error_messages(self):
        client=Client(SERVER_NAME=self.domain.domain)
        self.assertEqual(client.post('/api/auth/login',{'username':self.staff.username,'password':'pw-p2-api'},content_type='application/json').json()['code'],0)
        cases=[(503,503,'暂时无法确认订单权限，请稍后重试','暂时无法确认订单权限，请稍后重试'),
               (200,503,'暂时无法确认订单权限，请稍后重试','内部沟通暂时不可用'),
               (503,503,'arbitrary unsafe message','内部沟通暂时不可用'),
               (403,403,'订单权限已撤销','订单权限已撤销'),
               (403,403,'订单群已归档','当前账号无权执行这项聊天操作')]
        for status,code,message,expected in cases:
            response=httpx.Response(status,json={'code':code,'msg':message,'data':None},request=httpx.Request('POST','http://127.0.0.1/v1/core'))
            with self.subTest(status=status,message=message),override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',return_value=response):
                result=client.get('/api/internal-chat/conversations/1000000000/messages/')
            self.assertEqual(result.json()['msg'],expected)

    def test_remote_member_without_local_account_cannot_be_a_local_pin_user(self):
        self.group.members.create(wire_key='b:999',local_user=None,name='合成远端发单人',publisher=True)
        row=self.group_data()['groups'][0]
        self.assertNotIn(None,row['room']['can_pin_user_ids'])
        self.assertEqual(row['room']['can_pin_user_ids'],[self.staff.pk])

    def test_known_group_checks_include_new_worker_not_in_callers_previous_roster(self):
        from apps.internal_chat.order_groups import assignment_changed
        self.performance.user=self.other
        self.performance.save(update_fields=['user'])
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            assignment_changed(self.order,self.other,former_worker=self.worker)
        payload={'checks':[{'conversation_id':self.group.conversation_id,'user_ids':[self.staff.pk,self.worker.pk]}]}
        row=self.group_data(payload)['groups'][0]
        self.assertIn(self.other.pk,[m['local_user_id'] for m in row['participants']])
        self.assertEqual(set(row['read_user_ids']),{self.staff.pk,self.other.pk})

    def test_role_and_group_delete_invalidate_users_without_querying_order_chat_history(self):
        import re
        from apps.rbac.models import Role
        def reject_history(execute,sql,params,many,context):
            self.assertFalse(re.search(r'internal_chat_(groupmember|ordergroup|message)',sql),sql)
            return execute(sql,params,many,context)
        url='http://127.0.0.1:4194/notify'
        role=Role.objects.get(group__user=self.staff)
        group=Role.objects.get(group__user=self.worker).group
        with override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL=url),patch('apps.internal_chat.realtime._notify') as notify,connection.execute_wrapper(reject_history):
            with self.captureOnCommitCallbacks(execute=True):
                role.delete()
            notify.assert_called_once_with(connection.schema_name,[self.staff.pk],url,'synthetic')
            notify.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                group.delete()
            self.assertTrue(notify.call_args_list)
            self.assertTrue(all(call.args==(connection.schema_name,[self.worker.pk],url,'synthetic') for call in notify.call_args_list))

    def test_completion_revision_uses_real_transition_timestamp_and_survives_plain_edit(self):
        from datetime import timedelta
        from apps.business.models import OrderStatus
        from apps.business.order_status import apply_transition
        from tests.p5_auth_helpers import grant_nodes
        self.performance.user=None
        self.performance.save(update_fields=['user'])
        self.order.status=OrderStatus.PENDING_REVIEW
        self.order.project_text='合成完成时间测试'
        self.order.save(update_fields=['status','project_text'])
        first=timezone.now()-timedelta(hours=2)
        second=first+timedelta(hours=1)
        options={'nodes':{'order.change_status','order.change_status_completed'},'notify_worker':False,
                 'sync_personal_orders':False,'sync_peer_orders':False,'suppress_outbound_sync':True}
        with patch('apps.business.order_status.timezone.now',return_value=first):
            apply_transition(self.order,OrderStatus.COMPLETED,**options)
        self.order.refresh_from_db()
        self.assertEqual(self.order.completed_at,first)
        initial=self.group_data()['groups'][0]
        self.assertEqual(initial['completion_revision'],first.isoformat())
        self.assertEqual(self.group_data()['groups'][0]['completion_revision'],initial['completion_revision'])
        grant_nodes(self.staff,'order.add_edit')
        client=Client(SERVER_NAME=self.domain.domain)
        self.assertEqual(client.post('/api/auth/login',{'username':self.staff.username,'password':'pw-p2-api'},content_type='application/json').json()['code'],0)
        response=client.patch('/api/business/orders/'+self.order.order_no+'/',{'remark':'合成普通编辑'},content_type='application/json')
        self.assertEqual(response.json()['code'],0,response.content)
        self.assertEqual(self.group_data()['groups'][0]['completion_revision'],initial['completion_revision'])
        apply_transition(self.order,OrderStatus.ISSUE,**options)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.completed_at)
        apply_transition(self.order,OrderStatus.PENDING_REVIEW,**options)
        with patch('apps.business.order_status.timezone.now',return_value=second):
            apply_transition(self.order,OrderStatus.COMPLETED,**options)
        changed=self.group_data()['groups'][0]
        self.assertTrue(changed['completed'])
        self.assertEqual(changed['completion_revision'],second.isoformat())
        self.assertNotEqual(changed['completion_revision'],initial['completion_revision'])

    def test_completion_revision_does_not_invent_a_timestamp_for_missing_legacy_fact(self):
        """合成历史夹具模拟没有完单时间的存量已完成行。"""
        from apps.business.models import OrderStatus
        self.assertIsNone(self.group_data()['groups'][0]['completion_revision'])
        self.order.status=OrderStatus.COMPLETED
        self.order.save(update_fields=['status'])
        row=self.group_data()['groups'][0]
        self.assertTrue(row['completed'])
        self.assertIsNone(row['completion_revision'])
        self.assertIsNone(self.group_data()['groups'][0]['completion_revision'])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.completed_at)

    def test_order_facts_without_source_group_are_read_only_and_authorized_by_order(self):
        self.group.delete()
        before=Conversation.objects.count()
        result=self.group_data({'checks':[{'order_no':self.order.order_no,'user_ids':[self.staff.pk,self.worker.pk,self.other.pk]}]})
        fact=result['orders'][0]
        self.assertEqual(fact['order_no'],self.order.order_no)
        self.assertIsNone(fact['conversation_id'])
        self.assertEqual(set(fact['read_user_ids']),{self.staff.pk,self.worker.pk})
        self.assertFalse(fact['deleted'])
        self.assertEqual(OrderGroup.objects.count(),0)
        self.assertEqual(Conversation.objects.count(),before)

    def test_outer_order_fact_preserves_room_send_and_pin_permissions_when_binding_disconnects(self):
        params={'checks':[{'order_no':self.order.order_no,'user_ids':[self.staff.pk]}]}
        allowed=self.group_data(params)['orders'][0]
        self.assertEqual(allowed['can_send'],allowed['room']['can_send'])
        self.assertTrue(allowed['can_send'])
        self.assertEqual(allowed['can_pin_user_ids'],[self.staff.pk])
        self.assertEqual(allowed['can_pin_user_ids'],allowed['room']['can_pin_user_ids'])
        self.group.binding_role='upstream'
        self.group.binding_id=None
        self.group.save(update_fields=['binding_role','binding_id'])
        denied=self.group_data(params)['orders'][0]
        self.assertFalse(denied['can_send'])
        self.assertEqual(denied['can_send'],denied['room']['can_send'])
        self.assertEqual(denied['can_pin_user_ids'],[])
        self.assertEqual(denied['can_pin_user_ids'],denied['room']['can_pin_user_ids'])

    def test_group_less_order_fact_reuses_publisher_pin_rule_without_creating_records(self):
        from apps.business.models import OrderStatus
        self.group.delete()
        count=Conversation.objects.count()
        params={'checks':[{'order_no':self.order.order_no,'user_ids':[self.staff.pk]}]}
        fact=self.group_data(params)['orders'][0]
        self.assertTrue(fact['can_send'])
        self.assertEqual(fact['can_pin_user_ids'],[self.staff.pk])
        self.order.status=OrderStatus.REFUNDED
        self.order.save(update_fields=['status'])
        refunded=self.group_data(params)['orders'][0]
        self.assertTrue(refunded['can_send'])
        self.assertEqual(refunded['can_pin_user_ids'],[])
        self.assertEqual((OrderGroup.objects.count(),Conversation.objects.count()),(0,count))
