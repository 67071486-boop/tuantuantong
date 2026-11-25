import type { AxiosRequestConfig } from 'axios'
import type { VisitorChatScope } from './internalChatVisitors'

type Request = <T>(config: AxiosRequestConfig) => Promise<T>
type MessagePosition = { conversation_id: number, seq: number }
type Conversation = { id: number, last_message?: MessagePosition | null }
type ConversationList = { conversations: Conversation[] }
type HistoryPage = { messages: (MessagePosition & { id: number })[], has_newer: boolean }
type SyncPage = {
  cursor: number
  changes: { message?: MessagePosition | null }[]
  revoked_conversation_ids?: number[]
}
type PendingSync = { cursor: number, positions: MessagePosition[], revoked: number[] }

export function createVisitorHistoryTransport(scope: VisitorChatScope, request: Request): Request {
  const prefix = `/api/internal-chat/visitor/${scope}/`
  const positions = new Map<number, number>()
  let syncStarted = false
  let pending: PendingSync | undefined

  function record(conversationId: number, seq: number) {
    positions.set(conversationId, Math.max(positions.get(conversationId) ?? 0, seq))
  }

  function seed({ id, last_message }: Conversation) {
    let seq = last_message?.seq ?? 0
    // A handoff can make older history visible even though it is behind the sync cursor.
    for (const message of pending?.positions ?? []) {
      if (message.conversation_id === id) seq = Math.min(seq, message.seq - 1)
    }
    record(id, seq)
  }

  return async <T>(config: AxiosRequestConfig): Promise<T> => {
    if (!config.url?.startsWith(prefix)) return request<T>(config)
    const [resource = '', query] = config.url.slice(prefix.length).split('?', 2)

    if (scope === 'guest' && config.method === 'post' && resource === 'conversations/') {
      const room = await request<T & Conversation>(config)
      if (!positions.has(room.id)) seed(room)
      return room
    }

    if (config.method !== 'get') return request<T>(config)
    if (resource === 'conversations/') {
      const list = await request<T & ConversationList>(config)
      for (const room of list.conversations) {
        if (!syncStarted || !positions.has(room.id)) seed(room)
      }
      return list
    }

    if (resource === 'sync/') {
      const after = Number(new URLSearchParams(query).get('after') ?? 0)
      syncStarted = true
      // A request using the returned cursor means Web K has already dispatched that page's live messages.
      if (pending && pending.cursor <= after) {
        for (const { conversation_id, seq } of pending.positions) {
          record(conversation_id, seq)
        }
        for (const id of pending.revoked) positions.delete(id)
        pending = undefined
      }
      const page = await request<T & SyncPage>(config)
      if (page.cursor > after) {
        pending = {
          cursor: page.cursor,
          positions: page.changes.flatMap(({ message }) => message
            ? [{ conversation_id: message.conversation_id, seq: message.seq }]
            : []),
          revoked: page.revoked_conversation_ids ?? [],
        }
      }
      return page
    }

    const room = /^conversations\/([1-9]\d*)\/messages\/$/.exec(resource)
    if (!room) return request<T>(config)
    const position = positions.get(Number(room[1]))
    if (position === undefined) return request<T>(config)
    const params = new URLSearchParams(query)
    const around = params.get('around')
    const before = params.get('before')
    if (before !== null && (around !== null || !/^[1-9]\d*$/.test(before))) return request<T>(config)
    if (around !== null) {
      const history = await request<T & HistoryPage>(config)
      const anchor = history.messages.find(message => message.id === Number(around))
      if (!anchor) return history
      // Prefetch may use the read anchor. Keep explicit jumps reachable while limiting newer neighbors.
      const through = Math.max(position, anchor.seq)
      const messages = history.messages.filter(message => message.seq <= through)
      return { ...history, messages, has_newer: (messages[messages.length - 1]?.seq ?? 0) < through }
    }

    // Background history reads must not mark an undispatched live message as already loaded.
    params.set('before', String(before === null ? position + 1 : Math.min(Number(before), position + 1)))
    const history = await request<T & HistoryPage>({ ...config, url: prefix + resource + '?' + params })
    const { messages } = history
    return { ...history, has_newer: before !== null && (messages[messages.length - 1]?.seq ?? 0) < position }
  }
}
