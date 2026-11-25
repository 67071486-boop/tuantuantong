import { request } from './http'
import type { Page } from './unwrap'

export type ExternalPayStatus = {
  enabled: boolean
  configured: boolean
  last_run_at: string | null
  last_success_at: string | null
  last_error: string | null
}

export type ExternalPayIssue = {
  id: number
  bill_type: 0 | 1
  transaction_id: string
  source_order_no: string
  payment_amount: string
  pay_time: string | null
  reason: string
  candidate_count: number
  candidates: Array<{ order_no: string; order_amount: string }>
}

export type ExternalPayInvalid = {
  id: number
  reason: string
  window_start: string | null
  window_end: string | null
  last_retry_at: string | null
  identifiable: boolean
}

export function getExternalPayStatus(): Promise<ExternalPayStatus> {
  return request({ method: 'get', url: '/api/wecom/external-pay/status/' })
}

export function listExternalPayIssues(page: number): Promise<Page<ExternalPayIssue>> {
  return request({ method: 'get', url: '/api/wecom/external-pay/issues/', params: { page } })
}

export function listExternalPayInvalid(page: number): Promise<Page<ExternalPayInvalid>> {
  return request({ method: 'get', url: '/api/wecom/external-pay/invalid/', params: { page } })
}
