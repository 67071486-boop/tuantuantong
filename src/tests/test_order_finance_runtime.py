from decimal import Decimal
from io import StringIO
import json
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core.envelope import api_response
from apps.core.order_finance_browser import BrowserGrant, GRANT_ENVIRON_KEY, OrderFinanceBrowserMiddleware

LIST = "/api/business/orders/"
DETAIL = "/api/business/orders/20261123000000000091/"


def _cache_in_child(path, arriving, entered, release, pipe):
    from apps.core.order_finance_runtime import active_hall_runtime
    from apps.core.short_cache import short_cached_payload

    def build():
        entered.set()
        release.wait(3)
        return {"label": "合成大厅结果"}

    with override_settings(TT_ORDER_FINANCE_RUNTIME_DIR=path):
        with (
            active_hall_runtime("finance_a"),
            patch("apps.core.short_cache.connection", schema_name="finance_a"),
            patch("apps.core.order_finance_runtime._CACHE_WAIT_S", 1),
        ):
            arriving.set()
            pipe.send(short_cached_payload(("hall/web/v1", ("可见",)), ttl=1, build=build))


def _hold_in_child(path, entered):
    from apps.core.order_finance_runtime import FinanceRuntime

    with FinanceRuntime(Path(path)).admit("finance_a", global_limit=2, tenant_limit=1) as refusal:
        if refusal:
            raise RuntimeError(refusal)
        entered.set()
        sleep(15)


class FinanceRuntimeTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        self.enterContext(override_settings(
            TT_ORDER_FINANCE_RUNTIME_DIR=str(self.runtime),
            TT_ORDER_FINANCE_GLOBAL_HEAVY_READS=2,
            TT_ORDER_FINANCE_TENANT_HEAVY_READS=1,
            TT_ORDER_FINANCE_DIAGNOSTICS_ENABLED=True,
        ))

    def _request(self, tenant, path, method="GET"):
        request = RequestFactory().generic(method, path, HTTP_AUTHORIZATION="OrderFinance TEST-SECRET")
        request.tenant = SimpleNamespace(schema_name=tenant, is_active=True)
        request.META[GRANT_ENVIRON_KEY] = BrowserGrant(tenant, 1, "TEST-SESSION", "", "")
        return request

    def test_heavy_read_limit_keeps_other_tenant_detail_and_writes_available(self):
        started = Event()
        release = Event()
        observed = []

        def browser_view(request):
            if request.tenant.schema_name == "finance_a" and request.method == "GET" and request.path == LIST:
                started.set()
                release.wait(3)
            if request.path == "/api/business/support-stats/":
                return api_response(code=1000, msg="测试参数错误")
            return HttpResponse("业务可用")

        app = OrderFinanceBrowserMiddleware(browser_view)

        def request(tenant, path, method="GET"):
            with patch("apps.core.order_finance_browser.connection", schema_name=tenant):
                return app(self._request(tenant, path, method))

        with patch("apps.core.order_finance_browser._session_user", return_value=SimpleNamespace(id=1)):
            first = Thread(target=lambda: observed.append(request("finance_a", LIST).status_code))
            first.start()
            try:
                self.assertTrue(started.wait(2))
                limited = request("finance_a", LIST + "?schema=finance_b&phrase=NOT_FOR_DIAGNOSTICS")
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited["Retry-After"], "1")
                self.assertEqual(json.loads(limited.content)["code"], 429)
                for path in ("/api/business/my-orders/export/", "/api/business/support-stats/"):
                    self.assertEqual(request("finance_a", path).status_code, 429, path)
                self.assertEqual(request("finance_b", LIST).status_code, 200)
                self.assertEqual(request("finance_a", DETAIL).status_code, 200)
                self.assertEqual(request("finance_a", LIST, "POST").status_code, 200)
            finally:
                release.set()
                first.join(3)
            self.assertEqual(observed, [200])
            self.assertEqual(request("finance_a", LIST).status_code, 200)
            self.assertEqual(request("finance_a", "/api/business/support-stats/").status_code, 200)

        output = StringIO()
        call_command("order_finance_diagnostics", stdout=output)
        rows = json.loads(output.getvalue())["groups"]
        self.assertTrue(any(row["tenant"] == "finance_a" and row["rejected"] == 1 for row in rows))
        support = next(row for row in rows if row["route"] == "support-stats")
        self.assertEqual((support["requests"], support["errors"], support["rejected"]), (2, 2, 1))
        for secret in ("测试参数错误", "NOT_FOR_DIAGNOSTICS", "20261123000000000091", "TEST-SECRET"):
            self.assertNotIn(secret, output.getvalue())

    def test_a_dead_worker_releases_admission_slots(self):
        from apps.core.order_finance_runtime import FinanceRuntime

        ctx = get_context("fork")
        entered = ctx.Event()
        worker = ctx.Process(target=_hold_in_child, args=(str(self.runtime), entered))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            runtime = FinanceRuntime(self.runtime)
            with runtime.admit("finance_a", global_limit=2, tenant_limit=1) as refusal:
                self.assertEqual(refusal, "busy")
            with runtime.admit("finance_b", global_limit=2, tenant_limit=1) as refusal:
                self.assertIsNone(refusal)
        finally:
            worker.kill()
            worker.join(3)
        with runtime.admit("finance_a", global_limit=2, tenant_limit=1) as refusal:
            self.assertIsNone(refusal)

    def test_same_hall_key_waiters_in_other_process_reuse_inflight_result(self):
        ctx = get_context("fork")
        entered, release, spare_entered = ctx.Event(), ctx.Event(), ctx.Event()
        arriving, spare_arriving = ctx.Event(), ctx.Event()
        first_result, write_first = ctx.Pipe(duplex=False)
        second_result, write_second = ctx.Pipe(duplex=False)
        first = ctx.Process(target=_cache_in_child, args=(str(self.runtime), arriving, entered, release, write_first))
        second = ctx.Process(target=_cache_in_child, args=(str(self.runtime), spare_arriving, spare_entered, release, write_second))
        first.start()
        try:
            self.assertTrue(entered.wait(3))
            second.start()
            self.assertTrue(spare_arriving.wait(3))
            self.assertFalse(second_result.poll(.025))
            release.set()
            self.assertTrue(first_result.poll(3))
            self.assertTrue(second_result.poll(3))
            self.assertEqual(first_result.recv(), {"label": "合成大厅结果"})
            self.assertEqual(second_result.recv(), {"label": "合成大厅结果"})
            self.assertFalse(spare_entered.is_set())
        finally:
            release.set()
            for worker in (first, second):
                if worker.pid is not None:
                    worker.join(3)
                    if worker.is_alive():
                        worker.kill()
                        worker.join(3)
            for pipe in (first_result, write_first, second_result, write_second):
                pipe.close()

    def test_hall_results_are_isolated_across_tenants_and_visible_permissions(self):
        from apps.core.order_finance_runtime import active_hall_runtime
        from apps.core.short_cache import short_cached_payload

        def read(schema, scope, value):
            with active_hall_runtime(schema), patch("apps.core.short_cache.connection", schema_name=schema):
                return short_cached_payload(("hall/web/v1", scope), ttl=1, build=lambda: {"total": value})

        self.assertEqual(read("finance_a", ("可看价格",), 9), {"total": 9})
        self.assertEqual(read("finance_b", ("可看价格",), 4), {"total": 4})
        self.assertEqual(read("finance_a", ("隐藏价格",), 0), {"total": 0})
        self.assertEqual(read("finance_a", ("可看价格",), 0), {"total": 9})

    def test_hall_failure_expiry_and_value_types(self):
        from apps.core.order_finance_runtime import active_hall_runtime
        from apps.core.short_cache import short_cached_payload

        def fail():
            raise ValueError("synthetic leader failure")

        with active_hall_runtime("finance_a"), patch("apps.core.short_cache.connection", schema_name="finance_a"):
            key = ("hall/web/v1", ("合成",))
            with patch("apps.core.order_finance_runtime.monotonic_ns", return_value=5_000_000_000) as wall:
                with self.assertRaisesMessage(ValueError, "synthetic leader failure"):
                    short_cached_payload(key, ttl=1, build=fail)
                self.assertEqual(short_cached_payload(key, ttl=1, build=lambda: Decimal("1.40")), Decimal("1.40"))
                self.assertEqual(short_cached_payload(key, ttl=1, build=fail), Decimal("1.40"))
                wall.return_value = 6_000_000_000
                self.assertEqual(short_cached_payload(key, ttl=1, build=lambda: Decimal("2.10")), Decimal("2.10"))

    def test_runtime_files_cannot_be_opened_from_an_untrusted_directory(self):
        from apps.core.order_finance_runtime import FinanceRuntime

        self.runtime.chmod(0o755)
        with self.assertRaises(PermissionError):
            FinanceRuntime(self.runtime)
        self.runtime.chmod(0o700)
        ready = FinanceRuntime(self.runtime)
        (self.runtime / "admit-global-0").symlink_to(self.runtime / "outside")
        with ready.admit("finance_a", global_limit=2, tenant_limit=1) as refusal:
            self.assertEqual(refusal, "unavailable")

    def test_a_business_exception_and_global_capacity_release_slots(self):
        from apps.core.order_finance_runtime import FinanceRuntime

        runtime = FinanceRuntime(self.runtime)

        def admit(schema):
            return runtime.admit(schema, global_limit=2, tenant_limit=1)

        with admit("finance_a") as one:
            self.assertIsNone(one)
            with self.assertRaisesMessage(RuntimeError, "synthetic request failure"):
                with admit("finance_b") as two:
                    self.assertIsNone(two)
                    with admit("finance_c") as refusal:
                        self.assertEqual(refusal, "busy")
                    raise RuntimeError("synthetic request failure")
            with admit("finance_c") as available:
                self.assertIsNone(available)

    def test_diagnostics_have_bounded_time_window_byte_and_bucket_counts(self):
        from apps.core.order_finance_runtime import FinanceRuntime

        runtime = FinanceRuntime(self.runtime)
        event = {"tenant": "finance_a", "route": "orders.list", "duration_ms": 17, "status": 200, "refused": False}
        with patch("apps.core.order_finance_runtime._DIAGNOSTIC_BYTES", 80):
            for minute in range(1, 12):
                with patch("apps.core.order_finance_runtime.time", return_value=minute * 60 + 5):
                    for _ in range(8):
                        runtime.record_diagnostic(**event)
            files = list(self.runtime.glob("diagnostics-*"))
            self.assertEqual(len(files), 6)
            self.assertTrue(all(file.stat().st_size <= 80 for file in files))
            with patch("apps.core.order_finance_runtime.time", return_value=11 * 60 + 6):
                self.assertEqual(list(runtime.recent(8)), [("finance_a", "orders.list", 200, 17, False, False)])
