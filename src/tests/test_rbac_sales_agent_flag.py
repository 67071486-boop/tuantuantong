"""`Role.can_be_sales_agent`（「可作为销售客服」标记）与它的**迁移路径**。

**背景**（业务方裁定）：录单页「接单销售客服」那一格的候选源
（`apps/business/selectors.py::kefu_options`）原先**写死两个角色名**
（`Q(groups__name__in=("客服", "客服主管"))`）⇒ 租户自建的角色永远进不了候选。
改成读 `Role.can_be_sales_agent` 之后，**有两条路**必须把内置那两个角色置上：

1. **存量租户** —— 数据迁移 `apps/rbac/migrations/0005_role_can_be_sales_agent.py`
   （漏跑的症状**不是报错**：那一格从「两个角色的人」静默变成「只剩当前登录人」）；
2. **新建租户** —— `apps/rbac/seed.py::seed_builtin_roles`（`provision_tenant` 的建库路径）。

⚠️ 两条路**各有一条用例**：只测一条时，另一条写坏了不会有任何信号——它们改的是两处代码
（迁移文件 vs 种子函数），而且**测试库建库时迁移已经跑过**（`TenantTestCase` 的 schema
是从模板克隆的），所以「种子这条绿」**不构成**「迁移那条也对」的任何保证，反之亦然。

⚠️ 候选口径本身的行为用例在 `tests/test_order_kefu_options.py`（端点载荷、阳性 / 阴性 /
当前登录人恒在）；本文件只管**这一列与它的两条写入路径**。
"""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.db import migrations
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.platform.provisioning import provision_tenant
from apps.rbac.models import Role
from apps.rbac.seed import KEFU_CANDIDATE_ROLES, seed_builtin_roles
from apps.rbac.services import rename_role

#: 本线新增的迁移（模块路径；`_migration_module()` 里 import）。
MIGRATION = "apps.rbac.migrations.0005_role_can_be_sales_agent"

#: 改动前 `kefu_options` 里那份**写死的角色名**（冻结字面量）。
#: ⚠️ 它今天是**历史值**：`apps/business/selectors.py` 里已经没有这个名字了
#: （见 `test_the_constant_that_hardcoded_the_names_is_gone`）。
FORMER_HARDCODED_NAMES = ("客服", "客服主管")


def _migration_module():
    return importlib.import_module(MIGRATION)


class RoleSalesAgentFieldTests(TenantTestCase):
    """模型那一列本身：默认值必须是 `False`，且**不是**任何形式的默认 `True`。"""

    def test_default_is_false(self):
        """⚠️ `default=False` 是**承重**的，不是装饰。

        改成 `True` 的后果：**每个角色**（含管理员新建的）都进候选
        ⇒ 「按标记取」退化成「按有没有职位取」，阴性用例随之失效。
        ⚠️ 这里钉的是**列上的默认值**（`get_field(...).default`）与**建行后的取值**两件事：
        只钉后者的话，`null=True` + 应用层兜底那类实现会漏过去。
        """
        self.assertIs(Role._meta.get_field("can_be_sales_agent").default, False)
        role = Role.objects.create(name="默认值", group=Group.objects.create(name="默认值"))
        role.refresh_from_db()
        self.assertIs(role.can_be_sales_agent, False)

    def test_verbose_name_and_type(self):
        """列名（业务方在角色编辑页看到的那行字）与类型：`BooleanField`、中文名照抄需求原话。"""
        field = Role._meta.get_field("can_be_sales_agent")
        self.assertEqual(field.verbose_name, "可作为销售客服")
        self.assertEqual(field.get_internal_type(), "BooleanField")


class BuiltinRolesAreMarkedByTheSeedTests(TenantTestCase):
    """**新建租户**那条路径（`seed_builtin_roles`）。"""

    def test_seed_marks_the_two_kefu_roles_and_no_others(self):
        seed_builtin_roles()
        marked = set(
            Role.objects.filter(can_be_sales_agent=True).values_list("name", flat=True)
        )
        # 恰好集合：多一个（例如「代练员」被顺手置上）也是错——那会放大候选名单。
        self.assertEqual(marked, {"客服", "客服主管"})

    def test_a_second_seed_does_not_flip_back_an_admin_decision(self):
        """幂等的**第二层**：管理员在角色编辑页**取消勾选**后，种子不许把它翻回来。

        ⚠️ 这与「已存在的角色不重配权限」是**同一条边界**（`if not was_created: continue`）：
        权限那半边有人守（`test_rbac_seed.py` 的两条），标记这半边今天由本用例守。
        ⛔ 别为了「让存量租户也带上标记」把标记那一句挪到 `was_created` 判断之外——
        那会让管理员取消勾选这个动作在下次种子后**静默复活**，而权限那半边仍是尊重的，
        两半就互相矛盾了（存量租户的补法只有数据迁移那一次）。
        """
        seed_builtin_roles()
        Role.objects.filter(name="客服").update(can_be_sales_agent=False)

        seed_builtin_roles()

        self.assertIs(Role.objects.get(name="客服").can_be_sales_agent, False)

    def test_provisioned_tenant_has_the_two_builtin_roles_marked(self):
        """端到端（新建租户那条路真的接上了）：`provision_tenant` 出来的租户里那两个角色带标记。

        ⚠️ 这条**不能**由上面两条代替：它们自己显式调了 `seed_builtin_roles()`
        ⇒ 把 `provisioning.py` 里那两行 `seed_rbac()` 删掉，它们照样绿
        （`test_rbac_seed.py` 的 `SeedTenantWiringTests` 对「目录与角色被灌」有同样的顾虑）。
        """
        tenant, _ = provision_tenant(
            schema_name="rbac_sales_agent_wire",
            name="销售客服标记",
            domain="rbac-sales-agent-wire.test.local",
        )
        with tenant_context(tenant):
            marked = set(
                Role.objects.filter(can_be_sales_agent=True).values_list(
                    "name", flat=True
                )
            )
            self.assertEqual(marked, {"客服", "客服主管"})


class BuiltinRolesAreMarkedByTheMigrationTests(TenantTestCase):
    """**存量租户**那条路径（数据迁移 `0005` 的 `RunPython`）。

    ⚠️ **测试库建库时这条迁移已经跑过** ⇒ 「迁移前的状态」在本用例里并不存在，
    必须自己造（把标记压回 `False`，再跑一次迁移函数）——这是数据迁移用例最容易写错的地方
    （同 `tests/test_rbac_catalog_migration.py` 的 `_reset_to_pre_migration_state`）。
    ⚠️ 这里调的是**迁移模块里那个函数**、不是生产代码：迁移是冻结的历史记录，
    它的行为只能由它自己那份代码决定。
    """

    def _backfill(self):
        return _migration_module().backfill_builtin_kefu_roles(django_apps, None)

    def test_it_marks_the_two_builtin_roles_of_an_existing_tenant(self):
        seed_builtin_roles()
        # 造「迁移前」的状态：那两个角色还没有标记。
        Role.objects.update(can_be_sales_agent=False)
        self.assertEqual(
            Role.objects.filter(can_be_sales_agent=True).count(), 0, "构造物不成立"
        )

        self._backfill()

        self.assertEqual(
            set(
                Role.objects.filter(can_be_sales_agent=True).values_list(
                    "name", flat=True
                )
            ),
            {"客服", "客服主管"},
        )

    def test_it_does_not_touch_a_renamed_role_that_merely_used_to_be_kefu(self):
        """**反向的边界**：角色被管理员**改过名**时，迁移**不猜**、⛔ 不置标记。

        依据：`Role.name` 是可改的（改名走生产路径 `apps/rbac/services.py::rename_role`，
        角色与同名 Group **一起**改名）⇒ 迁移按**名字字面量**匹配，「曾经叫客服、现在叫别的」
        那个角色命中 0 行。这是**要的**——照别的线索猜（例如「有 order 栏目的角色」）
        会把一个不再叫客服的职位静默变成候选源。
        """
        seed_builtin_roles()
        Role.objects.update(can_be_sales_agent=False)
        # ⚠️ 走**真实改名路径**（不是 `Role.objects.filter(...).update(name=…)`）：
        #    只改 Role 那一行会造出「Role 与 Group 不同名」的形状，那不是管理员能做到的事。
        rename_role(Role.objects.get(name="客服"), name="白班", remark="")
        self.assertFalse(Group.objects.filter(name="客服").exists(), "构造物不成立：组没跟着改名")

        self._backfill()

        self.assertIs(Role.objects.get(name="白班").can_be_sales_agent, False)
        self.assertIs(Role.objects.get(name="客服主管").can_be_sales_agent, True)
        # 非空哨兵：这条迁移**确实改到了行**（否则上面那条会因「什么都没做」假绿）。
        self.assertEqual(Role.objects.filter(can_be_sales_agent=True).count(), 1)

    def test_it_is_idempotent_and_runs_on_an_empty_schema(self):
        """幂等 + 空表可跑（全新 schema、或租户把这两个角色都删过 ⇒ 命中 0 行也是成功）。"""
        self.assertEqual(Role.objects.count(), 0, "构造物不成立：本用例要空表")
        self._backfill()
        self._backfill()
        self.assertEqual(Role.objects.count(), 0)


class SalesAgentMigrationStructureTests(SimpleTestCase):
    """迁移**接线**的结构钉子（照 `tests/test_rbac_admin_migration.py` / `test_p4_migration.py` 的形状）。

    ⚠️ **判别力**：把 `RunPython` 从 `operations` 里删掉，上面那些行为用例**全绿**
    （它们直接调函数，根本不经过 `operations`）⇒「函数写对了」不等于「迁移被接上了」。
    """

    def test_backfill_is_wired_once_after_the_addfield(self):
        mod = _migration_module()
        ops = list(mod.Migration.operations)
        # ① `AddField` 存在且**严格排在**回填之前：列还不存在时 `update` 会
        #    `ProgrammingError`（迁移中途失败 ⇒ 存量租户半新半旧）。
        add_at = [
            i
            for i, op in enumerate(ops)
            if isinstance(op, migrations.AddField) and op.name == "can_be_sales_agent"
        ]
        self.assertEqual(len(add_at), 1, f"0005 应当恰有 1 个 AddField，实际 {len(add_at)} 个")
        run_at = [i for i, op in enumerate(ops) if isinstance(op, migrations.RunPython)]
        self.assertEqual(len(run_at), 1, f"0005 应当恰有 1 个 RunPython，实际 {len(run_at)} 个")
        self.assertLess(add_at[0], run_at[0], "回填必须排在 AddField 之后")
        # ② 那一条回填**就是本迁移的回填函数**（防止塞一个空 RunPython 骗过 ①）
        self.assertIs(ops[run_at[0]].code, mod.backfill_builtin_kefu_roles)

    def test_the_reverse_is_a_deliberate_noop(self):
        """反向是 `noop`（**有意**）：反向拿到的是一组「当前带标记的角色」，
        **无从区分「本迁移置的」与「管理员自己勾的」** ⇒ 任何自动回退都会误伤后者。

        ⚠️ 判据钉在「**不是 `RunPython.noop` 之外的任何东西**」上：把反向换成
        `Role.objects.update(can_be_sales_agent=False)` 时本条红。
        """
        mod = _migration_module()
        run_python = [
            op for op in mod.Migration.operations if isinstance(op, migrations.RunPython)
        ][0]
        self.assertIs(run_python.reverse_code, mod.noop_reverse)
        self.assertIsNot(run_python.reverse_code, migrations.RunPython.noop)

    def test_the_inlined_role_names_match_the_seed_constant(self):
        """**内联副本对拍红线**：迁移里的角色名字面量与 `apps.rbac.seed.KEFU_CANDIDATE_ROLES`
        必须逐字相同（迁移⛔ 不 import 应用代码——它是冻结的历史记录，同 `0002` 的 `TENANT_ADMIN_NAME`）。

        判别力：只改种子那一侧（例如把默认标记挪到别的角色）而不动迁移 ⇒ 两条写入路径
        对「默认该是谁」给出不同答案，而**没有任何行为用例**会同时看到两边 ⇒ 本条先红。
        ⚠️ 两侧都钉**字面值**（E-0024）：若写成「`migration.常量 == seed.常量`」，
        两边一起被改坏时它照样绿；下面第一条把「默认该是谁」钉成历史字面量。
        """
        self.assertEqual(FORMER_HARDCODED_NAMES, ("客服", "客服主管"))
        self.assertEqual(tuple(KEFU_CANDIDATE_ROLES), FORMER_HARDCODED_NAMES)
        self.assertEqual(
            tuple(_migration_module().KEFU_CANDIDATE_ROLES), FORMER_HARDCODED_NAMES
        )

    def test_it_depends_on_the_previous_leaf(self):
        """依赖必须是**上一个叶子**（`0004`）；写错会让迁移图出现两个叶子 ⇒ `makemigrations` 报冲突。"""
        self.assertEqual(
            [dep for dep in _migration_module().Migration.dependencies],
            [("rbac", "0004_remove_dropped_system_page_nodes")],
        )

    def test_the_constant_that_hardcoded_the_names_is_gone(self):
        """改动前那份「写死的角色名」常量不许留在 `selectors.py` 里（**半新半旧**判据）。

        ⚠️ 判据搜的是**被废除的主词本身**（`KEFU_CANDIDATE_ROLE_NAMES`），⛔ 不是它派生出的
        几个短语（§5-C / E-0011）：留着一个没人用的常量，下一个实现者会照它改、
        得到「改了没变」的结果。
        ⚠️ 只扫 `selectors.py` 里那份**常量定义**：`("客服", "客服主管")` 这两个名字在别处
        （种子常量、迁移、本文件的字面量）**是**要留的。
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "apps" / "business" / "selectors.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "KEFU_CANDIDATE_ROLE_NAMES",
            source,
            "改动前的写死常量还留在 selectors.py 里（⛔ 有引用就改注释、没引用就整个删掉）",
        )
        # 非空哨兵：确认读的**就是**那份文件（读到空串时上面的断言恒真）。
        self.assertIn("def kefu_options(", source)
