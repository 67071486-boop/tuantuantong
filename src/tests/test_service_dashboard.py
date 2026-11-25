from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


URL = "/api/business/dashboard/service/"


class ServiceDashboardTests(TenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.addCleanup(connection.set_tenant, self.tenant)
        self.game = Game.objects.create(name="授权游戏")
        self.other_game = Game.objects.create(name="未授权游戏")
        self.client = make_logged_in_client(
            username="service_a", groups=("客服甲",), nodes=("order.list", "order.add_edit", "order.add_log", "order.restrict_to_self_created"), games=[self.game]
        )
        self.user = User.objects.get(username="service_a")
        self.other_client = make_logged_in_client(username="service_b", groups=("客服乙",), nodes=("order.list", "order.add_log"))
        self.other = User.objects.get(username="service_b")

    def order(self, suffix, *, game=None, owner=None, creator="service_a", status="ISSUE"):
        return Order.objects.create(
            order_no=f"2026111400000000{suffix:04d}", game=game or self.game,
            sales_user=owner or self.user, created_by=creator, status=status,
        )

    def test_visible_own_orders_dates_and_distinct_personal_operations(self):
        first = self.order(1)
        second = self.order(2, status="PAUSED")
        completed = self.order(3, status="COMPLETED")
        Order.objects.filter(pk=completed.pk).update(completed_at=timezone.now())
        self.order(4, game=self.other_game)
        self.order(5, owner=self.other)
        self.order(6, creator="service_b")
        deleted = self.order(7)
        Order.objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())
        for _ in range(2):
            response = self.client.post(f"/api/business/orders/{first.order_no}/notes/", {"content": "跟进"}, content_type="application/json")
            self.assertEqual(response.json()["code"], 0)
        OperationLog.objects.create(log_type=10, action="order.transition", actor_id=self.user.pk, actor=self.user.username, order_no=second.order_no, summary="操作")
        OperationLog.objects.create(log_type=10, action="order.note", actor_id=self.other.pk, actor=self.other.username, order_no=completed.order_no, summary="他人操作")
        OperationLog.objects.create(log_type=10, action="order.note", actor_id=self.user.pk, actor=self.user.username, order_no=completed.order_no, summary="昨日操作", created_at=timezone.now() - timedelta(days=1))
        data = self.client.get(URL, {"user_id": self.other.pk, "tenant": "other"}).json()["data"]
        self.assertEqual(data["today_created"], 3)
        self.assertEqual(data["today_completed"], 1)
        self.assertEqual(data["today_followed"], 2)
        self.assertEqual(data["status_counts"]["ISSUE"], 1)
        self.assertEqual(data["status_counts"]["PAUSED"], 1)
        self.assertEqual(data["status_counts"]["COMPLETED"], 1)
        self.assertEqual({r["order_no"] for r in data["attention_orders"]}, {first.order_no, second.order_no})

    def test_permission_and_stats_restriction(self):
        self.assertEqual(Client(SERVER_NAME="tenant.test.com").get(URL).status_code, 401)
        denied = make_logged_in_client(username="no_order_permission")
        self.assertEqual(denied.get(URL).status_code, 403)
        restricted = make_logged_in_client(username="no_total", groups=("受限员",), nodes=("order.list", "order.restrict_view_total_stats"))
        self.assertEqual(restricted.get(URL).status_code, 403)

    def test_real_intake_and_notes_count_each_order_once(self):
        def create_order():
            response = self.client.post(
                "/api/business/orders/", {"game": self.game.pk, "project_text": "合成项目"},
                content_type="application/json",
            ).json()
            self.assertEqual(response["code"], 0, response)
            return response["data"]["order_no"]

        first = create_order()
        self.assertEqual(Order.objects.get(order_no=first).sales_user_id, self.user.pk)
        self.assertTrue(OperationLog.objects.filter(order_no=first, action="order.create", actor_id=self.user.pk).exists())
        self.assertEqual(self.client.get(URL).json()["data"]["today_followed"], 1)
        note = self.client.post(
            f"/api/business/orders/{first}/notes/", {"content": "联系客户"},
            content_type="application/json",
        ).json()
        self.assertEqual(note["code"], 0, note)
        self.assertTrue(OperationLog.objects.filter(order_no=first, action="order.note", actor_id=self.user.pk).exists())
        self.assertEqual(self.client.get(URL).json()["data"]["today_followed"], 1)
        create_order()
        self.assertEqual(self.client.get(URL).json()["data"]["today_followed"], 2)

    def test_status_counts_match_filtered_order_list_for_split_groups(self):
        other_owner = self.order(14, owner=self.other)
        issue_first = self.order(11)
        issue_second = self.order(12)
        paused = self.order(13, status="PAUSED")
        self.order(15, status="PAUSED")
        for order in (issue_first, issue_second, paused, other_owner):
            Order.objects.filter(pk=order.pk).update(split_id="split-synthetic-group")
        for order in (issue_first, issue_second):
            OperationLog.objects.create(log_type=10, action="order.note", actor_id=self.user.pk, actor=self.user.username, order_no=order.order_no, summary="跟进")

        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["status_counts"]["ISSUE"], 1)
        self.assertEqual(data["status_counts"]["PAUSED"], 2)
        self.assertEqual(data["today_created"], 4)
        self.assertEqual(data["today_followed"], 2)
        for status in ("ISSUE", "PAUSED"):
            response = self.client.get("/api/business/orders/", {"status": status, "sales_user": self.user.pk}).json()
            self.assertEqual(response["code"], 0, response)
            self.assertEqual(data["status_counts"][status], response["data"]["total"])
        combined = self.client.get("/api/business/orders/", {"status": "ISSUE,PAUSED", "sales_user": self.user.pk}).json()
        self.assertEqual(combined["code"], 0, combined)
        self.assertEqual({r["order_no"] for r in data["attention_orders"]}, {r["order_no"] for r in combined["data"]["items"]})


class ServiceDashboardIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.tenant_b, _ = provision_tenant(schema_name="service_board_b", name="客服看板乙", domain="service-board-b.test.local")
        finally:
            connection.set_tenant(cls.tenant)

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.addCleanup(connection.set_tenant, self.tenant)

    def test_tenant_credentials_cannot_see_other_tenant_orders(self):
        game = Game.objects.create(name="甲游戏")
        client_a = make_logged_in_client(username="same_service", groups=("客服甲",), nodes=("order.list",), games=[game])
        user_a = User.objects.get(username="same_service")
        Order.objects.create(order_no="20261114000000000101", game=game, sales_user=user_a, status="ISSUE")
        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="乙游戏")
                client_b = make_logged_in_client(domain="service-board-b.test.local", username="same_service", groups=("客服乙",), nodes=("order.list",), games=[game_b])
                user_b = User.objects.get(username="same_service")
                Order.objects.create(order_no="20261114000000000202", game=game_b, sales_user=user_b, status="PAUSED")
            data_b = client_b.get(URL, SERVER_NAME="service-board-b.test.local").json()["data"]
        finally:
            connection.set_tenant(self.tenant)
        data_a = client_a.get(URL).json()["data"]
        self.assertEqual([r["order_no"] for r in data_a["attention_orders"]], ["20261114000000000101"])
        self.assertEqual([r["order_no"] for r in data_b["attention_orders"]], ["20261114000000000202"])
        try:
            denied = client_a.get(URL, SERVER_NAME="service-board-b.test.local")
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(denied.status_code, 401)
