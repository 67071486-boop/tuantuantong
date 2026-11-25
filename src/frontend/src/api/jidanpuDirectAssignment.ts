import { request } from './http'

export type JidanpuTaker = { name: string; number: string }
export type JidanpuStudio = { id: number; name: string }
export type JidanpuTakerSearch = { items: JidanpuTaker[]; more: boolean; studio_token?: string; fee?: string }
export type JidanpuDirectAssignment = {
  bound: boolean
  current: JidanpuTaker | null
  assignable: boolean
  reason: string
  fee: string | null
  pending: string | null
  reservation?: string
  studios?: JidanpuStudio[]
  price?: string | null
  operation_id?: string
  operation_status?: 'working' | 'done' | 'failed' | 'needs_review'
}

function assignmentUrl(orderNo: string): string {
  return `/api/business/orders/${encodeURIComponent(orderNo)}/jidanpu-direct-assignment/`
}

export function fetchJidanpuDirectAssignment(orderNo: string, studioId?: number, refresh = false): Promise<JidanpuDirectAssignment> {
  return request({
    method: 'get', url: assignmentUrl(orderNo),
    params: { studio_id: studioId, refresh: refresh || undefined },
  })
}

export function searchJidanpuTakers(orderNo: string, query: string, studioId?: number): Promise<JidanpuTakerSearch> {
  return request({
    method: 'post', url: `${assignmentUrl(orderNo)}search/`,
    data: { query, ...(studioId === undefined ? {} : { studio_id: studioId }) },
  })
}

export function assignJidanpuTaker(
  orderNo: string,
  data: { operation_id: string; number: string; name: string; expected_number: string; expected_fee: string | null; fee?: string }
    & ({ studio_id: number; studio_token: string; expected_price: string | null; price?: string } | { studio_id?: never }),
): Promise<JidanpuDirectAssignment> {
  return request({ method: 'post', url: assignmentUrl(orderNo), data })
}

export function fetchJidanpuAssignmentStatus(orderNo: string, operationId: string): Promise<JidanpuDirectAssignment | null> {
  return request({ method: 'get', url: assignmentUrl(orderNo), params: { operation_id: operationId } })
}

export function verifyJidanpuAssignment(orderNo: string, pendingRef: string, closeUnconfirmed = false): Promise<JidanpuDirectAssignment> {
  return request({
    method: 'post', url: `${assignmentUrl(orderNo)}verify/`,
    data: { pending_ref: pendingRef, close_unconfirmed: closeUnconfirmed },
  })
}
