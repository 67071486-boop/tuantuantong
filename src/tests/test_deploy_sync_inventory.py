"""发布与巡检入口的实际传输参数。外部命令走本地夹具。"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase


_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
_REMOTE_SHARED = "/home/ubuntu/projects/tuantuantong/shared"
_IDLE_STEMS = (
    "tuantuantong-online-payroll", "tuantuantong-payout", "tuantuantong-order-qr-fetch",
    "tuantuantong-wecom-group-sync", "tuantuantong-wecom-notification",
)


def _unit_names(script):
    assignment = re.search(r"^SYNC_(?:SERVICES|UNITS)=\((.*)\)$", script, re.MULTILINE)
    if not assignment:
        raise AssertionError("发布产物缺少常驻同步清单")
    return shlex.split(assignment[1])


def rendered_sync_script(template: Path) -> str:
    with TemporaryDirectory() as directory:
        output = Path(directory) / template.name
        subprocess.run(
            ["bash", "-ec", 'source "$1"; render_sync_script "$2" "$3"', "bash",
             str(_DEPLOY / "sync-services.sh"), str(template), str(output)],
            check=True, capture_output=True, timeout=5,
        )
        return output.read_text()


def _loop_units(deploy):
    names = set()
    for unit in (deploy / "systemd").glob("*.service"):
        lines = unit.read_text().replace("\\\n", " ").splitlines()
        for line in lines:
            if line.startswith("ExecStart="):
                args = shlex.split(line.removeprefix("ExecStart="))
                if "manage.py" in args and "--loop" in args:
                    names.add(unit.stem)
    return names


_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class SyncInventoryWiringTests(SimpleTestCase):
    def setUp(self):
        workdir = TemporaryDirectory(prefix="tt deploy ")
        self.addCleanup(workdir.cleanup)
        self.root = Path(workdir.name)
        self.deploy = self.root / "deploy"
        shutil.copytree(_DEPLOY, self.deploy)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace.jsonl"
        self.trace.touch()
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TT_BACKUP_DIR": str(self.root / "backups"),
        }
        for command, body in {
            "flock": """
import fcntl
if len(args) != 2 or args[0] != "--nonblock":
    sys.exit(2)
try:
    fcntl.flock(int(args[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
""",
            "git": "if args == ['rev-parse', '--show-toplevel']: print(Path.cwd())",
            "pnpm": """
output = Path("src/frontend/dist/index.html")
output.parent.mkdir(parents=True)
output.write_text("synthetic frontend")
""",
            "rsync": """
if args[-1].endswith(":" + shared + "/"):
    for item in args[1:-1]:
        path = Path(item)
        if path.name in ("activate.sh", "rollback.sh"):
            Path(path.name).write_text(path.read_text())
elif "--from0" in args:
    sys.stdin.buffer.read()
""",
            "ssh": """
remote = args[-1]
if remote.startswith("test -e "):
    sys.exit(1)
if remote.startswith("test -d ") and os.environ.get("TT_TEST_INDEPENDENT_GATEWAY") == "1":
    print("independent")
if remote.endswith("bash -s"):
    payload = sys.stdin.read()
    Path("inspection-payload.sh").write_text(payload)
    if remote.startswith("INSPECT_EXPECT=down "):
        env = {**os.environ, "INSPECT_EXPECT": "down"}
        payload = payload.replace(
            "R=/home/ubuntu/projects/tuantuantong", "R=" + shlex.quote(str(Path.cwd() / "remote")), 1,
        )
        sys.exit(subprocess.run(["bash", "-s"], input=payload, text=True, env=env).returncode)
""",
            "systemctl": """
if args == ["is-active", "tuantuantong-peer-sync"]:
    print("active")
else:
    print("inactive")
""",
        }.items():
            self._stub(command, body)

    def _stub(self, command, body):
        path = self.bin / command
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, shlex, subprocess, sys\nfrom pathlib import Path\n"
            f"shared = {_REMOTE_SHARED!r}\n"
            "args = sys.argv[1:]\n"
            f"with open({str(self.trace)!r}, 'a') as trace:\n"
            f"    trace.write(json.dumps([{command!r}, *args]) + '\\n')\n"
            + body + "\n"
        )
        path.chmod(0o755)

    def _run(self, script, *args):
        return subprocess.run(
            ["bash", f"deploy/{script}", *args],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=20,
        )

    def _calls(self, command):
        return [
            record[1:] for line in self.trace.read_text().splitlines()
            if (record := json.loads(line))[0] == command
        ]

    def test_stage_uploads_and_checks_the_same_units_as_activation_and_rollback(self):
        result = self._run("deploy.sh", "synthetic-host", "--stage-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已暂存", result.stdout)
        ssh = [call[-1] for call in self._calls("ssh")]
        self.assertFalse(any("bash " in call for call in ssh), ssh)

        activate = _unit_names((self.root / "activate.sh").read_text())
        rollback = _unit_names((self.root / "rollback.sh").read_text())
        expected = _loop_units(self.deploy)
        self.assertTrue(expected)
        self.assertCountEqual(activate, expected)
        self.assertEqual(rollback, activate)

        upload, = [args for args in self._calls("rsync") if args[-1].endswith(":" + _REMOTE_SHARED + "/")]
        checks, = [call for call in ssh if call.startswith("chmod 644 ")]
        chmod, checksum = (shlex.split(part) for part in checks.split("; ", 1))
        self.assertEqual(chmod[:2], ["chmod", "644"])
        self.assertEqual(checksum[0], "md5sum")
        service_files = [f"{unit}.service" for unit in activate]
        local_paths = [f"deploy/systemd/{name}" for name in service_files]
        shared_paths = [f"{_REMOTE_SHARED}/{name}" for name in service_files]
        for args, expected_paths in ((upload[1:-1], local_paths), (chmod[2:], shared_paths), (checksum[1:], shared_paths)):
            self.assertEqual([path for path in args if Path(path).name in service_files], expected_paths)
        for script in ("activate.sh", "rollback.sh"):
            self.assertIn(f"{_REMOTE_SHARED}/{script}", chmod)
            self.assertIn(f"{_REMOTE_SHARED}/{script}", checksum)

        for artifact in ("internal-chat-gateway.sh", "tuantuantong-chat-realtime.service",
                         "chat-gateway-release.py", "tuantuantong-chat-gateway.service"):
            self.assertIn(f"deploy/internal-chat/{artifact}", upload)
            self.assertIn(f"{_REMOTE_SHARED}/{artifact}", chmod)
            self.assertIn(f"{_REMOTE_SHARED}/{artifact}", checksum)
        for path in ("order-grab-qr-notices.sh", "systemd/tuantuantong-order-grab-qr-notices.service",
                     "systemd/tuantuantong-order-grab-qr-notices.timer"):
            self.assertIn(f"deploy/{path}", upload)
            artifact = Path(path).name
            self.assertIn(f"{_REMOTE_SHARED}/{artifact}", chmod)
            self.assertIn(f"{_REMOTE_SHARED}/{artifact}", checksum)

    def test_normal_release_checks_shared_files_before_activating(self):
        result = self._run("deploy.sh", "synthetic-host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ssh = [call[-1] for call in self._calls("ssh")]
        self.assertTrue(ssh[-2].startswith("chmod 644 ") and "md5sum " in ssh[-2], ssh)
        self.assertRegex(ssh[-1], rf"^bash '{_REMOTE_SHARED}/activate\.sh' '[0-9]{{8}}T[0-9]{{6}}'$")

    def test_independent_gateway_opt_in_still_builds_the_web_chat_assets(self):
        self.env.pop("TT_BUILD_INTERNAL_CHAT", None)
        self.env["TT_TEST_INDEPENDENT_GATEWAY"] = "1"
        builder = self.root / "src/webk-preview/build-production.sh"
        builder.parent.mkdir(parents=True)
        builder.write_text('mkdir -p "$1"\nprintf "synthetic chat" > "$1/index.html"\n')
        result = self._run("deploy.sh", "synthetic-host", "--stage-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "src/frontend/dist/internal-chat-webk/index.html").read_text(), "synthetic chat")
        self.assertTrue(any(args[-1].endswith("/internal-chat/") for args in self._calls("rsync")))

    def test_inspection_streams_the_list_and_checks_every_unit_on_a_down_host(self):
        result = self._run("inspect.sh", "synthetic-host:down")
        self.assertIn("tuantuantong-peer-sync 在停用机上 active", result.stdout)
        names = _unit_names((self.root / "inspection-payload.sh").read_text())
        self.assertCountEqual(names, _loop_units(self.deploy))
        probes = [unit for action, unit in self._calls("systemctl") if action == "is-active"]
        self.assertEqual([unit for unit in probes if unit in names], names)

    def _legacy_release_fixture(self):
        remote = self.root / "remote"
        shared = remote / "shared"
        releases = [shared / "releases" / name for name in ("old", "new")]
        for release, timestamp in zip(releases, (1, 2), strict=True):
            for name in (".venv/bin", "frontend/dist", "staticfiles"):
                (release / "src" / name).mkdir(parents=True)
            python = release / "src/.venv/bin/python"
            python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TT_TEST_TRACE"\n'
                'case "$*" in\n'
                '  "manage.py send_wxpusher_notices --check-rollback") exit "${TT_TEST_WXPUSHER_CHECK_RESULT:-0}" ;;\n'
                '  *) exit "${TT_TEST_ROUTES_RESULT:-0}" ;;\n'
                'esac\n'
            )
            python.chmod(0o755)
            os.utime(release, (timestamp, timestamp))
        current = remote / "current"
        current.symlink_to(releases[0])
        cgroup = self.root / "cgroup" / "shared-peer"
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.procs").write_text("9191\n9192\n9193\n")
        (shared / ".env").write_text("DJANGO_ALLOWED_HOSTS=synthetic.invalid\n")
        (shared / "env.sh").touch()
        (shared / "backup_daily.py").touch()
        shutil.copyfile(self.deploy / "wxpusher-notification.sh", shared / "wxpusher-notification.sh")
        shutil.copyfile(self.deploy / "order-grab-qr-notices.sh", shared / "order-grab-qr-notices.sh")
        shutil.copyfile(
            self.deploy / "internal-chat" / "internal-chat-gateway.sh",
            shared / "internal-chat-gateway.sh",
        )
        for unit in (self.deploy / "systemd").iterdir():
            shutil.copyfile(unit, shared / unit.name)

        stubs = self.root / "stubs.sh"
        stubs.write_text(r"""
curl() {
  [[ "${TT_TEST_WEB_UNHEALTHY:-0}" != 1 ]] || return 1
  [[ " $* " != *' -w '* ]] || printf '200'
}
pgrep() { echo 1; }
sleep() { :; }
sudo() {
  printf '%s\n' "$*" >> "$TT_TEST_TRACE"
  if [[ "${TT_TEST_SDK_DISABLE_FAIL:-0}" == "1" && "$*" == "systemctl disable --now tuantuantong-openapi-dispatch" ]]; then
    return 1
  fi
  if [[ "$*" == "systemctl disable --now tuantuantong-shared-peer-sync" ]]; then
    case "${TT_TEST_SHARED_PEER_STOP_MODE:-ok}" in
      failure) return 1 ;;
      still-active|unreadable) ;;
      ok) TT_TEST_SHARED_PEER_STOPPED=1 ;;
    esac
  fi
}
systemctl() {
  if [[ $# == 2 && "$2" == "${TT_TEST_INSPECT_UNIT:-}" ]]; then
    if [[ "$1" == "is-enabled" ]]; then echo "${TT_TEST_UNITFILE_STATE:-disabled}";
    elif [[ "$1" == "is-active" ]]; then echo "${TT_TEST_INSPECT_STATE-inactive}"; fi
    return
  fi
  case "$*" in
    'is-active tuantuantong') echo active ;;
    'is-active tuantuantong-openapi-dispatch') echo "${TT_TEST_SDK_STATE:-inactive}" ;;
    'is-active tuantuantong-shared-peer-sync') echo "${TT_TEST_SHARED_PEER_STATE:-inactive}" ;;
    'is-active tuantuantong-peer-sync') echo "${TT_TEST_LEGACY_PEER_STATE:-inactive}" ;;
    'cat tuantuantong-shared-peer-sync') [[ "${TT_TEST_SHARED_PEER_INSTALLED:-1}" == 1 ]] ;;
    'show --property=LoadState --value tuantuantong-peer-sync') echo "${TT_TEST_LEGACY_PEER_LOAD_STATE:-loaded}" ;;
    'show --property=UnitFileState --value tuantuantong-peer-sync') echo "${TT_TEST_LEGACY_PEER_UNITFILE_STATE:-disabled}" ;;
    'show --property=ActiveState --value tuantuantong-peer-sync') echo "${TT_TEST_LEGACY_PEER_STATE:-inactive}" ;;
    'show --property=MainPID --value tuantuantong-peer-sync') echo 0 ;;
    'show --property=ControlGroup --value tuantuantong-peer-sync') echo "" ;;
    'show --property=LoadState --value tuantuantong-shared-peer-sync') echo "${TT_TEST_SHARED_PEER_LOAD_STATE:-loaded}" ;;
    'show --property=ActiveState --value tuantuantong-shared-peer-sync')
      [[ "${TT_TEST_SHARED_PEER_STOP_MODE:-ok}" != unreadable ]] || return 1
      if [[ "${TT_TEST_SHARED_PEER_STOPPED:-0}" == 1 ]]; then echo inactive;
      else echo "${TT_TEST_SHARED_PEER_STATE:-inactive}"; fi
      ;;
    'show --property=MainPID --value tuantuantong-shared-peer-sync')
      if [[ "${TT_TEST_SHARED_PEER_STOPPED:-0}" == 1 || "${TT_TEST_SHARED_PEER_STATE:-inactive}" != active ]]; then
        echo 0
      else echo 9191; fi
      ;;
    'show --property=ControlGroup --value tuantuantong-shared-peer-sync')
      if [[ "${TT_TEST_SHARED_PEER_STOPPED:-0}" != 1 && "${TT_TEST_SHARED_PEER_STATE:-inactive}" == active ]]; then
        echo /shared-peer
      else echo ""; fi
      ;;
    is-enabled*'.timer') echo disabled ;;
    is-active*) echo inactive ;;
  esac
}
uv() { :; }
readlink() {
  if [[ "$1" == "/proc/1/cwd" ]]; then
    pwd -P
  elif [[ "$*" == "-f /proc/"*"/cwd" ]]; then
    if [[ "$2" == "/proc/${TT_TEST_SHARED_PEER_WRONG_CWD:-0}/cwd" ]]; then echo "/synthetic/wrong";
    else (cd "$TT_TEST_CURRENT/src" && pwd -P); fi
  else
    command readlink "$@"
  fi
}
""")
        self.env.update(BASH_ENV=str(stubs), TT_TEST_TRACE=str(self.trace), TT_TEST_CURRENT=str(current))
        markers = self.root / "disabled-background-workers"
        markers.mkdir()
        remote_markers = self.root / "remote-background-workers"
        remote_markers.mkdir()
        for name in ("activate.sh", "rollback.sh", "inspect_remote.sh"):
            script = rendered_sync_script(self.deploy / name)
            script = script.replace(
                "R=/home/ubuntu/projects/tuantuantong", "R=" + shlex.quote(str(remote)), 1,
            ).replace(
                'UNIT_CGROUP="/sys/fs/cgroup/system.slice/${SERVICE}.service/cgroup.procs"',
                'UNIT_CGROUP="/synthetic/cgroup.procs"',
            ).replace("/sys/fs/cgroup", str(self.root / "cgroup")).replace("/etc/tuantuantong/disabled-background-workers/", f"{markers}/").replace(
                '"/etc/tuantuantong/remote-background-workers/$1"', f'"{remote_markers}/$1"',
            ).replace(
                "/etc/tuantuantong/shared-peer-sync/enabled", shlex.quote(str(self.root / "shared-peer-enabled")),
            )
            (self.deploy / name).write_text(script)
        return shared, releases, current, markers

    def test_order_grab_notice_timer_installs_with_external_fetch_off_and_stops_on_old_release(self):
        shared, releases, current, _markers = self._legacy_release_fixture()
        (shared / "order-qr-fetch-unit.env").write_text("ORDER_QR_FETCH_UNIT_ENABLE=0\n")
        (shared / "sync-unit.env").write_text("SYNC_UNIT_ENABLE=0\n")
        command = "src/apps/internal_chat/management/commands/send_order_grab_qr_notices.py"
        new_command = releases[1] / command
        new_command.parent.mkdir(parents=True)
        new_command.touch()
        stem = "tuantuantong-order-grab-qr-notices"
        service, timer = f"{stem}.service", f"{stem}.timer"
        service_unit = (shared / service).read_text()
        timer_unit = (shared / timer).read_text()
        self.assertIn(f"ConditionPathExists=/home/ubuntu/projects/tuantuantong/current/{command}", service_unit)
        self.assertIn("manage.py send_order_grab_qr_notices", service_unit)
        self.assertIn("OnCalendar=minutely", timer_unit)
        self.assertIn(f"Unit={service}", timer_unit)

        result = self._run("activate.sh", "new")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(current.resolve(), releases[1].resolve())
        calls = self.trace.read_text().splitlines()
        self.assertIn(f"systemctl restart {timer}", calls)
        self.assertIn(f"install -m 644 {shared / service} /etc/systemd/system/{service}", calls)
        timer_install = f"install -m 644 {shared / timer} /etc/systemd/system/{timer}"
        main_restart = "systemctl restart tuantuantong"
        self.assertLess(calls.index(timer_install), calls.index(main_restart))
        self.assertLess(calls.index(main_restart), calls.index(f"systemctl restart {timer}"))
        self.assertNotIn("systemctl restart tuantuantong-order-qr-fetch.timer", calls)

        self.trace.write_text("")
        result = self._run("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(current.resolve(), releases[0].resolve())
        calls = self.trace.read_text().splitlines()
        self.assertIn(f"systemctl disable {timer}", calls)
        self.assertIn(f"systemctl stop {timer} {service}", calls)
        self.assertLess(calls.index(f"systemctl stop {timer} {service}"), calls.index(main_restart))
        self.assertNotIn(f"systemctl restart {timer}", calls)

        (shared / timer).unlink()
        self.trace.write_text("")
        result = self._run("activate.sh", "new")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(timer, result.stderr)
        self.assertEqual(current.resolve(), releases[0].resolve())
        self.assertNotIn(main_restart, self.trace.read_text().splitlines())

    def test_legacy_release_and_rollback_skip_only_marked_idle_timers(self):
        shared, releases, current, markers = self._legacy_release_fixture()
        (shared / "sync-unit.env").write_text("SYNC_UNIT_ENABLE=1\n")
        command_files = (
            "business/management/commands/poll_online_payroll.py",
            "wecom/management/commands/sync_wecom_group_chats.py",
            "wecom/management/commands/send_wecom_notifications.py",
            "wecom/management/commands/sync_wecom_external_pay.py",
        )
        for release in releases:
            for relative in command_files:
                command = release / "src/apps" / relative
                command.parent.mkdir(parents=True, exist_ok=True)
                command.touch()
        unrelated = "tuantuantong-wecom-external-pay"
        (markers / f"{unrelated}.service").touch()
        for disabled in (False, True):
            if disabled:
                for stem in _IDLE_STEMS:
                    (markers / f"{stem}.service").touch()
            for script, args, release in (("activate.sh", ("new",), releases[1]),
                                          ("rollback.sh", (), releases[0])):
                with self.subTest(disabled=disabled, script=script):
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(current.resolve(), release.resolve())
                    calls = self.trace.read_text().splitlines()
                    self.assertIn(f"systemctl restart {unrelated}.timer", calls)
                    for stem in _IDLE_STEMS:
                        if disabled:
                            self.assertFalse(any(stem in call for call in calls), calls)
                        else:
                            self.assertIn(f"systemctl restart {stem}.timer", calls)

    def test_shared_peer_worker_requires_legacy_handoff_and_stops_when_rolling_back_to_old_code(self):
        _, releases, current, _ = self._legacy_release_fixture()
        unit = "tuantuantong-shared-peer-sync"
        command = releases[1] / "src/apps/business/management/commands/shared_peer_sync.py"
        command.parent.mkdir(parents=True)
        command.touch()
        self.env["TT_TEST_SHARED_PEER_STATE"] = "active"
        (self.root / "shared-peer-enabled").touch()
        legacy_marker = self.root / "remote-background-workers/tuantuantong-peer-sync.service"
        for handed_off in (False, True):
            if handed_off:
                legacy_marker.touch()
            for script, args, release in (
                ("activate.sh", ("new",), releases[1]),
                ("rollback.sh", (), releases[0]),
            ):
                with self.subTest(handed_off=handed_off, script=script):
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(current.resolve(), release.resolve())
                    calls = self.trace.read_text().splitlines()
                    self.assertEqual("systemctl restart tuantuantong-peer-sync" in calls, not handed_off)
                    if handed_off and script == "activate.sh":
                        self.assertIn(f"systemctl restart {unit}", calls)
                    else:
                        self.assertNotIn(f"systemctl restart {unit}", calls)
                        stopped = calls.index(f"systemctl disable --now {unit}")
                        self.assertLess(stopped, calls.index("systemctl restart tuantuantong"))
                        if not handed_off:
                            self.assertLess(stopped, calls.index("systemctl restart tuantuantong-peer-sync"))

    def test_shared_peer_worker_must_stop_before_a_release_changes_current_or_restarts_web(self):
        _, releases, current, _ = self._legacy_release_fixture()
        unit = "tuantuantong-shared-peer-sync"
        command = releases[1] / "src/apps/business/management/commands/shared_peer_sync.py"
        command.parent.mkdir(parents=True)
        command.touch()
        self.env["TT_TEST_SHARED_PEER_STATE"] = "active"
        markers = (
            self.root / "shared-peer-enabled",
            self.root / "remote-background-workers/tuantuantong-peer-sync.service",
        )
        for script, args, initial in (
            ("activate.sh", ("new",), releases[0]),
            ("rollback.sh", (), releases[1]),
        ):
            for marker in markers:
                if script == "rollback.sh":
                    marker.touch()
                else:
                    marker.unlink(missing_ok=True)
            for failure in ("failure", "still-active", "unreadable"):
                with self.subTest(script=script, failure=failure):
                    current.unlink()
                    current.symlink_to(initial)
                    self.trace.write_text("")
                    self.env["TT_TEST_SHARED_PEER_STOP_MODE"] = failure
                    result = self._run(script, *args)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(unit, result.stderr)
                    self.assertEqual(current.resolve(), initial.resolve())
                    calls = self.trace.read_text().splitlines()
                    self.assertIn(f"systemctl disable --now {unit}", calls)
                    self.assertNotIn("systemctl restart tuantuantong", calls)
                    self.assertNotIn("systemctl restart tuantuantong-peer-sync", calls)

    def test_rollback_stops_shared_peer_before_a_later_web_health_failure(self):
        _, releases, current, _ = self._legacy_release_fixture()
        current.unlink()
        current.symlink_to(releases[1])
        (self.root / "shared-peer-enabled").touch()
        (self.root / "remote-background-workers/tuantuantong-peer-sync.service").touch()
        self.env.update(TT_TEST_SHARED_PEER_STATE="active", TT_TEST_WEB_UNHEALTHY="1")
        result = self._run("rollback.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(current.resolve(), releases[0].resolve())
        calls = self.trace.read_text().splitlines()
        self.assertLess(
            calls.index("systemctl disable --now tuantuantong-shared-peer-sync"),
            calls.index("systemctl restart tuantuantong"),
        )
        self.assertNotIn("systemctl restart tuantuantong-peer-sync", calls)

    def test_shared_enable_rejects_active_legacy_and_invalid_routes_before_cutover(self):
        _, releases, current, _ = self._legacy_release_fixture()
        for release in releases:
            command = release / "src/apps/business/management/commands/shared_peer_sync.py"
            command.parent.mkdir(parents=True)
            command.touch()
        (self.root / "shared-peer-enabled").touch()
        (self.root / "remote-background-workers/tuantuantong-peer-sync.service").touch()

        for script, args, initial in (
            ("activate.sh", ("new",), releases[0]),
            ("rollback.sh", (), releases[1]),
        ):
            for legacy, preflight_code, expected in (
                ("active", "0", "未确认本机旧 tuantuantong-peer-sync 已停止"),
                ("inactive", "1", "租户清单未通过只读检查"),
            ):
                with self.subTest(script=script, legacy=legacy):
                    current.unlink()
                    current.symlink_to(initial)
                    self.trace.write_text("")
                    self.env.update(TT_TEST_LEGACY_PEER_STATE=legacy, TT_TEST_ROUTES_RESULT=preflight_code)
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(expected, result.stderr)
                    self.assertEqual(current.resolve(), initial.resolve())
                    calls = self.trace.read_text()
                    self.assertEqual("--check-routes" in calls, legacy == "inactive")
                    self.assertNotIn("systemctl restart tuantuantong\n", calls)
                    self.assertNotIn("systemctl stop tuantuantong-peer-sync", calls)
                    self.assertNotIn("systemctl stop tuantuantong-wxpusher-notification.service", calls)

    def test_wxpusher_preflight_runs_before_stopping_shared_and_respects_independent_notice(self):
        _, releases, current, _ = self._legacy_release_fixture()
        for script, args, source, target in (
            ("activate.sh", ("new",), releases[0], releases[1]),
            ("rollback.sh", (), releases[1], releases[0]),
        ):
            new_config = source / "src/apps/accounts/wxpusher_config.py"
            legacy = target / "src/apps/core/wxpusher_login.py"
            new_config.parent.mkdir(parents=True, exist_ok=True)
            legacy.parent.mkdir(parents=True, exist_ok=True)
            new_config.touch()
            legacy.touch()
            notice_marker = self.root / "remote-background-workers/tuantuantong-wxpusher-notification.service"
            self.env.update(TT_TEST_WXPUSHER_CHECK_RESULT="1", TT_TEST_SHARED_PEER_STATE="active")
            for independent in (False, True):
                with self.subTest(script=script, independent=independent):
                    current.unlink()
                    current.symlink_to(source)
                    if independent:
                        notice_marker.touch()
                    else:
                        notice_marker.unlink(missing_ok=True)
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    calls = self.trace.read_text()
                    self.assertEqual(result.returncode, 0 if independent else 1, result.stdout + result.stderr)
                    self.assertEqual(current.resolve(), (target if independent else source).resolve())
                    self.assertEqual("manage.py send_wxpusher_notices --check-rollback" in calls, not independent)
                    self.assertEqual("systemctl disable --now tuantuantong-shared-peer-sync" in calls, independent)
                    self.assertNotIn("systemctl stop tuantuantong-wxpusher-notification.service", calls)
            new_config.unlink()
            legacy.unlink()

    def test_shared_enable_rejects_legacy_autostart_or_independent_shared_ownership(self):
        _, releases, current, _ = self._legacy_release_fixture()
        for release in releases:
            command = release / "src/apps/business/management/commands/shared_peer_sync.py"
            command.parent.mkdir(parents=True)
            command.touch()
        (self.root / "shared-peer-enabled").touch()
        (self.root / "remote-background-workers/tuantuantong-peer-sync.service").touch()
        shared_marker = self.root / "remote-background-workers/tuantuantong-shared-peer-sync.service"

        for script, args, initial in (
            ("activate.sh", ("new",), releases[0]),
            ("rollback.sh", (), releases[1]),
        ):
            for state, independent, reason in (
                ("enabled", False, "持久停用"),
                ("enabled-runtime", False, "持久停用"),
                ("disabled", True, "独立管理"),
            ):
                with self.subTest(script=script, state=state, independent=independent):
                    current.unlink()
                    current.symlink_to(initial)
                    if independent:
                        shared_marker.touch()
                    else:
                        shared_marker.unlink(missing_ok=True)
                    self.env["TT_TEST_LEGACY_PEER_UNITFILE_STATE"] = state
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(reason, result.stderr)
                    self.assertEqual(current.resolve(), initial.resolve())
                    calls = self.trace.read_text()
                    self.assertNotIn("--check-routes", calls)
                    self.assertNotIn("systemctl stop tuantuantong-wxpusher-notification.service", calls)

    def test_shared_rollback_checks_installed_unit_state_all_processes_and_release(self):
        _, releases, current, _ = self._legacy_release_fixture()
        for release in releases:
            command = release / "src/apps/business/management/commands/shared_peer_sync.py"
            command.parent.mkdir(parents=True)
            command.touch()
        (self.root / "shared-peer-enabled").touch()
        (self.root / "remote-background-workers/tuantuantong-peer-sync.service").touch()
        self.env["TT_TEST_LEGACY_PEER_LOAD_STATE"] = "not-found"

        all_processes = "9191\n9192\n9193\n"
        for installed, state, processes, wrong_cwd, expected in (
            ("0", "inactive", "", "0", "本机缺少"),
            ("1", "inactive", "", "0", "未在目标版本运行"),
            ("1", "active", "9191\n", "0", "未在目标版本运行"),
            ("1", "active", all_processes, "9192", "未在目标版本运行"),
            ("1", "active", all_processes, "0", ""),
        ):
            with self.subTest(installed=installed, state=state, processes=processes, wrong_cwd=wrong_cwd):
                current.unlink()
                current.symlink_to(releases[1])
                (self.root / "cgroup/shared-peer/cgroup.procs").write_text(processes)
                self.trace.write_text("")
                self.env.update(TT_TEST_SHARED_PEER_INSTALLED=installed, TT_TEST_SHARED_PEER_STATE=state,
                                TT_TEST_SHARED_PEER_WRONG_CWD=wrong_cwd)
                result = self._run("rollback.sh")
                self.assertEqual(result.returncode, bool(expected), result.stdout + result.stderr)
                if expected:
                    self.assertIn(expected, result.stderr)
                    self.assertNotIn("✅ 已回滚", result.stdout)
                selected = releases[1] if installed == "0" else releases[0]
                self.assertEqual(current.resolve(), selected.resolve())
                calls = self.trace.read_text().splitlines()
                self.assertEqual("systemctl restart tuantuantong-shared-peer-sync" in calls, installed == "1")
                self.assertNotIn("systemctl restart tuantuantong-peer-sync", calls)
                if installed == "0":
                    self.assertNotIn("systemctl stop tuantuantong-wxpusher-notification.service", calls)

    def test_inspection_reports_two_peer_modes_running_after_handoff(self):
        _, releases, current, _ = self._legacy_release_fixture()
        command = releases[1] / "src/apps/business/management/commands/shared_peer_sync.py"
        command.parent.mkdir(parents=True)
        command.touch()
        current.unlink()
        current.symlink_to(releases[1])
        (self.root / "shared-peer-enabled").touch()
        (self.root / "remote-background-workers/tuantuantong-peer-sync.service").touch()
        self.env.update(TT_TEST_SHARED_PEER_STATE="active", TT_TEST_LEGACY_PEER_STATE="active")
        overlap = self._run("inspect_remote.sh")
        self.assertIn("❌ tuantuantong-peer-sync: active（本机配置应停用）", overlap.stdout)
        self.assertIn("✅ tuantuantong-shared-peer-sync: active", overlap.stdout)

        self.env["TT_TEST_LEGACY_PEER_STATE"] = "inactive"
        handed_off = self._run("inspect_remote.sh")
        self.assertIn("✅ tuantuantong-peer-sync: inactive（本机配置停用）", handed_off.stdout)
        self.assertIn("✅ tuantuantong-shared-peer-sync: active", handed_off.stdout)

    def test_inspection_checks_marked_idle_timers_and_services(self):
        _, _, _, markers = self._legacy_release_fixture()
        initial = self._run("inspect_remote.sh")
        self.assertNotIn("tuantuantong-online-payroll.timer:", initial.stdout)
        for stem in _IDLE_STEMS:
            (markers / f"{stem}.service").touch()
        (markers / "tuantuantong-wecom-external-pay.service").touch()
        inspection = self._run("inspect_remote.sh")
        for stem in _IDLE_STEMS:
            self.assertIn(f"✅ {stem}.timer: 本机已停用", inspection.stdout, inspection.stderr)
        self.assertIn("❌ tuantuantong-wecom-external-pay.timer:", inspection.stdout)
        for suffix, active, enabled in (("timer", "active", "disabled"), ("service", "activating", "disabled"),
                                        ("service", "", "disabled"), ("timer", "inactive", "enabled")):
            with self.subTest(suffix=suffix, active=active, enabled=enabled):
                self.env.update(TT_TEST_INSPECT_UNIT=f"tuantuantong-payout.{suffix}",
                                TT_TEST_INSPECT_STATE=active, TT_TEST_UNITFILE_STATE=enabled)
                inspection = self._run("inspect_remote.sh")
                self.assertIn("❌ tuantuantong-payout.timer: 本机停用状态未确认", inspection.stdout)

    def test_sdk_switch_controls_only_sdk_worker_across_releases(self):
        shared, releases, current, _ = self._legacy_release_fixture()

        # Python 后台发布持锁时，两个旧入口都须在切版本或重启前拒绝。
        with (shared / ".background-worker-release.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for script, args in (("activate.sh", ("new",)), ("rollback.sh", ())):
                with self.subTest(locked=script):
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("该站正在进行网页或后台发布", result.stderr)
                    self.assertEqual(current.resolve(), releases[0].resolve())
                    self.assertNotIn("systemctl", self.trace.read_text())

        workers = _loop_units(self.deploy) - {"tuantuantong-shared-peer-sync"}
        sdk = "tuantuantong-openapi-dispatch"
        config = shared / "sync-unit.env"
        self.env.pop("OPENAPI_DISPATCH_UNIT_ENABLE", None)
        for values, expected in (
            ("SYNC_UNIT_ENABLE=0\nOPENAPI_DISPATCH_UNIT_ENABLE=1\n", {sdk}),
            ("SYNC_UNIT_ENABLE=1\nOPENAPI_DISPATCH_UNIT_ENABLE=0\n", workers - {sdk}),
            ("SYNC_UNIT_ENABLE=1\n", workers),
            ("SYNC_UNIT_ENABLE=0\n", set()),
        ):
            config.write_text(values)
            with self.subTest(config=values):
                for script, args, release in (
                    ("activate.sh", ("new",), releases[1]),
                    ("rollback.sh", (), releases[0]),
                ):
                    self.trace.write_text("")
                    result = self._run(script, *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(current.resolve(), release.resolve())
                    calls = self.trace.read_text().splitlines()
                    restarted = {unit for unit in workers if f"systemctl restart {unit}" in calls}
                    self.assertSetEqual(restarted, expected, script)
                    if script == "activate.sh":
                        enabled = {unit for unit in workers if f"systemctl enable {unit}" in calls}
                        self.assertSetEqual(enabled, expected)
                        self.assertNotIn("systemctl enable tuantuantong-callback.service", calls)
                        self.assertNotIn("systemctl enable tuantuantong-dailiantong-callback.service", calls)
                    if values.endswith("OPENAPI_DISPATCH_UNIT_ENABLE=0\n"):
                        self.assertIn(f"systemctl disable --now {sdk}", calls)

        config.write_text("SYNC_UNIT_ENABLE=0\nOPENAPI_DISPATCH_UNIT_ENABLE=invalid\n")
        result = self._run("activate.sh", "new")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("OPENAPI_DISPATCH_UNIT_ENABLE", result.stderr)
        self.assertEqual(current.resolve(), releases[0].resolve())

        config.write_text('SYNC_UNIT_ENABLE=0\nexport OPENAPI_DISPATCH_UNIT_ENABLE="1" # SDK')
        self.env["TT_TEST_SDK_STATE"] = "active"
        inspection = self._run("inspect_remote.sh")
        self.assertIn(f"✅ {sdk}: active", inspection.stdout, inspection.stderr)
        self.assertIn("✅ tuantuantong-peer-sync: inactive（本机配置停用）", inspection.stdout)
        self.env["TT_TEST_SDK_STATE"] = "inactive"
        inspection = self._run("inspect_remote.sh")
        self.assertIn(f"❌ 同步单元 {sdk} 不是 active", inspection.stdout, inspection.stderr)

        config.write_text("SYNC_UNIT_ENABLE=1\nOPENAPI_DISPATCH_UNIT_ENABLE=0\n")
        self.env["TT_TEST_SDK_DISABLE_FAIL"] = "1"
        result = self._run("activate.sh", "new")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"未能停用 {sdk}", result.stderr)

    def test_shell_release_lock_blocks_independent_background_until_shell_exits(self):
        (self.root / "shared").mkdir()
        probe = """
import runpy, sys
from pathlib import Path

release = runpy.run_path(sys.argv[1])
try:
    with release["site_lock"](Path(sys.argv[2])):
        print("independent background entered")
except release["web"].ReleaseError as error:
    sys.exit(str(error))
"""
        command = [sys.executable, "-c", probe, self.deploy / "background-worker-release.py", self.root]
        result = subprocess.run(
            ["bash", "-ec", 'source "$1"; lock_background_worker_release "$2"; shift 2; "$@"',
             "bash", self.deploy / "sync-services.sh", self.root, *command],
            env=self.env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("该站正在进行网页或后台发布", result.stderr)
        self.assertNotIn("independent background entered", result.stdout)

        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("independent background entered", result.stdout)
