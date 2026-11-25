import importlib.util
from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


CLI = Path(__file__).resolve().parents[2] / "deploy/internal-chat/chat-gateway-release.py"
OLD = "a" * 40
NEW = "b" * 40
HELPER = CLI.with_name("internal-chat-gateway.sh")


def load_release():
    spec = importlib.util.spec_from_file_location("chat_gateway_release", CLI)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    return release


class ChatGatewayReleaseTests(unittest.TestCase):
    def test_package_uses_the_commit_and_stage_checks_hash_before_installing(self):
        release = load_release()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "gateway.tar.gz"
            second = root / "same-commit.tar.gz"
            gateway = release.Gateway(root, "chat.test.invalid", root / "unused")
            release.build("HEAD", output)
            release.build("HEAD", second)
            self.assertEqual(output.read_bytes(), second.read_bytes())
            with tarfile.open(output) as archive:
                self.assertCountEqual(archive.getnames(), ["gateway.mjs", "package.json", "package-lock.json", "VERSION"])
                version = archive.extractfile("VERSION").read().decode("ascii").strip()

            def npm_ci(command, *, check, cwd, timeout):
                self.assertEqual(command, ["/usr/bin/npm", "ci", "--omit=dev", "--ignore-scripts"])
                self.assertTrue(check)
                self.assertGreater(timeout, 0)
                dependency = cwd / "node_modules/socket.io/package.json"
                dependency.parent.mkdir(parents=True)
                dependency.write_text("{}\n")

            with patch.object(release.subprocess, "run", side_effect=npm_ci) as install:
                with self.assertRaisesRegex(release.ReleaseError, "SHA256"):
                    gateway.stage(output, "0" * 64)
                install.assert_not_called()
                gateway.stage(output, release.digest(output))
                gateway.stage(output, release.digest(output))
                self.assertEqual(install.call_count, 1)
            self.assertFalse(gateway.current.exists())
            self.assertTrue((gateway.releases / version / "node_modules/socket.io/package.json").is_file())

    def test_failed_candidate_restores_the_serving_release(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = load_release()
            route = root / "nginx-target"
            route.write_text("server 127.0.0.1:4192;\n")
            gateway = release.Gateway(root, "chat.test.invalid", route)
            for directory in (gateway.legacy, gateway.releases / OLD, gateway.releases / NEW):
                (directory / "node_modules/socket.io").mkdir(parents=True)
                for name in (*release.FILES, "node_modules/socket.io/package.json"):
                    (directory / name).write_text("fixture\n")
            (gateway.releases / OLD / "READY").touch()
            gateway.current.symlink_to(gateway.releases / OLD)
            running = {release.INDEPENDENT: gateway.releases / OLD}
            failures = set()

            def systemctl(command, unit):
                if command == "start":
                    running[unit] = gateway.legacy if unit == release.LEGACY else gateway.current.resolve()
                elif command == "stop":
                    running.pop(unit, None)

            def wait_direct(port, directory):
                self.assertEqual(running[gateway.unit(port)], directory)
                if ("direct", directory.name) in failures:
                    raise release.ReleaseError("fixture: candidate unhealthy")

            def check_routes():
                selected = gateway.unit(gateway.selected())
                self.assertIn(selected, running)
                if ("route", running[selected].name, gateway.current.resolve().name) in failures:
                    raise release.ReleaseError("fixture: route unhealthy")

            def route_to(port):
                self.assertIn(gateway.unit(port), running)
                route.write_text(f"server 127.0.0.1:{port};\n")

            with (patch.object(release, "systemctl", side_effect=systemctl),
                  patch.object(gateway, "wait_direct", side_effect=wait_direct),
                  patch.object(gateway, "check_routes", side_effect=check_routes),
                  patch.object(gateway, "route_to", side_effect=route_to)):
                for case, points, message in (
                    ("fallback failed before updating current", {("route", "legacy", OLD)}, "route unhealthy"),
                    ("candidate failed after changing routes", {("route", NEW, NEW)}, "route unhealthy"),
                    ("candidate and fallback failed", {("direct", NEW), ("route", "legacy", NEW)},
                     "candidate unhealthy"),
                ):
                    with self.subTest(case=case):
                        failures = points
                        with self.assertRaisesRegex(release.ReleaseError, message):
                            gateway.transition(NEW)
                        self.assertEqual(gateway.current.resolve(), gateway.releases / OLD)
                        self.assertEqual(route.read_text(), "server 127.0.0.1:4192;\n")
                        self.assertEqual(running, {release.INDEPENDENT: gateway.releases / OLD})
                failures = set()
                with self.assertRaisesRegex(release.ReleaseError, "此前通过探活"):
                    gateway.transition(NEW, rollback=True)
            self.assertEqual(gateway.current.resolve(), gateway.releases / OLD)
            self.assertEqual(route.read_text(), "server 127.0.0.1:4192;\n")
            self.assertEqual(running, {release.INDEPENDENT: gateway.releases / OLD})
            self.assertFalse((gateway.releases / NEW / "READY").exists())

            with (patch.object(release, "systemctl", side_effect=systemctl),
                  patch.object(gateway, "wait_direct", side_effect=wait_direct),
                  patch.object(gateway, "check_routes", side_effect=check_routes),
                  patch.object(gateway, "route_to", side_effect=route_to)):
                gateway.transition("legacy", rollback=True)
                self.assertEqual(running, {release.LEGACY: gateway.legacy})
                self.assertEqual(gateway.selected(), 4191)
                gateway.transition(OLD, rollback=True)
                self.assertEqual(running, {release.INDEPENDENT: gateway.releases / OLD})
                self.assertEqual(gateway.selected(), 4192)

    def test_candidate_needs_a_direct_websocket_upgrade(self):
        release = load_release()
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            gateway = release.Gateway(root, "chat.test.invalid", root / "unused")
            candidate = gateway.releases / NEW
            (root / "cwd").symlink_to(candidate)

            def status(_host, _port, path, **_kwargs):
                return {release.POLLING: 200, "/notify": 400,
                        "/socket.io/?EIO=4&transport=websocket": 403}[path]

            with (patch.object(release, "service_pid", return_value=root),
                  patch.object(release, "owns_listener", return_value=True),
                  patch.object(release, "gateway_secret", return_value="synthetic-key"),
                  patch.object(release, "http_status", side_effect=status),
                  patch.object(release.time, "sleep")):
                with self.assertRaisesRegex(release.ReleaseError, "WebSocket 握手返回 HTTP 403，预期 101"):
                    gateway.wait_direct(4192, candidate)

    def test_running_pid_must_belong_to_the_systemd_unit(self):
        release = load_release()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / "system.slice" / release.INDEPENDENT
            group.mkdir(parents=True)
            members = group / "cgroup.procs"
            members.write_text("100\n")
            unit = f"MainPID=99\nControlGroup=/system.slice/{release.INDEPENDENT}\n".encode()
            with patch.object(release, "CGROUP_ROOT", root, create=True), patch.object(release, "run", return_value=unit):
                with self.assertRaisesRegex(release.ReleaseError, "主进程不在服务控制组"):
                    release.service_pid(release.INDEPENDENT)
                members.write_text("99\n100\n")
                self.assertEqual(release.service_pid(release.INDEPENDENT), release.PROC_ROOT / "99")

    def test_first_activation_enables_the_service_before_publishing_its_route(self):
        release = load_release()
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            route = root / "nginx-target"
            route.write_text("server 127.0.0.1:4191;\n")
            gateway = release.Gateway(root, "chat.test.invalid", route)
            for directory in (gateway.legacy, gateway.releases / NEW):
                (directory / "node_modules/socket.io").mkdir(parents=True)
                for name in (*release.FILES, "node_modules/socket.io/package.json"):
                    (directory / name).write_text("fixture\n")
            enabled = {release.LEGACY}

            def systemctl(command, unit):
                if command == "enable":
                    enabled.add(unit)

            def publish(port):
                # After a power loss, systemd must start the service the route points at.
                self.assertIn(gateway.unit(port), enabled)
                route.write_text(f"server 127.0.0.1:{port};\n")

            with (patch.object(release, "systemctl", side_effect=systemctl),
                  patch.object(gateway, "wait_direct"), patch.object(gateway, "check_routes"),
                  patch.object(gateway, "route_to", side_effect=publish)):
                gateway.transition(NEW)
            self.assertEqual(gateway.selected(), 4192)
            self.assertTrue((gateway.releases / NEW / "READY").is_file())

    def test_failed_first_activation_clears_current_even_if_public_checks_keep_failing(self):
        release = load_release()
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            route = root / "nginx-target"
            route.write_text("server 127.0.0.1:4191;\n")
            gateway = release.Gateway(root, "chat.test.invalid", route)
            (gateway.releases / NEW).mkdir(parents=True)
            running = {release.LEGACY}

            def systemctl(command, unit):
                if command == "start":
                    running.add(unit)
                elif command == "stop":
                    running.discard(unit)

            def publish(port):
                route.write_text(f"server 127.0.0.1:{port};\n")

            error = release.ReleaseError("fixture: public TLS outage")
            stderr = StringIO()
            with (patch.object(release, "systemctl", side_effect=systemctl),
                  patch.object(gateway, "check_directory"), patch.object(gateway, "wait_direct"),
                  patch.object(gateway, "check_routes", side_effect=error) as probe,
                  patch.object(gateway, "route_to", side_effect=publish), redirect_stderr(stderr)):
                with self.assertRaisesRegex(release.ReleaseError, "恢复未通过"):
                    gateway.transition(NEW)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(gateway.selected(), 4191)
            self.assertEqual(running, {release.LEGACY})
            self.assertIsNone(gateway.current_release())
            self.assertFalse((gateway.releases / NEW / "READY").exists())
            self.assertNotIn("已恢复原上游并通过探活", stderr.getvalue())

    def test_web_hook_checks_the_target_and_keeps_both_gateway_services_untouched(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared"
            shared.mkdir()
            (shared / "chat-gateway-release.json").write_text("{}\n")
            (shared / "internal-chat.env").touch()
            release = root / "older-web"
            assets = release / "src/frontend/dist/internal-chat-webk"
            venv = release / "src/.venv/bin"
            venv.mkdir(parents=True)
            python = venv / "python"
            python.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TRACE"\n')
            python.chmod(0o755)
            trace = root / "trace"
            script = r'''
set -euo pipefail
source "$1"
sudo() { echo 'web release must keep the gateway services untouched' >&2; return 1; }
systemctl() { echo 'web release must keep the gateway services untouched' >&2; return 1; }
prepare_internal_chat_gateway "$2" "$3"
check_independent_internal_chat_web "$2" "$3" chat.test.invalid
configure_internal_chat_gateway "$2" "$3" chat.test.invalid
'''
            command = ["bash", "-c", script, "bash", HELPER, root, release]
            missing = subprocess.run(command, capture_output=True, env={**os.environ, "TRACE": str(trace)})
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse(trace.exists())
            assets.mkdir(parents=True)
            (assets / "index.html").write_text("<html>old web assets</html>\n")
            result = subprocess.run(command, capture_output=True, env={**os.environ, "TRACE": str(trace)})
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(trace.read_text().splitlines(), [
                f"{shared}/chat-gateway-release.py --root {root} web-check chat.test.invalid",
                f"{shared}/chat-gateway-release.py --root {root} web-check chat.test.invalid --live",
            ])

    def test_route_checks_require_public_upgrade_and_both_notify_statuses(self):
        release = load_release()
        gateway = release.Gateway(Path.cwd(), "chat.test.invalid", Path("unused"))
        statuses = {("chat.test.invalid", "POST", "/notify", None): 404,
                    ("127.0.0.1", "POST", "/notify", None): 401,
                    ("127.0.0.1", "POST", "/notify", "synthetic-key"): 400,
                    ("chat.test.invalid", "GET", release.POLLING, None): 200,
                    ("chat.test.invalid", "GET", "/socket.io/?EIO=4&transport=websocket", None): 101}

        def status(host, _port, path, *, method="GET", token=None, **_kwargs):
            return statuses[host, method, path, token]

        with patch.object(release, "http_status", side_effect=status), patch.object(release.time, "sleep"):
            gateway.check_routes("synthetic-key")
            statuses["127.0.0.1", "POST", "/notify", "synthetic-key"] = 401
            with self.assertRaisesRegex(release.ReleaseError, "已授权通知返回 HTTP 401，预期 400"):
                gateway.check_routes("synthetic-key")
