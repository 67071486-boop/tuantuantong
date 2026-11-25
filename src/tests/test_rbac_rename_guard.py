"""改名路径的「最后管理员」保护（spec §6.5 / §10-A5；Task 12 终局评审 A1）与锁序（B4）。

**缺陷形状**（评审证据）：`views.update` → `services.rename_role` 全程不取
`lock_tenant_admin_mutations()`、也不做管理员算术，而管理员判据是
`groups__name=TENANT_ADMIN`（`accounts/guards.py::_admin_filter`），`rename_role` 改的正是
**`Group` 行本身**。可达链：停用 superuser（账号线允许，只要还有别的管理员）→
剩下的非 superuser 管理员 PATCH 改名 ⇒ **零管理员**（不可自救）。

⚠️ **两条方向都要钉**：
- **离开** `系统管理员`：该组活跃成员当场失去管理员身份；
- **进入** `系统管理员`：该组活跃成员**一次性变成管理员** = 提权。

⚠️ 夹具的硬约束（前几轮踩过，照 `test_rbac_delete_guard.py` 的口径）：
调用者必须是**非管理员**（`_admin_filter()` = 持该 Group；2026-09-23 判据统一后**不再**
看 `is_superuser`——把登录账号放进该组会让它自己贡献 1 个管理员，"只剩一个"的算术就不成立；
原来这里写的是 `is_superuser` ∪ 持该 Group，随本线作废）；
一律走 `make_logged_in_client`（`force_login` 的会话没有 `identity_plane` ⇒ 401）；
`Role.name` 是 `varchar(5)`，夹具名必须短。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_builtin_roles, seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/role-authorities/"
#: 调用者用的**非管理员**角色名（持编辑点，但不计入管理员）。`varchar(5)`：5 个字。
CALLER_ROLE = "改角色权"
ADMIN_ROLE = "系统管理员"
#: 改名目标（不与任何内置角色/Group 撞名）。
FREE_NAME = "总管"


def _is_admin_arithmetic(statement):
    """这条 SQL 是不是「还剩几个活跃管理员」的算术。

    判据取「`COUNT(` + `auth_user`」：本路径上只有 `active_admin_count()` 与守卫里的
    `dependent` 两个聚合查询满足；`validate_name` / `rename_role` 的存在性检查是
    `SELECT 1 … LIMIT 1`，不含 `COUNT`。
    """
    upper = statement.upper()
    return "COUNT(" in upper and "AUTH_USER" in upper


class _RenameGuardFixture(TenantTestCase):
    """四个验收用例与锁序用例共用的夹具（非管理员调用者 + 可显式造管理员）。"""

    def setUp(self):
        seed_catalog()
        seed_builtin_roles()
        self.client = make_logged_in_client(username="rn-caller", password="pw-123456")
        caller = get_user_model().objects.get(username="rn-caller")
        role = Role.objects.create(
            name=CALLER_ROLE, group=Group.objects.create(name=CALLER_ROLE)
        )
        role.permissions.set(
            PermissionNode.objects.filter(
                code__in=(
                    "system.role_authority_edit",
                    # 锁序用例也走删除路径（两条写路径共用同一个守卫），所以两个点都要给。
                    "system.role_authority_delete",
                )
            )
        )
        caller.groups.add(role.group)

    def _admin_member(self, username):
        """造一个持「系统管理员」角色的活跃账号（`create_superuser` 不进任何 Group）。"""
        user = get_user_model().objects.create_user(
            username=username, password="pw-123456"
        )
        user.groups.add(Group.objects.get(name=ADMIN_ROLE))
        return user

    def _superuser(self, username):
        user = get_user_model().objects.create_user(
            username=username, password="pw-123456"
        )
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
        return user

    def _rename(self, role, name):
        return self.client.patch(
            f"{BASE}{role.pk}/", {"name": name}, content_type="application/json"
        )


class RenameGuardTests(_RenameGuardFixture):
    """A1 的验收（2026-09-23 口径变更后）：离开恒被拒 / 提权被拒 / 空角色改名进来放行 /
    非管理员角色不受影响。⚠️ 原第二条是"有 superuser 幸存放行"——新口径下裸超管不算
    管理员，那条路作废（见 `test_renaming_away_is_refused_even_when_a_bare_superuser_exists`）。"""

    def test_renaming_away_the_last_admin_role_is_refused(self):
        """① 唯一管理员非 superuser ⇒ 改名离开 `系统管理员` 被拒，且**行未变**。

        判别力：去掉 `views.update` 里那处守卫调用，本用例红（改名被提交、返回 code 0）。
        """
        self._admin_member("rn-only")
        role = Role.objects.get(name=ADMIN_ROLE)
        response = self._rename(role, FREE_NAME)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000, response.json())
        role.refresh_from_db()
        self.assertEqual(role.name, ADMIN_ROLE, "被拒的改名却改了角色行")
        self.assertEqual(role.group.name, ADMIN_ROLE, "被拒的改名却改了 Group 行")

    def test_renaming_away_is_refused_even_when_a_bare_superuser_exists(self):
        """**反例变正例（2026-09-23 判据统一）**：`_superuser("rn-su")` 只置 `is_superuser`、
        不进组 ⇒ 新口径下**不是**管理员，不再是与角色脱钩的"幸存者"。

        ⚠️ 原文（`test_renaming_away_is_allowed_when_a_superuser_survives`）断言"有 superuser
        幸存 ⇒ 放行"，依据是旧 `_admin_filter = is_superuser ∪ 该 Group`。新口径下管理员
        恰好就是该 Group 的成员 ⇒ `active_admin_count() - dependent` 恒为 0 ⇒ 必须拒。

        ⚠️ 与 `test_renaming_away_the_last_admin_role_is_refused` 的区别：那条的成员是
        **非超管**；本条显式证明**超管标志也不豁免**。两边的判别力都由其它用例补齐：
        "改名对一个空角色仍然放行"见 `test_renaming_an_empty_role_into_the_admin_role_is_allowed`
        的 entering 方向；"非管理员角色不受影响"见下一条。
        """
        self._superuser("rn-su")
        self._admin_member("rn-admin")
        role = Role.objects.get(name=ADMIN_ROLE)
        response = self._rename(role, FREE_NAME)
        self.assertEqual(response.json()["code"], 1000, response.json())
        role.refresh_from_db()
        self.assertEqual(role.name, ADMIN_ROLE, "被拒的改名却改了角色行")
        self.assertEqual(role.group.name, ADMIN_ROLE, "被拒的改名却改了 Group 行")

    def test_renaming_into_the_admin_role_with_members_is_refused(self):
        """③ 改名为 `系统管理员` 且该 Group 有活跃成员 ⇒ 拒绝（防提权）。

        构造：先删掉「系统管理员」的 **Group**（级联删同名 `Role`，否则名字被占、
        序列化器先拒），再给「客服」加两个活跃成员。判别力：去掉守卫调用后本用例红
        ——两个客服成员会一次性变成管理员。
        """
        Group.objects.get(name=ADMIN_ROLE).delete()
        self.assertFalse(Role.objects.filter(name=ADMIN_ROLE).exists(), "构造物不成立")
        for username in ("rn-m1", "rn-m2"):
            member = get_user_model().objects.create_user(
                username=username, password="pw-123456"
            )
            member.groups.add(Group.objects.get(name="客服"))
        service = Role.objects.get(name="客服")
        response = self._rename(service, ADMIN_ROLE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000, response.json())
        service.refresh_from_db()
        self.assertEqual(service.name, "客服", "被拒的提权改名却落了库")

    def test_renaming_an_empty_role_into_the_admin_role_is_allowed(self):
        """③ 的正向对照：**空**角色改名进来放行（不授予任何人）。

        没有它，"提权被拒"可能由"改名为系统管理员一律拒"凑出来——而
        「系统管理员」角色被删掉之后，重建它正是这条正常路径。
        """
        Group.objects.get(name=ADMIN_ROLE).delete()
        empty = Role.objects.create(
            name="空角色", group=Group.objects.create(name="空角色")
        )
        response = self._rename(empty, ADMIN_ROLE)
        self.assertEqual(response.json()["code"], 0, response.json())
        empty.refresh_from_db()
        self.assertEqual(empty.name, ADMIN_ROLE)

    def test_renaming_a_non_admin_role_is_unaffected(self):
        """④ 非管理员角色改名不受影响，也**不取** advisory 锁（锁序要求越少路径取它越好）。

        「不取锁」这条是有断言的，不是顺口：`views.update` 只在旧名或新名沾到
        `TENANT_ADMIN` 时才调守卫，让不改管理员集合的改名排队是纯代价。
        """
        member = get_user_model().objects.create_user(
            username="rn-kefu", password="pw-123456"
        )
        member.groups.add(Group.objects.get(name="客服"))
        service = Role.objects.get(name="客服")
        with CaptureQueriesContext(connection) as captured:
            response = self._rename(service, "客服A")
        self.assertEqual(response.json()["code"], 0, response.json())
        service.refresh_from_db()
        self.assertEqual(service.name, "客服A")
        self.assertFalse(
            [s for s in (e["sql"] for e in captured.captured_queries) if "pg_advisory_xact_lock" in s],
            "不改管理员集合的改名也取了 advisory 锁——锁序要求越少路径取它越好",
        )


class AdminRoleWriteLockOrderTests(_RenameGuardFixture):
    """B4：advisory 锁必须出现在**管理员算术之前**（单连接可观察的锁序判据）。

    ⚠️ **这不是并发测试**。真正的并发判别力（两条真实连接同时改/删管理员角色）本轮
    **未覆盖**，已明确登记在 `task-final-report.md` 的未做/未覆盖表里。
    本类钉的是"锁真的被取了、且顺序正确"——去掉
    `guards.ensure_not_last_admin_role` 里的 `lock_tenant_admin_mutations()` 会让两条都红。
    """

    def _capture_delete(self, role):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.delete(f"{BASE}{role.pk}/")
        return response, [entry["sql"] for entry in captured.captured_queries]

    def _capture_rename(self, role, name):
        with CaptureQueriesContext(connection) as captured:
            response = self._rename(role, name)
        return response, [entry["sql"] for entry in captured.captured_queries]

    def _assert_lock_precedes_arithmetic(self, sql):
        advisory = [i for i, s in enumerate(sql) if "pg_advisory_xact_lock" in s]
        arithmetic = [i for i, s in enumerate(sql) if _is_admin_arithmetic(s)]
        # 先自证两条语句都真的出现过——否则下面的 `assertLess` 会由"某一边压根没发出"
        # 而 vacuous 通过（取索引会 IndexError，但这里先把意图说清）。
        self.assertTrue(advisory, f"这条请求没有取 advisory 锁：{sql}")
        self.assertTrue(arithmetic, f"这条请求没有管理员算术（COUNT over auth_user）：{sql}")
        self.assertLess(
            advisory[0],
            arithmetic[0],
            "advisory 锁发在管理员算术之后了——不变式没有承重点",
        )

    def test_delete_takes_the_advisory_lock_before_the_admin_arithmetic(self):
        """删除路径：唯一管理员依赖该角色 ⇒ 被拒，但锁与算术都已发生且顺序正确。"""
        self._admin_member("lo-del-only")
        response, sql = self._capture_delete(Role.objects.get(name=ADMIN_ROLE))
        self.assertEqual(response.json()["code"], 1000)
        self._assert_lock_precedes_arithmetic(sql)

    def test_rename_takes_the_advisory_lock_before_the_admin_arithmetic(self):
        """改名路径（A1 新接的写路径）：同样要求锁在算术之前。"""
        self._admin_member("lo-rn-only")
        response, sql = self._capture_rename(Role.objects.get(name=ADMIN_ROLE), FREE_NAME)
        self.assertEqual(response.json()["code"], 1000)
        self._assert_lock_precedes_arithmetic(sql)
