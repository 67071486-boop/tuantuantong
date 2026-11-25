"""列表载荷的加法：接单人名字与退款额（spec §5.3）、头部统计块（spec §7.1）。

⚠️ 除了 brief 列出的单租户用例，本文件还有一个双租户类 `TakerNameCrossTenantTests`：
spec §10 第 1 条与 `AGENTS.md` §4 都要求「新增查询必须补一条以 A 的凭据访问 B 的数据
必须失败」的测试，而 `taker_name` 正是本任务新增的查询（相关子查询）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_flags import can_view_taker
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 列表端点的类级节点门（`OrderViewSet.ORDER_PERMISSIONS["list"]`）——不挂它拿 403。
LIST_NODES = ("order.list",)


class OrderListPayloadTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(username="cs1", groups=("客服",), nodes=LIST_NODES)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类的读者都要看到 `self.game` 下的单；没有档案 / 没勾游戏 ⇒ 读侧空结果。
        grant_games(User.objects.get(username="cs1"), self.game)

    def _order(self, no, **kw):
        return Order.objects.create(
            game=self.game, order_no=no, status=OrderStatus.UNASSIGNED, **kw
        )

    def _taker(self, order, username):
        """在当前 schema 里给订单挂一条 `emp_type=MAIN` 的绩效行（= 接单人）。"""
        user = User.objects.create_user(username=username)
        Performance.objects.create(order=order, user=user, emp_type=EmpType.MAIN)
        return user

    def _items(self, client=None):
        """取信封里的 `items`，先断言端点本身成功（前置哨兵）。

        空集会让「字段存在」那类断言失去意义，所以端点失败必须在**断言取值之前**响亮。
        """
        body = (client or self.client).get("/api/business/orders/").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def test_taker_name_is_empty_when_nobody_took_it(self):
        self._order("20260922000000000001")
        assert self._items()[0]["taker_name"] == ""

    def test_taker_name_is_the_main_performance_user(self):
        order = self._order("20260922000000000002")
        self._taker(order, "taker1")
        assert self._items()[0]["taker_name"] == "taker1"

    def test_two_performance_rows_do_not_duplicate_the_order_row(self):
        """`performances` 是多值关系：子查询口径下仍然只回一行（spec §2 第 19 条的坑）。"""
        order = self._order("20260922000000000003")
        self._taker(order, "taker2")
        User.objects.create_user(username="cs2")
        Performance.objects.create(
            order=order, user=User.objects.get(username="cs2"), emp_type=EmpType.SALES
        )
        items = self._items()
        assert len(items) == 1
        assert items[0]["taker_name"] == "taker2"

    def test_refund_fee_is_zero_by_default_and_carries_the_amount(self):
        self._order("20260922000000000004")
        self._order("20260922000000000005", refund_fee=Decimal("30.00"))
        items = {i["order_no"]: i for i in self._items()}
        assert items["20260922000000000004"]["refund_fee"] == "0.00"
        assert items["20260922000000000005"]["refund_fee"] == "30.00"

    def test_restrict_view_taker_hides_every_taker_name(self):
        order = self._order("20260922000000000006")
        self._taker(order, "taker3")
        restricted = make_logged_in_client(
            username="cs3", groups=("客服",), nodes=(*LIST_NODES, "order.restrict_view_taker")
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 受限的是接单人**名字**（flag），不是这一行的可见性：本用例要的是「行在、名字被脱敏」，
        # 所以这位读者必须看得到本单所属的游戏（补游戏权限，不动 flag / 节点 / 角色）。
        grant_games(User.objects.get(username="cs3"), self.game)
        items = self._items(restricted)
        assert [i["taker_name"] for i in items] == ["无权查看"]

    def test_without_the_flag_taker_name_is_not_redacted(self):
        """fail-open 的反面：未授予该 flag 时**不**施加限制（与既有 flag 同口径）。"""
        order = self._order("20260922000000000007")
        self._taker(order, "taker4")
        assert self._items()[0]["taker_name"] == "taker4"

    def test_hall_taker_name_stays_empty_under_the_flag(self):
        """大厅的池内单本来就没有接单人 ⇒ 持旗帜时也是空串，**不是**常量 `无权查看`。

        判别力：把 `get_taker_name` 里「没有 `taker_name` 注解就回空串」那一支删掉 ⇒
        本用例变红（拿到 `无权查看`）。
        """
        pool = Order.objects.create(
            game=self.game,
            order_no="20260922000000000008",
            status=OrderStatus.WAITING_GRAB,
        )
        # 大厅只列「已抛单且还没人接」：MAIN 绩效行在、`user` 为空（`search_hall` 的条件）。
        Performance.objects.create(order=pool, emp_type=EmpType.MAIN)
        taker = make_logged_in_client(
            username="taker5",
            groups=("代练员",),
            nodes=("grab_hall.take_order", "order.restrict_view_taker"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 大厅与订单列表同一维度收窄：代练员要看到 `self.game` 下那张池内单才谈得上载荷。
        grant_games(User.objects.get(username="taker5"), self.game)
        body = taker.get("/api/business/hall/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        # 非空哨兵：大厅里真的有一行，否则下面的断言是空谈
        self.assertEqual([i["order_no"] for i in items], [pool.order_no])
        assert items[0]["taker_name"] == ""

    def test_flag_is_resolved_once_per_page_not_once_per_row(self):
        """旗帜一页只解**一次**，不是每行一次。

        判别力：把 `get_fields()` 里的缓存去掉、改回在 `get_taker_name` 里逐行调
        `can_view_taker` ⇒ 本用例变红（`call_count` 会等于行数）。那条判据落到一个
        没有缓存的节点查询上，20 行列表就会多出 20 条查询。
        """
        for i in range(3):
            order = self._order(f"2026092200000001{i:02d}")
            self._taker(order, f"taker-{i}")
        with mock.patch(
            "apps.business.api.serializers.can_view_taker", wraps=can_view_taker
        ) as spy:
            items = self._items()
        self.assertEqual(len(items), 3)  # 非空哨兵：真的序列化了三行
        self.assertEqual(spy.call_count, 1)


# ── 跨租户：`taker_name` 的子查询必须留在本租户 schema 内 ──────────────────
#: 租户 A = `TenantTestCase` 的 `test` schema（域名与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：与 A 不同；`.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "olp1-taker-b.test.local"
SCHEMA_B = "olp1_taker_b"

#: 判据用的业务值：**租户内唯一、且两租户取值不同**——不是主键。
NO_A = "20260922000000000901"
NO_B = "20260922000000000902"
TAKER_A = "iso-olp1-taker-a"
TAKER_B = "iso-olp1-taker-b"


class TakerNameCrossTenantTests(TenantTestCase):
    """`taker_name` 的跨租户隔离（spec §10 第 1 条 / `AGENTS.md` §4 的红线）。

    ⚠️ 判据用**订单号 + 用户名**，**不用主键**：两个 schema 的主键序列各自从 1 开始，
    `filter(id=a_row.id)` 在另一个租户里命中的是它自己的那一行（E-0023 的同型坑）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="列表载荷隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # 跨域名请求不会在响应结束时还原 schema（`TenantMainMiddleware` 只进不出），
        # 上一条用例可能把连接留在 B 上 ⇒ 这里显式钉回 A。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(
            domain=DOMAIN_A, username="iso-olp1-cs-a", groups=("客服",), nodes=LIST_NODES
        )

    def _make_taken_order(self, order_no, taker_username):
        """在当前 schema 里造「游戏 + 订单 + MAIN 绩效行」，返回订单。"""
        game = Game.objects.create(name=f"游戏-{order_no}")
        order = Order.objects.create(
            game=game, order_no=order_no, status=OrderStatus.IN_PROGRESS
        )
        user = User.objects.create_user(username=taker_username)
        Performance.objects.create(order=order, user=user, emp_type=EmpType.MAIN)
        return order

    def _rows(self, client, domain):
        """取 `{order_no: taker_name}`；先断言端点本身成功（前置哨兵）。"""
        body = client.get("/api/business/orders/", SERVER_NAME=domain).json()
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"]: row["taker_name"] for row in body["data"]["items"]}

    def test_taker_name_does_not_leak_across_tenants(self):
        order_a = self._make_taken_order(NO_A, TAKER_A)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # A 侧读者要能看到**自己租户、自己游戏**那一单（否则正向对照与「行在」都是空谈）；
        # 补的是游戏权限，跨租户的否定断言一位没动。
        grant_games(User.objects.get(username="iso-olp1-cs-a"), order_a.game)

        try:
            with tenant_context(self.tenant_b):
                order_b = self._make_taken_order(NO_B, TAKER_B)
                b_client = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="iso-olp1-cs-b",
                    groups=("客服",),
                    nodes=LIST_NODES,
                )
                # 同上，B 侧读者只授 B 自己那款游戏。
                grant_games(User.objects.get(username="iso-olp1-cs-b"), order_b.game)
                b_rows = self._rows(b_client, DOMAIN_B)
        finally:
            # B 侧真实登录 + B 域名请求都会把连接留在 B 上（见 setUp 的说明）。
            connection.set_tenant(self.tenant)

        # ⚠️ **必须在 B 的行建出来之后再取 A 的列表**：在 B 建行**之前**取的快照里本来
        # 就没有 B 的订单号/用户名，那两条否定断言会**恒真**（恒真的断言看起来比没测更
        # 可靠）。先建 B、后取 A，A 的列表才会覆盖「B 已有行」的那一刻。
        a_rows = self._rows(self.client, DOMAIN_A)

        # 正向对照 + 非空哨兵：两侧都真的看得到**自己**那单与自己的接单人
        self.assertEqual(a_rows.get(NO_A), TAKER_A)
        self.assertEqual(b_rows.get(NO_B), TAKER_B)
        # 两个方向都断，判据落在**订单号**与**接单人用户名**上
        self.assertNotIn(NO_B, a_rows)
        self.assertNotIn(TAKER_B, a_rows.values())
        self.assertNotIn(NO_A, b_rows)
        self.assertNotIn(TAKER_A, b_rows.values())


# ── 列表头部统计块（spec §7.1，决策 C12/C13/C14）─────────────────────


class OrderListSummaryTests(TenantTestCase):
    """`data.summary` 的口径：同一份筛选上的聚合、差额不含退款、全 8 状态补零。

    ⚠️ **口径**：`diff = total_price − total_hall_price`，**不含** `refund_fee`（C13）。
    ⚠️ 受限用户（持 `order.restrict_view_total_stats`）拿到 `summary: null`（C14）——
    **键在、值为 `null`**（不是缺键）：前端据此隐藏那句话。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(username="cs9", groups=("客服",), nodes=LIST_NODES)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 统计块打在**同一份筛选后的 queryset** 上 ⇒ 列表读者没有游戏权限就会是空集统计。
        grant_games(User.objects.get(username="cs9"), self.game)
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000011",
            status=OrderStatus.UNASSIGNED,
            total_price=Decimal("100.00"),
            hall_price=Decimal("60.00"),
            created_by="cs9",
        )
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000012",
            status=OrderStatus.COMPLETED,
            total_price=Decimal("200.00"),
            hall_price=None,
            created_by="cs9",
        )

    def _body(self, query="", client=None):
        """取整个信封；先断言端点本身成功（前置哨兵），否则取值失败会归错因。"""
        body = (client or self.client).get("/api/business/orders/" + query).json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _summary(self, query="", client=None):
        return self._body(query, client)["data"]["summary"]

    def test_summary_totals_and_diff(self):
        s = self._summary()
        assert s["count"] == 2
        assert s["total_price"] == "300.00"
        assert s["total_hall_price"] == "60.00"
        # = total_price − total_hall_price（不含退款，spec C13）
        assert s["diff"] == "240.00"

    def test_summary_count_equals_envelope_total_not_the_page(self):
        """`count` 是**筛选后的总数**，与信封的 `total` 同值、不是当前页的行数。"""
        body = self._body("?page=1&page_size=1")
        assert len(body["data"]["items"]) == 1  # 非空哨兵：这一页确实只有一行
        assert body["data"]["summary"]["count"] == 2 == body["data"]["total"]

    def test_status_counts_cover_all_eight_with_zeros(self):
        s = self._summary()
        assert set(s["status_counts"]) == {
            "UNASSIGNED", "WAITING_GRAB", "IN_PROGRESS", "PENDING_REVIEW",
            "COMPLETED", "ISSUE", "PAUSED", "REFUNDED",
        }
        assert s["status_counts"]["UNASSIGNED"] == 1
        assert s["status_counts"]["COMPLETED"] == 1
        assert s["status_counts"]["REFUNDED"] == 0

    def test_status_counts_are_not_split_by_ordering(self):
        """同一状态两张单 ⇒ 计数是 **2**，而不是被排序拆成一行一个 1。

        实测的判别力（Task 2 报告）：把计数查询写成 `.order_by("-created_at")`（**显式**排序）
        ⇒ 排序列进 GROUP BY（`SQLCompiler.get_group_by()` 在 `_meta_ordering` 为空时追加
        ORDER BY 的列）⇒ 本用例变红。
        ⚠️ 反过来，「整段去掉 `.order_by()`」**杀不死本用例**：Django 5.2 不把
        `Meta.ordering` 的列放进 GROUP BY。所以这条守的是「计数查询不带排序」这个**形状**，
        不是「一定要有 `Meta.ordering`」。
        """
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000013",
            status=OrderStatus.UNASSIGNED,
            total_price=Decimal("1.00"),
        )
        assert self._summary()["status_counts"]["UNASSIGNED"] == 2

    def test_summary_follows_the_filter(self):
        s = self._summary("?status=COMPLETED")
        assert s["count"] == 1
        assert s["total_price"] == "200.00"
        # 新字段跟着同一份筛选收窄（2026-11-05 统计句口径）：筛 COMPLETED ⇒ 无已退单单。
        assert s["total_refund_fee"] == "0.00"
        assert s["refunded_diff"] == "0.00"

    def test_summary_refund_fee_and_refunded_diff(self):
        """`total_refund_fee` = 全部筛选结果的退款费合计；`refunded_diff` = 只算已退单
        （REFUNDED）那些单自己的 `接单价 − 发单价`（2026-11-05 业务方要求统计句写出
        「含已退单部分」的具体金额）。

        判别力：非退单单的利润（80−60=20）**不进** `refunded_diff`；`diff` 仍不含退款费。
        """
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000014",
            status=OrderStatus.REFUNDED,
            total_price=Decimal("90.00"),
            hall_price=Decimal("40.00"),
            refund_fee=Decimal("7.50"),
            created_by="cs9",
        )
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000015",
            status=OrderStatus.COMPLETED,
            total_price=Decimal("80.00"),
            hall_price=Decimal("60.00"),
            refund_fee=Decimal("2.50"),
            created_by="cs9",
        )
        s = self._summary()
        assert s["total_refund_fee"] == "10.00"
        assert s["refunded_diff"] == "50.00"
        # diff = (100+200+90+80) − (60+40+60) = 310，照旧不减退款费（C13 口径没动）。
        assert s["diff"] == "310.00"

    def test_summary_of_an_empty_result_is_all_zeros(self):
        """一条都没筛到：金额全 `0.00`、`count` 为 0、8 个状态全 0（`Sum` 的空集回落）。

        判别力：把 `Sum(..., default=...)` 的 `default` 去掉、并去掉后面的 `None` 归一化
        ⇒ `f"{None:.2f}"` 抛 `TypeError` ⇒ 本用例变红（空结果集是检索列表最常走的一支）。
        """
        s = self._summary("?status=ISSUE")
        assert s["count"] == 0
        assert s["total_price"] == "0.00"
        assert s["total_hall_price"] == "0.00"
        assert s["total_refund_fee"] == "0.00"
        assert s["refunded_diff"] == "0.00"
        assert s["diff"] == "0.00"
        assert set(s["status_counts"].values()) == {0}

    def test_summary_follows_creator_scoping(self):
        """`order.restrict_to_self_created` 的收窄对统计块同样生效（与列表同一份 queryset）。

        判别力：把 `list()` 里的 `self.filter_queryset(...)` 换成 `self.get_queryset()` ⇒
        统计块会把**别人录的单**也算进去 ⇒ 本用例的 `count == 1` 变红。
        """
        restricted = make_logged_in_client(
            username="cs11", groups=("客服",),
            nodes=(*LIST_NODES, "order.restrict_to_self_created"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 受限的是**录单人范围**（flag 收窄），不是游戏可见性：本用例要 `count == 1`
        # （自己那一单），所以这位读者必须看得到本单所属的游戏。
        grant_games(User.objects.get(username="cs11"), self.game)
        # 自己录一单（`created_by` 装的是认证态用户名）；setUp 那两单的 `created_by` 是 `cs9`
        # （夹具里显式写死，**不是** `Order.created_by` 的默认空串），所以收窄后看不到它们。
        Order.objects.create(
            game=self.game,
            order_no="20260922000000000014",
            status=OrderStatus.ISSUE,
            total_price=Decimal("5.00"),
            created_by="cs11",
        )
        s = self._summary(client=restricted)
        assert s["count"] == 1
        assert s["status_counts"]["ISSUE"] == 1
        assert s["total_price"] == "5.00"

    def test_summary_does_not_change_with_pagination(self):
        first = self._summary("?page=1&page_size=1")
        second = self._summary("?page=2&page_size=1")
        assert first == second

    def test_summary_is_null_when_restricted(self):
        restricted = make_logged_in_client(
            username="cs10", groups=("客服",),
            nodes=(*LIST_NODES, "order.restrict_view_total_stats"),
        )
        data = self._body(client=restricted)["data"]
        assert "summary" in data          # 键在……
        assert data["summary"] is None    # ……值为 null（C14）


class HallSummaryRegressionTests(TenantTestCase):
    """大厅与订单列表**共用** `OrderListPagination` ⇒ 大厅载荷不该长出 `summary` 键。

    判别力：把分页器的哨兵撤掉、退回「永远写 `summary`」的朴素实现 ⇒ 本用例变红
    （Task 2 报告里有这次变异的实测输出）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.pool = Order.objects.create(
            game=self.game,
            order_no="20260922000000000021",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("50.00"),
        )
        # 大厅只列「有 MAIN 绩效行、且还没人接」的单（`search_hall` 的两个条件）。
        Performance.objects.create(order=self.pool, emp_type=EmpType.MAIN)

    def test_hall_response_has_no_summary_key(self):
        taker = make_logged_in_client(
            username="taker-hall-summary",
            groups=("代练员",),
            nodes=("grab_hall.take_order",),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 大厅与订单列表同一维度收窄：代练员要看到 `self.game` 下那张池内单才谈得上载荷。
        grant_games(User.objects.get(username="taker-hall-summary"), self.game)
        body = taker.get("/api/business/hall/").json()
        self.assertEqual(body["code"], 0, body)
        # 非空哨兵：大厅真的回了那一行；否则「没有 summary」会因为载荷本来就是空的而恒真。
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]], [self.pool.order_no]
        )
        assert "summary" not in body["data"]


class HallShadowOrderVisibilityTests(TenantTestCase):
    """⭐ 2026-10-26：镜像卡派生的**隐藏单**不得出现在面向人的订单读面上。

    判据（从 **API 响应**断言，⛔ 不是只查数据库）：
    - 订单管理列表 `items` 不含它；
    - 列表头部的统计块（`summary`：条数 / 金额）也不含它；
    - 按单号打开详情**仍然成功**（隐藏 ≠ 不存在，客服排障要看得到）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="阴影夹具游戏")
        self.client = make_logged_in_client(
            username="cs-shadow",
            groups=("客服",),
            # ⭐ 阻断 3：本类要证「隐藏单**只有 retrieve** 放行」⇒ 写动作的节点必须给全，
            #    否则 403（权限门）会先于 1002（对隐藏单挡住）发生，用例就没有判别力。
            nodes=(
                "order.list", "order.view_detail",
                "order.add_edit", "order.submit_edit",
                "order.assign_taker", "order.change_status", "order.delete",
                "order.recycle",
            ),
        )
        grant_games(User.objects.get(username="cs-shadow"), self.game)

        from apps.business.models import PeerHallMirror, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token

        binding = PeerUpstreamBinding.objects.create(
            peer_name="阴影夹具上家",
            peer_base_url="https://syn-shadow-list-up.test.invalid",
            peer_account="syn-shadow-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://syn-shadow-list-local.test.invalid",
            bound_by=User.objects.get(username="cs-shadow"),
            hall_sync_enabled=True,
        )
        self.mirror = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="SYN-SHADOW-1",
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            remote_hall_price=Decimal("111.00"),
            first_seen_at=timezone.now(),
        )
        self.normal = Order.objects.create(
            game=self.game,
            order_no="20261026000000000001",
            status=OrderStatus.UNASSIGNED,
            total_price=Decimal("10.00"),
            hall_price=Decimal("5.00"),
        )
        #: 隐藏单：金额故意大得离谱 ⇒ 漏进统计会一眼看出来。
        self.shadow = Order.objects.create(
            game=self.game,
            order_no="20261026000000000002",
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("999.00"),
            hall_price=Decimal("111.00"),
            source_order_no="SYN-SHADOW-1",
            hall_card=self.mirror,
        )

    def test_the_order_list_and_its_summary_exclude_the_hidden_order(self):
        body = self.client.get("/api/business/orders/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(
            [row["order_no"] for row in items], [self.normal.order_no]
        )
        summary = body["data"]["summary"]
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_price"], "10.00")
        self.assertEqual(summary["total_hall_price"], "5.00")

    def test_the_hidden_order_detail_still_opens_by_order_no(self):
        """详情按单号仍可打开（⛔ 不要 404：隐藏只是不进列表，不是不存在）。"""
        body = self.client.get(
            f"/api/business/orders/{self.shadow.order_no}/"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["order_no"], self.shadow.order_no)

    def test_the_hidden_order_audit_still_opens(self):
        """⭐ 二轮评审 Important 1：`/audit/` 是**只读** detail 动作 ⇒ 与 retrieve 一起放行。

        它读的是 OperationLog 留痕、是详情页的一块内容。挡掉它时**不报错**：详情页轮询
        `/audit/` 每次回 1002 + 「记录不存在」，前端每 2 分钟弹一次。⚠️ 这是**行为**判据
        （结构网 `HallShadowDetailActionWhitelistTests` 另外钉「放行集恰好 = retrieve + 全部
        detail GET 动作」）。
        """
        response = self.client.get(
            f"/api/business/orders/{self.shadow.order_no}/audit/"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"], [], "这张单没有任何留痕 ⇒ 空列表（不是 1002）")

    # ── ⭐ 阻断 3：隐藏单**只放行 retrieve**（其余动作回既有业务错误 1002）──────────
    def test_patch_on_the_hidden_order_is_refused(self):
        before = self.shadow.project_text
        response = self.client.patch(
            f"/api/business/orders/{self.shadow.order_no}/",
            data={"project_text": "syn-改一下"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 1002, body)
        self.shadow.refresh_from_db()
        self.assertEqual(self.shadow.project_text, before, "隐藏单被 PATCH 改到了")

    def test_assign_on_the_hidden_order_is_refused(self):
        body = self.client.post(
            f"/api/business/orders/{self.shadow.order_no}/assign/",
            data={"user_id": self.client.session.get("_auth_user_id")},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)

    def test_transition_on_the_hidden_order_is_refused(self):
        body = self.client.post(
            f"/api/business/orders/{self.shadow.order_no}/transition/",
            data={"target": "IN_PROGRESS"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.shadow.refresh_from_db()
        self.assertEqual(self.shadow.status, OrderStatus.WAITING_GRAB)

    def test_delete_on_the_hidden_order_is_refused(self):
        body = self.client.post(
            f"/api/business/orders/{self.shadow.order_no}/delete/",
            data={"isDelEmp": "N"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.shadow.refresh_from_db()
        self.assertIsNone(self.shadow.deleted_at, "隐藏单被删掉了")

    def test_restore_and_batch_actions_are_refused_too(self):
        """其余非 retrieve 动作也一并挡住（恢复 / 批量改状态 / 批量删除）。"""
        restored = self.client.post(
            f"/api/business/orders/{self.shadow.order_no}/restore/",
            data={}, content_type="application/json",
        ).json()
        self.assertEqual(restored["code"], 1002, restored)
        for path, payload in (
            ("batch/transition/", {"order_nos": [self.shadow.order_no],
                                   "target": "IN_PROGRESS", "dry_run": False}),
            ("batch/delete/", {"order_nos": [self.shadow.order_no],
                               "isDelEmp": "N", "dry_run": False}),
            ("batch/assign/", {
                "order_nos": [self.shadow.order_no], "dry_run": False,
                "user_id": User.objects.get(username="cs-shadow").pk,
            }),
        ):
            body = self.client.post(
                f"/api/business/orders/{path}", data=payload,
                content_type="application/json",
            ).json()
            # `run_batch` 的整批拒绝形状：1002 + `data.conflict`（取不到这一行）。
            self.assertEqual(body["code"], 1002, (path, body))
            self.assertEqual(
                body["data"]["conflict"]["order_no"], self.shadow.order_no, (path, body)
            )
        self.shadow.refresh_from_db()
        self.assertEqual(self.shadow.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.shadow.deleted_at)
