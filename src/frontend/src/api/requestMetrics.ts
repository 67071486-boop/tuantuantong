/** 「性能分析」页（`pages/system-perfanalysis.ts`）的端点封装。spec：`docs/plans/2026-10-04-request-metrics.md` §4（**接口契约已冻结**）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/system/request-metrics/` | 时间窗内的请求耗时聚合（按接口 / 按人 / 最慢逐条） |## 只读（spec §8 第 3 条）本模块**只有这一条 GET**：⛔ 没有写接口、⛔ 没有导出接口。那两点是**业务裁定**（「只对账号管理员开放、不提供导出、不提供跨租户汇总」），不是"还没做"——要加写 / 导出必须先有新的裁定，⛔ 别照着别的页面的形状把 POST / 导出补上。## 查询参数（spec §4：全部可选，这里只实现本页要用的两个）契约里一共有 5 个可选参数：`date_from` / `date_to`（日期字符串）、`route` / `username`、
 * `slow_only`（真值开关）。本页的筛选栏**只有时间范围**（spec §1 第 3 条的目标态就是「时间范围筛选 + 按接口 / 按人聚合 + 最慢若干条」）⇒ 这里只实现两个日期参数。⚠️ **另外三个不实现是有意的**（不是漏了）：写一个没有任何调用方的参数出口，正是 `api/orderActionLogs.ts` 2026-10-04 删掉那个死导出的同一种东西（`grep` 实测零调用点）。将来真加「按接口 / 按人筛选」的控件时，在**那时**按当时的载荷形状把参数加进来，并同一次补它的判据。## 为什么把查询串自己拼出来（而不用 axios 的 `params`）这条 URL 是**判据的落点**：页测试驱动真交互后要断言「发出的查询串逐字正确」（`AGENTS.md` §5-G：自建 island 页必须有能证明自己会红的交互用例），而 `URLSearchParams` 的拼接结果是**确定性**的（同一批参数每次同样的顺序与同样的百分号编码）⇒ 断言可以打在**字面值**上，而不是「传了一个对象」这种弱判据。
 * 参数名一律取 spec §4 的键名（⛔ 别自创：`dateFrom` / `start` 这类名字后端会**静默忽略**，症状是「页面永远显示默认窗的数据」而没有任何报错）。 */
import { request } from './http'

/** 端点路径（spec §4）。尾斜杠与后端 `apps/core/urls.py` 的字面路径一致
 * （该字面量的唯一出处是 `apps/audit/api/request_metric_views.py::ENDPOINT_ROUTE`）。 */
export const REQUEST_METRICS_URL = '/api/system/request-metrics/'

/** 时间范围（spec §4 响应的 `range`；同时是请求里那两个参数的形状）。 */
export type RequestMetricsRange = {
  date_from: string
  date_to: string
}

/** 分档直方图的一格（spec §3 的 6 个桶；`label` 由后端给，前端**不镜像**那张词表）。 */
export type RequestMetricsHistBucket = {
  label: string
  count: number
}

/** 概览（spec §4 的 `summary`）。 */
export type RequestMetricsSummary = {
  requests: number
  slow_requests: number
  avg_ms: number
  max_ms: number
  hist: RequestMetricsHistBucket[]
}

/** 「按接口」一行（spec §4 的 `by_route`，前 50 条、按请求数倒序）。 */
export type RequestMetricsByRouteRow = {
  /** 接口**模板**（如 `api/business/orders/`）——不含真实 id，见 spec §3 的 `route` 列。 */
  route: string
  requests: number
  avg_ms: number
  max_ms: number
}

/** 「按人」一行（spec §4 的 `by_user`，前 50 条、按请求数倒序）。 */
export type RequestMetricsByUserRow = {
  /** 账号名快照。⚠️ 未登录行的 `username` 是**空串**、`user_id` 是 `0`（spec §2）。 */
  username: string
  user_id: number
  requests: number
  avg_ms: number
  max_ms: number
}

/** 「最慢若干条」一行（spec §4 的 `slowest`，前 50 条、按耗时倒序）。 */
export type RequestMetricsSlowestRow = {
  occurred_at: string
  route: string
  method: string
  status_code: number
  username: string
  duration_ms: number
}

/** 响应 `data` 的**恰好 5 个键**（spec §4，键名冻结）。 */
export type RequestMetricsPayload = {
  range: RequestMetricsRange
  summary: RequestMetricsSummary
  by_route: RequestMetricsByRouteRow[]
  by_user: RequestMetricsByUserRow[]
  slowest: RequestMetricsSlowestRow[]
}

/** 本页会发的查询参数（两个日期都可选）。⚠️ **都为空 = 一个参数都不发** ⇒ 后端按它自己的默认窗（spec §4：不传 `date_from`⇒ **最近 7 天**）返回。这是**契约行为**，不是"页面没填就不筛"——本页的默认值恰好也是最近 7 天（`pages/system-perfanalysis.ts::defaultPerfDateRange`），所以用户清空两个框再点查询时看到的是同一段数据，不会静默变成"全部时间"（那会全表扫）。 */
export type RequestMetricsQuery = {
  date_from?: string
  date_to?: string
}

/** 组装查询参数（**唯一的线上形状出口**）：空值 / 空串 **不发**。判据是「空」而不是真值（E-0021 家族）：今天是字符串日期，没有 `0` / `false` 这种边界值，但这条口径与 `api/orderActionLogs.ts::toOrderActionLogParams` 保持一致——将来加`slow_only`（真值开关）时**不要**写成 `if (slow_only)`，`false` 与"没传"在那里是同一件事、而 `0` 不是。 */
export function toRequestMetricsParams(
  query: RequestMetricsQuery,
): Record<string, string> {
  const out: Record<string, string> = {}
  const put = (key: string, value: string | undefined): void => {
    if (value === undefined) return
    const trimmed = value.trim()
    if (trimmed === '') return
    out[key] = trimmed
  }
  put('date_from', query.date_from)
  put('date_to', query.date_to)
  return out
}

/** 拼出**要请求的那条 URL**（含查询串）。空参数时就是端点原文（不带 `?`）。
 * ⚠️ 用 `URLSearchParams`（不是手拼 `&`）：它对值做百分号编码，将来出现非 ASCII 参数
 * （如按人的 `username`）时不会拼出一条非法 URL。 */
export function requestMetricsUrl(query: RequestMetricsQuery = {}): string {
  const qs = new URLSearchParams(toRequestMetricsParams(query)).toString()
  return qs === '' ? REQUEST_METRICS_URL : `${REQUEST_METRICS_URL}?${qs}`
}

/** 取一段时间的请求耗时聚合。
 * 失败（401 / 403 / 业务码 / 网络）由 `request()` 抛类型化错误，调用方提示即可——
 * 本函数**不吞异常、也不把失败伪装成空结果**（空结果 ≠ 没有数据，`AGENTS.md` §5-E）。 */
export function fetchRequestMetrics(
  query: RequestMetricsQuery = {},
): Promise<RequestMetricsPayload> {
  return request<RequestMetricsPayload>({ method: 'get', url: requestMetricsUrl(query) })
}
