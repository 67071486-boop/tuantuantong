"""结算 P0 修复第 2 / 3 条（2026-10-22）：部分退款不再双扣、第二次退款也回冲钱包。

依据：[`docs/plans/2026-10-22-settlement-p0-fixes.md`](../../docs/plans/2026-10-22-settlement-p0-fixes.md)
的 §2（P0-2）与 §3（P0-3）+ [`docs/reviews/2026-10-22-settlement-review.md`](
../../docs/reviews/2026-10-22-settlement-review.md) 的 P0-2 / P0-3。

## 两件被修的钱

1. **部分退款双扣**（P0-2）：`refunds._apply_sales_deductions` 对**未结账**的客服行先建一条
   `−退款额 × 提成率` 的冲减，末尾 `refresh_customer_estimate` 又按**新利润**重算
   `bonus_amount` ⇒ 同一笔减量进两次（`payable_net = bonus + 未结账调整`）。
   修法：未结账的行**先不建**冲减，钱由 `bonus_amount` 重算体现；只有「刷新算不出」
   （典型是客服档案 `FIXED` 模式）才回落到建冲减。
2. **第二次退款不回冲**（P0-3）：回冲的幂等键原来是 `PAYOUT-CLAWBACK-<单号>` ⇒ 同一张单的
   第二次退款命中 `exists()` 直接返回，钱包留着第二轮的钱。修法：键改成**这一轮结算身份**
   `PAYOUT-CLAWBACK-<绩效主键>-<settled_at 微秒>`。

## 判据钉在哪

一律钉**数据库行的值**（`AGENTS.md` §5-G）：`bonus_amount`、调整记录的 `(kind, amount)` 形状、
`payable_net`、钱包余额、`request_id` 的字面形状。⛔ 不断言「函数被调用了」。

⚠️ 期望值一律写**字面十进制值**（E-0024）：不从被测模块 import 常量当期望。
⚠️ 夹具一律**合成值**（E-0077）：单号 / 用户名 / 游戏名都是编的。

## 哪些用例在修前是红的（如实登记，⛔ 不把护栏当红灯）

| 用例 | 修前 | 说明 |
|---|---|---|
| `…test_an_unsettled_row_is_deducted_by_the_recomputed_bonus_only` | **红**（应付 `−8.00`） | P0-2 本体 |
| `…test_a_settled_and_an_unsettled_row_each_get_the_right_number` | **红**（未结账行 `−8.00`） | P0-2 混合形态 |
| `…test_a_fixed_mode_row_still_gets_the_explicit_deduction` | 绿（修复护栏） | 回落到旧做法的那条路：⛔ 不许为了修 P0-2 把它变成「一分不减」 |
| `…test_a_second_refund_after_a_redo_takes_the_money_back_again` | **红**（钱包停在 `60.00`） | P0-3 本体 |
| `…test_the_same_round_is_only_clawed_back_once` | 绿（回归网） | 同一轮重复回冲只扣一次，今天就是对的 |
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource, CommissionMode
from apps.accounts.models import StaffProfile, TakerWallet
from apps.accounts.payout import payout_completed_order
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderRefund,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.performance import set_taker
from apps.business.refunds import refund_set_total
from apps.business.settlement_amount import payable_net
from tests.order_rules_helpers import release_taker

#: 客服提成率（**利润**百分比）与打手提成率（订单价百分比）。夹具数字与计划 §2 的代数一致：
#: `A=100.00`（订单总价）、`T=60.00`（打手 100 × 60%）、客服 `(100 − 60) × 40% = 16.00`。
SALES_RATE = Decimal("40.00")
TAKER_RATE = Decimal("60.00")

#: 部分退款的金额（计划 §2 判据 1 的字面值）。
REFUND = Decimal("30.00")

#: 完成迁移要求的节点门（与该单的实际角色无关：本文件只调领域函数）。
COMPLETED_NODES = {"order.change_status_completed"}
CHANGE_STATUS_NODES = {"order.change_status"}


class _P0RefundFixture(TenantTestCase):
    """游戏 + 两个客服 + 一个 App 注册打手（自动结账只对 App 注册打手生效，D-0279）。"""

    def setUp(self):
        self.game = Game.objects.create(name="结算P0测试游戏")
        self.sales_user = User.objects.create_user(username="p0-sales")
        StaffProfile.objects.create(
            user=self.sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        # ⚠️ 「混合行」那条用例需要**两个**客服：`uniq_performance_order_user`
        #    让同一单同一人的第二行插不进去。
        self.second_sales_user = User.objects.create_user(username="p0-sales-2")
        StaffProfile.objects.create(
            user=self.second_sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.taker_user = User.objects.create_user(username="p0-taker")
        StaffProfile.objects.create(
            user=self.taker_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
            # 自动入账（`payout_completed_order`）只对 App 注册打手生效（D-0279）。
            account_source=AccountSource.APP,
        )
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _order(
        self,
        *,
        status=OrderStatus.COMPLETED,
        total="100.00",
        hall="100.00",
        refund_fee="0.00",
        sales_user=None,
    ):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026102200000{self._seq:07d}",
            status=status,
            total_price=Decimal(total),
            hall_price=Decimal(hall),
            refund_fee=Decimal(refund_fee),
            sales_user=sales_user,
        )

    def _taker(self, order, *, temp="60.00", bonus="60.00"):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=self.taker_user,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
        )

    def _sales(self, order, *, user=None, bonus="16.00", settled=False):
        """客服行：默认 `bonus_amount = 16.00`（`(100 − 60) × 40%`），未结账。"""
        performance = Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=user or self.sales_user,
            temp_amount=Decimal("16.00"),
            bonus_amount=Decimal(bonus),
            commission_rate=SALES_RATE,
            review_status=ReviewStatus.APPROVED,
        )
        if settled:
            Performance.objects.filter(pk=performance.pk).update(
                settle_status=SettleStatus.SETTLED,
                settled_at=timezone.now(),
                settled_amount=Decimal(bonus),
            )
            performance.refresh_from_db()
            # 夹具自证：这一行**真的**是「已结账」（否则下面的闸门断言没有判别力）。
            self.assertTrue(performance.is_settled)
        return performance

    def _adjustments(self, performance):
        return [
            (row.kind, row.amount)
            for row in PerformanceAdjustment.objects.filter(
                performance=performance
            ).order_by("id")
        ]

    def _payable(self, performance):
        """P4-7 结账口径：`bonus_amount − 扣点 + Σ未结账调整`（与 `settle_performances` 同）。"""
        return payable_net(
            performance,
            PerformanceAdjustment.objects.filter(
                performance=performance, settle_status=SettleStatus.UNSETTLED
            ),
        )

    # ── 状态迁移 ──────────────────────────────────────────────────────────
    def _complete(self, order):
        """推进「已完成」，并**显式**跑一次工资入账（2026-10-23 起不再自动）。

        ⛔ `apps.business.order_status.apply_transition` 已不调
        `payout_completed_order`（spec `2026-10-23-taker-income-and-early-settlement.md`
        §3.1：钱包不再收订单绩效）⇒ 本文件的用例测的是**入账函数之后**那几段语义
        （结账 / 冲账 / 回退 / 上家跟退），所以在这一跳里显式补上。
        「完成单不再自动入账」那条新口径的判据在
        `tests/test_app_income_api.py::CompletionDoesNotCreditWalletTests`。
        """
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        payout_completed_order(order)

    def _refund(self, order, actor="cs-1"):
        apply_transition(order, OrderStatus.REFUNDED, nodes=CHANGE_STATUS_NODES, actor=actor)

    def _redo_round(self, order):
        """退单 → 回池 → 第二轮 `/assign` → `/submit` → `/review` 的函数体（同既有夹具口径）。"""
        release_taker(order, nodes=CHANGE_STATUS_NODES, actor="cs-1")
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=CHANGE_STATUS_NODES, actor="cs-2")
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        set_taker(main, self.taker_user)
        apply_transition(
            order, OrderStatus.PENDING_REVIEW, nodes=CHANGE_STATUS_NODES, actor="taker-2"
        )
        main.bonus_amount = Decimal("60.00")
        main.review_status = ReviewStatus.PENDING
        main.save(update_fields=["bonus_amount", "review_status", "updated_at"])
        main.review_status = ReviewStatus.APPROVED
        main.save(update_fields=["review_status", "updated_at"])
        self._complete(order)


class PartialRefundSingleDeductionTests(_P0RefundFixture):
    """P0-2：未结账的客服行**只**由 `bonus_amount` 重算体现减量，⛔ 不再多一条冲减。"""

    def test_an_unsettled_row_is_deducted_by_the_recomputed_bonus_only(self):
        """未结账客服行部分退 `30.00` ⇒ 应付 **`4.00`**（⛔ 不是 `−8.00`）。

        代数：净实收 `100 − 30 = 70`、打手 `60` ⇒ 利润 `10` ⇒ `10 × 40% = 4.00`。
        修前：`bonus` 同样被刷成 `4.00`，**另加**一条 `−12.00`（`30 × 40%`）的冲减
        ⇒ `payable_net = 4.00 − 12.00 = −8.00`（同一笔钱扣两次）。

        **杀法**：把「未结账行先不建冲减」那段改回去（无条件建）⇒ 本用例红。
        """
        order = self._order(sales_user=self.sales_user)
        sales = self._sales(order)
        self._taker(order)

        refund_set_total(order, REFUND, "退差价", "p0-partial-1", "cs-1")

        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("4.00"))     # 钱由重算体现
        self.assertEqual(self._adjustments(sales), [])            # ⛔ 没有独立的冲减行
        self.assertEqual(self._payable(sales), Decimal("4.00"))   # ⛔ 不是 −8.00

    def test_a_settled_and_an_unsettled_row_each_get_the_right_number(self):
        """同一单「已结账行 + 未结账行」⇒ 两个数**各自**正确（`4.00` / `4.00`，但路径不同）。

        - 已结账行：`bonus` **冻结**在 `16.00`（Q1：一个字节都不动）+ 一条 `−12.00` 的冲减
          ⇒ 应付 `16.00 − 12.00 = 4.00`（**这条是修前就对的回归网**，⛔ 别为修 P0-2 把它改坏）；
        - 未结账行：`bonus` 重算成 `4.00`、链上**零行** ⇒ 应付 `4.00`。

        ⚠️ 夹具自证：`refresh_customer_estimate` 只刷 `.first()`（`Meta.ordering` 是
        `-created_at, -id`，即**最新**那条）⇒ 必须先建已结账行、后建未结账行，
        否则本用例考的是「刷新刷到了已结账行、早退成 None」那条路，**判别力会变形**。
        """
        order = self._order(sales_user=self.second_sales_user)
        settled = self._sales(order, user=self.sales_user, bonus="16.00", settled=True)
        unsettled = self._sales(order, user=self.second_sales_user, bonus="16.00")
        self._taker(order)
        first_sales_row = (
            Performance.objects.filter(
                order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
            )
            .first()
        )
        self.assertEqual(first_sales_row.pk, unsettled.pk)        # 夹具自证

        refund_set_total(order, REFUND, "退差价", "p0-partial-mixed", "cs-1")

        settled.refresh_from_db()
        unsettled.refresh_from_db()
        # 已结账：冻结 + 一条冲减（`30 × 40%`）。
        self.assertEqual(settled.bonus_amount, Decimal("16.00"))
        self.assertEqual(
            self._adjustments(settled), [("PARTIAL_REFUND", Decimal("-12.00"))]
        )
        self.assertEqual(self._payable(settled), Decimal("4.00"))
        # 未结账：重算到 `4.00`、链上零行（修前这里会多一条 `−12.00` ⇒ 应付 `−8.00`）。
        self.assertEqual(unsettled.bonus_amount, Decimal("4.00"))
        self.assertEqual(self._adjustments(unsettled), [])
        self.assertEqual(self._payable(unsettled), Decimal("4.00"))

    def test_a_fixed_mode_row_still_gets_the_explicit_deduction(self):
        """客服档案 `FIXED` 模式 ⇒ 刷新**算不出**（`CommissionError`）⇒ 回落到建冲减：仍减 `12.00`。

        这一条是修 P0-2 的**护栏**（修前就绿）：⛔ 不许多跨一步把所有未结账行都跳过
        ——那样这类客服（算不出提成率的人是**算不出**，不是「不该扣」）从此退款一分不减。

        代数：`bonus` 保持 `16.00`（算不出 ⇒ 不重算）+ 一条 `−12.00` ⇒ 应付 `4.00`。
        """
        StaffProfile.objects.filter(user=self.sales_user).update(
            commission_mode=CommissionMode.FIXED,
            commission_value=Decimal("10.00"),
        )
        order = self._order(sales_user=self.sales_user)
        sales = self._sales(order)
        self._taker(order)

        refund_set_total(order, REFUND, "退差价", "p0-partial-fixed", "cs-1")

        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("16.00"))    # 算不出 ⇒ 不重算
        self.assertEqual(
            self._adjustments(sales), [("PARTIAL_REFUND", Decimal("-12.00"))]
        )
        # ⚠️ 这三条是从 `test_refund_partial` 旧期望里搬过来的锚（P0-2 之后**只有回落这一档**
        #    还会建 `PARTIAL_REFUND` 行 ⇒ 这几个字段的判据跟着搬到这里，⛔ 不是丢掉了）：
        #    新调整挂**设置事件行**、操作人是退款人、自身是「未结账」（进下一次结账）。
        row = PerformanceAdjustment.objects.get(performance=sales)
        self.assertEqual(row.refund_id, OrderRefund.objects.get(order=order).id)
        self.assertEqual(row.operator, "cs-1")
        self.assertEqual(row.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(self._payable(sales), Decimal("4.00"))


class SecondRefundClawbackTests(_P0RefundFixture):
    """P0-3：回冲的幂等键按**轮**派生 ⇒ 第二次退款也要把钱扣回来。"""

    def _clawback_keys(self):
        return list(
            PerformanceAdjustment.objects.filter(
                request_id__startswith="PAYOUT-CLAWBACK-"
            )
            .order_by("id")
            .values_list("request_id", flat=True)
        )

    def test_a_second_refund_after_a_redo_takes_the_money_back_again(self):
        """完成 → 入账 → 退款 → 重做完成 → 再退款 ⇒ 钱包回 **`0.00`**。

        修前：第二次回冲算出的键与第一次相同（`PAYOUT-CLAWBACK-<单号>`）⇒ `exists()` 命中、
        直接返回 ⇒ 钱包**停在第二轮那 `60.00`**（打手白拿）。
        修后：键带**这一轮**的 `settled_at` ⇒ 两次退款两把不同的键、各扣一次。
        """
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        self._taker(order)

        self._complete(order)
        wallet = TakerWallet.objects.get(user=self.taker_user)
        self.assertEqual(wallet.balance, Decimal("60.00"))        # 阳性对照：自动入账真的发生

        self._refund(order)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))         # 第一轮回冲

        self._redo_round(order)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("60.00"))        # 前置：第二轮又入账了

        self._refund(order)
        wallet.refresh_from_db()
        # ⭐ 今天（修前）停在这一行的 `60.00` 上：第二次回冲被旧幂等键挡掉了。
        self.assertEqual(wallet.balance, Decimal("0.00"))

        keys = self._clawback_keys()
        self.assertEqual(len(keys), 2)                            # 两轮各一条回冲
        self.assertEqual(len(set(keys)), 2)                       # 键**不同**（本条的核心）
        for key in keys:
            # 形状 = `PAYOUT-CLAWBACK-<绩效主键>-<settled_at 微秒>`，且必须 < 64（列宽）。
            self.assertRegex(key, r"^PAYOUT-CLAWBACK-[0-9]+-[0-9]{20}$")
            self.assertLess(len(key), 64)

    def test_the_same_round_is_only_clawed_back_once(self):
        """**回归网**（修前就绿）：同一轮重复调回冲 ⇒ 只扣一次、不多写第二条。

        机制不是幂等键，而是**状态**：第一次回冲把该行的结账痕迹复位成未结账
        ⇒ 第二次进来在 `settle_status != 已结账` 那一句就返回（`_claw_back_auto_paid_taker`
        的出口判据）。本条钉「加键改动没有把这条状态护栏弄坏」。
        """
        from apps.business.refunds import _claw_back_auto_paid_taker

        order = self._order(status=OrderStatus.PENDING_REVIEW)
        self._taker(order)
        self._complete(order)

        self._refund(order)
        # 第二轮重做之前再手工调一次（同一轮的重试形态）。
        self.assertIsNone(_claw_back_auto_paid_taker(order, "cs-1"))

        wallet = TakerWallet.objects.get(user=self.taker_user)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertEqual(len(self._clawback_keys()), 1)
