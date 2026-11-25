from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import WxPusherBinding
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherQRCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

GUIDE = "/api/auth/wxpusher/guide"
BIND = "/api/auth/wxpusher/binding"
SUBSCRIBE_A = "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=900000001"
SUBSCRIBE_QR = "https://wxpusher.zjiecode.com/api/qrcode/TestGuideQrOnly.jpg"
DOMAIN_B = "wxp-guide-b.test.local"


class WxPusherGuideTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="wxp_guide_b", name="示例工作室乙", domain=DOMAIN_B,
        )

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_TOKEN="",
            WXPUSHER_APP_ID="",
            WXPUSHER_SUBSCRIBE_URL="",
            WXPUSHER_SUBSCRIBE_QR_URL="",
        ))

    def worker_client(self):
        return make_logged_in_client(groups=(WORKER,), nodes=("my_profile",), games=None)

    @override_settings(WXPUSHER_SUBSCRIBE_URL=SUBSCRIBE_A)
    def test_guide_returns_only_the_public_link_to_a_taker(self):
        response = self.worker_client().get(GUIDE)
        self.assertEqual(response.json(), {
            "code": 0, "msg": "", "data": {
                "subscribe_url": SUBSCRIBE_A, "qr_url": None, "binding_enabled": False,
                "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
            },
        })
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(WXPUSHER_SUBSCRIBE_URL=SUBSCRIBE_A)
    def test_only_a_signed_in_worker_with_my_profile_can_read_configuration(self):
        self.assertEqual(Client(SERVER_NAME=self.get_test_tenant_domain()).get(GUIDE).status_code, 401)
        clients = (
            make_logged_in_client(username="guide-other", nodes=("my_profile",), games=None),
            make_logged_in_client(username="guide-admin", is_superuser=True, games=None),
        )
        for client in clients:
            response = client.get(GUIDE)
            self.assertEqual(response.status_code, 403)
            self.assertIsNone(response.json()["data"])

    @override_settings(WXPUSHER_SUBSCRIBE_URL=SUBSCRIBE_A)
    def test_worker_without_my_profile_cannot_read_guide_configuration(self):
        client = make_logged_in_client(groups=(WORKER,), games=None)
        response = client.get(GUIDE)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(response.json()["data"])

    def test_empty_or_invalid_links_do_not_generate_an_active_subscription(self):
        client = self.worker_client()
        for value in (
            "",
            "http://wxpusher.zjiecode.com/app/#/follow?type=1&id=900000001",
            "https://wxpusher.zjiecode.com.example.com/app/#/follow?type=1&id=900000001",
            "https://wxpusher.zjiecode.com/app/?openId=PRIVATE#/follow?type=1&id=900000001",
            "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=900000001&appToken=PRIVATE",
            "https://wxpusher.zjiecode.com/app/#/follow?type=2&id=900000001",
        ):
            with self.subTest(value=value), override_settings(WXPUSHER_SUBSCRIBE_URL=value):
                response = client.get(GUIDE)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"], {
                    "subscribe_url": None, "qr_url": None, "binding_enabled": False,
                    "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
                })

    @override_settings(WXPUSHER_SUBSCRIBE_QR_URL=SUBSCRIBE_QR)
    def test_official_qr_can_be_the_only_subscription_entry(self):
        response = self.worker_client().get(GUIDE)
        self.assertEqual(response.json()["data"], {
            "subscribe_url": None, "qr_url": SUBSCRIBE_QR, "binding_enabled": False,
            "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
        })

    @override_settings(WXPUSHER_SUBSCRIBE_QR_URL=SUBSCRIBE_QR + "?appToken=PRIVATE")
    def test_qr_with_unexpected_query_is_not_exposed(self):
        self.assertIsNone(self.worker_client().get(GUIDE).json()["data"]["qr_url"])

    @override_settings(
        WXPUSHER_SUBSCRIBE_TENANT_SCHEMA="",
        WXPUSHER_SUBSCRIBE_URL=SUBSCRIBE_A,
        WXPUSHER_SUBSCRIBE_QR_URL=SUBSCRIBE_QR,
    )
    def test_an_unscoped_subscription_is_never_exposed(self):
        self.assertEqual(self.worker_client().get(GUIDE).json()["data"], {
            "subscribe_url": None, "qr_url": None, "binding_enabled": False,
            "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
        })

    @override_settings(WXPUSHER_SUBSCRIBE_URL=SUBSCRIBE_A, WXPUSHER_SUBSCRIBE_QR_URL=SUBSCRIBE_QR)
    def test_only_the_configured_tenant_reads_the_guide_configuration(self):
        client_a = self.worker_client()
        self.assertEqual(client_a.get(GUIDE).json()["data"], {
            "subscribe_url": SUBSCRIBE_A, "qr_url": SUBSCRIBE_QR, "binding_enabled": False,
            "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
        })
        try:
            with tenant_context(self.tenant_b):
                client_b = make_logged_in_client(
                    domain=DOMAIN_B, groups=(WORKER,), nodes=("my_profile",), games=None,
                )
                response = client_b.get(GUIDE)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"], {
                    "subscribe_url": None, "qr_url": None, "binding_enabled": False,
                    "binding_prompt_enabled": False, "bound": False, "pending_binding": None,
                })
                response = client_a.get(GUIDE, SERVER_NAME=DOMAIN_B)
                self.assertEqual(response.status_code, 401)
                self.assertIsNone(response.json()["data"])
        finally:
            connection.set_tenant(self.tenant)

    @override_settings(WXPUSHER_APP_TOKEN="synthetic-app-token")
    def test_other_tenant_cannot_inspect_or_change_bindings(self):
        client_a = make_logged_in_client(
            username="bound-in-a", groups=(WORKER,), nodes=("my_profile",), games=None,
        )
        WxPusherBinding.objects.create(
            user=get_user_model().objects.get(username="bound-in-a"), uid="UID_private_a",
        )
        self.assertTrue(client_a.get(GUIDE).json()["data"]["bound"])

        try:
            with tenant_context(self.tenant_b):
                client_b = make_logged_in_client(
                    domain=DOMAIN_B, username="different-worker-in-b",
                    groups=(WORKER,), nodes=("my_profile",), games=None,
                )
                self.assertFalse(client_b.get(GUIDE).json()["data"]["bound"])
                with patch("apps.accounts.wxpusher_binding.WxPusherClient") as remote:
                    self.assertNotEqual(client_b.post(f"{BIND}/start").json()["code"], 0)
                    remote.assert_not_called()
                self.assertNotEqual(client_b.delete(BIND).json()["code"], 0)
                self.assertEqual(
                    client_b.delete(BIND, SERVER_NAME=self.get_test_tenant_domain()).status_code, 401,
                )
                connection.set_tenant(self.tenant_b)
                self.assertFalse(WxPusherBinding.objects.exists())
        finally:
            connection.set_tenant(self.tenant)

        self.assertEqual(
            WxPusherBinding.objects.get(user__username="bound-in-a").uid, "UID_private_a",
        )

    @override_settings(WXPUSHER_APP_TOKEN="synthetic-app-token")
    def test_tenant_a_qr_cannot_be_completed_on_tenant_b(self):
        client_a = make_logged_in_client(
            username="scanning-in-a", groups=(WORKER,), nodes=("my_profile",), games=None,
        )
        with patch("apps.accounts.wxpusher_binding.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            remote.create_qrcode.return_value = WxPusherQRCode("qr-code-in-a", SUBSCRIBE_QR)
            remote.verify_scanned_uid.return_value = "UID_private_scan_a"
            self.assertEqual(client_a.post(f"{BIND}/start").json()["code"], 0)
            try:
                with tenant_context(self.tenant_b):
                    client_b = make_logged_in_client(
                        domain=DOMAIN_B, username="scanning-in-b",
                        groups=(WORKER,), nodes=("my_profile",), games=None,
                    )
                    self.assertIsNone(client_b.get(GUIDE).json()["data"]["pending_binding"])
                    self.assertNotEqual(client_b.post(f"{BIND}/progress").json()["code"], 0)
                    remote.verify_scanned_uid.assert_not_called()
                    connection.set_tenant(self.tenant_b)
                    self.assertFalse(WxPusherBinding.objects.filter(
                        scan_bound_uid="UID_private_scan_a",
                    ).exists())
            finally:
                connection.set_tenant(self.tenant)

            self.assertEqual(client_a.post(f"{BIND}/progress").json()["data"], {"bound": True})
            self.assertEqual(
                WxPusherBinding.objects.get(user__username="scanning-in-a").scan_bound_uid, "UID_private_scan_a",
            )
            try:
                self.assertEqual(client_a.post(f"{BIND}/progress", SERVER_NAME=DOMAIN_B).status_code, 401)
            finally:
                connection.set_tenant(self.tenant)
            remote.verify_scanned_uid.assert_called_once()
