"""大厅读口 1 秒短缓存的验收网（spec `reviews-hall-short-cache-20261031` §5）。

两个读口（`/api/business/hall/`、`/api/taker/v1/hall/`）接了进程内短 TTL 缓存
（`apps/core/short_cache.py`）。本文件钉九件事：

| # | 判据 | spec §5 |
|---|---|---|
| 1 | 命中：同用户同参数第二次 GET 的取数面整体消失、响应体逐键相等（web / taker 各一条） | 1 |
| 2 | 按人隔离·免押：模拟卡维度在键里，非免押者与免押者互不命中、互不毒化 | 2 |
| 3 | 按人隔离·游戏权限：C 无游戏 X 权限 ⇒ 吃不到 A（有权限）的缓存（含镜像行一路） | 3 |
| 4 | 按人隔离·价格脱敏：受限者 `hall_price=null`，互相预热后两边的值各自正确 | 4 |
| 5 | taker 按人隔离：两个 taker（一个免押）各拿各的，互不串 | 5 |
| 6 | 参数隔离：`?game=` 不同值 / 不同 `page` ⇒ 各自正确，不串页 | 6 |
| 7 | 紧急开关：`HALL_LIST_CACHE_TTL_S=0` ⇒ 改库立即可见 + 零缓存读写 | 7 |
| 8 | TTL 过期：到期后重算（拨 `_expire_info` 模拟时间前进，⛔ 不 sleep） | 8 |
| 9 | 键带 schema 前缀（防跨租户） | 9 |

夹具照既有大厅测试的现成写法抄（`tests/test_hall_api.py` / `tests/test_takerapi_hall_api.py`
/ `tests/test_hall_simulated.py`），⛔ 不自造第二套形状。

⚠️ **缓存不随 `TestCase` 的数据库回滚**（locmem 住在进程里）⇒ 每个用例类的 `setUp`
都先 `caches["hall_list"].clear()`，让每条用例从空缓存出发。

⚠️ **查询数判据用相对口径**（照 `tests/test_account_api.py` / `test_jidanpu_upstream_offer_price.py`
的既有先例）：`CaptureQueriesContext` 只数 `SELECT`——`assertNumQueries` 的总数会把
django-tenants 的 `SET search_path` 也算进去。

⚠️ 本文件里所有依赖「缓存真的开着」的用例都**显式** `override_settings` 一个大于 0 的
TTL：它们钉的是「命中 / 分键」这件事，不是「默认值恰好是 1 秒」——顺带让判据在慢机器上
不被 1 秒真实时间的流逝打红（TTL 过期另有专门用例钉）。
"""
import json
import re
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.core.cache import caches
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    HallSimulatedOrder,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.roles import TENANT_ADMIN
from apps.core import short_cache
from apps.rbac.models import PermissionNode, Role
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import make_logged_in_client, permission_select_counts

HALL_URL = "/api/business/hall/"
TAKER_HALL_PATH = "/api/taker/v1/hall/"
TOKEN_PATH = "/api/taker/oauth/token"

#: 文档保留段 `203.0.113.0/24`（照 `tests/test_takerapi_hall_api.py`，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.42"

#: web 大厅的门（照 `tests/test_hall_api.py::HALL_NODES` 抄齐：看大厅 + 造单需要的全套；
#: 本文件只**读**大厅，最小集是 `grab_hall.take_order`，多声明的不参与判据）。
WEB_HALL_NODE = "grab_hall.take_order"
#: 价格脱敏 flag（照 `tests/test_hall_api.py::test_gray_rows_are_price_redacted_for_restricted_viewers`）。
RESTRICT_OFFER_PRICE = "order.restrict_view_offer_price"

# ── 合成凭据（照 test_takerapi_hall_api 的形状；⛔ 与任何真实租户无关）──────────
TAKER_HIT_KEY = "tshortcachekey00001"
TAKER_HIT_SECRET = "t-short-cache-secret-000000001"
TAKER_A_KEY = "tshortcachekey00002"
TAKER_A_SECRET = "t-short-cache-secret-000000002"
TAKER_B_KEY = "tshortcachekey00003"
TAKER_B_SECRET = "t-short-cache-secret-000000003"


def _sim_payload(no, game_id, price):
    """模拟卡的全形状 payload（照 `tests/test_hall_api.py::_sim_payload` 抄）。"""
    from apps.business.jidanpu_common import REMOTE_ROW_BASE

    data = dict(REMOTE_ROW_BASE)
    data.update(
        {
            "order_no": no, "game": game_id, "status": "WAITING_GRAB",
            "hall_price": price, "origin": "simulated", "is_preferred": True,
            "customer_phone": "", "customer_ww": "", "game_role_name": "",
        }
    )
    return data


class _WebHallFixture(TenantTestCase):
    """web 侧共用夹具：两款游戏 + 造单 + 真实登录 + AppToken（照 test_hall_api 抄）。"""

    def setUp(self):
        # ⚠️ locmem 不随数据库回滚 ⇒ 每个用例从空缓存出发，防上一条用例的键漏进来。
        caches["hall_list"].clear()
        self.game1 = Game.objects.create(name="短缓存游戏甲")
        self.game2 = Game.objects.create(name="短缓存游戏乙")
        self.url = HALL_URL

    # ── 造物（照 test_hall_api 的 `_order`：单 + 无主 MAIN 绩效 = 真的在厅）─────
    def _order(self, game, no, *, price="88.00", status=OrderStatus.WAITING_GRAB):
        order = Order.objects.create(
            game=game, order_no=no, status=status, hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price),
        )
        return order

    def _viewer(self, username, games, *, extra_nodes=(), group="代练员"):
        """真实登录的查看者。⚠️ `Role.name` 的 `max_length=5`（`apps/rbac/models.py:46`）
        ⇒ 组名最多 5 字；**持限制 flag 的账号必须单独给组名**（flag 挂组→角色上，
        与对照账号共用组名会把限制带给对方——test_hall_api 灰卡用例的同一条教训）。"""
        return make_logged_in_client(
            username=username,
            groups=(group,),
            games=games,
            nodes=(WEB_HALL_NODE,) + extra_nodes,
        )

    def _app_token(self, username):
        """给已有用户配一枚 AppToken（照 `test_hall_api._app_body`；模拟卡只在 App 面可见）。"""
        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token, new_token

        raw = new_token()
        AppToken.objects.create(
            user=User.objects.get(username=username),
            key_hash=hash_token(raw),
            client="short-cache-app",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return raw

    def _app_get(self, token, params=None):
        client = Client(SERVER_NAME="tenant.test.com")
        return client.get(self.url, params or {}, HTTP_AUTHORIZATION="Bearer " + token)

    def _sim(self, no, *, price="150.00"):
        """一张在厅模拟卡（照 `test_hall_api.py::HallPreferredFilterTests._sim` 抄）。"""
        return HallSimulatedOrder.objects.create(
            game=self.game1, display_order_no=no,
            project_text="短缓存模拟卡", price=Decimal(price),
            status=HallSimulatedOrder.Status.ACTIVE,
            generated_at=timezone.now(),
            payload=_sim_payload(no, self.game1.pk, price),
        )

    def _upstream_mirror(self, game, order_id):
        """一条鸡蛋铺镜像行（照 `test_hall_api.py::HallMetaKeysTests._upstream` 抄）。"""
        from apps.business.models import JidanpuUpstreamBinding, JidanpuUpstreamHallOrder

        owner = User.objects.create_user(username=f"{order_id}-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url="https://short-cache.test.invalid",
            account="acc-short-cache", password="pw-canary-short-cache", enabled=True,
        )
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=binding.jidanpu_base_url, upstream_order_id=order_id,
            owner_binding=binding, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="短缓存镜像合成项目", remote_rec_fee=Decimal("66.00"),
            upstream_payload={
                "orderId": order_id, "orderIdView": f"V-{order_id}", "recFee": "66.00",
            },
            first_seen_at=timezone.now(),
        )

    @staticmethod
    def _items(response):
        return response.json()["data"]["items"]

    @staticmethod
    def _selects(ctx):
        """上下文里的 `SELECT` 语句（相对口径的判据面；SET search_path 不算）。"""
        return [
            q for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT")
        ]

    def _claim(self, order, username):
        """把一张在厅单接走（「已接走」口径照 test_hall_api：状态迁走 + MAIN 行挂人）。"""
        taker = User.objects.create_user(username=username)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status"])
        Performance.objects.filter(order=order).update(user=taker)
        return taker


class HallWebCacheHitTests(_WebHallFixture):
    """spec §5-1（web）：命中 = 第二次的取数面消失 + 响应体逐键相等。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_second_request_serves_the_cached_payload(self):
        self.client = self._viewer("web-hit-viewer", [self.game1, self.game2])
        first_order = self._order(self.game1, "20261031000000000001")
        self._order(self.game2, "20261031000000000002")

        with CaptureQueriesContext(connection) as first_ctx:
            first = self.client.get(self.url)
        with CaptureQueriesContext(connection) as second_ctx:
            second = self.client.get(self.url)

        # 命中：第二次的 SELECT 显著少于第一次（miss = 四路取数 + 序列化，hit = 只算指纹）。
        first_selects = self._selects(first_ctx)
        second_selects = self._selects(second_ctx)
        self.assertLess(
            len(second_selects), len(first_selects),
            f"第二次请求的查询数没有下降（{len(first_selects)} → {len(second_selects)}），缓存没接上",
        )
        # 更尖的钉子：命中路径**连 orders 表都不该碰**（指纹只查权限面；
        # 若这条红了但上面那条绿，说明命中后的请求仍在偷偷取数）。
        self.assertEqual(
            [q["sql"][:120] for q in second_ctx.captured_queries
             if "business_order" in q["sql"]],
            [],
            "缓存命中的请求不应该再查订单表",
        )
        # 响应体逐键相等（缓存的应该就是同一个载荷）。
        self.assertEqual(first.json(), second.json())
        # 前置哨兵：列表里真的有那张单（防「大厅恒空」的假绿，E-0004）。
        self.assertIn(first_order.order_no, [r["order_no"] for r in self._items(first)])


class HallWebPermissionReadsTests(_WebHallFixture):
    @override_settings(HALL_LIST_CACHE_TTL_S=0)
    def test_get_reads_admin_group_and_permission_nodes_once_each(self):
        viewer = self._viewer("hall-query-viewer", [self.game1])
        order = self._order(self.game1, "20261031000000000201")

        with CaptureQueriesContext(connection) as queries:
            response = viewer.get(self.url, {"include_exited": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["order_no"] for row in self._items(response)], [order.order_no])
        self.assertEqual(
            permission_select_counts(queries.captured_queries),
            {"admin_group": 1, "permission_nodes": 1},
        )

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_next_get_reads_price_restriction_added_to_the_role(self):
        viewer = self._viewer("hall-role-viewer", [self.game1], group="大厅调价")
        self._order(self.game1, "20261031000000000202", price="88.00")
        first = viewer.get(self.url)
        self.assertEqual([row["hall_price"] for row in self._items(first)], ["88.00"])

        role = Role.objects.get(name="大厅调价")
        role.permissions.add(PermissionNode.objects.get(code=RESTRICT_OFFER_PRICE))

        second = viewer.get(self.url)
        self.assertEqual([row["hall_price"] for row in self._items(second)], [None])

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_next_get_loses_hall_access_after_admin_position_is_removed(self):
        viewer = self._viewer("hall-admin-viewer", [self.game1], group=TENANT_ADMIN)
        self._order(self.game1, "20261031000000000203")
        self.assertEqual(viewer.get(self.url).status_code, 200)

        admin_group = Group.objects.get(name=TENANT_ADMIN)
        admin_group.user_set.remove(User.objects.get(username="hall-admin-viewer"))

        self.assertEqual(viewer.get(self.url).status_code, 403)


class _TakerHallFixture(TenantTestCase):
    """taker 侧共用夹具（照 test_takerapi_hall_api 的 `_HallFixture` 抄）。"""

    def setUp(self):
        caches["hall_list"].clear()
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game1 = Game.objects.create(name="短缓存打手游戏甲")
        self.game2 = Game.objects.create(name="短缓存打手游戏乙")

    def _make_taker(self, username, *, games, nodes=(WEB_HALL_NODE,)):
        """普通打手 + 显式游戏权限（照 test_takerapi_hall_api 的 `_make_taker`；要测无节点显式传 `nodes=()`）。"""
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        user = User.objects.create_user(username=username, password="pw-12345678")
        StaffProfile.objects.create(user=user).games.set(games)
        if nodes:
            # ⚠️ 组名 ≤5 字（`Role.name` 的 `max_length=5`，`apps/rbac/models.py:46`）。
            group, _ = Group.objects.get_or_create(name="短缓节点")
            user.groups.add(group)
            grant_nodes(user, *nodes)
        return user

    def _make_app(self, name, app_key, app_secret, taker):
        return TakerApp.objects.create(
            name=name, app_key=app_key, app_secret=app_secret,
            taker=taker, created_by=taker,
        )

    def _token(self, app) -> str:
        """令牌一律从取令牌端点换（照 test_takerapi_hall_api 的 `issue_token`）。"""
        response = self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": "client_credentials",
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        return json.loads(response.content)["access_token"]

    def _hall(self, token, params=None):
        return self.anonymous.get(
            TAKER_HALL_PATH, params or {},
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
        )

    @staticmethod
    def _nos(response):
        return [row["order_no"] for row in response.json()["data"]["results"]]

    def _order(self, game, no, *, price="88.00"):
        order = Order.objects.create(
            game=game, order_no=no, status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price),
        )
        return order

    @staticmethod
    def _selects(ctx):
        return [
            q for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT")
        ]


class HallTakerCacheHitTests(_TakerHallFixture):
    """spec §5-1（taker）：命中 = 第二次的取数面消失 + 响应体逐键相等。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_second_request_serves_the_cached_rows(self):
        taker = self._make_taker("taker-hit", games=[self.game1])
        app = self._make_app("短缓存命中应用", TAKER_HIT_KEY, TAKER_HIT_SECRET, taker)
        token = self._token(app)
        order = self._order(self.game1, "20261031000000000101")

        with CaptureQueriesContext(connection) as first_ctx:
            first = self._hall(token)
        with CaptureQueriesContext(connection) as second_ctx:
            second = self._hall(token)

        first_selects = self._selects(first_ctx)
        second_selects = self._selects(second_ctx)
        self.assertLess(
            len(second_selects), len(first_selects),
            f"第二次请求的查询数没有下降（{len(first_selects)} → {len(second_selects)}），缓存没接上",
        )
        self.assertEqual(
            [q["sql"][:120] for q in second_ctx.captured_queries
             if "business_order" in q["sql"]],
            [],
            "缓存命中的请求不应该再查订单表",
        )
        self.assertEqual(first.json(), second.json())
        # 前置哨兵（E-0004）：正对照——那张单真的在两份响应里。
        self.assertIn(order.order_no, self._nos(first))
        self.assertIn(order.order_no, self._nos(second))

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_pages_slice_the_same_cached_rows(self):
        """翻页共享**同一份**缓存的 rows（评审 S1 的判据形态）：分页两键不在键里，
        第 2 页命中第 1 页的条目、各自切片后结果仍逐键正确。

        判别力：若分页参数回到键里（或 build 开始读它们），第 2 页会**重新取数**
        ⇒ 下面「不再查订单表」的断言红；若键剔了分页但切片坏了（串页），
        两页「不相交且并集=全集」的断言红。
        ⚠️ web 侧（`HallParamIsolationTests::test_pages_are_sliced_without_mixing`）
        是**相反**的语义且两边都要钉：web 的分页在 `_build` 里面，`page` 必须在键里。
        """
        taker = self._make_taker("taker-page", games=[self.game1])
        app = self._make_app("短缓存翻页应用", TAKER_HIT_KEY, TAKER_HIT_SECRET, taker)
        token = self._token(app)
        order_a = self._order(self.game1, "20261031000000000105")
        order_b = self._order(self.game1, "20261031000000000106")

        first = self._hall(token, {"page_size": "1"})
        with CaptureQueriesContext(connection) as page2_ctx:
            second = self._hall(token, {"page_size": "1", "page": "2"})

        nos1 = self._nos(first)
        nos2 = self._nos(second)
        # 切片正确：两页各 1 行、不相交、并集 = 全集；count 都是命中总数。
        self.assertEqual(len(nos1), 1)
        self.assertEqual(len(nos2), 1)
        self.assertNotEqual(nos1, nos2)
        self.assertEqual(set(nos1) | set(nos2), {order_a.order_no, order_b.order_no})
        self.assertEqual(first.json()["data"]["count"], 2)
        self.assertEqual(second.json()["data"]["count"], 2)
        # 共享缓存条目：第 2 页不再取数（分页参数不在键里 ⇒ 命中第 1 页的 rows）。
        self.assertEqual(
            [q["sql"][:120] for q in page2_ctx.captured_queries
             if "business_order" in q["sql"]],
            [],
            "第 2 页应该命中缓存的 rows（分页参数已剔出键），不应该重新查订单表",
        )


class HallWebViewerIsolationTests(_WebHallFixture):
    """spec §5-2/3/4：按人维度逐一钉「换了人绝不命中别人的缓存」——本线的泄漏主判据。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_deposit_free_viewer_never_hits_the_paying_viewers_cache(self):
        """免押维度（⭐ 泄漏主判据）：模拟卡只给没免押的 App 查看者；
        A（没免押）预热后，B（免押）同参数拉不得看见 A 的卡，A 再拉仍看得见。"""
        self._viewer("sim-paying-a", [self.game1])
        self._viewer("sim-free-b", [self.game1])
        token_a = self._app_token("sim-paying-a")
        token_b = self._app_token("sim-free-b")
        StaffProfile.objects.filter(
            user=User.objects.get(username="sim-free-b")
        ).update(deposit_quota_enabled=True)
        sim = self._sim("SIM-SC-0001")

        # A（没免押，App 面）：看得到模拟卡——正对照（E-0004）。
        rows_a = self._items(self._app_get(token_a))
        self.assertIn(
            sim.display_order_no, [r["order_no"] for r in rows_a],
            "前置哨兵失败：非免押 App 查看者应该看得到模拟卡",
        )
        # B（免押）同参数：不得命中 A 的缓存 ⇒ 看不到模拟卡。
        rows_b = self._items(self._app_get(token_b))
        self.assertNotIn(sim.display_order_no, [r["order_no"] for r in rows_b])
        # A 再拉：仍看得到（B 的查询不得毒化 A 的缓存）。
        rows_a2 = self._items(self._app_get(token_a))
        self.assertIn(sim.display_order_no, [r["order_no"] for r in rows_a2])

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_game_scope_is_part_of_the_cache_key(self):
        """游戏权限维度：A（两款游戏）预热后，C（只有游戏甲）同参数拉吃不到 A 的缓存；
        镜像行那一路同样由 scope 收窄。A 再拉仍是全集（C 的查询不得毒化）。"""
        viewer_a = self._viewer("scope-full-a", [self.game1, self.game2])
        viewer_c = self._viewer("scope-partial-c", [self.game1])
        own = self._order(self.game1, "20261031000000000011")
        foreign = self._order(self.game2, "20261031000000000012")
        mirror = self._upstream_mirror(self.game2, "UP-SC-0001")

        # A 热身：原生（含乙游戏那张）与镜像行都在——正对照。
        # ⚠️ 内部读口镜像行的 `order_no` 键恒空串（REMOTE_ROW_BASE 基线，未覆写），
        #    展示值在 `remote_order_view` 键上（`serialize_upstream_row`）。
        rows_a = self._items(viewer_a.get(self.url))
        nos_a = [r["order_no"] for r in rows_a]
        self.assertIn(own.order_no, nos_a)
        self.assertIn(foreign.order_no, nos_a)
        self.assertEqual(
            [r.get("remote_order_view") for r in rows_a if r.get("origin") == "upstream"],
            [f"V-{mirror.upstream_order_id}"],
            "前置哨兵失败：A 应该看得到乙游戏的镜像行",
        )
        # C 同参数：不得命中 A 的缓存 ⇒ 原生与镜像都只剩甲游戏。
        rows_c = self._items(viewer_c.get(self.url))
        self.assertEqual([r["order_no"] for r in rows_c], [own.order_no])
        self.assertEqual(
            [r for r in rows_c if r.get("origin") == "upstream"],
            [],
        )
        # A 再拉：仍是全集（C 的查询不得毒化 A 的缓存）。
        nos_a2 = [r["order_no"] for r in self._items(viewer_a.get(self.url))]
        self.assertIn(own.order_no, nos_a2)
        self.assertIn(foreign.order_no, nos_a2)

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_price_redaction_stays_per_viewer_across_cache_hits(self):
        """价格脱敏维度：受限者 `hall_price=null`、不受限者拿数字；互相预热缓存后
        两边的值各自正确（谁也不能把脱敏值 / 原始值漏给对方）。"""
        order = self._order(self.game1, "20261031000000000021", price="250.00")
        normal = self._viewer("price-normal", [self.game1])
        restricted = self._viewer(
            "price-restricted", [self.game1], extra_nodes=(RESTRICT_OFFER_PRICE,),
            group="受限价组",
        )

        def prices(client):
            return [
                r["hall_price"] for r in self._items(client.get(self.url))
                if r["order_no"] == order.order_no
            ]

        # 不受限者预热（第二次吃自己的缓存）：两回都是数字。
        self.assertEqual(prices(normal), ["250.00"])
        self.assertEqual(prices(normal), ["250.00"])
        # 受限者同参数：不得吃到不受限者的缓存 ⇒ 脱敏成 null（连吃两次，第二次是他的缓存）。
        self.assertEqual(prices(restricted), [None])
        self.assertEqual(prices(restricted), [None])
        # 不受限者再拉：仍是数字（受限者的查询不得毒化）。
        self.assertEqual(prices(normal), ["250.00"])


class HallTakerIsolationTests(_TakerHallFixture):
    """spec §5-5：taker 按人隔离——打手身份就是键维度，两个 taker 各拿各的。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_two_takers_never_share_a_cache_entry(self):
        taker_a = self._make_taker("taker-iso-a", games=[self.game1, self.game2])
        taker_b = self._make_taker("taker-iso-b", games=[self.game1])
        # A 是免押那个（spec 的场景；taker 大厅内容不挂免押，
        # 挂在这里是让「两个 taker 配置不同」与生产场景同形）。
        StaffProfile.objects.filter(user=taker_a).update(deposit_quota_enabled=True)
        app_a = self._make_app("短缓存打手应用甲", TAKER_A_KEY, TAKER_A_SECRET, taker_a)
        app_b = self._make_app("短缓存打手应用乙", TAKER_B_KEY, TAKER_B_SECRET, taker_b)
        token_a = self._token(app_a)
        token_b = self._token(app_b)
        # 乙游戏里的在厅单：A（有两款游戏权限）看得见、B 看不见。
        foreign = self._order(self.game2, "20261031000000000102")

        # A 拉：看得到那张单——正对照。
        nos_a = self._nos(self._hall(token_a))
        self.assertIn(foreign.order_no, nos_a)
        # B 拉：不得命中 A 的缓存 ⇒ 看不到。
        nos_b = self._nos(self._hall(token_b))
        self.assertNotIn(foreign.order_no, nos_b)
        # A 再拉：仍看得到（B 的查询不得毒化 A 的缓存）。
        nos_a2 = self._nos(self._hall(token_a))
        self.assertIn(foreign.order_no, nos_a2)


class HallParamIsolationTests(_WebHallFixture):
    """spec §5-6：参数隔离——同用户不同 `?game=` / 不同 `page` 各自正确。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_different_game_filters_never_share_a_key(self):
        viewer = self._viewer("param-viewer", [self.game1, self.game2])
        order1 = self._order(self.game1, "20261031000000000031")
        order2 = self._order(self.game2, "20261031000000000032")

        def nos(params):
            return [
                r["order_no"] for r in self._items(viewer.get(self.url, params))
            ]

        # 三种参数各自正确（首个请求各建各的缓存条目）。
        self.assertEqual(nos({"game": str(self.game1.id)}), [order1.order_no])
        self.assertEqual(nos({"game": str(self.game2.id)}), [order2.order_no])
        self.assertEqual(set(nos({})), {order1.order_no, order2.order_no})
        # 各自再拉一次（各自吃各自的缓存）：结果不变，互不串。
        self.assertEqual(nos({"game": str(self.game1.id)}), [order1.order_no])
        self.assertEqual(nos({"game": str(self.game2.id)}), [order2.order_no])
        self.assertEqual(set(nos({})), {order1.order_no, order2.order_no})

    @override_settings(HALL_LIST_CACHE_TTL_S=60)
    def test_pages_are_sliced_without_mixing(self):
        """web 的分页在 `_build` **里面** ⇒ `page` 必须在键里：两页各建各的缓存条目、
        各自正确、不串页（评审 S1 核对：本条钉的是 web 语义，保留）。

        ⚠️ 与 taker 侧**相反**且两边都要钉：taker 的分页在缓存之后切片，分页两键
        被剔出键（`hall_views.TAKER_CACHE_SKIP_PARAMS`）、翻页共享同一份 rows
        （`HallTakerCacheHitTests::test_pages_slice_the_same_cached_rows`）。
        ⛔ 别把两处统一成一种：哪边变，哪边的键就跟着变。
        """
        viewer = self._viewer("page-viewer", [self.game1])
        order_a = self._order(self.game1, "20261031000000000033")
        order_b = self._order(self.game1, "20261031000000000034")

        def page_nos(params):
            body = viewer.get(self.url, params).json()["data"]
            return body["total"], [r["order_no"] for r in body["items"]]

        total_1, page1 = page_nos({"page_size": "1"})
        total_2, page2 = page_nos({"page_size": "1", "page": "2"})
        # 两页各自是全集的**不相交**的一半（`page` 在键里 ⇒ 各建各的；若 page 不进键，
        # 第 2 页会命中第 1 页的缓存条目，page1 == page2，本用例红）。
        self.assertEqual(total_1, 2)
        self.assertEqual(total_2, 2)
        self.assertEqual(len(page1), 1)
        self.assertEqual(len(page2), 1)
        self.assertNotEqual(page1, page2)
        self.assertEqual(set(page1) | set(page2), {order_a.order_no, order_b.order_no})


class HallCacheKillSwitchTests(_WebHallFixture):
    """spec §5-7：`HALL_LIST_CACHE_TTL_S=0` = 紧急关闭——改库立即可见、零缓存读写。"""

    @override_settings(HALL_LIST_CACHE_TTL_S=0)
    def test_ttl_zero_reads_fresh_from_the_db(self):
        viewer = self._viewer("killswitch-viewer", [self.game1])
        order = self._order(self.game1, "20261031000000000041")

        first = viewer.get(self.url)
        self.assertIn(order.order_no, [r["order_no"] for r in self._items(first)])
        # 两次请求之间改库：把这张单接走。
        self._claim(order, "killswitch-taker")
        # 第二次请求立即反映变化（证明零缓存读写；开着缓存时这里会吃到旧载荷）。
        second = viewer.get(self.url)
        self.assertNotIn(order.order_no, [r["order_no"] for r in self._items(second)])


class HallCacheExpiryTests(_WebHallFixture):
    """spec §5-8：TTL 过期后重算——把已存键的到期时刻拨到过去模拟时间前进，⛔ 不 sleep。

    拨表判据的出处：locmem 的过期判定是 `exp <= time.time()`
    （`django/core/cache/backends/locmem.py::_has_expired`，实测 5.2.17），
    把 `_expire_info` 里的值改成过去即等价于「时间已经走过 TTL」。
    """

    @override_settings(HALL_LIST_CACHE_TTL_S=600)
    def test_expired_entry_is_rebuilt(self):
        viewer = self._viewer("expiry-viewer", [self.game1])
        order = self._order(self.game1, "20261031000000000051")

        def in_hall():
            return order.order_no in [r["order_no"] for r in self._items(viewer.get(self.url))]

        self.assertTrue(in_hall())
        # TTL 之内改库：仍看到旧载荷——证明它真的在缓存（不是每次直读）。
        self._claim(order, "expiry-taker")
        self.assertTrue(in_hall())
        # 时间推进（拨表）⇒ 条目过期 ⇒ 下一次请求重算，改库立即可见。
        backend = caches["hall_list"]
        self.assertTrue(backend._expire_info, "前置哨兵失败：缓存里应该有已存的键")
        for key in list(backend._expire_info):
            backend._expire_info[key] = 0
        self.assertFalse(in_hall())


class ShortCachedPayloadUnitTests(TenantTestCase):
    """`short_cached_payload` 本体的三条：紧急开关、命中副本语义、过期重算。"""

    def setUp(self):
        caches["hall_list"].clear()

    def test_ttl_zero_bypasses_cache_completely(self):
        """`ttl <= 0`（E-0021：`0` = 关闭，不是"没配置"）⇒ 每次 build、缓存里零写入。"""
        calls = []

        def build():
            calls.append(1)
            return {"round": len(calls)}

        self.assertEqual(short_cache.short_cached_payload(("unit-off",), ttl=0, build=build), {"round": 1})
        self.assertEqual(short_cache.short_cached_payload(("unit-off",), ttl=0, build=build), {"round": 2})
        self.assertIsNone(
            caches["hall_list"].get(short_cache._cache_key(("unit-off",))),
            "紧急开关路径不允许写缓存",
        )
        self.assertEqual(len(calls), 2)

    def test_positive_ttl_caches_and_returns_fresh_copies(self):
        """`ttl > 0` ⇒ 一次 build 两次使用；且命中返回的是**新副本**
        （locmem set pickle / get 反 pickle）——调用方原地改不漏给下一个命中者。"""
        calls = []

        def build():
            calls.append(1)
            return {"round": len(calls)}

        first = short_cache.short_cached_payload(("unit-copy",), ttl=60, build=build)
        second = short_cache.short_cached_payload(("unit-copy",), ttl=60, build=build)
        self.assertEqual(first, {"round": 1})
        self.assertEqual(second, {"round": 1})
        self.assertEqual(len(calls), 1)
        # 调用方把拿到的载荷改掉（大厅读口就有一路「整批原地抹 is_preferred」的写法）
        first["mutated"] = True
        third = short_cache.short_cached_payload(("unit-copy",), ttl=60, build=build)
        self.assertEqual(third, {"round": 1})
        self.assertNotIn("mutated", third)

    def test_expired_entry_triggers_a_rebuild(self):
        calls = []

        def build():
            calls.append(1)
            return {"round": len(calls)}

        backend = caches["hall_list"]
        key = short_cache._cache_key(("unit-expiry",))
        self.assertEqual(short_cache.short_cached_payload(("unit-expiry",), ttl=60, build=build), {"round": 1})
        self.assertEqual(short_cache.short_cached_payload(("unit-expiry",), ttl=60, build=build), {"round": 1})
        self.assertEqual(len(calls), 1)
        # 拨表（同 HallCacheExpiryTests 的判据出处）。⚠️ `_expire_info` 存的是
        # `make_and_validate_key` 之后的键（带 version 前缀），⛔ 不是我传进去的原串。
        backend._expire_info[backend.make_and_validate_key(key)] = 0
        self.assertEqual(short_cache.short_cached_payload(("unit-expiry",), ttl=60, build=build), {"round": 2})
        self.assertEqual(len(calls), 2)


class HallCacheKeyTests(TenantTestCase):
    """spec §5-9：键带 schema 前缀（测试进程多租户共用一份 locmem，防跨租户串数据）。"""

    def test_key_carries_the_schema_prefix(self):
        key = short_cache._cache_key(("hall/web/v1", [], ()))
        self.assertTrue(key.startswith("hallcache:"), key)
        # schema 段就在前缀之后（schema 名里没有冒号，第二段必是它）。
        self.assertEqual(key.split(":")[1], connection.schema_name)

    def test_different_key_parts_produce_different_keys(self):
        parts = ("hall/web/v1", [("game", "1")], ((1,), True, (True,), True))
        other_game = ("hall/web/v1", [("game", "2")], ((1,), True, (True,), True))
        other_viewer = ("hall/web/v1", [("game", "1")], ((2,), True, (True,), True))
        key = short_cache._cache_key(parts)
        self.assertNotEqual(key, short_cache._cache_key(other_game))
        self.assertNotEqual(key, short_cache._cache_key(other_viewer))

    def test_key_text_has_no_space_or_control_characters(self):
        """键文本不得含空格 / 控制字符（评审 M1 的防回归钉子）。

        出处：`memcache_key_warnings` 的正则 `[\x00-\x20\x7f]`
        （`django/core/cache/backends/base.py:393`，实测 5.2.17）把空格判为
        memcached 非法字符 ⇒ locmem 每次 get/set 打一行 `CacheKeyWarning`。
        json 的**默认分隔符**（`", "` / `": "`）带空格 ⇒ `_cache_key` 必须
        `separators=(",", ":")`——删掉它本用例红（分隔符空格落在键里）。
        ⚠️ 本钉钉的是**分隔符**这一形态；键料自身（如带空格的 keyword）仍可能
        把空格带进键——那只是日志告警，locmem 行为不受影响。
        """
        key = short_cache._cache_key(
            ("hall/taker/v1", 3, [("game", "1"), ("keyword", "合成-检索词")], True),
        )
        self.assertIsNone(
            re.search(r"[\x00-\x20\x7f]", key),
            f"键文本含空格/控制字符（json 分隔符没收紧？）：{key!r}",
        )
