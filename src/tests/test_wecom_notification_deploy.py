from pathlib import Path
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class NotificationDeploymentTests(SimpleTestCase):
    def test_timer_is_installed_on_deploy_and_disabled_on_older_rollback(self):
        service=(ROOT/'deploy/systemd/tuantuantong-wecom-notification.service').read_text()
        timer=(ROOT/'deploy/systemd/tuantuantong-wecom-notification.timer').read_text()
        self.assertIn('manage.py send_wecom_notifications',service)
        self.assertIn('OnCalendar=*:0/1',timer)
        self.assertIn('Persistent=true',timer)
        for name in ('activate.sh','rollback.sh'):
            text=(ROOT/'deploy'/name).read_text()
            self.assertIn('WECOM_NOTIFICATION_UNITS=(tuantuantong-wecom-notification.timer)',text)
            self.assertIn('sudo install -m 644',text)
        rollback=(ROOT/'deploy/rollback.sh').read_text()
        self.assertIn('send_wecom_notifications.py',rollback)
        self.assertIn('sudo systemctl disable --now "$unit"',rollback)
        deploy=(ROOT/'deploy/deploy.sh').read_text()
        for unit in ('service','timer'):
            self.assertIn(f'deploy/systemd/tuantuantong-wecom-notification.{unit}',deploy)
            self.assertEqual(deploy.count(f'$R/shared/tuantuantong-wecom-notification.{unit}'),2)
        self.assertIn('WECOM_NOTIFICATION_TIMER',(ROOT/'deploy/inspect_remote.sh').read_text())
