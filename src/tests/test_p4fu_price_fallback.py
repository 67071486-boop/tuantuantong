"""毛收款 / 净实收的**唯一来源** = 订单总价 `total_price`（业务方 2026-09-23 裁定，D-0142）。

| 函数 | 位置 | 语义 |
|---|---|---|
| `settlement_amount.net_received` | 客服提成 / 利润 / 结账 | `total_price − refund_fee` |
| `refunds._gross_received` | 退单 / 部分退款的闸门 | `total_price` |

⚠️ **`received_price`（接单实收款）从此不参与这两个量**（录单页那个隐藏框把它写成 `0`，
所以它当毛收款用本来就是错的）。下面 `CORPUS` 里的 `received_price` 全部是**陷阱值**：
谁把它读回来，对应行立刻红。`hall_price` 同样取与期望值不同的陷阱值。
`refund_fee` 一律 0.00（由夹具固定）⇒ 净实收 == 毛收款，两个函数可以直接对拍。

⚠️ **为什么用「同一份语料跑两个函数」来测**（而不是各写各的用例）：
本项目吃过「同一个数两套算法」的亏，而「两处的规则逐字一致」这句话**用两套用例是证不出来的**
——两边各自绿、口径却分叉（例如只把其中一处改回 `hall_price`）会**静默**留下。
做法照 AGENTS §5「主张差异只有这几类时，跑一段能量化差异的语料对拍两侧」：
下面 `CORPUS` 是**一处**定义，两个函数都按它逐条断**字面值**，并对同一条断言两者相等。

⚠️ 期望值一律写**字面值**（E-0024）：不引用被测模块导出的常量，也不写成
`net_received(o) == _gross_received(o)` 那种自指断言——后者在「两处一起改错」时不会红。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.business.refunds import _gross_received
from apps.business.settlement_amount import net_received

#: 同一份语料：`(标签, received_price（陷阱）, total_price, hall_price（陷阱）, 期望值)`。
#:
#: ⚠️ `received_price` 与 `hall_price` 全部取**与期望值不同的陷阱值**：谁把它们读回来，
#: 对应行立刻红。
#: `refund_fee` 一律 0.00（由夹具固定）⇒ 净实收 == 毛收款，两个函数可以直接对拍。
CORPUS = (
    # 判据 1：实收款是陷阱值（总价 200 / 大厅价 50 都不是它）
    ("实收款是陷阱值", "100.00", "200.00", "50.00", Decimal("200.00")),
    # 判据 2：实收为空也只看总价
    ("实收为空也只看总价", None, "200.00", "50.00", Decimal("200.00")),
    # 判据 3：零实收款同样是陷阱值（`0.00` 不再缩短毛收款）
    ("零实收款同样是陷阱值", "0.00", "200.00", "50.00", Decimal("200.00")),
    # 判据 3 反向：总价为 0 ⇒ 0，不报错（「双空」这个状态不存在）
    ("总价为 0 ⇒ 0", None, "0.00", "50.00", Decimal("0.00")),
    # 判据 6（D-0015）：负数照实代入，不取 abs、不拒绝——两处各一条
    ("负实收是陷阱值", "-50.00", "200.00", "100.00", Decimal("200.00")),
    ("负总价照实代入", None, "-50.00", "100.00", Decimal("-50.00")),
)

# ⚠️ 本语料**刻意不含**「第三位小数进位」那一类：`net_received` 会量化到分，
# 而 `_gross_received` 返回**未量化**的 `Decimal`（量化发生在写库/比较处）。
# 那是两个函数**职责**上的差别，不是口径的差别——混进同一份语料会让
# 「两处逐字一致」这句话假掉。量化行为由 `tests/test_settlement_amount.py` 单独钉。


class PriceFallbackSameCorpusTests(TenantTestCase):
    """⭐「同源」判据：**一份语料、两个函数、同一组期望值**。

    任一函数与 `CORPUS` 分叉（读 `received_price` / 读 `hall_price` / 只改一处）⇒ 本测试红。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self._seq = 0

    def _order(self, *, received, total, hall):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092400000{self._seq:07d}",
            status="COMPLETED",
            received_price=None if received is None else Decimal(received),
            total_price=Decimal(total),
            hall_price=None if hall is None else Decimal(hall),
            refund_fee=Decimal("0.00"),
        )

    def test_net_received_matches_the_corpus(self):
        for label, received, total, hall, expected in CORPUS:
            with self.subTest(case=label):
                order = self._order(received=received, total=total, hall=hall)
                self.assertEqual(net_received(order), expected)

    def test_gross_received_matches_the_corpus(self):
        """退款路径（`_gross_received`）与结算路径**逐条同值**——same corpus, same answers。"""
        for label, received, total, hall, expected in CORPUS:
            with self.subTest(case=label):
                order = self._order(received=received, total=total, hall=hall)
                self.assertEqual(_gross_received(order), expected)

    def test_the_two_functions_agree_row_by_row(self):
        """对拍（不是自指断言）：每一条**各自钉字面值**之后，再钉两者相等。

        ⚠️ 这里额外跑一遍「对拍」是为了让报告能说清「两处规则一致」的**证据形态**；
        真正的判别力在上一对用例的字面值断言里（对拍在「两处一起改错」时会一起错）。
        本类六条语料里 `refund_fee` 恒为 `0.00` ⇒ 两处都只读总价，所以逐行相等。
        """
        for label, received, total, hall, expected in CORPUS:
            with self.subTest(case=label):
                order = self._order(received=received, total=total, hall=hall)
                gross = _gross_received(order)
                net = net_received(order)
                self.assertEqual(net, gross, f"{label}: 两处口径分叉")
                self.assertEqual(net, expected, f"{label}: 与语料期望值不一致")


class GrossMinusRefundIdentityTests(TenantTestCase):
    """⭐ 恒等式：`net_received == _gross_received − refund_fee`（spec §8 第 12 条）。

    这不是巧合，是本次改动的直接后果：毛收款 − 累计退款 = 净实收，
    而毛收款只读总价 ⇒ 它与新 `net_received` **恒等**（退款上限判据在绝对值口径下直接用毛收款）。
    期望值写**字面值**（不写成 `net == gross - refund` 那种自指断言）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self._seq = 0

    def _order(self, total, refund):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092400000{self._seq:07d}",
            status="COMPLETED",
            received_price=Decimal("999.99"),      # 陷阱值：参与就会红
            total_price=Decimal(total),
            refund_fee=Decimal(refund),
        )

    def test_the_two_functions_differ_by_exactly_the_refund_fee(self):
        cases = (
            ("200.00", "0.00", Decimal("200.00"), Decimal("200.00")),
            ("200.00", "30.00", Decimal("170.00"), Decimal("200.00")),
            ("100.00", "100.00", Decimal("0.00"), Decimal("100.00")),
            ("0.00", "-15.00", Decimal("15.00"), Decimal("0.00")),
        )
        for total, refund, want_net, want_gross in cases:
            with self.subTest(total=total, refund=refund):
                order = self._order(total, refund)
                self.assertEqual(net_received(order), want_net)
                self.assertEqual(_gross_received(order), want_gross)
                self.assertEqual(
                    net_received(order),
                    _gross_received(order) - Decimal(order.refund_fee),
                )
