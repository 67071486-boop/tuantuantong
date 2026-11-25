"""D-0357（拆单组协作重设计）的**绩效组闸网**（spec §7.3 / G11-G15）。

判据（逐字来自 spec §3.4）：

- **G11 子单 SALES 恒 0**：组闸落在 `recompute_customer_commission` 与
  `refresh_customer_estimate` **两个本体**的开头 ⇒ 完成钩子 / 预计跟随 /
  `/orders/batch/recompute-commission/` 三条路一次堵死，负数提成无处可生；
- **G12 主单生效闸**：组未齐 ⇒ 不写 bonus、temp 维持 0；组齐那一刻由两路触发
  （末单完成那一跳 / G8 出组收口）；
- **G13 减项 = 组口径**：`taker_bonus_base`——非组内单 = 本单 MAIN 行 bonus
  （现状不变）；组内主单 = 全组在组成员 MAIN 行 bonus 之和；预计侧不需要组 helper
  （temp 减项仍取本单 MAIN 行）；
- **G14 跨单跟随唯一咽喉**：只改 `refresh_customer_estimate` 本体——对组内成员
  执行时末尾追加「刷新主单」（防递归：成员==主单跳过）；
- **G15 跨单触发吞 `CommissionError` + 响亮日志**：末单完成绝不被主单客服的提成
  配置挡住；主单自己完成且组齐（普通路）时照旧整笔回滚。

**先红证明（§7 的「弄红」义务）**：G11 三条用例在实现前是红的——现状子单完成 /
刷新 / 重算会把「`total_price=0 − 打手提成 < 0`」的负数客服提成**写进库**
（spec §2.3 点名的现状缺陷）；G12/G13 的末单用例在实现前红——主单提成要么不生效
要么按「无打手成本」足额生效。本网随实现同批落地时已全绿 ⇒ 提交信息里登记这一点。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.commission import CommissionError
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from apps.business.order_status import apply_transition
from apps.business.performance_amount import commission_amount
from apps.business.settlement_amount import taker_bonus_base
from apps.business.settlement_hooks import recompute_customer_commission

GROUP = "75000000000000000001"
NO_MAIN = "20261101000000000501"
NO_A = "20261101000000000502"
NO_B = "20261101000000000503"
#: 组外的**普通单**（G13 的非组支对照）。
NO_PLAIN = "20261101000000000509"

COMPLETE_NODES = {"order.change_status", "order.change_status_completed"}

#: 客服提成率 40%（利润百分比）。
RATE = Decimal("40.00")


class _SplitPerfFixture(TenantTestCase):
    """一组三单（主单 + 两子单）+ 一张组外普通单；双打手不同预计额。"""

    def setUp(self):
        self.game = Game.objects.create(name="拆单绩效夹具游戏")
        users = get_user_model().objects
        self.sales = users.create_user(username="sp-sales", password="pw-sales")
        profile, _ = StaffProfile.objects.get_or_create(user=self.sales)
        profile.commission_mode = CommissionMode.PROFIT_PERCENT
        profile.commission_value = RATE
        profile.save()
        self.taker_a = users.create_user(username="sp-taker-a", password="pw-a")
        self.taker_b = users.create_user(username="sp-taker-b", password="pw-b")

        self.main = self._order(NO_MAIN, total_price=Decimal("300.00"))
        self.sub_a = self._order(NO_A)
        self.sub_b = self._order(NO_B)
        self.plain = self._order(NO_PLAIN, total_price=Decimal("200.00"))
        for order in (self.main, self.sub_a, self.sub_b):
            order.split_id = GROUP
            order.save(update_fields=["split_id"])

        self._perf(self.main, EmpType.MAIN, user=None, amount=Decimal("0.00"))
        self._perf(self.sub_a, EmpType.MAIN, user=self.taker_a, amount=Decimal("100.00"))
        self._perf(self.sub_b, EmpType.MAIN, user=self.taker_b, amount=Decimal("50.00"))
        self._perf(self.plain, EmpType.MAIN, user=self.taker_a, amount=Decimal("60.00"))
        for order in (self.main, self.sub_a, self.sub_b, self.plain):
            self._perf(order, EmpType.SALES, user=self.sales, amount=Decimal("0.00"))

    def _order(self, order_no, *, total_price=Decimal("0.00")):
        return Order.objects.create(
            game=self.game, order_no=order_no,
            status=OrderStatus.PENDING_REVIEW,
            total_price=total_price, sales_user=self.sales,
        )

    def _perf(self, order, emp_type, *, user, amount):
        return Performance.objects.create(
            order=order, emp_type=emp_type, user=user,
            temp_amount=amount, bonus_amount=Decimal("0.00"),
        )

    def complete(self, order):
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETE_NODES)

    def sales_row(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.SALES)


class ChildSalesRowStaysZeroTests(_SplitPerfFixture):
    """**G11 子单 SALES 恒 0**：完成 / 刷新 / 重算三条路（先红：现状落负数）。"""

    def test_completing_a_child_leaves_its_sales_row_at_zero(self):
        """子单完成 ⇒ 自己的 SALES 行 temp/bonus 仍 0（实现前这里会写 `-40.00`）。"""
        self.complete(self.sub_a)
        row = self.sales_row(self.sub_a)
        self.assertEqual(row.temp_amount, Decimal("0.00"))
        self.assertEqual(row.bonus_amount, Decimal("0.00"))
        self.assertIsNone(row.review_status)  # 钱没生效过 ⇒ 审核态不动

    def test_the_estimate_follow_path_also_leaves_the_child_at_zero(self):
        """改子单发单价（完成前的跟随路径）⇒ SALES 行仍 0（刷新本体的组闸）。"""
        from apps.business.order_writes import change_hall_price

        outcome = change_hall_price(
            self.sub_a, price=Decimal("80.00"), dry_run=False,
            ctx=_ctx(),
        )
        self.assertTrue(outcome.ok, outcome.msg)
        row = self.sales_row(self.sub_a)
        self.assertEqual(row.temp_amount, Decimal("0.00"))
        self.assertEqual(row.bonus_amount, Decimal("0.00"))

    def test_the_recompute_endpoint_gate_reports_children_as_noop(self):
        """批量重算端点的两个 dry_run 档对子单**同答案**：原样返回（没有可算的东西）。"""
        self.complete(self.sub_a)
        row = self.sales_row(self.sub_a)
        first = recompute_customer_commission(self.sub_a, dry_run=True)
        second = recompute_customer_commission(self.sub_a, dry_run=False)
        self.assertEqual(first.pk, row.pk)
        self.assertEqual(second.pk, row.pk)
        row.refresh_from_db()
        self.assertEqual(row.temp_amount, Decimal("0.00"))
        self.assertEqual(row.bonus_amount, Decimal("0.00"))


class MainCommissionGateTests(_SplitPerfFixture):
    """**G12 主单生效闸 + G13 组减项 + G15 两档回滚语义**。"""

    def test_main_completing_with_the_group_incomplete_writes_nothing(self):
        """主单先完成（组未齐）⇒ SALES bonus 0、temp 0、rate/review 不动。"""
        self.complete(self.main)
        row = self.sales_row(self.main)
        self.assertEqual(row.bonus_amount, Decimal("0.00"))
        self.assertEqual(row.temp_amount, Decimal("0.00"))
        self.assertIsNone(row.commission_rate)
        self.assertIsNone(row.review_status)

    def test_the_last_child_completing_activates_the_main_commission(self):
        """末单（子单）完成 ⇒ 主单 SALES 生效：减项 == 全组和、钉字面 `60.00`。

        300 −（甲 100 + 乙 50）= 150 利润 × 40% = 60.00。实现前这里要么不生效
        （闸不存在时按「主单自己 MAIN=0」足额生效成 120.00）——两条都红。
        """
        self.complete(self.main)
        self.complete(self.sub_a)
        self.complete(self.sub_b)
        # 打手照旧「自己那单完成即生效」（口径 10 的前半，钉住不被组闸误伤）
        self.assertEqual(
            Performance.objects.get(order=self.sub_a, emp_type=EmpType.MAIN).bonus_amount,
            Decimal("100.00"),
        )
        self.assertEqual(
            Performance.objects.get(order=self.sub_b, emp_type=EmpType.MAIN).bonus_amount,
            Decimal("50.00"),
        )
        row = self.sales_row(self.main)
        self.assertEqual(row.bonus_amount, Decimal("60.00"))
        self.assertEqual(row.review_status, ReviewStatus.APPROVED)
        # temp（预计）的减项照旧取**本单** MAIN 行（G13：预计侧不需要组 helper）
        # ⇒ (300 − 0) × 40% = 120.00。这是 spec 的字面口径，钉住防漂。
        self.assertEqual(row.temp_amount, Decimal("120.00"))

    def test_cross_order_commission_error_is_swallowed_and_logged_loudly(self):
        """**G15 ⓑ**：末跳触发遇 `CommissionError` ⇒ 吞掉、主单留 0、响亮日志、最后一单照常完成。"""
        # 主单档案改成「订单价百分比」⇒ PROFIT_PERCENT 算式抛 CommissionError
        profile = StaffProfile.objects.get(user=self.sales)
        profile.commission_mode = CommissionMode.ORDER_PERCENT
        profile.save()

        self.complete(self.main)      # 组未齐 ⇒ 组闸先于算式早退，不抛
        self.complete(self.sub_a)
        self.complete(self.sub_b)     # 末跳：跨单触发对主单重算 ⇒ 抛 ⇒ 吞
        self.sub_b.refresh_from_db()
        self.assertEqual(self.sub_b.status, OrderStatus.COMPLETED)  # 最后一单不被挡
        row = self.sales_row(self.main)
        self.assertEqual(row.bonus_amount, Decimal("0.00"))
        self.assertIsNone(row.review_status)
        log = OperationLog.objects.get(action="order.split_main_commission_error")
        self.assertEqual(log.order_no, NO_MAIN)
        self.assertIn(NO_MAIN, log.summary)
        self.assertIn("请到结算页核查", log.summary)

    def test_the_main_completing_last_with_a_broken_rate_still_rolls_back(self):
        """普通路（主单自己完成且组齐）的 `CommissionError` 照旧整笔回滚（现状语义不变）。"""
        from django.db import transaction

        profile = StaffProfile.objects.get(user=self.sales)
        profile.commission_mode = CommissionMode.ORDER_PERCENT
        profile.save()
        self.complete(self.sub_a)
        self.complete(self.sub_b)     # 末跳是子单 ⇒ 跨单触发对主单重算 ⇒ 吞（不回滚）
        # ⚠️ 钩子的抛出语义 = 「调用方的 atomic 里抛 ⇒ 状态与钩子写的行一起回滚」
        # （settlement_hooks 模块 docstring 的合约）。用例照合约包一层 atomic：
        # 异常穿出 atomic ⇒ 那一层回滚 ⇒ 状态留在 PENDING_REVIEW。
        with self.assertRaises(CommissionError):
            with transaction.atomic():
                self.complete(self.main)
        self.main.refresh_from_db()
        self.assertEqual(self.main.status, OrderStatus.PENDING_REVIEW)
        row = self.sales_row(self.main)
        self.assertEqual(row.bonus_amount, Decimal("0.00"))

    def test_a_price_change_on_a_completed_child_follows_into_the_main(self):
        """**G14 跨单跟随**：全组完成后改子单发单价 ⇒ 主单 SALES 跟着重算。

        子单甲 100 → 80 ⇒ 减项变 (80 + 50) = 130 ⇒ 300 − 130 = 170 × 40% = 68.00。
        实现前这里红（跟随不会跨单传到主单）。
        """
        from apps.business.order_writes import change_hall_price

        self.complete(self.main)
        self.complete(self.sub_a)
        self.complete(self.sub_b)
        self.assertEqual(self.sales_row(self.main).bonus_amount, Decimal("60.00"))

        outcome = change_hall_price(
            self.sub_a, price=Decimal("80.00"), dry_run=False, ctx=_ctx(),
        )
        self.assertTrue(outcome.ok, outcome.msg)
        row = self.sales_row(self.main)
        self.assertEqual(row.bonus_amount, Decimal("68.00"))


class TakerBonusBaseTests(_SplitPerfFixture):
    """**G13 helper 的两支**（`settlement_amount.taker_bonus_base` 的单元口径）。"""

    def test_a_plain_order_deducts_only_its_own_main_bonus(self):
        self.complete(self.plain)  # 激活：plain MAIN bonus := 60.00
        self.assertEqual(taker_bonus_base(self.plain), Decimal("60.00"))

    def test_the_group_main_deducts_the_sum_of_all_active_members(self):
        self.complete(self.sub_a)  # 甲 100
        self.assertEqual(taker_bonus_base(self.main), Decimal("100.00"))
        self.complete(self.sub_b)  # 乙 50 ⇒ 全组和 150
        self.assertEqual(taker_bonus_base(self.main), Decimal("150.00"))

    def test_a_child_order_gets_its_own_row_not_the_group_sum(self):
        """子单**不是**主单：helper 对它走「本单自己」那支（组口径只挂主单）。"""
        self.complete(self.sub_a)
        self.assertEqual(taker_bonus_base(self.sub_a), Decimal("100.00"))

    def test_a_missing_main_row_is_zero_not_a_500(self):
        """容错口径照旧：无打手行 ⇒ `0.00`（评审后修复轮 I-4 的现状不变）。"""
        Performance.objects.filter(order=self.plain, emp_type=EmpType.MAIN).delete()
        self.assertEqual(taker_bonus_base(self.plain), Decimal("0.00"))


class CommissionFormulaParityTests(_SplitPerfFixture):
    """算式读数对照：`commission_amount(150, 40) == 60.00`（钉量纲，防「率语义」漂）。"""

    def test_the_rate_semantics_behind_the_literal_expectations(self):
        self.assertEqual(
            commission_amount(Decimal("150.00"), RATE), Decimal("60.00")
        )
        self.assertEqual(
            commission_amount(Decimal("200.00"), RATE), Decimal("80.00")
        )


def _ctx():
    """域层直调的写上下文（机器形态：无认证态 ⇒ `system=True` 值级声明由 record 自判）。"""
    from apps.business.order_writes import WriteContext

    return WriteContext()
