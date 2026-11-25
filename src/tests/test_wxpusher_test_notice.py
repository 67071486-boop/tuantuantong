from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import AppToken, WxPusherBinding
from apps.accounts.wxpusher_config import load_wxpusher_config
from apps.business.peer_sync import hash_token
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherError
from apps.platform.models import Domain
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

PATH = "/api/auth/wxpusher/binding/test"
APP_ID = "8123"
TOKEN = "synthetic-notice-token"
OTHER_DOMAIN = "wxp-notice-other.test.local"


class WxPusherTestNoticeTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(schema_name="wxp_notice_other", name="示例工作室乙", domain=OTHER_DOMAIN)

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_APP_TOKEN=TOKEN,
        ))
        self.worker = make_logged_in_client(
            username="notified-worker", groups=(WORKER,), nodes=("my_profile",), games=None,
        )
        self.user = get_user_model().objects.get(username="notified-worker")
        provider = self.enterContext(patch("apps.accounts.wxpusher_binding.WxPusherClient"))
        self.send = provider.return_value.__enter__.return_value.send

    def test_sends_fixed_message_to_current_binding_and_enforces_cooldown(self):
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, uid="UID_mine")
        now = timezone.now()
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            result = self.worker.post(PATH, {
                "uid": "UID_other", "content": "other text", "appToken": "untrusted",
            }, content_type="application/json")
            self.assertEqual(result.json()["code"], 0)
            self.assertEqual(result.json()["data"], {"accepted": True})
            sent = self.send.call_args.kwargs
            self.assertEqual(sent["uid"], "UID_mine")
            self.assertEqual(sent["app_token"], TOKEN)
            self.assertIn("测试通知", sent["content"])
            self.assertNotIn("other text", sent["content"])

            clock.return_value = now + timedelta(seconds=59)
            self.assertIn("60 秒", self.worker.post(PATH).json()["msg"])
            self.send.assert_called_once()
            clock.return_value = now + timedelta(seconds=60)
            self.assertEqual(self.worker.post(PATH).json()["code"], 0)
            self.assertEqual(self.send.call_count, 2)

    def app_client(self):
        token = "synthetic-app-notice-bearer"
        app_token = AppToken.objects.create(
            user=self.user, key_hash=hash_token(token),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return Client(
            SERVER_NAME=self.get_test_tenant_domain(), HTTP_AUTHORIZATION=f"Bearer {token}",
        ), app_token

    def test_app_worker_sends_a_test_link_to_their_own_binding(self):
        Domain.objects.create(tenant=self.tenant, domain="suqing.suqing.chat", is_primary=True)
        client, _ = self.app_client()
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, scan_bound_uid="UID_app_worker")
        response = client.post(PATH, {"uid": "UID_other", "url": "https://ignored.test/"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"accepted": True})
        sent = self.send.call_args.kwargs
        self.assertEqual(sent["uid"], "UID_app_worker")
        self.assertEqual(sent["url"], "daidaitong://suqing.suqing.chat/m/wxpusher/open")
        self.assertTrue(sent["html"])
        self.assertIn('href="https://suqing.suqing.chat/m/?entry=wxpusher"', sent["content"])
        self.assertNotIn("ignored.test", sent["content"])

        try:
            self.assertEqual(client.post(PATH, SERVER_NAME=OTHER_DOMAIN).status_code, 401)
            self.send.assert_called_once()
        finally:
            connection.set_tenant(self.tenant)

    def test_revoked_app_token_cannot_send_after_preliminary_authentication(self):
        client, app_token = self.app_client()
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, scan_bound_uid="UID_app_worker")

        def config_after_revocation():
            AppToken.objects.filter(pk=app_token.pk).update(revoked_at=timezone.now())
            return load_wxpusher_config()

        with patch("apps.core.wxpusher_views.load_wxpusher_config", side_effect=config_after_revocation):
            result = client.post(PATH).json()
        self.assertNotEqual(result["code"], 0)
        self.assertIn("登录已失效", result["msg"])
        self.send.assert_not_called()

    def test_rejects_a_binding_for_another_app_and_unconfigured_provider(self):
        WxPusherBinding.objects.create(user=self.user, app_id="old-app", uid="UID_old")
        self.assertNotEqual(self.worker.post(PATH).json()["code"], 0)
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, uid="UID_mine")
        with override_settings(WXPUSHER_APP_TOKEN=""):
            self.assertNotEqual(self.worker.post(PATH).json()["code"], 0)
        self.send.assert_not_called()

    def test_provider_error_is_sanitized_and_prevents_immediate_retry(self):
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, uid="UID_mine")
        self.send.side_effect = WxPusherError("internal provider error")
        result = self.worker.post(PATH).json()
        self.assertNotEqual(result["code"], 0)
        self.assertIn("无法确认", result["msg"])
        self.assertNotIn("internal provider error", result["msg"])
        self.assertIn("60 秒", self.worker.post(PATH).json()["msg"])
        self.send.assert_called_once()

    def test_tenant_a_session_cannot_send_on_b_even_with_distinct_eligible_binding(self):
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, uid="UID_tenant_a")
        self.assertEqual(self.worker.post(PATH).json()["code"], 0)
        self.assertEqual(self.send.call_args.kwargs["uid"], "UID_tenant_a")
        self.send.reset_mock()
        try:
            with tenant_context(self.other):
                worker_b = make_logged_in_client(
                    domain=OTHER_DOMAIN, username="worker-other-tenant",
                    groups=(WORKER,), nodes=("my_profile",), games=None,
                )
                user_b = get_user_model().objects.get(username="worker-other-tenant")
                WxPusherBinding.objects.create(user=user_b, app_id=APP_ID, uid="UID_tenant_b")
                self.assertEqual(self.worker.post(PATH, SERVER_NAME=OTHER_DOMAIN).status_code, 401)
                self.assertNotEqual(worker_b.post(PATH).json()["code"], 0)
                self.send.assert_not_called()
        finally:
            connection.set_tenant(self.tenant)
