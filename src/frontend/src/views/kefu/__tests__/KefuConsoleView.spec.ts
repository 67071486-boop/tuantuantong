// @vitest-environment jsdom
// ↑ 本仓库测试默认 environment 是 `node`（`vitest.config.ts:16`），挂载组件必须单独覆盖。
import { createPinia, setActivePinia } from 'pinia'
import { pickSelect, provideFramework7 } from './dialogFixture'
import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/orders', () => ({
  fetchOrders: vi.fn(),
  fetchOrder: vi.fn(),
  createOrder: vi.fn(),
  updateOrder: vi.fn(),
  // ⭐ 编辑视图的 `KefuOrderForm` 走带回信封 msg 的出口（vitest 对
  // 工厂里没列的导出会在访问时抛「No export defined on the mock」⇒ 必须列全）。
  updateOrderWithMessage: vi.fn(),
  readPlatformOrder: vi.fn(),
  fetchKefuOptions: vi.fn(),
  fetchAssigneeOptions: vi.fn(),
}))
vi.mock('@/api/config', () => ({
  fetchGames: vi.fn(),
  fetchServers: vi.fn(),
  listConfig: vi.fn(),
}))
vi.mock('@/api/orderSources', () => ({ fetchOrderSources: vi.fn() }))
vi.mock('@/api/auditLogs', () => ({ reportOrderOpenEditQuietly: vi.fn() }))
vi.mock('@/api/http', async (importOriginal) => {
  const http = await importOriginal<typeof import('@/api/http')>()
  return {
    ...http,
    request: (config: Parameters<typeof http.request>[0]) => config.url === '/api/internal-chat/conversations/'
      ? Promise.resolve({ conversations: [] })
      : http.request(config),
  }
})

import {
  createOrder,
  fetchAssigneeOptions,
  fetchKefuOptions,
  fetchOrder,
  fetchOrders,
  updateOrderWithMessage,
  type Order,
  type OrderListItem,
} from '@/api/orders'
import { fetchGames, fetchServers, listConfig, type ConfigRow } from '@/api/config'
import { fetchOrderSources } from '@/api/orderSources'
import { reportOrderOpenEditQuietly } from '@/api/auditLogs'
import { BizError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import KefuConsoleView from '../KefuConsoleView.vue'

const api = {
  fetchOrders: vi.mocked(fetchOrders),
  fetchOrder: vi.mocked(fetchOrder),
  // createOrder 在 Task 6 才被用例直接用；提前放进 `api` 是为了让 `beforeEach` 的
  // `mockReset()` 一并覆盖它（否则 1022 的 rejected 桩会漏到后面的用例里）。
  createOrder: vi.mocked(createOrder),
  fetchKefuOptions: vi.mocked(fetchKefuOptions),
  fetchAssigneeOptions: vi.mocked(fetchAssigneeOptions),
  fetchGames: vi.mocked(fetchGames),
  fetchServers: vi.mocked(fetchServers),
  listConfig: vi.mocked(listConfig),
  fetchOrderSources: vi.mocked(fetchOrderSources),
  updateOrderWithMessage: vi.mocked(updateOrderWithMessage),
}
const reportOpenEdit = vi.mocked(reportOrderOpenEditQuietly)

// 自动卸载 + 移除 document 监听：搜索浮层的「点外部关」是 document 级监听，
// 不卸载会跨用例累积（stale wrapper 的监听照样在跑）。
provideFramework7('md')
enableAutoUnmount(afterEach)

/** 示例订单号：20 位纯数字（后端 `lookup_value_regex = r"\d{20}"`）。 */
const ORDER_NO = '20261020123456789012'

const ALL_NODES = [
  'order.list',
  'order.add_edit',
  'order.submit_edit',
  'order.assign_taker',
  'order.view_detail',
  'order.view_content',
  'order.allow_change_sales_agent',
]

function rowFixture(overrides: Partial<OrderListItem> = {}): OrderListItem {
  return {
    order_no: ORDER_NO,
    source_order_no: '',
    source: '闲管家',
    taker_name: '',
    status: 'UNASSIGNED',
    game: 3,
    server: null,
    project_text: '王者荣耀/上分',
    total_price: '88.00',
    refund_fee: '0.00',
    customer_phone: '13800000000',
    is_urgent: false,
    created_at: '2026-10-20T10:30:00+08:00',
    game_role_name: '',
    customer_ww: '买家昵称',
    hall_price: null,
    sales_agent: '',
    created_by: 'kefu',
    remark: '',
    net_received: '88.00',
    hall_publisher_name: '',
    completed_at: null,
    time_requirement: '',
    payment_status: 'UNSETTLED',
    payment_method: '',
    taker_settled: false,
    ...overrides,
  }
}

/** `Order` 全量键（缺键在 `vue-tsc` 下会红）。 */
function orderFixture(overrides: Partial<Order> = {}): Order {
  return {
    order_no: ORDER_NO,
    status: 'UNASSIGNED',
    order_type: 'BOOST',
    game: 3,
    server: 11,
    category: 21,
    project: 31,
    project_text: '王者荣耀/上分',
    split_id: '',
    content: '商品内容',
    demand: '',
    time_requirement: '',
    game_account: '',
    game_sub_account: '',
    game_role_name: '',
    current_info_1: '',
    current_info_2: '',
    customer_phone: '13800000000',
    customer_qq: '',
    customer_ww: '买家昵称',
    total_price: '88.00',
    received_price: '50.00',
    platform_price: '60.00',
    hall_price: null,
    refund_fee: '0.00',
    fee: '5.00',
    payment_method: '',
    payment_status: 'UNSETTLED',
    source: '闲管家',
    order_source: 1,
    source_order_no: '',
    sales_agent: '客服小九',
    is_urgent: false,
    remark: '',
    tag: '',
    created_by: 'kefu',
    updated_by: 'kefu',
    created_at: '2026-10-20T10:30:00+08:00',
    updated_at: '2026-10-20T11:30:00+08:00',
    allowed_transitions: [],
    completed_requires_review: false,
    ...overrides,
  }
}

const GAME: ConfigRow = { id: 3, name: '王者荣耀', sort_order: 0, is_active: true, created_at: '' }
const PROJECT: ConfigRow = { id: 31, name: '星耀上王者', sort_order: 0, is_active: true, created_at: '' }

function pageOf(items: OrderListItem[], total = items.length) {
  return { items, total, page: 1, page_size: 20 }
}

/** 手写一个可外部 resolve 的 promise（测 latest-wins 的「慢响应」用）。 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: { template: '<div />' } },
      { path: '/kefu', name: 'kefu-console', component: { template: '<div />' } },
    ],
  })
}

function mountPage(nodes: string[] = ALL_NODES) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.status = 'ready'
  session.user = {
    id: 1,
    username: 'kefu',
    display_name: '小客服',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    permissions: [],
    menu_permissions: nodes,
  }
  const router = makeRouter()
  const wrapper = mount(KefuConsoleView, { attachTo: document.body, global: { plugins: [pinia, router] } })
  return { wrapper, router, session }
}

/** 在订单号搜索框里输入 + 回车（页面唯一入口；输入框 name 固定为 `order_search`）。 */
async function searchFor(wrapper: VueWrapper, q: string): Promise<void> {
  const input = wrapper.find('.kefu-searchbar input[type="search"]')
  await input.setValue(q)
  await wrapper.get('form.kefu-searchbar').trigger('submit')
  await flushPromises()
}

let openSpy: ReturnType<typeof vi.spyOn>
beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    class ResizeObserverStub {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    Object.defineProperty(globalThis, 'ResizeObserver', {
      value: ResizeObserverStub,
      configurable: true,
    })
  }
  openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
})
afterAll(() => {
  openSpy.mockRestore()
})

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  reportOpenEdit.mockReset()
  openSpy.mockClear()
  api.fetchOrders.mockResolvedValue(pageOf([rowFixture()]))
  api.fetchOrder.mockResolvedValue(orderFixture())
  api.createOrder.mockResolvedValue(orderFixture())
  api.updateOrderWithMessage.mockResolvedValue({ order: orderFixture(), message: '' })
  api.fetchGames.mockResolvedValue([GAME])
  api.fetchServers.mockResolvedValue([])
  api.listConfig.mockResolvedValue({ items: [PROJECT], total: 1, page: 1, page_size: 200 })
  api.fetchOrderSources.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
  api.fetchKefuOptions.mockResolvedValue({ items: [], truncated: false })
  api.fetchAssigneeOptions.mockResolvedValue({
    items: [],
    truncated: false,
    roles: [],
    defAssRecManUid: 0,
    defAssRecMan: '',
    defAssIsHall: false,
  })
})

describe('客服工作台（/kefu）· 窄窗改版', () => {
  it('预载客服聊天，展开时复用窗口并跳过浏览器通知提示', async () => {
    let idle: IdleRequestCallback | undefined
    const requestPermission = vi.fn()
    class BrowserNotification {
      static permission = 'default'
      static requestPermission = requestPermission
    }
    vi.stubGlobal('isSecureContext', true)
    vi.stubGlobal('Notification', BrowserNotification)
    vi.stubGlobal('requestIdleCallback', (callback: IdleRequestCallback) => { idle = callback; return 1 })
    vi.stubGlobal('cancelIdleCallback', () => { idle = undefined })
    try {
      const { session } = mountPage()
      await flushPromises()
      const launcher = () => document.querySelector<HTMLButtonElement>('button[aria-label="打开内部通讯"]')
      expect(launcher()).toBeNull()
      expect(idle).toBeUndefined()

      session.user = { ...session.user!, internal_chat_enabled: true }
      await vi.waitFor(() => expect(launcher()?.getAttribute('aria-expanded')).toBe('false'))
      idle?.({ didTimeout: false, timeRemaining: () => 20 })
      await flushPromises()
      const frame = document.querySelector<HTMLIFrameElement>('iframe[title="内部通讯聊天内容"]')
      expect(frame?.src).toContain('/internal-chat-webk/')
      expect(frame?.closest('section')?.hasAttribute('inert')).toBe(true)

      launcher()!.click()
      await flushPromises()
      expect(document.querySelector('iframe[title="内部通讯聊天内容"]')).toBe(frame)
      expect(frame?.closest('section')?.hasAttribute('inert')).toBe(false)
      expect(frame?.closest('section')?.querySelector('.cv2-chat-notification-control')).toBeNull()
      expect(requestPermission).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('从内部通讯打开页内订单详情，返回后保留原录单草稿', async () => {
    const { wrapper, session } = mountPage()
    session.user = { ...session.user!, internal_chat_enabled: true }
    const draft = wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]')
    await draft.setValue('演示录单草稿')

    const launcher = () => document.querySelector<HTMLButtonElement>('button[aria-label="打开内部通讯"]')
    await vi.waitFor(() => expect(launcher()).not.toBeNull())
    launcher()!.click()
    await flushPromises()
    const frame = document.querySelector<HTMLIFrameElement>('iframe[title="内部通讯聊天内容"]')!
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'tt:chat.openOrder', detail: ORDER_NO },
      origin: location.origin,
      source: frame.contentWindow,
    }))
    await flushPromises()
    expect(api.fetchOrder).toHaveBeenCalledExactlyOnceWith(ORDER_NO)
    expect(wrapper.get('.kefu-title').text()).toBe('订单详情')
    expect(frame.closest('section')?.hasAttribute('inert')).toBe(true)

    await wrapper.get('.kefu-back').trigger('click')
    expect(wrapper.get('.kefu-title').text()).toBe('快捷录单')
    expect(draft.element.value).toBe('演示录单草稿')
  })

  it('默认 Material，切换 iOS 再切回时保留录单草稿和已展开的搜索结果', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    const draft = wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]')
    await draft.setValue('演示录单草稿')
    await searchFor(wrapper, ORDER_NO)

    const picker = wrapper.get('[role="group"][aria-label="页面风格"]')
    const [ios, material] = picker.findAll('button')
    expect(ios?.text()).toBe('iOS')
    expect(material?.text()).toBe('Material')
    expect(material?.attributes('aria-pressed')).toBe('true')

    await ios!.trigger('pointerdown')
    await ios!.trigger('click')
    expect(ios?.attributes('aria-pressed')).toBe('true')
    expect(document.documentElement.classList.contains('ios')).toBe(true)
    expect(wrapper.get('.kefu-result-card').text()).toContain(ORDER_NO)
    expect(wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]').element.value).toBe('演示录单草稿')

    await material!.trigger('pointerdown')
    await material!.trigger('click')
    expect(material?.attributes('aria-pressed')).toBe('true')
    expect(document.documentElement.classList.contains('md')).toBe(true)
    expect(wrapper.get('.kefu-result-card').text()).toContain(ORDER_NO)
    expect(wrapper.get<HTMLInputElement>('input[type="search"]').element.value).toBe(ORDER_NO)
    expect(wrapper.get<HTMLTextAreaElement>('textarea[name="project_text"]').element.value).toBe('演示录单草稿')
    expect(api.fetchOrders).toHaveBeenCalledTimes(1)
  })

  it('默认是录单表单：不请求订单列表，也不显示状态筛选/＋录单', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    expect(api.fetchOrders).not.toHaveBeenCalled()
    expect(wrapper.find('.kefu-form').exists()).toBe(true)
    expect(wrapper.find('.kefu-status-filter').exists()).toBe(false)
    expect(wrapper.find('.kefu-add').exists()).toBe(false)
    const navbar = wrapper.get('.kefu-topbar')
    expect(navbar.get('.kefu-user').text()).toBe('小客服')
    expect(navbar.find('a').exists()).toBe(false)
  })

  it('订单号搜索：回车 ⇒ 按 page_size=20 查单，结果以卡片列出', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    expect(api.fetchOrders).toHaveBeenCalledWith({ keyword: ORDER_NO, page_size: 20 })
    expect(wrapper.find('.kefu-result-card').exists()).toBe(true)
    expect(wrapper.text()).toContain('王者荣耀/上分')
  })

  it('搜索超过 20 条 ⇒ 如实提示只显示前 20 条', async () => {
    api.fetchOrders.mockResolvedValue({ items: [rowFixture()], total: 25, page: 1, page_size: 20 })
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    expect(wrapper.text()).toContain('共 25 条，只显示前 20 条')
  })

  it('点搜索结果卡片 ⇒ 页内详情（不是 window.open）', async () => {
    api.fetchOrder.mockResolvedValue(orderFixture({ payment_status: 'SETTLING' }))
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    await wrapper.find('.kefu-result-card').trigger('click')
    await flushPromises()
    expect(api.fetchOrder).toHaveBeenCalledWith(ORDER_NO)
    expect(wrapper.find('.kefu-detail').exists()).toBe(true)
    expect(wrapper.findAll('.kefu-detail .list')).toHaveLength(1)
    expect(wrapper.find('.kefu-search-panel').exists()).toBe(false)
    expect(openSpy).not.toHaveBeenCalled()

    // ⭐ I-c：下面三条语义必须在**详情渲染出来的文本**里看得见，不能只存在于组件脚本里。
    const detailText = wrapper.find('.kefu-detail').text()
    expect(detailText).toContain('接单实收款（录单填写）') // B4
    expect(detailText).toContain('结算中') // I1（逐字用后端 label，⛔ 不是「收款中」）
    expect(detailText).toContain('录单人（账号）') // M9

    // 另一档收款状态也要覆盖（否则映射表只被验到一顿）。
    await wrapper.find('.kefu-back').trigger('click')
    await flushPromises()
    api.fetchOrder.mockResolvedValue(orderFixture({ payment_status: 'SETTLED' }))
    await searchFor(wrapper, ORDER_NO)
    await wrapper.find('.kefu-result-card').trigger('click')
    await flushPromises()
    const settledText = wrapper.find('.kefu-detail').text()
    expect(settledText).toContain('已结款')
    expect(settledText).not.toContain('已收款')
    expect(settledText).not.toContain('收款中')
  })

  it('从详情返回 ⇒ 录单草稿还在（表单是 v-show，不卸载）', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    // ⭐ 用顶层稳定字段 `source_order_no`：Task 4 只把 project_text 改成 textarea，
    // 这条草稿判据不必跟着改。
    await wrapper.find('input[name="source_order_no"]').setValue('TB2026102700001')
    await searchFor(wrapper, ORDER_NO)
    await wrapper.find('.kefu-result-card').trigger('click')
    await flushPromises()
    await wrapper.find('.kefu-back').trigger('click')
    await flushPromises()
    expect(
      (wrapper.find('input[name="source_order_no"]').element as HTMLInputElement).value,
    ).toBe('TB2026102700001')
  })

  it('搜索 0 条 ⇒ 没查到订单', async () => {
    api.fetchOrders.mockResolvedValue(pageOf([]))
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, '999')
    expect(wrapper.text()).toContain('没查到订单')
  })

  it('搜索 latest-wins：旧响应回来不覆盖新结果', async () => {
    const slowFirst = deferred<ReturnType<typeof pageOf>>()
    api.fetchOrders
      .mockReturnValueOnce(slowFirst.promise)
      .mockResolvedValueOnce(pageOf([rowFixture({ project_text: '第二次结果' })]))
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, 'first')
    await searchFor(wrapper, 'second')
    expect(api.fetchOrders).toHaveBeenCalledTimes(2)
    slowFirst.resolve(pageOf([rowFixture({ project_text: '第一次结果' })]))
    await flushPromises()
    expect(wrapper.find('.kefu-result-project').text()).toBe('第二次结果')
  })

  it('没有查看详情的权限 ⇒ 不发 fetchOrder、关掉浮层、提示可见（B1）', async () => {
    const { wrapper } = mountPage(['order.list'])
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    expect(wrapper.find('.kefu-search-panel').exists()).toBe(true)
    await wrapper.find('.kefu-result-card').trigger('click')
    await flushPromises()
    expect(api.fetchOrder).not.toHaveBeenCalled()
    expect(wrapper.find('.kefu-search-panel').exists()).toBe(false)
    expect(wrapper.find('.kefu-notice').text()).toContain('没有查看订单详情的权限')
  })

  it('⭐ D-0301：卡片单号 = 展示单号（来源号非空取它）', async () => {
    api.fetchOrders.mockResolvedValue(pageOf([rowFixture({ source_order_no: 'SRC-TEST-1' })]))
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    expect(wrapper.find('.kefu-result-no').text()).toBe('SRC-TEST-1')
  })

  it('a11y：aria-expanded 跟着浮层、Esc 关、点浮层外部关', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    await searchFor(wrapper, ORDER_NO)
    expect(wrapper.find('.kefu-search-btn').attributes('aria-expanded')).toBe('true')

    await wrapper.find('.kefu-searchbar input[type="search"]').trigger('keyup.esc')
    await flushPromises()
    expect(wrapper.find('.kefu-search-panel').exists()).toBe(false)
    expect(wrapper.find('.kefu-search-btn').attributes('aria-expanded')).toBe('false')

    await searchFor(wrapper, ORDER_NO)
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await flushPromises()
    expect(wrapper.find('.kefu-search-panel').exists()).toBe(false)
  })
})

// `createOrder` 已在 Task 1 的 `api` 对象里（含 mockReset），本任务直接用 `api.createOrder`。

/** 页面里把最常用三格填好并保存。 */
async function fillRequiredAndSave(wrapper: VueWrapper): Promise<void> {
  await pickSelect(wrapper, '.kefu-game', 3)
  await flushPromises()
  await wrapper
    .find('textarea[name="project_text"], input[name="project_text"]')
    .setValue('王者荣耀/星耀上王者')
  await wrapper.find('input[name="total_price"]').setValue('88')
  await wrapper.find('.kefu-submit').trigger('click')
  await flushPromises()
}


describe('保存复位 / 失败文案', () => {
  it('保存成功 ⇒ 表单复位、留在录单页、顶部有单号', async () => {
    api.createOrder.mockResolvedValue(orderFixture({ order_no: ORDER_NO }))
    const { wrapper } = mountPage()
    await flushPromises()
    await fillRequiredAndSave(wrapper)
    expect(wrapper.find('.kefu-notice').text()).toContain(`录单成功（单号 ${ORDER_NO}）`)
    expect(
      (wrapper.find('input[name="source_order_no"]').element as HTMLInputElement).value,
    ).toBe('')
    expect(wrapper.find('.kefu-form').exists()).toBe(true)
  })

  it('1022 ⇒ 提示单号 + 到后台重试，表单复位，text 不含「列表」', async () => {
    api.createOrder.mockRejectedValue(
      new BizError(1022, `单已建好（单号 ${ORDER_NO}），指派没成功`),
    )
    const { wrapper } = mountPage()
    await flushPromises()
    await fillRequiredAndSave(wrapper)
    const notice = wrapper.find('.kefu-notice')
    expect(notice.text()).toContain('单已建好')
    expect(notice.text()).toContain('请到团团通后台重试')
    expect(wrapper.text()).not.toContain('列表')
    expect(wrapper.find('.kefu-form').exists()).toBe(true)
  })
})

describe('详情 → 二次编辑（2026-10-29 业务方需求；默认值调度侧代定、业务方过目）', () => {
  /** 搜单 → 开详情 的两步前置（编辑入口只在详情里）。 */
  async function openDetailViaSearch(wrapper: VueWrapper): Promise<void> {
    await searchFor(wrapper, ORDER_NO)
    await wrapper.find('.kefu-result-card').trigger('click')
    await flushPromises()
  }

  /** 本单号的 fetchOrder 次数（详情与编辑表单各自加载都算，用于断言「重新取数」）。 */
  function detailFetchCount(): number {
    return api.fetchOrder.mock.calls.filter(([no]) => no === ORDER_NO).length
  }

  it('详情底部有「编辑」按钮；点它 ⇒ 进编辑视图、表单拿到单号并回填、上报 order.open_edit', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    await openDetailViaSearch(wrapper)
    expect(wrapper.find('.kefu-detail-edit').exists()).toBe(true)

    await wrapper.find('.kefu-detail-edit').trigger('click')
    await flushPromises()

    // 审计与主控台对齐：进入编辑态这一刻上报（⛔ 详情加载本身不报——那是「看」不是「改」）。
    expect(reportOpenEdit).toHaveBeenCalledTimes(1)
    expect(reportOpenEdit).toHaveBeenCalledWith(ORDER_NO)

    // 编辑是**独立的第二表单实例**（录单那个 v-show 常驻实例仍然在）。
    expect(wrapper.findAll('.kefu-form')).toHaveLength(2)
    // 表单真的拿到单号：它自己发了一次 fetchOrder，并把回填报进自己的「代练项目/标题」。
    const editForm = wrapper.findAll('.kefu-form')[1]!
    expect(
      (editForm.find('textarea[name="project_text"]').element as HTMLTextAreaElement).value,
    ).toBe('王者荣耀/上分')
  })

  it('无保存节点（order.add_edit ∪ order.submit_edit 都没有）⇒ 不画「编辑」按钮（fail-closed）', async () => {
    const { wrapper } = mountPage(['order.list', 'order.view_detail', 'order.view_content'])
    await flushPromises()
    await openDetailViaSearch(wrapper)
    // 详情照常可看（fetchOrder 发过），但底部没有编辑入口。
    expect(api.fetchOrder).toHaveBeenCalledWith(ORDER_NO)
    expect(wrapper.find('.kefu-detail').exists()).toBe(true)
    expect(wrapper.find('.kefu-detail-edit').exists()).toBe(false)
  })

  it('编辑保存成功 ⇒ 回详情、详情重新取数、编辑实例卸载、页级提示带单号', async () => {
    api.updateOrderWithMessage.mockResolvedValue({ order: orderFixture(), message: '' })
    const { wrapper } = mountPage()
    await flushPromises()
    await openDetailViaSearch(wrapper)
    await wrapper.find('.kefu-detail-edit').trigger('click')
    await flushPromises()
    const fetchesBeforeSave = detailFetchCount()

    // 编辑表单已回填（game / project_text 来自 fixture）⇒ 直接点它自己的「保存」。
    await wrapper.findAll('.kefu-form')[1]!.find('.kefu-submit').trigger('click')
    await flushPromises()

    expect(api.updateOrderWithMessage).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.kefu-detail').exists()).toBe(true)
    // ⭐ 判据打在「重新取数」上：详情 v-if 重挂后 fetchOrder 比保存前又多发了。
    expect(detailFetchCount()).toBeGreaterThan(fetchesBeforeSave)
    expect(wrapper.findAll('.kefu-form')).toHaveLength(1)
    expect(wrapper.find('.kefu-notice').text()).toContain(`保存成功（单号 ${ORDER_NO}）`)
  })

  it('编辑保存的信封 msg 原样上屏（H1 同族：成功码里的「代练通未能撤回」警告不许吞）', async () => {
    api.updateOrderWithMessage.mockResolvedValue({
      order: orderFixture(),
      message: '保存成功；代练通订单未能撤回（对方已下架），请到「第三方平台订单管理」处理',
    })
    const { wrapper } = mountPage()
    await flushPromises()
    await openDetailViaSearch(wrapper)
    await wrapper.find('.kefu-detail-edit').trigger('click')
    await flushPromises()
    await wrapper.findAll('.kefu-form')[1]!.find('.kefu-submit').trigger('click')
    await flushPromises()
    expect(wrapper.find('.kefu-notice').text()).toContain('代练通订单未能撤回')
  })

  it('录单草稿在进编辑 / 退出编辑后仍在（编辑是独立实例，不碰录单那个）', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    await wrapper.find('input[name="source_order_no"]').setValue('TB2026102700001')

    await openDetailViaSearch(wrapper)
    await wrapper.find('.kefu-detail-edit').trigger('click')
    await flushPromises()
    // 编辑视图里有两个「来源单号」框（录单实例 v-show 隐藏 + 编辑实例 v-if）；
    // 录单那个的草稿必须原封不动（没被订单数据覆盖）。
    const tidInputs = wrapper.findAll('input[name="source_order_no"]')
    expect(tidInputs).toHaveLength(2)
    expect((tidInputs[0]!.element as HTMLInputElement).value).toBe('TB2026102700001')

    // 取消回详情 → 返回录单：草稿内容还在 DOM。
    await wrapper.findAll('.kefu-form')[1]!.find('.kefu-cancel').trigger('click')
    await flushPromises()
    expect(wrapper.find('.kefu-detail').exists()).toBe(true)
    await wrapper.find('.kefu-back').trigger('click')
    await flushPromises()
    expect(
      (wrapper.find('input[name="source_order_no"]').element as HTMLInputElement).value,
    ).toBe('TB2026102700001')
  })

  it('编辑「取消」⇒ 回详情并重取（不弹草稿确认——录单草稿根本没被碰过）', async () => {
    const { wrapper } = mountPage()
    await flushPromises()
    await openDetailViaSearch(wrapper)
    await wrapper.find('.kefu-detail-edit').trigger('click')
    await flushPromises()
    const fetchesBeforeCancel = detailFetchCount()

    await wrapper.findAll('.kefu-form')[1]!.find('.kefu-cancel').trigger('click')
    await flushPromises()

    expect(wrapper.find('.kefu-detail').exists()).toBe(true)
    expect(wrapper.findAll('.kefu-form')).toHaveLength(1)
    expect(detailFetchCount()).toBeGreaterThan(fetchesBeforeCancel)
    // ElMessageBox.confirm 没被触发（取消直接回详情）。
    expect(wrapper.find('.kefu-detail-edit').exists()).toBe(true)
  })
})
