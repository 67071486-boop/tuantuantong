"""同步台账（business_jidanpusyncevent）两把索引的登记补录（RunSQL）。

索引先于本迁移存在：业务方批准后已在各生产租户库用 `CREATE INDEX CONCURRENTLY`
手工加好（同步轮询每分钟按 `payload['member_binding_id']` 过滤取活、按
`(target_ref, kind)` 解析入站事件——`jidanpu_member.py`；生产台账 25~47 万行、
修复前每分钟被全表扫数百次，缺索引即每轮裸扫全表）。本迁移只负责「迁移状态与环境一致」：新租户供给 / 环境重建
自动带索引；已手工加过的库靠 `IF NOT EXISTS` 直接跳过（同名 no-op）。

## 形态约束

- ⛔ 不在 `Meta.indexes` 重复登记：模型状态里没有这两把索引，Django 才不会在
  本迁移之外再建一份（同名会撞「已存在」）；`makemigrations --check` 零输出
  也以此为前提。
- ⛔ 不带 `CONCURRENTLY`：已加过的库 no-op，新库表空秒建，普通 `CREATE` 可在
  原子迁移内跑。
- 表名不带 schema 前缀：tenant 迁移逐 schema 应用。
- 定义与生产逐字同形同名字（判据网 `tests.test_sync_event_ledger_indexes.py`）。
"""
from django.db import migrations

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_member_event_live_binding ON business_jidanpusyncevent ((payload -> 'member_binding_id'::text), created_at, id) WHERE direction = 'out' AND status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS idx_jdp_event_in_target_kind ON business_jidanpusyncevent (target_ref, kind) WHERE direction = 'in';
"""

DROP_INDEXES = """
DROP INDEX IF EXISTS idx_member_event_live_binding;
DROP INDEX IF EXISTS idx_jdp_event_in_target_kind;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0132_shorten_upstream_hall_poll_interval_30s"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEXES, reverse_sql=DROP_INDEXES),
    ]
