import { request } from './http'
import type { Page } from './unwrap'

export type WecomNotificationSettings = {
  group_sync_enabled: boolean
  group_qr_enabled: boolean
  external_pay_enabled: boolean
  payment_mismatch_enabled: boolean
  order_status_enabled: boolean
}

export type WecomNotificationRecord = {
  id: number
  kind: 'payment_mismatch' | 'order_status'
  order_no: string
  detail: string
  status: 'pending' | 'sent' | 'failed'
  reason: string
  created_at: string | null
  recipients?: Array<{ userid: string; status: 'sent' | 'failed' | 'unknown' | 'pending' | 'sending' }>
}

export function getWecomNotificationSettings(): Promise<WecomNotificationSettings> {
  return request({ method: 'get', url: '/api/wecom/notification/settings/' })
}

export function saveWecomNotificationSettings(settings: WecomNotificationSettings): Promise<WecomNotificationSettings> {
  return request({ method: 'put', url: '/api/wecom/notification/settings/', data: settings })
}

export function listWecomNotificationRecords(page: number): Promise<Page<WecomNotificationRecord>> {
  return request({ method: 'get', url: '/api/wecom/notification/records/', params: { page } })
}
