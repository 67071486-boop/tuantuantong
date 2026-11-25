// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionStore } from '@/stores/session'

const api = vi.hoisted(() => ({ games: vi.fn(), qr: vi.fn(), mine: vi.fn(), orders: vi.fn(), service: vi.fn() }))
vi.mock('@/api/config', () => ({ fetchGames: api.games }))
vi.mock('@/api/homeQrCodes', () => ({ fetchHomeQrCodes: api.qr }))
vi.mock('@/api/myOrders', () => ({ fetchMyOrders: api.mine }))
vi.mock('@/api/orders', () => ({ fetchOrders: api.orders }))
vi.mock('@/api/serviceDashboard', () => ({ fetchServiceDashboard: api.service }))
import V2RoleDashboard from '../V2RoleDashboard.vue'

beforeEach(() => {
  vi.resetAllMocks()
  api.games.mockResolvedValue([{ id: 1, name: '示例游戏' }])
  api.qr.mockResolvedValue({ items: [{ id: 1, is_enabled: true, media_url: '/example.png', kind: 'qq_bot' }, { id: 2, is_enabled: false, media_url: '/hidden.png', kind: 'qq_bot' }] })
  api.mine.mockResolvedValue({ total: 1, items: [{ order_no: 'TEST-1', source_order_no: '', project_text: '示例项目', status: 'COMPLETED' }] })
  api.orders.mockResolvedValue({ total: 1, items: [] })
  api.service.mockResolvedValue({ status_counts: { WAITING_GRAB: 3, ISSUE: 2 }, today_created: 4, today_completed: 5, today_followed: 6, attention_orders: [{ order_no: '20261114000000000002', source_order_no: '', project_text: '工作项目', status: 'ISSUE', game_name: '示例游戏', updated_at: '2026-11-14T10:00:00+08:00' }] })
})

async function render(layout: 'worker' | 'work', nodes: string[]) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = { id: 1, username: 'test', display_name: '示例用户', identity_plane: 'tenant', is_tenant_admin: false, is_account_admin: false, roles: ['示例职位'], permissions: [], menu_permissions: nodes, game_ids: [1] }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, ...['v2-my-orders', 'v2-income', 'v2-hall', 'v2-profile', 'v2-messages', 'v2-order-new', 'v2-order-manage', 'v2-performance', 'v2-user-list'].map((name) => ({ path: `/${name}`, name, component: { template: '<div />' } }))] })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(V2RoleDashboard, { props: { layout }, global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('角色工作台', () => {
  it('打手展示真实个人订单、授权游戏和启用二维码；快捷入口按节点过滤', async () => {
    const { wrapper, router } = await render('worker', ['my_orders', 'my_orders.send_to_my_system'])
    expect(api.mine).toHaveBeenCalledWith({ page: 1, page_size: 5 })
    expect(api.orders).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('示例游戏')
    expect(wrapper.text()).toContain('TEST-1')
    expect(wrapper.find('.cv2-st--mark').text()).toBe('已完成')
    expect(wrapper.find('.cv2-st--mark').classes()).toEqual(expect.arrayContaining(['cv2-order-status', 'cv2-st--done']))
    expect(wrapper.find('.cv2-st--mark .cv2-dot').exists()).toBe(true)
    expect(wrapper.findAll('img')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('管理订单')
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('v2-my-orders')
  })
  it('快捷入口逐个跳到对应的 V2 页面', async () => {
    const pairs = [
      ['我的订单', 'v2-my-orders'], ['我的收入', 'v2-income'], ['内部接单', 'v2-hall'],
      ['我的资料', 'v2-profile'], ['消息通知', 'v2-messages'], ['录入订单', 'v2-order-new'],
      ['管理订单', 'v2-order-manage'], ['结算情况', 'v2-performance'], ['系统子账户', 'v2-user-list'],
    ]
    const { wrapper, router } = await render('work', ['my_orders', 'my_performance', 'grab_hall', 'my_profile', 'order.add_edit', 'order.list', 'finance.payroll', 'system_user.list'])
    for (const [label, route] of pairs) {
      const button = wrapper.findAll('button').find((item) => item.text().startsWith(label!))
      expect(button).toBeDefined()
      await button!.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.name).toBe(route)
    }
  })
  it('工作看板只调用已有受权限和范围约束的订单接口', async () => {
    const { wrapper, router } = await render('work', ['order.list'])
    expect(api.service).toHaveBeenCalledOnce()
    expect(api.orders).not.toHaveBeenCalled()
    expect(api.mine).not.toHaveBeenCalled()
    expect(api.qr).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('20261114000000000002')
    expect(wrapper.text()).toContain('今日跟进')
    expect(wrapper.text()).toContain('问题单')
    expect(wrapper.find('.service-table .cv2-dot').exists()).toBe(true)
    await wrapper.findAll('.service-stat')[2]!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('v2-order-manage')
    expect(router.currentRoute.value.query).toEqual({ self_sales: '1', status: 'ISSUE' })
    await wrapper.get('.service-order-link').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({ self_sales: '1', keyword: '20261114000000000002', open_order: '20261114000000000002' })
  })
  it('选择布局不会授予订单权限', async () => {
    const { wrapper } = await render('worker', [])
    expect(api.mine).not.toHaveBeenCalled()
    expect(api.orders).not.toHaveBeenCalled()
    expect(api.service).not.toHaveBeenCalled()
    expect(wrapper.find('[data-role-orders]').exists()).toBe(false)
    expect(wrapper.findAll('button')).toHaveLength(0)
  })
  it('无订单权限时不请求客服订单统计', async () => {
    await render('work', [])
    expect(api.service).not.toHaveBeenCalled()
  })
  it('读取失败明确提示，不伪装为零订单', async () => {
    api.service.mockRejectedValue(new Error('failed'))
    const { wrapper } = await render('work', ['order.list'])
    expect(wrapper.text()).toContain('订单数据加载失败')
    expect(wrapper.text()).not.toContain('暂无订单')
  })
})
