/** 「经营报表」页（`pages/emp-dayoperation.ts`）的端点封装。spec：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` §6.2（**接口契约已冻结**；口径见同文件 §2 / §5）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/business-report` | 6 个利润/退款数字卡 + 来源分布 + 客服接单统计 + 12 月趋势 |## 只读本模块**只有这一条 GET**。这一页照抄的是对标的**看板**（数字卡 + 4 张图），对标那边也没有写入口。## 路径为什么**不带尾斜杠**后端注册处是 `apps/business/urls.py` 的 `path("business-report", …)`（实施计划 §3 第 1 步，与 `support-stats` 同形）⇒ 带尾斜杠会 404
 * （Django 的 `APPEND_SLASH` 只补斜杠、**不剥**斜杠）。⚠️ spec §6.2 的标题里写成`/api/business/business-report/` 是文档笔误；**判据以后端 `urls.py` 的字面量为准**。## 参数形状（spec §6.2）`date_from` / `date_to`（`yyyy-MM-dd`）、`stat_type`（`claim` 默认 / `submit`）、`game`（游戏主键，可空）、`kefu`（**逗号分隔**的用户主键，可空）。⚠️ `kefu` 这里发的是一串**用户主键**（`"4,9"`），不是客服名字：名字会随改名漂移，而主键是接口自己回给我们的 `kefu_options[].id`（spec §5「值 = 用户主键」）。后端按 `sales_user_id__in` 收窄；只有文本快照 `sales_agent` 的老单无法按人筛选（spec §9 第 4 条，如实登记）。 */
import { request } from './http'

/** 端点路径（见文件头「路径为什么**不带尾斜杠**」）。 */
export const BUSINESS_REPORT_URL = '/api/business/business-report'

/** 统计口径：`claim` = 按接单时间（主绩效行的 `claimed_at`）/ `submit` = 按交单时间（spec §2）。 */
export type BusinessReportStatType = 'claim' | 'submit'

/** 默认统计口径（spec §5：默认「按接单时间统计」）。 */
export const DEFAULT_STAT_TYPE: BusinessReportStatType = 'claim'

/** 响应里的 `range`（spec §6.2）：后端实际采用的日期窗 + 统计口径。 */
export type BusinessReportRange = {
  date_from: string
  date_to: string
  stat_type: BusinessReportStatType
}

/** 6 个数字卡（spec §5 / §6.2 的 `cards`）。⚠️ 金额一律是**两位小数字符串**（`"100.00"`，后端 `Decimal` 算、序列化成字符串）。口径（spec §5 表格）：前 5 个 = `总接单价 − 总发单价 − 总记录退款金额`，窗口各不相同；第 6 个只求和 `refund_fee`。**全部排除已退单状态**（对标文案）。 */
export type BusinessReportCards = {
  /** 搜索结果利润：筛选日期范围。 */
  search_profit: string
  /** 近一月利润：今天往前 30 天（含今天）。 */
  last_month_profit: string
  /** 上月利润：上一个自然月。 */
  prev_month_profit: string
  /** 近三月利润：今天往前 90 天（含今天）。 */
  three_month_profit: string
  /** 近一年利润：今天往前 365 天（含今天）。 */
  year_profit: string
  /** 搜索结果退款：筛选日期范围内只求和 `refund_fee`。 */
  search_refund: string
}

/** 6 个数字卡的 key（顺序 = 页面上的显示顺序，照对标 `dayoperation.html`）。 */
export const CARD_KEYS = [
  'search_profit',
  'last_month_profit',
  'prev_month_profit',
  'three_month_profit',
  'year_profit',
  'search_refund',
] as const

/** 数字卡的 key 类型（`CARD_KEYS` 的成员）。 */
export type BusinessReportCardKey = (typeof CARD_KEYS)[number]

/** 来源分布一行（spec §6.2 的 `source_distribution`）。
 * ⚠️ 同一行同时给 `amount`（接单价合计）与 `profit`（利润）：后端**一次 group by** 出两列，
 * 前端画两张饼时各取一列 ⇒ 同一份数据不查两遍（spec §6.2 末尾）。 */
export type BusinessReportSourceRow = {
  name: string
  amount: string
  profit: string
}

/** 来源合计（两张饼的副标题「[总计：X元]」用它）。 */
export type BusinessReportSourceTotal = {
  amount: string
  profit: string
}

/** 客服接单统计（spec §6.2 的 `support_stats`）：四条**等长**序列，索引对齐 `names`。
 * ⚠️ 订单数 / 金额排除已退单，退单量只数已退单（spec §5 第 3 张图）。 */
export type BusinessReportSupportStats = {
  names: string[]
  order_counts: number[]
  amounts: string[]
  refund_counts: number[]
}

/** 12 个自然月的趋势（spec §5 第 4 张图 / §6.2 的 `trend`）。
 * `months` 是 `yyyy-MM`（连续 12 个自然月，含本月）；四个序列与它等长。
 * 环比 = `(本月 − 上月) / 上月 × 100`，上月为 0 时记 `"0.00"`（spec §5）。 */
export type BusinessReportTrend = {
  months: string[]
  order_counts: number[]
  amounts: string[]
  count_growth: string[]
  amount_growth: string[]
}

/** 「接单客服」多选的候选（spec §5 / §6.2 的 `kefu_options`）。 */
export type BusinessReportKefuOption = {
  id: number
  name: string
}

/** 响应 `data` 的**恰好 7 个键**（spec §6.2，键名冻结）。 */
export type BusinessReportPayload = {
  range: BusinessReportRange
  cards: BusinessReportCards
  source_distribution: BusinessReportSourceRow[]
  source_total: BusinessReportSourceTotal
  support_stats: BusinessReportSupportStats
  trend: BusinessReportTrend
  kefu_options: BusinessReportKefuOption[]
}

/** 本页会发的查询参数。
 * `dateFrom` / `dateTo` / `statType` 是页面的三个固定筛选；`game` / `kefu` 是可选收窄
 * （「全部游戏」= 不传、没有勾客服 = 不传）。 */
export type BusinessReportQuery = {
  dateFrom?: string
  dateTo?: string
  statType?: BusinessReportStatType
  /** 游戏主键；`null` / `undefined` = 全部游戏（不发这个参数）。 */
  game?: number | null
  /** 接单客服的用户主键；空数组 = 不筛（不发这个参数）。 */
  kefu?: number[]
}

/** 组装查询参数（**唯一的线上形状出口**）：空值 / 空串 / 空数组**不发**。⚠️ 判据一律是「空」而不是真值（E-0021）：`game` 是主键、`kefu` 是主键数组，它们没有 `0` 这种合法边界值（后端的 `game` 校验是 `min_value=1`），但写「空」比写「真值」更难在将来接进一个 `0` 时静默丢参数。⚠️ `kefu` 的**顺序原样保留**（`"4,9"` 与 `"9,4"` 对后端等价，但查询串的判据要确定性）。 */
export function toBusinessReportParams(
  query: BusinessReportQuery,
): Record<string, string> {
  const out: Record<string, string> = {}
  const put = (key: string, value: string | undefined): void => {
    if (value === undefined) return
    const trimmed = value.trim()
    if (trimmed === '') return
    out[key] = trimmed
  }
  put('date_from', query.dateFrom)
  put('date_to', query.dateTo)
  put('stat_type', query.statType)
  put('game', query.game === null || query.game === undefined ? undefined : String(query.game))
  put(
    'kefu',
    query.kefu === undefined || query.kefu.length === 0 ? undefined : query.kefu.join(','),
  )
  return out
}

/** 拼出**要请求的那条 URL**（含查询串）。空参数时就是端点原文（不带 `?`）。 */
export function businessReportUrl(query: BusinessReportQuery = {}): string {
  const qs = new URLSearchParams(toBusinessReportParams(query)).toString()
  return qs === '' ? BUSINESS_REPORT_URL : `${BUSINESS_REPORT_URL}?${qs}`
}

/** 取一份经营报表。
 * 失败（401 / 403 / 业务码 / 网络）由 `request()` 抛类型化错误，调用方提示即可——
 * 本函数**不吞异常、也不把失败伪装成空结果**（空结果 ≠ 没有数据，`AGENTS.md` §5-E）。 */
export function fetchBusinessReport(
  query: BusinessReportQuery = {},
): Promise<BusinessReportPayload> {
  return request<BusinessReportPayload>({
    method: 'get',
    url: businessReportUrl(query),
  })
}
