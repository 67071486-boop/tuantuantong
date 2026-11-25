"""把「靠 Django 开关的管理员」迁进「系统管理员」职位（2026-09-23 业务方裁定）。

背景：本线把「租户管理员」的判据从 `is_superuser` 改成「持有 `系统管理员` Group」。
⚠️ **不改数据的切换会立刻锁死**：实测开发库 `dev` 里唯一的超管**不属于任何组**
⇒ 切换那一刻全租户零管理员，而且**没有 API 能自救**（只能上服务器改库）。
所以本迁移必须**先于**判据切换上线（部署顺序见 `docs/RUNBOOK.md`）。

判据：`is_superuser=True` 的账号（**不筛 `is_active`**）⇒ 加进 `系统管理员` Group。
- ✅ 停用的超管**一并迁**（业务方 2026-09-23 裁定，原「停用不加」的拟口径作废）：旧判据
  `is_tenant_admin = bool(is_superuser)`（该表达式今天是 `public` 平面的那一条分支：
  `apps/core/permissions.py:78-79`）**完全不看 `is_active`**
  ⇒ 停用的超管本来就算管理员；本迁移的职责是「换判据后**身份不变**」，所以把**所有**超管
  迁进职位，不筛活跃。
- ⛔ 不动任何账号的 `is_superuser` / `is_active` 标志（本线只换**判据**，不删数据）。
- Group 用 `get_or_create`：`public` 平面没有 `apps.rbac` 的表，但本 app 只在 `TENANT_APPS`
  ⇒ 本迁移只在租户 schema 里跑；租户里那个 Group 由 `seed_rbac()` / `seed_tenant` 建过，
  这里 `get_or_create` 只是防御（若无同名 `Role`，该组不带来任何节点权限——
  **所以部署顺序必须先跑 `seed_rbac_catalog`**）。
  ⚠️ 只有确实存在超管时才建它：**没有任何超管就一行都不写**（见 `forwards` 的
  `if admins.exists()` 守卫与那里的实测理由）。

## 为什么用字面量 `"系统管理员"`

`"系统管理员"` 就是 `apps.business.roles.TENANT_ADMIN`（= 同模块的 `SYSTEM_ADMIN`）在
2026-09-23 的取值，**但本迁移不 import 它**：迁移是冻结的历史记录，而那个常量是应用代码、
会漂移——若将来它被改名，在一台全新数据库上重放本迁移会给出与今天不同的行，
历史就被静默改写了。改这个字面量需要一次新的数据修复迁移（同 `0004_backfill_from_auditlog`
的内联副本口径）。

## 反向 no-op（如实取舍，不假装可逆）

`backwards` **不摘组**。真正的理由与存量形状无关：`RunPython` 的反向拿到的是一组「当前挂在
这个组上的人」，**无从区分「本迁移加进来的行」与「业务方自己配的成员行」** ⇒ 任何自动摘除
都会误伤后者。所以这里如实声明**不可逆**；真要回退，回退的是判据代码
（`is_superuser` 分支仍在 `accounts` 那条线上），不是这批成员行。

⚠️ 「摘组会把管理员重新打成零管理员」这句**只在本次部署的 `dev` 形状下成立**（该库唯一的
超管不属于任何组 ⇒ 摘掉他就没有管理员了）；换个存量形状就未必，所以不拿它当理由（E-0011）。

⚠️ 仓里对同类不可逆还有另一种取舍：`apps/audit/migrations/0004_backfill_from_auditlog.py:89-93`
选的是**明确不可逆、让 `migrate` 拒绝反向**（那里 `RunPython(backfill)` 不传 `reverse_code`
⇒ 反向抛 `IrreversibleError`），理由是「写一个只会删真实数据的反向比明确不可逆更糟」。
本迁移选 no-op 是**计划的明确要求**（Task 1 Step 3），不是疏忽——代价如实说：反向会
「看起来成功、实际什么都没做」。
"""
from django.db import migrations

#: `apps.business.roles.TENANT_ADMIN` 的**冻结副本**（理由见模块 docstring）。
TENANT_ADMIN_NAME = "系统管理员"


def forwards(apps, schema_editor):
    """正向：**所有**超管 ⇒ 「系统管理员」组成员（不筛 `is_active`，理由见模块 docstring）。

    走 `apps.get_model` 的历史模型（不是 `django.contrib.auth.models` 的直接 import），
    这样本迁移在迁移图里独立可重放；`admin.groups.add(group)` 是幂等的
    （M2M 中间表有唯一约束，重复执行不会产生第二行）。

    ⚠️ **没有任何超管时一行都不写**（`if admins.exists()` 守卫）。理由：`get_or_create`
    无条件跑会在**每个** schema 里凭空造一个没有同名 `Role` 的裸 Group，而本迁移只负责
    「把存量管理员接上职位」——建目录/角色是 `seed_rbac_catalog` 的活。这不是"防御性写法"：
    通过守卫与「无超管」区分建组/加人，由
    `test_no_superuser_means_no_group_is_created_at_all` 钉住；把守卫改成 `if True:` 时它变红。
    去掉守卫，全套实测**5 条既有用例变红**（`tests/test_rbac_role_migration.py` 两条
    「dry-run 不写库」的 canary、`tests/test_rbac_services.py` 两条 `Group.objects.count()`
    绝对计数、`tests/test_account_api.py::test_role_derivation_actually_discriminates_orderings`
    那条「默认职位 pk 必须按 `DEFAULT_ROLES` 递增」的夹具前提）——都是夹具前提失效，
    不是放宽判据。
    """
    admins = apps.get_model("auth", "User").objects.filter(is_superuser=True)
    if admins.exists():
        group, _ = apps.get_model("auth", "Group").objects.get_or_create(
            name=TENANT_ADMIN_NAME
        )
        for admin in admins:
            admin.groups.add(group)


def backwards(apps, schema_editor):
    """反向 no-op——理由见模块 docstring「反向 no-op」一节。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            forwards,
            backwards,
        ),
    ]
