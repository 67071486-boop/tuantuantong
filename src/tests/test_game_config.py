from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, GameServer, Project, ProjectCategory


class GameConfigTests(TenantTestCase):
    def test_create_full_chain(self):
        game = Game.objects.create(name="原神")
        server = GameServer.objects.create(game=game, name="国服")
        category = ProjectCategory.objects.create(game=game, name="深境螺旋")
        project = Project.objects.create(category=category, name="满星")
        self.assertEqual(project.category.game, game)
        self.assertEqual(server.game, game)

    def test_game_name_is_unique_within_tenant(self):
        Game.objects.create(name="原神")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Game.objects.create(name="原神")

    def test_server_name_unique_per_game_not_globally(self):
        """同名区服可以存在于两个不同游戏下——唯一约束的作用域是「同游戏内」。"""
        a = Game.objects.create(name="原神")
        b = Game.objects.create(name="崩坏")
        GameServer.objects.create(game=a, name="国服")
        GameServer.objects.create(game=b, name="国服")
        self.assertEqual(GameServer.objects.filter(name="国服").count(), 2)

    def test_project_belongs_to_category_which_belongs_to_game(self):
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="深境螺旋")
        project = Project.objects.create(category=category, name="满星", base_price=100)
        self.assertEqual(project.category.game_id, game.id)
        project.refresh_from_db()
        self.assertEqual(str(project.base_price), "100.00")

    def test_game_delete_is_protected_when_referenced(self):
        """配置被引用后不得被删——PROTECT 兜底。"""
        game = Game.objects.create(name="原神")
        GameServer.objects.create(game=game, name="国服")
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                game.delete()
