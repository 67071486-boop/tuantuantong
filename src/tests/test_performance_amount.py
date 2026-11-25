"""B1-2：金额纯函数（spec §6.1 / §10 #4；P4 的 B2、B3）。

三件事，各自只有一处实现：

1. **生效接单价** = `Performance.taker_price` 优先，为空才用 `Order.hall_price`（spec §6.3）。
   ⚠️ 大厅列表侧的同一事实是 **SQL 注解**（`selectors.effective_taker_price_expression()`），
   所以这里有一条**交叉核对**测试：同一行，Python 读到的值必须等于那条注解算出的值
   ——两个表示法允许存在（一个给列表、一个给单行），但**必须对得上**。
2. **佣金**：`base × percent / 100`，结果量化到分（`ROUND_HALF_UP`，与 PostgreSQL `numeric`
   的「四舍五入远离零」一致）。
3. **扣点额**（P4 的 B3 要求抽成可复用函数）：`mode "0"` 按比例（上限 100%）、
   `mode "1"` 固定金额（上限 = 预计绩效总计）、`""`/未设 ⇒ 0。

⚠️ **金额可以为负**（D-0015）：扣点额**只设上界**（对标物的自校验原文就是单边的：
「扣点比例不能超过100%」「扣点金额不能超过预计绩效总计」），负数 = **冲抵/加钱**，
**不得**顺手加 `≥ 0`。有一条测试专门钉住「负数被接受」。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.performance_amount import (
    AmountError,
    commission_amount,
    deduct_amount,
    effective_taker_price,
)
from apps.core.error_codes import ErrorCode


class _FixturesMixin:
    def _order_perf(self, *, hall_price="88.00", taker_price=None, no=1):
        game = Game.objects.create(name=f"原神{no}")
        order = Order.objects.create(
            game=game, order_no=f"2026092000000000{no:04d}",
            status=OrderStatus.IN_PROGRESS, hall_price=hall_price,
        )
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("0.00"),
            taker_price=taker_price,
        )
        return order, perf


class EffectiveTakerPriceTests(_FixturesMixin, TenantTestCase):
    def test_override_wins(self):
        """绩效上有覆盖价 ⇒ 用它，**不受**订单默认价影响。"""
        _, perf = self._order_perf(hall_price="100.00", taker_price=Decimal("300.00"))
        self.assertEqual(effective_taker_price(perf), Decimal("300.00"))

    def test_falls_back_to_order_default(self):
        _, perf = self._order_perf(hall_price="100.00", taker_price=None)
        self.assertEqual(effective_taker_price(perf), Decimal("100.00"))

    def test_return_type_is_decimal_even_when_the_instance_is_not_refreshed(self):
        """⚠️ **返回值必须是 `Decimal`，与调用方怎么拿到实例无关**。

        本用例的夹具刻意**不从库里刷新**：`Order.objects.create(hall_price="100.00")` 之后，
        内存里的 `order.hall_price` 仍是 **`str`**（实测；Django 不会把输入改写回来）。
        若函数直接把它返回，`commission_amount` 会 `TypeError: unsupported operand type(s)`
        ——而生产路径上「未刷新实例」随时可能出现（`/throw-to-hall` 刚 `save()` 完就接着算）。
        """
        _, perf = self._order_perf(hall_price="100.00", taker_price=None)
        self.assertIsInstance(perf.order.hall_price, str)      # 前置哨兵：确实是未刷新的
        self.assertIsInstance(effective_taker_price(perf), Decimal)
        self.assertIsInstance(commission_amount(effective_taker_price(perf),
                                                Decimal("20.00")), Decimal)

    def test_override_of_zero_is_not_treated_as_missing(self):
        """⚠️ `0` 是**合法金额**（D-0015）：覆盖价写 0 ⇒ 生效价就是 0，不能回落默认价。

        判据必须是 `is not None` 而不是真值（E-0021 的同族）。
        """
        _, perf = self._order_perf(hall_price="100.00", taker_price=Decimal("0.00"))
        self.assertEqual(effective_taker_price(perf), Decimal("0.00"))

    def test_both_missing_raises_a_business_error(self):
        """两边都没有价 ⇒ 拒绝（不是算成 0）。与 `/claim` 的 NULL 价分支同一口径。"""
        _, perf = self._order_perf(hall_price=None, taker_price=None)
        with self.assertRaises(AmountError) as ctx:
            effective_taker_price(perf)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertIn("接单价", ctx.exception.message)

    def test_python_reader_agrees_with_the_sql_annotation(self):
        """**两个表示法必须对得上**：单行读（本函数）与列表注解（`selectors`）。

        没有这条，`taker_price` 的覆盖规则会有两套实现，且**只在列表或只在单行**生效
        ——那正是 Ruling 44（「展示与筛选同源」）要防的分叉形状。
        """
        from apps.business.selectors import (
            EFFECTIVE_TAKER_PRICE,
            effective_taker_price_expression,
        )

        # 用显式序号当订单号（**不要**用 hash() 造号：进程内外不一致，还可能撞号）
        for idx, taker in enumerate((Decimal("300.00"), None, Decimal("0.00")), start=1):
            order, perf = self._order_perf(
                hall_price="100.00", taker_price=taker, no=idx
            )
            annotated = (
                Order.objects.filter(pk=order.pk)
                .annotate(**{EFFECTIVE_TAKER_PRICE: effective_taker_price_expression()})
                .values_list(EFFECTIVE_TAKER_PRICE, flat=True)
                .get()
            )
            self.assertEqual(effective_taker_price(perf), annotated)


class CommissionAmountTests(TenantTestCase):
    def test_twenty_percent_of_88(self):
        """断**具体十进制值**——不断「被写入了」（`bonus_amount` 默认就是 0.00，写 0 也会绿）。"""
        self.assertEqual(commission_amount(Decimal("88.00"), Decimal("20.00")), Decimal("17.60"))

    def test_rounds_half_up_to_cents(self):
        """`0.05 × 10 / 100 = 0.005` ⇒ `0.01`（`ROUND_HALF_UP`，与 PostgreSQL `numeric` 一致）。"""
        self.assertEqual(commission_amount(Decimal("0.05"), Decimal("10.00")), Decimal("0.01"))

    def test_result_is_quantized_to_two_places(self):
        """`88.00 × 33.33 / 100 = 29.3304` ⇒ `29.33`（不留 4 位小数进库）。"""
        self.assertEqual(commission_amount(Decimal("88.00"), Decimal("33.33")), Decimal("29.33"))

    def test_zero_percent_is_zero(self):
        self.assertEqual(commission_amount(Decimal("88.00"), Decimal("0.00")), Decimal("0.00"))

    def test_full_percent_is_the_base(self):
        self.assertEqual(commission_amount(Decimal("88.00"), Decimal("100.00")), Decimal("88.00"))

    def test_negative_base_is_allowed(self):
        """D-0015：基数（接单价）可以是负数（退款 / 冲抵）⇒ 佣金也跟着为负。"""
        self.assertEqual(commission_amount(Decimal("-88.00"), Decimal("20.00")), Decimal("-17.60"))


class DeductAmountTests(TenantTestCase):
    BASE = Decimal("100.00")

    def test_unset_mode_is_zero(self):
        self.assertEqual(
            deduct_amount(mode="", value=Decimal("30.00"), base=self.BASE), Decimal("0.00")
        )

    def test_zero_value_is_zero(self):
        self.assertEqual(
            deduct_amount(mode="0", value=Decimal("0.00"), base=self.BASE), Decimal("0.00")
        )

    def test_percent_mode(self):
        self.assertEqual(
            deduct_amount(mode="0", value=Decimal("20.00"), base=self.BASE), Decimal("20.00")
        )

    def test_percent_mode_boundary_100_is_allowed(self):
        self.assertEqual(
            deduct_amount(mode="0", value=Decimal("100.00"), base=self.BASE), Decimal("100.00")
        )

    def test_percent_mode_above_100_is_rejected(self):
        """对标物自校验原文：「扣点比例不能超过100%」（spec §6.1，**读码**）。"""
        with self.assertRaises(AmountError) as ctx:
            deduct_amount(mode="0", value=Decimal("100.01"), base=self.BASE)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertIn("100", ctx.exception.message)

    def test_fixed_mode(self):
        self.assertEqual(
            deduct_amount(mode="1", value=Decimal("30.00"), base=self.BASE), Decimal("30.00")
        )

    def test_fixed_mode_exactly_equal_to_base_is_allowed(self):
        self.assertEqual(
            deduct_amount(mode="1", value=self.BASE, base=self.BASE), Decimal("100.00")
        )

    def test_fixed_mode_above_base_is_rejected(self):
        """对标物自校验原文：「扣点金额不能超过预计绩效总计」（spec §6.1）。"""
        with self.assertRaises(AmountError) as ctx:
            deduct_amount(mode="1", value=Decimal("100.01"), base=self.BASE)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertIn("预计绩效", ctx.exception.message)

    def test_negative_deduct_is_allowed(self):
        """⚠️ **D-0015：负数 = 冲抵 / 加钱**。对标物的自校验是**单边**的（只有上界），
        所以这里**不设下界**——谁加了 `≥ 0`，本用例变红。
        """
        self.assertEqual(
            deduct_amount(mode="1", value=Decimal("-30.00"), base=self.BASE), Decimal("-30.00")
        )
        self.assertEqual(
            deduct_amount(mode="0", value=Decimal("-20.00"), base=self.BASE), Decimal("-20.00")
        )
        # 负比例也不设下界（上界才是对标物原文的那一条）
        self.assertEqual(
            deduct_amount(mode="0", value=Decimal("-200.00"), base=self.BASE),
            Decimal("-200.00"),
        )

    def test_unknown_mode_is_rejected(self):
        """畸形 `mode` 必须响亮拒绝（`/adjust` 的序列化器是第一道，这里是第二道）。"""
        with self.assertRaises(AmountError) as ctx:
            deduct_amount(mode="9", value=Decimal("10.00"), base=self.BASE)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
