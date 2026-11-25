"""给**存量租户**补上自有限制节点 `customer.member_list_self_only`（客户线第五批，2026-11-11）。

## 为什么需要这个迁移

与 `0006` / `0007` / `0008` / `0010` / `0011` / `0012` / `0013` / `0014`
**同一条理由**：`seed_catalog()` 只 upsert、**不是发版流程的一步** ⇒ 只改
`apps/rbac/catalog.py` 对**已有**租户是空操作：权限树上看不到这个点，管理员没法把它勾给
「只做录单」之类的职位。漏跑的症状不是 500、不是报错，而是「**界面没变**」——
限制权限收不到人（功能对该租户等于不存在）。

## 口径（与 `0006` / `0013` / `0014` 逐条相同）

1. **幂等**：`get_or_create(code=…)` 可重跑、可在新建租户的空表上跑（新建租户由
   `seed_catalog` 自己建行，`code` 唯一 ⇒ 两路互不冲突）。
2. ⛔ **只补不改 + 一行角色授权都不碰**：限制权限要不要发给哪个职位是管理员的
   业务决定（与 `0006` 涉钱写动作那批「自动补授」**不同**——本点是收窄可见范围的
   限制，默认谁都不给才是安全态）。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 只在租户 schema 里跑。

## 建的是什么（与 `catalog.NODES` 那一行逐字同值）

`benchmark_id=None`（**第十条自有节点**）/ `kind=flag` / `level=1` / `available=True` /
`url=""`（flag 不落 URL，与对标 flag 同一口径）。`name` / `note` 是**冻结副本**
（迁移是历史记录，⛔ 不 import `apps.rbac.catalog`）。

每跑一个租户 schema 打印一行：`[rbac.0016] <schema>: 新建节点 0/1`。
"""
from django.db import connection, migrations

#: 自有节点的 code（**唯一**匹配键）。
SELF_ONLY_CODE = "customer.member_list_self_only"

#: 它挂在哪个栏目下（`customer` 栏目行）。
MODULE_CODE = "customer"

#: 与 `apps.rbac.catalog.NODES` 那一行的**冻结副本**（理由见模块 docstring）。
NODE_DEFAULTS = {
    "benchmark_id": None,
    "name": "只看自己录单的客户",
    "note": (
        "勾选后客户管理列表只显示录单人为自己的系统订单所连的客户；"
        "与会员管理权限任一即可进入页面，只持本点看不到全部客户"
    ),
    "url": "",
    "kind": "flag",
    "level": 1,
    "order_by": 0,
    "available": True,
}


def _schema_label() -> str:
    """迁移日志里的 schema 名（django-tenants 把它挂在 connection 上）。"""
    return getattr(connection, "schema_name", "") or "public"


def add_customer_member_list_self_only_node(apps, schema_editor):
    """建点（幂等）；⛔ 不动任何角色授权（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")

    defaults = dict(NODE_DEFAULTS)
    parent = Node.objects.filter(code=MODULE_CODE).first()
    if parent is not None:
        defaults["parent"] = parent
    _, created = Node.objects.get_or_create(code=SELF_ONLY_CODE, defaults=defaults)
    print(f"[rbac.0016] {_schema_label()}: 新建节点 {1 if created else 0}")


def noop_reverse(apps, schema_editor):
    """有意为空：反向删点会 CASCADE 掉租户已勾好的授权。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0015_prune_customer_module_nodes"),
    ]

    operations = [
        migrations.RunPython(add_customer_member_list_self_only_node, noop_reverse),
    ]
