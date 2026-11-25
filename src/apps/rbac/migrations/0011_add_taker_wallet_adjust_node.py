"""给**存量租户**补上自有节点 `finance.taker_wallet_adjust`（客服后台打手钱包管理线，2026-10-20）。

## 为什么需要这个迁移

与 `0006_add_taker_open_api_node` / `0007_add_system_edit_settings_node` /
`0008_add_home_qr_manage_node` / `0008_add_qqbot_settings_node` /
`0010_add_taker_withdraw_review_node` **同一条理由**（那几条的 docstring 已写明）：
`seed_catalog()` 只 upsert、**不是发版流程的一步**（`deploy/activate.sh` 里只有
`migrate_schemas`）⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是**空操作**：
角色编辑页看不到这个点，管理员没法把它勾给「客服主管」之类的职位。漏跑的症状不是 500、
不是报错，而是「**界面没变**」——钱包列表 / 调账 / 押金规则四个端点全部回 403。

⭐ 首选就是这条数据迁移（不依赖人的记性）：随 `migrate_schemas --tenant` 自动落到
每台租户机的每个租户 schema 上。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `finance.taker_wallet_adjust` |
| `benchmark_id` | `None` —— 本仓**第六条**自有节点（对标物里没有打手钱包管理 / 按游戏改押金） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（`url` 落我方自己的端点路径） |
| `kind` / `level` / `available` | `api` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0006` / `0010` 的手法）。

## 口径（与 `0006` / `0007` / `0008` / `0010` 逐条相同）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ 一个字段都不动（租户可能改过显示名）。
   ⛔⭐ **不碰任何角色授权**：「谁能勾」由管理员在角色编辑页决定。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 本迁移只在租户 schema 里跑。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0006` / `0007` / `0008` / `0010` 一致）

反向要删点会经 `Role.permissions` 的 through 表 CASCADE 掉租户已勾好的授权 ⇒
⛔ 不写一个假装能恢复的 reverse。
"""
from django.db import migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
WALLET_ADJUST_CODE = "finance.taker_wallet_adjust"

#: 它挂在哪个栏目下（`finance` 栏目行）。
MODULE_CODE = "finance"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "打手钱包与押金管理",
    "note": "打手钱包管理：查看余额 / 押金冻结、手工加减款；按游戏维护押金金额（影响谁能接单）",
    "url": "/api/business/wallets/",
    "kind": "api",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def add_taker_wallet_adjust_node(apps, schema_editor):
    """按 `code` 建点（幂等）；已存在 ⇒ 一个字段都不改（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    Node.objects.get_or_create(code=WALLET_ADJUST_CODE, defaults=defaults)


def noop_reverse(apps, schema_editor):
    """有意为空：反向会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0010_add_taker_withdraw_review_node"),
    ]

    operations = [
        migrations.RunPython(add_taker_wallet_adjust_node, noop_reverse),
    ]
