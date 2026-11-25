from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, Performance
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_builtin_roles, seed_catalog
from tests.p5_auth_helpers import make_logged_in_client


class RoleDashboardTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        self.admin = make_logged_in_client(username="board-admin", is_superuser=True)

    def create_role(self, name, kind):
        response = self.admin.post("/api/role-authorities/", {
            "name": name, "permission_codes": ["order.list"], "dashboard_type": kind,
        }, content_type="application/json")
        self.assertEqual(response.json()["code"], 0)
        return response.json()["data"]

    def test_create_patch_copy_and_atomic_failure(self):
        role = self.create_role("甲角色", "work")
        url = f'/api/role-authorities/{role["id"]}/'
        self.assertEqual(self.admin.get(url).json()["data"]["dashboard_type"], "work")
        self.admin.patch(url, {"remark": "保留"}, content_type="application/json")
        self.assertEqual(Role.objects.get(pk=role["id"]).dashboard_type, "work")
        bad = self.admin.patch(url, {"dashboard_type": "worker", "permission_codes": ["unknown"]}, content_type="application/json")
        self.assertNotEqual(bad.json()["code"], 0)
        self.assertEqual(Role.objects.get(pk=role["id"]).dashboard_type, "work")
        bad = self.admin.patch(url, {"dashboard_type": "admin"}, content_type="application/json")
        self.assertNotEqual(bad.json()["code"], 0)
        copy = self.admin.post(url + "copy/", {"name": "乙角色"}, content_type="application/json")
        self.assertEqual(copy.json()["data"]["dashboard_type"], "work")
        self.admin.patch(url, {"dashboard_type": ""}, content_type="application/json")
        self.assertEqual(Role.objects.get(pk=role["id"]).dashboard_type, "")

    def test_user_layout_conflicts_and_permissions_are_independent(self):
        first = self.create_role("甲角色", "worker")
        second = self.create_role("乙角色", "worker")
        client = make_logged_in_client(username="board-user")
        user = get_user_model().objects.get(username="board-user")
        user.groups.add(Role.objects.get(pk=first["id"]).group)
        def layout():
            return client.get("/api/auth/me").json()["data"]["user"]["dashboard_type"]
        self.assertEqual(layout(), "worker")
        user.groups.add(Role.objects.get(pk=second["id"]).group)
        self.assertEqual(layout(), "worker")
        Role.objects.filter(pk=second["id"]).update(dashboard_type="work")
        self.assertEqual(layout(), "")
        Role.objects.filter(pk=second["id"]).update(dashboard_type="")
        self.assertEqual(layout(), "")
        user.groups.remove(Role.objects.get(pk=second["id"]).group)
        user.groups.add(Group.objects.create(name="无配置组"))
        self.assertEqual(layout(), "")
        user.groups.clear()
        self.assertEqual(layout(), "")
        user.groups.add(Group.objects.get(name="系统管理员"))
        self.assertEqual(layout(), "admin")
        user.groups.clear()
        user.groups.add(Role.objects.get(pk=first["id"]).group)
        self.assertEqual(client.get("/api/business/dashboard/trend/").status_code, 403)

    def test_seed_only_initializes_new_roles(self):
        seed_builtin_roles()
        role = Role.objects.get(name="代练员")
        self.assertEqual(role.dashboard_type, "worker")
        self.assertEqual(Role.objects.get(name="客服").dashboard_type, "work")
        self.assertEqual(Role.objects.get(name="客服主管").dashboard_type, "work")
        role.dashboard_type = ""
        role.save(update_fields=["dashboard_type"])
        seed_builtin_roles()
        role.refresh_from_db()
        self.assertEqual(role.dashboard_type, "")

    def test_changing_layout_does_not_change_order_or_game_scope(self):
        role = self.create_role("范围员", "worker")
        role_obj = Role.objects.get(pk=role["id"])
        role_obj.permissions.add(PermissionNode.objects.get(code="my_orders.send_to_my_system"))
        game_a = Game.objects.create(name="授权游戏")
        game_b = Game.objects.create(name="未授权游戏")
        client = make_logged_in_client(username="范围用户", groups=("范围员",), games=[game_a])
        other = make_logged_in_client(username="另一用户")
        user = get_user_model().objects.get(username="范围用户")
        other_user = get_user_model().objects.get(username="另一用户")
        for no, game, owner in (("20261113000000000101", game_a, user), ("20261113000000000102", game_b, user), ("20261113000000000103", game_a, other_user)):
            order = Order.objects.create(order_no=no, game=game, project_text=no, created_by=owner.username)
            Performance.objects.create(order=order, user=owner, emp_type=EmpType.MAIN, temp_amount=1, bonus_amount=0)
        def rows(url):
            response = client.get(url)
            self.assertEqual(response.json()["code"], 0)
            return {row["order_no"] for row in response.json()["data"]["items"]}
        own = {"20261113000000000101", "20261113000000000102"}
        permitted_game = {"20261113000000000101", "20261113000000000103"}
        for layout in ("worker", "work", ""):
            role_obj.dashboard_type = layout
            role_obj.save(update_fields=["dashboard_type"])
            self.assertEqual(rows("/api/business/my-orders/"), own)
            self.assertEqual(rows("/api/business/orders/"), permitted_game)
            self.assertEqual(other.get("/api/business/my-orders/").status_code, 403)
            self.assertEqual(client.get("/api/business/dashboard/trend/").status_code, 403)
        role_obj.permissions.add(PermissionNode.objects.get(code="order.restrict_to_self_created"))
        for layout in ("worker", "work", ""):
            role_obj.dashboard_type = layout
            role_obj.save(update_fields=["dashboard_type"])
            self.assertEqual(rows("/api/business/orders/"), {"20261113000000000101"})


class RoleDashboardTenantIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings
        from django_tenants.utils import tenant_context
        from apps.platform.provisioning import provision_tenant

        settings.ALLOWED_HOSTS += ["board-a.test.local", "board-b.test.local"]
        cls.tenant_a, _ = provision_tenant(schema_name="board_a", name="看板甲", domain="board-a.test.local")
        cls.tenant_b, _ = provision_tenant(schema_name="board_b", name="看板乙", domain="board-b.test.local")
        with tenant_context(cls.tenant_a):
            Role.objects.filter(name="客服").update(dashboard_type="worker")
        with tenant_context(cls.tenant_b):
            Role.objects.filter(name="客服").update(dashboard_type="work")

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings
        for host in ("board-a.test.local", "board-b.test.local"):
            settings.ALLOWED_HOSTS.remove(host)
        super().tearDownClass()

    def test_same_role_and_account_stay_within_tenant(self):
        from django_tenants.utils import tenant_context

        clients = []
        for tenant, host, no in ((self.tenant_a, "board-a.test.local", "20261113000000000201"), (self.tenant_b, "board-b.test.local", "20261113000000000202")):
            with tenant_context(tenant):
                game = Game.objects.create(name="隔离游戏")
                client = make_logged_in_client(domain=host, username="board-shared", groups=("客服",), nodes=("my_orders.send_to_my_system",))
                user = get_user_model().objects.get(username="board-shared")
                order = Order.objects.create(order_no=no, game=game, project_text=no)
                Performance.objects.create(order=order, user=user, emp_type=EmpType.MAIN, temp_amount=1, bonus_amount=0)
                clients.append(client)
        client_a, client_b = clients
        self.assertEqual(client_a.get("/api/auth/me").json()["data"]["user"]["dashboard_type"], "worker")
        self.assertEqual(client_b.get("/api/auth/me").json()["data"]["user"]["dashboard_type"], "work")
        for client, expected in ((client_a, "20261113000000000201"), (client_b, "20261113000000000202")):
            data = client.get("/api/business/my-orders/").json()["data"]
            self.assertEqual({row["order_no"] for row in data["items"]}, {expected})
        response = client_a.get("/api/auth/me", HTTP_HOST="board-b.test.local")
        self.assertNotEqual(response.json()["code"], 0)
        response = client_a.get("/api/business/my-orders/", HTTP_HOST="board-b.test.local")
        self.assertNotEqual(response.json()["code"], 0)
