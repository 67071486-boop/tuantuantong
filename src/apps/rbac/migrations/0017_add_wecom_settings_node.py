"""给**存量租户**补上自有节点 `system.wecom_settings`（企微自建应用线，D-0379）。

## 为什么需要这个迁移

与 `0008_add_qqbot_settings_node` **同一条理由**（那条的 docstring 已写明）：
`seed_catalog()` 只 upsert、**不是发版流程的一步** ⇒ 只改 `apps/rbac/catalog.py` 对
**已有**租户是**空操作**：角色编辑页看不到这个点，管理员没法把它勾给
「系统管理员」以外的角色。漏跑的症状是「企微设置页的端点全部回 403」。

⭐ 首选就是这条数据迁移：随 `migrate_schemas --tenant` 自动落到每台租户机的每个租户 schema 上。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `system.wecom_settings` |
| `benchmark_id` | `None` —— 本仓**第十一条**自有节点（对标物里没有企业微信对接这一项） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（`url` 落我方 v2 路由；本页 v2-only） |
| `kind` / `level` / `available` | `page` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0008` 的手法）。

## 口径（与 `0008` 逐条相同）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ 一个字段都不动（租户可能改过显示名）。
   ⛔⭐ **不碰任何角色授权**：「谁能勾」由管理员在角色编辑页决定。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 本迁移只在租户 schema 里跑。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0008` 一致）

反向要删点会经 `Role.permissions` 的 through 表 CASCADE 掉租户已勾好的授权 ⇒
⛔ 不写一个假装能恢复的 reverse。
"""
from django.db import migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
WECOM_SETTINGS_CODE = "system.wecom_settings"

#: 它挂在哪个栏目下（`system` 栏目行）。
MODULE_CODE = "system"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "企微自建应用设置",
    "note": "企微自建应用：应用凭据、回调验证、通讯录与客户联系只读拉取、测试发消息、消息台账",
    "url": "/v2/wecom-settings",
    "kind": "page",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def add_wecom_settings_node(apps, schema_editor):
    """按 `code` 建点（幂等）；已存在 ⇒ 一个字段都不改（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    Node.objects.get_or_create(code=WECOM_SETTINGS_CODE, defaults=defaults)


def noop_reverse(apps, schema_editor):
    """有意为空：反向会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0016_add_customer_member_list_self_only_node"),
    ]

    operations = [
        migrations.RunPython(add_wecom_settings_node, noop_reverse),
    ]
