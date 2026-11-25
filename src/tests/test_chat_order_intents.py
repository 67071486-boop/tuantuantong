import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.internal_chat.models import Conversation, GroupMember, Message, OrderChatIntent, OrderGroup
from chat_service.protocol import sign_request
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_chat_service_bridge import ServiceClient

BASE = '/api/internal-chat/'
INTENTS = BASE + 'service/order-intents/'
AUTHORITY = BASE + 'service/order-authorizations/'


class ChatOrderIntentTests(TenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.enterContext(override_settings(DEBUG=True,DEPOSIT_ENFORCED=False,TT_CHAT_ORDER_GROUPS_ENABLED=True,
                                            TT_CHAT_CORE_BACKEND='legacy'))
        self.game = Game.objects.create(name='待办合成游戏')
        self.admin = make_logged_in_client(domain=self.domain.domain,username='intent-admin',is_superuser=True)
        self.actor = get_user_model().objects.get(username='intent-admin')
        profile = self.actor.staff_profile
        profile.commission_mode = CommissionMode.PROFIT_PERCENT
        profile.save(update_fields=['commission_mode'])
        self.workers = []
        for index in range(2):
            client = make_logged_in_client(domain=self.domain.domain,username=f'intent-worker-{index}',groups=('代练员',),
                nodes=('my_orders.send_to_my_system','grab_hall.take_order','order.submit_work'))
            self.workers.append((client,get_user_model().objects.get(username=f'intent-worker-{index}')))
        self.folder = self.enterContext(TemporaryDirectory())
        self.config = Path(self.folder) / 'service.json'
        self.valid_config = json.dumps({'service_url':'http://127.0.0.1:9050','tenants':{
            connection.schema_name:{'key_id':'intent-a','secret':'synthetic-intent-a-key'}}})
        self.config.write_text(self.valid_config)
        self.config.chmod(0o600)
        self.enterContext(override_settings(TT_CHAT_SERVICE_CONFIG_FILE=str(self.config)))

    def data(self,response):
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['code'],0,response.content)
        return response.json()['data']

    def signed(self,path,payload):
        raw=json.dumps(payload,separators=(',',':')).encode()
        headers=sign_request('synthetic-intent-a-key','intent-a','POST',path,raw)
        return ServiceClient(SERVER_NAME=self.domain.domain).post(path,raw,content_type='application/json',
            **{'HTTP_'+key.upper().replace('-','_'):value for key,value in headers.items()})

    def create_order(self):
        response=self.admin.post('/api/business/orders/',{'game':self.game.pk,'project_text':'合成业务故障对照',
            'total_price':'100.00','hall_price':'50.00'},content_type='application/json')
        return Order.objects.get(order_no=self.data(response)['order_no'])

    def test_new_order_intent_is_durable_sticky_and_stale_ack_preserves_new_changes(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            order=self.create_order()
            row=OrderChatIntent.objects.get(order_no=order.order_no)
            self.assertTrue(row.ensure_group)
            first=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'][0]
            self.assertEqual(first['order']['order_no'],order.order_no)
            self.assertIsNone(first['order']['conversation_id'])
            order.status=OrderStatus.ISSUE
            order.save(update_fields=['status'])
            row.refresh_from_db()
            self.assertGreater(row.revision,first['revision'])
            self.assertEqual(self.data(self.signed(INTENTS,{'operation':'ack','intents':[{'id':row.pk,'revision':first['revision']}]}))['acked'],0)
            current=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'][0]
            self.assertTrue(current['ensure_group'])
            self.assertEqual(current['revision'],row.revision)
            self.assertEqual(self.data(self.signed(INTENTS,{'operation':'ack','intents':[{'id':row.pk,'revision':row.revision}]}))['acked'],1)
            self.assertEqual(self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'],[])
        self.assertEqual((OrderGroup.objects.count(),Conversation.objects.count(),Message.objects.count()),(0,0,0))

    def test_old_group_less_order_update_does_not_become_new_group_intent(self):
        order=self.create_order()
        self.assertFalse(OrderChatIntent.objects.exists())
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            root='/api/business/orders/'+order.order_no+'/'
            self.data(self.admin.post(root+'assign/',{'user_id':self.workers[0][1].pk},content_type='application/json'))
            row=OrderChatIntent.objects.get(order_no=order.order_no)
            self.assertFalse(row.ensure_group)
            discovered=self.data(self.signed(AUTHORITY,{'checks':[],'viewer_id':self.workers[0][1].pk,'discover':True}))
            self.assertEqual(discovered['orders'],[])
            self.assertEqual(discovered['groups'],[])
        self.assertFalse(OrderGroup.objects.exists())

    def test_broken_chat_configuration_cannot_block_real_create_publish_claim_reassign_or_settlement(self):
        self.config.write_text('{invalid chat config')
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=AssertionError('business must not call chat transport')):
            order=self.create_order()
            root='/api/business/orders/'+order.order_no+'/'
            self.data(self.admin.post(root+'throw-to-hall/',{},content_type='application/json'))
            self.data(self.workers[0][0].post('/api/business/hall/'+order.order_no+'/claim/',{},content_type='application/json'))
            self.data(self.workers[0][0].get('/api/business/my-orders/'+order.order_no+'/detail/'))
            self.data(self.admin.post(root+'assign/',{'user_id':self.workers[1][1].pk},content_type='application/json'))
            lost=self.workers[0][0].get('/api/business/my-orders/'+order.order_no+'/detail/')
            self.assertNotEqual(lost.json()['code'],0)
            self.data(self.workers[1][0].get('/api/business/my-orders/'+order.order_no+'/detail/'))
            self.data(self.admin.post(root+'transition/',{'target':OrderStatus.PENDING_REVIEW},content_type='application/json'))
            self.data(self.admin.post(root+'transition/',{'target':OrderStatus.COMPLETED},content_type='application/json'))
            from apps.business.settlement import settle_performances
            performance=Performance.objects.get(order=order,emp_type=EmpType.MAIN)
            row=OrderChatIntent.objects.get(order_no=order.order_no)
            revision=row.revision
            settle_performances([performance.pk],actor=self.actor.username,user=self.actor)
            performance.refresh_from_db()
            self.assertIsNotNone(performance.settled_at)
            row.refresh_from_db()
            self.assertEqual(row.revision,revision)
        self.config.write_text(self.valid_config)
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            jobs=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents']
            self.assertEqual(len(jobs),1)
            fact=jobs[0]['order']
            self.assertTrue(fact['completed'])
            self.assertEqual(fact['worker_id'],self.workers[1][1].pk)
            self.assertIn(self.workers[1][1].pk,fact['default_join_user_ids'])
        self.assertEqual((OrderGroup.objects.count(),Conversation.objects.count(),GroupMember.objects.count(),Message.objects.count()),(0,0,0,0))

    def test_actual_soft_delete_queues_deleted_fact_without_granting_history_or_reopen(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            order=self.create_order()
            self.data(self.admin.post('/api/business/orders/'+order.order_no+'/delete/',{'isDelEmp':'Y'},content_type='application/json'))
            job=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'][0]
            fact=job['order']
            order=Order.all_objects.get(pk=order.pk)
            self.assertTrue(fact['deleted'])
            self.assertEqual(fact['deletion_revision'],order.deleted_at.isoformat())
            self.assertEqual(fact['read_user_ids'],[])
            self.assertEqual(fact['reopen_user_ids'],[])

    def test_rolled_back_order_does_not_leave_an_intent(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'),transaction.atomic():
            self.create_order()
            self.assertTrue(OrderChatIntent.objects.exists())
            transaction.set_rollback(True)
        self.assertFalse(OrderChatIntent.objects.exists())

    def test_chat_maintenance_preserves_business_and_signed_worker_controls(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_MAINTENANCE=True):
            order=self.create_order()
            for path in ('conversations/','unread/','orders/'+order.order_no+'/'):
                result=self.admin.get(BASE+path)
                self.assertEqual((result.status_code,result.json()['msg']),(503,'内部沟通暂时不可用'))
            self.assertTrue(self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'])
            self.assertTrue(self.data(self.signed(AUTHORITY,{'checks':[{'order_no':order.order_no,'user_ids':[self.actor.pk]}]}))['orders'])
            path=BASE+'service/export/'
            headers=sign_request('synthetic-intent-a-key','intent-a','GET',path,b'')
            exported=ServiceClient(SERVER_NAME=self.domain.domain).get(path,**{'HTTP_'+k.upper().replace('-','_'):v for k,v in headers.items()})
            self.assertTrue(self.data(exported)['chat_paused'])

    def test_private_intent_endpoint_rejects_unsigned_requests_and_extra_tenant_input(self):
        self.assertEqual(self.admin.post(INTENTS,{'operation':'pull'},content_type='application/json').status_code,403)
        result=self.signed(INTENTS,{'operation':'pull','tenant':'another'})
        self.assertNotEqual(result.json()['code'],0)

    def test_delete_then_restore_while_chat_is_unavailable_keeps_the_real_deletion_fact_after_ack(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=AssertionError('business must not call chat')):
            order=self.create_order()
            root='/api/business/orders/'+order.order_no+'/'
            self.data(self.admin.post(root+'delete/',{'isDelEmp':'N'},content_type='application/json'))
            deleted=Order.all_objects.get(pk=order.pk).deleted_at
            self.assertIsNotNone(deleted)
            self.data(self.admin.post(root+'restore/',{},content_type='application/json'))
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            job=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'][0]
            fact=job['order']
            self.assertFalse(fact['deleted'])
            self.assertIsNone(fact['deletion_revision'])
            self.assertEqual(fact['last_deletion_revision'],deleted.isoformat())
            self.assertEqual(self.data(self.signed(INTENTS,{'operation':'ack','intents':[{'id':job['id'],'revision':job['revision']}]}))['acked'],1)
            replay=self.data(self.signed(AUTHORITY,{'checks':[{'order_no':order.order_no,'user_ids':[self.actor.pk]}]}))['orders'][0]
            self.assertEqual(replay['last_deletion_revision'],fact['last_deletion_revision'])
            self.assertFalse(replay['deleted'])

    def test_complete_then_leave_while_chat_is_unavailable_keeps_the_real_completion_fact(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=AssertionError('business must not call chat')):
            order=self.create_order()
            root='/api/business/orders/'+order.order_no+'/'
            self.data(self.admin.post(root+'transition/',{'target':OrderStatus.PENDING_REVIEW},content_type='application/json'))
            self.data(self.admin.post(root+'transition/',{'target':OrderStatus.COMPLETED},content_type='application/json'))
            completed=Order.objects.get(pk=order.pk).completed_at
            self.data(self.admin.post(root+'transition/',{'target':OrderStatus.ISSUE},content_type='application/json'))
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            job=self.data(self.signed(INTENTS,{'operation':'pull'}))['intents'][0]
            fact=job['order']
            self.assertFalse(fact['completed'])
            self.assertIsNone(fact['completion_revision'])
            self.assertEqual(fact['last_completion_revision'],completed.isoformat())
            self.data(self.signed(INTENTS,{'operation':'ack','intents':[{'id':job['id'],'revision':job['revision']}]}))
            replay=self.data(self.signed(AUTHORITY,{'checks':[{'order_no':order.order_no,'user_ids':[self.actor.pk]}]}))['orders'][0]
            self.assertEqual(replay['last_completion_revision'],fact['last_completion_revision'])

    def test_known_high_core_conversation_is_authorized_by_order_without_source_mapping(self):
        order=self.create_order()
        result=self.data(self.signed(AUTHORITY,{'checks':[{'conversation_id':1000000017,'order_no':order.order_no,'user_ids':[self.actor.pk]}]}))
        self.assertEqual(result['groups'][0]['conversation_id'],1000000017)
        self.assertEqual(result['groups'][0]['room']['id'],1000000017)
        self.assertIn(self.actor.pk,result['groups'][0]['read_user_ids'])
        self.assertFalse(OrderGroup.objects.exists())

    def test_order_preview_and_empty_history_do_not_create_source_groups(self):
        import httpx
        order=self.create_order()
        calls=[]
        def remote(url,**kwargs):
            body=json.loads(kwargs['content']);calls.append(body)
            return httpx.Response(200,json={'code':0,'data':{'id':None,'last_message':None,'has_conversation':False,
                'order_no':order.order_no,'can_send':True,'can_join':False,'archived':False}},request=httpx.Request('POST',url))
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=remote):
            root=BASE+'orders/'+order.order_no+'/'
            self.assertFalse(self.data(self.admin.get(root))['has_conversation'])
            self.assertEqual(self.data(self.admin.get(root+'messages/')),{'messages':[],'has_more':False,'has_newer':False})
        self.assertEqual([c['operation'] for c in calls],['order_preview','order_preview'])
        self.assertEqual((OrderGroup.objects.count(),Conversation.objects.count(),OrderChatIntent.objects.count()),(0,0,0))

    def test_first_send_text_and_image_use_one_authenticated_order_operation_with_same_retry_key(self):
        import base64,httpx
        from io import BytesIO
        from PIL import Image
        order=self.create_order();calls=[]
        raw=BytesIO();Image.new('RGB',(2,3),'blue').save(raw,format='PNG')
        encoded=base64.b64encode(raw.getvalue()).decode()
        summary={'id':1000000031,'has_conversation':True,'last_message':None}
        message={'id':1000000032,'seq':1,'conversation_id':summary['id'],'text':'合成首发'}
        result={'conversation':summary,'messages':[message]}
        def remote(url,**kwargs):
            calls.append(json.loads(kwargs['content']))
            if len(calls)==1:
                raise httpx.ConnectError('synthetic chat outage')
            return httpx.Response(200,json={'code':0,'data':result},request=httpx.Request('POST',url))
        body={'text':'合成首发','client_id':'same-first-send','viewer_id':self.workers[1][1].pk,'image':{'mime':'image/png','data':encoded}}
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=remote):
            path=BASE+'orders/'+order.order_no+'/messages/'
            failed=self.admin.post(path,body,content_type='application/json')
            self.assertEqual((failed.status_code,failed.json()['msg']),(503,'内部沟通暂时不可用'))
            self.assertEqual(self.data(self.admin.post(path,body,content_type='application/json')),result)
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(calls[0]['operation'],'order_send')
        self.assertEqual(calls[0]['viewer_id'],self.actor.pk)
        self.assertEqual(calls[0]['payload']['viewer']['id'],self.actor.pk)
        self.assertEqual(calls[0]['payload']['message']['image']['data'],encoded)
        self.assertFalse(OrderGroup.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_app_chat_grant_can_first_send_for_its_current_order_without_source_group(self):
        import httpx
        from datetime import timedelta
        from django.test import Client
        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token
        order=self.create_order()
        worker=self.workers[0][1]
        with override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify'):
            self.data(self.admin.post('/api/business/orders/'+order.order_no+'/assign/',{'user_id':worker.pk},content_type='application/json'))
            raw='synthetic-order-first-app'
            AppToken.objects.create(user=worker,key_hash=hash_token(raw),expires_at=timezone.now()+timedelta(minutes=5))
            app=Client(SERVER_NAME=self.domain.domain,HTTP_AUTHORIZATION=f'Bearer {raw}')
            grant=self.data(app.post(BASE+'app/access/'))['grant']
            web=Client(SERVER_NAME=self.domain.domain,HTTP_AUTHORIZATION=f'Chat {grant}')
            response=httpx.Response(200,json={'code':0,'data':{'conversation':{'id':1000000041},'messages':[{'id':1000000042,'seq':1}]}},request=httpx.Request('POST','http://127.0.0.1/v1/core'))
            with patch('httpx.post',return_value=response) as remote:
                result=self.data(web.post(BASE+'orders/'+order.order_no+'/messages/',{'text':'合成App首发','client_id':'app-first'},content_type='application/json'))
            self.assertEqual(result['conversation']['id'],1000000041)
            self.assertEqual(json.loads(remote.call_args.kwargs['content'])['viewer_id'],worker.pk)
            with override_settings(TT_CHAT_MAINTENANCE=True):
                for path,method in (('unread/','get'),('app/me/','get'),('app/refresh/','post')):
                    paused=getattr(web,method)(BASE+path)
                    self.assertEqual((paused.status_code,paused.json()['msg']),(503,'内部沟通暂时不可用'))

    def test_existing_group_history_image_and_order_mapping_survive_offline_assignment(self):
        import base64
        from io import BytesIO
        from PIL import Image
        order=self.create_order()
        self.data(self.admin.post('/api/business/orders/'+order.order_no+'/throw-to-hall/',{},content_type='application/json'))
        group=OrderGroup.objects.get(order=order)
        image=BytesIO();Image.new('RGB',(2,3),'blue').save(image,format='PNG')
        self.data(self.admin.post(BASE+f'conversations/{group.conversation_id}/messages/',{'text':'合成原群历史',
            'client_id':'legacy-stable','image':{'mime':'image/png','data':base64.b64encode(image.getvalue()).decode()}},content_type='application/json'))
        message=Message.objects.get(conversation_id=group.conversation_id)
        snapshot=(group.pk,group.conversation_id,group.order_id,message.pk,message.text,bytes(message.image_data),list(group.members.values()))
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=AssertionError('business must not depend on chat')):
            self.data(self.admin.post('/api/business/orders/'+order.order_no+'/assign/',{'user_id':self.workers[0][1].pk},content_type='application/json'))
            fact=self.data(self.signed(AUTHORITY,{'checks':[{'order_no':order.order_no,'user_ids':[self.actor.pk]}]}))['orders'][0]
        group.refresh_from_db();message.refresh_from_db()
        self.assertEqual(snapshot,(group.pk,group.conversation_id,group.order_id,message.pk,message.text,bytes(message.image_data),list(group.members.values())))
        self.assertEqual(fact['conversation_id'],group.conversation_id)
        self.assertTrue(fact['has_legacy_messages'])
        self.assertEqual(self.admin.post(BASE+'orders/'+order.order_no+'/messages/',{'text':'legacy unsupported','client_id':'unsupported'},content_type='application/json').status_code,405)

    def test_invalid_global_chat_mode_cannot_send_business_writes_into_the_legacy_chat_path(self):
        config=json.loads(self.valid_config)
        config['tenants'][connection.schema_name]['core_backend']='legacy'
        self.config.write_text(json.dumps(config))
        with override_settings(TT_CHAT_CORE_BACKEND='invalid-config'),patch('httpx.post',side_effect=AssertionError('no chat dependency')):
            order=self.create_order()
            self.data(self.admin.post('/api/business/orders/'+order.order_no+'/throw-to-hall/',{},content_type='application/json'))
        self.assertTrue(OrderChatIntent.objects.filter(order_no=order.order_no).exists())
        self.assertFalse(OrderGroup.objects.exists())
