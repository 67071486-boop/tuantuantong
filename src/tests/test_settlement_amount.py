"""P4-4：结算金额纯函数（净实收额 / 利润 / 应付净额 + 负号语义）。

**为什么单独一个文件、且不经 HTTP**：金额算错**不会报错，只会算错钱**。spec §4.4 的三条算式
（净实收额 / 利润 / 应付净额）是 P4-6/P4-7/P4-8/P4-9 的共同底座，所以它们
① **只有一处实现**（`apps/business/settlement_amount.py`），
② 每一条都有**具体十进制值**的断言（spec §9 的四条纪律：`bonus_amount` 默认就是 `0`，
   写 `0` 也会通过 ⇒ 一律断具体值）。

四条纪律，逐条都有判别力（变异实测见任务报告）：

1. ⭐ **净实收的毛收款 = 订单总价 `total_price`，与 `received_price` 无关**
   （业务方裁定，D-0142）：`net_received = total_price − refund_fee`。
   ⛔ 不再读 `received_price`——录单页那个隐藏框把它写成 `0`（C10），当毛收款用是**算错钱**。
   ⇒ `0.00` 的实收款与 `None` 的实收款**给出同一个答案**（都只看总价），
   所以「`is not None` 还是真值」这个区分在本函数上已无意义（那条教训仍适用于别的调用点）。
2. ⭐ **`hall_price`（发内部大厅价）不参与本函数**：那是给打手的价格（D-0081 的裁定理由不变）。
   `total_price` 是 `null=False default=0.00` ⇒ **没有「两边都空」这个状态**、本函数**不会失败**；
   原「双空 ⇒ 抛 `AmountError`」的用例已按新语义重写为「`None` + 总价 `0` ⇒ `0.00`」。
3. ⭐ **`payable_net` 必须含扣点项**（spec §4.4 ③ / §17.1 #2）：漏掉它 = 打手按**未扣点**金额结账，
   P3 的两种扣点模式被静默废掉。用例断**手算值** `8.80`。
4. ⭐ **`payable_net` 允许为负**：拒绝结账是 P4-7 的 `1011`，**不是**这个函数的事（spec §4.4 规则 4）。

⚠️ 本文件**不测任何 HTTP / 序列化器**；夹具照 `test_p4_models.py` 用 `TenantTestCase` + ORM。
⚠️ 期望值一律写字面值（不引用被测模块导出的常量，自指断言不会红，见 E-0024）。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    Performance,
    PerformanceAdjustment,
)
from apps.business.performance_amount import deduct_amount
from apps.business.settlement_amount import net_received, payable_net, profit_of


class _FixturesMixin:
    """ORM 夹具：订单 / 绩效 / 调整记录（P4-2 的两张表之一）。"""

    _seq = 0

    def _make_order(self, *, hall_price="100.00", received_price=None,
                    total_price="200.00", refund_fee="0.00"):
        type(self)._seq += 1
        seq = type(self)._seq
        game = Game.objects.create(name=f"原神-{seq}")
        return Order.objects.create(
            game=game,
            order_no=f"20260920{seq:012d}",
            hall_price=hall_price,
            received_price=received_price,
            total_price=total_price,
            refund_fee=refund_fee,
        )

    def _make_perf(self, order=None, **overrides):
        if order is None:
            order = self._make_order(hall_price="88.00")
        payload = {
            "order": order,
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("0.00"),
            "bonus_amount": Decimal("0.00"),
        }
        payload.update(overrides)
        return Performance.objects.create(**payload)

    def _make_adjustment(self, perf, amount):
        return PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=amount,
            reason="测试调整（P4-4）",
        )


class NetReceivedTests(_FixturesMixin, TenantTestCase):
    """净实收额 = **订单总价 − Σ退款**（`total_price − refund_fee`，D-0142）。"""

    def test_a_zero_received_price_is_ignored_in_favour_of_the_total_price(self):
        """⭐ `received_price = 0.00` **不再是上限**：毛收款只看订单总价 ⇒ `200.00`。

        旧口径（`is not None` 判据）下这里的期望是 `0.00`；新口径（D-0142）下仍读
        `received_price` 会得到 `0.00` ⇒ 本用例当场红，这就是判别力。
        期望值写字面值 `200.00`，并**同时**断陷阱值 `received_price` 确实是 `0.00`
        （证明本用例不是「两个数恰好相等」而通过）。
        ⚠️ 夹具里 `hall_price = 50.00`（又一个不同的数）⇒ 谁把毛收款改成大厅价，本用例也红。
        """
        order = self._make_order(
            hall_price="50.00", received_price=Decimal("0.00"), total_price="200.00"
        )
        # 前置哨兵：陷阱值确实是 0.00（未刷新实例的属性是 str，这里显式换算）。
        self.assertEqual(Decimal(order.received_price), Decimal("0.00"))
        self.assertEqual(net_received(order), Decimal("200.00"))

    def test_a_negative_received_price_is_ignored_in_favour_of_the_total_price(self):
        """⭐ 负的实收款同样是**陷阱值**：毛收款只看订单总价 ⇒ `200.00`。

        旧口径下负实收按冲抵照实代入（期望 `-50.00`）；新口径（D-0142）下
        `received_price` 不参与 ⇒ 谁还读它，本用例当场红。
        `total_price = 200.00` 与 `hall_price = 100.00` 都与陷阱值不同 ⇒
        「改读大厅价」的变异同样红。
        """
        order = self._make_order(
            hall_price="100.00", received_price=Decimal("-50.00"), total_price="200.00"
        )
        self.assertEqual(net_received(order), Decimal("200.00"))

    def test_the_total_price_wins_over_the_received_price(self):
        """⭐ 判据反转（D-0142）：`received=100 / total=200 / hall=50` ⇒ **200**。

        旧口径下这条叫 `test_received_price_wins_over_total_price`、期望 `100`；
        新口径下期望值改成 `200`——**同一条夹具、判据反转**，不是新增的宽松支。
        读 `received_price` 会得到 `100.00`；「取 `max`」在这条恰好也是 200，
        但上一条（负实收）与语料里的零实收会把取 `max` 打红。
        """
        order = self._make_order(
            hall_price="50.00", received_price=Decimal("100.00"), total_price="200.00"
        )
        self.assertEqual(net_received(order), Decimal("200.00"))

    def test_a_missing_received_price_does_not_matter(self):
        """⭐ `received=None / total=200 / hall=50` ⇒ **200**（总价是**唯一来源**，不是回落档）。

        `hall_price = 50.00` 是**陷阱值**：改读大厅价 ⇒ 得到 `50.00` ⇒ 本用例红。
        本条期望值与旧口径相同（`None` 时本来就取总价）——保留它钉子的是
        「缺失的实收款不改变答案」这条事实，不是回落链。
        """
        order = self._make_order(
            hall_price="50.00", received_price=None, total_price="200.00"
        )
        self.assertEqual(net_received(order), Decimal("200.00"))

    def test_a_negative_total_price_is_taken_at_face_value(self):
        """⭐ 负号语义在**总价**上成立（spec §4.4 规则 4）：负的订单总价（冲抵）照实代入，
        **不取 `abs()`、不拒绝**。

        判别力：把 `net_received` 写成 `abs(Decimal(order.total_price))` 或加「拒绝负数」
        ⇒ 本用例红。期望值写字面值 `-50.00`，不引用被测模块导出的常量（E-0024）。
        """
        order = self._make_order(
            hall_price="100.00", received_price=None, total_price="-50.00"
        )
        self.assertEqual(net_received(order), Decimal("-50.00"))

    def test_none_received_price_with_zero_total_price_returns_zero(self):
        """⭐ `received=None`、`total_price=0` ⇒ **0.00**（不报错）。

        ⚠️ 本条**替换**了原 `test_both_prices_missing_raises_instead_of_silently_returning_zero`
        （它钉「两边都空 ⇒ `AmountError`」）。理由：`total_price` 是
        `null=False default=0.00` ⇒ **「两边都空」这个状态不存在**，本函数不会失败。
        `hall_price = 50.00` 仍是陷阱值：改读大厅价会得到 `50.00` ⇒ 本用例红。
        """
        order = self._make_order(
            hall_price="50.00", received_price=None, total_price="0.00"
        )
        self.assertEqual(net_received(order), Decimal("0.00"))

    def test_refund_fee_is_subtracted(self):
        """`888.00 − 30.00 = 858.00`（陷阱值 `received_price = 100.00` 不参与）。

        旧口径下期望是 `100.00 − 30.00 = 70.00`；新口径是 `888.00 − 30.00`。
        """
        order = self._make_order(
            hall_price="999.00", received_price=Decimal("100.00"), total_price="888.00",
            refund_fee=Decimal("30.00"),
        )
        self.assertEqual(net_received(order), Decimal("858.00"))

    def test_zero_refund_fee_subtracts_nothing(self):
        """`total` 取夹具默认 `200.00`、`refund_fee = 0.00` ⇒ `200.00`。

        ⚠️ 把 `received_price` 换成一个**陷阱值** `1.00`（旧写法是 `100.00`）：
        若实现仍读实收款，本用例会得到 `1.00` ⇒ 红；写成 `100.00` 时「恰好被忽略」
        看不出来（它不等于 `total_price` 也只是巧合）。
        """
        order = self._make_order(received_price=Decimal("1.00"), refund_fee="0.00")
        self.assertEqual(net_received(order), Decimal("200.00"))

    def test_rounds_half_up_to_two_places(self):
        """spec §4.4 规则 3：逐笔 `ROUND_HALF_UP` 两位小数。

        用一条**未刷新**实例（内存里 `total_price` 仍是 `str`，见下条用例的机制）构造
        第三位小数为 5 的值：`88.005` ⇒ `88.01`。
        `ROUND_HALF_EVEN` / `ROUND_DOWN` 给 `88.00`，不量化给 `88.005` —— 三者都让本用例红。
        """
        order = self._make_order(
            hall_price="77.00", received_price=None, total_price="88.005"
        )
        self.assertEqual(net_received(order), Decimal("88.01"))


class ReturnTypeTests(_FixturesMixin, TenantTestCase):
    """⭐ 三个函数的返回值**恒为 `Decimal`**，与调用方怎么拿到实例无关。

    本项目真实踩过的坑（`performance_amount.py` 的 docstring 记着）：`objects.create(total_price="88.00")`
    之后，**内存里的属性仍是 `str`**（Django 不会把 `get_prep_value` 的结果写回实例）——
    生产路径上「刚 `save()` 完就接着算」随时会出现。若函数把 `str` 直接透出或直接相乘，
    轻则 `TypeError`，重则调用方拿到 `str` 去做后续运算。
    每条用例都先断**前置哨兵**（属性确实是 `str`），否则本测试可能只是在测「已刷新的实例」。
    """

    def test_net_received_returns_decimal_for_an_unrefreshed_order(self):
        order = self._make_order(hall_price="50.00", received_price=None, total_price="88.00")
        self.assertIsInstance(order.total_price, str)        # 前置哨兵：确实未刷新
        result = net_received(order)
        self.assertIsInstance(result, Decimal)
        self.assertEqual(result, Decimal("88.00"))

    def test_profit_of_returns_decimal_for_an_unrefreshed_order(self):
        order = self._make_order(hall_price="50.00", received_price=None, total_price="88.00")
        self.assertIsInstance(order.total_price, str)
        self.assertIsInstance(profit_of(order, "8.00"), Decimal)

    def test_payable_net_returns_decimal_for_an_unrefreshed_performance(self):
        perf = self._make_perf(
            bonus_amount="17.60", temp_amount="88.00",
            deduct_mode="0", deduct_value="10.00",
        )
        # 前置哨兵：三个参与运算的属性都还是 str —— 尤其 `temp_amount`，它是扣点的 `base`
        self.assertIsInstance(perf.bonus_amount, str)
        self.assertIsInstance(perf.temp_amount, str)
        self.assertIsInstance(perf.deduct_value, str)
        result = payable_net(perf, [])
        self.assertIsInstance(result, Decimal)
        self.assertEqual(result, Decimal("8.80"))


class ProfitOfTests(_FixturesMixin, TenantTestCase):
    """利润 = 净实收额 − 打手生效绩效（spec §4.4 ②）。"""

    def test_profit_is_net_received_minus_taker_bonus(self):
        order = self._make_order(
            hall_price="999.00", received_price=Decimal("100.00"), total_price="888.00",
            refund_fee=Decimal("30.00"),
        )
        # 888.00 − 30.00 − 40.00 = 818.00（陷阱值 received_price = 100.00 不参与）
        self.assertEqual(profit_of(order, Decimal("40.00")), Decimal("818.00"))

    def test_negative_profit_is_returned(self):
        """利润**允许为负**（亏损单真实存在，spec §4.4 规则 4）——这里不拒绝。

        ⚠️ 夹具的 `total_price` 取默认 `200.00`、`received_price = 10.00` 是**陷阱值**：
        旧口径下利润是 `10.00 − 25.00 = −15.00`；新口径（D-0142）下是
        `200.00 − 25.00 = 175.00`。这条**不在** spec §4.2 第 11 条的清单里（清单漏了它），
        但按新算式同样红 ⇒ 期望值按新事实重取。
        负利润的哨兵仍在（`test_negative_profit_from_a_negative_total_price`）。
        """
        order = self._make_order(received_price=Decimal("10.00"))
        self.assertEqual(profit_of(order, Decimal("25.00")), Decimal("175.00"))

    def test_negative_profit_from_a_negative_total_price(self):
        """负号一路传到利润：`-50.00`（总价）− `8.00` = `-58.00`。

        与上一条同因：`profit_of` 走的是 `net_received`，若那里取 `abs()`
        本用例同样必须红。`hall_price = 100.00` 是陷阱值：改读大厅价会得到 `92.00` ⇒ 本用例红。
        """
        order = self._make_order(
            hall_price="100.00", received_price=None, total_price="-50.00"
        )
        self.assertEqual(profit_of(order, Decimal("8.00")), Decimal("-58.00"))

    def test_rounds_half_up_to_two_places(self):
        """`88.00 − 0.005 = 87.995` ⇒ `88.00`（进位）。

        不量化 ⇒ `87.995`；`ROUND_HALF_DOWN` / `ROUND_DOWN` ⇒ `87.99` —— 三者都让本用例红。
        """
        order = self._make_order(
            hall_price="50.00", received_price=None, total_price="88.00"
        )
        self.assertEqual(profit_of(order, Decimal("0.005")), Decimal("88.00"))


class PayableNetTests(_FixturesMixin, TenantTestCase):
    """应付净额 = `bonus_amount − 扣点额 + Σ(未结账调整记录.amount)`（spec §4.4 ③）。

    扣点额**一律**经 `performance_amount.deduct_amount`（P4 的 B3：两侧同一口径），
    `base` 是 **`temp_amount`**（「预计绩效总计」），**不是** `bonus_amount`。
    """

    def test_includes_the_deduction_term(self):
        """⭐ spec §17.1 #2 的回归：`bonus=17.60`、按比例扣 `10%` of `temp=88.00` ⇒ 扣 `8.80`。

        期望值是**手算值** `8.80`（= 17.60 − 8.80）。漏掉扣点项 ⇒ `17.60`（打手按未扣点金额结账）；
        `base` 误用 `bonus_amount` ⇒ `10% x 17.60 = 1.76`，净额 `15.84` —— 两种变异都让本用例红。
        """
        perf = self._make_perf(
            bonus_amount=Decimal("17.60"), temp_amount=Decimal("88.00"),
            deduct_mode="0", deduct_value=Decimal("10.00"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("8.80"))

    def test_fixed_deduction_mode_subtracts_the_literal_value(self):
        """第二种扣点模式（`"1"` 固定额）也走同一条路径。"""
        perf = self._make_perf(
            bonus_amount=Decimal("17.60"), temp_amount=Decimal("88.00"),
            deduct_mode="1", deduct_value=Decimal("5.00"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("12.60"))

    def test_percent_mode_uses_the_over_100_dimension(self):
        """量纲：`x rate / 100`（spec §4.4 开头「量纲统一」）。

        `deduct_mode="0", deduct_value=100` 是上界情形 ⇒ 扣点 = `temp_amount` 全额 = `88.00`：
        净额 `17.60 − 88.00 = −70.40`。「忘了 `/100`」会得到 `17.60 − 8800 = −8782.40`；
        「`rate` 已是小数」会得到 `17.60 − 88 = −70.40`（这条恰好相同）——
        所以另由 `test_includes_the_deduction_term`（10% ⇒ 8.80）钉住除法。
        """
        perf = self._make_perf(
            bonus_amount=Decimal("17.60"), temp_amount=Decimal("88.00"),
            deduct_mode="0", deduct_value=Decimal("100"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("-70.40"))

    def test_unset_deduction_mode_means_zero_deduction(self):
        """`deduct_mode=""`（未设置）⇒ 扣点额为 0，**即使 `deduct_value` 有值**。

        期望值 `12.00`：若把 `""` 当固定额会得 `12.00 − 9.99 = 2.01`；
        当比例会得 `12.00 − 8.78 = 3.22` —— 两种变异都让本用例红。
        """
        perf = self._make_perf(
            bonus_amount=Decimal("12.00"), temp_amount=Decimal("88.00"),
            deduct_mode="", deduct_value=Decimal("9.99"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("12.00"))

    def test_negative_adjustments_are_summed(self):
        """调整记录的 `amount` 是**带符号**的（负 = 退款冲减，spec §3.3）。"""
        perf = self._make_perf(bonus_amount=Decimal("8.00"), deduct_mode="")
        adjustment = self._make_adjustment(perf, Decimal("-8.00"))
        self.assertEqual(payable_net(perf, [adjustment]), Decimal("0.00"))

    def test_multiple_adjustments_with_mixed_signs_are_summed(self):
        perf = self._make_perf(bonus_amount=Decimal("10.00"), deduct_mode="")
        plus = self._make_adjustment(perf, Decimal("3.00"))
        minus = self._make_adjustment(perf, Decimal("-1.50"))
        self.assertEqual(payable_net(perf, [plus, minus]), Decimal("11.50"))

    def test_negative_payable_is_returned_not_rejected(self):
        """⭐ `payable_net` **允许返回负值**：拒绝结账是 P4-7 的 `1011`，不是本函数的职责。

        本条是「不许在本函数里加 `>= 0` 拒绝」的哨兵（spec §4.4 规则 4 的亏损单）。
        """
        perf = self._make_perf(bonus_amount=Decimal("8.00"), deduct_mode="")
        adjustment = self._make_adjustment(perf, Decimal("-20.00"))
        self.assertEqual(payable_net(perf, [adjustment]), Decimal("-12.00"))

    def test_rounds_half_up_to_two_places(self):
        """`bonus=17.605`（无扣点）⇒ `17.61`：不量化给 `17.605`，`ROUND_DOWN` 给 `17.60`。"""
        perf = self._make_perf(bonus_amount="17.605", temp_amount="0.00", deduct_mode="")
        self.assertEqual(payable_net(perf, []), Decimal("17.61"))

    def test_payable_net_does_not_depend_on_received_or_total_price(self):
        """⭐ 结账逐行实付只由 `bonus_amount − 扣点 + Σ未结账调整` 决定（spec §4.2 第 5 行）。

        同一份绩效行、两张价差极大的订单 ⇒ `payable_net` **逐字相等**。
        判别力：谁把 `net_received`（或 `received_price` / `total_price`）混进结账算式，
        本例的两次值会分叉。
        """
        perf = self._make_perf(
            order=self._make_order(received_price="100.00", total_price="200.00"),
            bonus_amount=Decimal("17.60"), temp_amount=Decimal("88.00"),
            deduct_mode="0", deduct_value=Decimal("10.00"),
        )
        other = self._make_perf(
            order=self._make_order(received_price="0.00", total_price="-999.00"),
            bonus_amount=Decimal("17.60"), temp_amount=Decimal("88.00"),
            deduct_mode="0", deduct_value=Decimal("10.00"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("8.80"))
        self.assertEqual(payable_net(other, []), Decimal("8.80"))


class DeductionBaseFollowsTheSubmittedAmountTests(_FixturesMixin, TenantTestCase):
    """⭐ **D-0049 方案 A** 的算术端到端：交单后 `temp_amount == bonus_amount`，扣点基数跟着实际走。

    复刻 D-0049 的时序（价 `100.00`、率 `20% → 50%`、比例扣点 `50%`）：

    | 量 | 手算 |
    |---|---|
    | 认领时刻的预计值 | `100.00 × 20 / 100 = 20.00` |
    | 交单后的实际佣金（**也是**交单后的 `temp_amount`） | `100.00 × 50 / 100 = 50.00` |
    | 扣点额 = `50% × 50.00` | `25.00`（基数停在 `20.00` ⇒ `10.00`，**少扣 15.00**） |
    | 应付净额 = `50.00 − 25.00` | `25.00` |

    ⚠️ 本文件**不经 HTTP**（见模块 docstring），所以这条钉的是「交单后那一行的**算术**」：
    行上 `temp_amount` 与 `bonus_amount` 已经是同一个实际值（由 `test_submit_api.py`
    的 `test_temp_amount_follows_the_bonus_when_the_rate_changed_after_claiming` 钉住写入）。
    期望值写字面值（E-0024）。
    """

    def test_percent_deduction_uses_the_post_submit_temp_amount(self):
        order = self._make_order(hall_price="100.00", total_price="100.00")
        perf = self._make_perf(
            order=order,
            bonus_amount=Decimal("50.00"),
            temp_amount=Decimal("50.00"),      # 交单后跟着 bonus 走的那个值
            deduct_mode="0",                   # 按比例
            deduct_value=Decimal("50.00"),
        )
        self.assertEqual(
            deduct_amount(
                mode="0", value=Decimal("50.00"), base=Decimal(perf.temp_amount)
            ),
            Decimal("25.00"),
        )
        self.assertEqual(payable_net(perf, []), Decimal("25.00"))
