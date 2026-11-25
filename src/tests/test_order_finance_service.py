import json
from unittest.mock import Mock

from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


class OrderFinanceEntryTests(SimpleTestCase):
    def test_management_and_payment_callbacks_stay_on_the_existing_service(self):
        from config.order_finance_wsgi import OrderFinanceApplication

        web = Mock()
        application = OrderFinanceApplication(web)
        for path in (
            "/api/auth/login",
            "/api/business/games/",
            "/api/business/xgj/push/order/",
            "/api/business/peer-sync/inbound/events/",
            "/api/business/approvals/settings/",
            "/api/business/orders/xgj-receipt-sync/",
            "/api/business/orders-archive/",
            "/api/business/orders.json/unowned",
            "/api/app/pay/notify/",
            "/api/open/v1/orders/",
            "/api/taker/v1/hall/",
            "/api/integration/v1/orders/",
            "/api/media/blob/example",
        ):
            with self.subTest(path=path):
                start = Mock()
                response = application({"PATH_INFO": path}, start)
                self.assertEqual(start.call_args.args[0], "404 Not Found")
                self.assertEqual(b"".join(response), b"Not found")
        web.assert_not_called()

    def test_receipt_sync_format_variants_follow_the_originating_web_service(self):
        from config.order_finance_routes import render_nginx
        from config.order_finance_wsgi import OrderFinanceApplication

        web = Mock()
        application = OrderFinanceApplication(web)
        receipt_sync = "/api/business/orders/xgj-receipt-sync"
        original = resolve(receipt_sync + "/")
        for suffix in (".json", ".json/", ".api", ".api/"):
            with self.subTest(suffix=suffix):
                path = receipt_sync + suffix
                self.assertEqual(resolve(path).url_name, original.url_name)
                start = Mock()
                self.assertEqual(b"".join(application({"PATH_INFO": path}, start)), b"Not found")
                self.assertEqual(start.call_args.args[0], "404 Not Found")
        web.assert_not_called()

        include = render_nginx(18511, 18001)
        self.assertIn('location ~ "^/api/business/orders/xgj-receipt-sync[.][a-z0-9]+/?$" '
                      '{ include proxy_params; proxy_pass http://127.0.0.1:18001; }', include)
        self.assertNotIn("location ^~ /api/business/orders/", include)

    def test_proxy_and_service_follow_the_existing_order_and_finance_routes(self):
        from config.order_finance_routes import render_nginx
        from config.order_finance_wsgi import OrderFinanceApplication

        routes = (
            "/api/business/orders/",
            "/api/business/orders.json",
            "/api/business/approvals/",
            "/api/business/approvals/42/decision/",
            "/api/business/orders/20261120000000000001/refunds",
            "/api/business/hall/20261120000000000001/claim/",
            "/api/business/my-orders/",
            "/api/business/screenshot-browser/start",
            "/api/business/performances/export",
            "/api/business/settlements",
            "/api/business/online-payroll/mine/",
            "/api/business/wallet-transactions/",
            "/api/business/xgj-receipts/summary/",
            "/api/business/orders/batch/payment-status/",
            "/api/app/income/summary/",
            "/api/app/wallet/",
        )
        web = Mock(return_value=[b"business response"])
        application = OrderFinanceApplication(web)
        include = render_nginx(18511, 18001)
        for path in routes:
            with self.subTest(path=path):
                resolve(path)
                response = application({"PATH_INFO": path}, Mock())
                self.assertEqual(response, [b"business response"])

        self.assertIn("location = /api/business/settlements { include proxy_params; "
                      "proxy_pass http://127.0.0.1:18511; }", include)
        self.assertIn("location /api/business/orders/ { include proxy_params; "
                      "proxy_pass http://127.0.0.1:18511; }", include)
        self.assertIn("location = /api/business/orders/xgj-receipt-sync/ { include proxy_params; "
                      "proxy_pass http://127.0.0.1:18001; }", include)
        self.assertIn("location = /api/business/approvals/settings/ { include proxy_params; "
                      "proxy_pass http://127.0.0.1:18001; }", include)
        self.assertEqual(resolve("/api/business/approvals/settings/").url_name, "approval-settings")
        self.assertNotIn("/api/auth", include)
        self.assertNotIn("/api/app/pay/notify", include)
        self.assertEqual(application({"PATH_INFO": "/healthz"}, Mock()), [b"business response"])


class OrderFinanceSessionTests(TenantTestCase):
    def _get_from_service(self, path, domain, client):
        from config.order_finance_wsgi import application

        cookie = "; ".join(f"{name}={cookie.value}" for name, cookie in client.cookies.items())
        start = Mock()
        request = RequestFactory().get(path, HTTP_HOST=domain, HTTP_COOKIE=cookie)
        # Like ClientHandler, preserve the outer test transaction across WSGI signals.
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

    def test_web_session_reads_its_own_order_and_cannot_cross_tenants(self):
        domain_a = self.get_test_tenant_domain()
        domain_b = "order-finance-b.test.local"
        tenant_b, _ = provision_tenant(schema_name="finance_b", name="订单财务测试乙", domain=domain_b)
        order_no = "20261120000000000001"
        path = f"/api/business/orders/{order_no}/"

        def create_order_and_login(domain, project):
            game = Game.objects.create(name="合并服务测试游戏")
            Order.objects.create(game=game, order_no=order_no, project_text=project)
            return make_logged_in_client(
                domain=domain, groups=("客服",), nodes=("order.view_detail",), games=[game],
            )

        with tenant_context(self.tenant):
            client_a = create_order_and_login(domain_a, "仅甲租户可见的项目")
        with tenant_context(tenant_b):
            client_b = create_order_and_login(domain_b, "仅乙租户可见的项目")

        for domain, client, project in (
            (domain_a, client_a, "仅甲租户可见的项目"),
            (domain_b, client_b, "仅乙租户可见的项目"),
        ):
            status, payload = self._get_from_service(path, domain, client)
            self.assertEqual(status, "200 OK")
            self.assertEqual(payload["code"], 0, payload)
            self.assertEqual(payload["data"]["project_text"], project)

        status, payload = self._get_from_service(path, domain_b, client_a)
        self.assertEqual(status, "401 Unauthorized")
        self.assertIsNone(payload["data"])
