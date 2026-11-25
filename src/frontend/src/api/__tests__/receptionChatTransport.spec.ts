import { describe, expect, it, vi } from 'vitest'
import type { AxiosRequestConfig } from 'axios'
import { HttpError, NetworkError } from '../errors'
import { createReceptionChatTransport } from '../receptionChatTransport'

const api = '/api/internal-chat/'
const desk = api + 'visitor/desk/'
const visitorMessage = { id: 3, conversation_id: 7, reply_to_id: 2, seq: 9, text: '访客消息' }
const staffMessage = { ...visitorMessage, text: '员工消息' }
const visitorRoom = { id: 7, last_message: visitorMessage, first_unread_message_id: 3 }
const staffRoom = { id: 7, last_message: staffMessage, first_unread_message_id: 3 }

describe('reception native directory and chats', () => {
  it('combines normal and visitor pages without dropping order or system conversations', async () => {
    const request = vi.fn(async ({ url }: AxiosRequestConfig) => {
      switch (url) {
        case desk + 'conversations/?limit=1&offset=0':
          return { cursor: 30, conversations: [visitorRoom], has_more: false }
        case desk + 'conversations/?limit=1&offset=1':
          return { cursor: 30, conversations: [], has_more: false }
        case api + 'conversations/?limit=1&offset=0':
          return { cursor: 80, conversations: [staffRoom], has_more: true, next_offset: 1 }
        case api + 'conversations/?limit=1&offset=1':
          return { cursor: 81, conversations: [{ id: 8, kind: 'order', pinned_message_id: 4, last_message: null },
            { id: 9, kind: 'system', last_message: null }], has_more: false }
        case desk + 'sync/?after=30':
          return { cursor: 31, changes: [], revoked_conversation_ids: [7], has_more: false }
        case api + 'sync/?after=80':
          return { cursor: 81, changes: [{ kind: 'conversation.removed', conversation_id: 8 }],
            order_visibility_token: 'access-changed', has_more: false }
        default: throw new Error(`unexpected URL ${url}`)
      }
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0], { fullInternal: true })
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('conversations/?limit=1&offset=0')).toMatchObject({
      cursor: 1, has_more: true, next_offset: 1, conversations: [{ id: 14 }, { id: 15 }],
    })
    expect(await get('conversations/?limit=1&offset=1')).toMatchObject({
      cursor: 1, has_more: false, conversations: [{ id: 17, kind: 'order', pinned_message_id: 9 }, { id: 19, kind: 'system' }],
    })
    expect(await get('sync/?after=1')).toMatchObject({
      cursor: 2, changes: [{ kind: 'conversation.removed', conversation_id: 17 }],
      order_visibility_token: 'access-changed', revoked_conversation_ids: [7],
    })
  })

  it('keeps native order controls, message search and album IDs in the internal conversation namespace', async () => {
    const orderNumber = '20000101000000000001'
    const request = vi.fn(async ({ url }: AxiosRequestConfig) => {
      if (url === api + `orders/${orderNumber}/reopen/`) return { ...staffRoom, kind: 'order', pinned_message_id: 3 }
      if (url === api + 'conversations/7/messages/search/?q=abc&before=9') return { messages: [staffMessage], count: 1 }
      if (url === api + 'conversations/7/messages/') return { messages: [staffMessage] }
      if (url === api + 'conversations/7/messages/3/revoke/') return staffMessage
      if (url === api + 'conversations/7/pin/') return { pinned_message_id: 3 }
      if (url === api + 'conversations/7/preferences/') return { pinned: true }
      throw new Error(`unexpected URL ${url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0], { fullInternal: true })
    const post = (path: string, body: unknown) => send({ path: api + path, method: 'post', body })
    expect(await post(`orders/${orderNumber}/reopen/`, undefined)).toMatchObject({ id: 15, pinned_message_id: 7 })
    expect(await send({ path: api + 'conversations/15/messages/search/?q=abc&before=9', method: 'get' })).toMatchObject({
      messages: [{ id: 7, conversation_id: 15, reply_to_id: 5, seq: 9 }], count: 1,
    })
    expect(await post('conversations/15/messages/', { reply_to_id: 5, images: ['example'] })).toMatchObject({ messages: [{ id: 7 }] })
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: api + 'conversations/7/messages/', data: { reply_to_id: 2, images: ['example'] } }))
    expect(await post('conversations/15/messages/7/revoke/', undefined)).toMatchObject({ id: 7 })
    expect(await post('conversations/15/pin/', { message_id: 7 })).toEqual({ pinned_message_id: 7 })
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: api + 'conversations/7/pin/', data: { message_id: 3 } }))
    expect(await post('conversations/15/preferences/', { pinned: true })).toEqual({ pinned: true })
    await expect(post('conversations/15/pin/', { message_id: 6 })).rejects.toThrow()
    await expect(post('conversations/14/preferences/', { pinned: true })).rejects.toThrow()
  })

  it('keeps internal conversations live and replays visitor changes after a visitor-only outage', async () => {
    let visitorOffline = true
    const request = vi.fn(async ({ url }: AxiosRequestConfig) => {
      if (url === desk + 'conversations/') throw new HttpError(503, '访客服务暂时不可用')
      if (url === api + 'conversations/') return { cursor: 60, conversations: [staffRoom] }
      if (url === desk + 'sync/?after=0') {
        if (visitorOffline) throw new NetworkError('连接中断')
        return { cursor: 12, changes: [{ kind: 'message.created', conversation_id: 7, message: visitorMessage }] }
      }
      if (url === api + 'sync/?after=60' || url === api + 'sync/?after=61') return { cursor: 61, changes: [] }
      throw new Error(`unexpected URL ${url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0], { fullInternal: true })
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('conversations/')).toMatchObject({ cursor: 1, conversations: [{ id: 15 }] })
    expect(await get('sync/?after=1')).toMatchObject({ cursor: 2, changes: [] })
    visitorOffline = false
    expect(await get('sync/?after=2')).toMatchObject({ cursor: 3, changes: [{ conversation_id: 14, message: { id: 6 } }] })
  })

  it('marks an incomplete order list and replays the internal cursor without holding visitor updates', async () => {
    let internalDown = false
    let authorityDown = false
    const request = vi.fn(async ({ url }: AxiosRequestConfig) => {
      if (url === desk + 'conversations/?limit=1&offset=0') {
        return { cursor: 30, conversations: [visitorRoom], has_more: true, next_offset: 1 }
      }
      if (url === desk + 'conversations/?limit=1&offset=1') return { cursor: 30, conversations: [] }
      if (url === api + 'conversations/?limit=1&offset=0') {
        if (internalDown) throw new HttpError(503, '暂时无法读取会话列表')
        return { cursor: 80, conversations: [{ ...staffRoom, kind: 'order' }] }
      }
      if (url === desk + 'sync/?after=30') {
        return { cursor: 31, changes: [{ kind: 'message.created', conversation_id: 7, message: visitorMessage }] }
      }
      if (url === desk + 'sync/?after=31') return { cursor: 31, changes: [] }
      if (url === api + 'sync/?after=80') {
        if (internalDown) throw new NetworkError('连接中断')
        if (authorityDown) {
          return { cursor: 82, has_more: true, changes: [], order_permission_unavailable: true, order_visibility_token: null }
        }
        return { cursor: 82, changes: [{ kind: 'conversation.removed', conversation_id: 7 }],
          order_permission_unavailable: false, order_visibility_token: 'confirmed' }
      }
      throw new Error(`unexpected URL ${url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0],
      { fullInternal: true })
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('conversations/?limit=1&offset=0')).toMatchObject({ cursor: 1, conversations: [{ id: 14 }, { id: 15 }] })
    expect(await get('conversations/?limit=1&offset=1')).not.toMatchObject({ order_permission_unavailable: true })
    internalDown = true
    expect(await get('conversations/?limit=1&offset=0')).toMatchObject({
      cursor: 1, conversations: [{ id: 14 }], order_permission_unavailable: true,
    })
    expect(await get('sync/?after=1')).toMatchObject({
      cursor: 2, has_more: false, changes: [{ conversation_id: 14 }], order_permission_unavailable: true,
    })
    internalDown = false
    authorityDown = true
    const partial = await get('sync/?after=2')
    expect(partial).toMatchObject({ cursor: 2, has_more: false, changes: [], order_permission_unavailable: true })
    expect(partial).not.toMatchObject({ order_visibility_token: null })
    authorityDown = false
    expect(await get('sync/?after=2')).toMatchObject({
      cursor: 3, changes: [{ kind: 'conversation.removed', conversation_id: 15 }], order_visibility_token: 'confirmed',
    })
  })

  it('preserves a second visitor history page without fetching ordinary conversations again', async () => {
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      switch (config.url) {
        case desk + 'conversations/?limit=1&offset=0':
          return { cursor: 30, conversations: [visitorRoom], has_more: true, next_offset: 1 }
        case desk + 'conversations/?limit=1&offset=1':
          return { cursor: 31, conversations: [{ id: 8, last_message: null }], has_more: false, next_offset: 2 }
        case api + 'conversations/': return { cursor: 80, conversations: [staffRoom] }
        default: throw new Error(`unexpected URL ${config.url}`)
      }
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0])
    const get = (offset: number) => send({ method: 'get', path: api + 'conversations/?limit=1&offset=' + offset })
    expect(await get(0)).toMatchObject({ cursor: 1, conversations: [{ id: 14 }, { id: 15 }], has_more: true, next_offset: 1 })
    expect(await get(1)).toMatchObject({ cursor: 1, conversations: [{ id: 16 }], has_more: false, next_offset: 2 })
    expect(request.mock.calls.filter(([config]) => config.url === api + 'conversations/')).toHaveLength(1)
  })

  it('lets the original directory query the entire normal roster, and separates identical visitor and staff message IDs', async () => {
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      if (config.url === api + 'contacts/?q=colleague') return { contacts: [{ id: 61, name: '非接待员工' }] }
      if (config.url === desk + 'conversations/') return { cursor: 31, conversations: [visitorRoom] }
      if (config.url === api + 'conversations/' && config.method === 'get') {
        return { cursor: 72, conversations: [staffRoom, { id: 8, kind: 'order' }, { id: 9, kind: 'system' }] }
      }
      if (config.url === api + 'conversations/' && config.method === 'post') return staffRoom
      if (config.url === desk + 'conversations/7/messages/?around=2') return { messages: [visitorMessage] }
      if (config.url === api + 'conversations/7/messages/' && config.method === 'post') return staffMessage
      throw new Error(`unexpected URL ${config.url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0])
    const get = (path: string) => send({ method: 'get', path: api + path })
    const post = (path: string, body: unknown) => send({ method: 'post', path: api + path, body })

    expect(await get('contacts/?q=colleague')).toMatchObject({ contacts: [{ id: 61 }] })
    expect(await get('conversations/')).toMatchObject({
      conversations: [
        { id: 14, first_unread_message_id: 6, last_message: { id: 6, conversation_id: 14, reply_to_id: 4, seq: 9 } },
        { id: 15, first_unread_message_id: 7, last_message: { id: 7, conversation_id: 15, reply_to_id: 5, seq: 9 } },
      ],
    })
    expect(await post('conversations/', { peer_id: 61 })).toMatchObject({ id: 15 })
    expect(await get('conversations/14/messages/?around=4')).toMatchObject({ messages: [{ text: '访客消息', id: 6 }] })
    expect(await post('conversations/15/messages/', { reply_to_id: 5, text: '回复' })).toMatchObject({ text: '员工消息', id: 7 })
    expect(request).toHaveBeenCalledWith(expect.objectContaining({
      url: api + 'conversations/7/messages/', data: { reply_to_id: 2, text: '回复' },
    }))
    await expect(post('conversations/15/messages/', { reply_to_id: 4 })).rejects.toThrow('引用的消息不在当前会话')
    await expect(get('conversations/17/messages/')).rejects.toThrow('聊天会话不存在')
    await expect(post('conversations/14/transfer/', { target_id: 61 })).rejects.toThrow('访客会话暂不支持此操作')
  })

  it('replays the last page until acknowledged and keeps transfer revocation on the visitor side', async () => {
    let visitorCursor = 105
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      switch (config.url) {
        case desk + 'conversations/': return { cursor: 30, conversations: [visitorRoom] }
        case api + 'conversations/': return { cursor: 73, conversations: [staffRoom] }
        case desk + 'sync/?after=30':
          return { cursor: visitorCursor, has_more: false, changes: [], revoked_conversation_ids: [7] }
        case api + 'sync/?after=73': return {
          cursor: 80, has_more: true, changes: [{ kind: 'message.created', conversation_id: 7, message: staffMessage }],
        }
        case desk + 'sync/?after=106': return { cursor: 106, has_more: false, changes: [] }
        case api + 'sync/?after=80': return { cursor: 80, has_more: false, changes: [] }
        default: throw new Error(`unexpected URL ${config.url}`)
      }
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0])
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('conversations/')).toMatchObject({ cursor: 1 })
    expect(await get('sync/?after=1')).toMatchObject({
      cursor: 2, has_more: true, revoked_conversation_ids: [7],
      changes: [{ kind: 'message.created', conversation_id: 15, message: { id: 7 } }],
    })
    visitorCursor = 106
    expect(await get('sync/?after=1')).toMatchObject({ cursor: 2 })
    expect(await get('sync/?after=1')).toMatchObject({ cursor: 2 })
    expect(await get('sync/?after=2')).toMatchObject({ cursor: 2, has_more: false, changes: [] })
  })

  it('includes a first incoming staff message as soon as its direct room becomes visible', async () => {
    let firstMessage = false
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      if (config.url === desk + 'conversations/') return { cursor: 30, conversations: [visitorRoom] }
      if (config.url === api + 'conversations/') {
        return { cursor: 80, conversations: firstMessage ? [staffRoom] : [] }
      }
      if (config.url === desk + 'sync/?after=30') return { cursor: 30, changes: [] }
      if (config.url === api + 'sync/?after=80') return {
        cursor: 81, changes: [
          { kind: 'message.created', conversation_id: 7, message: staffMessage },
          { kind: 'conversation.removed', conversation_id: 9 },
        ],
      }
      throw new Error(`unexpected URL ${config.url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0])
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('conversations/')).toMatchObject({ cursor: 1, conversations: [{ id: 14 }] })
    firstMessage = true
    expect(await get('sync/?after=1')).toMatchObject({
      changes: [{ kind: 'message.created', conversation_id: 15, message: { id: 7 } }],
    })
  })

  it('keeps reception available during an ordinary chat outage and retries the same staff cursor', async () => {
    let availability: 'offline' | 'online' | 'forbidden' = 'offline'
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      if (config.url === desk + 'conversations/') return { cursor: 31, conversations: [visitorRoom] }
      if (config.url === desk + 'sync/?after=31' || config.url === desk + 'sync/?after=32') {
        return { cursor: 32, has_more: false, changes: [
          { kind: 'message.created', conversation_id: 7, message: visitorMessage },
        ] }
      }
      if (availability === 'offline') {
        if (config.url === api + 'contacts/') throw new NetworkError('连接中断')
        throw new HttpError(503, '内部沟通暂时不可用')
      }
      if (availability === 'forbidden') throw new HttpError(403, '登录状态已变更')
      if (config.url === api + 'conversations/') return { cursor: 10, conversations: [staffRoom] }
      if (config.url === api + 'sync/?after=0') return { cursor: 10, has_more: false, changes: [
        { kind: 'message.created', conversation_id: 7, message: staffMessage },
      ] }
      throw new Error(`unexpected URL ${config.url}`)
    })
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0])
    const get = (path: string) => send({ method: 'get', path: api + path })
    expect(await get('contacts/')).toMatchObject({ contacts: [] })
    expect(await get('conversations/')).toMatchObject({ cursor: 1, conversations: [{ id: 14 }] })
    expect(await get('sync/?after=1')).toMatchObject({
      cursor: 2, changes: [{ conversation_id: 14, message: { text: '访客消息' } }],
    })
    availability = 'online'
    expect(await get('sync/?after=2')).toMatchObject({
      cursor: 3, changes: [{ conversation_id: 14 }, { conversation_id: 15, message: { text: '员工消息' } }],
    })
    availability = 'forbidden'
    await expect(get('contacts/')).rejects.toThrow('登录状态已变更')
  })
})
