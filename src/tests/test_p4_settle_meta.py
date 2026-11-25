"""P4-FE-0：绩效的「结算时间 / 结算人」两列 + 结算列表暴露 `project` / `settled_at` / `settled_by`。

需求来源：业务方裁定——对标的结算记录页（`/Emp/billsummary`）有
「结算时间」「结算人」「项目」三列，而我们 `Performance` 上没有前两个字段、
结算列表也没暴露 `Order.project_text`。

四条纪律（每条都有会红的用例）：

1. **T1**：结账后逐行断言 `settled_at` 非空、`settled_by == 该 actor`（**字面值**）；
   未结账的行 `settled_at is None`、`settled_by == ""`。
2. ⭐ **T2**：**「只结调整记录」不覆盖绩效行的 `settled_at` / `settled_by`**——
   第一次结完整行后，那两个字段是**上一轮**的结算人与时间；再追加一条调整记录并再结一次，
   行上这两个字段必须**逐字不变**（记下第一次读到的值比对，⛔ 不用 `now()` 之类的活值）。
3. **T3**：`GET /api/business/settlements` 的每一行都带 `project` / `settled_at` / `settled_by`，
   `project` 等于该订单的 `project_text`（**字面值**）、未结账时 `settled_at` 是 `null`。
   ⭐ 并钉住「`project` 这一列**不引入 N+1**」：行数从 1 涨到 5，SQL 条数**不变**。
4. **T4**：第 5 步 `update()` 的 **rowcount 校验**在加了两个字段之后**没有被削弱**——
   用确定性桩让它谎报 0 行 ⇒ `1006` + 整批回滚 + 新字段一个都没写 + 零审计行。

⚠️ **判别力要求**（brief 的变异清单）：T1 的 `actor` 与绩效归属人的 `username`
**刻意取不同的值**（`"cs-1"` vs `"taker-a"`）——否则「错写成 `user`」也能绿。
⚠️ 期望值一律写**字面值**（E-0024）：`"cs-1"` / `"已结账"` / `1006`，
不引被测模块导出的常量（自指断言不会红）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog,
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 结算列表端点（无尾斜杠，与 `test_settlement_api.py` 的既有约定一致）。
SETTLEMENTS_URL = "/api/business/settlements"
#: 结账端点。
SETTLE_URL = "/api/business/settlements/settle"

#: ⭐ T1 的判别力关键：结算人**与绩效归属人不同名**（`username="taker-a"`）。
#: 若实现把 `settled_by` 写成 `user` 而不是 `actor`，两者不同名时用例必红。
ACTOR = "cs-1"


class _SettleMetaFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整记录的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 业务用例用管理员只是为了跳过建角色目录（闸门本身由既有 P4-7/P4-8 的权限测试钉住）。
        self.finance = make_logged_in_client(
            username="finance-fe0", is_superuser=True
        )
        self.taker_a = User.objects.create_user(username="taker-a")
        self.taker_b = User.objects.create_user(username="taker-b")
        # ⭐ 按游戏权限收窄：本文件的直调用例把 `taker_a` 当作
        #    `settle_performances(..., user=)` 的**请求方**（数据权限那一维）⇒
        #    显式声明它持有本夹具的游戏。本文件不依赖「没有档案」这个前提
        #    （`grep StaffProfile` 零命中），补档案不改任何期望值。
        grant_games(self.taker_a, self.game)
        self._order_seq = 0

    def _order(self, *, project_text="原神-深渊代练"):
        self._order_seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092000000{self._order_seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
            project_text=project_text,
        )

    def _perf(self, order, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(self, performance, amount):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=SettleStatus.UNSETTLED,
        )


class SettledMetaWriteTests(_SettleMetaFixture):
    """T1 / T2：`settle_performances` 第 5 步写入这两个字段，且「只结调整」不覆盖行。"""

    def test_T1_settling_writes_the_actor_and_a_non_null_time_on_every_row(self):
        """T1：结账后逐行 `settled_at` 非空、`settled_by == "cs-1"`（字面值）。

        ⚠️ 走**领域层直调**（不是 API）正是为了能传一个与归属人**不同名**的 `actor`：
        `/settle` 的 `actor` 恒等于登录名，构造不出「两者不同名」的形状，
        而那正是让「错写成 `user`」这条变异会红的唯一办法。
        """
        first = self._perf(self._order(), self.taker_a)
        second = self._perf(self._order(), self.taker_b)
        # 对照组：**不在这批里**的行，两个字段必须保持「没结过账」。
        untouched = self._perf(self._order(), self.taker_b)

        settle_performances(
            [first.id, second.id], actor=ACTOR, user=self.taker_a
        )

        first.refresh_from_db()
        second.refresh_from_db()
        untouched.refresh_from_db()
        for row in (first, second):
            self.assertEqual(row.settled_by, "cs-1")
            self.assertIsNotNone(row.settled_at)
            self.assertEqual(row.settle_status, "已结账")
        self.assertIsNone(untouched.settled_at)
        self.assertEqual(untouched.settled_by, "")
        self.assertEqual(untouched.settle_status, "未结账")

    def test_T1b_the_api_writes_the_logged_in_username_as_the_settler(self):
        """T1 的端到端对照：`/settle` 的 `actor` 来自认证态 ⇒ 落库的是登录名。

        与上一条互补：上一条钉「`actor` 参数真的被用上」，这条钉「视图真的把登录名传进去」
        （少了它，视图漏传 `actor=""` 时上一条仍绿）。
        """
        perf = self._perf(self._order(), self.taker_a)

        body = self.finance.post(
            SETTLE_URL, {"performance_ids": [perf.id]}, content_type="application/json"
        ).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settled_by, "finance-fe0")
        self.assertIsNotNone(perf.settled_at)

    def test_T2_settling_only_an_adjustment_does_not_touch_the_rows_settle_meta(self):
        """⭐ T2：已结账的行被追加新调整记录后**再结一次**，行上两个字段**逐字不变**。

        判据用**第一次读到的值**比对（`assertEqual(第二次, 第一次)`），
        ⛔ 不写 `assertIsNotNone` / 不拿 `now()` 比——那些对「被覆盖成新值」照样绿。
        两次的 `actor` 刻意不同（`"cs-1"` → `"cs-2"`）：若实现顺手把行也 `UPDATE` 一遍，
        两个断言都会红。
        """
        perf = self._perf(self._order(), self.taker_a)
        settle_performances([perf.id], actor=ACTOR, user=self.taker_a)
        perf.refresh_from_db()
        first_settled_at = perf.settled_at
        first_settled_by = perf.settled_by
        self.assertEqual(first_settled_by, "cs-1")
        self.assertIsNotNone(first_settled_at)

        adjustment = self._adj(perf, "-5.00")

        result = settle_performances([perf.id], actor="cs-2", user=self.taker_a)

        # 前置哨兵：本轮**真的结了东西**（否则「没变」可能只是「什么都没发生」）。
        self.assertEqual(result.settled_performances, [])
        self.assertEqual(result.settled_adjustments, [adjustment.id])

        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settled_by, first_settled_by)
        self.assertEqual(perf.settled_at, first_settled_at)
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)
        # 审计确实写了本轮那一条（本轮结的是调整记录）。
        # ⭐ M3a：旧表 `performance_id` 的判据改到新表（动作 `order.refund` 同级——
        # 这里是 `performance.settle`），按「订单号」定位这批两行。
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle", order_no=perf.order.order_no
            ).count(),
            2,
        )
        # 旧出口退役：这条路径**一行都不许**再写旧表。
        self.assertEqual(AuditLog.objects.count(), 0)


class SettlementListFieldTests(_SettleMetaFixture):
    """T3：结算列表暴露 `project` / `settled_at` / `settled_by`，且不引入 N+1。"""

    def _items(self, params=None):
        body = self.finance.get(SETTLEMENTS_URL, params or {}).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def test_T3_the_row_carries_the_three_new_fields_with_literal_values(self):
        """T3：结过账的行三个字段都在；`project` = `project_text`（字面值）、结算人是字面值。"""
        order = self._order(project_text="王者荣耀-上分")
        perf = self._perf(order, self.taker_a)
        settle_performances([perf.id], actor=ACTOR, user=self.taker_a)

        items = self._items()

        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["project"], "王者荣耀-上分")
        self.assertEqual(row["settled_by"], "cs-1")
        self.assertIsNotNone(row["settled_at"])
        self.assertEqual(row["settle_status"], "已结账")

    def test_T3_an_unsettled_row_reports_null_time_and_an_empty_settler(self):
        """T3 边界：未结账 ⇒ `settled_at` 是 `null`、`settled_by` 是**空串**（不转 `null`）。

        与上面同一次比对里同时放一条结过账的行：**先证明有数据**再下否定断言
        （E-0004：只调一次就返回空集时先怀疑自己的调用）。
        """
        settled = self._perf(self._order(project_text="英雄联盟-排位"), self.taker_a)
        settle_performances([settled.id], actor=ACTOR, user=self.taker_a)
        self._perf(self._order(project_text="原神-日常"), self.taker_b)

        items = self._items()

        self.assertEqual(len(items), 2)
        by_project = {row["project"]: row for row in items}
        self.assertEqual(by_project["英雄联盟-排位"]["settled_by"], "cs-1")
        unsettled = by_project["原神-日常"]
        self.assertIsNone(unsettled["settled_at"])
        self.assertEqual(unsettled["settled_by"], "")

    def test_T3_the_project_column_adds_no_query_per_row(self):
        """⭐ N+1 判据：行数 1 → 5，`GET /settlements` 的 SQL 条数**不变**。

        `project` 读 `order.project_text`，而视图的 `get_queryset` 已经
        `select_related("order", "user")` ⇒ 它与既有的 `order_no` 走同一次 JOIN。
        ⛔ 若哪天有人把 `select_related("order")` 去掉，这条会红（每条多一次订单查询）。
        """
        orders = [self._order(project_text=f"项目-{i}") for i in range(5)]
        perfs = [self._perf(order, self.taker_a) for order in orders]

        with CaptureQueriesContext(connection) as one_row:
            items = self._items({"order_no": perfs[0].order.order_no})
            self.assertEqual(len(items), 1)
        with CaptureQueriesContext(connection) as five_rows:
            items = self._items()
            self.assertEqual(len(items), 5)

        self.assertEqual(
            len(five_rows.captured_queries),
            len(one_row.captured_queries),
            "行数从 1 到 5 时 SQL 条数变了 ⇒ `project` 引入了 N+1：\n"
            + "\n".join(q["sql"] for q in five_rows.captured_queries),
        )


class RowcountGuardRegressionTests(_SettleMetaFixture):
    """T4：第 5 步的 rowcount 校验在加了两个字段之后**没有被削弱**（E-0022 同族）。"""

    def test_T4_a_stale_rowcount_still_rolls_back_and_leaves_the_new_fields_unwritten(self):
        """T4：`Performance.QuerySet.update` 谎报 **0 行** ⇒ `1006` + 两个新字段一个都没写。

        这条与既有 `test_settle_api.py::test_a_stale_performance_rowcount_rolls_back_
        and_writes_no_audit` 是**同一判据**，但断言面多了「新字段」：`update()` 的字段变多了，
        若实现顺手把比较改成「比字段」或干脆去掉比较，这条会红。
        """
        perf = self._perf(self._order(), self.taker_a)
        self._adj(perf, "-5.00")
        calls = []
        real_update = QuerySet.update

        def fake_update(queryset, **kwargs):
            calls.append((queryset.model, tuple(sorted(kwargs))))
            if queryset.model is Performance:
                return 0
            return real_update(queryset, **kwargs)

        with mock.patch.object(QuerySet, "update", fake_update):
            body = self.finance.post(
                SETTLE_URL,
                {"performance_ids": [perf.id]},
                content_type="application/json",
            ).json()

        self.assertIn(
            (Performance, ("settle_status", "settled_at", "settled_by")),
            calls,
            "桩没接上，或第 5 步没有把 settled_at / settled_by 一起写进 update()：%r" % (calls,),
        )
        self.assertEqual(body["code"], 1006)
        self.assertIn("本批在你读取之后已被改动", body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertEqual(perf.settled_by, "")
        self.assertEqual(OperationLog.objects.count(), 0)
