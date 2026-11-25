"""P4 的跨租户隔离回归（spec §9 #13；`AGENTS.md` §4 的「以租户 A 的凭据访问 B 的数据必须失败」）。

P4 新增了两张表（`OrderRefund` / `PerformanceAdjustment`）与五个读写端点
（结算列表 / 待归属 / 退款记录 / 调整记录链 GET / 人工修正 POST），
P4-8 与 P4-9 的评审都按计划把这条红线留给了本任务。**本文件就是它的落点。**

**本文件证什么 / 不证什么**（照 `tests/test_p3b_isolation.py` 分三类，别混着读）：

- **数据层**（`RefundAndAdjustmentDataIsolationTests`）：A 写的退款行 / 调整行在 B 的 schema
  里不可见，且 B 自己写的行可见。两个方向都断。
- **HTTP 层**（`CrossTenantHttpIsolationTests`）：每个租户一个**真实登录**的超级管理员 client +
  各自的域名，逐个读路径断言另一租户的业务值不出现；并单钉 **P4-11 的新 POST**
  （`POST /performances/{id}/adjustments`）跨租户**不可达**。
- **凭据层**（最后一条）：A 的会话拿到 B 的域名上 ⇒ **401**；同一 client 回自己的域名 ⇒ 200。

⚠️ **判据纪律（Global Constraints #8/#9）**：

1. ⭐ **不用主键指认「另一个租户的那一行」**：两个 schema 的主键序列**各自从起点开始**
   ⇒ 判据用**租户内唯一、且两租户取值不同**的业务值：订单号（`NO_A` / `NO_B`）与
   `reason` 标记（`MARKER_A` / `MARKER_B`）。唯一的例外是 `POST .../adjustments` 的路径本身
   要传 `performance_id`——那里**先断言那个 id 在 B 里不存在**（否则 B 会命中的是它自己的行），
   并额外断言 B 里**没有**那个 `request_id` 的行。
2. **非空哨兵**：每条否定断言前先证明「B 里能看到 B 自己的那一行」，否则空列表能让
   `assertNotIn` 假通过。**不用 `count()`** 当可见性判据。
3. **每条测试都能单独运行**：`setUp` 第一行显式 `connection.set_tenant(self.tenant)`，
   挡住上一条用例留在 B 上的 schema 残留（跨域名请求**不会**在响应结束时还原，见
   `tests/test_account_isolation.py` 的模块说明）。
4. 期望值写**字面值**；断言打**响应体 / 数据库行的值**（E-0009 / E-0024）。

⚠️ **本文件抓不到什么**：它只对**走当前 schema 下 ORM** 的查询路径有判别力。
一条手工拼 SQL、或把 schema 固定在别处的查询路径不会被这里抓到。
"""
from decimal import Decimal

import json

from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

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
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 租户 A = `TenantTestCase` 的 `test` schema（域名与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
DOMAIN_B_DATA = "p4-iso-data-b.test.local"
DOMAIN_B_HTTP = "p4-iso-http-b.test.local"
SCHEMA_B_DATA = "p4_iso_data_b"
SCHEMA_B_HTTP = "p4_iso_http_b"

# ── 判据用的业务值（租户内唯一、且两租户取值不同；**不是主键**）────────────
MARKER_A = "p4-only-in-a"
MARKER_B = "p4-only-in-b"
#: 20 位订单号（与 `order_no` 列宽一致）：A / B 各两条（一条可退款、一条待归属）。
NO_A = "20260920000000009301"
NO_B = "20260920000000009302"
UNATTR_A = "20260920000000009311"
UNATTR_B = "20260920000000009312"
USER_A = "p4-iso-user-a"
USER_B = "p4-iso-user-b"
REQUEST_A = "p4-iso-adj-req-a"
REQUEST_B = "p4-iso-adj-req-b"
#: 绩效列表的跨租户判据用**代练员**（持 `my_performance.export` ⇒ 能进列表，
#: 不持 `order.list` ⇒ 只看自己的行）。用户名租户内唯一、两租户取值不同（判据纪律 1）。
PERF_TAKER_A = "p4-iso-perf-taker-a"
PERF_TAKER_B = "p4-iso-perf-taker-b"
PERF_NO_A = "20260920000000009303"
PERF_NO_B = "20260920000000009304"


def _make_order(order_no, status=OrderStatus.COMPLETED):
    """在当前 schema 里造「游戏 + 订单」（订单号是判据来源之一）。"""
    game = Game.objects.create(name=f"p4-iso-game-{order_no}")
    return Order.objects.create(
        game=game,
        order_no=order_no,
        status=status,
        received_price=Decimal("100.00"),
    )


def _make_sales_row(order, username):
    """在当前 schema 里给订单造一条客服行（`emp_type=销售提成`）。"""
    user = User.objects.create_user(username=username)
    return Performance.objects.create(
        order=order,
        emp_type=EmpType.SALES,
        user=user,
        temp_amount=Decimal("40.00"),
        bonus_amount=Decimal("8.00"),
        commission_rate=Decimal("20.00"),
        review_status=ReviewStatus.APPROVED,
        settle_status=SettleStatus.UNSETTLED,
    )


def _make_main_row(order, user):
    """在当前 schema 里给订单造一条**代练员**行（`emp_type=打手`，归属人 = `user`）。

    绩效列表的 `username` 列取 `user.username` ⇒ 这就是跨租户判据的来源。
    ⚠️ `user` 由调用方给（真实登录夹具已经建过同名用户）——本函数**不**再建用户，
    否则会撞 `auth_user_username_key`。
    """
    return Performance.objects.create(
        order=order,
        emp_type=EmpType.MAIN,
        user=user,
        temp_amount=Decimal("50.00"),
    )


class RefundAndAdjustmentDataIsolationTests(TenantTestCase):
    """数据层：`OrderRefund` 与 `PerformanceAdjustment` 不跨 schema。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建：在一个用例内部建 schema 会与外层事务相冲
    （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B_DATA, name="P4 隔离租户 B（数据层）", domain=DOMAIN_B_DATA
        )

    def _make_refund_and_adjustment(self, order_no, marker):
        """在当前 schema 里造「订单 + 客服行 + 退款行 + 调整行」。

        `reason` 是 **租户内唯一、两租户取值不同** 的判据来源（订单号同理）。
        """
        order = _make_order(order_no)
        perf = _make_sales_row(order, f"iso-{order_no}")
        refund = OrderRefund.objects.create(
            order=order,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"),
            reason=marker,
            operator=marker,
            request_id=f"refund-{marker}",
        )
        PerformanceAdjustment.objects.create(
            performance=perf,
            refund=refund,
            kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
            amount=Decimal("-2.00"),
            reason=marker,
            operator=marker,
            settle_status=SettleStatus.UNSETTLED,
        )
        return refund

    def _refund_markers(self):
        return list(OrderRefund.objects.values_list("reason", flat=True))

    def _adjustment_markers(self):
        return list(PerformanceAdjustment.objects.values_list("reason", flat=True))

    def test_b_schema_does_not_see_a_rows(self):
        """B 看不到 A 的退款行 / 调整行；同时 B 自己的那两行可见（非空哨兵）。"""
        with tenant_context(self.tenant):
            self._make_refund_and_adjustment(NO_A, MARKER_A)

        with tenant_context(self.tenant_b):
            self._make_refund_and_adjustment(NO_B, MARKER_B)
            # ── 非空哨兵：先证明 B 能看到自己的行
            self.assertIn(MARKER_B, self._refund_markers())
            self.assertIn(MARKER_B, self._adjustment_markers())
            self.assertTrue(OrderRefund.objects.filter(order__order_no=NO_B).exists())
            # ── 否定断言：A 的行按**标记**与 **A 的订单号** 判（两租户取值不同，不用主键）
            self.assertNotIn(MARKER_A, self._refund_markers())
            self.assertNotIn(MARKER_A, self._adjustment_markers())
            self.assertFalse(OrderRefund.objects.filter(order__order_no=NO_A).exists())
            self.assertFalse(
                PerformanceAdjustment.objects.filter(performance__order__order_no=NO_A).exists()
            )
            self.assertTrue(
                PerformanceAdjustment.objects.filter(performance__order__order_no=NO_B).exists()
            )

        with tenant_context(self.tenant):
            self.assertIn(MARKER_A, self._refund_markers())
            self.assertIn(MARKER_A, self._adjustment_markers())

    def test_a_schema_does_not_see_b_rows(self):
        """反方向：A 也看不到 B 的退款行 / 调整行（只断一个方向会漏掉「读写在错误的 schema 上」）。"""
        with tenant_context(self.tenant_b):
            self._make_refund_and_adjustment(NO_B, MARKER_B)

        with tenant_context(self.tenant):
            self._make_refund_and_adjustment(NO_A, MARKER_A)
            self.assertIn(MARKER_A, self._refund_markers())
            self.assertIn(MARKER_A, self._adjustment_markers())
            self.assertNotIn(MARKER_B, self._refund_markers())
            self.assertNotIn(MARKER_B, self._adjustment_markers())
            self.assertFalse(OrderRefund.objects.filter(order__order_no=NO_B).exists())
            self.assertTrue(OrderRefund.objects.filter(order__order_no=NO_A).exists())


class CrossTenantHttpIsolationTests(TenantTestCase):
    """HTTP 层：每条 P4 读路径都不得出现另一租户的业务值；P4-11 的新 POST 跨租户不可达。"""

    URL_SETTLEMENTS = "/api/business/settlements"
    URL_UNATTRIBUTED = "/api/business/settlements/unattributed"
    URL_PEOPLE = "/api/business/settlements/people"
    URL_PERFORMANCES = "/api/business/performances/"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B_HTTP, name="P4 隔离租户 B（HTTP 层）", domain=DOMAIN_B_HTTP
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求不会在响应结束时还原 schema）。
        connection.set_tenant(self.tenant)

        # ── B 侧**先**建（两条 `.test.local` 都不与 A 撞；用 try/finally 还原 schema）──
        try:
            with tenant_context(self.tenant_b):
                self.admin_b = make_logged_in_client(
                    domain=DOMAIN_B_HTTP, username="p4-iso-admin-b", is_superuser=True
                )
                order_b = _make_order(NO_B)
                self.perf_b = _make_sales_row(order_b, USER_B)
                OrderRefund.objects.create(
                    order=order_b,
                    kind=OrderRefund.Kind.PARTIAL,
                    amount=Decimal("10.00"),
                    reason=MARKER_B,
                    operator=MARKER_B,
                    request_id=f"refund-{MARKER_B}",
                )
                PerformanceAdjustment.objects.create(
                    performance=self.perf_b,
                    kind=PerformanceAdjustment.Kind.MANUAL,
                    amount=Decimal("-1.00"),
                    reason=MARKER_B,
                    operator=MARKER_B,
                    settle_status=SettleStatus.UNSETTLED,
                    request_id=REQUEST_B,
                )
                _make_order(UNATTR_B)
        finally:
            connection.set_tenant(self.tenant)

        # ── A 侧：超级管理员（`has_node` 对他短路），真实登录 ──
        self.admin_a = make_logged_in_client(
            domain=DOMAIN_A, username="p4-iso-admin-a", is_superuser=True
        )
        order_a = _make_order(NO_A)
        # ⭐ **A 的绩效 id 必须大于 B 的最大绩效 id**：`POST .../adjustments` 的路径参数就是主键，
        #    而两个 schema 的主键序列**各自从起点开始** ⇒ 不推进的话 A 的行 id 可能正好等于
        #    B 自己的某行 id，那条「跨租户不可达」的用例就会打到 **B 自己的行**上（判据恒假）。
        #    所以这里循环造行直到 id 超过 B 的最大值——「A 的行在 B 里不存在」成为**构造保证**，
        #    不是巧合（判据纪律 1 的那个例外因此有前提可断）。
        attempt = 0
        while True:
            attempt += 1
            self.perf_a = _make_sales_row(order_a, f"{USER_A}-{attempt}")
            if self.perf_a.pk > self.perf_b.pk:
                break
        OrderRefund.objects.create(
            order=order_a,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"),
            reason=MARKER_A,
            operator=MARKER_A,
            request_id=f"refund-{MARKER_A}",
        )
        PerformanceAdjustment.objects.create(
            performance=self.perf_a,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-1.00"),
            reason=MARKER_A,
            operator=MARKER_A,
            settle_status=SettleStatus.UNSETTLED,
            request_id=REQUEST_A,
        )
        _make_order(UNATTR_A)                      # 已 COMPLETED、**没有**销售提成行 ⇒ 待归属

    # ── 工具 ───────────────────────────────────────────────────────
    def _get_b(self, url, params=None):
        """在 B 的域名上用 B 的凭据发请求，**结束后把连接钉回租户 A**。"""
        try:
            return self.admin_b.get(url, params or {}, SERVER_NAME=DOMAIN_B_HTTP)
        finally:
            connection.set_tenant(self.tenant)

    def _post_b(self, url, payload):
        try:
            response = self.admin_b.post(
                url, data=json.dumps(payload), content_type="application/json",
                SERVER_NAME=DOMAIN_B_HTTP,
            )
        finally:
            connection.set_tenant(self.tenant)
        return response

    def _items(self, response):
        """取信封里的 `items`；先断言端点本身成功（前置哨兵）。"""
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _numbers(self, response, field):
        return [row[field] for row in self._items(response)]

    # ── 逐读路径：另一租户的业务值不得出现 ──────────────────────────
    def test_settlement_list_does_not_leak_the_other_tenant(self):
        """`GET /settlements`：B 的列表只含 B 的订单号（非空哨兵 + 两租户取值不同的订单号）。"""
        a_nos = self._numbers(self.admin_a.get(self.URL_SETTLEMENTS), "order_no")
        self.assertIn(NO_A, a_nos)                 # 正向对照：A 看得到自己那单
        self.assertNotIn(NO_B, a_nos)

        b_nos = self._numbers(self._get_b(self.URL_SETTLEMENTS), "order_no")
        self.assertTrue(b_nos, "B 的结算列表为空——负断言被空列表凑出来了，测试自身失效")
        self.assertIn(NO_B, b_nos)                 # 非空哨兵
        self.assertNotIn(NO_A, b_nos)              # 判据：A 的订单号（不是主键）

    def test_unattributed_list_does_not_leak_the_other_tenant(self):
        """`GET /settlements/unattributed`：待归属清单同样按租户收窄。"""
        a_nos = self._numbers(self.admin_a.get(self.URL_UNATTRIBUTED), "order_no")
        self.assertIn(UNATTR_A, a_nos)
        self.assertNotIn(UNATTR_B, a_nos)

        b_nos = self._numbers(self._get_b(self.URL_UNATTRIBUTED), "order_no")
        self.assertTrue(b_nos, "B 的待归属清单为空——负断言被空列表凑出来了")
        self.assertIn(UNATTR_B, b_nos)
        self.assertNotIn(UNATTR_A, b_nos)

    def test_people_candidates_do_not_leak_the_other_tenant(self):
        """`GET /settlements/people`：候选人只含**本租户**的用户名（D-0089）。

        ⚠️ 判据是**用户名**（租户内唯一、且两租户取值不同——A 侧是 `f"{USER_A}-{attempt}"`、
        B 侧是 `USER_B`），⛔ **不是主键**：两个 schema 的主键序列各自从 1 起，
        `{"id": 1}` 在 A 与 B 里指的不是同一个人。
        A 侧的用户名因 setUp 的「推进 id」循环带后缀 ⇒ 否定判据用**前缀**（`startswith`），
        正向哨兵用 `all(...)`，避免「空列表凑出负断言」。
        """
        a_names = self._numbers(self.admin_a.get(self.URL_PEOPLE), "username")
        self.assertTrue(a_names, "A 的人员候选为空——负断言会被空列表凑出来，测试自身失效")
        self.assertTrue(
            all(name.startswith(USER_A) for name in a_names),
            f"A 的候选里混进了非 A 侧的用户名：{a_names}",
        )
        self.assertEqual(
            [name for name in a_names if name.startswith(USER_B)], [], "B 的用户名泄漏进 A"
        )

        b_names = self._numbers(self._get_b(self.URL_PEOPLE), "username")
        self.assertTrue(b_names, "B 的人员候选为空——负断言会被空列表凑出来")
        self.assertTrue(
            all(name.startswith(USER_B) for name in b_names),
            f"B 的候选里混进了非 B 侧的用户名：{b_names}",
        )
        self.assertEqual(
            [name for name in b_names if name.startswith(USER_A)], [], "A 的用户名泄漏进 B"
        )

    def test_refund_records_do_not_leak_the_other_tenant(self):
        """`GET /orders/{order_no}/refunds`：另一租户的订单号查不到**任何**退款行。

        ⚠️ 判据是订单号（两租户取值不同），不是退款行主键。
        """
        a_items = self._numbers(
            self.admin_a.get(f"/api/business/orders/{NO_A}/refunds"), "order_no"
        )
        self.assertEqual(a_items, [NO_A])          # A 自己那单恰有一条退款（正向对照）

        # B 的域名 + B 的凭据 查 A 的订单号 ⇒ 空（B 的 schema 里没有这张单）
        crossed = self._get_b(f"/api/business/orders/{NO_A}/refunds")
        self.assertEqual(crossed.json()["code"], 0, crossed.content[:200])
        self.assertEqual(self._numbers(crossed, "order_no"), [])

        # 非空哨兵：B 查**自己的**订单号能查到
        b_items = self._numbers(self._get_b(f"/api/business/orders/{NO_B}/refunds"), "order_no")
        self.assertEqual(b_items, [NO_B])

    def test_adjustment_chain_does_not_leak_the_other_tenant(self):
        """`GET /performances/{id}/adjustments`：调整记录链只含本租户的标记。"""
        a_reasons = self._numbers(
            self.admin_a.get(f"/api/business/performances/{self.perf_a.pk}/adjustments"),
            "reason",
        )
        self.assertIn(MARKER_A, a_reasons)         # 非空哨兵
        self.assertNotIn(MARKER_B, a_reasons)

        b_reasons = self._numbers(
            self._get_b(f"/api/business/performances/{self.perf_b.pk}/adjustments"), "reason"
        )
        self.assertIn(MARKER_B, b_reasons)         # 非空哨兵
        self.assertNotIn(MARKER_A, b_reasons)

    def _performance_usernames(self, client):
        """取本租户绩效列表里的 `username` 列（信封成功是前置哨兵）。"""
        body = client.get(self.URL_PERFORMANCES).json()
        self.assertEqual(body["code"], 0, body)
        return [row["username"] for row in body["data"]["items"]]

    def test_performance_list_does_not_leak_across_tenants(self):
        """以租户 A 的代练员凭据调绩效列表 ⇒ 看不到租户 B 的行。

        ⚠️ 判据用**租户内唯一、且两租户取值不同**的字段（绩效归属人的 `username`），
        **不用**主键指认「另一个租户的那一行」（两个 schema 的主键各自从 1 开始）。
        ⚠️ 本线的数据范围把非管理侧收窄到「只看自己」⇒ 这条读路径同时钉两件事：
        跨租户不泄漏，且 A 的列表里也不会混进 A 自己其它人的行。
        """
        # ── B 侧：代练员（持 `my_performance.export`）+ 一条属于他的 MAIN 绩效 ──
        try:
            with tenant_context(self.tenant_b):
                taker_b = make_logged_in_client(
                    domain=DOMAIN_B_HTTP, username=PERF_TAKER_B,
                    groups=("代练员",), nodes=("my_performance.export",),
                )
                order_b = _make_order(PERF_NO_B)
                _make_main_row(order_b, User.objects.get(username=PERF_TAKER_B))
                # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。绩效列表按
                # **请求方**的 `StaffProfile.games` 收窄 ⇒ 不补这张单的游戏，下面那条
                # 非空哨兵会先撞上空集，跨租户判据根本没被执行。期望值一个字没改。
                grant_games(User.objects.get(username=PERF_TAKER_B), order_b.game)
                b_names = self._performance_usernames(taker_b)
                self.assertIn(PERF_TAKER_B, b_names)          # 非空哨兵
                self.assertNotIn(PERF_TAKER_A, b_names)       # 判据：A 的用户名，不是主键
        finally:
            connection.set_tenant(self.tenant)

        # ── A 侧：代练员 + 一条属于自己的 MAIN 绩效 ──
        taker_a = make_logged_in_client(
            domain=DOMAIN_A, username=PERF_TAKER_A,
            groups=("代练员",), nodes=("my_performance.export",),
        )
        order_a = _make_order(PERF_NO_A)
        _make_main_row(order_a, User.objects.get(username=PERF_TAKER_A))
        # 同 B 侧：请求方必须显式持有这张单的游戏（**夹具补齐**，不是放宽判据）。
        grant_games(User.objects.get(username=PERF_TAKER_A), order_a.game)
        a_names = self._performance_usernames(taker_a)
        self.assertIn(PERF_TAKER_A, a_names)                  # 非空哨兵
        self.assertNotIn(PERF_TAKER_B, a_names)
        # ⭐ 数据范围（spec §6 第 11 行）：非管理侧只看自己 ⇒ A 的客服行归属人不出现
        self.assertEqual(set(a_names), {PERF_TAKER_A})

    def test_the_manual_adjustment_post_cannot_reach_another_tenants_performance(self):
        """⭐ P4-11 的**新 POST** 跨租户不可达（计划清单 8 的追加义务）。

        ⚠️ **先证明那个 `performance_id` 在 B 里不存在**：`POST` 的路径参数就是主键，
        若 A 的行 id 恰好在 B 里也存在，B 命中的会是**它自己**的行（两个 schema 的主键序列
        各自从起点开始）。`setUp` 已把 A 的目标绩效 id 推进到**大于 B 的最大绩效 id**，
        这里把那条构造保证**显式断出来**——它失效时本用例必须响亮地红，而不是静默打错行。
        控制项：**同一个** client 打自己的绩效 id 必须成功（否则上面的 `1000` 可能由
        「这个 client 根本写不了」凑出来）。
        """
        from apps.business.models import Performance

        # ⚠️ 判据必须在 **B 的 schema** 里问（`_get_b` / `_post_b` 会把连接钉回 A，
        #    在 A 里问 `filter(pk=perf_a.pk).exists()` 恒为 True ⇒ 断言恒假）。
        with tenant_context(self.tenant_b):
            self.assertFalse(
                Performance.objects.filter(pk=self.perf_a.pk).exists(),
                f"前置条件失败：B 的 schema 里也存在绩效 {self.perf_a.pk} —— "
                "`setUp` 的「A 的 id > B 的最大 id」构造保证已失效（见模块说明的判据纪律 1）",
            )

        crossed = self._post_b(
            f"/api/business/performances/{self.perf_a.pk}/adjustments",
            {"amount": "-1.00", "reason": "跨租户探针", "request_id": "p4-iso-cross-req"},
        )
        self.assertEqual(crossed.status_code, 200, crossed.content[:300])
        self.assertEqual(crossed.json()["code"], 1000, crossed.content[:300])
        self.assertIsNone(crossed.json()["data"])
        # 判据落在**数据库行**上：B 里不许留下任何带那个 request_id 的调整行
        with tenant_context(self.tenant_b):
            self.assertFalse(
                PerformanceAdjustment.objects.filter(
                    request_id="p4-iso-cross-req"
                ).exists(),
                "跨租户 POST 在 B 的 schema 里留下了调整行",
            )

        # 控制项：同一个 client 打**自己的**绩效 id ⇒ 成功
        own = self._post_b(
            f"/api/business/performances/{self.perf_b.pk}/adjustments",
            {"amount": "-1.00", "reason": "自己租户的探针", "request_id": "p4-iso-own-req"},
        )
        self.assertEqual(own.status_code, 200, own.content[:300])
        self.assertEqual(own.json()["code"], 0, own.content[:300])
        with tenant_context(self.tenant_b):
            self.assertTrue(
                PerformanceAdjustment.objects.filter(request_id="p4-iso-own-req").exists()
            )

    # ── 凭据层：跨租户不是靠业务过滤兜的 ────────────────────────────
    def test_a_session_on_b_host_is_rejected(self):
        """A 的会话 + B 的域名 ⇒ **401**（中间件短路，未进视图）。

        正向对照：**同一个** client 回自己的域名必须 200——否则这条 401 可能由
        「这个会话压根不可用」凑出来，与域名绑定无关。
        """
        on_own_host = self.admin_a.get(self.URL_SETTLEMENTS, SERVER_NAME=DOMAIN_A)
        self.assertEqual(on_own_host.status_code, 200, on_own_host.content[:200])
        self.assertEqual(on_own_host.json()["code"], 0)

        try:
            crossed = self.admin_a.get(self.URL_SETTLEMENTS, SERVER_NAME=DOMAIN_B_HTTP)
        finally:
            # 被短路的请求同样会把连接留在 B 的 schema 上。
            connection.set_tenant(self.tenant)
        self.assertEqual(crossed.status_code, 401, crossed.content[:200])
