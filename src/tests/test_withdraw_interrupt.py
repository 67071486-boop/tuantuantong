"""财务中断（2026-10-21，提现改造增量 §2.4 / §2.5）。

判据钉在数据库行：

- `approved` 中断 ⇒ `interrupted` + 钱退回余额 + 原因 + 审计；
- 重复中断 / `pending` / `paying` / `paid` ⇒ `CONFLICT`，余额不变；
- 退款时 `approved` 的提现单被中断，`paying` 不动；
  ⚠️「申请即通过」的快路已下线（spec §8.2）⇒ 本文件的夹具**显式**把
  提现单置成 `approved`（产品路径不再产出它）。
- 手工重跑（`from_status=REFUNDED`）不中断。
"""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import TakerWallet, WithdrawOrder
from apps.accounts.wallet import WalletError, interrupt_withdraw, request_withdraw
from apps.audit.models import OperationLog
from apps.business.models import OrderStatus
from apps.business.refunds import on_order_refunded
from tests.test_wallet_payout import PayoutFixture


class InterruptTests(PayoutFixture):
    def _approved_withdrawal(self, amount="10.00"):
        wallet = TakerWallet.objects.get_or_create(user=self.taker)[0]
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance"])
        return WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal(amount),
            alipay_account="payout@example.com",
            alipay_name="测试打手",
            state=WithdrawOrder.STATE_APPROVED,
            reviewed_at=timezone.now(),
            approved_at=timezone.now(),
            payout_after=timezone.now(),
        )

    def test_interrupt_returns_the_money_and_records_the_reason(self):
        order = self._approved_withdrawal()

        interrupt_withdraw(order, operator=self.admin_user, reason="风控复核")

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_INTERRUPTED)
        self.assertIsNotNone(order.interrupted_at)
        self.assertEqual(order.interrupt_reason, "风控复核")
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("110.00"))
        logs = OperationLog.objects.filter(action="finance.withdraw_interrupt")
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs[0].reason, "风控复核")
        self.assertEqual(logs[0].to_value, "10.00")

    def test_a_second_interrupt_is_rejected_and_does_not_refund_twice(self):
        order = self._approved_withdrawal()
        interrupt_withdraw(order, operator=self.admin_user, reason="第一次")

        with self.assertRaises(WalletError):
            interrupt_withdraw(order, operator=self.admin_user, reason="第二次")

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("110.00"))

    def test_pending_cannot_be_interrupted(self):
        wallet = TakerWallet.objects.get_or_create(user=self.taker)[0]
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance"])
        order = WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal("10.00"),
            alipay_account="payout@example.com",
            alipay_name="测试打手",
        )

        with self.assertRaises(WalletError):
            interrupt_withdraw(order, operator=self.admin_user, reason="x")
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("100.00"))

    def test_paying_cannot_be_interrupted(self):
        order = self._approved_withdrawal()
        WithdrawOrder.objects.filter(pk=order.pk).update(state=WithdrawOrder.STATE_PAYING)

        with self.assertRaises(WalletError):
            interrupt_withdraw(order, operator=self.admin_user, reason="x")

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("100.00"))

    def test_an_empty_reason_is_rejected(self):
        order = self._approved_withdrawal()

        with self.assertRaises(WalletError):
            interrupt_withdraw(order, operator=self.admin_user, reason="  ")

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)


class RefundInterruptTests(PayoutFixture):
    def _completed_order_with_approved_withdrawal(self):
        """一张完成单 + 一笔「已通过、未打款」的提现（夹具显式置 approved）。

        ⛔ **2026-10-23 起 `request_withdraw` 恒产出 `pending`**（前三单快路整段下线，
        spec §8.2）⇒ 本文件的用例考的是「已通过、未打款」那一档的退款中断，
        所以在这里**显式**把单置成审核通过（模拟财务点「通过」），而不是依赖申请即通过。
        """
        order = self._order()
        self._main(order)
        self._complete(order)  # 人工补入账 30 元
        withdrawal = request_withdraw(self.taker, Decimal("10.00"))
        self.assertFalse(withdrawal.fast_lane)
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_PENDING)
        WithdrawOrder.objects.filter(pk=withdrawal.pk).update(
            state=WithdrawOrder.STATE_APPROVED,
            reviewed_at=timezone.now(),
            approved_at=timezone.now(),
        )
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_APPROVED)
        return order, withdrawal

    def test_a_refund_interrupts_an_approved_withdrawal(self):
        order, withdrawal = self._completed_order_with_approved_withdrawal()

        on_order_refunded(order, OrderStatus.COMPLETED)

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_INTERRUPTED)
        self.assertEqual(withdrawal.interrupt_reason, "订单退款")
        # 30 入账 - 10 提现 = 20；中断退回 10 ⇒ 30；退款冲账 -30 ⇒ 0。
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("0.00"))

    def test_a_rerun_does_not_interrupt(self):
        order, withdrawal = self._completed_order_with_approved_withdrawal()

        # 手工重跑：`from_status` 已经是 REFUNDED ⇒ `is_rerun=True`。
        on_order_refunded(order, OrderStatus.REFUNDED)

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_APPROVED)

    def test_a_paying_withdrawal_is_not_interrupted(self):
        order, withdrawal = self._completed_order_with_approved_withdrawal()
        WithdrawOrder.objects.filter(pk=withdrawal.pk).update(
            state=WithdrawOrder.STATE_PAYING
        )

        on_order_refunded(order, OrderStatus.COMPLETED)

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_PAYING)
