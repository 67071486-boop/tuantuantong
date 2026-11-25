from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import AppToken, WxPusherBinding
from apps.business.peer_sync import hash_token
from apps.business.roles import WORKER
from apps.core.error_codes import ErrorCode
from apps.core.wxpusher import WxPusherError, WxPusherQRCode
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

GUIDE = "/api/auth/wxpusher/guide"
BIND = "/api/auth/wxpusher/binding"
QR = "https://wxpusher.zjiecode.com/api/qrcode/SyntheticQr.jpg"


class WxPusherBindingTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_APP_TOKEN="synthetic-app-token",
            WXPUSHER_APP_ID="",
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
        ))
        self.worker = make_logged_in_client(
            username="binding-worker", groups=(WORKER,), nodes=("my_profile",), games=None,
        )

        provider = self.enterContext(patch("apps.accounts.wxpusher_binding.WxPusherClient"))
        self.remote = provider.return_value.__enter__.return_value
        self.remote.create_qrcode.return_value = WxPusherQRCode("qr-code", QR)
        self.remote.verify_scanned_uid.return_value = "UID_synthetic"
        self.enterContext(patch("apps.accounts.wxpusher_binding.secrets.randbelow", return_value=420))

    def post(self, action, **fields):
        response = self.worker.post(f"{BIND}/{action}", fields, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def app_client(self, token, username="binding-worker"):
        AppToken.objects.create(
            user=get_user_model().objects.get(username=username), key_hash=hash_token(token),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return Client(SERVER_NAME=self.get_test_tenant_domain(), HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_binding_requires_delivered_code_and_unlink_clears_it(self):
        guide = self.worker.get(GUIDE).json()["data"]
        self.assertTrue(guide["binding_enabled"])
        self.assertIs(guide["binding_link_supported"], False)
        self.assertEqual(self.post("start")["data"]["qr_url"], QR)
        self.assertEqual(self.post("scan")["code"], 0)
        self.assertEqual(self.post("scan")["code"], 0)
        self.remote.send.assert_called_once()
        self.assertEqual(self.remote.send.call_args.kwargs["uid"], "UID_synthetic")
        self.assertIn("000420", self.remote.send.call_args.kwargs["content"])
        self.assertNotEqual(self.post("confirm", code="111111")["code"], 0)
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])
        self.assertTrue(self.post("confirm", code="000420")["data"]["bound"])
        binding = WxPusherBinding.objects.get(user__username="binding-worker")
        self.assertEqual(binding.uid, "UID_synthetic")
        self.assertEqual(binding.code_digest, "")
        self.assertTrue(self.worker.get(GUIDE).json()["data"]["bound"])
        with override_settings(WXPUSHER_APP_TOKEN=""):
            self.assertEqual(self.worker.delete(BIND).json()["data"], {"bound": False})
        self.assertEqual(self.worker.delete(BIND).json()["data"], {"bound": False})
        self.assertNotEqual(self.post("confirm", code="000420")["code"], 0)
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])

    def test_unscanned_qr_retries_after_provider_interval(self):
        now = timezone.now()
        self.remote.verify_scanned_uid.side_effect = [WxPusherError("尚未扫码"), "UID_synthetic"]
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            self.assertEqual(self.post("start")["code"], 0)
            self.assertIn("太频繁", self.post("start")["msg"])
            self.assertIn("暂未查到", self.post("scan")["msg"])
            clock.return_value = now + timedelta(seconds=9)
            self.assertIn("等待 10 秒", self.post("scan")["msg"])
            self.remote.verify_scanned_uid.assert_called_once()
            self.remote.send.assert_not_called()
            clock.return_value = now + timedelta(seconds=10)
            self.assertEqual(self.post("scan")["code"], 0)
        self.remote.create_qrcode.assert_called_once()
        self.remote.send.assert_called_once()

    @override_settings(WXPUSHER_APP_ID="8123", WXPUSHER_LOGIN_SIGNING_KEY="test-only-signing-key")
    def test_auto_binding_resumes_same_session_and_never_sends_a_code(self):
        now = timezone.now()
        self.remote.verify_scanned_uid.side_effect = [WxPusherError("暂未扫码"), "UID_synthetic"]
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            start = self.post("start")["data"]
            self.assertEqual(set(start), {"qr_url", "expires_at"})
            self.assertEqual(self.worker.get(GUIDE).json()["data"]["pending_binding"], start)

            other_browser = Client(SERVER_NAME=self.get_test_tenant_domain())
            login = other_browser.post("/api/auth/login", {
                "username": "binding-worker", "password": P2_TEST_PASSWORD,
            }, content_type="application/json")
            self.assertEqual(login.status_code, 200)
            self.assertIsNone(other_browser.get(GUIDE).json()["data"]["pending_binding"])
            self.assertNotEqual(other_browser.post(f"{BIND}/progress").json()["code"], 0)
            self.remote.verify_scanned_uid.assert_not_called()

            self.assertEqual(self.post("progress")["data"], {"bound": False})
            clock.return_value = now + timedelta(seconds=9)
            self.assertEqual(self.post("progress")["data"], {"bound": False})
            self.remote.verify_scanned_uid.assert_called_once()

            clock.return_value = now + timedelta(seconds=10)
            response = self.worker.post(
                f"{BIND}/progress", {"uid": "UID_from_request"}, content_type="application/json",
            )
            self.assertEqual(response.json()["data"], {"bound": True})
            self.assertIn("wxpusher_browser", response.cookies)
            self.assertEqual(self.remote.verify_scanned_uid.call_count, 2)
            self.remote.verify_scanned_uid.assert_called_with(
                app_token="synthetic-app-token", code="qr-code", app_id="8123",
            )
            self.remote.send.assert_not_called()

        binding = WxPusherBinding.objects.get(user__username="binding-worker", app_id="8123")
        self.assertIsNone(binding.uid)
        self.assertEqual(binding.scan_bound_uid, "UID_synthetic")
        self.assertFalse(WxPusherBinding.objects.filter(app_id="8123", uid="UID_synthetic").exists())
        self.assertFalse(binding.challenge)
        guide = self.worker.get(GUIDE).json()["data"]
        self.assertTrue(guide["bound"])
        self.assertIsNone(guide["pending_binding"])
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        readonly = other_browser.post(f"{BIND}/progress")
        self.assertEqual(readonly.json()["data"], {"bound": True})
        self.assertNotIn("wxpusher_browser", readonly.cookies)
        self.assertEqual(self.post("test")["data"], {"accepted": True})
        self.assertEqual(self.remote.send.call_args.kwargs["uid"], "UID_synthetic")
        self.assertEqual(self.worker.delete(BIND).json()["data"], {"bound": False})
        binding.refresh_from_db()
        self.assertIsNone(binding.scan_bound_uid)

    def test_app_can_bind_by_scanning_without_a_browser_cookie_or_verification_code(self):
        app = self.app_client("synthetic-native-binding-token")
        self.assertTrue(app.get(GUIDE).json()["data"]["native_binding_supported"])
        self.assertNotIn("native_binding_supported", self.worker.get(GUIDE).json()["data"])

        start = app.post(f"{BIND}/start", {}, content_type="application/json").json()["data"]
        self.assertEqual(start["qr_url"], QR)
        self.assertIsNone(self.worker.get(GUIDE).json()["data"]["pending_binding"])

        response = app.post(f"{BIND}/progress", {"challenge": start["challenge"]}, content_type="application/json")
        self.assertEqual(response.json()["data"], {"bound": True})
        self.assertNotIn("wxpusher_browser", response.cookies)
        self.assertTrue(app.get(GUIDE).json()["data"]["bound"])
        binding = WxPusherBinding.objects.get(user__username="binding-worker")
        self.assertEqual(binding.scan_bound_uid, "UID_synthetic")
        self.assertIsNone(binding.uid)
        self.remote.send.assert_not_called()

    def test_app_binding_proof_is_restricted_to_the_original_login(self):
        make_logged_in_client(username="other-binding-worker", groups=(WORKER,), nodes=("my_profile",), games=None)
        no_permission = make_logged_in_client(username="not-a-binding-worker", nodes=("my_profile",), games=None)
        app = self.app_client("synthetic-native-first-token")
        other_device = self.app_client("synthetic-native-second-token")
        other_account = self.app_client("synthetic-native-other-account", username="other-binding-worker")
        start = app.post(f"{BIND}/start", {}, content_type="application/json").json()["data"]
        data = {"challenge": start["challenge"]}

        for client in (other_device, other_account, self.worker):
            response = client.post(f"{BIND}/progress", data, content_type="application/json")
            self.assertNotEqual(response.json()["code"], ErrorCode.OK)
        self.assertEqual(no_permission.post(f"{BIND}/progress", data, content_type="application/json").status_code, 403)
        anonymous = Client(SERVER_NAME=self.get_test_tenant_domain())
        self.assertEqual(anonymous.post(f"{BIND}/progress", data, content_type="application/json").status_code, 401)
        malformed = app.post(f"{BIND}/progress", {"challenge": []}, content_type="application/json")
        self.assertNotEqual(malformed.json()["code"], ErrorCode.OK)
        self.remote.verify_scanned_uid.assert_not_called()
        self.assertFalse(WxPusherBinding.objects.filter(scan_bound_uid="UID_synthetic").exists())

        response = app.post(f"{BIND}/progress", data, content_type="application/json")
        self.assertEqual(response.json()["data"], {"bound": True})
        self.remote.verify_scanned_uid.assert_called_once()

    def test_app_binding_refuses_a_token_revoked_while_querying_provider(self):
        token = "synthetic-native-revoked-token"
        app = self.app_client(token)
        start = app.post(f"{BIND}/start", {}, content_type="application/json").json()["data"]

        def revoke_token(**_):
            AppToken.objects.filter(key_hash=hash_token(token)).update(revoked_at=timezone.now())
            return "UID_synthetic"

        self.remote.verify_scanned_uid.side_effect = revoke_token
        response = app.post(f"{BIND}/progress", {"challenge": start["challenge"]}, content_type="application/json")
        self.assertIn("重新登录", response.json()["msg"])
        self.assertIsNone(WxPusherBinding.objects.get(user__username="binding-worker").scan_bound_uid)

    def test_auto_binding_does_not_attach_a_result_after_another_generation_starts(self):
        now = timezone.now()
        self.assertEqual(self.post("start")["code"], 0)
        original = WxPusherBinding.objects.get(user__username="binding-worker")

        def replace_qr(**kwargs):
            self.assertEqual(kwargs["code"], "qr-code")
            WxPusherBinding.objects.filter(user__username="binding-worker").update(
                challenge="replacement", qr_code="replacement-code",
                expires_at=now + timedelta(minutes=10),
            )
            return "UID_synthetic"

        self.remote.verify_scanned_uid.side_effect = replace_qr
        self.assertIn("失效", self.post("progress")["msg"])
        original.refresh_from_db()
        self.assertEqual(original.challenge, "replacement")
        self.assertIsNone(original.scan_bound_uid)
        self.assertIsNone(self.worker.get(GUIDE).json()["data"]["pending_binding"])

    def test_auto_binding_checks_expiry_and_does_not_dispatch_a_provider_request(self):
        now = timezone.now()
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            self.assertEqual(self.post("start")["code"], 0)
            clock.return_value = now + timedelta(minutes=5)
            self.assertIsNone(self.worker.get(GUIDE).json()["data"]["pending_binding"])
            self.assertIn("失效", self.post("progress")["msg"])
            self.remote.verify_scanned_uid.assert_not_called()

    def test_auto_binding_refuses_password_changed_while_querying_provider(self):
        self.assertEqual(self.post("start")["code"], 0)

        def password_reset(**kwargs):
            worker = get_user_model().objects.get(username="binding-worker")
            worker.set_password("different-test-password")
            worker.save(update_fields=["password"])
            return "UID_synthetic"

        self.remote.verify_scanned_uid.side_effect = password_reset
        response = self.worker.post(f"{BIND}/progress")
        self.assertIn("重新登录", response.json()["msg"])
        self.assertNotIn("wxpusher_browser", response.cookies)
        self.assertIsNone(WxPusherBinding.objects.get(user__username="binding-worker").scan_bound_uid)

    def test_auto_binding_cannot_duplicate_an_otp_uid_even_if_writes_race(self):
        other = get_user_model().objects.create_user(username="existing-notification-owner")
        WxPusherBinding.objects.create(user=other, uid="UID_synthetic")
        self.assertEqual(self.post("start")["code"], 0)
        self.assertIn("已在本工作室绑定", self.post("progress")["msg"])
        row = WxPusherBinding.objects.get(user__username="binding-worker")
        self.assertIsNone(row.scan_bound_uid)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WxPusherBinding.objects.filter(pk=row.pk).update(scan_bound_uid="UID_synthetic")

    def test_expired_qr_and_expired_code_cannot_be_used(self):
        now = timezone.now()
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            self.assertEqual(self.post("start")["code"], 0)
            clock.return_value = now + timedelta(minutes=5)
            self.assertIn("失效", self.post("scan")["msg"])
            self.remote.verify_scanned_uid.assert_not_called()
            self.assertEqual(self.post("start")["code"], 0)
            self.assertEqual(self.post("scan")["code"], 0)
            clock.return_value = now + timedelta(minutes=10)
            self.assertIn("失效", self.post("confirm", code="000420")["msg"])
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])

    def test_uncertain_delivery_is_not_retried_and_wrong_codes_lock_challenge(self):
        self.remote.send.side_effect = WxPusherError("timeout")
        self.assertEqual(self.post("start")["code"], 0)
        self.assertTrue(self.post("scan")["data"]["delivery_unconfirmed"])
        self.assertTrue(self.post("scan")["data"]["delivery_unconfirmed"])
        self.remote.send.assert_called_once()
        for _ in range(4):
            self.assertIn("不正确", self.post("confirm", code="111111")["msg"])
        self.assertIn("次数已用完", self.post("confirm", code="111111")["msg"])
        self.assertIn("失效", self.post("confirm", code="000420")["msg"])
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])

    def test_already_linked_wxpusher_uid_cannot_receive_new_code(self):
        other = get_user_model().objects.create_user(username="existing-notification-owner")
        WxPusherBinding.objects.create(user=other, uid="UID_synthetic")
        self.assertEqual(self.post("start")["code"], 0)
        self.assertIn("已在本工作室绑定", self.post("scan")["msg"])
        self.remote.send.assert_not_called()
        self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])

    def test_switch_to_new_app_requires_its_own_binding_and_remembers_current_browser(self):
        worker = get_user_model().objects.get(username="binding-worker")
        WxPusherBinding.objects.create(user=worker, uid="UID_synthetic")
        with override_settings(WXPUSHER_APP_ID="8123", WXPUSHER_LOGIN_SIGNING_KEY="test-only-signing-key"):
            self.assertFalse(self.worker.get(GUIDE).json()["data"]["bound"])
            self.assertNotEqual(self.post("scan")["code"], 0)
            self.assertEqual(self.post("start")["code"], 0)
            self.assertEqual(self.post("scan")["code"], 0)
            self.remote.verify_scanned_uid.assert_called_once_with(
                app_token="synthetic-app-token", code="qr-code", app_id="8123",
            )
            response = self.worker.post(
                f"{BIND}/confirm", {"code": "000420"}, content_type="application/json",
            )
            self.assertEqual(response.json()["data"], {"bound": True})
            self.assertIn("wxpusher_browser", response.cookies)
            self.assertTrue(self.worker.get(GUIDE).json()["data"]["bound"])
        self.assertTrue(self.worker.get(GUIDE).json()["data"]["bound"])
        self.assertEqual(
            set(WxPusherBinding.objects.filter(user=worker).values_list("app_id", "uid")),
            {("", "UID_synthetic"), ("8123", "UID_synthetic")},
        )

    def test_soft_deleted_account_does_not_keep_notification_binding(self):
        admin = make_logged_in_client(username="binding-admin", is_superuser=True, games=None)
        worker = get_user_model().objects.get(username="binding-worker")
        WxPusherBinding.objects.create(user=worker, uid="UID_synthetic")
        response = admin.post(f"/api/accounts/{worker.pk}/delete/")
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertFalse(WxPusherBinding.objects.filter(user__username="binding-worker").exists())
