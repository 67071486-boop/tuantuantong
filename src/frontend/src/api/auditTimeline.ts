/** 「订单时间线」的 API 封装（D-0360，2026-11-01；本线独占模块）。端点：`GET /api/audit/order-timeline/?key=<单号>`（后端出处`apps/audit/api/timeline_views.py::OrderTimelineView`；聚合与查询形状的权威出处 =`apps/audit/timeline.py` 模块 docstring；准入节点 `order.platform_action_logs`）。⚠️ 本线的另一个端点 `GET /api/audit/jidanpu-breakers/`（全店熔断态）**不在本模块**：计划 §二 的页面只用时间线内嵌的 `bindings_health`；全店列表是接诊手册（TRIAGE）的curl 口径，没有页面消费方 ⇒ 前端不摆没人调的封装。## 失败语义（都由后端判，前端不复刻）查无 / 跨租户 / key 缺失**同形同码**：HTTP 200 + `code=1002` + `data=null`
 * （「未找到匹配的订单」）——`request()` 按 `unwrap` 抛 `BizError(1002)`，页面把 `msg` 原样显示。无节点 ⇒ HTTP 403（`HttpError`）。载荷形状的权威出处是后端 §1.6 契约（计划 `docs/plans/2026-11-01-order-audit.md`）：`data = {order, related, sections, bindings_health}`；sections 每节`{key, label, truncated, items}`、item 统一 `{at, kind, kind_label, status, summary, actor?}`+ 原始字段随行。前端**零 kind 字典**（人话短名全部来自后端 `kind_label`，fail-open时后端给的就是原始 kind 串）。 */
import { request } from './http'

/** 单据摘要（`data.order`）。金额未进一期契约（计划 §1.2 摘要格清单）。 */
export type TimelineOrder = {
  order_no: string
 /** 来源单号；`blank=True` 的非 NULL 文本列 ⇒ 没填就是空串。 */
  source_order_no: string
 /** 订单状态（`OrderStatus` 8 取值之一）。 */
  status: string
  status_label: string
  game_name: string
  project_text: string
 /** 审计语义：软删单**不是查无**，整条时间线照常返回、只打这个标记。 */
  is_deleted: boolean
  deleted_at: string | null
  created_at: string | null
  updated_at: string | null
 /** 接手人显示名（后端从 `Performance` 打单工资行直取；无行 = `null`）。 */
  taker_display: string | null
}

/** 一条事件（统一形状 + 原始字段随行；原始字段不逐个声明，按下标取用）。 */
export type TimelineItem = {
 /** ISO 8601；查码 note 那类「没有时刻的事实行」是 `null`。 */
  at: string | null
  kind: string
 /** 人话短名（后端登记表；未登记 kind = 原始 kind 串，fail-open）。 */
  kind_label: string
  status: string
  summary: string
 /** 原始字段随行（actor / attempts / target_ref / …），按节各异。 */
  [raw: string]: unknown
}

/** 一节（§1.6：节名固定、空节照出、超出上限置 `truncated`）。 */
export type TimelineSection = {
  key: string
  label: string
  truncated: boolean
  items: TimelineItem[]
}

/** 绑定读回熔断健康态（`data.bindings_health` 与 `/jidanpu-breakers/` 共用形状）。 */
export type BreakerHealth = {
  binding_id: number
 /** 店标识（下家账号，空则地址；后端 `_breaker_store_label` 同款口径）。 */
  store: string
  enabled: boolean
 /** true = 该绑定 12h 窗内有熔断 note 且现算判挂起（详见后端 §1.4 两级查询）。 */
  suspended: boolean
 /** 挂起判定细节（人话；不挂起为空串）。 */
  detail: string
}

/** 时间线载荷（§1.6 钉死的 `data` 四键）。 */
export type OrderTimeline = {
  order: TimelineOrder
  related: {
    peer_links: Array<{
      binding_role: string
      binding_id: number | string
      peer_order_no: string
      origin_order_no: string
    }>
    split_group: { split_id: string; member_order_nos: string[] } | null
  }
  sections: TimelineSection[]
  bindings_health: BreakerHealth[]
}

/** 按单号（系统单号**或**来源单号，D-0301 双号口径）取完整时间线。 */
export async function fetchOrderTimeline(key: string): Promise<OrderTimeline> {
  return request<OrderTimeline>({
    method: 'get',
    url: '/api/audit/order-timeline/',
    params: { key },
  })
}
