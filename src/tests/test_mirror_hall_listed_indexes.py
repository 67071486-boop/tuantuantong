"""两张镜像表的大厅读口部分索引（第五轮审计 N4 / framework W-02，迁移 0125）。

`business_jidanpuupstreamhallorder` / `business_peerhallmirror` 两表零删除路径
（行只增不减）⇒ 大厅读口（`upstream_hall_queryset` / `peer_hall_queryset`，形状都是
「status='listed' + ORDER BY first_seen_at DESC, id DESC」）的成本随**总行数**单调恶化。
0125 给两表各加**部分索引** `(first_seen_at DESC, id DESC) WHERE status='listed'`。

⚠️ 判据打 `pg_indexes.indexdef`（数据库自己怎么描述这条索引），⛔ 不打模型声明——
「模型里写了」证明不了「库里建上了」（约束类变异必须同时改模型与迁移，`AGENTS.md` §5-G；
先例：`test_jidanpu_upstream_models.py` 的部分唯一索引那条、`test_jidanpu_member_models.py` 同款）。
弄红方式（本任务实测过，读数在包报告）：**摘迁移 0125 里的 `AddIndex`**（原始文本变异、
跑完写回还原）⇒ 本模块两条用例红（测试库由迁移建出来，索引不在库里）；
同一变异下 `makemigrations --check` 也红（模型 Meta 还在 ⇒ 自检出缺口，第二路独立证据）。

⚠️ 列清单断言写**字面量** `first_seen_at DESC` / `id DESC`（读口排序键，⛔ 不写
`== 读口常量` 之类的自指形态——两边一起改坏就永远不会红，E-0024）。
夹具：本模块不需要业务行——`pg_indexes` 是 schema 级判据， TenantTestCase 把迁移
跑完（索引建上）即断言可打。

⚠️ 本模块还住着 peer 侧**枚举漂移钉子**（`PeerMirrorStatusEnumNailTests`，第五轮评审
必修 ②）：0125 的条件是**字面量** `'listed'`（迁移 Meta 里写不了枚举名），改
`PeerHallMirror.Status.LISTED` 的枚举值会让索引静默变死重、三路判据全绿——逐字钉堵它。
"""
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import JidanpuUpstreamHallOrder, PeerHallMirror

#: 0125 建出来的两条索引名（与迁移文件、模型 Meta 三处逐字同源；改名字必须同一次改三处）。
UPSTREAM_LISTED_IDX = "idx_jdp_upstream_hall_listed"
PEER_MIRROR_LISTED_IDX = "idx_peer_hall_mirror_listed"


def _index_definitions(table):
    """当前租户 schema 里这张表每条索引的定义（`pg_indexes.indexdef` 文本，含 `WHERE` 那一半）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = %s AND tablename = %s",
            [connection.schema_name, table],
        )
        return {name: definition for name, definition in cursor.fetchall()}


def _assert_listed_partial_index(self, indexes, index_name):
    """大厅读口索引的三半判据：列清单（含**两条** DESC）、条件（WHERE … listed）、形状（btree）。

    ⚠️ 「部分」两个字承重：缺 `WHERE` 那一半它就退化为全量 b-tree——gone/promoted
    历史行只增不减 ⇒ 扫描集随历史稀释，等于白建（framework W-02 的成本模型）。
    ⚠️ `DESC` 必须两条都在：只钉 `first_seen_at` 那条、`id` 那条丢了，读口
    `ORDER BY first_seen_at DESC, id DESC` 的并列次序（同刻进厅的行）就吃不到索引序。
    ⚠️ 条件钉**精确谓词** `"(status)::text = 'listed'::text"`（indexdef 实际形状），
    ⛔ 不是 `"listed"` 子串——`'unlisted'` 也含 "listed"、会假绿（第五轮评审点名的洞）。
    """
    self.assertIn(index_name, indexes, f"索引没建上：{sorted(indexes)}")
    definition = indexes[index_name]
    self.assertIn("USING btree", definition)
    self.assertIn("first_seen_at DESC", definition)
    self.assertIn("id DESC", definition)
    self.assertIn("WHERE", definition.upper())
    self.assertIn("(status)::text = 'listed'::text", definition)


class MirrorHallListedIndexTests(TenantTestCase):
    """0125 的两条部分索引**真的在数据库里**，且列清单/条件与读口形状逐字对齐。"""

    def test_upstream_mirror_listed_index_matches_the_read_path(self):
        """鸡蛋铺镜像表：`(first_seen_at DESC, id DESC) WHERE status='listed'` 在库里。"""
        indexes = _index_definitions(JidanpuUpstreamHallOrder._meta.db_table)
        _assert_listed_partial_index(self, indexes, UPSTREAM_LISTED_IDX)

    def test_peer_mirror_listed_index_matches_the_read_path(self):
        """团团通镜像表：同形状（迁移 0125 的第二条 AddIndex）。"""
        indexes = _index_definitions(PeerHallMirror._meta.db_table)
        _assert_listed_partial_index(self, indexes, PEER_MIRROR_LISTED_IDX)


class PeerMirrorStatusEnumNailTests(TenantTestCase):
    """`PeerHallMirror.Status` 五个值**逐字钉死**（照 `test_jidanpu_upstream_models.py`
    `Status.values` 同款先例）。

    ⚠️ 承重：0125 的索引条件是**字面量** `'listed'`（迁移 Meta 里写不了枚举名）——
    将来改 `LISTED` 的枚举值，读口查询（走枚举名、值跟着变）与索引条件（字面量）会
    **静默脱钩**：pg_indexes 网（indexdef 仍含 'listed'）、`makemigrations --check`
    （模型 Meta 未变）、行为用例（枚举引用）**三路全绿**，索引变死重。往返/行为用例
    全走枚举名挡不住「改值」⇒ 只能逐字钉（鸡蛋铺表的同款钉在
    `test_jidanpu_upstream_models.py::UpstreamHallOrderShapeTests`）。
    """

    def test_peer_mirror_status_enum_values_match_the_spec(self):
        """五个状态值逐字集合（D-0303 spec §3.1）+ 默认值逐字。"""
        self.assertEqual(
            list(PeerHallMirror.Status.values),
            ["listed", "claiming", "promoted", "gone", "skipped"],
        )
        self.assertEqual(PeerHallMirror._meta.get_field("status").default, "listed")
