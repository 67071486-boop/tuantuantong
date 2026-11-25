// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { expect, it, vi } from 'vitest'
import { HttpError } from '@/api/errors'
import { confirmBrowserChatLogin, invalidateChatSession } from '@/api/chatSession'
import { request } from '@/api/http'
import { attachLiveChatPreview, webKLiveOrigin } from '../internalChat/liveChatPreview'

vi.mock('@/api/http', () => ({
  request: vi.fn().mockResolvedValue({ conversations: [] }),
}))

it('opens the latest requested conversation once the iframe is ready and accepts only trusted order links', () => {
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const openOrder = vi.fn()
  const connection = attachLiveChatPreview(ref(iframe), ref(true), openOrder)
  const fromFrame = (data: object, origin = webKLiveOrigin, source: Window = frame) => {
    window.dispatchEvent(new MessageEvent('message', { data, origin, source }))
  }

  try {
    fromFrame({ type: 'tt:chat.ready' })
    connection.openConversation(5)
    connection.openConversation(8)
    expect(send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'tt:chat.openConversation' }), webKLiveOrigin)

    fromFrame({ type: 'tt:chat.navigationReady' }, 'http://invalid.test')
    expect(send).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'tt:chat.openConversation' }), webKLiveOrigin)
    fromFrame({ type: 'tt:chat.navigationReady' })
    expect(send).toHaveBeenLastCalledWith({ type: 'tt:chat.openConversation', detail: 8 }, webKLiveOrigin)

    const order = { type: 'tt:chat.openOrder', detail: '20261116000000000826' }
    fromFrame(order, webKLiveOrigin, window)
    fromFrame({ ...order, detail: '/unrelated' })
    expect(openOrder).not.toHaveBeenCalled()
    fromFrame(order)
    expect(openOrder).toHaveBeenCalledExactlyOnceWith(order.detail)
  } finally {
    connection.dispose()
    send.mockRestore()
    iframe.remove()
  }
})

it('只允许可信聊天窗口发送明确的群置顶请求', async () => {
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const connection = attachLiveChatPreview(ref(iframe), ref(true), vi.fn())
  const path = '/api/internal-chat/conversations/42/pin/'
  const pin = { type: 'tt:chat.request', id: 1, method: 'post', path, body: { message_id: 44 } }
  const fromFrame = (data: object, origin = webKLiveOrigin, source: Window = frame) => {
    window.dispatchEvent(new MessageEvent('message', { data, origin, source }))
  }

  try {
    fromFrame(pin, 'http://invalid.test')
    fromFrame(pin, webKLiveOrigin, window)
    fromFrame({ ...pin, id: 2, method: 'get' })
    fromFrame({ ...pin, id: 3, path: 'http://invalid.test' + path })
    fromFrame({ ...pin, id: 4, path: path + '?message_id=44' })
    fromFrame({ ...pin, id: 5, path: '/api/internal-chat/conversations/0/pin/' })
    expect(vi.mocked(request).mock.calls.filter(([config]) => config.method === 'post')).toEqual([])

    vi.mocked(request).mockResolvedValueOnce({ pinned_message_id: 44 })
    fromFrame(pin)
    expect(request).toHaveBeenCalledWith({ url: path, method: 'post', data: pin.body })
    await flushPromises()
    expect(send).toHaveBeenCalledWith({
      type: 'tt:chat.response', id: 1, result: { pinned_message_id: 44 },
    }, webKLiveOrigin)
  } finally {
    connection.dispose()
    send.mockRestore()
    iframe.remove()
  }
})

it('does not authenticate the old desktop connection with the new browser session ticket', async () => {
  const done = vi.fn()
  const replaced = vi.fn()
  const gateway = {
    on: vi.fn().mockReturnThis(),
    connect: vi.fn().mockReturnThis(),
    disconnect: vi.fn().mockReturnThis(),
  }
  const factory = vi.fn(({ auth }: { auth: (callback: typeof done) => void }) => {
    auth(done)
    return gateway
  })
  vi.stubGlobal('io', factory)
  vi.mocked(request).mockImplementation(async (config) => (
    config.url?.endsWith('realtime-ticket/')
      ? { token: 'another-session', user: { id: 20, identity_plane: 'demo-studio' } }
      : { conversations: [] }
  ))
  const connection = attachLiveChatPreview(ref(), ref(false), vi.fn(), {
    identity: { id: 19, identity_plane: 'demo-studio' },
    onIdentityChanged: replaced,
  })
  try {
    document.querySelector<HTMLScriptElement>('script[src="/socket.io/socket.io.js"]')!.dispatchEvent(new Event('load'))
    await flushPromises()
    expect(replaced).toHaveBeenCalledTimes(1)
    expect(gateway.disconnect).toHaveBeenCalledTimes(1)
    expect(done).not.toHaveBeenCalled()
  } finally {
    connection.dispose()
    vi.unstubAllGlobals()
  }
})

it('正式站只接收同源的真实聊天 iframe 请求，不接受其他窗口或预览来源', async () => {
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const connection = attachLiveChatPreview(ref(iframe), ref(true), vi.fn(), { production: true })
  const path = '/api/internal-chat/conversations/42/messages/'
  const message = { type: 'tt:chat.request', id: 1, method: 'get', path }
  const fromFrame = (origin: string, source: Window) => {
    window.dispatchEvent(new MessageEvent('message', { data: message, origin, source }))
  }

  try {
    fromFrame(webKLiveOrigin, frame)
    fromFrame(location.origin, window)
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: path }))

    vi.mocked(request).mockResolvedValueOnce({ messages: [] })
    fromFrame(location.origin, frame)
    expect(request).toHaveBeenCalledWith({ url: path, method: 'get', data: undefined })
    await flushPromises()
    expect(send).toHaveBeenCalledWith({
      type: 'tt:chat.response', id: 1, result: { messages: [] },
    }, location.origin)

    vi.mocked(request).mockRejectedValueOnce(new HttpError(404, '消息不存在'))
    fromFrame(location.origin, frame)
    await flushPromises()
    expect(send).toHaveBeenLastCalledWith({
      type: 'tt:chat.response', id: 1, error: '消息不存在', errorStatus: 404,
    }, location.origin)
  } finally {
    connection.dispose()
    send.mockRestore()
    iframe.remove()
  }
})

it('订单状态和重开仅转发当前可信窗口的指定接口，关闭窗口后不能重开', async () => {
  vi.mocked(request).mockReset().mockResolvedValue({ conversations: [] })
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const isOpen = ref(true)
  const connection = attachLiveChatPreview(ref(iframe), isOpen, vi.fn(), { production: true })
  const path = '/api/internal-chat/orders/20261120000000000001/'
  const fromFrame = (data: object, source: Window = frame) => window.dispatchEvent(new MessageEvent('message', {
    data, source, origin: location.origin,
  }))
  try {
    fromFrame({ type: 'tt:chat.request', id: 1, method: 'get', path })
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: path, method: 'get', data: undefined })
    fromFrame({ type: 'tt:chat.request', id: 2, method: 'post', path: path + 'reopen/' })
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: path + 'reopen/', method: 'post', data: undefined })
    const prior = vi.mocked(request).mock.calls.length
    for (const invalid of [path + 'archive/', path + 'reopen/?order=another', path.replace('20261120000000000001', '0')]) {
      fromFrame({ type: 'tt:chat.request', id: 3, method: 'post', path: invalid })
    }
    fromFrame({ type: 'tt:chat.request', id: 4, method: 'post', path: path + 'reopen/' }, window)
    isOpen.value = false
    fromFrame({ type: 'tt:chat.request', id: 5, method: 'post', path: path + 'reopen/' })
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(prior)
    expect(send).toHaveBeenLastCalledWith({ type: 'tt:chat.response', id: 5,
      error: '请打开聊天后再重开群聊', errorStatus: undefined }, location.origin)
  } finally { connection.dispose(); send.mockRestore(); iframe.remove() }
})


it('聊天窗口补回同一用户的 CSRF 后重新确认身份并恢复实时连接', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', 'https://ceshi-chat.suqing.chat')
  document.cookie = 'csrftoken=; Max-Age=0'
  confirmBrowserChatLogin(7)
  vi.mocked(request).mockReset().mockImplementation(async ({ url }) => {
    if (url === '/api/auth/csrf') {
      document.cookie = 'csrftoken=restored-for-the-same-user'
      return null
    }
    if (url === '/api/internal-chat/direct-ticket/') return { enabled: false }
    return { user: { id: 7, display_name: '测试客服' } }
  })
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const send = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
  const connection = attachLiveChatPreview(ref(iframe), ref(true), vi.fn(), { production: true })
  try {
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'tt:chat.request', id: 1, method: 'get', path: '/api/auth/me' },
      source: frame, origin: location.origin,
    }))
    await flushPromises()
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/api/auth/csrf' }))
    expect(send).toHaveBeenCalledWith({
      type: 'tt:chat.response', id: 1, result: { user: { id: 7, display_name: '测试客服' } },
    }, location.origin)
    await vi.advanceTimersByTimeAsync(1500)
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/api/internal-chat/direct-ticket/' }))
  } finally {
    connection.dispose()
    send.mockRestore()
    vi.useRealTimers()
    document.cookie = 'csrftoken=; Max-Age=0'
    vi.unstubAllEnvs()
    iframe.remove()
  }
})

it('合并密集的未读刷新并在读完消息或切换登录后回读最新数字', async () => {
  const pending: Array<{ resolve: (value: { unread_count: number }) => void; reject: (error: Error) => void }> = []
  const iframe = document.createElement('iframe')
  document.body.append(iframe)
  const frame = iframe.contentWindow!
  const counts: number[] = []
  const recordUnread = (event: Event) => counts.push((event as CustomEvent<number>).detail)
  const reconnect = () => window.dispatchEvent(new Event('online'))
  vi.mocked(request).mockImplementation(({ url }) => {
    if (url !== '/api/internal-chat/unread/') return Promise.resolve({})
    return new Promise((resolve, reject) => pending.push({ resolve, reject }))
  })
  window.addEventListener('tt:chat-unread', recordUnread)
  const connection = attachLiveChatPreview(ref(iframe), ref(true), vi.fn())

  try {
    reconnect()
    reconnect()
    reconnect()
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 1 })
    await flushPromises()
    expect(counts).toEqual([1])
    expect(pending).toHaveLength(1)
    reconnect()
    reconnect()
    pending.shift()!.resolve({ unread_count: 3 })
    await flushPromises()
    expect(counts).toEqual([1, 3])
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 4 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4])
    expect(pending).toHaveLength(0)

    reconnect()
    pending.shift()!.reject(new Error('offline'))
    await flushPromises()
    expect(counts).toEqual([1, 3, 4])
    expect(pending).toHaveLength(0)

    reconnect()
    window.dispatchEvent(new MessageEvent('message', {
      origin: webKLiveOrigin, source: frame,
      data: { type: 'tt:chat.request', id: 1, method: 'post', path: '/api/internal-chat/conversations/42/read/', body: { seq: 7 } },
    }))
    await flushPromises()
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 4 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4, 4])
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 0 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4, 4, 0])

    reconnect()
    invalidateChatSession()
    reconnect()
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 5 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4, 4, 0])
    expect(pending).toHaveLength(1)
    pending.shift()!.resolve({ unread_count: 2 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4, 4, 0, 2])
    expect(pending).toHaveLength(0)

    reconnect()
    reconnect()
    expect(pending).toHaveLength(1)
    connection.dispose()
    pending.shift()!.resolve({ unread_count: 9 })
    await flushPromises()
    expect(counts).toEqual([1, 3, 4, 4, 0, 2])
    expect(pending).toHaveLength(0)
  } finally {
    connection.dispose()
    window.removeEventListener('tt:chat-unread', recordUnread)
    iframe.remove()
    vi.mocked(request).mockResolvedValue({ conversations: [] })
  }
})
