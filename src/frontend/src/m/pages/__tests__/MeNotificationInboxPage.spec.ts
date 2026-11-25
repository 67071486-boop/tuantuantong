// @vitest-environment jsdom
import { createPinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import mobileRouter from '../../router'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

installFramework7ForTest()
enableAutoUnmount(afterEach)
beforeEach(() => vi.resetAllMocks())

const inbox = '/api/auth/wxpusher/inbox'
const records = '/me/notifications/records'
const orderNo = '10000000000000000012'
const notice = {
  id: 11, kind: 'review_rejected', title: '订单提交验收不通过', order_no: '测试单 12',
  lines: ['未通过原因　请补交结单截图。'], target: `/m/orders/${orderNo}`,
  created_at: '2026-11-17T00:05:00Z', read_at: null,
}

async function open(path: string) {
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  session.$patch({ status: 'ready', user: { menu_permissions: ['my_profile'] } as typeof session.user })
  const router = createRouter({ history: createMemoryHistory('/m/'), routes: mobileRouter.options.routes })
  await router.push(path)
  const wrapper = mount(RouterView, {
    attachTo: document.getElementById('framework7-root')!,
    global: { plugins: [pinia, router] },
  })
  await flushPromises()
  return { wrapper, router }
}

it('打手未绑定也能从引导页查看通知，展开内容标记已读', async () => {
  vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (url === '/api/auth/wxpusher/guide') return { binding_enabled: false, bound: false, pending_binding: null }
    if (url === inbox && method === 'GET') return { items: [{ ...notice }], unread_count: 1, total: 1, page_size: 25 }
    if (url === `${inbox}/11/read` && method === 'POST') return { read_at: '2026-11-17T00:12:00Z' }
    throw new Error(`unexpected ${method} ${url}`)
  })
  const { wrapper, router } = await open('/me/notifications')
  expect(wrapper.text()).toContain('绑定暂未开放')

  await wrapper.get('[data-testid="wxpusher-notification-inbox"] a').trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(records))
  expect(wrapper.text()).toContain('未读 1 条')
  expect(wrapper.text()).toContain('订单 测试单 12')
  await wrapper.get('.m-notification-inbox__headline').trigger('click')
  await flushPromises()

  expect(request).toHaveBeenLastCalledWith({ url: `${inbox}/11/read`, method: 'POST' })
  expect(wrapper.text()).toContain('暂无未读通知')
  expect(wrapper.text()).toContain('请补交结单截图')
  expect(wrapper.get('[data-testid="notification-inbox-target"]').attributes('href')).toBe(`/m/orders/${orderNo}`)
  await wrapper.get('[data-testid="notification-inbox-back"]').trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/me/notifications'))

  await router.push(records)
  await flushPromises()
  await wrapper.get('.m-notification-inbox__headline').trigger('click')
  await flushPromises()
  await wrapper.get('[data-testid="notification-inbox-target"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(`/orders/${orderNo}`))
})

it('按分类分页并过滤不安全的外部详情链接', async () => {
  vi.mocked(request).mockImplementation(async ({ url, params }) => {
    if (url !== inbox) throw new Error(`unexpected ${url}`)
    return {
      items: [{ ...notice, read_at: '2026-11-17T00:12:00Z', target: 'https://example.invalid/' }],
      unread_count: 0, total: params.category ? 1 : 28, page_size: 25,
    }
  })
  const { wrapper } = await open(records)

  const clickButton = async (label: string) => {
    const button = wrapper.findAll('button').find((candidate) => candidate.text() === label)
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
  }
  await clickButton('下一页')
  expect(request).toHaveBeenLastCalledWith({ url: inbox, method: 'GET', params: { page: 2, category: undefined } })
  await clickButton('结算')
  expect(request).toHaveBeenLastCalledWith({ url: inbox, method: 'GET', params: { page: 1, category: 'settlement' } })
  await wrapper.get('.m-notification-inbox__headline').trigger('click')
  expect(wrapper.find('[data-testid="notification-inbox-target"]').exists()).toBe(false)
})
