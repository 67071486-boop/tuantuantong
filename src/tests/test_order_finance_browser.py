import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.backends.db import SessionStore
from django.core.signals import request_finished, request_started
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType, Game, Order, OrderStatus, PeerHallMirror, PeerUpstreamBinding, Performance,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import (
    P2_TEST_PASSWORD, P2_TEST_USERNAME, _apply_games, _grant_nodes, permission_select_counts,
)

GATEWAY = "finance.browser.test.local"
AUDIENCE = f"https://{GATEWAY}"
TICKET_PATH = "/api/auth/order-finance-ticket/"
ORDER_NO = "20261123000000000091"
ORDER_PATH = f"/api/business/orders/{ORDER_NO}/"


class OrderFinanceBrowserTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.domain_a = self.get_test_tenant_domain()
        self.domain_b = "finance-b.test.local"
        self.origin_a = f"https://{self.domain_a}"
        self.origin_b = f"https://{self.domain_b}"
        self.tenant_b, _ = provision_tenant(schema_name="finance_b", name="乙工作室", domain=self.domain_b)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_file = Path(directory.name, "browser.json")
        self.config_file.write_text(json.dumps({
            "audience": AUDIENCE,
            "origins": {
                self.origin_a: {"schema": self.tenant.schema_name, "key": Fernet.generate_key().decode()},
                self.origin_b: {"schema": self.tenant_b.schema_name, "key": Fernet.generate_key().decode()},
            },
        }))
        self.config_file.chmod(0o600)
        self.enterContext(self.settings(
            ALLOWED_HOSTS=[self.domain_a, self.domain_b],
            TT_ORDER_FINANCE_BROWSER_FILE=str(self.config_file),
        ))
        self.client_a, self.user_a = self._fixture(self.tenant, self.domain_a, "甲订单项目")
        self.client_b, self.user_b = self._fixture(self.tenant_b, self.domain_b, "乙订单项目")
        self.assertEqual(self.user_a, self.user_b)

    @staticmethod
    def _fixture(tenant, domain, project):
        with tenant_context(tenant):
            game = Game.objects.create(name="合成测试游戏")
            Order.objects.create(game=game, order_no=ORDER_NO, project_text=project)
            user = get_user_model().objects.create_user(
                pk=42091, username=P2_TEST_USERNAME, password=P2_TEST_PASSWORD,
            )
            user.groups.add(Group.objects.get_or_create(name="客服")[0])
            _apply_games(user, [game])
            _grant_nodes(user, ("order.view_detail", "order.add_edit"))
            client = Client(enforce_csrf_checks=True, SERVER_NAME=domain)
            client.get("/api/auth/csrf", secure=True)
            response = client.post(
                "/api/auth/login",
                data={"username": P2_TEST_USERNAME, "password": P2_TEST_PASSWORD},
                content_type="application/json", secure=True, HTTP_ORIGIN=f"https://{domain}",
                HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
            )
            if response.status_code != 200:
                raise AssertionError(f"测试账号登录失败: {response.content[:200]!r}")
            return client, user.pk

    def _source(self, client=None, origin=None, *, with_csrf=True, path=TICKET_PATH):
        client = client or self.client_a
        origin = origin or self.origin_a
        csrf = {"HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value} if with_csrf else {}
        return client.post(path, secure=True, HTTP_ORIGIN=origin, **csrf)

    def _ticket(self, client=None, origin=None):
        response = self._source(client, origin)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        ticket = response.json()["data"]
        self.assertTrue(ticket["enabled"])
        self.assertEqual(ticket["ttl_seconds"], 120)
        self.assertEqual(ticket["user_id"], self.user_a)
        return ticket["token"]

    def _direct(self, token=None, origin=None, *, method="GET", path=ORDER_PATH, body=b"", **headers):
        from config.order_finance_wsgi import OrderFinanceApplication, web_application

        application = OrderFinanceApplication(web_application)
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"OrderFinance {token}"
        environ = RequestFactory().generic(
            method, path, body, content_type="application/json", secure=True, HTTP_HOST=GATEWAY,
            HTTP_ORIGIN=origin or self.origin_a, **headers,
        ).environ
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        try:
            start = Mock()
            result = application(environ, start)
            try:
                content = b"".join(result)
            finally:
                if hasattr(result, "close"):
                    result.close()
            status, response_headers = start.call_args.args[:2]
            return int(status.split()[0]), dict(response_headers), json.loads(content) if content else None
        finally:
            request_started.connect(close_old_connections)
            request_finished.connect(close_old_connections)

    def test_shared_hall_cache_cannot_leak_across_authenticated_tenants(self):
        for tenant in (self.tenant, self.tenant_b):
            with tenant_context(tenant):
                _grant_nodes(get_user_model().objects.get(pk=self.user_a), ("grab_hall.take_order",))
                Order.objects.filter(order_no=ORDER_NO).update(
                    status=OrderStatus.WAITING_GRAB, hall_price="13.00",
                )
                order = Order.objects.get(order_no=ORDER_NO)
                Performance.objects.get_or_create(order=order, emp_type=EmpType.MAIN)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.settings(TT_ORDER_FINANCE_RUNTIME_DIR=directory.name, HALL_LIST_CACHE_TTL_S=60):
            token_a = self._ticket()
            token_b = self._ticket(self.client_b, self.origin_b)
            path = "/api/business/hall/"

            def get(token, origin):
                status, _, response = self._direct(token, origin, path=path)
                self.assertEqual(status, 200, response)
                return [row["project_text"] for row in response["data"]["items"]]

            self.assertEqual(get(token_a, self.origin_a), ["甲订单项目"])
            with tenant_context(self.tenant):
                Order.objects.filter(order_no=ORDER_NO).update(project_text="合成缓存验收已修改")
            self.assertEqual(get(token_a, self.origin_a), ["甲订单项目"])
            self.assertEqual(get(token_b, self.origin_b), ["乙订单项目"])
            self.assertEqual(self._direct(token_a, self.origin_b, path=path)[0], 401)

    def test_shared_hall_cache_reuses_real_mirror_status_with_pagination(self):
        with tenant_context(self.tenant):
            user = get_user_model().objects.get(pk=self.user_a)
            _grant_nodes(user, ("grab_hall.take_order",))
            native = Order.objects.get(order_no=ORDER_NO)
            Order.objects.filter(pk=native.pk).update(status=OrderStatus.WAITING_GRAB, hall_price="13.00")
            Performance.objects.get_or_create(order=native, emp_type=EmpType.MAIN)
            binding = PeerUpstreamBinding.objects.create(
                peer_name="大厅测试上家", peer_base_url="https://hall-upper.test.invalid",
                peer_account="hall-mirror", token_in_hash="test-in", token_out="test-out",
                local_base_url=self.origin_a, bound_by=user, hall_sync_enabled=True,
            )
            mirror = PeerHallMirror.objects.create(
                peer_upstream_binding=binding, peer_base_url=binding.peer_base_url,
                peer_order_no="20261123000000000092", game=native.game,
                payload={"project_text": "合成镜像大厅订单"}, remote_hall_price="22.00",
                first_seen_at=timezone.now() + timedelta(minutes=1),
            )

        with tempfile.TemporaryDirectory() as directory, self.settings(
            TT_ORDER_FINANCE_RUNTIME_DIR=directory, HALL_LIST_CACHE_TTL_S=60,
        ):
            token = self._ticket()

            def get(page):
                status, _, response = self._direct(token, path=f"/api/business/hall/?page={page}&page_size=1")
                self.assertEqual(status, 200, response)
                data = response["data"]
                self.assertEqual((data["page"], data["page_size"], data["total"]), (page, 1, 2))
                self.assertEqual(len(data["items"]), 1)
                return data["items"][0]

            first = get(1)
            self.assertEqual(
                (first["origin"], first["status"], first["hall_price"]), ("peer", "WAITING_GRAB", "22.00"),
            )
            self.assertEqual(first["project_text"], "合成镜像大厅订单")
            self.assertEqual(get(2)["order_no"], ORDER_NO)
            with tenant_context(self.tenant):
                PeerHallMirror.objects.filter(pk=mirror.pk).update(payload={"project_text": "缓存后更新的镜像订单"})
            self.assertEqual(get(1), first)

    def test_browser_can_see_the_retry_interval_for_heavy_reads(self):
        from apps.core.order_finance_runtime import FinanceRuntime

        with tenant_context(self.tenant):
            _grant_nodes(get_user_model().objects.get(pk=self.user_a), ("order.list",))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.settings(
            TT_ORDER_FINANCE_RUNTIME_DIR=directory.name,
            TT_ORDER_FINANCE_GLOBAL_HEAVY_READS=2,
            TT_ORDER_FINANCE_TENANT_HEAVY_READS=1,
        ):
            token = self._ticket()
            with FinanceRuntime(directory.name).admit(self.tenant.schema_name, global_limit=2, tenant_limit=1) as refusal:
                self.assertIsNone(refusal)
                status, headers, response = self._direct(token, path="/api/business/orders/")
            self.assertEqual(status, 429, response)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertIn("Retry-After", headers["Access-Control-Expose-Headers"])

    def test_handoff_screenshot_upload_stays_at_the_original_same_origin(self):
        ticket = self._ticket()
        for path in ("/api/business/screenshot-browser", "/api/business/screenshot-browser/exchange/"):
            status, _, _ = self._direct(ticket, method="POST", path=path)
            self.assertEqual(status, 404)

    def test_ticket_requires_session_and_csrf_then_crosses_only_to_its_order(self):
        self.assertEqual(self._source(with_csrf=False).status_code, 403)
        self.assertEqual(
            self._source(origin=self.origin_b).status_code, 403,
        )
        anonymous = Client(SERVER_NAME=self.domain_a)
        self.assertEqual(anonymous.post(TICKET_PATH, secure=True, HTTP_ORIGIN=self.origin_a).status_code, 401)

        first = self._ticket()
        second = self._ticket(self.client_b, self.origin_b)
        for token, origin, project in (
            (first, self.origin_a, "甲订单项目"),
            (second, self.origin_b, "乙订单项目"),
        ):
            status, headers, payload = self._direct(token, origin, HTTP_X_FORWARDED_HOST=self.domain_b)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["project_text"], project)
            self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
            self.assertIn("Content-Disposition", headers["Access-Control-Expose-Headers"])
            self.assertNotIn("Access-Control-Allow-Credentials", headers)
        self.assertEqual(self._direct(first, self.origin_b)[0], 401)
        self.assertEqual(self._direct(second)[0], 401)

    def test_hall_ticket_get_reads_only_its_tenant_and_uses_current_permissions(self):
        for tenant, user_id in ((self.tenant, self.user_a), (self.tenant_b, self.user_b)):
            with tenant_context(tenant):
                order = Order.objects.get(order_no=ORDER_NO)
                order.status = OrderStatus.WAITING_GRAB
                order.save(update_fields=["status"])
                Performance.objects.create(order=order, emp_type=EmpType.MAIN)
                _grant_nodes(get_user_model().objects.get(pk=user_id), ("grab_hall.take_order",))

        hall = "/api/business/hall/"
        ticket_a = self._ticket()
        ticket_b = self._ticket(self.client_b, self.origin_b)
        for token, origin, project in (
            (ticket_a, self.origin_a, "甲订单项目"),
            (ticket_b, self.origin_b, "乙订单项目"),
        ):
            with CaptureQueriesContext(connection) as queries:
                status, _, payload = self._direct(token, origin, path=hall)
            self.assertEqual(status, 200)
            self.assertEqual([row["project_text"] for row in payload["data"]["items"]], [project])
            self.assertEqual(
                permission_select_counts(queries.captured_queries),
                {"admin_group": 1, "permission_nodes": 1},
            )

        with tenant_context(self.tenant_b):
            get_user_model().objects.get(pk=self.user_b).groups.clear()
        self.assertEqual(self._direct(ticket_b, self.origin_b, path=hall)[0], 403)
        self.assertEqual(self._direct(ticket_a, self.origin_a, path=hall)[0], 200)

    def test_public_entry_denies_mixed_credentials_and_preserves_web_only_routes(self):
        token = self._ticket()
        self.assertEqual(self._direct(token, HTTP_COOKIE="sessionid=untrusted")[0], 403)
        self.assertEqual(self._direct(HTTP_AUTHORIZATION="Bearer untrusted")[0], 401)
        self.assertEqual(self._direct(token, path="/api/business/orders/xgj-receipt-sync.json")[0], 404)
        self.assertEqual(self._direct(token, path="/api/auth/me")[0], 404)
        self.assertEqual(self._direct(token, method="TRACE")[0], 405)
        self.assertEqual(self._direct(token, method="HEAD")[0], 200)
        status, headers, _ = self._direct(token, origin="https://unknown.test.local")
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_preflight_needs_supported_method_and_headers_without_a_ticket(self):
        status, headers, _ = self._direct(
            method="OPTIONS", HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type",
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Methods"], "PATCH")
        self.assertEqual(headers["Access-Control-Allow-Headers"], "authorization, content-type")
        self.assertEqual(self._direct(
            method="OPTIONS", HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, authorization",
        )[0], 403)
        self.assertEqual(self._direct(
            method="OPTIONS", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, x-csrftoken",
        )[0], 403)

    def test_browser_write_uses_existing_permissions_and_changes_only_its_tenant(self):
        token_a = self._ticket()
        token_b = self._ticket(self.client_b, self.origin_b)
        status, _, payload = self._direct(
            token_a, method="PATCH", body=json.dumps({"project_text": "甲改后的订单项目"}).encode(),
        )
        self.assertEqual((status, payload["code"]), (200, 0), payload)
        with tenant_context(self.tenant):
            self.assertEqual(Order.objects.get(order_no=ORDER_NO).project_text, "甲改后的订单项目")
        with tenant_context(self.tenant_b):
            get_user_model().objects.get(pk=self.user_b).groups.clear()
        status, _, payload = self._direct(
            token_b, self.origin_b, method="PATCH", body=b'{"project_text":"different"}',
        )
        self.assertEqual((status, payload["code"]), (403, 403), payload)
        with tenant_context(self.tenant_b):
            self.assertEqual(Order.objects.get(order_no=ORDER_NO).project_text, "乙订单项目")

    def test_revoked_session_and_changed_password_invalidate_ticket(self):
        token_a = self._ticket()
        token_b = self._ticket(self.client_b, self.origin_b)
        with tenant_context(self.tenant_b):
            user = get_user_model().objects.get(pk=self.user_b)
            user.set_password("new-synthetic-password-for-test")
            user.save(update_fields=["password"])
        self.assertEqual(self._direct(token_b, self.origin_b)[0], 401)
        logout = self._source(path="/api/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(self._direct(token_a)[0], 401)

    def test_independent_secret_keys_permit_live_session_and_authenticate_no_stale_snapshot(self):
        token = self._ticket()
        session_key = self.client_a.cookies[settings.SESSION_COOKIE_NAME].value
        with self.settings(SECRET_KEY="independent-order-finance-server-secret", SECRET_KEY_FALLBACKS=[]):
            status, _, payload = self._direct(token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["project_text"], "甲订单项目")

        with tenant_context(self.tenant):
            session = SessionStore(session_key=session_key)
            session["synthetic_preference"] = "changed"
            session.save()
        with self.settings(SECRET_KEY="independent-order-finance-server-secret", SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(self._direct(token)[0], 401)
        refreshed = self._ticket()
        with tenant_context(self.tenant):
            session.cycle_key()
        with self.settings(SECRET_KEY="independent-order-finance-server-secret", SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(self._direct(refreshed)[0], 401)

    def test_expired_or_modified_ticket_returns_cross_origin_401(self):
        token = self._ticket()
        index = len(token) // 2
        changed = token[:index] + ("B" if token[index] == "A" else "A") + token[index + 1:]
        status, headers, _ = self._direct(changed)
        self.assertEqual(status, 401)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.origin_a)
        with patch("cryptography.fernet.time.time", return_value=time.time() + 121):
            self.assertEqual(self._direct(token)[0], 401)

    def test_unconfigured_tenant_keeps_same_origin_mode(self):
        with self.settings(TT_ORDER_FINANCE_BROWSER_FILE=""):
            response = self._source()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"enabled": False})
