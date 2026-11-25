// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { chatRealtimeOrigin, chatRequest } from '../chatTransport'
import { chatSessionSignal, confirmBrowserChatLogin, invalidateChatSession } from '../chatSession'
import { fetchMe } from '../auth'
import { request } from '../http'
import { AuthExpiredError, HttpError, NetworkError } from '../errors'
import { getInternalChatSettings, saveInternalChatSettings } from '../internalChatSettings'

const app = vi.hoisted(() => ({ hosted: false, grant: '', expire: vi.fn() }))
vi.mock('@/m/components/appChatWebView', () => ({
  get appChatHosted() { return app.hosted },
  get appChatGrant() { return app.grant },
  expireAppChatGrant: app.expire,
}))
vi.mock('../http', () => ({ request: vi.fn() }))

const browserUser = 7
const ticketPath = '/api/internal-chat/direct-ticket/'
const conversation = '/api/internal-chat/conversations/42/'
const order = '/api/internal-chat/orders/20261120000000000001/'
const destination = 'https://ceshi-chat.suqing.chat'
const fetcher = vi.fn<typeof fetch>()
const browserChannel = new class extends EventTarget {
  postMessage = vi.fn()
}()

function ticket(token = 'browser-token', expiresAt = Math.floor(Date.now() / 1000) + 60, userId = browserUser) {
  return { enabled: true, token, expires_at: expiresAt, ttl_seconds: 120, user_id: userId }
}

function envelope(data: unknown, status = 200, msg = ''): Response {
  return new Response(JSON.stringify({ code: status === 200 ? 0 : status, msg, data }), { status })
}

function dispatchedUrl(call = 0): string {
  return String(fetcher.mock.calls[call]?.[0])
}

function dispatchedOptions(call = 0): RequestInit | undefined {
  return fetcher.mock.calls[call]?.[1]
}

beforeEach(() => {
  vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', destination)
  vi.stubGlobal('BroadcastChannel', class { constructor() { return browserChannel } })
  document.cookie = 'csrftoken=synthetic-page-csrf'
  confirmBrowserChatLogin(browserUser)
  browserChannel.postMessage.mockClear()
  vi.stubGlobal('fetch', fetcher)
  vi.mocked(request).mockReset()
  fetcher.mockReset()
  app.hosted = false
  app.grant = ''
  app.expire.mockClear()
})

afterEach(() => {
  invalidateChatSession()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('聊天直连', () => {
  it('保留未配置构建及租户明确停用时的同域请求', async () => {
    vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', '')
    const config = { url: `${conversation}messages/`, method: 'post', data: { text: '原路径' } }
    vi.mocked(request).mockResolvedValueOnce({ id: 7 })
    await expect(chatRequest(config)).resolves.toEqual({ id: 7 })
    expect(request).toHaveBeenCalledExactlyOnceWith(config)
    expect(chatRealtimeOrigin()).toBeUndefined()

    vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', destination)
    vi.mocked(request).mockResolvedValueOnce({ enabled: false }).mockResolvedValueOnce({ id: 8 })
    await expect(chatRequest(config)).resolves.toEqual({ id: 8 })
    expect(request).toHaveBeenNthCalledWith(2, expect.objectContaining({ url: ticketPath, method: 'POST' }))
    expect(request).toHaveBeenNthCalledWith(3, expect.objectContaining({
      ...config, baseURL: '', headers: expect.objectContaining({ 'X-Chat-Page-User': String(browserUser) }),
    }))
    await expect(chatRealtimeOrigin()).resolves.toBeUndefined()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('聊天联系人设置读取和保存都携带页面账号，旧构建沿用原请求', async () => {
    const url = '/api/internal-chat/settings/'
    const data = { primary_contact_id: 18, backup_contact_ids: [21] }
    vi.mocked(request).mockResolvedValue({ ...data, candidates: [] })
    await getInternalChatSettings()
    await saveInternalChatSettings(data)
    const headers = { 'X-Chat-Page-User': String(browserUser) }
    expect(request).toHaveBeenNthCalledWith(1, { url, method: 'GET', baseURL: '', headers })
    expect(request).toHaveBeenNthCalledWith(2, { url, method: 'PUT', data, baseURL: '', headers })
    vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', '')
    await saveInternalChatSettings(data)
    expect(request).toHaveBeenNthCalledWith(3, { url, method: 'PUT', data })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('图片与消息直连，保留查询和原数据，只使用签发的短票', async () => {
    vi.mocked(request).mockResolvedValueOnce(ticket())
    fetcher.mockResolvedValueOnce(envelope({ image: 'image-data' })).mockResolvedValueOnce(envelope({ id: 8 }))
      .mockResolvedValueOnce(envelope({ messages: [] }))
    const image = { mime: 'image/png', data: 'iVBORw0KGgo=' }
    const results = await Promise.all([
      chatRequest({ url: `${conversation}messages/8/image/` }),
      chatRequest({ url: `${order}messages/`, method: 'POST', data: { image }, headers: { 'X-CSRFToken': 'local' } }),
      chatRequest({ url: `${conversation}messages/`, params: { before: 12 } }),
    ])
    expect(results).toEqual([{ image: 'image-data' }, { id: 8 }, { messages: [] }])
    expect(request).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ url: ticketPath }))
    expect(dispatchedUrl()).toBe(`${destination}/v1/browser/conversations/42/messages/8/image/`)
    expect(dispatchedOptions()).toMatchObject({
      credentials: 'omit', mode: 'cors', redirect: 'error', cache: 'no-store',
      headers: { Authorization: 'Bearer browser-token' },
    })
    expect(dispatchedUrl(1)).toBe(`${destination}/v1/browser/orders/20261120000000000001/messages/`)
    expect(dispatchedOptions(1)?.headers).toEqual({ Authorization: 'Bearer browser-token', 'Content-Type': 'application/json' })
    expect(dispatchedOptions(1)?.body).toBe(JSON.stringify({ image }))
    expect(dispatchedUrl(2)).toBe(`${destination}/v1/browser/conversations/42/messages/?before=12`)
    await expect(chatRealtimeOrigin()).resolves.toBe(destination)
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('浏览器时钟比服务器快时仍能签票并发送消息', async () => {
    const fromServer = ticket('clock-independent-ticket', 1_799_999_920)
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_200_000)
    try {
      vi.mocked(request).mockResolvedValueOnce(fromServer)
      fetcher.mockResolvedValueOnce(envelope({ id: 8 }))
      await expect(chatRequest({ url: `${conversation}messages/`, method: 'post', data: { text: '校时测试' } }))
        .resolves.toEqual({ id: 8 })
      expect(dispatchedOptions()?.headers).toMatchObject({ Authorization: 'Bearer clock-independent-ticket' })
    } finally {
      clock.mockRestore()
    }
  })

  it('首次补 CSRF 失败后，可由同一用户的成功资料请求补绑定', async () => {
    document.cookie = 'csrftoken=; Max-Age=0'
    confirmBrowserChatLogin(browserUser)
    const unread = { url: '/api/internal-chat/unread/' }
    vi.mocked(request).mockRejectedValueOnce(new NetworkError('合成超时'))
      .mockResolvedValueOnce({ user: { id: browserUser } })
    await expect(fetchMe()).resolves.toEqual({ id: browserUser })
    await expect(chatRequest(unread)).rejects.toBeInstanceOf(AuthExpiredError)

    vi.mocked(request).mockImplementationOnce(async () => {
      document.cookie = 'csrftoken=recovered-csrf'
      return null
    }).mockResolvedValueOnce({ user: { id: browserUser } }).mockResolvedValueOnce(ticket())
    fetcher.mockResolvedValueOnce(envelope({ unread_count: 1 }))
    await expect(fetchMe()).resolves.toEqual({ id: browserUser })
    await expect(chatRequest(unread)).resolves.toEqual({ unread_count: 1 })
  })

  it('登录响应与签票用户不同时阻止旧聊天发送', async () => {
    vi.mocked(request).mockResolvedValueOnce(ticket('different-browser-user', undefined, browserUser + 1))
    await expect(chatRequest({ url: `${conversation}messages/`, method: 'post', data: { text: '旧草稿' } }))
      .rejects.toBeInstanceOf(AuthExpiredError)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('原生 App WebView 的聊天授权留在工作室签票，通知数据使用短票', async () => {
    app.hosted = true
    app.grant = 'short-native-grant'
    vi.mocked(request).mockResolvedValueOnce(ticket('isolated-chat-token'))
    fetcher.mockResolvedValueOnce(envelope({ self_member_id: 7 }))
    const notification = '/api/internal-chat/notification-conversations/34/messages/'
    await chatRequest({ url: notification, params: { around: '9' } })
    expect(request).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      url: ticketPath, headers: { Authorization: 'Chat short-native-grant' }, skipUnauthorizedHandler: true,
    }))
    expect(dispatchedUrl()).toBe(`${destination}/v1/browser/notification-conversations/34/messages/?around=9`)
    expect(dispatchedOptions()?.headers).toEqual({ Authorization: 'Bearer isolated-chat-token' })
  })

  it('短票 401 时只重试读取，写入在后续操作时才重新取票', async () => {
    vi.mocked(request).mockResolvedValueOnce(ticket('old-token')).mockResolvedValueOnce(ticket('new-token'))
    fetcher.mockResolvedValueOnce(envelope(null, 401)).mockResolvedValueOnce(envelope({ messages: [] }))
      .mockResolvedValueOnce(envelope(null, 401))
    const config = { url: `${conversation}messages/` }
    await expect(chatRequest(config)).resolves.toEqual({ messages: [] })
    expect(dispatchedOptions(1)?.headers).toEqual({ Authorization: 'Bearer new-token' })
    await expect(chatRequest({ ...config, method: 'post', data: { text: '只发送一次' } })).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).toHaveBeenCalledTimes(2)
    expect(fetcher).toHaveBeenCalledTimes(3)
    vi.mocked(request).mockResolvedValueOnce(ticket('later-token'))
    fetcher.mockResolvedValueOnce(envelope({ id: 100 }))
    await expect(chatRequest(config)).resolves.toEqual({ id: 100 })
    expect(dispatchedOptions(3)?.headers).toEqual({ Authorization: 'Bearer later-token' })
  })

  it('直连服务失败和签票返回异常都不会回 Web 重发', async () => {
    vi.mocked(request).mockResolvedValueOnce({ enabled: true })
    const config = { url: `${conversation}messages/`, method: 'post', data: { text: '保持原始写入语义' } }
    await expect(chatRequest(config)).rejects.toBeInstanceOf(NetworkError)
    expect(fetcher).not.toHaveBeenCalled()
    vi.mocked(request).mockResolvedValueOnce(ticket())
    fetcher.mockRejectedValueOnce(new Error('disconnected')).mockResolvedValueOnce(envelope(null, 403, '订单权限已撤销'))
    await expect(chatRequest(config)).rejects.toBeInstanceOf(NetworkError)
    await expect(chatRequest(config)).rejects.toMatchObject({ status: 403, message: '订单权限已撤销' })
    expect(request).toHaveBeenCalledTimes(2)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('登录身份更换后，丢弃在途取得的旧票', async () => {
    let resolveOld!: (value: object) => void
    vi.mocked(request).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    const config = { url: '/api/internal-chat/unread/' }
    const previousRequest = chatRequest(config)
    invalidateChatSession()
    vi.mocked(request).mockResolvedValueOnce(ticket('new-identity'))
    fetcher.mockResolvedValueOnce(envelope({ unread_count: 2 }))
    await expect(chatRequest(config)).resolves.toEqual({ unread_count: 2 })
    resolveOld(ticket('previous-identity'))
    await expect(previousRequest).rejects.toBeInstanceOf(AuthExpiredError)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(dispatchedOptions()?.headers).toEqual({ Authorization: 'Bearer new-identity' })
  })

  it('其他标签通知后锁住旧聊天，后台身份刷新也不能拿新身份发送旧草稿', async () => {
    const signal = chatSessionSignal()
    browserChannel.dispatchEvent(new MessageEvent('message', { data: 'reset' }))
    expect(signal.aborted).toBe(true)
    expect(browserChannel.postMessage).not.toHaveBeenCalled()
    const send = { url: `${conversation}messages/`, method: 'post', data: { text: '旧草稿' } }
    await expect(chatRequest(send)).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).not.toHaveBeenCalled()

    vi.mocked(request).mockResolvedValueOnce({ user: { id: 8 } })
    await expect(fetchMe()).resolves.toEqual({ id: 8 })
    await expect(chatRequest(send)).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ url: '/api/auth/me' }))
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('跨标签通知到达前，旋转后的 Cookie 也不能给旧页面签票或复用旧票', async () => {
    const unread = { url: '/api/internal-chat/unread/' }
    vi.mocked(request).mockResolvedValueOnce(ticket())
    fetcher.mockResolvedValueOnce(envelope({ unread_count: 1 }))
    await expect(chatRequest(unread)).resolves.toEqual({ unread_count: 1 })
    document.cookie = 'csrftoken=another-login-csrf'
    await expect(chatRequest({ url: `${conversation}messages/`, method: 'post' })).rejects.toBeInstanceOf(AuthExpiredError)
    await expect(chatRequest(unread)).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('App 聊天授权变动时抛弃旧票和在途响应', async () => {
    app.hosted = true
    app.grant = 'original-grant'
    const config = { url: '/api/internal-chat/unread/' }
    vi.mocked(request).mockResolvedValueOnce(ticket('previous-ticket')).mockResolvedValueOnce(ticket('new-ticket'))
    fetcher.mockResolvedValueOnce(envelope({ unread_count: 1 }))
    await expect(chatRequest(config)).resolves.toEqual({ unread_count: 1 })

    let completePrevious!: (response: Response) => void
    fetcher.mockImplementationOnce(() => new Promise((resolve) => { completePrevious = resolve }))
      .mockResolvedValueOnce(envelope({ unread_count: 2 }))
    const previous = chatRequest(config)
    await Promise.resolve()
    app.grant = 'replacement-grant'
    invalidateChatSession()
    expect(dispatchedOptions(1)?.signal?.aborted).toBe(true)
    await expect(chatRequest(config)).resolves.toEqual({ unread_count: 2 })
    completePrevious(envelope({ unread_count: 1 }))
    await expect(previous).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ headers: { Authorization: 'Chat replacement-grant' } }))
    expect(dispatchedOptions(2)?.headers).toEqual({ Authorization: 'Bearer new-ticket' })
  })

  it('旧 App 授权迟到的 401 不会清除已经续期的授权', async () => {
    app.hosted = true
    app.grant = 'old-native-grant'
    const contact = { url: '/api/internal-chat/contacts/' }
    let rejectOld!: (error: Error) => void
    vi.mocked(request).mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject }))
    const old = chatRequest(contact)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ headers: { Authorization: 'Chat old-native-grant' } }))
    app.grant = 'renewed-native-grant'
    invalidateChatSession()
    rejectOld(new AuthExpiredError('旧授权已过期'))
    await expect(old).rejects.toBeInstanceOf(AuthExpiredError)
    expect(app.expire).not.toHaveBeenCalled()

    vi.mocked(request).mockRejectedValueOnce(new AuthExpiredError('当前授权已过期'))
    await expect(chatRequest(contact)).rejects.toBeInstanceOf(AuthExpiredError)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ headers: { Authorization: 'Chat renewed-native-grant' } }))
    expect(app.expire).toHaveBeenCalledTimes(1)
  })

  it('目录、建私聊和签实时票仍同源，未知直连域名拒绝发票', async () => {
    vi.mocked(request).mockResolvedValue({})
    for (const config of [
      { url: '/api/internal-chat/contacts/' },
      { url: '/api/internal-chat/conversations/', method: 'post' },
      { url: '/api/internal-chat/realtime-ticket/', method: 'post' },
    ]) await chatRequest(config)
    expect(request).toHaveBeenCalledTimes(3)
    for (const [config] of vi.mocked(request).mock.calls) {
      expect(config).toMatchObject({ baseURL: '', headers: { 'X-Chat-Page-User': String(browserUser) } })
    }
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: ticketPath }))
    vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', 'https://chat.suqing.chat.invalid.test')
    await expect(chatRequest({ url: `${order}messages/` })).rejects.toBeInstanceOf(NetworkError)
    expect(request).toHaveBeenCalledTimes(3)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('账号核对头只发送给同源聊天接口', async () => {
    vi.mocked(request).mockResolvedValue({})
    const own = { url: '/api/internal-chat/contacts/', baseURL: destination, headers: { Accept: 'application/json' } }
    const other = { url: destination + '/v1/browser/unread/' }
    const nonChat = { url: '/api/auth/me' }
    await chatRequest(own)
    await chatRequest(other)
    await chatRequest(nonChat)
    expect(request).toHaveBeenNthCalledWith(1, {
      ...own, baseURL: '', headers: expect.objectContaining({ Accept: 'application/json', 'X-Chat-Page-User': String(browserUser) }),
    })
    expect(request).toHaveBeenNthCalledWith(2, other)
    expect(request).toHaveBeenNthCalledWith(3, nonChat)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('途中禁用直连时，不把本次已出站的读请求回源', async () => {
    vi.mocked(request).mockResolvedValueOnce(ticket()).mockResolvedValueOnce({ enabled: false })
    fetcher.mockResolvedValueOnce(envelope(null, 401))
    await expect(chatRequest({ url: `${conversation}messages/` })).rejects.toBeInstanceOf(HttpError)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(vi.mocked(request).mock.calls.map(([config]) => config.url)).toEqual([ticketPath, ticketPath])
  })
})
