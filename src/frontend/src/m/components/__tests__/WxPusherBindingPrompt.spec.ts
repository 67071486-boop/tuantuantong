// @vitest-environment jsdom
import '../../framework7'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { useSessionStore, WORKER_ROLE } from '@/stores/session'
import App from '../../App.vue'
import { installMGuards } from '../../router/guards'

const nativeChat = vi.hoisted(() => ({ preview: false, hosted: false }))
const requestGuide = vi.mocked(request)
const guidePath = '/api/auth/wxpusher/guide'
const bindPage = '/me/notifications'
const page = defineComponent({ render: () => h('button', '页面内容') })
const openDialog = '[role="dialog"].modal-in'
const iosUserAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'
const androidUserAgent = 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('../appChatWebView', () => ({
  appChatGrant: undefined,
  get appChatHosted() { return nativeChat.hosted },
  appChatExpired: { value: false },
  appChatCloseEnabled: false,
  appChatRetainEnabled: false,
  get appChatPreview() { return nativeChat.preview },
}))

function worker(id = 7) {
  return {
    id, username: 'demo-worker', display_name: '测试打手', identity_plane: 'tenant_a',
    roles: [WORKER_ROLE], permissions: [], menu_permissions: ['my_profile', 'grab_hall.take_order'], is_tenant_admin: false,
  }
}

let dispose: (() => void) | undefined

beforeEach(() => {
  nativeChat.preview = false
  nativeChat.hosted = false
  window.sessionStorage.clear()
  window.localStorage.clear()
  vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(iosUserAgent)
  requestGuide.mockReset()
  vi.stubGlobal('matchMedia', (media: string) => ({
    media, matches: false, addEventListener() {}, removeEventListener() {},
  }))
})

afterEach(() => {
  dispose?.()
  dispose = undefined
  vi.restoreAllMocks()
  Reflect.deleteProperty(window, 'webkit')
  vi.unstubAllGlobals()
})

async function start(initial = '/login', restored = false, guards = false) {
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  session.status = restored ? 'ready' : 'guest'
  session.user = restored ? worker() : null
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'm-home', component: page },
      { path: '/login', name: 'm-login', component: page, meta: { requiresAuth: false } },
      { path: '/hall', name: 'm-hall', component: page },
      { path: '/orders/:orderNo', name: 'm-order-detail', component: page },
      { path: '/orders/:orderNo/screenshots', name: 'm-order-screenshots', component: page },
      { path: '/orders/:orderNo/screenshots/browser', name: 'm-order-screenshot-browser', component: page, meta: { requiresAuth: false } },
      { path: '/wxpusher/chat/1', name: 'm-wxpusher-chat', component: page },
      { path: bindPage, name: 'm-me-notifications', component: page },
    ],
  })
  if (guards) installMGuards(router)
  await router.push(initial)
  const wrapper = mount(App, { attachTo: document.body, global: { plugins: [pinia, router] } })
  dispose = () => wrapper.unmount()
  await flushPromises()
  return { router, session, wrapper }
}

describe('打手 H5 的 WxPusher 登录提醒', () => {
  it('确认未绑定才打开建议弹窗，保留登录后的订单地址供返回', async () => {
    requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const { router, session, wrapper } = await start()
    expect(requestGuide).not.toHaveBeenCalled()

    session.user = worker()
    session.status = 'ready'
    await router.replace('/orders/demo-order')
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ url: guidePath }))
    expect(router.currentRoute.value.name).toBe('m-order-detail')
    const dialog = wrapper.get(openDialog)
    expect(dialog.text()).toContain('强烈建议绑定 WxPusher，开启实时和离线通知，及时接收聊天消息和新订单推送。')
    expect(dialog.findAll('button').map(button => button.text())).toEqual(['去绑定', '稍后再说'])

    await dialog.get('button').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(bindPage)
    expect(wrapper.find(openDialog).exists()).toBe(false)

    router.back()
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('m-order-detail')
    expect(requestGuide).toHaveBeenCalledTimes(1)
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })


  it.each(['/hall', '/hall?entry=wxpusher'])('WxPusher 内置 iOS 页面不查绑定也不提示（%s）', async (path) => {
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { wxpusher: { postMessage: vi.fn() } } },
    })
    requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const { wrapper } = await start(path, true)

    expect(requestGuide).not.toHaveBeenCalled()
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('Safari 从带 WxPusher 来源标记的普通页面进入时仍可看到绑定提醒', async () => {
    requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const { wrapper } = await start('/hall?entry=wxpusher', true)

    expect(wrapper.find(openDialog).text()).toContain('建议绑定 WxPusher')
  })

  it('通知提醒打开后键盘可以进入操作项，关闭后回到原来焦点', async () => {
    let finish!: (value: unknown) => void
    requestGuide.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { wrapper } = await start('/hall', true)
    const previousFocus = wrapper.get('button').element
    previousFocus.focus()
    finish({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    await flushPromises()

    const dialog = wrapper.get(openDialog)
    const [bind, later] = dialog.findAll('button')
    expect(document.activeElement).toBe(dialog.element)
    expect(previousFocus.hasAttribute('inert')).toBe(true)
    expect(dialog.element.closest('[inert]')).toBeNull()
    expect(wrapper.get('.popup-backdrop.backdrop-in').element.closest('[inert]')).toBeNull()
    await dialog.trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(bind!.element)
    await bind!.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(later!.element)
    await later!.trigger('keydown', { key: 'Escape' })

    expect(wrapper.find(openDialog).exists()).toBe(false)
    expect(wrapper.find('.popup-backdrop.backdrop-in').exists()).toBe(false)
    expect(previousFocus.hasAttribute('inert')).toBe(false)
    expect(document.activeElement).toBe(previousFocus)
  })

  it('没有「我的资料」权限时不查绑定也不提示', async () => {
    const { session, router, wrapper } = await start()
    session.user = { ...worker(), menu_permissions: [] }
    session.status = 'ready'
    await router.replace('/hall')
    await flushPromises()

    expect(requestGuide).not.toHaveBeenCalled()
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('已有内聊弹窗时等用户关闭，再显示绑定提醒', async () => {
    requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const chat = document.createElement('dialog')
    chat.className = 'm-chat-screen'
    chat.open = true
    document.body.append(chat)
    try {
      const { wrapper } = await start('/hall', true)
      expect(wrapper.find(openDialog).exists()).toBe(false)
      expect(chat.hasAttribute('inert')).toBe(false)

      chat.open = false
      chat.dispatchEvent(new Event('close'))
      await flushPromises()
      expect(wrapper.find(openDialog).exists()).toBe(true)
      expect(chat.hasAttribute('inert')).toBe(false)
    } finally {
      chat.remove()
    }
  })

  it('从聊天打开订单和截图时延后绑定提醒，返回聊天并关闭后再显示', async () => {
    let finishGuide!: (value: unknown) => void
    const guide = new Promise(resolve => { finishGuide = resolve })
    requestGuide.mockImplementation(({ url }) => url === guidePath ? guide : Promise.resolve({ unread_count: 0 }))
    const { router, session, wrapper } = await start('/hall', true)
    session.user = { ...worker(), internal_chat_enabled: true }
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()

    const chat = document.querySelector<HTMLDialogElement>('dialog.m-chat-screen')!
    Object.assign(chat, {
      showModal: () => { chat.open = true },
      close: () => {
        chat.open = false
        queueMicrotask(() => chat.dispatchEvent(new Event('close')))
      },
    })
    document.querySelector<HTMLButtonElement>('.m-chat-bubble')!.click()
    await flushPromises()
    expect(chat.open).toBe(true)
    const frame = chat.querySelector('iframe')!
    function chatMessage(data: object): void {
      window.dispatchEvent(new MessageEvent('message', {
        source: frame.contentWindow, origin: window.location.origin, data,
      }))
    }

    finishGuide({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    await flushPromises()
    expect(wrapper.find(openDialog).exists()).toBe(false)

    const orderNo = '10000000000000000012'
    const orderPage = `/orders/${orderNo}`
    let resumeNavigation!: () => void
    const pendingNavigation = new Promise<void>(resolve => { resumeNavigation = resolve })
    const stopGuard = router.beforeEach(to => to.name === 'm-order-detail' ? pendingNavigation : undefined)
    chatMessage({ type: 'tt:chat.openOrder', detail: orderNo })
    await flushPromises()
    expect(chat.open).toBe(false)
    expect(router.currentRoute.value.path).toBe('/hall')
    expect(wrapper.find(openDialog).exists()).toBe(false)

    resumeNavigation()
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(orderPage))
    stopGuard()
    expect(wrapper.get('button').element.closest('[inert]')).toBeNull()
    await router.push(`${orderPage}/screenshots`)
    await flushPromises()
    expect(wrapper.get('button').element.closest('[inert]')).toBeNull()
    expect(wrapper.find(openDialog).exists()).toBe(false)

    router.back()
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(orderPage))
    expect(wrapper.find(openDialog).exists()).toBe(false)
    router.back()
    await vi.waitFor(() => expect(chat.open).toBe(true))
    expect(wrapper.find(openDialog).exists()).toBe(false)

    chatMessage({ type: 'tt:chat.closeFromList' })
    await flushPromises()
    expect(chat.open).toBe(false)
    expect(wrapper.get(openDialog).text()).toContain('建议绑定 WxPusher')
    expect(requestGuide.mock.calls.filter(([{ url }]) => url === guidePath)).toHaveLength(1)
  })

  it('桌面浏览器打开 H5 不查绑定也不提示', async () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15')
    const { wrapper } = await start('/hall', true)

    expect(requestGuide).not.toHaveBeenCalled()
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it.each([
    { binding_enabled: true, binding_prompt_enabled: true, bound: true },
    { binding_enabled: false, binding_prompt_enabled: true, bound: false },
    { binding_enabled: true, binding_prompt_enabled: true },
    { binding_enabled: true, binding_prompt_enabled: false, bound: false },
    { binding_enabled: true, bound: false },
    { binding_enabled: true, binding_prompt_enabled: 'true', bound: false },
  ])('登录提醒未开启或不符合绑定条件时不提示（%j）', async (guide) => {
    requestGuide.mockResolvedValue(guide)
    const { router, wrapper } = await start('/hall', true)
    await router.push('/orders/demo-order')
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledTimes(1)
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('同一标签刷新不再提醒，退出后重新登录同一账号仍会检查', async () => {
    requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const first = await start('/hall', true)
    expect(first.wrapper.find(openDialog).exists()).toBe(true)
    dispose?.()

    const restored = await start('/hall', true)
    expect(requestGuide).toHaveBeenCalledTimes(1)
    expect(restored.wrapper.find(openDialog).exists()).toBe(false)

    restored.session.clear()
    restored.session.user = worker()
    restored.session.status = 'ready'
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledTimes(2)
    expect(restored.wrapper.find(openDialog).exists()).toBe(true)
  })

  it('浏览器不允许存储时，当前页面仍只会检查一次', async () => {
    const storage = vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('Storage disabled', 'SecurityError')
    })
    try {
      requestGuide.mockResolvedValue({ binding_enabled: true, binding_prompt_enabled: true, bound: true })
      const { router } = await start('/hall', true)
      await router.push('/orders/demo-order')
      await flushPromises()
      expect(requestGuide).toHaveBeenCalledTimes(1)
    } finally {
      storage.mockRestore()
    }
  })

  it('检查失败时保留原页面，本次登录不再检查', async () => {
    requestGuide.mockRejectedValue(new Error('网络异常'))
    const { router, session, wrapper } = await start('/hall', true)
    await router.push('/orders/demo-order')
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      url: guidePath, skipUnauthorizedHandler: true,
    }))
    expect(router.currentRoute.value.name).toBe('m-order-detail')
    expect(session.status).toBe('ready')
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('旧账号的请求迟到后不显示给新账号，同账号下一次登录会重查', async () => {
    let finishFirst!: (value: unknown) => void
    requestGuide.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }))
      .mockResolvedValueOnce({ binding_enabled: true, binding_prompt_enabled: true, bound: true })
      .mockResolvedValueOnce({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    const { session, wrapper } = await start('/hall', true)

    session.clear()
    session.user = worker(12)
    session.status = 'ready'
    await flushPromises()
    finishFirst({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledTimes(2)
    expect(wrapper.find(openDialog).exists()).toBe(false)

    session.clear()
    session.user = worker(12)
    session.status = 'ready'
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledTimes(3)
    expect(wrapper.find(openDialog).exists()).toBe(true)
  })

  it('先进入绑定页又离开时，旧检查不重新弹窗', async () => {
    let finish!: (value: unknown) => void
    requestGuide.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { router, wrapper } = await start('/hall', true)
    await router.push(bindPage)
    await flushPromises()
    await router.push('/hall')
    await flushPromises()
    finish({ binding_enabled: true, binding_prompt_enabled: true, bound: false })
    await flushPromises()

    expect(requestGuide).toHaveBeenCalledTimes(1)
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('原生内聊预览保持无提示，不检查打手的绑定', async () => {
    nativeChat.preview = true
    const { wrapper } = await start('/hall', true)

    expect(requestGuide).not.toHaveBeenCalled()
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })
})

describe('打手 H5 的 Android App 下载提醒', () => {
  beforeEach(() => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent)
  })

  it.each([['/login', false], ['/hall', true]] as const)('从普通入口 %s 打开下载提醒，主按钮直达站内下载页', async (path, restored) => {
    const { wrapper } = await start(path, restored)
    const dialog = wrapper.get(openDialog)
    const download = dialog.get('a')

    expect(dialog.get('h2').text()).toBe('下载代代通 App')
    expect(download.text()).toBe('下载 App')
    expect(download.attributes('href')).toBe('/download/')
    expect(requestGuide).not.toHaveBeenCalled()

    const observeNavigation = vi.fn()
    const preventJsdomNavigation = (event: MouseEvent) => {
      observeNavigation(event.defaultPrevented)
      event.preventDefault()
    }
    window.addEventListener('click', preventJsdomNavigation)
    try {
      await download.trigger('click')
    } finally {
      window.removeEventListener('click', preventJsdomNavigation)
    }
    expect(observeNavigation).toHaveBeenCalledExactlyOnceWith(false)
    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('「稍后再说」在同一标签登录、切页和刷新后都保持关闭，新标签可以提示', async () => {
    const { router, session, wrapper } = await start()
    const later = wrapper.get(openDialog).findAll('button').find(button => button.text() === '稍后再说')
    await later!.trigger('click')
    expect(wrapper.find(openDialog).exists()).toBe(false)

    session.user = worker()
    session.status = 'ready'
    await router.replace('/hall')
    await router.push('/orders/demo-order')
    expect(wrapper.find(openDialog).exists()).toBe(false)
    dispose?.()

    const reloaded = await start('/hall', true)
    expect(reloaded.wrapper.find(openDialog).exists()).toBe(false)
    dispose?.()

    window.sessionStorage.clear()
    const freshTab = await start('/hall', true)
    expect(freshTab.wrapper.find(openDialog).exists()).toBe(true)
  })

  it('「不再提醒」在同站新标签也保持关闭', async () => {
    const { wrapper } = await start('/hall', true)
    const mute = wrapper.get(openDialog).findAll('button').find(button => button.text() === '不再提醒')
    await mute!.trigger('click')
    expect(wrapper.find(openDialog).exists()).toBe(false)
    dispose?.()

    window.sessionStorage.clear()
    const freshTab = await start('/hall', true)
    expect(freshTab.wrapper.find(openDialog).exists()).toBe(false)
  })

  it('关闭提示后恢复悬浮沟通入口的可用状态，保留原本不可用的区域', async () => {
    const bubble = document.createElement('div')
    bubble.className = 'm-chat-bubble-area'
    const chat = document.createElement('dialog')
    chat.className = 'm-chat-screen'
    chat.setAttribute('inert', '')
    document.body.append(bubble, chat)
    try {
      const { router, wrapper } = await start('/hall', true)
      expect(bubble.hasAttribute('inert')).toBe(true)

      await router.push('/orders/demo-order/screenshots/browser')
      await flushPromises()
      expect(wrapper.find(openDialog).exists()).toBe(false)
      expect(bubble.hasAttribute('inert')).toBe(false)
      expect(chat.hasAttribute('inert')).toBe(true)
    } finally {
      bubble.remove()
      chat.remove()
    }
  })

  it('下载提示显示时，返回内聊仍可重新打开并关闭原生聊天窗口', async () => {
    const chat = document.createElement('dialog')
    chat.className = 'm-chat-screen'
    document.body.append(chat)
    try {
      const { wrapper } = await start('/hall', true)
      expect(wrapper.find(openDialog).exists()).toBe(true)
      expect(chat.closest('[inert]')).toBeNull()

      chat.open = true
      await flushPromises()
      expect(chat.open).toBe(true)
      expect(chat.closest('[inert]')).toBeNull()

      chat.open = false
      chat.dispatchEvent(new Event('close'))
      await flushPromises()
      expect(wrapper.find(openDialog).exists()).toBe(true)
    } finally {
      chat.remove()
    }
  })

  it('浏览器禁用存储时，关闭下载提示后在当前页面内也保持关闭', async () => {
    for (const name of ['localStorage', 'sessionStorage'] as const) {
      vi.spyOn(window, name, 'get').mockImplementation(() => { throw new DOMException('Storage disabled', 'SecurityError') })
    }
    const { router, wrapper } = await start('/hall', true)
    await wrapper.get(openDialog).trigger('keydown', { key: 'Escape' })
    await router.push('/orders/demo-order/screenshots')
    await router.push('/hall')

    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it.each([
    ['/hall?entry=wxpusher', true],
    ['/orders/demo-order/screenshots', true],
    ['/orders/demo-order/screenshots/browser', false],
    ['/wxpusher/chat/1', true],
    [bindPage, true],
    ['/login?redirect=/m/wxpusher/chat/1', false],
    ['/login?redirect=/m/orders/demo-order/screenshots', false],
  ] as const)('特殊入口 %s 不弹出下载提醒，随后进入普通页面仍可提示', async (path, restored) => {
    const { router, wrapper } = await start(path, restored)
    expect(wrapper.find(openDialog).exists()).toBe(false)

    await router.push(restored ? '/hall' : '/login')
    await flushPromises()
    expect(wrapper.find(openDialog).exists()).toBe(true)
  })

  it('WxPusher 首页经实际守卫跳到不带标记的大厅时不弹窗，主动打开其他页面后可以提示', async () => {
    const { router, wrapper } = await start('/?entry=wxpusher', true, true)
    expect(router.currentRoute.value.fullPath).toBe('/hall')
    expect(wrapper.find(openDialog).exists()).toBe(false)

    await router.push('/orders/demo-order')
    await flushPromises()
    expect(wrapper.find(openDialog).exists()).toBe(true)
  })

  it('通知订单的密码登录结束后自动打开订单时不弹窗，主动打开大厅后可以提示', async () => {
    const { router, session, wrapper } = await start('/login?entry=wxpusher&redirect=/m/orders/demo-order')
    expect(wrapper.find(openDialog).exists()).toBe(false)
    session.user = worker()
    session.status = 'ready'
    await router.replace('/orders/demo-order')
    await flushPromises()
    expect(wrapper.find(openDialog).exists()).toBe(false)

    await router.push('/hall')
    await flushPromises()
    expect(wrapper.find(openDialog).exists()).toBe(true)
  })

  it.each(['preview', 'hosted'] as const)('代代通内聊 WebView（%s）不弹出下载提醒', async (flow) => {
    nativeChat[flow] = true
    const { wrapper } = await start('/hall', true)

    expect(wrapper.find(openDialog).exists()).toBe(false)
  })

  it('WxPusher Android 的内置页面不弹出下载提醒', async () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(androidUserAgent + '/WxPusher-Android')
    const { wrapper } = await start('/hall', true)

    expect(wrapper.find(openDialog).exists()).toBe(false)
  })
})
