// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
/** 后台首页 v2（管理员经营看板，V2Dashboard，接真数据）的判据：① charts 判据——环形
 * Metabase 复刻规格（8 色色板 / 名字哈希恒同色 / 「其他」<2.5% 候选 ≥2 才并 + >8 段前 7 硬顶 /
 * HTML 中心覆盖层与悬停切换 / 淡 30% 联动 / 环几何 / <0.5% 渲染值撑底 / 环外标签开关与浮层
 * 容器兜底）与 createBarLineCombo（双轴柱线形态 / 悬停卡 formatter / 金额刻度）；② 组件级——
 * KPI×6 钉值（home/overview **双窗口**）与占位标归零、区间五处联动（参数逐字）、竞态
 * latest-wins、状态环合并「其他」+ 小字行、图例悬停联动与点击显隐、排行榜弹窗四格、单卡降级
 * （含 KPI / 来源）、趋势 30 天补零、大厅三源取数、同步健康四渠道（闲管家「—」+ 口径待拍板）、
 * 待结算金额取 summary 生效绩效槽、App T+1 文案、代练通三源取数、非管理员零看板请求。
 * 全部夹具为合成值。 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toLocalIso } from '@/api/orders'

/* ── 假 ECharts 命名空间（真控制器 + 假实例：抓 setOption/on/dispatchAction）──────── */

type FakeInstance = {
  el: HTMLElement
  setOption: ReturnType<typeof vi.fn>
  on: ReturnType<typeof vi.fn>
  handlers: Map<string, (params: unknown) => void>
  dispatchAction: ReturnType<typeof vi.fn>
  resize: ReturnType<typeof vi.fn>
  dispose: ReturnType<typeof vi.fn>
}

const instances: FakeInstance[] = []
const fakeNamespace = {
  init: (el: HTMLElement): FakeInstance => {
    const instance: FakeInstance = {
      el,
      setOption: vi.fn(),
      handlers: new Map<string, (params: unknown) => void>(),
      on: vi.fn((name: string, handler: (params: unknown) => void) => {
        instance.handlers.set(name, handler)
      }),
      dispatchAction: vi.fn(),
      resize: vi.fn(),
      dispose: vi.fn(),
    }
    instances.push(instance)
    return instance
  },
}

function handlerOf(instance: FakeInstance, name: string): (params: unknown) => void {
  const handler = instance.handlers.get(name)
  expect(handler, `实例没有注册 ${name} 回调`).toBeDefined()
  return handler!
}

function lastSetOption(instance: FakeInstance): unknown {
  const calls = instance.setOption.mock.calls
  expect(calls.length, '实例还没有收到任何 setOption').toBeGreaterThan(0)
  return calls[calls.length - 1]![0]
}

/** 中心覆盖层（HTML，不用 echarts title）的两行读数。 */
function centerTextOf(instance: FakeInstance): { value: string; label: string } {
  const value = instance.el.querySelector<HTMLElement>('.tt2-donut-center-value')
  const label = instance.el.querySelector<HTMLElement>('.tt2-donut-center-label')
  expect(value, '中心数值覆盖层没建').not.toBeNull()
  expect(label, '中心标签覆盖层没建').not.toBeNull()
  return { value: value!.textContent ?? '', label: label!.textContent ?? '' }
}

function sliceOpacities(instance: FakeInstance): number[] {
  const option = lastSetOption(instance) as { series: { data: { itemStyle: { opacity: number } }[] }[] }
  return option.series[0]!.data.map((item) => item.itemStyle.opacity)
}

/** 嵌套形态（applySliceColors 开启的页）的淡化读数：vendored 3.6.2 只认 itemStyle.normal。 */
function nestedSliceOpacities(instance: FakeInstance): number[] {
  const option = lastSetOption(instance) as {
    series: { data: { itemStyle: { normal: { opacity: number } } }[] }[]
  }
  return option.series[0]!.data.map((item) => item.itemStyle.normal.opacity)
}

/** 嵌套形态下每片的 (名, 实际上盘色)。 */
function nestedSliceColors(instance: FakeInstance): { name: string; color: string }[] {
  const option = lastSetOption(instance) as {
    series: { data: { name: string; itemStyle: { normal: { color: string } } }[] }[]
  }
  return option.series[0]!.data.map((item) => ({ name: item.name, color: item.itemStyle.normal.color }))
}

function instanceFor(wrapper: VueWrapper, anchor: string): FakeInstance {
  const el = wrapper.find(`[data-v2="${anchor}"]`).element
  const found = instances.find((instance) => instance.el === el)
  expect(found, `${anchor} 没有对应的图表实例`).toBeDefined()
  return found!
}

/* ── API mock（部分 mock：charts 的真控制器/真 builder 保留）────────────────────── */

vi.mock('@/v2/charts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/v2/charts')>()),
  loadECharts: () => Promise.resolve(fakeNamespace),
}))

const fetchOrdersWithSummary = vi.fn()
vi.mock('@/v2/orderManageApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/v2/orderManageApi')>()),
  fetchOrdersWithSummary: (...args: unknown[]) => fetchOrdersWithSummary(...args),
}))

const fetchSupportStats = vi.fn()
vi.mock('@/api/supportStats', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/supportStats')>()),
  fetchSupportStats: (...args: unknown[]) => fetchSupportStats(...args),
}))

const fetchAppStatsOverview = vi.fn()
vi.mock('@/api/appStats', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/appStats')>()),
  fetchAppStatsOverview: (...args: unknown[]) => fetchAppStatsOverview(...args),
}))

const fetchPlatformIntegrationSummary = vi.fn()
vi.mock('@/api/platformIntegrations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/platformIntegrations')>()),
  fetchPlatformIntegrationSummary: (...args: unknown[]) => fetchPlatformIntegrationSummary(...args),
}))

const fetchDailiantongStats = vi.fn()
vi.mock('@/api/dailiantongExtra', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/dailiantongExtra')>()),
  fetchDailiantongStats: (...args: unknown[]) => fetchDailiantongStats(...args),
}))

const httpRequest = vi.fn()
vi.mock('@/api/http', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/http')>()),
  request: (...args: unknown[]) => httpRequest(...args),
}))

const fetchNotifications = vi.fn()
const markNotificationRead = vi.fn()
vi.mock('@/api/notifications', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/notifications')>()),
  fetchNotifications: (...args: unknown[]) => fetchNotifications(...args),
  markNotificationRead: (...args: unknown[]) => markNotificationRead(...args),
}))

const fetchSystemNotices = vi.fn()
vi.mock('@/api/systemNotices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/systemNotices')>()),
  fetchSystemNotices: (...args: unknown[]) => fetchSystemNotices(...args),
}))

vi.mock('@/v2/V2RoleDashboard.vue', () => ({ default: { props: ['layout'], template: '<div data-role-stub>{{ layout }}</div>' } }))

import V2Dashboard from '../V2Dashboard.vue'
import { useSessionStore } from '@/stores/session'
import {
  buildDonutData,
  COMBO_BAR_COLOR,
  COMBO_LINE_COLOR,
  DONUT_OTHER_COLOR,
  DONUT_PALETTE,
  paletteIndexForName,
  STATUS_RING_COLORS,
  type EChartsNamespace,
} from '@/v2/charts'
import { V2_ORDER_STATUS_LABELS } from '@/config/orderStatus'

/** 假命名空间按真类型上机（vi.fn 的调用签名在断言里看，类型这里收口一次）。 */
const fakeEcharts = fakeNamespace as unknown as EChartsNamespace

/* ── 合成夹具（PII 纪律：全部合成值）───────────────────────────────────────── */

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function isoDay(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

const NOW = new Date()
const TODAY = isoDay(NOW)
const D30_START = isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 29))

/** 订单 summary 页（带窗口/无窗口两用）。 */
function pageWith(counts: Record<string, number>): Record<string, unknown> {
  return {
    items: [],
    total: 0,
    page: 1,
    page_size: 1,
    summary: {
      count: Object.values(counts).reduce((a, b) => a + b, 0),
      total_hall_price: '0.00',
      total_price: '0.00',
      diff: '0.00',
      total_refund_fee: '0.00',
      refunded_diff: '0.00',
      status_counts: counts,
    },
  }
}

const STATUS_TODAY = {
  COMPLETED: 11, IN_PROGRESS: 3, PENDING_REVIEW: 2, WAITING_GRAB: 1, UNASSIGNED: 1,
  ISSUE: 0, PAUSED: 0, REFUNDED: 0,
}
const STATUS_D30 = {
  COMPLETED: 388, IN_PROGRESS: 44, PENDING_REVIEW: 21, WAITING_GRAB: 8, UNASSIGNED: 12,
  ISSUE: 5, PAUSED: 4, REFUNDED: 4,
}
/** 当前在库全集（无窗口调用）：在途 = 96+41+22+18 = 177。 */
const CURRENT_COUNTS = {
  COMPLETED: 500, IN_PROGRESS: 96, PENDING_REVIEW: 41, WAITING_GRAB: 22, UNASSIGNED: 18,
  ISSUE: 9, PAUSED: 3, REFUNDED: 20,
}

const BOARD_ROWS = [
  { idx: 1, uid: 101, userName: '测试甲', orderCount: 21, noSettAmount: 1240, complateRate: 96, createUserDate: '2026-01-01', lastRecOrderDate: '2026-11-06 09:41' },
  { idx: 2, uid: 102, userName: '测试乙', orderCount: 13, noSettAmount: 980, complateRate: 95, createUserDate: '2026-01-02', lastRecOrderDate: '2026-11-05 22:30' },
]

/** home/overview 的**恰好键集**（console_views.home_overview_data 的返回形状）。 */
function overview(
  fields: {
    add_count: number
    order_price: string
    profit_amount: string
    finish_order_rate: string
    refund_order_rate: string
  },
  start: string,
  end: string,
): Record<string, unknown> {
  return {
    start,
    end,
    add_count: fields.add_count,
    order_price: fields.order_price,
    hall_price_total: '0.00',
    profit_amount: fields.profit_amount,
    profit_rate: '0.0',
    rec_order_count: 0,
    rec_order_rate: '0.0',
    cancel_count: 0,
    cancel_order_rate: '0.0',
    refund_count: 0,
    refund_order_rate: fields.refund_order_rate,
    finish_count: 0,
    finish_order_rate: fields.finish_order_rate,
  }
}

/** overview 窗口（start 逐字）：今天 / 昨天（today 的等长前移窗）/ 近 30 天 / 30 天前移窗。 */
const YESTERDAY = isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 1))
const D30_PREV_START = isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 59))
const D30_PREV_END = isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 30))

const OVERVIEW_TODAY = overview({ add_count: 18, order_price: '4760.00', profit_amount: '1190.00', finish_order_rate: '94.1', refund_order_rate: '1.8' }, TODAY, TODAY)
const OVERVIEW_YESTERDAY = overview({ add_count: 16, order_price: '4364.00', profit_amount: '1118.00', finish_order_rate: '92.9', refund_order_rate: '2.1' }, YESTERDAY, YESTERDAY)
const OVERVIEW_D30 = overview({ add_count: 486, order_price: '128460.00', profit_amount: '32180.00', finish_order_rate: '92.4', refund_order_rate: '2.1' }, D30_START, TODAY)
const OVERVIEW_D30_PREV = overview({ add_count: 432, order_price: '118720.00', profit_amount: '30520.00', finish_order_rate: '91.3', refund_order_rate: '2.5' }, D30_PREV_START, D30_PREV_END)

/** dashboard/trend：30 天全有数据（count 逐日 +1，amount = count×100 的两位小数字符串）。 */
const TREND_DAYS = Array.from({ length: 30 }, (_, i) => ({
  date: isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 29 + i)),
  count: i + 1,
  amount: `${(i + 1) * 100}.00`,
}))

const GAME_ROWS = [
  { name: '游戏甲', count: 21 },
  { name: '游戏乙', count: 13 },
  { name: '游戏丙', count: 5 },
]

/** dashboard/hall-stay（分钟；UI 显示在厅数用的是 hall 列表 total，不是这里的 in_hall_count）。 */
const HALL_STAY = {
  in_hall_count: 12,
  avg_stay_minutes: 134,
  max_stay_minutes: 362,
  today_fastest_grab_minutes: 7,
  today_slowest_grab_minutes: 205,
}

const HALL_PAGE = { items: [], total: 12, page: 1, page_size: 1 }

/** dashboard/sync-health 四渠道（后端顺序：鸡蛋铺 / 下家互推 / 代练通 / 闲管家）。 */
const FIVE_MIN_AGO = new Date(Date.now() - 5 * 60_000).toISOString()
const SYNC_HEALTH = {
  channels: [
    { name: '鸡蛋铺', pending: 12, failed: 0, last_time: FIVE_MIN_AGO },
    { name: '下家互推', pending: 0, failed: 2, last_time: null },
    { name: '代练通', pending: 0, failed: 0, last_time: FIVE_MIN_AGO },
    { name: '闲管家', pending: null, failed: null, last_time: null },
  ],
}

/** performances summary（生效绩效槽 = bonus_amount；待结算金额读它）。 */
const PERFORMANCES_PAGE = {
  items: [],
  total: 0,
  page: 1,
  page_size: 1,
  summary: {
    temp_amount: '100.00',
    bonus_amount: '8123.45',
    order_price: '0.00',
    refund_fee: '0.00',
    profit_expected: null,
    profit_actual: null,
  },
}

const SUPPORT_OPERATORS = [
  { name: '客服甲', order_count: 5, total_price: '1620.00', total_hall_price: '1000.00', performance: '620.00' },
  { name: '客服乙', order_count: 4, total_price: '1300.00', total_hall_price: '900.00', performance: '400.00' },
]

/** 来源环吃同一份载荷的 sources[]（店铺级来源字典名快照，非样稿渠道档）。 */
const SUPPORT_SOURCES = [
  { name: '店铺甲', order_count: 156, total_price: '9000.00', total_hall_price: '0.00' },
  { name: '店铺乙', order_count: 12, total_price: '800.00', total_hall_price: '0.00' },
]

const SUPPORT_PAYLOAD = {
  range: { date_from: TODAY, date_to: TODAY },
  operators: SUPPORT_OPERATORS,
  operators_total: { name: '合计', order_count: 9, total_price: '2920.00', total_hall_price: '1900.00', performance: '1020.00' },
  sources: SUPPORT_SOURCES,
  sources_total: { name: '合计', order_count: 168, total_price: '9800.00', total_hall_price: '0.00' },
}

/** App 卡：`available` 档切换 PV/UV（T+1）；注册/押金两块与租户无关。 */
function appOverview(available: boolean) {
  return {
    days: 1,
    date_from: TODAY,
    date_to: TODAY,
    wechat: {
      available,
      reason: available ? '' : '未开通',
      date_from: D30_START,
      date_to: isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 1)),
      series: [],
      totals: available ? { session_cnt: 0, visit_pv: 1204, visit_uv: 386, visit_uv_new: 9 } : null,
    },
    registrations: {
      series: [],
      totals: { mp_weixin: 3, app_android: 2, unknown: 1, total: 6 },
      note: '',
    },
    claims: { series: [], totals: { total: 0, mobile: 0, by_reason: {} }, mobile_channels: [] },
    deposit: { total_frozen: '8400.00', taker_count: 23, series: [], note: '' },
  }
}

const DLT_SUMMARY = {
  platform: 10,
  platform_label: '代练通',
  account: '合成账号',
  configured: true,
  available_balance: '2340.00',
  frozen_balance: '0.00',
  error: '',
}

const MONTH_START = isoDay(new Date(NOW.getFullYear(), NOW.getMonth(), 1))

const DLT_STATS = {
  items: [
    { QDate: MONTH_START, PubCount: '40' },
    { QDate: TODAY, PubCount: '12' },
    { QDate: '2020-01-01', PubCount: '999' },
  ],
  total: {},
  record_count: 3,
  start: MONTH_START,
  end: TODAY,
  is_pub: 1,
}

const BALANCE_ALERT = {
  platform: 10,
  platform_label: '代练通',
  account: '合成账号',
  configured: true,
  needed: '500.00',
  pending_orders: 4,
  unpriced_orders: 0,
  available: '2340.00',
  frozen: '0.00',
  is_low: false,
  shortfall: '0.00',
  error: '',
}

const NOTICE_SYSTEM = {
  items: [{ id: 1, title: '示例公告标题', content: '正文', created_by: 'tester_a', created_at: '2026-11-05T09:00:00', updated_at: '2026-11-05T09:00:00' }],
  total: 1,
}
const NOTICE_PUSH = {
  items: [{ id: 5, kind: '被指派', order_no: '12345678901234567890', source_order_no: null, payload: {}, read_at: null, created_at: '2026-11-06T08:00:00' }],
  total: 1,
}

/* ── 装载 ──────────────────────────────────────────────────────────────────── */

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/accounts/messages', name: 'accounts-messages', component: Blank },
      { path: '/v2/messages', name: 'v2-messages', component: Blank },
    ],
  })
}

type MountOpts = { isAccountAdmin?: boolean }

async function mountDashboard(opts: MountOpts = {}): Promise<{ wrapper: VueWrapper; router: Router }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 7,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: opts.isAccountAdmin ?? true,
    roles: ['系统管理员'],
    role: '系统管理员',
    permissions: [],
    menu_permissions: ['order.list'],
    game_ids: [1],
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2Dashboard, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

/** 状态环图例的三行读数（名字串 + 值串分开钉，防 whitespace 归一化歧义）。 */
function legendCells(wrapper: VueWrapper): { names: string[]; vals: string[] } {
  return {
    names: wrapper.findAll('[data-v2="status-legend"] .cv2-dsh-legend-name').map((n) => n.text()),
    vals: wrapper.findAll('[data-v2="status-legend"] .cv2-dsh-legend-val').map((n) => n.text()),
  }
}

/** request() 的按 URL 路由（stubHappyPath 与单测覆盖共用同一份路由）。 */
function routeHttpRequest(config: { url: string; params?: Record<string, unknown> }): Promise<unknown> {
  const params = (config.params ?? {}) as Record<string, string | number>
  if (config.url === '/api/business/order-stats') {
    // 同一端点两个消费方：大厅「今日被抢」求和用 limit 2000 全行档；排行榜用 limit 8。
    if (Number(params.limit) === 2000) {
      return Promise.resolve({ items: BOARD_ROWS, total: BOARD_ROWS.length, page: 1, page_size: 2000 })
    }
    return Promise.resolve({ items: BOARD_ROWS, total: BOARD_ROWS.length, page: 1, page_size: 8 })
  }
  if (config.url === '/api/business/home/overview/') {
    const table: Record<string, Promise<unknown>> = {
      [TODAY]: Promise.resolve(OVERVIEW_TODAY),
      [YESTERDAY]: Promise.resolve(OVERVIEW_YESTERDAY),
      [D30_START]: Promise.resolve(OVERVIEW_D30),
      [D30_PREV_START]: Promise.resolve(OVERVIEW_D30_PREV),
    }
    const hit = table[String(params.start)]
    // 路由表没登记的窗口 = 组件发了不该发的请求 ⇒ 让用例响亮地失败（不给假数据）。
    return hit ?? Promise.reject(new Error(`意外的 overview 窗口：${String(params.start)}`))
  }
  if (config.url === '/api/business/dashboard/trend/') return Promise.resolve(TREND_DAYS)
  if (config.url === '/api/business/dashboard/game-distribution/') return Promise.resolve(GAME_ROWS)
  if (config.url === '/api/business/dashboard/hall-stay/') return Promise.resolve(HALL_STAY)
  if (config.url === '/api/business/dashboard/sync-health/') return Promise.resolve(SYNC_HEALTH)
  if (config.url === '/api/business/hall/') return Promise.resolve(HALL_PAGE)
  if (config.url === '/api/business/performances/') return Promise.resolve(PERFORMANCES_PAGE)
  return Promise.resolve(BALANCE_ALERT)
}

/** 默认成功回落：窗口调用按「第一通 = 今天档、其后 = 30 天档」返回（竞态用例自行覆盖）。 */
function stubHappyPath(): void {
  let windowCallSeen = false
  fetchOrdersWithSummary.mockImplementation((params: { date_from?: string }) => {
    if (params.date_from === undefined) return Promise.resolve(pageWith(CURRENT_COUNTS))
    windowCallSeen = !windowCallSeen
    return Promise.resolve(pageWith(windowCallSeen ? STATUS_TODAY : STATUS_D30))
  })
  fetchSupportStats.mockResolvedValue(SUPPORT_PAYLOAD)
  httpRequest.mockImplementation(routeHttpRequest)
  fetchAppStatsOverview.mockResolvedValue(appOverview(false))
  fetchPlatformIntegrationSummary.mockResolvedValue(DLT_SUMMARY)
  fetchDailiantongStats.mockResolvedValue(DLT_STATS)
  fetchSystemNotices.mockResolvedValue(NOTICE_SYSTEM)
  fetchNotifications.mockResolvedValue(NOTICE_PUSH)
  markNotificationRead.mockResolvedValue({ ...NOTICE_PUSH.items[0], read_at: '2026-11-06T08:03:00' })
}

beforeEach(() => {
  vi.clearAllMocks()
  instances.length = 0
  stubHappyPath()
})

/* ── charts：环形 Metabase 规格（首次上机器判据）────────────────────────── */

const donutData = {
  slices: [
    { name: '已完成', value: 60, amountText: '60', color: '#88BF4D', percentText: '60.0%' },
    { name: '代练中', value: 40, amountText: '40', color: '#509EE3', percentText: '40.0%' },
  ],
  rank: [],
  totalValue: 100,
}

describe('charts：createDonut（Metabase 环形规格）', () => {
  it('默认：中心覆盖层 = 总值 +「总计」、悬停联动注册、label 仍是 flat 形态、环几何 60%/100% + 12 点钟顺时针', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts)
    controller.update(donutData, '88', '金额')
    const instance = instances[0]!
    expect(centerTextOf(instance)).toEqual({ value: '88', label: '总计' })
    expect(instance.on).toHaveBeenCalledWith('mouseover', expect.any(Function))
    expect(instance.on).toHaveBeenCalledWith('mouseout', expect.any(Function))
    const option = instance.setOption.mock.calls[0]![0] as {
      series: {
        label: unknown
        labelLine: unknown
        radius: string[]
        startAngle: number
        clockwise: boolean
      }[]
    }
    expect(option.series[0]!.label).toEqual({ show: false })
    expect(option.series[0]!.labelLine).toEqual({ show: false })
    expect(option.series[0]!.radius).toEqual(['60%', '100%'])
    expect(option.series[0]!.startAngle).toBe(90)
    expect(option.series[0]!.clockwise).toBe(true)
  })

  it('悬停扇形 → 中心切「该块值 + 名」、其余片淡 30%；mouseout 复位总计与全亮', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts)
    controller.update(donutData, '100', '状态')
    const instance = instances[0]!
    handlerOf(instance, 'mouseover')({ seriesIndex: 0, dataIndex: 1 })
    expect(centerTextOf(instance)).toEqual({ value: '40', label: '代练中' })
    expect(sliceOpacities(instance)).toEqual([0.3, 1])
    handlerOf(instance, 'mouseout')({})
    expect(centerTextOf(instance)).toEqual({ value: '100', label: '总计' })
    expect(sliceOpacities(instance)).toEqual([1, 1])
  })

  it('防抖：非本系列 / 越界 dataIndex / 缺下标的悬停不切中心', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts)
    controller.update(donutData, '100', '状态')
    const instance = instances[0]!
    handlerOf(instance, 'mouseover')({ seriesIndex: 3, dataIndex: 0 })
    handlerOf(instance, 'mouseover')({ seriesIndex: 0, dataIndex: 99 })
    handlerOf(instance, 'mouseover')({ seriesIndex: 0 })
    expect(centerTextOf(instance)).toEqual({ value: '100', label: '总计' })
    handlerOf(instance, 'mouseover')({ seriesIndex: 0, dataIndex: 0 })
    expect(centerTextOf(instance)).toEqual({ value: '60', label: '已完成' })
  })

  it('centerLabel 自定义；图例悬停 highlight ⇄ 淡化其余 + 中心切该块、downplay 复位、未知名不动作', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts, { centerLabel: '区间单量' })
    controller.update(donutData, '100', '状态')
    expect(centerTextOf(instances[0]!)).toEqual({ value: '100', label: '区间单量' })
    controller.highlight('代练中')
    expect(centerTextOf(instances[0]!)).toEqual({ value: '40', label: '代练中' })
    expect(sliceOpacities(instances[0]!)).toEqual([0.3, 1])
    controller.highlight('不存在的块')
    expect(centerTextOf(instances[0]!)).toEqual({ value: '40', label: '代练中' })
    controller.downplay()
    expect(centerTextOf(instances[0]!)).toEqual({ value: '100', label: '区间单量' })
    expect(sliceOpacities(instances[0]!)).toEqual([1, 1])
  })

  it('hideSliceLabels / hideTipOnLeave：label 走 3.6.2 嵌套形态；容器离开兜底 hideTip；dispose 摘覆盖层并还原定位', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts, { hideSliceLabels: true, hideTipOnLeave: true })
    controller.update(donutData, '100', '状态')
    const instance = instances[0]!
    const option = instance.setOption.mock.calls[0]![0] as {
      series: {
        label: { normal: { show: boolean }; emphasis: { show: boolean } }
        labelLine: { normal: { show: boolean }; emphasis: { show: boolean } }
      }[]
    }
    expect(option.series[0]!.label.normal.show).toBe(false)
    expect(option.series[0]!.label.emphasis.show).toBe(false)
    expect(option.series[0]!.labelLine.normal.show).toBe(false)
    expect(option.series[0]!.labelLine.emphasis.show).toBe(false)
    el.dispatchEvent(new Event('mouseleave'))
    expect(instance.dispatchAction).toHaveBeenCalledWith({ type: 'hideTip' })
    controller.dispose()
    expect(instance.el.querySelector('.tt2-donut-center')).toBeNull()
    expect(el.style.position).toBe('')
    el.dispatchEvent(new Event('mouseleave'))
    expect(instance.dispatchAction).toHaveBeenCalledTimes(1)
  })

  it('容器太窄只显示数值：宽 <160px 隐藏中心标签行，恢复宽度 resize 后显示', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    Object.defineProperty(el, 'clientWidth', { value: 140, configurable: true })
    const controller = createDonut(el, fakeEcharts)
    controller.update(donutData, '100', '状态')
    const label = el.querySelector<HTMLElement>('.tt2-donut-center-label')!
    expect(label.style.display).toBe('none')
    Object.defineProperty(el, 'clientWidth', { value: 300, configurable: true })
    controller.resize()
    expect(label.style.display).toBe('')
  })

  it('<0.5% 的片渲染值撑到 0.5%（防不可见；percentText 仍按真值）', async () => {
    const { createDonut } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createDonut(el, fakeEcharts)
    controller.update(
      {
        slices: [
          { name: '大头', value: 1000, amountText: '1000', color: '#509EE3', percentText: '99.9%' },
          { name: '零头', value: 1, amountText: '1', color: '#88BF4D', percentText: '0.1%' },
        ],
        rank: [],
        totalValue: 1001,
      },
      '1001',
      '测试',
    )
    const option = instances[0]!.setOption.mock.calls[0]![0] as {
      series: { data: { value: number }[] }[]
    }
    expect(option.series[0]!.data[0]!.value).toBe(1000)
    expect(option.series[0]!.data[1]!.value).toBeCloseTo(5.005, 3)
  })
})

describe('charts：buildDonutData（色板 / 哈希恒同色 / 「其他」合并）', () => {
  it('8 色色板是 Metabase 值；名字哈希取色落在色板内且同名恒同色', () => {
    expect([...DONUT_PALETTE]).toEqual([
      '#509EE3', '#88BF4D', '#A989C5', '#EF8C8C', '#F9D45C', '#F2A86F', '#98D9D9', '#7172AD',
    ])
    const d1 = buildDonutData([{ name: '店铺甲', amount: '10' }, { name: '店铺乙', amount: '4' }])
    const d2 = buildDonutData([{ name: '店铺乙', amount: '9' }, { name: '店铺丙', amount: '1' }])
    const colorOf = (data: NonNullable<ReturnType<typeof buildDonutData>>, name: string): string => {
      const hit = data.slices.find((slice) => slice.name === name)
      expect(hit, `${name} 没进盘面`).toBeDefined()
      return hit!.color
    }
    // 同名跨两份数据取同色（哈希只看名字）。
    expect(colorOf(d1!, '店铺乙')).toBe(colorOf(d2!, '店铺乙'))
    for (const slice of [...d1!.slices, ...d2!.slices]) {
      expect(DONUT_PALETTE).toContain(slice.color)
    }
  })

  it('「其他」合并阈值：占比 <2.5% 的候选 ≥2 才并成一个灰「其他」（金额整数分求和）；单个候选照常显示', () => {
    // 90 / 2.2 / 2.2（总 94.4）：两个 2.33% 候选 → 并「其他」4.40，恒排最后。
    const merged = buildDonutData([
      { name: '甲', amount: '90' },
      { name: '乙', amount: '2.2' },
      { name: '丙', amount: '2.2' },
    ])
    expect(merged!.slices.map((slice) => slice.name)).toEqual(['甲', '其他'])
    expect(merged!.slices[1]!.color).toBe(DONUT_OTHER_COLOR)
    expect(merged!.slices[1]!.amountText).toBe('4.40')
    // 90 / 8.9 / 1.1（总 100）：只有 1.1% 一个候选 → 不合并。
    const kept = buildDonutData([
      { name: '甲', amount: '90' },
      { name: '乙', amount: '8.9' },
      { name: '丙', amount: '1.1' },
    ])
    expect(kept!.slices.map((slice) => slice.name)).toEqual(['甲', '乙', '丙'])
    expect(kept!.slices.every((slice) => DONUT_PALETTE.includes(slice.color))).toBe(true)
  })

  it('>8 段硬顶：收成前 7 名 + 「其他」（其他恒最后、吃下余量合计）', () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({ name: `来源${i}`, amount: String(10 - i) }))
    const data = buildDonutData(rows)!
    expect(data.slices).toHaveLength(8)
    expect(data.slices.slice(0, 7).map((slice) => slice.name)).toEqual([
      '来源0', '来源1', '来源2', '来源3', '来源4', '来源5', '来源6',
    ])
    expect(data.slices[7]!.name).toBe('其他')
    expect(data.slices[7]!.color).toBe(DONUT_OTHER_COLOR)
    expect(data.slices[7]!.amountText).toBe('6.00')
    // 排名明细 = 截断前完整榜；被并的行落「其他」灰。
    expect(data.rank).toHaveLength(10)
    expect(data.rank[7]!.color).toBe(DONUT_OTHER_COLOR)
    expect(data.rank[0]!.color).not.toBe(DONUT_OTHER_COLOR)
  })
})

describe('charts 扩展：createBarLineCombo（双轴柱线）', () => {
  const points = [
    { label: '11-04', barValue: 8, lineValue: 2100, lineText: '2100' },
    { label: '11-05', barValue: 14, lineValue: 3500, lineText: '3500.00' },
    { label: '11-06', barValue: 23, lineValue: 5600.5, lineText: '5600.50' },
  ]

  async function makeCombo() {
    const { createBarLineCombo } = await import('@/v2/charts')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const controller = createBarLineCombo(el, fakeEcharts)
    controller.update({ points, barName: '单量', lineName: '金额' })
    return { controller, instance: instances[0]! }
  }

  it('形态：柱=左轴、线=右轴；x 轴刻度、axis 触发、垂直参考线', async () => {
    const { instance } = await makeCombo()
    const option = instance.setOption.mock.calls[0]![0] as {
      series: { type: string; yAxisIndex: number; itemStyle: { color: string } }[]
      yAxis: { type: string }[]
      xAxis: { data: string[] }
      tooltip: { trigger: string; axisPointer: { type: string } }
    }
    expect(option.series).toHaveLength(2)
    expect(option.series[0]!.type).toBe('bar')
    expect(option.series[0]!.yAxisIndex).toBe(0)
    expect(option.series[0]!.itemStyle.color).toBe(COMBO_BAR_COLOR)
    expect(option.series[1]!.type).toBe('line')
    expect(option.series[1]!.yAxisIndex).toBe(1)
    expect(option.series[1]!.itemStyle.color).toBe(COMBO_LINE_COLOR)
    expect(option.yAxis).toHaveLength(2)
    expect(option.yAxis.every((axis) => axis.type === 'value')).toBe(true)
    expect(option.xAxis.data).toEqual(['11-04', '11-05', '11-06'])
    expect(option.tooltip.trigger).toBe('axis')
    expect(option.tooltip.axisPointer.type).toBe('line')
  })

  it('悬停白卡 formatter：行 = 柱值 + 线的原样串（⛔ 不拿几何值拼金额）', async () => {
    const { instance } = await makeCombo()
    const option = instance.setOption.mock.calls[0]![0] as {
      tooltip: { formatter: (params: { dataIndex: number }[]) => string }
    }
    const html = option.tooltip.formatter([{ dataIndex: 1 }])
    expect(html).toContain('11-05')
    expect(html).toContain('14')
    expect(html).toContain('3500.00')
    expect(html).not.toContain('5600')
  })

  it('右轴金额刻度：≥1000 缩写 k、其余取整', async () => {
    const { instance } = await makeCombo()
    const option = instance.setOption.mock.calls[0]![0] as {
      yAxis: { axisLabel: { formatter: (v: number) => string } }[]
    }
    const tick = option.yAxis[1]!.axisLabel.formatter
    expect(tick(1500)).toBe('1.5k')
    expect(tick(15000)).toBe('15k')
    expect(tick(999)).toBe('999')
  })
})

/* ── 组件级：管理员 ────────────────────────────────────────────────────────── */

describe('V2Dashboard：默认今天（KPI×6 钉值 + 双窗口真值）', () => {
  it('前五格 = home/overview 今天窗真值；在途订单 = 当前在库合计 + 四态拆行', async () => {
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('录单笔数')
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('18')
    expect(wrapper.find('[data-v2-kpi="price"]').text()).toContain('¥4760.00')
    expect(wrapper.find('[data-v2-kpi="profit"]').text()).toContain('1190.00')
    expect(wrapper.find('[data-v2-kpi="finish"]').text()).toContain('94.1%')
    expect(wrapper.find('[data-v2-kpi="refund"]').text()).toContain('1.8%')
    expect(wrapper.find('[data-v2-kpi="transit"] [data-v2="transit-total"]').text()).toBe('177')
    expect(wrapper.find('[data-v2="transit-sub"]').text()).toBe('当前在库 · 代练中 96 · 待验收 41 · 待接单 22 · 未分配 18')
    // 同比 delta（今天 18 vs 昨天窗 16 = ▲12.5%；退单率 1.8 vs 2.1 = ▼14.3% 且跌为好）。
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('▲ 12.5% 比上一周期')
    expect(wrapper.find('[data-v2-kpi="refund"]').text()).toContain('▼ 14.3% 比上一周期')
  })

  it('占位标归零；大厅 / 同步 / 待结算按真接口渲染（闲管家显「—」+ 口径待拍板）', async () => {
    const { wrapper } = await mountDashboard()
    expect(wrapper.findAll('[data-v2="ph-tag"]')).toHaveLength(0)
    // 大厅实时：在厅 = hall 列表 total；今日被抢 = order-stats 今日全行 orderCount 求和（21+13）；
    // 停留 = hall-stay 的分钟数（134 → 2h14m / 362 → 6h2m / 7 → 7 分钟）。
    expect(wrapper.find('[data-v2="hall-in"]').text()).toBe('12')
    expect(wrapper.find('[data-v2="hall-grabbed"]').text()).toBe('34')
    expect(wrapper.find('[data-v2="hall-avg"]').text()).toBe('2h14m')
    expect(wrapper.find('[data-v2="hall-sub"]').text()).toBe('最快被抢：7 分钟 · 最久滞留：6h2m')
    // 同步健康：四渠道真值；闲管家三键 null → 「—」，title 注明口径待拍板。
    const syncRows = wrapper.findAll('[data-v2="sync-rows"] .cv2-dsh-row')
    expect(syncRows).toHaveLength(4)
    expect(syncRows[0]!.text()).toContain('鸡蛋铺')
    expect(syncRows[0]!.text()).toContain('5 分钟前 · 积压 12')
    expect(syncRows[1]!.text()).toContain('失败 2')
    const xgjRow = wrapper.find('[data-v2-sync-row="闲管家"]')
    expect(xgjRow.text()).toContain('—')
    expect(xgjRow.attributes('title')).toContain('口径待拍板')
    // 待结算金额 = performances summary 的生效绩效槽（bonus_amount）。
    expect(wrapper.find('[data-v2="settle-amount"]').text()).toBe('¥8123.45')
  })

  it('首屏区间请求逐字：orders 带今天本地时区窗 + 无窗当前态；KPI 双窗口；趋势/游戏/大厅/同步/绩效各就各位', async () => {
    const { wrapper } = await mountDashboard()
    void wrapper
    const windowCalls = fetchOrdersWithSummary.mock.calls.filter(
      (call) => (call[0] as { date_from?: string }).date_from !== undefined,
    )
    expect(windowCalls).toHaveLength(1)
    const windowParams = windowCalls[0]![0] as { date_from: string; date_to: string; page: number; page_size: number }
    const midnight = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), 0, 0, 0)
    const endOfDay = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), 23, 59, 59)
    expect(windowParams).toEqual({ date_from: toLocalIso(midnight), date_to: toLocalIso(endOfDay), page: 1, page_size: 1 })
    expect(
      fetchOrdersWithSummary.mock.calls.some((call) => (call[0] as { date_from?: string }).date_from === undefined),
    ).toBe(true)
    // KPI 双窗口（§0.12）：当前窗（今天）+ 等长前移窗（昨天），参数逐字。
    const overviewParams = httpRequest.mock.calls
      .filter((call) => (call[0] as { url: string }).url === '/api/business/home/overview/')
      .map((call) => (call[0] as { params: { start: string; end: string } }).params)
    expect(overviewParams).toEqual([
      { start: TODAY, end: TODAY },
      { start: YESTERDAY, end: YESTERDAY },
    ])
    // 趋势 = 固定近 30 天；游戏环吃今天窗。
    const trendCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/dashboard/trend/',
    )![0] as { params: { start: string; end: string } }
    expect(trendCall.params).toEqual({ start: D30_START, end: TODAY })
    const gameCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/dashboard/game-distribution/',
    )![0] as { params: { start: string; end: string } }
    expect(gameCall.params).toEqual({ start: TODAY, end: TODAY })
    // order-stats 两个消费方各一发：排行榜（limit 8）与大厅今日求和（limit 2000），同吃今天窗。
    const statsParams = httpRequest.mock.calls
      .filter((call) => (call[0] as { url: string }).url === '/api/business/order-stats')
      .map((call) => (call[0] as { params: { limit: number; stime: string; etime: string } }).params)
    expect(statsParams).toHaveLength(2)
    expect(statsParams).toContainEqual({ page: 1, limit: 8, stime: TODAY, etime: TODAY })
    expect(statsParams).toContainEqual({ page: 1, limit: 2000, stime: TODAY, etime: TODAY })
    expect(fetchSupportStats.mock.calls[0]![0]).toEqual({ dateFrom: TODAY, dateTo: TODAY })
    // 大厅 / 同步 / 绩效：无窗快照一发 + 各自参数形状。
    const hallCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/hall/',
    )![0] as { params: Record<string, number> }
    expect(hallCall.params).toEqual({ page: 1, page_size: 1 })
    const hallStayCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/dashboard/hall-stay/',
    )![0] as { params?: Record<string, unknown> }
    expect(hallStayCall.params).toBeUndefined()
    const syncCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/dashboard/sync-health/',
    )
    expect(syncCall).toBeDefined()
    const perfCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/performances/',
    )![0] as { params: Record<string, unknown> }
    expect(perfCall.params).toEqual({ settle_status: '未结账', page_size: 1 })
  })

  it('客服接单统计从左到右按单量降序，金额跟随对应客服', async () => {
    fetchSupportStats.mockResolvedValue({
      ...SUPPORT_PAYLOAD,
      operators: [...SUPPORT_OPERATORS].reverse(),
    })
    const { wrapper } = await mountDashboard()
    const option = lastSetOption(instanceFor(wrapper, 'support-chart')) as {
      xAxis: { data: string[] }; series: { data: number[] }[]
    }
    expect(option.xAxis.data).toEqual(['客服甲', '客服乙'])
    expect(option.series.map((series) => series.data)).toEqual([[5, 4], [1620, 1300]])
  })

  it('趋势图：dashboard/trend 真数据 30 天点位（止于今天、首日起 D30_START、柱值逐日 +1）', async () => {
    const { wrapper } = await mountDashboard()
    const trend = instanceFor(wrapper, 'trend-chart')
    const option = lastSetOption(trend) as { xAxis: { data: string[] }; series: { data: number[] }[] }
    expect(option.xAxis.data).toHaveLength(30)
    expect(option.xAxis.data[29]!).toBe(TODAY.slice(5))
    expect(option.xAxis.data[0]!).toBe(D30_START.slice(5))
    expect(option.series[0]!.data).toEqual(TREND_DAYS.map((day) => day.count))
  })
})

describe('V2Dashboard：区间切换（五处联动 + 竞态）', () => {
  it('切最近30天：KPI 双窗口换档；orders/两个统计端点/游戏环参数跟着变；趋势不重建', async () => {
    const { wrapper } = await mountDashboard()
    fetchOrdersWithSummary.mockClear()
    fetchSupportStats.mockClear()
    httpRequest.mockClear()
    await wrapper.find('[data-v2-range="d30"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('486')
    expect(wrapper.find('[data-v2-kpi="price"]').text()).toContain('¥128460.00')
    expect(wrapper.find('[data-v2-kpi="finish"]').text()).toContain('92.4%')
    // KPI 双窗口：30 天窗 + 30 天前移窗，参数逐字。
    const overviewParams = httpRequest.mock.calls
      .filter((call) => (call[0] as { url: string }).url === '/api/business/home/overview/')
      .map((call) => (call[0] as { params: { start: string; end: string } }).params)
    expect(overviewParams).toEqual([
      { start: D30_START, end: TODAY },
      { start: D30_PREV_START, end: D30_PREV_END },
    ])
    const windowParams = fetchOrdersWithSummary.mock.calls[0]![0] as { date_from: string; date_to: string }
    expect(windowParams.date_from).toBe(toLocalIso(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - 29, 0, 0, 0)))
    const boardCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/order-stats' && Number((call[0] as { params?: { limit?: number } }).params?.limit) === 8,
    )![0] as { params: { stime: string; etime: string } }
    expect(boardCall.params.stime).toBe(D30_START)
    expect((fetchSupportStats.mock.calls[0]![0] as { dateFrom: string }).dateFrom).toBe(D30_START)
    const gameCall = httpRequest.mock.calls.find(
      (call) => (call[0] as { url: string }).url === '/api/business/dashboard/game-distribution/',
    )![0] as { params: { start: string; end: string } }
    expect(gameCall.params).toEqual({ start: D30_START, end: TODAY })
    // 来源环换 30 天档（店铺甲 156/全环 168 = 92.9%）；游戏环换 30 天档（游戏甲 21/39 = 53.8%）；
    // 图例列只钉百分比（Metabase 图例 = 名字 + 百分比）。
    expect(wrapper.find('[data-v2="src-legend"]').text()).toContain('店铺甲')
    expect(wrapper.find('[data-v2="src-legend"]').text()).toContain('92.9%')
    expect(wrapper.find('[data-v2="game-legend"]').text()).toContain('游戏甲')
    expect(wrapper.find('[data-v2="game-legend"]').text()).toContain('53.8%')
    // 趋势实例没有因切区间重建（同一元素 = 同一实例）。
    expect(wrapper.find('[data-v2="trend-chart"]').element).toBe(instanceFor(wrapper, 'trend-chart').el)
  })

  it('快速连切（30 天挂起 → 今天先回）：最终渲染今天的数据，旧轮响应落地即弃', async () => {
    let resolveSlow!: (value: unknown) => void
    fetchOrdersWithSummary.mockImplementation((params: { date_from?: string }) => {
      if (params.date_from === undefined) return Promise.resolve(pageWith(CURRENT_COUNTS))
      // 组件发的是带时区的完整 ISO（toLocalIso），这里按日期前缀分档。
      if (params.date_from.startsWith(D30_START)) {
        return new Promise((resolve) => {
          resolveSlow = resolve
        })
      }
      return Promise.resolve(pageWith(STATUS_TODAY))
    })
    const { wrapper } = await mountDashboard()
    await wrapper.find('[data-v2-range="d30"]').trigger('click')
    await wrapper.find('[data-v2-range="today"]').trigger('click')
    await flushPromises()
    // 今天档：已完成 11 / 全环 18 = 61.1%（图例列只钉百分比）。
    const cells = legendCells(wrapper)
    expect(cells.names[0]).toBe('已完成')
    expect(cells.vals[0]).toBe('61.1%')
    // 旧轮（30 天）此刻才落地 —— 不得覆盖今天。
    resolveSlow(pageWith(STATUS_D30))
    await flushPromises()
    expect(legendCells(wrapper).vals[0]).toBe('61.1%')
    expect(legendCells(wrapper).vals.join()).not.toContain('79.8%')
  })

  it('自定义区间：起>止自动对调、预设按钮清高亮、窗口参数对调后生效', async () => {
    const { wrapper } = await mountDashboard()
    await wrapper.find('[data-v2="range-box"]').trigger('click')
    await wrapper.find('[data-v2="cal-start"]').setValue('2026-11-06')
    await wrapper.find('[data-v2="cal-end"]').setValue('2026-11-01')
    await wrapper.find('[data-v2="cal-apply"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="range-box"]').text()).toContain('2026-11-01 ~ 2026-11-06')
    expect(wrapper.find('[data-v2-range="today"]').classes()).not.toContain('cv2-dsh-rbtn--on')
    const lastOrderCalls = fetchOrdersWithSummary.mock.calls
    const windowParams = lastOrderCalls[lastOrderCalls.length - 1]![0] as { date_from: string }
    expect(windowParams.date_from).toBe(toLocalIso(new Date(2026, 10, 1, 0, 0, 0)))
  })
})

describe('V2Dashboard：状态环合并 + 排行榜弹窗 + 单卡降级', () => {
  it('状态环：具名五态各自一格按值降序、「其他」恒最后 + 小字行列明', async () => {
    const { wrapper } = await mountDashboard()
    await wrapper.find('[data-v2-range="d30"]').trigger('click')
    await flushPromises()
    const cells = legendCells(wrapper)
    // 具名五态按值降序（388/44/21/12/8）+ 其他 13 恒最后；「其他」2.7%。
    expect(cells.names).toEqual(['已完成', '代练中', '待验收', '未分配', '待接单', '其他'])
    expect(cells.vals[0]).toBe('79.8%')
    expect(cells.vals[5]).toBe('2.7%')
    expect(wrapper.find('[data-v2="status-sub"]').text()).toBe('其他 = 问题单 5 · 已暂停 4 · 已退单 4')
    // 环容器拿到了悬停联动（on 注册 mouseover/mouseout）。
    const status = instanceFor(wrapper, 'status-chart')
    expect(status.on).toHaveBeenCalledWith('mouseover', expect.any(Function))
  })

  it('排行榜：行渲染 Top N + 点击行弹四格弹窗（金额/在途/近7天 = 待接口预留文案）', async () => {
    const { wrapper } = await mountDashboard()
    const rows = wrapper.findAll('[data-v2-board-row]')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('测试甲')
    expect(rows[0]!.text()).toContain('21 单 · 96%')
    expect(wrapper.find('[data-v2="board-note"]').text()).toContain('Top 8')
    await rows[0]!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="mm-count"]').text()).toBe('21 单')
    expect(wrapper.find('[data-v2="mm-rate"]').text()).toBe('96%')
    expect(wrapper.find('[data-v2="mm-unsettled"]').text()).toBe('¥1240')
    expect(wrapper.find('[data-v2="mm-last"]').text()).toBe('2026-11-06 09:41')
    expect(wrapper.find('[data-v2="mm-pending"]').text()).toContain('待接口')
  })

  it('单卡失败降级：客服统计挂 → 该卡与来源环显错误文案，状态环与排行榜照常', async () => {
    fetchSupportStats.mockRejectedValue(new Error('客服统计端点挂了'))
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="support-fail"]').text()).toContain('客服统计端点挂了')
    // 来源环吃同一份载荷 ⇒ 同一批失败在来源卡也要说得出来（不是静默空环）。
    expect(wrapper.find('[data-v2="src-fail"]').text()).toContain('客服统计端点挂了')
    expect(wrapper.find('[data-v2="status-legend"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-v2-board-row]')).toHaveLength(2)
  })

  it('KPI 概览挂：五格显 — 与错误文案（不给假数），大厅/待结算照常', async () => {
    httpRequest.mockImplementation((config: { url: string; params?: Record<string, unknown> }) =>
      config.url === '/api/business/home/overview/'
        ? Promise.reject(new Error('概览端点挂了'))
        : routeHttpRequest(config),
    )
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('—')
    expect(wrapper.findAll('[data-v2="kpi-fail"]')).toHaveLength(5)
    expect(wrapper.findAll('[data-v2="kpi-fail"]')[0]!.text()).toContain('概览端点挂了')
    expect(wrapper.find('[data-v2="hall-in"]').text()).toBe('12')
    expect(wrapper.find('[data-v2="settle-amount"]').text()).toBe('¥8123.45')
  })

  it('待结算金额取数失败：该行显 —（不给假数），其余卡照常', async () => {
    httpRequest.mockImplementation((config: { url: string; params?: Record<string, unknown> }) =>
      config.url === '/api/business/performances/'
        ? Promise.reject(new Error('绩效端点挂了'))
        : routeHttpRequest(config),
    )
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="settle-amount"]').text()).toBe('—')
    expect(wrapper.find('[data-v2="hall-grabbed"]').text()).toBe('34')
    expect(wrapper.find('[data-v2-kpi="add"]').text()).toContain('18')
  })
})

describe('V2Dashboard：环形图例联动与显隐（Metabase 规格）', () => {
  it('悬停图例行 → 其余片淡 30% + 中心切该块；移出复位；点击行隐藏重画、再点恢复', async () => {
    const { wrapper } = await mountDashboard()
    const src = instanceFor(wrapper, 'src-chart')
    const row = () => wrapper.find('[data-v2="src-legend"] [data-v2-legend="店铺甲"]')
    // 悬停：店铺甲全亮、店铺乙淡 30%，中心切「156 · 店铺甲」（嵌套形态：3.6.2 只认 normal）。
    await row().trigger('mouseenter')
    expect(nestedSliceOpacities(src)).toEqual([1, 0.3])
    expect(centerTextOf(src)).toEqual({ value: '156', label: '店铺甲' })
    await row().trigger('mouseleave')
    expect(nestedSliceOpacities(src)).toEqual([1, 1])
    expect(centerTextOf(src)).toEqual({ value: '168', label: '区间单量' })
    // 点击：店铺甲隐藏重画（盘面只剩店铺乙）、行转暗。
    await row().trigger('click')
    await flushPromises()
    expect(row().classes()).toContain('cv2-dsh-legend-row--off')
    const option = lastSetOption(src) as { series: { data: { name: string }[] }[] }
    expect(option.series[0]!.data.map((item) => item.name)).toEqual(['店铺乙'])
    // 再点恢复。
    await row().trigger('click')
    await flushPromises()
    expect(row().classes()).not.toContain('cv2-dsh-legend-row--off')
    const restored = lastSetOption(src) as { series: { data: { name: string }[] }[] }
    expect(restored.series[0]!.data.map((item) => item.name)).toEqual(['店铺甲', '店铺乙'])
  })

  it('切片色 = 图例点色（同一 slice.color：来源/游戏环走名字哈希、状态环走具名五态；嵌套形态上盘）', async () => {
    const { wrapper } = await mountDashboard()
    // 来源环：每片 = DONUT_PALETTE[paletteIndexForName(名)]——图例点的 inline background 绑同一份 slice.color。
    for (const slice of nestedSliceColors(instanceFor(wrapper, 'src-chart'))) {
      expect(slice.color, `来源「${slice.name}」的切片色没走名字哈希`).toBe(
        DONUT_PALETTE[paletteIndexForName(slice.name)],
      )
    }
    // 状态环：具名五态各走固定色（同名状态恒同色）。
    const statusColors = nestedSliceColors(instanceFor(wrapper, 'status-chart'))
    expect(statusColors.find((s) => s.name === V2_ORDER_STATUS_LABELS.COMPLETED)!.color).toBe(
      STATUS_RING_COLORS.COMPLETED,
    )
    expect(statusColors.find((s) => s.name === V2_ORDER_STATUS_LABELS.IN_PROGRESS)!.color).toBe(
      STATUS_RING_COLORS.IN_PROGRESS,
    )
  })
})

describe('V2Dashboard：App / 代练通（T+1 口径与三源取数）', () => {
  it('微信不可用租户：PV/UV 显 — 与「微信数据次日更新」；注册/押金照常', async () => {
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="app-pv"]').text()).toBe('—')
    expect(wrapper.find('[data-v2="app-uv"]').text()).toBe('—')
    expect(wrapper.find('[data-v2="app-reg"]').text()).toBe('6')
    expect(wrapper.find('[data-v2="app-wechat-note"]').text()).toBe('微信数据次日更新')
    expect(wrapper.find('[data-v2="app-card"]').text()).toContain('押金冻结 ¥8400.00 · 23 名打手')
  })

  it('可用租户：PV/UV 出 T+1 值并注明截至日', async () => {
    fetchAppStatsOverview.mockResolvedValue(appOverview(true))
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="app-pv"]').text()).toBe('1204')
    expect(wrapper.find('[data-v2="app-uv"]').text()).toBe('386')
    expect(wrapper.find('[data-v2="app-wechat-note"]').text()).toContain('微信数据 T+1 · 截至')
  })

  it('代练通：余额 = summary、发布 = stats（窗口内求和，窗口外行不计）、预警线 = balance-alert', async () => {
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="dlt-balance"]').text()).toBe('¥2340.00')
    expect(wrapper.find('[data-v2="dlt-month"]').text()).toBe('52 单')
    expect(wrapper.find('[data-v2="dlt-today"]').text()).toBe('12 单')
    expect(wrapper.find('[data-v2="dlt-note"]').text()).toContain('余额可覆盖未来 24h 待发所需 ¥500.00')
    expect(fetchDailiantongStats.mock.calls[0]![0]).toEqual({ start: MONTH_START, end: TODAY, is_pub: 1 })
  })

  it('余额低于未来 24h 待发所需：提示句转「低于」档', async () => {
    httpRequest.mockImplementation((config: { url: string }) => {
      if (config.url === '/api/business/order-stats') {
        return Promise.resolve({ items: BOARD_ROWS, total: BOARD_ROWS.length, page: 1, page_size: 8 })
      }
      return Promise.resolve({ ...BALANCE_ALERT, is_low: true })
    })
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-v2="dlt-note"]').text()).toContain('余额低于未来 24h 待发所需 ¥500.00')
  })

  it('通知与公告：公告在前、共 3 条封顶；通知行在首页打开详情', async () => {
    const { wrapper, router } = await mountDashboard()
    const items = wrapper.findAll('[data-v2="notice-rows"] .cv2-dsh-nt-row')
    expect(items.length).toBeLessThanOrEqual(3)
    expect(items[0]!.text()).toContain('【公告】示例公告标题')
    expect(items[1]!.text()).toContain('【被指派】订单 12345678901234567890')
    const currentPath = router.currentRoute.value.path
    await items[1]!.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="notice-detail"]').text()).toContain('订单号 12345678901234567890')
    expect(markNotificationRead).toHaveBeenCalledWith(5)
    expect(router.currentRoute.value.path).toBe(currentPath)
  })
})

/* ── 组件级：非管理员（零看板请求）────────────────────────────────────── */

describe('V2Dashboard：角色布局切换', () => {
  it('管理员优先，其他账号只显示后端返回的布局，清除会话后不留旧布局', async () => {
    const { wrapper } = await mountDashboard({ isAccountAdmin: false })
    const session = useSessionStore()
    expect(wrapper.find('[data-role-stub]').exists()).toBe(false)
    session.user = { ...session.user!, dashboard_type: 'worker' }
    await flushPromises()
    expect(wrapper.find('[data-role-stub]').text()).toBe('worker')
    session.user = { ...session.user!, dashboard_type: 'work' }
    await flushPromises()
    expect(wrapper.find('[data-role-stub]').text()).toBe('work')
    session.clear()
    await flushPromises()
    expect(wrapper.find('[data-role-stub]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="notice-rows"]').exists()).toBe(false)
  })
})

describe('V2Dashboard：非管理员（权限边界）', () => {
  it('看板请求一发不发（orders/stats/support/app/代练通），渲染锁定说明 + 仅通知卡', async () => {
    const { wrapper, router } = await mountDashboard({ isAccountAdmin: false })
    expect(fetchOrdersWithSummary).not.toHaveBeenCalled()
    expect(fetchSupportStats).not.toHaveBeenCalled()
    expect(fetchAppStatsOverview).not.toHaveBeenCalled()
    expect(fetchPlatformIntegrationSummary).not.toHaveBeenCalled()
    expect(fetchDailiantongStats).not.toHaveBeenCalled()
    expect(httpRequest).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="locked"]').text()).toContain('欢迎回来')
    expect(wrapper.find('[data-v2="range"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="trend"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="notices"]').text()).toContain('【公告】示例公告标题')
    expect(fetchNotifications).toHaveBeenCalledTimes(1)
    expect(fetchSystemNotices).toHaveBeenCalledTimes(1)
    await wrapper.find('[data-v2="notices"] button').trigger('click')
    expect(wrapper.find('[data-v2="notice-detail"]').text()).toContain('正文')
    const currentRouteName = router.currentRoute.value.name
    await wrapper.findAll('[data-v2="notices"] button')[1]!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="notice-detail"]').text()).toContain('【被指派】')
    expect(router.currentRoute.value.name).toBe(currentRouteName)
    expect(instances).toHaveLength(0)
  })
})
