// 后台首页「数据概览」接口。契约来源：`src/apps/business/api/console_views.py` +
// `src/apps/business/urls.py`（**实现即契约**）。计划见
// `docs/plans/2026-10-04-order-stats-console-and-price-sync.md` §4-T2。
//
// 与 `api/notifications.ts` 同一边界：本文件不 import 任何 UI 库；`request` 在 `http.ts`。
//
// ## 后端**实际**支持什么（逐条核对过实现，别照对标物的参数来）
//
// | 项 | 事实 | 出处 |
// |---|---|---|
// | 窗口 | 由**调用方**给 `start` / `end` 两个 `yyyy-MM-dd` 日期（含首尾整天）；对标那边是 `day=` 档位 + `stime=` 区间串，**两套形状不通用** | `console_views.py::HomeOverviewQuerySerializer` |
// | 权限 | 仅**系统管理员**（`is_account_admin`）：非管理员 ⇒ **HTTP 403 + code 403**，前端**一发都不该发**（本页在 `index-console.ts` 里按 `me.is_account_admin === true` 提前返回） | `console_views.py::HomeOverviewView` |
// | 错误 | 缺参 / 形状错 / `start > end` ⇒ **HTTP 200 + `code 1000`**（不是 500）；本文件只负责把 HTTP 错误抛成 `HttpError`、业务码抛成 `BizError`（`http.ts` / `unwrap.ts` 的既有分工） | `apps/core/exceptions.py` |
//
// ## 出参形状（**后端给什么就用什么**，前端不再格式化）
//
// - **金额**（`order_price` / `hall_price_total` / `profit_amount`）= **两位小数字符串**；
// - **率**（`profit_rate` / `rec_order_rate` / `cancel_order_rate` / `refund_order_rate` /
//   `finish_order_rate`）= **一位小数字符串**（**不带 `%`**，由页面拼上）；
// - **计数**（其余 4 个）= `number`。
//
// ⚠️ 对标那边金额是 `toFixed(1)`（一位小数）、率也是裸数字——**本仓刻意不同**：
// 出口金额统一两位小数（`selectors.order_list_summary` 的既有形状），页面**不再二次格式化**。
import { request } from './http'

/** 后端 `home_overview_data` 的出参（`console_views.py` 的返回字典，逐键）。 */
export type HomeOverview = {
  /** 回显窗口起点（`yyyy-MM-dd`，与请求一致）。 */
  start: string
  /** 回显窗口终点（`yyyy-MM-dd`，与请求一致）。 */
  end: string
  /** 录单笔数。 */
  add_count: number
  /** 订单总额（两位小数字符串）。 */
  order_price: string
  /** 发单总额（两位小数字符串；`hall_price` 为 NULL 的单按 0 计）。 */
  hall_price_total: string
  /** 预计利润（两位小数字符串；**排除已退单的单**，可以是负数）。 */
  profit_amount: string
  /** 利润率（一位小数字符串，百分数；分母为 0 ⇒ `"0.0"`）。 */
  profit_rate: string
  /** 接手数量（窗口内当前有 MAIN 接单人的笔数）。 */
  rec_order_count: number
  /** 接手率（一位小数字符串）。 */
  rec_order_rate: string
  /** 撤销数量：**恒 0**（本仓没有撤销 / 仲裁状态，数据源不存在）。 */
  cancel_count: number
  /** 撤销率：**恒 `"0.0"`**（同上）。 */
  cancel_order_rate: string
  /** 退单数量（状态 = 已退单）。 */
  refund_count: number
  /** 退单率（一位小数字符串；页面 `pan10` 显示成 `N(x%)`）。 */
  refund_order_rate: string
  /** 完成数量（状态 ∈ {待验收, 已完成}）。 */
  finish_count: number
  /** 完成率（一位小数字符串）。 */
  finish_order_rate: string
}

/** 端点路径（`apps/business/urls.py` 的 `home/overview/`，**带尾斜杠**）。 */
export const HOME_OVERVIEW_URL = '/api/business/home/overview/'

/** 取一段窗口的 12 格统计。⚠️ `start` / `end` 都必填，形状 `yyyy-MM-dd`，`start` **可以等于** `end`（算那一天）。401 / 403 由 `http.ts` 抛 `HttpError`；400/1000 之类的业务失败抛 `BizError`——调用方（`index-console.ts`）对两者都是「保留证据件的 0/0%」同一处置。 */
export function fetchHomeOverview(window: {
  start: string
  end: string
}): Promise<HomeOverview> {
  return request<HomeOverview>({ method: 'get', url: HOME_OVERVIEW_URL, params: window })
}
