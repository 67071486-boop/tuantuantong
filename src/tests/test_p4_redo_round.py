"""P4-M2：把「退单 → 回池 → 重做」当成**新一轮**（业务方选定候选 A）。

    依据：brief `.superpowers/sdd/2026-09-20-p4-settlement-and-finance/task-P4-M2-brief.md` +
    `.agents/notes/proposed/architecture/2026-09-21-p4-pending-business-items.md` 的「追加二」段 +
    计划 §P4-12 必做清单 **38**。

    ## 被修的那笔钱（第 0 步探针实测，走生产 HTTP）

    `A=100`（订单总价 `total_price`，D-0142 起毛收款只看它）/ 接单价 100 / 打手 60% / 客服 20%：

    | 时刻 | `refund_fee` | 客服行 `bonus_amount` | 客服终身合计 |
    |---|---|---|---|
    | 第一轮 `COMPLETED` | `0.00` | `8.00` | `8.00` |
    | 第一次全额退款 | `100.00` | `8.00`（链上 `FULL_REFUND −8.00`） | `0.00` |
    | 回池（**修复前**） | `100.00` | `8.00` | `0.00` |
    | 第二轮（重做）`COMPLETED`（**修复前**） | `100.00` | **`−12.00`** | **`−20.00`** |

    根因（brief 与实现者两路一致）：`refund_fee` 是单向累计、没有回退路径 ⇒ 重做那一轮
    `net_received = 100 − 100 = 0`、利润 `0 − 60 = −60` ⇒ 提成 `−12.00`。
    修复 = 回池时做三件事（复位 `refund_fee` + 一条 `REVERSAL` 事件行 + 客服行反向调整），
    代数与「为什么反向行必须是 `FULL_REFUND` 而不是 brief 字面的 `MANUAL`」见
    `apps/business/refunds.py::reverse_previous_round_refund`。

    ## ⭐ D-0181 口径：退单不再写 `refund_fee`、不再建 `OrderRefund(FULL)` 行

    退单（spec `2026-10-03-refund-amount-and-restore-sync-design.md` §3-G2/G3）**一个字都不写
    `refund_fee`、也不建任何 `OrderRefund` 行** ⇒

    - 第 1/2 件（`REVERSAL` 事件行 + 复位 `refund_fee`）今天只对「**部分退款留下的**
    `refund_fee != 0`」那一档生效 ⇒ 本文件几条要点「回池确实冲销过」的用例因此**改用
    先部分退款 30** 的形状（否则那两件按余额判据自然跳过，断言会落在空集上）；
    - 第 3 件（客服行冲减链的等额反向）**照旧必须做**，判据与代数**一字未变**——
    「重做那一轮不被上一轮吞掉」这条原意仍然由它守住。

    ## 本文件的纪律

    1. **断言一律钉字面值**（E-0024）：金额写 `Decimal("8.00")`、`kind` 写 `"FULL_REFUND"` /
    `"REVERSAL"`、业务码写字面量——不引被测模块导出的常量。
    2. **审计断言打在数据库行上**（E-0009）：不写日志断言。
    3. **每条用例自带阳性对照**（前置断言），证明「副作用没发生」不是因为链根本没跑。
    4. **变异**（brief 5 组，第 3 组方向已按实测更正）见交付报告；每条都实测会红。

    ## ⭐ 回池窗口与审计读数的口径

    - **F-1**：回池窗口里客服行的 `bonus_amount` 也归零（`PoolWindowCommissionTests`）。
    修前窗口净额 `8.00` 可被财务结账 ⇒ **纯多付**。
    - **F-2**：第 3 步审计的 `from/to` 改成**真实链余额**的前后值
    （`PoolEntryAuditTests` 的 `-8.00 → 0.00`）。
    - **F-3**：`FULL_REFUND` 的**两类行**（有事件 vs 回池反向）结构哨兵
    （`FullRefundKindStructureTests`）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource, CommissionMode
from apps.accounts.models import StaffProfile
from apps.accounts.payout import payout_completed_order
from apps.business.commission import commission_percent_of
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
from apps.business.order_status import (
    IllegalTransition,
    apply_transition,
)
from apps.business.performance import (
    _on_pool_entry,
    create_performance_for_order,
    set_taker,
)
from apps.business.performance_amount import commission_amount, effective_taker_price
from apps.business.refunds import refund_set_total, reverse_previous_round_refund
from apps.business.settlement_amount import payable_net
from tests.order_rules_helpers import release_taker
from tests.p5_auth_helpers import make_logged_in_client

TRANSITION_URL = "/api/business/orders/{order_no}/transition/"
ADMIN_USERNAME = "redo-round-admin"


def _refund_chain_sum(sales):
    """该客服行「整条退款冲减链」的余额 = `Σ(PARTIAL_REFUND + FULL_REFUND)`（裁定 Z 的口径）。"""
    return sum(
        (
            row.amount
            for row in PerformanceAdjustment.objects.filter(
                performance=sales,
                kind__in=("PARTIAL_REFUND", "FULL_REFUND"),
            )
        ),
        Decimal("0.00"),
    )

class _RedoRoundFixture(TenantTestCase):
    """第一轮完成态 + 第二轮（重做）的**生产函数体**；金额与 brief 的代数表一致。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.sales_user = User.objects.create_user(username="redo-sales")
        self.taker_user = User.objects.create_user(username="redo-taker")
        StaffProfile.objects.create(
            user=self.taker_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("60.00"),
            # ⭐ 打手来源钉成「打手 App 注册」（业务方口径，D-0279）：
            # 自动结账**只对 App 注册打手生效**。本夹具的第二轮完成依赖「完成即自动
            # 入账」（`FullRefundKindStructureTests` 的第三类 = 打手回冲行）⇒ 来源必须是
            # `app`，否则那条回冲行不再产生、结构哨兵会失真。
            account_source=AccountSource.APP,
        )
        StaffProfile.objects.create(
            user=self.sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self._seq = 0

    # ── 夹具 ────────────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.COMPLETED, received="1.00", hall="100.00",
               total="100.00", refund_fee="0.00", sales_user=True):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092300000{self._seq:07d}",
            status=status,
            received_price=None if received is None else Decimal(received),
            hall_price=None if hall is None else Decimal(hall),
            total_price=Decimal(total),
            refund_fee=Decimal(refund_fee),
            sales_user=self.sales_user if sales_user else None,
        )

    def _sales(self, order, **overrides):
        """客服行：第一轮 `COMPLETED` 时确定下来的 `bonus_amount = 8.00`。"""
        fields = {
            "emp_type": EmpType.SALES,
            "user": self.sales_user,
            "temp_amount": Decimal("0.00"),
            "bonus_amount": Decimal("8.00"),
            "commission_rate": Decimal("20.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _taker(self, order, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "user": self.taker_user,
            "temp_amount": Decimal("60.00"),
            "bonus_amount": Decimal("60.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _first_round(self, **kwargs):
        """第一轮完成态：客服 `8.00` / 打手 `60.00`（`A=100 / T=60 / r=20%`）。"""
        order = self._order(**kwargs)
        sales = self._sales(order)
        self._taker(order)
        return order, sales

    def _refund(self, order, actor="cs-1"):
        """`REFUNDED` 钩子（生产调用点：通用 `/transition/`）。"""
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"}, actor=actor)

    def _pool(self, order, actor="cs-1", target=OrderStatus.UNASSIGNED):
        """回池——`P4-M2` 的三件事挂在这里。

    ⭐ 目标状态按**产品动作**取：
    - 目标 `UNASSIGNED`：走**产品动作**「显式清除当前接单人」
    （= `/clear-taker/` 那条路，`release_taker`，spec §4.1.2 例外①）——这些单上
    有接单人，旧写法（直接 `apply_transition(…, UNASSIGNED)`）会被新闸拒。
    - 目标 `WAITING_GRAB`：D2 之下**没有**这个产品动作（`/throw-to-hall/` 不在例外
    清单里）⇒ 只有「**没有接单人**」的行才到得了；调用方自己负责先把 MAIN 行的
    `user` 清空（见 `test_the_other_pool_state_also_reverses_the_previous_round`
    的前置一步）。
"""
        if str(target) == str(OrderStatus.UNASSIGNED):
            return release_taker(order, nodes={"order.change_status"}, actor=actor)
        # ⭐ 计划 ④：`WAITING_GRAB` 目标已改判独占 36（原先 16 就够）。
        # 本分支只会收到这个目标（docstring 上一段把两种目标写死了）。
        return apply_transition(
            order, target,
            nodes={"order.send_to_grab_hall"}, actor=actor,
        )

    def _second_round(self, order, *, taker_price=None):
        """第二轮的 `/assign` → `/submit` → `/review` **函数体**（同既有夹具的口径）。

    `/assign` 的写价那一半（`taker_price` 覆盖）按 `/adjust` 的函数体补：
    覆盖价 + 按 §6.1 同一口径重算 `temp_amount`。
"""
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes={"order.change_status"}, actor="cs-2")
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        set_taker(main, self.taker_user)
        if taker_price is not None:
            main.taker_price = Decimal(taker_price)
            main.temp_amount = commission_amount(
                effective_taker_price(main), commission_percent_of(self.taker_user)
            )
            main.save(update_fields=["taker_price", "temp_amount", "updated_at"])
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
        """客服终身合计 = `bonus_amount + Σ全部调整`（**全部**，含 `MANUAL`）。"""
        return sales.bonus_amount + sum(
            (row.amount for row in PerformanceAdjustment.objects.filter(performance=sales)),
            Decimal("0.00"),
        )

    def _refunds(self, order):
        return list(OrderRefund.objects.filter(order=order).order_by("id"))

    def _payable_net(self, sales):
        """应付净额 = P4-7 结账口径（`bonus_amount − 扣点 + Σ未结账调整`）。

    与 `settlement.settle_performances` 的取数一致：**只取未结账调整**
    （已结账的调整不再进入下一次结账）。
"""
        return payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )

    def _sales_adjustments(self, sales):
        return list(
            PerformanceAdjustment.objects.filter(performance=sales).order_by("id")
        )

    def _sales_adjustment_shapes(self, sales):
        """该客服行的全部调整的 `(kind, amount)` 形状（判据口径）。

    ⚠️ **为什么还要它**：退单清 0 **现在会留痕**（一条 `MANUAL amount=0.00`，
    `SALES_TRACE_PREFIX`）⇒ 原来那些 `assertEqual(self._sales_adjustments(sales), [])`
    必须**如实**写出这条留痕，⛔ 不许把断言范围缩成「只看退款链」来让它变绿
    （那会把「不再写噪声行」这条哨兵悄悄削弱）。
"""
        return [(row.kind, row.amount) for row in self._sales_adjustments(sales)]


class RedoRoundMoneyTests(_RedoRoundFixture):
    """**T1（核心）**：退单 → 回池 → 重新做完 ⇒ `refund_fee` 归零、新一轮真的赚到提成。

    判别力变异：① 去掉第 2 步（不复位 `refund_fee`）⇒ 本组红（`8.00` 变 `−12.00`）；
    ② 去掉第 3 步（不反向冲减链）⇒ 终身合计红（`0.00` 而不是 `8.00`）。
"""

    def test_the_redo_round_earns_the_new_commission(self):
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        self._second_round(order)

        order.refresh_from_db()
        # 纯退单不写 `refund_fee`（一直是 `0.00`），也不建 `OrderRefund(kind=FULL)` 行。
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(self._refunds(order), [])

        # 第 3 件（**这一件一字未变**）：第一轮的冲减链被等额反向
        # ⇒ 新一轮的提成不再被它吞掉。
        # ⚠️ 业务方 Q1 口径：第一轮纯退单**不写** `−8.00`
        # 行（未结账的行被清 0 ⇒ 冲减额恒 0）⇒ 回池时链本来就是空的、反向也**无事可做**
        # （旧口径这里是 `−8.00 / +8.00` 两行；在 git 历史里）。
        # 本用例钉的核心改成**新一轮真的赚到**：`bonus 8.00`、终身合计 `8.00`。
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(self._lifetime(sales), Decimal("8.00"))
        # ⭐ 第一轮纯退单**不写**退款冲减行，但**留一条清 0 的痕**
        # （`MANUAL amount=0.00`，`reason` 带原 `temp`/`bonus`）⇒ 如实写成一条。
        self.assertEqual(
            self._sales_adjustment_shapes(sales), [("MANUAL", Decimal("0.00"))]
        )

    def test_the_asymmetric_redo_round_uses_the_new_price(self):
        """非对称变体：第二轮接单价改成 `80.00` ⇒ 打手 `48.00`、客服 `10.40`（字面值）。"""
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        main = self._second_round(order, taker_price="80.00")

        self.assertEqual(main.bonus_amount, Decimal("48.00"))    # 80.00 × 60%
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("10.40"))   # (100 − 48) × 20%
        self.assertEqual(self._lifetime(sales), Decimal("10.40"))
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_the_whole_chain_over_http(self):
        """端到端（真实 HTTP）：三件事在真正的入口上也会发生。"""
        # ⚠️ 显式 opt-out（默认值翻面）：下面自己建带提成率的档案 ⇒
        # 夹具必须**不建**，否则撞唯一约束。
        admin = make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        admin_user = User.objects.get(username=ADMIN_USERNAME)
        StaffProfile.objects.create(
            user=admin_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("60.00"),
        )
        order = self._order(status=OrderStatus.UNASSIGNED)
        create_performance_for_order(order)
        url = TRANSITION_URL.format(order_no=order.order_no)

        def post(path, body):
            return admin.post(path, body, content_type="application/json").json()

        def run_one_round():
            self.assertEqual(
                post(f"/api/business/orders/{order.order_no}/assign/",
                     {"user_id": admin_user.id})["code"], 0
            )
            self.assertEqual(
                post(f"/api/business/orders/{order.order_no}/submit/", {})["code"], 0
            )
            main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
            self.assertEqual(
                post(f"/api/business/performances/{main.id}/review/",
                     {"approved": True})["code"], 0
            )

        run_one_round()
        self.assertEqual(post(url, {"target": "REFUNDED"})["code"], 0)
        # ⭐ D2（spec §4.1.2 例外①）：退单保留 `user` ⇒ 回池要走**产品动作** `/clear-taker/`。
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/clear-taker/", {})["code"], 0
        )
        order.refresh_from_db()
        # ⭐ D-0181：纯退单不写 `refund_fee` ⇒ 它一直是 `0.00`。
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        run_one_round()

        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(self._lifetime(sales), Decimal("8.00"))

        # 第二轮再退单：钱回到 0（T4 也顺带在 HTTP 层钉一次）。
        self.assertEqual(post(url, {"target": "REFUNDED"})["code"], 0)
        sales.refresh_from_db()
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        # ⭐ D-0181：两轮退单 + 一次回池**都不写** `OrderRefund` 行。
        self.assertEqual([row.kind for row in self._refunds(order)], [])


class PoolWindowCommissionTests(_RedoRoundFixture):
    """**F-1**：回池窗口里客服行的钱必须归零——否则会被结账多付。

    危害形状：「回池 → 重做完成」这个窗口里
    客服行 `bonus_amount` 仍是上一轮的 `8.00`，而上一轮的 `FULL_REFUND(−8.00)` 刚被
    等额反向（`+8.00`）⇒ 窗口净额被读成 `8.00`；而结算列表与 `settle_performances`
    **都不按订单状态过滤** ⇒ 这一行可以被财务勾选结账（一直不重做 = **纯多付**）。

    判据（brief F-1，**全部字面值**）：窗口时刻 `bonus_amount == 0.00`、
    终身合计 `== 0.00`、`payable_net == 0.00`；重做完成 ⇒ `8.00`；再退单 ⇒ `0.00`。

    判别力变异：去掉 `_on_pool_entry` 里那一行 `bonus_amount` 归零 ⇒ 本组前三条红
    （实测 `Decimal('8.00') != Decimal('0.00')`）。
"""

    def test_the_pool_window_shows_zero_commission(self):
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")

        # ── 窗口时刻：还没重做 ⇒ 一分钱都不该欠（F-1 修的就是这三行）──────────
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))

        # ── 重做完成：新一轮真的赚到提成（窗口归零不是「把这一轮也抹了」）──────
        self._second_round(order)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(self._lifetime(sales), Decimal("8.00"))
        self.assertEqual(self._payable_net(sales), Decimal("8.00"))

        # ── 再退单：钱回到 0 —— ⚠️ **业务方 Q1 口径**：
        # 新口径下未结账的行在退单时被**清 0**（不再「不动 `bonus_amount`、靠目标式冲减」；
        # 在 git 历史里）⇒ 两列都是 0、链余额 0、应付 0.00。
        self._refund(order, actor="cs-2")
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))

    def test_the_window_zeroing_is_reachable_over_http(self):
        """真实 HTTP：`REFUNDED → UNASSIGNED` 之后、重做之前，窗口也是 `0.00`。"""
        # ⚠️ 显式 opt-out（默认值翻面）：下面自己建带提成率的档案 ⇒
        # 夹具必须**不建**，否则撞唯一约束。
        admin = make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        admin_user = User.objects.get(username=ADMIN_USERNAME)
        StaffProfile.objects.create(
            user=admin_user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("60.00"),
        )
        order = self._order(status=OrderStatus.UNASSIGNED)
        create_performance_for_order(order)
        url = TRANSITION_URL.format(order_no=order.order_no)

        def post(path, body):
            return admin.post(path, body, content_type="application/json").json()

        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/assign/",
                 {"user_id": admin_user.id})["code"], 0
        )
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/submit/", {})["code"], 0
        )
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(
            post(f"/api/business/performances/{main.id}/review/",
                 {"approved": True})["code"], 0
        )
        self.assertEqual(post(url, {"target": "REFUNDED"})["code"], 0)
        # ⭐ D2（spec §4.1.2 例外①）：同上——回池走 `/clear-taker/`（产品动作）。
        self.assertEqual(
            post(f"/api/business/orders/{order.order_no}/clear-taker/", {})["code"], 0
        )

        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))


class FullRefundKindStructureTests(_RedoRoundFixture):
    """**F-3 结构哨兵（D-0181 口径 + 钱包发薪线第三类）**：
    `FULL_REFUND` 现在**混合了三类来源**，必须可分。

    ⚠️ D-0181 口径：退单不再建事件行 ⇒ 三类**都是 `refund=None`**，区分只剩 `reason`：

    - `reason` 以回池反向前缀（`REVERSAL_DEDUCTION_REASON`）开头 ⇒ 回池反向那一类；
    - 其余（`FULL_REFUND_REASON`）⇒ `on_order_refunded` 第 3 步的目标式冲减（**客服行**）；
    - ⭐ **打手回冲**（钱包发薪线）：`on_order_refunded` 的
    1.6 步把**自动入账**的打手工资从钱包扣回来，写一条 `reason` 以
    `退单回冲自动入账的工资` 开头的 `FULL_REFUND`（**打手行 / MAIN**）。
    这一类是**钱包流水**的对应物，与上面两类（客服行的冲减链）不是同一件事。

    判据：**本类的三类都在场**（阳性对照），且**每条行的 `reason` 与它所属那一类一致**——
    同时钉住「没有一条 `FULL_REFUND` 行挂得上 `refund` 外键」（D-0181 的机械后果）。

    为什么值得一条哨兵：将来有人按 `kind=FULL_REFUND` 汇总（例如「全额退款一共冲减了多少」）
    时这几类会被静默混在一起 ⇒ 本条当场红，逼他先决定怎么区分。
"""

    #: 字面值（E-0024：不引被测模块导出的常量）。
    REVERSAL_PREFIX = "退单回池冲销上一轮退款冲减"
    #: ⭐ 打手回冲那一类的前缀（`accounts.payout` 的 `PAYOUT_CLAWBACK_REASON`，同样写字面值）。
    TAKER_CLAWBACK_PREFIX = "退单回冲自动入账的工资"


    def test_the_two_full_refund_classes_are_structural_separable_by_reason(self):
        order, sales = self._first_round()
        # ⚠️ 先用**部分退款**把 `refund_fee` 写成非 0，回池的第 1/2 件才有事可做
        # （纯退单在新口径下不写 `refund_fee`，那两件会按余额判据跳过）。
        # ⭐ **（结算 P0 第 2 条）为什么夹具要先把客服行标成「已结账」**：
        # 修 P0-2 之后，**未结账**的客服行做部分退款**不再留下 `PARTIAL_REFUND` 冲减**
        # （同一笔减量已由 `refresh_customer_estimate` 重算进 `bonus_amount`，再建一条
        # 就是扣两次）⇒ 本哨兵要的那条 `FULL_REFUND(+6.00)`「事件文案」样本会**凭空消失**。
        # 而「已结账」那一档**照旧建冲减**（钱已发过、`bonus` 冻结）⇒ 先标已结账让部分
        # 退款建出 `−6.00`，再把它复位成未结账，让随后那次退单走的仍是本用例要考的
        # 「未结账 ⇒ 先清 0、再由目标式把链冲平」那条路。
        # ⚠️ 复位那一句是**夹具记账**、不是产品路径：产品上把已结账行退回未结账要走
        # 「撤销结账」，那不是本哨兵要考的东西。
        Performance.objects.filter(pk=sales.pk).update(
            settle_status=SettleStatus.SETTLED,
            settled_at=timezone.now(),
            settled_amount=Decimal("8.00"),
        )
        sales.refresh_from_db()
        refund_set_total(order, Decimal("30.00"), "退差价", "kind-structure-1", "cs-1")
        Performance.objects.filter(pk=sales.pk).update(
            settle_status=SettleStatus.UNSETTLED,
            settled_at=None,
            settled_amount=None,
        )
        sales.refresh_from_db()
        # ⚠️ 夹具回读一次：本条判据读的是**库里的真值**（真实 API 路径每个请求都重新取单），
        # ⛔ 不依赖调用方实例被改写——紧接着的 `_pool` 必须看到非 0 的 `refund_fee`。
        order.refresh_from_db()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        self._second_round(order)
        # ⛔ **`apply_transition` 不再自动调 `payout_completed_order`**
        # （spec `2026-10-23-taker-income-and-early-settlement.md` §3.1：钱包不再收订单
        # 绩效）⇒ 本轮完成**不会**再自动发那 60.00。本哨兵要考的是「系统自动发薪的行
        # 在退单时被整笔回冲」那一条链 ⇒ 在这一步**显式**补上入账，让夹具形状与
        # 与结账那版保持同构（否则打手回冲那一类会凭空消失，断言落在空集上）。
        # 「完成单不再自动入账」的判据在
        # `tests/test_app_income_api.py::CompletionDoesNotCreditWalletTests`。
        payout_completed_order(order)
        self._refund(order, actor="cs-2")

        rows = list(
            PerformanceAdjustment.objects.filter(
                kind="FULL_REFUND", performance__order=order
            ).order_by("id")
        )
        # ⭐ `feat/wallet-single-payout`：第三类 = 打手行上的钱包回冲。
        # 按**绩效行的 `emp_type`** 分，⛔ 不按金额正负猜（客服链也可能出现正数行）。
        taker_clawback_rows = [
            row for row in rows if row.performance.emp_type == EmpType.MAIN
        ]
        sales_rows = [
            row for row in rows if row.performance.emp_type == EmpType.SALES
        ]
        reversal_rows = [
            row for row in sales_rows if row.reason.startswith(self.REVERSAL_PREFIX)
        ]
        event_rows = [
            row for row in sales_rows
            if not row.reason.startswith(self.REVERSAL_PREFIX)
        ]
        # ⭐ **业务方 Q1 口径下的链路与读数**，逐条如下
        # （本机探针 `DBGK` 的五格读数如实抄在报告里）：
        # · 部分退款 30 ⇒ 链 `−6.00`、`bonus` 被第一批的补刷刷成 `2.00`；
        # · 第一次退单 ⇒ 未结账的行被清 0，第 3 步读到 `net = 0 + (−6.00)`
        # ⇒ 写一条 **`+6.00`** 的 `FULL_REFUND`（`reason` = 事件文案那一类）把链归零；
        # · 回池 ⇒ 链已经是 0 ⇒ **反向前缀那一类今天不再产生行**（旧口径是 `+8.00`；
        # 原句与旧读数保留在 git 历史里）。
        # ⇒ 判据形态：**只**事件文案那一类在场（一条 `+6.00`），且它不该挂事件外键；
        # 下面三组断言各自都能失败（反向行若改回 `MANUAL`、或金额算式漂移都会红）。
        self.assertEqual(len(reversal_rows), 0)
        self.assertEqual(
            [row.amount for row in event_rows], [Decimal("6.00")]
        )
        # ⭐ 第二轮完成时自动入账 60.00（打手 60% 订单价）⇒ 第二次退单把它
        # 整笔扣回，写一条 **−60.00** 的 `FULL_REFUND`（打手行）。
        # 判据是**数据库里的值**（金额 + 前缀文案 + 行数），⛔ 不是「函数被调过」。
        self.assertEqual(
            [row.amount for row in taker_clawback_rows], [Decimal("-60.00")]
        )
        self.assertEqual(len(taker_clawback_rows), 1)
        for row in taker_clawback_rows:
            self.assertTrue(row.reason.startswith(self.TAKER_CLAWBACK_PREFIX), row.reason)
        # ⭐ D-0181 的机械后果：**没有任何** `FULL_REFUND` 行挂得上事件外键。
        self.assertEqual([row.refund_id for row in rows], [None, None])
        # 目标式那一类用事件文案（字面值）；回池那一类今天没有行。
        for row in event_rows:
            self.assertEqual(row.reason, "全额退款清空绩效")

        # 账仍然对得上：终身合计 0（`8.00` 原始 − `6.00` 部分退款 + `6.00` 目标式 = 0）。
        sales.refresh_from_db()
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(_refund_chain_sum(sales), Decimal("0.00"))


class RefundFeeInvariantTests(_RedoRoundFixture):
    """**T2（不变量，D-0181 口径）**：`refund_fee == Σ OrderRefund.amount`。

    ⚠️ 不变式本身恒成立（退单这条路径既不写行、也不改列 ⇒ 两侧恒同步），所以本用例
    以**部分退款**开头：只有它还能把 `refund_fee` 写成非 0，而回池的第 1/2 件
    （`REVERSAL` 行 + 复位）正是这条不变式的另一半。

    ⭐ D-0312：中间再插一次「往下改累计额」（30 → 10）⇒ 台账多一行 `SET(-20)`。于是本用例
    同时钉住「`REVERSAL` 行与 `SET` 行并存时 Σ 仍成立」
    ——回池那一刻 `REVERSAL` 必须是 `-10`（复位的是**当前累计**，不是第一笔的 30）。

    判别力变异：去掉第 1 步（只复位、不写 `REVERSAL` 事件行）⇒ 回池那一刻红
    （`refund_fee = 0.00` 而 `Σ = 10.00`）。
"""

    def test_the_invariant_holds_at_every_moment_of_the_redo_chain(self):
        order, sales = self._first_round()
        refund_set_total(order, Decimal("30.00"), "退差价", "invariant-1", "cs-1")
        order.refresh_from_db()

        def check(label):
            order.refresh_from_db()
            rows = self._refunds(order)
            self.assertEqual(
                sum((row.amount for row in rows), Decimal("0.00")),
                order.refund_fee,
                msg=f"{label}：refund_fee 与 Σ OrderRefund.amount 不一致",
            )
            return order.refund_fee

        self.assertEqual(check("部分退款后"), Decimal("30.00"))
        # ⭐ 绝对值口径：再设成 10（差额 -20）⇒ 台账多一行 `SET(-20)`。
        refund_set_total(order, Decimal("10.00"), "改小累计", "invariant-2", "cs-1")
        self.assertEqual(check("改小累计后"), Decimal("10.00"))
        self._refund(order, actor="cs-1")
        # ⭐ D-0181：全额退款这一步**不动** `refund_fee`。
        self.assertEqual(check("第一次全额退款"), Decimal("10.00"))
        self._pool(order, actor="cs-1")
        self.assertEqual(check("回池冲销后"), Decimal("0.00"))
        self._second_round(order)
        self.assertEqual(check("第二轮重做完成"), Decimal("0.00"))
        self._refund(order, actor="cs-2")
        # ⭐ 同上：第二次全额退款也不动 `refund_fee`（旧口径会顶到 100.00）。
        self.assertEqual(check("第二次全额退款"), Decimal("0.00"))
        # 阳性对照：账上确实有三行（两条 `SET` + `REVERSAL`），
        # 不变量不是「空集等于零」的假象；回池冲销复位的也必须是**当前累计 10**。
        self.assertEqual(
            [(row.kind, row.amount) for row in self._refunds(order)],
            [
                ("SET", Decimal("30.00")),
                ("SET", Decimal("-20.00")),
                ("REVERSAL", Decimal("-10.00")),
            ],
        )


class PoolEntryIdempotencyTests(_RedoRoundFixture):
    """**T3（幂等）**：同一次回池**重跑** ⇒ 不多行、不改钱、不抛异常。

    ⚠️ 重跑只可能来自**直接调钩子**：`apply_transition(order, UNASSIGNED)` 打在已在池内的
    订单上会被 `IllegalTransition` 挡在钩子**之前**（池内状态没有自环）——本文件把这条
    也断言下来，免得后来者以为「重跑」是端点层能发生的事。
"""

    def test_rerunning_the_pool_entry_hook_adds_nothing(self):
        order, sales = self._first_round()
        # ⚠️ D-0181：纯退单不写 `refund_fee` ⇒ 第 1/2 件会按余额判据跳过、
        # 连 `REVERSAL` 行都不会有。本用例要观察的正是那两件的幂等出口
        # ⇒ 先用**部分退款**把 `refund_fee` 写成非 0。
        refund_set_total(order, Decimal("30.00"), "退差价", "pool-idem-1", "cs-1")
        # ⚠️ 夹具回读一次：本条判据读的是**库里的真值**（真实 API 路径每个请求都重新取单），
        # ⛔ 不依赖调用方实例被改写——紧接着的 `_pool` 必须看到非 0 的 `refund_fee`。
        order.refresh_from_db()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")

        before = {
            "refunds": OrderRefund.objects.filter(order=order).count(),
            "reversals": OrderRefund.objects.filter(
                order=order, kind="REVERSAL"
            ).count(),
            "adjustments": PerformanceAdjustment.objects.filter(
                performance=sales
            ).count(),
            "audit_refund": OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).count(),
            "audit_adjust": OperationLog.objects.filter(
                action="performance.adjust_manual", order_no=order.order_no
            ).count(),
        }
        order.refresh_from_db()
        refund_fee_before = order.refund_fee
        sales.refresh_from_db()
        lifetime_before = self._lifetime(sales)

        # 重跑两次：**不抛异常**。
        _on_pool_entry(order, OrderStatus.REFUNDED)
        _on_pool_entry(order, OrderStatus.REFUNDED)

        order.refresh_from_db()
        sales.refresh_from_db()
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), before["refunds"])
        self.assertEqual(
            OrderRefund.objects.filter(order=order, kind="REVERSAL").count(),
            before["reversals"],
        )
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(),
            before["adjustments"],
        )
        self.assertEqual(
            OperationLog.objects.filter(action="order.refund", order_no=order.order_no).count(),
            before["audit_refund"],
        )
        self.assertEqual(
            OperationLog.objects.filter(action="performance.adjust_manual", order_no=order.order_no).count(),
            before["audit_adjust"],
        )
        self.assertEqual(order.refund_fee, refund_fee_before)
        self.assertEqual(self._lifetime(sales), lifetime_before)
        self.assertEqual(before["reversals"], 1)

    def test_apply_transition_cannot_rerun_the_hook_on_a_pool_state(self):
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        with self.assertRaises(IllegalTransition):
            apply_transition(order, OrderStatus.UNASSIGNED, nodes={"order.change_status"}, actor="cs-9")

    def test_a_duplicate_reversal_key_does_not_poison_the_outer_transaction(self):
        """伪造一次**并发**（两个事务算出同一个 `n`）：撞唯一键要走幂等出口，不许 500。

    做法照已被 D-0181 删除的 `SecondFullRefundSavepointTests`（那条路的事件行没了）：
    预置一条键正好等于「下一次会派生出来的」`REVERSAL` 行，再把序号函数打桩成 `1`
    强制同一个键。⚠️ 这条哨兵今天**只**挂在回池冲销这条路上（全仓仅剩它还有
    `request_id` 唯一约束的 `create()`）。
"""
        order, sales = self._first_round()
        # ⚠️ D-0181：要有可冲销的 `refund_fee`，第 1/2 件才会真的走到 `create()`
        # （纯退单在新口径下 `refund_fee == 0` ⇒ 那条路根本不执行、撞不出并发）。
        refund_set_total(order, Decimal("30.00"), "退差价", "pool-savepoint-1", "cs-1")
        # ⚠️ 夹具回读一次：本条判据读的是**库里的真值**（真实 API 路径每个请求都重新取单），
        # ⛔ 不依赖调用方实例被改写——紧接着的 `_pool` 必须看到非 0 的 `refund_fee`。
        order.refresh_from_db()
        self._refund(order, actor="cs-1")
        competitor = OrderRefund.objects.create(
            order=order,
            kind="REVERSAL",
            amount=Decimal("-30.00"),
            reason="并发的回池冲销",
            request_id=f"REVERSAL-{order.order_no}",
        )

        with mock.patch(
            "apps.business.refunds._next_reversal_ordinal", return_value=1
        ):
            with transaction.atomic():                 # ← 模拟调用方的外层事务
                _on_pool_entry(order, OrderStatus.REFUNDED)
                # ⭐ 判据的核心：外层事务**还能用**（嵌套 atomic 的 savepoint 生效）。
                PerformanceAdjustment.objects.create(
                    performance=sales,
                    kind="MANUAL",
                    amount=Decimal("0.00"),
                    reason="savepoint 之后仍可写",
                )

        # 两行 = `PARTIAL` + 那条伪造的 `REVERSAL`（撞键那一次没有第三行）。
        self.assertEqual(
            [(row.kind, row.amount) for row in self._refunds(order)],
            [("SET", Decimal("30.00")), ("REVERSAL", Decimal("-30.00"))],
        )
        self.assertTrue(
            OrderRefund.objects.filter(order=order, id=competitor.id).exists()
        )
        # 幂等出口 ⇒ 没有多写审计：`order.refund` 只有**部分退款**那一行
        # （退单在新口径下不写它；回池冲销那一次走的是撞键出口 ⇒ 也不写）。
        self.assertEqual(
            OperationLog.objects.filter(action="order.refund", order_no=order.order_no).count(), 1
        )


class NextRefundFormulaTests(_RedoRoundFixture):
    """**T4**：第二轮再退单 ⇒ 终身合计回 `0.00`，且这次 `FULL_REFUND` 是目标式算出的**字面值**。

    ⭐ **这条用例就是「反向行必须留在目标式链上」的哨兵**：反向行若写成 `MANUAL`
    （brief 的字面写法），目标式算出的会是 `0.00`、终身合计停在 `8.00` ⇒ 本组红。
"""

    def test_the_second_refund_zeroes_the_redo_round_commission(self):
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        self._second_round(order)

        self._refund(order, actor="cs-2")

        sales.refresh_from_db()
        # ⚠️ **业务方 Q1 口径**：两次纯退单都不写冲减行（未结账的行在第 3 步之前被清 0
        # ⇒ `deduction == 0`）⇒ 回池那一步也**无事可做**（链本来就是空的）。
        # ⭐ 本用例的**核心判据打在终态**：终身合计 `0.00`、两列清 0、链 0、应付 0.00；
        # ⭐ 两次清 0 **各留一条痕**（`MANUAL amount=0.00`）⇒ 如实写两条。
        self.assertEqual(
            self._sales_adjustment_shapes(sales),
            [("MANUAL", Decimal("0.00")), ("MANUAL", Decimal("0.00"))],
        )
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))
        # ⭐ D-0181：两轮退单 + 一次回池**都不写** `OrderRefund` 行。
        self.assertEqual(self._refunds(order), [])

    def test_the_second_refund_of_the_asymmetric_round_zeroes_it_too(self):
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-1")
        self._second_round(order, taker_price="80.00")
        sales.refresh_from_db()
        self.assertEqual(self._lifetime(sales), Decimal("10.40"))    # 前置：新一轮真的赚到了

        self._refund(order, actor="cs-2")

        sales.refresh_from_db()
        # ⚠️ 同上：第二次纯退单**不写**冲减行（清 0 在先 ⇒ 冲减额恒 0）。
        # 判据打终态：两列清 0、链 0、应付 0.00、终身合计 0.00；
        # ⭐ 两次清 0 各留一条 `MANUAL amount=0.00` 的痕。
        self.assertEqual(
            self._sales_adjustment_shapes(sales),
            [("MANUAL", Decimal("0.00")), ("MANUAL", Decimal("0.00"))],
        )
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))


class PoolEntryGuardTests(_RedoRoundFixture):
    """**T5 / T6**：来源态守卫（别的来源态什么都不做）+ 没有可冲销的东西时不写噪声行。

    ⚠️ brief 的触发点写的是**两个**池内状态（`UNASSIGNED` / `WAITING_GRAB`）——
    三件事挂在同一个 `_on_pool_entry` 上、两个状态都注册了它，本组把**另一个**也钉一次。
"""

    def test_the_other_pool_state_also_reverses_the_previous_round(self):
        """`REFUNDED → WAITING_GRAB`（brief 触发点的另一半）⇒ 钩子照做。

    ⭐ D2 口径：`WAITING_GRAB` 不是例外目标态，
    而退单**保留** `user` ⇒ 「有接手人的单进 `WAITING_GRAB`」已被新闸拒。要观察
    `WAITING_GRAB` 上那半个钩子，只能先把 MAIN 行的 `user` 清成空（= D2 之下这条边
    唯一可达的形状）。**判据一个没放宽**：第 3 件的等额反向行 / `refund_fee` /
    终身合计三样照断，删掉 `WAITING_GRAB` 的钩子注册本条仍会红。

    ⚠️ **D-0181 / 业务方 Q1 口径**：第一轮纯退单**不写**冲减行（未结账的行被清 0
    ⇒ 冲减额恒 0），回池的第 1/2 / 3 件**都无事可做**（链本来就是空的）。
    ⭐ 判据打**窗口终态**（⛔ 不放宽）：两列清 0、链 0、应付 0.00、终身合计 0.00。
    ⚠️ `WAITING_GRAB` 上那个钩子注册点的**可达性**仍由本用例证明：`_on_pool_entry`
    真的被跑到（`order.status` 迁成了 `WAITING_GRAB`、`user` 被 `reset_taker_performance`
    清空 —— 后者是**独立于**冲减链的副作用，见下面第二组断言）。
"""
        order, sales = self._first_round()
        self._refund(order, actor="cs-1")
        # ⚠️ 前置一步：清空接单人（不是为了绕闸，而是这条边在 D2 之下**只有**这个形状可达）。
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(user=None)
        self._pool(order, actor="cs-1", target=OrderStatus.WAITING_GRAB)

        order.refresh_from_db()
        self.assertEqual(order.status, "WAITING_GRAB")
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(
            [(row.kind, row.amount) for row in self._refunds(order)], []   # D-0181
        )
        sales.refresh_from_db()
        # ⭐ 钩子真的跑了（可达性判据一）：MAIN 行的 `user` 被 `reset_taker_performance` 清空。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        # ⭐ 业务方 Q1 口径：第一轮纯退单没留下冲减行 ⇒ 回池的第 3 件无事可做；
        # 那一轮清 0 留了一条 `MANUAL amount=0.00` 的痕（回池不再加）。
        self.assertEqual(
            self._sales_adjustment_shapes(sales), [("MANUAL", Decimal("0.00"))]
        )
        # ⭐ F-1：窗口期（**重做之前**）终身合计是 `0.00`——
        # 修前是 `8.00`（上一轮的 `bonus_amount` 快照 + 刚被反向的冲减），
        # 那会被财务当成「这张已全额退款的单还欠客服 8 元」结掉（纯多付）。
        self.assertEqual(self._lifetime(sales), Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        # ⭐ `temp_amount` 在窗口期是**重新算出来的活值**（不是 0）：回池那一刻 MAIN 行已被
        # `reset_taker_performance` 清空 `user`、两个金额列在退单时已是 0 ⇒
        # 客服预计 = `(净实收 100.00 − 减项 0.00) × 20% = 20.00`
        # （`refresh_customer_estimate` 第 3 条的口径；本机实测读数）。
        self.assertEqual(sales.temp_amount, Decimal("20.00"))
        self.assertEqual(self._payable_net(sales), Decimal("0.00"))

    def test_a_pool_entry_from_another_status_changes_nothing(self):
        """T5（负向）：`ISSUE → UNASSIGNED` ⇒ 不写冲销行、不写反向调整、`refund_fee` 不变。

    单上**确实有**可冲销的东西（`refund_fee = 30.00` + 一条 `−6.00` 的冲减链），
    所以「没写」不是因为没有东西可写。

    判别力变异：去掉 `_on_pool_entry` 里的来源态守卫 ⇒ 本条红。
"""
        order = self._order(status=OrderStatus.ISSUE, refund_fee="30.00")
        sales = self._sales(order)
        self._taker(order)
        prior = OrderRefund.objects.create(
            order=order,
            kind="PARTIAL",
            amount=Decimal("30.00"),
            reason="退差价",
            request_id="guard-partial-1",
        )
        PerformanceAdjustment.objects.create(
            performance=sales,
            refund=prior,
            kind="PARTIAL_REFUND",
            amount=Decimal("-6.00"),
            reason="部分退款冲减",
            settle_status=SettleStatus.UNSETTLED,
        )

        self._pool(order, actor="cs-5")

        order.refresh_from_db()
        sales.refresh_from_db()
        self.assertEqual(order.status, "UNASSIGNED")             # 阳性对照：迁移真的发生
        self.assertEqual(order.refund_fee, Decimal("30.00"))
        self.assertEqual(
            OrderRefund.objects.filter(order=order).count(), 1    # 只有那条 PARTIAL
        )
        self.assertEqual(
            [(row.kind, row.amount) for row in self._sales_adjustments(sales)],
            [("PARTIAL_REFUND", Decimal("-6.00"))],
        )

    def test_a_refunded_order_with_nothing_to_reverse_writes_no_noise(self):
        """T6：来源态**是** `REFUNDED`，但 `refund_fee == 0` 且冲减链为空 ⇒ 一行都不写。

    形态是「绕开退单钩子的老数据 / 已结清的退单」——第 1 步与第 3 步**各自**的
    「没有可冲销的东西」出口各断一次。
"""
        order = self._order(status=OrderStatus.REFUNDED, refund_fee="0.00")
        sales = self._sales(order)
        self._taker(order)

        self._pool(order, actor="cs-6")

        order.refresh_from_db()
        sales.refresh_from_db()
        self.assertEqual(order.status, "UNASSIGNED")
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        self.assertEqual(self._sales_adjustments(sales), [])
        # 两条审计也一条都不写（`apply_transition` 自己不写状态留痕，那是视图层的事）。
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=order.order_no,
                action__in=("order.refund", "performance.adjust_manual"),
            ).count(),
            0,
        )


class PoolEntryAuditTests(_RedoRoundFixture):
    """**T7（审计落库）**：第 1 步（回池冲销）与第 3 步（反向调整）各留**数据库行**（E-0009）。

    ⚠️ D-0181 口径：退单不再写 `order.refund` 审计 ⇒ 本用例以**部分退款**开头，让回池的
    第 1/2 件仍然有事可做：此时 `order.refund` 恰好 2 行 = **部分退款**那一行
    （`0.00 → 30.00`）+ **回池冲销**那一行（`30.00 → 0.00`）。
"""

    def test_the_reversal_leaves_an_audit_row_for_each_step(self):
        """第 1 步（回池冲销）与第 3 步（反向调整）各留**数据库行**（E-0009 / §9 #17）。

    ⚠️ **业务方 Q1 口径**：「部分退款 + 退单 + 回池」那条路上第 3 件无事可做
    （未结账的行在退单时被清 0 ⇒ 冲减额恒 0；探针实测：进 `_reverse_sales_refund_deductions`
    时 `order.refund_fee = 0.00`、该行链余额 = `0.00` ⇒ 第 3 件按自己的**余额判据正确地跳过**，
    不是漏跑）。⇒ 本用例拆成两段，**各自都还能失败**：

    ① 第 1/2 件的两行审计（部分退款 + 回池冲销）—— 走原来的三跳链路；
    ② 第 3 件那一行（`performance.adjust_manual`）—— 直接在一个**有链**的行上调
    `reverse_previous_round_refund`（这是「上一轮遗留冲减」的真实形态）。
"""
        # ── ① 第 1/2 件：部分退款那一行 + 回池冲销那一行 ─────────────────────
        order, sales = self._first_round()
        refund_set_total(order, Decimal("30.00"), "退差价", "pool-audit-1", "cs-1")
        # ⚠️ 夹具回读一次：本条判据读的是**库里的真值**（真实 API 路径每个请求都重新取单），
        # ⛔ 不依赖调用方实例被改写——紧接着的 `_pool` 必须看到非 0 的 `refund_fee`。
        order.refresh_from_db()
        self._refund(order, actor="cs-1")
        self._pool(order, actor="cs-2")

        refund_audit = list(
            OperationLog.objects.filter(
                action="order.refund", order_no=order.order_no
            ).order_by("id")
        )
        # 恰好两行 = 部分退款那一行（`0.00 → 30.00`）+ 回池冲销那一行（`30.00 → 0.00`）。
        self.assertEqual(len(refund_audit), 2)
        self.assertEqual(refund_audit[0].log_type, 10)
        self.assertEqual(refund_audit[0].target_kind, "order")
        self.assertEqual(refund_audit[0].target_id, order.order_no)
        self.assertEqual(refund_audit[0].from_value, "0.00")
        self.assertEqual(refund_audit[0].to_value, "30.00")
        self.assertEqual(refund_audit[0].reason, "退差价")
        self.assertEqual(refund_audit[0].actor, "cs-1")
        self.assertEqual(refund_audit[1].from_value, "30.00")
        self.assertEqual(refund_audit[1].to_value, "0.00")
        self.assertEqual(refund_audit[1].reason, "退单回池冲销上一轮退款")
        self.assertEqual(refund_audit[1].actor, "cs-2")
        # 这一跳里第 3 件**正确地**跳过（链已被上游复位）⇒ 没有 `adjust_manual` 行。
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.adjust_manual", order_no=order.order_no
            ).count(),
            0,
        )

        # ── ② 第 3 件：一个**有链**的行 ⇒ 等额反向 + 一条 `performance.adjust_manual` ──
        other_order, other_sales = self._first_round()
        PerformanceAdjustment.objects.create(
            performance=other_sales,
            kind="PARTIAL_REFUND",
            amount=Decimal("-5.00"),
            reason="上一轮遗留冲减（本段的夹具）",
            operator="cs-0",
            settle_status=SettleStatus.UNSETTLED,
        )
        reverse_previous_round_refund(other_order)

        other_sales.refresh_from_db()
        reversal_rows = list(
            PerformanceAdjustment.objects.filter(
                performance=other_sales, kind="FULL_REFUND"
            )
        )
        # 等额反向：链 `-5.00` ⇒ 写 `+5.00`（第 3 件的目标式）。
        self.assertEqual([row.amount for row in reversal_rows], [Decimal("5.00")])
        self.assertIsNone(reversal_rows[0].refund_id)
        self.assertEqual(reversal_rows[0].settle_status, "未结账")
        adjust_audit = list(
            OperationLog.objects.filter(
                action="performance.adjust_manual", order_no=other_order.order_no
            ).order_by("id")
        )
        self.assertEqual(len(adjust_audit), 1)
        self.assertEqual(adjust_audit[0].log_type, 20)
        self.assertEqual(adjust_audit[0].target_kind, "user")
        self.assertEqual(adjust_audit[0].target_id, str(other_sales.user_id))
        self.assertEqual(
            adjust_audit[0].target_name, other_sales.user.get_username()
        )
        # ⭐ F-2：`from/to` 是**链余额**的前后值
        # （冲销前 `-5.00` ⇒ 本次反向之后 `0.00`）。
        self.assertEqual(adjust_audit[0].from_value, "-5.00")
        self.assertEqual(adjust_audit[0].to_value, "0.00")
        self.assertEqual(adjust_audit[0].reason, "退单回池冲销上一轮退款冲减")
        self.assertEqual(adjust_audit[0].actor, "")
        # 旧出口退役：这条路径**一行都不许**再写旧表。
        self.assertEqual(AuditLog.objects.count(), 0)
