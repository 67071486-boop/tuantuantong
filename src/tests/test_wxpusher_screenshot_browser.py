from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import EmpType, Game, Order, Performance
from apps.business.order_writes import lock_order_row
from apps.core.storage import get_storage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

BASE = "/api/business/screenshot-browser/"
ORDER_NO = "10000000000000000031"
OTHER_ORDER_NO = "10000000000000000032"
WXPUSHER_AGENT = "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36/WxPusher-Android"
UPLOAD_NODES = ("my_orders.send_to_my_system", "order.upload_screenshot")


class ScreenshotBrowserTests(TenantTestCase):
    def setUp(self):
        self.source = make_logged_in_client(
            username="screenshot-worker", groups=("代练员",), nodes=UPLOAD_NODES,
        )
        self.browser = Client(SERVER_NAME="tenant.test.com")
        self.worker = get_user_model().objects.get(username="screenshot-worker")
        self.game = Game.objects.create(name="测试游戏")
        self.order = self.make_order(ORDER_NO, self.worker)

    def make_order(self, order_no, user):
        order = Order.objects.create(order_no=order_no, game=self.game)
        Performance.objects.create(order=order, user=user, emp_type=EmpType.MAIN)
        return order

    def prepare(self, client=None, order_no=ORDER_NO):
        client = self.source if client is None else client
        return client.post(
            BASE + "start", {"order_no": order_no}, content_type="application/json",
            HTTP_USER_AGENT=WXPUSHER_AGENT,
        )

    def exchange(self, browser, challenge, order_no=ORDER_NO, **extra):
        return browser.post(
            BASE + "exchange", {"order_no": order_no, "challenge": challenge},
            content_type="application/json", **extra,
        )

    def ticket(self, browser, order_no=ORDER_NO, **extra):
        return browser.post(
            BASE + "ticket", {"order_no": order_no, "ext": "jpg", **extra},
            content_type="application/json",
        )

    def test_external_browser_uploads_to_same_order_without_logging_in(self):
        prepared = self.prepare()
        self.assertEqual(prepared.json()["code"], 0, prepared.content)
        challenge = prepared.json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        self.assertEqual(self.browser.get("/api/auth/me").status_code, 401)

        response = self.ticket(self.browser)
        self.assertEqual(response.json()["code"], 0, response.content)
        ticket = response.json()["data"]
        photo = b"\xff\xd8\xff" + b"a" * (3 * 1024 * 1024)
        self.assertEqual(self.browser.put(ticket["upload_url"], photo, content_type="image/jpeg").status_code, 200)
        result = self.browser.post(
            BASE + "claim", {
                "order_no": ORDER_NO, "screenshot_id": ticket["screenshot_id"],
                "original_name": "测试进度.jpg",
            }, content_type="application/json",
        )
        self.assertEqual(result.json()["code"], 0, result.content)
        self.assertTrue(self.order.screenshots.filter(
            original_name="测试进度.jpg", status="ready", uploaded_by=self.worker, img_type=0,
        ).exists())
        replay = self.browser.put(ticket["upload_url"], b"replacement", content_type="image/jpeg")
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(get_storage().head(ticket["key"])[0], len(photo))

    def test_upload_locks_order_before_pending_screenshot(self):
        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        ticket = self.ticket(self.browser).json()["data"]
        with CaptureQueriesContext(connection) as queries:
            uploaded = self.browser.put(ticket["upload_url"], b"test-photo", content_type="image/jpeg")
        self.assertEqual(uploaded.status_code, 200)
        row_locks = [query["sql"] for query in queries if "FOR UPDATE" in query["sql"]]
        self.assertEqual(len(row_locks), 2, row_locks)
        self.assertIn('FROM "business_order"', row_locks[0])
        self.assertIn('FROM "business_orderscreenshot"', row_locks[1])

    def test_waiting_writes_recheck_order_access_after_lock(self):
        replacement = get_user_model().objects.create_user(username="replacement-worker")
        assignment = self.order.performances.get(user=self.worker, emp_type=EmpType.MAIN)
        photo = b"\xff\xd8\xff" + b"test-photo"

        def change_assignment_while_waiting(order):
            Performance.objects.filter(pk=assignment.pk).update(user=replacement)
            return lock_order_row(order)

        for operation in ("ticket", "blob", "claim"):
            with self.subTest(operation=operation):
                Performance.objects.filter(pk=assignment.pk).update(user=self.worker)
                challenge = self.prepare().json()["data"]["challenge"]
                self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
                ticket = self.ticket(self.browser).json()["data"]
                uploaded = self.browser.put(ticket["upload_url"], photo, content_type="image/jpeg")
                self.assertEqual(uploaded.status_code, 200)
                before = self.order.screenshots.count()

                with patch(
                    "apps.business.api.screenshot_browser.lock_order_row",
                    side_effect=change_assignment_while_waiting,
                ) as lock:
                    if operation == "ticket":
                        response = self.ticket(self.browser)
                    elif operation == "blob":
                        response = self.browser.put(
                            ticket["upload_url"], b"replacement", content_type="image/jpeg",
                        )
                    else:
                        response = self.browser.post(
                            BASE + "claim", {"order_no": ORDER_NO, "screenshot_id": ticket["screenshot_id"]},
                            content_type="application/json",
                        )

                self.assertEqual(response.status_code, 403, response.content)
                lock.assert_called_once()
                self.assertEqual(self.order.screenshots.count(), before)
                self.assertTrue(self.order.screenshots.filter(pk=ticket["screenshot_id"], status="pending").exists())
                self.assertEqual(get_storage().head(ticket["key"])[0], len(photo))

    @override_settings(MEDIA_STORAGE_BACKEND="cos")
    def test_cos_upload_rechecks_current_grant_and_does_not_sign_a_direct_put(self):
        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        with (
            patch("apps.business.api.screenshot_views.get_storage") as signing_storage,
            patch("apps.core.media_blob_views.get_storage") as writing_storage,
        ):
            response = self.ticket(self.browser)
            self.assertEqual(response.json()["code"], 0, response.content)
            ticket = response.json()["data"]
            self.assertTrue(ticket["upload_url"].startswith(BASE + "blob/"))
            uploaded = self.browser.put(ticket["upload_url"], b"test-photo", content_type="image/jpeg")
            self.assertEqual(uploaded.status_code, 200)
            signing_storage.assert_not_called()
            writing_storage.return_value.put_bytes.assert_called_once_with(
                ticket["key"], b"test-photo", content_type="image/jpeg",
            )
            self.assertEqual(self.source.post("/api/auth/logout").status_code, 200)
            revoked = self.browser.put(ticket["upload_url"], b"replacement", content_type="image/jpeg")
            self.assertEqual(revoked.status_code, 403)
            self.assertEqual(writing_storage.return_value.put_bytes.call_count, 1)

    def test_start_requires_an_authenticated_worker_with_both_permissions_and_the_order(self):
        self.assertEqual(self.prepare(self.browser).status_code, 401)

        wrong_browser = self.source.post(
            BASE + "start", {"order_no": ORDER_NO}, content_type="application/json",
        )
        self.assertEqual(wrong_browser.status_code, 403)
        other = make_logged_in_client(
            username="other-shot-worker", groups=("代练员",), nodes=UPLOAD_NODES,
        )
        order = self.make_order(OTHER_ORDER_NO, get_user_model().objects.get(username="other-shot-worker"))
        self.assertEqual(self.prepare(self.source, order.order_no).status_code, 403)
        self.assertEqual(self.prepare(other, order.order_no).json()["code"], 0)

        self.worker.groups.clear()
        self.assertEqual(self.prepare().status_code, 403)

    def test_only_external_browser_can_exchange_and_link_is_single_use(self):
        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(
            self.exchange(self.source, challenge, HTTP_USER_AGENT=WXPUSHER_AGENT).status_code, 403,
        )
        self.make_order(OTHER_ORDER_NO, self.worker)
        self.assertEqual(self.exchange(self.browser, challenge, OTHER_ORDER_NO).status_code, 403)
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        self.assertEqual(self.exchange(Client(SERVER_NAME="tenant.test.com"), challenge).status_code, 403)
        self.assertEqual(self.ticket(self.browser, OTHER_ORDER_NO).status_code, 403)

    def test_status_recovers_only_the_matching_exchange(self):
        first = self.prepare().json()["data"]["challenge"]
        second = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, first).json()["code"], 0)
        status_url = BASE + "status"
        query = {"order_no": ORDER_NO}
        matching = self.browser.get(status_url, query, HTTP_X_SCREENSHOT_CHALLENGE=first)
        different = self.browser.get(status_url, query, HTTP_X_SCREENSHOT_CHALLENGE=second)
        self.assertEqual(matching.json()["code"], 0)
        self.assertEqual(different.status_code, 403)

    def test_expired_link_and_scope_cannot_be_used(self):
        now = timezone.now()
        challenge = self.prepare().json()["data"]["challenge"]
        with patch("django.utils.timezone.now", return_value=now + timedelta(minutes=3)):
            self.assertEqual(self.exchange(self.browser, challenge).status_code, 403)

        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        with patch("django.utils.timezone.now", return_value=now + timedelta(minutes=16)):
            self.assertEqual(self.ticket(self.browser).status_code, 403)

    def test_source_logout_and_order_reassignment_revoke_scope(self):
        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        self.assertEqual(self.source.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.ticket(self.browser).status_code, 403)
        self.assertEqual(self.source.post(
            "/api/auth/login", {"username": self.worker.username, "password": P2_TEST_PASSWORD},
            content_type="application/json",
        ).json()["code"], 0)

        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        self.order.performances.all().delete()
        self.assertEqual(self.ticket(self.browser).status_code, 403)

    def test_a_different_browser_account_is_preserved(self):
        challenge = self.prepare().json()["data"]["challenge"]
        other = make_logged_in_client(username="browser-current-account")
        conflict = self.exchange(other, challenge)
        self.assertEqual(conflict.status_code, 403)
        self.assertIn("回到 WxPusher，从右上角复制上传链接", conflict.json()["msg"])
        self.assertIn("如果链接已用过，请先重新生成", conflict.json()["msg"])
        self.assertEqual(other.get("/api/auth/me").json()["data"]["user"]["username"], "browser-current-account")
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)

    def test_exchange_and_upload_require_csrf_even_with_no_login_cookie(self):
        challenge = self.prepare().json()["data"]["challenge"]
        browser = Client(SERVER_NAME="tenant.test.com", enforce_csrf_checks=True)
        self.assertEqual(self.exchange(browser, challenge).status_code, 403)
        self.assertEqual(browser.get("/api/auth/csrf").status_code, 200)
        csrf = browser.cookies["csrftoken"].value
        result = self.exchange(browser, challenge, HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(result.json()["code"], 0)
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertNotIn("sessionid", result.cookies)
        grant = result.cookies["ttt_shot_upload_" + ORDER_NO]
        self.assertTrue(grant["httponly"])
        self.assertEqual(grant["samesite"], "Strict")
        self.assertEqual(grant["path"], "/api/business/screenshot-browser/")
        self.assertEqual(self.ticket(browser).status_code, 403)

    def test_scoped_claim_cannot_publish_a_pending_group_qr(self):
        other_upload = self.source.post(
            f"/api/business/orders/{ORDER_NO}/screenshots/ticket",
            {"ext": "jpg", "img_type": 1}, content_type="application/json",
        ).json()["data"]
        get_storage().put_bytes(
            other_upload["key"], b"\xff\xd8\xff" + b"test-photo", content_type="image/jpeg",
        )
        challenge = self.prepare().json()["data"]["challenge"]
        self.assertEqual(self.exchange(self.browser, challenge).json()["code"], 0)
        result = self.browser.post(
            BASE + "claim", {"order_no": ORDER_NO, "screenshot_id": other_upload["screenshot_id"]},
            content_type="application/json",
        )
        self.assertEqual(result.json()["code"], 1000)
        self.assertFalse(self.order.screenshots.filter(img_type=1, status="ready").exists())


class ScreenshotBrowserTenantTests(TenantTestCase):
    DOMAIN_B = "shot-browser-b.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="shot_browser_b", name="截图隔离租户 B", domain=cls.DOMAIN_B,
        )

    def test_a_link_cannot_exchange_against_b_order(self):
        with tenant_context(self.tenant):
            challenge_a = self.start_for_current_tenant("tenant.test.com", ORDER_NO, "worker-a")
        try:
            with tenant_context(self.tenant_b):
                challenge_b = self.start_for_current_tenant(self.DOMAIN_B, OTHER_ORDER_NO, "worker-b")
            browser = Client(SERVER_NAME=self.DOMAIN_B)
            invalid = browser.post(
                BASE + "exchange", {"order_no": OTHER_ORDER_NO, "challenge": challenge_a},
                content_type="application/json",
            )
            self.assertEqual(invalid.status_code, 403)
            valid = browser.post(
                BASE + "exchange", {"order_no": OTHER_ORDER_NO, "challenge": challenge_b},
                content_type="application/json",
            )
            self.assertEqual(valid.json()["code"], 0)
        finally:
            connection.set_tenant(self.tenant)

    def start_for_current_tenant(self, domain, order_no, username):
        source = make_logged_in_client(
            domain=domain, username=username, groups=("代练员",), nodes=UPLOAD_NODES,
        )
        game = Game.objects.create(name="租户隔离游戏")
        order = Order.objects.create(order_no=order_no, game=game)
        Performance.objects.create(
            order=order, user=get_user_model().objects.get(username=username), emp_type=EmpType.MAIN,
        )
        response = source.post(
            BASE + "start", {"order_no": order_no}, content_type="application/json",
            HTTP_USER_AGENT=WXPUSHER_AGENT,
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]["challenge"]
