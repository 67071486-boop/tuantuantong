"""第五轮审计修复 N4（framework W-02）：两张镜像表各加**大厅读口部分索引**。

旧账 Z17 后半 / W-02：`business_jidanpuupstreamhallorder` 与 `business_peerhallmirror`
两表除幂等键 / 外键外没有 status / first_seen_at 的任何索引，而两表**零删除路径**
（行只增不减，W-04 保留期另议）⇒ 大厅读口（全系统最热读端点之一）的成本驱动是
**总行数（gone/promoted 历史）**、随时间单调恶化。

## 列清单为什么是这两列（写迁移前读码核对，⛔ 不凭计划照抄）

- 鸡蛋铺路：`jidanpu_upstream.upstream_hall_queryset`
  （`apps/business/jidanpu_upstream.py` 末句 `order_by("-first_seen_at", "-id")`）
  = `filter(status="listed", owner_binding__enabled=True)` + 该排序；
- 团团通路：`peer_hall.peer_hall_queryset`（`apps/business/peer_hall.py` 末句同形）
  = `filter(status="listed", 归属绑定三条件)` + 该排序；
- 两表模型 `Meta.ordering` 同为 `["-first_seen_at", "-id"]`；
  takerapi 的按展示号解析口（`hall_views`）也是同一排序（只是刻意不带 status 条件，
  那条单行 lookup 不吃本索引，属预期）。
⇒ 列清单 `(first_seen_at DESC, id DESC)`、条件 `WHERE status='listed'`，
  与读口「筛 + 排序」形状逐字对齐——**只建 status 单列吃不到 ORDER BY，等于白建**。
  下家拉取口（`peer_views` hall.pull 的 `order_by("first_seen_at", "pk")` 升序）
  走 b-tree **反向扫**，同一把索引也吃得到，不另建第二棵。

## ⚠️ 锁风险（照 0096 的口径如实登记）

普通 `AddIndex`（非 `CREATE INDEX CONCURRENTLY`）建索引期间锁写。两表上线天数少、
今天的行数量级小（几百到几千行），秒级建完；若将来要在**大表**上重建同族索引，
照 0096 的维护窗口径办，⛔ 不把这条当先例。

## W-05 追加事实（0092 退回脚手架口径，2026-10-30 评审修复登记）

0092 退回脚手架（`test_order_screenshot_group_qr_unique.py` setUp）的「手工补列清单
**只补列、不含索引**」口径**不变**：反向迁移回滚到 0092 之前时，本迁移的两条索引
随反向迁移一起删除，tearDown 前放时由本迁移自身的 `AddIndex` 重建——脚手架无需为
索引追加任何条目（W-05 口径见计划 `docs/plans/2026-10-29-audit-round5-fix-plan.md` N4）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0124_order_upstream_hall_card"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="jidanpuupstreamhallorder",
            index=models.Index(
                fields=["-first_seen_at", "-id"],
                condition=models.Q(status="listed"),
                name="idx_jdp_upstream_hall_listed",
            ),
        ),
        migrations.AddIndex(
            model_name="peerhallmirror",
            index=models.Index(
                fields=["-first_seen_at", "-id"],
                condition=models.Q(status="listed"),
                name="idx_peer_hall_mirror_listed",
            ),
        ),
    ]
