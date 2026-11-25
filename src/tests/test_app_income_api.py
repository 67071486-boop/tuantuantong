"""打手「我的收入」+ 提前结算申请的后端判据（2026-10-23）。

spec：`docs/specs/2026-10-23-taker-income-and-early-settlement.md`（§2 口径 / §3.1–§3.4 / §5 验收）。

本文件钉六组事：

1. **完成单不再进钱包**（spec §3.1 / §5）：完成一张 App 打手的单 ⇒ 钱包一行都不建、
   绩效行仍是「未结账」、没有 `performance.settle` 日志；而 `payout_completed_order`
   本身**仍然可用**（历史 / 人工场景，直接调用照样入账）。
2. **四数口径**（spec §2 / §3.2）：预算 / 实算用**接手价**（覆盖价优先、否则订单默认价），
   未结 / 已结用**绩效金额**；只算「我接的单」（`user=me` + `打单工资`）；
   **无价不计入金额**；「有行但一个价都没有」⇒ `null`（⛔ 不是 `0.00`）。
3. **`my-orders?income=` 四档筛选**（spec §3.3）：`all` / `unsettled` / `settled` / `completed`，
   非法值 ⇒ 业务码 `1000`（⛔ 不 500、⛔ 不静默返回空列表）。
4. **提前结算申请的四条拒绝**（spec §3.4 规则 2）：额度 / 重复 / 非本人 / 非完成单，
   外加「已结账也拒」；额度用完是**独立业务码** `1036`。
5. **结账联动**（spec §3.4 第 6 条）：财务结掉那一行 ⇒ 对应的申请变 `settled` 且
   `settled_at` 与绩效行**同一个值**。
6. **财务列表新字段**（spec §3.4 第 5 条）：`/api/business/performances` 每行带
   `settle_requested_at`（没申请 ⇒ `null`；有 ⇒ 项目时区 `+08:00` 的 ISO 串）。

⚠️ 断言纪律（AGENTS.md §5-G / E-0024）：金额一律**字面值**（`"600.00"` 这种），
⛔ 不 import 被测模块的常量来当期望值；副作用断言打**数据库行**与**响应体**，
⛔ 不断言「函数被调用了」。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.payout import payout_completed_order
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    PerformanceSettleRequest,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.performance_writes import revoke_settlements
from apps.business.refunds import on_order_refunded
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import make_logged_in_client

SUMMARY_URL = "/api/app/income/summary/"
REQUESTS_URL = "/api/app/income/settle-requests/"
MY_ORDERS_URL = "/api/business/my-orders/"
PERFORMANCES_URL = "/api/business/performances/"

#: `/my-orders` 的类级节点门（与 `tests/test_my_orders_api.py` 同源）。
MY_ORDERS_NODES = ("my_orders.send_to_my_system",)
#: 进 `COMPLETED` 需要的节点（与 `tests/test_wallet_payout.py` 同字面值）。
COMPLETED_NODES = {"order.change_status_completed"}


def _no(seq):
    return f"20261023{seq:012d}"


class IncomeFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="收入线游戏")
        self.client = make_logged_in_client(
            username="income_taker",
            groups=("代练员",),
            nodes=MY_ORDERS_NODES,
        )
        self.taker = User.objects.get(username="income_taker")
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.COMPLETED, hall_price="100.00", game=None):
        self._seq += 1
        return Order.objects.create(
            game=game or self.game,
            order_no=_no(self._seq),
            status=status,
            total_price=Decimal("300.00"),
            hall_price=None if hall_price is None else Decimal(hall_price),
        )

    def _perf(
        self,
        order,
        *,
        user=None,
        emp_type=EmpType.MAIN,
        temp="50.00",
        bonus="50.00",
        taker_price=None,
        settle_status=SettleStatus.UNSETTLED,
        settled_at=None,
    ):
        return Performance.objects.create(
            order=order,
            emp_type=emp_type,
            user=self.taker if user is None else user,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
            taker_price=None if taker_price is None else Decimal(taker_price),
            settle_status=settle_status,
            settled_at=settled_at,
        )

    def _summary(self):
        body = self.client.get(SUMMARY_URL).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _apply(self, performance_id):
        return self.client.post(
            REQUESTS_URL,
            {"performance_id": performance_id},
            content_type="application/json",
        ).json()


class CompletionDoesNotCreditWalletTests(IncomeFixture):
    """spec §3.1 / §5：完成单**不再**进钱包；函数本身仍可用于历史 / 人工场景。"""

    def _app_taker_order(self):
        # ⚠️ `make_logged_in_client` **已经**给这个账号建了档案（它要给游戏范围）⇒ 只能 `update`，
        #    ⛔ 不能再 `create`（`StaffProfile.user` 是 1:1，会撞唯一约束——本文件实测踩过）。
        StaffProfile.objects.filter(user=self.taker).update(
            mobile="13700001111",
            account_source=AccountSource.APP,
        )
        order = self._order(status=OrderStatus.PENDING_REVIEW, hall_price="30.00")
        perf = self._perf(order, bonus="30.00", temp="30.00")
        return order, perf

    def test_completing_an_order_does_not_touch_the_wallet(self):
        order, perf = self._app_taker_order()

        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)   # 阳性对照：状态真的改了
        # 钱包行都不该被建出来（`payout_completed_order` 是唯一会 `get_or_create` 它的地方）。
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)
        # 绩效行照旧是「未结账」——钱由财务按结账状态线下打款。
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        # 也没有「结账」那条操作日志（钱没动、状态也没标）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle", order_no=order.order_no
            ).count(),
            0,
        )

    def test_the_payout_function_is_kept_and_still_works_when_called_directly(self):
        """⛔ 函数**保留**（spec §3.1）：这是「历史 / 人工场景」那条路的判据。

        它同时是上一条的**对照**：同一个夹具、同一张完成单，只要没人调它，钱包就不动；
        显式调用它，钱才进钱包 ⇒ 「不再自动调用」这条改动是真的在调用点上生效的。
        """
        order, perf = self._app_taker_order()
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())

        payout_completed_order(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_amount, Decimal("30.00"))


class IncomeSummaryTests(IncomeFixture):
    """spec §2 / §3.2：四个数 + 四个计数 + 额度。"""

    def test_four_numbers_come_from_two_different_bases(self):
        """预算 / 实算 = Σ 接手价；未结 / 已结 = Σ 绩效金额（含调整）。"""
        # 进行中：计入预算、不计入实算。
        self._perf(self._order(status=OrderStatus.IN_PROGRESS, hall_price="100.00"),
                   temp="60.00", bonus="60.00")
        # 已完成·未结账：接手价 200，绩效金额也是 200。
        self._perf(self._order(hall_price="200.00"), temp="200.00", bonus="200.00")
        # 已完成·已结账：接手价 300，但绩效金额被调整成 290（未结/已结要读这个）。
        self._perf(
            self._order(hall_price="300.00"),
            temp="300.00",
            bonus="290.00",
            settle_status=SettleStatus.SETTLED,
        )
        # 已完成·无价：两边都没有价 ⇒ **不计入金额**，但仍算「一单」。
        self._perf(self._order(hall_price=None), temp="0.00", bonus="0.00")
        # 销售提成行：**不是「我接的单」** ⇒ 完全不计。
        self._perf(self._order(hall_price="999.00"), emp_type=EmpType.SALES, bonus="999.00")
        # 别人接的单 ⇒ 完全不计。
        other = User.objects.create_user(username="income_other")
        self._perf(self._order(hall_price="777.00"), user=other, bonus="777.00")
        # 已退单（作废类）⇒ **不计入预算**（追加口径），也不计入实算。
        self._perf(
            self._order(status=OrderStatus.REFUNDED, hall_price="888.00"),
            temp="0.00",
            bonus="0.00",
        )

        data = self._summary()

        # 预算 = 100 + 200 + 300（无价那单不计金额；退单那 888 也不算）
        self.assertEqual(data["budget"], "600.00")
        # 实算 = 200 + 300
        self.assertEqual(data["actual"], "500.00")
        # 未结 = 绩效金额 200；已结 = 绩效金额 290（**不是**接手价 300）
        self.assertEqual(data["unsettled"], "200.00")
        self.assertEqual(data["settled"], "290.00")
        # 计数：预算含无价那一单、**不含退单**（4 行）；实算 3 行（同样含无价那条已完成行）。
        # ⚠️ `unsettled` 是 **2**：无价那条也是「已完成 · 未结账」，只是绩效金额是 0
        #    （金额 `0.00` 照旧计入和，那不是「算不出」——E-0021 同族）。
        self.assertEqual(
            data["counts"],
            {"budget": 4, "actual": 3, "unsettled": 2, "settled": 1},
        )
        self.assertEqual(data["early_settlement"], {"used": 0, "limit": 3})

    def test_refunded_orders_are_left_out_of_the_budget(self):
        """作废类 = `REFUNDED`（已退单）：退掉的钱不再算进「接单总额」。

        ⚠️ 另两档状态**仍然算预算**（问题单 / 已暂停）：单还在、处理完可能回到进行中。
        """
        self._perf(self._order(status=OrderStatus.IN_PROGRESS, hall_price="100.00"))
        self._perf(self._order(status=OrderStatus.REFUNDED, hall_price="200.00"))
        self._perf(self._order(status=OrderStatus.PAUSED, hall_price="30.00"))
        self._perf(self._order(status=OrderStatus.ISSUE, hall_price="20.00"))

        data = self._summary()

        self.assertEqual(data["budget"], "150.00")     # 100 + 30 + 20
        self.assertEqual(data["counts"]["budget"], 3)
        # 实算只算已完成 ⇒ 这三种状态一条都不进去。
        self.assertEqual(data["actual"], "0.00")
        self.assertEqual(data["counts"]["actual"], 0)

    def test_taker_price_override_wins_over_the_order_price(self):
        """覆盖价优先（`effective_taker_price` 的唯一口径）：120 而不是 100。"""
        self._perf(self._order(hall_price="100.00"), taker_price="120.00", bonus="0.00")

        data = self._summary()

        self.assertEqual(data["budget"], "120.00")
        self.assertEqual(data["actual"], "120.00")

    def test_a_bucket_with_rows_but_no_price_is_null_not_zero(self):
        """算不出 ⇒ `null`（spec §2 / §3.2 的硬口径），⛔ 不用 `0.00` 冒充。"""
        self._perf(self._order(hall_price=None), temp="0.00", bonus="0.00")

        data = self._summary()

        self.assertIsNone(data["budget"])
        self.assertIsNone(data["actual"])
        # 计数照样是 1 —— 金额算不出不代表没有这一单。
        self.assertEqual(data["counts"]["budget"], 1)
        self.assertEqual(data["counts"]["actual"], 1)

    def test_an_empty_bucket_is_a_real_zero(self):
        """一行都没有 ⇒ `"0.00"`（空集的和是 0，那是**能算出来**的真值）。"""
        data = self._summary()

        self.assertEqual(data["budget"], "0.00")
        self.assertEqual(data["actual"], "0.00")
        self.assertEqual(data["unsettled"], "0.00")
        self.assertEqual(data["settled"], "0.00")
        self.assertEqual(
            data["counts"], {"budget": 0, "actual": 0, "unsettled": 0, "settled": 0}
        )

    def test_a_settled_zero_amount_is_zero_not_null(self):
        """金额 `0.00` 与「算不出」是两件事（E-0021 同族）：0 必须原样出 `"0.00"`。"""
        self._perf(
            self._order(hall_price="80.00"),
            temp="0.00",
            bonus="0.00",
            settle_status=SettleStatus.SETTLED,
        )

        data = self._summary()

        self.assertEqual(data["settled"], "0.00")
        self.assertEqual(data["counts"]["settled"], 1)

    def test_anonymous_cannot_read_the_summary(self):
        from django.test import Client

        response = Client(SERVER_NAME="tenant.test.com").get(SUMMARY_URL)
        self.assertEqual(response.status_code, 401)


class SettleRequestTests(IncomeFixture):
    """spec §3.4：申请的四条拒绝 + 成功 + 列表 + 结账联动。"""

    def _completed_perf(self, **kwargs):
        return self._perf(self._order(), **kwargs)

    def test_applying_returns_the_remaining_quota_and_persists_one_row(self):
        perf = self._completed_perf()

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["early_settlement"], {"used": 1, "limit": 3})
        self.assertEqual(body["data"]["settle_request"]["performance_id"], perf.pk)
        self.assertEqual(body["data"]["settle_request"]["state"], "pending")
        self.assertEqual(
            body["data"]["settle_request"]["bonus_amount"], "50.00"
        )
        row = PerformanceSettleRequest.objects.get()
        self.assertEqual(row.performance_id, perf.pk)
        self.assertEqual(row.user_id, self.taker.pk)
        self.assertEqual(row.state, "pending")
        self.assertIsNone(row.settled_at)

    def test_applying_twice_is_refused_with_the_state_code(self):
        perf = self._completed_perf()
        self.assertEqual(self._apply(perf.pk)["code"], 0)

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 1035, body)
        self.assertIn("已申请过", body["msg"])
        # 只留下第一条。
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)

    def test_someone_elses_performance_is_refused(self):
        other = User.objects.create_user(username="income_owner_b")
        perf = self._perf(self._order(), user=other)

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 1035, body)
        self.assertIn("不存在或不属于你", body["msg"])
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_an_unfinished_order_is_refused(self):
        perf = self._perf(self._order(status=OrderStatus.IN_PROGRESS))

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 1035, body)
        self.assertIn("订单未完成", body["msg"])
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_an_already_settled_performance_is_refused(self):
        perf = self._completed_perf(settle_status=SettleStatus.SETTLED)

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 1035, body)
        self.assertIn("已结账", body["msg"])
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_a_sales_performance_is_refused(self):
        """「我接的单」只认 `打单工资`：销售提成行不是他接的单。"""
        perf = self._perf(self._order(), emp_type=EmpType.SALES)

        body = self._apply(perf.pk)

        self.assertEqual(body["code"], 1035, body)
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_malformed_payloads_are_validation_errors_not_500(self):
        for payload in ({}, {"performance_id": "abc"}, {"performance_id": None}):
            body = self.client.post(
                REQUESTS_URL, payload, content_type="application/json"
            ).json()
            self.assertEqual(body["code"], 1000, (payload, body))

    def test_the_limit_is_a_separate_business_code(self):
        """额度用完 = `1036`（与「这一行不能申请」的 1035 分开）。"""
        first = self._completed_perf()
        second = self._completed_perf()
        override = override_settings(EARLY_SETTLE_LIMIT=1)
        override.enable()
        self.addCleanup(override.disable)

        self.assertEqual(self._apply(first.pk)["code"], 0)
        body = self._apply(second.pk)

        self.assertEqual(body["code"], 1036, body)
        self.assertIn("额度已用完", body["msg"])
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)

    def test_the_list_returns_my_requests_and_the_quota(self):
        first = self._completed_perf()
        second = self._completed_perf()
        self.assertEqual(self._apply(first.pk)["code"], 0)
        self.assertEqual(self._apply(second.pk)["code"], 0)

        body = self.client.get(REQUESTS_URL).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["early_settlement"], {"used": 2, "limit": 3})
        items = body["data"]["items"]
        self.assertEqual({item["performance_id"] for item in items}, {first.pk, second.pk})
        for item in items:
            self.assertEqual(item["state"], "pending")
            self.assertIsNone(item["settled_at"])
            self.assertTrue(item["order_no"])

    def test_the_list_only_contains_my_own_requests(self):
        other = User.objects.create_user(username="income_owner_c")
        their_perf = self._perf(self._order(), user=other)
        PerformanceSettleRequest.objects.create(performance=their_perf, user=other)
        mine = self._completed_perf()
        self.assertEqual(self._apply(mine.pk)["code"], 0)

        body = self.client.get(REQUESTS_URL).json()

        self.assertEqual(
            [item["performance_id"] for item in body["data"]["items"]], [mine.pk]
        )
        self.assertEqual(body["data"]["early_settlement"]["used"], 1)

    def test_settling_the_performance_closes_the_request_too(self):
        """spec §3.4 第 6 条：财务结掉那一行 ⇒ 申请变 `settled`，时刻**逐值相同**。"""
        perf = self._completed_perf(bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)
        make_logged_in_client(username="income_admin", is_superuser=True)
        admin = User.objects.get(username="income_admin")

        settle_performances([perf.pk], actor="财务", user=admin)

        perf.refresh_from_db()
        row = PerformanceSettleRequest.objects.get()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(row.state, "settled")
        self.assertIsNotNone(row.settled_at)
        self.assertEqual(row.settled_at, perf.settled_at)

    def test_settling_a_performance_without_a_request_does_not_create_one(self):
        perf = self._completed_perf()
        make_logged_in_client(username="income_admin_2", is_superuser=True)
        admin = User.objects.get(username="income_admin_2")

        settle_performances([perf.pk], actor="财务", user=admin)

        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)

    def test_settling_only_an_adjustment_leaves_the_request_of_a_settled_row_alone(self):
        """评审 Minor-1：**只结调整记录**时，不许动已结账绩效行上那条申请。

        构造（这是真实可达的形态）：绩效行已被结掉（`settle_status=已结账`），
        它上面留了一条 `pending` 申请（并发窗口：财务结账先落库、申请随后写入），
        之后又追加了一条未结账调整 ⇒ 财务再结一次，**这一轮只结那条调整记录**
        （`settled_performances` 为空）。

        判据：那条申请**保持 `pending`、`settled_at` 仍是 `null`**。
        ⚠️ 判别力：把 `settlement.py` 里那句 `performance_id__in=settled_performance_ids`
        改成 `ids` ⇒ 本用例红（会把一条**没被本次结账**的申请写成「已结账」，而那一刻
        财务并没有就这一行付款）。
        """
        perf = self._completed_perf(bonus="50.00")
        # 第一轮：这一行被结掉（直接写列，绕过 `settle_performances`——本用例要的是
        # 「已结账行上还留着一笔 pending 申请」这个起点）。
        Performance.objects.filter(pk=perf.pk).update(
            settle_status=SettleStatus.SETTLED,
            settled_at=timezone.now(),
            settled_amount=Decimal("50.00"),
        )
        request_row = PerformanceSettleRequest.objects.create(
            performance=perf, user=self.taker
        )
        self.assertEqual(request_row.state, "pending")
        # 已结账行上的**新**未结账调整（坏账出口）：这一轮财务只结它。
        adjustment = PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-10.00"),
            reason="对账调整",
            settle_status=SettleStatus.UNSETTLED,
        )
        make_logged_in_client(username="income_admin_3", is_superuser=True)
        admin = User.objects.get(username="income_admin_3")

        result = settle_performances([perf.pk], actor="财务", user=admin)

        # 阳性对照：这一轮**真的**结掉了那条调整记录（否则下面「申请没动」是空判据）。
        self.assertEqual(result.settled_adjustments, [adjustment.pk])
        self.assertEqual(result.settled_performances, [])
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "pending")
        self.assertIsNone(request_row.settled_at)


class SoftDeletedPerformanceTests(IncomeFixture):
    """评审 Important-1：垃圾桶软删的绩效行**不进四数、不能申请、不在「我的订单」**。

    ⚠️ 少了这条过滤的实测后果：summary 多算钱；软删行还能**成功申请**提前结算
    （白占 1/3 额度），而财务的报酬列表根本看不到它 ⇒ 那条申请永远结不掉。
    """

    def test_a_soft_deleted_row_is_invisible_everywhere_but_a_live_one_is_not(self):
        # 接手价来自订单默认价（`hall_price`）；绩效金额另给，两条口径分别断言。
        live = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        dead = self._perf(self._order(hall_price="80.00"), temp="80.00", bonus="80.00")
        # 垃圾桶（`POST /performances/<id>/delete/`）只写这一列，订单仍然活着。
        Performance.objects.filter(pk=dead.pk).update(deleted_at=timezone.now())

        data = self._summary()

        # 阳性对照：活行照常计入（否则「空集」也能让下面的 0 断言变绿）。
        self.assertEqual(data["budget"], "50.00")
        self.assertEqual(data["actual"], "50.00")
        self.assertEqual(data["unsettled"], "50.00")
        self.assertEqual(data["counts"]["budget"], 1)
        self.assertEqual(data["counts"]["actual"], 1)

        # 软删行不能申请 ⇒ 业务码 1035「该绩效不能申请」。
        self.assertEqual(self._apply(dead.pk)["code"], 1035)
        self.assertEqual(PerformanceSettleRequest.objects.count(), 0)
        # 阳性对照：活行能申请（证明 1035 不是「谁都申请不了」）。
        self.assertEqual(self._apply(live.pk)["code"], 0)

        listing = self.client.get(MY_ORDERS_URL).json()
        self.assertEqual(
            [row["order_no"] for row in listing["data"]["items"]],
            [live.order.order_no],
        )


class MyOrdersIncomeFilterTests(IncomeFixture):
    """spec §3.3：`?income=` 四档 + 非法值。"""

    def setUp(self):
        super().setUp()
        self.in_progress = self._perf(
            self._order(status=OrderStatus.IN_PROGRESS), bonus="10.00"
        )
        self.unsettled = self._perf(self._order(), bonus="20.00")
        self.settled = self._perf(
            self._order(), bonus="30.00", settle_status=SettleStatus.SETTLED
        )

    def _nos(self, income=None):
        params = {} if income is None else {"income": income}
        body = self.client.get(MY_ORDERS_URL, params).json()
        self.assertEqual(body["code"], 0, body)
        return sorted(row["order_no"] for row in body["data"]["items"])

    def test_all_is_the_default(self):
        everything = sorted(
            row.order.order_no
            for row in (self.in_progress, self.unsettled, self.settled)
        )
        self.assertEqual(self._nos(), everything)
        self.assertEqual(self._nos("all"), everything)

    def test_completed_only_keeps_finished_orders(self):
        self.assertEqual(
            self._nos("completed"),
            sorted([self.unsettled.order.order_no, self.settled.order.order_no]),
        )

    def test_unsettled_and_settled_split_the_completed_ones(self):
        self.assertEqual(self._nos("unsettled"), [self.unsettled.order.order_no])
        self.assertEqual(self._nos("settled"), [self.settled.order.order_no])

    def test_an_illegal_value_is_a_business_error(self):
        body = self.client.get(MY_ORDERS_URL, {"income": "bogus"}).json()
        self.assertEqual(body["code"], 1000, body)

    def test_an_empty_value_means_the_default(self):
        body = self.client.get(MY_ORDERS_URL, {"income": ""}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["total"], 3)


class MyOrdersSettleRequestedAtTests(IncomeFixture):
    """`/my-orders` 载荷也带**这一行自己的**申请时刻（2026-10-23 追加，产品缺口）。

    App 据此把「申请提前结算」按钮换成「已申请」——没有这个键，打手点第二次会被 1035 拒。
    """

    def test_each_row_carries_its_own_request_time_only(self):
        applied = self._perf(self._order(), bonus="40.00")
        not_applied = self._perf(self._order(), bonus="40.00")
        PerformanceSettleRequest.objects.create(performance=applied, user=self.taker)

        body = self.client.get(MY_ORDERS_URL).json()

        self.assertEqual(body["code"], 0, body)
        rows = {row["id"]: row for row in body["data"]["items"]}
        self.assertIn(applied.pk, rows)
        self.assertIn(not_applied.pk, rows)
        self.assertRegex(
            rows[applied.pk]["settle_requested_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+08:00$",
        )
        # 没申请的那一行必须是 `null` —— **不能**因为「这个人申请过别的单」就跟着显示
        # （那正是本条要防的泄漏形态）。
        self.assertIsNone(rows[not_applied.pk]["settle_requested_at"])

    def test_another_takers_request_never_leaks_into_my_rows(self):
        other = User.objects.create_user(username="income_other_2")
        their = self._perf(self._order(), user=other, bonus="40.00")
        PerformanceSettleRequest.objects.create(performance=their, user=other)
        mine = self._perf(self._order(), bonus="40.00")

        body = self.client.get(MY_ORDERS_URL).json()

        rows = {row["id"]: row for row in body["data"]["items"]}
        self.assertIn(mine.pk, rows)
        self.assertNotIn(their.pk, rows)      # 别人的行根本不在「我的订单」里
        self.assertIsNone(rows[mine.pk]["settle_requested_at"])


class PerformanceListSettleRequestedAtTests(IncomeFixture):
    """spec §3.4 第 5 条：财务列表能读到 `settle_requested_at`。"""

    def test_the_list_carries_the_request_time_and_null_without_one(self):
        applied = self._perf(self._order(), bonus="40.00")
        not_applied = self._perf(self._order(), bonus="40.00")
        PerformanceSettleRequest.objects.create(
            performance=applied, user=self.taker
        )
        admin = make_logged_in_client(username="income_list_admin", is_superuser=True)

        body = admin.get(PERFORMANCES_URL).json()

        self.assertEqual(body["code"], 0, body)
        rows = {row["id"]: row for row in body["data"]["items"]}
        self.assertIn(applied.pk, rows)
        self.assertIn(not_applied.pk, rows)
        # 有申请：项目时区（+08:00）的 ISO 串（与同载荷的 `settled_at` 同一条出口语义）。
        self.assertRegex(
            rows[applied.pk]["settle_requested_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+08:00$",
        )
        # 没申请：`null`（⛔ 不是空串、不是 0）。
        self.assertIsNone(rows[not_applied.pk]["settle_requested_at"])

    def test_the_new_key_does_not_leak_into_the_settlement_list(self):
        """结算列表的出参字段集**本批不动**（spec 只点名绩效列表）。"""
        perf = self._perf(self._order(), bonus="40.00")
        admin = make_logged_in_client(username="income_settlement_admin", is_superuser=True)

        body = admin.get("/api/business/settlements").json()

        self.assertEqual(body["code"], 0, body)
        rows = {row.get("id"): row for row in body["data"]["items"]}
        self.assertIn(perf.pk, rows)
        self.assertNotIn("settle_requested_at", rows[perf.pk])


class SettleRequestCancellationTests(IncomeFixture):
    """spec §8.1（2026-10-23 业务方改准）：申请随**撤销结账 / 订单退款**自动作废。

    两条路径的**口径不同**（别合并成一条）：

    - **撤销结账**：`pending` **和** `settled` 都作废 —— 结算被撤销 ⇒ 那次「已结」不成立，
      申请不能挡住重申（业务方原话「撤销结账要能重申」）；
    - **订单退款**：**只**作废 `pending` —— 已 `settled` 的申请意味着钱已按那次结账打过。

    ⇒ 下面的正对照是**一正一反**：撤销侧断言「settled 也变 cancelled、额度回落、可重申」；
    退款侧断言「settled 不动」。⛔ 两条不能都写成「不动」，那会把撤销侧的新口径测丢。
    """

    def _admin(self, username):
        make_logged_in_client(username=username, is_superuser=True)
        return User.objects.get(username=username)

    def _mark_settled_without_settling(self, perf):
        """把这一行置成「已结账」，但**不走** `settle_performances`。

        用途：造出「这一行已结账、压在它上面的申请还是 `pending`」这个形态——
        真实世界里它来自并发窗口（财务结账先落库、申请随后写入），也就是 §8.1 要兜的那一格。
        `settled_by` 写人名（不是 `"system"`），否则撤销结账会按 P0-1 整批拒绝。
        """
        Performance.objects.filter(pk=perf.pk).update(
            settle_status=SettleStatus.SETTLED,
            settled_at=timezone.now(),
            settled_by="财务",
            settled_amount=perf.bonus_amount,
        )
        perf.refresh_from_db()

    def test_revoking_a_settlement_cancels_the_pending_request_and_frees_the_quota(self):
        override = override_settings(EARLY_SETTLE_LIMIT=1)
        override.enable()
        self.addCleanup(override.disable)
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)          # 额度 1/1
        request_row = PerformanceSettleRequest.objects.get()
        first_created = request_row.created_at
        self._mark_settled_without_settling(perf)

        revoke_settlements(
            [perf.pk], actor="财务", user=self._admin("income_cancel_admin"), reason="对账有误"
        )

        # ① 申请作废（不是删除、也不是 settled），且没留下结账时刻。
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        # ② 额度 +1（按 summary 的真实出参断言，⛔ 不是自己数 ORM）。
        self.assertEqual(self._summary()["early_settlement"], {"used": 0, "limit": 1})
        # ③ App 侧不再显示「已申请」（否则按钮永远点不动、与后端「可重申」自相矛盾）。
        listing = self.client.get(MY_ORDERS_URL).json()
        self.assertEqual(len(listing["data"]["items"]), 1)
        self.assertIsNone(listing["data"]["items"][0]["settle_requested_at"])
        # ④ 可以重申：复用同一行、状态回 pending、申请时间换成本次。
        body = self._apply(perf.pk)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settle_request"]["state"], "pending")
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "pending")
        self.assertGreater(request_row.created_at, first_created)

    def test_revoking_also_cancels_a_settled_request_and_allows_reapply(self):
        """**撤销结账**：已 `settled` 的申请**也**作废（业务方 2026-10-23 改准）。

        口径：结算被撤销 ⇒ 那次「已结」不成立，申请不能挡住重申。作废之后：
        额度回落（`cancelled` 不占）、载荷回 `null`、可以重新申请（复用同一行）。
        ⚠️ 与退款路径**相反**（那边 `settled` 不动），对照见同类
        `test_a_refund_leaves_a_settled_request_alone`。
        """
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)
        admin = self._admin("income_cancel_admin_2")
        settle_performances([perf.pk], actor="财务", user=admin)
        request_row = PerformanceSettleRequest.objects.get()
        self.assertEqual(request_row.state, "settled")
        self.assertIsNotNone(request_row.settled_at)
        self.assertEqual(self._summary()["early_settlement"]["used"], 1)

        revoke_settlements([perf.pk], actor="财务", user=admin, reason="对账有误")

        # ① 已结的申请也作废，且旧结账时刻被清掉（那次结账已经被撤销）。
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        # ② 额度回落（作废不占）。
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)
        # ③ 载荷对作废申请回 null ⇒ App 把按钮换回「申请」。
        listing = self.client.get(MY_ORDERS_URL).json()
        self.assertEqual(len(listing["data"]["items"]), 1)
        self.assertIsNone(listing["data"]["items"][0]["settle_requested_at"])
        # ④ 撤销之后这一行是「未结账」⇒ 可以重申（复用同一行）。
        body = self._apply(perf.pk)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settle_request"]["state"], "pending")
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)

    def test_refunding_the_order_cancels_the_pending_request(self):
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)
        self.assertEqual(self._summary()["early_settlement"]["used"], 1)

        apply_transition(
            perf.order, OrderStatus.REFUNDED, nodes={"order.change_status"}
        )

        request_row = PerformanceSettleRequest.objects.get()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)

    def test_a_refund_leaves_a_settled_request_alone(self):
        """正对照：已结账的申请不随退款被改成 `cancelled`。

        ⚠️ **构造说明**：正常路径 `apply_transition(→REFUNDED)` 会被「绩效已结账，不能改状态」
        那道闸拒掉（`order_status.py` 的 `status_change_denial`）⇒ 打手行结过账的单**到不了**
        退款钩子。所以这里**直接调钩子**（`on_order_refunded`），判据钉的是钩子里那条
        `state=PENDING` 的过滤：已结账的申请不许被退款改写作废（spec §8.1 后半句）。
        """
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)
        admin = self._admin("income_cancel_admin_3")
        settle_performances([perf.pk], actor="财务", user=admin)
        request_row = PerformanceSettleRequest.objects.get()
        self.assertEqual(request_row.state, "settled")
        settled_at = request_row.settled_at

        on_order_refunded(perf.order, OrderStatus.COMPLETED)

        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "settled")
        self.assertEqual(request_row.settled_at, settled_at)

    def test_cancellation_is_idempotent(self):
        """重复作废命中 0 行（同一单重复退款 / 重复撤销不会重复扣额度）。"""
        from apps.business.settle_requests import cancel_pending_settle_requests

        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        self.assertEqual(self._apply(perf.pk)["code"], 0)

        self.assertEqual(cancel_pending_settle_requests([perf.pk]), 1)
        self.assertEqual(cancel_pending_settle_requests([perf.pk]), 0)

        self.assertEqual(
            PerformanceSettleRequest.objects.get().state, "cancelled"
        )
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)


class SettleRequestCancelledOnTakerChangeTests(IncomeFixture):
    """spec §8.1 的**第三条作废路径**（2026-10-23 业务方拍板）：清接单人 / 回池 / 重指派 /
    绩效行软删 ⇒ 该行上 `pending` 的提前结算申请置 `cancelled`。

    口径：**只作废 `pending`**（`settled` 的已经按那次结账打了钱，换人 / 删行不改变这个事实）。
    ⇒ 下面每组都有「作废」与「settled 不动」两侧，⛔ 少了正对照，一个「连 settled 一起改」
    的实现会全绿。

    ⚠️ 两条入口共用同一个落点：`/clear-taker/` 与「回池」都经 `apply_transition(→UNASSIGNED)`
    的钩子 `_on_pool_entry` → `reset_taker_performance`；`/assign/` 换人直接在
    `assign_order` 里调同一个函数 ⇒ 作废写在 `reset_taker_performance` 一处覆盖三条。
    """

    def _admin_client(self, username):
        """一个有全权限的**请求方**（超管 + 「系统管理员」职位）——端点用，不是断言的宾语。"""
        return make_logged_in_client(username=username, is_superuser=True)

    def _pending(self, perf):
        """走**产品路径**建一条 `pending` 申请（并要求它真的落库）。"""
        body = self._apply(perf.pk)
        self.assertEqual(body["code"], 0, body)
        row = PerformanceSettleRequest.objects.get()
        self.assertEqual(row.state, "pending")
        return row

    def test_clearing_the_taker_cancels_the_pending_request(self):
        """`/clear-taker/` 端点 ⇒ 申请作废、额度回落、该行不再属于我。"""
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        request_row = self._pending(perf)
        admin = self._admin_client("income_clear_admin")

        response = admin.post(
            f"/api/business/orders/{perf.order.order_no}/clear-taker/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        # 额度回落（作废不占）。⚠️ 这一行已经不归我了，所以口径只能从额度看。
        self.assertEqual(self._summary()["early_settlement"], {"used": 0, "limit": 3})
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)

    def test_going_back_to_the_pool_cancels_the_pending_request(self):
        """回池（`apply_transition → UNASSIGNED`，与清接单人同一个钩子）⇒ 申请作废。"""
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        request_row = self._pending(perf)

        apply_transition(
            perf.order,
            OrderStatus.UNASSIGNED,
            nodes={"order.change_status"},
            allow_taker_release=True,
        )

        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)

    def test_reassigning_cancels_the_old_request_and_the_new_taker_can_apply(self):
        """重指派 ⇒ **旧人**的申请作废、新人那行不显示「已申请」、新人可重新申请（额度是新的）。"""
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        old_request = self._pending(perf)
        new_client = make_logged_in_client(
            username="income_new_taker", groups=("代练员",), nodes=MY_ORDERS_NODES
        )
        new_taker = User.objects.get(username="income_new_taker")
        admin = self._admin_client("income_assign_admin")
        # `/assign/` 要求单在 IN_PROGRESS（R-3）；`COMPLETED → IN_PROGRESS` 是合法边。
        apply_transition(
            perf.order, OrderStatus.IN_PROGRESS, nodes={"order.change_status"}
        )

        response = admin.post(
            f"/api/business/orders/{perf.order.order_no}/assign/",
            {"user_id": new_taker.pk},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        old_request.refresh_from_db()
        self.assertEqual(old_request.state, "cancelled")
        # ① 旧人的额度回落。
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)
        # ② 新人那一行**不**挂着旧人的申请（不然财务列表会看到「旧人已申请」）。
        listing = new_client.get(MY_ORDERS_URL).json()
        self.assertEqual(listing["code"], 0, listing)
        rows = {row["id"]: row for row in listing["data"]["items"]}
        self.assertIn(perf.pk, rows)
        self.assertIsNone(rows[perf.pk]["settle_requested_at"])
        # ③ 新人可以重新申请（额度是**新人**的，不是继承旧人的）。
        apply_transition(
            perf.order, OrderStatus.PENDING_REVIEW, nodes={"order.change_status"}
        )
        apply_transition(
            perf.order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES
        )
        body = new_client.post(
            REQUESTS_URL,
            {"performance_id": perf.pk},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["early_settlement"], {"used": 1, "limit": 3})
        self.assertEqual(PerformanceSettleRequest.objects.count(), 1)

    def test_soft_deleting_the_performance_cancels_the_pending_request(self):
        """垃圾桶（绩效行软删）⇒ 申请作废 + 额度回落。"""
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        request_row = self._pending(perf)
        admin = self._admin_client("income_delete_admin")

        response = admin.post(
            f"/api/business/performances/{perf.pk}/delete/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")
        self.assertIsNone(request_row.settled_at)
        self.assertEqual(self._summary()["early_settlement"]["used"], 0)

    def test_soft_deleting_a_taker_less_row_also_cancels(self):
        """历史 / 脏行：接单人早被清过、申请还挂着 `pending` ⇒ 软删是它唯一的收口点。

        ⚠️ 这一条钉的是**软删端点自己那一句**：`taker_cleared=False` ⇒ 不会走
        `apply_transition → reset_taker_performance`，只有端点里的作废能命中它。
        判别力：把端点里那句删掉 ⇒ 本用例红（申请停在 `pending`）。
        """
        perf = self._perf(self._order(hall_price="50.00"), temp="50.00", bonus="50.00")
        request_row = self._pending(perf)
        # 模拟本口径上线前的历史行：接单人已被清掉，申请却没被收口。
        Performance.objects.filter(pk=perf.pk).update(user=None, review_status=None)
        admin = self._admin_client("income_delete_admin_2")

        response = admin.post(
            f"/api/business/performances/{perf.pk}/delete/",
            {},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        request_row.refresh_from_db()
        self.assertEqual(request_row.state, "cancelled")

    def test_settled_requests_survive_all_four_paths(self):
        """正对照：`settled` 的申请在四条路径里**都不动**（只命中 `pending`）。

        ⚠️ 构造说明：已结账的**申请**配一条**未结账**的绩效行——生产上这是本口径上线前的
        历史 / 脏行（已结账的绩效行会被清接单人 / 软删 / 改派各自的上游闸门拒掉，
        根本到不了本函数）。这里直接写状态，为的是**只**钉「作废只命中 pending」这条判据。
        """
        rows = []
        for _ in range(4):
            perf = self._perf(
                self._order(hall_price="50.00"), temp="50.00", bonus="50.00"
            )
            request_row = PerformanceSettleRequest.objects.create(
                performance=perf,
                user=self.taker,
                state=PerformanceSettleRequest.State.SETTLED,
                settled_at=timezone.now(),
            )
            rows.append((perf, request_row))
        admin = self._admin_client("income_control_admin")

        # ① 清接单人（HTTP）
        perf1, _ = rows[0]
        admin.post(
            f"/api/business/orders/{perf1.order.order_no}/clear-taker/",
            {},
            content_type="application/json",
        )
        # ② 回池（领域入口）
        perf2, _ = rows[1]
        apply_transition(
            perf2.order,
            OrderStatus.UNASSIGNED,
            nodes={"order.change_status"},
            allow_taker_release=True,
        )
        # ③ 重指派（HTTP；先转 IN_PROGRESS）
        perf3, _ = rows[2]
        # 重指派需要一个「有游戏权限」的新人 ⇒ 用真实登录夹具建号（它的 client 不参与断言）。
        make_logged_in_client(
            username="income_control_new_taker", groups=("代练员",), nodes=MY_ORDERS_NODES
        )
        new_taker = User.objects.get(username="income_control_new_taker")
        apply_transition(
            perf3.order, OrderStatus.IN_PROGRESS, nodes={"order.change_status"}
        )
        admin.post(
            f"/api/business/orders/{perf3.order.order_no}/assign/",
            {"user_id": new_taker.pk},
            content_type="application/json",
        )
        # ④ 软删
        perf4, _ = rows[3]
        admin.post(
            f"/api/business/performances/{perf4.pk}/delete/",
            {},
            content_type="application/json",
        )

        for _perf, request_row in rows:
            request_row.refresh_from_db()
            self.assertEqual(request_row.state, "settled")
            self.assertIsNotNone(request_row.settled_at)
        # 占额度照旧（settled 仍算用掉一条）——四条申请都还在。
        self.assertEqual(
            PerformanceSettleRequest.objects.filter(state="settled").count(), 4
        )
