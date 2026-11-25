// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { expect, it, vi } from 'vitest'
import { bindBrowserChatSession, browserChatCsrf, invalidateChatSession } from '@/api/chatSession'
import { request } from '@/api/http'
import { attachLiveChatPreview } from '../internalChat/liveChatPreview'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

it('只订阅当前可见会话并忽略切换后到达的旧票据和状态', async () => {
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const open = ref(true)
  let grantFirst: ((value: { token: string }) => void) | undefined
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url?.endsWith('/7/presence-ticket/')) {
      return new Promise(resolve => { grantFirst = resolve })
    }
    if (url?.endsWith('/8/presence-ticket/')) return { token: 'available-ticket' }
    return { conversations: [] }
  })

  const handlers = new Map<string, (value?: unknown) => void>()
  const gateway = {
    on(event: string, handler: (value?: unknown) => void) {
      handlers.set(event, handler)
      return this
    },
    emit: vi.fn(),
    connect() { handlers.get('connect')?.(); return this },
    disconnect: vi.fn(),
  }
  vi.stubGlobal('io', () => gateway)
  const connection = attachLiveChatPreview(ref(iframe), open, vi.fn(), { production: true })
  const fromFrame = (data: object, origin = location.origin, source: Window = frame) =>
    window.dispatchEvent(new MessageEvent('message', { data, origin, source }))
  const subscribe = (id: number) => ({ type: 'tt:chat.watchPresence', detail: id })

  try {
    document.querySelector('script[src="/socket.io/socket.io.js"]')!.dispatchEvent(new Event('load'))
    await flushPromises()
    fromFrame({ type: 'tt:chat.ready' })
    fromFrame(subscribe(7), 'http://not-this-studio.test')
    fromFrame(subscribe(7), location.origin, window)
    expect(grantFirst).toBeUndefined()
    fromFrame(subscribe(7))
    expect(grantFirst).toBeTypeOf('function')
    fromFrame(subscribe(8))
    await flushPromises()
    expect(gateway.emit).toHaveBeenCalledExactlyOnceWith('chat:presence.watch', 'available-ticket')
    grantFirst?.({ token: 'stale-ticket' })
    await flushPromises()
    expect(gateway.emit).not.toHaveBeenCalledWith('chat:presence.watch', 'stale-ticket')

    handlers.get('chat:presence')?.({ conversationId: 7, online: true })
    expect(send).not.toHaveBeenCalledWith(expect.objectContaining({ detail: { conversationId: 7, online: true } }), location.origin)
    handlers.get('chat:presence')?.({ conversationId: 8, online: false })
    expect(send).toHaveBeenCalledWith({
      type: 'tt:chat.presence', detail: { conversationId: 8, online: false },
    }, location.origin)

    open.value = false
    connection.reportOpen()
    expect(gateway.emit).toHaveBeenLastCalledWith('chat:presence.watch', null)
    open.value = true
    connection.reportOpen()
    fromFrame(subscribe(8))
    await flushPromises()
    expect(gateway.emit).toHaveBeenCalledTimes(3)
    expect(gateway.emit).toHaveBeenLastCalledWith('chat:presence.watch', 'available-ticket')
  } finally {
    connection.dispose()
    send.mockRestore()
    vi.unstubAllGlobals()
    iframe.remove()
  }
})

it('启用时从聊天域打开实时连接，身份变化后断开并忽略旧连接', async () => {
  const origin = 'https://ceshi-chat.suqing.chat'
  const identity = { id: 4, identity_plane: 'ceshi' }
  vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', origin)
  document.cookie = 'csrftoken=synthetic-page-csrf'
  bindBrowserChatSession(identity.id, browserChatCsrf())
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    code: 0, msg: '', data: { unread_count: 0 },
  }))))
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url?.endsWith('/direct-ticket/')) {
      return { enabled: true, token: 'browser-ticket', ttl_seconds: 120, user_id: identity.id }
    }
    return { token: 'realtime-ticket', user: identity }
  })
  const done = vi.fn()
  const incoming = vi.fn()
  const handlers = new Map<string, (value?: unknown) => void>()
  const gateway = {
    on(event: string, handler: (value?: unknown) => void) {
      handlers.set(event, handler)
      return this
    },
    connect: vi.fn(() => gateway),
    disconnect: vi.fn(),
  }
  const factory = vi.fn((_origin, options) => {
    queueMicrotask(() => options.auth(done))
    return gateway
  })
  vi.stubGlobal('io', factory)
  const connection = attachLiveChatPreview(ref(), ref(true), vi.fn(), {
    identity, onIncomingMessage: incoming, production: true,
  })
  try {
    await flushPromises()
    const script = document.querySelector<HTMLScriptElement>(`script[src="${origin}/socket.io/socket.io.js"]`)!
    expect(script.crossOrigin).toBe('anonymous')
    expect(script.integrity).toBe('sha384-Reot/vo8FFwN0x9+TUlfgfqhwfzvBM0YeCDPf977fZzLqc55oTKZGNv8BZA0LR4J')
    script.dispatchEvent(new Event('load'))
    await flushPromises()
    expect(factory).toHaveBeenCalledExactlyOnceWith(origin, expect.objectContaining({ transports: ['websocket'] }))
    expect(done).toHaveBeenCalledExactlyOnceWith({ token: 'realtime-ticket' })
    expect(gateway.connect).toHaveBeenCalledTimes(1)
    handlers.get('chat:message')?.({ id: 7 })
    handlers.get('chat:visitor-message')?.({ id: 8, conversationId: 12, scope: 'visitor' })
    handlers.get('chat:visitor-message')?.({ id: 16, conversationId: 12 })
    invalidateChatSession()
    expect(gateway.disconnect).toHaveBeenCalledTimes(1)
    handlers.get('chat:message')?.({ id: 9 })
    handlers.get('chat:visitor-message')?.({ id: 10, conversationId: 12, scope: 'visitor' })
    expect(incoming.mock.calls).toEqual([
      [{ id: 7 }],
      [{ id: 8, conversationId: 12, scope: 'visitor' }],
    ])
  } finally {
    connection.dispose()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  }
})


it('opens a legacy visitor notification through the direct reception route', async () => {
  vi.mocked(request).mockImplementation(async ({ url }) => url?.endsWith('/visitor/desk/me/')
    ? { seat_mode: false, can_receive: true } : { unread_count: 0 })
  const openLegacyVisitorConversation = vi.fn()
  const connection = attachLiveChatPreview(ref(), ref(true), vi.fn(), {
    identity: { id: 19, identity_plane: 'demo-studio' },
    visitorReception: true, production: true, openLegacyVisitorConversation,
  })
  try {
    connection.openVisitorConversation(43)
    await flushPromises()
    expect(openLegacyVisitorConversation).toHaveBeenCalledExactlyOnceWith(43)
  } finally {
    connection.dispose()
  }
})
