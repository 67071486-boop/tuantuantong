import { request } from './http'

export type WecomAftersalesRecipient = 'creator' | 'publisher' | 'sales'
export type WecomAftersalesStatus = 'disabled' | 'pending' | 'synced' | 'error'
export type WecomAftersalesOption<ID extends string | number> = { id: ID; name: string; detail?: string }

export type WecomAftersalesDraft = {
  enabled: boolean
  keywords: string[]
  group_ids: string[]
  order_recipient: WecomAftersalesRecipient
  fixed_recipient_ids: number[]
}

export type WecomAftersalesSettings = WecomAftersalesDraft & {
  available: boolean
  legacy_pilot: boolean
  status: WecomAftersalesStatus
  default_keywords: string[]
  group_options: WecomAftersalesOption<string>[]
  fixed_recipient_options: WecomAftersalesOption<number>[]
}

type OptionPage<ID extends string | number> = { items: WecomAftersalesOption<ID>[]; has_more: boolean }

export function getWecomAftersalesSettings(): Promise<WecomAftersalesSettings> {
  return request({ method: 'get', url: '/api/wecom/aftersales/settings/' })
}

export function saveWecomAftersalesSettings(draft: WecomAftersalesDraft): Promise<WecomAftersalesSettings> {
  return request({ method: 'put', url: '/api/wecom/aftersales/settings/', data: draft })
}

export function listWecomAftersalesOptions(kind: 'group', q: string, page: number): Promise<OptionPage<string>>
export function listWecomAftersalesOptions(kind: 'user', q: string, page: number): Promise<OptionPage<number>>
export function listWecomAftersalesOptions(kind: 'group' | 'user', q: string, page: number): Promise<OptionPage<string | number>> {
  return request({ method: 'get', url: '/api/wecom/aftersales/options/', params: { kind, q, page } })
}
