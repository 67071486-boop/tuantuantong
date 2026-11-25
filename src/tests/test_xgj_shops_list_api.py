"""`GET /api/business/xgj-shops/` 的契约测试（2026-10-26，计划 §3.2 / §3.4 / §6 批次 1a）。

这是「订单来源管理」页「店铺归属」区块的数据源：**跨应用**的店铺列表，可按
「订单来源」「公司店铺分组」筛。本文件覆盖计划点名的六件事：

1. 按来源筛 / 按分组筛（各自只留命中行，且先有**非空哨兵**）；
2. 跨租户隔离（A 的 client 看不见 B 的店）；
3. `0` / `abc` ⇒ 200 + `1000`（入参过序列化器，⛔ 不是 HTML 500）；
4. **未登录 401**；
5. **孤儿店铺**（`binding IS NULL`）**出现在列表里**（重新同步后会回来 ⇒ 要看得见）；
6. 行形状恰好那 12 个键（含 `group_id` / `group_name`）。

⚠️ 判据纪律：跨租户否定断言**不用主键**指认另一租户的行（两个 schema 的主键各自从 1 起），
用租户内唯一、且两租户取值不同的 `user_identity` / `shop_name`。
"""
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, OrderSourceGroup, XgjShop, XgjShopBinding
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/business/xgj-shops/"

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "xgj-shops-b.test.local"
SCHEMA_B = "xgj_shops_b"

#: 行形状的**恰好**键集（计划 §3.4 的列 + 对接页子表那几列；⛔ 不从序列化器派生，E-0024）。
ROW_FIELDS = [
    "id", "shop_name", "user_name",
    "order_source", "order_source_name", "group_id", "group_name", "groups",
    "is_valid", "valid_end_time", "subscription_warning",
    "binding", "binding_name",
]


class XgjShopListApiTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(username="shops-api")
        self.binding = XgjShopBinding.objects.create(shop_name="应用甲")
        self.group = OrderSourceGroup.objects.create(name="甲组")
        self.source_a = OrderSource.objects.create(name="来源甲", group=self.group)
        self.source_b = OrderSource.objects.create(name="来源乙")

    def _shop(self, identity, *, source, binding=None, name=None):
        return XgjShop.objects.create(
            user_identity=identity,
            shop_name=name or identity,
            order_source=source,
            binding=binding,
        )

    def _get(self, params=None):
        response = self.client.get(BASE, params or {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def test_row_shape_is_exactly_the_enumerated_keys(self):
        self._shop("shop-a", source=self.source_a, binding=self.binding)
        body = self._get()
        self.assertEqual(body["code"], 0)
        item = body["data"]["items"][0]
        self.assertEqual(list(item.keys()), ROW_FIELDS)
        # 分组两列来自「店铺当前所属来源的分组」。
        self.assertEqual(item["group_id"], self.group.id)
        self.assertEqual(item["group_name"], "甲组")
        self.assertEqual(item["order_source"], self.source_a.id)
        self.assertEqual(item["order_source_name"], "来源甲")
        self.assertEqual(item["binding"], self.binding.id)
        self.assertEqual(item["binding_name"], "应用甲")

    def test_unassigned_source_shows_empty_group(self):
        """来源未分组 ⇒ `group_id=null` / `group_name=""`（页面显示「未分组」，不伪装）。"""
        self._shop("shop-b", source=self.source_b, binding=self.binding)
        item = self._get()["data"]["items"][0]
        self.assertIsNone(item["group_id"])
        self.assertEqual(item["group_name"], "")

    def test_orphan_shop_without_binding_is_listed(self):
        """`binding IS NULL` 的孤儿店铺**必须看得见**（删了对接后店行还在，重新同步会回来）。"""
        self._shop("orphan", source=self.source_a, binding=None)
        self._shop("normal", source=self.source_a, binding=self.binding)
        body = self._get()
        self.assertEqual(body["data"]["total"], 2)
        identities = [i["shop_name"] for i in body["data"]["items"]]
        self.assertEqual(identities, ["orphan", "normal"])  # order_by("id")
        orphan = body["data"]["items"][0]
        self.assertIsNone(orphan["binding"])
        self.assertEqual(orphan["binding_name"], "")

    def test_shop_without_source_is_listed_with_empty_names(self):
        """`order_source` 也是 `SET_NULL`：删了来源的店行照样出现，来源/分组列显示空。"""
        self._shop("no-source", source=None, binding=self.binding)
        item = self._get()["data"]["items"][0]
        self.assertIsNone(item["order_source"])
        self.assertEqual(item["order_source_name"], "")
        self.assertIsNone(item["group_id"])
        self.assertEqual(item["group_name"], "")

    def test_filter_by_order_source(self):
        self._shop("shop-a", source=self.source_a, binding=self.binding)
        self._shop("shop-b", source=self.source_b, binding=self.binding)
        body = self._get({"order_source": self.source_a.id})
        self.assertEqual(body["code"], 0)
        self.assertEqual([i["shop_name"] for i in body["data"]["items"]], ["shop-a"])
        self.assertEqual(body["data"]["total"], 1)

    def test_filter_by_source_group(self):
        self._shop("shop-a", source=self.source_a, binding=self.binding)
        self._shop("shop-b", source=self.source_b, binding=self.binding)
        body = self._get({"source_group": self.group.id})
        self.assertEqual(body["code"], 0)
        self.assertEqual([i["shop_name"] for i in body["data"]["items"]], ["shop-a"])

    def test_both_filters_intersect(self):
        """两个筛选同时给 ⇒ 取交集（页面两个下拉各自独立，但都发出去）。"""
        self._shop("shop-a", source=self.source_a, binding=self.binding)
        body = self._get({"source_group": self.group.id, "order_source": self.source_b.id})
        self.assertEqual(body["data"]["items"], [])

    def test_zero_and_abc_are_1000(self):
        for params in (
            {"order_source": "0"},
            {"source_group": "0"},
            {"order_source": "-1"},
            {"source_group": "abc"},
        ):
            with self.subTest(params=params):
                body = self._get(params)
                self.assertEqual(body["code"], 1000, body)

    def test_malformed_page_does_not_500(self):
        self._shop("shop-a", source=self.source_a, binding=self.binding)
        for params in ({"page": "abc"}, {"page_size": "abc"}):
            with self.subTest(params=params):
                response = self.client.get(BASE, params)
                self.assertEqual(response.status_code, 200)
                self.assertIn("code", response.json())

    def test_unauthenticated_is_401(self):
        """权限 = 只登录（端点不加节点）；未登录必须是 401 + `data:null`，不是 200。"""
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        response = anonymous.get(BASE)
        self.assertEqual(response.status_code, 401, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 401)
        self.assertIsNone(body["data"])


class XgjShopListIsolationTests(TenantTestCase):
    """跨租户隔离（`AGENTS.md` §4 的红线：A 的凭据拿不到 B 的店）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="店铺归属隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_each_tenant_only_sees_its_own_shops(self):
        with tenant_context(self.tenant):
            XgjShop.objects.create(user_identity="identity-a", shop_name="A 的店")
            client_a = make_logged_in_client(domain=DOMAIN_A, username="shop-iso-a")
        with tenant_context(self.tenant_b):
            XgjShop.objects.create(user_identity="identity-b", shop_name="B 的店")
        body = client_a.get(BASE).json()
        self.assertEqual(body["code"], 0)
        # 非空哨兵 + 否定断言。
        self.assertEqual([i["shop_name"] for i in body["data"]["items"]], ["A 的店"])
        self.assertNotIn("B 的店", [i["shop_name"] for i in body["data"]["items"]])
