"""给 `Role` 加「可作为销售客服」标记，并把内置「客服」「客服主管」置 `True`。

## 为什么要有这一列（业务方 2026-10-08 裁定）

录单页「接单销售客服」那一格（`#inputOrderKefu`）的候选源是
`apps/business/selectors.py::kefu_options`，它原先**写死两个角色名**
（`Q(groups__name__in=("客服", "客服主管"))`）。后果是：租户在「角色职位权限」页
**自建的角色**即使干的是客服的活，也永远进不了候选——只能改代码。改成读角色上的
`can_be_sales_agent` 标记后，租户自己勾一下就纳入。

## 为什么这一列默认 `False`，还要一条数据迁移

`default=False` 是**承重**的：它是「谁都不是销售客服」这个空前提出发点，任何没被显式置
`True` 的角色（含管理员新建的角色）都不进候选。

⚠️ 但**只加列不改数据**会**静默缩小**候选名单：存量租户的「客服」「客服主管」两个内置角色
会落在 `False` 上 ⇒ 录单页那一格从「两个角色的人」变成**只剩当前登录人**，
而**不会报任何错**（页面看得到的只是「下拉里少人了」）。所以本迁移必须**同一次**把这两个
内置角色置 `True`，让存量租户的候选名单与改动前**逐条相同**。

## 数据迁移的口径

- 匹配键是 `Role.name` 的**字面量** `("客服", "客服主管")`：迁移是冻结的历史记录，
  ⛔ 不 import `apps.rbac.seed`（那是应用代码、会漂移；同 `0002_grant_tenant_admin_group_to_superusers`
  内联 `TENANT_ADMIN_NAME` 的理由）。`tests/test_rbac_sales_agent_flag.py` 有一条对拍红线，
  内联副本与 `apps.rbac.seed.KEFU_CANDIDATE_ROLES` 分离时先红。
- ⚠️ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS`（`config/settings.py`）⇒ 本迁移
  **只在租户 schema 里跑**，`Role.objects` 拿到的就是当前租户那两行。
  ⛔ 不要写 `schema_context(...)` 去「确保在租户上跑」——那会从租户 schema 里跳出去。
- `filter(...).update(...)` 是**幂等**的：命中 0 行（全新 schema、或租户已删过这两个角色）
  也是成功。⚠️ 另有一个真实场景：`Role.name` 是**可改**的（管理员能把「客服」改名）
  ⇒ 那几个租户本迁移命中 0 行，**这是要的**（改名后的角色不该被猜着置上标记，
  否则就是把一个不再叫「客服」的职位静默变成候选源）。

## 反向迁移是 `noop`，这是**有意**的

反向往回置 `False` 会**误伤管理员**：`can_be_sales_agent` 一旦上线就可能在角色编辑页被租户
自己改过，而反向拿到的是一组「当前带这个标记的角色」——**无从区分「本迁移置的」与
「管理员自己勾的」**（同 `0002` 的 `backwards` 那条理由）。所以要回退就回退代码，
⛔ 不写一个会静默改掉真实配置的反向。
"""
from django.db import migrations, models

#: `apps.rbac.seed.KEFU_CANDIDATE_ROLES` 的**冻结副本**（理由见模块 docstring）。
#: ⚠️ 这与 `0002_grant_tenant_admin_group_to_superusers.TENANT_ADMIN_NAME` 是同一手法。
KEFU_CANDIDATE_ROLES = ("客服", "客服主管")


def backfill_builtin_kefu_roles(apps, schema_editor):
    """存量租户：内置「客服」「客服主管」置 `can_be_sales_agent=True`（幂等）。"""
    Role = apps.get_model("rbac", "Role")
    Role.objects.filter(name__in=KEFU_CANDIDATE_ROLES).update(can_be_sales_agent=True)


def noop_reverse(apps, schema_editor):
    """有意为空：反向无法区分「本迁移置的」与「管理员自己勾的」（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0004_remove_dropped_system_page_nodes"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="can_be_sales_agent",
            field=models.BooleanField(default=False, verbose_name="可作为销售客服"),
        ),
        # ⚠️ 回填**必须排在 `AddField` 之后**：列还不存在时那条 `update` 会抛
        #    `ProgrammingError`，迁移中途失败。由 `tests/test_rbac_sales_agent_flag.py`
        #    的下标断言钉住（照 `tests/test_p4_migration.py::test_runpython_backfill_precedes_*` 的形状）。
        migrations.RunPython(backfill_builtin_kefu_roles, noop_reverse),
    ]
