from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import FavoriteAssignee, StaffProfile
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client


URL = "/api/business/orders/assignee-favorites/"
ASSIGN = "order.assign_taker"


def toggle(client, assignee, favorite, **extra):
    return client.put(
        URL, {"user_id": assignee.pk, "favorite": favorite, **extra},
        content_type="application/json",
    )


class FavoriteAssigneeTests(TenantTestCase):
    def setUp(self):
        self.operator = make_logged_in_client(username="收藏测试客服", groups=("客服甲",), nodes=(ASSIGN,), games=None)
        self.taker = User.objects.create_user(username="收藏测试打手", first_name="打手昵称甲")

    def test_explicit_save_order_and_remove_follow_login_across_browsers(self):
        self.assertEqual(self.operator.get(URL).json()["data"], [])
        self.assertEqual(
            toggle(self.operator, self.taker, True).json()["data"],
            [{"id": self.taker.pk, "name": "打手昵称甲", "role": None, "tag": "", "remark": ""}],
        )
        second = User.objects.create_user(username="收藏测试打手乙")
        self.assertEqual(toggle(self.operator, second, True).json()["code"], 0)
        self.assertEqual(
            [item["name"] for item in toggle(self.operator, self.taker, True).json()["data"]],
            ["收藏测试打手乙", "打手昵称甲"],
        )
        self.assertEqual(FavoriteAssignee.objects.count(), 2)
        self.assertFalse(StaffProfile.objects.filter(user__username="收藏测试客服").exists())

        browser = Client(SERVER_NAME="tenant.test.com")
        logged_in = browser.post(
            "/api/auth/login", {"username": "收藏测试客服", "password": P2_TEST_PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(logged_in.json()["code"], 0)
        self.assertEqual(
            [item["name"] for item in browser.get(URL).json()["data"]],
            ["收藏测试打手乙", "打手昵称甲"],
        )
        for _ in range(2):
            self.assertEqual(toggle(browser, self.taker, False).json()["code"], 0)
        self.assertEqual([item["name"] for item in self.operator.get(URL).json()["data"]], ["收藏测试打手乙"])

    def test_favorites_only_belong_to_logged_in_operator(self):
        colleague = make_logged_in_client(username="另一收藏测试客服", groups=("客服乙",), nodes=("order.submit_payout_price",))
        colleague_id = User.objects.get(username="另一收藏测试客服").pk
        self.assertEqual(toggle(self.operator, self.taker, True, operator=colleague_id).json()["code"], 0)
        self.assertEqual(colleague.get(URL, {"operator": User.objects.get(username="收藏测试客服").pk}).json()["data"], [])
        self.assertEqual(toggle(colleague, self.taker, False).json()["data"], [])
        self.assertEqual([item["name"] for item in self.operator.get(URL).json()["data"]], ["打手昵称甲"])

    def test_no_assign_node_cannot_read_or_write(self):
        unauthorized = make_logged_in_client(username="只读收藏测试客服", groups=("只读",), nodes=("order.list", "order.clear_taker"))
        self.assertEqual(unauthorized.get(URL).status_code, 403)
        self.assertEqual(toggle(unauthorized, self.taker, True).status_code, 403)
        self.assertFalse(FavoriteAssignee.objects.exists())

    def test_inactive_deleted_and_invalid_accounts_are_not_available(self):
        self.assertEqual(toggle(self.operator, self.taker, True).json()["code"], 0)
        self.taker.is_active = False
        self.taker.save(update_fields=["is_active"])
        self.assertEqual(self.operator.get(URL).json()["data"], [])
        self.assertNotEqual(toggle(self.operator, self.taker, True).json()["code"], 0)
        self.taker.is_active = True
        self.taker.save(update_fields=["is_active"])
        StaffProfile.objects.create(user=self.taker, deleted_at=timezone.now())
        self.assertEqual(self.operator.get(URL).json()["data"], [])
        self.assertNotEqual(toggle(self.operator, self.taker, True).json()["code"], 0)
        self.taker.delete()
        self.assertFalse(FavoriteAssignee.objects.exists())
        malformed = self.operator.put(URL, {"user_id": "错误"}, content_type="application/json")
        self.assertNotEqual(malformed.json()["code"], 0)


class FavoriteAssigneeTenantIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="favorite_assignee_other",
            name="收藏隔离测试乙站",
            domain="favorite-assignee-other.test.local",
        )

    def test_tenant_a_session_cannot_read_or_write_tenant_b_favorite(self):
        connection.set_tenant(self.tenant)
        first = make_logged_in_client(username="收藏隔离客服甲", groups=("客服甲",), nodes=(ASSIGN,))
        taker_a = User.objects.create_user(username="收藏隔离打手甲")
        self.assertEqual(toggle(first, taker_a, True).json()["code"], 0)
        self.assertEqual([item["name"] for item in first.get(URL).json()["data"]], ["收藏隔离打手甲"])

        domain_b = "favorite-assignee-other.test.local"
        try:
            with tenant_context(self.other_tenant):
                second = make_logged_in_client(domain=domain_b, username="收藏隔离客服乙", groups=("客服甲",), nodes=(ASSIGN,))
                taker_b = User.objects.create_user(username="收藏隔离打手乙")
                self.assertEqual(toggle(second, taker_b, True).json()["code"], 0)
                self.assertEqual([item["name"] for item in second.get(URL).json()["data"]], ["收藏隔离打手乙"])

            cross_tenant = Client(SERVER_NAME=domain_b)
            cross_tenant.cookies = first.cookies.copy()
            self.assertEqual(cross_tenant.get(URL).status_code, 401)
            cross_tenant.cookies = first.cookies.copy()
            response = toggle(cross_tenant, taker_b, False)
            self.assertEqual(response.status_code, 401)
            self.assertEqual([item["name"] for item in second.get(URL).json()["data"]], ["收藏隔离打手乙"])
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(
            list(FavoriteAssignee.objects.values_list("assignee__username", flat=True)),
            ["收藏隔离打手甲"],
        )
