from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.wecom.external_pay import match_payment, parse_bill
from apps.wecom.models import ExternalPayBill, ExternalPayState, WecomAppConfig, WecomNotification
from apps.wecom.notifications import prepare_order_status, _recover_order_journal, run_due


def queue_order_status(order, previous_status, *, event_at=None, recover=True):
    with transaction.atomic():
        if prepare_order_status(order, previous_status, event_at=event_at):
            order.save(update_fields=["status", "wecom_notice_journal", "wecom_notice_pending"])
    if recover:
        _recover_order_journal()


class NotificationTests(TenantTestCase):
    def setUp(self):
        self.config = WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id='corp', agent_id=1, secret='secret', payment_mismatch_enabled=True, order_status_enabled=True)
        self.state = ExternalPayState.objects.create(identity_revision=0, corp_id='corp')
        self.game = Game.objects.create(name='合成游戏')
        self.order = Order.objects.create(order_no='20261114000000000001', game=self.game, source_order_no='synthetic-payment', total_price=Decimal('9.00'), sales_agent='小李')

    def test_payment_mismatch_notifies_once_and_preserves_payee(self):
        now = timezone.now()
        data = parse_bill({'bill_type': 0, 'transaction_id':'synthetic-payment', 'mch_id':'merchant', 'out_trade_no':'out', 'total_fee':1000, 'total_refund_fee':0, 'pay_time':int((now-timedelta(hours=1)).timestamp()), 'trade_state':1, 'payee_userid':'payee-a'})
        bill = ExternalPayBill.objects.create(state=self.state, **data)
        match_payment(bill.pk, self.config, now)
        match_payment(bill.pk, self.config, now)
        row = WecomNotification.objects.get()
        self.assertEqual(row.recipients, {'payee-a':'pending'})
        self.assertIn('10.00', row.detail)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.payment_status, 'SETTLED')

    def test_old_mismatch_not_replayed_on_enable(self):
        self.config.payment_mismatch_enabled = False; self.config.save(update_fields=['payment_mismatch_enabled'])
        now = timezone.now()
        data = parse_bill({'bill_type':0, 'transaction_id':'synthetic-payment', 'mch_id':'merchant', 'out_trade_no':'out', 'total_fee':1000, 'total_refund_fee':0, 'pay_time':int(now.timestamp()), 'trade_state':1, 'payee_userid':'payee-a'})
        bill = ExternalPayBill.objects.create(state=self.state, **data)
        match_payment(bill.pk,self.config,now)
        self.config.payment_mismatch_enabled=True; self.config.save(update_fields=['payment_mismatch_enabled'])
        match_payment(bill.pk,self.config,now)
        self.assertFalse(WecomNotification.objects.exists())

    def test_one_status_event_and_multiple_recipients_individually(self):
        self.order.status='ISSUE'; queue_order_status(self.order, 'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'客服小李'},{'userid':'b','name':'小李'}, {'userid':'a','name':'小李'}]
            client.message.send_text.side_effect=[{'errcode':0},{'errcode':0,'invaliduser':'b'}]
            run_due()
        row=WecomNotification.objects.get()
        self.assertEqual(row.recipients, {'a':'sent','b':'failed'})
        self.assertEqual(row.status,'failed')
        self.assertEqual(client.message.send_text.call_count,2)

    def test_absent_customer_is_failed_without_sending(self):
        self.order.status='PAUSED'; queue_order_status(self.order,'ISSUE')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            factory.return_value.user.list.return_value=[]
            run_due()
            factory.return_value.message.send_text.assert_not_called()
        self.assertEqual(WecomNotification.objects.get().reason, '未找到匹配的企微成员')

    def test_enqueue_rolls_back_with_order(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.order.status='ISSUE'; queue_order_status(self.order,'IN_PROGRESS')
                raise ValueError()
        self.assertFalse(WecomNotification.objects.exists())

    def test_status_transition_enqueues_only_on_entry(self):
        from apps.business.order_status import apply_transition
        self.order.status='IN_PROGRESS'; self.order.save(update_fields=['status'])
        with transaction.atomic():
            apply_transition(self.order, 'ISSUE', nodes=frozenset({'order.change_status'}))
        _recover_order_journal()
        self.assertEqual(WecomNotification.objects.filter(kind='order_status').count(),1)
        with transaction.atomic():
            apply_transition(self.order, 'IN_PROGRESS', nodes=frozenset({'order.change_status'}))
        with transaction.atomic():
            apply_transition(self.order, 'PAUSED', nodes=frozenset({'order.change_status'}))
        _recover_order_journal()
        self.assertEqual(WecomNotification.objects.filter(kind='order_status').count(),2)

    def test_uncertain_send_is_not_retried(self):
        self.order.status='ISSUE'; queue_order_status(self.order,'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'小李'}]
            client.message.send_text.side_effect=TimeoutError()
            run_due(); run_due()
        self.assertEqual(client.message.send_text.call_count, 1)
        row=WecomNotification.objects.get()
        self.assertEqual(row.recipients, {'a':'unknown'})
        self.assertEqual(row.status,'failed')

    def test_missing_payee_is_visible_without_sending(self):
        now = timezone.now()
        data = parse_bill({'bill_type':0, 'transaction_id':'synthetic-payment', 'mch_id':'merchant', 'out_trade_no':'out', 'total_fee':1000, 'total_refund_fee':0, 'pay_time':int(now.timestamp()), 'trade_state':1})
        bill=ExternalPayBill.objects.create(state=self.state, **data)
        match_payment(bill.pk,self.config,now)
        row=WecomNotification.objects.get()
        self.assertEqual(row.status,'pending')
        self.assertIn('查询企微收款人',row.reason)

    def test_disabled_existing_features_do_not_consume_pending_work(self):
        from apps.wecom.external_pay import run_due as run_payments
        from apps.wecom.group_sync import queue_event, run_due as run_groups
        from apps.wecom.group_qr import get_or_create_qr
        from apps.wecom.models import WecomGroupChat
        self.config.external_pay_enabled = False
        self.config.group_sync_enabled = False
        self.config.group_qr_enabled = False
        self.config.save(update_fields=['external_pay_enabled','group_sync_enabled','group_qr_enabled'])
        queue_event({'MsgType':'event','Event':'change_external_chat','ChatId':'synthetic-chat','ChangeType':'update','CreateTime':str(int(timezone.now().timestamp()))})
        group=WecomGroupChat.objects.get(chat_id='synthetic-chat')
        self.assertTrue(group.pending)
        with patch('apps.wecom.group_sync.sdk.get_contacts_client') as contacts, patch('apps.wecom.external_pay.sdk.get_client') as payment:
            self.assertEqual(run_groups(),0)
            run_payments()
            contacts.assert_not_called(); payment.assert_not_called()
        group.refresh_from_db()
        self.assertTrue(group.pending)
        self.assertEqual(get_or_create_qr(group)['status'], 'unavailable')

    def test_switch_defaults_preserve_old_functions(self):
        fresh=WecomAppConfig()
        self.assertTrue(fresh.group_sync_enabled and fresh.group_qr_enabled and fresh.external_pay_enabled)
        self.assertFalse(fresh.order_status_enabled or fresh.payment_mismatch_enabled)

    def test_notice_api_tenant_isolation(self):
        from django_tenants.utils import tenant_context
        from apps.platform.provisioning import provision_tenant
        from tests.p5_auth_helpers import make_logged_in_client
        own = make_logged_in_client(username='notice-admin-a', is_superuser=True)
        other, _ = provision_tenant(schema_name='notice_b', name='测试乙', domain='notice-b.test.com')
        with tenant_context(other):
            WecomAppConfig.objects.create(tenant_schema=other.schema_name, enabled=True, corp_id='corp', agent_id=1, secret='secret')
            WecomNotification.objects.create(identity_revision=0, corp_id='corp', agent_id=1, kind='order_status', order_no='other-private-order', detail='other-private')
        self.order.status='ISSUE'; queue_order_status(self.order,'IN_PROGRESS')
        data=own.get('/api/wecom/notification/records/').json()['data']['items']
        self.assertEqual([row['order_no'] for row in data],[self.order.order_no])
        try:
            response=own.get('/api/wecom/notification/records/', HTTP_HOST='notice-b.test.com')
            if response.status_code==200:
                self.assertNotEqual(response.json()['code'],0)
            else:
                self.assertIn(response.status_code,(401,403,404))
        finally:
            connection.set_tenant(self.tenant)

    def test_switch_api_requires_node_and_persists_all_flags(self):
        from tests.p5_auth_helpers import make_logged_in_client
        denied=make_logged_in_client(username='no-wecom-node')
        self.assertNotEqual(denied.get('/api/wecom/notification/settings/').json()['code'],0)
        permitted=make_logged_in_client(username='wecom-admin',is_superuser=True)
        response=permitted.put('/api/wecom/notification/settings/', data={'group_sync_enabled':False,'group_qr_enabled':False,'external_pay_enabled':False,'payment_mismatch_enabled':False,'order_status_enabled':False}, content_type='application/json')
        self.assertEqual(response.json()['code'],0)
        self.config.refresh_from_db()
        self.assertFalse(self.config.group_sync_enabled or self.config.group_qr_enabled or self.config.external_pay_enabled or self.config.payment_mismatch_enabled or self.config.order_status_enabled)

    def test_reenable_status_notification_records_new_boundary(self):
        from tests.p5_auth_helpers import make_logged_in_client
        client=make_logged_in_client(username='switch-admin',is_superuser=True)
        self.config.order_status_enabled=False
        self.config.save(update_fields=['order_status_enabled'])
        response=client.put('/api/wecom/notification/settings/', data={'order_status_enabled':True}, content_type='application/json')
        self.assertEqual(response.json()['code'],0)
        self.config.refresh_from_db()
        self.assertTrue(self.config.order_status_enabled)
        self.assertIsNotNone(self.config.order_status_enabled_at)
        self.order.status='ISSUE'
        queue_order_status(self.order, 'IN_PROGRESS', event_at=self.config.order_status_enabled_at - timedelta(seconds=1))
        self.assertFalse(WecomNotification.objects.exists())
        queue_order_status(self.order, 'IN_PROGRESS', event_at=self.config.order_status_enabled_at + timedelta(seconds=1))
        self.assertEqual(WecomNotification.objects.count(),1)

    def test_notification_enqueue_failure_does_not_block_status_change(self):
        from apps.business.order_status import apply_transition
        self.order.status='IN_PROGRESS'; self.order.save(update_fields=['status'])
        with patch('apps.wecom.notifications.WecomNotification.objects.create', side_effect=RuntimeError('synthetic failure')):
            with transaction.atomic():
                apply_transition(self.order,'ISSUE',nodes=frozenset({'order.change_status'}))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status,'ISSUE')
        self.assertFalse(WecomNotification.objects.exists())
        self.assertTrue(self.order.wecom_notice_pending)
        self.assertEqual(len(self.order.wecom_notice_journal),1)

    def test_status_notice_fallback_preserves_each_sales_snapshot_and_recovers(self):
        from apps.business.order_status import apply_transition
        self.order.status='IN_PROGRESS'; self.order.save(update_fields=['status'])
        with patch('apps.wecom.notifications.WecomNotification.objects.create', side_effect=RuntimeError('synthetic failure')):
            with transaction.atomic():
                apply_transition(self.order,'ISSUE',nodes=frozenset({'order.change_status'}))
            self.order.sales_agent='小王';self.order.save(update_fields=['sales_agent'])
            with transaction.atomic():
                apply_transition(self.order,'PAUSED',nodes=frozenset({'order.change_status'}))
        self.order.refresh_from_db()
        self.assertEqual([x['lookup_name'] for x in self.order.wecom_notice_journal],['小李','小王'])
        with patch('apps.wecom.notifications.WecomNotification.objects.get_or_create',side_effect=RuntimeError('synthetic failure')):
            run_due()
        self.order.refresh_from_db();self.assertEqual(len(self.order.wecom_notice_journal),2)
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'小李'},{'userid':'b','name':'小王'}]
            client.message.send_text.return_value={'errcode':0}
            run_due();run_due()
        self.order.refresh_from_db()
        self.assertFalse(self.order.wecom_notice_pending)
        self.assertEqual(self.order.wecom_notice_journal,[])
        self.assertEqual(WecomNotification.objects.count(),2)
        self.assertEqual(client.message.send_text.call_count,2)

    def test_payment_enqueue_failure_is_visible_and_retried(self):
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        with patch('apps.wecom.notifications.WecomNotification.objects.create',side_effect=RuntimeError('synthetic failure')):
            match_payment(bill.pk,self.config,now)
        bill.refresh_from_db();self.assertTrue(bill.mismatch_notification_pending)
        match_payment(bill.pk,self.config,now+timedelta(hours=1))
        bill.refresh_from_db();self.assertFalse(bill.mismatch_notification_pending)
        self.assertEqual(WecomNotification.objects.count(),1)

    def test_temporary_error_does_not_repeat_same_mismatch(self):
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        match_payment(bill.pk,self.config,now)
        bill.refresh_from_db()
        bill.issue='锁忙，稍后重试'; bill.save(update_fields=['issue'])
        match_payment(bill.pk,self.config,now)
        self.assertEqual(WecomNotification.objects.count(),1)
        self.order.total_price=Decimal('10.00'); self.order.save(update_fields=['total_price'])
        match_payment(bill.pk,self.config,now)
        bill.refresh_from_db(); self.assertFalse(bill.mismatch_active)
        self.order.total_price=Decimal('9.00'); self.order.save(update_fields=['total_price'])
        match_payment(bill.pk,self.config,now)
        self.assertEqual(WecomNotification.objects.count(),2)

    def test_interrupted_send_becomes_unknown_without_retry(self):
        WecomNotification.objects.create(identity_revision=0,corp_id='corp',agent_id=1,kind='order_status',order_no=self.order.order_no,detail='合成',resolved=True,recipients={'staff':'sending'})
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            run_due()
            factory.return_value.message.send_text.assert_not_called()
        row=WecomNotification.objects.get()
        self.assertEqual(row.status,'failed')
        self.assertEqual(row.recipients,{'staff':'unknown'})

    def test_config_incomplete_records_and_sends_after_recovery(self):
        self.config.secret=''; self.config.save(update_fields=['secret'])
        self.order.status='ISSUE'; queue_order_status(self.order,'IN_PROGRESS')
        row=WecomNotification.objects.get()
        self.assertEqual(row.status,'pending')
        self.assertIn('配置不完整',row.reason)
        self.assertEqual(run_due(),0)
        self.config.secret='new-secret'; self.config.identity_revision=1; self.config.save(update_fields=['secret','identity_revision'])
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'小李'}]
            client.message.send_text.return_value={'errcode':0}
            run_due()
        row.refresh_from_db()
        self.assertEqual(row.status,'sent')

    def test_mismatch_cycle_clears_after_order_disappears(self):
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        match_payment(bill.pk,self.config,now)
        self.order.source_order_no='different'; self.order.save(update_fields=['source_order_no'])
        match_payment(bill.pk,self.config,now)
        bill.refresh_from_db(); self.assertFalse(bill.mismatch_active)
        self.order.source_order_no='synthetic-payment'; self.order.save(update_fields=['source_order_no'])
        match_payment(bill.pk,self.config,now)
        self.assertEqual(WecomNotification.objects.count(),2)

    def test_migration_conservatively_seeds_old_masked_mismatches(self):
        import importlib
        from django.apps import apps
        from types import SimpleNamespace
        migration=importlib.import_module('apps.wecom.migrations.0006_wecomnotification_externalpaybill_payee_userid_and_more')
        now=timezone.now()
        def make(txn,fee,issue):
            row={'bill_type':0,'transaction_id':txn,'mch_id':'merchant','out_trade_no':txn,'total_fee':fee,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1}
            return ExternalPayBill.objects.create(state=self.state,**parse_bill(row),issue=issue)
        masked=make('synthetic-payment',1000,'稍后重试')
        matched=make('matched-history',1000,'')
        unknown=make('unknown-history',1000,'网络异常')
        unlinked=make('unlinked-history',1000,'尚未匹配到订单')
        Order.objects.create(order_no='20261114000000000002', game=self.game,source_order_no='matched-history',total_price=Decimal('10.00'))
        migration.preserve_existing_mismatch(apps,SimpleNamespace(connection=connection))
        for row,expected in ((masked,True),(matched,False),(unknown,True),(unlinked,False)):
            row.refresh_from_db(); self.assertEqual(row.mismatch_active,expected)

    def test_directory_failure_retries_then_sends(self):
        self.order.status='ISSUE'; queue_order_status(self.order,'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.side_effect=[TimeoutError(), [{'userid':'a','name':'小李'}]]
            client.message.send_text.return_value={'errcode':0}
            run_due()
            row=WecomNotification.objects.get()
            self.assertEqual(row.status,'pending')
            self.assertEqual(row.attempts,1)
            client.message.send_text.assert_not_called()
            row.retry_at=timezone.now()-timedelta(seconds=1);row.save(update_fields=['retry_at'])
            run_due()
        row.refresh_from_db()
        self.assertEqual(row.status,'sent')
        self.assertEqual(client.message.send_text.call_count,1)

    def test_old_payment_without_payee_is_paginated_and_identity_checked(self):
        now=timezone.now()
        raw={'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1}
        bill=ExternalPayBill.objects.create(state=self.state,**parse_bill(raw))
        match_payment(bill.pk,self.config,now)
        actual=parse_bill({**raw,'payee_userid':'payee-a'})
        other=parse_bill({**raw,'transaction_id':'other','payee_userid':'wrong'})
        with patch('apps.wecom.external_pay._get_page',side_effect=[([other],'next'),([actual],'')]), patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.message.send_text.return_value={'errcode':0}
            run_due()
        row=WecomNotification.objects.get(); bill.refresh_from_db()
        self.assertEqual(bill.payee_userid,'payee-a')
        self.assertEqual(row.recipients,{'payee-a':'sent'})
        client.message.send_text.assert_called_once_with(1,['payee-a'],row.detail)

    def test_old_payment_identity_conflict_never_sends(self):
        now=timezone.now()
        raw={'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1}
        bill=ExternalPayBill.objects.create(state=self.state,**parse_bill(raw))
        match_payment(bill.pk,self.config,now)
        wrong=parse_bill({**raw,'total_fee':1100,'payee_userid':'wrong'})
        with patch('apps.wecom.external_pay._get_page',return_value=([wrong],'')), patch('apps.wecom.notifications.sdk.get_client') as factory:
            run_due()
            factory.return_value.message.send_text.assert_not_called()
        row=WecomNotification.objects.get()
        self.assertEqual(row.status,'failed')
        self.assertIn('冲突',row.reason)

    def test_payment_page_close_mid_request_preserves_cursor_and_resumes(self):
        from apps.wecom.external_pay import pull_one_page, SyncPaused
        now=timezone.now().replace(microsecond=0)
        self.state.window_start=now-timedelta(hours=1); self.state.window_end=now
        self.state.save(update_fields=['window_start','window_end'])
        data=parse_bill({'bill_type':0,'transaction_id':'another-synthetic','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1})
        def close(*args):
            WecomAppConfig.objects.filter(pk=self.config.pk).update(external_pay_enabled=False)
            return [data], ''
        with patch('apps.wecom.external_pay._get_page',side_effect=close):
            with self.assertRaises(SyncPaused): pull_one_page(self.config,self.state)
        self.state.refresh_from_db()
        self.assertIsNone(self.state.watermark)
        self.assertFalse(ExternalPayBill.objects.exists())
        self.config.external_pay_enabled=True; self.config.save(update_fields=['external_pay_enabled'])
        with patch('apps.wecom.external_pay._get_page',return_value=([data],'')):
            pull_one_page(self.config,self.state)
        self.assertEqual(ExternalPayBill.objects.count(),1)

    def test_group_close_mid_request_leaves_pending_then_resumes(self):
        from apps.wecom.group_sync import queue_event,run_due as run_groups
        from apps.wecom.models import WecomGroupChat
        queue_event({'MsgType':'event','Event':'change_external_chat','ChatId':'mid-chat','ChangeType':'update','CreateTime':str(int(timezone.now().timestamp()))})
        group=WecomGroupChat.objects.get(); group.retry_at=timezone.now()-timedelta(seconds=1);group.save(update_fields=['retry_at'])
        with patch('apps.wecom.group_sync._contacts_client') as factory:
            def close(*args, **kwargs):
                WecomAppConfig.objects.filter(pk=self.config.pk).update(group_sync_enabled=False)
                return {'errcode':0,'group_chat':{'name':'new name'}}
            factory.return_value.post.side_effect=close
            run_groups()
        group.refresh_from_db(); self.assertTrue(group.pending); self.assertEqual(group.name,'')
        self.config.group_sync_enabled=True;self.config.save(update_fields=['group_sync_enabled'])
        with patch('apps.wecom.group_sync._contacts_client') as factory:
            factory.return_value.post.return_value={'errcode':0,'group_chat':{'name':'new name'}}
            run_groups()
        group.refresh_from_db(); self.assertEqual(group.name,'new name')

    def test_closed_group_dismissal_is_applied_only_after_reenable(self):
        from apps.wecom.group_sync import queue_event,run_due as run_groups
        from apps.wecom.models import WecomGroupChat
        group=WecomGroupChat.objects.create(chat_id='dismiss-chat',name='group')
        self.config.group_sync_enabled=False;self.config.save(update_fields=['group_sync_enabled'])
        queue_event({'MsgType':'event','Event':'change_external_chat','ChatId':'dismiss-chat','ChangeType':'dismiss','CreateTime':str(int(timezone.now().timestamp()))})
        group.refresh_from_db();self.assertFalse(group.dismissed);self.assertTrue(group.pending_dismissal)
        self.config.group_sync_enabled=True;self.config.save(update_fields=['group_sync_enabled'])
        group.retry_at=timezone.now()-timedelta(seconds=1);group.save(update_fields=['retry_at'])
        run_groups()
        group.refresh_from_db();self.assertTrue(group.dismissed);self.assertFalse(group.pending)

    def test_qr_closed_during_creation_keeps_config_but_hides_code(self):
        from apps.wecom.group_qr import get_or_create_qr
        from apps.wecom.models import WecomGroupChat,WecomGroupQr
        group=WecomGroupChat.objects.create(chat_id='qr-chat',name='群 synthetic')
        def remote(config,endpoint,data):
            if endpoint=='get':return {'errcode':0,'group_chat':{'chat_id':'qr-chat','name':'群 synthetic'}}
            if endpoint=='add_join_way':
                WecomAppConfig.objects.filter(pk=self.config.pk).update(group_qr_enabled=False)
                return {'errcode':0,'config_id':'synthetic-config'}
            raise AssertionError('关闭后不应读取群码')
        with patch('apps.wecom.group_qr._call',side_effect=remote):
            reply=get_or_create_qr(group)
        self.assertEqual(reply['status'],'unavailable')
        self.assertEqual(WecomGroupQr.objects.get(group=group).config_id,'synthetic-config')

    def test_notification_switch_closed_between_recipients(self):
        self.order.status='ISSUE';queue_order_status(self.order,'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'小李'},{'userid':'b','name':'小李'}]
            def send(*args):
                WecomAppConfig.objects.filter(pk=self.config.pk).update(order_status_enabled=False)
                return {'errcode':0}
            client.message.send_text.side_effect=send
            run_due()
        row=WecomNotification.objects.get()
        self.assertEqual(row.recipients,{'a':'sent','b':'failed'})
        self.assertEqual(client.message.send_text.call_count,1)

    def test_same_app_secret_rotation_keeps_history_other_app_does_not(self):
        from tests.p5_auth_helpers import make_logged_in_client
        client=make_logged_in_client(username='notice-rotation-admin',is_superuser=True)
        self.order.status='ISSUE';queue_order_status(self.order,'IN_PROGRESS')
        self.config.secret='new-secret';self.config.identity_revision=1;self.config.save(update_fields=['secret','identity_revision'])
        self.assertEqual(len(client.get('/api/wecom/notification/records/').json()['data']['items']),1)
        self.config.corp_id='other-corp';self.config.save(update_fields=['corp_id'])
        self.assertEqual(len(client.get('/api/wecom/notification/records/').json()['data']['items']),0)

    def test_payment_enqueue_failure_retry_survives_expiry(self):
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int((now-timedelta(days=3)+timedelta(minutes=1)).timestamp()),'trade_state':1,'payee_userid':'payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        with patch('apps.wecom.notifications.WecomNotification.objects.create',side_effect=RuntimeError('synthetic failure')):
            match_payment(bill.pk,self.config,now)
        bill.refresh_from_db();self.assertTrue(bill.mismatch_notification_pending)
        match_payment(bill.pk,self.config,now+timedelta(hours=1))
        bill.refresh_from_db();self.assertFalse(bill.mismatch_notification_pending)
        self.assertEqual(WecomNotification.objects.count(),1)

    def test_pending_payment_enqueue_is_cleared_when_feature_disabled(self):
        from tests.p5_auth_helpers import make_logged_in_client
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        with patch('apps.wecom.notifications.WecomNotification.objects.create',side_effect=RuntimeError('synthetic failure')):
            match_payment(bill.pk,self.config,now)
        client=make_logged_in_client(username='mismatch-switch-admin',is_superuser=True)
        response=client.put('/api/wecom/notification/settings/',data={'payment_mismatch_enabled':False},content_type='application/json')
        self.assertEqual(response.json()['code'],0)
        bill.refresh_from_db();self.assertTrue(bill.mismatch_notification_pending)
        response=client.put('/api/wecom/notification/settings/',data={'payment_mismatch_enabled':True},content_type='application/json')
        self.assertEqual(response.json()['code'],0)
        self.config.refresh_from_db()
        match_payment(bill.pk,self.config,now+timedelta(hours=1))
        bill.refresh_from_db();self.assertFalse(bill.mismatch_notification_pending)
        row=WecomNotification.objects.get()
        self.assertEqual(row.status,'failed')
        self.assertIn('开关',row.reason)

    def test_recovery_journal_retains_many_events_without_overwrite(self):
        from apps.wecom.notifications import _recover_order_journal
        with patch('apps.wecom.notifications.WecomNotification.objects.create',side_effect=RuntimeError('synthetic failure')), patch('apps.wecom.notifications.logger.exception'):
            for i in range(100):
                self.order.status='ISSUE' if i%2==0 else 'PAUSED'
                queue_order_status(self.order,'IN_PROGRESS',recover=False)
        self.order.refresh_from_db()
        self.assertEqual(len(self.order.wecom_notice_journal),100)
        self.assertEqual(len({event['event_key'] for event in self.order.wecom_notice_journal}),100)
        self.assertEqual(_recover_order_journal(),100)
        self.order.refresh_from_db()
        self.assertFalse(self.order.wecom_notice_pending)
        self.assertEqual(WecomNotification.objects.count(),100)
        self.assertEqual(_recover_order_journal(),0)

    def test_explicit_api_rejection_retries_without_repeating_success(self):
        from wechatpy.exceptions import WeChatClientException
        self.order.status='ISSUE';queue_order_status(self.order,'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':'a','name':'小李'},{'userid':'b','name':'小李'}]
            client.message.send_text.side_effect=[{'errcode':0},WeChatClientException(45009,'rate limited'),{'errcode':0}]
            run_due()
            row=WecomNotification.objects.get()
            self.assertEqual(row.status,'pending')
            self.assertEqual(row.recipients,{'a':'sent','b':'pending'})
            row.retry_at=timezone.now()-timedelta(seconds=1);row.save(update_fields=['retry_at'])
            run_due()
        row.refresh_from_db()
        self.assertEqual(row.status,'sent')
        self.assertEqual([c.args[1] for c in client.message.send_text.call_args_list],[['a'],['b'],['b']])

    def test_payment_pending_count_hides_orders_outside_game_scope(self):
        from tests.p5_auth_helpers import make_logged_in_client
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'payee'})
        ExternalPayBill.objects.create(state=self.state,**data,mismatch_notification_pending=True)
        restricted=make_logged_in_client(username='notice-no-game',groups=('客服',),nodes=('system.wecom_settings',),games=[])
        self.assertIsNone(restricted.get('/api/wecom/external-pay/status/').json()['data']['last_error'])
        admin=make_logged_in_client(username='notice-count-admin',is_superuser=True)
        self.assertIn('1 项企微通知等待补偿',admin.get('/api/wecom/external-pay/status/').json()['data']['last_error'])

    def test_old_enterprise_pending_payment_becomes_redacted_failed_notice(self):
        from tests.p5_auth_helpers import make_logged_in_client
        now=timezone.now()
        data=parse_bill({'bill_type':0,'transaction_id':'synthetic-payment','mch_id':'merchant','out_trade_no':'out','total_fee':1000,'total_refund_fee':0,'pay_time':int(now.timestamp()),'trade_state':1,'payee_userid':'old-payee'})
        bill=ExternalPayBill.objects.create(state=self.state,**data)
        with patch('apps.wecom.notifications.WecomNotification.objects.create',side_effect=RuntimeError('synthetic failure')), patch('apps.wecom.notifications.logger.exception'):
            match_payment(bill.pk,self.config,now)
        self.config.corp_id='new-corp';self.config.agent_id=2;self.config.identity_revision=1
        self.config.save(update_fields=['corp_id','agent_id','identity_revision'])
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            run_due()
            factory.assert_not_called()
        bill.refresh_from_db();self.assertFalse(bill.mismatch_notification_pending)
        row=WecomNotification.objects.get()
        self.assertEqual((row.corp_id,row.agent_id,row.status),('new-corp',2,'failed'))
        self.assertEqual(row.order_no,'')
        self.assertNotIn('old-payee',str(row.recipients)+row.detail+row.reason)
        self.assertNotIn('synthetic-payment',row.detail+row.reason)
        admin=make_logged_in_client(username='notice-switched-admin',is_superuser=True)
        items=admin.get('/api/wecom/notification/records/').json()['data']['items']
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['status'],'failed')

    def test_five_recipients_retry_rate_limits_independently(self):
        from wechatpy.exceptions import WeChatClientException
        self.order.status='ISSUE';queue_order_status(self.order,'IN_PROGRESS')
        with patch('apps.wecom.notifications.sdk.get_client') as factory:
            client=factory.return_value
            client.user.list.return_value=[{'userid':f'user-{i}','name':'小李'} for i in range(5)]
            client.message.send_text.side_effect=[WeChatClientException(45009,'rate limited') for _ in range(5)]+[{'errcode':0} for _ in range(5)]
            run_due()
            row=WecomNotification.objects.get()
            self.assertEqual(row.status,'pending')
            self.assertEqual(set(row.recipients.values()),{'pending'})
            self.assertEqual(set(row.recipient_attempts.values()),{1})
            row.retry_at=timezone.now()-timedelta(seconds=1);row.save(update_fields=['retry_at'])
            run_due()
        row.refresh_from_db()
        self.assertEqual(row.status,'sent')
        self.assertEqual(client.message.send_text.call_count,10)

    def test_status_and_notice_are_written_in_same_order_update(self):
        from django.test.utils import CaptureQueriesContext
        from apps.business.order_status import apply_transition
        self.order.status='IN_PROGRESS';self.order.save(update_fields=['status'])
        with CaptureQueriesContext(connection) as queries, transaction.atomic():
            apply_transition(self.order,'ISSUE',nodes=frozenset({'order.change_status'}))
        updates=[item['sql'].lower() for item in queries.captured_queries if item['sql'].lower().startswith('update ') and 'wecom_notice_journal' in item['sql'].lower()]
        self.assertEqual(len(updates),1)
        self.assertIn('"status"',updates[0])
        self.assertIn('"wecom_notice_pending"',updates[0])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status,'ISSUE')
        self.assertEqual(len(self.order.wecom_notice_journal),1)
