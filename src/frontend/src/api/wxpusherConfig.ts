import { request } from './http'

const BASE_URL = '/api/business/wxpusher-config/'

export type WxPusherOrderEvent =
  | 'assigned' | 'unassigned' | 'review_rejected' | 'completed' | 'paused_resumed' | 'issue' | 'refunded'

export interface WxPusherConfig {
  configured: boolean
  source: 'environment' | 'tenant' | 'none'
  app_id: string
  subscribe_url: string
  system_url: string
  has_app_token: boolean
  has_login_signing_key: boolean
  push_enabled: boolean
  login_enabled: boolean
  binding_prompt_enabled: boolean
  order_enabled: boolean
  settlement_enabled: boolean
  order_events: Record<WxPusherOrderEvent, boolean>
}

export type WxPusherConfigChange = Partial<Pick<WxPusherConfig,
  'app_id' | 'subscribe_url' | 'push_enabled' | 'login_enabled' | 'binding_prompt_enabled'
  | 'order_enabled' | 'settlement_enabled'
>> & {
  app_token?: string
  login_signing_key?: string
  order_events?: Partial<WxPusherConfig['order_events']>
}

export interface WxPusherRecipient {
  id: number
  name: string
}

export interface WxPusherRecord {
  id: number
  kind: string
  title: string
  recipient: string
  order_no: string
  skip_reason: string
  status: 'pending' | 'attempted' | 'sent' | 'skipped' | 'unconfirmed'
  created_at: string
  attempted_at: string | null
}

export interface WxPusherPreview {
  kind: string
  title: string
  html: string
  url: string
}

export function getWxPusherConfig() {
  return request<WxPusherConfig>({ url: BASE_URL, method: 'GET' })
}

export function saveWxPusherConfig(data: WxPusherConfigChange, expectedAppId: string) {
  return request<WxPusherConfig>({ url: BASE_URL, method: 'PATCH', data: { ...data, expected_app_id: expectedAppId } })
}

export function listWxPusherRecipients() {
  return request<{ items: WxPusherRecipient[] }>({ url: `${BASE_URL}recipients/`, method: 'GET' })
}

export function sendWxPusherTest(userId: number, expectedAppId: string) {
  return request<{ accepted: boolean }>({ url: `${BASE_URL}test/`, method: 'POST', data: { user_id: userId, expected_app_id: expectedAppId } })
}

export type WxPusherHistoryQuery = Partial<{
  order_no: string
  recipient: string
  status: WxPusherRecord['status'] | ''
  date_from: string
  date_to: string
  page: number
}>

export function listWxPusherHistory(params: WxPusherHistoryQuery = {}) {
  return request<{ items: WxPusherRecord[]; total: number }>({ url: `${BASE_URL}history/`, method: 'GET', params })
}

export function listWxPusherPreviews() {
  return request<{ items: WxPusherPreview[] }>({ url: `${BASE_URL}preview/`, method: 'GET' })
}
