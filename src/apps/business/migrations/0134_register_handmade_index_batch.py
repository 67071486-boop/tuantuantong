"""第二批性能索引（九把）的登记补录（RunSQL）。

索引先于本迁移存在：业务方批准后已在各生产租户库用 `CREATE INDEX CONCURRENTLY`
手工加好（同步台账按 `payload` 里的绑定/镜像标识取活与解析——`jidanpu_member.py` /
`jidanpu_upstream.py`；链路与镜像行的查找键见 `jidanpu_member_openapi.py` /
`peer_worker.py` / `peer_inbound.py` / `jidanpu.py`）。本迁移只负责「迁移状态与
环境一致」：新租户供给 / 环境重建自动带索引；已手工加过的库靠 `IF NOT EXISTS`
直接跳过（同名 no-op）。

## 形态约束

- ⛔ 不在 `Meta.indexes` 重复登记：模型状态里没有这九把索引，Django 才不会在
  本迁移之外再建一份（同名会撞「已存在」）；`makemigrations --check` 零输出
  也以此为前提。
- ⛔ 不带 `CONCURRENTLY`：已加过的库 no-op，新库表空秒建，普通 `CREATE` 可在
  原子迁移内跑。
- 表名不带 schema 前缀：tenant 迁移逐 schema 应用。
- 定义与生产逐字同形同名字（判据网 `tests.test_sync_event_index_batch_indexes.py`）。
"""
from django.db import migrations

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jdp_event_member_out ON business_jidanpusyncevent ((payload -> 'member_binding_id'::text), created_at, id) WHERE direction = 'out';
CREATE INDEX IF NOT EXISTS idx_jdp_event_member_in ON business_jidanpusyncevent ((payload -> 'member_binding_id'::text), id) WHERE direction = 'in';
CREATE INDEX IF NOT EXISTS idx_jdp_event_upstream_out ON business_jidanpusyncevent ((payload -> 'upstream_binding_id'::text), created_at, id) WHERE direction = 'out' AND status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS idx_jdp_event_hall_claim_mirror ON business_jidanpusyncevent ((payload -> 'mirror_id'::text), id) WHERE direction = 'out' AND kind = 'upstream.hall_claim';
CREATE INDEX IF NOT EXISTS idx_jdp_member_link_remote_id ON business_jidanpumemberorderlink (jidanpu_order_id);
CREATE INDEX IF NOT EXISTS idx_peer_mirror_claiming ON business_peerhallmirror (claiming_at) WHERE status = 'claiming';
CREATE INDEX IF NOT EXISTS idx_peer_mirror_order_no ON business_peerhallmirror (peer_order_no);
CREATE INDEX IF NOT EXISTS idx_jdp_upstream_hall_view_no ON business_jidanpuupstreamhallorder ((upstream_payload ->> 'orderIdView'));
CREATE INDEX IF NOT EXISTS idx_jdp_upstream_hall_order_id ON business_jidanpuupstreamhallorder (upstream_order_id);
"""

DROP_INDEXES = """
DROP INDEX IF EXISTS idx_jdp_event_member_out;
DROP INDEX IF EXISTS idx_jdp_event_member_in;
DROP INDEX IF EXISTS idx_jdp_event_upstream_out;
DROP INDEX IF EXISTS idx_jdp_event_hall_claim_mirror;
DROP INDEX IF EXISTS idx_jdp_member_link_remote_id;
DROP INDEX IF EXISTS idx_peer_mirror_claiming;
DROP INDEX IF EXISTS idx_peer_mirror_order_no;
DROP INDEX IF EXISTS idx_jdp_upstream_hall_view_no;
DROP INDEX IF EXISTS idx_jdp_upstream_hall_order_id;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0133_sync_event_live_binding_and_in_target_idx"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEXES, reverse_sql=DROP_INDEXES),
    ]
