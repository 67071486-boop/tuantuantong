from unittest.mock import patch

from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.roles import SERVICE, TENANT_ADMIN
from apps.internal_chat.models import SystemNoticeSettings
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/internal-chat/system-notification-settings/"
DEFAULTS = {
    "approval_submitted": True,
    "approval_approved": True,
    "wecom_aftersales": True,
    "order_grab_qr": False,
}


class InternalChatNoticeSettingsTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_MAINTENANCE=False, TT_CHAT_CORE_BACKEND="legacy",
        ))

    def account(self, username, group, domain=None):
        return make_logged_in_client(
            domain=domain or self.domain.domain, username=username, groups=(group,), games=None,
        )

    def put(self, client, **values):
        return client.put(URL, values, content_type="application/json")

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def test_admin_can_change_individual_rules_and_audit_names_actor(self):
        admin = self.account("notice-rule-admin", TENANT_ADMIN)
        staff = self.account("notice-rule-staff", SERVICE)
        self.assertEqual(self.data(admin.get(URL)), DEFAULTS)
        self.assertEqual(self.data(self.put(admin, approval_submitted=False)), {**DEFAULTS, "approval_submitted": False})
        changed = {**DEFAULTS, "approval_submitted": False, "order_grab_qr": True}
        self.assertEqual(self.data(self.put(admin, order_grab_qr=True)), changed)
        self.assertEqual(self.data(self.put(admin, order_grab_qr=True)), changed)
        self.assertEqual(staff.get(URL).status_code, 403)
        self.assertEqual(self.put(staff, order_grab_qr=False).status_code, 403)
        for values in ({"approval_submitted": []}, {"unknowable_rule": True}):
            self.assertEqual(self.put(admin, **values).json()["code"], 1000)
        self.assertEqual(self.data(admin.get(URL)), changed)
        logs = list(OperationLog.objects.filter(
            action="system.internal_chat_notice_settings_update",
        ).order_by("id").values_list("actor", "field_name"))
        self.assertEqual(logs, [("notice-rule-admin", "提交审批"), ("notice-rule-admin", "接单群二维码")])

    def test_admin_credentials_and_switches_are_isolated_between_tenants(self):
        admin = self.account("notice-primary-admin", TENANT_ADMIN)
        self.data(self.put(admin, approval_approved=False))
        primary = self.account("notice-primary-second-admin", TENANT_ADMIN)
        domain = "notice-rule-other.test.local"
        other, _ = provision_tenant(schema_name="notice_rule_other", name="合成其他通知租户", domain=domain)
        with tenant_context(other):
            other_admin = self.account("notice-other-admin", TENANT_ADMIN, domain)
            self.assertEqual(self.data(other_admin.get(URL)), DEFAULTS)
            self.assertEqual(
                self.data(self.put(other_admin, wecom_aftersales=False)), {**DEFAULTS, "wecom_aftersales": False},
            )
        cross = Client(SERVER_NAME=domain)
        cross.cookies = admin.cookies.copy()
        self.assertEqual(cross.get(URL).status_code, 401)
        self.assertEqual(self.put(cross, wecom_aftersales=True).status_code, 401)
        self.assertEqual(self.data(other_admin.get(URL)), {**DEFAULTS, "wecom_aftersales": False})
        self.assertEqual(self.data(primary.get(URL)), {**DEFAULTS, "approval_approved": False})

    def test_switching_wecom_back_on_records_the_resume_cutoff(self):
        admin = self.account("notice-wecom-admin", TENANT_ADMIN)
        self.data(self.put(admin, wecom_aftersales=False))
        self.assertIsNone(SystemNoticeSettings.objects.get(pk=1).wecom_aftersales_resumed_at)
        now = timezone.now()
        with patch("apps.internal_chat.system_notice_settings.timezone.now", return_value=now):
            self.assertEqual(self.data(self.put(admin, wecom_aftersales=True)), DEFAULTS)
        self.data(self.put(admin, wecom_aftersales=True))
        self.assertEqual(SystemNoticeSettings.objects.get(pk=1).wecom_aftersales_resumed_at, now)

    def test_reopening_order_qr_notifications_moves_the_cutoff(self):
        admin = self.account("notice-qr-admin", TENANT_ADMIN)
        self.data(self.put(admin, order_grab_qr=True))
        first = SystemNoticeSettings.objects.get(pk=1).order_grab_qr_resumed_at
        self.data(self.put(admin, order_grab_qr=False))
        self.data(self.put(admin, order_grab_qr=True))
        self.assertGreater(SystemNoticeSettings.objects.get(pk=1).order_grab_qr_resumed_at, first)

    def test_independent_chat_service_cannot_save_inactive_rules(self):
        admin = self.account("notice-core-admin", TENANT_ADMIN)
        with (
            self.settings(TT_CHAT_CORE_BACKEND="service"),
            patch("apps.internal_chat.core_bridge.matrix_bridge.tenant_config", return_value={"core_backend": "service"}),
        ):
            self.assertEqual(self.data(admin.get(URL)), {**DEFAULTS, "available": False})
            error = self.put(admin, order_grab_qr=True).json()
        self.assertNotEqual(error["code"], 0, error)
        self.assertIn("暂不支持发送系统通知", str(error))
        self.assertFalse(SystemNoticeSettings.objects.exists())
