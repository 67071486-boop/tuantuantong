"""第二批手工先行索引（迁移 0134，九把）的登记网。

0134 用 `RunSQL` 把**先于迁移存在于生产**的九把索引登记进迁移链（业务方批准的
手工止血，本迁移是登记补录）：同步台账四把（`payload->'member_binding_id'` 取活
与入站解析、`payload->'upstream_binding_id'` 上游离口取活、`payload->'mirror_id'`
的 `upstream.hall_claim` 收口——查询点 `jidanpu_member.py` / `jidanpu_upstream.py`）、
链路表一把（`jidanpu_member_openapi.py` 的 `_link_of` 按 `jidanpu_order_id`）、
团团通上家镜像两把（`peer_worker.py` 的 claim 失败收口按 `peer_order_no` 找仍停在
`claiming` 的行）、上家大厅镜像单两把（`jidanpu.py` 的 `orderIdView` 子串回读与
`jidanpu_upstream.py` 按 `upstream_order_id` 定位）。

⚠️ 判据打 `pg_indexes.indexdef`（数据库自己怎么描述这条索引），⛔ 不打迁移文本——
「迁移里写了」证明不了「库里建上了」（先例：`test_sync_event_ledger_indexes.py`、
`test_mirror_hall_listed_indexes.py`）。**本网是这九把索引的唯一判据**：RunSQL 不进
模型状态，`makemigrations` 侦不到它被摘掉。

弄红方式（本任务实测过，读数在包报告）：**掏掉 0134 的任意两条 `CREATE INDEX` 语句**
（原始文本变异、跑完写回还原）⇒ 对应用例红（测试 schema 由迁移建出来，索引
不在库里）。

⚠️ 列清单断言写**实测 canonical 形**（含括号与 `::text` / `= ANY` 归一化，钉住列
次序与谓词集合；PG 16 实测抄录）——写宽松子串会把「谓词被改宽 / 列被反序」放绿
（E-0024「钉值不钉常量」）。方向 / 活口 / 接单中枚举值本身由
`test_jidanpu_models.py::test_enum_values_match_the_spec` 逐字钉死，本网不重复。
夹具：本模块不需要业务行——`pg_indexes` 是 schema 级判据，TenantTestCase 把
迁移跑完（索引建上）即断言可打。
"""
from django.db import connection
from django_tenants.test.cases import TenantTestCase

#: 0134 建出来的九把索引名（与迁移文件逐字同源；改名字必须同一次改两处）。
MEMBER_OUT_IDX = "idx_jdp_event_member_out"
MEMBER_IN_IDX = "idx_jdp_event_member_in"
UPSTREAM_OUT_IDX = "idx_jdp_event_upstream_out"
HALL_CLAIM_MIRROR_IDX = "idx_jdp_event_hall_claim_mirror"
MEMBER_LINK_REMOTE_ID_IDX = "idx_jdp_member_link_remote_id"
PEER_MIRROR_CLAIMING_IDX = "idx_peer_mirror_claiming"
PEER_MIRROR_ORDER_NO_IDX = "idx_peer_mirror_order_no"
UPSTREAM_HALL_VIEW_NO_IDX = "idx_jdp_upstream_hall_view_no"
UPSTREAM_HALL_ORDER_ID_IDX = "idx_jdp_upstream_hall_order_id"


def _index_definitions(table):
    """当前租户 schema 里这张表每条索引的定义（`pg_indexes.indexdef` 文本，含 `WHERE` 那一半）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = %s AND tablename = %s",
            [connection.schema_name, table],
        )
        return {name: definition for name, definition in cursor.fetchall()}


class SyncEventIndexBatchRegistrationTests(TenantTestCase):
    """0134 的九把索引**真的在数据库里**，且列清单 / 次序 / 部分谓词与生产逐字同形。"""

    def _get(self, indexes, name):
        self.assertIn(name, indexes, f"索引没建上：{sorted(indexes)}")
        return indexes[name]

    def test_member_out_index_matches_the_relaxed_poll_filter(self):
        """取活索引：`((payload -> 'member_binding_id'), created_at, id)` + 仅 `direction='out'`（谓词放宽版，无活口集合）在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpusyncevent"), MEMBER_OUT_IDX
        )
        self.assertIn("USING btree", definition)
        # 全形列清单（含括号）：钉列次序——表达式在前、排序键在后，反序即白建。
        self.assertIn("(((payload -> 'member_binding_id'::text)), created_at, id)", definition)
        # 谓词钉精确 canonical 形：只钉方向；⚠️ 刻意断言**不含** status 条件——
        # 这把是谓词放宽版（done/skipped 也进索引），谁把活口集合加回来即红。
        self.assertIn("(direction)::text = 'out'::text", definition)
        self.assertNotIn("(status)::text", definition)

    def test_member_in_index_matches_the_inbound_member_lookup(self):
        """入站成员解析索引：`((payload -> 'member_binding_id'), id)` + `direction='in'` 谓词在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpusyncevent"), MEMBER_IN_IDX
        )
        self.assertIn("USING btree", definition)
        # 键列次序钉死：表达式在前、id 在后（无 created_at——与取活那把分开钉）。
        self.assertIn("(((payload -> 'member_binding_id'::text)), id)", definition)
        self.assertIn("(direction)::text = 'in'::text", definition)

    def test_upstream_out_index_matches_the_upstream_live_set(self):
        """上游离口取活索引：`((payload -> 'upstream_binding_id'), created_at, id)` + 活口谓词在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpusyncevent"), UPSTREAM_OUT_IDX
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(((payload -> 'upstream_binding_id'::text)), created_at, id)", definition)
        # 方向钉死 'out'；活口集合钉死 pending/failed 两值（canonical 形实测抄录）。
        self.assertIn("(direction)::text = 'out'::text", definition)
        self.assertIn("(status)::text = ANY", definition)
        self.assertIn("'pending'::character varying, 'failed'::character varying", definition)

    def test_hall_claim_mirror_index_matches_the_claim_settlement_lookup(self):
        """claim 收口索引：`((payload -> 'mirror_id'), id)` + `direction='out' AND kind='upstream.hall_claim'` 在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpusyncevent"), HALL_CLAIM_MIRROR_IDX
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(((payload -> 'mirror_id'::text)), id)", definition)
        self.assertIn("(direction)::text = 'out'::text", definition)
        # kind 钉死到唯一的台账类型值（canonical 形实测抄录）。
        self.assertIn("(kind)::text = 'upstream.hall_claim'::text", definition)

    def test_member_link_remote_id_index_matches_the_link_lookup(self):
        """链路查找索引：裸列 `(jidanpu_order_id)`、无谓词在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpumemberorderlink"),
            MEMBER_LINK_REMOTE_ID_IDX,
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(jidanpu_order_id)", definition)
        self.assertNotIn(" WHERE ", definition)

    def test_peer_mirror_claiming_index_matches_the_stuck_claim_sweep(self):
        """卡单收口索引：`(claiming_at)` + `status='claiming'` 谓词在库里。"""
        definition = self._get(
            _index_definitions("business_peerhallmirror"), PEER_MIRROR_CLAIMING_IDX
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(claiming_at)", definition)
        self.assertIn("(status)::text = 'claiming'::text", definition)

    def test_peer_mirror_order_no_index_matches_the_target_ref_lookup(self):
        """镜像行按上家单号查找索引：裸列 `(peer_order_no)`、无谓词在库里。"""
        definition = self._get(
            _index_definitions("business_peerhallmirror"), PEER_MIRROR_ORDER_NO_IDX
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(peer_order_no)", definition)
        self.assertNotIn(" WHERE ", definition)

    def test_upstream_hall_view_no_index_matches_the_key1_recheck(self):
        """orderIdView 回读索引：`((upstream_payload ->> 'orderIdView'))` 表达式、无谓词在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpuupstreamhallorder"),
            UPSTREAM_HALL_VIEW_NO_IDX,
        )
        self.assertIn("USING btree", definition)
        # canonical 形实测抄录（表达式索引的外层括号与键上的 ::text 归一化）。
        self.assertIn("(((upstream_payload ->> 'orderIdView'::text)))", definition)
        self.assertNotIn(" WHERE ", definition)

    def test_upstream_hall_order_id_index_matches_the_mirror_lookup(self):
        """镜像单按上家单号定位索引：裸列 `(upstream_order_id)`、无谓词在库里。"""
        definition = self._get(
            _index_definitions("business_jidanpuupstreamhallorder"),
            UPSTREAM_HALL_ORDER_ID_IDX,
        )
        self.assertIn("USING btree", definition)
        self.assertIn("(upstream_order_id)", definition)
        self.assertNotIn(" WHERE ", definition)
