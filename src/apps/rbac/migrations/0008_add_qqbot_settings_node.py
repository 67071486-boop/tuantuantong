"""给**存量租户**补上自有节点 `system.qqbot_settings`（QQ 机器人线批次B，2026-10-19）。

## 为什么需要这个迁移

与 `0006_add_taker_open_api_node` / `0007_add_system_edit_settings_node` **同一条理由**
（那两条的 docstring 已写明）：`seed_catalog()` 只 upsert、**不是发版流程的一步**
（`deploy/activate.sh` 里只有 `migrate_schemas`）⇒ 只改 `apps/rbac/catalog.py` 对
**已有**租户是**空操作**：角色编辑页看不到这个点，管理员没法把它勾给
「系统管理员」以外的角色。漏跑的症状不是 500、不是报错，而是「**界面没变**」
（QQ 机器人页的端点全部回 403）。

⭐ 首选就是这条数据迁移（不依赖人的记性）：随 `migrate_schemas --tenant` 自动落到
每台租户机的每个租户 schema 上。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `system.qqbot_settings` |
| `benchmark_id` | `None` —— 本仓**第三条**自有节点（对标物里没有机器人对接这一项） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（`url` 自拟，照 `system` 栏目设置页的 `/System/…` 形态） |
| `kind` / `level` / `available` | `page` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0006` 的手法）。

## 口径（与 `0006` / `0007` 逐条相同）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ 一个字段都不动（租户可能改过显示名）。
   ⛔⭐ **不碰任何角色授权**：「谁能勾」由管理员在角色编辑页决定。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 本迁移只在租户 schema 里跑。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0006` / `0007` 一致）

反向要删点会经 `Role.permissions` 的 through 表 CASCADE 掉租户已勾好的授权 ⇒
⛔ 不写一个假装能恢复的 reverse。
"""
from django.db import migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
QQBOT_SETTINGS_CODE = "system.qqbot_settings"

#: 它挂在哪个栏目下（`system` 栏目行）。
MODULE_CODE = "system"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "QQ 机器人设置",
    "note": "QQ 机器人设置：机器人密钥、群与推送配置、入群审批、菜单",
    "url": "/System/QQBotSetting",
    "kind": "page",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def add_qqbot_settings_node(apps, schema_editor):
    """按 `code` 建点（幂等）；已存在 ⇒ 一个字段都不改（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    Node.objects.get_or_create(code=QQBOT_SETTINGS_CODE, defaults=defaults)


def noop_reverse(apps, schema_editor):
    """有意为空：反向会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0007_add_system_edit_settings_node"),
    ]

    operations = [
        migrations.RunPython(add_qqbot_settings_node, noop_reverse),
    ]
