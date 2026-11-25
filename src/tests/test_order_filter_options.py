"""打手 / 发单人筛选 + 候选名单端点 `GET /api/business/orders/filter-options/`
（spec §8-T11 第 52–69 条；**T11-59 是生产代码里的注释级登记**（`selectors.search_orders`
的 `is not None` 说明）、不是本文件的断言；T11-70–72 是前端，归 T9。D13 / §5.4 / §5.6）。

⚠️ 夹具值一律合成（E-0077）；跨租户的否定断言用**用户名 / 订单号**指认，⛔ 不用主键（§5-G）。
⚠️ 计划示例里的三处笔误按**实测事实**改了（已在执行记录里登记）：
   ① `make_logged_in_client()` 返回的是 **client**（不是 user）⇒ user 对象按用户名回查；
   ② `nodes=` 必须配 `groups=`（`_grant_nodes` 只把节点挂到「当前所在 Group 的 Role」上）；
   ③ 列表载荷是 `{items,total,page,page_size}`（**不是** DRF 默认的 `{results,count}`）。
⚠️ T11-53 的原始夹具（「同一单两行 MAIN 绩效」）**在本仓不可能**：`uniq_order_main_taker`
   （`Performance.Meta.constraints`，`fields=("order",) condition=Q(emp_type=MAIN)`）
   保证一条订单最多一条 MAIN 行 ⇒ 第二条 `create()` 直接 `IntegrityError`。见本文件
   `TakerFilterDoesNotFanOutTests` 的处置（把「不可能」本身钉成判据 + 用真实的多行形状做对照）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import EmpType, Game, Order, OrderSource, Performance
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

NO_A = "20260924000000000701"
NO_B = "20260924000000000702"

#: 跨租户隔离用例（T11-68 / T11-69）的租户 B 域名 / schema。
#: 后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "order-filter-options-b.test.local"
SCHEMA_B = "order_filter_options_b"

#: 跨租户用例的各租户取值（合成值）。判据一律用**用户名 / 订单号**这类
#: 租户内唯一、且本例两租户取值不同的字段，⛔ 不用主键（`AGENTS.md` §5-G）。
NO_ISO_A1 = "20260924000000000711"
NO_ISO_A2 = "20260924000000000712"
NO_ISO_B = "20260924000000000713"
TAKER_A1 = "测试打手甲"
TAKER_A2 = "测试打手乙"
#: B 的打手用户名刻意与 A 的两个都不同（否则「A 的名单不含 B 的名字」会假红）。
TAKER_B = "测试跨租户打手丁"


def _user(username):
    """按用户名取账号（`make_logged_in_client` 返回的是 client，不是 user）。"""
    make_logged_in_client(username=username)
    return get_user_model().objects.get(username=username)


class FilterOptionsShapeTests(TenantTestCase):
    """第 60 / 63 / 64 条：形状、`name` 口径、去重。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(game=self.game, order_no=NO_A)
        # ⚠️ 一条订单最多一条 MAIN 行（`uniq_order_main_taker`）⇒ 两个打手必须挂在**两张单**上。
        self.order_b = Order.objects.create(game=self.game, order_no=NO_B)
        self.nick = _user("测试打手甲")
        self.nick.first_name = "测试昵称甲"
        self.nick.save(update_fields=["first_name"])
        self.no_nick = _user("测试打手乙")   # first_name 空串
        Performance.objects.create(order=self.order, user=self.nick, emp_type=EmpType.MAIN)
        Performance.objects.create(order=self.order_b, user=self.no_nick, emp_type=EmpType.MAIN)
        self.order.hall_publisher = self.nick
        self.order.save(update_fields=["hall_publisher"])
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def test_the_response_keys_are_exactly_the_four_contracted_ones(self):
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        self.assertEqual(
            set(data),
            {"takers", "publishers", "takers_truncated", "publishers_truncated"},
        )
        for row in data["takers"] + data["publishers"]:
            self.assertEqual(set(row), {"id", "name"})

    def test_name_prefers_the_nickname_and_falls_back_to_the_login_name(self):
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        by_id = {row["id"]: row["name"] for row in data["takers"]}
        self.assertEqual(by_id[self.nick.pk], "测试昵称甲")
        self.assertEqual(by_id[self.no_nick.pk], "测试打手乙")

    def test_a_person_appearing_on_many_orders_is_listed_once(self):
        third = Order.objects.create(game=self.game, order_no="20260924000000000703")
        Performance.objects.create(order=third, user=self.nick, emp_type=EmpType.MAIN)
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        self.assertEqual([r["id"] for r in data["takers"]].count(self.nick.pk), 1)

    def test_a_sales_only_user_is_not_a_taker(self):
        sales_only = _user("测试客服甲")
        Performance.objects.create(order=self.order, user=sales_only, emp_type=EmpType.SALES)
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        self.assertNotIn(sales_only.pk, [r["id"] for r in data["takers"]])

    def test_an_order_without_a_publisher_contributes_no_publisher(self):
        self.assertIsNone(self.order_b.hall_publisher_id)      # 那一单确实没有发单人
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        # 只有 `self.order` 有发单人 ⇒ 名单**恰好**是他一个（没有发单人的那单不贡献候选人）。
        self.assertEqual([r["id"] for r in data["publishers"]], [self.nick.pk])


class FilterOptionsExclusionAndTenancyTests(TenantTestCase):
    """第 52 / 65 / 66 / 67 条：排除面、`?game=` 收窄、上界与截断、权限门。"""

    def setUp(self):
        self.game_x = Game.objects.create(name="测试游戏甲")
        self.game_y = Game.objects.create(name="测试游戏乙")
        self.taker_x = _user("测试打手甲")
        self.taker_y = _user("测试打手乙")
        # ⚠️ 订单上再挂一个**发单人**（M3）：`publisher_options` 的收窄判据需要这一侧真有
        #    候选人，否则「publishers 侧也过了同一道卡点」只能靠空集假通过。
        self.order_x = Order.objects.create(
            game=self.game_x, order_no=NO_A, hall_publisher=self.taker_x
        )
        self.order_y = Order.objects.create(
            game=self.game_y, order_no=NO_B, hall_publisher=self.taker_y
        )
        Performance.objects.create(order=self.order_x, user=self.taker_x, emp_type=EmpType.MAIN)
        Performance.objects.create(order=self.order_y, user=self.taker_y, emp_type=EmpType.MAIN)
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def test_only_the_deleted_order_does_not_contribute_a_candidate(self):
        self.order_x.deleted_at = timezone.now()
        self.order_x.save(update_fields=["deleted_at"])
        data = self.client.get("/api/business/orders/filter-options/").json()["data"]
        ids = [r["id"] for r in data["takers"]]
        self.assertNotIn(self.taker_x.pk, ids)     # 只挂在已删单上 ⇒ 不在名单
        self.assertIn(self.taker_y.pk, ids)        # 未删单上的仍在（对照半）

    def test_the_game_parameter_narrows_the_candidate_lists(self):
        ids = [
            r["id"]
            for r in self.client.get(
                f"/api/business/orders/filter-options/?game={self.game_x.pk}"
            ).json()["data"]["takers"]
        ]
        self.assertIn(self.taker_x.pk, ids)
        self.assertNotIn(self.taker_y.pk, ids)

    def test_without_the_game_parameter_both_games_are_included(self):
        ids = [
            r["id"]
            for r in self.client.get("/api/business/orders/filter-options/").json()["data"]["takers"]
        ]
        self.assertIn(self.taker_x.pk, ids)
        self.assertIn(self.taker_y.pk, ids)

    def test_the_game_permission_also_narrows_the_candidate_lists(self):
        """⭐ §5.6「过同一道数据权限卡点」的直接判据（⛔ 与 `?game=` 的显式收窄是两件事）。

        用**部分游戏授权**的账号（`games=[game_x]`）⇒ 只看得见 `game_x` ⇒ 只在 `game_y` 上
        出现过的那个人**不在**名单里。若实现漏了 `scope_queryset`，这条会红。
        ⭐ **两侧各一条判据**（M3）：`takers` 侧是「在 / 不在」，`publishers` 侧是**等值**
        （恰好只剩 `game_x` 上那个人）——两条各钉一个 `scope_queryset` 调用点。
        `publishers` 侧的判别力已实测：删掉 `publisher_options` 里那次 `scope_queryset`
        ⇒ 本条红（`[2, 1] != [1]`）。
        ⚠️ `FilterOptionsCrossTenantIsolationTests.test_every_candidate_id_filters_at_least_one_row`
        从另一面（候选 ↔ 筛选同源）也钉这件事。
        """
        partial = make_logged_in_client(
            username="测试受限客服甲",
            groups=("客服",),
            nodes=("order.list",),
            games=[self.game_x],
        )
        data = partial.get("/api/business/orders/filter-options/").json()["data"]
        ids = [r["id"] for r in data["takers"]]
        self.assertIn(self.taker_x.pk, ids)
        self.assertNotIn(self.taker_y.pk, ids)
        # ⚠️ publisher 侧同样收窄（`publisher_options` 自己那次 `scope_queryset`）。
        self.assertEqual([r["id"] for r in data["publishers"]], [self.taker_x.pk])

    def test_the_endpoint_needs_order_list_and_is_401_when_anonymous(self):
        from rest_framework.test import APIClient
        anon = APIClient(SERVER_NAME="tenant.test.com")
        self.assertEqual(anon.get("/api/business/orders/filter-options/").status_code, 401)
        no_perm = make_logged_in_client(username="测试无权限甲", nodes=())
        self.assertEqual(
            no_perm.get("/api/business/orders/filter-options/").status_code, 403
        )

    def test_limit_over_the_cap_is_1000_and_truncation_is_reported_both_ways(self):
        self.assertEqual(
            self.client.get("/api/business/orders/filter-options/?limit=501").json()["code"], 1000
        )
        tight = self.client.get("/api/business/orders/filter-options/?limit=1").json()["data"]
        self.assertTrue(tight["takers_truncated"])
        self.assertEqual(len(tight["takers"]), 1)
        roomy = self.client.get("/api/business/orders/filter-options/?limit=500").json()["data"]
        self.assertFalse(roomy["takers_truncated"])


class TakerFilterDoesNotFanOutTests(TenantTestCase):
    """第 53 / 54 / 55 / 56 / 57 / 58 条：两档筛选的承重判据。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(game=self.game, order_no=NO_A)
        self.taker = _user("测试打手甲")
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def test_a_second_main_row_for_the_same_user_is_rejected_by_uniq_performance_order_user(self):
        """⚠️ **归因**：同一单、**同一个用户**建第二条 MAIN 行 ⇒ 实际命中的是
        `uniq_performance_order_user`（`fields=("order", "user")`），**不是**
        `uniq_order_main_taker`——后者是另一条用例（换一个用户）才拿得到的归因。
        实测报错文本含 `unique constraint "uniq_performance_order_user"`。
        """
        Performance.objects.create(order=self.order, user=self.taker, emp_type=EmpType.MAIN)
        with self.assertRaises(IntegrityError) as ctx:
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order, user=self.taker, emp_type=EmpType.MAIN
                )
        self.assertIn("uniq_performance_order_user", str(ctx.exception))

    def test_a_second_main_row_for_another_user_is_rejected_by_uniq_order_main_taker(self):
        """⭐ T11-53 的计划夹具（同一单两行 MAIN）**在本仓不可能** —— 这一条把「不可能」钉死。

        `Performance.Meta.constraints` 的 `uniq_order_main_taker`（`fields=("order",)`、
        `condition=Q(emp_type=MAIN)`）保证一条订单最多一条 MAIN 行；第二条 `create()` 直接
        `IntegrityError`。这正是 `taker_user` 那条 join **在 MAIN 维度上不可能放大行**的结构原因
        （也是本仓 `search_hall()` docstring 自己写下的口径）。

        ⚠️ **必须换另一个用户**：同用户第二条 MAIN 行会先撞 `uniq_performance_order_user`
        ⇒ 拿不到本约束的归因（实测文本见上一条）。本用例要支撑的是「MAIN 维度不可能放大行」
        那个降级说明 ⇒ 它必须让 `IntegrityError` 文本真的含 `uniq_order_main_taker`。
        """
        other = _user(TAKER_A2)
        Performance.objects.create(order=self.order, user=self.taker, emp_type=EmpType.MAIN)
        with self.assertRaises(IntegrityError) as ctx:
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order, user=other, emp_type=EmpType.MAIN
                )
        self.assertIn("uniq_order_main_taker", str(ctx.exception))

    def test_one_order_with_two_performance_rows_still_returns_one_row_and_the_same_total(self):
        """第 53 条的**可实测替代**：一单两行绩效（一条 MAIN + 一条别人的 SALES）⇒ 仍恰好一行、
        分页 `total` 与不筛时相同。⚠️ 这里两行的 `emp_type` 不同，所以它钉的是
        「实现按 MAIN 精确匹配、且不因**别的**绩效行把订单放大」，不是计划里那句
        「两行 MAIN」（那句与 DB 约束矛盾，见上一条）。

        ⚠️ **本用例不区分 `Exists` 与 `join+distinct`**（实测：把实现换成
        `qs.filter(performances__emp_type=MAIN, performances__user_id=…).distinct()`
        后本模块全绿）；它钉的是**返回值不放大**这个结果，⛔ 不是实现形状的承重证据。
        """
        other_user = _user(TAKER_A2)
        Performance.objects.create(order=self.order, user=self.taker, emp_type=EmpType.MAIN)
        Performance.objects.create(order=self.order, user=other_user, emp_type=EmpType.SALES)
        unfiltered = self.client.get("/api/business/orders/").json()["data"]["total"]
        body = self.client.get(f"/api/business/orders/?taker_user={self.taker.pk}").json()["data"]
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["total"], unfiltered)

    def test_a_sales_only_user_matches_no_row(self):
        Performance.objects.create(order=self.order, user=self.taker, emp_type=EmpType.SALES)
        body = self.client.get(f"/api/business/orders/?taker_user={self.taker.pk}").json()["data"]
        self.assertEqual(body["total"], 0)

    def test_publisher_is_matched_by_user_id_not_by_name(self):
        # ⚠️ 判据要能杀「按名字串匹配」的退化实现 ⇒ 两个账号的**昵称相同**、用户名不同；
        #    退化成 `hall_publisher__first_name=<name>` 时，按 B 的 id 筛会**错误命中** A 的单。
        same_name_a = _user("测试同名甲")
        same_name_b = _user("测试同名乙")
        for user in (same_name_a, same_name_b):
            user.first_name = "测试同名"
            user.save(update_fields=["first_name"])
        self.order.hall_publisher = same_name_a
        self.order.save(update_fields=["hall_publisher"])
        hit = self.client.get(
            f"/api/business/orders/?hall_publisher_user={same_name_a.pk}"
        ).json()["data"]
        miss = self.client.get(
            f"/api/business/orders/?hall_publisher_user={same_name_b.pk}"
        ).json()["data"]
        self.assertEqual(hit["total"], 1)
        self.assertEqual(miss["total"], 0)   # 退化成按名字文本匹配 ⇒ 这条会红

    def test_the_two_branches_can_be_combined_as_and(self):
        """第 57 条**改准**（2026-10-26，D-0311）：两档**可共存**，语义是 AND。

        ⚠️ **判据必须能区分 AND 与「任取一个 / 忽略一个」**：只断 `code=0` 的话，那种实现
        也会绿。造**三张单**——① 只满足 `taker_user`（接手人 = A，抛单人 = C）；
        ② 只满足 `hall_publisher_user`（接手人 = D，抛单人 = B）；③ 两者都满足。
        请求 `taker_user=A&hall_publisher_user=B` ⇒ 结果**恰好 1 条、就是第三张**。
        两条单参数请求各命中 **2 张**（防「两个参数都被忽略、恰好空集」的假通过）。
        """
        publisher_b = _user("测试发单人乙")
        publisher_c = _user("测试发单人丙")
        taker_d = _user(TAKER_A2)
        both = Order.objects.create(
            game=self.game, order_no="20260924000000000721", hall_publisher=publisher_b
        )
        taker_only = Order.objects.create(
            game=self.game, order_no="20260924000000000722", hall_publisher=publisher_c
        )
        publisher_only = Order.objects.create(
            game=self.game, order_no="20260924000000000723", hall_publisher=publisher_b
        )
        Performance.objects.create(order=both, user=self.taker, emp_type=EmpType.MAIN)
        Performance.objects.create(order=taker_only, user=self.taker, emp_type=EmpType.MAIN)
        Performance.objects.create(order=publisher_only, user=taker_d, emp_type=EmpType.MAIN)

        combined = self.client.get(
            f"/api/business/orders/?taker_user={self.taker.pk}"
            f"&hall_publisher_user={publisher_b.pk}"
        ).json()
        self.assertEqual(combined["code"], 0, combined.get("msg"))
        self.assertEqual(combined["data"]["total"], 1)
        self.assertEqual(
            [row["order_no"] for row in combined["data"]["items"]], [both.order_no]
        )

        # 反向对照：两条单参数各自命中 2 张 ⇒ 上面的「恰好 1 条」不是靠空集/忽略参数得来的。
        taker_only_body = self.client.get(
            f"/api/business/orders/?taker_user={self.taker.pk}"
        ).json()
        self.assertEqual(taker_only_body["code"], 0, taker_only_body.get("msg"))
        self.assertEqual(taker_only_body["data"]["total"], 2)
        publisher_only_body = self.client.get(
            f"/api/business/orders/?hall_publisher_user={publisher_b.pk}"
        ).json()
        self.assertEqual(publisher_only_body["code"], 0, publisher_only_body.get("msg"))
        self.assertEqual(publisher_only_body["data"]["total"], 2)

    def test_publisher_stacks_with_the_source_filter_as_and(self):
        """第 ③ 条（本分支的等价落点）：`hall_publisher_user` 与既有独立筛选叠加 = AND。

        ⚠️ 计划原文写「与 `source_group` 叠加」，而 `source_group` 属于并行的「订单分组」线，
        **本分支还没有这个参数**（全仓没有 `source_group`）⇒ 判据落在现存的独立筛选
        `order_source` 上（同一类 AND 叠加：`order_source_id=` + `hall_publisher_id=`）；
        分组线合并后由合并方在合并树上同形补一条（本线交付说明已登记残差）。

        三张单：只满足来源 / 只满足发单人 / 两者都满足 ⇒ 两个条件一起给恰好回第三张。
        """
        source_x = OrderSource.objects.create(name="测试来源甲")
        source_y = OrderSource.objects.create(name="测试来源乙")
        publisher = _user("测试发单人甲")
        other_publisher = _user("测试发单人乙")
        both = Order.objects.create(
            game=self.game,
            order_no="20260924000000000731",
            order_source=source_x,
            hall_publisher=publisher,
        )
        # 两张阴性对照：只满足来源 / 只满足发单人。
        Order.objects.create(
            game=self.game,
            order_no="20260924000000000732",
            order_source=source_x,
            hall_publisher=other_publisher,
        )
        Order.objects.create(
            game=self.game,
            order_no="20260924000000000733",
            order_source=source_y,
            hall_publisher=publisher,
        )
        body = self.client.get(
            f"/api/business/orders/?hall_publisher_user={publisher.pk}"
            f"&order_source={source_x.pk}"
        ).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual([row["order_no"] for row in body["data"]["items"]], [both.order_no])
        # 反向对照：两个单条件各命中 2 张（同一条 AND 判据的负半）。
        self.assertEqual(
            self.client.get(
                f"/api/business/orders/?hall_publisher_user={publisher.pk}"
            ).json()["data"]["total"],
            2,
        )
        self.assertEqual(
            self.client.get(
                f"/api/business/orders/?order_source={source_x.pk}"
            ).json()["data"]["total"],
            2,
        )

    def test_illegal_ids_are_1000_and_are_not_silently_treated_as_missing(self):
        """第 58 条：**两条腿都测**（`taker_user` 与 `hall_publisher_user`）。

        ⚠️ 原用例只测 `taker_user`：`hall_publisher_user` 的 `min_value=1` 被删掉时它照样绿
        ⇒ 本用例把参数名逐个点名（`0` / `abc` / `-1` × 两条腿）。
        """
        baseline = self.client.get("/api/business/orders/").json()["data"]["total"]
        for branch in ("taker_user", "hall_publisher_user"):
            for bad in ("abc", "0", "-1"):
                with self.subTest(branch=branch, bad=bad):
                    resp = self.client.get(f"/api/business/orders/?{branch}={bad}").json()
                    self.assertEqual(resp["code"], 1000)
                    # 非法值不许落成「没给这个条件」（那会静默全量）。
                    self.assertIsNone(resp["data"])
        # ⚠️ 同时断言**不是静默全量**：拿「对照请求」比（对照 = 不传该参数）。
        self.assertEqual(self.client.get("/api/business/orders/").json()["data"]["total"], baseline)


class FilterOptionsCrossTenantIsolationTests(TenantTestCase):
    """第 68 / 69 条：候选名单的跨租户隔离 + 「候选与筛选同源」。

    ⚠️ **不用主键指认「另一个租户的那一行」**（`AGENTS.md` §5-G）：两个 schema 的主键序列
    各自从 1 开始 ⇒ B 的用户 id 在 A 里会撞上 A 自己的行。判据一律用**用户名 / 订单号**
    这类租户内唯一、且本例两租户取值不同的字段。

    - **68**：A 的 `filter-options` 名单不含 B 的人（先证明 A 自己非空，防「两边都空」假通过）；
      A 拿 **B 的用户 id** 去打列表筛选 ⇒ 结果里**不出现 B 的 `order_no`**
      （⛔ **不断言 `total == 0`**——见上，B 的 id 可能撞上 A 自己的行）。
    - **69**：遍历名单里**每一个** id（takers 与 publishers 全量，⛔ 不是抽样），
      用它筛列表必须**至少 1 行**。本类 viewer 只被授 `game_a1`
      ⇒ 「候选名单过了 `scope_queryset` / 游戏范围，列表筛选也过同一道卡点」这件事
      在同一条用例里被钉住：删掉 `taker_options` / `publisher_options` 里的
      `qs = scope_queryset(...)` ⇒ 名单里会多出 `game_a2` 上的人 ⇒ 本条红。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建（用例内部的 schema DDL 与外层事务相冲，
        #    与 `tests/test_order_source_isolation.py` 同一配方）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="候选名单隔离租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据 ⇒ 不能假定本次一定是新建。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（schema 上下文会跨用例残留）。
        connection.set_tenant(self.tenant)
        self.game_a1 = Game.objects.create(name="测试游戏甲")
        self.game_a2 = Game.objects.create(name="测试游戏乙")
        self.taker_a1 = _user(TAKER_A1)
        self.taker_a2 = _user(TAKER_A2)
        self.order_a1 = Order.objects.create(
            game=self.game_a1, order_no=NO_ISO_A1, hall_publisher=self.taker_a1
        )
        self.order_a2 = Order.objects.create(
            game=self.game_a2, order_no=NO_ISO_A2, hall_publisher=self.taker_a2
        )
        Performance.objects.create(
            order=self.order_a1, user=self.taker_a1, emp_type=EmpType.MAIN
        )
        Performance.objects.create(
            order=self.order_a2, user=self.taker_a2, emp_type=EmpType.MAIN
        )
        # ⭐ viewer 只被授 `game_a1` ⇒ `game_a2` 上的人既不该进名单、也不该被列表筛出来
        #    （T11-69 的判别力来源，见类 docstring）。
        self.client_a = make_logged_in_client(
            username="测试候选客服甲",
            groups=("客服",),
            nodes=("order.list",),
            games=[self.game_a1],
        )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="测试游戏丙")
            self.taker_b = User.objects.create_user(username=TAKER_B)
            order_b = Order.objects.create(
                game=game_b, order_no=NO_ISO_B, hall_publisher=self.taker_b
            )
            Performance.objects.create(
                order=order_b, user=self.taker_b, emp_type=EmpType.MAIN
            )
            # B 侧自证：这一行**确实存在于 B**（下面的否定断言才有意义）。
            self.assertTrue(Order.objects.filter(order_no=NO_ISO_B).exists())

    def _options(self):
        body = self.client_a.get("/api/business/orders/filter-options/").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return body["data"]

    def test_a_does_not_see_b_in_its_candidate_lists(self):
        data = self._options()
        names = [r["name"] for r in data["takers"]] + [r["name"] for r in data["publishers"]]
        # 非空哨兵：A 自己那份名单**确实有人**，否则下面的否定断言会因「两边都空」假通过。
        self.assertIn(TAKER_A1, names)
        # 判据（用户名，租户内唯一、两租户取值不同；⛔ 不用主键）：
        self.assertNotIn(TAKER_B, names)

    def test_filtering_by_b_user_id_returns_no_order_of_b(self):
        # 对照半：A 不筛任何参数时，列表**恰好**是 A 自己看得见的那一单。
        all_nos = [
            r["order_no"]
            for r in self.client_a.get("/api/business/orders/").json()["data"]["items"]
        ]
        self.assertEqual(all_nos, [NO_ISO_A1])
        # 判据：拿 B 的用户 id 筛（主键只在 B 里唯一，在 A 里可能撞上 A 自己的行）。
        body = self.client_a.get(f"/api/business/orders/?taker_user={self.taker_b.pk}").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        nos = [r["order_no"] for r in body["data"]["items"]]
        # ⛔ **不断言 `total == 0`**（§5-G）：本行只钉「B 的单不出现」。
        self.assertNotIn(NO_ISO_B, nos)
        # 更强的形状断言：结果里**只能**是 A 自己看得见的那一单，不可能是别的租户的行。
        self.assertTrue(set(nos) <= {NO_ISO_A1})

    def test_every_candidate_id_filters_at_least_one_row(self):
        """T11-69：候选与筛选同源（防「选得到、筛不出」）。"""
        data = self._options()
        takers = [r["id"] for r in data["takers"]]
        publishers = [r["id"] for r in data["publishers"]]
        # 非空哨兵 + 判别力：名单**恰好**是 A 里 `game_a1` 上那个人
        # （`game_a2` 上的人被数据权限挡住；⛔ 不是空集、也不是两个人）。
        self.assertEqual(takers, [self.taker_a1.pk])
        self.assertEqual(publishers, [self.taker_a1.pk])
        for user_id in takers:
            with self.subTest(branch="taker_user", user_id=user_id):
                body = self.client_a.get(f"/api/business/orders/?taker_user={user_id}").json()
                self.assertEqual(body["code"], 0, body.get("msg"))
                self.assertGreaterEqual(len(body["data"]["items"]), 1)
        for user_id in publishers:
            with self.subTest(branch="hall_publisher_user", user_id=user_id):
                body = self.client_a.get(
                    f"/api/business/orders/?hall_publisher_user={user_id}"
                ).json()
                self.assertEqual(body["code"], 0, body.get("msg"))
                self.assertGreaterEqual(len(body["data"]["items"]), 1)


class FilterOptionsSearchTests(TenantTestCase):
    """`?search=`（2026-11-09「下拉改远程关键词搜索」线，计划 §3.3）：显示名 icase 包含。

    夹具（合成值；显示名 = 昵称优先，见 `FilterOptionsShapeTests` 同一条口径）：
    - `sq1551`（game_x 上接过单 + 是 game_x 那单的发单人）；
    - `sq13717`（只在 game_x 上接过单）；
    - 另一个 `sq1551`（只在 game_y 上接过单 + 是 game_y 那单的发单人）
      —— 两个租户内不同的人**同显示名**，既钉 icase 命中的「包含」语义，
      又给「search × game 叠加」一条能区分「只叠加了一半」的判据
      （单独 search 或单独 game 都比叠加结果大，见对应用例）。
    """

    def setUp(self):
        self.game_x = Game.objects.create(name="测试游戏甲")
        self.game_y = Game.objects.create(name="测试游戏乙")
        self.taker_s1 = self._named_taker("测试打手甲", "sq1551")
        self.taker_s2 = self._named_taker("测试打手乙", "sq13717")
        self.taker_y = self._named_taker("测试打手丙", "sq1551")
        order_x = Order.objects.create(
            game=self.game_x, order_no="20260924000000000741", hall_publisher=self.taker_s1
        )
        order_x2 = Order.objects.create(game=self.game_x, order_no="20260924000000000742")
        order_y = Order.objects.create(
            game=self.game_y, order_no="20260924000000000743", hall_publisher=self.taker_y
        )
        Performance.objects.create(order=order_x, user=self.taker_s1, emp_type=EmpType.MAIN)
        Performance.objects.create(order=order_x2, user=self.taker_s2, emp_type=EmpType.MAIN)
        Performance.objects.create(order=order_y, user=self.taker_y, emp_type=EmpType.MAIN)
        self.client = make_logged_in_client(groups=("客服",), nodes=("order.list",))

    def _named_taker(self, username, first_name):
        user = _user(username)
        user.first_name = first_name
        user.save(update_fields=["first_name"])
        return user

    def _data(self, query=""):
        body = self.client.get(f"/api/business/orders/filter-options/{query}").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return body["data"]

    def test_search_hits_the_display_name_case_insensitively(self):
        """计划 §3.3-①：search 命中显示名；`SQ1551`（大写）命中 `sq1551`（icase）。

        ⚠️ 两个**不同的人**（game_x 的甲、game_y 的丙）显示名同为 `sq1551` ⇒ 不带 `game`
        的检索两人都命中（名字列上的 icase 包含，不知道也不关心游戏）；
        `sq13717`（乙）不在其中——这半是「匹配的是显示名」的阴性对照。
        """
        data = self._data("?search=SQ1551")
        self.assertEqual({r["name"] for r in data["takers"]}, {"sq1551"})
        self.assertEqual(
            {r["id"] for r in data["takers"]},
            {self.taker_s1.pk, self.taker_y.pk},
        )
        # publishers 同一面（同一次请求里的两份名单都过了同一个 search）。
        self.assertEqual(
            {r["id"] for r in data["publishers"]},
            {self.taker_s1.pk, self.taker_y.pk},
        )

    def test_search_and_game_stack_as_and(self):
        """计划 §3.3-②：search 与 `game` 叠加 = AND。

        判别力（防「只叠加了一半」的假通过）：单独 `search=sq1551` 命中两个人
        （game_x 的甲 + game_y 的丙）；单独 `game=game_x` 也命中两个人（甲 + 乙）；
        叠加后**恰好一人**（甲）。任一侧漏掉，这三行里必有一行红。
        """
        search_alone = {r["id"] for r in self._data("?search=sq1551")["takers"]}
        self.assertEqual(search_alone, {self.taker_s1.pk, self.taker_y.pk})
        game_alone = {r["id"] for r in self._data(f"?game={self.game_x.pk}")["takers"]}
        self.assertEqual(game_alone, {self.taker_s1.pk, self.taker_s2.pk})
        combined = self._data(f"?search=sq1551&game={self.game_x.pk}")
        self.assertEqual([r["id"] for r in combined["takers"]], [self.taker_s1.pk])
        self.assertEqual([r["id"] for r in combined["publishers"]], [self.taker_s1.pk])

    def test_a_search_with_no_hits_returns_empty_lists_and_no_truncation(self):
        """计划 §3.3-③：无命中 ⇒ 两份名单空数组（不是 null）+ 两个 truncated 都 False。"""
        data = self._data("?search=sq99999")
        self.assertEqual(data["takers"], [])
        self.assertEqual(data["publishers"], [])
        self.assertIs(data["takers_truncated"], False)
        self.assertIs(data["publishers_truncated"], False)

    def test_without_search_the_results_are_exactly_as_before_the_parameter(self):
        """计划 §3.3-④：无 search ⇒ 与改造前完全一致（回归钉）。

        全量口径的两半（三人都进 takers、两个发单人都进 publishers）既有用例已各自钉过
        （`FilterOptionsShapeTests` / `FilterOptionsExclusionAndTenancyTests`，一条没改）；
        这里补的是**新契约**的另一半：`?search=`（空串）也视同未传，与不传逐字同形。
        """
        without = self._data()
        blank = self._data("?search=")
        self.assertEqual(blank, without)
        self.assertEqual(
            {r["id"] for r in blank["takers"]},
            {self.taker_s1.pk, self.taker_s2.pk, self.taker_y.pk},
        )

    def test_truncation_is_still_reported_when_searching(self):
        """计划 §3.3-⑤：search 命中数超过 `limit` ⇒ truncated 仍按 `limit + 1` 判。"""
        data = self._data("?search=sq&limit=1")
        self.assertEqual(len(data["takers"]), 1)
        self.assertIs(data["takers_truncated"], True)
        self.assertEqual(len(data["publishers"]), 1)
        self.assertIs(data["publishers_truncated"], True)
