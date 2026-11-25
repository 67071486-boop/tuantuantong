from decimal import Decimal
from unittest.mock import patch

from apps.audit.models import OperationLog
from django.utils import timezone
from apps.business.models import EmpType, LegacySplitRefundPolicy, OrderRefund, OrderStatus, Performance, PerformanceAdjustment, SettleStatus
from apps.business.order_writes import WriteContext, refund_total_write
from apps.wecom.models import ExternalPayBill, ExternalPayRecheck, ExternalPayState, WecomAppConfig
from tests.test_refund_partial import _RefundFixture


class WecomRefundWriteTests(_RefundFixture):
    def setUp(self):
        super().setUp()
        self.order = self._order(status=OrderStatus.IN_PROGRESS, sales_user=self.sales_user, hall='60')
        self.sales = self._sales(self.order, commission_rate=None, bonus_amount=Decimal('0'))
        self._taker(self.order)

    def write(self, amount, request='test-request', enabled=True):
        return refund_total_write(
            self.order, refund_total=Decimal(amount), reason='企微退款', request_id=request,
            ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=enabled,
        )

    def test_in_progress_refund_updates_ledger_estimate_and_is_idempotent(self):
        self.assertTrue(self.write('10').ok)
        self.assertTrue(self.write('10').ok)
        self.assertTrue(self.write('25', 'test-next').ok)
        self.order.refresh_from_db()
        self.sales.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('25'))
        self.assertEqual(list(OrderRefund.objects.filter(order=self.order).order_by('id').values_list('amount', flat=True)), [Decimal('10'), Decimal('15')])
        self.assertEqual(self.sales.temp_amount, Decimal('3'))
        self.assertEqual(self.sales.bonus_amount, Decimal('0'))
        self.assertEqual(list(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('kind', 'amount')), [])
        self.assertEqual(OperationLog.objects.filter(action='order.refund', order_no=self.order.order_no).count(), 2)

    def test_manual_refund_still_rejects_in_progress(self):
        result = self.write('10', enabled=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 1007)
        self.assertFalse(OrderRefund.objects.filter(order=self.order).exists())

    def test_price_change_queues_bound_payment_for_recheck(self):
        from django.db import connection

        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, corp_id='synthetic-corp',
                                      agent_id=1, secret='synthetic-secret', enabled=True)
        state = ExternalPayState.objects.create(identity_revision=0, corp_id='synthetic-corp')
        ExternalPayBill.objects.create(state=state, bill_type=0, transaction_id='synthetic-payment',
                                       mch_id='synthetic-merchant', out_trade_no='synthetic-order',
                                       total_fee=10000, pay_time=timezone.now(), order=self.order)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/price/',
            {'total_price': '90.00', 'reason': '合成改价'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.assertTrue(ExternalPayRecheck.objects.filter(order=self.order, pending=True).exists())

    def test_completion_after_refund_uses_net_receipt_once(self):
        self.assertTrue(self.write('10').ok)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/transition/',
            {'target': 'PENDING_REVIEW'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/transition/',
            {'target': 'COMPLETED'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('6'))
        self.assertEqual(self.sales.commission_rate, Decimal('20'))
        self.assertFalse(PerformanceAdjustment.objects.filter(performance=self.sales).exists())

    def test_return_to_pool_then_restore_refund_in_new_round(self):
        self.assertTrue(self.write('10').ok)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/transition/',
            {'target': 'REFUNDED'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/clear-taker/',
            {}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('0'))
        self.assertTrue(self.write('10', 'new-round').ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('10'))
        self.assertEqual(sum(OrderRefund.objects.filter(order=self.order).values_list('amount', flat=True)), Decimal('10'))

    def test_refunded_order_records_total_without_extra_commission_deduction(self):
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/transition/',
            {'target': 'REFUNDED'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        existing = list(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True))
        self.assertTrue(self.write('10').ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('10'))
        self.assertEqual(list(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True)), existing)

    def test_external_total_restores_after_manual_change(self):
        for target in ('PENDING_REVIEW', 'COMPLETED'):
            result = self.admin.post(
                f'/api/business/orders/{self.order.order_no}/transition/',
                {'target': target}, content_type='application/json',
            ).json()
            self.assertEqual(result['code'], 0, result)
        self.order.refresh_from_db()
        self.assertEqual(self.write('101', enabled=False).code, 1008)
        self.assertTrue(self.write('10', 'external-event-1').ok)
        self.assertTrue(self.write('5', 'manual-event', enabled=False).ok)
        self.assertTrue(self.write('10', 'external-event-2').ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('10'))
        self.assertEqual(list(OrderRefund.objects.filter(order=self.order).order_by('id').values_list('amount', flat=True)), [Decimal('10'), Decimal('-5'), Decimal('5')])

    def test_refunded_directly_reopened_then_completed_uses_refunded_amount(self):
        for target in ('PENDING_REVIEW', 'COMPLETED', 'REFUNDED'):
            response = self.admin.post(
                f'/api/business/orders/{self.order.order_no}/transition/',
                {'target': target}, content_type='application/json',
            ).json()
            self.assertEqual(response['code'], 0, response)
        self.order.refresh_from_db()
        self.assertTrue(self.write('10').ok)
        for target in ('IN_PROGRESS', 'PENDING_REVIEW', 'COMPLETED'):
            response = self.admin.post(
                f'/api/business/orders/{self.order.order_no}/transition/',
                {'target': target}, content_type='application/json',
            ).json()
            self.assertEqual(response['code'], 0, response)
        self.order.refresh_from_db()
        self.sales.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('10'))
        self.assertEqual(self.sales.bonus_amount, Decimal('6'))
        self.assertEqual(sum(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True)), Decimal('0'))

    def test_settled_customer_needs_rate_and_adjustment(self):
        self.sales.settle_status = SettleStatus.SETTLED
        self.sales.save(update_fields=['settle_status'])
        self.assertFalse(self.write('10').ok)
        self.assertFalse(OrderRefund.objects.filter(order=self.order).exists())
        self.sales.commission_rate = Decimal('20')
        self.sales.save(update_fields=['commission_rate'])
        self.assertTrue(self.write('10').ok)
        self.assertEqual(PerformanceAdjustment.objects.get(performance=self.sales).amount, Decimal('-2'))

    def test_invalid_amount_and_failure_roll_back(self):
        self.assertFalse(self.write('101', enabled=False).ok)
        self.assertFalse(self.write('-1').ok)
        with patch('apps.business.settlement_hooks.refresh_customer_estimate', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.write('10')
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('0'))
        self.assertFalse(OrderRefund.objects.filter(order=self.order).exists())
        self.assertFalse(OperationLog.objects.filter(action='order.refund', order_no=self.order.order_no).exists())

    def test_external_refund_can_exceed_current_price_and_affects_commission(self):
        self.assertTrue(self.write('110').ok)
        self.order.refresh_from_db()
        self.sales.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('110'))
        self.assertEqual(self.order.total_price - self.order.refund_fee, Decimal('-10'))
        self.assertEqual(self.sales.temp_amount, Decimal('-14'))
        self.assertEqual(self.sales.bonus_amount, Decimal('0'))
        response = self.admin.get('/api/business/orders/').json()
        self.assertEqual(response['code'], 0, response)
        item = next(row for row in response['data']['items'] if row['order_no'] == self.order.order_no)
        self.assertEqual(item['net_received'], '-10.00')

    def test_other_unfinished_states_record_external_refunds(self):
        for status in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB,
                       OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE, OrderStatus.PAUSED):
            with self.subTest(status=status):
                order = self._order(status=status)
                result = refund_total_write(
                    order, refund_total=Decimal('110'), reason='企微退款',
                    request_id=f'other-state-{status}', ctx=WriteContext(log_actor='企微同步'),
                    dry_run=False, allow_external_refund=True,
                )
                self.assertTrue(result.ok, result)
                order.refresh_from_db()
                self.assertEqual(order.refund_fee, Decimal('110'))

    def test_incomplete_split_main_without_commission_rate_records_refund(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-unfinished-split'
        self.order.save(update_fields=['status', 'split_id'])
        child = self._order(status=OrderStatus.PENDING_REVIEW, total='0', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.save(update_fields=['split_id'])
        self._sales(child, commission_rate=None, bonus_amount=Decimal('0'))
        self.assertTrue(self.write('10').ok)
        self.assertFalse(PerformanceAdjustment.objects.filter(performance=self.sales).exists())
        response = self.admin.post(
            f'/api/business/orders/{child.order_no}/transition/',
            {'target': 'COMPLETED'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('7'))

    def test_completed_legacy_child_refund_reduces_main_commission(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-child-refund'
        self.order.save(update_fields=['status', 'split_id'])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(bonus_amount=0, temp_amount=0)
        self.sales.commission_rate = Decimal('20')
        self.sales.bonus_amount = Decimal('28')
        self.sales.save(update_fields=['commission_rate', 'bonus_amount'])
        child = self._order(status=OrderStatus.COMPLETED, total='40', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.save(update_fields=['split_id'])
        child_sales = self._sales(child, commission_rate=None, bonus_amount=Decimal('0'))
        result = refund_total_write(
            child, refund_total=Decimal('10'), reason='企微退款', request_id='child-external',
            ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
        )
        self.assertTrue(result.ok, result)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('26'))
        self.assertFalse(PerformanceAdjustment.objects.filter(performance=child_sales).exists())
        child_sales.commission_rate = Decimal('20')
        child_sales.save(update_fields=['commission_rate'])
        manual = refund_total_write(
            child, refund_total=Decimal('5'), reason='人工退款', request_id='child-after-auto',
            ctx=WriteContext(log_actor='客服'), dry_run=False,
        )
        self.assertTrue(manual.ok, manual)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('26'))
        self.assertEqual(PerformanceAdjustment.objects.get(performance=child_sales).amount, Decimal('1'))
        retry = refund_total_write(
            child, refund_total=Decimal('10'), reason='企微退款', request_id='child-external-retry',
            ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
        )
        self.assertFalse(retry.ok)
        self.assertIn('人工核对', retry.msg)
        child.refresh_from_db()
        self.assertEqual(child.refund_fee, Decimal('5'))

    def test_manual_completed_legacy_child_refund_keeps_existing_behaviour(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-manual-child'
        self.order.save(update_fields=['status', 'split_id'])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(bonus_amount=0, temp_amount=0)
        self.sales.commission_rate = Decimal('20')
        self.sales.bonus_amount = Decimal('28')
        self.sales.save(update_fields=['commission_rate', 'bonus_amount'])
        child = self._order(status=OrderStatus.COMPLETED, total='40', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.save(update_fields=['split_id'])
        child_sales = self._sales(child, commission_rate=Decimal('20'), bonus_amount=Decimal('0'))
        result = refund_total_write(
            child, refund_total=Decimal('10'), reason='人工退款', request_id='child-manual',
            ctx=WriteContext(log_actor='客服'), dry_run=False,
        )
        self.assertTrue(result.ok, result)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('28'))
        self.assertEqual(PerformanceAdjustment.objects.get(performance=child_sales).amount, Decimal('-2'))

    def test_legacy_child_with_historical_child_deduction_is_held_for_review(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-history-split'
        self.order.save(update_fields=['status', 'split_id'])
        child = self._order(status=OrderStatus.COMPLETED, total='40', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.refund_fee = Decimal('5')
        child.save(update_fields=['split_id', 'refund_fee'])
        child_sales = self._sales(child, commission_rate=Decimal('20'))
        old = OrderRefund.objects.create(order=child, kind=OrderRefund.Kind.SET, amount=Decimal('5'),
                                         reason='旧退款', operator='客服', request_id='old-refund')
        PerformanceAdjustment.objects.create(performance=child_sales, refund=old,
                                             kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
                                             amount=Decimal('-1'), operator='客服')
        result = refund_total_write(
            child, refund_total=Decimal('10'), reason='企微退款', request_id='new-refund',
            ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
        )
        self.assertFalse(result.ok)
        self.assertIn('人工核对', result.msg)
        child.refresh_from_db()
        self.assertEqual(child.refund_fee, Decimal('5'))
        main_result = self.write('10', 'main-new-refund')
        self.assertFalse(main_result.ok)
        self.assertIn('人工核对', main_result.msg)
        from apps.business.settlement_hooks import recompute_customer_commission
        recompute_customer_commission(self.order, dry_run=False)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('17'))

    def test_unmarked_legacy_child_with_old_refund_ledger_keeps_old_commission(self):
        from apps.business.settlement_hooks import recompute_customer_commission
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-old-ledger'
        self.order.save(update_fields=['status', 'split_id'])
        child = self._order(status=OrderStatus.COMPLETED, total='40', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.refund_fee = Decimal('5')
        child.save(update_fields=['split_id', 'refund_fee'])
        OrderRefund.objects.create(order=child, kind=OrderRefund.Kind.SET, amount=Decimal('5'),
                                   reason='旧退款', operator='客服', request_id='historical-ledger')
        recompute_customer_commission(self.order, dry_run=False)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('17'))
        result = refund_total_write(
            child, refund_total=Decimal('10'), reason='企微退款', request_id='new-ledger',
            ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
        )
        self.assertFalse(result.ok)
        self.assertIn('人工核对', result.msg)

    def test_group_anchor_with_old_child_policy_does_not_double_count_refund(self):
        from apps.business.settlement_amount import legacy_group_net_received
        self.order.split_id = 'synthetic-promoted-anchor'
        self.order.refund_fee = Decimal('10')
        self.order.save(update_fields=['split_id', 'refund_fee'])
        LegacySplitRefundPolicy.objects.create(order=self.order, split_id=self.order.split_id, refund_total=Decimal('10'))
        self.assertEqual(legacy_group_net_received(self.order), Decimal('90'))

    def test_legacy_child_refund_reverses_settled_main_adjustment_on_pool_reentry(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.split_id = 'synthetic-settled-split'
        self.order.save(update_fields=['status', 'split_id'])
        self.sales.commission_rate = Decimal('20')
        self.sales.bonus_amount = Decimal('28')
        self.sales.settle_status = SettleStatus.SETTLED
        self.sales.save(update_fields=['commission_rate', 'bonus_amount', 'settle_status'])
        child = self._order(status=OrderStatus.COMPLETED, total='40', sales_user=self.sales_user)
        child.split_id = self.order.split_id
        child.save(update_fields=['split_id'])
        self._sales(child, commission_rate=Decimal('20'), bonus_amount=Decimal('0'))
        def sync(request):
            return refund_total_write(
                child, refund_total=Decimal('10'), reason='企微退款', request_id=request,
                ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
            )
        self.assertTrue(sync('settled-child-1').ok)
        self.assertEqual(sum(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True)), Decimal('-2'))
        for status in ('REFUNDED', 'UNASSIGNED'):
            response = self.admin.post(
                f'/api/business/orders/{child.order_no}/transition/',
                {'target': status}, content_type='application/json',
            ).json()
            self.assertEqual(response['code'], 0, response)
        self.assertEqual(sum(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True)), Decimal('0'))
        self.assertEqual(LegacySplitRefundPolicy.objects.get(order=child).refund_total, Decimal('0'))
        child.refresh_from_db()
        self.assertTrue(sync('settled-child-2').ok)
        self.assertTrue(sync('settled-child-3').ok)
        self.assertEqual(sum(PerformanceAdjustment.objects.filter(performance=self.sales).values_list('amount', flat=True)), Decimal('-2'))
        self.assertEqual(LegacySplitRefundPolicy.objects.get(order=child).refund_total, Decimal('10'))

    def test_completed_external_over_refund_recalculates_negative_commission(self):
        for target in ('PENDING_REVIEW', 'COMPLETED'):
            response = self.admin.post(
                f'/api/business/orders/{self.order.order_no}/transition/',
                {'target': target}, content_type='application/json',
            ).json()
            self.assertEqual(response['code'], 0, response)
        self.order.refresh_from_db()
        self.assertTrue(self.write('110').ok)
        self.sales.refresh_from_db()
        self.assertEqual(self.sales.bonus_amount, Decimal('-14'))
        self.assertEqual(self.sales.temp_amount, Decimal('-14'))

    def test_refunded_then_pool_restores_external_over_refund(self):
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/transition/',
            {'target': 'REFUNDED'}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.assertTrue(self.write('110', 'refunded-event').ok)
        response = self.admin.post(
            f'/api/business/orders/{self.order.order_no}/clear-taker/',
            {}, content_type='application/json',
        ).json()
        self.assertEqual(response['code'], 0, response)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refund_fee, Decimal('0'))
        self.assertTrue(self.write('110', 'restored-event').ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price - self.order.refund_fee, Decimal('-10'))
        self.assertEqual(sum(OrderRefund.objects.filter(order=self.order).values_list('amount', flat=True)), Decimal('110'))
