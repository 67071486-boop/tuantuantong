import hashlib
import hmac
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import WxPusherBinding
from apps.audit.models import OperationLog
from apps.business.roles import SERVICE, WORKER
from apps.platform.provisioning import provision_tenant
from apps.core.wxpusher import WxPusherError
from apps.rbac.models import PermissionNode, Role
from apps.rbac.permissions import has_node_now
from tests.p5_auth_helpers import P2_TEST_PASSWORD, grant_nodes, make_logged_in_client

GUIDE = "/api/auth/wxpusher/guide"
BIND = "/api/auth/wxpusher/binding"
CALLBACK = f"{BIND}/callback"
LINK = "https://wxpusher.zjiecode.com/api/s/ProviderIssuedLink"
SIGNING_KEY = "fixture-only-link-signing-key"
APP_ID = "8123"


class WxPusherLinkBindingTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_APP_TOKEN="fixture-only-link-app-token",
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_LOGIN_SIGNING_KEY=SIGNING_KEY,
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
        ))
        self.worker = self.login("link-worker")
        self.attempt_id = ""
        self.callback_client = Client(SERVER_NAME=self.get_test_tenant_domain(), enforce_csrf_checks=True)
        provider = self.enterContext(patch("apps.accounts.wxpusher_binding.WxPusherClient"))
        self.remote = provider.return_value.__enter__.return_value
        self.remote.create_qrcode.return_value = SimpleNamespace(
            code="fixture-code", url="https://wxpusher.zjiecode.com/api/qrcode/Fixture.jpg", link=LINK,
        )
        self.remote.is_subscribed.return_value = True

    def login(self, username, group=WORKER):
        return make_logged_in_client(username=username, groups=(group,), nodes=("my_profile",), games=None)

    def binding(self, username="link-worker"):
        return WxPusherBinding.objects.get(user__username=username, app_id=APP_ID)

    def post(self, action, *, browser=None, **kwargs):
        fields = {"mode": "link", **kwargs}
        if action != "start":
            fields.setdefault("attempt_id", self.attempt_id)
        response = (browser or self.worker).post(f"{BIND}/{action}", fields, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def start(self):
        response = self.post("start")
        self.assertEqual(response["code"], 0, response)
        self.attempt_id = response["data"]["attempt_id"]
        return response["data"], self.remote.create_qrcode.call_args.kwargs["extra"]

    def callback(self, extra, uid="UID_linknew", **payload):
        response = self.callback_client.post(CALLBACK, {
            "action": "app_subscribe",
            "data": {"extra": extra, "appId": int(APP_ID), "uid": uid, "source": "link", **payload},
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_link_starts_without_exposing_provider_proof_and_updates_after_callback(self):
        result, extra = self.start()
        self.assertEqual(set(result), {"mode", "attempt_id", "subscribe_url", "expires_at"})
        self.assertEqual(result["mode"], "link")
        self.assertEqual(result["subscribe_url"], LINK)
        self.assertNotIn(extra, str(result))
        guide = self.worker.get(GUIDE).json()["data"]
        self.assertIs(guide["binding_link_supported"], True)
        self.assertEqual(guide["pending_binding"], result)
        self.assertEqual(self.post("progress", uid="UID_from_browser")["data"], {"bound": False})
        self.assertIsNone(self.binding().effective_uid)
        self.callback(extra)
        self.assertIsNone(self.binding().effective_uid)
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        self.assertEqual(self.binding().scan_bound_uid, "UID_linknew")
        self.assertIsNone(self.binding().uid)
        self.assertIsNone(self.worker.get(GUIDE).json()["data"]["pending_binding"])
        self.remote.is_subscribed.assert_called_once_with(
            app_token="fixture-only-link-app-token", uid="UID_linknew", app_id=APP_ID,
        )
        self.remote.send.assert_not_called()

    def test_verified_link_replaces_own_existing_uid_only_after_success(self):
        generation = timezone.now() - timedelta(days=2)
        WxPusherBinding.objects.create(
            user=get_user_model().objects.get(username="link-worker"), app_id=APP_ID,
            uid="UID_linkold", last_start_at=generation,
        )
        result, extra = self.start()
        self.assertEqual(self.worker.get(GUIDE).json()["data"]["pending_binding"], result)
        self.assertEqual(self.post("progress")["data"], {"bound": False})
        self.assertEqual(self.binding().last_start_at, generation)
        self.assertEqual(self.binding().uid, "UID_linkold")
        self.assertEqual(self.post("test")["data"], {"accepted": True})
        self.assertEqual(self.remote.send.call_args.kwargs["uid"], "UID_linkold")
        self.callback(extra)
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        self.assertEqual(self.binding().scan_bound_uid, "UID_linknew")
        self.assertIsNone(self.binding().uid)
        self.assertGreater(self.binding().last_start_at, generation)

    def test_verified_uid_moves_between_workers_and_is_not_replayed(self):
        donor = self.login("old-link-worker")
        WxPusherBinding.objects.create(
            user=get_user_model().objects.get(username="old-link-worker"), app_id=APP_ID, uid="UID_linknew",
        )
        _, extra = self.start()
        self.assertTrue(donor.get(GUIDE).json()["data"]["bound"])
        self.callback(extra)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.post("progress")["data"], {"bound": True})
        actor = get_user_model().objects.get(username="link-worker")
        old_account = get_user_model().objects.get(username="old-link-worker")
        account_locks = [
            match.groups() for query in captured.captured_queries
            if (match := re.search(r'WHERE "auth_user"\."id" IN \((\d+), (\d+)\) ORDER BY "auth_user"\."id" ASC FOR UPDATE',
                                   query["sql"]))
        ]
        self.assertEqual([set(map(int, ids)) for ids in account_locks], [{actor.pk, old_account.pk}])
        self.assertFalse(donor.get(GUIDE).json()["data"]["bound"])
        self.callback(extra, uid="UID_other")
        self.assertNotEqual(self.post("progress")["code"], 0)
        self.assertEqual(self.binding().effective_uid, "UID_linknew")
        audit = OperationLog.objects.get(action="user.update", field_name="wxpusher_binding")
        self.assertEqual((audit.actor_id, audit.target_id, audit.reason), (actor.pk, str(old_account.pk), "WxPusher 通知换绑"))
        self.assertNotIn("UID_linknew", str(vars(audit)))
        self.assertNotIn(extra, str(vars(audit)))

    def test_can_transfer_from_a_disabled_worker_without_signing_in_as_them(self):
        self.login("disabled-link-worker")
        donor = get_user_model().objects.get(username="disabled-link-worker")
        actor = get_user_model().objects.get(username="link-worker")
        actor.groups.add(Group.objects.create(name="可改资料"))
        grant_nodes(actor, "my_profile")
        Role.objects.get(name=WORKER).permissions.remove(PermissionNode.objects.get(code="my_profile"))
        self.assertFalse(has_node_now(donor, "my_profile"))
        donor.is_active = False
        donor.save(update_fields=["is_active"])
        WxPusherBinding.objects.create(user=donor, app_id=APP_ID, scan_bound_uid="UID_linknew")
        _, extra = self.start()
        self.callback(extra)
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        self.assertIsNone(self.binding("disabled-link-worker").effective_uid)
        self.assertEqual(self.binding().scan_bound_uid, "UID_linknew")

    def test_wrong_missing_and_canceled_proof_leave_old_binding(self):
        _, extra = self.start()
        WxPusherBinding.objects.filter(pk=self.binding().pk).update(uid="UID_linkold")
        self.callback("")
        self.callback(extra, appId=6789)
        self.callback(extra + "modified")
        self.assertEqual(self.post("progress")["data"], {"bound": False})
        self.remote.is_subscribed.assert_not_called()
        self.callback(extra)
        self.assertEqual(self.post("cancel")["data"], {"canceled": True})
        self.callback(extra)
        self.assertNotEqual(self.post("progress")["code"], 0)
        self.assertEqual(self.binding().effective_uid, "UID_linkold")

    def test_unrelated_signed_login_return_does_not_supply_binding_proof(self):
        self.start()
        ts = str(int(timezone.now().timestamp() * 1000))
        nonce = "fixture_unrelated_entry"
        uid = "UID_unrelated"
        canonical = f"nonce={nonce}&sv=1&ts={ts}&uid={uid}".encode()
        sign = hmac.new(SIGNING_KEY.encode(), canonical, hashlib.sha256).hexdigest()
        self.worker.get("/api/auth/wxpusher/login", {"ts": ts, "nonce": nonce, "uid": uid, "sv": "1", "sign": sign})
        self.assertEqual(self.post("progress")["data"], {"bound": False})
        self.assertIsNone(self.binding().effective_uid)
        self.remote.is_subscribed.assert_not_called()

    @override_settings(WXPUSHER_LOGIN_SIGNING_KEY="")
    def test_code_fallback_transfers_an_already_subscribed_worker_without_a_signed_return(self):
        self.login("previous-code-owner")
        donor = get_user_model().objects.get(username="previous-code-owner")
        WxPusherBinding.objects.create(user=donor, app_id=APP_ID, uid="UID_existing_subscriber")
        target = get_user_model().objects.get(username="link-worker")
        WxPusherBinding.objects.create(user=target, app_id=APP_ID, uid="UID_kept_while_waiting")
        _, extra = self.start()
        with patch("apps.accounts.wxpusher_binding.secrets.randbelow", return_value=420):
            requested = self.post("link-code", uid="UID_existing_subscriber")
        self.assertEqual(requested["code"], 0, requested)
        self.assertTrue(requested["data"]["sent"])
        self.assertNotIn("000420", str(requested))
        self.remote.is_subscribed.assert_called_once_with(
            app_token="fixture-only-link-app-token", uid="UID_existing_subscriber", app_id=APP_ID,
        )
        self.assertEqual(self.remote.send.call_args.kwargs["uid"], "UID_existing_subscriber")
        self.assertIn("000420", self.remote.send.call_args.kwargs["content"])
        self.callback(extra, uid="UID_unintended_delayed_callback")
        self.assertEqual(self.post("progress")["data"], {"bound": False})
        self.assertEqual(self.binding().effective_uid, "UID_kept_while_waiting")
        self.assertNotEqual(self.post("link-confirm", code="000421")["code"], 0)
        self.assertEqual(self.binding("previous-code-owner").uid, "UID_existing_subscriber")
        self.assertEqual(self.post("link-confirm", code="000420")["data"], {"bound": True})
        self.assertEqual(self.binding().scan_bound_uid, "UID_existing_subscriber")
        self.assertIsNone(self.binding("previous-code-owner").effective_uid)
        self.assertNotEqual(self.post("link-confirm", code="000420")["code"], 0)
        audit = OperationLog.objects.get(action="user.update", field_name="wxpusher_binding")
        self.assertEqual((audit.actor_id, audit.target_id), (target.pk, str(donor.pk)))
        self.assertNotIn("000420", str(vars(audit)))
        self.assertNotIn("UID_existing_subscriber", str(vars(audit)))

    def test_code_throttle_survives_cancel_and_prevents_multiple_workers_messaging_one_uid(self):
        _, _ = self.start()
        now = timezone.now()
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            self.assertEqual(self.post("link-code", uid="UID_already_subscribed")["code"], 0)
            self.assertEqual(self.post("cancel")["data"], {"canceled": True})
            self.assertNotEqual(self.post("start")["code"], 0)
            clock.return_value = now + timedelta(seconds=16)
            self.start()
            self.assertNotEqual(self.post("link-code", uid="UID_different_subscriber")["code"], 0)
            second_worker = self.login("second-code-worker")
            second = self.post("start", browser=second_worker)["data"]["attempt_id"]
            self.assertNotEqual(self.post("link-code", browser=second_worker, attempt_id=second,
                                          uid="UID_already_subscribed")["code"], 0)
            self.remote.is_subscribed.assert_called_once()
            self.remote.send.assert_called_once()

    def test_unsubscribed_typo_leaves_original_link_available_for_callback(self):
        _, extra = self.start()
        self.remote.is_subscribed.side_effect = [False, True]
        self.assertNotEqual(self.post("link-code", uid="UID_typo_unsubscribed")["code"], 0)
        self.remote.send.assert_not_called()
        self.callback(extra, uid="UID_real_subscriber")
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        self.assertEqual(self.binding().effective_uid, "UID_real_subscriber")

    def test_wrong_codes_lock_the_attempt_without_replacing_old_receiver(self):
        self.start()
        WxPusherBinding.objects.filter(pk=self.binding().pk).update(uid="UID_original")
        with patch("apps.accounts.wxpusher_binding.secrets.randbelow", return_value=42):
            self.assertEqual(self.post("link-code", uid="UID_already_subscribed")["code"], 0)
        for _ in range(5):
            self.assertNotEqual(self.post("link-confirm", code="000043")["code"], 0)
        self.assertNotEqual(self.post("link-confirm", code="000042")["code"], 0)
        self.assertEqual(self.binding().effective_uid, "UID_original")

    def test_failed_provider_verification_or_staff_recipient_preserves_current_bindings(self):
        self.login("staff-link-owner", SERVICE)
        WxPusherBinding.objects.create(
            user=get_user_model().objects.get(username="staff-link-owner"), app_id=APP_ID, uid="UID_linknew",
        )
        _, extra = self.start()
        now = timezone.now()
        self.callback(extra)
        self.remote.is_subscribed.side_effect = [WxPusherError("retry"), True]
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now) as clock:
            self.assertEqual(self.post("progress")["data"], {"bound": False})
            clock.return_value = now + timedelta(seconds=10)
            self.assertNotEqual(self.post("progress")["code"], 0)
        self.assertEqual(self.binding("staff-link-owner").effective_uid, "UID_linknew")
        self.assertIsNone(self.binding().effective_uid)

    def test_other_session_cannot_complete_link_and_invalid_url_does_not_start_it(self):
        self.remote.create_qrcode.return_value.link = LINK + "?extra=should-not-be-public"
        self.assertNotEqual(self.post("start")["code"], 0)
        self.assertIsNone(self.worker.get(GUIDE).json()["data"]["pending_binding"])
        now = timezone.now() + timedelta(seconds=16)
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now):
            self.remote.create_qrcode.return_value.link = LINK
            _, extra = self.start()
            self.callback(extra)
            other = Client(SERVER_NAME=self.get_test_tenant_domain())
            other.post("/api/auth/login", {
                "username": "link-worker", "password": P2_TEST_PASSWORD,
            }, content_type="application/json")
            self.assertNotEqual(self.post("progress", browser=other)["code"], 0)
            self.assertIsNone(self.binding().effective_uid)
            self.assertEqual(self.post("progress")["data"], {"bound": True})

    def test_older_tab_cannot_cancel_or_finish_new_link_and_completed_binding_cannot_be_canceled(self):
        first, first_extra = self.start()
        now = timezone.now() + timedelta(seconds=16)
        with patch("apps.accounts.wxpusher_binding.timezone.now", return_value=now):
            second, second_extra = self.start()
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])
            self.callback(first_extra, uid="UID_oldlink")
            self.callback(second_extra)
            self.assertNotEqual(self.post("progress", attempt_id=first["attempt_id"])["code"], 0)
            self.assertNotEqual(self.post("cancel", attempt_id=first["attempt_id"])["code"], 0)
            self.assertEqual(self.worker.get(GUIDE).json()["data"]["pending_binding"], second)
            self.assertEqual(self.post("progress")["data"], {"bound": True})
            self.assertNotEqual(self.post("cancel")["code"], 0)
        self.assertEqual(self.binding().effective_uid, "UID_linknew")

    def test_callback_cannot_link_a_different_studio_even_with_the_same_app_id(self):
        _, sender_extra = self.start()
        another, _ = provision_tenant(
            schema_name="wx_link_other", name="另一间工作室", domain="wx-link-other.test.local",
        )
        try:
            with tenant_context(another), override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=another.schema_name):
                receiver = make_logged_in_client(
                    domain="wx-link-other.test.local", username="other-link-worker",
                    groups=(WORKER,), nodes=("my_profile",), games=None,
                )
                attempt = self.post("start", browser=receiver)["data"]
                receiver_extra = self.remote.create_qrcode.call_args.kwargs["extra"]
                anonymous = Client(SERVER_NAME="wx-link-other.test.local", enforce_csrf_checks=True)
                payload = {"action": "app_subscribe", "data": {
                    "extra": sender_extra, "appId": int(APP_ID), "uid": "UID_other_studio_forged",
                }}
                self.assertEqual(anonymous.post(CALLBACK, payload, content_type="application/json").status_code, 200)
                self.assertEqual(self.post("progress", browser=receiver, attempt_id=attempt["attempt_id"])["data"],
                                 {"bound": False})
                payload["data"].update(extra=receiver_extra, uid="UID_other_studio_real")
                self.assertEqual(anonymous.post(CALLBACK, payload, content_type="application/json").status_code, 200)
                self.assertEqual(self.post("progress", browser=receiver, attempt_id=attempt["attempt_id"])["data"],
                                 {"bound": True})
                self.assertEqual(self.binding("other-link-worker").effective_uid, "UID_other_studio_real")
        finally:
            connection.set_tenant(self.tenant)
        self.assertIsNone(self.binding().effective_uid)
        self.callback(sender_extra)
        self.assertEqual(self.post("progress")["data"], {"bound": True})
        self.assertEqual(self.binding().effective_uid, "UID_linknew")
