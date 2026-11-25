"""给**存量租户**补上自有节点 `system.edit_settings`（系统开关线，D-0223，2026-10-10）。

## 为什么需要这个迁移

与 `0006_add_taker_open_api_node` **同一条理由**（那条的 docstring 已写明）：
`seed_catalog()` 只 upsert、**不是发版流程的一步**（`deploy/activate.sh` 里只有
`migrate_schemas`）⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是**空操作**：
角色编辑页看不到这个点，管理员没法把它勾给「系统管理员」以外的角色。
漏跑的症状不是 500、不是报错，而是「**界面没变**」（改开关回 403）。

⭐ 首选就是这条数据迁移（不依赖人的记性）：随 `migrate_schemas --tenant` 自动落到
每台租户机的每个租户 schema 上。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `system.edit_settings` |
| `benchmark_id` | `None` —— 本仓**第二条**自有节点（对标清单文档里没有这个保存端点） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（`url` = 对标实测端点 `/SystemAjax/SystemOrderSetting`） |
| `kind` / `level` / `available` | `api` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0006` 的手法）。

## 口径（与 `0006` 逐条相同）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ 一个字段都不动（租户可能改过显示名）。
   ⛔⭐ **不碰任何角色授权**：「谁能勾」由管理员在角色编辑页决定。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 本迁移只在租户 schema 里跑。

⚠️ 同一批还有另一件事**不归本迁移管**：`system.settings`(91) 的 `available`
在 `catalog.py` 翻成 `True`——那是「有没有消费方」的登记位（`has_node` 不看它，
`apps/rbac/permissions.py`），存量租户库里的旧值由重灌目录传播（同此前历次翻真的口径）。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0006` 一致）

反向要删点会经 `Role.permissions` 的 through 表 CASCADE 掉租户已勾好的授权 ⇒
⛔ 不写一个假装能恢复的 reverse。
"""
from django.db import migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
SYSTEM_EDIT_SETTINGS_CODE = "system.edit_settings"

#: 它挂在哪个栏目下（`system` 栏目行）。
MODULE_CODE = "system"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "编辑系统设置",
    "note": "编辑系统设置里的开关（保存设置）",
    "url": "/SystemAjax/SystemOrderSetting",
    "kind": "api",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def add_system_edit_settings_node(apps, schema_editor):
    """按 `code` 建点（幂等）；已存在 ⇒ 一个字段都不改（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    Node.objects.get_or_create(code=SYSTEM_EDIT_SETTINGS_CODE, defaults=defaults)


def noop_reverse(apps, schema_editor):
    """有意为空：反向会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0006_add_taker_open_api_node"),
    ]

    operations = [
        migrations.RunPython(add_system_edit_settings_node, noop_reverse),
    ]
