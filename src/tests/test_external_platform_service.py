import json
import re
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.core.signals import request_finished, request_started
from django.db import close_old_connections, connection
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order
from apps.openapi.models import ApiApp, ApiAppState
from apps.platform.provisioning import provision_tenant


def nginx_matches_path(include, path):
    for mode, expression in re.findall(r"^location (\S+) (\S+) \{", include, re.MULTILINE):
        if mode == "=" and expression == path:
            return True
        if mode == "^~" and path.startswith(expression):
            return True
        if mode == "~" and re.fullmatch(expression.strip('"'), path):
            return True
    return False


class ExternalPlatformEntryTests(SimpleTestCase):
    def test_gateway_and_wsgi_follow_existing_external_endpoints(self):
        from config.external_platform_routes import render_nginx
        from config.external_platform_wsgi import ExternalPlatformApplication
        from config.order_finance_routes import serves_order_finance

        routes = (
            "/api/open/oauth/token",
            "/api/taker/v1/hall/",
            "/api/integration/v1/orders/",
            "/api/wechatmp/callback",
            "/api/wecom/callback",
            "/api/wecom/archive/search/",
            "/api/qqbot/binding/",
            "/api/business/member/jidanpu-binding/test-connection/",
            "/api/business/member/jidanpu-sync-now/",
            "/api/business/member/jidanpu-push-now/",
            "/api/business/member/upstream-jidanpu-binding/",
            "/api/business/member/upstream-jidanpu-sync-now/",
            "/api/business/order-push-settings.json",
            "/api/business/order-qr/config/",
            "/api/business/peer-bindings/",
            "/api/business/peer-sync/bind/",
            "/api/business/peer-sync/inbound/events/",
            "/api/business/peer-sync/unbind/",
            "/api/business/platform-auto-replies/",
            "/api/business/platform-integrations/7.json/",
            "/api/business/platform-integrations/settings/",
            "/api/business/platform-integrations.json/",
            "/api/business/platform-pushes/",
            "/api/business/read-platform-order/",
            "/api/business/shop-bindings/",
            "/api/business/tb-send-logs/",
            "/api/business/upstream-bindings/jidanpu/",
            "/api/business/upstream-price-change-logs/",
            "/api/business/wxpusher-config/",
            "/api/business/xgj-bindings/",
            "/api/business/xgj-shops/",
            "/api/business/xgj/order-detail/",
            "/api/business/xgj/push/order/",
        )
        django = Mock(return_value=[b"Django response"])
        application = ExternalPlatformApplication(django)
        include = render_nginx(18521)
        for path in routes:
            with self.subTest(path=path):
                resolve(path)
                self.assertTrue(nginx_matches_path(include, path))
                self.assertFalse(serves_order_finance(path))
                self.assertEqual(application({"PATH_INFO": path}, Mock()), [b"Django response"])

        for path in ("/api/business/peer-sync/bind", "/api/wechatmp", "/api/integration/v1"):
            with self.subTest(path=path):
                self.assertTrue(nginx_matches_path(include, path))
                self.assertEqual(application({"PATH_INFO": path}, Mock()), [b"Django response"])

        self.assertIn("location = /api/business/peer-sync/bind/ { client_max_body_size 64k; "
                      "include proxy_params; proxy_pass http://127.0.0.1:18521; }", include)
        self.assertNotIn("/healthz", include)
        self.assertEqual(application({"PATH_INFO": "/healthz"}, Mock()), [b"Django response"])

    def test_gateway_and_wsgi_leave_other_paths_to_their_original_handlers(self):
        from config.external_platform_routes import render_nginx
        from config.external_platform_wsgi import ExternalPlatformApplication

        django = Mock()
        application = ExternalPlatformApplication(django)
        include = render_nginx(18521)
        for path in (
            "/",
            "/orders/20261120000000000001",
            "/api/app/pay/notify/",
            "/api/auth/login",
            "/api/auth/wxpusher/login",
            "/wechatmp/bind-confirm",
            "/api/business/approvals/settings/",
            "/api/business/orders/20261120000000000001/platform-push/",
            "/api/business/orders/20261120000000000001/jidanpu-direct-assignment/",
            "/api/business/orders/xgj-receipt-sync.json",
            "/api/business/platform-integrations.json/unowned",
            "/api/business/xgj-receipts/summary/",
            "/api/integration/v10/orders/",
            "/api/internal-chat/health/",
            "/api/jidanpu/callback/abcdefghijklmnopqrstuv",
            "/api/media/blob/example",
            "/api/open-docs/",
            "/media/example",
        ):
            with self.subTest(path=path):
                self.assertFalse(nginx_matches_path(include, path))
                start = Mock()
                self.assertEqual(application({"PATH_INFO": path}, start), [b"Not found"])
                self.assertEqual(start.call_args.args[0], "404 Not Found")
        django.assert_not_called()


class ExternalPlatformOAuthTests(TenantTestCase):
    def _call(self, request):
        from config.external_platform_wsgi import application

        start = Mock()
        # Keep Django's WSGI connection cleanup outside the test transaction.
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        try:
            response = application(request.environ, start)
            try:
                return start.call_args.args[0], json.loads(b"".join(response))
            finally:
                response.close()
        finally:
            request_started.connect(close_old_connections)
            request_finished.connect(close_old_connections)
            connection.set_tenant(self.tenant)

    def test_openapi_route_authenticates_and_reads_only_the_requested_tenant(self):
        requests = RequestFactory()
        domain_a = self.get_test_tenant_domain()
        domain_b = "external-platform-b.test.local"
        tenant_b, _ = provision_tenant(schema_name="external_b", name="外部平台测试乙", domain=domain_b)
        tenants = (
            (self.tenant, domain_a, "external-platform-a", "合成项目甲"),
            (tenant_b, domain_b, "external-platform-b", "合成项目乙"),
        )
        order_no = "20261120000000000001"
        order_path = f"/api/open/v1/orders/{order_no}/"
        secret = "synthetic-external-platform-test-only"
        tokens = {}

        for tenant, domain, key, project in tenants:
            with tenant_context(tenant):
                creator = get_user_model().objects.create_user(username=key)
                ApiApp.objects.create(name=key, app_key=key, app_secret=secret, created_by=creator)
                game = Game.objects.create(name=project)
                Order.objects.create(game=game, order_no=order_no, project_text=project)

            status, payload = self._call(requests.post(
                "/api/open/oauth/token",
                f"grant_type=client_credentials&client_id={key}&client_secret={secret}",
                content_type="application/x-www-form-urlencoded",
                HTTP_HOST=domain,
            ))
            self.assertEqual(status, "200 OK", payload)
            tokens[domain] = payload["access_token"]
            status, payload = self._call(requests.get(
                order_path, HTTP_HOST=domain, HTTP_AUTHORIZATION=f"Bearer {tokens[domain]}",
            ))
            self.assertEqual(status, "200 OK", payload)
            self.assertEqual(payload["code"], 0)
            self.assertEqual(payload["data"]["order"]["project_text"], project)

        for credentials in ({}, {"HTTP_AUTHORIZATION": f"Bearer {tokens[domain_a]}"}):
            status, payload = self._call(requests.get(order_path, HTTP_HOST=domain_b, **credentials))
            self.assertEqual(status, "401 Unauthorized")
            self.assertIsNone(payload["data"])

        with tenant_context(tenant_b):
            app = ApiApp.objects.get(app_key="external-platform-b")
            app.state = ApiAppState.DISABLED
            app.save(update_fields=["state"])
        status, payload = self._call(requests.get(
            order_path, HTTP_HOST=domain_b, HTTP_AUTHORIZATION=f"Bearer {tokens[domain_b]}",
        ))
        self.assertEqual(status, "401 Unauthorized")
        self.assertIsNone(payload["data"])
