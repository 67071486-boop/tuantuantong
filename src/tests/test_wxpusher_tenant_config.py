import json
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.db import connection

from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import WxPusherBinding, WxPusherTenantSettings
from apps.audit.models import OperationLog
from apps.business.roles import TENANT_ADMIN, WORKER
from apps.core.wxpusher import WxPusherClient
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client


PATH = "/api/business/wxpusher-config/"
TOKEN = "synthetic-studio-app-token"
KEY = "synthetic-wxpusher-studio-long-login-key"
OTHER_DOMAIN = "push-config-other.test.local"


class WxPusherTenantConfigTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(schema_name="wxp_config_other", name="示例工作室乙", domain=OTHER_DOMAIN)

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=""))
        self.admin = make_logged_in_client(username="wxpusher-admin", groups=(TENANT_ADMIN,), games=None)

    def patch(self, body):
        return self.admin.patch(PATH, body, content_type="application/json").json()

    def test_admin_can_save_credentials_and_switches_without_exposing_secrets(self):
        status = self.admin.get(PATH).json()
        self.assertEqual(status["code"], 0)
        self.assertEqual(status["data"]["source"], "none")
        self.assertFalse(status["data"]["push_enabled"])
        self.assertFalse(status["data"]["binding_prompt_enabled"])
        saved = self.patch({
            "app_id": "8123",
            "app_token": TOKEN,
            "login_signing_key": KEY,
            "push_enabled": True,
            "login_enabled": True,
            "binding_prompt_enabled": True,
            "order_events": {"paused_resumed": False},
        })
        self.assertEqual(saved["code"], 0)
        self.assertEqual(saved["data"]["source"], "tenant")
        self.assertEqual(saved["data"]["app_id"], "8123")
        self.assertTrue(saved["data"]["configured"])
        self.assertTrue(saved["data"]["has_app_token"])
        self.assertTrue(saved["data"]["has_login_signing_key"])
        self.assertFalse(saved["data"]["order_events"]["paused_resumed"])
        self.assertTrue(saved["data"]["order_events"]["refunded"])
        self.assertTrue(saved["data"]["login_enabled"])
        self.assertTrue(saved["data"]["push_enabled"])
        self.assertTrue(saved["data"]["binding_prompt_enabled"])
        self.assertEqual(saved["data"]["system_url"], "https://tenant.test.com/api/auth/wxpusher/login")
        self.assertEqual(self.patch({"app_token": "", "login_signing_key": "", "push_enabled": False})["code"], 0)
        status = self.admin.get(PATH).json()
        self.assertTrue(status["data"]["has_app_token"])
        self.assertTrue(status["data"]["has_login_signing_key"])
        self.assertFalse(status["data"]["push_enabled"])
        self.assertTrue(status["data"]["login_enabled"])
        self.assertTrue(status["data"]["binding_prompt_enabled"])
        row = WxPusherTenantSettings.objects.get()
        self.assertTrue(row.app_token_encrypted)
        self.assertTrue(row.signing_key_encrypted)
        self.assertTrue(row.binding_prompt_enabled)
        audit = OperationLog.objects.filter(action="system.wxpusher_config_update").earliest("id")
        self.assertEqual(audit.actor, "wxpusher-admin")
        self.assertIn("binding_prompt_enabled", audit.field_name.split(","))
        for message in (saved, status, row.app_token_encrypted, row.signing_key_encrypted, vars(audit)):
            self.assertNotIn(TOKEN, str(message))
            self.assertNotIn(KEY, str(message))

    def test_only_tenant_admin_can_read_or_write(self):
        worker = make_logged_in_client(username="wxpusher-worker", groups=(WORKER,), games=None)
        anonymous = Client(SERVER_NAME="tenant.test.com")
        for endpoint in ("", "recipients/", "history/", "preview/"):
            self.assertEqual(anonymous.get(PATH + endpoint).status_code, 401)
            self.assertEqual(worker.get(PATH + endpoint).status_code, 403)
        self.assertEqual(worker.patch(PATH, {"push_enabled": False}, content_type="application/json").status_code, 403)
        self.assertEqual(worker.post(PATH + "test/", {"user_id": 1}, content_type="application/json").status_code, 403)

    def bind_worker(self, name, app_id, *, active=True):
        make_logged_in_client(username=name, groups=(WORKER,), games=None)
        user = get_user_model().objects.get(username=name)
        if not active:
            user.is_active = False
            user.save(update_fields=["is_active"])
        return WxPusherBinding.objects.create(user=user, app_id=app_id, scan_bound_uid="UID_" + name)

    def test_legacy_credentials_and_existing_binding_survive_first_save(self):
        legacy_follow = "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=8123"
        legacy_qr = "https://wxpusher.zjiecode.com/api/qrcode/synthetic_example.jpg"
        self.bind_worker("existing-worker", "8123")
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="8123",
            WXPUSHER_APP_TOKEN=TOKEN,
            WXPUSHER_LOGIN_SIGNING_KEY=KEY,
            WXPUSHER_SUBSCRIBE_URL=legacy_follow,
            WXPUSHER_SUBSCRIBE_QR_URL=legacy_qr,
        ):
            initial = self.admin.get(PATH).json()["data"]
            self.assertEqual(initial["source"], "environment")
            self.assertEqual(initial["subscribe_url"], legacy_follow)
            self.assertTrue(initial["login_enabled"])
            self.assertTrue(initial["push_enabled"])
            self.assertFalse(initial["binding_prompt_enabled"])
            self.assertEqual(
                [item["name"] for item in self.admin.get(PATH + "recipients/").json()["data"]["items"]],
                ["existing-worker"],
            )
            saved = self.patch({"push_enabled": False})["data"]
            self.assertEqual(saved["source"], "tenant")
            self.assertTrue(saved["configured"])
            self.assertFalse(saved["push_enabled"])
        # Once saved, the local row still works without inherited process settings.
        data = self.admin.get(PATH).json()["data"]
        self.assertTrue(data["login_enabled"])
        self.assertTrue(data["configured"])
        self.assertEqual(data["subscribe_url"], legacy_follow)
        worker_client = make_logged_in_client(username="guide-worker", groups=(WORKER,), nodes=("my_profile",), games=None)
        guide = worker_client.get("/api/auth/wxpusher/guide").json()["data"]
        self.assertEqual(guide["qr_url"], legacy_qr)

    def test_replacing_app_requires_token_and_resets_old_integration(self):
        self.assertEqual(self.patch({
            "app_id": "8123", "app_token": TOKEN, "login_signing_key": KEY, "login_enabled": True,
            "subscribe_url": "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=8123",
        })["code"], 0)
        self.bind_worker("old-app-worker", "8123")
        self.assertNotEqual(self.patch({"app_id": "9123"})["code"], 0)
        self.assertNotEqual(self.patch({
            "app_id": "9123", "app_token": "synthetic-replacement-token",
            "subscribe_url": "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=8123",
        })["code"], 0)
        self.assertEqual(self.admin.get(PATH).json()["data"]["app_id"], "8123")
        data = self.patch({"app_id": "9123", "app_token": "synthetic-replacement-token"})["data"]
        self.assertEqual(data["app_id"], "9123")
        stale_save = self.patch({"expected_app_id": "8123", "app_token": TOKEN})
        self.assertIn("应用已更新", stale_save["msg"])
        stale_send = self.admin.post(
            PATH + "test/", {"user_id": 1, "expected_app_id": "8123"}, content_type="application/json",
        ).json()
        self.assertIn("应用已更新", stale_send["msg"])
        self.assertEqual(data["subscribe_url"], "")
        self.assertFalse(data["has_login_signing_key"])
        self.assertFalse(data["login_enabled"])
        self.assertEqual(self.admin.get(PATH + "recipients/").json()["data"]["items"], [])

    def test_invalid_payload_leaves_configuration_untouched(self):
        for payload in (
            {"push_enabled": True},
            {"login_enabled": True},
            {"push_enabled": "false"},
            {"binding_prompt_enabled": "false"},
            {"app_id": "8123", "app_token": TOKEN, "login_signing_key": "too-short"},
            {"app_id": 8123},
            {"app_id": "abc8123", "app_token": TOKEN},
            {"app_id": "08123", "app_token": TOKEN},
            {"app_id": "-8123", "app_token": TOKEN},
            {"app_id": "8123", "app_token": ["not-a-token"]},
            {"app_id": "8123", "app_token": TOKEN, "subscribe_url": "https://example.invalid/"},
            {"order_events": {"settled": False}},
            {"order_events": {"refunded": 1}},
            {"tenant": "other"},
            ["not-an-object"],
        ):
            with self.subTest(payload=payload):
                self.assertNotEqual(self.patch(payload)["code"], 0)
        self.assertEqual(self.admin.get(PATH).json()["data"]["source"], "none")

    def test_only_bound_active_workers_can_receive_test_message_and_see_status_in_history(self):
        self.assertEqual(self.patch({"app_id": "8123", "app_token": TOKEN})["code"], 0)
        valid = self.bind_worker("test-recipient", "8123")
        second = self.bind_worker("other-recipient", "8123")
        inactive = self.bind_worker("inactive-recipient", "8123", active=False)
        wrong_app = self.bind_worker("wrong-app", "3210")
        names = self.admin.get(PATH + "recipients/").json()["data"]["items"]
        self.assertEqual(names, [
            {"id": second.user_id, "name": "other-recipient"}, {"id": valid.user_id, "name": "test-recipient"},
        ])

        requests = []

        def provider(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(request.url.path, "/api/send/message")
            if payload["uids"] == ["UID_test-recipient"]:
                return httpx.Response(200, json={
                    "code": 1000,
                    "data": [{"uid": "UID_test-recipient", "code": 1000, "messageContentId": 816}],
                })
            return httpx.Response(200, json={"code": 1000, "data": [{"uid": "UID_other-recipient", "code": 1003}]})

        def send(user_id):
            return self.admin.post(PATH + "test/", {"user_id": user_id}, content_type="application/json").json()

        with patch(
            "apps.accounts.wxpusher_binding.WxPusherClient",
            side_effect=lambda **kwargs: WxPusherClient(transport=httpx.MockTransport(provider), **kwargs),
        ):
            self.assertNotEqual(send(inactive.user_id)["code"], 0)
            self.assertNotEqual(send(wrong_app.user_id)["code"], 0)
            self.assertEqual(send(valid.user_id)["data"], {"accepted": True})
            self.assertNotEqual(send(valid.user_id)["code"], 0)
            self.assertNotEqual(send(second.user_id)["code"], 0)
        self.assertEqual([payload["uids"] for payload in requests], [["UID_test-recipient"], ["UID_other-recipient"]])
        self.assertEqual([payload["appToken"] for payload in requests], [TOKEN, TOKEN])
        self.assertIn("测试通知", requests[0]["content"])
        history = self.admin.get(PATH + "history/").json()["data"]["items"]
        self.assertEqual([(item["recipient"], item["kind"], item["status"]) for item in history], [
            ("other-recipient", "test", "unconfirmed"), ("test-recipient", "test", "sent"),
        ])
        self.assertNotIn("UID_test-recipient", str(history))
        self.assertNotIn(TOKEN, str(history))

    def test_preview_uses_the_production_templates_with_sample_values(self):
        response = self.admin.get(PATH + "preview/").json()
        self.assertEqual(response["code"], 0)
        previews = {item["kind"]: item for item in response["data"]["items"]}
        self.assertEqual(len(previews), 17)
        self.assertEqual(previews["direct_message"]["title"], "单聊有新消息")
        self.assertIn("打开单聊", previews["direct_message"]["html"])
        self.assertNotIn("示例订单", previews["direct_message"]["html"])
        self.assertEqual(previews["group_message"]["title"], "订单群有新消息")
        self.assertIn("打开群聊", previews["group_message"]["html"])
        self.assertIn("打开群聊", previews["group_mention"]["html"])
        self.assertIn("客服小林", previews["group_reply"]["html"])
        self.assertEqual(previews["hall_new_order"]["url"], "https://tenant.test.com/m/hall")
        self.assertIn("前往抢单", previews["hall_new_order"]["html"])
        self.assertEqual(previews["refund_assigned"]["url"], "https://tenant.test.com/v2/approvals")
        self.assertEqual(previews["assigned"]["title"], "收到新的订单指派")
        self.assertIn("¥120.00", previews["assigned"]["html"])
        self.assertIn("示例订单 0086", previews["assigned"]["html"])
        self.assertEqual(previews["assigned"]["url"], "https://tenant.test.com/m/mine")
        self.assertIn('href="https://tenant.test.com/m/mine"', previews["assigned"]["html"])
        self.assertEqual(previews["settled"]["url"], "https://tenant.test.com/m/performance")
        self.assertIn("订单提交验收不通过", previews["review_rejected"]["title"])

    def test_second_tenant_config_and_recipient_ids_are_isolated(self):
        self.assertEqual(self.patch({
            "app_id": "8123", "app_token": TOKEN, "binding_prompt_enabled": True,
        })["code"], 0)
        self.bind_worker("worker-from-a", "8123")
        try:
            with tenant_context(self.other):
                admin_b = make_logged_in_client(
                    domain=OTHER_DOMAIN, username="admin-in-b", groups=(TENANT_ADMIN,), games=None,
                )
                body = admin_b.get(PATH).json()["data"]
                self.assertEqual(body["source"], "none")
                self.assertEqual(body["app_id"], "")
                self.assertFalse(body["binding_prompt_enabled"])
                saved = admin_b.patch(PATH, {
                    "app_id": "9123",
                    "app_token": "synthetic-tenant-b-token",
                }, content_type="application/json").json()
                self.assertEqual(saved["code"], 0)
                self.assertFalse(saved["data"]["binding_prompt_enabled"])
                make_logged_in_client(
                    domain=OTHER_DOMAIN, username="worker-from-b", groups=(WORKER,), games=None,
                )
                worker_b = get_user_model().objects.get(username="worker-from-b")
                WxPusherBinding.objects.create(user=worker_b, app_id="9123", uid="UID_other_studio")
                recipients_b = admin_b.get(PATH + "recipients/").json()["data"]["items"]
                self.assertEqual([item["name"] for item in recipients_b], ["worker-from-b"])
                self.assertEqual(self.admin.get(PATH, SERVER_NAME=OTHER_DOMAIN).status_code, 401)
        finally:
            connection.set_tenant(self.tenant)
        # The cross-tenant guard invalidates the rejected session.
        login = self.admin.post("/api/auth/login", {
            "username": "wxpusher-admin", "password": P2_TEST_PASSWORD,
        }, content_type="application/json")
        self.assertEqual(login.json()["code"], 0)
        body = self.admin.get(PATH).json()["data"]
        self.assertEqual(body["app_id"], "8123")
        self.assertTrue(body["binding_prompt_enabled"])
        self.assertEqual(
            [item["name"] for item in self.admin.get(PATH + "recipients/").json()["data"]["items"]],
            ["worker-from-a"],
        )
