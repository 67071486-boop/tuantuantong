"""批次 5 · 线 E：回填 `Order.published_at`（发单时间）与 `Order.submitted_at`（交单时间）。

## 为什么与加列那个迁移**分开**（设计 §2.3 逐字要求）

`0069_order_time_fields.py` 只加两列（schema 变更，快、必须成功）；本文件只搬历史数据
（慢、可重跑、允许在某个租户上留空）。合成一个文件的话，某台机器上「数据搬了一半」与
「列还没加」就分不出来了，也没法单独重跑回填。

## 数据来源（业务方拍板选 A；口径见 `docs/specs/2026-10-15-batch5-…-design.md` §2.3）

- `submitted_at` ← 每张单在 `OperationLog` 里 `to_value = '待验收'` 的**最早** `created_at`；
- `published_at` ← min(该单最早一条 `action='order.throw_to_hall'` 的日志,
  该单所有 `Performance.claimed_at` 里最早的那个)。

## 三条硬口径（⛔ 不要「顺手优化」掉）

1. **只填 `NULL` 的行**（幂等）：每一条 `.update()` 的 `WHERE` 里都带
   `submitted_at__isnull=True` / `published_at__isnull=True` ⇒ 重跑一个字节都不改
   （已有值**永不覆盖**）。
2. **不猜**：找不到事件 ⇒ **留空**。⛔ 不许拿 `created_at` / `updated_at` 兜底冒充
   ——那是「编一个看起来合理的时间」，会让筛选结果指向一件没发生过的事。
3. **不许拿别的动作冒充**：`published_at` 只认 `order.throw_to_hall` 日志与
   `Performance.claimed_at` 两个来源；`submitted_at` 只认 `to_value` 是「待验收」的日志行。
   ⚠️ `to_value` 在本仓写的是**中文标签**（`order_writes._status_text` / `OrderStatus.choices`），
   ⛔ 不是枚举名 `PENDING_REVIEW` —— 写成枚举名会**静默匹配 0 行**（下面那条常量
   就是唯一出处，改文案时它跟着改）。

## ⚠️ `OperationLog` 上**没有** `order` 外键（本迁移因此不能用 `order__…` 联接）

`OperationLog.order_no` 是**字符串**（`apps/audit/models.py`），不是 FK ⇒ 本文件必须
「先按 `order_no` 取事件、再把单号映射回 `Order` 的**主键**」。⛔ 别顺手写
`OperationLog.objects.filter(order__submitted_at__isnull=True)`：那会以
`FieldError: Cannot resolve keyword 'order'` **响亮失败**（不是静默空）。
（`Performance.order` **是** FK ⇒ 那一边照常用 `order__order_no`。）

## 覆盖范围（如实登记）

本文件在**每个租户 schema** 上各跑一次（`migrate_schemas --tenant`，见 `docs/RUNBOOK.md`
§2.5.3）。回填是**尽力而为**：只有存在相应事件的单才填得上，其余保持 NULL
（生产 2026-10-15 只读实测：11,596 条绩效行里，「待验收」事件覆盖 3,000 张单、
抛过大厅的 320 张单）⇒ 按这两档筛选时**筛不到那些单**是数据的真相，
⛔ 不用时间兜底把它「补全」。
"""
from collections import defaultdict

from django.db import migrations
from django.db.models import Min

#: `OrderStatus.PENDING_REVIEW` 的**中文标签**（`apps/business/models.py` 的
#: `OrderStatus` TextChoices 里那一行的第二个元素）。本文件不 import 应用模型
#: （迁移里 `apps.get_model` 拿到的是**历史模型**，而 `TextChoices` 的 label 不在历史
#: 模型上）⇒ 按本仓惯例把它写成模块级常量，并在改动 label 时同步改这里。
#: ⛔ 别改成 `"PENDING_REVIEW"`：`OperationLog.to_value` 存的是中文标签。
STATUS_TEXT_PENDING_REVIEW = "待验收"

#: 抛到大厅那个动作的日志 `action`（`apps/business/api/order_views.py::throw_to_hall`
#: 的 `record(action="order.throw_to_hall", …)`，与 `apps/audit/actions.py` 的键逐字相同）。
ACTION_THROW_TO_HALL = "order.throw_to_hall"

#: 分批大小：`QuerySet.update()` 一次只打一批**主键**，避免把整库单号塞进一条
#: `IN (…)`（生产 kexing 有 5 万+ 单）。⛔ 不用 `bulk_update`：它不走 `pre_save`。
_BATCH = 500


def _earliest_by(queryset, key):
    """`queryset` 是 `values(key).annotate(first_at=Min(...))` ⇒ 取 `{单号: 最早时刻}`。"""
    return {
        row[key]: row["first_at"]
        for row in queryset.iterator()
        if row[key] and row["first_at"] is not None
    }


def _flush(Order, pending, *, field):
    """把 `{单号: 时刻}` 分批写进 `Order.<field>`（每批只写**该列仍是 NULL**的行）。"""
    order_nos = list(pending)
    for start in range(0, len(order_nos), _BATCH):
        chunk = order_nos[start:start + _BATCH]
        todo = set(
            Order.objects.filter(
                order_no__in=chunk, **{f"{field}__isnull": True}
            ).values_list("order_no", flat=True)
        )
        for order_no in todo:
            # ⚠️ `WHERE` 里**再判一次 NULL**：上面那次读与这次写之间没有锁，
            #    再判一次才是「已有值永不覆盖」这条口径真正的落实处（幂等）。
            Order.objects.filter(
                order_no=order_no, **{f"{field}__isnull": True}
            ).update(**{field: pending[order_no]})


def backfill_submitted_at(apps, schema_editor):
    """`submitted_at` ← 每张单最早一条「改成待验收」的日志时刻。只填 NULL 行。"""
    Order = apps.get_model("business", "Order")
    OperationLog = apps.get_model("audit", "OperationLog")

    pending = _earliest_by(
        OperationLog.objects
        .filter(to_value=STATUS_TEXT_PENDING_REVIEW)
        .exclude(order_no="")
        .values("order_no")
        .annotate(first_at=Min("created_at")),
        "order_no",
    )
    _flush(Order, pending, field="submitted_at")


def backfill_published_at(apps, schema_editor):
    """`published_at` ← min(最早抛大厅日志, 该单最早的 `Performance.claimed_at`)。只填 NULL 行。"""
    Order = apps.get_model("business", "Order")
    OperationLog = apps.get_model("audit", "OperationLog")
    Performance = apps.get_model("business", "Performance")

    # 来源①：抛到内部抢单大厅（`/throw-to-hall/`、通用 `/transition/` 目标「待抢单」、
    # 录单即抛单三条路都记同一个 `action`）。
    earliest_throw = _earliest_by(
        OperationLog.objects
        .filter(action=ACTION_THROW_TO_HALL)
        .exclude(order_no="")
        .values("order_no")
        .annotate(first_at=Min("created_at")),
        "order_no",
    )
    # 来源②：第一次接手（`/claim` 的条件更新与 `/assign` / 上家线同步指派写的
    # `Performance.claimed_at`）。⚠️ 取**整张单**最早的，不按 `emp_type` 收窄——
    # 客服行（`SALES`）不写这一列，而收窄会漏掉将来别的收入类型上的接手时刻。
    # `Performance.order` **是**外键 ⇒ 经 `order__order_no` 取单号（与来源①不同形）。
    earliest_claim = _earliest_by(
        Performance.objects
        .filter(claimed_at__isnull=False, order__order_no__gt="")
        .values("order__order_no")
        .annotate(first_at=Min("claimed_at")),
        "order__order_no",
    )
    # 两个来源**取先发生的那个**（业务方口径：抛大厅 或 第一次接手，取先发生的）。
    first_at_of = defaultdict(lambda: None)
    for source in (earliest_throw, earliest_claim):
        for order_no, moment in source.items():
            current = first_at_of[order_no]
            if current is None or moment < current:
                first_at_of[order_no] = moment
    _flush(
        Order,
        {no: at for no, at in first_at_of.items() if at is not None},
        field="published_at",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0069_order_time_fields"),
        # `OperationLog` 在 `audit` app 里（两个 app 都在 `TENANT_APPS` ⇒ 每个租户 schema
        # 各建一份）⇒ 必须显式依赖它的**叶子**迁移，否则 `apps.get_model("audit", …)`
        # 拿到的历史模型可能比库里的表旧。
        ("audit", "0008_merge_0007_operationlog_actor_name_0007_requestmetric"),
    ]

    operations = [
        migrations.RunPython(backfill_submitted_at, migrations.RunPython.noop),
        migrations.RunPython(backfill_published_at, migrations.RunPython.noop),
    ]
