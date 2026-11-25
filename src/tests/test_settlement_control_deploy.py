"""独立服务部署脚本的本机模拟检查，不连接或修改目标机。"""
import importlib.util
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from cryptography.fernet import Fernet

MODULE = Path(__file__).resolve().parents[2] / "deploy/settlement-control/release.py"
spec = importlib.util.spec_from_file_location("settlement_control_release", MODULE)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(TestCase):
    def test_targets_are_explicit_and_restricted(self):
        release.validate_args("/opt/settlement-control", "r1", "pay.example.com", 18081, "settlement_prod")
        for root, version, host, port, db in [
            ("/home/tenant", "r1", "pay.example.com", 18081, "settlement_prod"),
            ("/opt/../other", "r1", "pay.example.com", 18081, "settlement_prod"),
            ("/opt/control", "../x", "pay.example.com", 18081, "settlement_prod"),
            ("/opt/control", "r1", "127.0.0.1", 18081, "settlement_prod"),
            ("/opt/control", "r1", "pay.example.com", 80, "settlement_prod"),
            ("/opt/control", "r1", "pay.example.com", 18081, "tenant_db"),
        ]:
            with self.subTest(root=root, version=version, host=host, port=port, db=db):
                with self.assertRaises(release.ReleaseError):
                    release.validate_args(root, version, host, port, db)

    def test_migration_mismatch_prevents_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a", Path(d) / "b"
            for folder in (a, b):
                migrations = folder / "src/apps/settlement_control/migrations"
                migrations.mkdir(parents=True)
                (migrations / "0001_initial.py").write_text("same migration")
            release.require_compatible(a, b)
            (b / "src/apps/settlement_control/migrations/0002_extra.py").write_text("new migration")
            with self.assertRaises(release.ReleaseError):
                release.require_compatible(a, b)

    def test_rollback_target_requires_a_prior_success_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "shared/verified").mkdir(parents=True)
            candidate = root / "releases/r1"
            folder = candidate / "src/apps/settlement_control/migrations"
            folder.mkdir(parents=True)
            (folder / "0001_initial.py").write_text("migration")
            with patch.object(release, "secure_file"):
                with self.assertRaises((release.ReleaseError, FileNotFoundError)):
                    release.verified_marker(root, candidate)
                release.verified_marker(root, candidate, create=True)
                release.verified_marker(root, candidate)
                (folder / "0001_initial.py").write_text("changed migration")
                with self.assertRaises(release.ReleaseError):
                    release.verified_marker(root, candidate)

    def test_preflight_https_requires_verified_certificate_and_postflight_exact_health(self):
        challenge = "a" * 32
        body = json.dumps({"ok": True, "service": "settlement-control", "instance": "b" * 32, "challenge": challenge}) + "\n200"
        with patch.object(release.secrets, "token_hex", return_value=challenge), patch.object(release, "run", return_value=body) as command:
            release.probe("pay.example.com", 18081, True, expect_ready=True)
        args = command.call_args.args[0]
        self.assertIn("pay.example.com:443:127.0.0.1", args)
        self.assertNotIn("--insecure", args)
        with patch.object(release, "run", return_value='{"ok":true,"service":"other"}\n200'):
            with self.assertRaises(release.ReleaseError):
                release.probe("pay.example.com", 18081, True, expect_ready=True)
        with patch.object(release.secrets, "token_hex", return_value="c" * 32), patch.object(release, "run", return_value=body):
            with self.assertRaises(release.ReleaseError):
                release.probe("pay.example.com", 18081, True, expect_ready=True)
        with patch.object(release.secrets, "token_hex", return_value=challenge), patch.object(release, "run", return_value=body):
            with self.assertRaises(release.ReleaseError):
                release.probe("pay.example.com", 18081, True, expect_ready=True, resource="authorizations")
        with patch.object(release, "run", return_value="bad gateway\n502"):
            release.probe("pay.example.com", 18081, True, expect_ready=False)
            with self.assertRaises(release.ReleaseError):
                release.probe("pay.example.com", 18081, True, expect_ready=True)

    def test_each_post_probe_checks_route_method_and_challenge(self):
        challenge = "a" * 32
        for route in ("create", "authorize", "refresh"):
            body = {"ok": True, "service": "settlement-control", "instance": "b" * 32,
                    "challenge": challenge, "route": route, "method": "POST"}
            with self.subTest(route=route), patch.object(release.secrets, "token_hex", return_value=challenge), \
                 patch.object(release, "run", return_value=json.dumps(body) + "\n200") as command:
                self.assertEqual(release.probe("pay.example.com", 18081, True, expect_ready=True, resource=route), "b" * 32)
                args = command.call_args.args[0]
                self.assertIn("X-Settlement-Probe: 1", args)
                self.assertEqual(json.loads(args[args.index("--data") + 1]), {"challenge": challenge})
                self.assertTrue(args[-1].endswith("/authorizations" if route == "create" else "/" + route))
                body["route"] = "wrong"
                command.return_value = json.dumps(body) + "\n200"
                with self.assertRaises(release.ReleaseError):
                    release.probe("pay.example.com", 18081, True, expect_ready=True, resource=route)

    def test_pid_cgroup_cwd_and_socket_must_all_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proc, groups, target = root / "proc", root / "cgroups", root / "release"
            (target / "src").mkdir(parents=True)
            (proc / "123/fd").mkdir(parents=True)
            (proc / "net").mkdir()
            (proc / "123/cwd").symlink_to(target / "src")
            (proc / "123/fd/7").symlink_to("socket:[500]")
            (proc / "net/tcp").write_text("header\n0: 0100007F:46A1 00000000:0000 0A 0 0 0 0 0 500\n")
            group = groups / "system.slice" / release.UNIT
            group.mkdir(parents=True)
            (group / "cgroup.procs").write_text("123\n")

            def command(args, **kwargs):
                if "--property=MainPID" in args:
                    return "123\n"
                if "--property=ControlGroup" in args:
                    return f"/system.slice/{release.UNIT}\n"
                return ""

            with patch.object(release, "PROC_ROOT", proc), patch.object(release, "CGROUP_ROOT", groups), patch.object(release, "run", side_effect=command):
                release.check_cgroup(target, 18081)
                (group / "cgroup.procs").write_text("124\n")
                with self.assertRaises(release.ReleaseError):
                    release.check_cgroup(target, 18081)
                (group / "cgroup.procs").write_text("123\n")
                (proc / "net/tcp").write_text("header\n0: 0100007F:46A1 00000000:0000 0A 0 0 0 0 0 501\n")
                with self.assertRaises(release.ReleaseError):
                    release.check_cgroup(target, 18081)

    def test_migration_failure_never_switches_release(self):
        with tempfile.TemporaryDirectory() as d:
            candidate = Path(d) / "candidate"
            actor = SimpleNamespace(pw_uid=123, pw_gid=456)
            group = SimpleNamespace(gr_gid=456)
            with patch.object(release.os, "geteuid", return_value=0), patch.object(release.pwd, "getpwnam", return_value=actor), \
                 patch.object(release.os, "getgrouplist", return_value=[456]), patch.object(release.grp, "getgrnam", return_value=group), patch.object(release, "verify_paths", return_value=candidate), \
                 patch.object(release, "previous_release", return_value=None), patch.object(release, "probe"), \
                 patch.object(release, "deployment_lock", return_value=nullcontext()), \
                 patch.object(release, "transient", side_effect=[None, release.ReleaseError("migration failure")]), \
                 patch.object(release, "switch") as switch:
                with self.assertRaises(release.ReleaseError):
                    release.execute("activate", "/opt/control", "r1", "pay.example.com", 18081, "settlement_prod")
            switch.assert_not_called()

    def test_concurrent_deployment_is_blocked_before_any_modification(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lock = root / "deploy.lock"
            lock.touch(mode=0o600)
            fake_stat = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1)
            with patch.object(release, "DEPLOY_LOCK", lock), patch.object(release, "validate_lock_directory"), patch.object(release, "validate_args"), \
                 patch.object(release.os, "geteuid", return_value=0), patch.object(release.os, "fstat", return_value=fake_stat), \
                 patch.object(release, "verify_paths") as verify:
                with release.deployment_lock():
                    with self.assertRaisesRegex(release.ReleaseError, "未作任何修改"):
                        release.execute("activate", "/srv/other-control", "r1", "pay.example.com", 18081, "settlement_prod")
                verify.assert_not_called()

    def test_service_user_cannot_have_additional_groups(self):
        actor, group = SimpleNamespace(pw_uid=123, pw_gid=456), SimpleNamespace(gr_gid=456)
        with patch.object(release.os, "geteuid", return_value=0), patch.object(release.pwd, "getpwnam", return_value=actor), \
             patch.object(release.grp, "getgrnam", return_value=group), patch.object(release.os, "getgrouplist", return_value=[456, 789]), \
             patch.object(release, "deployment_lock", return_value=nullcontext()), patch.object(release, "verify_paths") as verify:
            with self.assertRaises(release.ReleaseError):
                release.execute("activate", "/opt/control", "r1", "pay.example.com", 18081, "settlement_prod")
        verify.assert_not_called()

    def test_wrong_https_upstream_fails_even_when_both_report_healthy(self):
        with tempfile.TemporaryDirectory() as d:
            candidate = Path(d) / "candidate"
            actor, group = SimpleNamespace(pw_uid=123, pw_gid=456), SimpleNamespace(gr_gid=456)
            def probe(host, port, public, *, expect_ready, resource="healthz"):
                if not expect_ready:
                    return None
                return ("b" if public and resource == "authorizations" else "a") * 32
            with patch.object(release.os, "geteuid", return_value=0), patch.object(release.pwd, "getpwnam", return_value=actor), \
                 patch.object(release.os, "getgrouplist", return_value=[456]), patch.object(release.grp, "getgrnam", return_value=group), patch.object(release, "verify_paths", return_value=candidate), \
                 patch.object(release, "previous_release", return_value=None), patch.object(release, "deployment_lock", return_value=nullcontext()), \
                 patch.object(release, "probe", side_effect=probe), patch.object(release, "transient"), patch.object(release, "run"), \
                 patch.object(release, "check_cgroup"), patch.object(release, "switch"), patch.object(release.time, "sleep"), \
                 patch.object(release, "verified_marker") as verified, patch.object(release, "UNIT_DIR", Path(d)):
                with self.assertRaisesRegex(release.ReleaseError, "切换后检查失败"):
                    release.execute("activate", "/opt/control", "r1", "pay.example.com", 18081, "settlement_prod")
            verified.assert_not_called()

    def test_wrong_https_post_routes_never_mark_release_verified(self):
        actor, group = SimpleNamespace(pw_uid=123, pw_gid=456), SimpleNamespace(gr_gid=456)
        with tempfile.TemporaryDirectory() as d:
            candidate = Path(d) / "candidate"
            for wrong in ("create", "authorize", "refresh"):
                def probe(host, port, public, *, expect_ready, resource="healthz"):
                    return ("b" if public and resource == wrong else "a") * 32
                with self.subTest(route=wrong), patch.object(release.os, "geteuid", return_value=0), \
                     patch.object(release.pwd, "getpwnam", return_value=actor), patch.object(release.grp, "getgrnam", return_value=group), \
                     patch.object(release.os, "getgrouplist", return_value=[456]), patch.object(release, "verify_paths", return_value=candidate), \
                     patch.object(release, "previous_release", return_value=None), patch.object(release, "deployment_lock", return_value=nullcontext()), \
                     patch.object(release, "probe", side_effect=probe), patch.object(release, "transient"), patch.object(release, "run"), \
                     patch.object(release, "check_cgroup"), patch.object(release, "switch"), patch.object(release.time, "sleep"), \
                     patch.object(release, "verified_marker") as verified, patch.object(release, "UNIT_DIR", Path(d)):
                    with self.assertRaisesRegex(release.ReleaseError, "切换后检查失败"):
                        release.execute("activate", "/opt/control", "r1", "pay.example.com", 18081, "settlement_prod")
                    verified.assert_not_called()

    def test_rollback_does_not_require_old_service_or_proxy_to_be_healthy(self):
        with tempfile.TemporaryDirectory() as d:
            candidate, previous = Path(d) / "old", Path(d) / "broken"
            actor, group = SimpleNamespace(pw_uid=123, pw_gid=456), SimpleNamespace(gr_gid=456)
            with patch.object(release.os, "geteuid", return_value=0), patch.object(release.pwd, "getpwnam", return_value=actor), \
                 patch.object(release.os, "getgrouplist", return_value=[456]), patch.object(release.grp, "getgrnam", return_value=group), patch.object(release, "verify_paths", return_value=candidate), \
                 patch.object(release, "previous_release", return_value=previous), patch.object(release, "require_compatible"), \
                 patch.object(release, "deployment_lock", return_value=nullcontext()), patch.object(release, "transient") as transient, \
                 patch.object(release, "run"), patch.object(release, "check_cgroup"), patch.object(release, "switch") as switch, \
                 patch.object(release, "verified_marker"), patch.object(release, "UNIT_DIR", Path(d)):
                def probe(host, port, public, *, expect_ready, resource="healthz"):
                    self.assertTrue(switch.called, "rollback must not probe the broken old instance")
                    return "a" * 32
                with patch.object(release, "probe", side_effect=probe) as health:
                    release.execute("rollback", "/opt/control", "r1", "pay.example.com", 18081, "settlement_prod")
            self.assertEqual(health.call_count, 10)
            self.assertEqual(transient.call_count, 2)
            self.assertEqual(transient.call_args.args[2], ["manage.py", "migrate", "--check"])

    def test_secrets_file_must_be_exactly_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "service.env"
            path.write_text("placeholder")
            path.chmod(0o600)
            release.secure_file(path, os.getuid(), os.getgid(), 0o600)
            path.chmod(0o644)
            with self.assertRaises(release.ReleaseError):
                release.secure_file(path, os.getuid(), os.getgid(), 0o600)

    def test_duplicate_client_credentials_fail_before_database_access(self):
        env = {
            "SETTLEMENT_DJANGO_SECRET": "s" * 32, "SETTLEMENT_ALLOWED_HOSTS": "pay.example.com",
            "SETTLEMENT_DB_NAME": "settlement_test", "SETTLEMENT_DB_USER": "control_test",
            "SETTLEMENT_DB_PASSWORD": "test-password", "SETTLEMENT_FERNET_KEY": Fernet.generate_key().decode(),
            "SETTLEMENT_OPERATION_LOCK_FILE": "/synthetic/lock", "SETTLEMENT_USERNAME": "test-user",
            "SETTLEMENT_PASSWORD": "test-password", "SETTLEMENT_MERCHANT": "test-merchant",
            "SETTLEMENT_TESSERACT": "/bin/true", "SETTLEMENT_UPSTREAM_URL": "https://agent.jeepay.vip",
            "SETTLEMENT_CLIENT_KEYS": '{"client_a":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","client_b":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
            "SETTLEMENT_CLIENT_NAMES": '{"client_a":"A","client_b":"B"}',
        }
        result = subprocess.run([sys.executable, "-c", release.PREFLIGHT, "pay.example.com", "settlement_test", "/synthetic/lock"],
                                env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 12)
        self.assertEqual(result.stdout, b"")
