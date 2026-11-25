import { request } from './http'

export type PeerStudio = { id: number; name: string }
export type PeerTaker = { studio_name: string; account_name: string | null }
export type PeerTakerSelection = { studio_id: number; account_name: string }
export type PeerTakerMatch = { account_name: string; match_token: string }
export type KefuPeerSelection = PeerTakerSelection & { match_token?: string; expected_price: string | null; price?: string }
export type PeerDirectAssignment = {
  studios: PeerStudio[]
  assignee: PeerTaker | null
  assignee_lookup_failed?: boolean
  assignee_history_unverified?: boolean
  pending: (PeerTakerSelection & { studio_name: string; price: string }) | null
  price: string | null
}

const ordersUrl = '/api/business/orders/'

function assignmentUrl(orderNo?: string): string {
  return `${ordersUrl}${orderNo === undefined ? '' : `${encodeURIComponent(orderNo)}/`}peer-direct-assignment/`
}

export function fetchPeerDirectAssignment(orderNo: string, gameId?: number): Promise<PeerDirectAssignment> {
  return request({ method: 'get', url: assignmentUrl(orderNo), ...(gameId === undefined ? {} : { params: { game_id: gameId } }) })
}

export function findPeerTaker(orderNo: string, data: PeerTakerSelection): Promise<{ account_name: string; match_token: string }> {
  return request({ method: 'post', url: `${assignmentUrl(orderNo)}search/`, data })
}

export function fetchPeerDraftStudios(gameId: number): Promise<{ studios: PeerStudio[] }> {
  return request({ method: 'get', url: `${assignmentUrl()}options/`, params: { game_id: gameId } })
}

export function searchPeerTakers(
  orderNo: string | undefined, data: { studio_id: number; query: string; game_id?: number },
): Promise<{ items: PeerTakerMatch[] }> {
  return request({ method: 'post', url: `${assignmentUrl(orderNo)}search/`, data })
}

export function assignPeerTaker(orderNo: string, data: KefuPeerSelection): Promise<PeerTaker> {
  return request({ method: 'post', url: assignmentUrl(orderNo), data })
}
