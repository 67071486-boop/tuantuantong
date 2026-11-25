"""绩效底表的结算聚合复合索引（N3 评审 R1 留白天 / framework W-01 修法②，D-0350，迁移 0126）。

`business_performance` 是结算日汇总 / 来源分解 / 绩效页的唯一聚合底表（永久保留），
两端点基集形状 = `settle_status='已结账'`（等值）+ `settled_at` 半开区间（裸列范围，
N3 的 sargable 改写）。0126 给它建复合索引 `(settle_status, settled_at)`
（选型理由：复合而非部分索引，见迁移 0126 的 docstring——部分索引的 WHERE 是
端点基集的第二份拷贝、基集漂移即静默死重）。

⚠️ 判据打 `pg_indexes.indexdef`（数据库自己怎么描述这条索引），⛔ 不打模型声明——
「模型里写了」证明不了「库里建上了」（约束类变异必须同时改模型与迁移，`AGENTS.md` §5-G；
先例：`test_mirror_hall_listed_indexes.py`（0125 同族网）、`test_jidanpu_upstream_models.py`）。
弄红方式（本任务实测过，读数在线报告）：**摘迁移 0126 里的 `AddIndex`**（原始文本变异、
跑完写回还原）⇒ 本模块用例红（测试库由迁移建出来，索引不在库里）；
同一变异下 `makemigrations --check` 也红（模型 Meta 还在 ⇒ 自检出缺口，第二路独立证据）。

⚠️ 列清单断言写**字面量** `(settle_status, settled_at)`（含括号的**全形元组**，钉住
列次序——`(settled_at, settle_status)` 反序索引同样能命中「两列都在」的子串断言，
等值列在前、范围列在后是本索引成立的前提，E-0024「钉值不钉常量」）；
索引名断言写**全名** `idx_perf_settle_settledat`（名字与迁移、模型 Meta 三处逐字同源，
改名字必须同一次改三处）。
夹具：本模块不需要业务行——`pg_indexes` 是 schema 级判据，TenantTestCase 把迁移
跑完（索引建上）即断言可打。
"""
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Performance

#: 0126 建出来的索引名（与迁移文件、模型 Meta 三处逐字同源；改名字必须同一次改三处）。
PERF_SETTLE_IDX = "idx_perf_settle_settledat"


def _index_definitions(table):
    """当前租户 schema 里这张表每条索引的定义（`pg_indexes.indexdef` 文本）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = %s AND tablename = %s",
            [connection.schema_name, table],
        )
        return {name: definition for name, definition in cursor.fetchall()}


class PerformanceSettleIndexTests(TenantTestCase):
    """0126 的复合索引**真的在数据库里**，且列清单/次序/形状与聚合基集逐字对齐。"""

    def test_settle_settledat_composite_index_matches_the_aggregate_base(self):
        """`(settle_status, settled_at)` 在库里：名 + btree + 列元组（含次序）三半判据。"""
        indexes = _index_definitions(Performance._meta.db_table)
        self.assertIn(PERF_SETTLE_IDX, indexes, f"索引没建上：{sorted(indexes)}")
        definition = indexes[PERF_SETTLE_IDX]
        self.assertIn("USING btree", definition)
        # 全形元组（含括号）：钉列次序——反序索引 `(settled_at, settle_status)` 会
        # 让「等值列在前、范围列在后」的选型前提落空，必须能红。
        self.assertIn("(settle_status, settled_at)", definition)

    def test_no_partial_predicate_on_the_settle_index(self):
        """本索引**不带 WHERE 谓词**（选型的承重半边，E-0065「钉子也要能失败」）。

        选型刻意取复合、弃部分索引（理由见迁移 0126 docstring：部分索引的 WHERE 是
        端点基集的第二份拷贝）。这条断言钉住「它真的是复合」——将来谁把它改成部分
        索引（加 `condition=`），迁移与模型会一起改、`makemigrations` 与列元组断言
        都不会红，只有这条能红。
        """
        indexes = _index_definitions(Performance._meta.db_table)
        self.assertIn(PERF_SETTLE_IDX, indexes, f"索引没建上：{sorted(indexes)}")
        definition = indexes[PERF_SETTLE_IDX]
        self.assertNotIn("WHERE", definition.upper())
