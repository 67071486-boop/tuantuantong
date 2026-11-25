/**
 * 后台首页看板（V2Dashboard）的**区间档契约**。Phase 3（接真数据）起，占位数值
 * （D-1..D-4 的 kpi / src / game / trend / 大厅 / 同步合成演示值）已随真接口接线
 * 逐项拆除：KPI×5 走 `home/overview/` 双窗口、趋势/游戏/大厅/同步走
 * `api/dashboard.ts` 的四条聚合端点、来源走 `support-stats` 的 `sources[]`、
 * 待结算金额走 `performances` 的 summary——形状契约各自住在那些模块里。
 * 本文件只剩「区间档」这份页面级契约（区间类型 + 中文标签 + 分布行形状）。
 */

/** 看板区间档：四预设 + 日期自选（自定义）。 */
export type RangeKey = 'yesterday' | 'today' | 'd7' | 'd30' | 'custom'

/** 区间档的中文标签（排行榜 note 等复用）。 */
export const RANGE_LABELS: Readonly<Record<RangeKey, string>> = {
  yesterday: '昨天',
  today: '今天',
  d7: '最近 7 天',
  d30: '最近 30 天',
  custom: '自定义区间',
}

/** 来源 / 游戏两张分布环的一行（count = 区间内单量）。 */
export type DashboardDistRow = { name: string; count: number }
