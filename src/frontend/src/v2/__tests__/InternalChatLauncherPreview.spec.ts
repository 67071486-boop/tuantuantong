// @vitest-environment jsdom
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import { h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import type { User } from '@/api/auth'
import { invalidateChatSession } from '@/api/chatSession'
import * as chatTransport from '@/api/chatTransport'
import InternalChatWideView from '../internalChat/InternalChatWideView.vue'
import InternalChatLauncherPreview from '../internalChat/InternalChatLauncherPreview.vue'

const preview = vi.hoisted(() => ({
  fetchMe: vi.fn(), dispose: vi.fn(),
  live: true, openVisitorConversation: vi.fn(), openConversation: vi.fn(), receiveMessage: undefined as ((data: unknown) => void) | undefined,
}))
vi.mock('@/api/auth', () => ({ fetchMe: preview.fetchMe }))
vi.mock('../internalChat/liveChatPreview', async (importOriginal) => ({
  ...await importOriginal<typeof import('../internalChat/liveChatPreview')>(),
  get liveChatPreview() { return preview.live },
  attachLiveChatPreview: (...args: Parameters<typeof import('../internalChat/liveChatPreview').attachLiveChatPreview>) => {
    preview.receiveMessage = args[3]?.onIncomingMessage
    args[3]?.onNavigationReady?.()
    return {
      reportOpen: vi.fn(), openConversation: preview.openConversation,
      openVisitorConversation: preview.openVisitorConversation, dispose: preview.dispose,
    }
  },
}))

enableAutoUnmount(afterEach)
afterEach(() => {
  preview.live = true
  preview.openConversation.mockClear()
  preview.openVisitorConversation.mockClear()
  preview.dispose.mockClear()
  preview.fetchMe.mockReset()
  preview.receiveMessage = undefined
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function holdIdle() {
  let pending: IdleRequestCallback | undefined
  vi.stubGlobal('requestIdleCallback', (callback: IdleRequestCallback) => {
    pending = callback
    return 1
  })
  vi.stubGlobal('cancelIdleCallback', vi.fn(() => { pending = undefined }))
  return async () => {
    const callback = pending
    pending = undefined
    callback?.({ didTimeout: false, timeRemaining: () => 20 })
    await nextTick()
  }
}

const desktopIdentity = { id: 19, identity_plane: 'demo-studio' }

function mountLauncher(notificationOwner?: string) {
  preview.fetchMe.mockResolvedValue(desktopIdentity)
  return mount(InternalChatLauncherPreview, {
    props: { openRequest: 0, notificationOwner, identity: desktopIdentity },
    global: { plugins: [createPinia()] },
  })
}

function chatPanel() {
  return document.getElementById('cv2-chat-launcher-window')!
}

function launcherButton() {
  return document.querySelector<HTMLButtonElement>('[aria-controls="cv2-chat-launcher-window"]')!
}

it('warms up the hidden window on idle without waiting for all page resources', async () => {
  const allowIdle = holdIdle()
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('interactive')
  mountLauncher()

  expect(chatPanel().querySelector('iframe')).toBeNull()

  const unreadLabel = launcherButton().getAttribute('aria-label')
  expect(unreadLabel).toMatch(/\d+ 条未读消息/)
  await allowIdle()
  expect(chatPanel().querySelector('iframe')).not.toBeNull()
  expect(chatPanel().hasAttribute('inert')).toBe(true)
  expect(launcherButton().getAttribute('aria-label')).toBe(unreadLabel)
})

it('opens immediately before the idle turn and keeps the same chat when folded', async () => {
  const allowIdle = holdIdle()
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('complete')
  const wrapper = mountLauncher()

  expect(chatPanel().querySelector('iframe')).toBeNull()
  await wrapper.setProps({ openRequest: 1 })
  const chat = chatPanel().querySelector('iframe')
  expect(chat).not.toBeNull()
  expect(chatPanel().hasAttribute('inert')).toBe(false)

  launcherButton().click()
  await allowIdle()
  expect(chatPanel().hasAttribute('inert')).toBe(true)
  expect(chatPanel().querySelector('iframe')).toBe(chat)
})

it('reopens the same iframe after handing off to an order conversation', async () => {
  holdIdle()
  const wrapper = mountLauncher()
  await wrapper.setProps({ openRequest: 1, conversationId: 81 })
  const chat = chatPanel().querySelector('iframe')
  expect(chat).not.toBeNull()
  expect(preview.openConversation).toHaveBeenCalledExactlyOnceWith(81)

  launcherButton().click()
  await nextTick()
  expect(chatPanel().hasAttribute('inert')).toBe(true)
  launcherButton().click()
  await nextTick()
  expect(chatPanel().hasAttribute('inert')).toBe(false)
  expect(chatPanel().querySelector('iframe')).toBe(chat)
  expect(preview.openConversation).toHaveBeenCalledExactlyOnceWith(81)
})

it('waits until the user returns to a hidden tab before preparing the chat', async () => {
  const allowIdle = holdIdle()
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('complete')
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  mountLauncher()

  await allowIdle()
  expect(chatPanel().querySelector('iframe')).toBeNull()

  hidden.mockReturnValue(false)
  document.dispatchEvent(new Event('visibilitychange'))
  await allowIdle()
  expect(chatPanel().querySelector('iframe')).not.toBeNull()
})

it('cancels a pending preload when the desktop session removes the launcher', async () => {
  holdIdle()
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('complete')
  const loggedIn = ref(true)
  mount({
    setup: () => () => loggedIn.value ? h(InternalChatLauncherPreview, { openRequest: 0 }) : null,
  }, { global: { plugins: [createPinia()] } })
  expect(chatPanel().querySelector('iframe')).toBeNull()

  loggedIn.value = false
  await nextTick()
  expect(window.cancelIdleCallback).toHaveBeenCalledWith(1)
  expect(document.getElementById('cv2-chat-launcher-window')).toBeNull()
})

it('keeps click-only loading when the live preview is disabled', async () => {
  preview.live = false
  const allowIdle = holdIdle()
  vi.spyOn(document, 'readyState', 'get').mockReturnValue('complete')
  mountLauncher()

  await allowIdle()
  expect(chatPanel().querySelector('iframe')).toBeNull()

  launcherButton().click()
  await nextTick()
  expect(chatPanel().querySelector('iframe')).not.toBeNull()
})

function mockBrowserNotifications() {
  const permission = vi.fn(async (): Promise<NotificationPermission> => {
    BrowserNotification.permission = 'granted'
    return 'granted'
  })
  const notices: BrowserNotification[] = []
  class BrowserNotification {
    static permission: NotificationPermission = 'default'
    static requestPermission = permission
    onclick: ((event: Event) => void) | null = null
    close = vi.fn()

    constructor(readonly title: string, readonly options: NotificationOptions) {
      notices.push(this)
    }
  }
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('Notification', BrowserNotification)
  vi.spyOn(window, 'focus').mockImplementation(() => {})
  return { BrowserNotification, permission, notices }
}

function notificationButton() {
  return chatPanel().querySelector<HTMLButtonElement>('.cv2-chat-notification-control [data-enable-browser-notifications]')!
}

it('requests browser permission from the button, then opens the notified conversation and ignores duplicates', async () => {
  const { permission, notices } = mockBrowserNotifications()
  const wrapper = mountLauncher('demo-studio:staff:19')
  await wrapper.setProps({ openRequest: 1 })
  expect(notificationButton().textContent).toContain('开启浏览器通知')
  expect(permission).not.toHaveBeenCalled()

  preview.receiveMessage?.({ id: 70, conversationId: 12 })
  expect(notices).toHaveLength(0)

  notificationButton().click()
  await flushPromises()
  expect(permission).toHaveBeenCalledTimes(1)
  expect(notificationButton()).toBeNull()

  launcherButton().click()
  await nextTick()
  expect(chatPanel().hasAttribute('inert')).toBe(true)
  preview.receiveMessage?.({ id: 71, conversationId: 12, body: '绝不显示' })
  preview.receiveMessage?.({ id: 71, conversationId: 12 })
  preview.receiveMessage?.({ id: 0, conversationId: 12 })
  await flushPromises()
  expect(notices).toHaveLength(1)
  expect(notices[0]).toMatchObject({
    title: '团团通 · 内部沟通',
    options: { body: '你有一条新的内部沟通消息' },
  })

  notices[0]!.onclick?.(new Event('click'))
  await flushPromises()
  expect(window.focus).toHaveBeenCalledTimes(1)
  expect(preview.openConversation).toHaveBeenLastCalledWith(12)
  expect(chatPanel().hasAttribute('inert')).toBe(false)
  expect(notices[0]!.close).toHaveBeenCalledTimes(1)

  expect(notificationButton()).toBeNull()
})

it('checks for permission on each open and lets the account dismiss the prompt for that visit', async () => {
  const { BrowserNotification, permission } = mockBrowserNotifications()
  const wrapper = mountLauncher('demo-studio:staff:19')
  await wrapper.setProps({ openRequest: 1 })
  expect(notificationButton()).not.toBeNull()

  chatPanel().querySelector<HTMLButtonElement>('[aria-label="暂不提醒"]')?.click()
  await nextTick()
  expect(notificationButton()).toBeNull()

  launcherButton().click()
  BrowserNotification.permission = 'denied'
  launcherButton().click()
  await nextTick()
  expect(notificationButton()).toBeNull()
  expect(chatPanel().textContent).toContain('在网站设置中允许')
  expect(permission).not.toHaveBeenCalled()
})

it('does not pass an opt-in or an unfinished permission request to the next account', async () => {
  const { BrowserNotification, permission, notices } = mockBrowserNotifications()
  let allow: ((value: NotificationPermission) => void) | undefined
  permission.mockImplementationOnce(() => new Promise((resolve) => { allow = resolve }))
  const wrapper = mountLauncher('demo-studio:staff:19')
  await wrapper.setProps({ openRequest: 1 })
  notificationButton().click()
  await wrapper.setProps({ notificationOwner: 'demo-studio:staff:20' })
  BrowserNotification.permission = 'granted'
  allow?.('granted')
  await flushPromises()

  preview.receiveMessage?.({ id: 71, conversationId: 12 })
  expect(notices).toHaveLength(0)
  notificationButton().click()
  await flushPromises()
  preview.receiveMessage?.({ id: 72, conversationId: 12 })
  await flushPromises()
  expect(notices).toHaveLength(1)

  await wrapper.setProps({ notificationOwner: undefined })
  expect(notices[0]!.close).toHaveBeenCalledTimes(1)
  expect(notices[0]!.onclick).toBeNull()
  expect(notificationButton()).toBeNull()
})

it('rejects a new signal from a previous account after the same browser changes its session', async () => {
  const { BrowserNotification, notices } = mockBrowserNotifications()
  BrowserNotification.permission = 'granted'
  const wrapper = mountLauncher('demo-studio:staff:19')
  await wrapper.setProps({ openRequest: 1 })
  notificationButton().click()
  await flushPromises()

  preview.receiveMessage?.({ id: 71, conversationId: 12 })
  await flushPromises()
  expect(notices).toHaveLength(1)

  preview.dispose.mockClear()
  preview.fetchMe.mockResolvedValue({ id: 20, identity_plane: 'demo-studio' })
  preview.receiveMessage?.({ id: 72, conversationId: 12 })
  await flushPromises()
  expect(notices).toHaveLength(1)
  expect(notices[0]!.close).toHaveBeenCalledTimes(1)
  expect(preview.dispose).toHaveBeenCalledTimes(1)
  expect(chatPanel()).toBeNull()
})

it('shares browser opt-in across the launcher and wide view, claiming each scoped message once', async () => {
  const { BrowserNotification, notices } = mockBrowserNotifications()
  BrowserNotification.permission = 'granted'
  const locks = vi.fn(async (_key: string, claim: () => void) => claim())
  Object.defineProperty(window.navigator, 'locks', { configurable: true, value: { request: locks } })
  try {
    const launcher = mountLauncher('示例工作室:demo-studio:19')
    await launcher.setProps({ openRequest: 1 })
    const fromLauncher = preview.receiveMessage
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/wide', name: 'v2-internal-chat-window', component: InternalChatWideView }],
    })
    await router.push('/wide?conversation=6')
    const pinia = createPinia()
    useSessionStore(pinia).user = {
      ...desktopIdentity, internal_chat_enabled: true, tenant_name: '示例工作室',
    } as User
    const wide = mount(InternalChatWideView, { global: { plugins: [pinia, router] } })
    const fromWide = preview.receiveMessage
    expect(fromWide).not.toBe(fromLauncher)
    await flushPromises()
    await wide.get('[data-enable-browser-notifications]').trigger('click')
    await flushPromises()
    window.dispatchEvent(new StorageEvent('storage', { key: 'tt.internal-chat.desktop-notifications.示例工作室:demo-studio:19' }))
    await nextTick()
    expect(notificationButton()).toBeNull()

    const visitor = { id: 71, conversationId: 12, scope: 'visitor' }
    fromWide?.(visitor)
    fromLauncher?.(visitor)
    await flushPromises()
    expect(notices).toHaveLength(1)
    expect(locks).toHaveBeenCalledTimes(2)
    notices[0]!.onclick?.(new Event('click'))
    await flushPromises()
    expect(preview.openVisitorConversation).toHaveBeenCalledExactlyOnceWith(12)

    fromLauncher?.({ id: 71, conversationId: 21 })
    await flushPromises()
    expect(notices).toHaveLength(2)
    notices[1]!.onclick?.(new Event('click'))
    await flushPromises()
    expect(preview.openConversation).toHaveBeenLastCalledWith(21)
  } finally {
    Reflect.deleteProperty(window.navigator, 'locks')
  }
})

it('does not display a notification after its login changed while it awaited the cross-window lock', async () => {
  const { BrowserNotification, notices } = mockBrowserNotifications()
  BrowserNotification.permission = 'granted'
  let claim: (() => void) | undefined
  Object.defineProperty(window.navigator, 'locks', {
    configurable: true,
    value: { request: vi.fn((key: string, callback: () => void) => { claim = callback }) },
  })
  try {
    const launcher = mountLauncher('demo-studio:staff:19')
    await launcher.setProps({ openRequest: 1 })
    notificationButton().click()
    await flushPromises()
    preview.receiveMessage?.({ id: 75, conversationId: 12 })
    await flushPromises()
    expect(claim).toBeTypeOf('function')
    invalidateChatSession()
    claim?.()
    expect(notices).toHaveLength(0)
  } finally {
    Reflect.deleteProperty(window.navigator, 'locks')
  }
})

it('keeps the notification target when the wide window’s default conversation arrives late', async () => {
  const { BrowserNotification, notices } = mockBrowserNotifications()
  BrowserNotification.permission = 'granted'
  preview.fetchMe.mockResolvedValue(desktopIdentity)
  let defaultList: ((value: { conversations: { id: number }[] }) => void) | undefined
  vi.spyOn(chatTransport, 'chatRequest').mockImplementationOnce(() => new Promise(resolve => { defaultList = resolve }))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/wide', name: 'v2-internal-chat-window', component: InternalChatWideView }],
  })
  await router.push('/wide')
  const pinia = createPinia()
  useSessionStore(pinia).user = {
    ...desktopIdentity, internal_chat_enabled: true, tenant_name: '示例工作室',
  } as User
  const wide = mount(InternalChatWideView, { global: { plugins: [pinia, router] } })
  await flushPromises()
  await wide.get('[data-enable-browser-notifications]').trigger('click')
  await flushPromises()
  preview.receiveMessage?.({ id: 75, conversationId: 12, scope: 'visitor' })
  await flushPromises()
  expect(notices).toHaveLength(1)
  notices[0]!.onclick?.(new Event('click'))
  await flushPromises()
  expect(preview.openVisitorConversation).toHaveBeenCalledExactlyOnceWith(12)
  expect(defaultList).toBeTypeOf('function')
  defaultList?.({ conversations: [{ id: 6 }] })
  await flushPromises()
  expect(preview.openConversation).not.toHaveBeenCalled()
})
