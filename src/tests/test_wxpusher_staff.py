from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_notifications import send_notification_batch
from apps.business.approvals import create_refund_approval
from apps.business.models import Game, Order
from apps.business.roles import SERVICE
from apps.core.wxpusher import WxPusherQRCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client


BASE = "/api/auth/wxpusher/staff"
QR = "https://wxpusher.zjiecode.com/api/qrcode/StaffNoticeSynthetic.jpg"
OTHER_DOMAIN = "wx-staff-other.test.local"


class StaffWxPusherTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="812",
            WXPUSHER_APP_TOKEN="AT_synthetic_staff_qr",
            WXPUSHER_LOGIN_SIGNING_KEY="synthetic-staff-login-key",
        ))
        self.client = make_logged_in_client(username="staff-local", groups=(SERVICE,), nodes=("my_profile",))

    def json(self, method, path, data=None):
        response = method(BASE + path, data or {}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:180])
        return response.json()

    def test_staff_binds_by_scanning_and_sends_one_test_message(self):
        self.assertFalse(self.json(self.client.get, "/guide")["data"]["bound"])
        with patch("apps.accounts.wxpusher_binding.WxPusherClient") as provider:
            remote = provider.return_value.__enter__.return_value
            remote.create_qrcode.return_value = WxPusherQRCode("synthetic-staff-code", QR)
            remote.verify_scanned_uid.return_value = "UID_synthetic_staff"
            self.assertEqual(self.json(self.client.post, "/binding/start")["data"]["qr_url"], QR)
            self.assertTrue(self.json(self.client.get, "/guide")["data"]["pending_binding"])
            self.assertTrue(self.json(self.client.post, "/binding/progress")["data"]["bound"])
            remote.send.assert_not_called()
            self.assertNotIn("wxpusher_browser", self.client.cookies)
            self.assertEqual(self.json(self.client.post, "/binding/test")["data"], {"accepted": True})
            self.assertNotEqual(self.json(self.client.post, "/binding/test")["code"], 0)
            remote.send.assert_called_once()
        self.assertTrue(WxPusherBinding.objects.filter(
            user__username="staff-local", scan_bound_uid="UID_synthetic_staff",
        ).exists())
        self.assertEqual(WxPusherNotice.objects.get(user__username="staff-local", kind="test").status, "sent")
        self.assertFalse(self.json(self.client.delete, "/binding")["data"]["bound"])

    def test_preferences_staff_opt_in_defaults_off_and_updates_keys_independently(self):
        expected = {"enabled": False, "group_chat_enabled": True, "events": {"approval_assigned": True, "refund_assigned": True}}
        self.assertEqual(self.json(self.client.get, "/preferences")["data"], expected)
        with CaptureQueriesContext(connection) as queries:
            updated = self.json(self.client.patch, "/preferences", {
                "enabled": True, "group_chat_enabled": False, "events": {"refund_assigned": False},
            })
        account_locks = [
            query["sql"] for query in queries.captured_queries if 'FROM "auth_user"' in query["sql"] and "FOR " in query["sql"]
        ]
        self.assertEqual(len(account_locks), 1)
        self.assertIn("FOR NO KEY UPDATE", account_locks[0])
        self.assertEqual(updated["data"], {"enabled": True, "group_chat_enabled": True, "events": {
            "approval_assigned": True, "refund_assigned": False,
        }})
        refused = self.json(self.client.patch, "/preferences", {"events": {"refund_assigned": "yes"}})
        self.assertNotEqual(refused["code"], 0)
        self.assertFalse(self.json(self.client.get, "/preferences")["data"]["events"]["refund_assigned"])

    def test_staff_endpoints_require_own_web_account_and_do_not_open_worker_route(self):
        guest = Client(SERVER_NAME=self.get_test_tenant_domain())
        self.assertEqual(guest.get(BASE + "/guide").status_code, 401)
        self.assertEqual(self.client.get("/api/auth/wxpusher/guide").status_code, 403)
        user = get_user_model().objects.get(username="staff-local")
        StaffProfile.objects.filter(user=user).update(account_source=AccountSource.APP)
        self.assertEqual(self.client.get(BASE + "/guide").status_code, 403)


    def test_app_token_cannot_manage_web_staff_notifications(self):
        app = Client(SERVER_NAME=self.get_test_tenant_domain())
        response = app.post("/api/app/auth/login/", {
            "username": "staff-local", "password": P2_TEST_PASSWORD,
        }, content_type="application/json")
        self.assertEqual(response.json()["code"], 0)
        headers = {"HTTP_AUTHORIZATION": "Bearer " + response.json()["data"]["token"]}
        self.assertEqual(app.get("/api/app/auth/me/", **headers).status_code, 200)
        self.assertEqual(app.get(BASE + "/guide", **headers).status_code, 401)
        with patch("apps.accounts.wxpusher_binding.WxPusherClient") as provider:
            self.assertEqual(app.post(BASE + "/binding/start", **headers).status_code, 401)
            self.assertEqual(app.delete(BASE + "/binding", **headers).status_code, 401)
            self.assertEqual(app.patch(
                BASE + "/preferences", {"enabled": True}, content_type="application/json", **headers,
            ).status_code, 401)
            provider.assert_not_called()

    def test_refund_created_once_and_delivered_only_to_current_assigned_staff(self):
        admin = make_logged_in_client(username="staff-approvals-admin", is_superuser=True)
        replacement = make_logged_in_client(username="staff-replacement", groups=(SERVICE,), nodes=("my_profile",))
        self.assertEqual(self.json(self.client.patch, "/preferences", {"enabled": True})["code"], 0)
        self.assertEqual(self.json(replacement.patch, "/preferences", {"enabled": True})["code"], 0)
        users = get_user_model().objects
        previous_user = users.get(username="staff-local")
        replacement_user = users.get(username="staff-replacement")
        WxPusherBinding.objects.create(user=previous_user, app_id="812", uid="UID_staff_previous")
        WxPusherBinding.objects.create(user=replacement_user, app_id="812", uid="UID_staff_replacement")
        path = "/api/business/approvals/settings/"
        configured = admin.patch(path, {"refund_handler_id": previous_user.pk}, content_type="application/json")
        self.assertEqual(configured.json()["code"], 0, configured.json())

        order = Order.objects.create(
            game=Game.objects.create(name="合成员工退款游戏"),
            order_no="20261117000000000042", source_order_no="synthetic-staff-refund",
        )
        approval = create_refund_approval(order, reason="只在审批页显示的合成退款原因")
        again = create_refund_approval(order, reason="重复回调")
        self.assertEqual(approval.pk, again.pk)
        notices = WxPusherNotice.objects.filter(order_no=order.order_no, kind="refund_assigned")
        self.assertEqual(list(notices.values_list("user_id", flat=True)), [previous_user.pk])

        def route_to(user):
            assigned = admin.patch(path, {"refund_handler_id": user.pk}, content_type="application/json")
            self.assertEqual(assigned.json()["code"], 0, assigned.json())

        route_to(replacement_user)
        route_to(previous_user)
        with patch("apps.accounts.wxpusher_notifications.WxPusherClient") as provider:
            result = send_notification_batch(self.get_test_tenant_domain())
            self.assertEqual(result["sent"], 1, result)
            self.assertEqual(result["skipped"], 2, result)
            send = provider.return_value.__enter__.return_value.send
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs["uid"], "UID_staff_previous")
            self.assertEqual(send.call_args.kwargs["url"], f"https://{self.get_test_tenant_domain()}/v2/approvals")
            body = send.call_args.kwargs["content"]
            self.assertEqual(send.call_args.kwargs["summary"], "有一条退款待你处理")
            self.assertIn("待我处理", body)
            self.assertNotIn(order.order_no, body)
            self.assertNotIn(order.source_order_no, body)
            self.assertNotIn("只在审批页显示", body)
            filtered = admin.get("/api/business/wxpusher-config/history/", {"order_no": order.source_order_no})
            self.assertEqual(filtered.json()["data"]["total"], 3)
            self.assertEqual(list(notices.filter(user=previous_user).order_by("pk").values_list("status", flat=True)),
                             ["skipped", "sent"])
            self.assertEqual(notices.get(user=replacement_user).skip_reason, "recipient_unavailable")
            route_to(replacement_user)
            route_to(replacement_user)
            send.reset_mock()
            result = send_notification_batch(self.get_test_tenant_domain())
            self.assertEqual(result["sent"], 1, result)
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs["uid"], "UID_staff_replacement")
            self.assertEqual(list(notices.filter(user=replacement_user).order_by("pk").values_list("status", flat=True)),
                             ["skipped", "sent"])



class StaffWxPusherTenantIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(
            schema_name="wx_staff_other", name="合成员工通知乙", domain=OTHER_DOMAIN,
        )

    def test_tenant_a_credentials_cannot_read_or_change_tenant_b_preference(self):
        with tenant_context(self.other):
            other_client = make_logged_in_client(
                domain=OTHER_DOMAIN, username="staff-other-only",
                groups=(SERVICE,), nodes=("my_profile",),
            )
            response = other_client.patch(BASE + "/preferences", {"enabled": True}, content_type="application/json")
            self.assertEqual(response.json()["code"], 0)
        connection.set_tenant(self.tenant)
        own = make_logged_in_client(username="staff-main-only", groups=(SERVICE,), nodes=("my_profile",))
        cross_tenant = Client(SERVER_NAME=OTHER_DOMAIN)
        cross_tenant.cookies = own.cookies.copy()
        self.assertEqual(cross_tenant.get(BASE + "/preferences").status_code, 401)
        self.assertEqual(
            cross_tenant.patch(BASE + "/preferences", {"enabled": False}, content_type="application/json").status_code,
            401,
        )
        with tenant_context(self.other):
            self.assertTrue(other_client.get(BASE + "/preferences").json()["data"]["enabled"])
        connection.set_tenant(self.tenant)
