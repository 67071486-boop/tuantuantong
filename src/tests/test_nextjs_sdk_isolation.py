from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant


class NextjsSdkIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="sdk_isolation_other", name="SDK 隔离乙",
            domain="sdk-isolation-other.test.local",
        )

    def create_workspace_order_and_app(self, suffix, order_no):
        owner = get_user_model().objects.create_user(username=f"sdk-owner-{suffix}")
        game = Game.objects.create(name=f"SDK 测试游戏 {suffix}")
        Order.objects.create(game=game, order_no=order_no)
        return ApiApp.objects.create(
            name=f"SDK 隔离应用 {suffix}", kind="business_integration", state="sdk_enabled",
            app_key=f"sdk-synthetic-key-{suffix}", app_secret=f"sdk-synthetic-secret-{suffix}",
            created_by=owner,
        )

    def authenticate(self, client, app):
        response = client.post("/api/open/oauth/token", {
            "grant_type": "client_credentials", "client_id": app.app_key,
            "client_secret": app.app_secret,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return {"HTTP_AUTHORIZATION": "Bearer " + response.json()["access_token"]}

    def test_each_credential_reads_orders_only_in_its_own_workspace(self):
        connection.set_tenant(self.tenant)
        order_a = "20261116000000000081"
        order_b = "20261116000000000082"
        app_a = self.create_workspace_order_and_app("a", order_a)
        with tenant_context(self.other_tenant):
            app_b = self.create_workspace_order_and_app("b", order_b)
        client_a = Client(HTTP_HOST=self.domain.domain, REMOTE_ADDR="203.0.113.71")
        client_b = Client(HTTP_HOST="sdk-isolation-other.test.local", REMOTE_ADDR="203.0.113.72")
        auth_a = self.authenticate(client_a, app_a)
        auth_b = self.authenticate(client_b, app_b)

        path = "/api/integration/v1/orders/"
        for client, auth, order_no in ((client_a, auth_a, order_a), (client_b, auth_b, order_b)):
            response = client.get(path, **auth)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["code"], 0)
            self.assertEqual(
                [item["order_no"] for item in response.json()["data"]["results"]],
                [order_no],
            )

        response = client_b.get(path, **auth_a)
        self.assertEqual(response.status_code, 401, response.content)
        response = client_a.get(path + order_b + "/", **auth_a)
        self.assertNotEqual(response.json()["code"], 0, response.content)
        self.assertIsNone(response.json()["data"])
