// @vitest-environment jsdom

import { AxiosHeaders, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'


const destination = 'https://order-finance.suqing.chat'
const orderRoute = '/api/business/orders/123/'
const credentialRoute = '/api/auth/order-finance-ticket/'
const grant = 'gAAAAAB-real_Fernet_shape=='
let captured: InternalAxiosRequestConfig[]
let ticket: unknown
let outbound: ReturnType<typeof vi.fn>
let auth: typeof import('../auth')
let http: typeof import('../http').http
let request: typeof import('../http').request
let setUnauthorizedHandler: typeof import('../http').setUnauthorizedHandler

const envelope = (data: unknown) => ({ code: 0, msg: '', data })
const adapter: AxiosAdapter = async (config) => {
  captured.push(config)
  const data = config.url === credentialRoute ? ticket
    : config.url === '/api/auth/me' ? { user: { id: 12 } }
      : 'tenant response'
  return { status: 200, statusText: 'OK', data: envelope(data), headers: new AxiosHeaders(), config }
}

beforeEach(async () => {
  vi.resetModules()
  vi.stubEnv('VITE_ORDER_FINANCE_DIRECT_ORIGIN', destination)
  vi.stubEnv('VITE_API_BASE', '')
  document.cookie = 'csrftoken=old-cookie; path=/'
  auth = await import('../auth')
  ;({http, request, setUnauthorizedHandler} = await import('../http'))
  captured = []
  ticket = { enabled: true, token: grant, ttl_seconds: 120, user_id: 12 }
  outbound = vi.fn(async (_request: Request) => new Response(JSON.stringify(envelope('finance response')), {
    headers: { 'Content-Type': 'application/json' },
  }))
  http.defaults.adapter = adapter
  http.defaults.env = { fetch: outbound as typeof fetch }
})

afterEach(() => {
  http.defaults.env = undefined
  vi.unstubAllEnvs()
})

describe('订单财务独立传输', () => {
  async function recoverAfterFirstBootstrap(account: number): Promise<void> {
    document.cookie = 'csrftoken=; max-age=0; path=/'
    let firstBootstrap = true
    let currentAccount = 12
    http.defaults.adapter = async (config) => {
      if (config.url === '/api/auth/csrf' && firstBootstrap) {
        firstBootstrap = false
        throw new Error('临时网络故障')
      }
      const response = await adapter(config)
      if (config.url === '/api/auth/me') response.data = envelope({ user: { id: currentAccount } })
      return response
    }
    await auth.fetchMe()
    currentAccount = account
    document.cookie = 'csrftoken=recovered-cookie; path=/'
  }

  it('首次 CSRF 引导失败后，同一账号的并发订单请求可以恢复', async () => {
    await recoverAfterFirstBootstrap(12)
    await expect(Promise.all([request({ url: orderRoute }), request({ url: orderRoute })]))
      .resolves.toEqual(['finance response', 'finance response'])
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(1)
    expect(captured.filter(config => config.url === '/api/auth/me')).toHaveLength(2)
  })

  it('首次 CSRF 引导失败后，另一个账号不能继续旧页面的订单请求', async () => {
    await recoverAfterFirstBootstrap(13)
    await expect(Promise.all([request({ url: orderRoute }), request({ url: orderRoute })]))
      .rejects.toMatchObject({ name: 'AuthExpiredError' })
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(0)
    expect(outbound).not.toHaveBeenCalled()
    expect(captured.filter(config => config.url === '/api/auth/me')).toHaveLength(2)
  })

  it('聊天与订单财务同时启用时共享一次登录确认，并在登出后同时拒绝请求', async () => {
    vi.stubEnv('VITE_CHAT_DIRECT_ORIGIN', 'https://chat.suqing.chat')
    document.cookie = 'csrftoken=; max-age=0; path=/'
    http.defaults.adapter = async (config) => {
      if (config.url === '/api/auth/csrf') document.cookie = 'csrftoken=renewed-cookie; path=/'
      const response = await adapter(config)
      if (config.url === '/api/internal-chat/direct-ticket/') {
        response.data = envelope({ enabled: true, token: 'chat-ticket', ttl_seconds: 60, user_id: 12 })
      }
      return response
    }
    const chatFetch = vi.fn(async () => new Response(JSON.stringify(envelope({ unread_count: 2 }))))
    vi.stubGlobal('fetch', chatFetch)
    const { chatRequest } = await import('../chatTransport')
    try {
      await auth.fetchMe()
      const chatInput = { url: '/api/internal-chat/unread/' }
      await expect(request({ url: orderRoute })).resolves.toBe('finance response')
      await expect(chatRequest(chatInput)).resolves.toEqual({ unread_count: 2 })
      expect(captured.filter(config => config.url === '/api/auth/csrf')).toHaveLength(1)
      expect(captured.filter(config => config.url === '/api/auth/me')).toHaveLength(1)
      expect(outbound.mock.calls[0]?.[0].url).toBe(destination + orderRoute)
      expect(chatFetch).toHaveBeenCalledWith(
        new URL('https://chat.suqing.chat/v1/browser/unread/'),
        expect.objectContaining({ credentials: 'omit', headers: { Authorization: 'Bearer chat-ticket' } }),
      )

      await auth.logout()
      await expect(request({ url: orderRoute })).rejects.toMatchObject({ name: 'AuthExpiredError' })
      await expect(chatRequest(chatInput)).rejects.toMatchObject({ name: 'AuthExpiredError' })
      expect(outbound).toHaveBeenCalledTimes(1)
      expect(chatFetch).toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('同源签票后让普通写请求和附件 Blob 直连，且不发送业务会话 Cookie', async () => {
    await auth.fetchMe()
    expect(await request({ url: orderRoute, method: 'post', data: { amount: 0 } })).toBe('finance response')
    const write = outbound.mock.calls[outbound.mock.calls.length - 1]?.[0] as Request
    expect(write.url).toBe(destination + orderRoute)
    expect(write.method).toBe('POST')
    expect(write.credentials).toBe('omit')
    expect(write.redirect).toBe('error')
    expect(write.headers.get('Authorization')).toBe('OrderFinance ' + grant)
    expect(write.headers.get('X-CSRFToken')).toBeNull()
    expect(await write.json()).toEqual({ amount: 0 })
    expect(captured.find((config) => config.url === credentialRoute)?.headers.get('X-CSRFToken')).toBe('old-cookie')

    outbound.mockResolvedValueOnce(new Response('archive', {
      headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="a.zip"' },
    }))
    const response = await http.get<Blob>(orderRoute + 'screenshots/zip', { responseType: 'blob' })
    expect(await response.data.text()).toBe('archive')
    expect(response.headers['content-disposition']).toContain('a.zip')
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(1)
  })

  it('未启用时保留旧地址，且跳过认证、手工同步、设置和外部平台', async () => {
    ticket = { enabled: false }
    await auth.fetchMe()
    expect(await request({ url: orderRoute })).toBe('tenant response')
    expect(await request({ url: orderRoute, method: 'patch', data: { amount: 0 } })).toBe('tenant response')
    expect(await request({ url: '/api/business/orders/xgj-receipt-sync.json', method: 'post' })).toBe('tenant response')
    expect(await request({ url: '/api/business/approvals/settings/' })).toBe('tenant response')
    expect(await request({ url: '/api/business/platform-integrations/' })).toBe('tenant response')
    expect(await request({ url: '/api/business/orders-fake/' })).toBe('tenant response')
    expect(outbound).not.toHaveBeenCalled()
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(1)
    expect(captured.find(config => config.url === orderRoute && config.method === 'patch')?.headers.get('X-CSRFToken')).toBe('old-cookie')
  })

  it('外部浏览器截图上传维持同域和原挑战凭据', async () => {
    expect(await request({ url: '/api/business/screenshot-browser/exchange/', method: 'post',
      headers: { 'X-Screenshot-Challenge': 'fixture-only' } })).toBe('tenant response')
    expect(captured.some(config => config.headers.get('X-Screenshot-Challenge') === 'fixture-only')).toBe(true)
    expect(captured.some(config => config.url === credentialRoute)).toBe(false)
    expect(outbound).not.toHaveBeenCalled()
  })

  it('浏览器账号轮换后锁住旧页面，刷新当前用户也无法用新账号发送旧草稿', async () => {
    await auth.fetchMe()
    expect(await request({ url: orderRoute })).toBe('finance response')
    document.cookie = 'csrftoken=replaced-cookie; path=/'
    await auth.fetchMe()

    await expect(request({ url: orderRoute, method: 'patch', data: { amount: 9 } }))
      .rejects.toMatchObject({ name: 'AuthExpiredError' })
    expect(outbound).toHaveBeenCalledTimes(1)
    expect(captured.some(config => config.url === orderRoute)).toBe(false)
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(1)
  })

  it('凭证用户与页面登录用户不同时拦下旧草稿', async () => {
    await auth.fetchMe()
    ticket = { enabled: true, token: grant, ttl_seconds: 120, user_id: 13 }
    await expect(request({ url: orderRoute, method: 'patch', data: { amount: 9 } }))
      .rejects.toMatchObject({ name: 'AuthExpiredError' })
    expect(outbound).not.toHaveBeenCalled()
  })

  it('写请求被拒后原样报错，随后才换新票，不自动重送或清除网站会话', async () => {
    const expire = vi.fn()
    setUnauthorizedHandler(expire)
    outbound.mockResolvedValueOnce(new Response(JSON.stringify({ code: 401, msg: '凭证过期', data: null }), {
      status: 401, headers: { 'Content-Type': 'application/json' },
    }))
    await auth.fetchMe()
    await expect(request({ url: orderRoute, method: 'patch', data: { amount: 9 } })).rejects.toMatchObject({
      name: 'AuthExpiredError', message: '凭证过期',
    })
    expect(expire).not.toHaveBeenCalled()
    expect(outbound).toHaveBeenCalledTimes(1)
    expect(captured.some(config => config.url === orderRoute)).toBe(false)
    expect(await request({ url: orderRoute })).toBe('finance response')
    expect(captured.filter(config => config.url === credentialRoute)).toHaveLength(2)
  })

  it('用 layui 的旧 JSON 返回结构筛选，拆去原 XHR 请求头且不接受无关路径', async () => {
    await auth.fetchMe()
    const { installOrderFinanceTableTransport } = await import('../../layui/orderFinanceTransport')
    const ajaxTransport = vi.fn()
    const jquery = { ajaxTransport }
    installOrderFinanceTableTransport(jquery)
    installOrderFinanceTableTransport(jquery)
    expect(ajaxTransport).toHaveBeenCalledTimes(1)
    expect(ajaxTransport.mock.calls[0]?.[0]).toBe('+*')

    const transportFactory = ajaxTransport.mock.calls[0]?.[1]
    expect(transportFactory({ url: '/api/auth/me', type: 'GET' })).toBeUndefined()
    expect(transportFactory({ url: orderRoute, type: 'DELETE' })).toBeUndefined()
    expect(transportFactory({ url: orderRoute, type: 'GET', dataTypes: ['script'] })).toBeUndefined()
    const transfer = transportFactory({ url: orderRoute + '?page=3&limit=10', type: 'GET', dataTypes: ['json'] })
    const browser = vi.fn().mockResolvedValue(new Response(JSON.stringify(envelope({ total: 1 })), {
      status: 200, statusText: 'OK', headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', browser)
    try {
      const result: unknown[] = await new Promise(resolve => {
        transfer.send({ Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': 'old-cookie' }, (...args: unknown[]) => resolve(args))
      })
      const [url, sent] = browser.mock.calls[0] ?? []
      expect(url).toBe(destination + orderRoute + '?page=3&limit=10')
      expect(sent.credentials).toBe('omit')
      expect(sent.headers.get('Authorization')).toBe('OrderFinance ' + grant)
      expect(sent.headers.get('X-Requested-With')).toBeNull()
      expect(sent.headers.get('X-CSRFToken')).toBeNull()
      expect(result.slice(0, 3)).toEqual([200, 'OK', { text: JSON.stringify(envelope({ total: 1 })) }])
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
