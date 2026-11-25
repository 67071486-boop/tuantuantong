// @vitest-environment jsdom
import { openOptions, selectOption } from '../../components/__tests__/selectOption'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是唯一的桩边界（与 HallPage.spec 同一手法）：myOrders / config 跑真代码，
// 判据打在 mock 的 request 被调时的 url / method / params / 有没有 data 上。
// ⚠️ `http` 键必须给：`api/orders.ts` 顶层 `import { http, request }`（单号形状校验用）。
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

// PullDownRefresh(渲染 slot)、Button(disabled/click/slot)、Input(v-model)、
vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({
  push,
  currentRoute: { value: { fullPath: '/mine' } },
  options: { history: { state: {}, replace: vi.fn() } },
}) }))

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import type { MyOrderItem } from '@/api/myOrders'
import type { User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import MinePage from '../MinePage.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as { success: Mock; error: Mock }

/* ── 合成夹具（E-0077：单号从 10000000000000000001 起、游戏「测试游戏甲」） ── */

const ME_FULL: User = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
  menu_permissions: ['my_orders', 'my_orders.send_to_my_system', 'order.submit_work'],
  game_ids: [101],
  tenant_name: '测试工作室甲',
}

function myRow(overrides: Partial<MyOrderItem> = {}): MyOrderItem {
  return {
    id: 1,
    order_no: '10000000000000000001',
    source_order_no: '',
    status: 'IN_PROGRESS',
    game_name: '测试游戏甲',
    project_text: '测试项目甲',
    is_urgent: false,
    temp_amount: '12.50',
    bonus_amount: '0.00',
    review_status: null,
    created_at: '2026-10-10T08:05:00+08:00',
    username: 'ceshidashoujia',
    // ⭐ `MyOrderItem` 有接手人 / 接手价两格（桌面「指派给我的订单」用）；
    // 手机端本页不渲染它们，但类型是必填的 ⇒ 按后端形状补齐（⛔ 不改成可选，那会让漏接变成静默）。
    taker_name: '测试打手甲',
    taker_price: '12.50',
    emp_type: '打单工资',
    settle_status: '未结账',
    // ⭐ D-0365：载荷自 2026-10-23 起带申请时刻（类型必填按后端形状补；手机端不渲染）。
    settle_requested_at: null as string | null,
    ...overrides,
  }
}

/** 五档分组夹具：代练中 / 待验收 / 问题单各一，+「其余状态」两张（COMPLETED 与 REFUNDED 都落已完结）。 */
function allStatusRows(): MyOrderItem[] {
  return [
    myRow({ id: 1, order_no: '10000000000000000001', status: 'IN_PROGRESS' }),
    myRow({ id: 2, order_no: '10000000000000000002', status: 'PENDING_REVIEW', project_text: '测试项目乙' }),
    myRow({ id: 3, order_no: '10000000000000000003', status: 'ISSUE', project_text: '测试项目丙', is_urgent: true }),
    myRow({ id: 4, order_no: '10000000000000000004', status: 'COMPLETED', project_text: '测试项目丁' }),
    myRow({ id: 5, order_no: '10000000000000000005', status: 'REFUNDED', project_text: '测试项目戊' }),
  ]
}

/* ── mock 的请求路由 ── */

let myOrderItems: MyOrderItem[]
let myOrdersFail: Error | null
/** submit / report-issue 请求的实现（成功 / 失败 / 挂起，各用例自设）。 */
let submitImpl: (config: AxiosRequestConfig) => Promise<unknown>
const myOrdersCalls: Array<Record<string, unknown>> = []
const performanceCalls: Array<Record<string, unknown>> = []

let holdSales: Promise<void> | undefined
let failSecondSalesPage: boolean
let failSummaryRefresh: boolean
const overallAmounts = { temp_amount: '63.00', bonus_amount: '54.00' }
const settlementAmounts = { unsettled_bonus_amount: '-4.75', settled_bonus_amount: '58.75' }
const ownSales = {
  id: 21,
  user: 7,
  order_no: '10000000000000000021',
  dl_project: '独立绩效记录',
  game_name: '测试游戏乙',
  emp_type: '销售提成',
  order_status: 'COMPLETED',
  settle_status: '已结账',
  created_at: '2026-10-10T08:05:00+08:00',
  temp_amount: '21.00',
  bonus_amount: '18.00',
}

function wireRequestMock(): void {
  requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
    const url = String(config.url)
    if (url === '/api/business/my-orders/') {
      myOrdersCalls.push({ ...(config.params ?? {}) })
      if (myOrdersFail !== null) {
        const err = myOrdersFail
        myOrdersFail = null
        throw err
      }
      return {
        items: myOrderItems,
        total: myOrderItems.length,
        page: 1,
        page_size: Number(config.params?.page_size ?? 200),
      }
    }
    if (url === '/api/business/performances/') {
      performanceCalls.push({ ...(config.params ?? {}) })
      const main = {
        ...ownSales,
        id: 2,
        order_no: '10000000000000000002',
        emp_type: '打单工资',
      }
      const isSales = config.params?.emp_type === '销售提成'
      const includesSettlement = config.params?.include_settlement_summary === 1
      if (failSummaryRefresh && includesSettlement) throw new Error('个人绩效汇总暂不可用')
      if (isSales) {
        await holdSales
        if (failSecondSalesPage && config.params?.page === 2) throw new Error('第二页销售提成读取失败')
      }
      const items = config.params?.page_size === 1
        ? [main]
        : config.params?.emp_type === '销售提成'
          ? [ownSales, { ...ownSales, id: 22, user: 9, order_no: '10000000000000000022' }]
          : [main, ownSales]
      return {
        items,
        total: isSales && failSecondSalesPage ? 201 : items.length,
        summary: includesSettlement ? { ...overallAmounts, ...settlementAmounts } : overallAmounts,
      }
    }
    if (url.includes('/submit/') || url.includes('/report-issue/')) return submitImpl(config)
    throw new Error(`spec: 未预期的请求 ${url}`)
  })
}

/** 便捷读数：第几发 my-orders 请求的参数（恰断言用，不用 toContain 放宽）。 */
function myOrdersParams(index: number): Record<string, unknown> {
  const params = myOrdersCalls[index]
  if (!params) throw new Error(`spec: my-orders 第 ${index + 1} 发不存在（共 ${myOrdersCalls.length} 发）`)
  return params
}

/* ── 挂载与 session ── */

type SessionOverrides = Omit<Partial<User>, 'menu_permissions'> & {
  menu_permissions?: string[] | null
}

function loginSession(overrides: SessionOverrides = {}): void {
  const session = useSessionStore()
  session.status = 'ready'
  const { menu_permissions: menuOverride, ...rest } = overrides
  session.user =
    menuOverride === null
      ? null // 显式 null = 取不到 me（fail-closed 分支的夹具）
      : { ...ME_FULL, ...(menuOverride !== undefined ? { menu_permissions: menuOverride } : {}), ...rest }
}

function mountMine() {
  return mount(MinePage)
}

const cardSel = '.m-mine-card'
const submitBtnSel = '.m-mine-card__submit'
const reportBtnSel = '.m-mine-card__report'
const dialogConfirmSel = '.m-submit-dialog__confirm'
const dialogCancelSel = '.m-submit-dialog__cancel'

/** 按 data-order-no 取某张卡片；不存在即抛（不给断言静默通过的机会）。 */
function cardOf(wrapper: ReturnType<typeof mount>, orderNo: string) {
  const card = wrapper.find(`${cardSel}[data-order-no="${orderNo}"]`)
  if (!card.exists()) throw new Error(`spec: 卡片 ${orderNo} 不存在`)
  return card
}

function cardOrderNos(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll(cardSel).map((c) => c.attributes('data-order-no') ?? '')
}

function popupVisible(wrapper: ReturnType<typeof mount>): string {
  return String(wrapper.find('.m-submit-sheet.modal-in').exists())
}

/** requestMock 里 url 命中 match 的第 0 发的 config（不存在即抛——不给断言静默通过的机会）。 */
function firstCallTo(match: string): AxiosRequestConfig {
  const hit = requestMock.mock.calls.map((c) => c[0] as AxiosRequestConfig).find(
    (cfg) => String(cfg.url).includes(match),
  )
  if (hit === undefined) throw new Error(`spec: 没有命中 ${match} 的请求`)
  return hit
}

enableAutoUnmount(afterEach)

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  myOrderItems = allStatusRows()
  myOrdersFail = null
  holdSales = undefined
  failSecondSalesPage = false
  failSummaryRefresh = false
  submitImpl = async () => ({ order_no: '10000000000000000001', status: 'PENDING_REVIEW' })
  myOrdersCalls.length = 0
  performanceCalls.length = 0
  wireRequestMock()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('m 端我的订单（MinePage）', () => {
  it('刷新汇总失败时保留上一次完整的四项数值，提示重试', async () => {
    loginSession({ menu_permissions: [...ME_FULL.menu_permissions!, 'my_performance', 'order.list'] })
    const wrapper = mountMine()
    await flushPromises()
    const readAmounts = () => wrapper.get('[aria-label="绩效汇总"]').findAll('.m-perf-summary__value').map((value) => value.text())
    expect(readAmounts()).toEqual(['63.00', '54.00', '-4.75', '58.75'])

    failSummaryRefresh = true
    wrapper.getComponent({ name: 'MobilePullToRefresh' }).vm.$emit('refresh')
    await flushPromises()
    expect(wrapper.text()).toContain('个人绩效汇总暂不可用')
    expect(readAmounts()).toEqual(['63.00', '54.00', '-4.75', '58.75'])
  })

  it('在我的订单显示全部个人绩效的预算和实算，以及已完成订单的未结和已结金额', async () => {
    loginSession({ menu_permissions: [...ME_FULL.menu_permissions!, 'my_performance', 'order.list'] })
    const wrapper = mountMine()
    await flushPromises()

    const summary = wrapper.get('[aria-label="绩效汇总"]')
    expect(summary.findAll('.m-perf-summary__label').map((label) => label.text())).toEqual(['预算', '实算', '未结', '已结'])
    expect(summary.findAll('.m-perf-summary__value').map((value) => value.text())).toEqual(['63.00', '54.00', '-4.75', '58.75'])
    expect(performanceCalls).toEqual([
      { user: 7, page: 1, page_size: 1, include_settlement_summary: 1 },
      { user: 7, emp_type: '销售提成', page: 1, page_size: 200 },
    ])
  })

  it('订单和自己的绩效共用一份卡片列表、筛选和下拉刷新', async () => {
    vi.useFakeTimers()
    loginSession({ menu_permissions: [...ME_FULL.menu_permissions!, 'my_performance', 'my_performance.export'] })
    myOrderItems[1] = myRow({ id: 2, order_no: '10000000000000000002', project_text: '测试项目乙',
      status: 'PENDING_REVIEW', game_name: '测试游戏乙', game_role_name: '只在角色名里出现的字',
      temp_amount: '42.00', bonus_amount: '36.00', settle_status: '已结账' })
    const wrapper = mountMine()
    await flushPromises()

    const summary = wrapper.get('[data-testid="perf-summary"]')
    const order = cardOf(wrapper, '10000000000000000002')
    const sales = cardOf(wrapper, '10000000000000000021')
    expect(summary.text()).toContain('63.00')
    expect(summary.element.closest('#performance')).not.toBeNull()
    const searchRow = wrapper.get('[data-testid="mine-search"]')
    expect(searchRow.element.compareDocumentPosition(summary.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(summary.element.compareDocumentPosition(order.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(wrapper.text()).not.toContain('订单与绩效')
    expect(wrapper.findAll('[data-order-no="10000000000000000002"]')).toHaveLength(1)
    expect(order.text()).toContain('42.00')
    expect(order.text()).toContain('36.00')
    expect(order.text()).toContain('已结账')
    expect(sales.text()).toContain('销售提成')
    expect(sales.text()).toContain('18.00')
    expect(cardOrderNos(wrapper)).not.toContain('10000000000000000022')
    await sales.trigger('click')
    expect(push).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('绩效明细')
    expect(wrapper.findAll('input[type="search"]')).toHaveLength(1)
    const filterRow = wrapper.get('[role="group"][aria-label="游戏和结算筛选"]')
    expect(summary.element.compareDocumentPosition(filterRow.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(filterRow.find('[data-testid="mine-game-filter"]').exists()).toBe(true)
    await selectOption(wrapper, 'mine-game-filter', '测试游戏乙')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002', '10000000000000000021'])
    await searchRow.get('input').setValue('只在角色名里出现的字')
    await vi.advanceTimersByTimeAsync(300)
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002'])
    await searchRow.get('input').setValue('')
    await vi.advanceTimersByTimeAsync(300)
    const settled = filterRow.get('[data-testid="mine-settle-filter"]')
    await selectOption(wrapper, 'mine-settle-filter', '已结账')
    expect(settled.attributes('aria-label')).toBe('结算状态：已结账')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002', '10000000000000000021'])

    await wrapper.get('[data-testid="mine-search"] input').setValue('独立绩效记录')
    await vi.advanceTimersByTimeAsync(300)
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000021'])
    expect(myOrdersCalls).toHaveLength(1)

    const pull = wrapper.findAllComponents({ name: 'MobilePullToRefresh' })
    expect(pull).toHaveLength(1)
    const performanceRequestCount = performanceCalls.length
    pull[0]!.vm.$emit('refresh')
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(2)
    expect(performanceCalls).toHaveLength(performanceRequestCount * 2)
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000021'])
  })

  it('销售提成取数较慢时，先显示已返回的个人汇总和接单卡片', async () => {
    loginSession({ menu_permissions: [...ME_FULL.menu_permissions!, 'my_performance', 'my_performance.export'] })
    let finishSales!: () => void
    holdSales = new Promise((resolve) => { finishSales = resolve })
    const wrapper = mountMine()
    await flushPromises()
    expect(wrapper.get('[data-testid="perf-summary-temp"]').text()).toBe('63.00')
    expect(cardOrderNos(wrapper)).toHaveLength(5)
    finishSales()
    await flushPromises()
    expect(cardOrderNos(wrapper)).toContain('10000000000000000021')
  })

  it('订单已取完而销售提成还在加载时不报空，后续翻页失败时保留已读到的卡片', async () => {
    loginSession({ menu_permissions: [...ME_FULL.menu_permissions!, 'my_performance', 'my_performance.export'] })
    myOrderItems = []
    let finishSales!: () => void
    holdSales = new Promise((resolve) => { finishSales = resolve })
    failSecondSalesPage = true
    const wrapper = mountMine()
    await flushPromises()
    expect(wrapper.text()).toContain('正在加载绩效记录')
    expect(wrapper.text()).not.toContain('暂无符合条件的记录')
    finishSales()
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000021'])
    expect(wrapper.text()).toContain('第二页销售提成读取失败')
  })

  it.each([
    { nodes: ['my_orders', 'my_orders.send_to_my_system', 'my_performance', 'finance.payroll'], orders: true, performance: false },
    { nodes: ['my_orders', 'my_performance', 'my_performance.export'], orders: false, performance: true },
    { nodes: ['my_orders.send_to_my_system', 'my_performance.export'], orders: false, performance: false },
  ])('按各区权限分别请求和显示数据（$nodes）', async ({ nodes, orders, performance }) => {
    loginSession({ menu_permissions: nodes })
    const wrapper = mountMine()
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(orders ? 1 : 0)
    expect(wrapper.find('.m-mine-card').exists()).toBe(orders)
    expect(performanceCalls).toHaveLength(performance ? 1 : 0)
    expect(wrapper.find('[data-testid="perf-summary"]').exists()).toBe(performance)
    expect(wrapper.find('#performance').exists()).toBe(performance)
  })

  it('取数：一次全量（fetchAllMyOrders → 恰好一发 /my-orders/，page=1 & page_size=200）', async () => {
    loginSession()
    mountMine()
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(1)
    expect(myOrdersParams(0)).toEqual({ page: 1, page_size: 200 })
  })

  it('卡片字段：单号 / 游戏名 / 项目 / 状态徽标 / 加急标 / 接单金额字符串原样 / 接手时间（按串内偏移换算）', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()

    const card = cardOf(wrapper, '10000000000000000001')
    expect(card.find('.m-mine-card__game').text()).toBe('测试游戏甲')
    expect(card.find('.m-mine-card__project').text()).toBe('测试项目甲')
    expect(card.find('.m-mine-card__status').text()).toBe('代练中')
    expect(card.text()).toContain('单号 10000000000000000001')
    expect(card.find('.m-mine-card__urgent').exists()).toBe(false)
    // 金额是**字符串**（DRF DecimalField）⇒ 原样显示，不转数字、不补零。
    expect(card.find('.m-mine-card__amount').text()).toBe('12.50 元')
    // created_at 是 +08:00 串 ⇒ 墙钟原样（不按浏览器时区平移）。
    expect(card.find('.m-mine-card__time').text()).toBe('接手 2026-10-10 08:05')

    // ⭐ D-0301：来源号非空 ⇒ 单号行逐字显示来源号（空 ⇒ 回退内置号，
    // 即上面那条既有断言）。
    myOrderItems = [myRow({ id: 21, order_no: '10000000000000000021', source_order_no: 'SRC-TEST-1' })]
    const wrapper2 = mountMine()
    await flushPromises()
    expect(cardOf(wrapper2, '10000000000000000021').text()).toContain('单号 SRC-TEST-1')

    const urgent = cardOf(wrapper, '10000000000000000003')
    expect(urgent.find('.m-mine-card__urgent').text()).toBe('加急')
  })

  it('取数失败：toast msg 原样 + 错误文案 + 重试按钮；重试成功后卡片出现', async () => {
    loginSession()
    myOrdersFail = new HttpError(403, '您没有该功能的访问权限')
    const wrapper = mountMine()
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('您没有该功能的访问权限')
    expect(wrapper.text()).toContain('您没有该功能的访问权限')
    expect(wrapper.findAll(cardSel)).toHaveLength(0)

    await wrapper.find('.m-mine__retry-btn').trigger('click')
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(2)
    expect(wrapper.findAll(cardSel)).toHaveLength(5)
    expect(wrapper.text()).not.toContain('您没有该功能的访问权限')
  })

  it('空列表：显示「暂无订单」', async () => {
    loginSession()
    myOrderItems = []
    const wrapper = mountMine()
    await flushPromises()
    expect(wrapper.get('.m-mine__end[role="status"]').text()).toBe('暂无订单')
    expect(wrapper.findAll(cardSel)).toHaveLength(0)
  })

  it('分组：五个 tab 各自正确；COMPLETED 与 REFUNDED（其余状态）都落「已完结」且徽标仍显原状态', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual([
      '10000000000000000001',
      '10000000000000000002',
      '10000000000000000003',
      '10000000000000000004',
      '10000000000000000005',
    ])

    const switchTab = async (value: string) => {
      await wrapper.find(`.m-mine-segments button[data-value="${value}"]`).trigger('click')
      await flushPromises()
    }

    await switchTab('IN_PROGRESS')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000001'])
    await switchTab('PENDING_REVIEW')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002'])
    await switchTab('ISSUE')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000003'])
    // 「已完结」= 其余全部状态：COMPLETED 与 REFUNDED 都在，且卡片仍显示**原状态**徽标（不合并文案）。
    await switchTab('done')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000004', '10000000000000000005'])
    expect(cardOf(wrapper, '10000000000000000004').find('.m-mine-card__status').text()).toBe('已完成')
    expect(cardOf(wrapper, '10000000000000000005').find('.m-mine-card__status').text()).toBe('已退单')
    // 回「全部」：五张都在。
    await switchTab('all')
    expect(cardOrderNos(wrapper)).toHaveLength(5)
    // 分组是**前端**过滤：切 tab 不发新请求。
    expect(myOrdersCalls).toHaveLength(1)
  })

  it('交单两键恰好断言：仅代练中的卡片有、且成对出现；无 order.submit_work / 取不到 me ⇒ 一颗都不渲染', async () => {
    loginSession()
    let wrapper = mountMine()
    await flushPromises()
    expect(wrapper.findAll(submitBtnSel)).toHaveLength(1)
    expect(wrapper.findAll(reportBtnSel)).toHaveLength(1)
    // 两键都长在代练中那张卡片上（其他状态一张都没有）。
    expect(cardOf(wrapper, '10000000000000000001').find(submitBtnSel).exists()).toBe(true)
    expect(cardOf(wrapper, '10000000000000000002').find(submitBtnSel).exists()).toBe(false)
    wrapper.unmount()

    loginSession({ menu_permissions: ['my_orders', 'my_orders.send_to_my_system'] })
    wrapper = mountMine()
    await flushPromises()
    expect(wrapper.findAll(submitBtnSel)).toHaveLength(0)
    expect(wrapper.findAll(reportBtnSel)).toHaveLength(0)
    wrapper.unmount()

    loginSession({ menu_permissions: null })
    wrapper = mountMine()
    await flushPromises()
    expect(wrapper.findAll(submitBtnSel)).toHaveLength(0)
    expect(wrapper.findAll(reportBtnSel)).toHaveLength(0)
  })

  it('点卡片 → 进详情（router.push 到 m-order-detail，参数取行内单号）；点两键不触发导航（.stop）', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()

    await cardOf(wrapper, '10000000000000000002').trigger('click')
    expect(push).toHaveBeenCalledWith({
      name: 'm-order-detail',
      params: { orderNo: '10000000000000000002' },
    })

    push.mockClear()
    await cardOf(wrapper, '10000000000000000003').get('button.m-mine-card__project').trigger('click')
    expect(push).toHaveBeenCalledExactlyOnceWith({
      name: 'm-order-detail',
      params: { orderNo: '10000000000000000003' },
    })

    // 「报问题」键开了弹层（下面有专门用例），但**不**触发进详情。
    push.mockClear()
    await cardOf(wrapper, '10000000000000000001').find(reportBtnSel).trigger('click')
    expect(push).not.toHaveBeenCalled()
    expect(popupVisible(wrapper)).toBe('true')
  })

  it('提交完成 → 弹层默认勾待验收；确认 POST /submit/ 无 data；成功后卡片状态从**响应载荷**就地更新且不重取列表', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()

    await cardOf(wrapper, '10000000000000000001').find(submitBtnSel).trigger('click')
    expect(popupVisible(wrapper)).toBe('true')
    expect(
      (wrapper.find('input[value="PENDING_REVIEW"]').element as HTMLInputElement).checked,
    ).toBe(true)

    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    const cfg = firstCallTo('/submit/')
    expect(cfg).toMatchObject({
      method: 'post',
      url: '/api/business/orders/10000000000000000001/submit/',
    })
    expect('data' in cfg).toBe(false)

    // 卡片状态 = **响应载荷里的 status**（mock 回 PENDING_REVIEW）⇒ 徽标变待验收、两键消失。
    expect(cardOf(wrapper, '10000000000000000001').find('.m-mine-card__status').text()).toBe('待验收')
    expect(cardOf(wrapper, '10000000000000000001').find(submitBtnSel).exists()).toBe(false)
    // 恰好一发提交 + ⛔ 不重取全量：my-orders 仍只有首屏那一发。
    expect(
      requestMock.mock.calls.filter((c) => String((c[0] as AxiosRequestConfig).url).includes('/submit/')),
    ).toHaveLength(1)
    expect(myOrdersCalls).toHaveLength(1)
    expect(toast.success).toHaveBeenCalledWith('提交成功')
    expect(popupVisible(wrapper)).toBe('false')
  })

  it('报问题 → 弹层预勾问题档，填写原因后提交，徽标变问题单', async () => {
    loginSession()
    // 真实 /report-issue/ 成功回的是流转后的 ISSUE 订单（就地更新以**载荷**为准）。
    submitImpl = async () => ({ order_no: '10000000000000000001', status: 'ISSUE' })
    const wrapper = mountMine()
    await flushPromises()

    await cardOf(wrapper, '10000000000000000001').find(reportBtnSel).trigger('click')
    expect(popupVisible(wrapper)).toBe('true')
    expect((wrapper.find('input[value="ISSUE"]').element as HTMLInputElement).checked).toBe(true)

    await wrapper.get('textarea[aria-label="说明原因"]').setValue('账号掉线')
    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    const cfg = firstCallTo('/report-issue/')
    expect(cfg).toMatchObject({
      method: 'post',
      url: '/api/business/orders/10000000000000000001/report-issue/',
      data: { reason: '账号掉线' },
    })

    expect(cardOf(wrapper, '10000000000000000001').find('.m-mine-card__status').text()).toBe('问题单')
    expect(cardOf(wrapper, '10000000000000000001').find(reportBtnSel).exists()).toBe(false)
    expect(
      requestMock.mock.calls.filter((c) =>
        String((c[0] as AxiosRequestConfig).url).includes('/report-issue/'),
      ),
    ).toHaveLength(1)
    expect(myOrdersCalls).toHaveLength(1)
    expect(toast.success).toHaveBeenCalledWith('已报问题单')
  })

  it('提交失败（1002）：toast = msg 原样；卡片不变；弹层留着；成功路径零调用', async () => {
    loginSession()
    submitImpl = async () => {
      throw new BizError(1002, '只有代练中的单可以提交，当前状态：PENDING_REVIEW')
    }
    const wrapper = mountMine()
    await flushPromises()

    await cardOf(wrapper, '10000000000000000001').find(submitBtnSel).trigger('click')
    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('只有代练中的单可以提交，当前状态：PENDING_REVIEW')
    expect(toast.success).not.toHaveBeenCalled()
    expect(cardOf(wrapper, '10000000000000000001').find('.m-mine-card__status').text()).toBe('代练中')
    expect(cardOf(wrapper, '10000000000000000001').find(submitBtnSel).exists()).toBe(true)
    expect(popupVisible(wrapper)).toBe('true')
    expect(myOrdersCalls).toHaveLength(1) // 失败也不重取
  })

  it('弹层在途闸：确认在途禁点 / 禁取消；结束后卡片就地更新', async () => {
    loginSession()
    let resolveSubmit: (v: unknown) => void = () => {}
    submitImpl = () => new Promise((res) => { resolveSubmit = res })
    const wrapper = mountMine()
    await flushPromises()

    await cardOf(wrapper, '10000000000000000001').find(submitBtnSel).trigger('click')
    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()
    expect(wrapper.find(dialogConfirmSel).attributes('disabled')).toBeDefined()
    expect(wrapper.find(dialogCancelSel).attributes('disabled')).toBeDefined()

    // 在途再点确认 / 取消：不发第二条、不关弹层。
    await wrapper.find(dialogConfirmSel).trigger('click')
    await wrapper.find(dialogCancelSel).trigger('click')
    const submitCalls = requestMock.mock.calls.filter(
      (c) => String((c[0] as AxiosRequestConfig).url).includes('/submit/'),
    )
    expect(submitCalls).toHaveLength(1)
    expect(popupVisible(wrapper)).toBe('true')

    resolveSubmit({ order_no: '10000000000000000001', status: 'PENDING_REVIEW' })
    await flushPromises()
    expect(wrapper.find(dialogConfirmSel).attributes('disabled')).toBeUndefined()
    expect(cardOf(wrapper, '10000000000000000001').find('.m-mine-card__status').text()).toBe('待验收')
    expect(popupVisible(wrapper)).toBe('false')
  })

  it('下拉刷新 = 重取全量；取数失败时 toast 且已加载内容保留', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(1)

    wrapper.getComponent({ name: 'MobilePullToRefresh' }).vm.$emit('refresh')
    await flushPromises()
    expect(myOrdersCalls).toHaveLength(2)
    expect(myOrdersParams(1)).toEqual({ page: 1, page_size: 200 })
    expect(wrapper.findAll(cardSel)).toHaveLength(5)

    myOrdersFail = new BizError(1002, '刷新失败')
    wrapper.getComponent({ name: 'MobilePullToRefresh' }).vm.$emit('refresh')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('刷新失败')
    // 失败不清空已加载内容。
    expect(wrapper.findAll(cardSel)).toHaveLength(5)
    expect(wrapper.text()).toContain('刷新失败')
  })
})

/* ── 真机反馈第一批：卡片轮廓 / 搜索 / 游戏筛 / 分状态计数（全部本地，零新请求） ── */

describe('m 端我的订单 — 搜索 / 游戏筛 / 计数（真机反馈第一批）', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('订单使用 Framework7 的 iOS 描边卡片和有分隔线的卡片底部', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()
    const cards = wrapper.findAll(cardSel)
    expect(cards).toHaveLength(myOrderItems.length)
    expect(cards.length).toBeGreaterThan(0)
    for (const card of cards) {
      expect(card.classes()).toContain('card-outline-ios')
      expect(card.find('.card-footer').exists()).toBe(true)
      expect(card.classes()).toContain('card-footer-divider')
    }
  })

  it('搜索：300ms 防抖后生效，单号 / 项目 / 游戏名任一「包含」即命中（不区分大小写）', async () => {
    loginSession()
    myOrderItems = [
      ...allStatusRows(),
      // 带拉丁字母的游戏名（大小写判据要有真对立面：中文没有大小写，测它等于没测）。
      myRow({ id: 9, order_no: '10000000000000000009', status: 'COMPLETED', game_name: 'synthGameZ' }),
    ]
    vi.useFakeTimers()
    const wrapper = mountMine()
    await flushPromises()
    const input = wrapper.find('[data-testid="mine-search"] input')

    // 防抖窗口内：列表不变（6 张全在）。
    ;(input.element as HTMLInputElement).value = '项目乙'
    await input.trigger('input')
    expect(wrapper.findAll(cardSel)).toHaveLength(6)
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002'])

    // 游戏名命中，**不区分大小写**（小写夹具、大写输入）。
    ;(input.element as HTMLInputElement).value = 'SYNTHGAMEZ'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000009'])

    // 单号片段命中。
    ;(input.element as HTMLInputElement).value = '0000000000000000003'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000003'])



    // 清空：全量回来。筛选全程不发新请求（本地筛）。
    ;(input.element as HTMLInputElement).value = ''
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(6)
    expect(myOrdersCalls).toHaveLength(1)
  })

  it('⭐ D-0301（2026-10-24）搜索：来源号能搜到、系统号仍能搜到（单号搜索两个号都认）', async () => {
    loginSession()
    myOrderItems = [
      myRow({ id: 11, order_no: '10000000000000000011', source_order_no: 'SRC-TEST-1' }),
    ]
    vi.useFakeTimers()
    const wrapper = mountMine()
    await flushPromises()
    const input = wrapper.find('[data-testid="mine-search"] input')

    // 来源号片段命中。
    ;(input.element as HTMLInputElement).value = 'SRC-TEST'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000011'])

    // 系统内置号照旧命中（双向之二）。
    ;(input.element as HTMLInputElement).value = '0000000000000000011'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000011'])

    // 不相干的片段 ⇒ 空（阴性对照：筛选真的在筛）。
    ;(input.element as HTMLInputElement).value = '999'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(cardOrderNos(wrapper)).toEqual([])
    vi.useRealTimers()
  })

  it('游戏按钮旁打开浮层，从已有订单里的游戏或全部游戏中选择后立即筛选', async () => {
    loginSession()
    myOrderItems = [
      myRow({ id: 1, status: 'IN_PROGRESS', game_name: '测试游戏甲' }),
      myRow({ id: 2, status: 'IN_PROGRESS', game_name: '测试游戏甲', order_no: '10000000000000000002' }),
      myRow({ id: 3, status: 'PENDING_REVIEW', game_name: '测试游戏乙', order_no: '10000000000000000003' }),
    ]
    const wrapper = mountMine()
    await flushPromises()
    const dialog = await openOptions(wrapper, 'mine-game-filter')
    expect(dialog.element.closest('.popover')).not.toBeNull()
    const options = dialog.findAll('button[data-option-value]')
    await dialog.get('.m-select__close').trigger('click')
    // 哨兵 + distinct（两个「测试游戏甲」只出一次；⛔ 不调接口）。
    expect(options.map((option) => option.get('span').text())).toEqual(['全部游戏', '测试游戏甲', '测试游戏乙'])
    expect(myOrdersCalls).toHaveLength(1)

    await selectOption(wrapper, 'mine-game-filter', '测试游戏乙')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000003'])

    await selectOption(wrapper, 'mine-game-filter', '')
    expect(wrapper.findAll(cardSel)).toHaveLength(3)
    expect(myOrdersCalls).toHaveLength(1)
  })

  it('分状态计数：五个 tab 各带计数 = 先过搜索+游戏筛之后的基数再分组（筛选与 tab 叠加）', async () => {
    loginSession()
    myOrderItems = allStatusRows().map((row) =>
      row.status === 'ISSUE' ? { ...row, game_name: '测试游戏乙' } : row,
    )
    const wrapper = mountMine()
    await flushPromises()
    const tabText = (value: string): string =>
      wrapper.find(`.m-mine-segments button[data-value="${value}"]`).text()
    // 夹具：代练中 1 / 待验收 1 / 问题单 1 / 已完结 2（COMPLETED+REFUNDED）。
    expect(tabText('all')).toBe('全部 (5)')
    expect(tabText('IN_PROGRESS')).toBe('代练中 (1)')
    expect(tabText('PENDING_REVIEW')).toBe('待验收 (1)')
    expect(tabText('ISSUE')).toBe('问题单 (1)')
    expect(tabText('done')).toBe('已完结 (2)')

    // 搜索先筛 ⇒ 计数跟着缩：搜「项目乙」后基数只剩待验收那一张。
    vi.useFakeTimers()
    const input = wrapper.find('[data-testid="mine-search"] input')
    ;(input.element as HTMLInputElement).value = '项目乙'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(tabText('all')).toBe('全部 (1)')
    expect(tabText('PENDING_REVIEW')).toBe('待验收 (1)')
    expect(tabText('IN_PROGRESS')).toBe('代练中 (0)')
    expect(tabText('done')).toBe('已完结 (0)')
    vi.useRealTimers()

    // 游戏筛**叠加**在搜索之上：夹具里「项目乙」那张卡的游戏是「测试游戏甲」
    // ⇒ 搜「项目乙」+ 筛「测试游戏甲」仍是它；换成没有行的「测试游戏乙」⇒ 全部归零。
    await selectOption(wrapper, 'mine-game-filter', '测试游戏甲')
    expect(tabText('all')).toBe('全部 (1)')
    expect(tabText('PENDING_REVIEW')).toBe('待验收 (1)')
    expect(tabText('ISSUE')).toBe('问题单 (0)')
    await selectOption(wrapper, 'mine-game-filter', '测试游戏乙')
    expect(tabText('all')).toBe('全部 (0)')
    expect(tabText('PENDING_REVIEW')).toBe('待验收 (0)')
  })

  it('筛选 + tab 叠加：搜索后切「已完结」，看到的 = 筛后基数里落已完结的行', async () => {
    loginSession()
    vi.useFakeTimers()
    const wrapper = mountMine()
    await flushPromises()
    // 搜「0000000000000000004」（COMPLETED，落已完结档）。
    const input = wrapper.find('[data-testid="mine-search"] input')
    ;(input.element as HTMLInputElement).value = '0000000000000000004'
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    await wrapper.find('.m-mine-segments button[data-value="done"]').trigger('click')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000004'])
    vi.useRealTimers()
  })
})

/* ── 真机反馈第二批：计数大时五个状态 tab 互相覆盖 → 横向滚动（space-evenly=false） ── */

describe('m 端我的订单 — 状态标签计数溢出（真机反馈第二批）', () => {
  /** 大数字合成夹具（业务方截图实报的量级）：1975 = 1150 + 170 + 25 + 630。 */
  function bigCountRows(): MyOrderItem[] {
    const rows: MyOrderItem[] = []
    const make = (n: number, status: string, offset: number): void => {
      for (let i = 0; i < n; i += 1) {
        rows.push(myRow({
          id: offset + i,
          order_no: String(10000000000000000000n + BigInt(offset + i)),
          status,
        }))
      }
    }
    make(1150, 'IN_PROGRESS', 1)
    make(170, 'PENDING_REVIEW', 1151)
    make(25, 'ISSUE', 1321)
    make(630, 'COMPLETED', 1346)
    return rows
  }

  it('状态筛选使用 Framework7 圆按钮，当前档随点击改变', async () => {
    loginSession()
    const wrapper = mountMine()
    await flushPromises()
    const group = wrapper.get('[role="group"][aria-label="订单状态"]')
    expect(group.findAll('button.button-round')).toHaveLength(5)
    expect(group.get('[aria-pressed="true"]').text()).toBe('全部 (5)')
    await group.get('[data-value="PENDING_REVIEW"]').trigger('click')
    expect(group.findAll('[aria-pressed="true"]')).toHaveLength(1)
    expect(group.get('[aria-pressed="true"]').text()).toBe('待验收 (1)')
    expect(cardOrderNos(wrapper)).toEqual(['10000000000000000002'])
  })

  it('数字很大（1975/1150/170/…）时五个 tab 文案仍**完整**渲染', async () => {
    loginSession()
    myOrderItems = bigCountRows()
    // 这条用例只检查筛选计数，卡片的真实渲染由小数据集用例覆盖。
    const wrapper = mount(MinePage, { global: { stubs: { 'f7-card': true } } })
    await flushPromises()

    // 五个 label 文案完整（计数是三位/四位数也一字不缺）。
    const labels = wrapper.findAll('.m-mine-segments button').map((w) => w.text())
    expect(labels).toEqual([
      '全部 (1975)',
      '代练中 (1150)',
      '待验收 (170)',
      '问题单 (25)',
      '已完结 (630)',
    ])
    // 计数仍随筛选联动（抽一档：搜索单号片段后「全部」计数收缩）。
    vi.useFakeTimers()
    const input = wrapper.find('[data-testid="mine-search"] input')
    ;(input.element as HTMLInputElement).value = String(10000000000000000000n + 1n)
    await input.trigger('input')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(wrapper.findAll('.m-mine-segments button').map((w) => w.text())[0]).toBe('全部 (1)')
  })

  it('数量较大的状态标签仍按自然宽度排列并可横向滚动', async () => {
    const source = (await import('../MinePage.vue?raw')).default
    expect(source).toMatch(/\.m-mine-segments\s*\{[^}]*overflow-x: auto;/)
    expect(source).toMatch(/\.m-mine-segments :deep\(\.button\)\s*\{[^}]*white-space: nowrap;/)
  })
})
