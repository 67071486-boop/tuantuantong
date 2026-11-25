import { request } from './http'

export type LabSignature = { corp_id: string; agent_id: number; timestamp: number; nonce: string; corp_signature: string; agent_signature: string }
export type LabGroup = { chat_id: string; name: string }

export function getLabSignature(url: string): Promise<LabSignature> {
  return request({ method: 'get', url: '/api/wecom/client-lab/signature/', params: { url } })
}
export function findLabGroups(name: string): Promise<{ groups: LabGroup[]; too_many: boolean }> {
  return request({ method: 'get', url: '/api/wecom/client-lab/groups/', params: { name } })
}
