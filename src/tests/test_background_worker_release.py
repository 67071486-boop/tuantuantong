import importlib.util
from itertools import count
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
WORKER = "tuantuantong-openapi-dispatch.service"
TIMER = "tuantuantong-audit-prune.timer"
WECOM_TIMER = "tuantuantong-wecom-notification.timer"
WECOM_SERVICE = "tuantuantong-wecom-notification.service"
COMMIT = "a" * 40


class BackgroundWorkerReleaseTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("background_worker_release", DEPLOY / "background-worker-release.py")
        self.release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.release)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.legacy = self.folder / "legacy"
        self.original = self.legacy / "shared/releases/original"
        self.root = self.folder / "workers"
        self.target = self.root / "releases" / COMMIT
        for checkout in (self.original, self.target):
            (checkout / "src").mkdir(parents=True)
        (self.target / "deploy").symlink_to(DEPLOY)
        (self.target / "VERSION").write_text(COMMIT + "\n", encoding="ascii")
        for item in self.release.web.REQUIRED_FILES:
            if item.startswith("src/"):
                path = self.target / item
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
        (self.legacy / "current").symlink_to(self.original)
        app_env = self.legacy / "shared/.env"
        app_env.write_text("DJANGO_SECRET_KEY=synthetic-release-key\n", encoding="utf-8")
        app_env.chmod(0o600)
        self.marker = self.target / "PACKAGE-SHA256"
        self.marker.write_text("ab\n", encoding="ascii")
        path = self.folder / "worker.json"
        path.write_text(json.dumps({"root": str(self.root), "legacy_root": str(self.legacy), "app_env": str(app_env),
                                    "hosts": ["fixture.test.local"], "units": [WORKER, TIMER]}), encoding="utf-8")
        self.config = self.release.read_config(path)
        self.unit_root = self.folder / "systemd"
        self.cgroups = self.folder / "cgroups"
        self.procs = self.folder / "proc"
        for resource in (self.unit_root, self.cgroups, self.procs):
            resource.mkdir()
        for unit in (WORKER, self.release.service_name(TIMER)):
            shutil.copyfile(DEPLOY / "systemd" / unit, self.unit_root / unit)
        clock = count()
        for module, key, value in ((self.release, "UNIT_ROOT", self.unit_root),
                                   (self.release, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None)),
                                   (self.release.web, "CGROUP_ROOT", self.cgroups),
                                   (self.release.web, "PROC_ROOT", self.procs),
                                   (self.release.web, "run", self.command)):
            override = patch.object(module, key, value)
            override.start()
            self.addCleanup(override.stop)
        cgroup = self.cgroups / "worker"
        cgroup.mkdir()
        self.members = cgroup / "cgroup.procs"
        self.cwd = self.procs / "41/cwd"
        self.cwd.parent.mkdir()
        self.cwd.symlink_to(self.original / "src")
        self.members.write_text("41\n", encoding="ascii")
        self.active = {WORKER: "active", TIMER: "inactive", self.release.service_name(TIMER): "inactive"}
        self.events = []
        self.fail_start = False
        self.fail_after_start = False
        self.observations = 0

    def command(self, *args, **kwargs):
        release = self.release
        if args[0] == "systemctl":
            unit = args[-1]
            if unit.endswith(".timer"):
                properties = {"LoadState": "loaded", "Triggers": release.service_name(unit)}
            else:
                enabled = "enabled" if unit == WORKER else "disabled"
                independent = release.drop_in(unit).is_file()
                cwd = (self.root if independent else self.legacy) / "current/src"
                command = release.command_from(DEPLOY.parent, unit).replace(release.LEGACY_SRC, str(cwd), 1)
                properties = {"LoadState": "loaded", "User": release.pwd.getpwuid(release.os.getuid()).pw_name,
                              "WorkingDirectory": str(cwd), "Environment": "PYTHONUNBUFFERED=1",
                              "EnvironmentFiles": "", "ExecStart": f"{{ argv[]={command} ; }}",
                              "UnitFileState": enabled, "Result": "success", "NRestarts": "0",
                              "MainPID": "41" if unit == WORKER and self.active[WORKER] == "active" else "0",
                              "ControlGroup": "/worker" if unit == WORKER else ""}
            properties.update(ActiveState=self.active[unit], UnitFileState="enabled" if unit == WORKER else "disabled")
            if (unit == WORKER and self.fail_after_start and independent and self.active[unit] == "active"
                    and "--property=ActiveState" in args):
                self.observations += 1
                if self.observations > 1:
                    properties.update(ActiveState="activating", Result="exit-code", NRestarts="1", MainPID="0")
            output = [f"{key}={properties[key]}" for arg in args[2:-1]
                      if (key := arg.removeprefix("--property=")) in properties]
            return subprocess.CompletedProcess(args, 0, "\n".join(output))
        if args[:3] == ("sudo", "-n", "systemctl"):
            action, unit = args[3], args[-1]
            self.events.append((action, unit))
            if action == "stop":
                self.active[unit] = "inactive"
                if unit == WORKER:
                    self.members.write_text("", encoding="ascii")
            elif action == "start":
                self.assertEqual(self.active[unit], "inactive")
                if unit == WORKER:
                    independent = release.drop_in(unit).is_file()
                    if independent and self.fail_start:
                        self.fail_start = False
                        raise subprocess.CalledProcessError(1, args)
                    self.cwd.unlink()
                    self.cwd.symlink_to(((self.root if independent else self.legacy) / "current/src").resolve())
                    self.members.write_text("41\n", encoding="ascii")
                self.active[unit] = "active"
            return subprocess.CompletedProcess(args, 0)
        if args[:2] == ("sudo", "-n"):
            if args[2] == "install":
                shutil.copyfile(*args[-2:])
                return subprocess.CompletedProcess(args, 0)
            return subprocess.run(args[2:], check=True, **kwargs)
        self.events.append((Path(args[0]).name, kwargs.get("env", {}).get("PGOPTIONS")))
        return subprocess.CompletedProcess(args, 0)

    def test_render_keeps_the_existing_management_command_and_rejects_http(self):
        release = self.release
        text = release.render(DEPLOY.parent, Path("/home/example/background"), WORKER)
        self.assertEqual(text, "[Service]\nWorkingDirectory=/home/example/background/current/src\n"
                               "ExecStart=\nExecStart=/home/example/background/current/src/.venv/bin/python "
                               "manage.py openapi_dispatch --loop\n")
        with self.assertRaises(release.web.ReleaseError):
            release.render(DEPLOY.parent, Path("/home/example/background"), "tuantuantong-callback.service")

    def test_switch_verifies_read_only_then_reuses_the_same_active_worker_and_preserves_disabled_timer(self):
        self.release.switch(self.config, self.target)

        self.assertEqual((self.root / "current").resolve(), self.target)
        self.assertEqual(self.cwd.resolve(), self.target / "src")
        self.assertEqual((self.target / "READY").read_bytes(), self.marker.read_bytes())
        self.assertEqual(self.events, [("uv", None), ("python", "-c default_transaction_read_only=on"),
                                       ("stop", WORKER), ("daemon-reload", "daemon-reload"), ("start", WORKER)])

        self.release.switch(self.config, None)
        self.assertEqual(self.cwd.resolve(), self.original / "src")
        self.assertFalse(self.release.drop_in(WORKER).exists())
        self.assertFalse(self.release.drop_in(TIMER).exists())
        self.assertFalse((self.root / "current").exists())
        self.assertEqual(self.active[TIMER], "inactive")

    def test_failed_new_worker_start_restores_original_unit_and_does_not_mark_candidate_ready(self):
        self.fail_start = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.release.switch(self.config, self.target)
        self.assertEqual(self.cwd.resolve(), self.original / "src")
        self.assertEqual(self.active[WORKER], "active")
        self.assertEqual(self.active[TIMER], "inactive")
        self.assertFalse(self.release.drop_in(WORKER).exists())
        self.assertFalse((self.target / "READY").exists())

    def test_crash_after_start_cancels_restart_before_returning_to_original(self):
        self.fail_after_start = True
        with self.assertRaisesRegex(self.release.web.ReleaseError, "启动后退出"):
            self.release.switch(self.config, self.target)
        self.assertGreater(self.observations, 1)
        self.assertEqual(self.events.count(("stop", WORKER)), 2)
        self.assertEqual(self.cwd.resolve(), self.original / "src")
        self.assertEqual(self.active[WORKER], "active")
        self.assertFalse(self.release.drop_in(WORKER).exists())
        self.assertFalse((self.target / "READY").exists())

    def test_another_release_root_cannot_claim_a_second_unit(self):
        existing = self.release.drop_in("tuantuantong-peer-sync.service")
        existing.parent.mkdir(parents=True)
        existing.write_text("[Service]\nWorkingDirectory=/home/other-worker/current/src\n", encoding="utf-8")
        with self.assertRaisesRegex(self.release.web.ReleaseError, "归属发生变化"):
            self.release.switch(self.config, self.target)
        self.assertEqual(self.events, [])
        self.assertFalse((self.target / "READY").exists())

    def test_active_timer_is_left_on_its_original_schedule(self):
        self.active[TIMER] = "active"
        with self.assertRaisesRegex(self.release.web.ReleaseError, "原调度时间"):
            self.release.switch(self.config, self.target)
        self.assertEqual(self.events, [])
        self.assertEqual(self.active[TIMER], "active")
        self.assertFalse(self.release.drop_in(TIMER).exists())

    def test_remote_ownership_fences_a_local_background_release(self):
        remote_root = self.folder / "etc/tuantuantong/remote-background-workers"
        remote_root.mkdir(parents=True)
        (remote_root / WECOM_SERVICE).touch()
        self.config["units"] = [WECOM_TIMER]
        self.active[WECOM_TIMER] = "active"
        with patch.object(self.release, "REMOTE_WORKER_ROOT", remote_root):
            for target in (self.target, None):
                with self.subTest(target=target), self.assertRaisesRegex(self.release.web.ReleaseError, "其他服务器"):
                    self.release.switch(self.config, target)
        self.assertEqual(self.events, [])
        self.assertFalse((self.root / "current").exists())

    def test_idle_timer_marker_fences_a_local_background_release(self):
        disabled_root = self.folder / "etc/tuantuantong/disabled-background-workers"
        disabled_root.mkdir(parents=True)
        (disabled_root / WECOM_SERVICE).touch()
        (disabled_root / WORKER).touch()
        with patch.object(self.release, "DISABLED_WORKER_ROOT", disabled_root, create=True):
            self.config["units"] = [WECOM_TIMER]
            self.active[WECOM_TIMER] = "active"
            for target in (self.target, None):
                with self.subTest(target=target), self.assertRaisesRegex(self.release.web.ReleaseError, "本机已停用"):
                    self.release.switch(self.config, target)
            self.assertEqual(self.events, [])
            self.assertFalse((self.root / "current").exists())

            self.config["units"] = [WORKER]
            self.release.switch(self.config, self.target)
            self.assertIn(("start", WORKER), self.events)

    def test_generated_release_guard_skips_only_the_remotely_owned_timer(self):
        remote_root = self.folder / "etc/tuantuantong/remote-background-workers"
        remote_root.mkdir(parents=True)
        owner = remote_root / WECOM_SERVICE
        owner.touch()
        template = self.folder / "template.sh"
        template.write_text(
            '# SYNC_SERVICES\nfor unit in "$@"; do\n'
            '  independent_background_worker "$unit" && continue\n'
            '  printf "%s\\n" "$unit"\ndone\n', encoding="utf-8",
        )
        rendered = self.folder / "rendered.sh"
        subprocess.run(["bash", "-c", 'source "$1"; render_sync_script "$2" "$3"',
                        "render", DEPLOY / "sync-services.sh", template, rendered], check=True)
        text = rendered.read_text(encoding="utf-8").replace("/etc/", f"{self.folder}/etc/")
        rendered.write_text(text, encoding="utf-8")
        command = ["bash", rendered, WECOM_TIMER, WECOM_SERVICE, TIMER]

        selected_units = subprocess.check_output(command, text=True).splitlines()
        self.assertEqual(selected_units, [TIMER])
        owner.unlink()
        self.assertEqual(subprocess.check_output(command, text=True).splitlines(), [WECOM_TIMER, WECOM_SERVICE, TIMER])

    def test_wecom_inspection_flags_local_execution_and_does_not_claim_remote_health(self):
        rendered = self.folder / "inspection.sh"
        subprocess.run(["bash", "-c", 'source "$1"; render_sync_script "$2" "$3"',
                        "render", DEPLOY / "sync-services.sh", DEPLOY / "inspect_remote.sh", rendered], check=True)
        text = rendered.read_text(encoding="utf-8").replace("/etc/", f"{self.folder}/etc/")
        helpers = text.split("CALLBACK_UNIT=", 1)[0]
        wecom = text.split('echo "== 企微通知 =="\n', 1)[1].split('echo "== 上家收款同步 =="\n', 1)[0]
        rendered.write_text(helpers + r'''
WECOM_NOTIFICATION_TIMER=tuantuantong-wecom-notification.timer
systemctl() {
  case "$2" in
    tuantuantong-wecom-notification.timer) printf '%s\n' "$TIMER_STATE" ;;
    tuantuantong-wecom-notification.service) printf '%s\n' "$SERVICE_STATE" ;;
    *) return 2 ;;
  esac
}
ok() { printf 'OK %s\n' "$*"; }
bad() { printf 'BAD %s\n' "$*"; }
uncov() { printf 'UNCOVERED %s\n' "$*"; }
TIMER_STATE="$1"
SERVICE_STATE="$2"
''' + wecom, encoding="utf-8")
        command = ["bash", rendered]
        local = self.folder / f"etc/systemd/system/{WECOM_SERVICE}.d/80-tuantuantong-background-worker.conf"
        self.assertIn("不会自动发送", subprocess.check_output(command + ["inactive", "inactive"], text=True))
        local.parent.mkdir(parents=True)
        local.touch()
        self.assertIn("OK", subprocess.check_output(command + ["active", "inactive"], text=True))
        self.assertIn("不会自动发送", subprocess.check_output(command + ["inactive", "inactive"], text=True))
        local.unlink()

        owner = self.folder / "etc/tuantuantong/remote-background-workers" / WECOM_SERVICE
        owner.parent.mkdir(parents=True)
        owner.touch()
        claimed = subprocess.check_output(command + ["inactive", "inactive"], text=True)
        self.assertIn("UNCOVERED", claimed)
        self.assertIn("远端健康未核验", claimed)
        self.assertNotIn("BAD", claimed)
        for timer_state, service_state in (("active", "inactive"), ("inactive", "activating")):
            with self.subTest(timer=timer_state, service=service_state):
                self.assertIn("BAD", subprocess.check_output(command + [timer_state, service_state], text=True))

    def test_legacy_generated_scripts_use_the_same_lock_and_skip_owned_workers(self):
        key = "independent_background_worker"
        for filename in ("activate.sh", "rollback.sh"):
            rendered = self.folder / filename
            subprocess.run(["bash", "-c", 'source "$1"; render_sync_script "$2" "$3"',
                            "render", DEPLOY / "sync-services.sh", DEPLOY / filename, rendered], check=True)
            text = rendered.read_text(encoding="utf-8")
            self.assertIn(f'"$1/shared/{self.release.LOCK_NAME}"', text)
            self.assertIn(self.release.DROP_IN, text)
            for variable in ("SYNC_SERVICE", "PAYOUT_UNIT", "AUDIT_PRUNE_UNIT", "LEDGER_PRUNE_UNIT",
                             "ORDER_QR_FETCH_UNIT", "WECOM_GROUP_SYNC_UNIT", "RECEIPT_SYNC_UNIT", "unit"):
                self.assertIn(f'{key} "${variable}" && continue', text)
            self.assertIn(f'if ! {key} "$WXPUSHER_NOTICE_UNIT"; then', text)
            self.assertNotIn(f'{key} "$BACKUP_UNIT"', text)
