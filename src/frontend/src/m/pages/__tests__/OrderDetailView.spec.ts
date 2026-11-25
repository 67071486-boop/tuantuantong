// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

// 传输层是唯一的桩边界（照 MinePage.spec 同一手法）：myOrders / errors 跑真代码，判据打在 mock 的 request 被调时的 url / method / 有没有 data 上。
// ⚠️ `http` 键必须给：`api/orders.ts` 顶层 `import { http, request }`（SubmitWorkDialog 的 submitMyOrder 单号形状校验用）。
vi.mock('../../router/useWorkerBack', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../router/useWorkerBack')>(),
  useWorkerBack: () => h.back,
}))
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

// Popup（页面内嵌的 SubmitWorkDialog 用它承载）、ImageViewer（群码放大数据）。
vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))
vi.mock('../../components/MobilePhotoBrowser.vue', () => ({
  default: {
  name: 'MobilePhotoBrowser',
  props: ['visible', 'images', 'closeBtn'],
  emits: ['update:visible'],
  template:
    '<div class="photo-browser-stub" :data-visible="String(visible)"'
    + ' :data-images="JSON.stringify(images || [])"></div>',
},
}))

// 路由参数是**可变夹具**：形状闸用例要塞非 20 位单号。vi.hoisted 保证工厂引用不踩 TDZ。
const h = vi.hoisted(() => ({
  params: { orderNo: '10000000000000000002' },
  query: {} as Record<string, string>,
  back: vi.fn(),
  push: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: h.params, query: h.query }),
  useRouter: () => ({
    push: h.push,
    currentRoute: { value: { fullPath: `/orders/${h.params.orderNo}` } },
    options: { history: { state: {}, replace: vi.fn() } },
  }),
}))
h.params = reactive(h.params)

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { AuthExpiredError, BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import type { MyOrderDetail } from '@/api/myOrders'
import type { OrderChatSummary } from '@/api/orderChat'
import type { ScreenshotItem } from '@/api/screenshots'
import type { User } from '@/api/auth'
import { useSessionStore, WORKER_ROLE } from '@/stores/session'
import { openInternalChatPreview } from '@/v2/internalChat/launcherPreview'
import OrderDetailView from '../OrderDetailView.vue'

const requestMock = request as unknown as Mock
const toast = Toast as unknown as { success: Mock; error: Mock }

/* ── 合成夹具（E-0077：单号从 10000000000000000002 起、游戏「测试游戏甲」） ── */

const ORDER_NO = '10000000000000000002'

/** 任务书 T4 恰好断言用的 §6.2 16 键 + **D-0301（2026-10-24）加的 `source_order_no`** =
 * **17 键**（sorted 字面值；⛔ 不用 contains 放宽）。 */
const DETAIL_KEYS_16 = [
  'claimed_at',
  'content',
  'demand',
  'game_account',
  'game_name',
  'game_role_name',
  'game_sub_account',
  'group_qr_url',
  'is_urgent',
  'order_no',
  'project_text',
  'remark',
  'server_name',
  'source_order_no',
  'status',
  'temp_amount',
  'time_requirement',
].sort()

function detailFixture(overrides: Partial<MyOrderDetail> = {}): MyOrderDetail {
  return {
    order_no: ORDER_NO,
    // ⚠️ 基准夹具来源号为空串（回落变体）；「来源号非空 ⇒ 单号行逐字显示来源号」
    // 的主变体在 D-0301 那组用例里给。
    source_order_no: '',
    status: 'IN_PROGRESS',
    game_name: '测试游戏甲',
    server_name: '测试区服甲',
    project_text: '测试项目甲',
    content: '合成客户原始需求：十二层满星后截图',
    demand: '合成需求描述：打手看的这一段',
    time_requirement: '48小时',
    game_account: 'synth-account-a',
    game_sub_account: 'synth-sub-account-a',
    game_role_name: 'synth-role-a',
    is_urgent: true,
    remark: '合成重要备注：晚班交接',
    claimed_at: '2026-10-10T08:05:00+08:00',
    temp_amount: '88.00',
    group_qr_url: 'https://qr-synth.test.invalid/signed',
    ...overrides,
  }
}

/**
 * **类型恰好集钉子（vue-tsc 门在守，不是 vitest）**：`Record<keyof MyOrderDetail, true>` 要求**不多不少**——载荷类型多出第 17 个键（例如哪天加了 `customer_wechat`）⇒ 这里缺属性变红；少掉任何一个现有键 ⇒ 这里多出未知属性变红。
 * 运行时再配一条 `Object.keys(fixture)` 的恰好断言（见「恰好断言」用例），两条各管一侧。
 */
type ExactMyOrderDetailKeys = Record<keyof MyOrderDetail, true>
const EXACT_KEYS_PIN: ExactMyOrderDetailKeys = {
  order_no: true,
  source_order_no: true,
  status: true,
  game_name: true,
  server_name: true,
  project_text: true,
  content: true,
  demand: true,
  time_requirement: true,
  game_account: true,
  game_sub_account: true,
  game_role_name: true,
  is_urgent: true,
  remark: true,
  claimed_at: true,
  temp_amount: true,
  group_qr_url: true,
}
void EXACT_KEYS_PIN

/* ── mock 的请求路由 ── */

let detailImpl: (config: AxiosRequestConfig) => Promise<MyOrderDetail>
/** submit / report-issue 请求的实现（成功 / 失败 / 挂起，各用例自设）。 */
let submitImpl: (config: AxiosRequestConfig) => Promise<unknown>
let shotsImpl: (config: AxiosRequestConfig) => Promise<{ items: ScreenshotItem[]; total: number }>
/** 命中 /detail/ 的请求计数（「成功后不重取详情」的判据）。 */
let detailCalls = 0

function wireRequestMock(): void {
  requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
    const url = String(config.url)
    if (url.includes('/detail/')) {
      detailCalls += 1
      return detailImpl(config)
    }
    if (url.includes('/submit/') || url.includes('/report-issue/')) return submitImpl(config)
    if (url.endsWith('/screenshots')) return shotsImpl(config)
    throw new Error(`spec: 未预期的请求 ${url}`)
  })
}

/** requestMock 里 url 命中 match 的第 0 发的 config（不存在即抛——不给断言静默通过的机会）。 */
function firstCallTo(match: string): AxiosRequestConfig {
  const hit = requestMock.mock.calls
    .map((c) => c[0] as AxiosRequestConfig)
    .find((cfg) => String(cfg.url).includes(match))
  if (hit === undefined) throw new Error(`spec: 没有命中 ${match} 的请求`)
  return hit
}

/* ── 挂载与 session ── */

type SessionOverrides = Omit<Partial<User>, 'menu_permissions'> & {
  menu_permissions?: string[] | null
}

const ME_FULL: User = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
  menu_permissions: ['my_orders', 'order.submit_work'],
  game_ids: [101],
  tenant_name: '测试工作室甲',
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

function mountDetail(openChat = vi.fn()) {
  return mount(OrderDetailView, {
    global: { provide: { [openInternalChatPreview as symbol]: openChat } },
  })
}

/* ── 剪贴板替身（jsdom 没有 clipboard；逐用例显式装/拆） ── */

function stubClipboard(writeText: Mock): void {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
}

function removeClipboardStub(): void {
  Reflect.deleteProperty(navigator, 'clipboard')
}

/* ── 选择器 ── */

const missingSel = '[data-testid="detail-404"]'
const loadingSel = '[data-testid="detail-loading"]'
const errorSel = '[data-testid="detail-error"]'
const accountCardSel = '[data-testid="detail-card-account"]'
const qrImgSel = '[data-testid="detail-qr-img"]'
const qrEmptySel = '[data-testid="detail-qr-empty"]'
const viewerSel = '.photo-browser-stub'
const actionsSel = '[data-testid="detail-actions"]'
const openSubmitSel = '[data-testid="detail-open-submit"]'
const openReportSel = '[data-testid="detail-open-report"]'
const statusBadgeSel = '[data-testid="detail-status-badge"]'
const dialogConfirmSel = '.m-submit-dialog__confirm'

function rowOf(wrapper: ReturnType<typeof mount>, testId: string) {
  const row = wrapper.find(`[data-testid="${testId}"]`)
  if (!row.exists()) throw new Error(`spec: 行 ${testId} 不存在`)
  return row
}

function popupVisible(wrapper: ReturnType<typeof mount>): string {
  return String(wrapper.findComponent({ name: 'f7-sheet' }).props('opened')) ?? ''
}

enableAutoUnmount(afterEach)

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  h.params.orderNo = ORDER_NO
  h.query = {}
  h.back.mockClear()
  detailCalls = 0
  detailImpl = async () => detailFixture()
  submitImpl = async () => ({ order_no: ORDER_NO, status: 'PENDING_REVIEW' })
  shotsImpl = async () => ({ items: [], total: 0 })
  wireRequestMock()
})

afterEach(() => {
  removeClipboardStub()
  vi.useRealTimers()
})

describe('m 端订单详情（OrderDetailView）— 单号形状闸', () => {
  it('非 20 位单号（19 位 / 字母）→ 404 态页、零请求（进入即验，不发注定打不中的请求）', async () => {
    for (const bad of ['1000000000000000001', 'abc']) {
      h.params.orderNo = bad
      const wrapper = mountDetail()
      await flushPromises()
      expect(wrapper.find(missingSel).exists()).toBe(true)
      expect(wrapper.text()).toContain('订单不存在或链接有误')
      // 零请求：形状不符连一条 /detail/ 都不发。
      expect(requestMock.mock.calls).toHaveLength(0)
      wrapper.unmount()
    }
  })

  it('404 态页有「返回」键且已接线（router.back）', async () => {
    h.params.orderNo = 'abc'
    const wrapper = mountDetail()
    await flushPromises()
    await wrapper.find('[data-testid="detail-back"]').trigger('click')
    expect(h.back).toHaveBeenCalledTimes(1)
  })
})

describe('m 端订单详情（OrderDetailView）— 16 键逐组渲染', () => {
  it('在 WxPusher 订单通知打开的详情上保留手动打开代代通的入口', async () => {
    loginSession()
    h.query = { entry: 'wxpusher' }
    const location = window.location
    vi.stubGlobal('location', { ...location, origin: 'https://suqing.suqing.chat' })
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android/WxPusher-Android')
    try {
      const wrapper = mountDetail()
      await flushPromises()
      expect(wrapper.get('[data-testid="detail-open-daidaitong"]').attributes('href'))
        .toBe(`daidaitong://suqing.suqing.chat/m/orders/${ORDER_NO}`)
      expect(wrapper.find('[data-testid="detail-card-base"]').exists()).toBe(true)
    } finally {
      vi.unstubAllGlobals()
      userAgent.mockRestore()
    }
  })

  it('基本信息组：单号（带复制）/ 游戏 / 区服 / 项目 / 状态徽标 / 加急 / 接手时间', async () => {
    loginSession()
    const wrapper = mountDetail()
    await flushPromises()

    const orderNoRow = rowOf(wrapper, 'detail-row-order-no')
    expect(orderNoRow.text()).toContain(ORDER_NO)
    expect(orderNoRow.find('[data-testid="detail-copy-order-no"]').exists()).toBe(true)
    expect(rowOf(wrapper, 'detail-row-game').text()).toContain('测试游戏甲')
    expect(rowOf(wrapper, 'detail-row-server').text()).toContain('测试区服甲')
    expect(rowOf(wrapper, 'detail-row-project').text()).toContain('测试项目甲')
    // 状态徽标复用 T3 的状态映射（后端词表）。
    expect(wrapper.find(statusBadgeSel).text()).toBe('代练中')
    expect(rowOf(wrapper, 'detail-row-urgent').text()).toContain('加急')
    // claimed_at 是 +08:00 串 ⇒ 墙钟原样（不按浏览器时区平移，与 T3 同一换算件）。
    expect(rowOf(wrapper, 'detail-row-claimed-at').text()).toContain('2026-10-10 08:05')
  })

  it('有值时展示订单内容、代练要求、时间要求和备注', async () => {
    loginSession()
    const wrapper = mountDetail()
    await flushPromises()

    expect(rowOf(wrapper, 'detail-row-content').text()).toContain('合成客户原始需求：十二层满星后截图')
    expect(rowOf(wrapper, 'detail-row-demand').text()).toContain('合成需求描述：打手看的这一段')
    expect(rowOf(wrapper, 'detail-row-time-requirement').text()).toContain('48小时')
    expect(rowOf(wrapper, 'detail-row-remark').text()).toContain('合成重要备注：晚班交接')
  })

  it('隐藏空白资料行，同时显示已有的账号和 0 元接单金额', async () => {
    loginSession()
    detailImpl = async () => detailFixture({
      project_text: ' ',
      content: '',
      demand: '  ',
      server_name: null,
      time_requirement: '',
      remark: '\n',
      claimed_at: '',
      game_sub_account: ' ',
      temp_amount: '0.00',
    })
    const wrapper = mountDetail()
    await flushPromises()

    const emptyFields = ['project', 'content', 'demand', 'server', 'time-requirement', 'remark', 'claimed-at', 'game-sub-account']
    const selectors = emptyFields.map((field) => `[data-testid="detail-row-${field}"]`).join(', ')
    expect(wrapper.findAll(selectors).map((row) => row.text())).toEqual([])
    expect(wrapper.find('[data-testid="detail-card-require"]').exists()).toBe(false)
    expect(rowOf(wrapper, 'detail-row-game-account').text()).toContain('synth-account-a')
    expect(rowOf(wrapper, 'detail-row-temp-amount').text()).toBe('接单金额0.00 元')
  })

  it('游戏账号组：三字段 + 恰好三颗复制按钮；三字段全空 ⇒ 整块不渲染', async () => {
    loginSession()
    let wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(accountCardSel).exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-row-game-account"]').text()).toContain('synth-account-a')
    expect(wrapper.find('[data-testid="detail-row-game-sub-account"]').text()).toContain('synth-sub-account-a')
    expect(wrapper.find('[data-testid="detail-row-game-role-name"]').text()).toContain('synth-role-a')
    expect(wrapper.findAll(`${accountCardSel} .m-odetail-row__copy`)).toHaveLength(3)
    wrapper.unmount()

    detailImpl = async () =>
      detailFixture({ game_account: '', game_sub_account: '', game_role_name: '   ' })
    wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(accountCardSel).exists()).toBe(false)
  })

  it('账号有的没填时，仅显示有值的账号和对应复制按钮', async () => {
    loginSession()
    detailImpl = async () => detailFixture({ game_account: '', game_role_name: '' })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(accountCardSel).exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-row-game-account"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="detail-row-game-role-name"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="detail-copy-game-account"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="detail-copy-game-sub-account"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-copy-game-role-name"]').exists()).toBe(false)
  })

  it('我的报酬组：temp_amount 字符串原样 + 「 元」（不转数字、不补零）', async () => {
    loginSession()
    const wrapper = mountDetail()
    await flushPromises()
    expect(rowOf(wrapper, 'detail-row-temp-amount').text()).toBe('接单金额88.00 元')
  })

  it('群聊二维码：有值 → 图片 + 点击放大（ImageViewer visible 翻转）；null → 「暂无群码」占位', async () => {
    loginSession()
    let wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(qrImgSel).attributes('src')).toBe('https://qr-synth.test.invalid/signed')
    expect(wrapper.find(viewerSel).attributes('data-visible')).toBe('false')
    await wrapper.find(qrImgSel).trigger('click')
    expect(wrapper.find(viewerSel).attributes('data-visible')).toBe('true')
    wrapper.unmount()

    detailImpl = async () => detailFixture({ group_qr_url: null })
    wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(qrEmptySel).text()).toBe('暂无群码')
    expect(wrapper.find(qrImgSel).exists()).toBe(false)
    expect(wrapper.find(viewerSel).exists()).toBe(false)
  })

  it('恰好断言（R1）：载荷键集 = 16 键字面值；页面任何地方不出现客户联系字段', async () => {
    loginSession()
    const wrapper = mountDetail()
    await flushPromises()

    // ① 夹具键集恰好 17（D-0301 加 source_order_no；类型侧由 EXACT_KEYS_PIN 在 vue-tsc 门下钉死）。
    expect(Object.keys(detailFixture()).sort()).toEqual(DETAIL_KEYS_16)
    // ② 渲染面没有 customer_ 字样（R1：联系方式不给）。
    expect(wrapper.text()).not.toContain('customer_')
    expect(wrapper.text()).not.toContain('联系电话')
    expect(wrapper.text()).not.toContain('联系方式')
  })
})

describe('m 端订单详情（OrderDetailView）— 复制', () => {
  beforeEach(() => {
    loginSession()
  })

  it('游戏账号三字段各配按钮：点击把**各自的值**写进剪贴板，成功 toast「已复制」', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.find('[data-testid="detail-copy-game-account"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('synth-account-a')

    await wrapper.find('[data-testid="detail-copy-game-sub-account"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('synth-sub-account-a')

    await wrapper.find('[data-testid="detail-copy-game-role-name"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('synth-role-a')

    expect(toast.success).toHaveBeenCalledTimes(3)
    expect(toast.success).toHaveBeenCalledWith('已复制')
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('单号复制：把 20 位单号写进剪贴板', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const wrapper = mountDetail()
    await flushPromises()
    await wrapper.find('[data-testid="detail-copy-order-no"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(ORDER_NO)
    expect(toast.success).toHaveBeenCalledWith('已复制')
  })

  it('writeText 拒绝 → toast「复制失败」；剪贴板 API 不在 → 同样「复制失败」且无成功提示', async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied'))
    stubClipboard(writeText)
    let wrapper = mountDetail()
    await flushPromises()
    await wrapper.find('[data-testid="detail-copy-game-account"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('复制失败')
    expect(toast.success).not.toHaveBeenCalled()
    wrapper.unmount()

    // API 缺席（非安全上下文 / 老内核）：一个不抛错的失败分支。
    removeClipboardStub()
    toast.error.mockClear()
    wrapper = mountDetail()
    await flushPromises()
    await wrapper.find('[data-testid="detail-copy-game-account"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('复制失败')
    expect(toast.success).not.toHaveBeenCalled()
  })
})

describe('m 端订单详情（OrderDetailView）— 交单入口', () => {
  /** 状态 × 权限四象限：只有（代练中 × 有 order.submit_work）画两键，其余象限一颗都不画。 */
  it('按钮可见性四象限（fail-closed：取不到 me 按无权限）', async () => {
    loginSession()
    let wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(openSubmitSel).exists()).toBe(true)
    expect(wrapper.find(openReportSel).exists()).toBe(true)
    wrapper.unmount()

    loginSession({ menu_permissions: ['my_orders'] })
    wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(openSubmitSel).exists()).toBe(false)
    expect(wrapper.find(openReportSel).exists()).toBe(false)
    wrapper.unmount()

    loginSession()
    detailImpl = async () => detailFixture({ status: 'PENDING_REVIEW' })
    wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(openSubmitSel).exists()).toBe(false)
    expect(wrapper.find(openReportSel).exists()).toBe(false)
    wrapper.unmount()

    loginSession({ menu_permissions: null })
    detailImpl = async () => detailFixture()
    wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(openSubmitSel).exists()).toBe(false)
    expect(wrapper.find(openReportSel).exists()).toBe(false)
  })

  it('提交完成 → 弹层默认勾待验收；确认 POST /submit/ 无 data；徽标从**响应载荷**就地更新且不重取详情', async () => {
    loginSession()
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.find(openSubmitSel).trigger('click')
    expect(popupVisible(wrapper)).toBe('true')
    expect(
      (wrapper.find('input[value="PENDING_REVIEW"]').element as HTMLInputElement).checked,
    ).toBe(true)

    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    const cfg = firstCallTo('/submit/')
    expect(cfg).toMatchObject({
      method: 'post',
      url: `/api/business/orders/${ORDER_NO}/submit/`,
    })
    expect('data' in cfg).toBe(false)

    // 徽标 = 响应载荷里的 status；两键随状态闸消失；⛔ 不重取详情（detail 恰好首屏那一发）。
    expect(wrapper.find(statusBadgeSel).text()).toBe('待验收')
    expect(wrapper.find(actionsSel).exists()).toBe(false)
    expect(detailCalls).toBe(1)
    expect(toast.success).toHaveBeenCalledWith('提交成功')
    expect(popupVisible(wrapper)).toBe('false')
  })

  it('报问题 → 弹层预勾问题档，填写原因后提交，徽标变问题单、两键消失', async () => {
    loginSession()
    submitImpl = async () => ({ order_no: ORDER_NO, status: 'ISSUE' })
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.find(openReportSel).trigger('click')
    expect(popupVisible(wrapper)).toBe('true')
    expect((wrapper.find('input[value="ISSUE"]').element as HTMLInputElement).checked).toBe(true)

    await wrapper.get('textarea[aria-label="说明原因"]').setValue('账号掉线')
    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    expect(firstCallTo('/report-issue/')).toMatchObject({
      method: 'post',
      url: `/api/business/orders/${ORDER_NO}/report-issue/`,
      data: { reason: '账号掉线' },
    })
    expect(wrapper.find(statusBadgeSel).text()).toBe('问题单')
    expect(wrapper.find(actionsSel).exists()).toBe(false)
    expect(detailCalls).toBe(1)
    expect(toast.success).toHaveBeenCalledWith('已报问题单')
  })

  it('提交失败（1002）：toast = 后端 msg 原样；状态与两键不变；不重取详情', async () => {
    loginSession()
    submitImpl = async () => {
      throw new BizError(1002, '只有代练中的单可以提交，当前状态：PENDING_REVIEW')
    }
    const wrapper = mountDetail()
    await flushPromises()

    await wrapper.find(openSubmitSel).trigger('click')
    await wrapper.find(dialogConfirmSel).trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('只有代练中的单可以提交，当前状态：PENDING_REVIEW')
    expect(wrapper.find(statusBadgeSel).text()).toBe('代练中')
    expect(wrapper.find(actionsSel).exists()).toBe(true)
    expect(detailCalls).toBe(1)
  })

  it('上一个订单仍在提交时，可以为当前订单打开独立的提交弹窗', async () => {
    loginSession()
    let resolveOldSubmit!: (value: unknown) => void
    submitImpl = () => new Promise((resolve) => { resolveOldSubmit = resolve })
    const wrapper = mountDetail()
    await flushPromises()
    await wrapper.get(openSubmitSel).trigger('click')
    await wrapper.get(dialogConfirmSel).trigger('click')
    expect(wrapper.get(dialogConfirmSel).attributes('disabled')).toBeDefined()

    const nextNo = '10000000000000000003'
    detailImpl = async () => detailFixture({ order_no: nextNo })
    h.params.orderNo = nextNo
    await flushPromises()
    await wrapper.get(openSubmitSel).trigger('click')
    expect(wrapper.get(dialogConfirmSel).attributes('disabled')).toBeUndefined()
    expect(popupVisible(wrapper)).toBe('true')

    resolveOldSubmit({ order_no: ORDER_NO, status: 'PENDING_REVIEW' })
    await flushPromises()
    expect(popupVisible(wrapper)).toBe('true')
    expect(wrapper.get(statusBadgeSel).text()).toBe('代练中')
  })
})

describe('m 端订单详情（OrderDetailView）— 取数失败与加载态', () => {
  it('403（不是你接的单）：错误态显示后端 msg 原样 + 「返回」、没有「重试」', async () => {
    loginSession()
    detailImpl = async () => {
      throw new HttpError(403, '该单不是你接的，不能查看详情')
    }
    const wrapper = mountDetail()
    await flushPromises()

    expect(wrapper.find(errorSel).exists()).toBe(true)
    expect(wrapper.text()).toContain('该单不是你接的，不能查看详情')
    expect(wrapper.find('[data-testid="detail-back"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-error-retry"]').exists()).toBe(false)
    expect(toast.error).toHaveBeenCalledWith('该单不是你接的，不能查看详情')

    await wrapper.find('[data-testid="detail-back"]').trigger('click')
    expect(h.back).toHaveBeenCalledTimes(1)
  })

  it('其它错误（业务码 1002）：msg 原样 + 「重试」；重试成功后卡片渲染出来', async () => {
    loginSession()
    detailImpl = async () => {
      throw new BizError(1002, '记录不存在或已被删除')
    }
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.text()).toContain('记录不存在或已被删除')
    expect(wrapper.find('[data-testid="detail-error-retry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-back"]').exists()).toBe(true)

    detailImpl = async () => detailFixture()
    await wrapper.find('[data-testid="detail-error-retry"]').trigger('click')
    await flushPromises()
    expect(detailCalls).toBe(2)
    expect(wrapper.find(statusBadgeSel).text()).toBe('代练中')
    expect(wrapper.text()).not.toContain('记录不存在或已被删除')
  })

  it('加载中：取数在途显示「加载中…」，落地后渲染卡片', async () => {
    loginSession()
    detailImpl = () => new Promise<MyOrderDetail>(() => {})
    const wrapper = mountDetail()
    expect(wrapper.find(loadingSel).exists()).toBe(true)
    expect(wrapper.find(statusBadgeSel).exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('m 端订单详情（OrderDetailView）— 订单截图入口（M2）', () => {
  const shotsBtnSel = '[data-testid="detail-open-screenshots"]'
  const shotsCardSel = '[data-testid="detail-card-screenshots"]'
  const screenshotPath = `/api/business/orders/${ORDER_NO}/screenshots`
  const shotRow = (id: number, img_type: number, url: string): ScreenshotItem => ({
    id, img_type, url, original_name: `图 ${id}`, size_bytes: 64,
    remark: '', uploaded_by: 7, created_at: '2026-10-10T08:05:00+08:00',
  })

  it('先核对订单归属，再展示本单的进度截图；较新的群码只显示在群聊卡', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.view_screenshot'] })
    let resolveDetail!: (value: MyOrderDetail) => void
    detailImpl = () => new Promise((resolve) => { resolveDetail = resolve })
    const items = [
      shotRow(2, 1, 'https://signed.test/old-qr'),
      { ...shotRow(4, 0, 'https://signed.test/one'), thumbnail_url: 'https://signed.test/thumb-one' },
      shotRow(6, 1, 'https://signed.test/latest-qr'),
      shotRow(7, 0, 'https://signed.test/two'),
      shotRow(8, 1, ''),
      shotRow(9, 2, 'https://signed.test/other'),
    ]
    shotsImpl = async () => ({ items, total: items.length })
    const wrapper = mountDetail()
    await flushPromises()
    expect(requestMock).toHaveBeenCalledTimes(1)
    expect(wrapper.find(shotsCardSel).exists()).toBe(false)

    resolveDetail(detailFixture({ source_order_no: '外部单号-甲' }))
    await flushPromises()
    expect(firstCallTo('/screenshots')).toMatchObject({ url: screenshotPath, method: 'GET' })
    expect(wrapper.findAll('[role="heading"]').map((heading) => heading.text())).toEqual([
      '代练内容', '本单群聊', '订单截图', '订单信息',
    ])
    expect(wrapper.get(qrImgSel).attributes('src')).toBe('https://signed.test/latest-qr')
    const shotsCard = wrapper.get(shotsCardSel)
    const thumbs = shotsCard.findAll('img')
    expect(thumbs.map((image) => image.attributes('src'))).toEqual([
      'https://signed.test/thumb-one', 'https://signed.test/two',
    ])
    await thumbs[1]!.trigger('click')
    const viewer = shotsCard.get(viewerSel)
    expect(viewer.attributes('data-visible')).toBe('true')
    expect(viewer.attributes('index')).toBe('1')
    expect(JSON.parse(viewer.attributes('data-images')!)).toEqual([
      'https://signed.test/one', 'https://signed.test/two',
    ])
    expect(wrapper.get('[data-testid="detail-qr-viewer"]').attributes('data-visible')).toBe('false')
  })

  it('同一页面换单时，上一单晚到的截图与群码不会混进当前订单', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.view_screenshot'] })
    const nextNo = '10000000000000000003'
    let resolveOldShots!: (value: { items: ScreenshotItem[]; total: number }) => void
    shotsImpl = ({ url }) => url === screenshotPath
      ? new Promise((resolve) => { resolveOldShots = resolve })
      : Promise.resolve({ items: [shotRow(10, 0, 'https://signed.test/current')], total: 1 })
    const wrapper = mountDetail()
    await flushPromises()
    expect(firstCallTo('/screenshots').url).toBe(screenshotPath)

    detailImpl = async () => detailFixture({ order_no: nextNo, group_qr_url: 'https://signed.test/current-qr' })
    h.params.orderNo = nextNo
    await flushPromises()
    const card = wrapper.get(shotsCardSel)
    expect(card.get('img').attributes('src')).toBe('https://signed.test/current')
    expect(wrapper.get(qrImgSel).attributes('src')).toBe('https://signed.test/current-qr')

    resolveOldShots({ items: [shotRow(11, 0, 'https://signed.test/old'), shotRow(12, 1, 'https://signed.test/old-qr')], total: 2 })
    await flushPromises()
    expect(card.findAll('img').map((image) => image.attributes('src'))).toEqual(['https://signed.test/current'])
    expect(wrapper.get(qrImgSel).attributes('src')).toBe('https://signed.test/current-qr')
  })

  it('后端拒绝详情后不读取截图，后端拒绝截图时说明权限问题', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.view_screenshot'] })
    detailImpl = async () => { throw new HttpError(403, '该单不是你接的') }
    const deniedDetail = mountDetail()
    await flushPromises()
    expect(requestMock).toHaveBeenCalledTimes(1)
    expect(deniedDetail.find(shotsCardSel).exists()).toBe(false)
    deniedDetail.unmount()

    detailImpl = async () => detailFixture()
    shotsImpl = async () => { throw new HttpError(403, '没有截图权限') }
    const deniedShots = mountDetail()
    await flushPromises()
    expect(deniedShots.get(shotsCardSel).text()).toContain('没有查看订单截图的权限')
    expect(deniedShots.find('[data-testid="detail-shots-retry"]').exists()).toBe(false)
    expect(deniedShots.find(shotsBtnSel).exists()).toBe(false)
  })

  it('截图读取失败可只重试截图，成功但没有图片时显示空态', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.view_screenshot'] })
    shotsImpl = async () => { throw new Error('读取截图超时') }
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.get(shotsCardSel).text()).toContain('读取截图超时')
    shotsImpl = async () => ({ items: [], total: 0 })
    await wrapper.get('[data-testid="detail-shots-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.get(shotsCardSel).text()).toContain('暂无订单截图')
    expect(detailCalls).toBe(1)
  })

  it('有 order.view_screenshot → 入口渲染；点击按单号跳截图页', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.view_screenshot'] })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.get(shotsBtnSel).text()).toBe('截图详情')
    await wrapper.get(shotsBtnSel).trigger('click')
    expect(h.push).toHaveBeenCalledWith({
      name: 'm-order-screenshots',
      params: { orderNo: ORDER_NO },
    })
  })

  it('只有上传权限时仍可点「上传截图」，页面不读取已有截图', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.upload_screenshot'] })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.get(shotsCardSel).text()).toContain('没有查看订单截图的权限')
    expect(requestMock).toHaveBeenCalledTimes(1)
    expect(wrapper.get(shotsBtnSel).text()).toBe('上传截图')
    await wrapper.get(shotsBtnSel).trigger('click')
    expect(h.push).toHaveBeenCalledWith({ name: 'm-order-screenshots', params: { orderNo: ORDER_NO } })
  })

  it('两个截图节点都没有 → 入口不渲染（fail-closed，不置灰；先例 D-0154）', async () => {
    loginSession({ menu_permissions: ['my_orders', 'order.submit_work'] })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(shotsBtnSel).exists()).toBe(false)
    expect(wrapper.get(shotsCardSel).text()).toContain('没有查看订单截图的权限')
    expect(requestMock).toHaveBeenCalledTimes(1)
  })

  it('取不到 me → 入口不渲染（fail-closed）', async () => {
    loginSession({ menu_permissions: null })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(shotsBtnSel).exists()).toBe(false)
  })
})

describe('m 端订单详情（OrderDetailView）— D-0301 来源单号优先', () => {
  beforeEach(() => {
    loginSession()
    detailImpl = async () => detailFixture({ source_order_no: 'SRC-TEST-1' })
    wireRequestMock()
  })

  it('⭐ 来源号非空 ⇒ 单号行逐字显示来源号、复制键复制的就是它', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const wrapper = mountDetail()
    await flushPromises()

    const row = rowOf(wrapper, 'detail-row-order-no')
    expect(row.text()).toContain('SRC-TEST-1')
    // ⛔ 内置号不再出现在单号行（来源号非空时）。
    expect(row.text()).not.toContain(ORDER_NO)
    await row.find('[data-testid="detail-copy-order-no"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('SRC-TEST-1')
  })

  it('⭐ 来源号为空 ⇒ 单号行回退内置号（回落变体）', async () => {
    detailImpl = async () => detailFixture({ source_order_no: '' })
    const wrapper = mountDetail()
    await flushPromises()
    const row = rowOf(wrapper, 'detail-row-order-no')
    expect(row.text()).toContain(ORDER_NO)
    expect(row.text()).not.toContain('SRC-TEST-1')
  })
})

describe('订单详情内的内部沟通独立加载', () => {
  const openChatSel = '[data-testid="detail-open-internal-chat"]'
  const chatUrl = `/api/internal-chat/orders/${ORDER_NO}/`
  const memberGroup: OrderChatSummary = {
    id: 81,
    title: '本单内部群聊',
    order_no: '10000000000000000003',
    local_order_no: ORDER_NO,
    is_member: true,
    can_join: true,
    self_member_id: 17,
    members: [],
  }

  it('提示用微信联系老板，点击内部沟通一次就打开本单群', async () => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    const fallback = requestMock.getMockImplementation()!
    requestMock.mockImplementation((config: AxiosRequestConfig) =>
      config.url === chatUrl ? Promise.resolve(memberGroup) : fallback(config))
    const openChat = vi.fn()
    const wrapper = mountDetail(openChat)
    await flushPromises()

    expect(openChat).not.toHaveBeenCalled()
    await wrapper.get(openChatSel).trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()

    expect(firstCallTo(chatUrl)).toMatchObject({ method: 'GET', url: chatUrl })
    expect(openChat).toHaveBeenCalledExactlyOnceWith(81)
    expect(wrapper.find('[data-v2="order-chat-pane"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('此群聊仅供内部沟通。请用微信扫描「本单群聊」二维码进群，联系老板上号。')
    expect(wrapper.get(qrImgSel).attributes('src')).toBe(detailFixture().group_qr_url)
  })

  it('可加入的群在入口点击后加入并打开同一个会话', async () => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    const fallback = requestMock.getMockImplementation()!
    const joinUrl = `${chatUrl}join/`
    requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
      if (config.url === chatUrl) return { ...memberGroup, is_member: false, self_member_id: null }
      if (config.url === joinUrl) return memberGroup
      return fallback(config)
    })
    const openChat = vi.fn()
    const wrapper = mountDetail(openChat)
    await flushPromises()

    await wrapper.get(openChatSel).trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()

    expect(firstCallTo(joinUrl)).toMatchObject({ method: 'POST', url: joinUrl })
    expect(openChat).toHaveBeenCalledExactlyOnceWith(81)
  })

  it.each([
    { status: 403, message: '订单权限已撤销', showsHistory: false },
    { status: 403, message: '没有聊天权限', showsHistory: false },
    { status: 403, message: '登录状态已变更，请刷新页面后再打开聊天', showsHistory: false },
    { status: 401, message: '聊天登录已过期', showsHistory: false },
    { status: 503, message: '暂时无法确认订单权限，请稍后重试', showsHistory: false },
    { status: 404, message: '未找到。', notice: '订单群聊已不可用', showsHistory: false },
    { status: 200, businessCode: 1002, message: '记录不存在或已被删除', notice: '订单群聊已不可用', showsHistory: false },
    { status: 403, message: '群聊已结束，仅可查看历史消息', showsHistory: true },
  ])('一键加入失败后按权限决定是否保留聊天记录（$message）', async ({ status, businessCode, message, notice, showsHistory }) => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    const fallback = requestMock.getMockImplementation()!
    const joinUrl = `${chatUrl}join/`
    requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
      if (config.url === chatUrl) return { ...memberGroup, is_member: false, self_member_id: null }
      if (config.url === joinUrl) {
        if (businessCode !== undefined) throw new BizError(businessCode, message)
        if (status === 401) throw new AuthExpiredError(message)
        throw new HttpError(status, message)
      }
      return fallback(config)
    })
    const openChat = vi.fn()
    const wrapper = mountDetail(openChat)
    await flushPromises()

    await wrapper.get(openChatSel).trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()

    expect(firstCallTo(joinUrl)).toMatchObject({ method: 'POST', url: joinUrl })
    expect(wrapper.get('[role="alert"]').text()).toContain(notice ?? message)
    expect(wrapper.find('iframe[title="订单群聊天记录"]').exists()).toBe(showsHistory)
    expect(openChat).not.toHaveBeenCalled()
  })

  it('详情切到另一单后，上一单晚到的群聊不会打开', async () => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    let resolveOldChat!: (value: OrderChatSummary) => void
    const pendingChat = new Promise<OrderChatSummary>((resolve) => { resolveOldChat = resolve })
    const fallback = requestMock.getMockImplementation()!
    requestMock.mockImplementation((config: AxiosRequestConfig) =>
      config.url === chatUrl ? pendingChat : fallback(config))
    const openChat = vi.fn()
    const wrapper = mountDetail(openChat)
    await flushPromises()

    await wrapper.get(openChatSel).trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(firstCallTo(chatUrl)).toMatchObject({ method: 'GET' })

    const nextOrderNo = '10000000000000000004'
    detailImpl = async () => detailFixture({ order_no: nextOrderNo })
    h.params.orderNo = nextOrderNo
    resolveOldChat(memberGroup)
    await flushPromises()

    expect(openChat).not.toHaveBeenCalled()
    expect(wrapper.get(openChatSel).text()).toBe('打开内部沟通')
    expect(wrapper.get('[data-testid="detail-row-order-no"]').text()).toContain(nextOrderNo)
  })

  it('内聊接口拒绝后保留订单详情和微信二维码，且不打开会话', async () => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    const fallback = requestMock.getMockImplementation()!
    requestMock.mockImplementation(async (config: AxiosRequestConfig) => {
      if (config.url === chatUrl) throw new HttpError(403, '订单权限已撤销')
      return fallback(config)
    })
    const openChat = vi.fn()
    const wrapper = mountDetail(openChat)
    await flushPromises()

    await wrapper.get(openChatSel).trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('订单权限已撤销')
    expect(wrapper.get(qrImgSel).attributes('src')).toBe(detailFixture().group_qr_url)
    expect(openChat).not.toHaveBeenCalled()
  })

  it('代练要求下方展示本单群聊、内部沟通和截图入口，再显示单号等信息', async () => {
    loginSession({
      internal_chat_enabled: true,
      roles: [WORKER_ROLE],
      menu_permissions: ['my_orders', 'order.view_screenshot', 'order.upload_screenshot'],
    })
    const wrapper = mountDetail()
    await flushPromises()

    const expected = [
      'detail-row-content', 'detail-row-demand', 'detail-card-qr', 'detail-open-internal-chat',
      'detail-open-screenshots', 'detail-row-order-no', 'detail-row-game-account',
    ]
    const selectors = expected.map((id) => `[data-testid="${id}"]`).join(', ')
    expect(wrapper.findAll(selectors).map((element) => element.attributes('data-testid'))).toEqual(expected)
  })

  it.each([{ enabled: false, worker: true }, { enabled: true, worker: false }])('沿用手机聊天开关和打手身份，不可用时不额外请求聊天 %j', async ({ enabled, worker }) => {
    loginSession({ internal_chat_enabled: enabled, roles: worker ? [WORKER_ROLE] : [] })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find('[data-testid="detail-open-internal-chat"]').exists()).toBe(false)
    expect(requestMock.mock.calls.some(([config]) => String(config.url).includes('/internal-chat/'))).toBe(false)
  })

  it('展开后才拉取空摘要，聊天503不影响已加载订单及交单按钮', async () => {
    loginSession({ internal_chat_enabled: true, roles: [WORKER_ROLE] })
    const fallback = requestMock.getMockImplementation()!
    requestMock.mockImplementation((config: AxiosRequestConfig) => {
      if (String(config.url).includes('/api/internal-chat/')) throw new HttpError(503, '内部沟通暂时不可用')
      return fallback(config)
    })
    const wrapper = mountDetail()
    await flushPromises()
    expect(wrapper.find(openSubmitSel).exists()).toBe(true)
    expect(requestMock.mock.calls.some(([config]) => String(config.url).includes('/internal-chat/'))).toBe(false)
    await wrapper.get('[data-testid="detail-open-internal-chat"]').trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(wrapper.text()).toContain('内部沟通暂时不可用')
    expect(wrapper.find(openSubmitSel).exists()).toBe(true)
    expect(wrapper.text()).toContain('测试游戏甲')
    expect(requestMock.mock.calls.filter(([config]) => String(config.url).includes('/internal-chat/')))
      .toEqual([[{ method: 'GET', url: `/api/internal-chat/orders/${ORDER_NO}/` }]])
    await wrapper.get(openSubmitSel).trigger('click')
    await wrapper.get(dialogConfirmSel).trigger('click')
    await flushPromises()
    expect(firstCallTo('/submit/')).toMatchObject({ method: 'post', url: `/api/business/orders/${ORDER_NO}/submit/` })
    expect(wrapper.find(statusBadgeSel).text()).toBe('待验收')
  })
})
