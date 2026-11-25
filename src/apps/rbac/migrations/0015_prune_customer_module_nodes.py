"""裁剪客户维护组的 8 个无功能 / 已删功能点（2026-11-11 业务方拍板「收敛到 3 个页面」）。

## 为什么需要这个迁移

与 `0002_remove_pruned_nodes_and_rename_submit` / `0004_remove_dropped_system_page_nodes`
**同一条理由**：`seed_catalog()` 只 `update_or_create`、**从不删行** ⇒ 只改
`apps/rbac/catalog.py` 对**已有**租户是空操作，权限树里照样看得见这 8 个点，管理员还能继续
把它们勾给角色。漏跑的**症状不是报错**，而是「界面没变」。

## 这 8 个点是哪来的

业务方 2026-11-11 拍板（客户线第五批，`docs/plans/2026-11-11-customer-perm-src.md` §0.2）：
客户维护组只留 member_list / member_analysis_report / blacklist 三页；其余 8 个点
（余额明细 / 等级设置等无功能或页面已删的）从目录删除。它们**全是 `level=1`、原本
`available=False`**（页面已删或从未实现，位点登记只有 PENDING/无）：

| benchmark_id | code |
|---|---|
| 143 | `customer.export_phones` |
| 1211 | `customer.export_member_data` |
| 1224 | `customer.add_edit_member` |
| 1225 | `customer.member_level_settings` |
| 1251 | `customer.delete_member` |
| 1252 | `customer.adjust_member_balance` |
| 1254 | `customer.member_balance_details` |
| 1282 | `customer.export_fund_details` |

⚠️ **相邻的东西一个都不许删**：保留的 3 个点（`customer.blacklist`(138) /
`customer.member_list`(141) / `customer.member_analysis_report`(1226)）有各自的
v2 页面与端点在用。`tests/test_rbac_catalog_migration.py` 把删面钉成**恰好这 8 个 code**。

## 反向迁移是 `noop`（口径与 `0002` / `0004` 一致）

重建这 8 个点需要同时重建角色上的分配，而那份信息已随中间表行被删——不许写假装能恢复的
reverse。要恢复请从 `docs/JIDANPU-PERMISSION-CATALOG.md` 基线重建并重跑 `seed_catalog`。

## 部署窗口（同 `0004`）

`apps.rbac` 在 `TENANT_APPS` ⇒ 本迁移随 `migrate_schemas --tenant` 逐租户库跑；
漏跑的症状是「界面没变」、不报错。
"""
from django.db import migrations

#: 与 `apps/rbac/catalog.py` 的本次裁剪同步（8 条）。「这些对标 id」的权威字面量在
#: `tests/test_rbac_structure.py::PRUNED_BENCHMARK_IDS`；`(benchmark_id, code)` 映射与
#: id 侧对拍在 `tests/test_rbac_catalog_migration.py::DROPPED_CUSTOMER_NODES`（等号）。
#: 按 **code** 删（`0004` 同款：code 是对外稳定标识、自种子之日起没变过）。
PRUNED_CODES = (
    "customer.export_phones",
    "customer.export_member_data",
    "customer.add_edit_member",
    "customer.member_level_settings",
    "customer.delete_member",
    "customer.adjust_member_balance",
    "customer.member_balance_details",
    "customer.export_fund_details",
)


def remove_pruned_customer_nodes(apps, schema_editor):
    """删这 8 行；`Role.permissions` 的隐式 through 表带 CASCADE ⇒ 授权行随点一起走。

    幂等（可重跑、可在新建租户的空表上跑）：命中 0 行也是成功。
    """
    Node = apps.get_model("rbac", "PermissionNode")
    Node.objects.filter(code__in=PRUNED_CODES).delete()


def noop_reverse(apps, schema_editor):
    """有意为空：重建需要重建角色分配，信息已丢（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0014_add_order_platform_write_node"),
    ]

    operations = [
        migrations.RunPython(remove_pruned_customer_nodes, noop_reverse),
    ]
