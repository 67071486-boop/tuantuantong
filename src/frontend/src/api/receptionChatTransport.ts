import type { AxiosRequestConfig } from 'axios'
import { HttpError, NetworkError } from './errors'
import { visitorFrameRequest } from './internalChatVisitors'

const chatRoot = '/api/internal-chat/'
type Request = <T>(config: AxiosRequestConfig) => Promise<T>
type Scope = 'visitor' | 'internal'
type Cursors = Record<Scope, number>

type ChatMessage = { id: number, conversation_id: number, reply_to_id?: number | null }
type Conversation = {
  id: number
  kind?: 'direct' | 'order' | 'system'
  last_message: ChatMessage | null
  first_unread_message_id?: number | null
  first_unread_mention_id?: number | null
  pinned_message_id?: number | null
}
type ConversationList = {
  cursor: number, conversations: Conversation[], has_more?: boolean, next_offset?: number | null,
  order_permission_unavailable?: boolean,
}
type ChatChange = { conversation_id: number, kind: string, message?: ChatMessage | null }
type ChatChanges = {
  cursor: number, has_more: boolean, changes: ChatChange[], revoked_conversation_ids?: number[],
  order_permission_unavailable?: boolean, order_visibility_token?: string | null,
}
type MessagePage = { messages: ChatMessage[] }
type Incoming = Parameters<typeof visitorFrameRequest>[1]

// Web K keys conversations and messages by one number, while both APIs have their own IDs.
export function receptionFrameId(scope: Scope, id: number): number {
  const framed = id * 2 + Number(scope === 'internal')
  if (!Number.isSafeInteger(framed) || id < 1 || !Number.isInteger(id)) throw new Error('聊天编号无效')
  return framed
}

export function receptionSourceId(id: number): { scope: Scope, id: number } | undefined {
  if (!Number.isSafeInteger(id) || id < 2) return
  return { scope: id % 2 === 0 ? 'visitor' : 'internal', id: Math.floor(id / 2) }
}

function messageForFrame(scope: Scope, message: ChatMessage): ChatMessage {
  return {
    ...message,
    id: receptionFrameId(scope, message.id),
    conversation_id: receptionFrameId(scope, message.conversation_id),
    reply_to_id: message.reply_to_id == null ? message.reply_to_id : receptionFrameId(scope, message.reply_to_id),
  }
}

function conversationForFrame(scope: Scope, conversation: Conversation): Conversation {
  const { first_unread_message_id: firstUnread, first_unread_mention_id: firstMention,
    last_message: last, pinned_message_id: pinned } = conversation
  return {
    ...conversation,
    id: receptionFrameId(scope, conversation.id),
    last_message: last && messageForFrame(scope, last),
    first_unread_message_id: firstUnread == null ? firstUnread : receptionFrameId(scope, firstUnread),
    first_unread_mention_id: firstMention == null ? firstMention : receptionFrameId(scope, firstMention),
    pinned_message_id: pinned == null ? pinned : receptionFrameId(scope, pinned),
  }
}

function changeForFrame(scope: Scope, change: ChatChange): ChatChange {
  return {
    ...change,
    conversation_id: receptionFrameId(scope, change.conversation_id),
    message: change.message && messageForFrame(scope, change.message),
  }
}

function requestForSource(resource: string, input: Incoming, scope: Scope): AxiosRequestConfig {
  const path = chatRoot + resource
  const config = scope === 'visitor' ? visitorFrameRequest('desk', { ...input, path }) : {
    url: path, method: input.method as 'get' | 'post', data: input.method === 'post' ? input.body : undefined,
  }
  if (!config) throw new Error('访客会话暂不支持此操作')
  return config
}

function requestForConversation(
  source: { id: number, scope: Scope }, suffix: 'messages/' | 'read/', input: Incoming, params: URLSearchParams,
) {
  const around = params.get('around')
  if (around !== null) {
    const target = receptionSourceId(Number(around))
    if (!target || target.scope !== source.scope) throw new Error('引用的消息不在当前会话')
    params.set('around', String(target.id))
  }
  const body = input.body as { reply_to_id?: unknown } | undefined
  if (input.method === 'post' && suffix === 'messages/' && body?.reply_to_id != null) {
    const target = receptionSourceId(Number(body.reply_to_id))
    if (!target || target.scope !== source.scope) throw new Error('引用的消息不在当前会话')
    input = { ...input, body: { ...body, reply_to_id: target.id } }
  }
  const query = params.toString()
  return requestForSource(`conversations/${source.id}/${suffix}${query ? '?' + query : ''}`, input, source.scope)
}

export function createReceptionChatTransport(request: Request, { fullInternal = false } = {}) {
  const internalConversations = new Set<number>()
  const otherConversations = new Set<number>()
  let current = { token: 0, cursors: { visitor: 0, internal: 0 } }
  let previous: { token: number, cursors: Cursors } | undefined
  let initialized = false
  let internalUnpaginated = false

  // Retain the source cursors until Web K acknowledges the page by requesting its new token.
  function advance(from: typeof current, cursors: Cursors) {
    if (cursors.visitor === from.cursors.visitor && cursors.internal === from.cursors.internal) return from.token
    previous = from
    current = { token: from.token + 1, cursors }
    return current.token
  }

  async function readWhenAvailable<T>(config: AxiosRequestConfig, allowMissing: boolean): Promise<T | undefined> {
    try {
      return await request<T>({ ...config, timeout: 4_000 })
    } catch (cause) {
      if (cause instanceof NetworkError || (cause instanceof HttpError && (cause.status >= 500 || (allowMissing && cause.status === 404)))) return
      throw cause
    }
  }

  function tryInternalRead<T>(resource: string) {
    return readWhenAvailable<T>({ url: chatRoot + resource, method: 'get' }, true)
  }

  function readVisitor<T>(resource: string) {
    const config = requestForSource(resource, { method: 'get' }, 'visitor')
    return fullInternal ? readWhenAvailable<T>(config, false) : request<T>(config)
  }

  async function listInternal() {
    const list = await tryInternalRead<ConversationList>('conversations/')
    if (!list) return
    const direct = list.conversations.filter(room => {
      const allowed = room.kind === undefined || room.kind === 'direct'
      if (allowed) internalConversations.add(room.id)
      else otherConversations.add(room.id)
      return allowed
    })
    return { ...list, conversations: direct }
  }

  async function conversationList(query?: string) {
    const resource = query === undefined ? 'conversations/' : 'conversations/?' + query
    const offset = new URLSearchParams(query).get('offset')
    const firstPage = offset === null || offset === '0'
    const checkInternal = firstPage || !internalUnpaginated
    const [visitor, internal] = await Promise.all([
      readVisitor<ConversationList>(resource),
      fullInternal ? (checkInternal ? tryInternalRead<ConversationList>(resource) : undefined)
        : firstPage ? listInternal() : undefined,
    ])
    if (fullInternal && firstPage && internal) internalUnpaginated = internal.has_more === undefined
    if (!initialized) {
      initialized = true
      advance(current, { visitor: visitor?.cursor ?? 0, internal: internal?.cursor ?? 0 })
    }
    return {
      ...visitor,
      cursor: current.token,
      ...(fullInternal ? {
        has_more: visitor?.has_more === true || internal?.has_more === true,
        next_offset: visitor?.has_more ? visitor.next_offset : internal?.next_offset,
        order_permission_unavailable: (checkInternal && !internal) || internal?.order_permission_unavailable,
      } : {}),
      conversations: [
        ...(visitor?.conversations ?? []).map(room => conversationForFrame('visitor', room)),
        ...(internal?.conversations ?? []).map(room => conversationForFrame('internal', room)),
      ],
    }
  }

  async function sync(after: number) {
    const pair = after === current.token ? current : previous?.token === after ? previous : undefined
    if (!pair) throw new Error('聊天更新游标无效')
    const [visitor, normalPage] = await Promise.all([
      readVisitor<ChatChanges>(`sync/?after=${pair.cursors.visitor}`),
      tryInternalRead<ChatChanges>(`sync/?after=${pair.cursors.internal}`),
    ])
    let internal = normalPage
    const unknown = !fullInternal && internal?.changes.some(change => change.kind !== 'conversation.removed'
      && !internalConversations.has(change.conversation_id) && !otherConversations.has(change.conversation_id))
    if (unknown && !await listInternal()) internal = undefined
    const changes = internal?.changes.filter(change => fullInternal || internalConversations.has(change.conversation_id)) ?? []
    const authorityUnavailable = fullInternal && (!internal || internal.order_permission_unavailable === true)
    return {
      ...(fullInternal ? {
        order_permission_unavailable: authorityUnavailable,
        // The combined cursor can advance visitor changes while replaying the same internal page.
        order_visibility_token: authorityUnavailable ? undefined : internal?.order_visibility_token,
      } : {}),
      cursor: advance(pair, {
        visitor: visitor?.cursor ?? pair.cursors.visitor,
        internal: authorityUnavailable ? pair.cursors.internal : internal?.cursor ?? pair.cursors.internal,
      }),
      has_more: visitor?.has_more === true || (!authorityUnavailable && internal?.has_more === true),
      changes: [
        ...(visitor?.changes ?? []).map(change => changeForFrame('visitor', change)),
        ...changes.map(change => changeForFrame('internal', change)),
      ],
      revoked_conversation_ids: visitor?.revoked_conversation_ids,
    }
  }

  async function requestInternalAction(resource: string, query: string | undefined, input: Incoming) {
    const { method } = input
    const order = /^orders\/\d{20}\/(reopen\/)?$/.exec(resource)
    if (order && query === undefined && method === (order[1] ? 'post' : 'get')) {
      return conversationForFrame('internal', await request<Conversation>(requestForSource(resource, input, 'internal')))
    }
    const matching = /^conversations\/([1-9]\d*)\/(messages\/search\/|messages\/([1-9]\d*)\/revoke\/|preferences\/|pin\/)$/.exec(resource)
    const room = matching && receptionSourceId(Number(matching[1]))
    if (!matching || room?.scope !== 'internal') throw new Error('访客会话暂不支持此操作')
    const suffix = matching[2]
    const root = `conversations/${room.id}/`
    if (method === 'get' && suffix === 'messages/search/') {
      const resourceWithQuery = root + suffix + (query === undefined ? '' : `?${query}`)
      const page = await request<MessagePage>(requestForSource(resourceWithQuery, input, 'internal'))
      return { ...page, messages: page.messages.map(message => messageForFrame('internal', message)) }
    }
    if (method !== 'post' || query !== undefined || suffix === 'messages/search/') throw new Error('聊天请求无效')
    if (suffix === 'preferences/') return request(requestForSource(root + suffix, input, 'internal'))
    if (suffix === 'pin/') {
      const pinned = (input.body as { message_id?: unknown } | undefined)?.message_id
      const target = pinned == null ? undefined : receptionSourceId(Number(pinned))
      if (pinned !== null && target?.scope !== 'internal') throw new Error('置顶的消息不在当前会话')
      const result = await request<{ pinned_message_id: number | null }>(requestForSource(root + suffix, {
        ...input, body: { message_id: target?.id ?? null },
      }, 'internal'))
      return { pinned_message_id: result.pinned_message_id === null ? null : receptionFrameId('internal', result.pinned_message_id) }
    }
    const message = receptionSourceId(Number(matching[3]))
    if (message?.scope !== 'internal') throw new Error('待撤回消息不存在')
    const revoked = await request<ChatMessage>(requestForSource(root + `messages/${message.id}/revoke/`, input, 'internal'))
    return messageForFrame('internal', revoked)
  }

  async function requestFromFrame(input: Incoming): Promise<unknown> {
    const { path, method } = input
    const validated = visitorFrameRequest('desk', input)
    if (typeof path !== 'string') throw new Error('访客会话暂不支持此操作')
    const image = method === 'get' && path.length <= 1500 &&
      /^\/api\/internal-chat\/conversations\/([1-9]\d*)\/messages\/([1-9]\d*)\/image\/$/.exec(path)
    if (image) {
      const room = receptionSourceId(Number(image[1]))
      const message = receptionSourceId(Number(image[2]))
      if (!room || message?.scope !== room.scope ||
        (room.scope === 'internal' && !fullInternal && !internalConversations.has(room.id))) {
        throw new HttpError(404, '聊天图片不存在')
      }
      return request(requestForSource(`conversations/${room.id}/messages/${message.id}/image/`, input, room.scope))
    }
    if (fullInternal && !validated && path.startsWith(chatRoot) && path.length <= 1500) {
      const [resource = '', query] = path.slice(chatRoot.length).split('?', 2)
      return requestInternalAction(resource, query, input)
    }
    if (!validated) throw new Error('访客会话暂不支持此操作')
    if (!path.startsWith(chatRoot)) return request(validated)

    const [resource, query] = path.slice(chatRoot.length).split('?', 2)
    const params = new URLSearchParams(query)
    if (resource === 'contacts/') {
      if (query === undefined) return await tryInternalRead(resource) ?? { contacts: [], worker_directory_only: false }
      return request(requestForSource(path.slice(chatRoot.length), input, 'internal'))
    }
    if (resource === 'conversations/') {
      if (method === 'get') return conversationList(query)
      const peer = Number((input.body as { peer_id?: unknown } | undefined)?.peer_id)
      if (!Number.isSafeInteger(peer) || peer <= 0) throw new Error('联系人无效')
      const room = await request<Conversation>(requestForSource(resource, input, 'internal'))
      internalConversations.add(room.id)
      return conversationForFrame('internal', room)
    }
    if (resource === 'sync/') return sync(Number(params.get('after') ?? 0))

    const matching = /^conversations\/([1-9]\d*)\/(messages\/|read\/)$/.exec(resource ?? '')
    if (!matching) return request(validated)
    const source = receptionSourceId(Number(matching[1]))
    const suffix = matching[2]
    if (!source || (source.scope === 'internal' && !fullInternal && !internalConversations.has(source.id))) {
      throw new HttpError(404, '聊天会话不存在')
    }
    const config = requestForConversation(source, suffix as 'messages/' | 'read/', input, params)
    if (suffix === 'read/') return request(config)
    if (method === 'post') {
      const result = await request<ChatMessage | MessagePage>(config)
      return 'messages' in result ? { ...result, messages: result.messages.map(message => messageForFrame(source.scope, message)) }
        : messageForFrame(source.scope, result)
    }
    const page = await request<MessagePage>(config)
    return { ...page, messages: page.messages.map(message => messageForFrame(source.scope, message)) }
  }

  return requestFromFrame
}
