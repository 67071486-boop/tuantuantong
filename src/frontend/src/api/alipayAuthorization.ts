import { request } from './http'

export type AlipayAuthorization = {
  id: number
  request_id: string
  account_masked: string
  real_name: string
  status: string
  created_at: string | null
  authorized_at: string | null
  last_error: string | null
  authorization_url: string | null
  qr_png_base64?: string | null
  requires_platform_review?: boolean
  retryable_login?: boolean
  next_action?: string | null
}

const URL = '/api/business/alipay-authorizations/'

export function listAlipayAuthorizations(): Promise<AlipayAuthorization[]> {
  return request({ url: URL, method: 'GET' })
}

export function createAlipayAuthorization(data: { account: string; real_name: string; request_id: string }): Promise<AlipayAuthorization> {
  return request({ url: URL, method: 'POST', data })
}

export function authorizeAlipay(id: number): Promise<AlipayAuthorization> {
  return request({ url: `${URL}${id}/authorize/`, method: 'POST' })
}

export function refreshAlipayAuthorization(id: number): Promise<AlipayAuthorization> {
  return request({ url: `${URL}${id}/refresh/`, method: 'POST' })
}

export function identityAlipayAuthorization(id: number): Promise<AlipayAuthorization> {
  return request({ url: `${URL}${id}/identity/`, method: 'POST' })
}

export function refreshAlipayIdentity(id: number): Promise<AlipayAuthorization> {
  return request({ url: `${URL}${id}/identity-refresh/`, method: 'POST' })
}

export function revokeAlipayAuthorization(id: number): Promise<AlipayAuthorization> {
  return request({ url: `${URL}${id}/revoke/`, method: 'POST', data: { confirm: true } })
}

export function needsAlipayIdentity(row: AlipayAuthorization): boolean {
  return row.status === 'signed' && row.next_action === 'identity'
}

export function alipayStatusLabel(status: string, lastError?: string | null, requiresPlatformReview = false): string {
  if (['authorized', 'signed'].includes(status) && (lastError || requiresPlatformReview)) return '状态待确认'
  if (status === 'authorized') return '授权成功'
  if (status === 'signed') return '签约成功，打款待确认'
  if (status === 'revoked') return '已取消授权'
  if (status === 'pending') return '待授权'
  if (status === 'failed') return '授权失败'
  return '状态待确认'
}

export function alipayReviewMessage(row: AlipayAuthorization): string {
  return row.last_error || (row.requires_platform_review ? '付款身份信息需平台核对，请联系平台处理' : needsAlipayIdentity(row) ? '请扫码授权获取付款方身份' : row.status === 'signed' ? '打款条件待确认' : '')
}

export function newAlipayRequestId(): string {
  if (!globalThis.crypto?.getRandomValues) throw new Error('当前环境不支持安全生成申请编号')
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const PENDING_REQUEST_KEY = 'alipay-authorization-pending-request'

export function pendingAlipayRequestId(): string | null {
  return localStorage.getItem(PENDING_REQUEST_KEY)
}

export function rememberAlipayRequestId(id: string): void {
  const pending = pendingAlipayRequestId()
  if (pending && pending !== id) throw new Error('上次申请结果尚未确认，请关闭弹窗后用原账号和实名重试')
  localStorage.setItem(PENDING_REQUEST_KEY, id)
}

export function clearConfirmedAlipayRequest(rows: AlipayAuthorization[]): boolean {
  const id = pendingAlipayRequestId()
  if (!id || !rows.some((row) => row.request_id === id)) return false
  localStorage.removeItem(PENDING_REQUEST_KEY)
  return true
}
