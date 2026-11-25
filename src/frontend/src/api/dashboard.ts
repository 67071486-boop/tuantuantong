// 后台首页 v2 看板（`/v2` V2Dashboard）的四条聚合只读端点封装。契约来源：
// `src/apps/business/api/dashboard_views.py` + `src/apps/business/urls.py`（**实现即契约**，
// 路径字面量以后端 `path()` 逐字为准，全部**带尾斜杠**）。
//
// 与 `api/home.ts` 同一边界：本文件不 import 任何 UI 库；`request` 在 `http.ts`。
// 闸门 = 系统管理员（后端 `IsAccountAdmin`）：非管理员 ⇒ 403，页面**一发都不该发**（§2.3）。
//
// 出参形状（后端给什么就用什么，前端不再二次格式化金额）：
// - 趋势 `amount` / 无 —— 金额 = 两位小数字符串；
// - 停留时长 / 分钟数 = `number`；**无样本的键 = `null`**（⛔ 不是 0，前端按「—」渲染）；
// - 同步健康：闲管家三键恒 `null`（口径未拍板，发货队列在内存不落库）。
import { request } from './http'

/** 趋势一天：`date` = `yyyy-MM-dd`（本地日）；`amount` = 两位小数字符串。 */
export type DashboardTrendPoint = { date: string; count: number; amount: string }

/** 游戏单量分布一行（count 降序；空名后端已归「未分组」）。 */
export type DashboardDistRow = { name: string; count: number }

/** 大厅实时快照（`hall-stay/`，当前时点不随区间）。分钟数无样本时为 `null`。 */
export type DashboardHallStay = {
  in_hall_count: number
  avg_stay_minutes: number | null
  max_stay_minutes: number | null
  today_fastest_grab_minutes: number | null
  today_slowest_grab_minutes: number | null
}

/** 同步渠道一行。`last_time` = ISO 串（或 `null`）；闲管家三键恒 `null`。 */
export type DashboardSyncChannel = {
  name: string
  pending: number | null
  failed: number | null
  last_time: string | null
}

export type DashboardSyncHealth = { channels: DashboardSyncChannel[] }

const TREND_URL = '/api/business/dashboard/trend/'
const GAME_DISTRIBUTION_URL = '/api/business/dashboard/game-distribution/'
const HALL_STAY_URL = '/api/business/dashboard/hall-stay/'
const SYNC_HEALTH_URL = '/api/business/dashboard/sync-health/'

/** 窗口内逐日单量/金额（只含**有订单的日子**；补零日由调用方按窗口补齐）。 */
export function fetchDashboardTrend(window: {
  start: string
  end: string
}): Promise<DashboardTrendPoint[]> {
  return request<DashboardTrendPoint[]>({ method: 'get', url: TREND_URL, params: window })
}

/** 窗口内按游戏名分组的单量。 */
export function fetchDashboardGameDistribution(window: {
  start: string
  end: string
}): Promise<DashboardDistRow[]> {
  return request<DashboardDistRow[]>({ method: 'get', url: GAME_DISTRIBUTION_URL, params: window })
}

/** 大厅实时快照。 */
export function fetchDashboardHallStay(): Promise<DashboardHallStay> {
  return request<DashboardHallStay>({ method: 'get', url: HALL_STAY_URL })
}

/** 四渠道同步健康。 */
export function fetchDashboardSyncHealth(): Promise<DashboardSyncHealth> {
  return request<DashboardSyncHealth>({ method: 'get', url: SYNC_HEALTH_URL })
}
