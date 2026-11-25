/** 「客服接发单统计」页（`pages/emp-salesmanstatistics.ts`）的端点封装。spec：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` §6.1（**接口契约已冻结**；口径见同文件 §2）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/support-stats` | 时间窗内「按操作人员」与「按店铺（订单来源）」两组汇总 |## 只读本模块**只有这一条 GET**：这一页是**统计页**，不提供导出、不提供任何写入口（spec §4 只点名了筛选栏、两张卡与两张表）。要加导出是**新的一次裁定**，不是本页的遗漏。## 路径为什么**不带尾斜杠**后端的注册处是 `apps/business/urls.py` 的 `path("support-stats", …)`
 * （实施计划 `docs/plans/2026-10-17-support-stats-and-business-report.md` §3 第 1 步，与 `orders/<no>/screenshots` 一类同形）⇒ 带尾斜杠会 404，而 Django 的 `APPEND_SLASH`只补斜杠、**不剥**斜杠。⚠️ spec §6.1 的标题里写成 `/api/business/support-stats/` 是文档笔误；**判据以后端 `urls.py` 的 `path()` 字面量为准**，两边必须逐字一致。## 为什么把查询串自己拼出来（而不用 axios 的 `params`）这条 URL 是**判据的落点**：页测试驱动真交互后要断言「发出的查询串逐字正确」（`AGENTS.md` §5-G：自建 island 页必须有能证明自己会红的交互用例），而 `URLSearchParams`的拼接结果是**确定性**的（同一批参数每次同样的顺序与同样的百分号编码）⇒ 断言可以打在
 * **字面值**上。参数名一律取 spec §6.1 的键名（`date_from` / `date_to`）。 */
import { request } from './http'

/** 端点路径（见文件头「路径为什么**不带尾斜杠**」）。 */
export const SUPPORT_STATS_URL = '/api/business/support-stats'

/** 响应里的 `range`（spec §6.1）：后端实际采用的日期窗（含两端，`yyyy-MM-dd`）。 */
export type SupportStatsRange = {
  date_from: string
  date_to: string
}

/** 「操作人员接发单统计」一行（spec §4 卡 1 / §6.1 的 `operators`）。
 * ⚠️ 金额一律是**两位小数字符串**（`"300.00"`）：后端用 `Decimal` 算、序列化成字符串，
 * 前端**不做**浮点运算（spec §2「避免 JS 浮点误差」）⇒ 页面上直接显示这些字符串。 */
export type SupportStatsOperatorRow = {
  /** 接单销售客服的当前显示名；老单回落 `sales_agent` 文本快照；都没有时是「未指定」。 */
  name: string
  order_count: number
  total_price: string
  total_hall_price: string
  /** `total_price − total_hall_price`（逐行、逐合计都按这个式子，spec §2）。 */
  performance: string
}

/** 「店铺（订单来源）统计」一行（spec §4 卡 2 / §6.1 的 `sources`）。 */
export type SupportStatsSourceRow = {
  /** `Order.source` 文本快照；空串由后端归到「未指定」（spec §2）。 */
  name: string
  order_count: number
  total_price: string
  total_hall_price: string
}

/** 响应 `data` 的**恰好 5 个键**（spec §6.1，键名冻结）。
 * 两个 `*_total` 是后端算好的合计行；表格的合计行由 layui 的 `totalRow` **本地相加**得到
 * （同一批数据、同一个式子），所以页面**不把合计行当数据行再塞一遍**（计划 §4）。 */
export type SupportStatsPayload = {
  range: SupportStatsRange
  operators: SupportStatsOperatorRow[]
  operators_total: SupportStatsOperatorRow
  sources: SupportStatsSourceRow[]
  sources_total: SupportStatsSourceRow
}

/** 本页会发的查询参数（两个日期都可选）。⚠️ **都为空 = 一个参数都不发** ⇒ 后端按它自己的默认窗（spec §6.1：缺省 = 最近 30 天）返回。本页的默认值恰好也是最近 30 天（`pages/emp-salesmanstatistics.ts::defaultDateRange`），所以用户清空两个框再查询时看到的是同一段数据，不会静默变成「全部时间」。 */
export type SupportStatsQuery = {
  dateFrom?: string
  dateTo?: string
}

/** 组装查询参数（**唯一的线上形状出口**）：空值 / 空串**不发**。
 * 判据是「空」而不是真值（E-0021 家族）：这两个值都是日期字符串，没有 `0` / `false`
 * 这种边界值，但保持与 `api/requestMetrics.ts::toRequestMetricsParams` 同一口径。 */
export function toSupportStatsParams(query: SupportStatsQuery): Record<string, string> {
  const out: Record<string, string> = {}
  const put = (key: string, value: string | undefined): void => {
    if (value === undefined) return
    const trimmed = value.trim()
    if (trimmed === '') return
    out[key] = trimmed
  }
  put('date_from', query.dateFrom)
  put('date_to', query.dateTo)
  return out
}

/** 拼出**要请求的那条 URL**（含查询串）。空参数时就是端点原文（不带 `?`）。 */
export function supportStatsUrl(query: SupportStatsQuery = {}): string {
  const qs = new URLSearchParams(toSupportStatsParams(query)).toString()
  return qs === '' ? SUPPORT_STATS_URL : `${SUPPORT_STATS_URL}?${qs}`
}

/** 取一段时间内的接发单统计。
 * 失败（401 / 403 / 业务码 / 网络）由 `request()` 抛类型化错误，调用方提示即可——
 * 本函数**不吞异常、也不把失败伪装成空结果**（空结果 ≠ 没有数据，`AGENTS.md` §5-E）。 */
export function fetchSupportStats(
  query: SupportStatsQuery = {},
): Promise<SupportStatsPayload> {
  return request<SupportStatsPayload>({ method: 'get', url: supportStatsUrl(query) })
}
