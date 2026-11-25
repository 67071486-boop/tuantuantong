// 自动发消息到微信群设置接口。契约来源（实现即契约）：
// `src/apps/business/api/order_push_views.py` + `src/apps/business/urls.py` 的
// `router.register("order-push-settings", …)`。
//
// 端点（整个集合共用 `system.order_push_group_settings` 一个 RBAC 点）：
// - `GET /api/business/order-push-settings/`
// - `POST /api/business/order-push-settings/`
// - `PATCH /api/business/order-push-settings/<id>/`
// - `DELETE /api/business/order-push-settings/<id>/`
// - `POST /api/business/order-push-settings/<id>/test/`（同步发测试消息）
//
// ⚠️ 与对标的差异（见后端 `order_push.py` 文件头）：渠道分成 `wecom` / `custom`；
// 没有对标那个「内部抢单群聊 ID」。
import { request } from './http'
import type { Page } from './unwrap'

export type PushChannel = 'wecom' | 'custom'
export type PushTiming = 0 | 10 | 20 | 30
export type PushMsgType = 'markdown' | 'text'

export type OrderPushSetting = {
  id: number
  name: string
  channel: PushChannel
  webhook_url: string
 /** 限定游戏的主键数组（后端 `games` 是多对多；⛔ 空数组不合法）。 */
  games: number[]
  timing: PushTiming
  msg_type: PushMsgType
  call_all: boolean
  remark: string
  is_active: boolean
  created_at: string
  updated_at: string
}

/** 新增 / 编辑的请求体（`id` / 时间戳由服务端给）。 */
export type OrderPushSettingInput = Omit<
  OrderPushSetting,
  'id' | 'created_at' | 'updated_at'
>

export const ORDER_PUSH_PATH = '/api/business/order-push-settings/'
export const ORDER_PUSH_PAGE_SIZE = 200

/** 页面上的四档文案（**逐字照抄对标**的 `replyOcc` 单选项）。 */
export const TIMING_LABELS: Record<PushTiming, string> = {
  0: '发内部抢单大厅+交易平台时',
  10: '只发内部抢单大厅时',
  20: '发交易平台时',
  30: '交易订单产生重要变化时',
}

/** 某一档「今天到底会不会发」的人话。
 * `20` 是**唯一不发**的一档：交易平台对接（M6）还没做，后端也没有平台发单事件。
 * 页面上必须说破，否则用户配完一条永远不会响的规则。 */
export function timingHint(timing: PushTiming): string {
  if (timing === 20) return '交易平台还没对接，这一档暂时不会发送（配了也不生效）'
  if (timing === 0) return '大厅抛单时会发；交易平台那半截等 M6（订单重要变化请单独选最后一档）'
  if (timing === 10) return '只在订单被抛到内部抢单大厅时发'
  return '订单进入代练中 / 待验收 / 已完成 / 问题单 / 暂停 / 已退单时发'
}

export function listOrderPushSettings(
  params: { page?: number; page_size?: number } = {},
): Promise<Page<OrderPushSetting>> {
  return request<Page<OrderPushSetting>>({
    method: 'get',
    url: ORDER_PUSH_PATH,
    params,
  })
}

export function createOrderPushSetting(
  input: OrderPushSettingInput,
): Promise<OrderPushSetting> {
  return request<OrderPushSetting>({ method: 'post', url: ORDER_PUSH_PATH, data: input })
}

export function updateOrderPushSetting(
  id: number,
  input: Partial<OrderPushSettingInput>,
): Promise<OrderPushSetting> {
  return request<OrderPushSetting>({
    method: 'patch',
    url: `${ORDER_PUSH_PATH}${id}/`,
    data: input,
  })
}

export function deleteOrderPushSetting(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${ORDER_PUSH_PATH}${id}/` })
}

/** 同步发一条测试消息；失败时后端回业务码，`request` 会抛 `BizError`。 */
export function testOrderPushSetting(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>({
    method: 'post',
    url: `${ORDER_PUSH_PATH}${id}/test/`,
  })
}
