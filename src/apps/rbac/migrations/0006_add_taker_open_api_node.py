"""给**存量租户**补上自有节点 `system.allow_taker_open_api`（T4，打手侧开放接口）。

## 为什么需要这个迁移

与 `0002_remove_pruned_nodes_and_rename_submit` / `0004_remove_dropped_system_page_nodes`
**同一条理由**（那两条的 docstring 已写明）：`seed_catalog()` 只 upsert、**从不删行**，
而它**今天也不是发版流程的一步**（`deploy/activate.sh` 里只有 `migrate_schemas`；
`seed_rbac_catalog` 是手工命令）⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是**空操作**：
权限树里**看不见**这个点，管理员也就没法把它勾给任何角色。
漏跑的**症状不是 500、不是报错**，而是「**界面没变**」（打手进不来「打手对接」页）。

⭐ 按 spec §7.2 的裁定，**首选就是这条数据迁移**（不依赖人的记性）：它随
`migrate_schemas --tenant` 自动落到**每台租户机**的每个租户 schema 上。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `system.allow_taker_open_api` |
| `benchmark_id` | `None` —— ⭐ **本仓第一条自有节点**（对标物里没有它） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（spec §7.1） |
| `kind` / `level` / `available` | `page` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0005` 内联 `KEFU_CANDIDATE_ROLES` 的手法）。
`tests/test_rbac_catalog_migration.py::TakerOpenApiNodeTests` 有一条**对拍红线**：
内联副本与 `apps.rbac.catalog.NODES` 里那一行分离时先红。

## 口径（三条，逐条给出理由）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ **一个字段都不动**（`get_or_create` 的语义）。
   理由：这个点一旦进库，租户就可能在角色编辑页勾过它、管理员也可能改过它的显示名；
   迁移按 `code` 再写一遍 = 把真实配置**静默回滚**。
   ⛔⭐ **尤其不碰任何角色授权**：本迁移**一行 `Role.permissions` 都不写**
   （`PermissionNode` 这一侧只建点；"谁能看到 / 谁能勾"由管理员在角色编辑页决定）。
   ⚠️ 与 `seed_catalog` 的分工因此是清楚的：**存在性**由本迁移保证、**新值传播**由重灌保证。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS`（`config/settings.py`）⇒
   本迁移**只在租户 schema 里跑**，`PermissionNode.objects` 拿到的就是当前租户那一份。
   ⛔ 不要写 `schema_context(...)` 去「确保在租户上跑」——那会从租户 schema 里跳出去
   （同 `0005:27-29` 的理由）。

⚠️ **默认谁能拿到它**：`seed_rbac` 对「系统管理员」给**全部**节点 ⇒ 重灌之后租户管理员
自动拥有本点；**打手那条职位不会自动拿到**（要老板自己在角色编辑页勾）。
这一句同时登记在 `docs/RUNBOOK.md` 与 spec §7.2。

## 反向迁移是 `noop`，这是**有意**的（口径与 `0002` / `0004` 一致）

反向要删掉这个点**同时**会经 `Role.permissions` 的 through 表 CASCADE 掉租户已经勾好的授权，
而那份信息**无从区分**「本迁移建的」与「管理员后来自己勾的」⇒
⛔ 不写一个假装能恢复的 reverse（那种 reverse 会静默丢掉授权，比没有更糟）。
"""
from django.db import migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
TAKER_OPEN_API_CODE = "system.allow_taker_open_api"

#: 它挂在哪个栏目下（对标点里那个 `system` 栏目行）。
MODULE_CODE = "system"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（spec §7.1；理由见模块 docstring）。
#: ⚠️ `benchmark_id` 是 `None`（自有节点），所以它**不能**当匹配键——
#: `filter(benchmark_id=None)` 是 `isnull` 查找，第二条自有节点一进来就会
#: `MultipleObjectsReturned`（同 `apps/rbac/seed.py::seed_catalog` 那条 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "允许打手使用开发平台应用",
    "note": "打手可进入「打手对接」页并为自己创建一套接入凭据",
    "url": "/open-platform/taker-apps",
    "kind": "page",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def add_taker_open_api_node(apps, schema_editor):
    """按 `code` 建点（幂等）；已存在 ⇒ 一个字段都不改（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    Node.objects.get_or_create(code=TAKER_OPEN_API_CODE, defaults=defaults)


def noop_reverse(apps, schema_editor):
    """有意为空：反向会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0005_role_can_be_sales_agent"),
    ]

    operations = [
        migrations.RunPython(add_taker_open_api_node, noop_reverse),
    ]
