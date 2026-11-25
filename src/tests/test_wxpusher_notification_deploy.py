import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
HELPER = DEPLOY / "wxpusher-notification.sh"
UNIT = "tuantuantong-wxpusher-notification.service"
COMMAND = "src/apps/accounts/management/commands/send_wxpusher_notices.py"
SETTINGS = """
WXPUSHER_SUBSCRIBE_TENANT_SCHEMA = "suqing"
WXPUSHER_APP_ID = "12345"
WXPUSHER_APP_TOKEN = "fixture-token"
"""


class WxPusherDeployTests(TestCase):
    def _exercise(self, *, command=True, tenant_config=False, settings=SETTINGS, enabled="1", running=True):
        with tempfile.TemporaryDirectory(prefix="wxpusher deploy ") as directory:
            root = Path(directory)
            shared = root / "shared"
            release = root / "releases" / "test"
            shared.mkdir()
            (shared / UNIT).write_text("fixture")
            python = release / "src/.venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            config = release / "src/config"
            config.mkdir()
            (config / "settings.py").write_text(settings)
            if command:
                path = release / COMMAND
                path.parent.mkdir(parents=True)
                path.touch()
            if tenant_config:
                (release / "src/apps/accounts/wxpusher_config.py").touch()

            return subprocess.run(
                [
                    "bash", "-ceu", """
fake_running="$5"
systemctl() { [[ "$1" == cat ]]; }
sudo() {
  if [[ "$1" == systemctl ]]; then
    shift
    printf '%s\n' "$*"
    if [[ "$1" == is-active ]]; then
      [[ "$fake_running" == true ]] || return 1
    fi
  fi
}
sleep() { :; }
source "$1"
stop_wxpusher_notification
echo CUTOVER
configure_wxpusher_notification "$2" "$3" "$4"
""", "--", str(HELPER), str(root), str(release), enabled, str(running).lower(),
                ],
                capture_output=True, text=True, timeout=10,
            )

    def test_worker_stops_before_switch_and_restarts_on_a_configured_target(self):
        result = self._exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"stop {UNIT}", "CUTOVER", "daemon-reload", f"enable {UNIT}",
            f"restart {UNIT}", f"is-active --quiet {UNIT}",
        ])

    def test_rollback_to_an_older_release_and_unconfigured_sites_disable_the_worker(self):
        for options in ({"command": False}, {"settings": SETTINGS + '\nWXPUSHER_APP_TOKEN = " "'}, {"enabled": "0"}):
            with self.subTest(options=options):
                result = self._exercise(**options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[:3], [
                    f"stop {UNIT}", "CUTOVER", f"disable --now {UNIT}",
                ])
                self.assertNotIn("restart ", result.stdout)

    def test_first_time_configuration_can_start_after_release_without_restart(self):
        result = self._exercise(tenant_config=True, settings="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"restart {UNIT}", result.stdout)
        result = self._exercise(tenant_config=True, settings="", enabled="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"disable --now {UNIT}", result.stdout)

    def test_config_and_start_failures_surface_without_leaking_settings(self):
        invalid = self._exercise(settings='raise ValueError("sensitive-config")')
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn("sensitive-config", invalid.stdout + invalid.stderr)
        self.assertNotIn("restart ", invalid.stdout)
        self.assertIn("无法读取目标版本的 WxPusher 配置", invalid.stderr)
        unavailable = self._exercise(running=False)
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertIn("WxPusher 发送任务未启动", unavailable.stderr)

    def test_release_artifacts_and_cutover_hooks_include_the_worker(self):
        unit = (DEPLOY / "systemd" / UNIT).read_text()
        self.assertIn("ExecStart=/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python manage.py send_wxpusher_notices --watch", unit)
        self.assertIn(f"ConditionPathExists=/home/ubuntu/projects/tuantuantong/current/{COMMAND}", unit)
        deploy = (DEPLOY / "deploy.sh").read_text()
        for source in (f"deploy/systemd/{UNIT}", "deploy/wxpusher-notification.sh"):
            self.assertIn(source, deploy)
            self.assertEqual(deploy.count(f"$R/shared/{Path(source).name}"), 2)
        for script, release in (("activate.sh", "$REL"), ("rollback.sh", "$PREV")):
            text = (DEPLOY / script).read_text()
            cutover = text.index(f'ln -sfn "{release}" "$R/current"')
            self.assertLess(text.index('source "$R/shared/wxpusher-notification.sh"'), cutover)
            self.assertLess(text.index("stop_wxpusher_notification"), cutover)
            current = "$CUR" if script == "rollback.sh" else "$R/current"
            self.assertLess(
                text.index(f'check_wxpusher_notification_rollback "{current}" "{release}"'),
                text.index("stop_wxpusher_notification"),
            )
            self.assertGreater(text.index(f'configure_wxpusher_notification "$R" "{release}"'), cutover)
