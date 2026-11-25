"""企微群同步定时器随发版、回滚和巡检接线。"""
from pathlib import Path
import shlex

from django.test import SimpleTestCase

from .test_deploy_sync_inventory import rendered_sync_script

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SERVICE = "tuantuantong-wecom-group-sync.service"
TIMER = "tuantuantong-wecom-group-sync.timer"


class WecomGroupSyncDeploymentTests(SimpleTestCase):
    def test_unit_executes_expected_command_every_minute(self):
        service = (DEPLOY / "systemd" / SERVICE).read_text()
        timer = (DEPLOY / "systemd" / TIMER).read_text()
        argv = shlex.split(next(line.removeprefix("ExecStart=") for line in service.splitlines()
                                if line.startswith("ExecStart=")))
        self.assertEqual(argv, [
            "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
            "manage.py", "sync_wecom_group_chats",
        ])
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=ubuntu", service)
        self.assertIn("OnCalendar=*:0/1", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(f"Unit={SERVICE}", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_both_deployment_transfer_surfaces_and_both_permission_surfaces(self):
        text = (DEPLOY / "deploy.sh").read_text()
        transfer, rest = text.split('ssh "$HOST" "chmod 644 ', 1)
        chmod, checksum = rest.split('; md5sum ', 1)
        for unit in (SERVICE, TIMER):
            with self.subTest(unit=unit):
                self.assertIn(f"deploy/systemd/{unit}", transfer)
                self.assertIn(f"$R/shared/{unit}", chmod)
                self.assertIn(f"$R/shared/{unit}", checksum)

    def test_activate_and_rollback_include_separate_timer_and_old_release_guard(self):
        for script in ("activate.sh", "rollback.sh"):
            text = (DEPLOY / script).read_text()
            with self.subTest(script=script):
                self.assertIn(f"WECOM_GROUP_SYNC_UNITS=({TIMER})", text)
                self.assertIn("WECOM_GROUP_SYNC_UNIT_ENABLE", text)
                self.assertIn("$R/shared/wecom-group-sync-unit.env", text)
                self.assertIn('sudo systemctl restart "$WECOM_GROUP_SYNC_UNIT"', text)
                rendered = rendered_sync_script(DEPLOY / script)
                services = next(line for line in rendered.splitlines() if line.startswith("SYNC_SERVICES=("))
                self.assertNotIn("wecom-group-sync", services)
        rollback = (DEPLOY / "rollback.sh").read_text()
        self.assertIn('! -f "$PREV/src/apps/wecom/management/commands/sync_wecom_group_chats.py"', rollback)
        self.assertIn('sudo systemctl disable --now "$WECOM_GROUP_SYNC_UNIT"', rollback)
        self.assertIn('sudo systemctl stop "${WECOM_GROUP_SYNC_UNIT%.timer}.service"', rollback)

    def test_inspection_checks_timer(self):
        text = (DEPLOY / "inspect_remote.sh").read_text()
        self.assertIn(f"WECOM_GROUP_SYNC_TIMER={TIMER}", text)
        self.assertIn('systemctl is-active "$WECOM_GROUP_SYNC_TIMER"', text)
        self.assertIn("LOCAL_WECOM_GROUP_SYNC_ENABLE", text)
