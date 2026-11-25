from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.wecom.models import WecomAppConfig
from tests.p5_auth_helpers import make_logged_in_client


class WecomFeatureAuditTests(TenantTestCase):
    def test_changed_switch_is_recorded_with_actor_and_unchanged_write_is_not(self):
        config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True,
            corp_id='synthetic-corp', agent_id=1, secret='synthetic-secret',
        )
        client = make_logged_in_client(username='notice-audit-admin', is_superuser=True)
        user = get_user_model().objects.get(username='notice-audit-admin')
        url = '/api/wecom/notification/settings/'
        response = client.put(url, data={'order_status_enabled': True}, content_type='application/json')
        self.assertEqual(response.json()['code'], 0)
        config.refresh_from_db()
        self.assertTrue(config.order_status_enabled)
        logs = OperationLog.objects.filter(action='system.wecom_config_update', target_id=str(config.pk))
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.actor, 'notice-audit-admin')
        self.assertEqual(log.actor_id, user.pk)
        self.assertEqual(log.field_name, '企微功能开关')
        self.assertEqual(log.reason, '订单状态通知开启')
        response = client.put(url, data={'order_status_enabled': True}, content_type='application/json')
        self.assertEqual(response.json()['code'], 0)
        self.assertEqual(logs.count(), 1)
