from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import AppToken, WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_notifications import enqueue_worker_notice, send_notification_batch
from apps.business.peer_sync import hash_token
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherQRCode
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_wxpusher_login import PASSWORD_ENTRY, WORKER_ENTRY, signed_params

CONFIG = "/api/business/wxpusher-config/"
BIND = "/api/auth/wxpusher/binding"
GUIDE = "/api/auth/wxpusher/guide"
PREFERENCES = "/api/auth/wxpusher/preferences"
APP_ID = "8123456"
APP_TOKEN = "AT_synthetic_runtime_test_token"
KEY = "synthetic-runtime-test-signing-key"
QR = "https://wxpusher.zjiecode.com/api/qrcode/TestTenantRuntimeQr.jpg"


@override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA="")
class WxPusherTenantRuntimeTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_logged_in_client(username="wx-runtime-admin", is_superuser=True, games=None)
        self.worker = make_logged_in_client(
            username="wx-runtime-worker", groups=(WORKER,), nodes=("my_profile",), games=None,
        )
        self.user = get_user_model().objects.get(username="wx-runtime-worker")

    def save_config(self, **changes):
        response = self.admin.patch(CONFIG, changes, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())

    def enqueue(self, kind, reference):
        enqueue_worker_notice(user_id=self.user.pk, kind=kind, reference=reference)

    def test_new_app_supports_h5_binding_and_test_while_automatic_push_is_paused(self):
        self.save_config(app_id=APP_ID, app_token=APP_TOKEN, push_enabled=False)
        guide = self.worker.get(GUIDE).json()["data"]
        self.assertTrue(guide["binding_enabled"])
        self.assertFalse(guide["binding_prompt_enabled"])
        with patch("apps.accounts.wxpusher_binding.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            remote.create_qrcode.return_value = WxPusherQRCode("synthetic-tenant-qrcode", QR)
            remote.verify_scanned_uid.return_value = "UID_runtime"
            self.assertEqual(self.worker.post(f"{BIND}/start").json()["data"]["qr_url"], QR)
            self.assertEqual(self.worker.post(f"{BIND}/progress").json()["data"], {"bound": True})
            remote.verify_scanned_uid.assert_called_once_with(
                app_token=APP_TOKEN, code="synthetic-tenant-qrcode", app_id=APP_ID,
            )
            self.assertEqual(self.worker.post(f"{BIND}/test").json()["data"], {"accepted": True})
            self.assertEqual(remote.send.call_args.kwargs["uid"], "UID_runtime")
        row = WxPusherNotice.objects.get(kind="test", user=self.user)
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.binding.app_id, APP_ID)

    def test_admin_controls_binding_prompt_for_both_h5_and_app(self):
        self.save_config(binding_prompt_enabled=True)
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["binding_prompt_enabled"])

        self.save_config(app_id=APP_ID, app_token=APP_TOKEN)
        self.assertTrue(self.worker.get(GUIDE).json()["data"]["binding_prompt_enabled"])

        token = "synthetic-runtime-binding-prompt-token"
        AppToken.objects.create(
            user=self.user, key_hash=hash_token(token), expires_at=timezone.now() + timedelta(hours=1),
        )
        app = Client(
            SERVER_NAME=self.get_test_tenant_domain(), HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertTrue(app.get(GUIDE).json()["data"]["binding_prompt_enabled"])

        self.save_config(binding_prompt_enabled=False)
        for client in (self.worker, app):
            with self.subTest(client=client):
                guide = client.get(GUIDE).json()["data"]
                self.assertTrue(guide["binding_enabled"])
                self.assertFalse(guide["binding_prompt_enabled"])

    def test_tenant_controls_apply_again_at_dispatch_and_personal_opt_out_still_wins(self):
        self.save_config(app_id=APP_ID, app_token=APP_TOKEN, push_enabled=True)
        WxPusherBinding.objects.create(
            user=self.user, app_id=APP_ID, scan_bound_uid="UID_runtime", last_start_at=timezone.now(),
        )
        self.enqueue("assigned", "before-master-paused")
        self.save_config(push_enabled=False)
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch(self.tenant.get_primary_domain().domain)
            provider.assert_not_called()
            self.assertEqual(WxPusherNotice.objects.get(reference="before-master-paused").status, "skipped")

            self.save_config(push_enabled=True)
            self.enqueue("paused", "before-event-muted")
            self.save_config(order_events={"paused_resumed": False})
            self.enqueue("resumed", "after-event-muted")
            response = self.worker.patch(PREFERENCES, {"settlement_enabled": False}, content_type="application/json")
            self.assertEqual(response.json()["code"], 0)
            self.enqueue("settled", "personal-opt-out")
            self.enqueue("completed", "enabled-event")
            send_notification_batch(self.tenant.get_primary_domain().domain)
            remote = provider.return_value.__enter__.return_value
            remote.send.assert_called_once()
            self.assertEqual(remote.send.call_args.kwargs["app_token"], APP_TOKEN)
            self.assertEqual(remote.send.call_args.kwargs["summary"], "订单已完成")
        self.assertEqual(
            {row.reference: (row.status, row.skip_reason) for row in WxPusherNotice.objects.all()},
            {
                "before-master-paused": ("skipped", "workspace_disabled"),
                "before-event-muted": ("skipped", "workspace_disabled"),
                "after-event-muted": ("skipped", "workspace_disabled"),
                "personal-opt-out": ("skipped", "personal_disabled"),
                "enabled-event": ("sent", ""),
            },
        )

    def test_tenant_login_works_when_push_paused_and_respects_login_switch(self):
        self.save_config(app_id=APP_ID, app_token=APP_TOKEN, login_signing_key=KEY,
                         push_enabled=False, login_enabled=True)
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, uid="UID_runtime")
        path = "/api/auth/wxpusher/login"
        name = self.get_test_tenant_domain()
        with patch("apps.core.wxpusher_login.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            remote.is_subscribed.return_value = True
            browser = Client(SERVER_NAME=name)
            response = browser.get(path, signed_params(uid="UID_runtime", key=KEY))
            self.assertRedirects(response, WORKER_ENTRY, fetch_redirect_response=False)
            remote.is_subscribed.assert_called_once_with(app_token=APP_TOKEN, uid="UID_runtime", app_id=APP_ID)

            second_browser = Client(SERVER_NAME=name)
            self.save_config(login_enabled=False)
            response = second_browser.get(path, signed_params(uid="UID_runtime", nonce="after-disabled", key=KEY))
            self.assertRedirects(response, PASSWORD_ENTRY, fetch_redirect_response=False)
            remote.is_subscribed.assert_called_once()

    def test_replacing_the_app_preserves_old_history_but_never_sends_to_the_old_binding(self):
        self.save_config(app_id=APP_ID, app_token=APP_TOKEN, push_enabled=True)
        WxPusherBinding.objects.create(user=self.user, app_id=APP_ID, scan_bound_uid="UID_previous_application")
        self.assertTrue(self.worker.get(GUIDE).json()["data"]["bound"])
        self.enqueue("assigned", "old-application")

        self.save_config(app_id="8123457", app_token="AT_synthetic_runtime_other_token")
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            send_notification_batch(self.tenant.get_primary_domain().domain)
            provider.assert_not_called()
        row = WxPusherNotice.objects.get(reference="old-application")
        self.assertEqual(row.status, "skipped")
        self.assertEqual(row.binding.app_id, APP_ID)
