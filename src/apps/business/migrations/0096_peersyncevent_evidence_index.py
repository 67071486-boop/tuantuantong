"""审计批 C10（M-11）：`PeerSyncEvent` 补**证据查询索引**。

判据来源：抢单判据 `_hall_throw_delivered` 的两个 `exists()` 都按
`(binding_role, binding_id, kind, target_ref)` 打点（`hall.throw` 推送证据或
`hall.pull` 拉取证据），而台账只增不减 ⇒ 没有这把索引就是每轮全表扫。

## 为什么只有**一条**索引（计划写的是两条）

计划草案列了 `(status, next_attempt_at)` 与本条；**动手前实查既有索引**（§5-C 纪律）：
`(status, next_attempt_at)` 已经在位（`peer_event_due_idx`，P2 的取活索引），
重复添加只会多一份写放大。⇒ 本迁移只补缺的那条。

## ⚠️ 锁风险（如实登记）

普通 `AddIndex`（非 `CREATE INDEX CONCURRENTLY`）建索引期间**锁写**。本表在生产
（可行库）已有数万行、且每轮同步都在写 ⇒ 这条迁移要在**维护窗**跑（停
`tuantuantong-peer-sync` 单元 → 迁移 → 起回），口径见 `docs/RUNBOOK.md`「审计批 C」。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0095_peerupstreambinding_password_set"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="peersyncevent",
            index=models.Index(
                fields=["binding_role", "binding_id", "kind", "target_ref"],
                name="peer_event_evidence_idx",
            ),
        ),
    ]
