"""两条细粒度权限的现状：legacy codename 已冻结，真闸门是节点 code。

`business.override_taker_price` / `business.review_performance` 不再是消费点。
`/throw-to-hall`、`/assign` 的改接单价看 `order.submit_payout_price`；
绩效 `/review` 看 `order.change_status_completed`。
`OverrideTakerPriceTests` 测条件闸门（带价才判）。
`BizPermissionClassTests` 只守 legacy 工厂和库行：不删既有实现，但不要新增使用。

有判据要过（200 + code 0）、无判据要拒（403）。只测通过等于没测。
403 的 `code` 分不出 CSRF 失败和权限不足，所以断言断 **msg**；
同路径带对权限回 200，用来排除「客户端自己没带上」。
"""
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: 刻意不含 `order.submit_payout_price`：那条只在带价时判，由
#: `_cs(with_price_node=...)` 显式声明。混进基础集合，「不带价也 403」就断不出来。
BIZ_ORDER_NODES = (
    "order.set_offer_price",          # `/hall-price/`（改发单价）
    "order.send_to_grab_hall",        # `/throw-to-hall/`（2026-10-26 起**独占闸**）
    "order.assign_taker",             # `/assign/`
)

#: 带价才要的那一条判据（对标 1162）。
PRICE_NODE = "order.submit_payout_price"

OVERRIDE = "business.override_taker_price"
REVIEW = "business.review_performance"

#: 本文件所有 403 断言的判据——细粒度权限拒绝时视图给出的中文理由。
#: 它必须与 CSRF 失败（DRF 的 "CSRF Failed: ..."）**不同串**，否则断言没有判据力。
PERM_DENIED_MSG = "改接单价的权限"


class OverrideTakerPriceTests(TenantTestCase):
    """`/throw-to-hall/` 与 `/assign/` 的**条件**闸门（带价 ⇒ 额外要改价判据）。

    判据是节点 `order.submit_payout_price`。`can_edit_taker_price` 是 fail-open
    的限制型 flag，不能单独回答「能不能改价」（否则内置角色都能改）。
    """

    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.perm = Permission.objects.get(codename="override_taker_price")

    def _cs(self, *, with_price_node):
        """建一个客服并**真实登录**（不用 force_login，见 tests/p5_auth_helpers.py）。

        节点在 `.worktrees` 夹具里是登录**之前**声明的（`nodes=`），所以这里一次给全；
        legacy codename（`self.perm`）只在 `test_a_user_holding_only_the_legacy_codename_is_denied`
        里显式补挂——其余用例根本不需要它（判据已不看它）。
        """
        username = f"cs{int(with_price_node)}"
        nodes = BIZ_ORDER_NODES + ((PRICE_NODE,) if with_price_node else ())
        self.client = make_logged_in_client(
            username=username, groups=("客服",), nodes=nodes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 这些客服要读/改 `self.game` 上的订单（读侧过 `game_scope`、写侧过
        # `assert_game_allowed`），所以显式声明它持有该游戏。⚠️ 本文件测的是
        # 「带价才判 `order.submit_payout_price`」那道**权限**门——不给游戏权限的话，
        # 失败原因会变成"看不到这张单"（1002），而不是本用例要测的那一处；
        # 该用例的期望值一个字没改。
        grant_games(user, self.game)
        return user

    def _throw(self, payload):
        return self.client.post(
            f"/api/business/orders/{self.order.order_no}/throw-to-hall/",
            payload, content_type="application/json",
        )

    def _assign(self, payload):
        return self.client.post(
            f"/api/business/orders/{self.order.order_no}/assign/",
            payload, content_type="application/json",
        )

    def test_cs_can_throw_but_not_change_price_without_the_node(self):
        """不带价抛单只需「客服」，不应被改价判据挡住（闸门是**条件**的）。"""
        self._cs(with_price_node=False)
        resp = self._throw({})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0)

    def test_cs_without_the_node_cannot_set_price(self):
        self._cs(with_price_node=False)
        resp = self._throw({"price": "66.00"})
        self.assertEqual(resp.status_code, 403)      # P5：已认证但无权限 = 403
        # 断 msg 而不是只断 code（Ruling 32）：CSRF 失败也是 403 + code 403。
        self.assertIn(PERM_DENIED_MSG, resp.json()["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("88.00"))   # 没被改
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_cs_with_the_node_can_set_price(self):
        self._cs(with_price_node=True)
        resp = self._throw({"price": "66.00"})
        self.assertEqual(resp.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("66.00"))

    def test_cs_without_the_node_cannot_set_price_on_assign(self):
        """`/assign` 的写价与 `/throw-to-hall` 是**同一条**收口（spec §4.3 表）。"""
        taker = User.objects.create_user(username="taker-assign")
        self._cs(with_price_node=False)
        resp = self._assign({"user_id": taker.id, "price": "66.00"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn(PERM_DENIED_MSG, resp.json()["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("88.00"))
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_cs_with_the_node_can_set_price_on_assign(self):
        """正向对照：同路径、同客户端，带对判据就 200 —— 排除「403 来自 CSRF」。"""
        taker = User.objects.create_user(username="taker-assign-ok")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（本条考带价节点）。
        grant_games(taker, self.game)
        self._cs(with_price_node=True)
        resp = self._assign({"user_id": taker.id, "price": "66.00"})
        self.assertEqual(resp.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("66.00"))
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_cs_without_the_node_can_assign_without_price(self):
        """同一条闸门是**条件**的：不带价指派不需要那条判据。"""
        taker = User.objects.create_user(username="taker-noperm")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（本条考条件闸）。
        grant_games(taker, self.game)
        self._cs(with_price_node=False)
        resp = self._assign({"user_id": taker.id})
        self.assertEqual(resp.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_a_user_holding_only_the_legacy_codename_is_denied(self):
        """**D1 的判别力证据**：判据换成节点 code 后，只给旧 codename 不再放行。

        ⚠️ 与 `test_cs_with_the_node_can_set_price` 配对：同一条路径、同一个请求体，
        差别只有「旧 codename + 无节点」vs「有节点」。少了这条，`has_perm(OVERRIDE)`
        被换掉这件事**没有判据**（迁移断言不算数，除非它能变红）。
        """
        self._cs(with_price_node=False)
        u = User.objects.get(username="cs0")
        u.user_permissions.add(self.perm)
        # 前置哨兵：旧 codename **确实**挂上了（否则本条会因别的原因而绿）
        self.assertTrue(u.has_perm(OVERRIDE))
        resp = self._throw({"price": "66.00"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn(PERM_DENIED_MSG, resp.json()["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("88.00"))

    def test_tenant_admin_can_set_price_without_explicit_perm(self):
        """系统管理员恒放行（对标物 roleId 6「拥有一切权限操作能力」，spec §4.3.1③）。"""
        self.client = make_logged_in_client(username="admin", is_superuser=True)
        resp = self._throw({"price": "66.00"})
        self.assertEqual(resp.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("66.00"))

    def test_codename_is_exactly_what_we_intended(self):
        """⚠️ 钉住 codename 的字面值（legacy 契约，D-0028 之下**保留**）。

        Django 的**内建**权限会把模型名拼进 codename（`change_performance`），
        但 `Meta.permissions` 里的**自定义**权限原样使用（已读源码核实：
        `django/contrib/auth/management/__init__.py:19`）。
        这条测试是防止有人"顺手"改写法后前端/文档里的权限名失配。
        """
        self.assertTrue(
            Permission.objects.filter(codename="override_taker_price").exists()
        )
        self.assertTrue(
            Permission.objects.filter(codename="review_performance").exists()
        )
        # 完整权限串（`app_label.codename`）也必须是我们对外承诺的那两个
        self.assertTrue(Permission.objects.filter(content_type__app_label="business",
                                                  codename="override_taker_price").exists())
        self.assertTrue(Permission.objects.filter(content_type__app_label="business",
                                                  codename="review_performance").exists())


class BizPermissionClassTests(TenantTestCase):
    """`HasBizPermission` / `has_biz_permission` 的直接单测（不经 HTTP）。

    ⚠️ 为什么要单独测：这两条权限今天只被**条件闸门**用（`if not user.has_perm(...)`），
    **没有挂进任何 `permission_classes`**。没有调用点的类如果只靠接口测试覆盖，
    等 P3b 真挂上去时才会发现装配形状不对——那正是 P5 在 `HasRole` 上踩过的
    `TypeError: 'HasRole' object is not callable`（5 条 RBAC 测试全红）。
    这里把「工厂返回真类」与「DRF 无参实例化后仍能判定」都钉住。
    """

    class _Req:
        def __init__(self, user):
            self.user = user

    def test_codename_constants_match_the_model_permissions(self):
        from apps.business import permissions

        self.assertEqual(permissions.OVERRIDE_TAKER_PRICE, OVERRIDE)
        self.assertEqual(permissions.REVIEW_PERFORMANCE, REVIEW)
        # 常量必须与 **DB 里真实存在**的权限对得上（迁移/`post_migrate` 漏跑会在这里红）
        self.assertEqual(
            set(permissions.BIZ_PERMISSIONS),
            {
                f"business.{p}"
                for p in Permission.objects.filter(
                    content_type__app_label="business",
                    codename__in=["override_taker_price", "review_performance"],
                ).values_list("codename", flat=True)
            },
        )

    def test_factory_returns_a_class_drf_can_instantiate_without_args(self):
        """照 DRF 的真实调用形状走一遍：`[permission() for permission in ...]`。"""
        import inspect

        from apps.business.permissions import has_biz_permission

        cls = has_biz_permission(OVERRIDE)
        self.assertTrue(inspect.isclass(cls), "工厂必须返回类（不是实例、不是 partial）")
        accessor = cls()          # ← DRF `get_permissions()` 的那一行
        denied = User.objects.create_user(username="plain")
        allowed = User.objects.create_user(username="granted")
        allowed.user_permissions.add(Permission.objects.get(codename="override_taker_price"))
        self.assertFalse(accessor.has_permission(self._Req(denied), None))
        self.assertTrue(accessor.has_permission(self._Req(allowed), None))

    def test_factory_rejects_an_unregistered_codename(self):
        """拼错的 codename 必须在**声明处**炸，不能安静地变成「所有人都 403」。"""
        from apps.business.permissions import has_biz_permission

        with self.assertRaises(ValueError):
            has_biz_permission("business.override_taker_prices")

    def test_is_superuser_passes_without_the_permission_row(self):
        """系统管理员恒放行——机制是 `has_perm` 对 `is_superuser` 的天然行为
        （spec §4.3.1③），**不是**我们额外写了一个分支。"""
        from apps.business.permissions import has_biz_permission

        admin = User.objects.create_superuser(username="root")
        accessor = has_biz_permission(OVERRIDE)()
        self.assertTrue(accessor.has_permission(self._Req(admin), None))

    def test_anonymous_is_rejected(self):
        from types import SimpleNamespace

        from apps.business.permissions import has_biz_permission

        accessor = has_biz_permission(OVERRIDE)()
        self.assertFalse(
            accessor.has_permission(self._Req(SimpleNamespace(is_authenticated=False)), None)
        )
