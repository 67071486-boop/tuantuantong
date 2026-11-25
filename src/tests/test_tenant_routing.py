from django.conf import settings
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.platform.provisioning import provision_tenant

# 这些域名必须先进 ALLOWED_HOSTS。
# TenantTestCase 只自动放行它自己的 `tenant.test.com`（见其 add_allowed_test_domain），
# 不放行下面用的域名。不放行的话，`TenantMainMiddleware` 会捕获 `DisallowedHost`
# 并直接返回 **404**，下面「已知租户期望 200」的测试就会失败。
# 注意：是 404，**不是 400**——该中间件自己吞掉了 DisallowedHost
#（见 django_tenants/middleware/main.py 的 process_request）。
ROUTING_TEST_DOMAINS = ("routed.test.local", "unknown.test.local")


class TenantRoutingTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in ROUTING_TEST_DOMAINS if d not in settings.ALLOWED_HOSTS
        ]

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for domain in ROUTING_TEST_DOMAINS:
            if domain in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.remove(domain)

    def test_known_tenant_host_reaches_application(self):
        provision_tenant(
            schema_name="routed", name="路由租户", domain="routed.test.local"
        )
        client = Client(SERVER_NAME="routed.test.local")
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        # 断言**选中的 schema**，否则「解析到租户但没切 search_path」的实现
        # 也能让两条测试同时绿——而本任务的题目正是 Host → schema。
        self.assertEqual(connection.schema_name, "routed")

    def test_unknown_host_is_rejected_with_404(self):
        client = Client(SERVER_NAME="unknown.test.local")
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 404)
