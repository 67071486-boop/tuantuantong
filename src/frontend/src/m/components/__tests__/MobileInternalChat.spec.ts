// @vitest-environment jsdom
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { useSessionStore, WORKER_ROLE } from '@/stores/session'
import OrderDetailView from '../../pages/OrderDetailView.vue'
import * as wxpusherLinks from '../../utils/wxpusherOrderLink'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
const appHost = vi.hoisted(() => ({ retained: false }))
vi.mock('../appChatWebView', async (original) => ({
  ...await original<typeof import('../appChatWebView')>(),
  get appChatRetainEnabled() { return appHost.retained },
  get appChatCloseEnabled() { return appHost.retained },
  notifyAppChat: vi.fn(),
  notifyAppChatUnread: vi.fn(),
}))
vi.mock('framework7-vue', () => ({
  f7Sheet: defineComponent({ props: ['opened'], setup: (props, { slots }) => () => props.opened ? h('div', slots.default?.()) : null }),
  f7Radio: defineComponent({ setup: (_, { slots }) => () => h('label', slots.default?.()) }),
  f7Button: defineComponent({ setup: (_, { slots }) => () => h('button', slots.default?.()) }),
  f7BlockTitle: defineComponent({ setup: (_, { slots }) => () => h('h2', slots.default?.()) }),
  f7Badge: defineComponent({ setup: (_, { slots }) => () => h('span', slots.default?.()) }),
  f7App: defineComponent({ setup: (_, { slots }) => () => h('div', { id: 'framework7-root' }, slots.default?.()) }),
  f7Navbar: defineComponent({ setup: (_, { slots }) => () => h('header', [slots.left?.(), slots.title?.(), slots.right?.()]) }),
  f7Link: defineComponent({ setup: (_, { slots }) => () => h('button', slots.default?.()) }),
  f7Preloader: defineComponent({ setup: () => () => h('span') }),
  f7Block: defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) }),
  f7Chip: defineComponent({ setup: (_, { slots }) => () => h('span', slots.default?.()) }),
  f7List: defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) }),
  f7ListItem: defineComponent({ setup: (_, { slots }) => () => h('div', slots.default?.()) }),
  f7Card: defineComponent({ setup: (_, { slots }) => () => h('section', slots.default?.()) }),
}))

const webKLiveOrigin = 'http://127.0.0.1:4181'
const conversationPath = '/api/internal-chat/conversations/'
const orderNo = '10000000000000000012'
const detailPath = `/orders/${orderNo}`
const mineLocation = '/mine?status=pending#order-list'

beforeEach(() => {
  vi.stubEnv('VITE_INTERNAL_CHAT_PREVIEW', '1')
  vi.stubEnv('VITE_INTERNAL_CHAT_LIVE', '1')
  vi.stubEnv('VITE_INTERNAL_CHAT_WEBK_ORIGIN', webKLiveOrigin)
  vi.mocked(request).mockReset()
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url === `/api/business/my-orders/${orderNo}/detail/`) return new Promise(() => {})
    if (url === '/api/internal-chat/unread/') return { unread_count: 7 }
    return { conversations: [{ unread_count: 2 }, { unread_count: 5 }] }
  })
})

afterEach(() => {
  appHost.retained = false
  vi.unstubAllEnvs()
  document.querySelector('script[src="/socket.io/socket.io.js"]')?.remove()
})

async function mountH5(signedIn = true, initial = mineLocation, history = createMemoryHistory(), production = false) {
  const { default: App } = await import('../../App.vue')
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  if (signedIn) {
    session.status = 'ready'
    session.user = {
      id: 7, username: 'test-worker', display_name: '测试打手', identity_plane: 'tenant_a', roles: [WORKER_ROLE],
      permissions: [], menu_permissions: [], is_tenant_admin: false, internal_chat_enabled: production,
    }
  }
  const order = defineComponent({
    setup: () => () => h('label', ['订单筛选', h('input', { value: '' })]),
  })
  const router = createRouter({
    history,
    routes: [
      { path: '/mine', name: 'm-mine', component: order },
      { path: '/hall', name: 'm-hall', component: order },
      { path: '/', name: 'm-home', component: order },
      {
        path: '/wxpusher/chat/:conversationId', name: 'm-wxpusher-chat',
        component: () => import('../../pages/WxPusherChatPage.vue'),
      },
      { path: '/orders/:orderNo', name: 'm-order-detail', component: OrderDetailView },
    ],
  })
  await router.push(initial)
  const wrapper = mount(App, { attachTo: document.body, global: { plugins: [pinia, router] } })
  await vi.dynamicImportSettled()
  await flushPromises()
  return { wrapper, router, session }
}

function frameEvent(frame: HTMLIFrameElement, data: object, origin = webKLiveOrigin) {
  window.dispatchEvent(new MessageEvent('message', {
    origin,
    source: frame.contentWindow,
    data,
  }))
}

function mockDialog() {
  const dialog = document.querySelector('dialog')!
  // jsdom exposes the dialog element without its native modal methods.
  Object.assign(dialog, {
    showModal: () => { dialog.open = true },
    close: () => { dialog.open = false },
  })
  return dialog
}

function chatBubble() {
  return document.querySelector<HTMLButtonElement>('button[aria-label="打开内部沟通，7 条未读消息"]')!
}

async function openChat() {
  const dialog = mockDialog()
  chatBubble().click()
  await flushPromises()
  return dialog
}

it('正式聊天启用时从 WxPusher 自动打开完整会话，返回列表后可关闭回打手首页', async () => {
  const showModal = function (this: HTMLDialogElement) { this.open = true }
  const close = function (this: HTMLDialogElement) { this.open = false }
  Object.assign(HTMLDialogElement.prototype, { showModal, close })
  const { wrapper, router } = await mountH5(true, '/wxpusher/chat/32?message=81&entry=wxpusher', createMemoryHistory(), true)
  try {
    const dialog = document.querySelector('dialog[open]')!
    expect(dialog).not.toBeNull()
    expect(wrapper.find('.push-chat').exists()).toBe(false)
    expect(document.querySelector('.m-chat-bubble')).toBeNull()
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({
      url: expect.stringContaining('notification-conversations'),
    }))
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.ready' }, location.origin)
    expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'tt:chat.openConversation' }), location.origin)
    frameEvent(frame, { type: 'tt:chat.navigationReady' }, location.origin)
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.openConversation', detail: 32 }, location.origin)
    frameEvent(frame, { type: 'tt:chat.closeFromList' }, location.origin)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('m-home')
    expect(dialog.hasAttribute('open')).toBe(false)
  } finally {
    wrapper.unmount()
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'close')
  }
})

it('从通知里的聊天切换到订单群，查看订单返回后保留订单群', async () => {
  const showModal = function (this: HTMLDialogElement) { this.open = true }
  const close = function (this: HTMLDialogElement) { this.open = false }
  Object.assign(HTMLDialogElement.prototype, { showModal, close })
  const entry = '/wxpusher/chat/32?message=81&entry=wxpusher'
  const { wrapper, router } = await mountH5(true, entry, createMemoryHistory(), true)
  try {
    const frame = document.querySelector<HTMLIFrameElement>('dialog iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    const event = (data: object) => frameEvent(frame, data, location.origin)
    event({ type: 'tt:chat.ready' })
    event({ type: 'tt:chat.navigationReady' })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.openConversation', detail: 32 }, location.origin)
    post.mockClear()

    event({ type: 'tt:chat.openOrder', detail: orderNo })
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(detailPath)
    router.back()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(entry)
    expect(document.querySelector('dialog[open] iframe')).toBe(frame)
    expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'tt:chat.openConversation' }), location.origin)

    await router.push('/wxpusher/chat/38?message=87&entry=wxpusher')
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.openConversation', detail: 38 }, location.origin)
  } finally {
    wrapper.unmount()
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'close')
  }
})

it('有原生入口时在完整会话里保留手动打开代代通', async () => {
  const showModal = function (this: HTMLDialogElement) { this.open = true }
  Object.assign(HTMLDialogElement.prototype, { showModal })
  const { wrapper, router } = await mountH5(true, mineLocation, createMemoryHistory(), true)
  const appUrl = 'daidaitong://suqing.suqing.chat/m/wxpusher/chat/32?message=81'
  const appLink = vi.spyOn(wxpusherLinks, 'wxpusherChatAppLink').mockReturnValue(appUrl)
  try {
    expect(document.querySelector('.m-chat-app-link')).toBeNull()
    await router.push('/wxpusher/chat/32?message=81&entry=wxpusher&open=web')
    await flushPromises()
    const link = document.querySelector('dialog[open] a.m-chat-app-link')
    expect(link?.getAttribute('href')).toBe(appUrl)
    expect(appLink).toHaveBeenCalledWith('/m/wxpusher/chat/32?message=81&entry=wxpusher', 'wxpusher')
  } finally {
    wrapper.unmount()
    appLink.mockRestore()
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
  }
})

it('普通 H5 聊天跟随软键盘的可视高度，发送和页面滚动时不掉回底部', async () => {
  const viewport = Object.assign(new EventTarget(), { height: 770, offsetTop: 0 })
  vi.stubGlobal('visualViewport', viewport)
  const { wrapper } = await mountH5()
  try {
    const dialog = await openChat()
    expect(dialog.style.height).toBe('770px')
    viewport.height = 490
    viewport.offsetTop = 20
    viewport.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(dialog.style.height).toBe('490px')
    expect(dialog.style.top).toBe('20px')

    const frame = dialog.querySelector('iframe')!
    vi.mocked(request).mockResolvedValueOnce({ id: 12 })
    frameEvent(frame, { type: 'tt:chat.request', id: 1, method: 'post', path: `${conversationPath}4/messages/`,
      body: { text: '给自己发一条测试' } })
    viewport.offsetTop = 0
    viewport.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(dialog.style.height).toBe('490px')
    expect(dialog.style.top).toBe('0px')
    viewport.height = 770
    viewport.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(dialog.style.height).toBe('770px')
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
  }
})

it('在后台空闲时预热正式 H5 会话页，待主动打开才发送已读回执', async () => {
  const idle = vi.fn<(callback: () => void) => number>().mockReturnValue(1)
  vi.stubGlobal('requestIdleCallback', idle)
  vi.stubGlobal('cancelIdleCallback', vi.fn())
  const { wrapper } = await mountH5(true, mineLocation, createMemoryHistory(), true)
  try {
    const dialog = mockDialog()
    expect(dialog.querySelector('iframe')).toBeNull()
    expect(idle).toHaveBeenCalledOnce()
    idle.mock.calls[0]![0]()
    await flushPromises()
    const frame = dialog.querySelector('iframe')!
    expect(frame).toBeTruthy()
    expect(dialog.open).toBe(false)
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.ready' }, location.origin)
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, location.origin)
    vi.mocked(request).mockClear()

    const read = { type: 'tt:chat.request', id: 1, method: 'post', path: `${conversationPath}4/read/`, body: { seq: 18 } }
    frameEvent(frame, read, location.origin)
    await flushPromises()
    expect(request).not.toHaveBeenCalled()

    await openChat()
    expect(dialog.querySelector('iframe')).toBe(frame)
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, location.origin)
    frameEvent(frame, { ...read, id: 2 }, location.origin)
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: read.path, method: 'post', data: read.body })
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
  }
})

it('在 App 隐藏时预加载并保留同一聊天页，逐层返回，到根列表才告诉 App 退出', async () => {
  const { default: MobileInternalChat } = await import('../MobileInternalChat.vue')
  const { notifyAppChat, notifyAppChatUnread } = await import('../appChatWebView')
  const report = vi.mocked(notifyAppChat)
  report.mockClear()
  vi.mocked(notifyAppChatUnread).mockClear()
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: defineComponent({ render: () => null }) }],
  })
  const viewport = Object.assign(new EventTarget(), { height: 770, offsetTop: 0 })
  vi.stubGlobal('visualViewport', viewport)
  const showModal = () => { document.querySelector('dialog')!.open = true }
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, writable: true, value: showModal })
  await router.push('/')
  const wrapper = mount(MobileInternalChat, {
    attachTo: document.body,
    props: { appPreview: true },
    global: { plugins: [createPinia(), router] },
  })
  try {
    await flushPromises()
    const frame = document.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage')
    const dialog = document.querySelector<HTMLDialogElement>('dialog[open]')!
    expect(dialog.style.height).toBe('770px')
    viewport.height = 430
    viewport.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(dialog.style.height).toBe('430px')
    expect(document.querySelector('.m-chat-bubble')).toBeNull()
    expect(notifyAppChatUnread).toHaveBeenLastCalledWith(7)
    frameEvent(frame, { type: 'tt:chat.ready' })
    frameEvent(frame, { type: 'tt:chat.listControlsReady' })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, webKLiveOrigin)
    expect(report).toHaveBeenCalledWith('ready')
    window.ttAppChat!.setVisible(true)
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, webKLiveOrigin)
    window.ttAppChat!.back(1)
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.back', id: 1 }, webKLiveOrigin)
    frameEvent(frame, { type: 'tt:chat.backResult', id: 1, detail: true })
    expect(report).toHaveBeenLastCalledWith('back-handled', 1)
    window.ttAppChat!.back(2)
    frameEvent(frame, { type: 'tt:chat.backResult', id: 1, detail: false })
    expect(report).toHaveBeenLastCalledWith('back-handled', 1)
    frameEvent(frame, { type: 'tt:chat.backResult', id: 2, detail: false })
    expect(report).toHaveBeenLastCalledWith('back-empty', 2)
    window.ttAppChat!.setVisible(false)
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, webKLiveOrigin)
    window.ttAppChat!.setVisible(true)
    await flushPromises()
    expect(document.querySelector('iframe')).toBe(frame)
    frameEvent(frame, { type: 'tt:chat.closeFromList' })
    expect(report).toHaveBeenLastCalledWith('close')
  } finally {
    wrapper.unmount()
    expect(notifyAppChatUnread).toHaveBeenLastCalledWith(0)
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
    vi.unstubAllGlobals()
  }
})

it('正式 App 在预热和关闭时不读消息，重新入场沿用原会话并可打开新通知', async () => {
  appHost.retained = true
  const { default: MobileInternalChat } = await import('../MobileInternalChat.vue')
  const { notifyAppChat } = await import('../appChatWebView')
  const showModal = function (this: HTMLDialogElement) { this.open = true }
  Object.assign(HTMLDialogElement.prototype, { showModal })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: defineComponent({ render: () => null }) }],
  })
  await router.push('/')
  const wrapper = mount(MobileInternalChat, {
    attachTo: document.body,
    props: { production: true, embeddedApp: true },
    global: { plugins: [createPinia(), router] },
  })
  try {
    await flushPromises()
    const frame = document.querySelector<HTMLIFrameElement>('dialog iframe')!
    expect(new URL(frame.src).searchParams.get('retainedApp')).toBe('1')
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    const fromChat = (data: object) => frameEvent(frame, data, location.origin)
    fromChat({ type: 'tt:chat.ready' })
    fromChat({ type: 'tt:chat.navigationReady' })
    fromChat({ type: 'tt:chat.listControlsReady' })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, location.origin)
    expect(notifyAppChat).toHaveBeenLastCalledWith('ready')
    const readUrl = `${conversationPath}13/read/`
    const read = { type: 'tt:chat.request', id: 1, method: 'post', path: readUrl, body: { seq: 18 } }
    fromChat(read)
    await flushPromises()
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: readUrl }))

    window.ttAppChat!.setVisible(true)
    await flushPromises()
    fromChat({ ...read, id: 2 })
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: readUrl, method: 'post', data: { seq: 18 } })

    fromChat({ type: 'tt:chat.closeFromList' })
    expect(notifyAppChat).toHaveBeenLastCalledWith('close')
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, location.origin)
    vi.mocked(request).mockClear()

    // The retained thread can still ask to acknowledge a message while a new notification is opening.
    window.ttAppChat!.openTarget(37, 54)
    window.ttAppChat!.setVisible(true)
    await flushPromises()
    expect(post).toHaveBeenCalledWith({
      type: 'tt:chat.openRetainedConversation', detail: { conversationId: 37, messageId: 54, requestId: 1 },
    }, location.origin)
    fromChat({ ...read, id: 3, body: { seq: 22 } })
    await flushPromises()
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: readUrl }))
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.response', id: 3 }, location.origin)

    fromChat({ type: 'tt:chat.retainedNavigationDone', id: 99, detail: true })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.response', id: 3 }, location.origin)
    fromChat({ type: 'tt:chat.retainedNavigationDone', id: 1, detail: true })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, location.origin)
    fromChat({ ...read, id: 4, path: `${conversationPath}37/read/`, body: { seq: 20 } })
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: `${conversationPath}37/read/`, method: 'post', data: { seq: 20 } })
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: readUrl }))

    post.mockClear()
    window.ttAppChat!.setVisible(false)
    window.ttAppChat!.setVisible(true)
    await flushPromises()
    expect(document.querySelector('dialog iframe')).toBe(frame)
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, location.origin)
    expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'tt:chat.openConversation' }), location.origin)

    vi.useFakeTimers()
    window.ttAppChat!.openTarget(49, 62)
    fromChat({ type: 'tt:chat.closeFromList' })
    expect(notifyAppChat).toHaveBeenLastCalledWith('close')
    window.ttAppChat!.setVisible(true)
    await vi.advanceTimersByTimeAsync(12_000)
    const recovered = document.querySelector<HTMLIFrameElement>('dialog iframe')!
    expect(recovered).not.toBe(frame)
    const recoveredPost = vi.spyOn(recovered.contentWindow!, 'postMessage').mockImplementation(() => {})
    fromChat({ type: 'tt:chat.retainedNavigationDone', id: 2, detail: true })
    frameEvent(recovered, { type: 'tt:chat.ready' }, location.origin)
    expect(recoveredPost).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, location.origin)
    frameEvent(recovered, { type: 'tt:chat.listControlsReady' }, location.origin)
    expect(recoveredPost).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, location.origin)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
  }
})

function mockDragGeometry(bubble: HTMLButtonElement) {
  Object.assign(bubble, {
    setPointerCapture: () => {},
    getBoundingClientRect: () => new DOMRect(318, 700, 56, 56),
  })
  const areaBounds = vi.spyOn(bubble.parentElement!, 'getBoundingClientRect')
    .mockReturnValue(new DOMRect(16, 56, 358, 700))
  return () => areaBounds.mockRestore()
}

function pointer(bubble: HTMLButtonElement, type: string, x: number, y: number) {
  bubble.dispatchEvent(new PointerEvent(type, { pointerId: 1, pointerType: 'touch', button: 0, clientX: x, clientY: y }))
}

it('拖动松手不会打开聊天，下次轻点仍能打开', async () => {
  const { wrapper } = await mountH5()
  const bubble = chatBubble()
  const dialog = mockDialog()
  const restoreGeometry = mockDragGeometry(bubble)
  try {
    pointer(bubble, 'pointerdown', 338, 720)
    pointer(bubble, 'pointermove', 138, 520)
    pointer(bubble, 'pointerup', 138, 520)
    bubble.dispatchEvent(new MouseEvent('click', { detail: 1 }))
    await flushPromises()
    expect(dialog.open).toBe(false)
    expect(document.querySelector('iframe[title="内部沟通聊天内容"]')).toBeNull()

    pointer(bubble, 'pointerdown', 138, 520)
    pointer(bubble, 'pointermove', 140, 521)
    pointer(bubble, 'pointerup', 140, 521)
    bubble.dispatchEvent(new MouseEvent('click', { detail: 1 }))
    expect(dialog.open).toBe(true)
  } finally {
    restoreGeometry()
    wrapper.unmount()
    window.localStorage.clear()
  }
})

it('气泡贴近底部拖动后保留按钮空间，切页和重新打开仍记住位置', async () => {
  const { wrapper, router } = await mountH5()
  const bubble = chatBubble()
  const restoreGeometry = mockDragGeometry(bubble)
  let remounted: Awaited<ReturnType<typeof mountH5>> | undefined
  try {
    pointer(bubble, 'pointerdown', 338, 720)
    pointer(bubble, 'pointermove', 138, 900)
    pointer(bubble, 'pointerup', 138, 900)
    await router.push('/hall')
    await flushPromises()
    const draggedStyle = chatBubble().getAttribute('style')
    expect(draggedStyle).toContain('102px')
    expect(draggedStyle).toContain('644px')
    restoreGeometry()
    wrapper.unmount()

    remounted = await mountH5()
    expect(chatBubble().getAttribute('style')).toBe(draggedStyle)
  } finally {
    if (remounted) remounted.wrapper.unmount()
    else if (wrapper.exists()) wrapper.unmount()
    restoreGeometry()
    window.localStorage.clear()
  }
})

it.each(['pointercancel', 'lostpointercapture'])('拖动被 %s 中断时恢复位置，仍可用键盘打开聊天', async (type) => {
  const { wrapper } = await mountH5()
  const bubble = chatBubble()
  const dialog = mockDialog()
  const restoreGeometry = mockDragGeometry(bubble)
  try {
    const initialStyle = bubble.getAttribute('style')
    pointer(bubble, 'pointerdown', 338, 720)
    pointer(bubble, 'pointermove', 138, 520)
    await flushPromises()
    expect(bubble.getAttribute('style')).not.toBe(initialStyle)
    pointer(bubble, type, 138, 520)
    await flushPromises()
    expect(bubble.getAttribute('style') || null).toBe(initialStyle || null)

    bubble.dispatchEvent(new MouseEvent('click', { detail: 0 }))
    expect(dialog.open).toBe(true)
  } finally {
    restoreGeometry()
    wrapper.unmount()
    window.localStorage.clear()
  }
})

it('只给已登录打手显示气泡', async () => {
  const { wrapper } = await mountH5(false)
  try {
    expect(document.querySelector('.m-chat-bubble')).toBeNull()
    expect(request).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('气泡显示真实未读数字，返回后保留原路由、筛选值和滚动位置', async () => {
  const { wrapper, router } = await mountH5()
  try {
    const root = wrapper.element as HTMLElement
    const filter = wrapper.get('input').element
    filter.value = '本地订单筛选'
    root.scrollTop = 180

    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/api/internal-chat/unread/' }))
    const bubble = document.querySelector<HTMLButtonElement>('button[aria-label="打开内部沟通，7 条未读消息"]')
    expect(bubble?.textContent?.trim()).toBe('7')
    expect(document.querySelector('iframe[title="内部沟通聊天内容"]')).toBeNull()
    await openChat()

    const screen = document.querySelector<HTMLDialogElement>('dialog[open]')
    const frame = screen?.querySelector<HTMLIFrameElement>('iframe[title="内部沟通聊天内容"]')
    expect(frame).toBeTruthy()
    expect(screen?.querySelector('header')).toBeNull()
    expect(router.currentRoute.value.fullPath).toBe('/mine?status=pending#order-list')
    frameEvent(frame!, { type: 'tt:chat.listControlsReady' })
    frameEvent(frame!, { type: 'tt:chat.closeFromList' })
    await flushPromises()

    expect(document.querySelector('dialog[open]')).toBeNull()
    expect(document.querySelector('iframe[title="内部沟通聊天内容"]')).toBe(frame)
    expect(router.currentRoute.value.fullPath).toBe('/mine?status=pending#order-list')
    expect(wrapper.get('input').element).toBe(filter)
    expect(filter.value).toBe('本地订单筛选')
    expect(root.scrollTop).toBe(180)
    expect(document.activeElement).toBe(bubble)
    await openChat()
    expect(document.querySelector('dialog[open] iframe')).toBe(frame)
    expect(frame?.getAttribute('src')).toBe(`${webKLiveOrigin}/?local-chat=1&noServiceWorker=1&list=1&live=1&h5=1`)
  } finally {
    wrapper.unmount()
  }
})

it('加载时仍能关闭，列表可用后只接收当前聊天页面的关闭操作', async () => {
  const { wrapper } = await mountH5()
  try {
    const dialog = await openChat()
    const loadingClose = () => dialog.querySelector<HTMLButtonElement>('button[aria-label="关闭内部沟通"]')
    expect(loadingClose()).toBeTruthy()
    loadingClose()?.click()
    expect(dialog.open).toBe(false)

    await openChat()
    const frame = dialog.querySelector('iframe')!
    frameEvent(frame, { type: 'tt:chat.listControlsReady' })
    await flushPromises()
    expect(loadingClose()).toBeNull()

    dialog.dispatchEvent(new Event('cancel', { cancelable: true }))
    window.dispatchEvent(new MessageEvent('message', {
      origin: webKLiveOrigin,
      source: window,
      data: { type: 'tt:chat.closeFromList' },
    }))
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'https://other.example',
      source: frame.contentWindow,
      data: { type: 'tt:chat.closeFromList' },
    }))
    expect(dialog.open).toBe(true)

    frameEvent(frame, { type: 'tt:chat.closeFromList' })
    expect(dialog.open).toBe(false)
    await openChat()
    expect(loadingClose()).toBeNull()
  } finally {
    wrapper.unmount()
  }
})

it('聊天页面重载后恢复关闭入口，列表重新就绪后交还关闭操作', async () => {
  const { wrapper } = await mountH5()
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.listControlsReady' })
    await flushPromises()
    expect(dialog.querySelector('button[aria-label="关闭内部沟通"]')).toBeNull()

    frame.dispatchEvent(new Event('load'))
    await flushPromises()
    expect(dialog.querySelector('button[aria-label="关闭内部沟通"]')).toBeTruthy()
    expect(post).toHaveBeenCalledWith({ type: 'tt:chat.requestListControlsReady' }, webKLiveOrigin)

    frameEvent(frame, { type: 'tt:chat.listControlsReady' })
    await flushPromises()
    expect(dialog.querySelector('button[aria-label="关闭内部沟通"]')).toBeNull()
  } finally {
    wrapper.unmount()
  }
})

it('退出登录后，已发出的聊天请求成功也不会再次读取未读数', async () => {
  const { wrapper, session } = await mountH5()
  try {
    const dialog = await openChat()
    const iframe = dialog.querySelector('iframe')!
    let finishSending!: (response: unknown) => void
    const sending = new Promise((resolve) => { finishSending = resolve })
    vi.mocked(request).mockReturnValueOnce(sending)
    const url = `${conversationPath}1/messages/`
    const body = { text: '测试消息' }
    frameEvent(iframe, { type: 'tt:chat.request', id: 1, method: 'post', path: url, body })
    expect(request).toHaveBeenLastCalledWith({ url, method: 'post', data: body })

    session.clear()
    await flushPromises()
    expect(document.querySelector('.m-chat-bubble')).toBeNull()
    expect(document.querySelector('iframe[title="内部沟通聊天内容"]')).toBeNull()
    vi.mocked(request).mockClear()
    finishSending({ id: 1 })
    await flushPromises()

    expect(request).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('收起聊天后拒绝迟到的已读回执，重新打开后仍可发送回执', async () => {
  const { wrapper } = await mountH5()
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.ready' })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: true }, webKLiveOrigin)
    frameEvent(frame, { type: 'tt:chat.closeFromList' })
    expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.visible', detail: false }, webKLiveOrigin)
    const url = `${conversationPath}1/read/`
    const body = { seq: 18 }
    frameEvent(frame, { type: 'tt:chat.request', id: 1, method: 'post', path: url, body })
    expect(request).not.toHaveBeenCalledWith({ url, method: 'post', data: body })
    await openChat()
    frameEvent(frame, { type: 'tt:chat.request', id: 2, method: 'post', path: url, body })
    expect(request).toHaveBeenCalledWith({ url, method: 'post', data: body })
    await flushPromises()
  } finally {
    wrapper.unmount()
  }
})

it('气泡显示服务端汇总的实际未读与手动未读提醒，不再读取全部会话', async () => {
  vi.mocked(request).mockResolvedValue({ unread_count: 8 })
  const { wrapper } = await mountH5()
  try {
    expect(document.querySelector('.m-chat-bubble')?.getAttribute('aria-label')).toBe('打开内部沟通，8 条未读消息')
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/api/internal-chat/unread/' }))
    expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: conversationPath }))
  } finally {
    wrapper.unmount()
  }
})

it('只有打开的聊天能转发群置顶和取消置顶', async () => {
  const { wrapper } = await mountH5()
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    const path = `${conversationPath}1/pin/`
    const pin = { type: 'tt:chat.request', id: 1, method: 'post', path, body: { message_id: 44 } }
    frameEvent(frame, pin)
    expect(request).toHaveBeenCalledWith({ url: path, method: 'post', data: pin.body })
    await flushPromises()

    frameEvent(frame, { type: 'tt:chat.closeFromList' })
    const unpin = { ...pin, id: 2, body: { message_id: null } }
    frameEvent(frame, unpin)
    expect(request).not.toHaveBeenCalledWith({ url: path, method: 'post', data: unpin.body })
    expect(post).toHaveBeenLastCalledWith({
      type: 'tt:chat.response', id: 2, error: '请打开聊天后再操作置顶',
    }, webKLiveOrigin)

    await openChat()
    frameEvent(frame, { ...unpin, id: 3 })
    expect(request).toHaveBeenCalledWith({ url: path, method: 'post', data: unpin.body })
    await flushPromises()
  } finally {
    wrapper.unmount()
  }
})

it.each([mineLocation, detailPath])('从群聊打开订单详情后返回同一个聊天（%s）', async (initial) => {
  const { wrapper, router } = await mountH5(true, initial)
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    frameEvent(frame, { type: 'tt:chat.openOrder', detail: orderNo })
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(detailPath))
    await flushPromises()
    expect(dialog.open).toBe(false)
    await wrapper.get('[data-testid="detail-back"]').trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(initial))
    await flushPromises()
    expect(dialog.open).toBe(true)
    expect(dialog.querySelector('iframe')).toBe(frame)
  } finally {
    wrapper.unmount()
  }
})

it('连续点订单详情的返回只退一个浏览器历史记录', async () => {
  window.history.replaceState(null, '', '/m/mine')
  const history = createWebHistory('/m/')
  const { wrapper, router } = await mountH5(true, mineLocation, history)
  const navigateHistory = vi.spyOn(history, 'go')
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    frameEvent(frame, { type: 'tt:chat.openOrder', detail: orderNo })
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(detailPath))
    const backButton = wrapper.get<HTMLButtonElement>('[data-testid="detail-back"]').element
    backButton.click()
    backButton.click()
    expect(navigateHistory).toHaveBeenCalledExactlyOnceWith(-1)
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(mineLocation))
    expect(dialog.open).toBe(true)
    expect(dialog.querySelector('iframe')).toBe(frame)
  } finally {
    wrapper.unmount()
    history.destroy()
    window.history.replaceState(null, '', '/')
  }
})

it('路由守卫拦下订单详情时，留在原页面和同一个聊天', async () => {
  const { wrapper, router } = await mountH5()
  const stopGuard = router.beforeEach((to) => to.name !== 'm-order-detail')
  try {
    const dialog = await openChat()
    const frame = dialog.querySelector('iframe')!
    frameEvent(frame, { type: 'tt:chat.openOrder', detail: orderNo })
    expect(dialog.open).toBe(false)
    await vi.waitFor(() => expect(dialog.open).toBe(true))
    expect(router.currentRoute.value.fullPath).toBe(mineLocation)
    expect(dialog.querySelector('iframe')).toBe(frame)
  } finally {
    stopGuard()
    wrapper.unmount()
  }
})


it('H5订单首次发送成功后通过共用入口打开同一Mobile Web K会话', async () => {
  const base = `/api/internal-chat/orders/${orderNo}/`
  const empty = { id: null, has_conversation: false, title: '合成订单', order_no: orderNo,
    local_order_no: orderNo, is_member: false, can_join: false, can_send: true, members: [],
    self_member_id: null, archived: false, can_reopen: false }
  const conversation = { ...empty, id: 1000000031, has_conversation: true, is_member: true, self_member_id: 7 }
  vi.mocked(request).mockImplementation(async config => {
    if (config.url?.includes('/detail/')) return { order_no: orderNo, source_order_no: '', status: 'IN_PROGRESS',
      game_name: '测试游戏', server_name: '合成区服', project_text: '合成项目', content: '合成订单内容',
      demand: '', time_requirement: '', game_account: '', game_sub_account: '', game_role_name: '',
      is_urgent: false, remark: '', claimed_at: null, temp_amount: '0.00', group_qr_url: null }
    if (config.url === base) return empty
    if (config.url === base + 'messages/' && config.method === 'POST') return { conversation,
      messages: [{ id: 1000000032, seq: 1, conversation_id: conversation.id }] }
    if (config.url?.endsWith('/unread/')) return { unread_count: 0 }
    return { conversations: [] }
  })
  const { wrapper } = await mountH5(true, detailPath, createMemoryHistory(), true)
  try {
    const dialog = mockDialog()
    expect(request).not.toHaveBeenCalledWith({ method: 'GET', url: base })
    await wrapper.get('[data-testid="detail-open-internal-chat"]').trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()
    await wrapper.get('textarea').setValue('H5首次合成消息')
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    await flushPromises()
    expect(dialog.open).toBe(true)
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.ready' }, location.origin)
    frameEvent(frame, { type: 'tt:chat.navigationReady' }, location.origin)
    expect(post).toHaveBeenCalledWith({ type: 'tt:chat.openConversation', detail: conversation.id }, location.origin)
    expect(vi.mocked(request).mock.calls.some(([config]) => config.url?.endsWith('/join/'))).toBe(false)
  } finally { wrapper.unmount() }
})

it.each([
  { action: '主动关闭后离开', closeFirst: true },
  { action: '浏览器返回离开', closeFirst: false },
])('聊天导航就绪前$action，再点气泡不会跳到上一单的内部群', async ({ closeFirst }) => {
  const base = `/api/internal-chat/orders/${orderNo}/`
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url?.includes('/detail/')) return { order_no: orderNo, source_order_no: '', status: 'IN_PROGRESS',
      game_name: '测试游戏', project_text: '合成项目', temp_amount: '0.00' }
    if (url === base) return { id: 81, title: '合成订单', order_no: orderNo, local_order_no: orderNo,
      is_member: true, can_join: true, members: [] }
    return { unread_count: 0 }
  })
  const { wrapper, router } = await mountH5(true, mineLocation, createMemoryHistory(), true)
  try {
    await router.push(detailPath)
    await vi.waitFor(() => expect(document.querySelector('dialog.m-chat-screen')).not.toBeNull())
    const dialog = mockDialog()
    await flushPromises()
    await wrapper.get('[data-testid="detail-open-internal-chat"]').trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(dialog.open).toBe(true)
    const frame = dialog.querySelector('iframe')!
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    frameEvent(frame, { type: 'tt:chat.ready' }, location.origin)

    if (closeFirst) dialog.querySelector<HTMLButtonElement>('.m-chat-loading-close')!.click()
    router.back()
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(mineLocation))
    expect(dialog.open).toBe(false)
    document.querySelector<HTMLButtonElement>('.m-chat-bubble')!.click()
    expect(dialog.open).toBe(true)
    frameEvent(frame, { type: 'tt:chat.navigationReady' }, location.origin)

    expect(post).toHaveBeenCalledWith({ type: 'tt:chat.visible', detail: true }, location.origin)
    expect(post).not.toHaveBeenCalledWith({ type: 'tt:chat.openConversation', detail: 81 }, location.origin)
  } finally { wrapper.unmount() }
})
