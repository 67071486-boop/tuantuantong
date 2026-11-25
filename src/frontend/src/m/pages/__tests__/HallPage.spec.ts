// @vitest-environment jsdom
import { openOptions, selectOption } from '../../components/__tests__/selectOption'
import { f7 } from 'framework7-vue'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是**唯一的桩边界**（vi.mock('@/api/http')）：hall / config / auth / jidanpuUpstream 跑**真代码**，判据打在 mock 的 request 被调时的 url / method / params 上。
// ⚠️ `http` 键必须给：`api/orders.ts` 顶层 `import { http, request }`（hall.ts 引它做单号形状校验），缺这个导出会让模块图在 import 阶段就炸。
vi.mock('@/api/http', () => ({
  request: vi.fn(),
  // ⭐ 抢单要读后端 `msg`（可能带「未能撤回」警告）⇒ 页面走 `requestEnvelope`。
  requestEnvelope: vi.fn(),
  http: {},
}))

vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
  confirm: vi.fn(),
}))
vi.mock('../../components/MobilePhotoBrowser.vue', () => ({
  default: {
  name: 'MobilePhotoBrowser',
  props: ['visible', 'images', 'index', 'showIndex'],
  emits: ['update:visible', 'index-change'],
  template: '<div class="photo-browser-stub"></div>',
},
}))

const replace = vi.fn()
// ⚠️ 工厂体里只返回箭头函数（`replace` / `push` 在**挂载时**才被读）⇒ 不会踩
// `vi.mock` 提升导致的 TDZ（本文件既有写法，2026-10-22 订单号那条线同样是这么加的）。
const push = vi.fn()
const routeQuery: Record<string, string | string[]> = {}
vi.mock('vue-router', () => ({ useRouter: () => ({ replace, push }), useRoute: () => ({ query: routeQuery }) }))

import { Toast, confirm } from '../../framework7Ui'
import ImageViewer from '../../components/MobilePhotoBrowser.vue'
import List from '../../components/MobilePagedList.vue'
import PullDownRefresh from '../../components/MobilePullToRefresh.vue'
import type { Mock } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request, requestEnvelope } from '@/api/http'
import type { ConfigRow, ServerRow } from '@/api/config'
import type { HallListItem } from '@/api/hall'
import type { User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import HallPage from '../HallPage.vue'
import { AUTO_REFRESH_STORAGE_KEY } from '../../utils/hallAutoRefresh'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as { success: Mock; error: Mock; warning: Mock }
const dialogConfirm = confirm as unknown as Mock

/* ── 合成夹具（E-0077：单号 / 游戏 / 昵称全部合成值） ── */

const GAME_A: ConfigRow = {
  id: 101,
  name: '测试游戏甲',
  sort_order: 1,
  is_active: true,
  created_at: '2026-10-01T00:00:00+00:00',
}
const GAME_B: ConfigRow = { ...GAME_A, id: 102, name: '测试游戏乙', sort_order: 2 }
const SERVER_A: ServerRow = { ...GAME_A, id: 501, name: '测试区服一', game: 101 }

const ME_FULL: User = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
  menu_permissions: ['grab_hall.take_order', 'my_orders'],
  game_ids: [101, 102],
  tenant_name: '测试工作室甲',
}

function nativeRow(overrides: Partial<HallListItem> = {}): HallListItem {
  return {
    order_no: '10000000000000000001',
    source_order_no: '',
    source: '',
    taker_name: '',
    status: 'WAITING_GRAB',
    game: 101,
    server: 501,
    project_text: '测试项目甲',
    total_price: '30.00',
    refund_fee: '0.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-10-10T08:00:00+00:00',
    game_role_name: '',
    customer_ww: '',
    hall_price: '12.50',
    sales_agent: '',
    created_by: '',
    remark: '',
    net_received: '0.00',
    hall_publisher_name: '',
    completed_at: null,
    time_requirement: '',
    payment_status: 'UNSETTLED',
    payment_method: '',
    // ⭐ 大厅行没有已结账的打手绩效 ⇒ `false`（键必须在）。
    taker_settled: false,
    origin: 'native',
    source_system: '',
    remote_order_id: '',
    remote_order_view: '',
    remote_game_area: '',
    remote_game_server: '',
    remote_hours_desc: '',
    mirror_pk: null,
    // ⭐ D-0296 四键（优选 / 灰卡 / 离厅原因 / 模拟单主键）：默认全「不是」。
    is_preferred: false,
    is_exited: false,
    exit_kind: null,
    sim_pk: null,
    ...overrides,
  }
}

/** 上家镜像行：没有我方单号、区服是远端文本、`mirror_pk` 是它的主键（api/hall.ts 的行契约）。 */
function upstreamRow(overrides: Partial<HallListItem> = {}): HallListItem {
  return nativeRow({
    order_no: '',
    server: null,
    project_text: '测试上家项目甲',
    hall_price: '20.00',
    origin: 'upstream',
    source_system: 'jidanpu',
    remote_order_id: 'JP10020030040050060',
    remote_order_view: '测试远端展示单号甲',
    remote_game_area: '测试远端区',
    remote_game_server: '测试远端区服甲',
    mirror_pk: 5,
    ...overrides,
  })
}

/**
 * 模拟单行（D-0296）：`order_no` 是**仿真单号**（后端 `_payload_for` 直填 display_order_no）、
 * `sim_pk` 是它自己的主键、恒优选；接单端点**永远**回 1012。
 */
function simulatedRow(overrides: Partial<HallListItem> = {}): HallListItem {
  return nativeRow({
    order_no: '10000000000000000009',
    project_text: '测试模拟项目甲',
    hall_price: '39.00',
    origin: 'simulated',
    is_preferred: true,
    sim_pk: 7,
    ...overrides,
  })
}

/** 造 n 张原生行（20 位合成单号从起始号顺增）。 */
function nativeRows(count: number, startNo: bigint): HallListItem[] {
  return Array.from({ length: count }, (_, i) =>
    nativeRow({ order_no: String(startNo + BigInt(i)) }),
  )
}

/**
 * 上家团团通镜像行（D-0303）：`order_no` 是**空串**（镜像卡不是我方订单）、`mirror_pk` 是它自己那张表的主键（**接单端点的路径参数**）、
 * 区服是远端文本、上家单号在 `remote_order_view` / `remote_order_id`。⚠️ `remote_game_server` 刻意留**空串**：后端 `peer_hall.serialize_peer_row` 对团团通行恒填 `""`（载荷里没有区服文本，spec）⇒ 夹具照真实形状造，「区服显示 —」也是一条判据。夹具一律合成值（E-0077）。
 */
function peerRow(overrides: Partial<HallListItem> = {}): HallListItem {
  return nativeRow({
    order_no: '',
    server: null,
    project_text: '测试团团通项目甲',
    hall_price: '18.00',
    origin: 'peer',
    source_system: 'peer',
    remote_order_id: '20000000000000000001',
    remote_order_view: '20000000000000000001',
    remote_game_area: '',
    remote_game_server: '',
    mirror_pk: 9,
    ...overrides,
  })
}

type HallFixture = { items: HallListItem[]; total: number }

/* ── mock 的请求路由（按 url 分流到夹具；hall 调用逐发记录供判据读数） ── */

let gamesData: ConfigRow[]
let serversData: ServerRow[]
let meData: User
/** 按游戏键的 hall 夹具（路由按请求里的 game 取；没配的游戏回落 '101'——多数用例只考 101）。 */
let hallFixtures: Record<string, HallFixture[]>
/** 下一发 hall 请求强制失败（load-more 的失败支）。 */
let hallFailNext: Error | null
/**
 * 挂起下一发 hall 请求（评审 Important-1 的探针）：置 true 后，下一发停在受控 promise 上，
 * 调 `releaseHeldHall()` 放行（载荷在挂起时就固化，放行前的夹具改动它看不到）。
 */
let holdNextHall = false
let releaseHeldHall: (() => void) | null = null
/** claim 请求的实现（成功 / 1012 / 403 / 挂起，各用例自设）。 */
let claimImpl: (config: AxiosRequestConfig) => Promise<unknown>
let claimTargetFixture: HallListItem | Error
/**
 * 参考图接口（`GET /api/business/orders/<单号>/screenshots`，D-0296）的夹具与失败开关。
 * 行形状照 `api/screenshots.ts::ScreenshotItem`（只取判据用到的键）。
 */
let shotsFixture: Array<Record<string, unknown>> = []
let shotsFailNext: Error | null = null
const hallCalls: Array<Record<string, unknown>> = []

function wireRequestMock(): void {
  requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
    const url = String(config.url)
    const params = (config.params ?? {}) as Record<string, unknown>
    if (url === '/api/business/hall/') {
      hallCalls.push({ ...params })
      if (holdNextHall) {
        holdNextHall = false
        return new Promise((resolve, reject) => {
          try {
            const payload = hallPayloadFor(params)
            releaseHeldHall = () => resolve(payload)
          } catch (error) {
            releaseHeldHall = () => reject(error)
          }
        })
      }
      return hallPayloadFor(params)
    }
    if (url === '/api/business/hall-claim-target/') {
      if (claimTargetFixture instanceof Error) throw claimTargetFixture
      return claimTargetFixture
    }
    if (url === '/api/business/games/') {
      return { items: gamesData, total: gamesData.length, page: 1, page_size: 200 }
    }
    if (url === '/api/auth/me') return { user: meData } // fetchMe() 解 .user（api/auth.ts:102）
    if (url === '/api/business/servers/') {
      return { items: serversData, total: serversData.length, page: 1, page_size: 200 }
    }
    if (url.startsWith('/api/business/orders/') && url.endsWith('/screenshots')) {
      if (shotsFailNext !== null) {
        const err = shotsFailNext
        shotsFailNext = null
        throw err
      }
      return { items: shotsFixture, total: shotsFixture.length }
    }
    if (url.includes('/claim/')) return claimImpl(config)
    if (url === '/api/auth/logout') return null
    throw new Error(`spec: 未预期的请求 ${url}`)
  })
}

/** hall 请求的夹具响应（按 game 键取页；hallFailNext 在这里消费）。 */
function hallPayloadFor(params: Record<string, unknown>): unknown {
  if (hallFailNext !== null) {
    const err = hallFailNext
    hallFailNext = null
    throw err
  }
  const gameKey = String(params.game ?? '101')
  const pages = hallFixtures[gameKey] ?? hallFixtures['101'] ?? []
  const pageNo = Math.max(1, Number(params.page ?? 1))
  const source = pages[Math.min(pageNo, pages.length) - 1]
  if (!source) throw new Error(`spec: hall game=${gameKey} 第 ${pageNo} 页没有夹具`)
  return {
    items: source.items,
    total: source.total,
    page: pageNo,
    page_size: Number(params.page_size ?? 20),
  }
}

/** 便捷读数：第几发 hall 请求的完整参数（恰断言用它，不用 toContain 放宽）。 */
function hallParams(index: number): Record<string, unknown> {
  const params = hallCalls[index]
  if (!params) throw new Error(`spec: hall 第 ${index + 1} 发不存在（共 ${hallCalls.length} 发）`)
  return params
}

function claimRequestCount(): number {
  return requestMock.mock.calls.filter((c) =>
    String((c[0] as AxiosRequestConfig).url).includes('/claim/'),
  ).length
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
      : {
          ...ME_FULL,
          ...(menuOverride !== undefined ? { menu_permissions: menuOverride } : {}),
          ...rest,
        }
}

function mountHall() {
  return mount(HallPage)
}

async function openHallMore(wrapper: ReturnType<typeof mountHall>) {
  const trigger = wrapper.get('[data-testid="hall-more-open"]')
  // Framework7 positions iOS popovers in the next frame.
  const animationFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
    queueMicrotask(() => callback(performance.now()))
    return 0
  })
  try {
    await trigger.trigger('click')
  } finally {
    animationFrame.mockRestore()
  }
  return wrapper.get(`[id="${trigger.attributes('aria-controls')}"]`)
}

async function openHallFilters(wrapper: ReturnType<typeof mountHall>): Promise<void> {
  const more = await openHallMore(wrapper)
  expect(more.classes()).toContain('modal-in')
  await more.get('[data-testid="hall-filter-open"]').trigger('click')
  expect(more.classes()).not.toContain('modal-in')
  expect(wrapper.get('[data-testid="hall-filter-form"]').element.closest('.sheet-modal')?.classList.contains('modal-in')).toBe(true)
}

async function openAutoRefreshIntervals(wrapper: ReturnType<typeof mountHall>) {
  const more = await openHallMore(wrapper)
  const interval = more.find('[data-testid="hall-refresh-interval-open"]')
  if (interval.exists()) {
    await interval.trigger('click')
  } else {
    const toggle = more.get<HTMLInputElement>('[role="switch"]')
    toggle.element.click()
    await flushPromises()
    expect(toggle.element.checked).toBe(false)
  }
  expect(more.classes()).not.toContain('modal-in')
  const panel = wrapper.get('[data-testid="hall-refresh-interval-panel"]')
  expect(panel.element.closest('.sheet-modal')?.classList.contains('modal-in')).toBe(true)
  return panel
}

const cardSel = '.m-hall-card'
const claimSel = '.m-hall-card__claim'
/** 订单号那一行（2026-10-22）：标签与可点的号文本分成两个选择器读。 */
const orderNoSel = '[data-testid="hall-detail-order-no"]'
const orderLabelSel = '[data-testid="hall-detail-panel"] dt'
const detailCloseSel = '[data-testid="hall-detail-close"]'
const priceSel = '.m-hall-order-heading__price'

/**
 * 卡片样式的**唯一出处**（本页 SFC 的 `<style scoped>`）。为什么要读源码：jsdom 不做布局 ⇒ 「卡片等高」「单行省略」这两条在渲染树里**断言不出来**（退成结构判据）。
 * 所以这里对**样式规则文本**下判据，每条规则都按选择器精确取出，⛔ 不用 `toContain` 全文匹配（会被别的规则蒙对）。
 */
// ⚠️ jsdom 环境下 `import.meta.url` 不是 file 协议（`new URL(...)` + `fileURLToPath` 会抛
// 「The URL must be of scheme file」）⇒ 用 `process.cwd()`（vitest 的 cwd = 前端工程根）定位，
// 与 `layui-island/pages/__tests__/feedback.spec.ts` 的同一条处置一致。
const HALL_SFC = readFileSync(path.resolve(process.cwd(), 'src/m/pages/HallPage.vue'), 'utf8')

/** 取 SFC 里 `selector { … }` 的规则体（找不到即抛——规则被改名时用例必须红）。 */
function cssRuleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = HALL_SFC.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))
  if (match === null || match[1] === undefined) {
    throw new Error(`spec: HallPage.vue 里找不到样式规则 ${selector}`)
  }
  // 去掉 CSS 注释再下判据：规则里的中文注释会解释「为什么不用 min-height」，
  // 带着注释判 `not.toContain('min-height')` 会被自己的说明文字喂成假红。
  return match[1].replace(/\/\*[\s\S]*?\*\//g, '')
}

/** 剪贴板替身（jsdom 两样都没有；逐用例显式装/拆，见 `afterEach`）。 */
function stubClipboard(): Mock {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  })
  return writeText
}

function removeClipboard(): void {
  Reflect.deleteProperty(navigator, 'clipboard')
}

/** 老接口兜底（`document.execCommand`）：jsdom 没有 ⇒ 定义 + 用完删掉。 */
function stubExecCommand(ok: boolean): void {
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: vi.fn(() => ok),
  })
}

function removeExecCommand(): void {
  Reflect.deleteProperty(document, 'execCommand')
}

/** 取 wrapper 里第 i 个匹配的文本（越界即抛——noUncheckedIndexedAccess 下下标访问恒可能 undefined）。 */
function textAt(wrapper: ReturnType<typeof mount>, selector: string, index: number): string {
  const el = wrapper.findAll(selector)[index]
  if (el === undefined) throw new Error(`spec: ${selector}[${index}] 不存在`)
  return el.text()
}

/** 取 mock 第 i 次调用的实参数组（越界即抛）。 */
function callArgsAt(mock: Mock, index: number): unknown[] {
  const args = mock.mock.calls[index]
  if (args === undefined) throw new Error(`spec: mock 第 ${index} 次调用不存在`)
  return args
}

/** 抢单确认弹层第 i 次调用的配置（confirm 弹层是本页抢单的必经口）。 */
function dialogOptsAt(index = 0): { content: string; onConfirm: () => void } {
  return callArgsAt(dialogConfirm, index)[0] as { content: string; onConfirm: () => void }
}

/** requestMock 里 url 命中 match 的第 i 次调用的 config。 */
function requestCallUrl(match: string, index: number): AxiosRequestConfig {
  const hits = requestMock.mock.calls.filter(
    (c) => String((c[0] as AxiosRequestConfig).url) === match,
  )
  return callArgsAt({ mock: { calls: hits } } as unknown as Mock, index)[0] as AxiosRequestConfig
}

/** 假定时器只伪装 setTimeout/setInterval 两族：⛔ 不伪装 setImmediate——`flushPromises` 走它
 * （读码 @vue/test-utils：`scheduler = setImmediate ?? setTimeout`），伪装了它整条 flush 就挂死。 */
function useFakeTimersForHall(): void {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
}

/** `document.hidden` 的替换与还原（jsdom 的 hidden 挂在原型上，实例属性可遮蔽、可删除）。 */
function setHidden(value: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, value })
}
function restoreHidden(): void {
  delete (document as unknown as { hidden?: boolean }).hidden
}

enableAutoUnmount(afterEach)

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  for (const key in routeQuery) delete routeQuery[key]
  gamesData = [GAME_A, GAME_B]
  serversData = [SERVER_A]
  meData = ME_FULL
  hallFixtures = { '101': [{ items: nativeRows(2, 10000000000000000001n), total: 2 }] }
  hallFailNext = null
  holdNextHall = false
  releaseHeldHall = null
  claimImpl = async () => ({ order_no: '10000000000000000001', status: 'IN_PROGRESS' })
  claimTargetFixture = nativeRow()
  shotsFixture = []
  shotsFailNext = null
  hallCalls.length = 0
  window.localStorage.clear()
  restoreHidden()
  wireRequestMock()
  // ⭐ 默认「后端 msg 为空」：`requestEnvelope` 转接 `request` 替身 ⇒ 既有用例只配 request 即可。
  vi.mocked(requestEnvelope).mockImplementation(async (config) => ({
    code: 0,
    msg: '',
    data: await requestMock(config),
  }))
})

afterEach(() => {
  vi.useRealTimers()
  restoreHidden()
  removeClipboard()
  removeExecCommand()
  // 降级那条路会往 body 塞离屏 textarea（失败路径也一样）⇒ 每例收口。
  document.querySelectorAll('textarea').forEach((el) => el.remove())
})

/* ── 用例 ── */

describe('m 端接单大厅（HallPage）', () => {
  it('从新单通知直达该单确认，列表未返回也无需搜索，点接单才提交', async () => {
    loginSession()
    routeQuery.claimSource = 'local'
    routeQuery.claimRef = '10000000000000000899'
    claimTargetFixture = nativeRow({ order_no: routeQuery.claimRef, hall_price: '41.50' })
    dialogConfirm.mockReturnValueOnce({ close: vi.fn() })
    holdNextHall = true
    mountHall()
    await flushPromises()

    expect(requestMock).toHaveBeenCalledWith({
      method: 'get', url: '/api/business/hall-claim-target/',
      params: { source: 'local', ref: '10000000000000000899' },
    })
    expect(dialogConfirm).toHaveBeenCalledOnce()
    expect(dialogOptsAt().content).toContain('41.50 元')
    expect(claimRequestCount()).toBe(0)

    dialogOptsAt().onConfirm()
    await flushPromises()
    expect(claimRequestCount()).toBe(1)
    expect(requestCallUrl('/api/business/hall/10000000000000000899/claim/', 0).method).toBe('post')
    releaseHeldHall?.()
  })

  it('离开大厅时关闭通知里的抢单确认，清除遮罩且不提交订单', async () => {
    loginSession()
    routeQuery.claimSource = 'local'
    routeQuery.claimRef = '10000000000000000899'
    claimTargetFixture = nativeRow({ order_no: routeQuery.claimRef })
    const actual = await vi.importActual<typeof import('../../framework7Ui')>('../../framework7Ui')
    dialogConfirm.mockImplementationOnce(actual.confirm)
    const page = mountHall()
    await flushPromises()

    expect(document.querySelector('.dialog[aria-label="抢单确认"]')).not.toBeNull()
    page.unmount()

    expect(document.querySelector('.dialog[aria-label="抢单确认"]')).toBeNull()
    expect(document.querySelector('.dialog-backdrop.backdrop-in')).toBeNull()
    expect(claimRequestCount()).toBe(0)
  })

  it('通知里的单已经离厅时提示原因，仍可浏览大厅', async () => {
    loginSession()
    routeQuery.claimSource = 'peer'
    routeQuery.claimRef = '42'
    claimTargetFixture = new BizError(1012, '订单已被接走或不在抢单大厅')
    const page = mountHall()
    await flushPromises()

    expect(page.get('[role="status"]').text()).toBe('订单已被接走或不在抢单大厅')
    expect(page.findAll(cardSel)).toHaveLength(2)
    expect(dialogConfirm).not.toHaveBeenCalled()
    expect(claimRequestCount()).toBe(0)
  })

  it('失效通知显示提示并清理标志，保留大厅的其他地址参数', async () => {
    loginSession()
    routeQuery.claimUnavailable = '1'
    routeQuery.game = '101'
    const page = mountHall()
    await flushPromises()

    expect(page.get('[role="status"]').text()).toBe('抢单链接已失效，请在大厅查看新单')
    expect(page.findAll(cardSel)).toHaveLength(2)
    expect(replace).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ query: { game: '101' } }))
    expect(requestMock).not.toHaveBeenCalledWith(expect.objectContaining({ url: '/api/business/hall-claim-target/' }))
    expect(dialogConfirm).not.toHaveBeenCalled()
    expect(claimRequestCount()).toBe(0)
  })

  it('拒绝重复编号参数且不发抢单目标请求', async () => {
    loginSession()
    routeQuery.claimSource = 'peer'
    routeQuery.claimRef = ['42', '43']
    const page = mountHall()
    await flushPromises()

    expect(page.get('[role="status"]').text()).toContain('该抢单链接不可用')
    expect(requestMock).not.toHaveBeenCalledWith(expect.objectContaining({ url: '/api/business/hall-claim-target/' }))
    expect(dialogConfirm).not.toHaveBeenCalled()
  })

  it('回包来源或编号与通知不一致时不展示抢单确认', async () => {
    loginSession()
    routeQuery.claimSource = 'upstream'
    routeQuery.claimRef = '42'
    claimTargetFixture = upstreamRow({ mirror_pk: 43 })
    const page = mountHall()
    await flushPromises()

    expect(page.get('[role="status"]').text()).toContain('该订单暂时无法接单')
    expect(dialogConfirm).not.toHaveBeenCalled()
    expect(claimRequestCount()).toBe(0)
  })

  it('首屏必带 game（=fetchGames 第一款）+ page=1 + page_size=20 + include_exited=1；关键字空不发；并按游戏取区服字典', async () => {
    loginSession()
    mountHall()
    await flushPromises()
    expect(hallCalls).toHaveLength(1)
    // 恰好集合断言：空关键字被 toHallParams 丢掉，不出现 keyword 键；
    // ⭐ D-0296：include_exited 恒带（灰区按离厅时间倒序排在可接区后面）。
    expect(hallParams(0)).toEqual({ game: 101, include_exited: 1, page: 1, page_size: 20 })
    // 区服字典按当前游戏取（原生行的 server 主键映射靠它）。
    expect(
      requestMock.mock.calls.filter(
        (c) => String((c[0] as AxiosRequestConfig).url) === '/api/business/servers/',
      ),
    ).toHaveLength(1)
    expect(requestCallUrl('/api/business/servers/', 0).params).toEqual({
      game: 101,
      page_size: 200,
    })
  })

  it('游戏字典为空 → 整页空态「当前账号没有可接的订单（未分配游戏权限）」+ 退出按钮；零 hall 请求', async () => {
    loginSession()
    gamesData = []
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.text()).toContain('当前账号没有可接的订单（未分配游戏权限）')
    expect(wrapper.text()).toContain('退出')
    expect(hallCalls).toHaveLength(0)
  })

  it('游戏字典失败 → 显示错误本身（不是「未分配权限」那句）+ 零 hall 请求', async () => {
    loginSession()
    requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
      if (String(config.url) === '/api/auth/me') return meData
      throw new HttpError(500, '游戏字典挂了')
    })
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.text()).toContain('游戏字典挂了')
    expect(hallCalls).toHaveLength(0)
  })

  it('卡片字段：游戏名 / 区服（两源合并）/ 项目 / 单价醒目 / 来源徽标 / 加急标', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        {
          items: [
            nativeRow({ order_no: '10000000000000000001', is_urgent: true }),
            upstreamRow(),
          ],
          total: 2,
        },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    const cards = wrapper.findAll(cardSel)
    expect(cards).toHaveLength(2)

    const first = textAt(wrapper, cardSel, 0)
    expect(first).toContain('测试游戏甲')
    expect(first).toContain('测试项目甲')
    expect(first).toContain('测试区服一') // 原生行：server=501 走字典映射
    expect(first).toContain('12.50')
    // ⭐ 业务方裁定：**不显示**「上家 / 本租户」角标（多余的标记）。
    expect(wrapper.find('.m-hall-card__origin').exists()).toBe(false)
    expect(first).not.toContain('本租户')
    expect(first).toContain('加急')

    const second = textAt(wrapper, cardSel, 1)
    expect(second).toContain('测试上家项目甲')
    expect(second).toContain('测试远端区服甲') // upstream 行：直用行内远端文本
    expect(second).toContain('20.00')
    expect(second).not.toContain('加急')
  })

  it('详情显示原生完整单号或镜像展示号，列表卡片不重复展示单号', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow(), upstreamRow()], total: 2 }] }
    const wrapper = mountHall()
    await flushPromises()
    const cards = wrapper.findAll(cardSel)
    expect(cards).toHaveLength(2)
    expect(cards[0]!.text()).not.toContain('10000000000000000001')
    expect(cards[1]!.text()).not.toContain('测试远端展示单号甲')

    await cards[0]!.trigger('click')
    expect(textAt(wrapper, orderLabelSel, 0)).toBe('订单号')
    expect(wrapper.get(orderNoSel).text()).toBe('10000000000000000001')
    await wrapper.get(detailCloseSel).trigger('click')

    await cards[1]!.trigger('click')
    expect(textAt(wrapper, orderLabelSel, 0)).toBe('订单号')
    expect(wrapper.get(orderNoSel).text()).toBe('测试远端展示单号甲')
  })

  it('镜像行：remote_order_view 为空 ⇒ 那一行回落 remote_order_id（与桌面/H5 既有口径一致）', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        {
          items: [upstreamRow({ remote_order_view: '', remote_order_id: 'JP10020030040050060' })],
          total: 1,
        },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(cardSel).trigger('click')
    expect(wrapper.get(orderNoSel).text()).toBe('JP10020030040050060')
  })

  it('★ 点订单号触发复制：写剪贴板的正是**完整单号** + toast「订单号已复制」', async () => {
    const writeText = stubClipboard()
    loginSession()
    hallFixtures = {
      '101': [{ items: [nativeRow({ order_no: '10000000000000000001' })], total: 1 }],
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(cardSel).trigger('click')
    await wrapper.get(orderNoSel).trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(callArgsAt(writeText, 0)[0]).toBe('10000000000000000001')
    expect(toast.success).toHaveBeenCalledWith('订单号已复制')
  })

  it('镜像行点复制：复制的是**上家展示单号**（⛔ 不是空的 order_no）', async () => {
    const writeText = stubClipboard()
    loginSession()
    hallFixtures = { '101': [{ items: [upstreamRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(cardSel).trigger('click')
    await wrapper.get(orderNoSel).trigger('click')
    await flushPromises()
    expect(callArgsAt(writeText, 0)[0]).toBe('测试远端展示单号甲')
  })

  it('http 内网域名（没有 clipboard API）⇒ 降级 execCommand 也能复制成功', async () => {
    stubExecCommand(true)
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(cardSel).trigger('click')
    await wrapper.get(orderNoSel).trigger('click')
    await flushPromises()
    expect(document.execCommand).toHaveBeenCalledWith('copy')
    expect(toast.success).toHaveBeenCalledWith('订单号已复制')
  })

  it('复制两路都失败 ⇒ toast 报错、⛔ 不假装成功', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    stubExecCommand(false)
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(cardSel).trigger('click')
    await wrapper.get(orderNoSel).trigger('click')
    await flushPromises()
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('复制失败，请手动选中复制')
  })

  it('卡片把游戏、加急、优选排成一行，顶部保留价格和抢单，正文独占整行', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow({ is_urgent: true, is_preferred: true })], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    const card = wrapper.get(cardSel)
    const head = card.get('.m-hall-card__head')

    expect(card.classes()).toEqual(expect.arrayContaining(['card', 'card-outline-ios']))
    const badges = head.findAll('.badge')
    expect(badges.map((badge) => badge.text())).toEqual(['测试游戏甲', '加急', '优选'])
    expect(badges.slice(1).every((badge) => badge.element.parentElement === badges[0]!.element.parentElement)).toBe(true)
    expect(head.get(priceSel).text()).toBe('¥12.50')
    expect(head.get(claimSel).text()).toBe('抢单')
    expect(card.element.lastElementChild?.matches('button.m-hall-card__project')).toBe(true)
    expect(card.text()).not.toContain('看全文')
  })

  it('长短卡片高度固定为 144 像素，项目占满卡片宽度、最多四行，抢单热区至少 44 像素高', () => {
    expect(cssRuleBody('.m-hall-card')).toMatch(/\n\s+height: 144px;/)
    const excerptStyle = cssRuleBody('.m-hall-card__excerpt')
    expect(excerptStyle).toContain('white-space: pre-line')
    expect(excerptStyle).toContain('-webkit-line-clamp: 4')
    expect(cssRuleBody('.m-hall-card__claim')).toContain('min-height: 44px')
  })

  it('项目正文保留原文，详情按钮能键盘操作，列表保留独立备注且省略重复备注', async () => {
    loginSession()
    const project = '合成项目示例：完成三个测试目标\n附加要求：保留最后一项说明'
    hallFixtures = {
      '101': [{
        items: [
          nativeRow({ project_text: project, remark: '另约合成时间' }),
          nativeRow({ order_no: '10000000000000000002', project_text: project, remark: ' ' + project }),
        ],
        total: 2,
      }],
    }
    const wrapper = mountHall()
    await flushPromises()
    const [unique, duplicate] = wrapper.findAll('button.m-hall-card__project')

    expect(unique!.text()).toContain(project)
    expect(unique!.text()).toContain('另约合成时间')
    expect(duplicate!.text()).toBe(project)
    await unique!.trigger('click')
    const detail = wrapper.get('[data-testid="hall-detail-panel"]')
    expect(detail.text()).toContain(project)
    expect(detail.text()).toContain('另约合成时间')
  })

  it('详情沿用大厅顶部的标签和价格，滚动区域显示完整项目，抢单独立留在底部', async () => {
    loginSession()
    const longProject = '测试项目甲：这是一段很长的项目描述。\n'.repeat(12) + '还有最后一条要求。'
    hallFixtures = {
      '101': [
        {
          items: [
            nativeRow({
              is_urgent: true,
              is_preferred: true,
              project_text: longProject,
              time_requirement: '测试时间要求甲',
              remark: '测试备注甲',
            }),
          ],
          total: 1,
        },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')

    const panel = wrapper.get('[data-testid="hall-detail-panel"]')
    expect(panel.attributes('aria-label')).toBe('订单详情')
    const heading = panel.get('header')
    expect(heading.findAll('.badge').map((badge) => badge.text())).toEqual(['测试游戏甲', '加急', '优选'])
    expect(heading.get(priceSel).text()).toBe('¥12.50')
    const body = panel.get('.m-hall-detail__body')
    expect(body.text()).toContain(longProject)
    expect(body.text()).toContain('测试区服一')
    expect(body.text()).toContain('测试时间要求甲')
    expect(body.text()).toContain('测试备注甲')
    const claim = panel.get('footer button')
    expect(claim.text()).toBe('抢单')
    expect(body.element.contains(claim.element)).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('打开详情把焦点移到关闭按钮，按 Escape 后还给该订单', async () => {
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    const trigger = wrapper.get<HTMLButtonElement>('.m-hall-card__project')
    trigger.element.focus()
    await trigger.trigger('click')
    await flushPromises()

    const close = wrapper.get(detailCloseSel)
    expect(document.activeElement).toBe(close.element)
    await close.trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
  })

  it('★ 点镜像卡 → 浮层同样打开，单号那行显示订单号（⛔ 不 push 路由）', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [upstreamRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')

    const panel = wrapper.find('[data-testid="hall-detail-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('订单号')
    expect(wrapper.find('[data-testid="hall-detail-order-no"]').text()).toBe('测试远端展示单号甲')
    expect(push).not.toHaveBeenCalled()
  })

  it('★ 浮层里点「抢单」→ 复用抢单确认（不跳详情），收起浮层', async () => {
    loginSession()
    hallFixtures = {
      '101': [{ items: [nativeRow({ order_no: '10000000000000000001' })], total: 1 }],
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')
    await wrapper.find('[data-testid="hall-detail-claim"]').trigger('click')

    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    expect(push).not.toHaveBeenCalled()
    // 确认弹层不能压在浮层下面 ⇒ 先收起浮层
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(false)
  })

  it('从详情复制单号不会关闭浮层，卡片抢单不会误开详情', async () => {
    stubClipboard()
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()

    await wrapper.get(cardSel).trigger('click')
    await wrapper.get(orderNoSel).trigger('click')
    await flushPromises()
    expect(toast.success).toHaveBeenCalledWith('订单号已复制')
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(true)
    await wrapper.get(detailCloseSel).trigger('click')

    await wrapper.get(claimSel).trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('★ 详情只走页内浮层：源码里不再有跳详情页的调用（⛔ 未接单必 403 的那条死路）', () => {
    // 比 spy 更硬的回归钉子：`push` 今天没有任何调用点，光 spy 只会恒绿；
    // 这里直接按源码判「页面里不再出现详情路由 / 跳转调用」。
    expect(HALL_SFC).not.toContain('m-order-detail')
    expect(HALL_SFC).not.toContain('navigateTo')
    expect(HALL_SFC).not.toContain('router.push')
  })

  it('浮层「关闭」按钮 / 点遮罩都能收起（回到列表）', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [nativeRow()], total: 1 }] }
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()

    await wrapper.find(cardSel).trigger('click')
    await wrapper.find('[data-testid="hall-detail-close"]').trigger('click')
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(false)

    await wrapper.find(cardSel).trigger('click')
    await wrapper.get('.popup-backdrop.backdrop-in').trigger('click') // 遮罩（面板外）
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(false)
  })

  it('抢单成功（原生路）：确认文案含单价与游戏名；POST /hall/<单号>/claim/；卡片移除；toast「抢单成功」+ 我的订单提示', async () => {
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')

    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    const opts = dialogOptsAt()
    expect(opts.content).toContain('测试游戏甲')
    expect(opts.content).toContain('12.50')

    opts.onConfirm()
    await flushPromises()
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'post',
        url: '/api/business/hall/10000000000000000001/claim/',
      }),
    )
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(textAt(wrapper, cardSel, 0)).not.toContain('10000000000000000001')
    expect(toast.success).toHaveBeenCalledWith('抢单成功')
    expect(wrapper.find('[data-testid="claim-hint"]').text()).toContain('我的订单')
  })

  it('抢单成功但后端 msg 带「未能撤回」警告 ⇒ 原文 toast.warning（停留更久）+ 刷新（⛔ 不吞）', async () => {
    const WARNING =
      '代练通订单未能撤回（原话：可操作资金不足，不能发布.），' +
      '请先到「第三方平台订单管理」撤回代练通平台订单'
    loginSession()
    vi.mocked(requestEnvelope).mockResolvedValue({ code: 0, msg: WARNING, data: {} } as never)
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()
    // 原文 + 警告主题 + 长停留；⛔ 不再只剩一句固定的「抢单成功」。
    expect(toast.warning).toHaveBeenCalledWith({ message: WARNING, duration: 8000 })
    expect(toast.success).not.toHaveBeenCalledWith('抢单成功')
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
  })

  it('确认后请求仍在等待时打开官方抢单加载弹窗，请求结束后收起', async () => {
    useFakeTimersForHall()
    loginSession()
    let finishClaim: (() => void) | undefined
    claimImpl = () => new Promise((resolve) => { finishClaim = () => resolve({}) })
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.get(claimSel).trigger('click')
    dialogOptsAt().onConfirm()

    const activePreloader = '.dialog-preloader.modal-in'
    expect(document.querySelector(activePreloader)).toBeNull()
    await vi.advanceTimersByTimeAsync(500)
    const dialog = document.querySelector(activePreloader)
    expect(dialog?.textContent).toContain('正在抢单…')
    expect(dialog?.querySelector('.preloader')).not.toBeNull()

    finishClaim?.()
    await flushPromises()
    expect(document.querySelector(activePreloader)).toBeNull()
    expect(toast.success).toHaveBeenCalledWith('抢单成功')
  })

  it('抢单成功（上家镜像路）：mirror_pk=5 → POST /hall/upstream/5/claim/（判据打在调用参数上）', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [upstreamRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'post',
        url: '/api/business/hall/upstream/5/claim/',
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('抢单成功')
    expect(wrapper.findAll(cardSel)).toHaveLength(0)
  })

  it('claim 失败 1012：toast = 后端 msg 原样 + 自动重取当前已加载列表（两页就重取两页）', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        { items: nativeRows(20, 10000000000000000001n), total: 45 },
        { items: nativeRows(20, 10000000000000000021n), total: 45 },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    // 先装满两页（20 + 20）。
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(hallCalls).toHaveLength(2)

    claimImpl = async () => {
      throw new BizError(1012, '该订单已被别人接走')
    }
    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('该订单已被别人接走')
    // 1012 的自动重取：按**已加载页数**逐页重取（第 3、4 发 = page 1、page 2）。
    expect(hallCalls).toHaveLength(4)
    expect(hallParams(2)).toMatchObject({ page: 1 })
    expect(hallParams(3)).toMatchObject({ page: 2 })
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('claim 失败非 1012（403）：toast msg 原样；不触发重取', async () => {
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    claimImpl = async () => {
      throw new HttpError(403, '没有接单权限')
    }
    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('没有接单权限')
    expect(hallCalls).toHaveLength(1) // 非 1012 不重取
  })

  it('claiming 并发闸：claim 在途再点不开新确认、不发第二条请求；结束后复位', async () => {
    loginSession()
    let resolveClaim: (v: unknown) => void = () => {}
    claimImpl = () => new Promise((res) => { resolveClaim = res })
    const wrapper = mountHall()
    await flushPromises()

    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()
    expect(wrapper.find(claimSel).attributes('disabled')).toBeDefined()

    // 在途再点：早返回闸 ⇒ 不开新弹层、不发第二条。
    await wrapper.find(claimSel).trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(1)

    resolveClaim({ order_no: '10000000000000000001' })
    await flushPromises()
    expect(claimRequestCount()).toBe(1)
    expect(wrapper.find(claimSel).attributes('disabled')).toBeUndefined()
  })

  it('无权限（me 缺 grab_hall.take_order）→ 抢单按钮不渲染（恰好 0 颗；卡片照常渲染）', async () => {
    loginSession({ menu_permissions: ['my_orders'] })
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(2)
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
  })

  it('取不到 me（user=null）→ 抢单按钮不渲染（fail-closed，恰好 0 颗）', async () => {
    loginSession({ menu_permissions: null })
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(2)
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
  })

  it('切换游戏：清空已加载列表并按新游戏重取 page=1；区服字典随游戏重取', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        { items: nativeRows(20, 10000000000000000001n), total: 45 },
        { items: nativeRows(20, 10000000000000000021n), total: 45 },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(40)

    const gameTrigger = wrapper.get('[data-testid="hall-game-filter"]')
    expect(gameTrigger.text()).toBe('游戏')
    const search = wrapper.get('.m-hall__toolbar .searchbar').element
    expect(search.compareDocumentPosition(gameTrigger.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(gameTrigger.element.compareDocumentPosition(wrapper.get('[data-testid="hall-more-open"]').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const games = await openOptions(wrapper, 'hall-game-filter')
    expect(games.get('[data-option-value="101"]').attributes('aria-pressed')).toBe('true')
    await games.get('[data-option-value="102"]').trigger('click')
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
    expect(hallParams(2)).toEqual({ game: 102, include_exited: 1, page: 1, page_size: 20 })
    // 已加载列表被清空后重取：只剩 page=1 夹具的 20 张。
    expect(wrapper.findAll(cardSel)).toHaveLength(20)
    expect(
      requestMock.mock.calls.filter(
        (c) => String((c[0] as AxiosRequestConfig).url) === '/api/business/servers/',
      ),
    ).toHaveLength(2) // 首屏 101 + 切换后 102
    expect(requestCallUrl('/api/business/servers/', 1).params).toEqual({
      game: 102,
      page_size: 200,
    })
  })

  it('切换游戏后可以刷新并继续翻页，旧游戏的翻页结果不会关闭新游戏的加载提示', async () => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = {
      101: [
        { items: nativeRows(20, 10000000000000000001n), total: 21 },
        { items: [nativeRow({ order_no: '10000000000000000021', project_text: '旧游戏的下一页' })], total: 21 },
      ],
      102: [
        { items: nativeRows(20, 10000000000000000101n).map((row) => ({ ...row, game: 102 })), total: 21 },
        { items: [nativeRow({ game: 102, order_no: '10000000000000000121', project_text: '新游戏的下一页' })], total: 21 },
      ],
    }
    const wrapper = mountHall()
    const more = '.m-paged-list__more'
    const loading = '.m-paged-list__loading'
    await flushPromises()

    holdNextHall = true
    await wrapper.get(more).trigger('click')
    const releaseOldPage = releaseHeldHall
    expect(releaseOldPage).toBeTypeOf('function')
    expect(hallParams(1)).toMatchObject({ game: 101, page: 2 })

    const games = await openOptions(wrapper, 'hall-game-filter')
    await games.get('[data-option-value="102"]').trigger('click')
    await flushPromises()
    expect(hallParams(2)).toMatchObject({ game: 102, page: 1 })
    expect(wrapper.find(more).exists()).toBe(true)

    await vi.advanceTimersByTimeAsync(5000)
    expect(hallParams(3)).toMatchObject({ game: 102, page: 1 })
    holdNextHall = true
    await wrapper.get(more).trigger('click')
    expect(hallParams(4)).toMatchObject({ game: 102, page: 2 })
    expect(wrapper.find(loading).exists()).toBe(true)

    releaseOldPage?.()
    await flushPromises()
    expect(wrapper.find(loading).exists()).toBe(true)
    expect(wrapper.findAll(cardSel)).toHaveLength(20)

    releaseHeldHall?.()
    await flushPromises()
    expect(wrapper.find(loading).exists()).toBe(false)
    expect(wrapper.findAll(cardSel)).toHaveLength(21)
    expect(textAt(wrapper, cardSel, 20)).toContain('新游戏的下一页')
  })

  it('游戏浮层贴着按钮，支持按 Esc、移出焦点、滚动外部和再次点击关闭', async () => {
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    const trigger = wrapper.get<HTMLButtonElement>('[data-testid="hall-game-filter"]')
    const frames: FrameRequestCallback[] = []
    const schedule = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => frames.push(callback))
    function finishPositioning(): void {
      for (const callback of frames.splice(0)) callback(performance.now())
    }

    try {
      trigger.element.focus()
      const panel = await openOptions(wrapper, 'hall-game-filter')
      const popover = panel.element.closest<HTMLElement>('.popover')!
      expect(popover).not.toBeNull()
      expect(panel.attributes('aria-modal')).toBeUndefined()
      expect(f7.popover.get(popover)?.targetEl).toBe(trigger.element)
      expect(document.querySelector('.popover-backdrop.backdrop-in')).toBeNull()

      await trigger.trigger('keydown', { key: 'Escape' })
      finishPositioning()
      expect(trigger.attributes('aria-expanded')).toBe('false')
      expect(document.activeElement).toBe(trigger.element)
      expect(popover.classList.contains('modal-in')).toBe(false)

      await openOptions(wrapper, 'hall-game-filter')
      finishPositioning()
      expect(trigger.attributes('aria-expanded')).toBe('true')
      const currentGame = panel.get('[data-option-value="101"]')
      expect(document.activeElement).toBe(currentGame.element)

      const outside = wrapper.get<HTMLInputElement>('input[type="search"]')
      outside.element.focus()
      await flushPromises()
      expect(trigger.attributes('aria-expanded')).toBe('false')
      expect(document.activeElement).toBe(outside.element)

      await openOptions(wrapper, 'hall-game-filter')
      finishPositioning()
      await panel.get('.list').trigger('scroll')
      expect(trigger.attributes('aria-expanded')).toBe('true')
      await wrapper.trigger('scroll')
      expect(trigger.attributes('aria-expanded')).toBe('false')

      await openOptions(wrapper, 'hall-game-filter')
      finishPositioning()
      await outside.trigger(f7.touchEvents.start)
      await outside.trigger('click')
      expect(trigger.attributes('aria-expanded')).toBe('false')
      expect(document.documentElement.classList.contains('with-modal-popover')).toBe(false)

      await openOptions(wrapper, 'hall-game-filter')
      finishPositioning()
      await trigger.trigger('pointerdown')
      if (f7.touchEvents.start !== 'pointerdown') await trigger.trigger(f7.touchEvents.start)
      trigger.element.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
      await flushPromises()
      expect(trigger.attributes('aria-expanded')).toBe('false')
    } finally {
      schedule.mockRestore()
    }
  })

  it('T2 评审 Important-1：切游戏撞上在途重取——新游戏的请求立即发出，旧响应落地被代数守卫丢弃', async () => {
    loginSession()
    hallFixtures = {
      '101': [{ items: nativeRows(2, 10000000000000000001n), total: 2 }],
      '102': [
        {
          items: [
            nativeRow({ order_no: '10000000000000000003', game: 102, project_text: '游戏乙的独有单' }),
          ],
          total: 1,
        },
      ],
    }
    holdNextHall = true // 首屏那发（game=101）挂在途
    const wrapper = mountHall()
    await flushPromises()
    expect(hallCalls).toHaveLength(1)
    expect(wrapper.get('[data-testid="hall-orders-loading"][role="status"]').text()).toBe('正在加载订单…')
    expect(wrapper.text()).not.toContain('暂无待抢订单')

    // 修复点：重取在途时切游戏，新意图**不被早退吞掉**——game=102 的请求当场发出。
    await selectOption(wrapper, 'hall-game-filter', '102')
    await flushPromises()
    expect(hallCalls).toHaveLength(2)
    expect(hallParams(1)).toEqual({ game: 102, include_exited: 1, page: 1, page_size: 20 })
    // 第 2 发没挂 → 先落地渲染 102 的数据。
    expect(textAt(wrapper, cardSel, 0)).toContain('游戏乙的独有单')
    expect(wrapper.find('[data-testid="hall-orders-loading"]').exists()).toBe(false)

    // 放行首屏那发（旧游戏、旧代数）：落地必须被整体丢弃，⛔ 不许把 101 的列表盖回去。
    releaseHeldHall?.()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(textAt(wrapper, cardSel, 0)).toContain('游戏乙的独有单')
  })

  it('T2 评审 Minor-1：自动刷新在途时手动下拉——下拉的重取立即发出并最终生效，旧发被代数守卫丢弃', async () => {
    useFakeTimersForHall()
    vi.setSystemTime(new Date('2026-10-10T02:04:00Z'))
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = { 101: [
      { items: nativeRows(20, 10000000000000000001n), total: 21 },
      { items: nativeRows(1, 10000000000000000021n), total: 21 },
    ] }
    const wrapper = mountHall()
    await flushPromises()
    expect(hallCalls).toHaveLength(1)

    holdNextHall = true
    await vi.advanceTimersByTimeAsync(5000) // 自动刷新那发 → 挂在途
    await flushPromises()
    expect(hallCalls).toHaveLength(2)
    expect(wrapper.find('[data-testid="hall-orders-loading"]').exists()).toBe(false)
    expect(wrapper.get('.m-hall__auto-status').text()).toContain('正在刷新')
    await wrapper.get('.m-paged-list__more').trigger('click')

    // 自动刷新没回来时就下拉：pull 的重取当场发出（修复点：不被 refreshLoaded 早退吞掉）。
    const freshRows = nativeRows(20, 10000000000000000001n)
    freshRows[0]!.project_text = '下拉拿到的最新单'
    hallFixtures['101']![0] = { items: freshRows, total: 21 }
    wrapper.getComponent(PullDownRefresh).vm.$emit('refresh')
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
    expect(hallParams(2)).toMatchObject({ page: 1 })
    // 第 3 发先落地：渲染的是下拉拿到的最新数据。
    expect(textAt(wrapper, cardSel, 0)).toContain('下拉拿到的最新单')

    expect(wrapper.get('.m-hall__auto-status time').text()).toBe('10:04:05')
    expect(wrapper.get('.m-hall__auto-status').text()).not.toContain('正在刷新')

    // 放行自动刷新那发（更老的代数）：列表和刷新时间都以已应用的下拉结果为准。
    await vi.advanceTimersByTimeAsync(1000)
    releaseHeldHall?.()
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
    expect(wrapper.findAll(cardSel)).toHaveLength(20)
    expect(wrapper.get('.m-hall__auto-status time').text()).toBe('10:04:05')
    expect(textAt(wrapper, cardSel, 0)).toContain('下拉拿到的最新单')
  })

  it('关键字 300ms 防抖：输入不立即发；到 300ms 提交并重取 page=1；继续输入重置计时', async () => {
    useFakeTimersForHall()
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    expect(hallCalls).toHaveLength(1)

    await wrapper.get('input[placeholder="搜索订单"]').setValue('抓鬼')
    await flushPromises()
    expect(hallCalls).toHaveLength(1) // 防抖期内不发

    await vi.advanceTimersByTimeAsync(299)
    await flushPromises()
    expect(hallCalls).toHaveLength(1) // 差 1ms 不发

    holdNextHall = true
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(hallCalls).toHaveLength(2)
    expect(hallParams(1)).toEqual({ game: 101, include_exited: 1, keyword: '抓鬼', page: 1, page_size: 20 })
    expect(wrapper.get('[data-testid="hall-orders-loading"]').find('.preloader').exists()).toBe(true)
    releaseHeldHall?.()
    await flushPromises()
    expect(wrapper.find('[data-testid="hall-orders-loading"]').exists()).toBe(false)

    // 继续输入 ⇒ 重置计时（不叠加两发）。
    await wrapper.get('input[placeholder="搜索订单"]').setValue('抓鬼2')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(200)
    await flushPromises()
    expect(hallCalls).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(100)
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
    expect(hallParams(2)).toEqual({ game: 101, include_exited: 1, keyword: '抓鬼2', page: 1, page_size: 20 })
  })

  it('上拉加载更多：追加下一页；失败保留已有内容并给重试；重试成功续上；取完不再发', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        { items: nativeRows(20, 10000000000000000001n), total: 45 },
        { items: nativeRows(20, 10000000000000000021n), total: 45 },
        { items: nativeRows(5, 10000000000000000041n), total: 45 },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(20)

    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(40)
    expect(hallParams(1)).toMatchObject({ page: 2 })

    hallFailNext = new Error('加载挂了')
    await wrapper.find('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('加载挂了')
    expect(wrapper.findAll(cardSel)).toHaveLength(40) // 已有内容不清空

    await wrapper.find('.m-hall__retry-btn').trigger('click')
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(45)
    expect(wrapper.text()).not.toContain('加载挂了')

    // 取完（items 45 == total 45）：再点加载不发请求。
    const before = hallCalls.length
    expect(wrapper.find('.m-paged-list__more').exists()).toBe(false)
    wrapper.getComponent(List).vm.$emit('load-more')
    await flushPromises()
    expect(hallCalls).toHaveLength(before)
    expect(wrapper.text()).toContain('没有更多了')
  })

  it('加载下一页期间自动刷新会让路，下一页仍能显示', async () => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = { 101: [
      { items: nativeRows(20, 10000000000000000001n), total: 21 },
      { items: nativeRows(1, 10000000000000000021n), total: 21 },
    ] }
    const wrapper = mountHall()
    await flushPromises()

    holdNextHall = true
    await wrapper.get('.m-paged-list__more').trigger('click')
    expect(hallParams(1)).toMatchObject({ page: 2 })
    await vi.advanceTimersByTimeAsync(5000)
    expect(hallCalls).toHaveLength(2)

    releaseHeldHall?.()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(21)
    expect(wrapper.find('.m-paged-list__more').exists()).toBe(false)
  })

  it.each(['成功', '失败'])('自动刷新%s后接着加载用户点击的下一页，连点也只加载一次', async (outcome) => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = { 101: [
      { items: nativeRows(20, 10000000000000000001n), total: 21 },
      { items: nativeRows(1, 10000000000000000021n), total: 21 },
    ] }
    const wrapper = mountHall()
    await flushPromises()

    holdNextHall = true
    if (outcome === '失败') hallFailNext = new Error('自动刷新失败')
    await vi.advanceTimersByTimeAsync(5000)
    expect(hallParams(1)).toMatchObject({ page: 1 })
    const more = wrapper.get('.m-paged-list__more')
    await more.trigger('click')
    await more.trigger('click')
    expect(hallCalls).toHaveLength(2)

    releaseHeldHall?.()
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
    expect(hallParams(2)).toMatchObject({ page: 2 })
    expect(wrapper.findAll(cardSel)).toHaveLength(21)
    expect(wrapper.find('.m-paged-list__more').exists()).toBe(false)
  })

  it('下拉刷新：回第 1 页重取并整体替换；抢单提示条随重取清掉', async () => {
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()
    expect(wrapper.find('[data-testid="claim-hint"]').exists()).toBe(true)

    // 服务器侧这单已被抢走：现在只剩 1 张（卡片不渲染单号，用项目文本区分）。
    hallFixtures = {
      '101': [{ items: [nativeRow({ order_no: '10000000000000000002', project_text: '测试项目乙' })], total: 1 }],
    }
    wrapper.getComponent(PullDownRefresh).vm.$emit('refresh')
    await flushPromises()
    expect(hallParams(1)).toMatchObject({ page: 1 })
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(textAt(wrapper, cardSel, 0)).toContain('测试项目乙')
    expect(wrapper.find('[data-testid="claim-hint"]').exists()).toBe(false)
  })

  it('自动刷新：按存储档位建定时器到点重取；页面隐藏跳过这一拍；变可见立刻补刷；卸载清定时器', async () => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    expect(hallCalls).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(hallCalls).toHaveLength(2)
    expect(hallParams(1)).toMatchObject({ page: 1 })

    // 页面隐藏 ⇒ 跳过这一拍（桌面 onAutoRefreshTick 的第①道闸，:950）。
    setHidden(true)
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(hallCalls).toHaveLength(2)

    // 变可见 ⇒ 立刻补刷一次并重置计时（桌面 onVisibilityChange，:978-984）。
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(hallCalls).toHaveLength(3)

    // 卸载 ⇒ 定时器清掉：再推 10 秒也不发（桌面卸载清理 :991-999 的移植）。
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
  })

  it('自动刷新选择间隔前不启用，调档后按新间隔重取，关闭开关后停止', async () => {
    useFakeTimersForHall()
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    expect(hallCalls).toHaveLength(1) // 默认关闭：推 60 秒也不发
    await vi.advanceTimersByTimeAsync(60000)
    await flushPromises()
    expect(hallCalls).toHaveLength(1)

    const canceled = await openAutoRefreshIntervals(wrapper)
    await canceled.get('[data-testid="hall-refresh-interval-cancel"]').trigger('click')
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBeNull()

    const initial = await openAutoRefreshIntervals(wrapper)
    await initial.get('[data-refresh-interval="10000"]').trigger('click')
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBe('10000')
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(hallCalls).toHaveLength(2)

    const intervals = await openAutoRefreshIntervals(wrapper)
    expect(intervals.get('[data-refresh-interval="10000"]').attributes('aria-pressed')).toBe('true')
    await intervals.get('[data-refresh-interval="30000"]').trigger('click')
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBe('30000')
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(hallCalls).toHaveLength(3)

    const more = await openHallMore(wrapper)
    const toggle = more.get<HTMLInputElement>('[role="switch"]')
    expect(toggle.element.checked).toBe(true)
    toggle.element.click()
    await flushPromises()
    expect(toggle.element.checked).toBe(false)
    expect(more.find('[data-testid="hall-refresh-interval-open"]').exists()).toBe(false)
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBe('0')
    await vi.advanceTimersByTimeAsync(60000)
    await flushPromises()
    expect(hallCalls).toHaveLength(3)
  })

  it('首次加载失败后，自动刷新成功会清除旧报错并展示最新订单', async () => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    hallFailNext = new Error('首次加载失败')
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.get('.m-hall__retry').text()).toContain('首次加载失败')

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.find('.m-hall__retry').exists()).toBe(false)
    expect(wrapper.find('.m-hall__auto-status time').exists()).toBe(true)
    expect(wrapper.findAll(cardSel).length).toBeGreaterThan(0)
  })

  it.each([
    {
      source: '原生单',
      order: nativeRow({ project_text: '待接项目' }),
      other: nativeRow({ order_no: '10000000000000000002', project_text: '同页项目' }),
      claimUrl: '/api/business/hall/10000000000000000001/claim/',
    },
    {
      source: '上家镜像单',
      order: upstreamRow({ project_text: '待接项目' }),
      other: upstreamRow({ mirror_pk: 6, project_text: '同页项目' }),
      claimUrl: '/api/business/hall/upstream/5/claim/',
    },
  ])('抢到$source后，晚到的自动刷新不会恢复旧卡，后续重新进厅仍可显示', async ({ order, other, claimUrl }) => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = { 101: [{ items: [order, other], total: 2 }] }
    const wrapper = mountHall()
    await flushPromises()

    hallFixtures = { 101: [{ items: [order, { ...other, project_text: '同页项目更新' }], total: 2 }] }
    holdNextHall = true
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.get('.m-hall__auto-status').text()).toContain('正在刷新')

    await wrapper.findAll(claimSel)[0]!.trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()
    expect(requestCallUrl(claimUrl, 0).method).toBe('post')
    expect(toast.success).toHaveBeenCalledWith('抢单成功')
    expect(wrapper.findAll(cardSel)).toHaveLength(1)

    releaseHeldHall?.()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(wrapper.get(cardSel).text()).toContain('同页项目更新')
    expect(wrapper.get('.m-hall__auto-status').text()).not.toContain('正在刷新')
    expect(wrapper.find('.m-paged-list__more').exists()).toBe(false)

    hallFixtures = { 101: [{ items: [{ ...order, project_text: '重新开放的项目' }, other], total: 2 }] }
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.findAll(claimSel)).toHaveLength(2)
    expect(textAt(wrapper, cardSel, 0)).toContain('重新开放的项目')
  })

  it('自动刷新成功后彩带只扫过可见订单，卡片依次淡入且仍可抢单', async () => {
    useFakeTimersForHall()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    hallFixtures = { 101: [{ items: nativeRows(4, 10000000000000000001n), total: 4 }] }
    const page = document.createElement('div')
    page.className = 'page'
    const scroll = document.createElement('main')
    scroll.className = 'm-shell__scroll'
    const toolbar = document.createElement('nav')
    toolbar.className = 'm-tabbar'
    const toastEl = document.createElement('div')
    toastEl.className = 'toast modal-in'
    page.append(scroll, toolbar, toastEl)
    document.getElementById('framework7-root')!.append(page)
    const animate = vi.fn(function (this: Element, _frames: Keyframe[], _options: KeyframeAnimationOptions) {
      return { cancel: vi.fn() }
    })
    const previousAnimate = Object.getOwnPropertyDescriptor(Element.prototype, 'animate')
    Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate })
    const wrapper = mount(HallPage, { attachTo: scroll })
    try {
      await flushPromises()
      const cards = wrapper.findAll(cardSel).map(({ element }) => element)
      const list = wrapper.getComponent(List).element
      vi.spyOn(scroll, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 80, 400, 300))
      vi.spyOn(toolbar, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 360, 400, 20))
      vi.spyOn(list, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, -80, 400, 600))
      cards.forEach((element, index) => {
        vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, -80 + index * 152, 400, 144))
      })
      scroll.scrollTop = 200
      const sweep = '[data-testid="hall-refresh-sweep"]'
      expect(wrapper.find(sweep).exists()).toBe(false)

      hallFailNext = new Error('网络失败')
      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()

      holdNextHall = true
      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()
      releaseHeldHall?.()
      await flushPromises()

      expect(wrapper.findAll(sweep)).toHaveLength(1)
      expect(animate.mock.contexts).toEqual(cards.slice(1, 3))
      const delays = animate.mock.calls.map(([, options]) => options.delay)
      expect(delays).toEqual([95, 195])
      expect(animate.mock.calls[0]?.[0]).toEqual([{ opacity: 1 }, { opacity: 0.52, offset: 0.25 }, { opacity: 1 }])
      expect(wrapper.findAll(cardSel).map(({ element }) => element)).toEqual(cards)
      expect(scroll.scrollTop).toBe(200)
      await wrapper.findAll(claimSel)[1]!.trigger('click')
      expect(dialogConfirm).toHaveBeenCalledTimes(1)

      await wrapper.get(`${sweep} > div`).trigger('animationend')
      expect(wrapper.find(sweep).exists()).toBe(false)
      animate.mockClear()

      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.find(sweep).exists()).toBe(true)
      scroll.dispatchEvent(new CustomEvent('ptr:pullstart', { bubbles: true }))
      await flushPromises()
      expect(wrapper.find(sweep).exists()).toBe(false)
      for (const { value } of animate.mock.results) expect(value.cancel).toHaveBeenCalledOnce()
      animate.mockClear()

      scroll.classList.add('ptr-pull-down')
      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()
      scroll.classList.remove('ptr-pull-down')

      // 手动下拉先回来，晚到的自动刷新也不应再触发动效。
      holdNextHall = true
      await vi.advanceTimersByTimeAsync(5000)
      const updatedRows = nativeRows(4, 10000000000000000001n)
      updatedRows[1]!.project_text = '下拉拿到的最新项目'
      hallFixtures = { 101: [{ items: updatedRows, total: 4 }] }
      wrapper.getComponent(PullDownRefresh).vm.$emit('refresh')
      await flushPromises()
      expect(textAt(wrapper, cardSel, 1)).toContain('下拉拿到的最新项目')
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()
      releaseHeldHall?.()
      await flushPromises()
      expect(textAt(wrapper, cardSel, 1)).toContain('下拉拿到的最新项目')
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()

      hallFixtures = { 101: [{ items: [], total: 0 }] }
      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.get('.m-hall__no-results').text()).toContain('暂无待抢订单')
      expect(wrapper.find(sweep).exists()).toBe(false)
      expect(animate).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      page.remove()
      if (previousAnimate) Object.defineProperty(Element.prototype, 'animate', previousAnimate)
      else Reflect.deleteProperty(Element.prototype, 'animate')
    }
  })

  it('自动刷新显示请求在途、失败和上次成功时间，并保留加载中的原有卡片', async () => {
    useFakeTimersForHall()
    vi.setSystemTime(new Date('2026-10-10T02:04:00Z'))
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '5000')
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    const firstCard = wrapper.get(cardSel).element
    const status = () => wrapper.get('.m-hall__auto-status')

    expect(status().get('time').text()).toBe('10:04:00')
    hallFailNext = new Error('合成网络失败')
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(status().text()).toContain('刷新失败')
    expect(status().get('time').text()).toBe('10:04:00')
    expect(wrapper.get(cardSel).element).toBe(firstCard)

    holdNextHall = true
    await vi.advanceTimersByTimeAsync(5000)
    expect(status().text()).toContain('正在刷新')
    expect(status().find('.preloader').exists()).toBe(true)
    expect(status().get('time').text()).toBe('10:04:00')
    expect(wrapper.get(cardSel).element).toBe(firstCard)

    await vi.advanceTimersByTimeAsync(1000)
    releaseHeldHall?.()
    await flushPromises()
    expect(status().text()).not.toMatch(/正在刷新|刷新失败/)
    expect(status().get('time').text()).toBe('10:04:11')
    expect(wrapper.get(cardSel).element).toBe(firstCard)
  })

  it('空大厅后台轮询时保留现有提示，且不显示前台加载状态', async () => {
    useFakeTimersForHall()
    loginSession()
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '10000')
    hallFixtures = { 101: [{ items: [], total: 0 }] }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.get('.m-hall__no-results').text()).toContain('暂无待抢订单')

    holdNextHall = true
    await vi.advanceTimersByTimeAsync(10000)
    expect(hallCalls).toHaveLength(2)
    expect(wrapper.get('.m-hall__no-results').text()).toContain('暂无待抢订单')
    expect(wrapper.find('[data-testid="hall-orders-loading"]').exists()).toBe(false)
    releaseHeldHall?.()
    await flushPromises()
  })

  it('List 组件拿到 asyncLoading 状态：加载中 loading / 有更多 load-more / 取完不再是可点状态', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        { items: nativeRows(2, 10000000000000000001n), total: 45 },
        { items: nativeRows(2, 10000000000000000003n), total: 45 },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.getComponent(List).props('asyncLoading')).toBe('load-more')

    wrapper.getComponent(List).vm.$emit('load-more')
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(4)
    // items(4) < total(45)，非加载中 ⇒ 仍可加载更多。
    expect(wrapper.getComponent(List).props('asyncLoading')).toBe('load-more')
  })

  // ── D-0296：优选 toggle / 优选角标 / 灰卡 / 模拟单 1012 / 详情浮层参考图 ──

  it('保存当前游戏的价格和项目关键词，分页与下拉刷新沿用筛选', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        { items: [
          nativeRow({ order_no: '10000000000000000001', project_text: '普通 ABC' }),
          nativeRow({ order_no: '10000000000000000002', project_text: '急单 ABC 排除词' }),
          nativeRow({ order_no: '10000000000000000003', is_exited: true, project_text: '已被抢的历史单' }),
        ], total: 21 },
        { items: [nativeRow({ order_no: '10000000000000000004', project_text: '另一个匹配' })], total: 21 },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.find('[data-testid="hall-preferred-toggle"]').exists()).toBe(false)
    await openHallFilters(wrapper)
    const sheet = wrapper.get('[data-testid="hall-filter-form"]')
    await sheet.get('[name="price-min"]').setValue('0')
    await sheet.get('[name="price-max"]').setValue('123.45')
    await sheet.get('[name="keyword-include"]').setValue('abc、匹配')
    await sheet.get('[name="keyword-exclude"]').setValue('排除词')
    await sheet.trigger('submit')
    await flushPromises()

    expect(hallParams(1)).toEqual({
      game: 101, page: 1, page_size: 20, include_exited: 1, price_min: '0', price_max: '123.45',
    })
    expect(wrapper.findAll(cardSel).map((card) => card.find('.m-hall-card__project').text()))
      .toEqual(['普通 ABC', '已被抢的历史单'])
    await wrapper.get('.m-paged-list__more').trigger('click')
    await flushPromises()
    expect(hallParams(2)).toMatchObject({ page: 2, price_min: '0', price_max: '123.45' })
    expect(wrapper.text()).toContain('另一个匹配')
    wrapper.getComponent(PullDownRefresh).vm.$emit('refresh')
    await flushPromises()
    expect(hallParams(3)).toMatchObject({ page: 1, price_min: '0', price_max: '123.45' })
  })

  it('Framework7 下拉刷新实例请求当前大厅的第一页', async () => {
    loginSession()
    const page = document.createElement('div')
    page.className = 'page m-shell'
    const scroll = document.createElement('main')
    scroll.className = 'm-shell__scroll'
    page.append(scroll)
    document.getElementById('framework7-root')!.append(page)
    const wrapper = mount(HallPage, { attachTo: scroll })
    try {
      await flushPromises()
      const refresh = f7.ptr.get(scroll)
      if (!refresh) throw new Error('大厅滚动区域未初始化 Framework7 下拉刷新')
      refresh.refresh()
      await flushPromises()
      expect(hallParams(1)).toMatchObject({ game: 101, page: 1 })
    } finally {
      wrapper.unmount()
      page.remove()
    }
  })

  it('关键词立即筛选现有订单，重取失败时仍保留匹配订单', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [
      nativeRow({ order_no: '10000000000000000001', project_text: '训练 ABC' }),
      nativeRow({ order_no: '10000000000000000002', project_text: '日常任务' }),
    ], total: 2 }] }
    const wrapper = mountHall()
    await flushPromises()
    await openHallFilters(wrapper)
    const sheet = wrapper.get('[data-testid="hall-filter-form"]')
    await sheet.get('[name="keyword-include"]').setValue('abc')
    hallFailNext = new Error('暂时无法刷新')
    await sheet.trigger('submit')
    await flushPromises()

    expect(wrapper.findAll(cardSel).map((card) => card.get('.m-hall-card__project').text()))
      .toEqual(['训练 ABC'])
    expect(wrapper.get('.m-hall__retry').text()).toContain('暂时无法刷新')
    await wrapper.get('.m-hall__retry-btn').trigger('click')
    await flushPromises()
    expect(hallParams(2)).toMatchObject({ page: 1 })
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
  })

  it('保存的价格区间按游戏、租户和账号隔离，重新进入时恢复', async () => {
    loginSession()
    const wrapper = mountHall()
    await flushPromises()
    await openHallFilters(wrapper)
    const sheet = wrapper.get('[data-testid="hall-filter-form"]')
    await sheet.get('[name="price-min"]').setValue('100')
    await sheet.trigger('submit')
    await flushPromises()
    expect(hallParams(1)).toMatchObject({ game: 101, price_min: '100' })

    await selectOption(wrapper, 'hall-game-filter', '102')
    await flushPromises()
    expect(hallParams(2)).toEqual({ game: 102, page: 1, page_size: 20, include_exited: 1 })
    await selectOption(wrapper, 'hall-game-filter', '101')
    await flushPromises()
    expect(hallParams(3)).toMatchObject({ game: 101, price_min: '100' })
    wrapper.unmount()

    const reopened = mountHall()
    await flushPromises()
    expect(hallParams(4)).toMatchObject({ game: 101, price_min: '100' })
    reopened.unmount()

    loginSession({ id: 8 })
    const otherWorker = mountHall()
    await flushPromises()
    expect(hallParams(5)).toEqual({ game: 101, page: 1, page_size: 20, include_exited: 1 })
    otherWorker.unmount()

    loginSession({ identity_plane: 'tenant_b' })
    mountHall()
    await flushPromises()
    expect(hallParams(6)).toEqual({ game: 101, page: 1, page_size: 20, include_exited: 1 })
  })

  it('筛选面板的键盘焦点在关闭和保存之间循环', async () => {
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    await openHallFilters(wrapper)
    const sheet = wrapper.get('[data-testid="hall-filter-form"]')
    const close = sheet.get<HTMLButtonElement>('[aria-label="关闭筛选"]')
    const save = sheet.get<HTMLButtonElement>('button[type="submit"]')

    close.element.focus()
    await close.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(save.element)
    await save.trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(close.element)
  })

  it('大厅读取时退出会关闭正在显示的筛选面板与遮罩', async () => {
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    await openHallFilters(wrapper)
    const openedSheet = wrapper.get('.m-hall-filter-sheet.modal-in').element
    expect(openedSheet.isConnected).toBe(true)
    expect(document.querySelector('.sheet-backdrop.backdrop-in')).not.toBeNull()

    wrapper.unmount()
    expect(openedSheet.isConnected).toBe(false)
    expect(document.querySelector('.sheet-backdrop.backdrop-in')).toBeNull()
    expect(document.documentElement.classList.contains('with-modal-sheet')).toBe(false)
  })

  it('更多菜单尚未完成第一帧时退出也会清理遮罩和打开状态', async () => {
    loginSession()
    const wrapper = mount(HallPage, { attachTo: document.getElementById('framework7-root')! })
    await flushPromises()
    const frames: FrameRequestCallback[] = []
    const schedule = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => frames.push(callback))
    try {
      await wrapper.get('[data-testid="hall-more-open"]').trigger('click')
      const menu = wrapper.get('.m-hall-more-menu')
      expect(menu.classes()).not.toContain('modal-in')
      expect(document.querySelector('.popover-backdrop.backdrop-in')).not.toBeNull()

      wrapper.unmount()

      expect(document.documentElement.classList.contains('with-modal-popover')).toBe(false)
      expect(document.querySelector('.popover-backdrop.backdrop-in')).toBeNull()
      for (const callback of frames.splice(0)) callback(performance.now())
      expect(menu.classes()).not.toContain('modal-in')
    } finally {
      schedule.mockRestore()
    }
  })

  it('is_preferred 行显示「优选」，普通行不显示', async () => {
    loginSession()
    hallFixtures = {
      '101': [
        {
          items: [
            nativeRow({ order_no: '10000000000000000001', is_preferred: true }),
            nativeRow({ order_no: '10000000000000000002' }),
          ],
          total: 2,
        },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.find('[data-testid="hall-pref-badge"]').text()).toBe('优选')
    // 第二张（普通行）没有角标。
    expect(wrapper.findAll('[data-testid="hall-pref-badge"]')).toHaveLength(1)

  })

  it('★ 灰卡：整卡置灰类 + 「已被抢」水印 + ⛔ 无抢单按钮（列表与浮层都没有）+ 仍可点开详情浮层 + 不发参考图请求', async () => {
    loginSession({ menu_permissions: ['grab_hall.take_order', 'order.view_screenshot'] })
    hallFixtures = {
      '101': [
        {
          items: [
            nativeRow({
              order_no: '10000000000000000001',
              is_exited: true,
              exit_kind: 'claimed',
            }),
          ],
          total: 1,
        },
      ],
    }
    const wrapper = mountHall()
    await flushPromises()
    const card = wrapper.find(cardSel)
    expect(card.classes()).toContain('m-hall-card--exited')
    expect(wrapper.find('[data-testid="hall-exit-mark"]').text()).toBe('已被抢')
    // ⛔ fail-closed：灰卡连一颗抢单按钮都没有（不是画一颗置灰的）。
    expect(wrapper.findAll(claimSel)).toHaveLength(0)

    // 点整卡 → 详情浮层照常打开；且 ⛔ 灰卡不发参考图请求（D-0296：灰卡不出图）。
    const before = requestMock.mock.calls.length
    await card.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hall-detail-claim"]').exists()).toBe(false)
    expect(requestMock.mock.calls.length).toBe(before)
    expect(wrapper.find('[data-testid="hall-detail-shots"]').exists()).toBe(false)
  })

  it('★ 模拟单：POST /hall/sim/7/claim/ → 恒 1012 → toast 后端 msg + 自动重取已加载列表（⛔ 无成功分支）', async () => {
    loginSession()
    hallFixtures = {
      '101': [{ items: [simulatedRow()], total: 1 }],
    }
    claimImpl = async () => {
      // 后端契约：模拟单接单端点**永远**回 1012（第一次点也一样，D-0296）。
      throw new BizError(1012, '此单已被人接手，请刷新')
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    ;dialogOptsAt().onConfirm()
    await flushPromises()

    // 判据打在真实出口：POST 的 url 按 sim_pk 拼（⛔ 不是 order_no、不是 mirror_pk）。
    expect(requestMock).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'post',
        url: '/api/business/hall/sim/7/claim/',
      }),
    )
    expect(toast.error).toHaveBeenCalledWith('此单已被人接手，请刷新')
    expect(toast.success).not.toHaveBeenCalled()
    // 1012 的自动重取：1012 那条公共支覆盖三路（模拟单只会走这条支）。
    expect(hallCalls).toHaveLength(2)
    expect(hallParams(1)).toMatchObject({ page: 1 })
    // 卡片没有被当成功移除（它要靠重取回来的新列表呈现灰卡形态）。
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
  })

  it('★ 参考图（无权限）：me 缺 order.view_screenshot ⇒ 不发 screenshots 请求、浮层里没有参考图段（fail-closed）', async () => {
    loginSession({ menu_permissions: ['grab_hall.take_order'] })
    shotsFixture = [{ id: 1, img_type: 0, url: 'https://测试签名/甲.png' }]
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')
    await flushPromises()

    expect(
      requestMock.mock.calls.filter(
        (c) => String((c[0] as AxiosRequestConfig).url).endsWith('/screenshots'),
      ),
    ).toHaveLength(0)
    expect(wrapper.find('[data-testid="hall-detail-shots"]').exists()).toBe(false)
  })

  it('★ 参考图（有权限）：只出 img_type=0（群二维码 img_type=1 ⛔ 不显示）；点缩略图开 ImageViewer', async () => {
    loginSession({ menu_permissions: ['grab_hall.take_order', 'order.view_screenshot'] })
    shotsFixture = [
      { id: 1, img_type: 0, url: 'https://测试签名/截图甲.png', thumbnail_url: 'https://测试签名/缩略甲.png' },
      { id: 2, img_type: 1, url: 'https://测试签名/群二维码.png' },
      { id: 3, img_type: 0, url: 'https://测试签名/截图乙.png' },
    ]
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')
    await flushPromises()

    // 打开浮层那次**现取**（COS 签名 300 秒过期，⛔ 不缓存 URL）。
    const shotsCalls = requestMock.mock.calls.filter(
      (c) => String((c[0] as AxiosRequestConfig).url).endsWith('/screenshots'),
    )
    expect(shotsCalls).toHaveLength(1)
    expect(String((shotsCalls[0]![0] as AxiosRequestConfig).url)).toBe(
      '/api/business/orders/10000000000000000001/screenshots',
    )

    const shots = wrapper.find('[data-testid="hall-detail-shots"]')
    expect(shots.exists()).toBe(true)
    const imgs = shots.findAll('img')
    expect(imgs).toHaveLength(2) // img_type=1 那张被滤掉
    expect(imgs[0]!.attributes('loading')).toBe('lazy')
    expect(imgs.map((img) => img.attributes('src'))).toEqual([
      'https://测试签名/缩略甲.png',
      'https://测试签名/截图乙.png',
    ])

    // 点第 2 张缩略图 → 受控预览从它打开，images 只含 img_type=0 的 url。
    await imgs[1]!.trigger('click')
    const viewer = wrapper.getComponent(ImageViewer)
    expect(viewer.props('visible')).toBe(true)
    expect(viewer.props('images')).toEqual([
      'https://测试签名/截图甲.png',
      'https://测试签名/截图乙.png',
    ])
    expect(viewer.props('index')).toBe(1)
  })

  it('参考图只在当前订单的请求在途时显示加载提示', async () => {
    loginSession({ menu_permissions: ['grab_hall.take_order', 'order.view_screenshot'] })
    const requestImpl = requestMock.getMockImplementation()!
    const release: Array<() => void> = []
    requestMock.mockImplementation((config: AxiosRequestConfig) => {
      if (!String(config.url).endsWith('/screenshots')) return requestImpl(config)
      const data = { items: shotsFixture, total: shotsFixture.length }
      return new Promise((resolve) => { release.push(() => resolve(data)) })
    })

    shotsFixture = [{ id: 1, img_type: 0, url: 'https://测试签名/甲.png' }]
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.findAll(cardSel)[0]!.trigger('click')
    const loading = '[data-testid="hall-shots-loading"]'
    expect(wrapper.get(`${loading}[role="status"]`).find('.preloader').exists()).toBe(true)
    await wrapper.get('[data-testid="hall-detail-close"]').trigger('click')
    expect(wrapper.find(loading).exists()).toBe(false)

    shotsFixture = [{ id: 2, img_type: 0, url: 'https://测试签名/乙.png' }]
    await wrapper.findAll(cardSel)[1]!.trigger('click')
    expect(wrapper.find(loading).exists()).toBe(true)
    release[0]!()
    await flushPromises()
    expect(wrapper.find(loading).exists()).toBe(true)
    expect(wrapper.find('[data-testid="hall-detail-shots"] img').exists()).toBe(false)

    release[1]!()
    await flushPromises()
    expect(wrapper.find(loading).exists()).toBe(false)
    expect(wrapper.get('[data-testid="hall-detail-shots"] img').attributes('src')).toBe('https://测试签名/乙.png')
  })

  it('★ 参考图取数失败 → 静默：浮层照常打开，参考图段不渲染（浮层里不为参考图报错）', async () => {
    loginSession({ menu_permissions: ['grab_hall.take_order', 'order.view_screenshot'] })
    shotsFailNext = new HttpError(500, '签名服务挂了')
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(cardSel).trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hall-detail-shots"]').exists()).toBe(false)
    expect(toast.error).not.toHaveBeenCalled()
  })

  /* ── ⭐ 团团通上家镜像卡（`origin === 'peer'`，D-0303） ────────────────
   * 判据与桌面 `order-insiderecorderlist-peer.spec.ts` 同一条口径：卡片渲染（游戏 / 项目 / 展示价 / **上家单号** / 时间，徽标「上家」）；数据门：`mirror_pk` 为 0 / 缺 ⇒ 一颗按钮都不画（⛔ 不拿空主键拼 URL）；接单落点：`POST /api/business/hall/peer/<mirror_pk>/claim/`（⛔ 不是另三条入口）；
   * 结局：成功「正在受理中」+ 置灰不删卡（不刷新）、1012 刷新、其余原样后端 msg 卡保留；双点只发一次（点击期 + 确认回调各判一次，在途集合按 `mirror_pk`）。
   */

  /** 打出本用例发过的 peer 接单请求 URL（判据钉 URL，不钉次数常量）。 */
  function peerClaimUrls(): string[] {
    return requestMock.mock.calls
      .map((c) => String((c[0] as AxiosRequestConfig).url))
      .filter((url) => url.startsWith('/api/business/hall/peer/'))
  }

  it('★ peer 行渲染：游戏 / 项目 / 展示价 / 上家单号 / 时间，（不再有「上家」角标）；区服为空时显示「—」', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()

    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    const text = textAt(wrapper, cardSel, 0)
    expect(text).toContain('测试游戏甲') // 游戏：按本端游戏字典解名字
    expect(text).toContain('测试团团通项目甲')
    expect(text).toContain('18.00') // 展示价（上家发单价原样，v1 不打折）
    // 单号：`order_no` 是空串 ⇒ 回落 `remote_order_view || remote_order_id`，标签统一「订单号」。
    expect(text).not.toContain('20000000000000000001')
    await wrapper.get(cardSel).trigger('click')
    expect(wrapper.get(orderNoSel).text()).toBe('20000000000000000001')
    // 时间：`created_at` = 进厅时刻（合成值 +00:00 ⇒ 项目时区 +08:00）。
    expect(text).toContain('进厅 16:00')
    expect(wrapper.get('[data-testid="hall-detail-peer-time"]').text()).toContain('2026-10-10 16:00')
    // 区服：团团通载荷没有区服文本 ⇒ 如实显示「—」（⛔ 不编一个名字）。
    expect(wrapper.get('[data-testid="hall-detail-panel"]').text()).toContain('区服')
    expect(wrapper.get('[data-testid="hall-detail-panel"]').text()).toContain('—')
    // ⭐ 业务方裁定：不显示「上家 / 本租户」角标。
    expect(wrapper.find('.m-hall-card__origin').exists()).toBe(false)
    expect(text).not.toContain('本租户')
    // 数据门通过 ⇒ 恰好一颗抢单按钮（权限门默认给足）。
    expect(wrapper.findAll(claimSel)).toHaveLength(1)
  })

  it('★ peer 行 `mirror_pk` 为 null / 0 ⇒ 不画抢单按钮（列表与浮层都没有）、一次请求都不发', async () => {
    loginSession()
    hallFixtures = {
      '101': [{ items: [peerRow({ mirror_pk: null }), peerRow({ mirror_pk: 0 })], total: 2 }],
    }
    const wrapper = mountHall()
    await flushPromises()

    expect(wrapper.findAll(cardSel)).toHaveLength(2) // 卡片照常渲染（只是接不了）
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
    // 浮层里那一颗也走同一道数据门（fail-closed，⛔ 不拿空主键拼 URL）。
    await wrapper.find(cardSel).trigger('click')
    expect(wrapper.find('[data-testid="hall-detail-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hall-detail-claim"]').exists()).toBe(false)
    expect(peerClaimUrls()).toEqual([])
  })

  it('★ peer 接单成功：POST /hall/peer/<mirror_pk>/claim/ + 「正在受理中」；卡置灰标「已接下」、⛔ 不删不刷新', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()

    await wrapper.find(claimSel).trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    dialogOptsAt().onConfirm()
    await flushPromises()

    // 落点：本线端点、路径参数是**镜像行主键**（⛔ 不是上家单号，也不是另三条入口）。
    expect(peerClaimUrls()).toEqual(['/api/business/hall/peer/9/claim/'])
    // ⛔ 另三条入口一条都没碰。
    expect(
      requestMock.mock.calls
        .map((c) => String((c[0] as AxiosRequestConfig).url))
        .filter((url) => url.includes('claim')),
    ).toEqual(['/api/business/hall/peer/9/claim/'])
    expect(toast.success).toHaveBeenCalledWith('正在受理中')
    expect(toast.success).not.toHaveBeenCalledWith('抢单成功')
    // 不删卡、不刷新（确认制：上家还没判决）⇒ 卡片置灰 + 「已接下」标记。
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(hallCalls).toHaveLength(1)
    expect(wrapper.find(cardSel).classes()).toContain('m-hall-card--peer-claimed')
    const mark = wrapper.find('[data-testid="hall-peer-claimed-mark"]')
    expect(mark.exists()).toBe(true)
    expect(mark.text()).toBe('已接下')
    // 已受理 ⇒ 按钮不再渲染（再点一次请求都不发；`hasClaimTarget` 的数据门）。
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
    // ⛔ 不提示「到我的订单查看」——这张单还不是我方的。
    expect(wrapper.find('[data-testid="claim-hint"]').exists()).toBe(false)
  })

  it('★ peer 1012 ⇒ 原样 toast 后端 msg + 刷新列表；⛔ 不置灰（状态交给重取后的载荷）', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    claimImpl = async () => {
      throw new BizError(1012, '该笔订单已被其他打手接取')
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('该笔订单已被其他打手接取')
    expect(hallCalls).toHaveLength(2) // 1012 ⇒ 重取已加载列表
    expect(wrapper.find(cardSel).classes()).not.toContain('m-hall-card--peer-claimed')
    expect(wrapper.find('[data-testid="hall-peer-claimed-mark"]').exists()).toBe(false)
  })

  it('★ peer 1015（上家没开同步）⇒ 原样 toast 后端 msg；卡留在列表、⛔ 不刷新', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    claimImpl = async () => {
      throw new BizError(1015, '还没配置好鸡蛋铺同步账号，请联系管理员')
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('还没配置好鸡蛋铺同步账号，请联系管理员')
    expect(hallCalls).toHaveLength(1)
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(wrapper.findAll(claimSel)).toHaveLength(1) // 还能再点
    expect(wrapper.find(cardSel).classes()).not.toContain('m-hall-card--peer-claimed')
  })

  it('★ peer 其它错误（403）⇒ 原样显示 msg，⛔ 不吞、不换文案；卡留在列表', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    claimImpl = async () => {
      throw new HttpError(403, '没有接单权限')
    }
    const wrapper = mountHall()
    await flushPromises()
    await wrapper.find(claimSel).trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('没有接单权限')
    expect(hallCalls).toHaveLength(1)
    expect(wrapper.findAll(claimSel)).toHaveLength(1)
    expect(wrapper.find(cardSel).classes()).not.toContain('m-hall-card--peer-claimed')
  })

  it('★ peer 双点（两个确认框都点「确定」）只发一次：在途互斥在确认回调里也判一次', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    let release: (value: unknown) => void = () => {}
    claimImpl = () =>
      new Promise((resolve) => {
        release = resolve
      })
    const wrapper = mountHall()
    await flushPromises()

    // 双击：两下都在第一发之前到达 ⇒ 会弹两个确认框（点击期那道闸还没被打上标记）。
    await wrapper.find(claimSel).trigger('click')
    await wrapper.find(claimSel).trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(2)
    // 两个确认框都点「确定」⇒ 第二个回调必须撞在途集合（⛔ 否则双击 = 两发请求）。
    dialogOptsAt(0).onConfirm()
    dialogOptsAt(1).onConfirm()
    await flushPromises()
    expect(peerClaimUrls()).toHaveLength(1)

    release({})
    await flushPromises()
    // 收口后不再挂着：卡片已受理、按钮消失。
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
  })

  it('★ peer 第一发在途时再点 ⇒ 连确认框都不弹（点击期那道闸）、请求仍是一发', async () => {
    loginSession()
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    let release: (value: unknown) => void = () => {}
    claimImpl = () =>
      new Promise((resolve) => {
        release = resolve
      })
    const wrapper = mountHall()
    await flushPromises()

    await wrapper.find(claimSel).trigger('click')
    dialogOptsAt().onConfirm()
    await flushPromises()
    expect(peerClaimUrls()).toHaveLength(1)

    await wrapper.find(claimSel).trigger('click')
    expect(dialogConfirm).toHaveBeenCalledTimes(1)
    expect(peerClaimUrls()).toHaveLength(1)

    release({})
    await flushPromises()
  })

  it('★ peer 无权限（me 缺 grab_hall.take_order）⇒ 不画按钮（权限门在数据门之外）', async () => {
    loginSession({ menu_permissions: ['my_orders'] })
    hallFixtures = { '101': [{ items: [peerRow()], total: 1 }] }
    const wrapper = mountHall()
    await flushPromises()
    expect(wrapper.findAll(cardSel)).toHaveLength(1)
    expect(wrapper.findAll(claimSel)).toHaveLength(0)
  })
})
