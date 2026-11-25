"""把两条并行线的 `rbac` 迁移收成一个叶子（Django 要求每个 app 只有一个 leaf）。

## 为什么需要它（合并窗口实测，2026-09-23）

`feat/rbac-consolidation`（本线）与 `feat/rbac-admin-predicate`（另一条线，已落 `main`）
**各自**在 `apps/rbac/migrations/` 下加了一个 `0002_*`，且都只依赖 `0001_initial`：

- 本线 `0002_remove_pruned_nodes_and_rename_submit`（裁 43 点 + 对标 105 改造）；
- 另一线 `0002_grant_tenant_admin_group_to_superusers`（把 Django 管理员迁进「系统管理员」职位）。

⇒ 图里出现**两个叶子**，Django 直接拒绝跑测试与 `migrate`：

    CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph:
    (0002_grant_tenant_admin_group_to_superusers, 0002_remove_pruned_nodes_and_rename_submit in rbac).

⛔ **不要**用别的办法绕（改名成 `0003_*`、或删掉其中一条）：本地开发库 `tuantuantong` 的
`dev` schema 里**两条都已应用**（`SELECT name FROM dev.django_migrations WHERE app='rbac'`
实测 = `0001_initial` / `0002_remove_pruned_nodes_and_rename_submit` /
`0002_grant_tenant_admin_group_to_superusers`）⇒ 改名会留下「库里记着、图上没有」的孤儿记录，
且要让操作员多跑一次等价的迁移。

本迁移**没有任何操作**，只把两个叶子收成一个：存量租户 `migrate` 时它会被记一行、其余不动。
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0002_grant_tenant_admin_group_to_superusers"),
        ("rbac", "0002_remove_pruned_nodes_and_rename_submit"),
    ]

    operations = []
