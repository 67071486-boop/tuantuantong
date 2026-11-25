from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, GameServer, Project, ProjectCategory
from apps.business.validation import (
    ValidationProblem,
    normalize_project_text,
    validate_order_chain,
    validate_required,
)


class OrderValidationTests(TenantTestCase):
    def setUp(self):
        self.genshin = Game.objects.create(name="原神")
        self.starrail = Game.objects.create(name="星穹铁道")
        self.gs_server = GameServer.objects.create(game=self.genshin, name="国服")
        self.sr_server = GameServer.objects.create(game=self.starrail, name="国服")
        self.gs_cat = ProjectCategory.objects.create(game=self.genshin, name="深境螺旋")
        self.gs_proj = Project.objects.create(category=self.gs_cat, name="满星")

    def test_same_chain_passes(self):
        validate_order_chain(
            game=self.genshin,
            server=self.gs_server,
            category=self.gs_cat,
            project=self.gs_proj,
        )

    def test_server_from_another_game_is_rejected(self):
        """选了 A 游戏的区服，不能配 B 游戏的订单——最该有测试的一条。"""
        with self.assertRaises(ValidationProblem):
            validate_order_chain(game=self.genshin, server=self.sr_server)

    def test_category_from_another_game_is_rejected(self):
        other = ProjectCategory.objects.create(game=self.starrail, name="忘却之庭")
        with self.assertRaises(ValidationProblem):
            validate_order_chain(game=self.genshin, category=other)

    def test_project_from_another_game_is_rejected(self):
        other_cat = ProjectCategory.objects.create(game=self.starrail, name="其它分类")
        other_proj = Project.objects.create(category=other_cat, name="别的项目")
        with self.assertRaises(ValidationProblem):
            validate_order_chain(
                game=self.genshin, category=self.gs_cat, project=other_proj
            )

    def test_project_without_category_is_rejected(self):
        with self.assertRaises(ValidationProblem):
            validate_order_chain(game=self.genshin, project=self.gs_proj)

    def test_all_optionals_may_be_none(self):
        """server/category/project 都可空——D9 与 spec §5.2。"""
        validate_order_chain(game=self.genshin)

    def test_required_is_only_game_and_project(self):
        """spec §5.2：必填只有游戏与项目；价格与联系方式都不是必填。

        ⚠️这是**接口层**口径：录单**页面**另有「订单总价必填、默认 0」，
        见 `docs/DECISIONS.md` D-0082。
        """
        validate_required(game=self.genshin, project_text="满星")
        with self.assertRaises(ValidationProblem):
            validate_required(game=None, project_text="满星")
        with self.assertRaises(ValidationProblem):
            validate_required(game=self.genshin, project_text="   ")

    def test_validation_problem_carries_business_code(self):
        problem = ValidationProblem("随便什么")
        self.assertEqual(problem.code, 1000)
        self.assertEqual(str(problem), "随便什么")

    def test_project_text_is_trimmed_and_inner_whitespace_collapsed(self):
        self.assertEqual(normalize_project_text("  满星  "), "满星")
        self.assertEqual(normalize_project_text("满星/  陪玩"), "满星/ 陪玩")
        self.assertEqual(normalize_project_text(None), "")
