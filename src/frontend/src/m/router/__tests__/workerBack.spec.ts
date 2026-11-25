// @vitest-environment jsdom
import { selectOption } from '../../components/__tests__/selectOption'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia } from 'pinia'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { BizError, HttpError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import mRouter from '../../router'
import { installMGuards } from '../guards'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

const orderNo = '10000000000000000012'
const detailPath = `/orders/${orderNo}`
const elementScroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTo')
const elementScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
const scrollIntoView = vi.fn()
let dismiss: (() => void) | undefined

afterEach(() => {
  dismiss?.()
  dismiss = undefined
  if (elementScroll) Object.defineProperty(Element.prototype, 'scrollTo', elementScroll)
  else Reflect.deleteProperty(Element.prototype, 'scrollTo')
  if (elementScrollIntoView) Object.defineProperty(Element.prototype, 'scrollIntoView', elementScrollIntoView)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  vi.resetAllMocks()
  vi.restoreAllMocks()
})

async function open(
  initial: string, permissions: string[], getDetail: () => unknown = () => new Promise(() => {}),
  options: { delayOrders?: boolean } = {},
) {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: vi.fn() })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
  window.history.replaceState(null, '', `/m${initial}`)
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  session.$patch({
    status: 'ready',
    user: { id: 61, identity_plane: 'tenant_synthetic', roles: ['代练员'], menu_permissions: permissions } as typeof session.user,
  })
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url === '/api/auth/wxpusher/guide') return { binding_enabled: false, bound: false }
    if (url === '/api/auth/profile') return { is_real_name: false, avatar_url: null }
    if (url === '/api/business/feedback/') return { items: [], total: 0 }
    if (url === '/api/business/performances/') return {
      items: [{
        id: 1, user: 61, order_no: orderNo, source_order_no: '', order_status: 'ISSUE',
        dl_project: '合成项目甲', emp_type: '打单工资', temp_amount: '21.50', bonus_amount: '12.00',
        settle_status: '已结账',
      }],
      total: 1,
      summary: { temp_amount: '21.50', bonus_amount: '12.00' },
    }
    if (url?.endsWith('/screenshots')) return { items: [] }
    if (url?.endsWith('/detail/')) return getDetail()
    if (url === '/api/business/my-orders/' && options.delayOrders) return new Promise(() => {})
    if (url === '/api/business/my-orders/') return {
      items: [
        {
          id: 1, order_no: orderNo, source_order_no: '', status: 'ISSUE',
          game_name: '测试游戏甲', project_text: '合成项目甲',
          temp_amount: '21.50', bonus_amount: '12.00', settle_status: '已结账',
        },
        { id: 2, order_no: '10000000000000000013', source_order_no: '', status: 'IN_PROGRESS', game_name: '测试游戏乙', project_text: '合成项目乙' },
      ],
      total: 2,
    }
    throw new Error(`意外请求 ${url}`)
  })
  const history = createWebHistory('/m/')
  const router = createRouter({ history, routes: mRouter.options.routes })
  installMGuards(router)
  const wrapper = mount(RouterView, {
    attachTo: document.getElementById('framework7-root')!,
    global: { plugins: [pinia, router] },
  })
  dismiss = () => {
    wrapper.unmount()
    history.destroy()
  }
  await router.isReady()
  await flushPromises()
  return { router, wrapper }
}

installFramework7ForTest()

it('从通知直接打开订单时，加载中可返回我的订单', async () => {
  const { router, wrapper } = await open(`/wxpusher${detailPath}?entry=wxpusher`, ['my_orders', 'my_orders.send_to_my_system'])
  expect(router.currentRoute.value.params.orderNo).toBe(orderNo)
  expect(wrapper.get('[data-testid="detail-loading"]').text()).toContain('加载中')
  const back = wrapper.get('[data-testid="detail-back"]')
  expect(back.classes()).toContain('back')
  expect(back.find('.icon-back').exists()).toBe(true)
  await back.trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-mine'), { timeout: 3000 })
})

it('从列表进入详情再打开截图，返回使用对应的站内历史并保留查询参数', async () => {
  const { router, wrapper } = await open('/mine?status=ISSUE', ['my_orders', 'my_orders.send_to_my_system', 'order.view_screenshot'])
  await router.push(detailPath)
  await router.push(`${detailPath}/screenshots`)
  await flushPromises()
  await wrapper.get('[data-testid="shot-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(detailPath))
  await flushPromises()
  await wrapper.get('[data-testid="detail-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/mine?status=ISSUE'))
})

it('截图从外部直接打开后没有订单权限时，返回有权限的打手页', async () => {
  const { router, wrapper } = await open(`${detailPath}/screenshots`, ['order.view_screenshot', 'my_profile'])
  await wrapper.get('[data-testid="shot-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-me'))
})

it('从外部直接进入通知分类明细，返回到通知设置再返回我的', async () => {
  const { router, wrapper } = await open('/me/notifications/order', ['my_profile'])
  await wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.name).toBe('m-me-notifications')
  await wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-me'))
})

it('从我的订单进入详情后，返回保留搜索、游戏选择和滚动位置', async () => {
  const { router, wrapper } = await open('/mine', ['my_orders', 'my_orders.send_to_my_system'])
  const gameFilter = '[data-testid="mine-game-filter"]'
  const search = '[data-testid="mine-search"] input'
  const scroll = () => wrapper.get('.m-shell__scroll').element as HTMLElement
  scroll().scrollTop = 184
  await wrapper.get(search).setValue('合成项目甲')
  await selectOption(wrapper, 'mine-game-filter', '测试游戏甲')
  await wrapper.get(`[data-order-no="${orderNo}"]`).trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-order-detail'))
  await wrapper.get('[data-testid="detail-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-mine'))
  await flushPromises()
  expect((wrapper.get(search).element as HTMLInputElement).value).toBe('合成项目甲')
  expect(wrapper.get(gameFilter).attributes('aria-label')).toBe('筛选游戏：测试游戏甲')
  expect(scroll().scrollTop).toBe(184)
})

it.each([
  ['/me/profile', '[data-testid="profile-back"]'],
  ['/me/password', '[data-testid="password-back"]'],
  ['/me/feedback', '[data-testid="feedback-back"]'],
  ['/me/notifications', '[data-testid="wxpusher-guide-back"]'],
])('%s 直达时，返回按钮进入我的', async (entry, backButton) => {
  const { router, wrapper } = await open(entry, ['my_profile', 'home.feedback'])
  await wrapper.get(backButton).trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-me'))
})

it.each([
  new HttpError(403, '该单不是你接的，不能查看详情'),
  new BizError(1002, '记录不存在或已被删除'),
])('截图直达且原订单详情不可访问时，返回我的订单（$message）', async (detailError) => {
  const { router, wrapper } = await open(`${detailPath}/screenshots`, ['my_orders', 'my_orders.send_to_my_system', 'order.view_screenshot'], () => { throw detailError })
  await wrapper.get('[data-testid="shot-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-mine'))
})

it('站内从详情进入截图，期间失去订单归属时回到我的订单', async () => {
  let reassigned = false
  const { router, wrapper } = await open(detailPath, ['my_orders', 'my_orders.send_to_my_system', 'order.view_screenshot'], () => {
    if (reassigned) throw new HttpError(403, '该单不是你接的，不能查看详情')
    return {
      order_no: orderNo, status: 'ISSUE', project_text: '合成项目甲',
      game_name: '合成游戏甲', server_name: '', game_account: '', game_sub_account: '',
      game_role_name: '', remark: '', content: '', demand: '', time_requirement: '', temp_amount: '20.00',
    }
  })
  await wrapper.get('[data-testid="detail-open-screenshots"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-order-screenshots'))
  await flushPromises()
  reassigned = true
  await wrapper.get('[data-testid="shot-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-mine'))
})

it('缺少读取我的订单的操作权限时，返回跳过该栏目', async () => {
  const { router, wrapper } = await open(`${detailPath}/screenshots`, [
    'my_orders', 'my_performance', 'my_profile', 'order.view_screenshot',
  ])
  await wrapper.get('[data-testid="shot-back"]').trigger('click')
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('m-me'))
  expect(request).not.toHaveBeenCalledWith(expect.objectContaining({
    url: `/api/business/my-orders/${orderNo}/detail/`,
  }))
})

it('旧绩效链接迁入同一个我的订单页面，只有绩效权限时只取绩效', async () => {
  const { router, wrapper } = await open('/performance?source=bookmark', [
    'my_performance', 'my_performance.export',
  ])
  expect(router.currentRoute.value.name).toBe('m-mine')
  expect(router.currentRoute.value.hash).toBe('#performance')
  expect(router.currentRoute.value.query.source).toBe('bookmark')
  expect(wrapper.get('[data-testid="perf-summary-temp"]').text()).toBe('21.50')
  expect(scrollIntoView.mock.contexts).toContain(wrapper.get('#performance').element)
  expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: '/api/business/my-orders/' }))
})

it('只有订单权限时打开旧链接会回到订单，且不取绩效', async () => {
  const { router, wrapper } = await open('/performance', ['my_orders', 'my_orders.send_to_my_system'])
  expect(router.currentRoute.value.fullPath).toBe('/mine')
  expect(wrapper.find('[data-testid="mine-search"]').exists()).toBe(true)
  expect(request).not.toHaveBeenCalledWith(expect.objectContaining({ url: '/api/business/performances/' }))
})

it('旧链接可在订单请求尚未完成时查看绩效汇总', async () => {
  const { wrapper } = await open('/performance', [
    'my_orders', 'my_orders.send_to_my_system', 'my_performance', 'my_performance.export',
  ], undefined, { delayOrders: true })
  expect(wrapper.text()).toContain('正在加载订单')
  expect(wrapper.get('[data-testid="perf-summary-temp"]').text()).toBe('21.50')
  expect(scrollIntoView.mock.contexts).toContain(wrapper.get('#performance').element)
})

it('旧绩效链接打开合并列表，同一笔订单只显示一张卡', async () => {
  const { router, wrapper } = await open('/performance', [
    'my_orders', 'my_orders.send_to_my_system', 'my_performance', 'my_performance.export',
  ])
  expect(router.currentRoute.value.fullPath).toBe('/mine#performance')
  const cards = wrapper.findAll(`[data-order-no="${orderNo}"]`)
  expect(cards).toHaveLength(1)
  expect(cards[0]?.text()).toContain('合成项目甲')
  expect(cards[0]?.text()).toContain('21.50')
  expect(cards[0]?.text()).toContain('12.00')
  expect(cards[0]?.text()).toContain('已结账')
})
