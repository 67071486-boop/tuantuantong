"""录单保留同一游戏内的显式分类，分类缺省时从常用项目反推。"""
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, Project, ProjectCategory
from tests.p5_auth_helpers import make_logged_in_client

CREATE_URL = "/api/business/orders/"

#: 录单端点自己的类级门（与 `tests/test_order_create_throw_to_hall.py` 同一条）。
CREATE_NODE = "order.add_edit"


class ProjectCategoryDerivationTests(TenantTestCase):
    def setUp(self):
        # ⚠️ 游戏必须在**建账号之前**建：`make_logged_in_client` 不传 `games` 时
        #    给的是「当前租户的全部游戏」（D-0136）。
        self.game = Game.objects.create(name="合成游戏甲")
        self.other_game = Game.objects.create(name="合成游戏乙")
        self.category = ProjectCategory.objects.create(
            game=self.game, name="合成分类甲"
        )
        self.project = Project.objects.create(
            category=self.category, name="合成项目甲"
        )
        self.sibling_category = ProjectCategory.objects.create(
            game=self.game, name="合成分类乙"
        )
        self.sibling_project = Project.objects.create(
            category=self.sibling_category, name="合成项目乙"
        )
        # 别的游戏下的分类：用来造「分类不属于该游戏」。
        self.foreign_category = ProjectCategory.objects.create(
            game=self.other_game, name="合成分类丙"
        )
        self.foreign_project = Project.objects.create(
            category=self.foreign_category, name="合成项目丙"
        )
        self.client = make_logged_in_client(
            groups=("客服",),
            nodes=(CREATE_NODE,),
        )

    def _post(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "合成项目文本甲",
        }
        payload.update(overrides)
        return self.client.post(
            CREATE_URL, payload, content_type="application/json"
        )

    def test_project_without_category_derives_category_from_project(self):
        """① 只选项目、分类缺省 ⇒ 建单成功，分类与项目一致（业务方 2026-10-27 口径）。"""
        body = self._post(project=self.project.id).json()
        self.assertEqual(body["code"], 0, body)

        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.project_id, self.project.id)
        # 判据是「订单分类 == 项目自带的分类」，⛔ 不是「恰好等于夹具里那个 id」：
        # 那句口径就是「从项目反推」，写死 id 会让反推改成别的来源时恒绿。
        self.assertEqual(order.category_id, self.project.category_id)

    def test_project_and_explicit_category_can_differ_within_game(self):
        body = self._post(
            project=self.sibling_project.id, category=self.category.id
        ).json()
        self.assertEqual(body["code"], 0, body)

        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.project_id, self.sibling_project.id)
        self.assertEqual(order.category_id, self.category.id)

    def test_category_from_another_game_is_still_rejected(self):
        """③ 分类属于别的游戏 ⇒ 仍 1000，且一行都不写。"""
        body = self._post(category=self.foreign_category.id).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "所选项目分类不属于该游戏")
        self.assertEqual(Order.objects.count(), 0)

    def test_project_from_another_game_is_rejected_with_valid_category(self):
        body = self._post(
            project=self.foreign_project.id, category=self.category.id
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "所选项目不属于该游戏")
        self.assertFalse(Order.objects.exists())

    def test_neither_category_nor_project_is_unchanged(self):
        """④ 「都不传」的既有路径不受影响：两列留空、照样建单。"""
        body = self._post().json()
        self.assertEqual(body["code"], 0, body)

        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertIsNone(order.project_id)
        self.assertIsNone(order.category_id)
