import hmac
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, WxPusherBinding
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherError
from apps.core.wxpusher_login import make_shared_hall_push_path
from apps.platform.models import Domain
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

ENTRY = "/api/auth/wxpusher/login"
APP_ID = "8123"
KEY = "test-only-system-key"
TOKEN = "test-only-app-token"
UID = "UID_signed_worker"
OTHER_DOMAIN = "wxp-login-other.test.local"
OFFICIAL = "https://wxpusher.zjiecode.com/"
ME = "/api/auth/me"
WORKER_ENTRY = "/m/?entry=wxpusher"
PASSWORD_ENTRY = "/m/login?entry=wxpusher"
HALL_CLAIM = "/m/hall?claimSource=local&claimRef=20261117000000000061"
HALL_PASSWORD = PASSWORD_ENTRY + "&" + urlencode({"redirect": HALL_CLAIM})
HALL_UNAVAILABLE = "/m/hall?claimUnavailable=1"
HALL_UNAVAILABLE_PASSWORD = PASSWORD_ENTRY + "&" + urlencode({"redirect": HALL_UNAVAILABLE})
DAIDAITONG_DOMAIN = "suqing.suqing.chat"
ANDROID_WXPUSHER_UA = "Mozilla/5.0 (Linux; Android 14) /WxPusher-Android"


def signed_params(uid=UID, *, nonce="example-nonce", offset_ms=0, key=KEY):
    fields = {"uid": uid, "nonce": nonce, "sv": "1", "ts": int(timezone.now().timestamp() * 1000) + offset_ms}
    canonical = "&".join(f"{name}={value}" for name, value in sorted(fields.items())).encode()
    return {**fields, "sign": hmac.new(key.encode(), canonical, "sha256").hexdigest()}


class WxPusherLoginTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(schema_name="wxp_login_other", name="示例工作室乙", domain=OTHER_DOMAIN)

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_LOGIN_SIGNING_KEY=KEY,
            WXPUSHER_APP_TOKEN=TOKEN,
        ))
        provider = self.enterContext(patch("apps.core.wxpusher_login.WxPusherClient"))
        self.subscription = provider.return_value.__enter__.return_value.is_subscribed
        self.subscription.return_value = True

    def browser(self, **kwargs):
        return Client(SERVER_NAME=self.get_test_tenant_domain(), **kwargs)

    def bind(self, username="linked-worker", uid=UID, app_id=APP_ID, roles=(WORKER,)):
        make_logged_in_client(username=username, groups=roles, games=None)
        user = get_user_model().objects.get(username=username)
        WxPusherBinding.objects.create(user=user, uid=uid, app_id=app_id)
        return user

    def assert_password_fallback(self, client, response):
        self.assertRedirects(response, PASSWORD_ENTRY, fetch_redirect_response=False)
        self.assertEqual(client.get(ME).status_code, 401)

    def test_unsigned_entry_falls_back_to_password_login(self):
        self.bind()
        client = self.browser()
        self.assert_password_fallback(client, client.get(ENTRY, {"uid": UID}))

    def test_shared_order_link_uses_current_tenant_login_and_requires_a_signed_callback(self):
        self.bind()
        path = make_shared_hall_push_path("peer", "23")
        self.assertEqual(path, "/m/wxpusher/hall-entry?entry=wxpusher&shared=1&source=peer&ref=23")
        params = {"source": "peer", "ref": "23"}
        url = "/api/auth/wxpusher/hall/continue"
        browser = self.browser()
        continuation = browser.get(url, params)
        self.assertEqual(continuation.json()["data"], {
            "url": "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=8123&jump=auto",
            "open_in_app": True,
        })
        self.assertIn("no-store", continuation["Cache-Control"])
        self.assertEqual(browser.get(ME).status_code, 401)
        self.assertEqual(browser.get(url, params | {"uid": UID}).status_code, 400)
        self.assertEqual(browser.get(url, params | {"ref": ["23", "24"]}).status_code, 400)
        self.assert_password_fallback(browser, browser.get(ENTRY, params | {"uid": UID}))
        self.subscription.assert_not_called()

        copied = Client(SERVER_NAME=OTHER_DOMAIN)
        self.assertIsNone(copied.get(url, params).json()["data"]["url"])
        self.assert_password_fallback(copied, copied.get(ENTRY, signed_params()))
        self.assertRedirects(
            browser.get(ENTRY, signed_params()), WORKER_ENTRY, fetch_redirect_response=False,
        )
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "linked-worker")

    @override_settings(DEPOSIT_ENFORCED_APP=True)
    def test_shared_hall_continuation_preserves_web_path_when_app_deposit_is_required(self):
        result = self.browser().get("/api/auth/wxpusher/hall/continue", {"source": "peer", "ref": "23"})
        self.assertFalse(result.json()["data"]["open_in_app"])
        self.assertIsNotNone(result.json()["data"]["url"])

    def confirm(self, client, response):
        return client.post(ENTRY, {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "confirmation": response.context["confirmation"],
        })

    def test_subscriber_who_unsubscribed_after_getting_link_falls_back_to_password(self):
        self.bind()
        client = self.browser(enforce_csrf_checks=True)
        self.subscription.return_value = False
        self.assert_password_fallback(client, client.get(ENTRY, signed_params()))
        self.subscription.assert_called_once_with(app_token=TOKEN, uid=UID, app_id=APP_ID)

    def test_subscription_check_timeout_during_direct_login_falls_back_to_password(self):
        self.bind()
        client = self.browser(enforce_csrf_checks=True)
        self.subscription.side_effect = WxPusherError("timeout")
        self.assert_password_fallback(client, client.get(ENTRY, signed_params()))

    def test_remembered_browser_logs_in_bound_worker_and_nonce_cannot_be_reused(self):
        self.bind()
        client = self.browser(enforce_csrf_checks=True)
        self.assertRedirects(
            client.get(ENTRY, signed_params(nonce="initial-link")), WORKER_ENTRY, fetch_redirect_response=False,
        )
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "linked-worker")

        cookie = client.cookies["wxpusher_browser"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], ENTRY)
        self.assertFalse(cookie["domain"])
        client.cookies.pop("sessionid")

        ticket = signed_params(nonce="fresh+link/=")
        response = client.get(ENTRY, ticket)
        self.assertRedirects(response, WORKER_ENTRY, fetch_redirect_response=False)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        me = client.get(ME).json()["data"]["user"]
        self.assertEqual(me["username"], "linked-worker")
        self.assertEqual(me["identity_plane"], self.tenant.schema_name)
        anonymous = self.browser()
        anonymous.cookies["wxpusher_browser"] = cookie.value
        self.assert_password_fallback(anonymous, anonymous.get(ENTRY, ticket))

    def test_unremembered_browser_logs_in_directly_on_official_callback(self):
        self.bind()
        ticket = signed_params()
        client = self.browser(enforce_csrf_checks=True)
        response = client.get(ENTRY, ticket, HTTP_REFERER=OFFICIAL)

        self.assertRedirects(response, WORKER_ENTRY, fetch_redirect_response=False)
        self.assertNotContains(response, UID, status_code=302)
        self.assertNotIn(ticket["sign"], response["Location"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "linked-worker")
        another = self.browser()
        self.assert_password_fallback(another, another.get(ENTRY, ticket, HTTP_REFERER=OFFICIAL))

    def test_key_change_during_subscription_check_prevents_new_session(self):
        self.bind()
        client = self.browser()

        def rotate_key(**_kwargs):
            self.enterContext(override_settings(WXPUSHER_LOGIN_SIGNING_KEY="also-test-only"))
            return True

        self.subscription.side_effect = rotate_key
        self.assert_password_fallback(client, client.get(ENTRY, signed_params()))
        self.subscription.assert_called_once()

    def test_uid_reassignment_during_subscription_check_prevents_new_session(self):
        self.bind()
        client = self.browser()

        def reassign_uid(**_kwargs):
            WxPusherBinding.objects.filter(uid=UID).update(uid=None)
            self.bind(username="other-worker")
            return True

        self.subscription.side_effect = reassign_uid
        self.assert_password_fallback(client, client.get(ENTRY, signed_params()))
        self.subscription.assert_called_once()

    def test_invalid_expired_future_or_duplicate_values_fail_closed(self):
        self.bind()
        invalid = (
            {**signed_params(), "sign": "0" * 64},
            signed_params(offset_ms=-601_000),
            signed_params(offset_ms=601_000),
            {**signed_params(), "sv": "2"},
        )
        for ticket in invalid:
            client = self.browser()
            with self.subTest(ticket_case=invalid.index(ticket)):
                self.assert_password_fallback(client, client.get(ENTRY, ticket, HTTP_REFERER=OFFICIAL))
        client = self.browser()
        duplicate = urlencode(signed_params()) + "&uid=UID_other"
        self.assert_password_fallback(client, client.get(f"{ENTRY}?{duplicate}", HTTP_REFERER=OFFICIAL))
        # 伪造链接不能抢占真票据的随机串。
        self.assertRedirects(
            client.get(ENTRY, signed_params(), HTTP_REFERER=OFFICIAL), WORKER_ENTRY, fetch_redirect_response=False,
        )

    def test_unbound_worker_can_still_use_password_and_legacy_app_is_not_implicitly_linked(self):
        self.bind(app_id="")
        client = self.browser()
        self.assert_password_fallback(client, client.get(ENTRY, signed_params(), HTTP_REFERER=OFFICIAL))
        response = client.post(
            "/api/auth/login", {"username": "linked-worker", "password": P2_TEST_PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "linked-worker")

    def test_scan_only_binding_logs_in_on_first_signed_callback_without_password(self):
        user = self.bind()
        WxPusherBinding.objects.filter(user=user).update(uid=None, scan_bound_uid=UID)
        browser = self.browser()
        response = browser.get(ENTRY, signed_params())
        self.assertRedirects(response, WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "linked-worker")
        self.assertTrue(response.cookies["wxpusher_browser"]["httponly"])
        self.subscription.assert_called_once_with(app_token=TOKEN, uid=UID, app_id=APP_ID)

    def test_old_pending_confirmation_cannot_be_used_for_scan_only_uid(self):
        user = self.bind()
        browser = make_logged_in_client(username="desktop-admin", is_superuser=True, games=None)
        page = browser.get(ENTRY, signed_params(), follow=True)
        self.assertContains(page, "确认切换")
        WxPusherBinding.objects.filter(user=user).update(uid=None, scan_bound_uid=UID)
        self.assertRedirects(self.confirm(browser, page), PASSWORD_ENTRY, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "desktop-admin")
        self.subscription.assert_not_called()

    def test_existing_session_never_switches_to_link_owner(self):
        self.bind()
        client = make_logged_in_client(username="already-logged-in", groups=(WORKER,), games=None)
        session_key = client.cookies["sessionid"].value
        self.assertRedirects(
            client.get(ENTRY, signed_params(), HTTP_REFERER=OFFICIAL), WORKER_ENTRY, fetch_redirect_response=False,
        )
        self.assertRedirects(client.get(ENTRY), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "already-logged-in")
        self.assertEqual(client.cookies["sessionid"].value, session_key)
        self.subscription.assert_not_called()

    def test_desktop_session_requires_csrf_confirmation_to_switch_even_in_remembered_browser(self):
        self.bind()
        client = self.browser(enforce_csrf_checks=True)
        self.assertRedirects(
            client.get(ENTRY, signed_params(nonce="remember-worker")), WORKER_ENTRY, fetch_redirect_response=False,
        )

        desktop_client = make_logged_in_client(username="desktop-admin", is_superuser=True, games=None)
        client.cookies["sessionid"] = desktop_client.cookies["sessionid"].value
        self.assertRedirects(
            client.get(ENTRY, {"uid": UID}), PASSWORD_ENTRY, fetch_redirect_response=False,
        )

        switch_page = client.get(ENTRY, signed_params(nonce="switch-explicitly"), follow=True)
        self.assertEqual(switch_page.redirect_chain, [(ENTRY, 302)])
        self.assertContains(switch_page, "确认切换")
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "desktop-admin")
        self.assertEqual(client.post(ENTRY, {"confirmation": switch_page.context["confirmation"]}).status_code, 403)
        self.subscription.assert_called_once()

        self.assertRedirects(self.confirm(client, switch_page), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "linked-worker")
        self.assertEqual(self.subscription.call_count, 2)

    def test_mere_binding_does_not_bypass_account_eligibility(self):
        inactive = self.bind(username="inactive-worker", uid="UID_inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        self.bind(username="non-worker", uid="UID_not_worker", roles=())

        for uid in ("UID_inactive", "UID_not_worker", "UID_unbound"):
            with self.subTest(uid=uid):
                client = self.browser()
                self.assert_password_fallback(client, client.get(ENTRY, signed_params(uid), HTTP_REFERER=OFFICIAL))

    def test_app_registered_worker_can_use_wxpusher_on_first_signed_callback(self):
        worker = self.bind(username="app-worker")
        StaffProfile.objects.create(user=worker, account_source=AccountSource.APP)
        WxPusherBinding.objects.filter(user=worker).update(uid=None, scan_bound_uid=UID)
        fresh = self.browser()
        self.assertRedirects(fresh.get(ENTRY, signed_params()), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(fresh.get(ME).json()["data"]["user"]["username"], "app-worker")

    def daidaitong_browser(self):
        Domain.objects.create(tenant=self.tenant, domain=DAIDAITONG_DOMAIN, is_primary=True)
        self.enterContext(override_settings(ALLOWED_HOSTS=[DAIDAITONG_DOMAIN]))
        return Client(SERVER_NAME=DAIDAITONG_DOMAIN, HTTP_USER_AGENT=ANDROID_WXPUSHER_UA, enforce_csrf_checks=True)

    def start_order(self, browser, order_no="12345678901234567890"):
        browser.get("/api/auth/csrf")
        return browser.post(
            f"/api/auth/wxpusher/orders/{order_no}/continue",
            HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value,
        )

    def test_chat_continue_returns_provider_url_without_signing_in(self):
        browser = self.browser(enforce_csrf_checks=True)
        response = browser.get("/api/auth/wxpusher/chat/continue")
        self.assertEqual(response.json()["data"], {
            "url": f"https://wxpusher.zjiecode.com/app/#/follow?type=1&id={APP_ID}&jump=auto",
        })
        self.assertEqual(browser.get(ME).status_code, 401)
        self.assertIn("no-store", response["Cache-Control"])

    def test_order_continuation_uses_app_id_without_subscription_url(self):
        browser = self.browser(enforce_csrf_checks=True)
        with override_settings(WXPUSHER_SUBSCRIBE_URL=""):
            response = self.start_order(browser)
        self.assertEqual(response.json(), {
            "code": 0, "msg": "", "data": {
                "url": "https://wxpusher.zjiecode.com/app/#/follow?type=1&id=8123&jump=auto",
            },
        })
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("sessionid", response.cookies)

    def test_order_continuation_waits_for_signed_callback_to_create_browser_session(self):
        self.bind()
        browser = self.browser(enforce_csrf_checks=True)
        self.assertEqual(self.start_order(browser).json()["code"], 0)
        self.assertEqual(browser.get(ME).status_code, 401)
        self.assertRedirects(browser.get(ENTRY, signed_params()), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "linked-worker")

    def test_order_continuation_requires_csrf_and_enabled_wxpusher(self):
        browser = self.browser(enforce_csrf_checks=True)
        self.assertEqual(browser.post("/api/auth/wxpusher/orders/12345678901234567890/continue").status_code, 403)
        with override_settings(WXPUSHER_LOGIN_SIGNING_KEY=""):
            response = self.start_order(browser)
        self.assertEqual(response.json()["code"], 1002)
        self.assertIsNone(response.json()["data"])

    def test_order_continuation_rejects_invalid_order_number(self):
        browser = self.browser(enforce_csrf_checks=True)
        self.assertEqual(self.start_order(browser, order_no="123%00").status_code, 404)
        response = self.start_order(browser, order_no="not-a-system-order")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 1000)
        self.assertIsNone(response.json()["data"])

    def test_android_extension_keeps_unsigned_and_invalid_links_on_web_login(self):
        worker = self.bind()
        WxPusherBinding.objects.filter(user=worker).update(uid=None, scan_bound_uid=UID)
        browser = self.daidaitong_browser()

        self.assert_password_fallback(browser, browser.get(ENTRY, {"uid": UID}))
        with override_settings(WXPUSHER_LOGIN_SIGNING_KEY=""):
            self.assert_password_fallback(browser, browser.get(ENTRY, signed_params()))
        self.assert_password_fallback(browser, browser.get(ENTRY, signed_params(key="wrong")))
        signed = signed_params()
        response = browser.get(ENTRY, signed, HTTP_SEC_FETCH_DEST="iframe")
        self.assert_password_fallback(browser, response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.subscription.assert_not_called()
        self.assertRedirects(browser.get(ENTRY, signed), WORKER_ENTRY, fetch_redirect_response=False)

    def test_android_extension_and_existing_session_stay_on_web(self):
        self.bind()
        browser = self.daidaitong_browser()
        self.assertRedirects(browser.get(ENTRY, signed_params()), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "linked-worker")
        self.assertRedirects(
            browser.get(ENTRY, signed_params(nonce="already-logged-in")), WORKER_ENTRY, fetch_redirect_response=False,
        )

    def test_android_order_continuation_logs_in_a_fresh_browser_and_stays_on_web(self):
        worker = self.bind()
        WxPusherBinding.objects.filter(user=worker).update(uid=None, scan_bound_uid=UID)
        browser = self.daidaitong_browser()

        self.assertEqual(self.start_order(browser).json()["code"], 0)
        callback = browser.get(ENTRY, signed_params(), HTTP_REFERER=OFFICIAL)
        self.assertRedirects(callback, WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], "linked-worker")
        self.subscription.assert_called_once_with(app_token=TOKEN, uid=UID, app_id=APP_ID)

    def test_link_for_tenant_a_cannot_log_in_on_tenant_b(self):
        self.bind(username="worker-in-a")
        ticket = signed_params()
        try:
            with tenant_context(self.other):
                make_logged_in_client(domain=OTHER_DOMAIN, username="different-worker-in-b", groups=(WORKER,), games=None)
                user = get_user_model().objects.get(username="different-worker-in-b")
                WxPusherBinding.objects.create(user=user, uid=UID, app_id=APP_ID)
                client = Client(SERVER_NAME=OTHER_DOMAIN)
                self.assert_password_fallback(client, client.get(ENTRY, ticket, HTTP_REFERER=OFFICIAL))
        finally:
            connection.set_tenant(self.tenant)

        client = self.browser()
        self.assertRedirects(client.get(ENTRY, ticket), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertEqual(client.get(ME).json()["data"]["user"]["username"], "worker-in-a")

    def hall_push_path(self, user, *, use_handoff=False):
        from apps.core.wxpusher_login import make_hall_push_path

        binding = WxPusherBinding.objects.get(user=user)
        notice = SimpleNamespace(details={"source": "local", "ref": "20261117000000000061"})
        return make_hall_push_path(notice, binding, use_handoff=use_handoff)

    def hall_entry_ticket(self, user):
        return parse_qs(urlsplit(self.hall_push_path(user, use_handoff=True)).fragment)["hall"][0]

    def test_hall_continuation_opens_official_login_without_trusting_the_push_link(self):
        user = self.bind()
        ticket = self.hall_entry_ticket(user)
        browser = self.browser(enforce_csrf_checks=True)
        endpoint = "/api/auth/wxpusher/hall/continue"
        self.assertEqual(browser.post(endpoint, {"hall": ticket}, content_type="application/json").status_code, 403)
        browser.get("/api/auth/csrf")
        csrf = browser.cookies["csrftoken"].value

        result = browser.post(endpoint, {"hall": ticket}, content_type="application/json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"], {"url": OFFICIAL + f"app/#/follow?type=1&id={APP_ID}&jump=auto"})
        self.assertEqual(browser.get(ME).status_code, 401)
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": ticket}), PASSWORD_ENTRY, fetch_redirect_response=False)
        self.assertRedirects(browser.get(ENTRY, {"hall": ticket}), HALL_UNAVAILABLE_PASSWORD, fetch_redirect_response=False)
        self.subscription.assert_not_called()

        old_ticket = parse_qs(urlsplit(self.hall_push_path(user)).query)["hall"][0]
        for invalid in ("", ticket + "altered", {"hall": ticket}, "a" * 1025, old_ticket):
            with self.subTest(invalid=type(invalid).__name__):
                response = browser.post(
                    endpoint, {"hall": invalid}, content_type="application/json", HTTP_X_CSRFTOKEN=csrf,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(response.json()["data"])

        with tenant_context(self.other):
            visitor = Client(SERVER_NAME=OTHER_DOMAIN)
            cross_tenant = visitor.post(endpoint, {"hall": ticket}, content_type="application/json")
            self.assertEqual(cross_tenant.status_code, 400)
            self.assertEqual(visitor.get(ME).status_code, 401)

    def test_hall_start_works_without_browser_cookies_and_leaves_login_to_official_callback(self):
        user = self.bind()
        ticket = self.hall_entry_ticket(user)
        browser = self.browser(enforce_csrf_checks=True)

        result = browser.get(ENTRY, {"hallStart": ticket})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"], {"url": OFFICIAL + f"app/#/follow?type=1&id={APP_ID}&jump=auto"})
        self.assertIn("no-store", result["Cache-Control"])
        self.assertEqual(result["Referrer-Policy"], "no-referrer")
        self.assertFalse(browser.cookies)
        self.assert_password_fallback(browser, browser.get(ENTRY, {"hallEntry": ticket}))
        self.subscription.assert_not_called()

        self.assertRedirects(browser.get(ENTRY, signed_params()), WORKER_ENTRY, fetch_redirect_response=False)
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": ticket}), HALL_CLAIM, fetch_redirect_response=False)

    def test_hall_start_rejects_changed_binding_other_tenants_and_mixed_parameters(self):
        user = self.bind()
        ticket = self.hall_entry_ticket(user)
        browser = self.browser(enforce_csrf_checks=True)
        old_ticket = parse_qs(urlsplit(self.hall_push_path(user)).query)["hall"][0]
        for params in (
            {"hallStart": ticket + "altered"},
            {"hallStart": "a" * 1025},
            {"hallStart": old_ticket},
            {"hallStart": [ticket, ticket]},
            signed_params() | {"hallStart": ticket},
            {"hallStart": ticket, "hallEntry": ticket},
        ):
            with self.subTest(params=list(params)):
                response = browser.get(ENTRY, params)
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(response.json()["data"])

        with tenant_context(self.other):
            visitor = Client(SERVER_NAME=OTHER_DOMAIN)
            self.assertEqual(visitor.get(ENTRY, {"hallStart": ticket}).status_code, 400)
            self.assertEqual(visitor.get(ME).status_code, 401)

        other = self.bind(username="hall-start-other", uid="UID_other_hall_start")
        logged_in = self.browser()
        logged_in.post("/api/auth/login", {"username": other.username, "password": P2_TEST_PASSWORD})
        self.assertEqual(logged_in.get(ENTRY, {"hallStart": ticket}).status_code, 400)
        self.assertEqual(logged_in.get(ME).json()["data"]["user"]["username"], other.username)

        WxPusherBinding.objects.filter(user=user).update(last_start_at=timezone.now())
        self.assertEqual(browser.get(ENTRY, {"hallStart": ticket}).status_code, 400)
        self.assertEqual(browser.get(ME).status_code, 401)
        self.subscription.assert_not_called()

    def test_hall_continuation_requires_the_current_binding_to_match_the_ticket(self):
        user = self.bind()
        ticket = self.hall_entry_ticket(user)
        browser = self.browser()
        endpoint = "/api/auth/wxpusher/hall/continue"

        other = self.bind(username="other-hall-worker", uid="UID_synthetic_other")
        browser.post("/api/auth/login", {"username": other.username, "password": P2_TEST_PASSWORD})
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], other.username)
        self.assertEqual(browser.post(endpoint, {"hall": ticket}, content_type="application/json").status_code, 400)
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": ticket}), HALL_UNAVAILABLE, fetch_redirect_response=False)
        browser.logout()

        browser.post("/api/auth/login", {"username": user.username, "password": P2_TEST_PASSWORD})
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": ticket}), HALL_CLAIM, fetch_redirect_response=False)
        old_ticket = parse_qs(urlsplit(self.hall_push_path(user)).query)["hall"][0]
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": old_ticket}), HALL_UNAVAILABLE, fetch_redirect_response=False)
        with override_settings(WXPUSHER_APP_ID="9182"):
            self.assertRedirects(
                browser.get(ENTRY, {"hallEntry": ticket}), HALL_UNAVAILABLE, fetch_redirect_response=False,
            )
        WxPusherBinding.objects.filter(user=user).update(last_start_at=timezone.now())
        self.assertEqual(browser.post(endpoint, {"hall": ticket}, content_type="application/json").status_code, 400)
        self.assertRedirects(browser.get(ENTRY, {"hallEntry": ticket}), HALL_UNAVAILABLE, fetch_redirect_response=False)
        self.assertEqual(browser.get(ME).json()["data"]["user"]["username"], user.username)

    def test_hall_push_link_preserves_current_worker_and_points_at_exact_claim(self):
        user = self.bind()
        browser = self.browser()
        browser.post("/api/auth/login", {"username": user.username, "password": P2_TEST_PASSWORD})
        self.assertRedirects(browser.get(self.hall_push_path(user)), HALL_CLAIM, fetch_redirect_response=False)
        self.subscription.assert_not_called()

    def test_hall_push_link_restores_only_the_trusted_browser(self):
        user = self.bind()
        WxPusherBinding.objects.filter(user=user).update(uid=None, scan_bound_uid=UID)
        path = self.hall_push_path(user)
        stranger = self.browser()
        self.assertRedirects(stranger.get(path), HALL_PASSWORD, fetch_redirect_response=False)
        self.assertEqual(stranger.get(ME).status_code, 401)

        trusted = self.browser()
        trusted.post("/api/auth/login", {"username": user.username, "password": P2_TEST_PASSWORD})
        self.assertIn("wxpusher_browser", trusted.cookies)
        trusted.cookies.pop("sessionid")
        self.assertRedirects(trusted.get(path), HALL_CLAIM, fetch_redirect_response=False)
        self.assertEqual(trusted.get(ME).json()["data"]["user"]["username"], user.username)
        self.subscription.assert_called_once_with(app_token=TOKEN, uid=UID, app_id=APP_ID)

        trusted.post("/api/auth/logout")
        self.assertRedirects(trusted.get(path), HALL_PASSWORD, fetch_redirect_response=False)
        self.assertEqual(trusted.get(ME).status_code, 401)

    def test_expired_tampered_or_previous_binding_links_cannot_log_in(self):
        user = self.bind()
        trusted = self.browser()
        trusted.post("/api/auth/login", {"username": user.username, "password": P2_TEST_PASSWORD})
        trusted.cookies.pop("sessionid")
        now = timezone.now()
        with patch("apps.core.wxpusher_login.timezone.now", return_value=now - timedelta(minutes=30)):
            expired = self.hall_push_path(user)
        active = self.hall_push_path(user)
        self.assertRedirects(trusted.get(expired), HALL_PASSWORD, fetch_redirect_response=False)
        self.assertEqual(trusted.get(ME).status_code, 401)
        self.assertRedirects(trusted.get(active + "tampered"), HALL_UNAVAILABLE_PASSWORD,
                             fetch_redirect_response=False)
        with patch("django.core.signing.time.time", return_value=(now - timedelta(days=8)).timestamp()):
            too_old = self.hall_push_path(user)
        self.assertRedirects(trusted.get(too_old), HALL_UNAVAILABLE_PASSWORD, fetch_redirect_response=False)

        logged_in = self.browser()
        logged_in.post("/api/auth/login", {"username": user.username, "password": P2_TEST_PASSWORD})
        self.assertRedirects(logged_in.get(expired), HALL_CLAIM, fetch_redirect_response=False)
        self.assertRedirects(logged_in.get(too_old), HALL_UNAVAILABLE, fetch_redirect_response=False)
        self.assertRedirects(logged_in.get(active + "&hall=duplicate"), HALL_UNAVAILABLE,
                             fetch_redirect_response=False)

        WxPusherBinding.objects.filter(user=user).update(last_start_at=now)
        self.assertRedirects(trusted.get(active), HALL_PASSWORD, fetch_redirect_response=False)
        self.assertEqual(trusted.get(ME).status_code, 401)
        self.subscription.assert_not_called()

    def test_hall_push_link_is_bound_to_the_issuing_workstudio(self):
        alice = self.bind(username="worker-in-a")
        path = self.hall_push_path(alice)
        with tenant_context(self.other):
            with override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.other.schema_name):
                client = make_logged_in_client(
                    domain=OTHER_DOMAIN, username="other-worker-in-b", groups=(WORKER,), games=None,
                )
                bob = get_user_model().objects.get(username="other-worker-in-b")
                WxPusherBinding.objects.create(user=bob, uid=UID, app_id=APP_ID)
                self.assertRedirects(client.get(path), HALL_UNAVAILABLE, fetch_redirect_response=False)
                self.assertEqual(client.get(ME).json()["data"]["user"]["username"], bob.username)
                visitor = Client(SERVER_NAME=OTHER_DOMAIN)
                self.assertRedirects(visitor.get(path), HALL_UNAVAILABLE_PASSWORD, fetch_redirect_response=False)
                self.assertEqual(visitor.get(ME).status_code, 401)
        self.subscription.assert_not_called()

    def test_worker_using_tenant_a_cannot_load_tenant_b_hall_target(self):
        from decimal import Decimal

        from apps.business.models import EmpType, Game, Order, OrderStatus, Performance

        target_url = "/api/business/hall-claim-target/"
        ref = "20261117000000000082"
        browser_a = make_logged_in_client(groups=(WORKER,), nodes=("grab_hall.take_order",))
        with tenant_context(self.other):
            game = Game.objects.create(name="合成乙工作室游戏")
            order = Order.objects.create(
                order_no=ref, status=OrderStatus.WAITING_GRAB, game=game, hall_price=Decimal("85.00"),
            )
            Performance.objects.create(order=order, emp_type=EmpType.MAIN)
            browser_b = make_logged_in_client(
                domain=OTHER_DOMAIN, username="can-read-b", groups=(WORKER,),
                games=[game], nodes=("grab_hall.take_order",),
            )
            result = browser_b.get(target_url, {"source": "local", "ref": ref}).json()
            self.assertEqual(result["data"]["order_no"], ref)
        result = browser_a.get(target_url, {"source": "local", "ref": ref}).json()
        self.assertEqual(result["code"], 1012)
        self.assertIsNone(result["data"])
