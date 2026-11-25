"""验收自动入账 + 已结账绩效的调整冲账（2026-10-21，`feat/wallet-single-payout` spec §1）。

## 判据钉在哪

一律钉**数据库行的值**（`AGENTS.md` §5-G）：钱包余额、绩效行的结账三列、
`WalletTransaction` 的 `kind/amount/ref_no/balance_after`、操作日志的条数与 actor。
⛔ 不断言「函数被调用了」——那不能回答「钱到底动没动」。

## 覆盖的十条（规格 §4 的一期部分）

1. 验收 ⇒ 钱包 +amount、绩效已结账、流水 `earning`、日志一条；再入账一次不重复加钱；
2. 没有 MAIN 绩效行 ⇒ 不抛错、不加钱；
3. 净额 ≤ 0 ⇒ 不加钱、不扣钱，绩效仍标已结账；
4. 已自动入账的行再调 `settle_performances` ⇒ 跳过（防双付）；
5. 已结账行上的负数调整 ⇒ 余额减少、可负；同一条调整只冲一次；
6. 余额为负 / 余额为 0 时提现 ⇒ 拒绝；
7. 客服行（SALES）不自动入账、它的调整也不冲钱包。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import (
    StaffProfile,
    TakerWallet,
    WalletTransaction,
    WithdrawOrder,
)
from apps.accounts.payout import payout_adjustment, payout_completed_order
from apps.accounts.wallet import WalletError, get_wallet, request_withdraw
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import make_logged_in_client

ADMIN_USERNAME = "wallet-payout-admin"
COMPLETED_NODES = {"order.change_status_completed"}


def _no(n):
    return f"20261021{n:012d}"


class PayoutFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="payout-taker")
        self.sales = User.objects.create_user(username="payout-sales")
        StaffProfile.objects.create(
            user=self.taker,
            mobile="13700002222",
            alipay_account="payout@example.com",
            alipay_name="测试打手",
            # ⭐ 本文件每条用例都建立在「自动结账」上 ⇒ 打手必须是**打手 App 注册**来源
                        #    （业务方口径 D-0279：自动结账只对 App 注册打手生效）。
            #    「非 App 不自动结账」那一支的判据在 `tests/test_payout_app_taker_only.py`。
            account_source=AccountSource.APP,
        )
        make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        self.admin_user = User.objects.get(username=ADMIN_USERNAME)
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.PENDING_REVIEW, sales_user=None):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=_no(self._seq),
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("30.00"),
            sales_user=sales_user,
        )

    def _main(self, order, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "user": self.taker,
            "temp_amount": Decimal("30.00"),
            "bonus_amount": Decimal("30.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _adjust(self, performance, amount, *, kind=PerformanceAdjustment.Kind.MANUAL):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=kind,
            amount=Decimal(amount),
            reason="测试调整",
            settle_status=SettleStatus.UNSETTLED,
        )

    def _complete(self, order):
        """把单推进「已完成」，然后**人工**补一次工资入账（⛔ 这不是系统行为）。

        ⛔ **2026-10-23 起 `apply_transition` 不再自动调 `payout_completed_order`**
        （spec `2026-10-23-taker-income-and-early-settlement.md` §3.1：钱包不再收订单绩效）。
        本文件的用例测的是**入账函数本身**（历史 / 人工场景）及其与结账、冲账、提现的关系
        ⇒ 这里显式补上那一跳，语义是「财务 / 运维按老口径人工补一笔」，**不是**「完成单
        会自动发钱」。⛔ 谁把这一行当成系统行为来读，就会得出与事实相反的结论；
        「完成单不再自动入账」的判据在
        `tests/test_app_income_api.py::CompletionDoesNotCreditWalletTests`（⛔ 不在本文件）。
        """
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        return payout_completed_order(order)


class ManualPayoutAfterCompletionTests(PayoutFixture):
    """**人工**补入账那条路（2026-10-23 起系统不再「完成即入账」）。

    ⚠️ 本类原名 `AutoCreditOnCompletionTests`、第一条用例原名
    `test_completion_credits_the_wallet_and_marks_the_performance_settled`——那两个名字
    陈述的是**已废除**的口径（评审 2026-10-23 Important-2 点名「名实相反」）。今天的语义是：
    完成只生成绩效行；`payout_completed_order` 由人显式调用（历史 / 人工场景）才动钱包。
    """

    def test_completion_itself_pays_nothing_and_a_manual_payout_does(self):
        """完成**本身**一分钱都不动；随后**人工**补一笔才入账（四列 + 流水 + 日志同写）。"""
        order = self._order()
        perf = self._main(order)

        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)

        # ── 第一段：**完成**这一步（起的新口径）──
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)   # 阳性对照：真的完成了
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)

        # ── 第二段：**人工**补一笔入账（⛔ 不是系统自动行为）──
        payout_completed_order(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(perf.settled_amount, Decimal("30.00"))
        self.assertIsNotNone(perf.settled_at)

        tx = WalletTransaction.objects.get(wallet=wallet, kind="earning")
        self.assertEqual(tx.amount, Decimal("30.00"))
        self.assertEqual(tx.ref_no, f"P{perf.pk}")
        self.assertEqual(tx.balance_after, Decimal("30.00"))

        logs = OperationLog.objects.filter(
            action="performance.settle", order_no=order.order_no
        )
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.actor, "system")
        self.assertEqual(log.target_kind, "performance")
        self.assertEqual(str(log.target_id), str(perf.pk))
        self.assertEqual(log.reason, "订单完成自动入账")
        self.assertEqual(log.to_value, "已结账")

    def test_a_second_payout_does_not_add_the_money_again(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)

        payout_completed_order(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=wallet, kind="earning"
            ).count(),
            1,
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle", order_no=order.order_no
            ).count(),
            1,
        )

    def test_the_amount_is_the_payable_net_including_deductions_and_adjustments(self):
        order = self._order()
        perf = self._main(
            order,
            temp_amount=Decimal("40.00"),
            bonus_amount=Decimal("40.00"),
            deduct_mode="1",
            deduct_value=Decimal("5.00"),
        )
        self._adjust(perf, "-3.00")

        self._complete(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        # 40 - 5 + (-3) = 32
        self.assertEqual(wallet.balance, Decimal("32.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("32.00"))

    def test_no_main_performance_row_does_not_raise_and_does_not_pay(self):
        order = self._order()

        self._complete(order)

        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(), 0
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)

    def test_a_non_positive_net_does_not_touch_the_balance_but_still_marks_settled(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("5.00")
        wallet.save(update_fields=["balance", "updated_at"])
        order = self._order()
        perf = self._main(
            order, temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00")
        )
        # 未结账时的调整：不冲钱包（后台结算时才按净额算），这里进的是 payable_net。
        self._adjust(perf, "-20.00")

        self._complete(order)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("5.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_amount, Decimal("-10.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind="earning").count(), 0
        )

    def test_settling_an_already_paid_row_again_is_skipped(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)

        result = settle_performances(
            [perf.pk], actor="财务", user=self.admin_user
        )

        self.assertEqual(result.settled_performances, [])
        self.assertEqual(result.skipped_performances, [perf.pk])
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind="earning").count(), 1
        )

    def test_a_sales_row_is_not_credited(self):
        order = self._order()
        Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=self.sales,
            temp_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
            settle_status=SettleStatus.SETTLED,
            settled_amount=Decimal("8.00"),
        )

        self._complete(order)

        self.assertFalse(TakerWallet.objects.filter(user=self.sales).exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)


class AdjustmentOffsetTests(PayoutFixture):
    def _completed(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)
        return order, perf

    def test_a_negative_adjustment_debits_a_settled_row_and_may_go_negative(self):
        _order, perf = self._completed()

        adjustment = self._adjust(
            perf, "-40.00", kind=PerformanceAdjustment.Kind.FULL_REFUND
        )

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("-10.00"))
        tx = WalletTransaction.objects.get(wallet=wallet, kind="earning_adjust")
        self.assertEqual(tx.amount, Decimal("-40.00"))
        self.assertEqual(tx.ref_no, f"A{adjustment.pk}")
        self.assertEqual(tx.balance_after, Decimal("-10.00"))

    def test_the_same_adjustment_is_only_offset_once(self):
        _order, perf = self._completed()
        adjustment = self._adjust(perf, "-40.00")

        # 重复调冲账 / 重复 save 同一条调整 ⇒ 一分不动（判据是那一条流水）。
        self.assertIsNone(payout_adjustment(adjustment))
        adjustment.reason = "改了原因再存一次"
        adjustment.save(update_fields=["reason"])

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("-10.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind="earning_adjust").count(), 1
        )

    def test_a_positive_adjustment_credits_a_settled_row(self):
        _order, perf = self._completed()
        self._adjust(perf, "7.50")

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("37.50"))

    def test_an_unsettled_row_is_not_touched_by_the_adjustment(self):
        order = self._order()
        perf = self._main(order)  # 仍是未结账
        self._adjust(perf, "-5.00")

        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_a_sales_adjustment_never_reaches_a_wallet(self):
        order = self._order(status=OrderStatus.COMPLETED)
        sales_perf = Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=self.sales,
            temp_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
            settle_status=SettleStatus.SETTLED,
            settled_amount=Decimal("8.00"),
        )

        self._adjust(sales_perf, "-8.00", kind=PerformanceAdjustment.Kind.FULL_REFUND)

        self.assertFalse(TakerWallet.objects.filter(user=self.sales).exists())
        self.assertEqual(
            WalletTransaction.objects.filter(kind="earning_adjust").count(), 0
        )


class ReCompletionTopUpTests(PayoutFixture):
    """完成 → 重开（`COMPLETED → PENDING_REVIEW`）→ 再完成：**按新净额补差、不重复加**。

    这是 2026-10-21 追加口径（合并树上 `TakerActivationRuleTests
    ::test_re_entering_completed_overwrites_the_bonus_without_duplicating_rows` 报的行为回归）：
    自动入账之后第一次完成就已「已结账」，若照规格第 2 条直接返回，第二次完成既不更新
    `settled_amount` 也不补差 ⇒ 打手永远拿旧金额。口径 = **只补不退**（与「只加不减」同源）。
    """

    def _reopen(self, order):
        return apply_transition(
            order, OrderStatus.PENDING_REVIEW, nodes={"order.change_status"}
        )

    def test_recompleting_tops_up_the_difference(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)
        self.assertEqual(
            TakerWallet.objects.get(user=self.taker).balance, Decimal("30.00")
        )

        self._reopen(order)
        perf.refresh_from_db()
        perf.temp_amount = Decimal("50.00")
        perf.save(update_fields=["temp_amount", "updated_at"])
        self._complete(order)

        perf.refresh_from_db()
        self.assertEqual(perf.bonus_amount, Decimal("50.00"))
        self.assertEqual(perf.settled_amount, Decimal("50.00"))
        self.assertEqual(perf.settled_by, "system")
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("50.00"))
        txs = list(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning")
            .order_by("id")
            .values_list("amount", flat=True)
        )
        self.assertEqual(txs, [Decimal("30.00"), Decimal("20.00")])

    def test_recompleting_with_a_lower_amount_does_not_claw_back(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)

        self._reopen(order)
        perf.refresh_from_db()
        perf.temp_amount = Decimal("10.00")
        perf.save(update_fields=["temp_amount", "updated_at"])
        self._complete(order)

        perf.refresh_from_db()
        self.assertEqual(perf.bonus_amount, Decimal("10.00"))
        # 已发 30 是实付快照；新净额更低 ⇒ 不倒扣（多发的靠退款 / 手工调整追）。
        self.assertEqual(perf.settled_amount, Decimal("30.00"))
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning").count(), 1
        )

    def test_calling_the_payout_again_after_a_top_up_pays_nothing(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)
        self._reopen(order)
        perf.refresh_from_db()
        perf.temp_amount = Decimal("50.00")
        perf.save(update_fields=["temp_amount", "updated_at"])
        self._complete(order)

        payout_completed_order(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("50.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning").count(), 2
        )


class RefundClawbackTests(PayoutFixture):
    """完成后退款 ⇒ 自动入账的钱同事务扣回（`refunds.on_order_refunded` 的 1.6 步）。"""

    def _refund(self, order):
        return apply_transition(
            order, OrderStatus.REFUNDED, nodes={"order.change_status"}
        )

    def test_a_full_refund_takes_the_auto_credited_money_back(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)

        self._refund(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        adjustment = PerformanceAdjustment.objects.get(
            request_id__startswith="PAYOUT-CLAWBACK-"
        )
        # ⭐ P0-3：幂等键从 `PAYOUT-CLAWBACK-<单号>` 改成
        #    `PAYOUT-CLAWBACK-<绩效主键>-<settled_at 微秒>`（**这一轮结算的身份**）——
        #    同一张单的第二次退款因此得到新键、会再回冲一次（判据在
        #    `tests/test_settle_p0_refund.py::SecondRefundClawbackTests`）。
        #    ⚠️ 这里**不放宽成「有任意一条就行」**：键里必须含本行主键（否则退回单号形态也会绿）。
        self.assertIn(f"-{perf.pk}-", adjustment.request_id)
        self.assertLess(len(adjustment.request_id), 64)
        self.assertEqual(adjustment.kind, PerformanceAdjustment.Kind.FULL_REFUND)
        self.assertEqual(adjustment.amount, Decimal("-30.00"))
        self.assertEqual(adjustment.settle_status, SettleStatus.SETTLED)
        tx = WalletTransaction.objects.get(kind="earning_adjust")
        self.assertEqual(tx.ref_no, f"A{adjustment.pk}")
        self.assertEqual(tx.amount, Decimal("-30.00"))
        self.assertEqual(tx.balance_after, Decimal("0.00"))
        # 钱退回来了 ⇒ 这一轮不再算结过账（重做才能再入账 / 回池）。
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(perf.settled_amount)
        self.assertIsNone(perf.settled_at)

    def test_a_refund_interrupts_the_unpaid_withdrawal_instead_of_going_negative(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)
        # 先把钱提走（走产品路径：申请提现立即扣余额）。
        withdrawal = request_withdraw(self.taker, Decimal("30.00"))
        # ⛔ **新申请恒 `pending`**（前三单快路整段下线，spec §8.2）⇒ 本用例
        # 要考的是「已通过、未打款」那一档，所以在这里**显式**把它置成审核通过
        # （模拟财务在后台点「通过」；产品路径本身不再产出这一状态）。
        WithdrawOrder.objects.filter(pk=withdrawal.pk).update(
            state=WithdrawOrder.STATE_APPROVED,
            reviewed_at=timezone.now(),
            approved_at=timezone.now(),
        )
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_APPROVED)
        self.assertFalse(withdrawal.fast_lane)     # 新单不再有快路标记

        self._refund(order)

        # ⭐ 退款时把「已通过、未打款」的提现单中断 ⇒ 钱退回余额再冲账
        # ⇒ 余额 0，而不是旧口径的 -30。
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_INTERRUPTED)
        with self.assertRaises(WalletError) as caught:
            request_withdraw(self.taker, Decimal("1.00"))
        self.assertIn("可用余额不足", str(caught.exception))

    def test_a_paid_withdrawal_pushes_the_balance_negative(self):
        order = self._order()
        perf = self._main(order)
        self._complete(order)
        withdrawal = request_withdraw(self.taker, Decimal("30.00"))
        # 钱已经打出去了 ⇒ 中断不了，只能欠着。
        WithdrawOrder.objects.filter(pk=withdrawal.pk).update(
            state=WithdrawOrder.STATE_PAID
        )

        self._refund(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("-30.00"))
        with self.assertRaises(WalletError) as caught:
            request_withdraw(self.taker, Decimal("1.00"))
        self.assertIn("余额为负", str(caught.exception))

    def test_the_refunded_order_can_go_back_to_the_pool(self):
        """回冲 + 结账复位之后，R-10a 不能再把「退单 → 回池」挡死。"""
        order = self._order()
        self._main(order)
        self._complete(order)

        self._refund(order)
        apply_transition(
            order,
            OrderStatus.UNASSIGNED,
            nodes={"order.change_status"},
            allow_taker_release=True,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)


class WithdrawGateTests(PayoutFixture):
    def test_a_negative_balance_cannot_withdraw(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("-10.00")
        wallet.save(update_fields=["balance", "updated_at"])

        with self.assertRaises(WalletError) as caught:
            request_withdraw(self.taker, Decimal("1.00"))

        self.assertIn("余额为负", str(caught.exception))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("-10.00"))

    def test_a_zero_balance_cannot_withdraw(self):
        with self.assertRaises(WalletError) as caught:
            request_withdraw(self.taker, Decimal("1.00"))

        self.assertIn("可用余额不足", str(caught.exception))

    @override_settings(WITHDRAW_AUTO_TRANSFER=False)
    def test_a_positive_balance_still_withdraws_normally(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("10.00")
        wallet.save(update_fields=["balance", "updated_at"])

        order = request_withdraw(self.taker, Decimal("2.00"))

        self.assertEqual(order.amount, Decimal("2.00"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("8.00"))

    @override_settings(WITHDRAW_AUTO_TRANSFER=False)
    def test_a_one_cent_balance_can_be_withdrawn_in_full(self):
        """⭐ 2026-10-23：不设提现门槛 ⇒ 余额能全额提走（小程序审核的判据）。

        余额 0.03 提 0.03 必须成功；⛔ 别把 1 元下限抬回来（抬回来这条会红）。
        """
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("0.03")
        wallet.save(update_fields=["balance", "updated_at"])

        order = request_withdraw(self.taker, Decimal("0.03"))

        self.assertEqual(order.amount, Decimal("0.03"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
