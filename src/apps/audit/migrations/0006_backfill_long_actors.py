"""回填 `0004` **放不下**的那些旧行（`actor` 长于 64）——M3a 修 M2 登记的残差。

## 为什么需要这条迁移（而不是改 `0004` 的截断 / 整条搬家）

`0004_backfill_from_auditlog` 在 `0005` **之前**跑，那时 `OperationLog.actor` 只有
**64** 宽，而旧 `AuditLog.actor` 是 **150**（`main` 的 P4 写入点取
`request.user.username`，65–150 字可达）。原实现原样搬运 ⇒ 带这种旧行的库**第一次**
`migrate` 会 `DataError` 中断，而 `0005` 排在它后面、救不了它（M2 实测，spec §16.2 第 11 条）。

两条被否掉的处置：

1. **在 `0004` 里按 64 截断**：操作人的名字变成前缀（**静默丢数据**，正是本线要修的
   那类缺陷）；
2. **把整条回填挪到 `0005` 之后**：`0004` 在**已应用过它**的库上不会重跑，而新迁移会
   把旧行**再搬一遍** ⇒ 本地开发库里已有的回填行被复制一份。

## 本迁移的判据：与 `0004` **互补**

`0004` 搬 `len(actor) <= BACKFILL_ACTOR_WIDTH` 的行，本迁移搬
`len(actor) > BACKFILL_ACTOR_WIDTH` 的行。两条判据**互斥且覆盖全表**（每条旧行恰好
被搬一次）⇒ 「全新库」与「已应用过 `0004`/`0005` 的库」两条路都恰好搬一次，**幂等**。
⚠️ 判据里的宽度就是 `0004` 那个常量，**只定义一次**（从 `0004` import）；改一处而忘
另一处时，全新库会漏行或复制行——`tests/test_operation_log_migration.py` 的
`test_actor_longer_than_the_column_survives_the_whole_chain` 从两端钉住它。

## 自包含性

`_build` / `_BATCH_SIZE` / 宽度常量都从 `0004` 的模块取（用 `importlib`，因为模块名以
数字开头）。`0004` 只 import `django.db.migrations`、**不** import app 代码，所以这条
跨模块引用同样不会把「迁移依赖当前代码」带回来（模板/标签仍是 `0004` 里的内联冻结副本）。
"""
from importlib import import_module

from django.db import migrations

#: `0004` 的模块：分类 / 渲染 / 建行的**同一份**内联冻结逻辑（不复制一份，避免两处漂移）。
_BACKFILL_MODULE = "apps.audit.migrations.0004_backfill_from_auditlog"


def backfill_long_actors(apps, schema_editor):
    """只搬 `actor` 超过 `0004` 当时列宽的行（此时 `0005` 已把列加宽到 150 ⇒ 不丢字）。"""
    backfill = import_module(_BACKFILL_MODULE)
    LegacyAuditLog = apps.get_model("business", "AuditLog")
    OperationLog = apps.get_model("audit", "OperationLog")
    summary_width = OperationLog._meta.get_field("summary").max_length
    width = backfill.BACKFILL_ACTOR_WIDTH
    batch_size = backfill._BATCH_SIZE

    batch = []
    for old in LegacyAuditLog.objects.order_by("id").iterator(chunk_size=batch_size):
        # 与 `0004` 的判据严格互补（`<=` vs `>`），见模块 docstring。
        if len(old.actor or "") <= width:
            continue
        batch.append(backfill._build(old, OperationLog, summary_width))
        if len(batch) >= batch_size:
            OperationLog.objects.bulk_create(batch, batch_size=batch_size)
            batch.clear()
    if batch:
        OperationLog.objects.bulk_create(batch, batch_size=batch_size)


class Migration(migrations.Migration):

    dependencies = [
        # ⚠️ **必须**排在加宽迁移之后：本迁移的全部意义就是「那时列已经是 150」。
        ("audit", "0005_alter_operationlog_actor_target_name"),
    ]

    operations = [
        migrations.RunPython(backfill_long_actors),
    ]
