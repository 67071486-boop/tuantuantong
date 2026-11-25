"""`has_node` 的双向判别力（spec §10-A6）。

**走真实端点**，不在进程内手工调 `has_permission`——进程内调用测的是"这个函数能返回
True/False"，而不是"端点真的被拦住了"（P5 的探针口径，`tests/test_p5_rbac.py` 同源）。

⚠️ **三处 brief 原文的实施期修订**（都是计划缺陷，实测抓到的；brief 六条用例的语义一字未改）：

1. **登录一律走真实端点**（brief 原文用 `self.client.force_login(...)`）：
   `force_login` 产出的会话里**没有 `identity_plane`**，会被
   `TenantSessionGuardMiddleware`（`apps/core/middleware.py:30-32`）与
   `TenantBoundAuthentication` 按 fail-closed 不变式拒掉 ⇒ **每个请求都是 401**，
   六条用例全部无法区分"判据拒绝"与"会话非法"。
   `tests/p5_auth_helpers.py` 的模块 docstring 已把这条写成硬结论，本文件照用它的夹具。

2. **`@override_settings` 一律放方法上**（brief 原文写在类上，实测**零效果**）：
   `TenantTestCase.setUpClass`（`django_tenants/test/cases.py`）**不调**
   `super().setUpClass()`，而 Django 5.2 的类级 `override_settings` 只在
   `SimpleTestCase.setUpClass` 里 `enterClassContext` 生效
   （`django/test/testcases.py:230-231`）⇒ 类级覆盖被**静默丢弃**。
   实测（throwaway 探针）：类级装饰下 `settings.ROOT_URLCONF` 仍是 `config.urls`；
   方法级才是 `tests.p5_probe_urls`。
   这不是本文件独有的坑，`tests/test_p5_rbac.py` 的模块 docstring 第 2 条已记录同一件事。

3. **探针 client 的 Host 必须是租户域名**（`p5_auth_helpers` 的默认值
   `tenant.test.com` 就是 `TenantTestCase.get_test_tenant_domain()` 的取值）：
   用 bare `Client` 的 `"testserver"` 会让 `TenantMainMiddleware` 找不到租户而抛
   `Http404`，**且它在查租户之前就 `connection.set_schema_to_public()`**
   （`django_tenants/middleware/main.py:36`（`set_schema_to_public()` 是本仓库实测行号）
   与 `:71`（查不到租户时 `raise TENANT_NOT_FOUND_EXCEPTION`，Http404 由此产生）；
   ⚠️ 原稿写的 `:33,57-58` 是**错的行号**，终局评审 D3 实测更正——行号是排障依据（E-0003））——
   404 之后连接**留在 public 平面**，
   于是本类第一个用例只拿到 404，后面每个用例的 `setUp` 都因为 public 里没有
   `rbac_permissionnode` 而 `ProgrammingError`（实测
   `Ran 6 tests ... FAILED (failures=1, errors=5)`）。夹具域名天然满足这一条。

⚠️ **第 7 条（`test_factory_returns_a_real_class_not_an_instance_or_partial`）不是 brief 原文**，
是 Task 5 独立评审的裁决产物（C-1，计划提交 `3c6668b`）：brief 的六条**没有一条**能分辨
"工厂返回真类"与"工厂返回 `functools.partial`"——实现者的变异 m2 实测，
把工厂换成 `partial` 后六条**全绿**。这条契约因此必须有自己的断言。
"""
import inspect

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.roles import TENANT_ADMIN
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

PROBE_URLCONF = "tests.p5_probe_urls"

USERNAME = "node-plain"
PASSWORD = "pw-123456"


class NodePredicateTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        self.client = make_logged_in_client(username=USERNAME, password=PASSWORD)
        self.user = get_user_model().objects.get(username=USERNAME)

    def _grant(self, *codes):
        role = Role.objects.create(
            name="代练员", group=Group.objects.create(name="代练员")
        )
        role.permissions.set(PermissionNode.objects.filter(code__in=codes))
        self.user.groups.add(role.group)

    @override_settings(ROOT_URLCONF=PROBE_URLCONF)
    def test_denies_user_without_the_node(self):
        response = self.client.get("/__probe__/node-only")
        self.assertEqual(response.status_code, 403)

    @override_settings(ROOT_URLCONF=PROBE_URLCONF)
    def test_allows_user_holding_the_node(self):
        self._grant("order.list")
        response = self.client.get("/__probe__/node-only")
        self.assertEqual(response.status_code, 200)

    @override_settings(ROOT_URLCONF=PROBE_URLCONF)
    def test_denies_user_holding_a_different_node(self):
        """判别力对照：拿**另一个**节点不该放行（否则"有任意权限就放行"也能绿）。"""
        self._grant("order.delete")
        self.assertEqual(self.client.get("/__probe__/node-only").status_code, 403)

    def test_public_plane_fails_closed_and_does_not_500(self):
        """`public` 平面一律 False，且**不抛 `ProgrammingError`**（spec §9 的平面规则）。

        `PermissionNode` / `Role` 是租户表，public 里没这两张表；不挡会在判据层炸成 500。
        用 `schema_context("public")` 直接切连接来构造该平面（不看 Host，判据只认连接）。
        """
        from django_tenants.utils import schema_context

        self._grant("order.list")
        with schema_context("public"):
            from apps.rbac.permissions import has_node_now, user_node_codes

            self.assertEqual(user_node_codes(self.user), set())
            self.assertFalse(has_node_now(self.user, "order.list"))

    def test_public_plane_denies_even_a_superuser(self):
        """平面守卫必须排在 `is_superuser` 短路**之前**（否则 public 上的超管会短路成真）。"""
        from django_tenants.utils import schema_context

        su = get_user_model().objects.create_superuser(
            username="node-su-pub", password="pw-123456"
        )
        with schema_context("public"):
            from apps.rbac.permissions import has_node_now

            self.assertFalse(has_node_now(su, "order.list"))

    @override_settings(ROOT_URLCONF=PROBE_URLCONF)
    def test_bare_superuser_does_not_pass_without_the_admin_position(self):
        """**口径变更（业务方 2026-09-23）**：租户平面的管理员判据是**持「系统管理员」职位**，
        只置 `is_superuser` 的账号过不了节点门——原文断言它 200，本线改成 403。

        ⚠️ 超管标志仍用 `.update()` 落库（模板方法见下），这里正好拿它构造"有标志、没职位"
        这个反例：`request.user` 每个请求都从库里重新加载（`ModelBackend.get_user`），
        所以下一条请求就看得到。构造前提显式断言（`groups` 为空），否则这一步无判别力。
        """
        self.assertFalse(self.user.groups.exists(), "构造失败：本账号不该有角色")
        get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=True)
        self.assertEqual(self.client.get("/__probe__/node-only").status_code, 403)

    @override_settings(ROOT_URLCONF=PROBE_URLCONF)
    def test_admin_position_holder_passes_without_any_node(self):
        """正向对照（**新增**）：持「系统管理员」职位、一个节点都不挂 ⇒ 管理员短路放行。

        没有它，上一条的 403 可能由"这个端点对谁都拒"凑出来（E-0003 的判别力纪律）。
        """
        group, _ = Group.objects.get_or_create(name=TENANT_ADMIN)
        self.user.groups.add(group)
        self.assertEqual(self.client.get("/__probe__/node-only").status_code, 200)

    def test_factory_returns_a_real_class_not_an_instance_or_partial(self):
        """工厂必须返回**真类**（Task 5 评审 C-1，2026-09-20 裁决）。

        `has_node(...)` 的返回值会被 DRF **无参实例化**（`rest_framework/views.py:283`）。
        `functools.partial(HasNode, "x")` 也能无参调用、**端点级测试分辨不出来**
        （实现者的变异 m2 实测：把工厂换成 `partial`，6 条测试**全绿**）——
        所以这条契约必须有它自己的断言，否则它就是一句没人验的话。
        同一缺口在 `apps/core/permissions.py` 的 `has_role` / `has_permission` 上**仍然存在**
        （不在本线范围内，留待后续）。
        """
        from apps.rbac.permissions import has_node

        self.assertTrue(inspect.isclass(has_node("order.list")))
        self.assertFalse(inspect.isclass(has_node("order.list")("order.list")))
