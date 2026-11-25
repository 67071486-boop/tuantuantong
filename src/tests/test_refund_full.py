"""全额退款：进入 `REFUNDED` 时清空绩效，钩子可以重跑。

退单不写退款额、不建 `OrderRefund(kind=FULL)`、不写 `order.refund`。
退单本身由调用方的 `order.transition` 记着。
`refund_fee` 停在与退单无关的值上；`PerformanceAdjustment.refund` 恒为 `None`。
幂等看「这次算出的冲减额是不是 0」：是 0 就不写行。
并发靠 `apply_transition` 的行锁；绕过它直接调钩子不在保护里。
`OrderRefund.Kind.FULL` 枚举留着（历史行还在用）。
`REFUNDED` 不是终态：出边是除自环外的每个目标态，自环仍非法。

会让断言变红的口径：

1. 第 3 步是目标式：`amount = −(bonus_amount + Σ整条退款冲减链)`，不是按费率回算。
   哨兵 `A=100 / T=60 / r=20%` 必须是 `-8.00`。误用公式得 `-20.00`，净额撞 `1011`，永远结不了账。
   已结账的部分退款冲减也要进合计，否则 `payable_net` 看到 0、闸门静默放行。
   `MANUAL` 不参与。
2. `OrderRefund(kind=FULL)` 恰好 0 行，`refund_fee` 不动。
   `refund_fee = Σ OrderRefund.amount` 仍成立，这条路径只是不再参与。
3. 重跑后 `FULL_REFUND` 冲减仍只有一条。
4. `RefundedTerminalStateTests` 名字沿用旧叫法，钉的是出边集合 = 其余全部状态，自环仍非法。
5. 毛收款只读 `total_price`。夹具里的 `received_price` 是陷阱值，改成任何数字都不该影响断言。
6. 退单没有「双空拒绝」前置（`1008` 仍可由部分退款的金额前置校验打到，不在本文件）。
7. `mock.patch` 打在 `apps.business.refunds` 的 `record` 上。另起一条订单跑「不接桩应当成功」。
8. 留痕看 `OperationLog` 的数据库行。`order.refund` 必须 0 行；旧表这条路径零新增。
9. 期望值写字面量（`1008` / `"REFUNDED"` / `Decimal("-8.00")`），不引被测模块的常量。
10. 会 `unregister()` 的类必须快照并还原生产钩子，否则同进程后面的测试静默变红。
11. 打手行金额置 0，行和 `user` 都保留。
12. 已结账的客服行也要冲到 0，新调整记录自身是未结账。
13. 同一订单迁移加手工重跑 2 次，`order.refund` 仍是 0 行。退单的账在 `order.transition`，只写一次。
14. 退单不再去鸡蛋铺撤单。「最后一步抛异常则前面一起回滚」今天没有网：
    旧桩打在已删除的 `queue_withdraw` 上，留着会恒真通过。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog,
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
from apps.business.commission import commission_percent_of
from apps.business.order_status import (
    TRANSITIONS,
    apply_transition,
    can_transition,
    clear_target_hooks,
    register_target_hook,
)
from apps.business.order_status import _TARGET_HOOKS
from apps.business.performance import create_performance_for_order, set_taker
from apps.business.performance_amount import commission_amount, effective_taker_price
from apps.business.refunds import on_order_refunded, refund_set_total
from apps.business.settlement import settle_performances
from apps.business.settlement_amount import payable_net
from tests.order_rules_helpers import release_taker
from tests.p5_auth_helpers import make_logged_in_client

#: 走 P2 通用端点的路径（spec §7 的端点表：**不新建** `/refund-full`）。
TRANSITION_URL = "/api/business/orders/{order_no}/transition/"
ADMIN_USERNAME = "full-refund-admin"


class _FullRefundFixture(TenantTestCase):
    """建单 / 建绩效 / 迁状态的小夹具（各用例只表达**自己的差异**）。

    默认金额就是 spec §5.2 代数表的实数：`A=100.00`（订单总价，D-0142 起毛收款只看它）、
    `T=60.00`（打手生效绩效）、`r=20.00%` ⇒ `COMPLETED` 时客服行
    `bonus_amount = (100 − 60) × 20% = 8.00`。`received_price` 一律是陷阱值（`trap_received`）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.admin = make_logged_in_client(
            # ⚠️ 显式 opt-out：本文件多处自己建档案 ⇒
            # 夹具这一份必须**不建**（管理员本来也不靠档案拿游戏权限）。
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        # `settle_performances(..., user=)` 需要一个真实用户对象（spec §6.1 的
        # `scope_queryset` 卡点）；`make_logged_in_client` 当场建的就是它。
        self.admin_user = User.objects.get(username=ADMIN_USERNAME)
        self.sales_user = User.objects.create_user(username="sales-full")
        self.taker_user = User.objects.create_user(username="taker-full")
        self._seq = 0

    def _order(self, *, status=OrderStatus.COMPLETED, trap_received="1.00", hall=None,
               total="100.00", refund_fee="0.00", sales_user=None):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092200000{self._seq:07d}",
            status=status,
            received_price=None if trap_received is None else Decimal(trap_received),
            hall_price=None if hall is None else Decimal(hall),
            total_price=Decimal(total),
            refund_fee=Decimal(refund_fee),
            sales_user=sales_user,
        )

    def _sales(self, order, **overrides):
        """客服行（`销售提成`）：`bonus_amount` 已由 `COMPLETED` 钩子确定为 8.00。"""
        fields = {
            "emp_type": EmpType.SALES,
            "user": self.sales_user,
            "temp_amount": Decimal("0.00"),
            "bonus_amount": Decimal("8.00"),
            "commission_rate": Decimal("20.00"),
            "review_status": "已通过",
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _taker(self, order, **overrides):
        """打手行（`打单工资`）：真实绩效 60.00（spec §5.2 表的 `T`）。"""
        fields = {
            "emp_type": EmpType.MAIN,
            "user": self.taker_user,
            "temp_amount": Decimal("60.00"),
            "bonus_amount": Decimal("60.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _url(self, order):
        return TRANSITION_URL.format(order_no=order.order_no)

    def _post_transition(self, order, client=None):
        return (client or self.admin).post(
            self._url(order),
            {"target": OrderStatus.REFUNDED},
            content_type="application/json",
        )


class FullRefundIdentityTests(_FullRefundFixture):
    """B1 + B2 + B6（spec §4.2 / D-0181）：退单只剩「打手留痕 + MAIN 清零 + SALES 清账」。

    - **B1**：已完成单改退单 ⇒ `refund_fee == 0.00`、`OrderRefund(kind=FULL)` **0** 行、
      `order.refund` 审计 **0** 行、`order.transition` 审计 **1** 行；
    - **B2**：同上 ⇒ MAIN 行两金额 `0.00`、打手留痕仍在；**未结账**的 SALES 行两列清 0、
      链余额 0、**不写**冲减行（已结账的行才走「恰好一条 `FULL_REFUND` 冲减」，见
      `FullRefundSettledSalesTests` / `FullRefundSettledPriorDeductionTests`）；
    - **B6**：先部分退款 30、再退单 ⇒ `refund_fee` 停在 **30.00**（⛔ 不被顶到总价），
      链仍被冲到 0。
    """

    def test_b1_and_b2_the_refund_writes_no_refund_row_and_clears_the_performance(self):
        """B1 + B2：走**真实 HTTP**（`order.transition` 那一行审计由端点层写）。"""
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)

        response = self._post_transition(order)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        order.refresh_from_db()
        # B1：状态真的迁了（阳性对照），而退款面一列 / 一行都没动。
        self.assertEqual(order.status, "REFUNDED")
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        self.assertEqual(OrderRefund.objects.filter(order=order, kind="FULL").count(), 0)
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            0,
        )
        # ⭐ D-0181：退单的账由 `order.transition` 这一行表达（`from/to` 是中文状态 label）。
        transitions = OperationLog.objects.filter(
            action="order.transition", order_no=order.order_no
        )
        self.assertEqual(transitions.count(), 1)
        row = transitions.get()
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, order.order_no)
        self.assertEqual(row.actor, ADMIN_USERNAME)
        self.assertEqual((row.from_value, row.to_value), ("已完成", "已退单"))
        # 旧出口退役：这条路径**一行都不许**再写旧表。
        self.assertEqual(AuditLog.objects.count(), 0)

        # B2 的前半：MAIN 行两金额归 0；**行与 `user` 都保留**（方案 A / D8）。
        taker.refresh_from_db()
        self.assertEqual(taker.temp_amount, Decimal("0.00"))
        self.assertEqual(taker.bonus_amount, Decimal("0.00"))
        self.assertEqual(taker.user_id, self.taker_user.id)
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        # B2 的另一半：清零**之前**的打手留痕仍在（恰好一条）。
        traces = PerformanceAdjustment.objects.filter(performance=taker)
        self.assertEqual(traces.count(), 1)
        self.assertEqual(traces.get().kind, "MANUAL")
        self.assertIsNone(traces.get().refund_id)
        self.assertIn("原金额 60.00", traces.get().reason)

        # B2 的后半：**未结账**的 SALES 行两列清 0、链余额 0——清 0 在第 3 步**之前**
        # ⇒ 纯退单那一格 `deduction == 0` ⇒ **不写行**；**不变量**（`bonus + Σ全部调整 == 0`）
        # 照旧成立（此时它是 `0 + 0`）。
        # ⛔ 不放宽成恒真：下面四条断言各自都能失败。
        sales.refresh_from_db()
        adjustments = PerformanceAdjustment.objects.filter(performance=sales)
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(
            sales.bonus_amount + sum(row.amount for row in adjustments),
            Decimal("0.00"),
        )

    def test_b6_a_prior_partial_refund_keeps_the_refund_fee(self):
        """B6：先部分退款 30、再退单 ⇒ `refund_fee` 停在 **30.00**（⛔ 不再被顶到毛收款）。

        副判据：清 0 在第 3 步**之前** ⇒ 第 3 步读到的 `net` 只剩链（`−6.00`）⇒
        它写 **`+6.00`** 把链归零；`OrderRefund` 只留下那条 `PARTIAL`。
        ⛔ 不放宽：下面每条断言各自都能失败。
        """
        order = self._order(hall="50.00", total="88.00")
        sales = self._sales(order)
        self._taker(order)
        refund_set_total(order, Decimal("30.00"), "退差价", "b6-partial-1", "cs-actor")
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("30.00"))     # 部分退款照旧写

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        order.refresh_from_db()
        # ⭐ 退单**不碰** `refund_fee`：它停在部分退款留下的累计额上。
        self.assertEqual(order.refund_fee, Decimal("30.00"))
        self.assertEqual(
            [
                (row.kind, row.amount)
                for row in OrderRefund.objects.filter(order=order).order_by("id")
            ],
            [("SET", Decimal("30.00"))],
        )

        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        # 链余额 = `−6.00(部分退款冲减) + 6.00(退单把它抵掉)` = 0。
        self.assertEqual(
            _refund_chain(sales), Decimal("0.00")
        )
        self.assertEqual(
            [
                (row.kind, row.amount)
                for row in PerformanceAdjustment.objects.filter(
                    performance=sales
                ).order_by("id")
            ],
            [
                ("PARTIAL_REFUND", Decimal("-6.00")),
                # 清 0 之前的留痕（`amount=0.00`，不改 Σ）。
                ("MANUAL", Decimal("0.00")),
                ("FULL_REFUND", Decimal("6.00")),
            ],
        )
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(
            sales.bonus_amount
            + sum(
                (
                    row.amount
                    for row in PerformanceAdjustment.objects.filter(performance=sales)
                ),
                Decimal("0.00"),
            ),
            Decimal("0.00"),
        )

    def test_a_zero_total_price_is_refunded_without_writing_a_refund_row(self):
        """毛收款为 0 的边角单：退单**成功**、绩效侧照旧清空、退款面一行不动。

        退单**根本不读任何价格列** ⇒ `trap_received = "0.00"` 与陷阱值 `hall = "50.00"`
        都影响不到断言。未结账的行在第 3 步之前被清 0 ⇒ 纯退单那一格 `deduction == 0`
        ⇒ **不写行**；判据钉**不变量**（`bonus` 清 0 + 链余额 0 + 应付 0.00），
        ⛔ 不放宽、也不加恒真断言。本夹具的客服行是**未结账**（`_sales` 的默认值）。
        """
        order = self._order(trap_received="0.00", hall="50.00", total="0.00")
        sales = self._sales(order)
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        order.refresh_from_db()
        self.assertEqual(order.status, "REFUNDED")
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        # 不变量（`bonus` 与链同时为 0 时是 `0 + 0`）：`bonus_amount + Σ全部调整 == 0`。
        self.assertEqual(
            sales.bonus_amount
            + sum(
                (
                    row.amount
                    for row in PerformanceAdjustment.objects.filter(performance=sales)
                ),
                Decimal("0.00"),
            ),
            Decimal("0.00"),
        )


#: 未结账的回退行不再有「恰好一条 `−8.00`」的形状（纯退单那一格 `deduction == 0` ⇒ 不写行）
#: ⇒ 「链余额」「应付净额」各由下面一个助手算在**一个地方**
#: （E-0021 的形状：同一事实只允许一处判据）。
#: ⛔ 它们**不替代** `bonus_amount + Σ(全部调整) == 0` 那条不变量（后者仍逐条留在用例里：
#: `bonus` 与链同时为 0 时它是 `0 + 0`）。
def _refund_chain(sales):
    """该客服行「整条退款冲减链」的余额 = `Σ(PARTIAL_REFUND + FULL_REFUND)`（裁定 Z 的口径）。"""
    return sum(
        (
            row.amount
            for row in PerformanceAdjustment.objects.filter(
                performance=sales,
                kind__in=(
                    PerformanceAdjustment.Kind.PARTIAL_REFUND,
                    PerformanceAdjustment.Kind.FULL_REFUND,
                ),
            )
        ),
        Decimal("0.00"),
    )


def _payable_of(sales):
    """该客服行的应付净额（只算**未结账**的调整记录，与 P4-7 的结账口径同源）。"""
    return payable_net(
        sales,
        PerformanceAdjustment.objects.filter(
            performance=sales, settle_status=SettleStatus.UNSETTLED
        ),
    )


class FullRefundAlgebraSentinelTests(_FullRefundFixture):
    """⭐ §5.2 的代数对照表：第 3 步**必须**是目标式，不是 §4.5 的 `−amount × rate / 100`。

    `A=100 / T=60 / r=20%`：`COMPLETED` 时利润 `40`、客服行 `bonus_amount = 8.00`。
    未结账的行**清 0 发生在第 3 步之前**（`refunds._clear_unsettled_sales_amounts`）
    ⇒ 纯退单那一格 `net = 0 + 空链 = 0` ⇒ `deduction == 0` ⇒ **一条 `FULL_REFUND` 行都不写**。
    两类哨兵因此各留一条：

    - **本节**：不动 `bonus`/`refund_fee` 的**代数自检**照旧（净实收 = 100、打手清零）；
      而「不是 §4.5 公式」这条由**更强**的判据承担——`payable_net == 0.00`
      （误用 §4.5 会给 `−(100 × 20%) = −20.00` ⇒ 应付 `0 + (−20.00) = −20.00`，**变负**）。
    - **`FullRefundPositiveCorrectionTests`**：链为负 ⇒ 写正数抵掉（**方向性哨兵**）——
      它在**已结账**的行上仍然成立（已结账的行不走清 0）。
    """

    def test_the_target_formula_never_uses_the_amount_times_rate_form(self):
        """哨兵：`payable_net == 0.00`（⛔ 不变负）⇒ 目标式**不是** `−amount × rate / 100`。

        误用 §4.5 会得到 `−(100.00 × 20%) = −20.00` ⇒ `bonus(0) + (−20.00) = −20.00`
        ⇒ 撞 P4-7 的 `1011`（这条调整记录**永久结不了账**）。
        判据同时钉住「不变负」与「确实被冲到 0」。
        **杀法**：把第 3 步换成 `−amount × rate / 100` ⇒ `payable_net` 变 `−20.00` ⇒ 红。
        """
        order = self._order()
        sales = self._sales(order, bonus_amount=Decimal("8.00"))
        self._taker(order, bonus_amount=Decimal("60.00"))

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        # 未结账的行两列清 0、链余额 0 ⇒ 应付 0.00（⛔ 不是 −20.00、⛔ 不是 −8.00）。
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        net = _payable_of(sales)
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))
        # 不变量（`bonus` 与链同时为 0 时它是 `0 + 0`）：`bonus_amount + Σ全部调整 == 0`。
        self.assertEqual(
            sales.bonus_amount
            + sum(
                (
                    row.amount
                    for row in PerformanceAdjustment.objects.filter(performance=sales)
                ),
                Decimal("0.00"),
            ),
            Decimal("0.00"),
        )
        # 连**误用公式**的形状一起排除：链上没有任何 `−20.00` 的行。
        self.assertNotIn(
            Decimal("-20.00"),
            [
                row.amount
                for row in PerformanceAdjustment.objects.filter(performance=sales)
            ],
        )
        # 利润自检（spec §5.2）：退单不再把 `refund_fee` 顶到毛收款
        # ⇒ `净实收 = 100 − 0 = 100`。
        taker = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(taker.bonus_amount, Decimal("0.00"))
        self.assertEqual(
            Decimal(order.total_price) - order.refund_fee, Decimal("100.00")
        )



class FullRefundPositiveCorrectionTests(_FullRefundFixture):
    """⭐ 链上冲减**超过** `bonus_amount` 时，目标式会产出**正**的 `FULL_REFUND`。

    这是**正确行为**（编排方核算代数后判定不是缺陷），但必须用测试钉住，
    否则后来的读者会以为「正调整 = 退款反给客服补钱」是 bug。

    代数（`A=100 / T=60 / r=20%`，部分退款 `50` ⇒ §4.5 冲减 `−(50 × 20%) = −10.00`）：

    | 时刻 | 提成应为 | 链上 | 目标式写出 |
    |---|---|---|---|
    | `COMPLETED` | `8.00` | `8.00` | — |
    | 部分退款 `50` | `−2.00` | `8 − 10 = −2.00` ✅ 自洽 | — |
    | 再全额退款 | `0.00` | — | `−(8 + (−10)) = +2.00` ⇒ 终身合计 `0.00` ✅ |

    ⇒ **正调整 = 把之前多扣的提成退回**（目标式只吃 `bonus_amount + 链`、不吃利润）。

    方向性（链为负 ⇒ 写**正**数抵掉）在两种行状态下都成立，但**写出的数不同**，所以**两条都留**：
    - 未结账：清 0 在先 ⇒ 第 3 步读到 `net = 0 + 链` ⇒ 写 **`−chain`**；链 `−10.00` ⇒ 写 **`+10.00`**
      （判据 = `test_an_unsettled_chain_overshoot_produces_a_positive_full_refund_and_a_zero_payable`）；
    - 已结账：`bonus 8.00` + 链 `−10.00` ⇒ 写 **`+2.00`**（判据 =
      `test_a_chain_overshoot_produces_a_positive_full_refund_and_a_zero_lifetime`）。
    **杀法**：把第 3 步的净额写成 `0`（或按 `−amount × rate / 100` 写 `−10.00`）⇒
    两条的「正值」断言当场都红。
    """

    #: 结算行（已结账的行**不**清 0）。
    SETTLED = SettleStatus.SETTLED

    def test_a_chain_overshoot_produces_a_positive_full_refund_and_a_zero_lifetime(self):
        order = self._order()
        sales = self._sales(
            order, bonus_amount=Decimal("8.00"), settle_status=self.SETTLED
        )
        self._taker(order, bonus_amount=Decimal("60.00"))

        # 第 1 步：部分退款 50（走**领域函数**，不用手搓调整行——链路要真实）。
        refund_set_total(order, Decimal("50.00"), "客户投诉", "overshoot-1", "财务甲")
        partial = PerformanceAdjustment.objects.get(
            performance=sales, kind="PARTIAL_REFUND"
        )
        self.assertEqual(partial.amount, Decimal("-10.00"))

        # 第 2 步：全额退款（P2 通用端点 → `REFUNDED` 钩子 → 目标式）。
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        full = PerformanceAdjustment.objects.get(performance=sales, kind="FULL_REFUND")
        # 哨兵①（**方向性**）：链上冲减超过 `bonus_amount` ⇒ 目标式给**正**的 `+2.00`
        # （不是 0、不是 −8、更不是 −10）。已结账的行不改 ⇒ 金额是 `−(8 + (−10)) = +2.00`。
        self.assertEqual(full.amount, Decimal("2.00"))
        self.assertGreater(full.amount, Decimal("0.00"))
        # 哨兵②：终身合计（原始提成 + 全部调整）= **0.00**。
        adjustments = sum(
            (
                row.amount
                for row in PerformanceAdjustment.objects.filter(performance=sales)
            ),
            Decimal("0.00"),
        )
        sales.refresh_from_db()
        self.assertEqual(adjustments, Decimal("-8.00"))
        self.assertEqual(sales.bonus_amount + adjustments, Decimal("0.00"))
        # 阳性对照（已结账的行**一个字节都不动**）：它没被清 0。
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))


    def test_an_unsettled_chain_overshoot_produces_a_positive_full_refund_and_a_zero_payable(
        self,
    ):
        """⭐ **未结账**行链冲过头的哨兵：目标式写 **`+10.00`** 的正数行，
        且 `payable_net == 0.00`。

        代数：部分退款 `50.00` ⇒ 冲减 `−10.00`；退单 ⇒ 第 2.7 步先把 `bonus` 清成 `0.00`
        （留痕 `MANUAL 0.00`）⇒ 第 3 步读到 `net = 0 + (−10.00) = −10.00` ⇒ 写
        `−net = +10.00` 把链冲平 ⇒ 链余额 = `−10 + 10 = 0.00`、`payable_net = 0 + 0 = 0.00`
        （⛔ 不变负）。
        **杀法**：① 把第 3 步的净额写成 `0` ⇒ `assertGreater` 与 `+10.00` 都红；
        ② 清 0 之后**跳过**第 3 步 ⇒ 链上留着 `−10.00` ⇒ `payable_net = −10.00` ⇒ 红。
        """
        order = self._order()
        # ⚠️ **不传 `settle_status`** ⇒ 用夹具默认的「未结账」。
        sales = self._sales(order, bonus_amount=Decimal("8.00"))
        self._taker(order, bonus_amount=Decimal("60.00"))

        refund_set_total(order, Decimal("50.00"), "客户投诉", "overshoot-unsettled-1", "财务甲")
        partial = PerformanceAdjustment.objects.get(
            performance=sales, kind="PARTIAL_REFUND"
        )
        self.assertEqual(partial.amount, Decimal("-10.00"))

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        full = PerformanceAdjustment.objects.get(performance=sales, kind="FULL_REFUND")
        # 哨兵①（**方向性**）：链上冲减超过 `bonus_amount`（已被清 0）⇒ 目标式给**正**的
        # `+10.00`（不是 0、不是 −10）——这就是「把之前多扣的提成退回」。
        self.assertEqual(full.amount, Decimal("10.00"))
        self.assertGreater(full.amount, Decimal("0.00"))
        # 哨兵②：链余额 0、应付 0.00（⛔ 不许变负）。
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        net = _payable_of(sales)
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))



def _kind_in_members(path):
    """从 `refunds.py` 的源码里取出目标式 `kind__in=(...)` 覆盖的 `Kind` 成员名。

    ⚠️ **必须走 AST 而不是文本 grep**（同 `test_rbac_structure` 的 B2 教训）：
    文本匹配会被一句注释 / docstring 满足，而这条哨兵的全部意义是
    「**将来新增一个 `Kind` 时它当场变红**，逼实现者在这里做决定」。
    找不到那个 `kind__in` 关键字参数 ⇒ 直接失败（不许静默返回空集让哨兵真空绿）。
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "kind__in":
                continue
            if not isinstance(keyword.value, ast.Tuple):
                raise AssertionError("`kind__in` 不是字面元组——哨兵读不懂它的形状")
            members = []
            for element in keyword.value.elts:
                # 形状是 `PerformanceAdjustment.Kind.PARTIAL_REFUND`
                # ⇒ `Attribute(value=Attribute(value=Name('PerformanceAdjustment'), attr='Kind'), attr='PARTIAL_REFUND')`。
                if not (
                    isinstance(element, ast.Attribute)
                    and isinstance(element.value, ast.Attribute)
                    and element.value.attr == "Kind"
                    and isinstance(element.value.value, ast.Name)
                    and element.value.value.id == "PerformanceAdjustment"
                ):
                    raise AssertionError(
                        f"`kind__in` 里出现了哨兵认不出的成员：{ast.dump(element)}"
                    )
                members.append(element.attr)
            return members
    raise AssertionError("refunds.py 里找不到 `kind__in=` ——目标式的实现形状变了")


class RefundTargetFormulaKindSentinelTests(TenantTestCase):
    """⭐ 目标式的 `kind__in` 是**排他列表**。

    `PerformanceAdjustment.Kind` 今天恰好三个取值，没有遗漏；但**新增第 4 个 `Kind`** 时，
    目标式会**静默**把它排除，而没人会注意到（`MANUAL` 的排除是**有意**的边界，见裁定 Z）。
    这条哨兵让「新增 kind」当场变红，逼实现者在此处做决定。

    判据的一半读**模型**（`Kind` 的全部取值），另一半读**实现**（`refunds.py` 的 AST），
    两半对拍 —— 光在 docstring 写「新增 kind 时要决定」不构成回流（AGENTS §5）。
    """

    def test_the_target_formula_covers_exactly_the_two_refund_kinds(self):
        import pathlib

        all_kinds = {value for value, _label in PerformanceAdjustment.Kind.choices}
        # ① 模型侧的全集钉**字面值**（E-0024：期望值不引被测模块的常量）。
        self.assertEqual(
            sorted(all_kinds), ["FULL_REFUND", "MANUAL", "PARTIAL_REFUND"],
            "PerformanceAdjustment.Kind 的取值集合变了——请在此处决定目标式要不要覆盖它",
        )
        # ② 实现侧：refunds.py 的 `kind__in` 覆盖了哪两个。
        covered = set(
            _kind_in_members(
                pathlib.Path(__file__).resolve().parent.parent / "apps/business/refunds.py"
            )
        )
        self.assertEqual(sorted(covered), ["FULL_REFUND", "PARTIAL_REFUND"])
        # ③ 哨兵本体：全集 − {MANUAL} **恰好**等于被覆盖的两个。
        #    `MANUAL` 是**有意**的例外（裁定 Z 的边界：人工修正是独立账务决定，不被退款抹掉）。
        self.assertEqual(
            all_kinds - {"MANUAL"}, covered,
            "新增了 Kind，而目标式的 kind__in 没有跟上——要么覆盖它，要么把例外写进这里",
        )


class FullRefundIdempotencyTests(_FullRefundFixture):
    """B3（spec §4.2 / D-0181）：钩子可被重跑 ⇒ **不能重复冲减、不再建事件行 / 退款审计**。

    幂等判据是「**本次算出来的冲减额是不是 0**」——事件行已删除，
    不存在「同一 `refund` 事件是否已有 `FULL_REFUND` 行」那种锚。
    """

    def test_rerunning_the_hook_with_refunded_from_status_adds_nothing(self):
        """重跑（`from_status == REFUNDED`）⇒ 连打手留痕都不再写（`is_rerun` 判据）。"""
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        before = {
            "refunds": OrderRefund.objects.filter(order=order).count(),
            "audit": OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            "traces": PerformanceAdjustment.objects.filter(performance=taker).count(),
            "adjustments": PerformanceAdjustment.objects.filter(
                performance=sales
            ).count(),
        }

        # 「第二次到达」：直接重跑钩子（`REFUNDED → REFUNDED` 不是合法边，
        # 端点重放会被 `IllegalTransition` 拒掉）。
        on_order_refunded(order, OrderStatus.REFUNDED)
        on_order_refunded(order, OrderStatus.REFUNDED)

        order.refresh_from_db()
        sales.refresh_from_db()
        # 退款面：一行都没有（第一次就没有，重跑更没有）。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), before["refunds"])
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        # ⭐ 重跑**不新增任何行**（含打手留痕：`is_rerun` 为真 ⇒ 第 1.5 步跳过）。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=taker).count(),
            before["traces"],
        )
        adjustments = PerformanceAdjustment.objects.filter(
            performance=sales, kind="FULL_REFUND"
        )
        # 未结账的行在第一次退单就被清 0 ⇒ 纯退单那一格 `deduction == 0`
        # ⇒ 一条 `FULL_REFUND` 都不写；重跑更是一条都不加。
        self.assertEqual(adjustments.count(), 0)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(),
            before["adjustments"],
        )
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(
            sales.bonus_amount
            + sum(
                row.amount
                for row in PerformanceAdjustment.objects.filter(performance=sales)
            ),
            Decimal("0.00"),
        )
        # `order.refund` 审计**一行都没有**；重跑自然也不会多写（它本来就不写）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            before["audit"],
        )
        self.assertEqual(before["audit"], 0)

    def test_b3_rerunning_with_a_lying_from_status_writes_no_second_deduction(self):
        """B3 的另一半：`from_status` **不是** `REFUNDED`（谎报成真实迁移）⇒ 冲减仍不重复。

        ⚠️ 判据的边界（如实登记，spec §G3 的残差）：`is_rerun` 只看 `from_status`
        ⇒ 这种谎报会让第 1.5 步（打手留痕）与第 5 件（鸡蛋铺撤下）按「真实事件」再走一次。
        本条**不断言它们**（它们不是钱），只钉**钱**那一半：
        `OrderRefund` / `order.refund` 审计仍是 0 行、`FULL_REFUND` 冲减**不新增**。
        生产里到不了这里（唯一调用点是 `apply_transition`，它传的 `from_status` 一定是真实来源态）。
        """
        order = self._order()
        sales = self._sales(order)
        self._taker(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})
        sales.refresh_from_db()
        deductions_before = PerformanceAdjustment.objects.filter(
            performance=sales, kind="FULL_REFUND"
        ).count()
        # 未结账的行清 0 ⇒ 纯退单那一格 `deduction == 0` ⇒ 不写行；
        # 阳性对照是**状态**那一条：两列确实被清 0 了。
        self.assertEqual(deductions_before, 0)
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))

        on_order_refunded(order, OrderStatus.COMPLETED)

        order.refresh_from_db()
        sales.refresh_from_db()
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            0,
        )
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).count(),
            deductions_before,
        )
        # 重跑不把已经清 0 的行又写回非 0（`refresh_customer_estimate` 在 `REFUNDED` 上恒写 0）。
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(
            sales.bonus_amount
            + sum(
                row.amount
                for row in PerformanceAdjustment.objects.filter(performance=sales)
            ),
            Decimal("0.00"),
        )

    def test_the_refunded_hook_is_registered_in_the_target_hook_table(self):
        """结构判据：`REFUNDED` 的钩子表里**真的有**这个函数（防「模块没被 import」）。"""
        hooks = _TARGET_HOOKS.get(str(OrderStatus.REFUNDED), [])
        self.assertIn(on_order_refunded, hooks)


class RefundedTerminalStateTests(TenantTestCase):
    """§9 #11 的哨兵：`REFUNDED` 作为 source 的出边集合。

    当前口径：出边 = **除自环外的每个目标态**；`REFUNDED → COMPLETED` 是**显式枚举的例外**
    （入 `COMPLETED` 的边只剩「待验收 → 已完成」一条）。开边的依据是 P2 spec D8
    「原状态被覆盖后，恢复去向应由人负责，而不是系统猜」。

    §5.2 末尾「给 `REFUNDED` 开边」的三步配套，落实现状：
    ① 清 `user` —— 由 `_on_pool_entry` → `reset_taker_performance` 覆盖（**不看来源态**，D-0032）；
    ② 「原接单人 + 原金额」的留痕 —— **没有**（退单 → 回池 → 重新接走后，
       上一个接单人是谁在绩效行里被覆盖掉了），登记在 P4 计划 §P4-12 必做清单
       与 **D-0044** 的触发条件里；
    ③ 补测试 —— 缺 MAIN 行的回归由
       `test_refunded_order_without_performance_row_recovers_to_the_pool` 覆盖；
       ② 那条留痕的测试**没有**。
    """

    def test_refunded_outgoing_edges_cover_every_other_status(self):
        outgoing = [edge for edge in TRANSITIONS if str(edge[0]) == "REFUNDED"]
        self.assertEqual(
            sorted(str(edge[1]) for edge in outgoing),
            sorted(
                str(status)
                for status in OrderStatus
                if status not in (OrderStatus.REFUNDED, OrderStatus.COMPLETED)
            ),
        )
        # 被删的那一条单独断（不是靠上面的集合相等「顺带」覆盖）
        self.assertNotIn((OrderStatus.REFUNDED, OrderStatus.COMPLETED), TRANSITIONS)
        # 两条回池边仍得能真的走通（不是「表里有、判据却说不行」）
        self.assertTrue(
            can_transition(OrderStatus.REFUNDED, OrderStatus.WAITING_GRAB)
        )
        self.assertTrue(can_transition(OrderStatus.REFUNDED, OrderStatus.UNASSIGNED))
        # ⚠️ **自环仍是假**（R8 之下仍非法的形状之一）：出边不包含指回自己那一条。
        self.assertFalse(can_transition(OrderStatus.REFUNDED, OrderStatus.REFUNDED))


class FullRefundPathCoverageTests(_FullRefundFixture):
    """§9 #12：副作用覆盖**所有**到达 `REFUNDED` 的路径（①②③④）。"""

    def test_the_generic_transition_endpoint_clears_the_performance(self):
        """⭐ §9 #12①：经 **P2 通用端点**退单也会清空绩效——钩子方案的核心收益。"""
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)

        body = self._post_transition(order).json()

        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, "REFUNDED")
        # 退单不再把 `refund_fee` 顶到毛收款（它一动不动）。
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        taker.refresh_from_db()
        self.assertEqual(taker.bonus_amount, Decimal("0.00"))
        self.assertEqual(taker.temp_amount, Decimal("0.00"))
        self.assertEqual(taker.user_id, self.taker_user.id)
        sales.refresh_from_db()
        # **未结账**的行在第 3 步之前被清 0 ⇒ 纯退单那一格 `deduction == 0` ⇒ 不写冲减行；
        #   断言钉**不变量**（`bonus` 清 0 + 链 0 + 应付 0.00），⛔ 不弱化，三条都能失败。
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        # 操作人取认证态（不是请求体）——两个角色都能退钱，事后必须能答「谁批的」。
        # 留痕的动作码是 `order.transition`（端点层写）；`order.refund` 那条路已不存在
        # （它记的「退款额从多少到多少」不再存在）。
        self.assertEqual(
            OperationLog.objects.get(
                action="order.transition", order_no=order.order_no
            ).actor,
            ADMIN_USERNAME,
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            0,
        )

    def test_direct_apply_transition_clears_the_performance(self):
        """⭐ §9 #12③：直接调 `apply_transition` → 同上。

        spec §17.4 #2：只靠「走端点 vs 直接 `Order.objects.update()`」两种断言
        **只能排除「钩子在 `Model.save()` 上」**，证明不了「挂在 `apply_transition` 上」。
        """
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        taker.refresh_from_db()
        self.assertEqual(taker.bonus_amount, Decimal("0.00"))
        sales.refresh_from_db()
        # **未结账**的行清 0 ⇒ 纯退单那一格不写冲减行；
        # 断言钉**不变量**（`bonus` / 链余额 / 应付 三个 0）。
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))

    def test_bypassing_the_transition_does_not_touch_anything(self):
        """⭐ §9 #12④ 反向：`Order.objects.update(status=REFUNDED)` 绕过迁移 → 副作用**没有**发生。"""
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)

        Order.objects.filter(pk=order.pk).update(status=OrderStatus.REFUNDED)

        order.refresh_from_db()
        self.assertEqual(order.status, "REFUNDED")   # 阳性对照：状态确实被改了
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        taker.refresh_from_db()
        self.assertEqual(taker.bonus_amount, Decimal("60.00"))
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(), 0
        )
        self.assertEqual(
            OperationLog.objects.filter(action="order.refund", order_no=order.order_no).count(), 0
        )


class FullRefundPreconditionTests(_FullRefundFixture):
    """原「双空 ⇒ 拒绝退单（`1008`）」这条路径已不存在，新语义在此。

    回落档是**订单总价 `total_price`**（`null=False default=0.00`）⇒ **「两处价都空」
    这个状态不存在**，`refunds._gross_received` 的那条错误分支已删。
    新语义（域级 + 端点级各一条）：`total_price=0.00`（`trap_received` 是陷阱值）⇒
    退单**成功**、`refund_fee` 停在 `0.00`、退款面**一行都不写**；
    绩效清空侧照旧执行（打手清零、未结账的客服行清 0）。这正是 spec §5.3
    「客户在未分配阶段就取消，是最常见的情况」原本想要的形态（当时被 `1008` 挡着）。
    退单**与任何价格列无关**：`hall = "88.00"` 与 `trap_received` 仍是陷阱值
    （谁把价格列重新接进退单路径，两条用例都会红）。
    """

    def test_a_zero_total_price_refunds_zero(self):
        order = self._order(hall="88.00", total="0.00")
        sales = self._sales(order)
        taker = self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        order.refresh_from_db()
        self.assertEqual(order.status, "REFUNDED")
        self.assertEqual(order.refund_fee, Decimal("0.00"))     # 没写 None，也没回落大厅价
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        # 绩效清空侧照旧（毛收款为 0 不改变「退单清空」这条副作用）。
        taker.refresh_from_db()
        self.assertEqual(taker.bonus_amount, Decimal("0.00"))
        sales.refresh_from_db()
        # **未结账**的行清 0 ⇒ 纯退单那一格不写冲减行；
        # 断言钉**不变量**（`bonus` / 链余额 / 应付 三个 0）。
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))

    def test_the_endpoint_completes_instead_of_raising_a_500(self):
        """端点级：不再有 `1008`，必须 `HTTP 200 + code 0`（不是未捕获异常 → 500）。"""
        order = self._order(hall="88.00", total="0.00")
        self._sales(order)
        self._taker(order)

        response = self._post_transition(order)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, "REFUNDED")
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)


class FullRefundPermissionGateTests(_FullRefundFixture):
    """门槛哨兵（spec §5.3 细节 3 / §10 #12）：退单**不是**任何人可调的动作。

    通用 `/transition/` 的授权轴是 **RBAC 节点**：没有 `order.change_status`（16）
    的账号在类级节点门就被 403 挡下，一行副作用都不写（角色轴已删除，
    节点是唯一判据，拒绝形态是 403；五类写入全零的断言不变）。
    """

    def test_a_user_without_the_change_status_node_cannot_clear_performance(self):
        # 通用 `/transition/` 的类级节点门就是 `order.change_status`
        # （`OrderViewSet.ORDER_PERMISSIONS`），而单轴化后它**不再只是拦截层**——
        # 它同时是状态机边的判据来源。没有它 ⇒ 403（`code == 403`），
        # 退单的五类写入（绩效回退 / 冲销 / 调整 / 退款单 / 留痕）一个都不该发生。
        # ⚠️ 组名用**非内置职位**的「录入组」，且**不给任何节点**（`nodes=()`）。
        no_node = make_logged_in_client(
            username="nonode-full",
            groups=("录入组",),
            nodes=(),
            # ⚠️ 显式 opt-out：下面自己建档案 ⇒ 夹具必须**不建**。
            games=None,
        )
        # ⚠️ **夹具要补齐本单的游戏档案**，不是放宽判据：
        #    `/transition/` 走 `get_object()`（同一条被 `game_scope` 收窄的 queryset），
        #    没有本单游戏的账号会拿到 **1002**（不可见），永远到不了本用例要测的
        #    节点门 403——同码不同因，判据就漂了。
        # 金额口径在 total_price；`_order` 不收 `received=`（别传）。
        StaffProfile.objects.create(
            user=User.objects.get(username="nonode-full")
        ).games.set([self.game])
        order = self._order()
        sales = self._sales(order)
        taker = self._taker(order)

        response = self._post_transition(order, client=no_node)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        order.refresh_from_db()
        self.assertEqual(order.status, "COMPLETED")          # 阳性对照：状态没动
        taker.refresh_from_db()
        self.assertEqual(taker.bonus_amount, Decimal("60.00"))
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(), 0
        )
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)


class FullRefundSettledSalesTests(_FullRefundFixture):
    """已结账的客服行也要被冲到 0（与 P4-9 的细节 1 同源）。

    ⚠️ 原行**一个字节都不改**（D2）；新调整记录自身是「未结账」，进入下一次结账。
    """

    def test_a_settled_sales_row_is_also_cleared_to_zero(self):
        order = self._order()
        sales = self._sales(order, settle_status=SettleStatus.SETTLED)
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.settle_status, "已结账")     # 原行可结账状态不动
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        adjustments = PerformanceAdjustment.objects.filter(performance=sales)
        self.assertEqual(adjustments.count(), 1)
        self.assertEqual(adjustments.get().amount, Decimal("-8.00"))
        self.assertEqual(adjustments.get().settle_status, "未结账")


class FullRefundSettledPriorDeductionTests(_FullRefundFixture):
    """⭐ 哨兵：prior 冲减**已结账**时，目标式仍要覆盖**整条冲减链**（裁定 Z）。

    时序（全部走正常 API 可达）：

    1. 建单 `A=100 / T=60 / r=20%` ⇒ 客服行 `bonus_amount=8.00`；
    2. `refund_set_total(30.00)` ⇒ 冲减 `-6.00`（未结账）；
    3. 财务把客服行**连那条 `-6.00` 一起结账**；
    4. 迁 `REFUNDED` ⇒ 目标式必须给 **`-2.00`**，终身合计才到 `0.00`。

    ⚠️ **字面公式（`Σ未结账`）在这一档给出 `-8.00`**：累计变成
    `8 − 6 − 8 = −6.00`（客户的 100 全退了，账上还欠客服 6 元），
    而 `payable_net`（只看未结账）**恰好看到 0** ⇒ P4-7 的 `1011` 静默放行。
    这就是 Critical 的哨兵：没有这一档，整条链的错账无人能发现。

    ⚠️ 结算净额为 `8.00 + (−6.00) = 2.00 ≥ 0`，所以第 3 步的结账**能成功**——
    这不是跳过护栏，是「客服提成 8 元、已冲减 6 元、还差 2 元」的真实账。
    """

    def _settled_prior_deduction(self):
        """建单 + 部分退款 30.00 + 把客服行**连冲减一起**结账；返回 `(order, sales)`。"""
        order = self._order()
        sales = self._sales(order)
        self._taker(order)
        refund_set_total(
            order, Decimal("30.00"), "退差价", "prior-partial-settled", "cs-actor"
        )
        # 前置 ①：那条冲减是 `-6.00` 且**未结账**（否则下面的结账是空的断言）。
        prior = PerformanceAdjustment.objects.get(
            performance=sales, kind="PARTIAL_REFUND"
        )
        self.assertEqual(prior.amount, Decimal("-6.00"))
        self.assertEqual(prior.settle_status, "未结账")
        # 前置 ②：结账**真的**同时把绩效行与那条冲减标记成「已结账」。
        result = settle_performances(
            [sales.id], actor="finance", user=self.admin_user
        )
        self.assertEqual(result.settled_performances, [sales.id])
        self.assertEqual(result.settled_adjustments, [prior.id])
        sales.refresh_from_db()
        prior.refresh_from_db()
        self.assertEqual(sales.settle_status, "已结账")
        self.assertEqual(prior.settle_status, "已结账")
        return order, sales

    def test_a_settled_prior_deduction_is_still_covered_by_the_target_formula(self):
        order, sales = self._settled_prior_deduction()

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        adjustments = PerformanceAdjustment.objects.filter(performance=sales)
        # 1) 目标式覆盖整条链 ⇒ `-2.00`（字面公式会给 `-8.00`）。
        self.assertEqual(
            adjustments.get(kind="FULL_REFUND").amount, Decimal("-2.00")
        )
        # 2) ⭐ 终身合计到 0——这才是「退款把提成冲到 0」的判据。
        self.assertEqual(
            sales.bonus_amount + sum(row.amount for row in adjustments),
            Decimal("0.00"),
        )
        # 3) 已结账的**原行**一个字节都不改（D2）；新调整记录自身是「未结账」。
        self.assertEqual(sales.settle_status, "已结账")
        self.assertEqual(
            adjustments.get(kind="FULL_REFUND").settle_status, "未结账"
        )

    def test_the_appended_correction_can_still_be_settled(self):
        """spec §3.3 的坏账出口：已结账绩效被追加的调整记录**结得掉**（不会被 1006 拒）。"""
        order, sales = self._settled_prior_deduction()

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        correction = PerformanceAdjustment.objects.get(
            performance=sales, kind="FULL_REFUND"
        )
        result = settle_performances(
            [sales.id], actor="finance", user=self.admin_user
        )
        self.assertEqual(result.settled_performances, [])   # 绩效行早已结账
        self.assertEqual(result.settled_adjustments, [correction.id])
        correction.refresh_from_db()
        self.assertEqual(correction.settle_status, "已结账")
        sales.refresh_from_db()
        self.assertEqual(sales.settle_status, "已结账")     # 原行不动
        self.assertEqual(
            sales.bonus_amount
            + sum(
                row.amount
                for row in PerformanceAdjustment.objects.filter(performance=sales)
            ),
            Decimal("0.00"),
        )


class FullRefundManualAdjustmentBoundaryTests(_FullRefundFixture):
    """⭐ 边界（裁定 Z）：`MANUAL` 人工修正**不参与**目标式。

    语义：全额退款把「这条订单的提成」冲到 0；**财务的人工修正是独立的账务决定**，
    不被退款抹掉。若把 `MANUAL` 也纳入 `kind__in`，一条已结账的人工扣款会被全额退款
    **反向撤销**（钱退给客服）——那是错的方向。
    """

    def test_a_settled_manual_deduction_is_not_reversed_by_the_full_refund(self):
        order = self._order()
        sales = self._sales(order)
        self._taker(order)
        manual = PerformanceAdjustment.objects.create(
            performance=sales,
            kind="MANUAL",
            amount=Decimal("-5.00"),
            reason="财务人工扣款",
            operator="finance",
            settle_status="未结账",
        )
        settled = settle_performances(
            [sales.id], actor="finance", user=self.admin_user
        )
        self.assertEqual(settled.settled_adjustments, [manual.id])
        manual.refresh_from_db()
        self.assertEqual(manual.settle_status, "已结账")     # 前置成立

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        adjustments = PerformanceAdjustment.objects.filter(performance=sales)
        # MANUAL 不在目标式里 ⇒ 冲减仍是 `-8.00`（若纳入会得 `-3.00`）。
        self.assertEqual(
            adjustments.get(kind="FULL_REFUND").amount, Decimal("-8.00")
        )
        # 人工扣款**仍在**，且终身合计是 `-5.00`（正是那笔独立决定）。
        self.assertEqual(
            sales.bonus_amount + sum(row.amount for row in adjustments),
            Decimal("-5.00"),
        )
        manual.refresh_from_db()
        self.assertEqual(manual.amount, Decimal("-5.00"))
        self.assertEqual(manual.settle_status, "已结账")


class RefundHookUnregisterTests(TenantTestCase):
    """`settlement_hooks.unregister()` 必须**同时**清 `COMPLETED` 与 `REFUNDED`。

    ⚠️ `_TARGET_HOOKS` 是**模块级**字典：本类会真的 `unregister()`（把两个键整条 pop），
    所以必须**快照 + 还原**生产钩子——否则同进程里后跑的用例会随执行顺序静默变红。
    """

    def setUp(self):
        from apps.business import settlement_hooks

        self.module = settlement_hooks
        self.saved = {
            target: list(_TARGET_HOOKS.get(str(target), []))
            for target in (OrderStatus.COMPLETED, OrderStatus.REFUNDED)
        }
        self.addCleanup(self._restore)

    def _restore(self):
        for target, hooks in self.saved.items():
            clear_target_hooks(target)
            for func in hooks:
                register_target_hook(target, func)

    def test_unregister_clears_the_refunded_hook_too(self):
        # 前置：注册表里**本来**就有生产钩子（否则「清空」是空的断言）。
        self.assertIn(
            on_order_refunded, _TARGET_HOOKS.get(str(OrderStatus.REFUNDED), [])
        )

        self.module.unregister()

        self.assertEqual(_TARGET_HOOKS.get(str(OrderStatus.REFUNDED), []), [])
        self.assertEqual(_TARGET_HOOKS.get(str(OrderStatus.COMPLETED), []), [])


# ══════════════════════════════════════════════════════════════════════════
# 二次退单 + 退单回池留痕：业务方裁定给 `REFUNDED` 开了回池边 ⇒ 订单可以
# `REFUNDED → 池内 → 重新被接走 → 再交单 → 再过审 → 再退单`。
# 每一轮的退单钩子都必须把「清 0 / 冲减」完整跑一遍（清 0 在第 3 步之前 ⇒ 纯退单不写行），
# 终态不变量是客服终身合计 `0.00`（见 `SecondFullRefundTerminalStateTests`）。
# ══════════════════════════════════════════════════════════════════════════


class _SecondRoundFixture(_FullRefundFixture):
    """把「第二轮」需要的提成配置补齐（打手 60% 订单价 / 客服 20% 利润）。"""

    def setUp(self):
        super().setUp()
        StaffProfile.objects.create(
            user=self.taker_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("60.00"),
        )
        StaffProfile.objects.create(
            user=self.sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("20.00"),
        )

    def _first_round(self, *, refund_fee="0.00"):
        """第一轮完成态：`A=100 / 接单价=100 / T=60 / r=20%` ⇒ 客服 `8.00`。"""
        order = self._order(
            hall="100.00", refund_fee=refund_fee,
            sales_user=self.sales_user,
        )
        sales = self._sales(order)
        self._taker(order)
        return order, sales

    def _second_round(self, order):
        """第二轮：`/assign` → `/submit` → `/review` 三个端点体的**生产调用**。

        ⚠️ 这里的四步与 `order_views.assign` / `order_views.submit` /
        `performance_views.review` 的函数体逐一对应（端点层可达性由
        `SecondRefundEndToEndTests` 用真实 HTTP 再钉一次）。
        """
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes={"order.change_status"}, actor="cs-2")
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        set_taker(main, self.taker_user)
        bonus = commission_amount(
            effective_taker_price(main), commission_percent_of(self.taker_user)
        )
        apply_transition(
            order, OrderStatus.PENDING_REVIEW, nodes={"order.change_status"}, actor="taker-2"
        )
        main.bonus_amount = bonus
        main.review_status = ReviewStatus.PENDING
        main.save(update_fields=["bonus_amount", "review_status", "updated_at"])
        main.review_status = ReviewStatus.APPROVED
        main.save(update_fields=["review_status", "updated_at"])
        apply_transition(
            order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"}, actor="reviewer-2"
        )
        main.refresh_from_db()
        return main

    def _lifetime(self, sales):
        """客服终身合计 = `bonus_amount + Σ全部调整`。"""
        return sales.bonus_amount + sum(
            row.amount for row in PerformanceAdjustment.objects.filter(performance=sales)
        )


class SecondFullRefundTerminalStateTests(_SecondRoundFixture):
    """**B4**（spec §4.2）：多轮「退单 → 回池 → 重做完成 → 再退单」⇒ 客服**终态不变量**成立。

    未结账的行在第 3 步**之前**就被清 0 ⇒ 两格的 `deduction` 都是 0 ⇒ 一条 `FULL_REFUND`
    都不写。本类钉的**不是**「又冲了一次」，而是**终态**：两列清 0、链余额 0、应付净额 0、
    终身合计 0、`OrderRefund` 一行都没有。

    代数（每一格都按「清 0 在先、第 3 步照旧跑」）：

    | 时刻 | 该行 `bonus_amount` | 链余额 | 终身合计 |
    |---|---|---|---|
    | 第一轮 `COMPLETED` | `8.00` | `0.00` | `8.00` |
    | 第一次退单 | `0.00`（清 0） | `0.00`（无需冲减） | `0.00` |
    | 回池（等额反向） | `0.00` | `0.00` | `0.00` |
    | 第二轮 `COMPLETED` | `8.00` | `0.00` | `8.00` |
    | 第二次退单 | `0.00`（清 0） | `0.00`（无需冲减） | `0.00` |

    退单不写 `refund_fee`，所以 `OrderRefund` 一行都没有；纯退单也不写冲减行。

    判别力：下面每条断言各自都能失败——`assertEqual(full, [])` 若退单又开始写事件行就会红，
    终态四条（两列 / 链 / 应付 / 终身合计）任一处被改坏也会红。
    """

    def test_the_second_refund_zeroes_the_lifetime(self):
        order, sales = self._first_round()
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")
        release_taker(order, nodes={"order.change_status"}, actor="cs-1")
        self._second_round(order)

        # 阳性对照：第二轮**真的**重新拿到了非零佣金（否则下面的冲减可能来自别处）。
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(self._lifetime(sales), Decimal("8.00"))

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-2")

        sales.refresh_from_db()
        full = list(
            PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).order_by("id")
        )
        # **未结账**的行两次退单都在第 3 步之前被清 0 ⇒ 两格的 `deduction` 都是 0
        # ⇒ **一条 `FULL_REFUND` 都不写**；本类钉的是**终态不变量**：
        #   终身合计 0.00、链余额 0、应付 0.00（⛔ 不放宽、也不留恒真断言）。
        self.assertEqual(full, [])
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        # 两次退单 + 一次回池都**不写** `OrderRefund` 行。
        self.assertEqual(list(OrderRefund.objects.filter(order=order).order_by("id")), [])


class SecondFullRefundEventTests(_SecondRoundFixture):
    """每次退款事件都**不留** `OrderRefund` 行 / `order.refund` 审计行
    （多轮链路上这两张表**恰好 0 行**）。

    `refund_fee = Σ OrderRefund.amount` 这条**不变式本身仍然成立**；能演练它的路径只剩
    「部分退款 + 回池冲销」那一半（见下面第二条用例）；退单那条路径既不写行、也不改列。

    判别力变异：恢复「退单建 `OrderRefund(kind=FULL)` 事件行」⇒ 本类两条都红。
    """

    def test_the_two_refund_rounds_write_no_refund_row_and_no_refund_audit(self):
        order, sales = self._first_round()
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")
        release_taker(order, nodes={"order.change_status"}, actor="cs-1")
        self._second_round(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-2")

        # 阳性对照是**终态**：两列清 0、链余额 0、应付 0.00、终身合计 0.00
        #（每条都能失败；冲减额恒 0 ⇒ 一条冲减行都不写）。
        sales.refresh_from_db()
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).count(),
            0,
        )
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(list(OrderRefund.objects.filter(order=order).order_by("id")), [])
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            0,
        )
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_the_invariant_survives_a_partial_refund_before_the_full_refund(self):
        """`refund_fee = Σ OrderRefund.amount` 对**部分退款 + 回池冲销**仍然成立。

        链路（D-0181 后的实测值）：部分退款 `30`（`PARTIAL` / `30.00` + 冲减 `-6.00`）
        → 退单（目标式 `-2.00`，**不写订单级行**）→ 回池（`refund_fee = 30.00 != 0`
        ⇒ `REVERSAL` / `-30.00` + 等额反向 `+8.00`）→ 重做 → 再退单（目标式 `-8.00`）
        ⇒ `Σ = 30 − 30 = 0 = refund_fee`。

        全额退款不再产生事件行。能演练这条不变式的只剩「部分退款 + 回池冲销」。
        退单那条路径既不写行、也不改列，不变式本身仍然成立。
        """
        order, sales = self._first_round()
        refund_set_total(order, Decimal("30.00"), "退差价", "m1-partial-1", "cs-1")
        # ⚠️ 夹具回读一次：判据读的是库里的真值，⛔ 不依赖调用方实例被改写
        #    （真实 API 路径每个请求都重新取单，所以只有本用例需要这一行）。
        order.refresh_from_db()
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")
        release_taker(order, nodes={"order.change_status"}, actor="cs-1")
        self._second_round(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-2")

        order.refresh_from_db()
        refunds = list(OrderRefund.objects.filter(order=order).order_by("id"))
        self.assertEqual(
            [(row.kind, row.amount) for row in refunds],
            [("SET", Decimal("30.00")), ("REVERSAL", Decimal("-30.00"))],
        )
        # 不变量的「部分退款 + 回池冲销」这一半（退单那一半已不写行）。
        self.assertEqual(sum(row.amount for row in refunds), order.refund_fee)
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        sales.refresh_from_db()
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))


class SecondFullRefundIdempotencyTests(_SecondRoundFixture):
    """幂等守护：同一次退款**手工重跑**不许加行、不许改钱、不许抛异常。

    ⚠️ 重跑只可能来自**直接调用钩子**：`apply_transition(order, REFUNDED)` 打在
    已在 `REFUNDED` 的订单上会被 `IllegalTransition` 拒掉（没有自环）——钩子根本不会跑。
    所以本用例直接调 `on_order_refunded(order, OrderStatus.REFUNDED)`，这正是既有
    `FullRefundIdempotencyTests` 用的形状（「订单本来就在 REFUNDED 上」= 重跑）。

    判别力变异：把第 3 步的「本次冲减额 = 0 ⇒ 不写」判据去掉
    （改成无条件写一条）⇒ 两次重跑各多一行 `FULL_REFUND` ⇒ 本用例红。
    """

    def test_rerunning_the_second_refund_adds_nothing(self):
        order, sales = self._first_round()
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")
        release_taker(order, nodes={"order.change_status"}, actor="cs-1")
        self._second_round(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-2")

        before = {
            "refunds": OrderRefund.objects.filter(order=order).count(),
            "audit": OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            "adjustments": PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).count(),
        }
        order.refresh_from_db()
        refund_fee_before = order.refund_fee

        # 重跑两次：**不抛异常**（savepoint 语义：撞唯一约束也要能继续）。
        on_order_refunded(order, OrderStatus.REFUNDED)
        on_order_refunded(order, OrderStatus.REFUNDED)

        order.refresh_from_db()
        sales.refresh_from_db()
        self.assertEqual(
            OrderRefund.objects.filter(order=order).count(), before["refunds"]
        )
        self.assertEqual(
            OperationLog.objects.filter(action="order.refund", order_no=order.order_no).count(),
            before["audit"],
        )
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).count(),
            before["adjustments"],
        )
        self.assertEqual(order.refund_fee, refund_fee_before)
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))


class PoolEntryTakerTraceTests(_SecondRoundFixture):
    """退单时把原接单人 + **真实原金额**留痕；别的来源态不留。

    主留痕在 `refunds.on_order_refunded` 的第 1.5 步（第 2 步清零**之前**）写，
    `reason` 里是**真实原金额**；`performance._record_taker_before_pool_entry`
    （回池那一刻）只是**兜底**，只服务「绕开退单钩子」的行，并按 `TAKER_TRACE_PREFIX`
    判重 ⇒ 正常链路**恰好一条**。

    主留痕写在清零之前，`reason` 里是原金额。写在清零之后会变成 `原金额 0.00`。

    判别力变异：① 把主留痕挪到第 2 步之后 ⇒ `reason` 变 `原金额 0.00` ⇒ 断言
    `assertIn("原金额 60.00", …)` 红；② 去掉兜底的「已有即不写」判据 ⇒ 回池后再写一条
    ⇒ 断言「回池后仍是 1 条」红（实测 2）。
    """

    def test_reentering_the_pool_after_a_refund_leaves_the_former_taker(self):
        order, sales = self._first_round()
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")

        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker_user.id)      # 前置：退单**保留** user
        # ⭐ 主留痕：清零**之前**写下，恰好一条，且带**真实原金额**（不是 0.00）。
        traces = list(PerformanceAdjustment.objects.filter(performance=main))
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace.kind, "MANUAL")
        self.assertEqual(trace.amount, Decimal("0.00"))
        self.assertEqual(trace.settle_status, "未结账")
        self.assertIsNone(trace.refund_id)
        self.assertEqual(trace.operator, "cs-1")               # 退款那一次的操作人
        self.assertIn("taker-full", trace.reason)
        # `原金额` 后面必须**紧接着**真实值（`60.00`）——写成 `0.00` 就是落点又跑到了清零之后。
        self.assertIn("原金额 60.00", trace.reason)
        # 前置：第 2 步**真的**把它清零了（留痕不是「金额没清」造出来的假象）。
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertEqual(main.temp_amount, Decimal("0.00"))

        # ⭐ D2（spec §4.1.2 例外①）：退单**保留** `user` ⇒ 回池要走「显式清除接单人」
        #    这个产品动作，否则会被新闸拒。本用例钉的仍是「回池**不**重复写那条兜底留痕」。
        release_taker(order, nodes={"order.change_status"}, actor="cs-2")

        main.refresh_from_db()
        traces = list(PerformanceAdjustment.objects.filter(performance=main))
        # ⭐ 回池后**仍是 1 条**：兜底没有重复写（它按 `TAKER_TRACE_PREFIX` 判重）。
        self.assertEqual(
            len(traces),
            1,
            msg=f"回池后应仍只有主留痕那一条（兜底按前缀判重）：{[t.reason for t in traces]}",
        )
        self.assertEqual(traces[0].id, trace.id)
        self.assertEqual(traces[0].operator, "cs-1")           # 那一行没有被回池改写
        # 回退本身没被改：`user` 清空、金额归零（D-0032 / spec §4.1.2）。
        self.assertIsNone(main.user_id)
        self.assertEqual(main.bonus_amount, Decimal("0.00"))

    def test_a_refunded_order_without_the_hook_keeps_the_real_amount_in_the_reason(self):
        """绕开退单钩子（ORM 直接把订单置成 `REFUNDED`，老数据/夹具的形态）⇒ **兜底**生效。

        ⚠️ 这条**不是**在鼓励绕过钩子：它证明兜底写的金额来自**行上的真实值**
        （`60.00`），而不是被硬编码成 `0.00`；也证明主留痕缺席时兜底**确实会写**
        （否则判重变成「一条都不写」也照样绿）。
        """
        order = self._order(
            hall="100.00", sales_user=self.sales_user
        )
        self._sales(order)
        self._taker(order, bonus_amount=Decimal("60.00"))
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.REFUNDED)
        order.refresh_from_db()
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        # 前置：钩子没跑 ⇒ 行上既没有留痕、金额也还在（否则下面的断言是空的）。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=main).count(), 0
        )
        self.assertEqual(main.bonus_amount, Decimal("60.00"))

        # ⭐ D2（spec §4.1.2 例外①）：兜底那条路要跑，前提也是「行上有接单人」
        #    ⇒ 与产品动作同口径（显式清除接单人）。
        release_taker(order, nodes={"order.change_status"}, actor="cs-9")

        main.refresh_from_db()
        traces = list(PerformanceAdjustment.objects.filter(performance=main))
        self.assertEqual(len(traces), 1)
        self.assertIn("taker-full", traces[0].reason)
        self.assertIn("60.00", traces[0].reason)

    def test_a_refunded_order_without_a_taker_writes_no_trace(self):
        """负向：MAIN 行**没有接单人** ⇒ 退单与回池**都不写**这条留痕。

        ⚠️ 判据必须两处都断：主留痕（退单那一刻）与兜底（回池那一刻）各有一个
        `user_id is None` 的出口，只断一侧会漏掉另一侧。
        """
        order = self._order(
            hall="100.00", sales_user=self.sales_user
        )
        self._sales(order)
        main = self._taker(order, user=None)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor="cs-1")
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=main).count(), 0
        )

        # ⚠️ D2（spec §4.1.2）：本条 MAIN 行**刻意没有接单人**（上面 `user=None`）
        #    ⇒ 新判据不命中（它只拦「MAIN 行 `user` 非空」），这里保持不带开关的原调用：
        #    它**不是**「显式清除接单人」这个动作，⛔ 不要为了凑形状把 `release_taker` 抄进来。
        apply_transition(order, OrderStatus.UNASSIGNED, nodes={"order.change_status"}, actor="cs-2")
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=main).count(), 0
        )

    def test_pool_entry_from_other_statuses_writes_no_trace(self):
        """负向：`ISSUE → UNASSIGNED` **不写**这条留痕（它只挂在退单那条边上）。"""
        order = self._order(
            status=OrderStatus.ISSUE, hall="100.00",
            sales_user=self.sales_user,
        )
        self._sales(order)
        main = self._taker(order, bonus_amount=Decimal("60.00"))

        # ⭐ D2（spec §4.1.2 例外①）：`ISSUE → UNASSIGNED` 也不是 D2 的例外，
        #    但这条行上有接单人 ⇒ 仍须走「显式清除接单人」那一次调用。
        release_taker(order, nodes={"order.change_status"}, actor="cs-3")

        main.refresh_from_db()
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=main).count(), 0
        )
        self.assertIsNone(main.user_id)                          # 回退照旧发生


class SecondRefundEndToEndTests(_SecondRoundFixture):
    """**端到端**回归：整条链走真实 HTTP，第二次退单必须 **200 + code 0**。

    本用例把「链真的能跑完 + 第二次退单真的又冲了一次提成」钉在 HTTP 层。

    回池把上一轮冲减链等额反向，所以第二轮的 `bonus_amount` 是 `8.00`。
    退单不再写 `refund_fee`、不建 `OrderRefund` 行。
    本用例钉的是钱：两次各冲一次、终身合计 0。
    """

    def setUp(self):
        super().setUp()
        # 端到端链路里打手是超级管理员本人（`/submit` 要求「这单是你接的」）。
        StaffProfile.objects.create(
            user=self.admin_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("60.00"),
        )

    def test_the_whole_chain_over_http(self):
        order = self._order(
            status=OrderStatus.UNASSIGNED, hall="100.00",
            sales_user=self.sales_user,
        )
        create_performance_for_order(order)
        url = self._url(order)

        def post(path, body, client=None):
            return (client or self.admin).post(
                path, body, content_type="application/json"
            ).json()

        # 打手 = 超级管理员本人（`/submit` 要求「这单是你接的」）。
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/assign/",
                 {"user_id": self.admin_user.id})["code"],
            0,
        )
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/submit/", {})["code"], 0
        )
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(
            post(f"/api/business/performances/{main.id}/review/",
                 {"approved": True})["code"],
            0,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, "COMPLETED")
        self.assertEqual(
            post(url, {"target": "REFUNDED"})["code"], 0
        )
        # ⭐ 退单钩子**保留** `user`
        #    （见 `test_reentering_the_pool_after_a_refund_leaves_the_former_taker` 的前置哨兵）
        #    ⇒ 回池这一跳必须走**产品动作** `/clear-taker/`（spec §4.1.2 例外①），
        #    ⛔ 不能再拿通用 `/transition/` 当清人用（那条路现在会被 D2 判据拒成 1002）。
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/clear-taker/", {})["code"], 0
        )

        # 第二轮
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/assign/",
                 {"user_id": self.admin_user.id})["code"],
            0,
        )
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/submit/", {})["code"], 0
        )
        main.refresh_from_db()
        self.assertEqual(
            post(f"/api/business/performances/{main.id}/review/",
                 {"approved": True})["code"],
            0,
        )
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        sales.refresh_from_db()
        # 重做那一轮真的重新拿到 `8.00`（只靠 `bonus` 那一列；第一轮纯退单
        # 不写冲减行 ⇒ 链本来就是空的，回池的等额反向无事可做）。
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))     # 前置：真的重算过
        self.assertEqual(self._lifetime(sales), Decimal("8.00"))

        body = post(url, {"target": "REFUNDED"})
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["status"], "REFUNDED")

        # ⭐ D-0181：两次退单 + 一次回池**都不写** `OrderRefund` 行，
        #    也不写 `order.refund` 审计（退单的账在 `order.transition` 上）。
        self.assertEqual(
            [row.kind for row in OrderRefund.objects.filter(order=order)], []
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            0,
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.transition", order_no=order.order_no
            ).count(),
            2,                                    # 两次退单各一行（回池不走本钩子外的这条审计）
        )
        # 两次退单都在第 3 步之前把未结账的行清 0 ⇒ 冲减额恒 0 ⇒ 一条 `FULL_REFUND`
        # 都不写；判据钉**终态**（⛔ 不放宽）：终身合计 0.00、链 0、应付 0.00、两列 0。
        sales.refresh_from_db()
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain(sales), Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales, kind="FULL_REFUND"
            ).count(),
            0,
        )


# ══════════════════════════════════════════════════════════════════════════
# savepoint / 唯一约束哨兵的现位置：回池冲销的 `REVERSAL` 事件行仍带 `request_id`
# 唯一约束，同形哨兵见 `tests/test_p4_redo_round.py` 的
# `PoolEntryIdempotencyTests.test_a_duplicate_reversal_key_does_not_poison_the_outer_transaction`
# （同样伪造一次并发、同样断言「外层事务还能继续写」）。
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# 本段（`UnsettledSalesClearedToZeroTests` 及其后）的裁定：退单清 0 **只对「未结账」
# 的行生效**（已结账的行保持不动并留痕）。
# 夹具沿用 `_FullRefundFixture`（`A=100 / T=60 / r=20%`），期望值一律**字面十进制数**。
# ══════════════════════════════════════════════════════════════════════════
class UnsettledSalesClearedToZeroTests(_FullRefundFixture):
    """A-1 / A-3：退单后**未结账**的客服行「预计」「生效」都清 0，且应付净额 = 0.00。"""

    def test_a_pure_refund_zeroes_both_columns_and_the_payable(self):
        """A-1：纯退单（无部分退款）⇒ 两列 `0.00`，`payable_net == 0.00`（⛔ 不许变负）。

        ⚠️ 夹具的客服行**刻意先给非 0**（`temp`/`bonus` 都是 `8.00`）：从 0 到 0 的断言没有
        判别力。**杀法**：删掉 `refunds._clear_unsettled_sales_amounts(order, …)` 那一句 ⇒
        两列停在 `8.00` ⇒ 红。

        本用例还钉**留痕**——清 0 之前在**数据库行**上多一条
        `MANUAL amount=0.00`（`AGENTS.md` §5-G：断言留痕要断言数据库行）。
        **杀法（留痕那一半）**：去掉 `_clear_unsettled_sales_amounts` 里那次
        `PerformanceAdjustment.objects.create(...)` ⇒ 下面 `traces.count() == 1` 红。
        """
        order = self._order()
        sales = self._sales(order, temp_amount=Decimal("8.00"))
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))     # ⛔ 变负会让 /settle 触发 1011

        # 清 0 的留痕（与 MAIN 侧 `_record_taker_trace_before_clearing` 同形）。
        traces = PerformanceAdjustment.objects.filter(
            performance=sales, kind=PerformanceAdjustment.Kind.MANUAL
        )
        self.assertEqual(traces.count(), 1)
        trace = traces.get()
        self.assertEqual(trace.amount, Decimal("0.00"))       # ⛔ 非 0 会改 payable 的 Σ
        self.assertIsNone(trace.refund_id)
        self.assertEqual(trace.settle_status, SettleStatus.UNSETTLED)
        self.assertIn("退单清空客服行：", trace.reason)
        # ⚠️ **本机实测读数**：`退单清空客服行：原预计 8.00 / 原生效 8.00`——**两个原值都在**。
        #    ⚠️ 之所以 `原预计` 也是真值 `8.00`：本夹具订单的 `sales_user` 为空
        #    （`_order()` 默认 `sales_user=None`）⇒ `refresh_customer_estimate` 在第 1 条
        #    就早退、**没有**先把 `temp` 写成 `0.00`。
        # 清 0 在 refresh_customer_estimate 之前，所以挂了 sales_user 时「原预计」也是清零前的真值。
        self.assertIn("原预计 8.00", trace.reason)
        self.assertIn("原生效 8.00", trace.reason)

    def test_the_clear_trace_records_the_pre_clear_estimate_when_the_order_has_a_sales_owner(
        self,
    ):
        """订单**挂了 `sales_user`** 时，留痕里的「原预计」必须是**清零前的真值**
        （⛔ 不是被 `refresh_customer_estimate` 第 2.5 条先写成 `0.00` 的那个值）。

        清 0 落在 `refunds._clear_unsettled_sales_amounts(...)`，位置在
        `refresh_customer_estimate(order)` **之前**（清 0 之后 `refresh` 在 `REFUNDED` 上
        本来也只会写 `0.00` ⇒ 无副作用的幂等），留痕因此读到真值——
        留痕的用途就是记「清零前的真值」，记成 `0.00` 是一句假话。

        ⚠️ 夹具**刻意挂 `sales_user`、且 `temp_amount` 非 0**（`8.00`）：`sales_user` 为空时
        `refresh_customer_estimate` 第 1 条就早退、根本不会先把 `temp` 写成 `0.00`
        ⇒ 那种夹具**看不出**这条判据的意义（本类第一条用例的夹具正是那一种）。
        **杀法**：把清 0 那句挪回 `refresh_customer_estimate` **之后**（或按旧顺序重排）
        ⇒ 留痕变成 `原预计 0.00` ⇒ 本用例红（读数见 `docs/STATUS.md` 本线条目）。
        """
        order = self._order(sales_user=self.sales_user)
        sales = self._sales(order, temp_amount=Decimal("8.00"))
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        trace = PerformanceAdjustment.objects.get(
            performance=sales, kind=PerformanceAdjustment.Kind.MANUAL
        )
        # ⛔ 逐字钉住整条 `reason`：这是「原预计 = 清零前真值」唯一的证据行。
        self.assertEqual(trace.reason, "退单清空客服行：原预计 8.00 / 原生效 8.00")

    def test_a_partial_refund_then_a_full_refund_ends_at_zero(self):
        """A-3（**先部分退款、再全额退单**）：`temp == bonus == 0`、**链余额 == 0**、
        `payable_net == 0.00`。

        代数（`A=100 / T=60 / r=20%`）：部分退款 `30.00` ⇒ 冲减 `−6.00`；
        退单 ⇒ 第 2.7 步清两列（清 0 在先），第 3 步读到的 `net` 只剩链 ⇒ 它写
        `−chain = +6.00` 把链归零。
        链余额 = `−6.00 + 6.00` = `0.00`；终身合计 = `0.00`（`bonus` 已清）
        `+ (−6.00 + 6.00)` = `0.00`。
        **杀法**：清 0 之后**跳过**第 3 步 ⇒ 链上留着 `−6.00` ⇒ `payable_net` 变**负** ⇒ 红。
        """
        order = self._order()
        sales = self._sales(order, temp_amount=Decimal("8.00"))
        self._taker(order)
        refund_set_total(order, Decimal("30.00"), "退差价", "task-a3-partial", "cs-actor")

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        # 清 0 落在第 3 步**之前** ⇒ 这一条（链非空）写 `+6.00` 把链归零；
        # 而**纯退单**那一格（链本来为空）会 `deduction == 0` ⇒ **不写行**。
        # 本用例只钉**代数终态**，不钉行数。
        chain = sum(
            (
                row.amount
                for row in PerformanceAdjustment.objects.filter(
                    performance=sales,
                    kind__in=(
                        PerformanceAdjustment.Kind.PARTIAL_REFUND,
                        PerformanceAdjustment.Kind.FULL_REFUND,
                    ),
                )
            ),
            Decimal("0.00"),
        )
        self.assertEqual(chain, Decimal("0.00"))
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))

    def test_a_settled_fixed_deduction_row_still_computes_a_negative_payable(self):
        """A-4（**已结账**档）：固定额扣点 `5.00` + 已结账的客服行 ⇒ 退单后 `payable_net`
        **算得出来**，但等于 **`−5.00`（负数）**；⛔ 不是 `0.00`。

        ⚠️ **已结账 + 扣点（比例或固定额都算）⇒ 应付 = −扣点额**；加了 `is_settled` 闸
        （已结账行的 `temp` 保留）之后的连带代价，逐条登记在 `docs/DECISIONS.md` 的
        `D-0187`（触发条件 / 现象 / 影响 / 为何不修）。**比例档**的成对判据见同文件
        `SettledRowDeductionResidualTests`。业务方裁定（逐字「保持现状即可 只登记」）
        ⇒ 只钉现状，不改算式。
        ⛔ **不要去把它改成「按 `payable_net` 归零」的算式**；
        ⛔ **不要**为了消掉负数去清已结账行的扣点列（违反「已结账的行保持不动」的裁定）。

        代数：已结账的行**一个字节都不动**⇒ `temp = bonus = 8.00`、扣点 `5.00` 原样；
        第 3 步照旧写 `−(bonus + chain) = −8.00`；应付 = `8.00 − 5.00 + (−8.00) = −5.00`。
        **阳性对照**：它**不抛** `AmountError`（下面这一句能跑完本身就是判据）。
        """
        order = self._order()
        sales = self._sales(
            order,
            temp_amount=Decimal("8.00"),
            settle_status=SettleStatus.SETTLED,
            deduct_mode="1",
            deduct_value=Decimal("5.00"),
        )
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        # 已结账的行**一个字节都不动**：两列与扣点列都原样。
        self.assertEqual(sales.temp_amount, Decimal("8.00"))
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(sales.deduct_mode, "1")
        self.assertEqual(sales.deduct_value, Decimal("5.00"))
        # 它照旧由第 3 步写 −(bonus + chain) = −8.00；应付 = 8.00 − 5.00 + (−8.00) = −5.00。
        # ⚠️ 这正是「已结账的行由**人工**去处理」那一档（钱可能已经付出去了），
        #    **不改**它的应付口径；下面钉「算得出来（不抛）、且真的是负数」。
        self.assertEqual(
            PerformanceAdjustment.objects.get(
                performance=sales, kind="FULL_REFUND"
            ).amount,
            Decimal("-8.00"),
        )
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("-5.00"))
        self.assertLess(net, Decimal("0.00"))    # ⛔ 这一档是**负数**（见 `D-0187` 的残差段）

    def test_the_real_adjust_path_configures_the_fixed_deduction_then_refund_clears_it(self):
        """⭐ 走**真实业务路径** `/adjust/` 配固定额扣点 ⇒ 退单 ⇒ `payable_net == 0.00`
        （spec §4-4 那一格）。

        ⚠️ 手工 ORM 摆出来的夹具**不能证明**这条生产路径可达，所以本条走真实端点：
        `POST /api/business/performances/{pk}/adjust/` 配
        `deduct_mode="1"` / `deduct_value="5.00"`（端点自己会用 `deduct_amount` 校验扣点算得出，
        `value <= temp_amount` 才放行）⇒ 再退单。

        代数（`total=130 / 打手 17.60 / 客服率 40%`）：客服预计 `(130 − 17.60) × 40% = 44.96`；
        `/adjust/` 配固定额 `5.00` ⇒ 应付 = `44.96 − 5.00 = 39.96`（阳性对照）；
        退单 ⇒ 未结账的行清两列 + 清扣点列 + 留痕 ⇒ 应付 = `0.00 − 0.00 + 0.00 = 0.00`。
        **杀法**：去掉清 0 时那句连扣点列一起清 ⇒ `payable_net` 抛 `AmountError` ⇒ 红。
        """
        order = self._order(total="130.00", hall="88.00")
        self._taker(order, temp_amount=Decimal("17.60"), bonus_amount=Decimal("17.60"))
        sales = self._sales(
            order, temp_amount=Decimal("44.96"), bonus_amount=Decimal("44.96")
        )
        client = make_logged_in_client(
            username="adjust-cs-real-path", groups=("客服",),
            nodes=("order.submit_payout_price",),
        )

        body = client.post(
            f"/api/business/performances/{sales.pk}/adjust/",
            {"deduct_mode": "1", "deduct_value": "5.00", "reason": "固定额扣点"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        sales.refresh_from_db()
        self.assertEqual(sales.deduct_mode, "1")                # 阳性对照：真配上了
        self.assertEqual(sales.deduct_value, Decimal("5.00"))
        self.assertEqual(sales.temp_amount, Decimal("44.96"))
        # 阳性对照：退单前这一行**算得出来**且是正数（否则下面「退单后为 0」没有判别力）。
        self.assertEqual(_payable_of(sales), Decimal("39.96"))

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.deduct_mode, "")                 # 扣点列被一起清
        self.assertEqual(sales.deduct_value, Decimal("0.00"))
        self.assertEqual(_payable_of(sales), Decimal("0.00"))

    def test_a_clean_unsettled_fixed_deduction_row_is_cleared(self):
        """A-4 的另一半：未结账 + 固定额扣点 ⇒ 退单时扣点列被一起清，
        `payable_net` 算得出来且等于 `0.00`（⛔ 不抛 `AmountError`）。

        本条的定位是**另一种数据形态**：ORM 直接摆出「未结账 + `temp=8.00` + 固定额 `5.00`」
        ——与 DB 里**存量**行的形状一致（结过账、后来状态被改回未结账的行）；
        真实业务路径那一条见
        `test_the_real_adjust_path_configures_the_fixed_deduction_then_refund_clears_it`。
        **杀法**：去掉扣点列的清理 ⇒ `payable_net` 抛 `AmountError`
        （实测原文：`扣点金额不能超过预计绩效总计（0.00），当前 5.00`）⇒ 红。
        """
        order = self._order()
        sales = self._sales(order, temp_amount=Decimal("8.00"))
        Performance.objects.filter(pk=sales.pk).update(
            deduct_mode="1", deduct_value=Decimal("5.00")
        )
        sales.refresh_from_db()
        self._taker(order)

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.deduct_mode, "")
        self.assertEqual(sales.deduct_value, Decimal("0.00"))
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("0.00"))


class UnsettledClearIdempotencyTests(_FullRefundFixture):
    """A-6：同一张单**手工重跑**退单钩子 ⇒ 不多写第二条调整行（与幂等判据同形）。

    连带判据：清 0 的 `MANUAL amount=0.00` **留痕**也**幂等**——
    重跑不写第二条（行数不变、`reason` 一字不变）。
    """

    def test_rerunning_after_the_clear_writes_nothing_more(self):
        order = self._order()
        sales = self._sales(order, temp_amount=Decimal("8.00"))
        self._taker(order)
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})
        before = {
            "count": PerformanceAdjustment.objects.filter(performance=sales).count(),
            "traces": list(
                PerformanceAdjustment.objects.filter(
                    performance=sales, kind=PerformanceAdjustment.Kind.MANUAL
                )
                .order_by("id")
                .values_list("id", "amount", "reason")
            ),
            "bonus": Performance.objects.get(pk=sales.pk).bonus_amount,
            "temp": Performance.objects.get(pk=sales.pk).temp_amount,
        }
        # 阳性对照：第一次退单**真的**留了痕（否则「重跑不多写」没有判别力）。
        self.assertEqual(len(before["traces"]), 1)

        on_order_refunded(order, OrderStatus.REFUNDED)      # 手工重跑（from_status == REFUNDED）

        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(),
            before["count"],
        )
        # 第二条留痕**没有**被写出来（行 id / 金额 / `reason` 逐项不变）。
        self.assertEqual(
            list(
                PerformanceAdjustment.objects.filter(
                    performance=sales, kind=PerformanceAdjustment.Kind.MANUAL
                )
                .order_by("id")
                .values_list("id", "amount", "reason")
            ),
            before["traces"],
        )
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, before["bonus"])
        self.assertEqual(sales.temp_amount, before["temp"])


class SettledRowDeductionResidualTests(_FullRefundFixture):
    """**已结账 + 有扣点 + 退单 ⇒ `payable_net` 为负**：登记在案的**残差**，成对钉住两档。

    业务方裁定（逐字「保持现状即可 只登记」）：⛔ 不改 `refunds` 的冲减额算式
    （不改成「按 `payable_net` 归零」）、⛔ 不清已结账行的扣点列（违反
    「已结账的行保持不动」）⇒ 本类**只把现状钉住**，防止后来者以为它是「算错了」而随手改
    （登记见 `D-0187` 的残差段：这是 `is_settled` 闸的连带代价，代数上应付必为 `−扣点额`）。

    ⚠️ 本类钉的是「今天就是这么算」，⛔ 不是「这个数业务上是对的」（同 `E-0011` 的口径）。
    """

    def test_a_settled_percentage_deduction_row_also_computes_a_negative_payable(self):
        """**比例扣点**档（与固定额那条成对）：`temp = bonus = 8.00`、比例 `10%`、已结账。

        退单前应付 `8.00 − 0.80 = 7.20`；退单后 `8.00 − 0.80 + (−8.00) = −0.80`（**负数**）。
        ⇒ 必须有一条判据把这个负值**显式**钉住，否则「已结账行不动」这道闸的连带后果
        会**静默**漂移。
        """
        order = self._order()
        sales = self._sales(
            order,
            temp_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
            settle_status=SettleStatus.SETTLED,
            deduct_mode="0",
            deduct_value=Decimal("10.00"),
        )
        self._taker(order)

        # 阳性对照：退单**前**算得出来，且是 `7.20`（证明夹具的扣点真的生效了）。
        self.assertEqual(payable_net(sales, []), Decimal("7.20"))

        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        sales.refresh_from_db()
        # 已结账的行**一个字节都不动**：两列与扣点列都原样。
        self.assertEqual(sales.temp_amount, Decimal("8.00"))
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(sales.deduct_mode, "0")
        self.assertEqual(sales.deduct_value, Decimal("10.00"))
        # 第 3 步照旧写 `−(bonus + 链) = −8.00`；应付 = `8.00 − 0.80 + (−8.00) = −0.80`。
        self.assertEqual(
            PerformanceAdjustment.objects.get(
                performance=sales, kind="FULL_REFUND"
            ).amount,
            Decimal("-8.00"),
        )
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("-0.80"))
        # ⛔ 这一档是**负数**（见 `D-0187` 的残差段与业务方「只登记」的裁定）。
        self.assertLess(net, Decimal("0.00"))
        # 负数的绝对值 == 扣点额（比例档 = `temp × 10% = 0.80`）——这正是那条代数结论。
        self.assertEqual(-net, commission_amount(Decimal("8.00"), Decimal("10.00")))
