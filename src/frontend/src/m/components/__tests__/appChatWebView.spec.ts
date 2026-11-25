// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://example.test/m/"}
import { afterEach, expect, it, vi } from 'vitest'

const { request } = vi.hoisted(() => ({
  request: vi.fn().mockResolvedValue({ user: { id: 2 } }),
}))
vi.mock('@/api/http', () => ({ request }))

afterEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
  history.replaceState(null, '', '/m/')
})

it('移除聊天凭据片段，只向本站专用身份接口提交', async () => {
  history.replaceState(null, '', '/m/?appChat=1#chatGrant=demo-grant:date:signature')
  const { fetchMe } = await import('@/api/auth')
  expect(location.hash).toBe('')
  expect(await fetchMe()).toEqual({ id: 2 })
  expect(request).toHaveBeenCalledExactlyOnceWith({
    url: '/api/internal-chat/app/me/',
    baseURL: '',
    method: 'GET',
    headers: { Authorization: 'Chat demo-grant:date:signature', 'Cache-Control': 'no-cache' },
    skipUnauthorizedHandler: true,
  })
})

it('只打开普通打手网页时仍使用原来的浏览器会话', async () => {
  history.replaceState(null, '', '/m/')
  const { fetchMe } = await import('@/api/auth')
  await fetchMe()
  expect(request).toHaveBeenCalledExactlyOnceWith({
    url: '/api/auth/me',
    method: 'GET',
    skipUnauthorizedHandler: true,
  })
})

it('App 聊天承载页丢失凭据时不改用已存在的网页会话', async () => {
  history.replaceState(null, '', '/m/?appChat=1')
  const { appChatExpired } = await import('../appChatWebView')
  const { fetchMe } = await import('@/api/auth')
  expect(appChatExpired.value).toBe(true)
  await expect(fetchMe()).rejects.toThrow('聊天登录已过期')
  expect(request).not.toHaveBeenCalled()
})

it.each([
  ['/m/?appChat=1&appChatClose=1', true],
  ['/m/?appChat=1', false],
  ['/m/?appChatClose=1', false],
  ['/m/orders/?appChat=1&appChatClose=1', false],
])('只在声明支持关闭的 App 承载地址启用页面退出（%s）', async (url, expected) => {
  history.replaceState(null, '', url)
  const { appChatCloseEnabled } = await import('../appChatWebView')
  expect(appChatCloseEnabled).toBe(expected)
})

it.each([
  ['/m/?appChat=1&appChatClose=1&appChatRetain=1#chatGrant=ok:grant', true],
  ['/m/?appChat=1&appChatClose=1#chatGrant=ok:grant', false],
  ['/m/?appChatRetain=1', false],
])('只对明确声明复用的 App 聊天启用原生桥（%s）', async (url, expected) => {
  history.replaceState(null, '', url)
  const { appChatRetainEnabled } = await import('../appChatWebView')
  expect(appChatRetainEnabled).toBe(expected)
})

it('续上的聊天凭据立即替换旧值，撤销后不恢复网页会话', async () => {
  history.replaceState(null, '', '/m/?appChat=1&conversation=28&message=76#chatGrant=old:grant')
  const { appChatTarget, expireAppChatGrant, appChatExpired, replaceAppChatGrant } = await import('../appChatWebView')
  const { fetchMe } = await import('@/api/auth')
  expect(appChatTarget()).toEqual({ conversationId: 28, messageId: 76 })
  replaceAppChatGrant('new:grant')
  await fetchMe()
  expect(request.mock.lastCall?.[0].headers.Authorization).toBe('Chat new:grant')
  expireAppChatGrant()
  replaceAppChatGrant('too:late')
  expect(appChatExpired.value).toBe(true)
  await expect(fetchMe()).rejects.toThrow('聊天登录已过期')
  expect(request).toHaveBeenCalledTimes(1)
})

it('前台刷新授权，第二次刷新遭撤销后保持已失效状态', async () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  history.replaceState(null, '', '/m/?appChat=1#chatGrant=old:grant')
  const { ref } = await import('vue')
  const { attachLiveChatPreview } = await import('@/v2/internalChat/liveChatPreview')
  const { appChatExpired } = await import('../appChatWebView')
  const { AuthExpiredError } = await import('@/api/errors')
  let refreshes = 0
  request.mockImplementation(async (config: { url: string }) => {
    if (!config.url.endsWith('/refresh/')) return { conversations: [] }
    if (++refreshes > 1) throw new AuthExpiredError('聊天登录已过期')
    return { grant: 'new:grant' }
  })
  const connection = attachLiveChatPreview(ref(), ref(true), vi.fn(), { production: true })
  try {
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({
      url: '/api/internal-chat/app/refresh/',
      headers: { Authorization: 'Chat old:grant' },
    }))
    expect(appChatExpired.value).toBe(false)
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({
      url: '/api/internal-chat/app/refresh/',
      headers: { Authorization: 'Chat new:grant' },
    }))
    expect(appChatExpired.value).toBe(true)
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(refreshes).toBe(2)
  } finally {
    connection.dispose()
    document.querySelector('script[src="/socket.io/socket.io.js"]')?.remove()
    visibility.mockRestore()
    vi.useRealTimers()
  }
})
