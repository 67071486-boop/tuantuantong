// @vitest-environment jsdom
/**
 * 手机端守卫的判据测试。⚠️ **环境是 jsdom**：身份门要真的调 `window.location.replace('/')` 并真的读写 `sessionStorage` 里的抑制标记——「打在真实出口上」需要一个浏览器环境。
 * location 在 jsdom 里只读 ⇒ 文件级换成可控替身（`replace` 是唯一被断言的方法）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
// 手机端守卫的提示走 Framework7 Toast（⛔ 不 import element-plus）；这里只桩它，断言打到调用参数上。
vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

import { request } from '@/api/http'
import { fetchMe } from '@/api/auth'
import { Toast } from '../../framework7Ui'
import { AuthExpiredError } from '@/api/errors'
import {
  readMobileRedirectSuppressed,
  redirectToMobileIfMobile,
  writeMobileRedirectSuppressed,
  type MobileRedirectStorage,
} from '@/mobileRedirect'
import { WORKER_ROLE, useSessionStore } from '@/stores/session'
import { installMGuards } from '../guards'

const me = vi.mocked(fetchMe)
const send = vi.mocked(request)
/** 工厂里造的是「可调用的 vi.fn + error/success/… 方法」，类型上给个窄口径好断言。 */
type ToastMock = ReturnType<typeof vi.fn> & { error: ReturnType<typeof vi.fn> }
const toast = Toast as unknown as ToastMock

/** 身份门的真实出口：非打手时守卫必须真的调 `window.location.replace('/')`。 */
const replace = vi.fn()
const assign = vi.fn()
const originalLocation = window.location
const mockLocation = { ...originalLocation, assign, replace }
beforeAll(() => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: mockLocation,
  })
})
afterAll(() => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: originalLocation,
  })
})

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

/**
 * 把 `window.sessionStorage` 换成替身跑一段，**跑完必须还原**（否则后面的用例全在看替身）。
 * jsdom 的这个属性可替换（已实测）；替身只用来模拟「写不进去」的两种形态（P1-2）。
 */
async function withSessionStorage(
  stub: MobileRedirectStorage,
  fn: () => Promise<void>,
): Promise<void> {
  const original = window.sessionStorage
  Object.defineProperty(window, 'sessionStorage', {
    configurable: true,
    writable: true,
    value: stub,
  })
  try {
    await fn()
  } finally {
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      writable: true,
      value: original,
    })
  }
}

const Blank = { template: '<div />' }

/** 镜像 `m/router.ts` 的路由形状（守卫测试不需要真组件）。 */
function makeMRouter(): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'm-login', component: Blank, meta: { requiresAuth: false } },
      { path: '/wxpusher/chat/:conversationId', name: 'm-wxpusher-chat', component: Blank },
      { path: '/wxpusher/open', name: 'm-wxpusher-open', component: Blank, meta: { requiresAuth: false } },
      { path: '/wxpusher/hall-entry', name: 'm-wxpusher-hall-entry', component: Blank, meta: { requiresAuth: false } },
      { path: '/wxpusher/return', name: 'm-wxpusher-return', component: Blank, meta: { requiresAuth: false } },
      { path: '', name: 'm-home', component: Blank },
      { path: '/hall', name: 'm-hall', component: Blank, meta: { permissions: ['grab_hall.take_order'] } },
      { path: '/mine', name: 'm-mine', component: Blank, meta: { permissions: ['my_orders', 'my_performance'] } },
      { path: '/channel', name: 'm-channel', component: Blank },
      {
        path: '/orders/:orderNo(\\d{20})', alias: '/wxpusher/orders/:orderNo(\\d{20})',
        name: 'm-order-detail', component: Blank, meta: { permissions: ['my_orders'] },
      },
      // M2：订单截图页（与桌面两颗入口按钮同一对节点，canNode 任一即进）。
      { path: '/orders/:orderNo(\\d{20})/screenshots', name: 'm-order-screenshots', component: Blank, meta: { permissions: ['order.view_screenshot', 'order.upload_screenshot'] } },
      // M3：「我的」聚合页 + 三个子页（反馈是 home.feedback，其余 my_profile——照桌面 routePermissions）。
      { path: '/me', name: 'm-me', component: Blank, meta: { permissions: ['my_profile'] } },
      { path: '/me/profile', name: 'm-me-profile', component: Blank, meta: { permissions: ['my_profile'] } },
      { path: '/me/notifications/order', name: 'm-me-notifications-order', component: Blank, meta: { permissions: ['my_profile'] } },
      { path: '/me/password', name: 'm-me-password', component: Blank, meta: { permissions: ['my_profile'] } },
      { path: '/me/feedback', name: 'm-me-feedback', component: Blank, meta: { permissions: ['home.feedback'] } },
      { path: '/empty', name: 'm-empty', component: Blank },
      { path: '/:pathMatch(.*)*', name: 'm-not-found', redirect: { name: 'm-empty' } },
    ],
  })
  installMGuards(router)
  return router
}

/** 合成夹具（E-0077：一律合成值）。按用例自己覆盖 menu_permissions / roles。 */
const booster = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  // 身份门（线 A）之后，进 `/m/` 的前提是「打手」；本文件既有的用例都在测节点门，
  // ⇒ 夹具默认就是打手（⛔ 不让身份门把这些用例的意图搅浑）。
  roles: [WORKER_ROLE],
  permissions: [],
  menu_permissions: [] as string[],
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
  send.mockReset()
  toast.mockClear()
  toast.error.mockClear()
  replace.mockClear()
  assign.mockReset()
  mockLocation.origin = originalLocation.origin
  // 抑制标记住在真 sessionStorage 里 ⇒ 每个用例自己收口。
  window.sessionStorage.clear()
  window.localStorage.clear()
})

describe('WxPusher 打手端入口', () => {
  const permissions = ['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system', 'my_profile']

  const orderPath = '/wxpusher/orders/10000000000000000002?entry=wxpusher'
  const androidUserAgent = 'Mozilla/5.0 Android/WxPusher-Android'
  const pendingOrderKey = 'ttt-wxpusher-order-continuation'
  const pendingHallKey = 'ttt-wxpusher-hall-continuation'

  const hallTicket = '.synthetic:ticket:a_b-c'
  const hallEntryPath = '/wxpusher/hall-entry?entry=wxpusher'
  const hallHash = '#hall=' + encodeURIComponent(hallTicket)
  const hallPath = hallEntryPath + hallHash
  const hallResume = '/api/auth/wxpusher/login?hallEntry=' + encodeURIComponent(hallTicket)
  const nativeUrl = 'daidaitong://suqing.suqing.chat/m/orders/10000000000000000002'
  const followUrl = 'https://wxpusher.zjiecode.com/app/#/follow?type=1&id=1&jump=auto'

  function storePendingOrder(expiresAt = Date.now() + 60_000) {
    window.sessionStorage.setItem(pendingOrderKey, JSON.stringify({ orderNo: '10000000000000000002', expiresAt }))
  }

  it('首次尝试打开代代通，打不开时保存本次订单的 H5 登录回跳', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      assign.mockImplementation(() => { throw new Error('未安装') })
      const router = makeMRouter()
      await router.push(orderPath)
      expect(assign).toHaveBeenCalledExactlyOnceWith('daidaitong://suqing.suqing.chat/m/orders/10000000000000000002')
      expect(router.currentRoute.value.name).toBe('m-login')
      expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: '/m/orders/10000000000000000002' })
    } finally {
      userAgent.mockRestore()
    }
  })

  it.each([
    ['visibilitychange', 'visibilitychange'],
    ['pagehide', 'pageshow'],
  ])('从独立网页打开原生订单后，回来时提示关闭网页且不发起网页登录（%s）', async (leave, back) => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const lifecycle = leave === 'visibilitychange' ? document : window
    const historyLength = vi.spyOn(window.history, 'length', 'get').mockReturnValue(1)
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      assign.mockImplementation(() => {
        if (leave === 'visibilitychange') visibility.mockReturnValue('hidden')
        lifecycle.dispatchEvent(new Event(leave))
      })
      const router = makeMRouter()
      const opening = router.push(orderPath)
      await vi.waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith(nativeUrl))
      expect(me).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
      expect(router.currentRoute.value.name).toBeUndefined()

      visibility.mockReturnValue('visible')
      lifecycle.dispatchEvent(new Event(back))
      await opening
      expect(router.currentRoute.value.fullPath).toBe('/wxpusher/return')
      expect(me).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
      expect(replace).not.toHaveBeenCalled()
      expect(assign).toHaveBeenCalledOnce()
    } finally {
      historyLength.mockRestore()
      visibility.mockRestore()
      userAgent.mockRestore()
    }
  })

  it('未观察到原生应用切换时自动申请系统登录并只在当前标签回原订单', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    vi.useFakeTimers()
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      send.mockResolvedValue({ url: followUrl })
      const router = makeMRouter()
      const opening = router.push(orderPath)
      await vi.advanceTimersByTimeAsync(1000)
      await opening
      expect(assign).toHaveBeenCalledExactlyOnceWith(nativeUrl)
      expect(send).toHaveBeenCalledExactlyOnceWith({
        method: 'POST', url: '/api/auth/wxpusher/orders/10000000000000000002/continue', skipUnauthorizedHandler: true,
      })
      expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)

      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      setActivePinia(createPinia())
      const callback = makeMRouter()
      await callback.push('/?entry=wxpusher')
      expect(callback.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
      expect(send).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
      userAgent.mockRestore()
    }
  })

  it.each([
    ['', true],
    ['https://wxpusher.zjiecode.com/wxuser/example', true],
    ['https://wxpusher.zjiecode.com.example.invalid/', false],
  ])('从订单 App 返回时只后退到原通知（来源 %s）', async (source, canGoBack) => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    const referrer = vi.spyOn(document, 'referrer', 'get').mockReturnValue(source)
    const historyLength = vi.spyOn(window.history, 'length', 'get').mockReturnValue(2)
    const historyBack = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    try {
      assign.mockImplementation(() => window.dispatchEvent(new Event('pagehide')))
      const router = makeMRouter()
      const opening = router.push(orderPath)
      await vi.waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith(nativeUrl))
      window.dispatchEvent(new Event('pageshow'))
      await opening
      expect(historyBack).toHaveBeenCalledTimes(canGoBack ? 1 : 0)
      expect(router.currentRoute.value.fullPath).toBe('/wxpusher/return')
      expect(replace).not.toHaveBeenCalled()
      expect(me).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
    } finally {
      historyBack.mockRestore()
      historyLength.mockRestore()
      referrer.mockRestore()
      userAgent.mockRestore()
    }
  })

  it('网页首次在后台打开时，等到前台后只尝试一次 App，切回才处理返回', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const lifecycle = vi.spyOn(document, 'addEventListener')
    try {
      const router = makeMRouter()
      const opening = router.push(orderPath)
      await vi.waitFor(() => expect(lifecycle).toHaveBeenCalledWith('visibilitychange', expect.any(Function)))
      expect(assign).not.toHaveBeenCalled()
      expect(me).not.toHaveBeenCalled()
      expect(replace).not.toHaveBeenCalled()

      visibility.mockReturnValue('visible')
      document.dispatchEvent(new Event('visibilitychange'))
      expect(assign).toHaveBeenCalledExactlyOnceWith(nativeUrl)
      expect(replace).not.toHaveBeenCalled()

      visibility.mockReturnValue('hidden')
      document.dispatchEvent(new Event('visibilitychange'))
      visibility.mockReturnValue('visible')
      document.dispatchEvent(new Event('visibilitychange'))
      await opening
      expect(router.currentRoute.value.fullPath).toBe('/wxpusher/return')
      expect(replace).not.toHaveBeenCalled()
      expect(assign).toHaveBeenCalledOnce()
      expect(me).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
    } finally {
      lifecycle.mockRestore()
      visibility.mockRestore()
      userAgent.mockRestore()
    }
  })

  const sharedHallPath = hallEntryPath + '&shared=1&source=peer&ref=23'
  const sharedHallClaim = '/hall?claimSource=peer&claimRef=23'

  it('共用抢单链接在现有打手会话中直接打开指定订单', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push(sharedHallPath)
    expect(router.currentRoute.value.fullPath).toBe(sharedHallClaim)
    expect(replace).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
  })

  it('共用抢单链接用官方对接，同一标签登录后仍打开原单', async () => {
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    send.mockResolvedValue({ url: followUrl, open_in_app: true })
    await makeMRouter().push(sharedHallPath)
    expect(send).toHaveBeenCalledExactlyOnceWith({
      method: 'GET', url: '/api/auth/wxpusher/hall/continue',
      params: { source: 'peer', ref: '23' }, skipUnauthorizedHandler: true,
    })
    expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)

    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    setActivePinia(createPinia())
    const callback = makeMRouter()
    await callback.push('/?entry=wxpusher')
    expect(callback.currentRoute.value.fullPath).toBe(sharedHallClaim)
  })

  it('共用抢单链接有 App 专用押金要求时，安卓也直接走 H5', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const agent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      send.mockResolvedValue({ url: followUrl, open_in_app: false })
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      const router = makeMRouter()
      await router.push(sharedHallPath)
      expect(send).toHaveBeenCalledOnce()
      expect(assign).not.toHaveBeenCalled()
      expect(router.currentRoute.value.fullPath).toBe(sharedHallClaim)
    } finally {
      agent.mockRestore()
    }
  })

  it('代代通 App 入口开放时，关闭网页免密仍可用共用链接打开 App', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const agent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      send.mockResolvedValue({ url: null, open_in_app: true })
      assign.mockImplementation(() => window.dispatchEvent(new Event('pagehide')))
      const router = makeMRouter()
      const opening = router.push(sharedHallPath)
      await vi.waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith(
        'daidaitong://suqing.suqing.chat/m/hall?source=peer&ref=23',
      ))
      window.dispatchEvent(new Event('pageshow'))
      await opening
      expect(router.currentRoute.value.name).toBe('m-wxpusher-return')
      expect(replace).not.toHaveBeenCalled()
      expect(me).not.toHaveBeenCalled()
    } finally {
      agent.mockRestore()
    }
  })

  it('共用抢单链接发现标签存储不可用时，保留同一订单的密码登录', async () => {
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    send.mockResolvedValue({ url: followUrl, open_in_app: true })
    const router = makeMRouter()
    await withSessionStorage({ getItem: () => null, setItem: () => {}, removeItem: () => {} }, async () => {
      await router.push(sharedHallPath)
    })
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: '/m' + sharedHallClaim })
    expect(replace).not.toHaveBeenCalled()
  })

  it('官方返回没有打手会话时保留原订单供显式重试', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    send.mockResolvedValue({ url: followUrl, open_in_app: true })
    await makeMRouter().push(sharedHallPath)
    setActivePinia(createPinia())
    const callback = makeMRouter()
    await callback.push('/login?entry=wxpusher')
    expect(callback.currentRoute.value.fullPath).toBe(sharedHallPath + '&retry=1')
    expect(replace).toHaveBeenCalledOnce()
    expect(send).toHaveBeenCalledOnce()
  })

  it('暂时取不到对接地址时保留共用链接供显式重试', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    send.mockRejectedValue(new Error('网络异常'))
    const router = makeMRouter()
    await router.push(sharedHallPath)
    expect(router.currentRoute.value.fullPath).toBe(sharedHallPath + '&retry=1')
    expect(replace).not.toHaveBeenCalled()
    expect(send).toHaveBeenCalledOnce()
  })

  it('共用抢单链接遇到已有员工会话时，保留会话并交给密码入口', async () => {
    me.mockResolvedValue({ ...booster, roles: ['客服'], menu_permissions: permissions })
    const router = makeMRouter()
    await router.push(sharedHallPath)
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: '/m' + sharedHallClaim })
    expect(send).not.toHaveBeenCalled()
    expect(replace).not.toHaveBeenCalled()
  })

  it('共用抢单链接拒绝不符合格式的订单标识或混入单人票据', async () => {
    for (const invalid of [sharedHallPath.replace('ref=23', 'ref=0023'), sharedHallPath + hallHash]) {
      const router = makeMRouter()
      await router.push(invalid)
      expect(router.currentRoute.value.name).toBe('m-wxpusher-hall-entry')
    }
    expect(me).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
    expect(assign).not.toHaveBeenCalled()
    expect(replace).not.toHaveBeenCalled()
  })

  it('抢单 App 只收到来源与标识，返回时回原通知且不发起网页登录', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    const historyLength = vi.spyOn(window.history, 'length', 'get').mockReturnValue(2)
    const historyBack = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    try {
      assign.mockImplementation(() => window.dispatchEvent(new Event('pagehide')))
      const router = makeMRouter()
      const opening = router.push(hallEntryPath + '&source=peer&ref=23' + hallHash)
      await vi.waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith(
        'daidaitong://suqing.suqing.chat/m/hall?source=peer&ref=23',
      ))
      window.dispatchEvent(new Event('pageshow'))
      await opening
      expect(historyBack).toHaveBeenCalledOnce()
      expect(router.currentRoute.value.fullPath).toBe('/wxpusher/return')
      expect(replace).not.toHaveBeenCalled()
      expect(me).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
    } finally {
      historyBack.mockRestore()
      historyLength.mockRestore()
      userAgent.mockRestore()
    }
  })

  it('抢单 App 未打开时启动系统对接，同一标签再回持票入口', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    vi.useFakeTimers()
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      send.mockResolvedValue({ url: followUrl })
      storePendingOrder()
      const router = makeMRouter()
      const opening = router.push(hallEntryPath + '&source=local&ref=10000000000000000002' + hallHash)
      await vi.advanceTimersByTimeAsync(1000)
      await opening
      expect(assign).toHaveBeenCalledExactlyOnceWith(
        'daidaitong://suqing.suqing.chat/m/hall?source=local&ref=10000000000000000002',
      )
      expect(send).toHaveBeenCalledExactlyOnceWith({
        method: 'GET', url: '/api/auth/wxpusher/login', params: { hallStart: hallTicket }, skipUnauthorizedHandler: true,
      })
      expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      setActivePinia(createPinia())
      const callback = makeMRouter()
      await callback.push('/?entry=wxpusher')
      expect(replace).toHaveBeenLastCalledWith(hallResume)
      const next = makeMRouter()
      await next.push('/?entry=wxpusher')
      expect(next.currentRoute.value.name).toBe('m-hall')
      expect(replace).toHaveBeenCalledTimes(2)
      expect(send).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
      userAgent.mockRestore()
    }
  })

  it.each([new AuthExpiredError('未认证'), new Error('网络异常')])(
    '抢单回调没有可用会话时保留原卡并等待手动重试（%s）',
    async (error) => {
      mockLocation.origin = 'https://suqing.suqing.chat'
      const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
      try {
        window.sessionStorage.setItem(pendingHallKey, JSON.stringify({ hall: hallTicket, expiresAt: Date.now() + 60_000 }))
        me.mockRejectedValueOnce(error)
        const callback = makeMRouter()
        await callback.push('/login?entry=wxpusher')
        const retry = callback.currentRoute.value
        expect(retry.name).toBe('m-wxpusher-hall-entry')
        expect(retry.query).toEqual({ entry: 'wxpusher', retry: '1' })
        expect(new URLSearchParams(retry.hash.slice(1)).get('hall')).toBe(hallTicket)
        expect(window.sessionStorage.getItem(pendingHallKey)).toBeNull()
        expect(replace).not.toHaveBeenCalled()
        expect(assign).not.toHaveBeenCalled()
        expect(send).not.toHaveBeenCalled()

        await makeMRouter().push(retry.fullPath)
        expect(replace).not.toHaveBeenCalled()
        expect(assign).not.toHaveBeenCalled()
        expect(send).not.toHaveBeenCalled()

        setActivePinia(createPinia())
        me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
        send.mockResolvedValue({ url: followUrl })
        await makeMRouter().push(hallPath)
        expect(send).toHaveBeenCalledExactlyOnceWith({
          method: 'GET', url: '/api/auth/wxpusher/login', params: { hallStart: hallTicket }, skipUnauthorizedHandler: true,
        })
        expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)
        expect(assign).not.toHaveBeenCalled()
      } finally {
        userAgent.mockRestore()
      }
    },
  )

  it('无有效原生提示的抢单链接直接走网页，已有会话时只恢复持票入口', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      const router = makeMRouter()
      await router.push(hallEntryPath + '&source=peer&ref=0023' + hallHash)
      expect(assign).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
      expect(replace).toHaveBeenCalledExactlyOnceWith(hallResume)
    } finally {
      userAgent.mockRestore()
    }
  })

  it('抢单对接失败留在链接页，拒绝自动打开第三方登录地址或密码页', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    send.mockResolvedValue({ url: 'https://other.example.invalid/app/#/follow?jump=auto' })
    const router = makeMRouter()
    await router.push(hallPath)
    expect(assign).not.toHaveBeenCalled()
    expect(send).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.fullPath).toBe(hallPath)
    expect(replace).not.toHaveBeenCalled()
    const callback = makeMRouter()
    await callback.push('/login?entry=wxpusher')
    expect(replace).not.toHaveBeenCalled()
  })

  it.each([
    ['https://suqing.suqing.chat', hallEntryPath + '&hall=' + encodeURIComponent(hallTicket)],
    ['https://suqing.suqing.chat', hallPath + '&hall=duplicate'],
    ['https://other.example.invalid', hallPath],
  ])('拒绝抢单查询票据、重复片段票据或非配置域名入口（%s, %s）', async (origin, path) => {
    mockLocation.origin = origin
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      window.sessionStorage.setItem(pendingHallKey, JSON.stringify({ hall: hallTicket, expiresAt: Date.now() + 60_000 }))
      const router = makeMRouter()
      await router.push(path)
      expect(router.currentRoute.value.name).toBe('m-wxpusher-hall-entry')
      expect(assign).not.toHaveBeenCalled()
      expect(replace).not.toHaveBeenCalled()
      expect(send).not.toHaveBeenCalled()
      expect(window.sessionStorage.getItem(pendingHallKey)).toBeNull()
    } finally {
      userAgent.mockRestore()
    }
  })

  it('旧扩展入口或过期抢单回跳均不会接管网页', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    window.sessionStorage.setItem(pendingHallKey, JSON.stringify({ hall: hallTicket, expiresAt: Date.now() + 60_000 }))
    const extension = makeMRouter()
    await extension.push('/wxpusher/open?entry=wxpusher')
    expect(extension.currentRoute.value.name).toBe('m-wxpusher-open')
    expect(replace).not.toHaveBeenCalled()

    window.sessionStorage.setItem(pendingHallKey, JSON.stringify({ hall: hallTicket, expiresAt: Date.now() - 60_000 }))
    const callback = makeMRouter()
    await callback.push('/?entry=wxpusher')
    expect(callback.currentRoute.value.name).toBe('m-hall')
    expect(replace).not.toHaveBeenCalled()
    expect(window.sessionStorage.getItem(pendingHallKey)).toBeNull()
  })

  it('自动登录不可用时保留同一订单的手动登录，拒绝离开到非官方地址', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      send.mockResolvedValue({ url: 'https://other.example.invalid/app/#/follow?jump=auto' })
      assign.mockImplementation(() => { throw new Error('未安装') })
      const router = makeMRouter()
      await router.push(orderPath)
      expect(assign).toHaveBeenCalledExactlyOnceWith(nativeUrl)
      expect(replace).not.toHaveBeenCalled()
      expect(window.sessionStorage.getItem(pendingOrderKey)).toBeNull()
      expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: '/m/orders/10000000000000000002' })
    } finally {
      userAgent.mockRestore()
    }
  })

  it('H5 会话已有效时回原订单，站内返回不重复唤起 App', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
    try {
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      const router = makeMRouter()
      await router.push(orderPath)
      expect(router.currentRoute.value.name).toBe('m-order-detail')
      await router.push('/mine')
      await router.push(orderPath)
      expect(assign).toHaveBeenCalledTimes(1)
      expect(send).not.toHaveBeenCalled()
      expect(router.currentRoute.value.name).toBe('m-order-detail')
    } finally {
      userAgent.mockRestore()
    }
  })

  it('已有桌面账号时进入通知订单，仍可主动登录打手并保留订单', async () => {
    me.mockResolvedValue({ ...booster, roles: [], menu_permissions: permissions })
    const router = makeMRouter()
    await router.push(orderPath)
    expect(replace).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: '/m/orders/10000000000000000002' })
  })

  it.each([
    ['https://other.example.invalid', androidUserAgent, orderPath],
    ['https://suqing.suqing.chat', IPHONE_UA, orderPath],
    ['https://suqing.suqing.chat', androidUserAgent, '/orders/10000000000000000002'],
  ])('其它访问仍打开 H5 订单详情（%s, %s, %s）', async (origin, ua, path) => {
    mockLocation.origin = origin
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua)
    try {
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      const router = makeMRouter()
      await router.push(path)
      expect(assign).not.toHaveBeenCalled()
      expect(router.currentRoute.value.name).toBe('m-order-detail')
    } finally {
      userAgent.mockRestore()
    }
  })

  it('旧扩展功能入口保留网页，无需检查登录状态即可显示入口', async () => {
    const router = makeMRouter()
    await router.push('/wxpusher/open?entry=wxpusher')
    expect(router.currentRoute.value.name).toBe('m-wxpusher-open')
    expect(me).not.toHaveBeenCalled()
  })

  it.each(['/wxpusher/open?entry=wxpusher', '/?entry=wxpusher', '/login?entry=wxpusher'])(
    '当前标签的订单登录回跳只打开准备好的订单一次（%s）',
    async (callback) => {
      me.mockResolvedValue({ ...booster, menu_permissions: permissions })
      storePendingOrder()
      const router = makeMRouter()
      await router.push(callback)
      expect(router.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
      expect(assign).not.toHaveBeenCalled()
      setActivePinia(createPinia())
      const extension = makeMRouter()
      await extension.push('/wxpusher/open?entry=wxpusher')
      expect(extension.currentRoute.value.name).toBe('m-wxpusher-open')
    },
  )

  it('订单回跳没有打手会话时，在同一张订单的密码页说明下一步', async () => {
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    storePendingOrder()
    const router = makeMRouter()
    await router.push('/wxpusher/open?entry=wxpusher')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query).toEqual({
      entry: 'wxpusher', redirect: '/m/orders/10000000000000000002', notice: 'wxpusher-order-password',
    })
    expect(assign).not.toHaveBeenCalled()
  })

  it('拥有待回订单时先完成既有账号确认，再打开该订单', async () => {
    storePendingOrder()
    const router = makeMRouter()
    await router.push('/wxpusher/open?entry=wxpusher&continue=confirm')
    expect(replace).toHaveBeenCalledExactlyOnceWith('/api/auth/wxpusher/login')
    expect(me).not.toHaveBeenCalled()
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const confirmed = makeMRouter()
    await confirmed.push('/?entry=wxpusher')
    expect(confirmed.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
  })

  it('过期订单不会接管扩展功能入口', async () => {
    storePendingOrder(Date.now() - 60_000)
    const router = makeMRouter()
    await router.push('/wxpusher/open?entry=wxpusher')
    expect(router.currentRoute.value.name).toBe('m-wxpusher-open')
    expect(window.sessionStorage.getItem(pendingOrderKey)).toBeNull()
  })

  it('另一个标签的普通扩展入口不会消费本标签的待回订单', async () => {
    storePendingOrder()
    const otherTab = new Map<string, string>()
    await withSessionStorage({
      getItem: (key) => otherTab.get(key) ?? null,
      setItem: (key, value) => otherTab.set(key, value),
      removeItem: (key) => otherTab.delete(key),
    }, async () => {
      const extension = makeMRouter()
      await extension.push('/wxpusher/open?entry=wxpusher')
      expect(extension.currentRoute.value.name).toBe('m-wxpusher-open')
    })
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const callback = makeMRouter()
    await callback.push('/wxpusher/open?entry=wxpusher')
    expect(callback.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
  })

  it('本标签明示返回大厅时，待回订单不覆盖它', async () => {
    storePendingOrder()
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    const router = makeMRouter()
    const redirect = '/m/hall?claimSource=wxpusher'
    await router.push({ name: 'm-login', query: { entry: 'wxpusher', redirect } })
    expect(router.currentRoute.value.query.redirect).toBe(redirect)
    expect(window.sessionStorage.getItem(pendingOrderKey)).not.toBeNull()
  })

  it('已到达普通订单后，之前放弃的登录回跳不会接管扩展功能', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    storePendingOrder()
    const router = makeMRouter()
    await router.push('/orders/10000000000000000003')
    const extension = makeMRouter()
    await extension.push('/wxpusher/open?entry=wxpusher')
    expect(extension.currentRoute.value.name).toBe('m-wxpusher-open')
    expect(window.sessionStorage.getItem(pendingOrderKey)).toBeNull()
  })

  it('重新打开后恢复最近的打手页面，忽略查询参数和密码页', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/orders/10000000000000000002?source=demo#tab')
    await router.push('/me/password')
    setActivePinia(createPinia())
    const reopened = makeMRouter()
    await reopened.push('/?entry=wxpusher')
    expect(reopened.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
    expect(replace).not.toHaveBeenCalled()
  })

  it.each(['/performance', '/mine#performance'])('从 WxPusher 复开时恢复绩效明细位置（%s）', async (saved) => {
    me.mockResolvedValue({ ...booster, menu_permissions: [...permissions, 'my_performance', 'my_performance.export'] })
    if (saved === '/performance') {
      window.localStorage.setItem('ttt:worker-page:tenant_a:7', saved)
    } else {
      await makeMRouter().push(saved)
    }
    setActivePinia(createPinia())
    const reopened = makeMRouter()
    await reopened.push('/?entry=wxpusher')
    expect(reopened.currentRoute.value.fullPath).toBe('/mine#performance')
  })

  it('从 WxPusher 复开时恢复打手上次查看的订单通知设置', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/me/notifications/order')
    setActivePinia(createPinia())
    const reopened = makeMRouter()
    await reopened.push('/?entry=wxpusher')
    expect(reopened.currentRoute.value.fullPath).toBe('/me/notifications/order')
  })

  it('当前用户已有打手会话时，WxPusher 登录回退仍恢复最近页面', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/mine')
    await router.push('/login?entry=wxpusher')
    expect(router.currentRoute.value.fullPath).toBe('/mine')
  })

  it('已有打手会话时，订单登录回退优先打开这次的订单', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/mine')
    await router.push({ name: 'm-login', query: { entry: 'wxpusher', redirect: '/m/orders/10000000000000000002' } })
    expect(router.currentRoute.value.fullPath).toBe('/orders/10000000000000000002')
    expect(assign).not.toHaveBeenCalled()
  })

  it('抢单推送的登录回退遇到有效会话时直达指定订单', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/mine')
    const target = '/hall?claimSource=peer&claimRef=42'
    await router.push({ name: 'm-login', query: { entry: 'wxpusher', redirect: `/m${target}` } })

    expect(router.currentRoute.value.fullPath).toBe(target)
  })

  it('退出后访客不能复用上一位打手页面，普通首页也不恢复', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/mine')
    setActivePinia(createPinia())
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    const guest = makeMRouter()
    await guest.push('/?entry=wxpusher')
    expect(guest.currentRoute.value.name).toBe('m-login')
    expect(guest.currentRoute.value.query.redirect).toBe('/m/?entry=wxpusher')
    setActivePinia(createPinia())
    const ordinary = makeMRouter()
    await ordinary.push('/')
    expect(ordinary.currentRoute.value.fullPath).toBe('/hall')
  })

  it.each([
    { identity_plane: 'tenant_b' },
    { id: 8 },
    { menu_permissions: ['grab_hall.take_order'] },
  ])('不同账号或失去权限时回到自己的大厅：%j', async (other) => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const router = makeMRouter()
    await router.push('/orders/10000000000000000002')
    setActivePinia(createPinia())
    me.mockResolvedValue({ ...booster, menu_permissions: permissions, ...other })
    const reopened = makeMRouter()
    await reopened.push('/?entry=wxpusher')
    expect(reopened.currentRoute.value.fullPath).toBe('/hall')
  })

  it('本机存储禁用时可以照常进入打手大厅', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: permissions })
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage')!
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get: () => { throw new DOMException('Storage unavailable', 'SecurityError') },
    })
    try {
      const router = makeMRouter()
      await router.push('/?entry=wxpusher')
      expect(router.currentRoute.value.fullPath).toBe('/hall')
    } finally {
      Object.defineProperty(window, 'localStorage', original)
    }
  })

  it('已有桌面会话仍能停留在明确的打手登录入口，保持原会话', async () => {
    const admin = { ...booster, roles: ['系统管理员'], is_tenant_admin: true }
    me.mockResolvedValue(admin)
    const router = makeMRouter()
    await router.push('/login?entry=wxpusher')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(useSessionStore().user).toEqual(admin)
    expect(replace).not.toHaveBeenCalled()
    expect(readMobileRedirectSuppressed()).toBe(false)
  })
})

describe('m 端守卫', () => {
  it('未登录访问受保护路由 → m-login，redirect 是站内 /m/ 路径（含前缀）', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    const router = makeMRouter()
    await router.push('/hall?game=3')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query.redirect).toBe('/m/hall?game=3')
  })

  it('未登录访问未声明 requiresAuth 的 m-home → 仍被拦（fail-closed 默认）', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    const router = makeMRouter()
    await router.push('/')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query.redirect).toBe('/m/')
  })

  it('已登录访问 /login → m-home 再落到第一个有权限的 tab（这里是大厅）', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['grab_hall.take_order', 'my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('m-hall')
  })

  it('订单和绩效都缺操作权限时，访问我的订单会回到频道', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['my_orders', 'my_performance'] })
    const router = makeMRouter()
    await router.push('/mine')
    expect(router.currentRoute.value.name).toBe('m-channel')
    expect(toast).toHaveBeenCalledWith('无权限访问该页面')
  })

  it('已登录但没有业务栏目权限时，默认打开频道', async () => {
    me.mockResolvedValue(booster)
    const router = makeMRouter()
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('m-channel')
  })

  it('无权限直输 /hall → toast 提示后回 m-home，再落到第一个有权限的 tab（只有我的订单）', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/hall')
    expect(toast).toHaveBeenCalledWith('无权限访问该页面')
    expect(router.currentRoute.value.name).toBe('m-mine')
  })

  it('有权限访问 /hall → 放行', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['grab_hall.take_order'] })
    const router = makeMRouter()
    await router.push('/hall')
    expect(router.currentRoute.value.name).toBe('m-hall')
  })

  it('M2：无截图节点直输截图页 → toast 后回第一个有权限的 tab（守卫回 home 语义）', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/orders/10000000000000000002/screenshots')
    expect(toast).toHaveBeenCalledWith('无权限访问该页面')
    expect(router.currentRoute.value.name).toBe('m-mine')
  })

  it('M2：持 view_screenshot 任一颗即可进截图页（canNode 任一即进）', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['order.view_screenshot'] })
    const router = makeMRouter()
    await router.push('/orders/10000000000000000002/screenshots')
    expect(router.currentRoute.value.name).toBe('m-order-screenshots')
  })

  it('M3：无 my_profile 直输 /me/profile → toast 后回第一个有权限的 tab', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/me/profile')
    expect(toast).toHaveBeenCalledWith('无权限访问该页面')
    expect(router.currentRoute.value.name).toBe('m-mine')
  })

  it('M3：持 my_profile 进 /me；反馈页要的是 home.feedback（不是 my_profile）', async () => {
    me.mockResolvedValue({ ...booster, menu_permissions: ['my_profile'] })
    const router = makeMRouter()
    await router.push('/me')
    expect(router.currentRoute.value.name).toBe('m-me')
    await router.push('/me/feedback')
    expect(toast).toHaveBeenCalledWith('无权限访问该页面')
    expect(router.currentRoute.value.name).toBe('m-me')
    await router.push('/me/password')
    expect(router.currentRoute.value.name).toBe('m-me-password')
  })

  it('管理员短路：menu_permissions 为空也放行 /hall（与桌面 canNode 同一条判据）', async () => {
    me.mockResolvedValue({ ...booster, is_tenant_admin: true, menu_permissions: [] })
    const router = makeMRouter()
    await router.push('/hall')
    expect(router.currentRoute.value.name).toBe('m-hall')
  })

  it('引导失败（非 401）用 Toast.error 提示一次，且按未登录处理', async () => {
    me.mockRejectedValue(new Error('网络异常，请检查连接'))
    const router = makeMRouter()
    await router.push('/hall')
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(toast.error).toHaveBeenCalledWith('网络异常，请检查连接')
    expect(router.currentRoute.value.name).toBe('m-login')
    // 只提示一次：下一次导航不再弹（loadError 已被消费，且 guest 态短路 load）。
    await router.push('/hall')
    expect(toast.error).toHaveBeenCalledTimes(1)
  })
})

describe('m 端身份门（线 A：手机端只给打手，2026-10-12）', () => {
  it('未登录（会话非 ready）⇒ 照旧走 m-login，⛔ 不因「不是打手」提前弹回桌面（D-A4 的顺序钉子）', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    const router = makeMRouter()
    await router.push('/hall')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query.redirect).toBe('/m/hall')
    expect(replace).not.toHaveBeenCalled()
    expect(readMobileRedirectSuppressed()).toBe(false)
  })

  it('非打手（客服）会话就绪 ⇒ 真的调 location.replace("/") 且写下抑制标记', async () => {
    me.mockResolvedValue({ ...booster, roles: ['客服'], menu_permissions: ['grab_hall.take_order'] })
    const router = makeMRouter()
    await router.push('/hall')
    // 真实出口：整页回桌面根（不是 m 端路由表里的目标）。
    expect(replace).toHaveBeenCalledWith('/')
    expect(replace).toHaveBeenCalledTimes(1)
    // 承重：标记必须先落，否则桌面入口会再跳回 /m/（死循环）。
    expect(readMobileRedirectSuppressed()).toBe(true)
    // 导航被中止 ⇒ 不会先闪出 m 端页面。
    expect(router.currentRoute.value.name).toBeUndefined()
  })

  it('非打手停在 /login（requiresAuth:false 的页）也弹回桌面根（身份门在权限面之前）', async () => {
    // 系统管理员：持全部节点、`canNode` 全程短路 ⇒ 唯一能把他请出去的就是身份门（spec §1.5 的代价）。
    me.mockResolvedValue({ ...booster, roles: ['系统管理员'], is_tenant_admin: true, menu_permissions: [] })
    const router = makeMRouter()
    await router.push('/login')
    expect(replace).toHaveBeenCalledWith('/')
    expect(readMobileRedirectSuppressed()).toBe(true)
  })

  it('打手（持「代练员」职位）⇒ 不弹回桌面，且清除残留的抑制标记', async () => {
    writeMobileRedirectSuppressed() // 残留：上一次是别人（非打手）在这个标签页用过
    me.mockResolvedValue({ ...booster, menu_permissions: ['grab_hall.take_order'] })
    const router = makeMRouter()
    await router.push('/hall')
    expect(replace).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('m-hall')
    expect(readMobileRedirectSuppressed()).toBe(false)
  })

  it('⭐ 死循环反向钉子：被弹回之后，桌面入口再判定 ⇒ 不再跳 /m/', async () => {
    // 第一跳：非打手进 /m/ ⇒ 守卫写标记 + 回桌面根。
    me.mockResolvedValue({ ...booster, roles: ['客服主管'], menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/mine')
    expect(replace).toHaveBeenCalledWith('/')
    expect(readMobileRedirectSuppressed()).toBe(true)

    replace.mockClear()
    // 第二跳：桌面根被加载 = `main.ts` 的那一行（手机 UA + 桌面路径 + 真 sessionStorage 读）。
    redirectToMobileIfMobile(IPHONE_UA, '/')
    expect(replace).not.toHaveBeenCalled()
  })

  /**
   * 存储**写不进去**时不许弹：旧写法「无条件写 + 无条件跳」在存储不可用（隐私模式 / 配额）时是真死循环——标记永远不在 ⇒ 桌面入口每次整页加载都跳 `/m/` ⇒ 守卫又 `replace('/')` ⇒ 来回跳。
   * 修法：写完自证，读不到就不弹，退回旧行为（非打手留在 `/m/` 按节点门走）。
   */
  it('存储写不进去（抛异常 / 静默丢弃）⇒ 不调 location.replace，退回 /m/ 旧行为（P1-2）', async () => {
    const throwing: MobileRedirectStorage = {
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceededError')
      },
      removeItem: () => {},
    }
    // 另加「静默丢弃」形态：不抛、但也没写进去（某些 WebView 的隐私模式就是这样）。
    const discarding: MobileRedirectStorage = {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    }

    for (const stub of [throwing, discarding]) {
      setActivePinia(createPinia())
      replace.mockClear()
      me.mockResolvedValue({ ...booster, roles: ['客服'], menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
      const router = makeMRouter()
      await withSessionStorage(stub, async () => {
        await router.push('/mine')
      })
      // 真实出口：一个跳转都不发（发了就是死循环的那一半）。
      expect(replace).not.toHaveBeenCalled()
      // 且真的退回了旧行为：照常按节点门落到有权限的 tab，而不是被中止在空白处。
      expect(router.currentRoute.value.name).toBe('m-mine')
    }
  })

  it('⭐ 会话终结清了标记 ⇒ 桌面入口会**再次**跳 /m/（P1-1 的真实出口）', async () => {
    me.mockResolvedValue({ ...booster, roles: ['客服'], menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    const router = makeMRouter()
    await router.push('/mine')
    expect(replace).toHaveBeenCalledWith('/')
    expect(readMobileRedirectSuppressed()).toBe(true)

    // 唯一会话终结点（`logout()` 调它，桌面 / 手机端 401 处理器也调它）。
    useSessionStore().clear()

    expect(readMobileRedirectSuppressed()).toBe(false)
    replace.mockClear()
    redirectToMobileIfMobile(IPHONE_UA, '/')
    expect(replace).toHaveBeenCalledWith('/m/')
  })
})


describe('WxPusher 订单群聊入口', () => {
  const path = '/wxpusher/chat/32?message=81&entry=wxpusher'
  const key = 'ttt-wxpusher-order-continuation'

  it('没有会话时保留登录后的群与消息', async () => {
    me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
    const router = makeMRouter()
    await router.push(path)
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query).toEqual({ entry: 'wxpusher', redirect: `/m${path}` })
    expect(send).not.toHaveBeenCalled()
  })

  it('员工使用普通网页会话打开通知群，安卓也保持网页', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 Android/WxPusher-Android')
    try {
      me.mockResolvedValue({ ...booster, roles: [] })
      const router = makeMRouter()
      await router.push(`${path}&open=web`)
      expect(router.currentRoute.value.name).toBe('m-wxpusher-chat')
      expect(assign).not.toHaveBeenCalled()
      expect(replace).not.toHaveBeenCalled()
    } finally {
      ua.mockRestore()
    }
  })

  it('安卓未切到 App 时，发起系统对接并保留会话和消息', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 Android/WxPusher-Android')
    const followUrl = 'https://wxpusher.zjiecode.com/app/#/follow?type=1&id=128&jump=auto'
    try {
      assign.mockImplementation(() => { throw new Error('未安装') })
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      send.mockResolvedValue({ url: followUrl })
      const router = makeMRouter()
      await router.push(path)
      expect(assign).toHaveBeenCalledExactlyOnceWith('daidaitong://suqing.suqing.chat/m/wxpusher/chat/32?message=81')
      expect(send).toHaveBeenCalledExactlyOnceWith({ method: 'GET', url: '/api/auth/wxpusher/chat/continue', skipUnauthorizedHandler: true })
      expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)
      expect(JSON.parse(window.sessionStorage.getItem(key) ?? 'null')?.chatPath).toBe(`/m${path}`)
    } finally {
      ua.mockRestore()
    }
  })

  it('iOS 内置页直接发起系统对接，不唤起代代通', async () => {
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(IPHONE_UA)
    const followUrl = 'https://wxpusher.zjiecode.com/app/#/follow?type=1&id=128&jump=auto'
    vi.stubGlobal('webkit', { messageHandlers: { wxpusher: { postMessage: vi.fn() } } })
    try {
      me.mockRejectedValueOnce(new AuthExpiredError('未认证'))
      send.mockResolvedValue({ url: followUrl })
      const router = makeMRouter()
      await router.push(path)
      expect(assign).not.toHaveBeenCalled()
      expect(send).toHaveBeenCalledExactlyOnceWith({ method: 'GET', url: '/api/auth/wxpusher/chat/continue', skipUnauthorizedHandler: true })
      expect(replace).toHaveBeenCalledExactlyOnceWith(followUrl)
      expect(JSON.parse(window.sessionStorage.getItem(key) ?? 'null')?.chatPath).toBe(`/m${path}`)
    } finally {
      vi.unstubAllGlobals()
      ua.mockRestore()
    }
  })

  it('系统对接回到同一条消息，不重复唤起代代通', async () => {
    mockLocation.origin = 'https://suqing.suqing.chat'
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 Android/WxPusher-Android')
    try {
      me.mockResolvedValue(booster)
      window.sessionStorage.setItem(key, JSON.stringify({ chatPath: `/m${path}`, expiresAt: Date.now() + 60_000 }))
      const router = makeMRouter()
      await router.push('/?entry=wxpusher')
      expect(router.currentRoute.value.fullPath).toBe(`${path}&open=web`)
      expect(window.sessionStorage.getItem(key)).toBeNull()
      expect(assign).not.toHaveBeenCalled()
    } finally {
      ua.mockRestore()
    }
  })
})
