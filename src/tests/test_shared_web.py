import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import OperationalError
from django.http import StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from config.shared_web import SharedTenantMiddleware, SharedWebApplication, shared_cache_key


@override_settings(ALLOWED_HOSTS=["alfa.test", "bravo.test", "unregistered.test"], TENANT_DB_PREFIX="tt_")
class SharedWebRoutingTests(SimpleTestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.routes = Path(directory) / "routes.json"
        self.routes.write_text(json.dumps({
            hostname: {"schema": schema, "password": f"{schema}-password", "host": "127.0.0.1", "port": 5432}
            for hostname, schema in [("alfa.test", "alfa"), ("bravo.test", "bravo")]
        }))
        self.routes.chmod(0o600)

    def test_unregistered_host_is_rejected_before_django_or_postgresql(self):
        django_app = Mock()
        start_response = Mock()
        app = SharedWebApplication(django_app, self.routes)
        body = app({"HTTP_HOST": "unregistered.test"}, start_response)
        self.assertEqual(start_response.call_args.args[0], "404 Not Found")
        self.assertEqual(b"".join(body), b"Not found")
        django_app.assert_not_called()

    def test_bad_permissions_and_duplicate_hosts_fail_at_startup(self):
        self.routes.chmod(0o644)
        with self.assertRaisesMessage(ImproperlyConfigured, "Invalid shared web route file or permissions"):
            SharedWebApplication(Mock(), self.routes)
        self.routes.chmod(0o600)
        self.routes.write_text('{"alfa.test": {}, "alfa.test": {}}')
        with self.assertRaisesMessage(ImproperlyConfigured, "Invalid shared web route file or permissions"):
            SharedWebApplication(Mock(), self.routes)

    def test_bad_route_does_not_proceed_to_application(self):
        for schema in ["public", "platform"]:
            with self.subTest(schema=schema):
                self.routes.write_text(json.dumps({"alfa.test": {
                    "schema": schema, "password": "secret", "host": "127.0.0.1", "port": 5432,
                }}))
                with self.assertRaisesMessage(ImproperlyConfigured, "Invalid shared web route file or permissions"):
                    SharedWebApplication(Mock(), self.routes)

    def test_aliases_must_point_to_the_same_tenant_database(self):
        routes = json.loads(self.routes.read_text())
        routes["bravo.test"] = routes["alfa.test"].copy()
        self.routes.write_text(json.dumps(routes))
        app = SharedWebApplication(lambda environ, start_response: StreamingHttpResponse([
            connections["default"].settings_dict["NAME"].encode(),
        ]), self.routes)
        self.assertEqual(list(app({"HTTP_HOST": "bravo.test"}, Mock())), [b"tt_alfa"])

        routes["bravo.test"]["port"] += 1
        self.routes.write_text(json.dumps(routes))
        with self.assertRaisesMessage(ImproperlyConfigured, "Invalid shared web route file or permissions"):
            SharedWebApplication(Mock(), self.routes)

    def test_request_stream_and_close_use_their_own_database(self):
        default = connections["default"]
        seen = []
        wrappers = []

        def django_app(environ, start_response):
            database = connections["default"]
            wrappers.append(database)
            self.assertNotEqual(database, default)
            self.assertEqual(database.settings_dict["NAME"], database.settings_dict["USER"])
            self.assertEqual(database.settings_dict["CONN_MAX_AGE"], 0)
            self.assertNotIn("wsgi.file_wrapper", environ)
            seen.append(("call", database.settings_dict["NAME"]))

            def chunks():
                yield shared_cache_key("shared-key", "", 1).encode()
                yield connections["default"].settings_dict["NAME"].encode()

            response = StreamingHttpResponse(chunks())
            response._resource_closers.append(lambda: seen.append(("close", connections["default"].settings_dict["NAME"])))
            start_response("200 OK", [])
            return response

        app = SharedWebApplication(django_app, self.routes)
        alfa_response = app({"HTTP_HOST": "alfa.test:8000", "wsgi.file_wrapper": Mock()}, Mock())
        bravo_response = app({"HTTP_HOST": "bravo.test"}, Mock())
        self.assertIs(connections["default"], default)
        self.assertIsNot(wrappers[0], wrappers[1])
        alfa_chunks = iter(alfa_response)
        bravo_chunks = iter(bravo_response)
        self.assertEqual(next(alfa_chunks), b":1:alfa:shared-key")
        self.assertIs(connections["default"], default)
        self.assertEqual(next(bravo_chunks), b":1:bravo:shared-key")
        self.assertEqual(next(alfa_chunks), b"tt_alfa")
        alfa_chunks.close()
        self.assertEqual(list(bravo_chunks), [b"tt_bravo"])
        alfa_response.close()
        bravo_response.close()
        self.assertIs(connections["default"], default)
        self.assertEqual(seen, [
            ("call", "tt_alfa"), ("call", "tt_bravo"), ("close", "tt_alfa"), ("close", "tt_bravo"),
        ])

    def test_failure_restores_default_for_the_next_request(self):
        default = connections["default"]
        database = None

        def django_app(environ, start_response):
            nonlocal database
            database = connections["default"]
            database.close = Mock()
            raise RuntimeError("fixture request failure")

        with self.assertRaisesMessage(RuntimeError, "fixture request failure"):
            SharedWebApplication(django_app, self.routes)({"HTTP_HOST": "alfa.test"}, Mock())
        database.close.assert_called_once_with()
        self.assertIs(connections["default"], default)

    def test_database_error_only_fails_the_bound_host(self):
        middleware = SharedTenantMiddleware(lambda request: request.tenant)
        requests = RequestFactory()

        def django_app(environ, start_response):
            return middleware(requests.get("/healthz", HTTP_HOST=environ["HTTP_HOST"]))

        app = SharedWebApplication(django_app, self.routes)
        with patch("django_tenants.middleware.main.TenantMainMiddleware.process_request", side_effect=OperationalError):
            with self.assertLogs("config.shared_web", level="WARNING"):
                response = app({"HTTP_HOST": "alfa.test"}, Mock())
            self.assertEqual(response.response.status_code, 503)
            self.assertEqual(list(response), [b"Service unavailable"])
        self.assertIsNone(getattr(connections["default"], "shared_web_schema", None))

    def test_domain_record_must_belong_to_configured_active_tenant(self):
        middleware = SharedTenantMiddleware(Mock())
        domain_model = Mock()
        domain_model.DoesNotExist = LookupError
        tenant = SimpleNamespace(schema_name="alfa", is_active=True)
        domain_model.objects.select_related.return_value.get.return_value.tenant = tenant

        def django_app(environ, start_response):
            found = middleware.get_tenant(domain_model, "alfa.test")
            self.assertIs(found, tenant)
            for wrong_schema, active in [("bravo", True), ("alfa", False)]:
                tenant.schema_name = wrong_schema
                tenant.is_active = active
                with self.assertRaises(LookupError):
                    middleware.get_tenant(domain_model, "alfa.test")
            with self.assertRaises(LookupError):
                middleware.get_tenant(domain_model, "bravo.test")
            return StreamingHttpResponse([])

        app = SharedWebApplication(django_app, self.routes)
        list(app({"HTTP_HOST": "alfa.test"}, Mock()))
        self.assertEqual(middleware.process_request(RequestFactory().get("/healthz")).status_code, 404)
