"""Check group ownership transitions across the systemd boundary."""
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "deploy/shared-peer-worker/group-release.py"
SPEC = importlib.util.spec_from_file_location("shared_peer_group_release", SOURCE)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class OfflineSystemd:
    def __init__(self, root, units, old_src, shared_src):
        self.root = root
        self.units = units
        self.states = {}
        self.groups = {}
        self.directories = {}
        self.calls = []
        self.blocked = set()
        self.fail_start = False
        self.interrupt_start = False
        self.hold_shared = False
        self.private_route = True
        self.next_pid = 100
        self.shared_src = shared_src
        self.states[release.ROUTE_SERVICE] = {"ActiveState": "active"}
        self.states[release.SHARED_ROUTE_SERVICE] = {"ActiveState": "active"}
        for name in release.TENANTS:
            unit = release.legacy(name)
            self.states[unit] = self.unit_state("enabled", old_src)
            self.states[unit].update(
                FragmentPath=str(self.units / unit),
                ExecStart=f"{{ argv[]={old_src}/.venv/bin/python manage.py peer_sync --tenant {name} --loop ; }}",
            )
            self.start(unit, old_src, 1)

    def unit_state(self, enabled, cwd):
        return dict(LoadState="loaded", ActiveState="inactive", UnitFileState=enabled,
                    MainPID="0", NRestarts="0", ControlGroup="", WorkingDirectory=str(cwd),
                    User=release.USER, Group=release.USER, DropInPaths="")

    def show(self, unit):
        return self.states.get(unit, dict(LoadState="not-found", ActiveState="inactive",
                                          UnitFileState="", MainPID="0", ControlGroup=""))

    def allowed_database(self, host, port):
        return host == "198.51.100.23" and port == 25432

    def verify_routed_identity(self):
        release.require(self.private_route, "共享服务账号未走后台专用数据库网络")

    def members(self, group):
        return self.groups.get(group, [])

    def process(self, pid):
        unit = next(unit for unit, group in self.groups.items() if pid in group).split("/")[-1]
        main = int(self.states[unit]["MainPID"])
        command = b"shared_peer_sync" if unit == release.SHARED else b"peer_sync"
        return main if pid != main else 1, self.directories[pid], [b"manage.py", command, b"--loop"], str(pid)

    def start(self, unit, cwd, count):
        state = self.states[unit]
        if state["ActiveState"] == "active":
            return
        pids = list(range(self.next_pid, self.next_pid + count))
        self.next_pid += count
        group = f"/system.slice/{unit}"
        state.update(ActiveState="active", ControlGroup=group, MainPID=str(pids[0]))
        self.groups[group] = pids
        for pid in pids:
            self.directories[pid] = cwd

    def check_route_file(self, file, *, admitting=None):
        tenants = release.route_names(json.loads(file.read_text()))
        if admitting in self.blocked:
            raise subprocess.CalledProcessError(1, "preflight")
        return tenants

    def preflight(self, _src, routes, _hosts, _secret, *, admitting=None):
        self.calls.append(("preflight",))
        self.check_route_file(routes, admitting=admitting)

    def command(self, action, *args):
        self.calls.append((action, *args))
        if action == "daemon-reload":
            if (self.units / release.SHARED).exists():
                self.states.setdefault(release.SHARED, self.unit_state("disabled", self.shared_src))
            else:
                self.states.pop(release.SHARED, None)
            return
        unit = args[-1]
        state = self.states[unit]
        if action in {"enable", "disable"}:
            state["UnitFileState"] = action + "d"
        if action == "disable" and "--now" in args:
            group = state["ControlGroup"]
            if unit == release.SHARED and self.hold_shared:
                return
            self.groups.pop(group, None)
            state.update(ActiveState="inactive", MainPID="0", ControlGroup="")
        if action != "start":
            return
        if unit == release.SHARED:
            if self.interrupt_start:
                self.interrupt_start = False
                raise SystemExit("interrupted")
            if self.fail_start:
                self.fail_start = False
                raise subprocess.CalledProcessError(1, "systemctl start")
            tenants = self.check_route_file(self.root / "active-routes.json")
            for tenant in tenants:
                old = self.show(release.legacy(tenant))
                assert old["ActiveState"] == "inactive" and old["UnitFileState"] == "disabled"
                assert (self.root / "fences" / tenant).is_file()
            self.start(unit, self.shared_src, 3)
        else:
            tenant = next(name for name in release.TENANTS if release.legacy(name) == unit)
            assert not (self.root / "fences" / tenant).exists()
            if self.show(release.SHARED)["ActiveState"] == "active":
                active = self.check_route_file(self.root / "active-routes.json")
                assert tenant not in active
            self.start(unit, Path(state["WorkingDirectory"]), 1)


class GroupReleaseTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name).resolve()
        self.root, self.units = parent / "configuration", parent / "units"
        self.root.mkdir()
        self.units.mkdir()
        sha = "f" * 40
        releases = parent / "releases"
        self.shared_src = releases / sha / "src"
        python = self.shared_src / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        scheduler = self.shared_src / "apps/business/management/commands/shared_peer_sync.py"
        scheduler.parent.mkdir(parents=True)
        scheduler.touch()
        self.old_src = parent / "legacy" / ("e" * 40) / "src"
        (self.root / "release").write_text(sha)
        (self.root / "django-secret").write_text("synthetic-only")
        (self.root / "django-secret").chmod(0o600)
        (self.root / "hosts").write_text("127.0.0.1 localhost\n")
        tenants = {}
        self.catalog = {}
        for name, (domain, _) in release.TENANTS.items():
            hosts = parent / f"{name}-hosts"
            hosts.write_text("127.0.0.1 LOCALHOST # same mapping\n")
            tenants[name] = domain, str(hosts)
            self.catalog[domain] = dict(schema=name, host="198.51.100.23", port=25432, password=f"synthetic-{name}")
        self.write_catalog()
        self.enterContext(patch.object(release, "TENANTS", tenants))
        self.enterContext(patch.object(release.time, "sleep"))
        self.enterContext(patch.object(release, "STABLE_SECONDS", 0))
        self.enterContext(patch.object(release, "LEGACY_RELEASES", self.old_src.parent.parent))
        self.system = OfflineSystemd(self.root, self.units, self.old_src, self.shared_src)
        self.group = release.Group(self.root, releases, self.units, self.system)
        self.original = {tenant: self.system.show(release.legacy(tenant))["MainPID"] for tenant in tenants}
        self.group.install()

    def write_catalog(self):
        path = self.root / "catalog.json"
        path.write_text(json.dumps(self.catalog))
        path.chmod(0o600)

    def routes(self):
        return release.route_names(json.loads((self.root / "active-routes.json").read_text()))

    def assert_legacy(self, *tenants):
        for tenant in tenants:
            state = self.system.show(release.legacy(tenant))
            self.assertEqual((state["ActiveState"], state["UnitFileState"]), ("active", "enabled"))
            self.assertFalse(self.group.fence(tenant).exists())

    def test_install_keeps_old_workers_running_and_shared_disabled(self):
        self.assertEqual(self.system.calls, [("daemon-reload",)])
        self.assert_legacy(*self.original)
        self.assertEqual(self.group.state(), ([], None))
        self.assertEqual(self.system.show(release.SHARED)["UnitFileState"], "disabled")
        self.assertEqual(self.system.show(release.SHARED)["ActiveState"], "inactive")

    def test_add_and_remove_one_without_disturbing_independent_worker(self):
        self.group.transition("feimeng", add=True)
        self.group.transition("zhouyu", add=True)
        self.assertEqual(self.routes(), ["feimeng", "zhouyu"])
        self.group.transition("feimeng", add=False)
        self.assertEqual(self.group.state(), (["zhouyu"], None))
        self.assertEqual(self.routes(), ["zhouyu"])
        self.assert_legacy("feimeng", "chuangling")
        self.assertEqual(self.system.show(release.legacy("chuangling"))["MainPID"], self.original["chuangling"])

    def test_bad_route_or_pending_chat_leaves_all_service_ownership_unchanged(self):
        self.catalog["fm.suqing.chat"]["schema"] = "zhouyu"
        self.write_catalog()
        initial = list(self.system.calls)
        with self.assertRaisesRegex(release.ReleaseError, "专库路由无效"):
            self.group.transition("feimeng", add=True)
        self.assertEqual(self.system.calls, initial)
        self.catalog["fm.suqing.chat"]["schema"] = "feimeng"
        self.write_catalog()
        self.system.blocked.add("feimeng")
        with self.assertRaises(subprocess.CalledProcessError):
            self.group.transition("feimeng", add=True)
        self.assertEqual(self.system.calls, initial + [("preflight",)])
        self.assertEqual(self.group.state(), ([], None))
        self.assert_legacy(*self.original)

    def test_worker_without_private_route_leaves_legacy_ownership_unchanged(self):
        self.system.private_route = False
        initial = list(self.system.calls)
        with self.assertRaisesRegex(release.ReleaseError, "未走后台专用数据库网络"):
            self.group.transition("feimeng", add=True)
        self.assertEqual(self.system.calls, initial)
        self.assertEqual(self.group.state(), ([], None))
        self.assert_legacy(*self.original)

    def test_unexpected_legacy_unit_command_is_rejected_before_stopping(self):
        self.system.show(release.legacy("feimeng"))["ExecStart"] += "{ argv[]=/usr/bin/true ; }"
        initial = list(self.system.calls)
        with self.assertRaisesRegex(release.ReleaseError, "旧同步命令不一致"):
            self.group.transition("feimeng", add=True)
        self.assertEqual(self.system.calls, initial)
        self.assert_legacy(*self.original)

    def test_recheck_after_old_owner_stops_refuses_new_chat_in_admission(self):
        actual_preflight = self.system.preflight

        def insert_chat(*args, admitting):
            if self.system.show(release.legacy(admitting))["ActiveState"] == "inactive":
                self.system.blocked.add(admitting)
            actual_preflight(*args, admitting=admitting)

        with patch.object(self.system, "preflight", side_effect=insert_chat):
            with self.assertRaises(subprocess.CalledProcessError):
                self.group.transition("feimeng", add=True)
        self.assert_legacy(*self.original)
        self.assertEqual(self.group.state(), ([], None))
        self.assertNotIn(("start", release.SHARED), self.system.calls)

    def test_failed_restart_restores_prior_shared_membership_and_legacy_worker(self):
        self.group.transition("feimeng", add=True)
        self.system.fail_start = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.group.transition("zhouyu", add=True)
        self.assert_legacy("zhouyu", "chuangling")
        self.assertEqual(self.routes(), ["feimeng"])
        self.assertEqual(self.group.state(), (["feimeng"], None))

    def test_interrupted_operation_uses_saved_routes_even_if_catalog_changed(self):
        self.group.transition("feimeng", add=True)
        self.system.interrupt_start = True
        with self.assertRaises(SystemExit):
            self.group.transition("zhouyu", add=True)
        self.catalog["fm.suqing.chat"]["password"] = "rotated"
        self.write_catalog()
        self.group.recover()
        self.assert_legacy("zhouyu", "chuangling")
        self.assertEqual(self.group.state(), (["feimeng"], None))
        active = json.loads((self.root / "active-routes.json").read_text())
        self.assertEqual(active["fm.suqing.chat"]["password"], "synthetic-feimeng")

    def test_remove_affected_tenant_even_after_shared_service_failed(self):
        self.group.transition("feimeng", add=True)
        self.group.transition("zhouyu", add=True)
        self.system.blocked.add("feimeng")
        self.system.command("disable", "--now", release.SHARED)
        self.system.show(release.SHARED)["ActiveState"] = "failed"
        self.group.transition("feimeng", add=False)
        self.assert_legacy("feimeng", "chuangling")
        self.assertEqual(self.group.state(), (["zhouyu"], None))
        self.assertEqual(self.system.show(release.SHARED)["ActiveState"], "active")

    def test_remove_uses_active_snapshot_after_other_tenant_catalog_and_chat_change(self):
        self.group.transition("feimeng", add=True)
        self.group.transition("zhouyu", add=True)
        self.catalog["zhouyu.suqing.chat"]["password"] = "future-password"
        self.write_catalog()
        Path(release.TENANTS["zhouyu"][1]).write_text("127.1.1.2 new-staged-host\n")
        self.system.blocked.add("zhouyu")
        self.group.transition("feimeng", add=False)
        self.assert_legacy("feimeng", "chuangling")
        self.assertEqual(self.group.state(), (["zhouyu"], None))
        active = json.loads((self.root / "active-routes.json").read_text())
        self.assertEqual(active["zhouyu.suqing.chat"]["password"], "synthetic-zhouyu")

    def test_legacy_recovery_does_not_need_shared_interpreter_or_readable_active_routes(self):
        self.group.transition("feimeng", add=True)
        self.group.transition("zhouyu", add=True)
        (self.shared_src / ".venv/bin/python").unlink()
        routes = self.root / "active-routes.json"
        routes.write_text("broken")
        routes.chmod(0o444)
        self.group.recover(legacy_only=True)
        self.assert_legacy(*self.original)
        self.assertEqual(self.group.state(), ([], None))
        self.assertFalse(routes.exists())

    def test_readiness_waits_for_worker_command_instead_of_counting_only_pids(self):
        self.group.transition("feimeng", add=True)
        main = int(self.system.show(release.SHARED)["MainPID"])
        settled = self.system.process(main)
        wrapper = (settled[0], settled[1], [b"group-release.py", b"worker-run"], settled[3])
        actual_process = self.system.process
        observed = iter((wrapper, None, settled))
        with patch.object(self.system, "process", side_effect=lambda pid: next(observed) if pid == main
                          else actual_process(pid)):
            self.group.verify_running(release.SHARED, 3, self.shared_src)
        self.assertEqual(list(observed), [])

    def test_shared_processes_that_cannot_stop_keep_legacy_fences(self):
        self.group.transition("feimeng", add=True)
        self.system.hold_shared = True
        with self.assertRaisesRegex(release.ReleaseError, "仍有进程"):
            self.group.transition("feimeng", add=False)
        self.assertEqual(self.group.state(), (["feimeng"], []))
        self.assertTrue(self.group.fence("feimeng").is_file())
        self.assertEqual(self.system.show(release.legacy("feimeng"))["ActiveState"], "inactive")
        self.system.hold_shared = False
        self.group.recover(legacy_only=True)
        self.assert_legacy(*self.original)
        self.assertEqual(self.group.state(), ([], None))


class WorkerIdentityTests(TestCase):
    def test_account_cannot_share_a_worker_uid(self):
        account = SimpleNamespace(pw_name=release.USER, pw_uid=321, pw_gid=321, pw_shell="/usr/sbin/nologin")
        group = SimpleNamespace(gr_gid=321, gr_mem=[])
        legacy_account = SimpleNamespace(pw_name="ttt-worker", pw_uid=321, pw_gid=999)
        with patch.object(release.pwd, "getpwnam", return_value=account), \
             patch.object(release.grp, "getgrnam", return_value=group), \
             patch.object(release.pwd, "getpwall", return_value=[account, legacy_account]):
            with self.assertRaisesRegex(release.ReleaseError, "正被其他账号共用"):
                release.Systemd().worker_account()

    def test_runtime_makes_credential_acl_copy_private_before_django(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            credentials, runtime = root / "credentials", root / "runtime"
            credentials.mkdir()
            runtime.mkdir(mode=0o700)
            cfg = {"fm.suqing.chat": dict(schema="feimeng", host="198.51.100.23", port=25432, password="synthetic")}
            source = credentials / "routes.json"
            source.write_text(json.dumps(cfg))
            source.chmod(0o440)
            (credentials / "django-secret").write_text("synthetic")
            account = SimpleNamespace(pw_uid=release.os.geteuid(), pw_gid=release.os.getegid())
            env = dict(CREDENTIALS_DIRECTORY=str(credentials), RUNTIME_DIRECTORY=str(runtime))
            with patch.dict(release.os.environ, env, clear=True), \
                 patch.object(release.Systemd, "worker_account", return_value=account), \
                 patch.object(release.Systemd, "verify_route"), \
                 patch.object(release.os, "execve", side_effect=SystemExit) as execve:
                with self.assertRaises(SystemExit):
                    release.worker("worker-check")
            _, command, django_env = execve.call_args.args
            route = Path(django_env["TT_SHARED_WEB_ROUTES_FILE"])
            self.assertEqual(route, runtime / "routes.json")
            self.assertEqual(route.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(route.read_text()), cfg)
            self.assertEqual(command[-1], "--check-route-identities")
