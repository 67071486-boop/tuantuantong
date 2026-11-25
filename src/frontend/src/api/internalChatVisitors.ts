import { request } from './http'

const base = '/api/internal-chat/visitor/'

export type VisitorChatScope = 'guest' | 'desk'
type ChatUser = { id: number, display_name: string }
type ReceptionContact = { id: number, name: string }
export type VisitorSeatKind = 'hr' | 'orders'
export type VisitorSeatShift = { contact_id: number, weekday: number, start: string, end: string }
export type VisitorSeat = {
  kind: VisitorSeatKind
  contact_ids: number[]
  capacity: number
  shifts: VisitorSeatShift[]
}
export type VisitorConversation = {
  id: number
  peer: ReceptionContact & { presence?: 'online' | 'offline' | 'busy' }
  seat_kind?: VisitorSeatKind
  service_status?: 'new' | 'waiting' | 'assigned' | 'closed'
  assignee_name?: string | null
}
export type VisitorHandoff = {
  from_id: number
  from_name: string | null
  to_id: number
  to_name: string | null
  transferred_at: string
}
export type VisitorGreeting = { contact_id: number, enabled: boolean, text: string }
export type VisitorSettingsFields = {
  contact_ids: number[]
  greetings: VisitorGreeting[]
  // 空数组表示没有明确移除人员，保留页面打开后恢复接待的人。
  removed_contact_ids: number[]
}
export type VisitorSettings = Omit<VisitorSettingsFields, 'removed_contact_ids'> & {
  candidates: ReceptionContact[]
  unavailable_contacts: ReceptionContact[]
  seats?: VisitorSeat[]
}

export const bootstrapVisitor = () =>
  request<{ registered: boolean, seat_mode?: boolean }>({ url: base + 'guest/bootstrap/', skipUnauthorizedHandler: true })

export const registerVisitor = (phone: string) =>
  request<{ user: ChatUser, conversation_id: number | null, seat_mode?: boolean }>({
    method: 'post', url: base + 'guest/register/', data: { phone }, skipUnauthorizedHandler: true,
  })

export const getVisitorDesk = (viewerId?: number, signal?: AbortSignal, timeout?: number) =>
  request<{ user: ChatUser, seat_mode?: boolean, can_receive?: boolean }>({
    url: base + 'desk/me/', skipUnauthorizedHandler: true, timeout,
    ...(viewerId === undefined ? {} : { headers: visitorPageHeaders(viewerId), signal }),
  })

export const getVisitorGuest = () =>
  request<{ user: ChatUser }>({ url: base + 'guest/me/', skipUnauthorizedHandler: true })

export const getVisitorSettings = () => request<VisitorSettings>({ url: base + 'settings/' })

export const saveVisitorSettings = (data: VisitorSettingsFields) =>
  request<VisitorSettings>({ method: 'put', url: base + 'settings/', data })

export const saveVisitorSeats = (seats: VisitorSeat[]) =>
  request<VisitorSettings>({ method: 'put', url: base + 'settings/', data: { seats } })

export const visitorPageHeaders = (viewerId: number) => ({ 'X-Chat-Page-User': String(viewerId) })

export const getVisitorConversations = (scope: VisitorChatScope, viewerId: number) =>
  request<{ conversations: { id: number }[] }>({
    url: base + scope + '/conversations/', headers: visitorPageHeaders(viewerId), skipUnauthorizedHandler: true,
  })

export const getVisitorConversation = (scope: VisitorChatScope, viewerId: number, id: number, signal?: AbortSignal) =>
  request<VisitorConversation>({
    url: `${base}${scope}/conversations/${id}/`, headers: visitorPageHeaders(viewerId), signal, skipUnauthorizedHandler: true,
  })

export const getVisitorDeskAvailability = (viewerId: number, signal?: AbortSignal) => request<{ enabled: boolean }>({
  url: base + 'desk/availability/', headers: visitorPageHeaders(viewerId), signal, skipUnauthorizedHandler: true,
})

export const setVisitorDeskAvailability = (viewerId: number, enabled: boolean) => request<{ enabled: boolean }>({
  method: 'post', url: base + 'desk/availability/', data: { enabled },
  headers: visitorPageHeaders(viewerId), skipUnauthorizedHandler: true,
})

export const getVisitorTransfer = (viewerId: number, id: number) =>
  request<{ candidates: ReceptionContact[], history: VisitorHandoff[] }>({
    url: `${base}desk/conversations/${id}/transfer/`, headers: visitorPageHeaders(viewerId), skipUnauthorizedHandler: true,
  })

export const transferVisitorConversation = (viewerId: number, id: number, targetId: number) => request<VisitorConversation>({
  method: 'post', url: `${base}desk/conversations/${id}/transfer/`, data: { target_id: targetId },
  headers: visitorPageHeaders(viewerId), skipUnauthorizedHandler: true,
})

export const closeVisitorConversation = (viewerId: number, id: number) => request<VisitorConversation>({
  method: 'post', url: `${base}desk/conversations/${id}/close/`,
  headers: visitorPageHeaders(viewerId), skipUnauthorizedHandler: true,
})

const chatRoot = '/api/internal-chat/'
const readPath = /^(contacts\/|conversations\/|conversations\/[1-9]\d*\/messages\/(?:[1-9]\d*\/image\/)?|sync\/|unread\/)$/
const writePath = /^conversations\/(?:[1-9]\d*\/(?:messages\/|read\/))?$/

export function visitorFrameRequest(scope: VisitorChatScope, payload: {
  path?: unknown, method?: unknown, body?: unknown,
}) {
  const { method, path, body } = payload
  if ((method !== 'get' && method !== 'post') || typeof path !== 'string' || path.length > 1500) return
  if (path === '/api/auth/me' && method === 'get') return { url: base + scope + '/me/', method }
  if (!path.startsWith(chatRoot)) return
  const [resource = '', search] = path.slice(chatRoot.length).split('?', 2)
  if (path.includes('#') || (search !== undefined && (method === 'post' || resource.endsWith('/image/')))) return
  if (!(method === 'get' ? readPath : writePath).test(resource)) return
  return {
    url: base + scope + '/' + resource + (search === undefined ? '' : '?' + search),
    method,
    data: method === 'post' ? body : undefined,
  }
}
