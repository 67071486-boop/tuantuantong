"""同步台账（business_jidanpusyncevent）两把手工先行索引的登记网（迁移 0133）。

0133 用 `RunSQL` 把**先于迁移存在于生产**的两把索引登记进迁移链（业务方批的手工
止血，本迁移是登记补录）：`idx_member_event_live_binding`（取活：
`payload->'member_binding_id'` 过滤 + `created_at, id` 排序，活口
`direction='out' AND status IN ('pending','failed')`）与 `idx_jdp_event_in_target_kind`
（入站事件按 `(target_ref, kind)` 解析，`direction='in'`）。查询点：
`jidanpu_member.py` 的取活 / 绑定解析 / `KIND_ASSIGN` 解析。

⚠️ 判据打 `pg_indexes.indexdef`（数据库自己怎么描述这条索引），⛔ 不打迁移文本——
「迁移里写了」证明不了「库里建上了」（先例：`test_mirror_hall_listed_indexes.py`、
`test_performance_settle_idx.py`）。**本网是这两把索引的唯一判据**：RunSQL 不进
模型状态，`makemigrations` 侦不到它被摘掉（0125/0126 那种「`--check` 第二路」
在这里不存在）。

弄红方式（本任务实测过，读数在包报告）：**摘掉 0133 的 `CREATE INDEX` 语句**
（原始文本变异、跑完写回还原）⇒ 两条用例红（测试 schema 由迁移建出来，索引
不在库里）。

⚠️ 列清单断言写**实测 canonical 形**（含括号与 `::text` / `= ANY` 归一化，钉住列
次序与谓词集合；PG 16 实测抄录）——写宽松子串会把「谓词被改宽 / 列被反序」放绿
（E-0024「钉值不钉常量」）。方向 / 活口枚举值本身另有
`test_jidanpu_models.py::test_enum_values_match_the_spec` 逐字钉死，本网不重复。
夹具：本模块不需要业务行——`pg_indexes` 是 schema 级判据，TenantTestCase 把
迁移跑完（索引建上）即断言可打。
"""
from django.db import connection
from django_tenants.test.cases import TenantTestCase

#: 0133 建出来的两条索引名（与迁移文件逐字同源；改名字必须同一次改两处）。
LIVE_BINDING_IDX = "idx_member_event_live_binding"
IN_TARGET_KIND_IDX = "idx_jdp_event_in_target_kind"


def _index_definitions(table):
    """当前租户 schema 里这张表每条索引的定义（`pg_indexes.indexdef` 文本，含 `WHERE` 那一半）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = %s AND tablename = %s",
            [connection.schema_name, table],
        )
        return {name: definition for name, definition in cursor.fetchall()}


class SyncEventLedgerIndexTests(TenantTestCase):
    """0133 的两把索引**真的在数据库里**，且列清单 / 次序 / 部分谓词与生产逐字同形。"""

    def test_live_binding_index_matches_the_poll_filter(self):
        """取活索引：`((payload -> 'member_binding_id'), created_at, id)` + 活口谓词在库里。"""
        indexes = _index_definitions("business_jidanpusyncevent")
        self.assertIn(LIVE_BINDING_IDX, indexes, f"索引没建上：{sorted(indexes)}")
        definition = indexes[LIVE_BINDING_IDX]
        self.assertIn("USING btree", definition)
        # 全形列清单（含括号）：钉列次序——表达式在前、排序键在后，反序即白建。
        self.assertIn("(((payload -> 'member_binding_id'::text)), created_at, id)", definition)
        # 部分谓词钉精确 canonical 形：方向钉死 'out'；活口集合钉死 pending/failed 两值。
        self.assertIn("(direction)::text = 'out'::text", definition)
        self.assertIn("(status)::text = ANY", definition)
        self.assertIn("'pending'::character varying, 'failed'::character varying", definition)

    def test_in_target_kind_index_matches_the_inbound_lookup(self):
        """入站解析索引：`(target_ref, kind)` + `direction='in'` 谓词在库里。"""
        indexes = _index_definitions("business_jidanpusyncevent")
        self.assertIn(IN_TARGET_KIND_IDX, indexes, f"索引没建上：{sorted(indexes)}")
        definition = indexes[IN_TARGET_KIND_IDX]
        self.assertIn("USING btree", definition)
        # 全形元组（含括号）：钉列次序。
        self.assertIn("(target_ref, kind)", definition)
        self.assertIn("(direction)::text = 'in'::text", definition)
