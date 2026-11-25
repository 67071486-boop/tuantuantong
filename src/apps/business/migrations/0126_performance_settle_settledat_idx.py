"""N3 评审 R1 留白天（framework W-01 修法②）：绩效底表加结算聚合复合索引。

`business_performance` 是结算日汇总 / 来源分解 / 绩效页共用的**唯一聚合底表**
（现网在役租户 3,056 行、每日新增可达几千、**永久保留**——业务方
读数与拍板；租户名等真值照 E-0077 不进 git，只记 `docs/DECISIONS.md` D-0350 与
不进 git 的 recon）。
N3 已把两端点的切天筛改成**裸列半开区间**（`settled_at >= a AND < b`，sargable），
但 `settled_at` / `settle_status` 上当时没有任何索引（本迁移前实查：claimed_at /
created_at / order_id / user_id 四条 + 主键 + 两条唯一，零 settle 族）⇒ 两端点
仍是每查全表扫（N3 评审在 10 万行合成库实测 Seq Scan，改写本身零提速——
「真正提速靠本索引」是 N3 评审 R1 的原话口径，⛔ 不得写成「N3 已提速」）。

## 列清单为什么是这两列、为什么这个次序（写迁移前读码核对，⛔ 不凭计划照抄）

- `SettlementDailySummaryView.get_queryset`（`apps/business/api/settlement_views.py`）：
  基集 `settle_status=已结账`（等值）+ `settled_at IS NOT NULL` +
  两条软删排除，时间筛 = `settled_at >= 当日00:00 AND < 次日00:00`（半开区间、
  可选 from/to；`_local_day_window` 的裸列形状）；
- `SettlementDailySourcesView.get`：**同一基集逐字** + **必带**单日窗口（同一助手）。
- 两个聚合都在基集上 GROUP BY（TruncDate / order_source），扫描面 = 「已结账 ∧
  窗口内」的行 ⇒ 复合索引 `(settle_status, settled_at)`、**等值列在前、范围列在后**
  （b-tree 惯例：等值收敛后再在索引内走范围），一次索引扫正好圈出扫描集。

## ⚠️ 选型：复合索引，不取部分索引（N3 评审给的两案，取一）

N3 评审 R1 原文两案：**`(settle_status, settled_at)` 复合**（W-01 修法②原文）
或部分索引 `ON (settled_at) WHERE settle_status='settled' AND settled_at IS NOT NULL`。
**取复合**，理由三条：

1. **部分索引的 WHERE 是端点基集的第二份拷贝**：`SettleStatus.SETTLED` 的字面值
   （`'已结账'`）+ `IS NOT NULL` 要在迁移里再抄一遍；两端点基集日后一旦漂移
   （聚合口径扩状态 / `settled_at` 语义变），部分索引**静默失配变死重**——
   pg_indexes 网、`makemigrations --check`、行为用例三路全绿（0125 的枚举漂移
   坑同族，那边靠逐字钉子堵；这里不引入这份耦合就不用堵）。复合索引不含谓词，
   两端点怎么改基集它都跟着吃（只要仍按这两列筛）。
2. **部分索引「扫描集更小」的前提在这里不成立**：0125 的部分索引省在 listed 是
   五态里的小子集、gone/promoted 历史只增不减稀释扫描集；本表绩效行**终态大多是
   已结账**（结账是常态路径、永久保留）⇒ 已结账占比随时间趋近全表，部分索引的
   大小/选择性优势趋近零，却照付第 1 条的漂移风险。写放大两边同价（每行结账时
   都要动一次索引）。
3. **复合顺带服务同列的其他等值筛**：结算列表的 `settle_status` 可选筛
   （`settlement_rows`）吃前导列；「已结账 / 未结账」两态都查得到。

EXPLAIN 实测（10 万行合成库、两表 ANALYZE，探针照 N3 先例、脚本不进 git）：
无索引时两端点单天窗走 Parallel **Seq Scan**（summary 6.2ms / sources 5.7ms）；建本索引后
改走 **Index Scan using idx_perf_settle_settledat**（0.75ms / 0.30ms，REINDEX 后复跑同形状，
非瞬态）；无时间筛的全量聚合保持 Seq Scan（全量聚合本就要读全部已结账行，无损失也无伤害）。
部分索引对照：两索引**并存**时计划器仍选本复合索引（部分索引无人选），大小只省 15%
（合成集全为已结账——这是部分索引省空间优势的**上界**）⇒ 不为打平的读数多付第 1 条的漂移风险。

## ⚠️ 锁风险与发版窗口（照 0096 口径如实登记，⛔ 不拿 N4「秒级」当先例）

普通 `AddIndex`（非 `CREATE INDEX CONCURRENTLY`）建索引期间**锁写**——挡的是
结账（财务手动动作，频率低）与绩效行写入，锁窗 = 建索引时长。耗时预期随行数：

- **现在**（现网 3 千行量级）：秒级，随发版跑无感（0125 两台随发版的实测形态）；
- **一年后**（按拍板增速到 10 万行量级）：**数十秒**——这期间内结账写入被挡。
  ⇒ **发版窗口建议**：本迁移随发版跑，挑财务不结账的低峰窗（本仓发版惯例的
  凌晨窗即可）；**表过 10 万行后**，同族加/重建索引照 **0096 维护窗口径**办
  （停写它的单元 → 迁移 → 起回），⛔ 不把本迁移的秒级当先例（N3 评审 R1 原文
  同一要求）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0125_mirror_hall_listed_partial_idx"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="performance",
            index=models.Index(
                fields=["settle_status", "settled_at"],
                name="idx_perf_settle_settledat",
            ),
        ),
    ]
