"""数据迁移探针：把每个租户的 Django 管理员迁进「系统管理员」职位。

用法：直接用 `importlib.import_module` 取迁移模块（模块名以数字开头，`import` 语句写不了），
再调 `forwards(real_apps, None)`——传**真实** app registry 是有意的：迁移体内部走
`apps.get_model("auth", ...)`，真实 registry 拿到的当前模型与历史模型在这两张表上字段一致。

⚠️ 行为断言一律钉**字面值**（`"系统管理员"` / `["系统管理员"]`），不 import
`apps.business.roles.TENANT_ADMIN` 当期望值：E-0024——拿导出常量当期望值时，常量被改坏
两边一起变、断言照绿，判别力为零。（唯一例外是 `TenantAdminNameLiteralDriftTests`：那里
**故意**把内联副本与 app 常量对拍，钉的是「两者一致」这条关系，不是「值等于某个常量」。）

⚠️ 这里测的是**迁移函数本身**，不是「schema 建起来时迁移有没有被 apply」；
后者由 django-tenants 的数据库迁移链保证（本文件的用例就活在跑完全部迁移的租户 schema 上）。
而「函数有没有被挂进 `operations`」是另一回事，由 `RbacAdminMigrationStructureTests` 单独钉
（评审 B2：只调函数测不出 `operations = []`）。
"""
import importlib

import django.apps
from django.contrib.auth.models import Group, User
from django.db import migrations
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.roles import TENANT_ADMIN
from apps.rbac.models import Role
from apps.rbac.permissions import user_node_codes
from apps.rbac.seed import seed_rbac

#: 迁移模块路径（编号以 `ls src/apps/rbac/migrations/` 当场取）。
MIGRATION_MODULE = "apps.rbac.migrations.0002_grant_tenant_admin_group_to_superusers"


def _migration_module():
    return importlib.import_module(MIGRATION_MODULE)


class RbacAdminMigrationTests(TenantTestCase):
    """行为用例：① 超管进组 ② 幂等 ③ 不动非超管 ④ 停用超管**也**进
    ⑤ 无超管不建组 ⑥ 无同名 `Role` 时 0 个节点权限。
    """

    def _forwards(self):
        """跑一次正向迁移。模块不存在时 `ModuleNotFoundError` ⇒ 用例 ERROR（红）。"""
        _migration_module().forwards(django.apps.apps, None)

    def test_active_superuser_without_any_group_lands_in_tenant_admin(self):
        """① 建组 + 加人：`is_superuser=True` 且不在任何组 ⇒ 迁完在「系统管理员」组里。"""
        admin = User.objects.create_user(
            username="mig_active_admin", password="pw-not-used", is_superuser=True
        )
        self.assertEqual(list(admin.groups.values_list("name", flat=True)), [])

        self._forwards()

        self.assertEqual(
            list(admin.groups.values_list("name", flat=True)), ["系统管理员"]
        )

    def test_running_twice_adds_no_duplicate_membership_row(self):
        """② 幂等：连跑两次不报错，成员行恰好 1 条（不是「1 个组名」这种去重后的视角）。"""
        admin = User.objects.create_user(
            username="mig_idempotent_admin", password="pw-not-used", is_superuser=True
        )

        self._forwards()
        self._forwards()

        group = Group.objects.get(name="系统管理员")
        self.assertEqual(group.user_set.filter(pk=admin.pk).count(), 1)
        # 直接数中间表：`values_list("name")` 会被 M2M 去重，重复行在那里看不见。
        self.assertEqual(admin.groups.through.objects.filter(user=admin).count(), 1)

    def test_non_superuser_keeps_only_its_own_group(self):
        """③ 反例：`is_superuser=False` 且属于「客服主管」⇒ 迁完仍只属于「客服主管」。"""
        clerk = User.objects.create_user(username="mig_clerk", password="pw-not-used")
        clerk.groups.add(Group.objects.create(name="客服主管"))

        self._forwards()

        self.assertEqual(
            list(clerk.groups.values_list("name", flat=True)), ["客服主管"]
        )

    def test_inactive_superuser_is_also_granted(self):
        """④ 停用的超管也进「系统管理员」组。

        旧判据 `is_tenant_admin = bool(is_superuser)` 不看 `is_active`，停用的超管本来就算管理员。
        迁移要换判据后身份不变，所以所有超管都进组。这是有意的口径，不是把断言改松。
        """
        retired = User.objects.create_user(
            username="mig_inactive_admin",
            password="pw-not-used",
            is_superuser=True,
            is_active=False,
        )

        self._forwards()

        self.assertEqual(
            list(retired.groups.values_list("name", flat=True)), ["系统管理员"]
        )

    def test_no_superuser_means_no_group_is_created_at_all(self):
        """⑤ 评审 B1：租户里**一个超管都没有** ⇒ 迁移连 `系统管理员` Group 都不建。

        判据必须打在**组是否存在**上：只断言「组里没人」区分不了「没建组」与「建了个空组」
        ——把 `if admins.exists():` 改成 `if True:` 时，组会被建出来而没人加，成员数仍是 0，
        那种判据照样绿（评审 B1 的原始发现）。
        """
        User.objects.create_user(username="mig_no_admin_1", password="pw-not-used")
        User.objects.create_user(username="mig_no_admin_2", password="pw-not-used")

        self._forwards()

        self.assertFalse(
            Group.objects.filter(name="系统管理员").exists(),
            "没有任何超管时不该凭空建出一个没有成员的裸 Group",
        )
        self.assertEqual(Group.objects.count(), 0)

    def test_granted_admin_gets_no_node_code_until_the_role_is_seeded(self):
        """⑥ 评审 B3：**部署承重结论**——只有 Group、没有同名 `Role` ⇒ 管理员 0 个节点权限。

        `user_node_codes` 经 `roles__group__in=user.groups` 取节点，`/api/auth/me`
        的菜单就是它。省掉种子会让管理员菜单全空。
        所以这里钉字面数字（种子前 0、种子后全目录），不是「为空 / 不为空」。
        """
        admin = User.objects.create_user(
            username="mig_nodes_admin", password="pw-not-used", is_superuser=True
        )

        self._forwards()
        self.assertEqual(
            list(admin.groups.values_list("name", flat=True)), ["系统管理员"]
        )
        self.assertFalse(Role.objects.filter(name="系统管理员").exists())
        self.assertEqual(
            list(user_node_codes(admin)), [], "没有同名 Role 时不该拿到任何节点权限"
        )

        seed_rbac()

        self.assertEqual(
            Role.objects.get(name="系统管理员").permissions.count(),
            # ⭐ T4：146 → **147**（自有节点 `system.allow_taker_open_api`
            # 是**非 flag** 点，「系统管理员」按 `None` 拿全目录 ⇒ 非 flag 142 → 143）。
            # ⭐ 首页二维码横幅线（D-0262）：148 → **149**（第三条自有节点
            # `home.qr_manage` 同样是非 flag 点、进全目录）。
            # ⭐ QQ 机器人线：149 → **150**（第四条自有节点
            # `system.qqbot_settings`，非 flag）。
            # ⭐ 打手钱包 / 提现审核线：150 → **151**（第五条自有节点
            # `finance.taker_withdraw_review`，非 flag、进全目录）。
            #    （第六条 `finance.taker_wallet_adjust` 的 151 → 152 只改了字面量、未补注记。）
            # ⭐ 闲管家收款明细线：152 → **153**（第七条自有节点
            # `finance.xgj_receipt_detail`，非 flag、进全目录）。
            # ⭐ 频道文章作品线：153 → **154**（第八条自有节点
            # `system.article_manage`，非 flag、进全目录）。
            # ⭐ 交易平台涉钱写动作线（审计 M5）：154 → **155**（第九条自有节点
            # `order.platform_write`，非 flag、进全目录）。
            # ⭐ 客户维护组收敛线（2026-11-11）：155 → **147**（裁掉的 8 个客户维护点
            # 全是非 flag、本来就在批发内；同批新增的限制节点 `customer.member_list_self_only`
            # 是 FLAG、不在「系统管理员」默认批发内 ⇒ 只减 8）。
            # ⭐ 企微自建应用线（D-0379）：147 → **148**（第十一条自有节点
            # `system.wecom_settings` 是非 flag 的 PAGE 点、进全目录）。
            # 审批中心新增 4 个非 flag 权限点。
            152,
            "「系统管理员」的默认权限数变了——若这是有意加 flag，本断言要一并更新",
        )
        # ⭐ 系统开关线（D-0223）：147 → **148**（第二条自有节点进目录）。
        # ⭐ 首页二维码横幅线（D-0262）：148 → **149**（第三条自有节点进目录）。
        # ⭐ QQ 机器人线：149 → **150**（第四条自有节点进目录）。
        # ⭐ 打手钱包 / 提现审核线：150 → **151**（第五条自有节点进目录）。
        #    （第六条 `finance.taker_wallet_adjust` 的 151 → 152 只改了字面量、未补注记。）
        # ⭐ 闲管家收款明细线：152 → **153**（第七条自有节点进目录）。
        # ⭐ 频道文章作品线：153 → **154**（第八条自有节点进目录）。
        # ⭐ 交易平台涉钱写动作线（审计 M5）：154 → **155**（第九条自有节点进目录）。
        # ⭐ 客户维护组收敛线（2026-11-11）：155 → **147**（裁 8 个非 flag 点；新自有点是 FLAG、不进批发）。
        # ⭐ 企微自建应用线（D-0379）：147 → **148**（非 flag 的 PAGE 点进全目录）。
        self.assertEqual(len(user_node_codes(admin)), 152)


class RbacAdminMigrationStructureTests(SimpleTestCase):
    """评审 B2：迁移**接线**的结构钉子（照 `tests/test_p4_migration.py:61-107` 的形状）。

    判别力（评审 B2 的原始发现）：把 `operations` 换成 `[]`，上面六条行为用例**全绿**
    ——它们直接调 `forwards`，根本不经过 `operations`。所以「函数写对了」不等于
    「这个迁移被接上了」。
    """

    def test_exactly_one_runpython_wired_to_forwards_and_backwards(self):
        mod = _migration_module()
        run_python = [
            op for op in mod.Migration.operations if isinstance(op, migrations.RunPython)
        ]
        self.assertEqual(
            len(run_python),
            1,
            f"0002 应当恰有 1 个 RunPython，实际 {len(run_python)} 个（迁移被改坏了？）",
        )
        self.assertIs(run_python[0].code, mod.forwards)
        self.assertIs(run_python[0].reverse_code, mod.backwards)


class TenantAdminNameLiteralDriftTests(SimpleTestCase):
    """评审 B4：迁移**有意内联**的职位名与 app 常量的对拍红线。

    迁移不能 import `apps.business.roles`（它是冻结的历史记录，理由见迁移 docstring），
    代价是内联副本可能与 app 常量分离。这个代价不靠「记得同步」的纪律兜底，而是靠本红线：
    `TENANT_ADMIN` 哪天改名，这里先红，逼人们显式决定「历史要不要改（要改就得再来一条
    数据修复迁移）」。同类先例：`tests/test_operation_log_migration.py` 的
    `InlineCopiesDriftTests`。
    ⚠️ 在**测试**里 import app 代码没问题；自包含约束只针对迁移文件本身。
    """

    def test_inlined_role_name_matches_the_app_constant(self):
        # 两边都钉字面值：app 常量变了、或内联副本变了，都会在这里红。
        self.assertEqual(TENANT_ADMIN, "系统管理员")
        self.assertEqual(_migration_module().TENANT_ADMIN_NAME, TENANT_ADMIN)
