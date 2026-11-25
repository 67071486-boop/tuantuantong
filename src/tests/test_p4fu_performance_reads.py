"""P4FU-C：绩效页的**只读**能力（A 出参 / B 筛选扩展 / C 支付宝查重）。

对应 brief §2 的三件事：

- **A**：列表出参加只读 `order_status`（D-0071 ③ 的后端一半）——不改 `review_status`，
  且 `select_related("order")` 的既有接线有**实测 SQL 条数**的判据（不是只看代码）。
- **B**：`PerformanceListSearchSerializer` 的新参数**逐条**「筛到了 / 不传时不变」，
  含边界值（`customer_type=0`、`order_type=-1` 非法、逗号串空项与超个数）。
- **C**：`GET /api/business/performances/alipay-duplicates` 的语义 / 出参 / 权限 / 隔离。

⚠️ **期望值一律写字面值**（E-0024）：状态写 `"COMPLETED"` / `"已完成"`、业务码写 `1000`
/`403`，不引被测模块导出的常量（自指断言不会红）。
⚠️ **跨租户判据不用主键**（AGENTS.md §5）：本文件用 `alipay_account` 这个**租户内取值不同**
的字符串当判据。
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderSourceGroup,
    OrderStatus,
    OrderType,
    PaymentStatus,
    Performance,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client
from tests.xlsx_helpers import detail_rows, values_of, workbook_of

LIST_URL = "/api/business/performances/"
EXPORT_URL = "/api/business/performances/export"
ALIPAY_URL = "/api/business/performances/alipay-duplicates"

#: 支付宝查重的节点 code（与结算三个读接口同一个，写字面值不引常量）。
FINANCE_NODE = "finance.payroll"


class _PerfReadsFixture(TenantTestCase):
    """两游戏 / 两来源 / 两销售 / 两接单人的小夹具（各用例只表达自己的差异）。

    ⚠️ 功能用例用**超级管理员**登录（与 `_PerfWriteFixture` 同处置）：本文件的关注点是
    「筛选 / 出参 / 查重语义」，权限维度由 `test_needs_the_finance_payroll_node` 与
    既有的 `test_performance_api.py` 单独钉；超管让这些用例不随权限配置漂移。
    """

    def setUp(self):
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="王者荣耀")
        self.source_a = OrderSource.objects.create(name="来源甲")
        self.source_b = OrderSource.objects.create(name="来源乙")
        self.sales_a = User.objects.create_user(username="sales_a")
        self.sales_b = User.objects.create_user(username="sales_b")
        self.taker_a = User.objects.create_user(username="taker_a")
        self.taker_b = User.objects.create_user(username="taker_b")
        self._seq = 0
        self.client = make_logged_in_client(username="p4fu-c-admin", is_superuser=True)

    def _order(self, **overrides):
        self._seq += 1
        fields = {
            "game": self.game_a,
            "order_no": f"2026092200000{self._seq:07d}",   # 20 位
            "status": OrderStatus.IN_PROGRESS,
            "hall_price": Decimal("100.00"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _perf(self, order, *, user=None, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("0.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _body(self, params=None):
        return self.client.get(LIST_URL, params or {}).json()

    def _nos(self, params=None):
        body = self._body(params)
        assert body["code"] == 0, body
        return sorted(row["order_no"] for row in body["data"]["items"])

    def _assert_only(self, expected, params):
        """一条判据里同时表达「筛到了」与「不传时不变」——省得每个参数写两遍。"""
        both = sorted(order.order_no for order in expected["all"])
        self.assertEqual(self._nos(), both, "不传该参数时结果变了")
        self.assertEqual(
            self._nos(params), sorted(o.order_no for o in expected["hit"]), params
        )


# ── A：列表出参加 `order_status`（D-0071 ③）────────────────────────────


class OrderStatusPayloadTests(_PerfReadsFixture):
    def test_recorder_display_is_batched_and_falls_back_for_unknown_account(self):
        User.objects.create_user(username="recorder_a", first_name="记录员甲")
        self._perf(self._order(created_by="recorder_a"), user=self.taker_a)
        with CaptureQueriesContext(connection) as one:
            rows = self._body()["data"]["items"]
        self.assertEqual(rows[0]["log_user_name"], "recorder_a")
        self.assertEqual(rows[0]["log_user_display"], "记录员甲")
        self._perf(self._order(created_by="missing_recorder"), user=self.taker_a)
        with CaptureQueriesContext(connection) as two:
            rows = self._body()["data"]["items"]
        one_user_queries = [q for q in one if '"auth_user"' in q["sql"]]
        two_user_queries = [q for q in two if '"auth_user"' in q["sql"]]
        self.assertEqual(len(one_user_queries), len(two_user_queries))
        self.assertEqual({r["log_user_display"] for r in rows}, {"记录员甲", "missing_recorder"})

    def test_list_row_carries_the_order_status(self):
        """那一列要显示**订单状态**（对标 `orderSta`），不是审核状态。"""
        order = self._order(status=OrderStatus.COMPLETED)
        self._perf(order, user=self.taker_a)

        row = self._body()["data"]["items"][0]
        self.assertEqual(row["order_status"], "COMPLETED")   # 字面值（E-0024）

    def test_review_status_is_still_present_and_not_replaced(self):
        """⛔ 不许把 `review_status` 换成订单状态：取消闸门后它仍被 `/review` 与既有页面用。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=self.taker_a, review_status="待审核")

        row = self._body()["data"]["items"][0]
        self.assertEqual(row["review_status"], "待审核")
        self.assertEqual(row["order_status"], "IN_PROGRESS")

    def test_taker_row_with_none_user_is_no_longer_listed_at_all(self):
        """⚠️ **口径已变（2026-09-22 第四轮 A）**：`user` 为 NULL 的行（还没人接）
        **整行不进本页**——业务方原话「只有订单关联的打手产生了绩效才可以生成到这里」。

        本用例的前身是 `test_taker_row_with_none_user_also_carries_order_status`，
        它断的是「这种行也在列表里、也带 `order_status`」——那条期望**已被业务方裁定作废**，
        所以这里改成**方向相反**的判据（不是放宽断言：从「在列表里」改成「不在列表里」）。
        `order_status` 不依赖 `user` 这条读码结论仍由上面两条「有归属人」的用例覆盖。
        """
        order = self._order(status=OrderStatus.WAITING_GRAB)
        excluded = self._perf(order, user=None)

        body = self._body()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["total"], 0)
        self.assertEqual(body["data"]["items"], [])
        # 说明性对照：行仍在库里（不是被删了），只是不进本页。
        self.assertTrue(
            Performance.objects.filter(pk=excluded.pk, user__isnull=True).exists()
        )

    def test_order_status_does_not_add_a_query_per_row(self):
        """**实测 SQL 条数**：1 行与 5 行的查询次数完全相同 ⇒ `select_related("order")` 真的在。

        ⚠️ 不带 `select_related` 时序列化器会为每行的 `order.status` 各发一条查询，
        这条判据会红。判据是「**不随行数增长**」而不是某个具体数字：具体数字会被
        中间件 / 认证 / `SET search_path` 的条数变化带红（那是噪音，不是缺陷）。
        """
        first = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(first, user=self.taker_a)

        with CaptureQueriesContext(connection) as one:
            self.assertEqual(self._body()["code"], 0)
        one_count = len(one.captured_queries)

        for _ in range(4):
            more = self._order(status=OrderStatus.COMPLETED)
            self._perf(more, user=self.taker_b)

        with CaptureQueriesContext(connection) as five:
            self.assertEqual(self._body()["code"], 0)
        five_count = len(five.captured_queries)

        self.assertEqual(
            five_count, one_count,
            f"列表每多一行就多查一次库（1 行 {one_count} 条 / 5 行 {five_count} 条）",
        )


# ── A2：列表出参加 `source_order_no`（业务方报的缺陷）──────────


class SourceOrderNoPayloadTests(_PerfReadsFixture):
    """⭐ 2026-10-09：绩效列表的「关联订单」列要显示**来源单号** ⇒ 出参必须有它。

    业务方原话：「列表的关联订单号采用的是系统内置单号、这里关联的采用应该是来源单号」。
    实测依据（对标字段语义，**不是**推断）见 `.tools/recon/order-view/_SOURCE.txt:14-19`：
    同一次真机取数里 `data[0].orderId` 是 20 位**本系统**订单号、同行 `jionOrder` 是 19 位
    **关联 / 交易平台**单号；对标那一列的 templet 正文取 `res.jionOrder`、`data-id` 取
    `res.orderId`（`.tools/recon/suqing1/assets/JS2019/Emp/emplist.js:314-323`）。
    """

    def test_list_row_carries_the_source_order_no(self):
        """钉**值**、且要求两个键**不同形**（只断「有这个键」抓不到「抄了两遍 `order_no`」）。"""
        order = self._order()
        order.source_order_no = "TB-合成-绩效-来源甲"
        order.save(update_fields=["source_order_no"])
        self._perf(order, user=self.taker_a)

        row = self._body()["data"]["items"][0]

        self.assertEqual(row["source_order_no"], "TB-合成-绩效-来源甲")
        self.assertEqual(row["order_no"], order.order_no)
        self.assertNotEqual(row["source_order_no"], row["order_no"])

    def test_a_blank_source_order_no_is_an_empty_string_not_null(self):
        """没填来源单号 ⇒ **空串**（`blank=True` 非 NULL 的文本列），不是 `null`。

        ⚠️ 前端「空就渲染空、**不回落**成系统单号」那条判据的前提就是这一条：
        后端回 `null` 的话前端的 `?? ''` 会把它吞掉，两件事就分不开了。
        """
        self._perf(self._order(), user=self.taker_a)

        row = self._body()["data"]["items"][0]

        self.assertEqual(row["source_order_no"], "")
        self.assertIsNotNone(row["source_order_no"])

    def test_the_source_order_no_does_not_add_a_query_per_row(self):
        """**实测 SQL 条数**不随行数增长 ⇒ 新字段走的是**既有那次** `select_related("order")`。

        ⚠️ 与 `OrderStatusPayloadTests.test_order_status_does_not_add_a_query_per_row`
        同一条判据手法（判「不随行数增长」而不是某个具体数字）。
        """
        first = self._order()
        self._perf(first, user=self.taker_a)

        with CaptureQueriesContext(connection) as one:
            self.assertEqual(self._body()["code"], 0)
        one_count = len(one.captured_queries)

        for _ in range(4):
            more = self._order()
            self._perf(more, user=self.taker_b)

        with CaptureQueriesContext(connection) as five:
            self.assertEqual(self._body()["code"], 0)
        five_count = len(five.captured_queries)

        self.assertEqual(
            five_count, one_count,
            f"列表每多一行就多查一次库（1 行 {one_count} 条 / 5 行 {five_count} 条）",
        )


# ── B：筛选扩展（每个新参数一条「筛到了 / 不传时不变」）────────────────


class FilterTests(_PerfReadsFixture):
    def _two(self, **kw_a):
        """两条绩效 + 两个订单；`kw_a` 只覆盖第一个订单的字段差异。"""
        order_a = self._order(**kw_a)
        order_b = self._order()
        self._perf(order_a, user=self.taker_a)
        self._perf(order_b, user=self.taker_b)
        return order_a, order_b

    def test_filters_by_game(self):
        order_a, order_b = self._two(game=self.game_a)
        order_b.game = self.game_b
        order_b.save(update_fields=["game"])

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]}, {"game": self.game_a.id}
        )
        self.assertEqual(self._nos({"game": self.game_b.id}), [order_b.order_no])
        self.assertEqual(self._nos({"game": 999999}), [])       # 筛到了：不存在的游戏 = 空

    def test_filters_by_order_status(self):
        order_a, order_b = self._two(status=OrderStatus.COMPLETED)

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"order_status": "COMPLETED"},
        )
        self.assertEqual(self._nos({"order_status": "IN_PROGRESS"}), [order_b.order_no])

    def test_filters_by_order_status_accepts_a_comma_separated_list(self):
        """⭐ 2026-10-29 实装多选：`?order_status=码1,码2` 筛**并集**（`order__status__in`）。

        三张单三个状态：两个码的并集能同时筛中两张、把第三张留在结果外
        （「筛了」与「全返回」可区分）；顺序无关（`__in` 是集合语义）。
        """
        completed = self._order(status=OrderStatus.COMPLETED)
        unassigned = self._order(status=OrderStatus.UNASSIGNED)
        running = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(completed, user=self.taker_a)
        self._perf(unassigned, user=self.taker_b)
        self._perf(running, user=self.taker_a)

        both = sorted([completed.order_no, unassigned.order_no])
        self.assertEqual(self._nos({"order_status": "COMPLETED,UNASSIGNED"}), both)
        self.assertEqual(self._nos({"order_status": "UNASSIGNED,COMPLETED"}), both)
        # 阴性对照：没选的那档不出现（否则并集语义没生效）
        self.assertNotIn(running.order_no, self._nos({"order_status": "COMPLETED,UNASSIGNED"}))
        # 非法 token 混进串里 ⇒ 1000 且 msg 点名它（⛔ 不静默丢掉那截继续筛）
        body = self._body({"order_status": "COMPLETED,BOGUS"})
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("BOGUS", body["msg"])

    def test_order_status_over_50_tokens_is_rejected(self):
        """超个数（>50）⇒ `1000`，⛔ 不静默截断（与 `username` 那条同族；恰好 50 通过）。"""
        too_many = ",".join("IN_PROGRESS" for _ in range(51))
        body = self._body({"order_status": too_many})
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("50", body["msg"])
        boundary = ",".join("IN_PROGRESS" for _ in range(50))
        self.assertEqual(self._body({"order_status": boundary})["code"], 0)

    def test_order_status_overlong_string_is_1000(self):
        """整串长度 >1500 ⇒ `1000`（`CharField(max_length=1500)` 的闸，E-0010：不进 `__in` 查询）。

        与订单侧 `test_order_search_filters.StatusMultiSelectTests::
        test_overlong_string_is_1000` **对称**——逐 token 判据虽是共享的
        （`_order_status_tokens`），但 `max_length` 这一闸挂在**各自的字段**上；
        这里钉的是绩效侧入口（`search_params` → `PerformanceListSearchSerializer`）
        没有第二份长度判据可以漂。
        ⚠️ **必须连 msg 一起钉**：超长串里的 token 也过不了枚举闸，两条闸都给 `1000`
        ——只断码的话，把 `max_length` 改大这条用例**照样绿**（1000 来自枚举闸，实测踩过）。
        长度闸的 msg 带 `1500`（DRF `MaxLengthValidator` 的文案），照
        `test_comma_string_total_length_is_capped` 的同款判法。
        """
        body = self._body({"order_status": "X" * 1501})
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])
        self.assertIn("1500", body["msg"])

    def test_filters_by_order_type(self):
        order_a, order_b = self._two(order_type=OrderType.BOOST)
        order_b.order_type = OrderType.COMPANION
        order_b.save(update_fields=["order_type"])

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"order_type": OrderType.BOOST},
        )
        self.assertEqual(
            self._nos({"order_type": OrderType.COMPANION}), [order_b.order_no]
        )

    def test_filters_by_order_source(self):
        order_a, order_b = self._two(order_source=self.source_a)
        order_b.order_source = self.source_b
        order_b.save(update_fields=["order_source"])

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"order_source": self.source_a.id},
        )
        self.assertEqual(self._nos({"order_source": self.source_b.id}), [order_b.order_no])

    def test_filters_by_source_group(self):
        """⭐ 公司店铺分组（2026-10-26，计划 §4 #4）：按「订单的来源所属分组」筛。

        两半都断：组内的单命中、别组（此处 = 未分组）的单不出现；`_assert_only` 同时断
        「不传该参数时结果不变」。
        """
        group = OrderSourceGroup.objects.create(name="测试分组甲")
        self.source_a.group = group
        self.source_a.save(update_fields=["group"])
        order_a, order_b = self._two(order_source=self.source_a)
        order_b.order_source = self.source_b
        order_b.save(update_fields=["order_source"])

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"source_group": group.id},
        )
        # 反向：另一组的单不会出现（此处 order_b 的来源未分组 ⇒ 任何组都筛不到它）。
        self.assertNotIn(order_b.order_no, self._nos({"source_group": group.id}))

    def test_both_bills_are_filtered_and_do_not_leak(self):
        """两账单（`打单工资` / `销售提成`）**各自**按分组筛、互不泄漏。

        ⚠️ 两账单共用同一个端点、只靠 `emp_type` 分账（页签只切 `emp_type`）⇒ 这条判据
        同时钉住「筛选对两档都生效」与「一档筛出来的不会带出另一档的行」。
        """
        group = OrderSourceGroup.objects.create(name="测试分组乙")
        self.source_a.group = group
        self.source_a.save(update_fields=["group"])
        in_group = self._order(order_source=self.source_a)
        out_group = self._order(order_source=self.source_b)
        self._perf(in_group, user=self.taker_a, emp_type=EmpType.MAIN)
        self._perf(out_group, user=self.taker_b, emp_type=EmpType.SALES)

        def nos(**params):
            body = self._body(params)
            assert body["code"] == 0, body
            return sorted(row["order_no"] for row in body["data"]["items"])

        # 代练员账单：组内那一单命中。
        self.assertEqual(nos(emp_type=EmpType.MAIN, source_group=group.id), [in_group.order_no])
        # 销售客服账单：组内没有它的行 ⇒ 空；但它自己的行在（非空哨兵防「一律空」）。
        self.assertEqual(nos(emp_type=EmpType.SALES, source_group=group.id), [])
        self.assertEqual(nos(emp_type=EmpType.SALES), [out_group.order_no])

    def test_bad_source_group_is_1000(self):
        for raw in ("0", "-1", "abc"):
            with self.subTest(raw=raw):
                body = self._body({"source_group": raw})
                self.assertEqual(body["code"], 1000, body)
                self.assertIsNone(body["data"])

    def test_customer_type_zero_filters_by_sales_user(self):
        """⚠️ **`customer_type=0` 是合法值**（E-0021）：写成 `if customer_type:` 会走错列。

        构造刻意让两条订单在这两列上**交叉**（order_a 的 `sales_user` = order_b 的 `created_by`）
        ⇒ 无论判据写反还是被真值判断吃掉，都会筛到**另一条**，用例必红。
        """
        order_a, order_b = self._two(sales_user=self.sales_a, created_by="sales_b")
        order_b.sales_user = self.sales_b
        order_b.created_by = "sales_a"
        order_b.save(update_fields=["sales_user", "created_by"])

        # 缺省（不传 customer_type）按对标语义 = 0 = 销售人。
        self.assertEqual(
            self._nos({"customer": "sales_a"}), [order_a.order_no]
        )
        # 显式传 0 = 销售人。
        self.assertEqual(
            self._nos({"customer": "sales_a", "customer_type": 0}), [order_a.order_no]
        )
        # 显式传 10 = 发单人（同一个名字，命中的是**另一条**）。
        self.assertEqual(
            self._nos({"customer": "sales_a", "customer_type": 10}), [order_b.order_no]
        )
        # 多值（逗号串）。
        self.assertEqual(
            self._nos({"customer": "sales_a,sales_b", "customer_type": 0}),
            sorted([order_a.order_no, order_b.order_no]),
        )
        # ⚠️ 只传 customer_type（没有 customer）**不过滤**——0 不能被当成「筛过了」。
        self.assertEqual(len(self._nos({"customer_type": 0})), 2)

    def test_filters_by_username_batch(self):
        order_a, order_b = self._two()

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"username": "taker_a"},
        )
        self.assertEqual(
            self._nos({"username": "taker_a,taker_b"}),
            sorted([order_a.order_no, order_b.order_no]),
        )
        self.assertEqual(self._nos({"username": "nobody"}), [])

    def test_filters_by_order_no_is_exact(self):
        order_a, order_b = self._two()

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]},
            {"order_no": order_a.order_no},
        )
        # **精确**：单号的片段不该命中（与结算页 `order_no` 同口径）。
        self.assertEqual(self._nos({"order_no": order_a.order_no[:12]}), [])

    def test_filters_by_keyword_hits_order_no_and_role_name(self):
        """对标 `key1` 的 placeholder 原文 = 「订单号/角色名」⇒ 两个字段都该命中。"""
        order_a = self._order()
        order_b = self._order(game_role_name="角色乙")
        self._perf(order_a, user=self.taker_a)
        self._perf(order_b, user=self.taker_b)

        self.assertEqual(
            self._nos({"keyword": order_a.order_no[-7:]}), [order_a.order_no]
        )
        self.assertEqual(self._nos({"keyword": "角色乙"}), [order_b.order_no])
        self.assertEqual(self._nos(), sorted([order_a.order_no, order_b.order_no]))

    def test_filters_by_keyword_also_hits_source_order_no(self):
        """⭐ **2026-10-09 追加的第三个面：来源单号**（业务方报「搜索1 来源单号搜不到」）。

        ⚠️ **纯加法**：上面那条用例继续钉既有两项（系统单号 / 角色名），本条只加这一项。

        ⚠️ **口径边界（如实登记，别当对拍结论读）**：对标那一档发的 `key` 到底匹配
        系统单号还是来源单号，本线**没有实测出来**（`.tools/recon/` 里没有该端点的回包
        样本）⇒ 本项依据的是**业务方的明确口径**，不是与对标语料对拍的结果。
        同族先例：管理订单页 / 抢单大厅 / 订单回收站的 `KEYWORD_FIELDS` 已在 2026-10-04
        同样补过 `source_order_no`（`apps/business/selectors.py`）。
        """
        # 两行的来源单号必须**不同形**，否则「只有它命中」这条判据会恒真。
        order_a = self._order()
        order_a.source_order_no = "TB-合成-绩效-来源乙"
        order_a.save(update_fields=["source_order_no"])
        order_b = self._order()
        order_b.source_order_no = "TB-合成-绩效-来源甲"
        order_b.save(update_fields=["source_order_no"])
        self._perf(order_a, user=self.taker_a)
        self._perf(order_b, user=self.taker_b)

        # 送的是**来源单号**的片段（不是任何一行的系统单号）⇒ 只有 `order_b` 命中。
        self.assertEqual(self._nos({"keyword": "来源甲"}), [order_b.order_no])
        # 不传时两行都在——否则「筛了」与「全都返回」不可区分。
        self.assertEqual(self._nos(), sorted([order_a.order_no, order_b.order_no]))
        # 反向对照：换另一个来源单号的片段必须换回**另一条**。
        self.assertEqual(self._nos({"keyword": "来源乙"}), [order_a.order_no])

    def test_filters_by_keyword_with_no_match_is_empty(self):
        """**阴性对照**：三个面（系统单号 / 角色名 / 来源单号）**都不**匹配 ⇒ 空集。

        ⚠️ 为什么必须有：上面三条都是「命中 ⇒ 非空」，若实现退化成「恒返回全量」
        （例如把 `keyword` 那一档整个删掉），它们**全都照绿**；只有本条会红。
        """
        order_a = self._order(game_role_name="角色甲")
        order_a.source_order_no = "TB-合成-绩效-来源甲"
        order_a.save(update_fields=["source_order_no"])
        self._perf(order_a, user=self.taker_a)

        self.assertEqual(self._nos({"keyword": "这条谁都不匹配"}), [])
        # 说明性对照：同一份数据、不传 keyword ⇒ 它确实在列表里（空集不是「没数据」造成的）。
        self.assertEqual(self._nos(), [order_a.order_no])

    def test_filters_by_project_and_customer_ww(self):
        order_a, order_b = self._two(project_text="原神代练", customer_ww="ww-aaa")
        order_b.project_text = "王者陪玩"
        order_b.customer_ww = "ww-bbb"
        order_b.save(update_fields=["project_text", "customer_ww"])

        self._assert_only(
            {"all": [order_a, order_b], "hit": [order_a]}, {"project": "代练"}
        )
        self.assertEqual(self._nos({"project": "陪玩"}), [order_b.order_no])
        self.assertEqual(self._nos({"customer_ww": "aaa"}), [order_a.order_no])
        self.assertEqual(self._nos({"customer_ww": "bbb"}), [order_b.order_no])

    def test_empty_string_and_separator_only_values_are_not_provided(self):
        """前端「没选」的形状：空串 / 只有分隔符 —— **不是**非法值，也不该筛出空集。"""
        order_a, order_b = self._two()
        both = sorted([order_a.order_no, order_b.order_no])
        for params in (
            {"game": "", "order_status": "", "order_type": "", "order_source": ""},
            {"customer": "", "customer_type": "", "username": ""},
            {"order_no": "", "keyword": "", "project": "", "customer_ww": ""},
            {"customer": ",,", "username": ", ,"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self._nos(params), both)

    def test_malformed_new_filters_are_business_codes_not_500(self):
        """非法值 ⇒ **200 + `code 1000`**（不是 HTML 500，E-0010）。"""
        self._two()
        cases = [
            {"game": "abc"},
            {"game": "0"},                       # 序列化器 min_value=1
            {"order_status": "BOGUS"},
            {"order_status": "COMPLETED,BOGUS"},  # ⭐ 2026-10-29 多值：串里任一非法 token 都拒
            {"order_status": "COMPLETED,,"},      # ⭐ 多值：空 token（拼错的形状）也拒
            {"order_type": "-1"},                # 对标的「全部订单」由**前端不传**处理
            {"order_type": "BOGUS"},
            {"order_source": "abc"},
            {"order_source": "0"},
            {"customer_type": "5"},
            {"order_no": "x" * 21},              # 列宽 20
            {"keyword": "x" * 129},              # `game_role_name` 列宽 128
            {"project": "x" * 901},              # `project_text` 列宽 900（D-0218）
            {"customer_ww": "x" * 65},           # 列宽 64
        ]
        for params in cases:
            with self.subTest(params=params):
                body = self._body(params)
                self.assertEqual(body["code"], 1000, params)
                self.assertIsNone(body["data"])

    def test_project_filter_boundary_is_900(self):
        """`project` 筛选值的边界 = **900**（与 `Order.project_text` 列宽对齐，D-0218；D-0140 起 300）。

        ⚠️ `project`（`PerformanceListSearchSerializer`）是**手写字面量、不从模型派生**
        ⇒ 模型列宽改了它**不会**跟着涨。所以这里要**两条腿**一起钉：
        恰好 **900 通过**（筛不到任何行为空集，但**不是** 1000）＋ **901 回 1000**。
        只钉 901 那一侧抓不到上限被**改小**（改回 300，901 > 300 照样被拒——变异实测，见报告）；
        上限被**改大**那一侧是抓得到的：一抬高，901 就从「被拒」变「通过」、`== 1000` 当场红。
        """
        self._two()
        # 900 字：合法值 ⇒ code 0、空集（不是「被拒」）
        self.assertEqual(self._nos({"project": "x" * 900}), [])
        # 901 字：超长 ⇒ 业务码 1000（不是 HTML 500，E-0010）
        body = self._body({"project": "x" * 901})
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])

    def test_too_many_usernames_is_rejected_not_truncated(self):
        """逗号串**超个数**（>50）⇒ `1000`，⛔ 不静默截断成前 50 个。"""
        self._two()
        too_many = ",".join(f"u{i}" for i in range(51))
        body = self._body({"username": too_many})
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("50", body["msg"])

        boundary = ",".join(f"u{i}" for i in range(50))
        self.assertEqual(self._body({"username": boundary})["code"], 0)

    def test_comma_string_total_length_is_capped(self):
        """逗号串的**整串长度**上限 = 1500：恰好 1500 通过、1501 回 `1000`。

        ⚠️ 这条是**评审后修复轮**补的（Finding Minor-1）：长度判据原本被写成了
        `CharField(max_length=...)`，而 DRF 是「先 `to_internal_value`（本字段返回 **list**）、
        后 `run_validators`」⇒ `MaxLengthValidator` 数的是**列表元素个数**，
        原始字符串长度**根本没被检查**（实测 1600 个 `x` 仍回 `code 0`）。
        判据现在写在 `to_internal_value` 里对 `raw` 字符串判；去掉它这条用例必红。
        """
        self._two()
        # 恰好等于上限 ⇒ 通过（单个 1500 字符的「用户名」，切分后只有 1 项，不会触发 too_many）。
        self.assertEqual(self._body({"username": "x" * 1500})["code"], 0)

        # 上限 +1 ⇒ `1000`，且 msg 里能看出是**长度**问题（不是「最多 50 个」）。
        body = self._body({"username": "x" * 1501})
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])
        self.assertIn("1500", body["msg"])
        self.assertNotIn("50 个值", body["msg"])


# ── B（导出侧）：新参数**自动**对导出生效（同源）──────────────────────


class ExportSharesTheNewFiltersTests(_PerfReadsFixture):
    def test_export_and_list_agree_on_the_new_order_status_filter(self):
        """导出与列表**同源**：新加的 `order_status` 在两边命中同一批订单号。

        ⚠️ 同源的既有判据（`test_p4fu_performance_writes.py`）用的是老参数 `user`；
        本线**扩了参数集**，所以要拿**新参数**再钉一次 —— 否则「导出只认老六条」
        这种漂移不会红（`search_params` 换成新序列化器这件事本身没人守）。
        """
        completed = self._order(status=OrderStatus.COMPLETED)
        running = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(completed, user=self.taker_a)
        self._perf(running, user=self.taker_b)

        params = {"order_status": "COMPLETED"}
        workbook = workbook_of(self.client.get(EXPORT_URL, params))
        cells = values_of(workbook, "绩效明细")
        self.assertIn(completed.order_no, cells)                # 非空哨兵
        self.assertNotIn(running.order_no, cells)

        body = self._body(params)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]], [completed.order_no]
        )

    def test_export_and_list_agree_on_the_order_status_multiselect(self):
        """⭐ 2026-10-29 实装多选：`?order_status=码1,码2` 在两边命中**同一批**订单号
        （导出与列表共用 `performance_queryset`，判据钉住「多值的并集语义两边一致」）。

        夹具三张单三个状态：两个码各命中一张、第三张（不命中）留在两边之外——
        防「两边恰好都空 / 都全量」的假绿。
        """
        completed = self._order(status=OrderStatus.COMPLETED)
        unassigned = self._order(status=OrderStatus.UNASSIGNED)
        running = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(completed, user=self.taker_a)
        self._perf(unassigned, user=self.taker_b)
        self._perf(running, user=self.taker_a)

        params = {"order_status": "COMPLETED,UNASSIGNED"}
        workbook = workbook_of(self.client.get(EXPORT_URL, params))
        cells = values_of(workbook, "绩效明细")
        self.assertIn(completed.order_no, cells)                # 非空哨兵：码 1 命中
        self.assertIn(unassigned.order_no, cells)               # 非空哨兵：码 2 命中
        self.assertNotIn(running.order_no, cells)               # 没选的档不出现

        body = self._body(params)
        self.assertEqual(
            sorted(row["order_no"] for row in body["data"]["items"]),
            sorted([completed.order_no, unassigned.order_no]),
        )

    def test_export_and_list_agree_on_the_new_source_group_filter(self):
        """⭐ 导出与列表**同源**（计划 §4 #5）：`?source_group=` 在两边命中同一批订单号。

        判别力：把 `performance_queryset` 里那一行删掉 ⇒ 两边同时变（这条红）；
        只在导出侧另拼一份才可能漏 —— 而本线刻意没有第二份。
        """
        group = OrderSourceGroup.objects.create(name="导出分组甲")
        self.source_a.group = group
        self.source_a.save(update_fields=["group"])
        in_group = self._order(order_source=self.source_a)
        out_group = self._order(order_source=self.source_b)
        self._perf(in_group, user=self.taker_a)
        self._perf(out_group, user=self.taker_b)

        params = {"source_group": group.id}
        workbook = workbook_of(self.client.get(EXPORT_URL, params))
        cells = values_of(workbook, "绩效明细")
        self.assertIn(in_group.order_no, cells)          # 非空哨兵
        self.assertNotIn(out_group.order_no, cells)

        body = self._body(params)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]], [in_group.order_no]
        )

    def test_export_carries_the_order_status_column_as_a_chinese_label(self):
        """导出的 `订单状态` 列在，且值是**中文 label**（附件如此），不是枚举名。

        导出是 29 列 xlsx。没有叫「订单号」的列，订单号在「系统内置单号」。
        `订单状态` 在第 19 列，值是中文 label。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        self._perf(order, user=self.taker_a)

        rows = detail_rows(workbook_of(self.client.get(EXPORT_URL)))

        self.assertEqual(rows[0][18], "订单状态")        # 第 19 列（0 基 18）
        self.assertEqual(rows[1][18], "已完成")          # 中文 label，不是 "COMPLETED"
        self.assertEqual(rows[1][9], order.order_no)     # 第 10 列 = 系统内置单号


# ── B4：「打手范围」三态（计划 docs/plans/2026-11-07-settle-forbidden-filter.md §3）──


class SettleForbiddenScopeFilterTests(_PerfReadsFixture):
    """`?settle_forbidden_scope=`（报酬页「排除禁止结算的」哨兵：只认 `excluded`）。

    **三人组造数**（评审 M-4，防 INNER JOIN 回归）：A 有档案未禁、B 有档案被禁、
    C **无档案行**（夹具自证）。判别力：把「排除」写成
    `filter(user__staff_profile__settle_forbidden=False)` ⇒ C 被 JOIN 整批丢掉（红）；
    判据唯一出处 `settle_forbidden_user_ids`（D-0109）。
    """

    def _seed_three_players(self):
        """A（有档案、未禁）= `taker_a`；B（有档案、被禁）= `taker_b`；C（无档案行）新造。"""
        StaffProfile.objects.create(user=self.taker_a, settle_forbidden=False)
        StaffProfile.objects.create(user=self.taker_b, settle_forbidden=True)
        self.no_profile = User.objects.create_user(username="sff-no-profile")
        # 夹具自证（承重，不是用例）：C 真的没有档案行——它就是「排除」档的判别力来源。
        self.assertFalse(StaffProfile.objects.filter(user=self.no_profile).exists())
        self.perf_a = self._perf(self._order(), user=self.taker_a)
        self.perf_b = self._perf(self._order(), user=self.taker_b)
        self.perf_c = self._perf(self._order(), user=self.no_profile)

    def test_without_param_all_three_players_still_show(self):
        """默认（不带参）回归零变化：三人组的行全在（含被禁与无档案行的）。"""
        self._seed_three_players()
        self.assertEqual(
            self._nos(),
            sorted(o.order_no for o in [self.perf_a.order, self.perf_b.order, self.perf_c.order]),
        )

    def test_excluded_keeps_profileless_and_allowed_drops_frozen(self):
        """`excluded`：被禁的消失；**无档案行的留在结果里**（INNER JOIN 写法这里必红）。"""
        self._seed_three_players()
        rows = self._nos({"settle_forbidden_scope": "excluded"})
        self.assertIn(self.perf_a.order.order_no, rows)
        self.assertIn(self.perf_c.order.order_no, rows)
        self.assertNotIn(self.perf_b.order.order_no, rows)

    def test_invalid_scope_is_1000(self):
        """`only`/`all`（撤档的旧值）与表外值 ⇒ 一律业务码 1000（信封形状，不是 500）。"""
        self._seed_three_players()
        for bad in ("only", "all", "forbidden"):
            body = self._body({"settle_forbidden_scope": bad})
            self.assertEqual(body["code"], 1000, body)
            self.assertIsNone(body["data"])

    def test_summary_follows_the_excluded_scope(self):
        """统计句跟随筛选（summary = 同一 queryset 的聚合，total 同源变小）。"""
        self._seed_three_players()
        full = self._body()["data"]
        self.assertEqual(full["total"], 3)
        filtered = self._body({"settle_forbidden_scope": "excluded"})["data"]
        self.assertEqual(filtered["total"], 2)
        # temp_amount 三行各 20.00 ⇒ 全量 60.00、排除后 40.00（行集真的变了，不是只变计数）。
        self.assertEqual(full["summary"]["temp_amount"], "60.00")
        self.assertEqual(filtered["summary"]["temp_amount"], "40.00")

    def test_export_and_list_agree_on_the_excluded_scope(self):
        """导出与列表**同源**（照 ExportSharesTheNewFiltersTests 先例）：`excluded` 两边命中同一批。"""
        self._seed_three_players()
        params = {"settle_forbidden_scope": "excluded"}
        workbook = workbook_of(self.client.get(EXPORT_URL, params))
        cells = values_of(workbook, "绩效明细")
        self.assertIn(self.perf_a.order.order_no, cells)      # 非空哨兵
        self.assertIn(self.perf_c.order.order_no, cells)      # 无档案行的也进导出（同源）
        self.assertNotIn(self.perf_b.order.order_no, cells)

        body = self._body(params)
        self.assertEqual(
            sorted(row["order_no"] for row in body["data"]["items"]),
            sorted([self.perf_a.order.order_no, self.perf_c.order.order_no]),
        )


# ── B3：收款状态筛选（⭐ D-0368 改准：判据 = `order.payment_status`；原 D-0365 口径
#    接 `received_price` 生产全库零条有值 ⇒ 结构性恒空，业务方报障指认语义）──


class NetReceiptsStateFilterTests(_PerfReadsFixture):
    """`?net_receipts_state=`（报酬页「收款状态」筛选，D-0368 改准）。

    判据 = 业务方指认的语义：「已收款就是订单的收款状态（同步上家的那个）」
    ⇒ **`order.payment_status`**：`received` ⇒ `SETTLED`（已结款）；
    `unreceived` ⇒ **非 `SETTLED` 的补集**（未收款 / 结算中都算「还没收到」）。
    ⚠️ 三张单把档位铺满（SETTLED / UNSETTLED / SETTLING 各一），
    防「两态筛选把中间档静默丢掉」的假绿。
    """

    def _three_orders(self):
        settled = self._order(order_no="20261102000000000011")
        Order.objects.filter(pk=settled.pk).update(
            payment_status=PaymentStatus.SETTLED,
        )
        unsettled = self._order(order_no="20261102000000000012")  # 默认 UNSETTLED
        settling = self._order(order_no="20261102000000000013")
        Order.objects.filter(pk=settling.pk).update(
            payment_status=PaymentStatus.SETTLING,
        )
        for order in (settled, unsettled, settling):
            self._perf(order, user=self.taker_a)
        return settled, unsettled, settling

    def test_received_hits_settled_only(self):
        settled, unsettled, settling = self._three_orders()

        nos = self._nos({"net_receipts_state": "received"})

        # 「已收款」= 收款状态**已结款**那一档（上家同步 / 闲管家自动已结款的字段）。
        self.assertEqual(nos, [settled.order_no])

    def test_unreceived_hits_everything_not_settled(self):
        settled, unsettled, settling = self._three_orders()

        nos = self._nos({"net_receipts_state": "unreceived"})

        # 「未收款」= 非 SETTLED 的**补集**（未收款 / 结算中都算「还没收到」）。
        self.assertEqual(
            sorted(nos), sorted([unsettled.order_no, settling.order_no])
        )

    def test_not_sending_the_param_keeps_every_row(self):
        settled, unsettled, settling = self._three_orders()

        nos = self._nos()

        self.assertEqual(len(nos), 3)

    def test_an_illegal_value_is_a_business_code_not_500(self):
        body = self._body({"net_receipts_state": "BOGUS"})
        self.assertEqual(body["code"], 1000, body)

    def test_export_and_list_agree_on_the_receipt_state(self):
        """导出与列表**同源**：`net_receipts_state` 在两边命中同一批订单号
        （`search_params` + `performance_queryset` 共用 ⇒ 导出自动跟着走，判据钉住它）。"""
        settled, unsettled, settling = self._three_orders()

        params = {"net_receipts_state": "received"}
        workbook = workbook_of(self.client.get(EXPORT_URL, params))
        cells = values_of(workbook, "绩效明细")
        self.assertIn(settled.order_no, cells)                  # 非空哨兵
        self.assertNotIn(unsettled.order_no, cells)
        self.assertNotIn(settling.order_no, cells)

        body = self._body(params)
        self.assertEqual(
            sorted(row["order_no"] for row in body["data"]["items"]),
            [settled.order_no],
        )


# ── C：检测重复收款支付宝（只读端点）──────────────────────────────────


class AlipayDuplicatesTests(_PerfReadsFixture):
    def _staff(self, username, *, alipay="", groups=(), is_active=True):
        user = User.objects.create_user(username=username, is_active=is_active)
        for name in groups:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)
        # ⚠️ `StaffProfile` 不是登录时自动建的（`accounts/views.py` 用 `get_or_create`）；
        #    这里显式建，别指望 `user.staff_profile` 一定存在。
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.alipay_account = alipay
        profile.save(update_fields=["alipay_account"])
        return user

    def _duplicates(self, *, client=None):
        response = (client or self.client).get(ALIPAY_URL)
        assert response.status_code == 200, response.content[:300]
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]["duplicates"]

    def test_two_users_sharing_an_account_are_reported_once(self):
        self._staff("dup-a", alipay="shared@example.com", groups=("客服",))
        self._staff(
            "dup-b", alipay="shared@example.com", groups=("客服主管", "客服")
        )

        duplicates = self._duplicates()
        self.assertEqual(len(duplicates), 1)
        row = duplicates[0]
        self.assertEqual(row["alipay_account"], "shared@example.com")
        self.assertEqual(row["count"], 2)
        self.assertEqual(
            [user["username"] for user in row["users"]], ["dup-a", "dup-b"]
        )
        # 职位 = Group 名，多个按名称升序用「、」连接。
        self.assertEqual(row["users"][0]["job"], "客服")
        self.assertEqual(row["users"][1]["job"], "客服、客服主管")
        self.assertIs(row["users"][0]["is_active"], True)

    def test_display_name_is_the_nickname_and_username_is_kept(self):
        """⭐ 2026-10-04：「显示人统一成昵称」线——每个 user 行**追加** `display_name`
        （昵称优先、空串回落登录账号），而老键 `username` **原样保留**（它是「账号」这个事实）。

        **判别力**：第一个账号的昵称与登录账号**不同形** ⇒「把 `username` 抄进
        `display_name`」的实现会让本条红；第二个账号空昵称 ⇒ 同时钉住回落那一半。
        """
        first = self._staff("dup-a", alipay="shared@example.com")
        first.first_name = "测试昵称甲"
        first.save(update_fields=["first_name"])
        self._staff("dup-b", alipay="shared@example.com")

        users = self._duplicates()[0]["users"]
        self.assertEqual([user["username"] for user in users], ["dup-a", "dup-b"])
        self.assertEqual(
            [user["display_name"] for user in users], ["测试昵称甲", "dup-b"]
        )

    def test_unique_account_is_not_reported(self):
        self._staff("solo-a", alipay="only-me@example.com")

        self.assertEqual(self._duplicates(), [])

    def test_blank_accounts_are_not_grouped(self):
        """空串不是「一个收款账户」：两个都没填的人**不构成重复**。"""
        self._staff("blank-a", alipay="")
        self._staff("blank-b", alipay="")
        self._staff("blank-c", alipay="real@example.com")

        self.assertEqual(self._duplicates(), [])

    def test_inactive_and_group_less_users_are_still_reported(self):
        """「当前状态」列要能看出停用；没有职位的人 `job` 是空串（不编造）。"""
        self._staff("mix-a", alipay="mix@example.com", is_active=False)
        self._staff("mix-b", alipay="mix@example.com")

        row = self._duplicates()[0]
        self.assertIs(row["users"][0]["is_active"], False)
        self.assertEqual(row["users"][0]["job"], "")
        self.assertIs(row["users"][1]["is_active"], True)

    def test_needs_the_finance_payroll_node(self):
        """无节点 ⇒ 403（msg 可辨）；有节点 ⇒ 200。

        ⚠️ 两侧用**互不相同的职位组**（`Role.name` 唯一 ⇒ 同组会让后一个人的 nodes
        加在前一个人身上，拒绝侧继承授予、403 断不出来）——组名 ≤5 字是 `Role.name`
        的列宽约束；本端点**不按职位放行**，所以随意命名不影响业务语义。
        """
        self._staff("perm-a", alipay="perm@example.com")
        self._staff("perm-b", alipay="perm@example.com")
        allowed = make_logged_in_client(
            username="perm-alipay-ok", groups=("PA1",), nodes=(FINANCE_NODE,)
        )
        denied = make_logged_in_client(username="perm-alipay-no", groups=("PN1",))

        rejected = denied.get(ALIPAY_URL)
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertIn("重复收款支付宝", rejected.json()["msg"])

        ok = allowed.get(ALIPAY_URL)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0)
        self.assertEqual(ok.json()["data"]["duplicates"][0]["count"], 2)

    def test_anonymous_is_401(self):
        response = Client(SERVER_NAME="tenant.test.com").get(ALIPAY_URL)
        self.assertEqual(response.status_code, 401)

    def test_read_only(self):
        """只读：请求前后 `StaffProfile` 一行都不变（快照逐字段比）。"""
        self._staff("ro-a", alipay="ro@example.com")
        self._staff("ro-b", alipay="ro@example.com")
        before = list(
            User.objects.order_by("id").values_list("username", "is_active")
        )
        self._duplicates()
        after = list(User.objects.order_by("id").values_list("username", "is_active"))
        self.assertEqual(before, after)


# ── C：跨租户隔离（判据用租户内取值不同的 `alipay_account`，不是主键）────

DOMAIN_B = "p4fu-c-iso.test.local"
SCHEMA_B = "p4fu_c_iso"
ACCOUNT_A = "dup-account-a@example.com"
ACCOUNT_B = "dup-account-b@example.com"


class AlipayDuplicatesIsolationTests(_PerfReadsFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="P4FU-C 隔离租户 B", domain=DOMAIN_B
        )

    def _staff(self, username, alipay, *, active=True):
        user = User.objects.create_user(username=username, is_active=active)
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.alipay_account = alipay
        profile.save(update_fields=["alipay_account"])
        return user

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        # A 侧：一个重复账户。
        self._staff("iso-a1", ACCOUNT_A)
        self._staff("iso-a2", ACCOUNT_A)

        # B 侧：另一个**取值不同**的重复账户（判据靠这个字符串，不靠主键）。
        try:
            with tenant_context(self.tenant_b):
                self._staff("iso-b1", ACCOUNT_B)
                self._staff("iso-b2", ACCOUNT_B)
                self.admin_b = make_logged_in_client(
                    domain=DOMAIN_B, username="iso-admin-b", is_superuser=True
                )
        finally:
            connection.set_tenant(self.tenant)

    def test_a_tenant_only_sees_its_own_duplicate_accounts(self):
        accounts = [row["alipay_account"] for row in self._duplicates_a()]
        self.assertEqual(accounts, [ACCOUNT_A])       # 非空哨兵
        self.assertNotIn(ACCOUNT_B, accounts)

    def test_recorder_names_are_resolved_within_each_tenant(self):
        User.objects.create_user(username="same_recorder", first_name="甲租户记录员")
        self._perf(self._order(created_by="same_recorder"), user=self.taker_a)
        try:
            with tenant_context(self.tenant_b):
                User.objects.create_user(username="same_recorder", first_name="乙租户记录员")
                worker = User.objects.create_user(username="b_worker")
                game = Game.objects.create(name="乙租户游戏")
                order = Order.objects.create(game=game, order_no="20260922000000000002", created_by="same_recorder")
                Performance.objects.create(order=order, user=worker, emp_type=EmpType.MAIN)
            b_response = self.admin_b.get(LIST_URL, SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)
        a_response = self._body()
        self.assertEqual([r["log_user_display"] for r in a_response["data"]["items"]], ["甲租户记录员"])
        self.assertEqual([r["log_user_display"] for r in b_response["data"]["items"]], ["乙租户记录员"])
        denied = self.client.get(LIST_URL, SERVER_NAME=DOMAIN_B).json()
        self.assertNotEqual(denied["code"], 0)

    def test_b_tenant_only_sees_its_own_duplicate_accounts(self):
        """正对照：同一条代码在 B 的 host 上返回 B 的账户（排除「永远只回 A」的假绿）。"""
        try:
            response = self.admin_b.get(ALIPAY_URL, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 200, response.content[:200])
        accounts = [row["alipay_account"] for row in response.json()["data"]["duplicates"]]
        self.assertEqual(accounts, [ACCOUNT_B])
        self.assertNotIn(ACCOUNT_A, accounts)

    def test_a_session_on_b_host_is_rejected(self):
        """凭据平面：A 的会话拿到 B 的域名上 ⇒ 401（D-0009 的会话强绑身份平面）。"""
        try:
            response = self.client.get(ALIPAY_URL, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 401, response.content[:200])

    def _duplicates_a(self):
        response = self.client.get(ALIPAY_URL)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["duplicates"]
