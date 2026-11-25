import { chatRequest as request } from './chatTransport'
import { NetworkError } from './errors'

export type OrderChatMember = {
  id: number
  name: string
  studio_name: string
  order_role: string
  left_at: string | null
  has_permission?: boolean
}

export type OrderChatSummary = {
  id: number
  has_conversation?: true
  title: string
  order_no: string
  local_order_no: string
  is_member: boolean
  can_join: boolean
  self_member_id: number | null
  members: OrderChatMember[]
  archived?: boolean
  can_reopen?: boolean
  can_send?: boolean
}

export type EmptyOrderChat = Omit<OrderChatSummary, 'id' | 'has_conversation'> & { id: null; has_conversation: false }
export type OrderChatState = OrderChatSummary | EmptyOrderChat
export type OrderChatImage = { mime: 'image/jpeg' | 'image/png' | 'image/webp'; data: string }
export type OrderChatSendBody = { text: string; client_id: string; image?: OrderChatImage }

export function hasOrderChatConversation(value: OrderChatState): value is OrderChatSummary {
  return value.has_conversation !== false && Number.isSafeInteger(value.id) && Number(value.id) > 0
}

export type OrderChatMessage = {
  id: number
  seq: number
  sender_id: number
  text: string
  created_at: string
  revoked: boolean
  reply_to_id: number | null
  conversation_id?: number
  image?: { mime: string; width: number; height: number }
}

type MessagePage = {
  messages: OrderChatMessage[]
  has_more: boolean
  has_newer: boolean
}

function orderChatUrl(orderNo: string): string {
  return `/api/internal-chat/orders/${encodeURIComponent(orderNo)}/`
}

export async function fetchOrderChat(orderNo: string): Promise<OrderChatState> {
  const result = await request<OrderChatState>({ method: 'GET', url: orderChatUrl(orderNo) })
  if (!result || !Array.isArray(result.members) || !(hasOrderChatConversation(result)
    || result.has_conversation === false && result.id === null && typeof result.can_send === 'boolean')) {
    throw new NetworkError('聊天摘要返回异常')
  }
  return result
}

export function sendOrderChatMessage(orderNo: string, data: OrderChatSendBody): Promise<{
  conversation: OrderChatSummary; messages: OrderChatMessage[]
}> {
  return request({ method: 'POST', url: `${orderChatUrl(orderNo)}messages/`, data })
}

export function fetchOrderChatMessages(orderNo: string, params?: { before: number } | { around: number }): Promise<MessagePage> {
  return request({ method: 'GET', url: `${orderChatUrl(orderNo)}messages/`, ...(params && { params }) })
}

export function fetchOrderChatImage(orderNo: string, messageId: number): Promise<{ mime: string; data: string }> {
  return request({ method: 'GET', url: `${orderChatUrl(orderNo)}messages/${messageId}/image/` })
}

export function joinOrderChat(orderNo: string): Promise<OrderChatSummary> {
  return request({ method: 'POST', url: `${orderChatUrl(orderNo)}join/` })
}

export function reopenOrderChat(orderNo: string): Promise<OrderChatSummary> {
  return request({ method: 'POST', url: `${orderChatUrl(orderNo)}reopen/` })
}
