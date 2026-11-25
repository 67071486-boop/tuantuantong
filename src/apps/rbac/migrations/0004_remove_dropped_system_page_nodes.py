"""裁剪 4 个「已下线系统设置页」的目录点（`catalog.NODES` 154 → 150）。

## 为什么需要这个迁移

与 `0002_remove_pruned_nodes_and_rename_submit` **同一条理由**（那条的 docstring 已写明）：
`seed_catalog()` 只 `update_or_create`、**从不删行**（`src/apps/rbac/seed.py`）
⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是**空操作**，权限树里照样看得见这 4 个点，
管理员还能继续把它们勾给角色。漏跑的**症状不是 500、不是报错**，而是「界面没变」。

## 这 4 个点是哪来的

业务方 2026-09-23 裁定下线 4 个纯前端照抄页（录单修正设置 / 订单标签维护 /
微信公众号设置 / 淘宝店对接设置，见
`docs/specs/2026-09-23-order-recognition-settings-design.md` §10）⇒ 4 个 RBAC 权限点
随之从目录删除。它们**全都是 `level=1` 的 `page` 点、且原本 `available=False`**
（「功能未实现：前端 1:1 渲染页、后端无模型/端点」）：

| benchmark_id | code | 页面 |
|---|---|---|
| 123 | `system.wechat_settings` | `/System/WechartSetting` |
| 134 | `system.taobao_ship_settings` | `/System/TaobaoShipSetting` |
| 1257 | `system.order_labels` | `/System/OrderLabelList` |
| 1271 | `system.order_correction_settings` | `/System/VerifiAddOrderKey` |

⚠️ **相邻的东西一个都不许删**（spec §10.2）：`order.taobao_ship_records`（1227，淘宝发货记录页）、
`order.update_tags`（1258，改/清除订单标签的 API 点）、`system.pinduoduo_settings`（1212，
其它平台店铺对接）。`tests/test_rbac_catalog_migration.py` 有专门的用例把那三条钉成活口
（删宽了会红）。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0002` 一致）

重建这 4 个点需要**同时**重建角色上的分配，而那份信息已经丢了（中间表行随点一起被删）。
要恢复请从 `docs/JIDANPU-PERMISSION-CATALOG.md` 的基线重建目录并重跑 `seed_catalog`——
**不许**写一个假装能恢复的 reverse：那种 reverse 会重建点、静默丢掉授权，比没有更糟。

## 两处指路（独立评审 2026-09-23 建议补，纯文档可发现性）

1. **删除面还有第二条级联路径**：`PermissionNode.parent` 是**指向自身**的外键
   （`0002` 的 docstring 已写明这层放大面）。本文件删的 4 条都是 `level=1`、无子点，
   所以这次只走 `Role.permissions` 那条 through 表的 CASCADE；**将来若裁有子点的节点**，
   父级级联会连带删掉整棵子树，必须重新量影响面。
2. **共享开发库要在部署窗口跑**：本迁移**不会**随代码合并自动落到已有租户 schema 上
   （`seed_catalog` 只 upsert、不删行）⇒ 漏跑的**症状是「界面没变」**、不报错。
   影响面与部署命令登记在
   `docs/specs/2026-09-23-order-recognition-settings-design.md` §16.5。
"""
from django.db import migrations

#: 与 `apps/rbac/catalog.py` 的本次裁剪同步（4 条，spec §10）。
#: 「这 4 条对标 id」的权威字面量在 `tests/test_rbac_structure.py::PRUNED_BENCHMARK_IDS`；
#: `(benchmark_id, code)` 的字面映射与 id 侧对拍在
#: `tests/test_rbac_catalog_migration.py::DROPPED_PAGE_NODES`（**等号**，不是包含）。
#: 本文件按 **code** 删——存量租户里这些行的 code 自种子之日起没变过，而 code 是本项目对外稳定标识。
PRUNED_CODES = (
    "system.wechat_settings",
    "system.taobao_ship_settings",
    "system.order_labels",
    "system.order_correction_settings",
)


def remove_dropped_system_page_nodes(apps, schema_editor):
    """删这 4 行；`Role.permissions` 的隐式 through 表带 CASCADE ⇒ 授权行随点一起走。

    幂等（可重跑、可在新建租户的空表上跑）：`filter(...).delete()` 命中 0 行也是成功。
    """
    Node = apps.get_model("rbac", "PermissionNode")
    Node.objects.filter(code__in=PRUNED_CODES).delete()


def noop_reverse(apps, schema_editor):
    """有意为空：重建这 4 个点需要重建角色分配，信息已丢（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0003_merge_20260923_1430"),
    ]

    operations = [
        migrations.RunPython(remove_dropped_system_page_nodes, noop_reverse),
    ]
