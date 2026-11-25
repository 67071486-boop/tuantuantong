// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, createWebHistory, RouterView } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { User } from '@/api/auth'
import { request } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import mRouter from '../../router'
import { installMGuards } from '../../router/guards'
import MeWxPusherPage from '../MeWxPusherPage.vue'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

enableAutoUnmount(afterEach)
installFramework7ForTest()
const elementScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
let workerHistory: ReturnType<typeof createWebHistory> | undefined

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
})
afterEach(() => {
  if (elementScrollIntoView) Object.defineProperty(Element.prototype, 'scrollIntoView', elementScrollIntoView)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  workerHistory?.destroy()
  workerHistory = undefined
  window.history.replaceState(null, '', '/')
  localStorage.removeItem('ttt:worker-page:tenant_synthetic:19')
  vi.restoreAllMocks()
})

const url = '/api/auth/wxpusher/preferences'
const orderSwitch = '[role="switch"][aria-label="订单状态变更"]'
const settlementSwitch = '[role="switch"][aria-label="结算结果"]'
const orderDetails = '[data-testid="wxpusher-order-details"]'
const summaryPath = '/me/notifications'
const hallSubscriptionPath = '/me/notifications#hall-subscription'
const orderPath = '/me/notifications/order'
const refundedSwitch = '[role="switch"][aria-label="订单已退单"]'

function samplePreferences() {
  return {
    order_enabled: true,
    settlement_enabled: true,
    order_events: {
      assigned: true, unassigned: true, review_rejected: true, completed: true,
      paused_resumed: true, issue: true, refunded: true,
    },
  }
}

async function openPreferences(path = summaryPath, canSubscribeToHall = false) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: summaryPath, name: 'm-me-notifications', component: MeWxPusherPage },
      { path: orderPath, name: 'm-me-notifications-order', component: MeWxPusherPage, props: { orderDetails: true } },
    ],
  })
  await router.push(path)
  const pinia = createPinia()
  if (canSubscribeToHall) {
    const session = useSessionStore(pinia)
    session.$patch({ status: 'ready', user: { menu_permissions: ['grab_hall.take_order'] } as typeof session.user })
  }
  const wrapper = mount(RouterView, {
    attachTo: document.getElementById('framework7-root')!,
    global: { plugins: [pinia, router] },
  })
  await flushPromises()
  return { router, wrapper }
}

async function openWorkerPage(path: string, hallPermission = true) {
  window.history.replaceState(null, '', `/m${path}`)
  workerHistory = createWebHistory('/m/')
  const router = createRouter({ history: workerHistory, routes: mRouter.options.routes })
  installMGuards(router)
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  const worker: User = {
    id: 19, username: 'test_worker', display_name: '测试打手', identity_plane: 'tenant_synthetic',
    roles: ['代练员'], game_ids: [], permissions: [], is_tenant_admin: false,
    menu_permissions: hallPermission ? ['my_profile', 'grab_hall.take_order'] : ['my_profile'],
  }
  session.$patch({ status: 'ready', user: worker })
  vi.mocked(request).mockImplementation(async ({ url: requestedUrl }) => {
    if (requestedUrl === '/api/auth/wxpusher/guide') return { binding_enabled: true, bound: true }
    if (requestedUrl === url) return samplePreferences()
    if (requestedUrl === '/api/auth/wxpusher/hall-subscription') return { enabled: true, games: [] }
    if (requestedUrl === '/api/auth/me') return { user: worker }
    if (requestedUrl === '/api/business/games/') return { items: [], total: 0 }
    if (requestedUrl === '/api/auth/csrf') return null
    throw new Error(`意外请求 ${requestedUrl}`)
  })
  const scrollIntoView = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
  const wrapper = mount(RouterView, {
    attachTo: document.getElementById('framework7-root')!,
    global: { plugins: [pinia, router] },
  })
  await router.isReady()
  await flushPromises()
  return { router, wrapper, scrollIntoView }
}

it('已绑定时从大厅点击如何快速抢单，直接打开并定位到展开的抢单订阅', async () => {
  const { router, wrapper, scrollIntoView } = await openWorkerPage('/hall')
  const help = wrapper.get('.m-navbar__hall-help')
  expect(help.text()).toBe('如何快速抢单')
  await help.trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(hallSubscriptionPath))
  await flushPromises()

  const subscription = wrapper.get('[data-testid="wxpusher-hall-subscription"]')
  expect(subscription.attributes('aria-expanded')).toBe('true')
  expect(wrapper.get('#wxpusher-hall-subscription-content button[type="submit"]').text()).toBe('保存订阅')
  expect(scrollIntoView.mock.contexts).toContain(subscription.element)
  expect(document.activeElement).toBe(subscription.element)
  expect(wrapper.find('[role="dialog"][aria-modal="true"]').exists()).toBe(false)
  await wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/hall'))
})

it('消息通知页复用时，定位链接展开抢单订阅并允许手动收起', async () => {
  const { router, wrapper, scrollIntoView } = await openWorkerPage(summaryPath)
  const subscription = wrapper.get('[data-testid="wxpusher-hall-subscription"]')
  expect(subscription.attributes('aria-expanded')).toBe('false')
  await router.push(hallSubscriptionPath)
  await flushPromises()
  expect(subscription.attributes('aria-expanded')).toBe('true')
  expect(scrollIntoView.mock.contexts).toContain(subscription.element)
  await subscription.trigger('click')
  expect(subscription.attributes('aria-expanded')).toBe('false')
})

it('没有大厅接单权限时，从定位链接打开消息通知也看不到抢单订阅', async () => {
  const { router, wrapper, scrollIntoView } = await openWorkerPage(hallSubscriptionPath, false)
  expect(router.currentRoute.value.fullPath).toBe(hallSubscriptionPath)
  expect(wrapper.get('h1').text()).toBe('消息通知')
  expect(wrapper.find('[data-testid="wxpusher-hall-subscription"]').exists()).toBe(false)
  expect(scrollIntoView).not.toHaveBeenCalled()
  expect(request).not.toHaveBeenCalledWith(expect.objectContaining({
    url: '/api/auth/wxpusher/hall-subscription', method: 'GET',
  }))
})

it('绑定区排在前面，通知记录、抢单订阅与推送开关共用一张设置卡', async () => {
  vi.mocked(request)
    .mockResolvedValueOnce({ binding_enabled: true, bound: false })
    .mockResolvedValueOnce(samplePreferences())
  const { wrapper } = await openPreferences(summaryPath, true)
  expect(wrapper.findAll('[role="heading"][aria-level="2"]').map((heading) => heading.text()))
    .toEqual(['绑定 WxPusher', '推送设置'])

  const preferences = wrapper.get('[data-testid="wxpusher-preferences"]')
  const list = preferences.get('.list')
  expect(list.get('[data-testid="wxpusher-notification-inbox"]').text()).toContain('通知记录')
  expect(list.get('[data-testid="wxpusher-hall-subscription"]').text()).toContain('抢单订阅')
  expect(list.get(orderSwitch).attributes('aria-checked')).toBe('true')
  expect(list.get(settlementSwitch).attributes('aria-checked')).toBe('true')

  const hallButton = list.get('[data-testid="wxpusher-hall-subscription"]').element
  const orderButton = list.get(orderDetails).element
  expect(hallButton.closest('.item-inner')).not.toBeNull()
  expect(orderButton.closest('.item-inner')).not.toBeNull()
  expect(orderButton.contains(list.get(orderSwitch).element)).toBe(false)
})

it('未绑定时也能设置自己的推送，关闭总开关保留七项明细的选择', async () => {
  let preferences = samplePreferences()
  vi.mocked(request).mockImplementation(async ({ url: requestedUrl, method, data }) => {
    if (requestedUrl === '/api/auth/wxpusher/guide') return { binding_enabled: true, bound: false }
    if (method === 'PATCH') {
      preferences = {
        ...preferences,
        ...data,
        order_events: { ...preferences.order_events, ...data.order_events },
      }
    }
    return preferences
  })
  const { router, wrapper } = await openPreferences()

  expect(request).toHaveBeenCalledWith({ url, method: 'GET' })
  expect(wrapper.text()).toContain('绑定 WxPusher 后接收手机推送')
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('true')
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
  expect(wrapper.find(refundedSwitch).exists()).toBe(false)
  expect(wrapper.get(orderDetails).attributes('aria-expanded')).toBe('false')
  await wrapper.get(orderDetails).trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe(summaryPath)
  expect(wrapper.get(orderDetails).attributes('aria-expanded')).toBe('true')
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('true')
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
  const orderEvents = wrapper.findAll('#wxpusher-order-events [role="switch"]')
  expect(orderEvents.map((input) => input.attributes('aria-label'))).toEqual([
    '新指派给我', '撤销指派', '交单被退回', '订单已完成', '订单暂停或恢复', '订单被标为问题单', '订单已退单',
  ])
  expect(orderEvents.every((input) => input.attributes('aria-checked') === 'true')).toBe(true)

  await wrapper.get(refundedSwitch).trigger('click')
  await flushPromises()
  expect(request).toHaveBeenLastCalledWith({ url, method: 'PATCH', data: { order_events: { refunded: false } } })
  expect(wrapper.get(refundedSwitch).attributes('aria-checked')).toBe('false')
  await wrapper.get(orderDetails).trigger('click')
  expect(wrapper.find(refundedSwitch).exists()).toBe(false)
  await wrapper.get(orderDetails).trigger('click')
  expect(wrapper.get(refundedSwitch).attributes('aria-checked')).toBe('false')

  await wrapper.get(orderSwitch).trigger('click')
  await flushPromises()
  expect(request).toHaveBeenLastCalledWith({ url, method: 'PATCH', data: { order_enabled: false } })
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('false')
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
  await wrapper.get(orderSwitch).trigger('click')
  await flushPromises()
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('true')

  await wrapper.get(settlementSwitch).trigger('click')
  await flushPromises()
  expect(request).toHaveBeenLastCalledWith({ url, method: 'PATCH', data: { settlement_enabled: false } })
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('true')

  wrapper.unmount()
  const reopened = await openPreferences(orderPath)
  expect(reopened.wrapper.get(refundedSwitch).attributes('aria-checked')).toBe('false')
  await reopened.wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
  await flushPromises()
  expect(reopened.router.currentRoute.value.path).toBe(summaryPath)
  expect(reopened.wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('false')
})

it('读取失败后可以重试，保存失败保留原设置供打手再次操作', async () => {
  const preferences = samplePreferences()
  vi.mocked(request)
    .mockResolvedValueOnce({ binding_enabled: true, bound: true })
    .mockRejectedValueOnce(new Error('请求失败'))
    .mockResolvedValueOnce(preferences)
    .mockRejectedValueOnce(new Error('请求失败'))
    .mockResolvedValueOnce({ ...preferences, settlement_enabled: false })
  const { wrapper } = await openPreferences()
  expect(wrapper.get('[role="alert"]').text()).toContain('推送设置加载失败')
  expect(wrapper.find(settlementSwitch).exists()).toBe(false)
  await wrapper.get('[data-testid="retry-wxpusher-preferences"]').trigger('click')
  await flushPromises()
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
  await wrapper.get(settlementSwitch).trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('未能确认保存，请再试一次')
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
  await wrapper.get(settlementSwitch).trigger('click')
  await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('false')
})


it.each([
  { bound: true, status: '已开启' },
  { bound: false, status: '绑定后生效' },
])('聊天提醒显示$status，没有个人开关', async ({ bound, status }) => {
  const preferences = { ...samplePreferences(), group_chat_enabled: true }
  vi.mocked(request).mockImplementation(async ({ url: requestedUrl }) => {
    if (requestedUrl === '/api/auth/wxpusher/guide') return { binding_enabled: true, bound }
    return preferences
  })
  const { wrapper } = await openPreferences()
  expect(wrapper.text()).toContain(`聊天提醒${status}`)
  expect(wrapper.find('[role="switch"][aria-label="聊天提醒"]').exists()).toBe(false)
  expect(wrapper.get(orderSwitch).attributes('aria-checked')).toBe('true')
  expect(wrapper.get(settlementSwitch).attributes('aria-checked')).toBe('true')
})
