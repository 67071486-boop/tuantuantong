"""企微收款同步与人工操作争用数据库锁的端到端验证。"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderRefund, OrderStatus, PaymentStatus, Performance
from apps.wecom.external_pay import run_due
from apps.wecom.models import ExternalPayBill, ExternalPayState, WecomAppConfig


class ExternalPayConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="wecom_pay_lock_test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain="wecom-pay-lock.test.com")
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_performance_lock_causes_atomic_retry_then_success(self):
        now = timezone.now()
        WecomAppConfig.objects.create(tenant_schema=self.tenant.schema_name, corp_id="synthetic-corp", secret="synthetic-secret", agent_id=1, enabled=True)
        state = ExternalPayState.objects.create(identity_revision=0, corp_id="synthetic-corp")
        game = Game.objects.create(name="合成并发游戏")
        sales = User.objects.create_user(username="synthetic-pay-sales")
        StaffProfile.objects.create(user=sales, commission_mode=CommissionMode.PROFIT_PERCENT, commission_value=Decimal("20"))
        order = Order.objects.create(game=game, order_no="99990000000000000001", source_order_no="synthetic-trade", status=OrderStatus.IN_PROGRESS, total_price=Decimal("100"), sales_user=sales)
        performance = Performance.objects.create(order=order, user=sales, emp_type=EmpType.SALES, commission_rate=Decimal("20"))
        bill = ExternalPayBill.objects.create(state=state, bill_type=0, transaction_id="synthetic-trade", mch_id="synthetic-merchant", out_trade_no="synthetic-order", total_fee=10000, total_refund_fee=1000, pay_time=now - timedelta(hours=1), trade_state=3, next_retry_at=now - timedelta(seconds=1))
        before_status = order.payment_status
        held, release, completed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def hold_performance():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    Performance.objects.select_for_update().get(pk=performance.pk)
                    held.set()
                    if not release.wait(15):
                        raise AssertionError("未及时释放绩效锁")
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        def sync_while_locked():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    run_due()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
                connections.close_all()

        holder = threading.Thread(target=hold_performance)
        syncer = threading.Thread(target=sync_while_locked)
        holder.start()
        try:
            self.assertTrue(held.wait(10), errors)
            with patch("apps.wecom.external_pay.prepare_window", return_value=False):
                syncer.start()
                self.assertTrue(completed.wait(5), "锁冲突应立即返回并安排重试")
                self.assertEqual(errors, [])
                order.refresh_from_db(); bill.refresh_from_db()
                self.assertEqual(order.refund_fee, Decimal("0"))
                self.assertEqual(order.payment_status, before_status)
                self.assertFalse(OrderRefund.objects.filter(order=order).exists())
                self.assertIsNone(bill.order_id)
                self.assertIn("稍后自动重试", bill.issue)
                self.assertGreaterEqual(bill.next_retry_at, now + timedelta(minutes=59))
                self.assertLessEqual(bill.next_retry_at, timezone.now() + timedelta(minutes=61))
        finally:
            release.set()
            holder.join(15)
            if syncer.ident is not None:
                syncer.join(15)
            connection.set_tenant(self.tenant)
        self.assertFalse(holder.is_alive())
        self.assertFalse(syncer.is_alive())
        self.assertEqual(errors, [])

        bill.next_retry_at = timezone.now() - timedelta(seconds=1)
        bill.save(update_fields=["next_retry_at"])
        with patch("apps.wecom.external_pay.prepare_window", return_value=False):
            run_due()
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(order.refund_fee, Decimal("10"))
        self.assertEqual(list(OrderRefund.objects.filter(order=order).values_list("amount", flat=True)), [Decimal("10")])
        self.assertEqual(bill.order_id, order.pk)
        self.assertEqual(bill.issue, "")
        self.assertIsNone(bill.next_retry_at)
