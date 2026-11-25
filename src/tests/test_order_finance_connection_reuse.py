import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import psycopg
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.db import connection
from django.test import RequestFactory, TransactionTestCase, override_settings
from django_tenants.utils import get_tenant_model, tenant_context

from apps.platform.db_provisioning import drop_tenant_database, provision_database, tenant_database_name
from config.order_finance_wsgi import OrderFinanceApplication
from config.shared_web import SharedWebApplication, TenantRoute, _bind_database
from tests import test_order_finance_browser as browser_tests
from tests.p5_auth_helpers import _grant_nodes


class OrderFinanceConnectionReuseTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        directory = Path(cls.enterClassContext(TemporaryDirectory()))
        segment = re.sub(r"[^a-z0-9_]", "_", connection.settings_dict["NAME"].lower())[:24]
        host = connection.settings_dict["HOST"]
        port = int(connection.settings_dict["PORT"])
        cls.domains = ("finance-reuse-a.test.local", "finance-reuse-b.test.local")
        cls.routes = {}
        browser_origins = {}
        for label, domain in zip(("a", "b"), cls.domains, strict=True):
            schema = f"fin_reuse_{label}_{segment}"
            database = tenant_database_name(schema)
            password = token_hex(16)
            cls.addClassCleanup(drop_tenant_database, schema_name=schema, confirm=database)
            provision_database(schema_name=schema, password=password)
            route = cls.routes[domain] = TenantRoute(schema, database, password, host, port)
            db = route.connection()
            try:
                with _bind_database(db):
                    call_command("install_tenant", schema=schema, name=domain, domain=domain, stdout=StringIO())
            finally:
                db.close()
            browser_origins[f"https://{domain}"] = {"schema": schema, "key": Fernet.generate_key().decode()}
        cls.browser_file = directory / "browser.json"
        cls.browser_file.write_text(json.dumps({"audience": browser_tests.AUDIENCE, "origins": browser_origins}))
        cls.browser_file.chmod(0o600)
        cls.routes_file = directory / "routes.json"
        cls.routes_file.write_text(json.dumps({
            domain: dict(schema=route.schema, password=route.password, host=route.host, port=route.port)
            for domain, route in cls.routes.items()
        }))
        cls.routes_file.chmod(0o600)

        cls.enterClassContext(override_settings(
            ALLOWED_HOSTS=cls.domains,
            TT_ORDER_FINANCE_BROWSER_FILE=str(cls.browser_file),
        ))
        cls.clients = {}
        for domain, route in cls.routes.items():
            db = route.connection()
            try:
                with _bind_database(db):
                    tenant = get_tenant_model().objects.get(schema_name=route.schema)
                    cls.clients[domain], user_id = browser_tests.OrderFinanceBrowserTests._fixture(tenant, domain, domain)
                    with tenant_context(tenant):
                        _grant_nodes(get_user_model().objects.get(pk=user_id), (
                            "order.list", "grab_hall.take_order", "my_orders.send_to_my_system",
                        ))
            finally:
                db.close()

    def setUp(self):
        self.tickets = {}
        for domain, route in self.routes.items():
            db = route.connection()
            try:
                with _bind_database(db):
                    client = self.clients[domain]
                    ticket = client.post(
                        browser_tests.TICKET_PATH, secure=True, HTTP_ORIGIN=f"https://{domain}",
                        HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
                    )
                    self.assertEqual(ticket.status_code, 200, ticket.content)
                    self.tickets[domain] = ticket.json()["data"]["token"]
            finally:
                db.close()
        middleware = ["config.shared_web.SharedTenantMiddleware", *settings.MIDDLEWARE[1:]]
        self.enterContext(self.settings(MIDDLEWARE=middleware))
        django_app = get_wsgi_application()
        self.connections = []
        self.before_response = None

        def observe(environ, start_response):
            def observed_start(status, headers, exc_info=None):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_database(), current_user, pg_backend_pid()")
                    self.connections.append(cursor.fetchone())
                if self.before_response is not None:
                    self.before_response()
                return start_response(status, headers, exc_info)

            return django_app(environ, observed_start)

        self.web = SharedWebApplication(observe, self.routes_file)
        self.finance = OrderFinanceApplication(self.web)

    def _open(self, domain, *, ticket=None, path=browser_tests.ORDER_PATH, method="GET", host=browser_tests.GATEWAY,
              start=None, **headers):
        authorization = self.tickets[domain] if ticket is None else ticket
        environ = RequestFactory().generic(
            method, path, secure=True, HTTP_HOST=host, HTTP_ORIGIN=f"https://{domain}",
            HTTP_AUTHORIZATION=f"OrderFinance {authorization}", **headers,
        ).environ
        start = start or Mock()
        return self.finance(environ, start), start

    def _request(self, domain, **kwargs):
        response, start = self._open(domain, **kwargs)
        try:
            body = b"".join(response)
        finally:
            if hasattr(response, "close"):
                response.close()
        status, headers = start.call_args.args[:2]
        content_type = dict(headers).get("Content-Type", "")
        payload = json.loads(body) if body and content_type.startswith("application/json") else body
        return int(status.split()[0]), payload

    def _assert_order(self, domain, **headers):
        status, payload = self._request(domain, **headers)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["project_text"], domain)
        database, role, pid = self.connections[-1]
        self.assertEqual((database, role), (self.routes[domain].database,) * 2)
        return pid

    def test_verified_normal_reads_reuse_only_their_own_physical_database(self):
        first, second = self.domains
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=tuple(route.schema for route in self.routes.values())):
            first_pid = self._assert_order(first)
            second_pid = self._assert_order(second)
            self.assertNotEqual(first_pid, second_pid)
            self.assertEqual(self._request(second, ticket=self.tickets[first])[0], 401)
            self.assertEqual(len(self.connections), 2)
            self.assertEqual(self._assert_order(first), first_pid)
            self.assertEqual(self._assert_order(second), second_pid)
            with patch.dict(self.web.routes, {first: self.routes[second]}):
                self.assertEqual(self._request(first), (404, b"Not found"))
                self.assertNotEqual(self.connections[-1][2], second_pid)
            self.assertEqual(self._assert_order(second), second_pid)

        route_a, route_b = self.routes[first], self.routes[second]
        with self.assertRaisesRegex(psycopg.OperationalError, "permission denied for database"):
            psycopg.connect(
                dbname=route_b.database, user=route_a.database, password=route_a.password,
                host=route_a.host, port=route_a.port, connect_timeout=3,
            ).close()

    def test_only_configured_browser_get_paths_reuse(self):
        first, second = self.domains
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=()):
            self.assertNotEqual(self._assert_order(first), self._assert_order(first))
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=(self.routes[first].schema,)):
            pid = self._assert_order(first)
            self.assertNotEqual(self._assert_order(second), self._assert_order(second))
            for path in ("/api/business/hall/", "/api/business/orders/?page=1", "/api/business/my-orders/"):
                with self.subTest(path=path):
                    status, payload = self._request(first, path=path)
                    self.assertEqual((status, payload["code"]), (200, 0), payload)
                    self.assertEqual(self.connections[-1][2], pid)
            self.assertEqual(self._request(first, method="HEAD")[0], 200)
            self.assertNotEqual(self.connections[-1][2], pid)
            self.assertEqual(self._request(first, path="/api/business/orders.json")[0], 200)
            self.assertNotEqual(self.connections[-1][2], pid)
            cookies = "; ".join(f"{name}={cookie.value}" for name, cookie in self.clients[first].cookies.items())
            self.assertNotEqual(self._assert_order(first, host=first, HTTP_COOKIE=cookies), pid)
            self.assertEqual(self._assert_order(first), pid)

    def test_expiry_and_terminated_database_session_are_reopened_on_next_read(self):
        domain = self.domains[0]
        route = self.routes[domain]
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=(route.schema,)):
            pid = self._assert_order(domain)
            with psycopg.connect(
                dbname=route.database, user=route.database, password=route.password, host=route.host, port=route.port,
            ) as killer:
                self.assertEqual(killer.execute("SELECT pg_terminate_backend(%s, 5000)", [pid]).fetchone(), (True,))
            reconnected_pid = self._assert_order(domain)
            self.assertNotEqual(reconnected_pid, pid)
            self.assertEqual(self._assert_order(domain), reconnected_pid)
            with patch("django.db.backends.base.base.time") as db_time:
                db_time.monotonic.return_value = time.monotonic() + 61
                expired_pid = self._assert_order(domain)
                self.assertNotEqual(expired_pid, reconnected_pid)
                self.assertEqual(self._assert_order(domain), expired_pid)

    def test_aborted_response_and_leaked_transaction_drop_connection(self):
        domain = self.domains[0]
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=(self.routes[domain].schema,)):
            pid = self._assert_order(domain)
            with self.assertRaisesMessage(RuntimeError, "synthetic response start failure"):
                self._open(domain, start=Mock(side_effect=RuntimeError("synthetic response start failure")))
            self.assertEqual(self.connections[-1][2], pid)
            replacement = self._assert_order(domain)
            self.assertNotEqual(replacement, pid)

            response, _ = self._open(domain)
            body = iter(response)
            next(body)
            body.close()
            fresh = self._assert_order(domain)
            self.assertNotEqual(fresh, replacement)

            self.before_response = lambda: connection.set_autocommit(False)
            self.assertEqual(self._assert_order(domain), fresh)
            self.before_response = None
            self.assertNotEqual(self._assert_order(domain), fresh)

    def test_open_responses_cannot_lend_their_connection_to_another_request_or_thread(self):
        domain = self.domains[0]
        with self.settings(TT_ORDER_FINANCE_REUSE_SCHEMAS=(self.routes[domain].schema,)):
            first_response, _ = self._open(domain)
            try:
                first_pid = self.connections[-1][2]
                second_pid = self._assert_order(domain)
                self.assertNotEqual(second_pid, first_pid)
                self.assertEqual(json.loads(b"".join(first_response))["data"]["project_text"], domain)
            finally:
                first_response.close()
            self.assertEqual(self._assert_order(domain), second_pid)
            with ThreadPoolExecutor(max_workers=1) as executor:
                thread_pid = executor.submit(self._assert_order, domain).result(timeout=10)
                self.assertNotEqual(thread_pid, second_pid)
                self.assertEqual(executor.submit(self._assert_order, domain).result(timeout=10), thread_pid)
            self.assertEqual(self._assert_order(domain), second_pid)
