import json
import os
import threading
import time
from contextlib import closing, contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.business.management.commands.shared_peer_sync import scheduler_lock
from apps.business.peer_client import (
    PeerUnreachable, enforce_total_peer_timeout, fetch_hall_page,
    new_peer_deadline_loop, push_events,
)
from apps.business.peer_worker import DeliveryWindow
from apps.business.shared_peer_sync import DeliveryTurn, TenantJobs, TenantLane, load_tenant_routes


class SharedPeerSchedulerTests(SimpleTestCase):
    def test_backlogged_tenant_cannot_take_both_threads_or_skip_waiting_tenant(self):
        lane = TenantLane(("a", "b", "c"))
        lane.poll()
        self.assertEqual(lane.take(), "a")
        for _ in range(10):
            lane.wake("a")
        self.assertEqual(lane.take(), "b")
        self.assertIsNone(lane.take())

        lane.complete("a", more=True)
        self.assertEqual(lane.take(), "c")
        lane.complete("b", more=False)
        self.assertEqual(lane.take(), "a")
        self.assertIsNone(lane.take())

    def test_timed_out_tenant_does_not_hold_slot_needed_by_other_tenant(self):
        lane = TenantLane(("a", "b"))
        lane.poll()
        self.assertEqual(lane.take(), "a")
        self.assertEqual(lane.take(), "b")
        lane.complete("b", more=True)
        self.assertEqual(lane.take(), "b")
        lane.complete("b", more=False)
        self.assertIsNone(lane.take())

        lane.complete("a", more=False)
        lane.poll()
        self.assertEqual(lane.take(), "a")
        self.assertEqual(lane.take(), "b")

    def test_wakeup_during_current_batch_is_retained_once(self):
        lane = TenantLane(("a", "b"))
        self.assertIsNone(lane.take())
        lane.wake("b")
        self.assertEqual(lane.take(), "b")
        lane.wake("b")
        lane.wake("b")
        self.assertIsNone(lane.take())
        lane.complete("b", more=False)
        self.assertEqual(lane.take(), "b")
        lane.complete("b", more=False)
        self.assertIsNone(lane.take())

    def test_delayed_retry_releases_slot_and_wakes_only_due_tenant(self):
        lane = TenantLane(("a", "b", "c"))
        lane.poll()
        self.assertEqual(lane.take(), "a")
        self.assertEqual(lane.take(), "b")
        lane.complete("a", more=False, wake_at=7)
        lane.wake_due(6.9)
        self.assertEqual(lane.take(), "c")
        self.assertIsNone(lane.take())
        lane.complete("b", more=False)
        lane.wake_due(7)
        lane.wake_due(8)
        self.assertEqual(lane.take(), "a")
        lane.complete("a", more=False)
        self.assertIsNone(lane.take())

    def test_due_retry_runs_before_regular_maintenance_for_its_binding(self):
        jobs = TenantJobs()
        stop = threading.Event()
        retry_at = timezone.now() + timedelta(seconds=5)
        deferred = {"retry_at": retry_at, "retry_binding_pk": 17, "pages": 0}
        with (
            patch("apps.business.shared_peer_sync.peer_hall.reclaim_stuck_claims", return_value=0) as reclaim,
            patch("apps.business.shared_peer_sync.peer_worker.run_reconcile", return_value={"requeued": 0, "errors": 0}) as reconcile,
            patch("apps.business.shared_peer_sync.peer_worker.run_hall_pull", side_effect=[deferred, {}]) as pull,
        ):
            self.assertEqual(jobs.maintain("a", stop), retry_at)
            with patch("apps.business.shared_peer_sync.timezone.now", return_value=retry_at - timedelta(seconds=1)):
                self.assertEqual(jobs.maintain("a", stop), retry_at)
            with patch("apps.business.shared_peer_sync.timezone.now", return_value=retry_at):
                self.assertFalse(jobs.maintain("a", stop))
        self.assertEqual(reclaim.call_count, 1)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(pull.call_count, 2)
        pull.assert_called_with(retry_binding_pk=17)


class SharedPeerRouteTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.filename = Path(directory.name) / "routes.json"
        self.route = {"schema": "accept_a", "password": "synth-db-pass", "host": "127.0.0.1", "port": 5432}

    def write_routes(self, config, mode=0o600):
        self.filename.write_text(json.dumps(config))
        self.filename.chmod(mode)

    @override_settings(ALLOWED_HOSTS=["a.test.invalid", "b.test.invalid"], TENANT_DB_PREFIX="tt_")
    def test_database_and_role_come_from_the_explicit_tenant_allowlist(self):
        self.write_routes({"a.test.invalid": self.route})
        routes = load_tenant_routes(self.filename)
        self.assertEqual(list(routes), ["accept_a"])
        hostname, route = routes["accept_a"]
        self.assertEqual(hostname, "a.test.invalid")
        self.assertEqual(route.database, "tt_accept_a")
        self.assertNotIn(self.route["password"], repr(route))

        self.write_routes({"a.test.invalid": {**self.route, "user": "postgres"}})
        with self.assertRaises(ImproperlyConfigured):
            load_tenant_routes(self.filename)

    @override_settings(ALLOWED_HOSTS=["a.test.invalid", "b.test.invalid"])
    def test_rejects_public_aliases_and_world_readable_manifests(self):
        for config, mode in (
            ({"a.test.invalid": {**self.route, "schema": "public"}}, 0o600),
            ({"a.test.invalid": self.route, "b.test.invalid": self.route}, 0o600),
            ({"a.test.invalid": self.route}, 0o644),
        ):
            with self.subTest(config=tuple(config), mode=mode):
                self.write_routes(config, mode)
                with self.assertRaises(ImproperlyConfigured):
                    load_tenant_routes(self.filename)

    def test_rejects_second_scheduler_for_the_same_lock(self):
        filename = self.filename.with_name("scheduler.lock")
        with scheduler_lock(filename):
            with self.assertRaisesMessage(CommandError, "不能取得共享同步锁"), scheduler_lock(filename):
                self.fail("Second scheduler acquired the lock")
        with scheduler_lock(filename):
            self.assertTrue(filename.is_file())


    @override_settings(ALLOWED_HOSTS=["a.test.invalid"])
    def test_check_routes_checks_readonly_and_refuses_chat_without_starting_a_worker(self):
        self.write_routes({"a.test.invalid": self.route})

        @contextmanager
        def identify(_host, _route, *, readonly):
            self.assertTrue(readonly)
            yield

        with (
            patch("apps.business.management.commands.shared_peer_sync.settings") as command_settings,
            patch("apps.business.shared_peer_sync.tenant_database", side_effect=identify),
            patch("apps.business.shared_peer_sync.chat_cutover_blocker", return_value="有跨工作室聊天待投递"),
            patch("apps.business.shared_peer_sync.run_scheduler") as launch,
        ):
            command_settings.TT_SHARED_WEB_ROUTES_FILE = str(self.filename)
            command_settings.DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
            with self.assertRaisesMessage(CommandError, "accept_a（有跨工作室聊天待投递）"):
                call_command("shared_peer_sync", "--check-routes", "--routes-file", str(self.filename))
        launch.assert_not_called()

    @override_settings(ALLOWED_HOSTS=["a.test.invalid", "b.test.invalid"])
    def test_only_new_tenant_needs_chat_admission_while_every_identity_is_checked(self):
        self.write_routes({"a.test.invalid": self.route, "b.test.invalid": {**self.route, "schema": "accept_b"}})
        identified, admitted = [], []

        @contextmanager
        def identify(_host, route, *, readonly):
            self.assertTrue(readonly)
            identified.append(route.schema)
            yield

        def blocker():
            schema = identified[-1]
            admitted.append(schema)
            return "有跨工作室聊天待投递" if schema == "accept_a" else ""

        with (
            patch("apps.business.management.commands.shared_peer_sync.settings") as command_settings,
            patch("apps.business.shared_peer_sync.tenant_database", side_effect=identify),
            patch("apps.business.shared_peer_sync.chat_cutover_blocker", side_effect=blocker),
        ):
            command_settings.TT_SHARED_WEB_ROUTES_FILE = str(self.filename)
            command_settings.DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
            command = ["shared_peer_sync", "--routes-file", str(self.filename)]
            call_command(*command, "--check-routes", "--admit-tenant", "accept_b")
            call_command(*command, "--check-route-identities")
        self.assertEqual(identified, ["accept_a", "accept_b"] * 2)
        self.assertEqual(admitted, ["accept_b"])


class DeliveryWindowRetryTests(SimpleTestCase):
    def test_partial_failure_settlement_retries_only_unfinished_bindings(self):
        window = DeliveryWindow(conn_failed={("downstream", "1"), ("downstream", "2")})
        now = timezone.now()
        settled = []

        def settle(_role, binding_id, **options):
            if binding_id == "2" and settled == ["1"]:
                settled.append("retry")
                raise OperationalError("synthetic lock conflict")
            settled.append(binding_id)
            return False

        with patch("apps.business.peer_worker._close_round", side_effect=settle):
            with self.assertRaises(OperationalError):
                window.close(now=now)
            window.close(now=now)
        self.assertEqual(settled, ["1", "retry", "2"])

    def test_shared_window_skips_the_locked_binding_and_delivers_the_next_batch(self):
        jobs = TenantJobs()
        now = timezone.now()
        window = DeliveryWindow(conn_failed={("downstream", "1"), ("downstream", "2")})
        jobs.delivery_windows["accept_a"] = DeliveryTurn(window, now, expires=0)
        settled = []

        def settle(_role, binding_id, *, now, since, nowait):
            self.assertTrue(nowait)
            if binding_id == "1":
                raise OperationalError("synthetic lock conflict")
            settled.append(binding_id)
            return False

        with (
            patch("apps.business.peer_worker._close_round", side_effect=settle),
            patch("apps.business.shared_peer_sync.has_pending_peer_chat", return_value=False),
            patch("apps.business.peer_worker.run_delivery_batch", return_value={"claimed": 0}) as deliver,
            self.assertLogs("apps.business.shared_peer_sync", level="WARNING") as logged,
        ):
            jobs.deliver("accept_a", threading.Event())
        self.assertEqual(settled, ["2"])
        self.assertIn("OperationalError", " ".join(logged.output))
        self.assertFalse(window.conn_failed)
        self.assertIsNot(deliver.call_args.kwargs["window"], window)


class SharedPeerHttpTests(SimpleTestCase):
    @contextmanager
    def hall_peer(self, *, chunk_pause):
        cursors = []

        class HallPeer(BaseHTTPRequestHandler):
            def do_POST(self):
                cursor = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["cursor"]
                cursors.append(cursor)
                body = json.dumps({
                    "code": 0,
                    "data": {
                        "shared": True,
                        "count": 2,
                        "rows": [{"target_ref": "A-2" if cursor else "A-1", "payload": {}}],
                        "next_cursor": None if cursor else "synthetic-next",
                        "complete": bool(cursor),
                    },
                }).encode()
                try:
                    chunks = [body] if cursor else [body[:4], body[4:8], body[8:12], body[12:16], body[16:]]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for chunk in chunks:
                        if not cursor:
                            time.sleep(chunk_pause)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), HallPeer) as server:
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}", cursors
            finally:
                server.shutdown()
                thread.join()

    def test_legacy_hall_pull_accepts_httpx_timeout_settings(self):
        with self.hall_peer(chunk_pause=0) as (base, _):
            for timeout in (None, httpx.Timeout(0.5, connect=1.0)):
                with self.subTest(timeout=timeout):
                    page = fetch_hall_page(base, "synthetic-peer-token", timeout=timeout)
                    self.assertEqual(page["rows"][0]["target_ref"], "A-1")

    def test_shared_hall_pull_accepts_progressing_page_and_follows_its_cursor(self):
        with self.hall_peer(chunk_pause=0.42) as (base, cursors):
            legacy = fetch_hall_page(base, "synthetic-peer-token")
            with closing(new_peer_deadline_loop()) as loop, enforce_total_peer_timeout(loop):
                shared = fetch_hall_page(base, "synthetic-peer-token")
                next_page = fetch_hall_page(base, "synthetic-peer-token", cursor=shared["next_cursor"])
        self.assertEqual(shared, legacy)
        self.assertEqual(next_page["rows"][0]["target_ref"], "A-2")
        self.assertTrue(next_page["complete"])
        self.assertEqual(cursors, [None, None, "synthetic-next"])

    def test_shared_hall_pull_still_bounds_a_continuously_arriving_page(self):
        with (
            self.hall_peer(chunk_pause=0.3) as (base, _),
            closing(new_peer_deadline_loop()) as loop,
            enforce_total_peer_timeout(loop),
        ):
            with self.assertRaisesMessage(PeerUnreachable, "总耗时超过"):
                fetch_hall_page(base, "synthetic-peer-token", timeout=0.6)

    def test_shared_hall_pull_keeps_the_short_wait_for_a_stalled_chunk(self):
        with (
            self.hall_peer(chunk_pause=0.8) as (base, _),
            closing(new_peer_deadline_loop()) as loop,
            enforce_total_peer_timeout(loop),
            self.assertRaises(PeerUnreachable) as error,
        ):
            fetch_hall_page(base, "synthetic-peer-token", timeout=0.5)
        self.assertIsInstance(error.exception.__cause__, httpx.ReadTimeout)

    def test_a_continuous_slow_response_still_hits_the_request_deadline(self):
        complete = threading.Event()

        class DrippingPeer(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"code":0,"data":{}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.05)
                    complete.set()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), DrippingPeer) as server:
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with closing(new_peer_deadline_loop()) as loop, enforce_total_peer_timeout(loop):
                    with self.assertRaisesMessage(PeerUnreachable, "总耗时超过"):
                        push_events(base, "synthetic-peer-token", [], timeout=0.35)
                self.assertFalse(complete.is_set())
                self.assertEqual(push_events(base, "synthetic-peer-token", [], timeout=0.35), {})
                self.assertTrue(complete.wait(0.5))
            finally:
                server.shutdown()
                thread.join()

    def test_slow_dns_does_not_add_resolvers_on_successive_turns(self):
        release = threading.Event()
        resolvers = []

        def blocked(*_args, **_kwargs):
            resolvers.append(threading.get_ident())
            release.wait(5)
            raise OSError("synthetic DNS unavailable")

        with (
            closing(new_peer_deadline_loop()) as loop,
            patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}),
            patch("socket.getaddrinfo", side_effect=blocked),
        ):
            try:
                for _ in range(3):
                    with (
                        enforce_total_peer_timeout(loop),
                        self.assertRaisesMessage(PeerUnreachable, "总耗时超过"),
                    ):
                        push_events("http://peer.test.invalid", "synthetic-peer-token", [], timeout=0.25)
                self.assertEqual(len(resolvers), 1)
            finally:
                release.set()
