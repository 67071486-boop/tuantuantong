import { request } from './http'

const URL = '/api/business/approvals/'

export const APPROVAL_APPLY_NODE = 'home.approval_apply'

export type ApprovalAccount = { id: number; name: string; detail?: string }
export type ApprovalMode = 'mine' | 'pending' | 'copied' | 'handled'
export type ApprovalDecision = 'approved' | 'rejected'
export type ApprovalTarget = 'studio' | 'worker'

export type PenaltyOrderFields = {
  game?: number
  server?: number | null
  category?: number | null
  project?: number | null
  order_type?: string
  project_text: string
  content?: string
  demand?: string
  time_requirement?: string
  order_source?: number | null
  game_account?: string
  game_role_name?: string
  game_sub_account?: string
  current_info_1?: string
  current_info_2?: string
  customer_phone?: string
  customer_qq?: string
  customer_ww?: string
  tag?: string
  is_urgent?: boolean
  total_price?: string
  received_price?: string
  platform_price?: string
  fee?: string
}

export type PenaltyApplication = {
  original_order_no: string | null
  target_id: number
  amount: string
  reason: string
  objection?: string
  remark?: string
  approver_id: number
  cc_ids: number[]
  image_keys: string[]
  order: PenaltyOrderFields
}

export type Approval = {
  id: number
  kind: 'penalty' | 'refund'
  status: 'pending' | ApprovalDecision
  original_order_no: string | null
  generated_order_no: string | null
  refund_source_no?: string | null
  initiator_name: string
  approver_name: string | null
  target_name: string | null
  target_type: ApprovalTarget | null
  amount: string | null
  reason: string
  objection: string
  remark: string
  comment: string
  created_at: string
  decided_at: string | null
  images: { url: string }[]
  cc_names: string[]
  can_decide: boolean
  requires_assignment: boolean
  order: Partial<Pick<PenaltyOrderFields, 'project_text' | 'order_type' | 'total_price'>>
}

export type ApprovalOptions = {
  can_apply: boolean
  can_manage: boolean
  approvers: ApprovalAccount[]
  available_cc: ApprovalAccount[]
  default_cc_ids: number[]
}

export type ApprovalSettingsInput = {
  approver_ids: number[]
  available_cc_ids: number[]
  default_cc_ids: number[]
  refund_handler_id: number | null
}

export type ApprovalSettings = ApprovalSettingsInput & {
  users: ApprovalAccount[]
}

export function fetchApprovals(mode: ApprovalMode, page: number) {
  return request<{ items: Approval[]; total: number }>({
    method: 'get', url: URL, params: { mode, page, page_size: 20 },
  })
}

export function fetchApprovalOptions() {
  return request<ApprovalOptions>({ method: 'get', url: `${URL}options/` })
}

export function fetchApprovalTargets(key: string, original_order_no?: string, game?: number) {
  return request<{ items: ApprovalAccount[]; suggested: ApprovalAccount | null }>({
    method: 'get', url: `${URL}targets/`, params: { key, original_order_no, game },
  })
}

export function submitPenalty(data: PenaltyApplication) {
  return request<Approval>({ method: 'post', url: URL, data })
}

export function decideApproval(id: number, decision: ApprovalDecision, comment: string) {
  return request<Approval>({ method: 'post', url: `${URL}${id}/decision/`, data: { decision, comment } })
}

export function fetchApprovalSettings() {
  return request<ApprovalSettings>({ method: 'get', url: `${URL}settings/` })
}

export function saveApprovalSettings(data: ApprovalSettingsInput) {
  return request<ApprovalSettings>({ method: 'patch', url: `${URL}settings/`, data })
}

export function requestApprovalImageTicket(ext: string) {
  return request<{ key: string; upload_url: string }>({
    method: 'post', url: `${URL}media/ticket/`, data: { ext },
  })
}
