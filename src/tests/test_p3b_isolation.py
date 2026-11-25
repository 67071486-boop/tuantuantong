"""B2-3：P3b 的跨租户隔离回归（`Notification` 数据层 + 绩效列表 + `/my-orders`）。

`AGENTS.md` §4：「任何新增查询都必须携带租户上下文，并补一条『以租户 A 的凭据访问 B 的
数据必须失败』的测试」。P3b 新增了 `Notification`（B2-1）、绩效列表（B1-4）与
`/api/business/my-orders`（B2-2），所以这条红线在**本文件**闭合。

**本文件证什么 / 不证什么**（照 `tests/test_account_isolation.py` 划分的三类，别混着读）：

- **数据层**（`NotificationDataIsolationTests`）：A 写的通知行在 B 的 schema 里不可见，
  且 B 自己写的行可见。照 `tests/test_p3a_isolation.py` 的形态。
- **HTTP 层**（`CrossTenantHttpIsolationTests`）：每个租户一个**真实登录** client + 各自的域名，
  逐个读路径（`/performances`、`/notifications`、`/my-orders`）断言另一租户的业务值不出现。
- **凭据层**（同文件最后一条）：A 的会话拿到 B 的域名上 ⇒ **401**；同一 client 回自己的域名 ⇒ 200。
  它由中间件短路、**根本没进视图**，所以它证明的是「跨租户不是靠业务过滤兜的」；
  P5 的 16 条红线在 `tests/test_p5_credential_plane.py`，这里**只**加这一条同族对照。

⚠️ **判据纪律（Global Constraints #8/#9，本任务的核心）**：

1. **不用主键指认「另一个租户的那一行」**：两个 schema 的主键序列**各自从 1 开始**
   ⇒ `filter(id=a_pk)` 在 B 里命中的是 **B 自己的行**，断言恒真/恒假
   （本文件实测到的形态：两个租户的 `Notification` 主键都是 `1`，见计划 Part 2 的 B2-3 段 M3）。
   本文件一律用**租户内唯一、且两租户取值不同**的业务值：
   订单号（`NO_A` / `NO_B`）与 `Notification.payload` 里的标记字符串（`MARKER_A` / `MARKER_B`）。
2. **非空哨兵**：每条否定断言前都先证明「B 里能看到 B 自己的那一行」，
   否则空列表能让 `assertNotIn` 假通过。**不用 `count()`** 当可见性判据——
   它的通过与否取决于两个租户各有多少行，而不是「这一行可不可见」。
3. **每条测试都能单独运行**（P1 的教训：红线测试曾靠测试顺序假通过）。
   **`CrossTenantHttpIsolationTests`** 的 `setUp` 第一行显式
   `connection.set_tenant(self.tenant)`，挡住上一条用例的 schema 残留。
   ⚠️ `NotificationDataIsolationTests` **没有 `setUp`**：它不依赖环境 schema——
   每次访问都在显式的 `tenant_context(...)` 内（见该类的两个用例与 `_markers`/`_make_notification`
   的调用点），所以「环境 schema 是谁」不影响它的判据。
4. 期望值写**字面值**；断言打**响应体 / 数据库行的值**（E-0009 / E-0024）。

⚠️ **跨域名请求会在请求结束后把连接留在那个租户的 schema 上**（实测，`tests/test_account_isolation.py`
的模块说明有详细记录）：`django_tenants` 的 `TenantMainMiddleware` 在请求进来时
`connection.set_tenant(...)`，但**不在响应结束时还原**。所以本文件所有 B 侧请求一律走
`_get_b()`，它用 `try/finally` 把连接显式钉回租户 A；`CrossTenantHttpIsolationTests.setUp`
的第一行也显式 `set_tenant`（数据层类不依赖环境 schema，见上面的纪律 3）。
**别把这些还原去掉**：它们是本文件能重复运行、且能单独运行的前提。

⚠️ **本文件抓不到什么**：它只对**走当前 schema 下 ORM** 的查询路径有判别力。
一条手工拼 SQL、或把 schema 固定在别处的查询路径不会被这里抓到。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType,
    Game,
    Notification,
    Order,
    OrderStatus,
    Performance,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 租户 A = `TenantTestCase` 的 `test` schema，域名与既有账号测试同口径。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名。与 A 不同（`Domain` 有唯一约束，撞了会在 `provision_tenant` 里报错）。
#: 后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里（与 `acct-iso-b` 同口径）。
DOMAIN_B_DATA = "p3b-iso-notif-b.test.local"
DOMAIN_B_HTTP = "p3b-iso-http-b.test.local"
SCHEMA_B_DATA = "p3b_iso_notif_b"
SCHEMA_B_HTTP = "p3b_iso_http_b"

# ── 判据用的业务值（租户内唯一、且两租户取值不同；**不是主键**）────────────
MARKER_A = "only-in-a"
MARKER_B = "only-in-b"
#: 20 位订单号（与 `order_no` 列宽一致）：A / B 各一条，取值不同。
NO_A = "20260920000000000901"
NO_B = "20260920000000000902"
GAME_A = "仅 A 的游戏"
GAME_B = "仅 B 的游戏"
CS_A = "iso-p3b-cs-a"
CS_B = "iso-p3b-cs-b"
TAKER_A = "iso-p3b-taker-a"
TAKER_B = "iso-p3b-taker-b"
USER_A = "iso-p3b-user-a"
USER_B = "iso-p3b-user-b"


def _new_order(order_no, game_name):
    """在当前 schema 里造「游戏 + 订单」，返回订单（订单号是判据来源之一）。"""
    game = Game.objects.create(name=game_name)
    return Order.objects.create(
        game=game,
        order_no=order_no,
        status=OrderStatus.IN_PROGRESS,
        hall_price=Decimal("88.00"),
    )


class NotificationDataIsolationTests(TenantTestCase):
    """§3.1：`Notification` 的数据层隔离（照 `tests/test_p3a_isolation.py` 形态）。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建：在一个用例内部建 schema 会与外层事务相冲
    （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B_DATA, name="P3b 通知隔离租户 B", domain=DOMAIN_B_DATA
        )

    def _markers(self):
        """当前 schema 里**所有**通知行的 payload 标记值（不是 `count()`）。"""
        return [
            row.get("marker")
            for row in Notification.objects.values_list("payload", flat=True)
        ]

    def _make_notification(self, marker, order_no):
        """在当前 schema 里造「用户 + 订单 + 通知」，返回通知行。

        订单号与 payload 标记都是**租户内唯一且两租户取值不同**的判据来源。
        """
        user = User.objects.create_user(username=f"iso-{marker}")
        order = _new_order(order_no, f"游戏-{marker}")
        return Notification.objects.create(
            user=user,
            kind="被指派",
            order=order,
            payload={"marker": marker, "order_no": order_no},
        )

    def test_b_schema_does_not_see_a_notification(self):
        """B 看不到 A 的通知；同时 B 自己的那条可见（非空哨兵）。

        判据用的是 **payload 标记**与**订单号**——两者都是租户内唯一、两租户取值不同的
        业务值。**不用主键**：两个 schema 的主键序列各自从 1 开始，
        `filter(id=a_row.id)` 在 B 里会命中 B 自己的那一行。
        """
        with tenant_context(self.tenant):
            self._make_notification(MARKER_A, NO_A)

        with tenant_context(self.tenant_b):
            # 非空哨兵：先证明 B 里**能看到自己的行**，下面的否定断言才有意义
            self._make_notification(MARKER_B, NO_B)
            markers = self._markers()
            self.assertIn(MARKER_B, markers)
            # 再断言 A 的行不可见——按**标记**与**A 的订单号**判（两租户取值不同）
            self.assertNotIn(MARKER_A, markers)
            self.assertFalse(Notification.objects.filter(order__order_no=NO_A).exists())
            self.assertTrue(Notification.objects.filter(order__order_no=NO_B).exists())

        with tenant_context(self.tenant):
            self.assertIn(MARKER_A, self._markers())

    def test_a_schema_does_not_see_b_notification(self):
        """反方向：A 也看不到 B 的通知（只断一个方向会漏掉「读写在错误的 schema 上」）。"""
        with tenant_context(self.tenant_b):
            self._make_notification(MARKER_B, NO_B)

        with tenant_context(self.tenant):
            # 非空哨兵：A 自己的行可见
            self._make_notification(MARKER_A, NO_A)
            markers = self._markers()
            self.assertIn(MARKER_A, markers)
            self.assertNotIn(MARKER_B, markers)
            self.assertFalse(Notification.objects.filter(order__order_no=NO_B).exists())
            self.assertTrue(Notification.objects.filter(order__order_no=NO_A).exists())


class CrossTenantHttpIsolationTests(TenantTestCase):
    """§3.2/§3.3：HTTP 层的跨租户隔离（照 `tests/test_account_isolation.py` 的手法）。

    每个租户一个**真实登录** client + 各自的域名：A 的 client 打 `DOMAIN_A`、
    B 的 client 打 `DOMAIN_B`（请求时显式 `SERVER_NAME=DOMAIN_B`）。
    """

    URL_PERFORMANCES = "/api/business/performances/"
    URL_NOTIFICATIONS = "/api/business/notifications/"
    URL_MY_ORDERS = "/api/business/my-orders/"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B_HTTP, name="P3b HTTP 隔离租户 B", domain=DOMAIN_B_HTTP
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A。跨域名请求不会在响应结束时还原 schema，
        #    所以上一条用例可能把连接留在 B 上（见模块说明）。
        connection.set_tenant(self.tenant)

        # ── A 侧：客服 / 代练员 / 普通用户，各自真实登录 ──
        # ⚠️ 客服要 `my_performance.export`：绩效列表有类级节点门
        #    （`PerformanceViewSet.PERFORMANCE_PERMISSIONS["list"]`），`groups` 不放行。
        # ⚠️ 还要 `order.list`（管理侧数据范围节点）：不给的话列表被收窄成
        #    「只看自己」，而本用例要验的是**跨租户**不泄漏（绩效行归属的是代练员）。
        self.cs_a = make_logged_in_client(
            domain=DOMAIN_A, username=CS_A, groups=("客服",),
            nodes=("my_performance.export", "order.list"),
        )
        self.taker_a = make_logged_in_client(
            domain=DOMAIN_A, username=TAKER_A, groups=("代练员",),
                    # `/my-orders` 的类级节点门 = `my_orders.send_to_my_system`。
            nodes=("my_orders.send_to_my_system",),
        )
        self.user_a = make_logged_in_client(
            domain=DOMAIN_A, username=USER_A, groups=("代练员",)
        )
        game_a = Game.objects.create(name=GAME_A)
        # ⭐ 按游戏权限收窄：绩效列表按**请求方**的游戏权限收窄 ⇒
        #    A 的客服要显式持有 A 的游戏，否则「A 看得到自己那单」这条正向对照会红
        #    （跨租户否定的判据本身不变：仍是订单号，不是主键）。
        grant_games(User.objects.get(username=CS_A), game_a)
        perf_a = self._make_performance(game_a, NO_A, User.objects.get(username=TAKER_A))
        # 通知挂在**同一张**订单上（订单号租户内唯一：再建一张同号的单会撞
        # `business_order_order_no_key`，实测踩到）。
        self._make_notification(User.objects.get(username=USER_A), MARKER_A, perf_a.order)

        # ── B 侧：同样的形状，在 B 的 schema 里（建 client 用 try/finally 还原）──
        try:
            with tenant_context(self.tenant_b):
                self.cs_b = make_logged_in_client(
                    domain=DOMAIN_B_HTTP, username=CS_B, groups=("客服",),
                    # 同 A 侧：绩效列表还要管理侧节点 `order.list`。
                    nodes=("my_performance.export", "order.list"),
                )
                self.taker_b = make_logged_in_client(
                    domain=DOMAIN_B_HTTP, username=TAKER_B, groups=("代练员",),
                    # 同 A 侧：`/my-orders` 的节点门。
                    nodes=("my_orders.send_to_my_system",),
                )
                self.user_b = make_logged_in_client(
                    domain=DOMAIN_B_HTTP, username=USER_B, groups=("代练员",)
                )
                game_b = Game.objects.create(name=GAME_B)
                # 同 A 侧：B 的客服要显式持有 B 的游戏（写在 B 的 schema 里）。
                grant_games(User.objects.get(username=CS_B), game_b)
                perf_b = self._make_performance(
                    game_b, NO_B, User.objects.get(username=TAKER_B)
                )
                self._make_notification(
                    User.objects.get(username=USER_B), MARKER_B, perf_b.order
                )
        finally:
            # 登录夹具发过跨域名请求（见模块说明）：连接必须钉回 A。
            connection.set_tenant(self.tenant)

    # ── 工具 ───────────────────────────────────────────────────────
    def _make_performance(self, game, order_no, user):
        """在当前 schema 里造一条「代练员接单」的绩效（`emp_type=MAIN`）。"""
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=user,
            temp_amount=Decimal("88.00"),
        )

    def _make_notification(self, user, marker, order):
        """在当前 schema 里给 `user` 造一条通知（挂在一张**已存在**的订单上）。"""
        return Notification.objects.create(
            user=user,
            kind="被指派",
            order=order,
            payload={"marker": marker, "order_no": order.order_no},
        )

    def _get_b(self, client, url, params=None):
        """在 B 的域名上发请求，**结束后把连接钉回租户 A**（见模块说明）。"""
        try:
            return client.get(url, params or {}, SERVER_NAME=DOMAIN_B_HTTP)
        finally:
            connection.set_tenant(self.tenant)

    def _items(self, response):
        """取信封里的 `items`，先断言端点本身是成功的（前置哨兵）。"""
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _order_nos(self, response):
        return [row["order_no"] for row in self._items(response)]

    def _markers(self, response):
        return [row["payload"].get("marker") for row in self._items(response)]

    # ── 逐读路径：另一租户的业务值不得出现 ──────────────────────────
    def test_performance_list_does_not_leak_the_other_tenant(self):
        """`/api/business/performances/`：B 的客服只看到 B 的订单号。

        两个方向都断（A 看得到自己的 / B 看得到自己的），且断言落在**具体订单号**上：
        B 的列表非空哨兵 + 明确不含 A 的订单号。
        """
        a_nos = self._order_nos(
            self.cs_a.get(self.URL_PERFORMANCES, SERVER_NAME=DOMAIN_A)
        )
        self.assertIn(NO_A, a_nos)                 # 正向对照：A 看得到自己那单
        self.assertNotIn(NO_B, a_nos)

        b_nos = self._order_nos(self._get_b(self.cs_b, self.URL_PERFORMANCES))
        self.assertTrue(b_nos, "B 的绩效列表为空——这条负断言被空列表凑出来了，测试自身失效")
        self.assertIn(NO_B, b_nos)                 # 非空哨兵
        self.assertNotIn(NO_A, b_nos)              # 判据：A 的订单号（不是主键）

    def test_notification_list_does_not_leak_the_other_tenant(self):
        """`/api/business/notifications/`：B 的用户只看到自己的通知标记。

        判据是 `payload` 里的**标记字符串**（租户内唯一、两租户取值不同），
        与通知行/订单的主键无关。
        """
        a_markers = self._markers(
            self.user_a.get(self.URL_NOTIFICATIONS, SERVER_NAME=DOMAIN_A)
        )
        self.assertIn(MARKER_A, a_markers)         # 正向对照
        self.assertNotIn(MARKER_B, a_markers)

        b_markers = self._markers(self._get_b(self.user_b, self.URL_NOTIFICATIONS))
        self.assertTrue(b_markers, "B 的通知列表为空——负断言被空列表凑出来了")
        self.assertIn(MARKER_B, b_markers)         # 非空哨兵
        self.assertNotIn(MARKER_A, b_markers)

    def test_my_orders_does_not_leak_the_other_tenant(self):
        """`/api/business/my-orders/`：B 的代练员只看到 B 自己接的单。"""
        a_nos = self._order_nos(
            self.taker_a.get(self.URL_MY_ORDERS, SERVER_NAME=DOMAIN_A)
        )
        self.assertIn(NO_A, a_nos)                 # 正向对照
        self.assertNotIn(NO_B, a_nos)

        b_nos = self._order_nos(self._get_b(self.taker_b, self.URL_MY_ORDERS))
        self.assertTrue(b_nos, "B 的「我接的单」为空——负断言被空列表凑出来了")
        self.assertIn(NO_B, b_nos)                 # 非空哨兵
        self.assertNotIn(NO_A, b_nos)              # 判据：A 的订单号（不是主键）

    # ── 凭据层：跨租户不是靠业务过滤兜的 ────────────────────────────
    def test_a_session_on_b_host_is_rejected(self):
        """A 的会话 + B 的域名 ⇒ **401**（中间件短路，未进视图）。

        正向对照：**同一个** client 回到自己的域名上必须 200——否则这条 401 可能由
        「这个会话压根不可用」凑出来，与域名绑定无关。
        """
        on_own_host = self.cs_a.get(self.URL_PERFORMANCES, SERVER_NAME=DOMAIN_A)
        self.assertEqual(on_own_host.status_code, 200, on_own_host.content[:200])
        self.assertEqual(on_own_host.json()["code"], 0)

        try:
            crossed = self.cs_a.get(self.URL_PERFORMANCES, SERVER_NAME=DOMAIN_B_HTTP)
        finally:
            # 被短路的请求同样会把连接留在 B 的 schema 上（见模块说明）。
            connection.set_tenant(self.tenant)
        self.assertEqual(crossed.status_code, 401, crossed.content[:200])
