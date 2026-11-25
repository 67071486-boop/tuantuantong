"""把客户两点的改名与翻真（`name` / `note` / `available`）传播进**存量租户**（2026-11-11）。

## 为什么需要这个迁移

与 `0002` / `0015` / `0016` **同一条理由**：`seed_catalog()` 只在种目录时跑、**不是发版
流程的一步** ⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是空操作：权限树里仍显示旧名
「会员管理 / 会员分析报表」、`available=False` 仍弹「尚未开放」——业务方 2026-11-11 要
真实授权，翻真落不进存量库就是阻塞项。漏跑的症状不是 500、不是报错，而是「**界面没变**」。

## 口径（与 `0006` / `0016` 逐条相同）

1. **幂等**：`update_or_create(code=…)` 可重跑、可在新建租户的空表上跑（新建租户由
   `seed_catalog` 自己建行，值与这里的冻结副本一致，两路互不冲突）。
2. ⛔ **一行角色授权都不碰**：改的是点自身的 `name` / `note` / `available`；
   `Role.permissions` 指向的是行 PK，本迁移**不删行、不建新行替换旧行** ⇒ 已有的授权
   关系原样保留（`0002` 的「改名不许 delete+create」红线同源）。
3. ⛔ **不自己切 schema**：`apps.rbac` 只在 `TENANT_APPS` ⇒ 只在租户 schema 里跑。

## 改的是什么（与 `catalog.NODES` 那两行逐字同值）

`benchmark_id`（141 / 1226）与 `url` 不变；`name` 改「客户管理 / 客户分析报表」、
`note` 同步客户口径、`available` 翻真。`name` / `note` 是**冻结副本**
（迁移是历史记录，⛔ 不 import `apps.rbac.catalog`）。

每跑一个租户 schema 打印一行：`[rbac.0017] <schema>: 改名翻真 2/2`。
"""
from django.db import connection, migrations

#: 客户管理（对标 141；旧名「会员管理」）。
MEMBER_LIST_CODE = "customer.member_list"
#: 客户分析报表（对标 1226；旧名「会员分析报表」）。
MEMBER_ANALYSIS_CODE = "customer.member_analysis_report"

#: 挂靠的栏目行（`customer` 栏目）。
MODULE_CODE = "customer"

#: 与 `apps.rbac.catalog.NODES` 那两行的**冻结副本**（理由见模块 docstring）。
#: `update_or_create` 按 **code** 匹配（本线不改 code ⇒ 存量行按 code 命中）。
FROZEN_NODES = (
    (MEMBER_LIST_CODE, {
        "benchmark_id": 141,
        "name": "客户管理",
        "note": "勾选此项后代表此职位的人可以进入客户管理列表",
        "url": "/Customer/CustomerList",
        "kind": "page",
        "level": 1,
        "order_by": 0,
        "available": True,
    }),
    (MEMBER_ANALYSIS_CODE, {
        "benchmark_id": 1226,
        "name": "客户分析报表",
        "note": "进入客户分析报表的页面",
        "url": "/Customer/HyAnalysisReport",
        "kind": "page",
        "level": 1,
        "order_by": 0,
        "available": True,
    }),
)


def _schema_label() -> str:
    """迁移日志里的 schema 名（django-tenants 把它挂在 connection 上）。"""
    return getattr(connection, "schema_name", "") or "public"


def rename_and_flip_customer_member_nodes(apps, schema_editor):
    """传播两点的 name / note / available；⛔ 不动任何角色授权（见模块 docstring 口径 2）。"""
    Node = apps.get_model("rbac", "PermissionNode")

    parent = Node.objects.filter(code=MODULE_CODE).first()
    done = 0
    for code, defaults in FROZEN_NODES:
        values = dict(defaults)
        if parent is not None:
            values["parent"] = parent
        _, created = Node.objects.update_or_create(code=code, defaults=values)
        done += 0 if created else 1
    print(f"[rbac.0017] {_schema_label()}: 改名翻真 {done}/{len(FROZEN_NODES)}")


def noop_reverse(apps, schema_editor):
    """有意为空：回滚改名会拦住业务方已发出的授权意图（旧名不再作为权威口径）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0016_add_customer_member_list_self_only_node"),
    ]

    operations = [
        migrations.RunPython(rename_and_flip_customer_member_nodes, noop_reverse),
    ]
