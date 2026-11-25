"""每单最多一张 `ready` 群二维码（计划 Rev.2 Task 4；2026-10-25 回环事故）。

## 为什么

代练员线「推上去 → 又拉回来」的回环让同一张单的群码行 2→4→8→…→193 增殖。代码侧已断
回环（`skip_remote_keys` + 图种过滤），这一条是**数据库兜底**：并发 / 未来新写入点绕过
应用层判据时，唯一索引仍然挡住第二张 `ready` 群码。

## 迁移前的去重

历史重复行（有的租户已手工清理，其它租户可能还有）必须先删掉，否则 `AddConstraint` 会
因为已有冲突行而失败。这里用**单条 SQL**（`row_number()` 每单保留 `max(id)`）：
⛔ 不把全表拉进 Python（生产单量下那会吃满内存）。

- 只处理 `img_type = 1 AND status = 'ready'`——`img_type=0` 的订单截图**不在本迁移范围**；
- ⚠️ 对象存储里的字节**不由本迁移删**：先运行
  `manage.py audit_order_group_qr --schema=<schema> --execute --delete-objects`
  （它先删行、事务提交后再删对象），否则迁移删行之后那些对象会成为**永久孤儿**。

## 回滚

约束是**部分唯一索引**（`CREATE UNIQUE INDEX … WHERE img_type = 1 AND status = 'ready'`），
**不是表约束** ⇒ ⛔ `ALTER TABLE … DROP CONSTRAINT` 会失败。回滚：
`manage.py migrate_schemas --tenant --schema=<schema> business 0090`
（反向 `RemoveConstraint` 执行 `DROP INDEX`）。

⚠️ **数据不回滚**：`RunPython` 的反向是 `noop`，被去重删掉的行**不会**复活。
有一个租户有迁移前的备份表 `business_orderscreenshot_dup_bak_20261025`（19,323 行），
其它租户没有备份表 ⇒ 回滚只回结构、不回数据。
"""
from django.db import migrations, models


def dedupe_ready_group_qr(apps, schema_editor):
    """每单保留 `max(id)`；单条 SQL，不把全表拉进 Python。"""
    OrderScreenshot = apps.get_model("business", "OrderScreenshot")
    table = schema_editor.quote_name(OrderScreenshot._meta.db_table)
    schema_editor.execute(
        f"""
        DELETE FROM {table} s
        USING (
            SELECT id,
                   row_number() OVER (PARTITION BY order_id ORDER BY id DESC) AS rn
            FROM {table}
            WHERE img_type = 1 AND status = 'ready'
        ) d
        WHERE s.id = d.id AND d.rn > 1
        """
    )


class Migration(migrations.Migration):
    dependencies = [("business", "0091_callback_file_offset")]

    operations = [
        # 先把历史重复行删掉（否则 AddConstraint 因冲突行失败）；反向 noop（数据不可逆）。
        migrations.RunPython(dedupe_ready_group_qr, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="orderscreenshot",
            constraint=models.UniqueConstraint(
                condition=models.Q(img_type=1, status="ready"),
                fields=("order",),
                name="uniq_order_ready_group_qr",
            ),
        ),
    ]
