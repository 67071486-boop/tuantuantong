from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class ExternalPayDeploymentTests(SimpleTestCase):
    def test_hourly_timer_is_installed_checked_and_rolled_back(self):
        service = (ROOT / "deploy/systemd/tuantuantong-wecom-external-pay.service").read_text()
        timer = (ROOT / "deploy/systemd/tuantuantong-wecom-external-pay.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("manage.py sync_wecom_external_pay", service)
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)
        for script in ("activate.sh", "rollback.sh"):
            text = (ROOT / "deploy" / script).read_text()
            self.assertIn("WECOM_EXTERNAL_PAY_UNITS=(tuantuantong-wecom-external-pay.timer)", text)
            self.assertIn('sudo install -m 644', text)
        rollback = (ROOT / "deploy/rollback.sh").read_text()
        self.assertIn('sync_wecom_external_pay.py', rollback)
        self.assertIn('sudo systemctl disable --now "$unit"', rollback)
        deploy = (ROOT / "deploy/deploy.sh").read_text()
        for unit in ("service", "timer"):
            self.assertIn(f"deploy/systemd/tuantuantong-wecom-external-pay.{unit}", deploy)
            self.assertEqual(deploy.count(f"$R/shared/tuantuantong-wecom-external-pay.{unit}"), 2)
        self.assertIn("WECOM_EXTERNAL_PAY_TIMER", (ROOT / "deploy/inspect_remote.sh").read_text())
