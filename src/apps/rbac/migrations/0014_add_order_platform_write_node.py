"""给**存量租户**补上自有节点 `order.platform_write`（交易平台涉钱写动作线，2026-10-28）。

## 为什么需要这个迁移

与 `0006_add_taker_open_api_node` / `0007_add_system_edit_settings_node` /
`0008_add_home_qr_manage_node` / `0008_add_qqbot_settings_node` /
`0010_add_taker_withdraw_review_node` / `0011_add_taker_wallet_adjust_node` /
`0012_add_xgj_receipt_detail_node` / `0013_add_system_article_manage_node`
**同一条理由**：`seed_catalog()` 只 upsert、**不是发版流程的一步**
（`deploy/activate.sh` 里只有 `migrate_schemas`）⇒ 只改 `apps/rbac/catalog.py` 对
**已有**租户是**空操作**：角色编辑页看不到这个点，管理员没法把它勾给「客服」之类的职位。
漏跑的症状不是 500、不是报错，而是「**界面没变**」——交易平台的写端点会全部回 403
（读端点照旧，因为它们是另一个节点 `order.list`）。

## ⭐ 本迁移比前几条多一步：**自动补授给已有 `order.list` 的角色**

只建点还不够。这批写端点的门从 `order.list` 换成了 `order.platform_write`，
而**存量租户的角色没有一个持有新节点** ⇒ 上线瞬间客服的平台动作（撤回 / 改价 /
确认完单 / 重发…）**全部 403**，等于把现有客服锁死。

⇒ 本迁移在**同一趟**里做第二件事：把 `order.platform_write` 补授给
**当前已拥有 `order.list` 的每个角色**（逐角色、幂等 `add`）。这样上线当天行为与今天
完全一致（「能看订单的人能操作平台写动作」），后续业务方再在角色编辑页按需收紧。

口径边界（⛔ 别读成「谁都给」）：

- 只补**已经持有 `order.list`** 的角色；没有它的角色（如「人事」「代练员」）**一个都不给**；
- **不新建角色 / 不改角色名 / 不动 `order.list` 之外的任何授权**；
- 反向迁移仍是 `noop`：撤回授权应该由管理员在页面做，⛔ 不写一个假装能恢复的反向。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

| 列 | 值 |
|---|---|
| `code` | `order.platform_write` |
| `benchmark_id` | `None` —— 本仓**第九条**自有节点（对标物里没有交易平台对接这一整套） |
| `name` / `note` / `url` | 见 `NODE_DEFAULTS`（`url` 落我方自己的端点路径） |
| `kind` / `level` / `available` | `api` / `1` / `True` |

⚠️ `name` / `note` / `url` 三处是**冻结副本**（迁移是历史记录，⛔ 不 import
`apps.rbac.catalog`——那是应用代码、会漂移；同 `0006` / `0013` 的手法）。

## 口径（建点部分与 `0006` / `0013` 逐条相同）

1. **幂等**：`get_or_create(code=…)` + M2M `add` 可重跑、可在新建租户的空表上跑。
2. ⛔ **只补不改**：`code` 已存在 ⇒ 节点行一个字段都不动（租户可能改过显示名）。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 本迁移只在租户 schema 里跑。

## 影响行数（写进迁移日志）

每跑一个租户 schema 打印一行：`[rbac.0014] <schema>: 新建节点 0/1 · 补授角色 N 个`。
N = 本次**新拿到**该节点的角色数（幂等重跑时为 0）。
"""
from django.db import connection, migrations

#: 自有节点的 code（**唯一**匹配键；`code` 是唯一列，天然一条）。
PLATFORM_WRITE_CODE = "order.platform_write"

#: 它挂在哪个栏目下（`order` 栏目行）。
MODULE_CODE = "order"

#: 「自动补授」的判据节点：已有这个点的角色，说明它本来就能做平台写动作
#: （写端点改门之前挂的就是它）⇒ 上线当天不该被新门锁死。
GRANT_ANCHOR_CODE = "order.list"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "交易平台涉钱写操作",
    "note": (
        "对交易平台订单执行涉钱 / 写动作（确认完单、同意撤销、补款、重发、加价改价、"
        "编辑、撤回、模板维护等）的提交请求；只读查询仍用订单列表权限"
    ),
    "url": "/api/business/orders/<order_no>/platform-push/",
    "kind": "api",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def _schema_label() -> str:
    """迁移日志里的 schema 名（django-tenants 把它挂在 connection 上）。"""
    return getattr(connection, "schema_name", "") or "public"


def add_order_platform_write_node(apps, schema_editor):
    """建点（幂等）+ 给已有 `order.list` 的角色补授；打印影响行数（见模块 docstring）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    Role = apps.get_model("rbac", "Role")

    defaults = dict(NODE_DEFAULTS)
    # ⚠️ 父级（栏目行）**存在就挂上**；不存在（极端情形：目录还没灌过）就留 `None`，
    #    ⛔ 不在这里顺手建栏目行——那是 `seed_catalog` 的职责，两个地方都建会各自漂。
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    node, created = Node.objects.get_or_create(
        code=PLATFORM_WRITE_CODE, defaults=defaults
    )

    granted = 0
    anchor = Node.objects.filter(code=GRANT_ANCHOR_CODE).first()
    if anchor is not None:
        # `permissions=anchor` = 经 through 表精确匹配；⛔ 不按角色名猜（角色可自建 / 改名）。
        roles = Role.objects.filter(permissions=anchor).distinct()
        for role in roles:
            if node.pk not in set(role.permissions.values_list("pk", flat=True)):
                role.permissions.add(node)
                granted += 1

    print(
        f"[rbac.0014] {_schema_label()}: 新建节点 {1 if created else 0} · "
        f"补授角色 {granted} 个"
    )


def noop_reverse(apps, schema_editor):
    """有意为空：反向删节点会 CASCADE 掉租户已勾好的授权（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0013_add_system_article_manage_node"),
    ]

    operations = [
        migrations.RunPython(add_order_platform_write_node, noop_reverse),
    ]
