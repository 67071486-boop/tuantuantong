from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.api.config_views import ParentFilteredQuerysetMixin
from apps.business.models import Game, GameServer, Project, ProjectCategory
from tests.p5_auth_helpers import make_logged_in_client


class ConfigApiTests(TenantTestCase):
    def setUp(self):
        # TenantTestCase 只把 tenant.test.com 加进 ALLOWED_HOSTS，默认 testserver 会 404。
        # 这些端点走 fail-closed 的 IsAuthenticated，要先真实登录。
        # 游戏写动作要 game.add_edit / game.delete；区服、分类、项目的写动作各要自己的节点。
        # 不声明节点，登录之后仍会被 403，用例测不到业务。
        # nodes= 必须配 groups=（无组时挂节点是静默 no-op）；组名 ≤5 字。
        self.client = make_logged_in_client(
            groups=("配置组",),
            nodes=(
                "game.add_edit",
                "game.delete",
                "game.server",
                "game.order_type_categories",
                "game.common_projects",
            ),
        )
        self.base = "/api/business"

    def test_create_game_returns_envelope_with_code_zero(self):
        response = self.client.post(
            f"{self.base}/games/", {"name": "原神"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["name"], "原神")
        self.assertTrue(Game.objects.filter(name="原神").exists())

    def test_list_games_is_paginated_inside_data(self):
        Game.objects.create(name="原神")
        Game.objects.create(name="崩坏")
        body = self.client.get(f"{self.base}/games/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(len(body["data"]["items"]), 2)

    def test_duplicate_game_name_returns_business_error_not_500(self):
        """唯一约束冲突必须变成业务错误码，不能抛成 500。"""
        Game.objects.create(name="原神")
        response = self.client.post(
            f"{self.base}/games/", {"name": "原神"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], 0)

    def test_missing_required_field_returns_validation_code(self):
        response = self.client.post(
            f"{self.base}/games/", {"name": ""}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_unknown_resource_returns_business_error(self):
        """不存在的资源按 P1 约定回 HTTP 200 + 业务码（P1 的 NotFound 分层）。"""
        body = self.client.get(f"{self.base}/games/999999/").json()
        self.assertEqual(body["code"], 1002)

    def test_api_root_does_not_serve_an_unenveloped_body(self):
        """DRF 自动生成的 API 根不是信封形状，必须关掉——否则契约正好在这里破。

        它是普通 APIView，不走 EnvelopeModelViewSet，所以返回的是 {"games": url, ...}。
        关掉之后根路径 404；404 比一个**形状不对的 200** 好。
        """
        response = self.client.get(f"{self.base}/")
        self.assertEqual(response.status_code, 404)

    def test_delete_referenced_game_is_soft_and_keeps_history(self):
        """被引用的游戏**现在删得掉了**——软删除：行留在库里，历史引用一个字不动（D-0085）。

        ⚠️ **本条取代了原来那条「被引用的游戏不能删、要回 1001」**：那条是**当时**的口径
        （`GameServer.game` 是 `on_delete=PROTECT`，真删会抛 `ProtectedError`，于是
        `EnvelopeModelViewSet.destroy` 接住它回 1001）。业务方裁定「允许删除，
        但要给一个警告，以后可恢复」⇒ 删除改成**软删除**：不碰 PROTECT，也就不再有那条 1001。
        留下来的要求一条没少：**历史数据不许被带走**、**列表里要消失**、**能恢复**。
        """
        game = Game.objects.create(name="原神")
        GameServer.objects.create(game=game, name="国服")

        response = self.client.delete(f"{self.base}/games/{game.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        game.refresh_from_db()
        self.assertIsNotNone(game.deleted_at, "删除必须打上 deleted_at")
        self.assertTrue(Game.objects.filter(id=game.id).exists(), "软删除不许真删行")
        self.assertEqual(
            GameServer.objects.filter(game=game).count(), 1, "历史/关联数据不许被带走"
        )
        # 列表里消失；`?deleted=1` 里能看到（前端「已删除」视图的数据源）。
        alive = [r["name"] for r in self.client.get(f"{self.base}/games/").json()["data"]["items"]]
        self.assertNotIn("原神", alive)
        deleted = self.client.get(f"{self.base}/games/?deleted=1").json()["data"]["items"]
        self.assertEqual([r["name"] for r in deleted], ["原神"])
        # 恢复就是一次 PATCH（不新增端点，复用既有的 *.update 埋点）。
        restored = self.client.patch(
            f"{self.base}/games/{game.id}/",
            {"deleted_at": None},
            content_type="application/json",
        )
        self.assertEqual(restored.json()["code"], 0)
        game.refresh_from_db()
        self.assertIsNone(game.deleted_at)
        alive = [r["name"] for r in self.client.get(f"{self.base}/games/").json()["data"]["items"]]
        self.assertIn("原神", alive)

    def test_delete_unreferenced_game_still_works(self):
        """阳性对照：没人引用的游戏删除仍然是「成功 + 从列表消失」（现在只是软删除）。"""
        game = Game.objects.create(name="没人引用的游戏")

        response = self.client.delete(f"{self.base}/games/{game.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        # 旧断言是「行没了」；软删除下行还在（只是 `deleted_at` 不为空）——判据改成「从列表消失」。
        self.assertFalse(Game.objects.filter(id=game.id, deleted_at__isnull=True).exists())

    def test_full_chain_creation_via_api(self):
        game_id = self.client.post(
            f"{self.base}/games/", {"name": "原神"}, content_type="application/json"
        ).json()["data"]["id"]
        server_id = self.client.post(
            f"{self.base}/servers/",
            {"game": game_id, "name": "国服"},
            content_type="application/json",
        ).json()["data"]["id"]
        cat_id = self.client.post(
            f"{self.base}/categories/",
            {"game": game_id, "name": "深境螺旋"},
            content_type="application/json",
        ).json()["data"]["id"]
        proj = self.client.post(
            f"{self.base}/projects/",
            {"category": cat_id, "name": "满星", "base_price": "100.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(proj["code"], 0)
        self.assertEqual(proj["data"]["name"], "满星")
        self.assertTrue(server_id)


class ConfigParentFilterTests(TenantTestCase):
    """配置接口按父级过滤（D11）。

    没有这个过滤，录单页的区服/分类下拉只能靠「拉全量再本地过滤」——
    而列表是分页的（默认 20 条），数据一多就会**静默漏项**。
    """

    def setUp(self):
        # ⚠️ 本类原先用匿名 `Client(SERVER_NAME="tenant.test.com")`：它在 main 上是对的，
        # 但本分支已把 DRF 默认权限类改成 fail-closed（`IsAuthenticated`，spec D11），
        # 于是 6 条全部 401（spec §13 预告的跨计划影响）。这些端点没有声明
        # `permission_classes`，必须先真实登录——与同类 `ConfigApiTests` 一致，
        # 登录流程同样收口在 `tests/p5_auth_helpers.py`。
        self.client = make_logged_in_client()
        self.base = "/api/business"
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="星穹铁道")
        self.category_a = ProjectCategory.objects.create(game=self.game_a, name="深境螺旋")
        self.category_b = ProjectCategory.objects.create(game=self.game_b, name="忘却之庭")

    def test_servers_are_filtered_by_game(self):
        GameServer.objects.create(game=self.game_a, name="国服")
        GameServer.objects.create(game=self.game_b, name="国际服")

        body = self.client.get(f"{self.base}/servers/?game={self.game_a.id}").json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 1)
        # 按具体行断言，不用 count()
        self.assertEqual(
            [item["name"] for item in body["data"]["items"]], ["国服"]
        )

    def test_categories_are_filtered_by_game(self):
        body = self.client.get(f"{self.base}/categories/?game={self.game_b.id}").json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            [item["name"] for item in body["data"]["items"]], ["忘却之庭"]
        )

    def test_projects_are_filtered_by_category(self):
        Project.objects.create(category=self.category_a, name="满星")
        Project.objects.create(category=self.category_b, name="忘却之庭满星")

        body = self.client.get(
            f"{self.base}/projects/?category={self.category_a.id}"
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual([item["name"] for item in body["data"]["items"]], ["满星"])

    def test_malformed_parent_filter_returns_validation_error(self):
        """`?game=abc` 不校验就是 ValueError → HTML 500（与订单检索同一个坑）。"""
        response = self.client.get(f"{self.base}/servers/?game=abc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_unused_but_valid_parent_filter_does_not_filter(self):
        """`?category=` 在 servers 上不是过滤条件——**校验但不使用**。

        规则：带父级的三个 ViewSet 都校验 `game`/`category` 两个键（所以拼错一定 1000），
        但每个资源**只应用自己认的那个**。写下来是为了让「没生效」不等于「静默失效」。
        """
        GameServer.objects.create(game=self.game_a, name="国服")
        GameServer.objects.create(game=self.game_b, name="国际服")

        body = self.client.get(
            f"{self.base}/servers/?category={self.category_a.id}"
        ).json()

        self.assertEqual(body["code"], 0)
        # 按具体行断言（不用 count() 单独下结论）。用集合而不是列表：
        # 断言「这两行都在」，不额外依赖返回顺序（排序会引入与中文码点有关的脆弱断言）。
        self.assertEqual(
            {item["name"] for item in body["data"]["items"]},
            {"国服", "国际服"},
        )

    def test_blank_parent_filter_is_treated_as_absent(self):
        """空串按「没传」处理：前端清空下拉常会送 `?game=`。

        判别力来自「去掉 `if v != ""` 守卫」这个变异（见 Step 6 变异三）——
        没有那个变异，这条测试在改动前后都是绿的，等于没有证明。
        """
        GameServer.objects.create(game=self.game_a, name="国服")
        GameServer.objects.create(game=self.game_b, name="国际服")

        body = self.client.get(f"{self.base}/servers/?game=").json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            {item["name"] for item in body["data"]["items"]},
            {"国服", "国际服"},
        )


class ParentFilterFieldGuardTests(SimpleTestCase):
    """`parent_filter_field` 漏声明必须在**类创建期**失败，而不是等请求打进来再 500。

    背景（Task 1 的 self-concern）：`parent_filter_field` 只有一行**注解**、没有默认值，
    漏声明的子类会在 `get_queryset` 里走到不存在的字段 → `FieldError` → HTML 500。
    那是**未预期的程序员错误**，本就该响亮失败——但正确的失败位置是导入期，不是请求期。

    红-绿：删掉 `ParentFilteredQuerysetMixin.__init_subclass__` → 第一条变红
    （类能正常创建，`assertRaises` 拿不到 `TypeError`）；第二条是反向对照。
    """

    def test_mixin_subclass_without_parent_filter_field_fails_at_class_creation(self):
        with self.assertRaises(TypeError):

            class MissingParentFilterField(ParentFilteredQuerysetMixin):
                pass

    def test_mixin_subclass_with_declared_field_is_fine(self):
        class WithParentFilterField(ParentFilteredQuerysetMixin):
            parent_filter_field = "game"

        self.assertEqual(WithParentFilterField.parent_filter_field, "game")
