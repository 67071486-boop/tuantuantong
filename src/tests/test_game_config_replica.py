"""对标 1:1 复刻带来的后端改动：7 个字段 + 排序方向（决策 D-0023 / spec §4）。

为什么这些断言值得存在：
- 四个游戏开关与三个分类字段是**照着对标页面加的**，前端三页直接绑它们；
  写错字段名或漏进序列化器，页面上就是「开关点了没反应」，而那种失败在 UI 上很难归因。
- 排序方向（数字大在前）是**唯一会外溢**的改动——⚠️ **这句原来写成"唯一会外溢"是错的**
  （独立评审 I-4 用探针实测出一个反例）：`Meta.ordering` 是模型级属性，凡是读这几个模型的地方都跟着反向。
  已知至少三处：① 录单页 / 订单详情页的下拉（前端 `fetchGames`/`fetchCategories`/`fetchProjects`）；
  ② **`/api/accounts/` 载荷里的 `games` 顺序**（`apps/accounts/payloads.py` 走 `profile.games.all()`，
  多对多的返回顺序取相关模型的 `Meta.ordering`）——这条本轮之前**没有测试覆盖**，本文件补了一条；
  ③ 任何按 `[0]`/`first()` 取「第一个」的调用点（例如项目页新增时取第一条分类）。
  这三处都必须直接钉住顺序，而不是靠「页面上看着对」。
"""
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Project, ProjectCategory
from tests.p5_auth_helpers import make_logged_in_client


class GameConfigReplicaTests(TenantTestCase):
    def setUp(self):
        # TenantTestCase 只把 tenant.test.com 放进 ALLOWED_HOSTS；端点受 fail-closed 权限类保护，
        # 所以要先真实登录（与 tests/test_config_api.py 同一套 helper）。
        # 写请求 `PATCH /games/<id>/` 还要
        # `game.add_edit`（`GameViewSet.GAME_PERMISSIONS["partial_update"]`）。
        # 本类还经端点 `PATCH /categories/<id>/`
        # 与 `POST /projects/`（`test_project_name_boundary_is_450`）⇒ 另需这两条节点。
        # ⚠️ `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）。
        self.client = make_logged_in_client(
            groups=("配置组",),
            nodes=("game.add_edit", "game.order_type_categories", "game.common_projects"),
        )
        self.base = "/api/business"

    def test_game_switches_are_writable_and_readable(self):
        game = Game.objects.create(name="原神")

        response = self.client.patch(
            f"{self.base}/games/{game.id}/",
            {"is_default": True, "is_mobile": True, "is_companion": True, "disable_sms": True},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        game.refresh_from_db()
        self.assertTrue(game.is_default)
        self.assertTrue(game.is_mobile)
        self.assertTrue(game.is_companion)
        self.assertTrue(game.disable_sms)

        detail = self.client.get(f"{self.base}/games/{game.id}/").json()["data"]
        for key in ("is_default", "is_mobile", "is_companion", "disable_sms"):
            self.assertTrue(detail[key], f"{key} 没有回读为 True")

    def test_category_replica_fields_roundtrip(self):
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="代练")

        response = self.client.patch(
            f"{self.base}/categories/{category.id}/",
            {"assigned_platforms": "8,30,90", "title_prefix": "急单", "title_suffix": "勿催"},
            content_type="application/json",
        )

        self.assertEqual(response.json()["code"], 0)
        category.refresh_from_db()
        self.assertEqual(category.assigned_platforms, "8,30,90")
        self.assertEqual(category.title_prefix, "急单")
        self.assertEqual(category.title_suffix, "勿催")

    def test_title_prefix_over_50_chars_is_a_business_error(self):
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="代练")

        response = self.client.patch(
            f"{self.base}/categories/{category.id}/",
            {"title_prefix": "x" * 51},
            content_type="application/json",
        )

        # 业务失败一律 HTTP 200 + code 1000（不要让 varchar(50) 在写库时变成 500）。
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        category.refresh_from_db()
        self.assertEqual(category.title_prefix, "")

    def test_project_name_boundary_is_450(self):
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="代练")

        ok = self.client.post(
            f"{self.base}/projects/",
            {"category": category.id, "name": "x" * 450},
            content_type="application/json",
        )
        self.assertEqual(ok.json()["code"], 0, "对标弹窗允许 450 字，这里不应被拒")

        too_long = self.client.post(
            f"{self.base}/projects/",
            {"category": category.id, "name": "y" * 451},
            content_type="application/json",
        )
        self.assertEqual(too_long.status_code, 200)
        self.assertEqual(too_long.json()["code"], 1000)

    def test_game_list_is_sorted_by_sort_order_descending(self):
        Game.objects.create(name="小", sort_order=1)
        Game.objects.create(name="大", sort_order=9)
        Game.objects.create(name="中", sort_order=5)

        items = self.client.get(f"{self.base}/games/?page_size=50").json()["data"]["items"]

        self.assertEqual([row["name"] for row in items], ["大", "中", "小"])

    def test_equal_sort_order_falls_back_to_id_ascending(self):
        first = Game.objects.create(name="先", sort_order=7)
        second = Game.objects.create(name="后", sort_order=7)

        items = self.client.get(f"{self.base}/games/?page_size=50").json()["data"]["items"]

        self.assertEqual([row["id"] for row in items], [first.id, second.id])

    def test_category_and_project_lists_share_the_same_direction(self):
        game = Game.objects.create(name="原神")
        ProjectCategory.objects.create(game=game, name="低", sort_order=1)
        ProjectCategory.objects.create(game=game, name="高", sort_order=9)
        top = ProjectCategory.objects.get(name="高")
        Project.objects.create(category=top, name="项目低", sort_order=1)
        Project.objects.create(category=top, name="项目高", sort_order=9)

        categories = self.client.get(
            f"{self.base}/categories/?game={game.id}&page_size=50"
        ).json()["data"]["items"]
        projects = self.client.get(
            f"{self.base}/projects/?category={top.id}&page_size=50"
        ).json()["data"]["items"]

        self.assertEqual([row["name"] for row in categories], ["高", "低"])
        self.assertEqual([row["name"] for row in projects], ["项目高", "项目低"])

    def test_projects_can_be_filtered_by_game(self):
        """`?game=` 是给「常用代练项目」页用的（该页只有游戏下拉，没有分类下拉）。

        判据要能分辨「过滤生效」与「没过滤但恰好只有这些」：两个游戏各建一个分类与项目，
        断言只回目标游戏那条。
        """
        target_game = Game.objects.create(name="原神")
        other_game = Game.objects.create(name="崩铁")
        target_category = ProjectCategory.objects.create(game=target_game, name="代练")
        other_category = ProjectCategory.objects.create(game=other_game, name="代练")
        Project.objects.create(category=target_category, name="目标项目")
        Project.objects.create(category=other_category, name="别的项目")

        items = self.client.get(
            f"{self.base}/projects/?game={target_game.id}&page_size=50"
        ).json()["data"]["items"]

        self.assertEqual([row["name"] for row in items], ["目标项目"])

    def test_projects_game_filter_rejects_malformed_value(self):
        """`?game=abc` 必须是 200 + code 1000，不是 HTML 500（E-0010 的同类）。"""
        response = self.client.get(f"{self.base}/projects/?game=abc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_account_payload_games_follow_the_reversed_order(self):
        """**排序反转会外溢到账号载荷**——这是一条此前无人覆盖的路径（独立评审 I-4 的探针结论）。

        `account_payload` 走 `profile.games.all()`，而 M2M 的返回顺序取相关模型的 `Meta.ordering`；
        `Game.ordering` 变成 `-sort_order` 之后，账号详情/列表里的 `games` 也跟着反向。
        判据要能分辨「按新顺序」与「按插入顺序」：**先建 sort_order 小的、后建大的**，
        断言结果是 `[大, 小]`——若 `ordering` 被改回升序，这条必红。
        """
        from django.contrib.auth import get_user_model

        from apps.accounts.payloads import account_payload
        from apps.accounts.models import StaffProfile

        low = Game.objects.create(name="排序小", sort_order=1)
        high = Game.objects.create(name="排序大", sort_order=9)
        user = get_user_model().objects.create_user(username="order-probe", password="x")
        profile = StaffProfile.objects.create(user=user)
        profile.games.add(low)
        profile.games.add(high)

        payload = account_payload(user)

        self.assertEqual(payload["games"], [high.pk, low.pk])

    def test_all_three_models_tie_break_by_id_ascending(self):
        """三个模型的 `ordering` 都是 `['-sort_order', 'id']`——同值时按 id **升序**。

        分开断言三个模型，是因为「只改了一个模型的 ordering」这种漏改在只看 Game 的用例下是绿的
        （独立评审 M-8：把 Project 的 `id` 那一段去掉，原有 9 条仍全过）。
        """
        game = Game.objects.create(name="原神")
        first_category = ProjectCategory.objects.create(game=game, name="分类先", sort_order=5)
        second_category = ProjectCategory.objects.create(game=game, name="分类后", sort_order=5)
        first_project = Project.objects.create(category=first_category, name="项目先", sort_order=5)
        second_project = Project.objects.create(category=first_category, name="项目后", sort_order=5)

        games = self.client.get(f"{self.base}/games/?page_size=50").json()["data"]["items"]
        categories = self.client.get(
            f"{self.base}/categories/?game={game.id}&page_size=50"
        ).json()["data"]["items"]
        projects = self.client.get(
            f"{self.base}/projects/?category={first_category.id}&page_size=50"
        ).json()["data"]["items"]

        self.assertEqual([row["id"] for row in games], [game.id])
        self.assertEqual(
            [row["id"] for row in categories], [first_category.id, second_category.id]
        )
        self.assertEqual([row["id"] for row in projects], [first_project.id, second_project.id])
