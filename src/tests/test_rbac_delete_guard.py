"""删除角色的两道守卫：①「最后管理员」保护（spec §6.5 / §10-A5，决策 D9）；②「在用即拒」——该职位还挂着系统用户（**含已停用账号**）时拒绝删除（本线 D2，2026-09-23 加）。"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.rbac.guards import ensure_not_last_admin_role, ensure_role_unused
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_catalog, seed_builtin_roles
from apps.rbac.services import RoleServiceError
from tests.p5_auth_helpers import make_logged_in_client

#: 调用者用的**非管理员**角色名（持删除点，但不计入管理员）。
CALLER_ROLE = "删角色专员"

BASE = "/api/role-authorities/"


class DeleteGuardTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        seed_builtin_roles()
        # ⚠️ **不要用 `self.client.force_login(...)`**：它产出的会话里没有 `identity_plane`，
        # 会被 `TenantSessionGuardMiddleware` 与 `TenantBoundAuthentication` 按 fail-closed 不变式
        # 拒成 **401**（`src/tests/p5_auth_helpers.py` 的 docstring 有实测，spec §5.1-3 有红线）。
        # 一律走真实登录夹具 `make_logged_in_client`；它**当场创建**用户（`seed_tenant` 的 admin
        # 是 `set_unusable_password()`，登不进去）。
        self.client = make_logged_in_client(username="guard-caller", password="pw-123456")
        # ⚠️ **调用者必须是"非管理员"**：
        # `_admin_filter()` = 持「系统管理员」Group（判据统一后**不再**看
        # `is_superuser`；原文写的是 `is_superuser=True` ∪ 持该 Group，随本线作废），
        # 所以把登录账号放进该组会让它**自己就贡献 1 个管理员**，
        # 再 `_admin_member()` 加一个就是 2 ⇒ "只剩一个"的拒绝用例算术上不可能成立。
        # 这里给调用者一个**非管理员**角色（只持 `system.role_authority_delete`）：
        # 它过得了端点门禁，但**不计入管理员** ⇒ "有几个管理员"完全由 `_admin_member()` 显式造。
        caller = get_user_model().objects.get(username="guard-caller")
        role = Role.objects.create(
            name=CALLER_ROLE, group=Group.objects.create(name=CALLER_ROLE)
        )
        role.permissions.set(
            PermissionNode.objects.filter(code="system.role_authority_delete")
        )
        caller.groups.add(role.group)

    def _admin_member(self, username):
        """造一个持「系统管理员」角色的账号。

        ⚠️ `create_superuser` **不会**把账号加进任何 Group（它只置 `is_superuser`），
        所以"有几个管理员"必须由本方法显式造——否则 `active_admin_count()` 恒为 0，
        正向对照会假红（也会让负向断言的判别力说不清）。
        """
        user = get_user_model().objects.create_user(username=username, password="pw-123456")
        user.groups.add(Group.objects.get(name="系统管理员"))
        return user

    def test_deleting_the_last_admin_role_is_refused(self):
        self._admin_member("only-admin")  # 活跃管理员恰好 1 个，且他依赖该角色
        with self.assertRaises(RoleServiceError):
            ensure_not_last_admin_role(Role.objects.get(name="系统管理员"))

    def test_refused_when_every_admin_depends_on_that_role(self):
        """**致命边界**：两个管理员**都是**该角色的持有者 ⇒ 删完剩 0，必须拒。

        ⚠️ 原判据是"现在总数 `<=1`"：两个持有者时总数 = 2 ⇒ **放行** ⇒ 删完**零个管理员**——
        正是守卫生来要防的那件事（Task 8 执行期实测的计划缺陷）。
        """
        self._admin_member("admin-one")
        self._admin_member("admin-two")
        with self.assertRaises(RoleServiceError):
            ensure_not_last_admin_role(Role.objects.get(name="系统管理员"))

    def test_deleting_a_non_admin_role_is_allowed(self):
        ensure_not_last_admin_role(Role.objects.get(name="客服"))

    def test_refused_even_when_a_bare_superuser_exists(self):
        """**反例变正例（2026-09-23 判据统一）**：只置 `is_superuser` 的账号**不是**管理员
        ⇒ 它不算"删完还活着的那个"，删除最后一个管理员角色**必须被拒**。

        ⚠️ 原文（`test_allowed_when_a_superuser_survives_the_deletion`）断言"存在一个
        **不依赖该角色**的管理员（superuser）⇒ 放行"——那句依据的是旧口径
        `_admin_filter = is_superuser ∪ 该 Group`。新口径下 `_admin_filter` 只认该 Group，
        所以 `active_admin_count()` 就是该 Group 的活跃成员数，删完恒为 0。

        ⚠️ 正向对照仍在：`test_deleting_a_non_admin_role_is_allowed` 证明本函数不是对谁都拒。
        """
        from apps.accounts.guards import active_admin_count

        su = get_user_model().objects.create_user(username="guard-su", password="pw-123456")
        su.is_superuser = True
        su.save(update_fields=["is_superuser"])
        self._admin_member("admin-one")
        self.assertIs(active_admin_count(), 1, "构造失败：活跃管理员应恰好 1 个（在组的那个）")
        with self.assertRaises(RoleServiceError):
            ensure_not_last_admin_role(Role.objects.get(name="系统管理员"))

    def test_endpoint_refuses_and_keeps_the_row(self):
        self._admin_member("only-admin")
        role_id = Role.objects.get(name="系统管理员").id
        response = self.client.delete(f"{BASE}{role_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertTrue(Role.objects.filter(pk=role_id).exists())

    def test_role_with_an_active_member_is_refused(self):
        role = Role.objects.get(name="客服")
        user = get_user_model().objects.create_user(username="cs-1", password="pw-123456")
        user.groups.add(role.group)
        with self.assertRaises(RoleServiceError):
            ensure_role_unused(role)

    def test_role_with_only_an_inactive_member_is_still_refused(self):
        """停用账号**仍挂在组上** —— 删组会让它当场丢职位。判据含停用（spec §3.2）。"""
        role = Role.objects.get(name="客服")
        user = get_user_model().objects.create_user(username="cs-off", password="pw-123456")
        user.is_active = False
        user.save(update_fields=["is_active"])
        user.groups.add(role.group)
        with self.assertRaises(RoleServiceError):
            ensure_role_unused(role)

    def test_role_without_members_is_allowed(self):
        """正向对照：没有成员就必须放行 —— 否则「拒绝」可能由「这函数对谁都拒」凑出来。"""
        ensure_role_unused(Role.objects.get(name="客服"))

    def test_last_admin_guard_reports_first_when_both_would_refuse(self):
        """两条守卫同时命中时，**拒绝理由确定**：先报「最后管理员」。

        判据：拿 `RoleServiceError.code` 断言（不是 msg 文本 —— msg 会随文案调整而漂）。
        """
        self._admin_member("only-admin")   # 既是用在用的职位，又是最后一个管理员
        role = Role.objects.get(name="系统管理员")
        with self.assertRaises(RoleServiceError) as ctx:
            ensure_not_last_admin_role(role)
        self.assertEqual(ctx.exception.code, "last_admin_role")
        with self.assertRaises(RoleServiceError) as ctx2:
            ensure_role_unused(role)
        self.assertEqual(ctx2.exception.code, "role_in_use")
        # 端点的顺序由下面的用例钉住（两条都拒，但报哪条是契约）

    def test_endpoint_reports_the_last_admin_reason_first(self):
        self._admin_member("only-admin")
        role_id = Role.objects.get(name="系统管理员").id
        body = self.client.delete(f"{BASE}{role_id}/").json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("最后一个系统管理员", body["msg"])

    def test_endpoint_refuses_a_role_that_still_has_a_member(self):
        """**补的**（brief 只给了函数级的在用用例）：新守卫真的**接在端点上**了。

        判别力：删掉 `destroy` 里那行 `self._run(ensure_role_unused, role)` ⇒ 本用例红
        （角色与 Group 真的被删掉、且回 `code 0`）——上面三条函数级用例**抓不到**这个缺口
        （它们直接调 `ensure_role_unused`），而顺序用例在没有新守卫时**照样绿**
        （「最后管理员」先报）。这是本项目 E-0009「测了动作、没测动作的落地机制」的同型形状。
        msg 只取能区分两条守卫的最短子串（不钉整句，文案会漂）。
        """
        member = get_user_model().objects.create_user(username="cs-1", password="pw-123456")
        member.groups.add(Group.objects.get(name="客服"))
        role_id = Role.objects.get(name="客服").id
        body = self.client.delete(f"{BASE}{role_id}/").json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("1 个系统用户在使用", body["msg"])
        self.assertTrue(Role.objects.filter(pk=role_id).exists(), "被拒的删除把角色删掉了")
        self.assertTrue(Group.objects.filter(name="客服").exists(), "被拒的删除把 Group 删掉了")

    def test_tenant_admin_constant_is_the_seeded_admin_role_name(self):
        """守卫按 `TENANT_ADMIN` 判管理员角色，而该常量此前只有**自指**断言（E-0024）。

        这里钉**字面值**：它必须等于 Task 3 播下的那个角色名，否则守卫会静默失效
        （`role.name != TENANT_ADMIN` 直接 return ⇒ 保护形同虚设）。
        """
        from apps.business.roles import TENANT_ADMIN

        self.assertEqual(TENANT_ADMIN, "系统管理员")
        self.assertTrue(Role.objects.filter(name="系统管理员").exists())
