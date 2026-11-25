from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.business.models import EmpType, Game, Order, Performance
from apps.business.roles import TENANT_ADMIN, WORKER
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

INBOX = "/api/auth/wxpusher/inbox"
ADMIN = "/api/business/wxpusher-config/"
ORDER_NO = "20261117010000000086"
DISPLAY_NO = "INBOX-WX-0086"


@override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA="")
class WxPusherInboxTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_logged_in_client(username="inbox-admin", groups=(TENANT_ADMIN,), games=None)
        self.worker = make_logged_in_client(username="inbox-worker", groups=(WORKER,), nodes=("my_profile",), games=None)
        self.other_worker = make_logged_in_client(
            username="inbox-other", groups=(WORKER,), nodes=("my_profile",), games=None,
        )
        self.worker_account = get_user_model().objects.get(username="inbox-worker")

    def _enqueue(self, reference, kind="assigned"):
        enqueue_worker_notice(
            user_id=self.worker_account.pk, kind=kind, reference=reference,
            order_no=ORDER_NO, details={"display_order_no": DISPLAY_NO, "project": "合成代练项目"},
        )

    def _configured_studio(self):
        response = self.admin.patch(ADMIN, {
            "app_id": "900021", "app_token": "AT_synthetic_inbox", "push_enabled": True,
        }, content_type="application/json")
        self.assertEqual(response.json()["code"], 0, response.content)

    def _data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def test_worker_can_read_a_notice_created_without_a_provider_configuration(self):
        self._enqueue("new")
        self._enqueue("new")

        inbox = self._data(self.worker.get(INBOX))
        self.assertEqual(inbox["unread_count"], 1)
        self.assertEqual(inbox["total"], 1)
        item, = inbox["items"]
        self.assertEqual(item["title"], "收到新的订单指派")
        self.assertEqual(item["order_no"], DISPLAY_NO)
        self.assertIn("代练内容　合成代练项目", item["lines"])
        self.assertIsNone(item["read_at"])
        self.assertEqual(self._data(self.other_worker.get(INBOX))["items"], [])
        denied = self.other_worker.post(f"{INBOX}/{item['id']}/read").json()
        self.assertEqual(denied["code"], ErrorCode.BUSINESS)
        self.assertIsNone(denied["data"])
        first = self._data(self.worker.post(f"{INBOX}/{item['id']}/read"))
        self.assertEqual(self._data(self.worker.post(f"{INBOX}/{item['id']}/read")), first)
        self.assertEqual(self._data(self.worker.get(INBOX))["unread_count"], 0)

    def test_history_shows_order_number_and_reason_for_unbound_and_opted_out_workers(self):
        self._configured_studio()
        self._enqueue("without-binding")
        response = self.worker.patch(
            "/api/auth/wxpusher/preferences", {"order_enabled": False}, content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        self._enqueue("opt-out")
        self._enqueue("transfer", kind="transfer_succeeded")
        order_items = self._data(self.worker.get(INBOX, {"category": "order"}))
        self.assertEqual(order_items["total"], 2)
        self.assertEqual(order_items["unread_count"], 3)
        self.assertEqual(self._data(self.worker.get(INBOX, {"category": "settlement"}))["total"], 1)
        for number in (ORDER_NO, DISPLAY_NO):
            history = self._data(self.admin.get(ADMIN + "history/", {"order_no": number, "status": "skipped"}))
            self.assertEqual(history["total"], 3)
            self.assertEqual([i["skip_reason"] for i in history["items"]], [
                "not_bound", "personal_disabled", "not_bound",
            ])
            self.assertEqual({i["order_no"] for i in history["items"]}, {DISPLAY_NO})

    def test_history_resolves_missing_display_number_only_for_legacy_rows(self):
        game = Game.objects.create(name="示例游戏")
        Order.objects.create(order_no=ORDER_NO, source_order_no=DISPLAY_NO, game=game)
        legacy = [
            WxPusherNotice.objects.create(
                user=self.worker_account, kind="assigned", reference=reference,
                order_no=ORDER_NO, details=details, status="sent",
            )
            for reference, details in (
                ("missing-display-number", {}),
                ("blank-display-number", {"display_order_no": ""}),
                ("null-display-number", {"display_order_no": None}),
            )
        ]
        snapshot = WxPusherNotice.objects.create(
            user=self.worker_account, kind="assigned", reference="stored-display-number",
            order_no=ORDER_NO, details={"display_order_no": "SAVED-NO"}, status="sent",
        )

        def matches(number):
            rows = self._data(self.admin.get(ADMIN + "history/", {"order_no": number}))
            return {row["id"] for row in rows["items"]}

        self.assertEqual(matches(DISPLAY_NO), {notice.pk for notice in legacy})
        self.assertEqual(matches("SAVED-NO"), {snapshot.pk})
        self.assertEqual(matches(ORDER_NO), {snapshot.pk, *(notice.pk for notice in legacy)})
        self.assertEqual(matches(DISPLAY_NO[:-1]), set())

    def test_history_finds_the_single_settlement_record_for_each_grouped_order(self):
        other_number = "INBOX-WX-0087"
        enqueue_worker_notice(
            user_id=self.worker_account.pk, kind="settled", reference="batch-with-two-orders",
            details={"related_order_nos": [DISPLAY_NO, other_number], "count": 2},
        )
        matched_ids = []
        for order in (DISPLAY_NO, other_number):
            history = self._data(self.admin.get(ADMIN + "history/", {"order_no": order}))
            row, = history["items"]
            self.assertEqual(row["order_no"], order)
            self.assertEqual(row["skip_reason"], "app_unconfigured")
            matched_ids.append(row["id"])
        self.assertEqual(matched_ids[0], matched_ids[1])

    def test_settlement_links_are_hidden_until_their_destination_is_authorized(self):
        self._enqueue("payment-without-income-access", kind="transfer_succeeded")

        def target():
            response = self._data(self.worker.get(INBOX, {"category": "settlement"}))
            return response["items"][0]["target"]

        self.assertIsNone(target())
        permissions = Role.objects.get(group__name=WORKER).permissions
        permissions.add(PermissionNode.objects.get(code="my_performance"))
        self.assertIsNone(target())
        permissions.add(PermissionNode.objects.get(code="my_performance.export"))
        self.assertEqual(target(), "/m/performance")

    def test_order_links_require_current_access_or_a_relevant_list(self):
        self._enqueue("assigned-with-limited-access")
        game = Game.objects.create(name="示例游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game)
        performance = Performance.objects.create(order=order, user=self.worker_account, emp_type=EmpType.MAIN)

        def target():
            return self._data(self.worker.get(INBOX))["items"][0]["target"]

        self.assertIsNone(target())
        permissions = Role.objects.get(group__name=WORKER).permissions
        permissions.add(
            PermissionNode.objects.get(code="my_orders"),
            PermissionNode.objects.get(code="my_orders.send_to_my_system"),
        )
        self.assertEqual(target(), f"/m/orders/{ORDER_NO}")
        performance.user = None
        performance.save(update_fields=["user"])
        self.assertEqual(target(), "/m/mine")
        permissions.remove(PermissionNode.objects.get(code="my_orders.send_to_my_system"))
        permissions.add(PermissionNode.objects.get(code="grab_hall.take_order"))
        self.assertIsNone(target())

    def test_inbox_has_existing_notices_but_excludes_test_pushes(self):
        self._configured_studio()
        binding = WxPusherBinding.objects.create(user=self.worker_account, app_id="900021", uid="UID_inbox")
        WxPusherNotice.objects.create(
            user=self.worker_account, binding=binding, reference="legacy-order", kind="assigned",
            order_no=ORDER_NO, status="sent",
        )
        WxPusherNotice.objects.create(
            user=self.worker_account, binding=binding, reference="manual-test", kind="test", status="sent",
        )
        inbox = self._data(self.worker.get(INBOX))
        self.assertEqual([item["kind"] for item in inbox["items"]], ["assigned"])
        self.assertEqual(inbox["unread_count"], 1)
        binding.delete()
        self.assertEqual(self._data(self.worker.get(INBOX))["items"], inbox["items"])


@override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA="")
class WxPusherInboxTenantTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(
            schema_name="wxp_inbox_other", name="示例消息工作室乙", domain="wxp-inbox-other.test.local",
        )

    def test_order_search_and_inbox_respect_tenant_and_session_scope(self):
        client = make_logged_in_client(username="wx-inbox-tenant-a", groups=(WORKER,), nodes=("my_profile",))
        admin = make_logged_in_client(username="wx-inbox-admin-a", groups=(TENANT_ADMIN,))
        game = Game.objects.create(name="示例游戏甲")
        Order.objects.create(order_no=ORDER_NO, source_order_no=DISPLAY_NO, game=game)
        WxPusherNotice.objects.create(
            user=get_user_model().objects.get(username="wx-inbox-admin-a"),
            kind="assigned", order_no=ORDER_NO, reference="local-legacy-record", status="sent",
        )
        with tenant_context(self.other):
            game = Game.objects.create(name="示例游戏乙")
            Order.objects.create(order_no=ORDER_NO, source_order_no="OTHER-WORKSTUDIO-ORDER", game=game)
            make_logged_in_client(
                domain="wxp-inbox-other.test.local", username="wx-inbox-tenant-b",
                groups=(WORKER,), nodes=("my_profile",),
            )
            user = get_user_model().objects.get(username="wx-inbox-tenant-b")
            enqueue_worker_notice(
                user_id=user.pk, kind="assigned", order_no=ORDER_NO,
                details={"display_order_no": "OTHER-WORKSTUDIO-ORDER"}, reference="other-record",
            )
            self.assertEqual(WxPusherNotice.objects.get(reference="other-record").status, "skipped")
        self.assertEqual(client.get(INBOX).json()["data"]["items"], [])
        self.assertEqual(admin.get(ADMIN + "history/", {"order_no": DISPLAY_NO}).json()["data"]["total"], 1)
        response = admin.get(ADMIN + "history/", {"order_no": "OTHER-WORKSTUDIO-ORDER"})
        self.assertEqual(response.json()["data"]["total"], 0)
        other_host = Client(SERVER_NAME="wxp-inbox-other.test.local")
        other_host.cookies.update(client.cookies)
        self.assertEqual(other_host.get(INBOX).status_code, 401)
