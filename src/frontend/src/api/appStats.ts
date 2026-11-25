/** 【APP 与小程序 → 数据概览】页（`layui-island/pages/app-stats.ts`）的端点封装。后端契约的权威出处 = `src/apps/business/api/app_stats_views.py` 与`src/apps/business/app_stats.py` 的模块 docstring；本模块按那一份逐键声明。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/app-stats/overview/?days=7` | 小程序访问 / 注册 / 每日接单 / 押金冻结四块 |⚠️ **金额一律字符串、原样上屏**（后端 `Decimal` 不交给 JSONEncoder 变 `float`）；前端不做金额加法（押金变动由后端按日算好）。⚠️ 微信块在未开通 / 失败时 `available=false`、`series=[]`、`totals=null` —— 页面必须显示「暂不可用」而不是 0（计划 §7/§8 的硬口径）。 */
import { request } from './http'

/** 本页的节点门（与 `config/routePermissions.ts` 的 `ROUTE_NODE_CODES['app-stats']` **同值**）。⚠️ 刻意住在本模块（⛔ 不在岛页里写字面量）：`tests/test_rbac_menu_alignment.py` 会把`layui-island/pages/*.ts` 里的**带点**字符串字面量当按钮级权限门收走并冻结条数——门常量放这里，那条冻结数不因为「多了一页」而变化（先例：`api/xgjReceipts.ts`）。 */
export const APP_STATS_NODE = 'finance.support_statistics'

/** 端点路径（与后端 `urls.py` 的显式 `path()` 同形，**带尾斜杠**）。 */
export const APP_STATS_URL = '/api/business/app-stats/overview/'

/** 微信块一天的访问趋势（官方字段名，后端已归一成 ISO 日期）。 */
export type WechatDay = {
  date: string
  /** 官方字段名（与 `date` 同值，后端一并给出；页面不读它）。 */
  ref_date?: string
  session_cnt: number
  visit_pv: number
  visit_uv: number
  visit_uv_new: number
}

/** 微信块。`available=false` 时 `reason` 非空、`series` 为空、`totals=null`
 * —— ⛔ 不把缺失数据渲染成 0。 */
export type WechatBlock = {
  available: boolean
  reason: string
  /** T+1 窗口（止于昨天）。 */
  date_from: string
  date_to: string
  series: WechatDay[]
  totals: {
    session_cnt: number
    visit_pv: number
    visit_uv: number
    visit_uv_new: number
  } | null
}

/** 注册块的一天（三桶 + 合计）。 */
export type RegistrationDay = {
  date: string
  mp_weixin: number
  app_android: number
  unknown: number
  total: number
}

export type RegistrationBlock = {
  series: RegistrationDay[]
  totals: { mp_weixin: number; app_android: number; unknown: number; total: number }
  /** 「历史未区分」提示（后端给的如实口径）。 */
  note: string
}

/** 接单块的一天（按渠道分）。 */
export type ClaimDay = {
  date: string
  total: number
  /** 移动端（`claim_channel.CHANNEL_APP`）的条数。 */
  mobile: number
  by_reason: Record<string, number>
}

export type ClaimBlock = {
  series: ClaimDay[]
  totals: { total: number; mobile: number; by_reason: Record<string, number> }
  /** 算进「移动端」的渠道值（今天只有一个）。 */
  mobile_channels: string[]
}

/** 押金块（只读汇总）。 */
export type DepositBlock = {
  total_frozen: string
  taker_count: number
  series: Array<{ date: string; delta: string }>
  /** 如实口径：「账面冻结额（含到期未结算）」。 */
  note: string
}

export type AppStatsOverview = {
  days: number
  /** 自家数据窗口（含今天）。 */
  date_from: string
  date_to: string
  wechat: WechatBlock
  registrations: RegistrationBlock
  claims: ClaimBlock
  deposit: DepositBlock
}

/** 取一屏数据。`days` 由后端夹在 1..30（默认 7）。 */
export function fetchAppStatsOverview(days: number): Promise<AppStatsOverview> {
  return request<AppStatsOverview>({
    method: 'get',
    url: APP_STATS_URL,
    params: { days },
  })
}
