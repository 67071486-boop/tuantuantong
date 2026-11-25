"""审计批 B2（M-7 + L-11）：`PeerUpstreamBinding` 补**大厅慢路拉取**的健康两列。

- `hall_pull_last_ok_at`（nullable）：最近一次**完整走完一轮拉取**的时刻。巡检脚本
  （`deploy/inspect_remote.sh`）展示它——下家的大厅几个月没刷新，在那之前数据面上
  零信号（L-11 现场：只能翻 journalctl）。
- `hall_pull_fail_count`（default 0）：**连续**拉取失败轮数。完整成功一轮即清零 + 盖章；
  数到 `peer_worker.HALL_PULL_DEAD_AFTER_FAILURES`（30 ≈ 5 小时）⇒ 该绑定名下 `listed`
  的镜像行判 `gone`（复用 `peer_hall.unlist_binding_mirrors`，可逆；绑定本身不解）。

判据与写入点在 `apps/business/peer_worker.py::run_hall_pull`；两列都不进页面序列化器
（内部调度 / 巡检列，不是页面字段）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0096_peersyncevent_evidence_index"),
    ]

    operations = [
        migrations.AddField(
            model_name='peerupstreambinding',
            name='hall_pull_fail_count',
            field=models.IntegerField(default=0, verbose_name='大厅拉取连续失败轮数'),
        ),
        migrations.AddField(
            model_name='peerupstreambinding',
            name='hall_pull_last_ok_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='上次完整拉取成功时刻'),
        ),
    ]
