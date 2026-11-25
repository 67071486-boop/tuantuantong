"""`/api/auth/me` 增 `menu_permissions`（spec §5.3）。"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client


class MePayloadTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 建 schema 是 DDL ⇒ 必须在 `setUpClass`（`TenantTestCase` 把每个测试包在事务里）。
        # 额外租户**只写 `cls.tenant_b`**：`cls.tenant` / `cls.domain` 是基类 `tearDownClass`
        # 用来删它自己那个共享 `test` 租户的，覆盖它们是 E-0015。
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="rbac_me_b", name="租户 B", domain="rbac-me-b.test.local"
        )

    def setUp(self):
        seed_catalog()
        # ⚠️ **不要用 `self.client.force_login(...)`**：它产出的会话里没有 `identity_plane`，
        # 会被 `TenantSessionGuardMiddleware` 与 `TenantBoundAuthentication` 按 fail-closed 不变式
        # 拒成 **401**（`src/tests/p5_auth_helpers.py` 的 docstring 有实测，spec §5.1-3 有红线）。
        # 一律走真实登录夹具 `make_logged_in_client`；它**当场创建**用户（`seed_tenant` 的 admin
        # 是 `set_unusable_password()`，登不进去）。
        self.client = make_logged_in_client(username="me-plain", password="pw-123456")
        self.user = get_user_model().objects.get(username="me-plain")

    def _role(self, name, *codes):
        role = Role.objects.create(name=name, group=Group.objects.create(name=name))
        role.permissions.set(PermissionNode.objects.filter(code__in=codes))
        return role

    def test_menu_permissions_lists_the_union_of_roles_sorted(self):
        self.user.groups.add(self._role("客服", "order.list").group)
        self.user.groups.add(self._role("代练员", "order.delete", "home.enter").group)
        payload = self.client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(
            payload["menu_permissions"],
            ["home.enter", "order.delete", "order.list"],
        )

    def test_menu_permissions_is_empty_without_roles(self):
        payload = self.client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(payload["menu_permissions"], [])

    def test_django_codenames_still_present_and_unchanged(self):
        """`permissions`（Django codename）**保留原样**，冻结为 legacy（D-0028）。"""
        payload = self.client.get("/api/auth/me").json()["data"]["user"]
        self.assertIn("permissions", payload)
        self.assertIsInstance(payload["permissions"], list)

    def test_predicate_scope_comes_from_the_connection_not_the_user_object(self):
        """AGENTS.md §4：判据的租户作用域来自**连接**，不是传进来的 `user` 对象。

        `user_node_codes` 是**新增查询**（本任务把它接到了 `/api/auth/me`），按 §4 必须补一条
        跨租户否定用例。构造：A 里的用户持有 `order.list`；把连接切到 B 的 schema 后再求值——
        必须看不到这份授权（`user.groups.all()` 查的是**当前 schema** 的 `auth_user_groups`，
        A 的那行不在那里）。

        ⚠️ **否定断言不靠主键指认"另一个租户的那一行"**（AGENTS.md §5：两个 schema 的主键
        各自从 1 开始）：判据是「`order.list` 在**这条连接上**查不到」，不是「B 里没有某一行」。

        ⚠️ **为什么先证明 B 的目录里有 `order.list`**：`provision_tenant` 会调 `seed_rbac`，
        B 的目录与内置角色**已经**拿到每一个点（`test_rbac_models` 的同型注释记了这条）。
        少了这句，"B 里查不到"就可能只是"B 里是空的"——那是假通过。
        ⚠️ **为什么还要 B 侧用户的正向对照**：它证明"切到 B"这件事真的生效、B 的判据是活的，
        否则整套否定断言同样可能靠一个坏掉的连接上下文通过。
        """
        from django_tenants.utils import tenant_context

        from apps.rbac.permissions import has_node_now, user_node_codes

        self.user.groups.add(self._role("客服", "order.list").group)
        # 阳性对照（切换之前）：A 里必须看得见，否则否定断言可能只是夹具没生效。
        self.assertEqual(user_node_codes(self.user), {"order.list"})

        with tenant_context(self.tenant_b):
            # B 的目录里**有**这个点（内置角色持有它）⇒ 下面的空集不是"目录为空"造成的。
            self.assertTrue(Role.objects.filter(permissions__code="order.list").exists())
            # B 侧判别力对照：B 自己的用户看得见 B 自己发的 code。
            b_user = get_user_model().objects.create_user(
                username="me-b", password="pw-123456"
            )
            b_user.groups.add(self._role("陪玩师", "home.enter").group)
            self.assertEqual(user_node_codes(b_user), {"home.enter"})

            # 否定：A 的用户在 B 的连接上求值 ⇒ A 的授权不可见（端点判据同源）。
            self.assertNotIn("order.list", user_node_codes(self.user))
            self.assertFalse(has_node_now(self.user, "order.list"))
