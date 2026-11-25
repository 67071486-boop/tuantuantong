"""存量角色迁移（spec §6.3 / §6.4 / §10-A10）。"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_catalog

#: 迁移命令的**五个**目标职位（旧五 Group → 新五角色，spec §6.3）。
#: ⚠️ 它**不是**「迁移后 Role 表的全集」：命令内部调 `seed_builtin_roles()`，
#: 而线 D之后内置角色多了一个「人事」（`BUILTIN_ROLE_MODULES`），
#: 所以迁移后还会多出它——那一条断言在 `test_apply_is_idempotent` 里显式并进来。
NEW = ("客服", "客服主管", "代练员", "系统管理员", "系统开发员")

#: 线 D 新加的内置职位。单独列出来是为了让「迁移命令顺带种出它」这件事在断言里可读
#: （⛔ 不是把 `NEW` 改宽：`NEW` 的语义是迁移映射的目标，不是库里的角色全集）。
HR = "人事"


def _codes(role):
    role.refresh_from_db()
    return set(role.permissions.values_list("code", flat=True))


class RoleMigrationTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        # 造出"旧世界"：五个旧 Group + 各自的成员
        self.old = {name: Group.objects.create(name=name) for name in
                    ("租户管理员", "打手", "客服", "财务", "审核人")}
        self.users = {}
        for name, group in self.old.items():
            user = get_user_model().objects.create_user(
                username=f"u-{name}", password="pw-123456"
            )
            user.groups.add(group)
            self.users[name] = user

    def _run(self, *args):
        call_command("migrate_rbac_roles", *args, verbosity=0)

    def test_dry_run_writes_nothing(self):
        self._run("--dry-run")
        self.assertTrue(Group.objects.filter(name="租户管理员").exists())
        self.assertFalse(Group.objects.filter(name="系统管理员").exists())

    def test_default_invocation_without_flags_writes_nothing(self):
        """**不传任何 flag** 时必须只演练（spec D13 的安全默认）。

        ⚠️ 既有那条 `test_dry_run_writes_nothing` 是**显式**传 `--dry-run` 的，
        所以它钉的是"这个 flag 有效"，不是"默认是演练"。默认一旦被翻成 apply，它会照样绿。
        """
        self._run()  # 一个参数都不传
        self.assertTrue(Group.objects.filter(name="租户管理员").exists())
        self.assertFalse(Group.objects.filter(name="系统管理员").exists())

    def test_apply_renames_and_merges(self):
        self._run("--apply")
        self.assertFalse(Group.objects.filter(name="租户管理员").exists())
        self.assertFalse(Group.objects.filter(name="打手").exists())
        self.assertFalse(Group.objects.filter(name="财务").exists())
        self.assertFalse(Group.objects.filter(name="审核人").exists())
        self.assertTrue(Group.objects.filter(name="系统管理员").exists())
        self.assertTrue(Group.objects.filter(name="代练员").exists())
        # 成员不丢：改名保成员，合并后原先的财务/审核人挂在客服上
        self.users["租户管理员"].refresh_from_db()
        self.assertEqual([g.name for g in self.users["租户管理员"].groups.all()], ["系统管理员"])
        self.users["财务"].refresh_from_db()
        self.assertEqual([g.name for g in self.users["财务"].groups.all()], ["客服"])
        # 审核人并入**客服主管**（C4 裁决）：并进客服会让状态机的 REVIEWER 失去持有人
        self.users["审核人"].refresh_from_db()
        self.assertEqual([g.name for g in self.users["审核人"].groups.all()], ["客服主管"])

    def test_apply_is_idempotent(self):
        self._run("--apply")
        self._run("--apply")
        self.assertEqual(Group.objects.filter(name="系统管理员").count(), 1)
        # ⚠️ 期望值含线 D新加的内置「人事」：本命令的 `_migrate()` 调
        # `seed_builtin_roles()`，它按 `BUILTIN_ROLE_MODULES` 建角色 ⇒ 迁移后
        # Role 表 = 五个迁移目标 ∪ {人事}。仍是**恰好集合**（多一个/少一个都红）。
        self.assertEqual(
            sorted(Role.objects.values_list("name", flat=True)),
            sorted(set(NEW) | {HR}),
        )

    def test_new_role_names_are_seeded_after_migration(self):
        self._run("--apply")
        for name in NEW:
            self.assertTrue(Group.objects.filter(name=name).exists(), name)

    def test_unknown_schema_is_a_loud_error_not_a_silent_success(self):
        """`--schema` 拼错时必须 `CommandError`（Task 12 终局评审 D4）。

        原实现过滤后为空 ⇒ `for` 循环零次 ⇒ **exit 0**，而本命令是部署顺序里的一步：
        运维会以为"迁移成功完成"，实际一个租户都没处理。零租户不是成功态，是输入错。
        """
        with self.assertRaises(CommandError):
            self._run("--schema", "rn-no-such-schema")

    def test_existing_schema_is_still_processed(self):
        """正向对照：命中真实 schema 时**不得**抛（否则上面那条可能由"带 --schema 一律抛"凑出来）。"""
        self._run("--schema", self.tenant.schema_name)
        self.assertTrue(Group.objects.filter(name="租户管理员").exists())  # dry-run 不写库

    # 代练员的交单点只写在种子元组里，只覆盖新建角色。没有第二套补授命令。
    def test_repeated_apply_keeps_custom_grants_on_an_existing_role(self):
        """重复 `--apply` **不抹掉**管理员给已存在角色手工加的授权（本仓的幂等承诺）。

        ⚠️ 本条原是本线 `_grant_extra_nodes` 那条用例的**后半**（它当时断言 `add()` 而不是
        `set()`）；补授机制删除后，「已存在角色不被重配」这条承诺仍是承重的——
        `seed_builtin_roles` 只对新角色 `set()`，若有人把它改成对已存在角色也 `set()`，
        管理员在界面上改过的授权会被静默回滚，而没有任何报错。
        """
        self._run("--apply")
        role = Role.objects.get(name="代练员")
        custom = PermissionNode.objects.get(code="finance.payroll")     # 管理员手工加的
        role.permissions.add(custom)
        self._run("--apply")
        self._run("--apply")
        self.assertIn("finance.payroll", _codes(role))
