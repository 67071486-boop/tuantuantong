from django.core.management import call_command
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.business.models import GameLibrary
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/game-library/"


class GameLibraryTests(TenantTestCase):
    def test_seed_contains_benchmark_games_and_the_new_title(self):
        self.assertEqual(GameLibrary.objects.count(), 597)
        self.assertTrue(GameLibrary.objects.filter(name="蓝色星原：旅谣").exists())
        self.assertTrue(GameLibrary.objects.filter(name="原神").exists())

    def test_requires_login(self):
        response = Client(SERVER_NAME="tenant.test.com").get(URL)
        self.assertEqual(response.status_code, 401)

    def test_admin_reads_the_tenant_library(self):
        client = make_logged_in_client(username="game-library-admin", is_superuser=True)
        body = client.get(URL).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 597)
        self.assertIn("蓝色星原：旅谣", {item["name"] for item in items})

    def test_add_command_upserts_with_sensible_defaults(self):
        call_command("game_library_add", name="测试新游", first_key="C", verbosity=0)
        entry = GameLibrary.objects.get(name="测试新游")
        self.assertEqual(entry.first_key, "C")
        self.assertGreater(entry.sort_order, 597)

    def test_add_command_update_keeps_fields_not_passed(self):
        call_command("game_library_add", name="原神", sort_order=999, verbosity=0)
        entry = GameLibrary.objects.get(name="原神")
        self.assertEqual(entry.first_key, "Y")
        self.assertTrue(entry.is_active)
        self.assertEqual(entry.sort_order, 999)

    def test_user_without_game_server_node_is_forbidden(self):
        client = make_logged_in_client(username="gamelib_nobody")
        self.assertEqual(client.get(URL).status_code, 403)

    def test_game_server_node_can_read(self):
        reader = make_logged_in_client(username="gamelib_reader", groups=("客服",), nodes=("game.server",))
        self.assertEqual(reader.get(URL).status_code, 200)

    def test_game_add_edit_alone_cannot_read(self):
        editor = make_logged_in_client(username="gamelib_editor", groups=("客服",), nodes=("game.add_edit",))
        self.assertEqual(editor.get(URL).status_code, 403)

    def test_inactive_entries_are_hidden_and_new_game_is_first(self):
        GameLibrary.objects.filter(name="蓝色星原：旅谣").update(is_active=False)
        GameLibrary.objects.filter(name="原神").update(name="原神-停用", is_active=False)
        client = make_logged_in_client(username="gamelib_active", is_superuser=True)
        items = client.get(URL).json()["data"]["items"]
        names = [item["name"] for item in items]
        self.assertNotIn("蓝色星原：旅谣", names)
        self.assertNotIn("原神-停用", names)
        self.assertEqual(names[0], "三角洲行动")
