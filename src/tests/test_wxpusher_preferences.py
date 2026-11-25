from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import AppToken
from apps.business.peer_sync import hash_token
from apps.business.roles import WORKER
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

PATH = "/api/auth/wxpusher/preferences"
OTHER_DOMAIN = "push-pref-other.test.local"
ALL_ORDER_EVENTS = {
    "assigned": True, "unassigned": True, "review_rejected": True, "completed": True,
    "paused_resumed": True, "issue": True, "refunded": True,
}


class WxPusherPreferencesTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(schema_name="push_pref_other", name="示例乙", domain=OTHER_DOMAIN)

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name))
        self.worker = make_logged_in_client(username="push-worker", groups=(WORKER,), nodes=("my_profile",))

    def _patch(self, body):
        return self.worker.patch(PATH, body, content_type="application/json")

    def test_default_on_and_switch_preserves_selected_items(self):
        initial = self.worker.get(PATH).json()
        self.assertEqual(initial["code"], 0)
        self.assertEqual(initial["data"], {
            "order_enabled": True, "settlement_enabled": True, "group_chat_enabled": True, "order_events": ALL_ORDER_EVENTS,
        })
        self.assertEqual(self._patch({"order_events": {"refunded": False}}).json()["code"], 0)
        self.assertEqual(self._patch({"order_enabled": False, "settlement_enabled": False}).json()["data"], {
            "order_enabled": False, "settlement_enabled": False, "group_chat_enabled": True,
            "order_events": {**ALL_ORDER_EVENTS, "refunded": False},
        })
        self.assertEqual(self._patch({"order_enabled": True}).json()["code"], 0)
        self.assertEqual(self.worker.get(PATH).json()["data"], {
            "order_enabled": True, "settlement_enabled": False, "group_chat_enabled": True,
            "order_events": {**ALL_ORDER_EVENTS, "refunded": False},
        })

    def test_invalid_type_and_unrecognized_keys_do_not_save(self):
        for invalid in (
            {"order_enabled": 0}, {"order_enabled": "false"}, {"settlement_enabled": None}, {"group_chat_enabled": 1},
            {"order_events": []}, {"order_events": {"settled": False}},
            {"order_events": {"refunded": 0}}, {"user_id": 2},
        ):
            with self.subTest(invalid=invalid):
                self.assertNotEqual(self._patch(invalid).json()["code"], 0)
        self.assertEqual(self.worker.get(PATH).json()["data"]["order_events"], ALL_ORDER_EVENTS)

    def test_app_worker_updates_the_same_preferences_as_h5_without_resetting_other_settings(self):
        second = make_logged_in_client(username="other-app-pref-worker", groups=(WORKER,), nodes=("my_profile",))
        token = "synthetic-app-preference-bearer"
        AppToken.objects.create(
            user=get_user_model().objects.get(username="push-worker"),
            key_hash=hash_token(token), expires_at=timezone.now() + timedelta(hours=1),
        )
        app = Client(
            SERVER_NAME=self.get_test_tenant_domain(), HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        initial = {
            "order_enabled": True, "settlement_enabled": False, "group_chat_enabled": True,
            "order_events": {**ALL_ORDER_EVENTS, "refunded": False},
        }
        saved = self._patch({"settlement_enabled": False, "order_events": {"refunded": False}}).json()
        self.assertEqual(saved["data"], initial)
        native_settings = app.get(PATH)
        self.assertEqual(native_settings.status_code, 200)
        self.assertEqual(native_settings.json()["data"], initial)

        updated = app.patch(
            PATH, {"order_events": {"assigned": False}, "group_chat_enabled": False},
            content_type="application/json",
        )
        self.assertEqual(updated.status_code, 200)
        expected = {**initial, "order_events": {**initial["order_events"], "assigned": False}}
        self.assertEqual(updated.json()["data"], expected)
        self.assertEqual(self.worker.get(PATH).json()["data"], expected)
        self.assertEqual(second.get(PATH).json()["data"], {
            "order_enabled": True, "settlement_enabled": True, "group_chat_enabled": True, "order_events": ALL_ORDER_EVENTS,
        })

        with tenant_context(self.other):
            self.assertEqual(app.get(PATH, SERVER_NAME=OTHER_DOMAIN).status_code, 401)
            rejected = app.patch(
                PATH, {"order_events": {"assigned": False}},
                content_type="application/json", SERVER_NAME=OTHER_DOMAIN,
            )
            self.assertEqual(rejected.status_code, 401)

    def test_requires_a_signed_in_worker(self):
        response = Client().get(PATH, SERVER_NAME=self.tenant.get_primary_domain().domain)
        self.assertEqual(response.status_code, 401)
        non_worker = make_logged_in_client(username="not-a-worker", nodes=("my_profile",))
        self.assertEqual(non_worker.get(PATH).status_code, 403)

    def test_each_tenant_and_worker_has_own_settings(self):
        second = make_logged_in_client(username="second-worker", groups=(WORKER,), nodes=("my_profile",))
        self.assertEqual(self._patch({"settlement_enabled": False}).json()["code"], 0)
        self.assertTrue(second.get(PATH).json()["data"]["settlement_enabled"])
        with tenant_context(self.other):
            other_worker = make_logged_in_client(
                domain=OTHER_DOMAIN, username="other-tenant-worker", groups=(WORKER,), nodes=("my_profile",),
            )
            self.assertEqual(self.worker.get(PATH, SERVER_NAME=OTHER_DOMAIN).status_code, 401)
            self.assertNotEqual(other_worker.get(PATH).json()["code"], 0)
            with override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.other.schema_name):
                self.assertTrue(other_worker.get(PATH).json()["data"]["settlement_enabled"])
