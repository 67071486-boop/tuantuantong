// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【第三方平台订单管理】（`order-coreordercenter`）的页测试 —— T11a 起**接真数据**。
 *
 * AGENTS.md §5-G 的四条陷阱逐条照做：
 *  ① 类名与选择器分成两个常量（生产侧已做，本文件只用 `#id` 选择器）；
 *  ② 以 `(` 开头的语句前置分号（本文件没有这种语句）；
 *  ③ **先 `await bootstrapIsland(...)` 挂样式、再塞页面标记**——用 `LAYUI.order` 记两个时刻并断言顺序；
 *     `initIslandElements(layui)` 必须留在塞标记之后（生产侧顺序，本文件靠真 DOM 点击验证它真的生效）；
 *  ④ ⭐ N7 起页里**有一个本地定时器**（验收倒计时，1 秒一次）⇒ 本文件用 **spy + 卸载**收口：
 *     `mountPage` 把卸载函数存进 `unmounters`，`afterEach` 统一调用（E-0198 的「每个用例自己收口」），
 *     定时器本身只改文本、**不发请求**（另有用例钉住）。
 *
 * ⚠️ **断言打在「页面真正交给 layui 的那一份配置」上**（`table.render` 的 `data` / `text`、
 * `laypage.render` 的 `count`），不是「某个元素存在」：这是「页面展示的字段来自接口返回」的判据。
 * 传输层替身打在 `@/api/http`（`request`）上 ⇒ 页面里 `api/dailiantongPush` 的 URL / 参数拼装
 * **走真身**，首屏请求参数测得的是真配置。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  /** 挂载期两个动作**真实发生的次序**（`bootstrap` = 挂样式，`artifact` = 取证据件）。 */
  const order: string[] = []
  const formRender = vi.fn()
  const tableRender = vi.fn()
  const tableOn = vi.fn()
  const laypageRender = vi.fn()
  const laydateRender = vi.fn()
  // 11 颗工具栏按钮要开确认层 / 输入层，还要 `layer.close` 关掉它们。
  const confirm = vi.fn()
  const prompt = vi.fn()
  const layerClose = vi.fn()
  // 勾选行由 `table.checkStatus('datatable').data` 取（对标同一句）。
  const checkStatus = vi.fn()
  // 行内「编辑 / 申请撤销」要开（表单型）弹层 ⇒ 需要一个可抓取参数的 `layer.open`。
  const layerOpen = vi.fn()
  return {
    msg,
    order,
    formRender,
    tableRender,
    tableOn,
    laypageRender,
    laydateRender,
    confirm,
    prompt,
    layerClose,
    layerOpen,
    checkStatus,
    form: { render: formRender, on: vi.fn() },
    table: { render: tableRender, on: tableOn, reloadData: vi.fn(), checkStatus },
    layer: { msg, confirm, prompt, close: layerClose, open: layerOpen },
    laydate: { render: laydateRender },
    laypage: { render: laypageRender },
    element: { init: vi.fn() },
  }
})

const API = vi.hoisted(() => ({ request: vi.fn() }))

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // 写成「真正被调用时才执行」的函数（不是立即求值的 Promise），否则 `order` 记不到时刻。
    bootstrapIsland: (() => {
      LAYUI.order.push('bootstrap')
      return Promise.resolve(LAYUI)
    }) as unknown as typeof actual.bootstrapIsland,
  }
})

// formselects 在 jsdom 里没有实例（`window.layui` 不存在）⇒ 换成 no-op；本文件不测它。
vi.mock('@/layui/vendor', () => ({ initFormSelects: vi.fn() }))

// 传输层替身：`fetchPlatformPushes()` 走真身，只把最下面的 `request()` 换掉。
vi.mock('@/api/http', () => ({ request: API.request }))

// ⚠️ 证据件取回**不**走 `config-replica` 的模块级缓存：`order` 必须记到**这一次挂载**的取数时刻，
// 否则「先 bootstrap 再塞标记」那条顺序断言会被前一个用例的缓存喂成假通过。
vi.mock('@/layui-island/config-replica', () => ({
  loadConfigArtifact: async (key: string): Promise<string> => {
    // 「取证据件」这个时刻由下面的 `fetch` 替身记（它同时是唯一允许发生的请求）。
    const res = await fetch(`/replica/${key}.html`)
    return res.text()
  },
}))

import type { Page } from '@/api/unwrap'
import { BizError, HttpError, NetworkError } from '@/api/errors'
import { request } from '@/api/http'
import {
  ACCEPT_CONFIRM_TEXT,
  ARBITRATION_CONFIRM_TEXT,
  CLEAR_TAG_CONFIRM_TEXT,
  CONTEXT_MENU_ITEMS,
  CONTEXT_MENU_STYLE_ID,
  COPY_ORDER_CONTENT_FAILED_MESSAGE,
  COPY_ORDER_CONTENT_SUCCESS_MESSAGE,
  COPY_ORDER_ID_FAILED_MESSAGE,
  COPY_ORDER_ID_SUCCESS_PREFIX,
  COPY_ORDER_ID_SUCCESS_SUFFIX,
  DATE_SPAN_MSG,
  ROW_DANGER_COLOR,
  ROW_WRITE_UNKNOWN_NOTE,
  HIDDEN_TOOLBAR_FILTERS,
  NO_DATA_SOURCE_TEXT,
  ORDER_CHILD_ROW_CLASS,
  ORDER_CHILD_ROW_SELECTOR,
  ORDER_DETAIL_LINK_SELECTOR,
  ORDER_LINE_CLASS,
  ORDER_LINE_SELECTOR,
  ORDER_NO_INVALID_MESSAGE,
  ORDER_TREE_CLASS,
  ORDER_TREE_SELECTOR,
  PAGE_ELEM_SELECTOR,
  PENDING_TAB_LABEL,
  REFUND_REASON,
  RESET_TABLE_LABEL,
  ROW_ACTION_AMOUNT,
  ROW_ACTION_BUTTONS,
  ROW_ACTION_EDIT,
  ROW_ACTION_LABELS,
  ROW_ACTION_NEGOTIATE,
  ROW_ACTION_STATUSES,
  ROW_ACTION_WITHDRAW,
  ROW_AMOUNT_INVALID_MESSAGE,
  ROW_EDIT_EMPTY_MESSAGE,
  ROW_EDIT_MEMO_ID,
  ROW_EDIT_TITLE_ID,
  ROW_NEGOTIATE_AUTO_ID,
  ROW_NEGOTIATE_COMMENT_ID,
  ROW_NEGOTIATE_DEFAULT_BALANCE,
  ROW_NEGOTIATE_ENSURE_ID,
  ROW_NEGOTIATE_ENSURE_INVALID,
  ROW_NEGOTIATE_PAY_ID,
  ROW_NEGOTIATE_PAY_INVALID,
  ROW_NEGOTIATE_TITLE,
  ROW_TARGET_LATEST,
  ROW_TARGET_MISSING_MESSAGE,
  ROW_WITHDRAW_CONFIRM_TEXT,
  groupStatusPush,
  latestActivePush,
  rowActionKeysFor,
  SCREENSHOT_MANAGE_PAGE,
  SCREENSHOT_ORDER_NO_INVALID_MESSAGE,
  SELECT_REQUIRED_MSG,
  SOURCE_ORDER_NO_COLOR,
  STATUS_SELECT_ID,
  SYNC_CONFIRM_TEXT,
  TAB_COUNTS_FAILED_PREFIX,
  TOOLBAR_ACTION_FILTERS,
  UNSUPPORTED_FILTERS,
  URGENT_CONFIRM_TEXT,
  UNSUPPORTED_FILTER_PREFIX,
  WITHDRAW_CONFIRM_TEXT,
  applyTabCounts,
  VERIFY_COUNTDOWN_CLASS,
  VERIFY_TICK_MS,
  actionRowTemplate,
  childCellHtml,
  custCancelAlert,
  formatDateTime,
  formatShoursRemaining,
  shoursCountdownHtml,
  formatVerifyCountdown,
  isNegativeMoney,
  orderSourceText,
  profitTextOf,
  tickVerifyCountdowns,
  verifyRemainingSeconds,
  groupPlatformCountText,
  hideUnsupportedToolbarButtons,
  PLATFORM_WRITE_TOOLBAR_FILTERS,
  isNoDataSourceTab,
  mountCoreOrderCenterPage,
  platformLabelOf,
  platformOrderHint,
  registerUnsupportedFilters,
  sourceOrderNoOf,
  sourceOrderTemplet,
  statusDialogHtml,
  statusForTab,
  syncFixedRowHeights,
  toDisplayRow,
  toGroupDisplayRow,
  toSinglePushDisplayRow,
  unsupportedFilterHits,
  unsupportedFiltersMessage,
  type CoreOrderDisplayRow,
} from '../order-coreordercenter'
import {
  applyPlatformWritePermission,
  canPlatformWrite,
  platformWriteAllowedNow,
  resetPlatformWriteAllowedForTests,
  setPlatformWriteNodeShipped,
} from '@/layui-island/perms'
import {
  PLATFORM_PUSH_ACTIONS_URL,
  PLATFORM_PUSH_COUNTS_URL,
  type PlatformPushCounts,
  type PlatformPushGroupItem,
  type PlatformPushListRow,
  type PlatformPushSubRow,
  type PlatformPushSummary,
} from '@/api/dailiantongPush'
import {
  platformDisplayStatus,
  platformStatusBadgeClass,
} from '@/layui-island/platformPush'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-coreordercenter.html'),
  'utf8',
)

/** 一行**真数据形状**的接口回包（14 个键，与后端 `PlatformPushRowSerializer` 同形）。 */
const ROW: PlatformPushListRow = {
  id: 7,
  platform: 10,
  serial_no: '19000000000000000201',
  order_no: '20261027000000000001',
  game: '原神',
  server: '天空岛',
  price: '5.00',
  state: 'sent',
  remote_status: 12,
  remote_cancel_status: null,
  order_status: 'IN_PROGRESS',
  created_at: '2026-10-27T09:30:00+08:00',
  last_synced_at: '2026-10-27T10:00:00+08:00',
  last_error: '',
}

function pageOf(
  items: PlatformPushListRow[] = [ROW],
  total: number = items.length,
  page = 1,
  page_size = 30,
): Page<PlatformPushListRow> {
  return { items, total, page, page_size }
}

/**
 * 页签数字的默认回包（`counts/`；一个「全部 + 待接单」有数、其余 0 的最小真形状）。
 * 需要区分各页签时用 `countsOf({...})` 覆盖。
 */
const COUNTS: PlatformPushCounts = {
  total: 1,
  counts: {
    '10': 0,
    '11': 1,
    '12': 0,
    '13': 0,
    '14': 0,
    '15': 0,
    '16': 0,
    '17': 0,
    // ⭐ H7：18 = 已下架（真机码），后端 counts 桶**恒在**（0 也回）。
    '18': 0,
    none: 0,
  },
  states: { pending: 0, sent: 1, failed: 0, unknown: 0, closed: 0 },
}

function countsOf(overrides: Partial<PlatformPushCounts> = {}): PlatformPushCounts {
  return {
    ...COUNTS,
    ...overrides,
    counts: { ...COUNTS.counts, ...(overrides.counts ?? {}) },
  }
}

const requestMock = vi.mocked(request)

/** 列表请求（`/platform-pushes/`）——**不含** counts 那一发（两件事分别断言）。 */
function listRequests(): Array<{
  method: string
  url: string
  params: Record<string, unknown>
}> {
  return requestMock.mock.calls
    .map(
      (call) =>
        call[0] as { method: string; url: string; params: Record<string, unknown> },
    )
    .filter((config) => config.url === '/api/business/platform-pushes/')
}

/** counts 请求（页签数字一次取回）。 */
function countsRequests(): Array<{
  method: string
  url: string
  params: Record<string, unknown>
}> {
  return requestMock.mock.calls
    .map(
      (call) =>
        call[0] as { method: string; url: string; params: Record<string, unknown> },
    )
    .filter((config) => config.url === PLATFORM_PUSH_COUNTS_URL)
}

/**
 * 默认回包：列表给 `page`，counts 给 `counts`（按 URL 分流）。
 *
 * 页面挂载会打**两发**（列表 + counts）⇒ 旧写法 `mockResolvedValue(pageOf())` 会让 counts
 * 拿到一个没有 `counts` 的页对象（页面会报「页签数字加载失败」）。这里统一分流。
 */
function mockApi(
  page: unknown = pageOf(),
  counts: PlatformPushCounts = COUNTS,
): void {
  requestMock.mockImplementation((config) =>
    Promise.resolve(
      (config as { url?: string }).url === PLATFORM_PUSH_COUNTS_URL
        ? counts
        : page,
    ),
  )
}

/** 本次用例挂过的页面卸载函数（`afterEach` 统一调用 ⇒ 定时器 / 监听器不泄漏，E-0198）。 */
const unmounters: Array<() => void> = []

/** 卸掉最近挂的那一页（卸载函数由 island 页面返回）。 */
function unmountLast(): void {
  unmounters.pop()?.()
}

/** 挂整页：证据件走全局 `fetch`（真文件），layui 与传输层走替身。 */
async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  unmounters.push(await mountCoreOrderCenterPage(root, { onNavigate: vi.fn() }))
  return root
}

/** 14 个状态页签的**基准文案**（第 2 个是「待接单」——证据件原文是「未接手」）。 */
const TAB_LABELS = [
  '全部',
  '待接单',
  '代练中',
  '已锁定',
  '异常中',
  '待验收',
  '撤销中',
  '仲裁中',
  '已仲裁',
  '已结算',
  '已撤销',
  '已下架',
  '已删除',
  '新交易消息',
]

/** 12 个筛选控件的 `id`（第 2 项 `dateType` 与区间框是两个控件）。 */
const FILTER_IDS = [
  'gameName',
  'dateType',
  'datetime',
  'operator',
  'orderSoures',
  'pushPlatm',
  'orderSta',
  'projectType',
  'seachType',
  'key1',
  'orderLabel',
  'orderBy',
]

/** 15 颗工具栏按钮的**可见文字**（顺序 = `#toolbarDemo` 里的顺序）。 */
const TOOLBAR_LABELS = [
  '刷新',
  '批量撤回',
  '批量验收',
  '批量仲裁',
  '同步交易状态',
  '改系统状态',
  '记退款',
  '更改标签',
  '改标记',
  '删标记',
  '加急',
  '改接单价',
  '发送短信',
  '导出订单',
  '平台消单数据统计',
]

function tabItems(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      '.layui-tab[lay-filter="staTab"] > .layui-tab-title > li[lay-id]',
    ),
  )
}

/** `table.render` 收到的**最后一次**配置。 */
function tableOptions(): Record<string, unknown> {
  const calls = LAYUI.tableRender.mock.calls
  const call = calls[calls.length - 1] as unknown as [Record<string, unknown>]
  return call[0]
}

/** `table.render` 收到的最后一屏数据（页面真正交给 layui 的那一份）。 */
function renderedRows(): CoreOrderDisplayRow[] {
  return tableOptions()['data'] as CoreOrderDisplayRow[]
}

/** `laypage.render` 收到的**最后一次**配置。 */
function pagerOptions(): Record<string, unknown> {
  const calls = LAYUI.laypageRender.mock.calls
  const call = calls[calls.length - 1] as unknown as [Record<string, unknown>]
  return call[0]
}

/** 取**列表**请求的第 n 次配置（0 起；counts 那一发不算，按 URL 过滤）。 */
function requestConfig(index: number): {
  method: string
  url: string
  params: Record<string, unknown>
} {
  const call = listRequests()[index]
  if (call === undefined) throw new Error(`没有第 ${index} 发列表请求`)
  return call
}

/* ───────── 工具栏 11 颗接真的测试工具 ───────── */

/** 一次批量动作的结果（后端 `PlatformPushActionView` 的 `data`）。 */
function actionOutcome(
  action: string,
  items: Array<{ order_no: string; ok: boolean; msg: string }>,
): Record<string, unknown> {
  const ok = items.filter((item) => item.ok).length
  return { action, items, ok_count: ok, fail_count: items.length - ok }
}

/** 列表请求回 `pageOf()`；counts 回默认计数；批量动作请求回给定结果。 */
function mockActionApi(result: Record<string, unknown>): void {
  requestMock.mockImplementation((config) => {
    const url = (config as { url?: string }).url
    if (url === PLATFORM_PUSH_ACTIONS_URL) return Promise.resolve(result)
    if (url === PLATFORM_PUSH_COUNTS_URL) return Promise.resolve(COUNTS)
    return Promise.resolve(pageOf())
  })
}

/** 批量动作请求（按 URL 过滤；列表取数不算）。 */
function actionRequests(): Array<{
  method: string
  url: string
  data: Record<string, unknown>
}> {
  return requestMock.mock.calls
    .map(
      (call) =>
        call[0] as { method: string; url: string; data: Record<string, unknown> },
    )
    .filter((config) => config.url === PLATFORM_PUSH_ACTIONS_URL)
}

/** 最后一次批量动作请求的 body。 */
function lastActionBody(): Record<string, unknown> {
  const calls = actionRequests()
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('没有发出批量动作请求')
  return call.data
}

/** 模拟勾选：`table.checkStatus('datatable').data` 回这几行。 */
function selectRows(...orderNos: string[]): void {
  LAYUI.checkStatus.mockReturnValue({
    data: orderNos.map((orderNo) => ({ t_orderid: orderNo })),
  })
}

/** 把 `#toolbarDemo` 的标记解析成真按钮并挂进页面（与既有用例同一手法）。 */
function mountToolbar(root: HTMLElement): HTMLElement {
  const template = root.querySelector<HTMLElement>('#toolbarDemo')!
  const holder = document.createElement('div')
  holder.innerHTML = template.textContent ?? ''
  root.appendChild(holder)
  return holder
}

function toolbarButton(holder: HTMLElement, filter: string): HTMLButtonElement {
  const button = holder.querySelector<HTMLButtonElement>(
    `button[lay-filter="${filter}"]`,
  )
  if (button === null) throw new Error(`工具栏里没有 lay-filter=${filter} 的按钮`)
  return button
}

function clickToolbar(holder: HTMLElement, filter: string): void {
  toolbarButton(holder, filter).dispatchEvent(
    new MouseEvent('click', { bubbles: true }),
  )
}

/** 取最后一次 `layer.confirm` 的（正文 / 选项 / 确定回调）。 */
function lastConfirm(): {
  text: string
  options: Record<string, unknown>
  yes: (index: number) => void
} {
  const calls = LAYUI.confirm.mock.calls
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('layer.confirm 没有被调用')
  return {
    text: String(call[0]),
    options: (call[1] ?? {}) as Record<string, unknown>,
    yes: call[2] as (index: number) => void,
  }
}

/** 取最后一次 `layer.prompt` 的（选项 / 确定回调）。 */
function lastPrompt(): {
  options: Record<string, unknown>
  yes: (value: string, index: number) => void
} {
  const calls = LAYUI.prompt.mock.calls
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('layer.prompt 没有被调用')
  return {
    options: (call[0] ?? {}) as Record<string, unknown>,
    yes: call[1] as (value: string, index: number) => void,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.msg.mockClear()
  LAYUI.confirm.mockReset()
  LAYUI.prompt.mockReset()
  LAYUI.layerClose.mockReset()
  LAYUI.checkStatus.mockReset()
  LAYUI.checkStatus.mockReturnValue({ data: [] })
  LAYUI.order.length = 0
  document.body.innerHTML = ''
  requestMock.mockReset()
  mockApi()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-coreordercenter.html')) {
        LAYUI.order.push('artifact')
        return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  while (unmounters.length > 0) unmountLast()
  // ⭐ 平台写动作的权限门是**模块级状态**（`@/layui-island/perms`）⇒ 每个用例收口，
  // 否则「没权限」那一支会把后面所有用例的按钮一起藏掉（还伪装成实现回归）。
  setPlatformWriteNodeShipped(false)
  resetPlatformWriteAllowedForTests()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('挂载顺序（E-0067：先挂样式，再塞标记）', () => {
  it('bootstrapIsland 在取证据件之前；initIslandElements 在标记之后（真 DOM 页签可切）', async () => {
    const root = await mountPage()
    expect(LAYUI.order).toEqual(['bootstrap', 'artifact'])
    // 页签类名切换由真 `initIslandElements` 接管 ⇒ 点第 2 个页签，`layui-this` 必须动。
    const lis = tabItems(root)
    expect(lis).toHaveLength(14)
    lis[1]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(lis[1]!.classList.contains('layui-this')).toBe(true)
    expect(lis[0]!.classList.contains('layui-this')).toBe(false)
  })

  it('页面文档标题照对标 <title>（菜单名是「第三方平台订单管理」，两者不是同一个东西）', async () => {
    await mountPage()
    expect(document.title).toBe('交易平台订单处理')
  })
})

describe('14 个状态页签：文案 + lay-id + 计数口径（需求 5：counts 一次填齐）', () => {
  it('页签基准文案逐条对上；第 2 个是「待接单」，lay-id 仍是证据件的筛选值', async () => {
    const root = await mountPage()
    const lis = tabItems(root)
    const base = lis.map((li) => li.dataset.baseLabel ?? '')
    expect(base).toEqual(TAB_LABELS)
    expect(lis[1]!.getAttribute('lay-id')).toBe('10')
    expect(lis[1]!.classList.contains('staTab10')).toBe(true)
    expect((lis[1]!.textContent ?? '').trim()).toBe(`${PENDING_TAB_LABEL}(1)`)
    // 证据件静态文本是「未接手」——屏幕文案必须已经是「待接单」。
    expect(EVIDENCE).toContain('未接手')
  })

  it('一次 counts 请求把 9 个有数据源的页签填好；5 个没数据源的保持「—」', async () => {
    mockApi(
      pageOf(),
      countsOf({
        total: 9,
        counts: { '11': 2, '12': 3, '15': 4, '14': 5, '13': 6, '16': 7, '17': 8, '18': 9 },
      }),
    )
    const root = await mountPage()
    expect(countsRequests()).toHaveLength(1)
    expect(countsRequests()[0]!.params).toEqual({ platform: 10 })
    expect(tabItems(root).map((li) => (li.textContent ?? '').trim())).toEqual([
      '全部(9)',
      '待接单(2)',
      '代练中(3)',
      '已锁定(4)',
      '异常中(5)',
      '待验收(6)',
      '撤销中(7)',
      '仲裁中(—)',
      '已仲裁(—)',
      '已结算(8)',
      '已撤销(—)',
      '已下架(9)',
      '已删除(—)',
      '新交易消息(—)',
    ])
  })

  it('counts 失败 ⇒ 14 个页签全部「—」+ 6000ms 中文原因（⛔ 不写 0、不静默）', async () => {
    requestMock.mockImplementation((config) =>
      (config as { url?: string }).url === PLATFORM_PUSH_COUNTS_URL
        ? Promise.reject(new Error('网络断了'))
        : Promise.resolve(pageOf()),
    )
    const root = await mountPage()
    expect(
      tabItems(root).every((li) => (li.textContent ?? '').trim().endsWith('(—)')),
    ).toBe(true)
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `${TAB_COUNTS_FAILED_PREFIX}网络断了`,
      { time: 6000 },
    )
  })

  it('缺桶 ⇒ 那一档「—」（⛔ 不补 0 冒充「查过且为 0」）', () => {
    const root = document.createElement('div')
    const ids = ['0', '10', '20', '25', '30', '40', '50', '55', '56', '60', '70', '75', '80', '100']
    root.innerHTML =
      '<div class="layui-tab" lay-filter="staTab"><ul class="layui-tab-title">' +
      ids.map((id, i) => `<li lay-id="${id}">${TAB_LABELS[i]}</li>`).join('') +
      '</ul></div>'
    applyTabCounts(root, {
      total: 0,
      counts: { '11': 2 },
      states: {},
    })
    const texts = tabItems(root).map((li) => (li.textContent ?? '').trim())
    expect(texts[0]).toBe('全部(0)')
    expect(texts[1]).toBe('待接单(2)')
    expect(texts[2]).toBe('代练中(—)')
  })

  it('页签 → status 映射恰好 8 档；其余 5 档是「没有数据源」', () => {
    expect(['10', '20', '25', '30', '40', '50', '60', '75'].map(statusForTab)).toEqual([
      11, 12, 15, 14, 13, 16, 17, 18,
    ])
    expect(statusForTab('0')).toBeUndefined()
    expect(['55', '56', '70', '80', '100'].every(isNoDataSourceTab)).toBe(true)
    expect(
      ['0', '10', '20', '25', '30', '40', '50', '60', '75'].some(isNoDataSourceTab),
    ).toBe(false)
  })

  it('点页签改 `status` 并重发列表请求（第 1 页）；⛔ 不再为计数多发请求', async () => {
    const root = await mountPage()
    mockApi(pageOf([ROW], 2))
    const lis = tabItems(root)

    lis[1]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    expect(requestConfig(1).params).toEqual({
      platform: 10,
      page: 1,
      page_size: 30,
      status: 11,
      group_by: 'order',
    })
    // 列表的 total 是 2，但页签数字只来自 counts（1）⇒ **不被列表 total 覆盖**。
    expect((lis[1]!.textContent ?? '').trim()).toBe('待接单(1)')
    expect(countsRequests()).toHaveLength(1)
    // 点第 3 个页签（代练中）→ status=12。
    lis[2]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(listRequests()).toHaveLength(3))
    expect(requestConfig(2).params).toMatchObject({ status: 12, page: 1 })
  })

  it('⭐ H7：「已下架」页签（lay-id 75）按 status=18 查、计数取 counts["18"]', async () => {
    mockApi(pageOf([ROW], 1), countsOf({ counts: { '18': 3 } }))
    const root = await mountPage()
    const li = root.querySelector<HTMLElement>('li[lay-id="75"]')!
    expect((li.textContent ?? '').trim()).toBe('已下架(3)')

    li.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    expect(requestConfig(1).params).toEqual({
      platform: 10,
      page: 1,
      page_size: 30,
      status: 18,
      group_by: 'order',
    })
    // 有数据源 ⇒ 走正常列表（不是「该分类暂无数据源」那只空态）。
    expect(tableOptions()['text']).not.toEqual({ none: NO_DATA_SOURCE_TEXT })
  })

  it('没有数据源的页签：零请求、空表 +「该分类暂无数据源」', async () => {
    const root = await mountPage()
    const callsAfterMount = requestMock.mock.calls.length
    const lis = tabItems(root)

    lis[7]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 零请求（判据打在**调用次数**上，不是「没报错」）。
    expect(requestMock.mock.calls.length).toBe(callsAfterMount)
    expect(LAYUI.tableRender).toHaveBeenCalledTimes(2)
    expect(renderedRows()).toEqual([])
    expect(tableOptions()['text']).toEqual({ none: NO_DATA_SOURCE_TEXT })
    // 该页签的计数仍是「—」（没有数据源，拿不到数）。
    expect((lis[7]!.textContent ?? '').trim()).toBe('仲裁中(—)')
  })
})

describe('搜索框 → keyword（第 1 页重发）', () => {
  it('填 `#key1` 点搜索：params 带 keyword、page 复位 1、不带 status（全部）', async () => {
    const root = await mountPage()
    const input = root.querySelector<HTMLInputElement>('input#key1')!
    input.value = '  20261027  '
    root
      .querySelector('button.sechbtn')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    expect(requestConfig(1).params).toEqual({
      platform: 10,
      page: 1,
      page_size: 30,
      keyword: '20261027',
      group_by: 'order',
    })
    // counts 也带同一个 keyword 重取一次（页签数字跟着搜索条件走）。
    await vi.waitFor(() => expect(countsRequests()).toHaveLength(2))
    expect(countsRequests()[1]!.params).toEqual({
      platform: 10,
      keyword: '20261027',
    })
  })

  it('搜索条件变了 ⇒ counts 重取一次把各页签一起刷新（⛔ 不再只回填当前那个）', async () => {
    const root = await mountPage()
    const lis = tabItems(root)
    expect((lis[0]!.textContent ?? '').trim()).toBe('全部(1)')

    mockApi(pageOf([ROW], 4), countsOf({ total: 4, counts: { '11': 4 } }))
    root.querySelector<HTMLInputElement>('input#key1')!.value = 'x'
    root
      .querySelector('button.sechbtn')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(countsRequests()).toHaveLength(2))
    await vi.waitFor(() =>
      expect((lis[0]!.textContent ?? '').trim()).toBe('全部(4)'),
    )
    expect((lis[1]!.textContent ?? '').trim()).toBe('待接单(4)')
  })
})

describe('表格：字段来自接口返回（不是「元素存在」）', () => {
  it('把接口一行映射成 24 列要显示的字符串：有数据的取真值、没数据的「—」', async () => {
    await mountPage()
    const rows = renderedRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      t_tid: '19000000000000000201',
      t_orderid: '20261027000000000001',
      t_gameServer: '原神 / 天空岛',
      t_throw: '5.00',
      t_pltSta: '正在代练',
      t_sta: '代练中',
      t_hour: '2026-10-27 09:30',
      // 这两个键**不是列**，只给「平台交易单号」格的 tooltip 用。
      t_lastSynced: '2026-10-27 10:00',
      t_state: '已发出',
      t_lastError: '',
    })
    // 接口**没有**的列一律「—」（⛔ 不造 0 / 空串）。
    expect(rows[0]).toMatchObject({
      t_safeeff: '—',
      profit: '—',
      t_shours: '—',
      t_exAccpetTime: '—',
      t_endTime: '—',
      t_soure: '—',
      t_recTime: '—',
      t_complte: '—',
      t_create: '—',
      t_pushMan: '—',
      // 这四列缺键 ⇒「—」（后端未升级也不报错）。
      t_acceptUser: '—',
      t_acceptOver: '—',
      t_custCancel: '—',
      t_remoteAccept: '—',
    })
    // 删掉的 5 列：映射里也不再产出这些键（⛔ 留死代码）。
    for (const gone of ['t_remark', 't_projectTypeName', 't_orderTitle', 't_role', 't_ww']) {
      expect(rows[0]).not.toHaveProperty(gone)
    }
  })

  it('边界：没有单号 / 没有区服 / 没有回读状态 / 没有同步时间 ⇒ 各自「—」，不拼假值', () => {
    const row = toDisplayRow({
      ...ROW,
      serial_no: '',
      server: null,
      remote_status: null,
      state: 'pending',
      last_synced_at: null,
    })
    expect(row['t_tid']).toBe('—')
    expect(row['t_gameServer']).toBe('原神')
    // 回读状态还没拿到 ⇒ 用**本地状态中文**兜底（⛔ 不再显示「—」，界面必须说清这单在哪个阶段）。
    expect(row['t_pltSta']).toBe('发送中')
    expect(row['t_lastSynced']).toBe('—')
    // 列表端点直接回模型值 ⇒ `serial_no` **可能是 null**（订单列表那边是归一后的空串）。
    expect(toDisplayRow({ ...ROW, serial_no: null })['t_tid']).toBe('—')
  })

  it('交易状态按 platformPushStatusLabel：closed 优先 / 远端码优先 / 否则本地状态中文（⛔ 不显示「—」）', () => {
    expect(toDisplayRow({ ...ROW, state: 'sent', remote_status: 12 })['t_pltSta']).toBe(
      '正在代练',
    )
    // 已撤回删除的单：远端还留着 11「未接手」，本地 closed 必须压过它。
    expect(
      toDisplayRow({ ...ROW, state: 'closed', remote_status: 11 })['t_pltSta'],
    ).toBe('已关闭')
    expect(
      toDisplayRow({ ...ROW, state: 'failed', remote_status: null })['t_pltSta'],
    ).toBe('发送失败')
    expect(
      toDisplayRow({ ...ROW, state: 'unknown', remote_status: null })['t_pltSta'],
    ).toBe('状态未知')
    // 还没回读过远端、但本地是发送中 ⇒ 也是中文，不是「—」。
    expect(toDisplayRow({ ...ROW, state: 'pending', remote_status: null })['t_pltSta']).toBe(
      '发送中',
    )
    // ⭐ 真机实测：`Status=18` = 已下架（上游码表只有 10–17；⛔ 不是「未知码」）。
    expect(toDisplayRow({ ...ROW, state: 'sent', remote_status: 18 })['t_pltSta']).toBe(
      '已下架',
    )
  })

  /**
   * 交易状态是**填充色块标签**：色档按远端码一档一色（码与中文的对应就是页签 `lay-id` 那套）。
   * 我方 `remote_status` 的码表在 `platformPush.ts::REMOTE_STATUS_LABELS`。
   */
  it('P2：每个远端状态码一档底色（class 字面值照对标；填充 + 白字 + 圆角由 layui-badge 提供）', () => {
    const cls = (
      remote_status: number | null,
      state: PlatformPushSummary['state'] = 'sent',
    ): string =>
      platformStatusBadgeClass({ state, remote_status })
    // 未接手（对标页签 `lay-id="10"`）⇒ switch 的 default 档 = 灰
    expect(cls(11)).toBe('layui-badge layui-bg-gray')
    // 正在代练（对标 `lay-id="20"`）⇒ 绿（业务方截图说的「青绿底」；layui.css 里 #16baaa）
    expect(cls(12)).toBe('layui-badge layui-bg-green')
    // 等待验收（对标 `lay-id="40"`）⇒ 蓝
    expect(cls(13)).toBe('layui-badge layui-bg-blue')
    // 订单异常（对标 `lay-id="30"` 异常中）⇒ 不带底色类的默认红（layui-badge 基色 #ff5722）
    expect(cls(14)).toBe('layui-badge')
    // 已锁定（对标 `lay-id="25"`；⭐ 15 = 被我们锁定）⇒ 默认红
    expect(cls(15)).toBe('layui-badge')
    // 撤销中（对标 `lay-id="50"`）⇒ 橙
    expect(cls(16)).toBe('layui-badge layui-bg-orange')
    // 已结算（对标 `lay-id="60"`）⇒ 黑
    expect(cls(17)).toBe('layui-badge layui-bg-black')
    // 临时订单（对标没有这一档）⇒ 同样是 default 灰，⛔ 不编一档新色
    expect(cls(10)).toBe('layui-badge layui-bg-gray')
    // ⭐ 18 已下架 ⇒ **推断**档：对标 switch 没有「已下架」，
    // 语义最接近「未接手 / default」的灰 ⇒ 取灰，⛔ 不新造颜色。
    expect(cls(18)).toBe('layui-badge layui-bg-gray')
    // 未知新码 / 还没回读 ⇒ default 灰（⛔ 不猜色）
    expect(cls(99)).toBe('layui-badge layui-bg-gray')
    expect(cls(null)).toBe('layui-badge layui-bg-gray')
    // 已在平台删除（本地 closed ⇒「已关闭给灰」）优先于远端码
    expect(cls(11, 'closed')).toBe('layui-badge layui-bg-gray')
    // ⭐ 没有可判远端状态（`null` / 未知码）时，别一律灰——
    // 本地推送状态是 failed / unknown 时，交易状态标签要跟列表页红 / 橙徽标对齐，
    // 否则同一条推送在两个页面长得不一样（页面在说两套话）。
    expect(cls(null, 'failed')).toBe('layui-badge layui-bg-red')
    expect(cls(99, 'failed')).toBe('layui-badge layui-bg-red')
    expect(cls(null, 'unknown')).toBe('layui-badge layui-bg-orange')
    expect(cls(99, 'unknown')).toBe('layui-badge layui-bg-orange')
    // 正常档（sent / pending）仍走 default 灰：这次只对齐 failed / unknown 两档。
    expect(cls(null, 'sent')).toBe('layui-badge layui-bg-gray')
  })

  it('P2：一行同时带出文字与底色 class（文字仍由 platformPushStatusLabel 决定）', () => {
    const row = toDisplayRow({ ...ROW, state: 'sent', remote_status: 12 })
    expect(row['t_pltSta']).toBe('正在代练')
    expect(row['t_pltStaClass']).toBe('layui-badge layui-bg-green')
    // closed 的文字与底色一起变「已关闭 / 灰」
    const closed = toDisplayRow({ ...ROW, state: 'closed', remote_status: 12 })
    expect(closed['t_pltSta']).toBe('已关闭')
    expect(closed['t_pltStaClass']).toBe('layui-badge layui-bg-gray')
  })

  /**
   * 「交易状态」格必须用**综合状态**（远端撤销流程一起判），⛔ 不许只看 `remote_status`。
   * 判据：① 后端给了 `display_status_label` 就**优先**用它；② 没有这两个键（旧回包）时
   * 本地推导也必须一起看 `remote_cancel_status`。
   */
  it('h1：后端 display_status_label 优先（12/15/16 三档各一条）', () => {
    const cases: Array<{
      remote: number
      cancel: number
      token: string
      label: string
    }> = [
      { remote: 16, cancel: 12, token: 'revoked', label: '已撤销' },
      { remote: 13, cancel: 15, token: 'revoked', label: '已撤销' },
      { remote: 16, cancel: 16, token: 'force_revoked', label: '已强制撤销' },
    ]
    for (const item of cases) {
      const row = toDisplayRow({
        ...ROW,
        remote_status: item.remote,
        remote_cancel_status: item.cancel,
        display_status: item.token,
        display_status_label: item.label,
      })
      expect(row['t_pltSta'], `CancelStatus=${item.cancel}`).toBe(item.label)
    }
    // 后端 label 与本地推导不同时，**以后端为准**（后端拿到的是当次事实）。
    const backendWins = toDisplayRow({
      ...ROW,
      remote_status: 16,
      remote_cancel_status: 11,
      display_status: 'revoked',
      display_status_label: '已撤销（后端）',
    })
    expect(backendWins['t_pltSta']).toBe('已撤销（后端）')
  })

  it('h1：没有 display_status 键时退回本地映射，且把 remote_cancel_status 一起判', () => {
    const label = (remote_status: number | null, remote_cancel_status: number | null): string =>
      toDisplayRow({ ...ROW, remote_status, remote_cancel_status })['t_pltSta'] ?? ''
    // 12 协商已处理 / 15 客服已处理 ⇒ 已撤销（⛔ 不是「撤销中」）
    expect(label(16, 12)).toBe('已撤销')
    expect(label(13, 15)).toBe('已撤销')
    // 16 客服强制撤销 ⇒ 已强制撤销
    expect(label(16, 16)).toBe('已强制撤销')
    // 11 协商撤销中 / 14 客服介入中 ⇒ 都还没撤销完
    expect(label(16, 11)).toBe('撤销中')
    expect(label(16, 14)).toBe('客服介入中')
    // 13 撤销已取消 ⇒ 回到按 Status 显示
    expect(label(16, 13)).toBe('撤销中')
    expect(label(12, 13)).toBe('正在代练')
    // 没有撤销流程 ⇒ 按 Status（这里的未撤销 10 与 null 都是「没撤销」）
    expect(label(16, 10)).toBe('撤销中')
    expect(label(12, null)).toBe('正在代练')
  })

  it('h1：已撤销的底色 = 对标 70 那一档灰（⛔ 不是「撤销中」的橙）', () => {
    const cls = (remote_status: number | null, remote_cancel_status: number | null): string =>
      toDisplayRow({ ...ROW, remote_status, remote_cancel_status })['t_pltStaClass'] ?? ''
    expect(cls(16, 12)).toBe('layui-badge layui-bg-gray')
    expect(cls(16, 16)).toBe('layui-badge layui-bg-gray')
    // 在途的那两档仍是「撤销中」的橙 / 客服介入中的橙
    expect(cls(16, 11)).toBe('layui-badge layui-bg-orange')
    expect(cls(16, 14)).toBe('layui-badge layui-bg-orange')
  })

  it('P2：「交易状态」格的 templet 输出填充标签（class / title 逐字照对标；`—` 不上标签）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    const templet = cols.find((col) => col['field'] === 't_pltSta')![
      'templet'
    ] as (row: CoreOrderDisplayRow) => string
    expect(templet({ t_pltSta: '正在代练', t_pltStaClass: 'layui-badge layui-bg-green' })).toBe(
      '<span class="layui-badge layui-bg-green" title="该笔订单在平台的交易状态">正在代练</span>',
    )
    // 没有状态可显示时只画「—」：一个灰标签会让「没有数据」看起来像一种状态。
    expect(templet({ t_pltSta: '—', t_pltStaClass: '' })).toBe('—')
    // 状态文字来自上游，进正文前必须转义（templet 出口）。
    expect(
      templet({ t_pltSta: '<b>x</b>', t_pltStaClass: 'layui-badge' }),
    ).toBe('<span class="layui-badge" title="该笔订单在平台的交易状态">&lt;b&gt;x&lt;/b&gt;</span>')
  })

  it('P2 补充：系统状态每个值一档文字颜色（色值逐条照对标 orderStaRow）', () => {
    const color = (order_status: string): string =>
      toDisplayRow({ ...ROW, order_status })['t_staColor'] ?? ''
    expect(color('WAITING_GRAB')).toBe('#D15FE8')
    expect(color('IN_PROGRESS')).toBe('#219227')
    expect(color('PENDING_REVIEW')).toBe('#ff6a00')
    expect(color('COMPLETED')).toBe('#0094ff')
    expect(color('ISSUE')).toBe('#ff0000')
    expect(color('PAUSED')).toBe('#FF5722')
    expect(color('REFUNDED')).toBe('#efaaaa')
    // 未分配：对标那张 switch 里没有这一档（它的 8 档是另一套业务状态）⇒ 用同一 switch 的
    // `default` 值 `#b0aeae`，⛔ 不编一档新色。
    expect(color('UNASSIGNED')).toBe('#b0aeae')
    // 未知枚举（前后端漂移）⇒ 同一个 default，⛔ 不崩也不猜。
    expect(color('SOMETHING_NEW')).toBe('#b0aeae')
    // 空值（后端没给）⇒「—」且**没有**颜色档。
    expect(color('')).toBe('')
  })

  it('P2 补充：系统状态格输出着色文字（`—` 不着色）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    const templet = cols.find((col) => col['field'] === 't_sta')![
      'templet'
    ] as (row: CoreOrderDisplayRow) => string
    expect(templet({ t_sta: '代练中', t_staColor: '#219227' })).toBe(
      '<span style="color:#219227">代练中</span>',
    )
    // 未知状态也照 default 色着色（它确实是一个状态，只是我们不认识这个名字）
    expect(templet({ t_sta: '未知状态', t_staColor: '#b0aeae' })).toBe(
      '<span style="color:#b0aeae">未知状态</span>',
    )
    // 「没有数据」不是一种状态 ⇒ 只画「—」，不带颜色
    expect(templet({ t_sta: '—', t_staColor: '#b0aeae' })).toBe('—')
    // 后端原文进正文前必须转义
    expect(templet({ t_sta: '<b>x</b>', t_staColor: '#219227' })).toBe(
      '<span style="color:#219227">&lt;b&gt;x&lt;/b&gt;</span>',
    )
  })

  it('`formatDateTime` 只格式化真值；空 / null ⇒「—」', () => {
    expect(formatDateTime('2026-10-27T09:30:00+08:00')).toBe('2026-10-27 09:30')
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
  })

  it('数据里的接口文本（游戏名 / 区服名）过 `escapeHtml`（templet 的出口）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    const templet = cols.find((col) => col['field'] === 't_gameServer')![
      'templet'
    ] as (row: CoreOrderDisplayRow) => string
    expect(templet({ t_gameServer: '<img src=x onerror=alert(1)>' })).toBe(
      '&lt;img src=x onerror=alert(1)&gt;',
    )
  })

  it('「平台交易单号」格的 tooltip：最近同步 + 推送状态 + 有错才拼错误原文', () => {
    const base: CoreOrderDisplayRow = {
      t_tid: '19000000000000000201',
      t_lastSynced: '2026-10-27 10:20',
      t_state: '已发出',
      t_lastError: '',
    }
    expect(platformOrderHint(base)).toBe(
      '最近同步：2026-10-27 10:20 · 状态：已发出',
    )
    expect(
      platformOrderHint({
        ...base,
        t_lastError: '上游超时：请求超时',
      }),
    ).toContain('错误：上游超时：请求超时')
  })

  it('平台交易单号格：正文仍是单号、附加信息只进 `title`（转义后）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    const templet = cols[1]!['templet'] as (row: CoreOrderDisplayRow) => string
    const html = templet({
      t_tid: '19000000000000000201',
      t_lastSynced: '2026-10-27 10:20',
      t_state: '已发出',
      t_lastError: 'x"><b>',
    })
    // 正文 = 单号（转义）；tooltip 走 `title` 且整串转义（含错误原文里的引号 / 尖括号）。
    expect(html).toContain('>19000000000000000201</span>')
    expect(html).toContain(
      'title="最近同步：2026-10-27 10:20 · 状态：已发出 · 错误：x&quot;&gt;&lt;b&gt;"',
    )
    expect(html).not.toContain('><b>')
  })

  it('首屏请求参数：`page=1&page_size=30`、平台固定 10、不带 status / keyword', async () => {
    await mountPage()
    expect(listRequests()).toHaveLength(1)
    const config = requestConfig(0)
    expect(config.method).toBe('get')
    expect(config.url).toBe('/api/business/platform-pushes/')
    expect(config.params).toEqual({ platform: 10, page: 1, page_size: 30, group_by: 'order' })
    expect('status' in config.params).toBe(false)
    expect('keyword' in config.params).toBe(false)
  })

  it('`table.render`：无 url、page:false、totalRow、toolbar、defaultToolbar；data 是接口行', async () => {
    await mountPage()
    const options = tableOptions()
    expect(options['elem']).toBe('#datatable')
    expect(options['url']).toBeUndefined()
    expect(options['page']).toBe(false)
    expect(options['totalRow']).toBe(true)
    expect(options['toolbar']).toBe('#toolbarDemo')
    expect(options['defaultToolbar']).toEqual([
      'filter',
      { title: RESET_TABLE_LABEL, layEvent: 'RESET_TABLE', icon: 'layui-icon-fonts-clear' },
    ])
    expect(renderedRows()).toHaveLength(1)
    // 正常 0 行的空态文案是「无数据」（与「没有数据源」「加载失败」区分开）。
    expect(options['text']).toEqual({ none: '无数据' })
  })

  it('24 列：勾选列 / hide / fixed / 合计行逐条对上（⭐ N7 改列后的形状）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    expect(cols).toHaveLength(24)
    expect(cols[0]).toMatchObject({ type: 'checkbox', width: 35 })
    expect(cols[0]!['title']).toBeUndefined()
    expect(cols.map((c) => c['title'])).toEqual([
      undefined,
      '平台交易单号',
      '系统单号',
      '游戏区服',
      '发单平台',
      '发单价',
      '接单价',
      '利润',
      '剩余时间',
      '交易状态',
      '接手人',
      '完成单数',
      '投诉/取消',
      '接单时间',
      '系统状态',
      '验收倒计',
      '订单来源',
      '发单时间',
      '接手时间',
      '完单时间',
      '录单时间',
      '撤销时间',
      '发单人',
      '操作',
    ])
    // ⛔ 不新增「最近同步」列；删掉的 5 列宽度也不许留空位。
    expect(cols.some((c) => c['title'] === '最近同步')).toBe(false)
    expect(cols.map((c) => c['width'])).toEqual([
      35, 180, 140, 120, 70, 70, 70, 80, 90, 70, 90, 80, 90, 130, 70, 110, 80, 80, 80, 80, 80, 80,
      80, 140,
    ])
    expect(cols.every((c) => typeof c['width'] === 'number')).toBe(true)
    // 录单时间仍默认隐藏；订单来源仍默认可见。
    expect(cols[20]!['hide']).toBe(true)
    expect(cols[16]!['hide']).toBe(false)
    expect(cols[23]!['fixed']).toBe('right')
    // 合计行只在「利润」那一列。
    expect(cols[7]).toMatchObject({ totalRow: true, totalRowText: '本页合计' })
    expect(cols.filter((c) => c['totalRow'] === true)).toHaveLength(1)
    // 删掉的 5 列不许有排序图标残留（它们本来也没有 sort；这里正向钉住新的排序集合）。
    expect(cols.filter((c) => c['sort'] === true).map((c) => c['title'])).toEqual([
      '发单价',
      '接单价',
      '利润',
      '剩余时间',
      '接单时间',
      '验收倒计',
      '发单时间',
      '接手时间',
      '完单时间',
      '录单时间',
      '撤销时间',
    ])
  })

  it('列 field 复用已修正（验收倒计 ≠ t_create）；排序列数跟着 N7 改列走', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Record<string, unknown>[][])[0]!
    const fieldOf = (title: string): unknown =>
      cols.find((col) => col['title'] === title)?.['field']
    // 对标把「验收倒计」与「录单时间」都用 `field:'t_create'`（同 field 两列会互相污染）⇒ 我们改开了。
    expect(fieldOf('验收倒计')).toBe('t_exAccpetTime')
    expect(fieldOf('录单时间')).toBe('t_create')
    expect(fieldOf('验收倒计')).not.toBe(fieldOf('录单时间'))
    // ⭐ 接单时间也带排序 ⇒ 排序列共 11（⛔ 删掉的 5 列本来都没有 sort）。
    expect(cols.filter((c) => c['sort'] === true)).toHaveLength(11)
    // 仍然**没有** `table.on('sort')` 事件（点表头不重新取数）。
    expect(LAYUI.tableOn).not.toHaveBeenCalled()
  })
})

describe('h6x 行内按钮：4 颗按交易状态出现（⛔ 不许全都放行）', () => {
  const TARGET = { orderNo: ROW.order_no, pushKey: 'p:7' }

  function keysOf(html: string): string[] {
    const holder = document.createElement('div')
    holder.innerHTML = html
    return Array.from(holder.querySelectorAll<HTMLElement>('[data-platform-action]')).map(
      (el) => el.dataset.platformAction ?? '',
    )
  }

  it('恰好 4 颗、顺序与可见文字照业务方定案（含「协商撤销」，不含重发 / 短信）', () => {
    expect(ROW_ACTION_BUTTONS.map((item) => item.key)).toEqual([
      ROW_ACTION_EDIT,
      ROW_ACTION_WITHDRAW,
      ROW_ACTION_NEGOTIATE,
      ROW_ACTION_AMOUNT,
    ])
    expect([...ROW_ACTION_LABELS]).toEqual(['编辑', '撤回', '协商撤销', '加款'])
    for (const banned of ['重发', '短信', '改价', '截图']) {
      expect(ROW_ACTION_LABELS).not.toContain(banned)
    }
  })

  it('可见性表逐档对拍（编辑只在未接手；加款只在 12/13）', () => {
    expect(rowActionKeysFor('unaccepted')).toEqual([ROW_ACTION_EDIT, ROW_ACTION_WITHDRAW])
    expect(rowActionKeysFor('in_progress')).toEqual([ROW_ACTION_NEGOTIATE, ROW_ACTION_AMOUNT])
    expect(rowActionKeysFor('waiting_review')).toEqual([
      ROW_ACTION_NEGOTIATE,
      ROW_ACTION_AMOUNT,
    ])
    expect(rowActionKeysFor('abnormal')).toEqual([ROW_ACTION_NEGOTIATE])
    expect(rowActionKeysFor('locked')).toEqual([ROW_ACTION_NEGOTIATE])
    // 终态 / 中间态：一颗都不出现（后端那几档都会拒）。
    for (const token of ['cancelling', 'settled', 'closed', 'revoked', 'force_revoked', 'arbitrating', 'temp_order', '']) {
      expect(rowActionKeysFor(token)).toEqual([])
    }
  })

  it('未接手(11) ⇒ 编辑 + 撤回；⛔ 不出现协商撤销 / 加款，也不带「尚未开放」的死按钮', () => {
    const html = actionRowTemplate(TARGET, 'unaccepted')
    expect(keysOf(html)).toEqual([ROW_ACTION_EDIT, ROW_ACTION_WITHDRAW])
    expect(html).not.toContain('协商撤销')
    expect(html).not.toContain('加款')
    // 只有两颗 ⇒ 没有换行（对标的两行排布只在中段出现）。
    expect(html).not.toContain('<br/>')
    // 每颗都带定位信息（否则点了不知道操作哪一单哪一条）。
    expect(html).toContain(`data-order="${ROW.order_no}"`)
    expect(html).toContain('data-push="p:7"')
  })

  it('正在代练(12) ⇒ 协商撤销 + 加款；⛔ 不出现编辑 / 撤回', () => {
    const html = actionRowTemplate(TARGET, 'in_progress')
    expect(keysOf(html)).toEqual([ROW_ACTION_NEGOTIATE, ROW_ACTION_AMOUNT])
    expect(html).not.toContain('编辑')
    expect(html).not.toContain('撤回</a>')
    // 两组按钮互斥（11 vs 12/13）⇒ 实际上一行就够，不插换行把两颗挤成两行。
    expect(html).not.toContain('<br/>')
  })

  it('订单异常(14) / 帐号锁定(15) ⇒ 只有协商撤销（加款后端不允许）', () => {
    expect(keysOf(actionRowTemplate(TARGET, 'abnormal'))).toEqual([ROW_ACTION_NEGOTIATE])
    expect(keysOf(actionRowTemplate(TARGET, 'locked'))).toEqual([ROW_ACTION_NEGOTIATE])
  })

  it('已下架(18) ⇒ 只有**撤回**（业务方：能撤就撤，不能撤给后端中文原因）', () => {
    // 18 是码表里比上游多的一档：`remote_status=18` ⇒ token `off_shelf`。
    const offShelf: PlatformPushSubRow = {
      ...PUSH_A,
      state: 'sent',
      remote_status: 18,
      remote_cancel_status: 10,
      id: 801,
      created_at: '2026-10-28T10:00:00+08:00',
      is_active: true,
    }
    // token 自己先钉住（码表与页面判据同源）。
    expect(platformDisplayStatus(offShelf)?.token).toBe('off_shelf')
    expect(rowActionKeysFor('off_shelf')).toEqual([ROW_ACTION_WITHDRAW])
    expect(keysOf(actionRowTemplate(TARGET, 'off_shelf'))).toEqual([ROW_ACTION_WITHDRAW])
    // ⛔ 编辑 / 协商撤销 / 加款都不该出现（后端那三条都不放行 18）。
    expect(rowActionKeysFor('off_shelf')).not.toContain(ROW_ACTION_EDIT)
    expect(rowActionKeysFor('off_shelf')).not.toContain(ROW_ACTION_NEGOTIATE)
    expect(rowActionKeysFor('off_shelf')).not.toContain(ROW_ACTION_AMOUNT)
  })

  it('撤销中 / 已结算 / 已关闭 ⇒ 操作列画「—」（⛔ 不摆点了必被拒的按钮）', () => {
    for (const token of ['cancelling', 'settled', 'closed']) {
      expect(actionRowTemplate(TARGET, token)).toBe('—')
    }
  })

  it('挂载一屏 11 / 12 两种单 ⇒ 各自只出现自己的那几颗', async () => {
    const accepted: PlatformPushGroupItem = {
      ...GROUP,
      order_no: '20261027000000000009',
      pushes: [
        { ...PUSH_A, id: 901, created_at: '2026-10-27T12:00:00+08:00', is_active: true },
      ],
    }
    mockApi(groupedPageOf([GROUP_ONE, accepted], 2))
    await mountPage()
    const rows = renderedRows()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const changeCol = cols.find((item) => item['field'] === 't_change')!
    const templet = changeCol['templet'] as (r: CoreOrderDisplayRow) => string
    expect(keysOf(templet(rows[0]!))).toEqual([ROW_ACTION_EDIT, ROW_ACTION_WITHDRAW])
    expect(keysOf(templet(rows[1]!))).toEqual([ROW_ACTION_NEGOTIATE, ROW_ACTION_AMOUNT])
  })

  it('没有定位信息的按钮：明确报错、零写请求（⛔ 不猜一个单号去写）', async () => {
    const root = await mountPage()
    const holder = document.createElement('div')
    holder.innerHTML = `<a href="javascript:void(0)" data-platform-action="${ROW_ACTION_EDIT}">编辑</a>`
    root.appendChild(holder)
    requestMock.mockClear()
    holder
      .querySelector<HTMLElement>('[data-platform-action]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_TARGET_MISSING_MESSAGE, { time: 6000 })
    expect(requestMock).not.toHaveBeenCalled()
  })
})

describe('手写分页：count 来自服务端总数，jump 重取', () => {
  it('laypage.render 收到 limit 30 / limits 10..100 / 完整 layout / 真实 count', async () => {
    mockApi(pageOf([ROW], 65))
    const root = await mountPage()
    const options = pagerOptions()
    expect(options['elem']).toBe('page')
    expect(options['limit']).toBe(30)
    expect(options['limits']).toEqual([10, 20, 30, 40, 50, 100])
    expect(options['layout']).toEqual(['count', 'prev', 'page', 'next', 'limit'])
    expect(options['curr']).toBe(1)
    expect(options['count']).toBe(65)
    // 65 > 30 ⇒ 分页条可见。
    const pageEl = root.querySelector<HTMLElement>(PAGE_ELEM_SELECTOR)!
    expect(pageEl.style.display).toBe('')
  })

  it('count <= limit ⇒ #page 隐藏（与对标 0 行 / 一页装下时一致）', async () => {
    const root = await mountPage()
    expect(pagerOptions()['count']).toBe(1)
    const pageEl = root.querySelector<HTMLElement>(PAGE_ELEM_SELECTOR)!
    expect(pageEl.style.display).toBe('none')
  })

  it('jump 到第 2 页 / 每页 10 ⇒ 真的用新参数重取', async () => {
    mockApi(pageOf([ROW], 65))
    await mountPage()
    const jump = pagerOptions()['jump'] as (obj: {
      curr?: number
      limit?: number
    }) => void
    jump({ curr: 2, limit: 10 })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    expect(requestConfig(1).params).toEqual({
      platform: 10,
      page: 2,
      page_size: 10,
      group_by: 'order',
    })
  })
})

describe('卡死防护：分页自回调与并发 load、取数超时', () => {
  it('jump 的状态（curr/limit）没变 ⇒ 直接 return，不重取（防 laypage 在 count=0 时自回调递归）', async () => {
    mockApi(pageOf([], 0))
    await mountPage()
    const jump = pagerOptions()['jump'] as (obj: {
      curr?: number
      limit?: number
    }) => void
    const calls = listRequests().length
    // laypage 在 count=0 / 重新 render 时可能用**原值**回调 jump：这不是用户翻页。
    jump({ curr: 1, limit: 30 })
    jump({ curr: 1 })
    jump({ limit: 30 })
    jump({})
    expect(listRequests()).toHaveLength(calls)
    // 真的改了才取数（否则分页就死了）。
    jump({ curr: 2, limit: 30 })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(calls + 1))
    expect(requestConfig(calls).params).toEqual({ platform: 10, page: 2, page_size: 30, group_by: 'order' })
  })

  it('同一时刻只允许一个 load，但在途时切页签会**排队补发**：最终渲染的是新页签的数据', async () => {
    const ROW_QUEUED: PlatformPushListRow = {
      ...ROW,
      order_no: '20261027000000000002',
      serial_no: '19000000000000000202',
    }
    const root = await mountPage() // 第 1 发：全部
    let resolveSecond: (page: Page<PlatformPushListRow>) => void = () => undefined
    requestMock.mockImplementationOnce(
      () =>
        new Promise<Page<PlatformPushListRow>>((resolve) => {
          resolveSecond = resolve
        }),
    )
    // 第 2 发：点「待接单」（lay-id=10 ⇒ status=11），先把它挂住。
    tabItems(root)[1]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    // 在途时切到「代练中」（lay-id=20 ⇒ status=12）：不并发发请求，但必须排队。
    tabItems(root)[2]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(listRequests()).toHaveLength(2)
    // 队列里的那一发用新数据回包（如果被静默丢弃，页面会停在「待接单」的旧数据上）。
    mockApi(pageOf([ROW_QUEUED], 65))
    resolveSecond(pageOf([ROW], 65))
    await vi.waitFor(() => expect(listRequests()).toHaveLength(3))
    // 排队的那一发读的是**最新**页签（status=12），不是被挂住时的 status=11。
    expect(requestConfig(2).params).toEqual({ platform: 10, page: 1, page_size: 30, status: 12, group_by: 'order' })
    // 页面最终展示的新页签数据（不是旧页签那一屏）。
    await vi.waitFor(() => expect(renderedRows()[0]!['t_orderid']).toBe('20261027000000000002'))
    expect(renderedRows()[0]!['t_tid']).toBe('19000000000000000202')
    // 页签数字只来自 counts（点页签不重取）⇒ 仍是挂载时那一发。
    expect(countsRequests()).toHaveLength(1)
  })

  it('取数 10 秒不回来 ⇒ 中止请求并显示「加载失败：请求超时」（⛔ 不静默空白）', async () => {
    vi.useFakeTimers()
    try {
      let aborted = false
      requestMock.mockImplementation(
        (config) =>
          new Promise<Page<PlatformPushListRow>>((_resolve, reject) => {
            config.signal?.addEventListener?.('abort', () => {
              aborted = true
              reject(new Error('aborted'))
            })
          }),
      )
      const mounting = mountPage()
      await vi.advanceTimersByTimeAsync(10_000)
      await mounting
      expect(aborted).toBe(true)
      expect(tableOptions()['text']).toEqual({ none: '加载失败：请求超时' })
      expect(LAYUI.msg).toHaveBeenCalledWith('加载失败：请求超时', { time: 1500 })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('加载失败要有可见提示（⛔ 不许静默空表）', () => {
  it('请求失败 ⇒ 空表 +「加载失败：<原因>」+ 顶部提示，计数为「—」', async () => {
    // 列表与 counts 都失败（两发独立；都失败时页签数字保持「—」，⛔ 不写 0）。
    requestMock.mockImplementation(() => Promise.reject(new Error('网络断了')))
    const root = await mountPage()
    expect(tableOptions()['text']).toEqual({ none: '加载失败：网络断了' })
    expect(renderedRows()).toEqual([])
    expect(LAYUI.msg).toHaveBeenCalledWith('加载失败：网络断了', { time: 1500 })
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `${TAB_COUNTS_FAILED_PREFIX}网络断了`,
      { time: 6000 },
    )
    expect((tabItems(root)[0]!.textContent ?? '').trim()).toBe('全部(—)')
  })
})

describe('12 个筛选控件与 15 颗工具栏按钮（照证据件渲染）', () => {
  it('12 个控件的 id 一个不缺（另有 2 个对标自带的隐藏日期域，不算筛选控件）', async () => {
    const root = await mountPage()
    const missing = FILTER_IDS.filter((id) => root.querySelector(`#${id}`) === null)
    expect(missing).toEqual([])
    expect(FILTER_IDS).toHaveLength(12)
    expect(root.querySelector('#sDate')).not.toBeNull()
    expect(root.querySelector('#eDate')).not.toBeNull()
  })

  it('搜索按钮是一颗 `button[lay-filter="seachBtnOk"]`（宽 80px 原文）', async () => {
    const root = await mountPage()
    const button = root.querySelector<HTMLElement>('button.sechbtn')
    expect(button).not.toBeNull()
    expect(button!.getAttribute('lay-filter')).toBe('seachBtnOk')
    expect((button!.textContent ?? '').trim()).toBe('搜索')
    expect(button!.getAttribute('style')).toContain('width: 80px')
  })

  it('工具栏模板里恰好 15 颗按钮，文案与顺序逐条对上', async () => {
    const root = await mountPage()
    const template = root.querySelector<HTMLElement>('#toolbarDemo')
    expect(template).not.toBeNull()
    const holder = document.createElement('div')
    holder.innerHTML = template!.textContent ?? ''
    const buttons = Array.from(holder.querySelectorAll<HTMLButtonElement>('button'))
    expect(buttons).toHaveLength(15)
    expect(buttons.map((b) => (b.textContent ?? '').trim())).toEqual(TOOLBAR_LABELS)
    expect(buttons[0]!.className).toContain('layui-btn-danger')
    expect(buttons[14]!.className).toContain('layui-btn-normal')
    expect(buttons.slice(1, 14).every((b) => !b.className.includes('layui-btn-danger'))).toBe(true)
  })

  it('「平台消单数据统计」被隐藏（⛔ 置灰但要看得见）；其余 14 颗仍显示', async () => {
    const root = await mountPage()
    // 测试环境里 `table.render` 是替身 ⇒ 工具栏按钮不在 DOM 里；先按证据件解析出来，
    // 再单独验证隐藏函数的**真实效果**（它只按 `lay-filter` 改 `display`）。
    const holder = mountToolbar(root)
    for (const filter of HIDDEN_TOOLBAR_FILTERS) {
      const button = toolbarButton(holder, filter)
      expect(button.style.display).not.toBe('none')
      hideUnsupportedToolbarButtons(holder)
      expect(button.style.display).toBe('none')
      expect((button.textContent ?? '').trim()).toBe('平台消单数据统计')
    }
    expect(HIDDEN_TOOLBAR_FILTERS).toEqual(['viewPlatformReportBtn'])
    // 隐藏的是**这一颗**，其余按钮的 display 不受影响。
    const others = Array.from(holder.querySelectorAll<HTMLButtonElement>('button')).filter(
      (button) => button.getAttribute('lay-filter') !== 'viewPlatformReportBtn',
    )
    expect(others.every((button) => button.style.display !== 'none')).toBe(true)
  })

  /**
   * 判据打在**渲染之后**：按 layui 的真实落点（`.layui-table-tool`）把工具栏建出来，
   * 再跑 `table.render` 的 `done` 回调，按钮必须已经被隐藏——渲染前查不到模板 ≠ 已隐藏。
   */
  it('工具栏「平台消单数据统计」在表格**渲染之后**被隐藏（渲染前查不到 ≠ 已隐藏）', async () => {
    const root = await mountPage()
    const options = tableOptions()
    // 模拟 layui 渲染工具栏：它把 `#toolbarDemo` 模板 laytpl 进 `.layui-table-tool`。
    const template = root.querySelector<HTMLElement>('#toolbarDemo')
    expect(template).not.toBeNull()
    const tool = document.createElement('div')
    tool.className = 'layui-table-tool'
    tool.innerHTML = template!.textContent ?? ''
    root.appendChild(tool)
    const target = tool.querySelector<HTMLElement>(
      'button[lay-filter="viewPlatformReportBtn"]',
    )!
    // 渲染那一刻按钮本来是可见的。
    expect(target.style.display).not.toBe('none')

    // ⚠️ 判据 = **done 回调**（生产代码真正隐藏的时机），不是「手工再调一次 hide」。
    const done = options['done']
    expect(typeof done).toBe('function')
    ;(done as () => void)()

    expect(target.style.display).toBe('none')
    expect((target.textContent ?? '').trim()).toBe('平台消单数据统计')
    // 其余 14 颗一颗都没被牵连
    const others = Array.from(tool.querySelectorAll<HTMLButtonElement>('button')).filter(
      (button) => button.getAttribute('lay-filter') !== 'viewPlatformReportBtn',
    )
    expect(others).toHaveLength(14)
    expect(others.every((button) => button.style.display !== 'none')).toBe(true)
  })

  it('「重置表格排版」（defaultToolbar 的自定义项）：点了提示尚未开放', async () => {
    const root = await mountPage()
    const reset = document.createElement('i')
    reset.setAttribute('lay-event', 'RESET_TABLE')
    root.appendChild(reset)
    reset.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenCalledWith(`「${RESET_TABLE_LABEL}」尚未开放`, { time: 1500 })
  })

  it('页签点击是结构 / 取数行为，不是「尚未开放」提示', async () => {
    const root = await mountPage()
    LAYUI.msg.mockClear()
    tabItems(root)[13]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })
})

describe('日期区间（纯前端控件；>90 天清空并提示）', () => {
  it('laydate.render 参数照对标（range:true / min:-180），合法区间回填两个隐藏域', async () => {
    const root = await mountPage()
    expect(LAYUI.laydateRender).toHaveBeenCalledTimes(1)
    const options = LAYUI.laydateRender.mock.calls[0]![0] as Record<string, unknown>
    expect(options['elem']).toBe('#datetime')
    expect(options['range']).toBe(true)
    expect(options['min']).toBe(-180)

    const done = options['done'] as (value: string) => void
    done('2026-10-01 - 2026-10-20')
    expect(root.querySelector<HTMLInputElement>('#sDate')!.value).toBe('2026-10-01')
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe('2026-10-20')
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('跨度 > 90 天：清空输入框 + 逐字提示「日期最多选择90天」', async () => {
    const root = await mountPage()
    const options = LAYUI.laydateRender.mock.calls[0]![0] as Record<string, unknown>
    const done = options['done'] as (value: string) => void
    const visible = root.querySelector<HTMLInputElement>('#datetime')!
    visible.value = '2026-01-01 - 2026-06-01'
    done('2026-01-01 - 2026-06-01')
    expect(visible.value).toBe('')
    expect(root.querySelector<HTMLInputElement>('#sDate')!.value).toBe('')
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe('')
    expect(LAYUI.msg).toHaveBeenCalledWith(DATE_SPAN_MSG, { time: 1500 })
    expect(DATE_SPAN_MSG).toBe('日期最多选择90天')
  })
})

describe('卸载：只清自己注入的标记与监听器', () => {
  it('cleanup 后再点旧按钮不再弹提示（委托已摘）', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountCoreOrderCenterPage(root, { onNavigate: vi.fn() })
    // ⚠️ 不能用 `refresh`：它已接真（点了只重取数、不弹提示）。
    // 用一颗仍在「尚未开放」表里的按钮验证委托是否被摘。
    const button = document.createElement('button')
    button.setAttribute('lay-filter', 'sendSmsBtn')
    button.textContent = '发送短信'
    root.appendChild(button)
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenCalledTimes(1)
    cleanup()
    expect(root.innerHTML).toBe('')
    // 标记被清了、但节点引用还在 ⇒ 重新挂回去再点：监听器已摘，**不再弹**。
    root.appendChild(button)
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenCalledTimes(1)
  })
})

/* ───────────────── 工具栏 11 颗按钮接真 ───────────────── */

describe('C1 工具栏批量动作：勾选闸 / 确认框 / 请求载荷 / 结果提示', () => {
  it('没勾选 ⇒ 逐颗只提示「请选择要操作的记录！」，零弹层、零动作请求', async () => {
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    const filters = Object.keys(TOOLBAR_ACTION_FILTERS)
    expect(filters).toHaveLength(11)
    for (const filter of filters) clickToolbar(holder, filter)
    expect(LAYUI.msg).toHaveBeenCalledTimes(11)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(SELECT_REQUIRED_MSG, { time: 1500 })
    expect(SELECT_REQUIRED_MSG).toBe('请选择要操作的记录！')
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(actionRequests()).toHaveLength(0)
  })

  it('11 颗都先弹确认框 / 输入框（写清后果），且未点确定前零请求', async () => {
    const root = await mountPage()
    const holder = mountToolbar(root)
    selectRows(ROW.order_no)
    requestMock.mockClear()

    clickToolbar(holder, 'cancelOrderBtn')
    expect(lastConfirm().text).toBe(WITHDRAW_CONFIRM_TEXT)
    expect(WITHDRAW_CONFIRM_TEXT).toContain('删除并即时退款')
    expect(WITHDRAW_CONFIRM_TEXT).toContain('协商撤销')
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)

    // 关掉这一层（走 options.cancel 分支）再点下一颗。
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'accpetOrderBtn')
    expect(lastConfirm().text).toBe(ACCEPT_CONFIRM_TEXT)
    expect(ACCEPT_CONFIRM_TEXT).toContain('扣款')
    expect(ACCEPT_CONFIRM_TEXT).toContain('不可撤销')
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'asyncOrderBtn')
    expect(lastConfirm().text).toBe(SYNC_CONFIRM_TEXT)
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'changeStaBtn')
    expect(lastConfirm().text).toContain(STATUS_SELECT_ID)
    expect((lastConfirm().text.match(/<option /g) ?? []).length).toBe(8)
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'setOrderJiajiBtn')
    expect(lastConfirm().text).toBe(URGENT_CONFIRM_TEXT)
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'cleanOrderTipsBtn')
    expect(lastConfirm().text).toBe(CLEAR_TAG_CONFIRM_TEXT)
    ;(lastConfirm().options.cancel as () => void)()

    clickToolbar(holder, 'arbitOrderBtn')
    expect(String(lastPrompt().options.title)).toContain('申请仲裁理由')
    ;(lastPrompt().options.cancel as () => void)()

    clickToolbar(holder, 'refundOrderBtn')
    expect(String(lastPrompt().options.title)).toContain('累计退款总额')
    ;(lastPrompt().options.cancel as () => void)()

    clickToolbar(holder, 'setOrderLabelBtn')
    expect(String(lastPrompt().options.title)).toContain('订单标签')
    ;(lastPrompt().options.cancel as () => void)()

    clickToolbar(holder, 'setOrderTipsBtn')
    expect(String(lastPrompt().options.title)).toContain('自定义标记')
    ;(lastPrompt().options.cancel as () => void)()

    clickToolbar(holder, 'changeOrderPriceBtn')
    expect(String(lastPrompt().options.title)).toBe('更改接单价，可为0，但不能为空')
    ;(lastPrompt().options.cancel as () => void)()

    expect(actionRequests()).toHaveLength(0)
  })

  it('批量撤回：确认后 POST 只带 action + 单号，成功提示 + 刷新列表', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('withdraw', [
        { order_no: ROW.order_no, ok: true, msg: '已删除并即时退款' },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'cancelOrderBtn')
    lastConfirm().yes(1)

    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'withdraw',
      order_nos: [ROW.order_no],
    })
    // 成功后**重新取数**（列表请求 1 次）。
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
    expect(requestConfig(0).url).toBe('/api/business/platform-pushes/')
    expect(LAYUI.msg).toHaveBeenCalledWith('批量撤回已完成：成功 1 笔', { time: 1500 })
  })

  it('批量验收：载荷只有 action + 单号（花钱动作的文案已在确认框里说清）', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('accept', [
        { order_no: ROW.order_no, ok: true, msg: '已确认完单（远端状态：已结算）' },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    clickToolbar(holder, 'accpetOrderBtn')
    lastConfirm().yes(1)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({ action: 'accept', order_nos: [ROW.order_no] })
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('批量验收已完成：成功 1 笔', { time: 1500 }),
    )
  })

  it('同步交易状态 / 加急 / 删标记：各自带对键（加急带 urgent:true）', async () => {
    selectRows(ROW.order_no)
    const root = await mountPage()
    const holder = mountToolbar(root)

    for (const [filter, action, extra] of [
      ['asyncOrderBtn', 'sync', {}],
      ['setOrderJiajiBtn', 'urgent', { urgent: true }],
      ['cleanOrderTipsBtn', 'clear_tag', {}],
    ] as const) {
      mockActionApi(actionOutcome(action, [{ order_no: ROW.order_no, ok: true, msg: 'ok' }]))
      requestMock.mockClear()
      clickToolbar(holder, filter)
      lastConfirm().yes(1)
      await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
      expect(lastActionBody()).toEqual({
        action,
        order_nos: [ROW.order_no],
        ...extra,
      })
      await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
      // 等这一发的 `finally` 把在途闸放回去（否则下一颗会被「在途」静默吞掉）。
      await vi.waitFor(() =>
        expect(toolbarButton(holder, filter).hasAttribute('disabled')).toBe(false),
      )
    }
  })

  it('批量仲裁：输入理由 → 再确认（文案写明「不会提交到代练通」）→ 载荷带 comment', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('arbitrate', [
        { order_no: ROW.order_no, ok: true, msg: '已在本地订单日志记下「待仲裁」' },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'arbitOrderBtn')
    lastPrompt().yes('打手一直不上号', 1)
    expect(lastConfirm().text).toBe(ARBITRATION_CONFIRM_TEXT)
    expect(ARBITRATION_CONFIRM_TEXT).toContain('不会提交到代练通')
    lastConfirm().yes(2)

    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'arbitrate',
      order_nos: [ROW.order_no],
      comment: '打手一直不上号',
    })
  })

  it('记退款：累计总额 + 固定原因 + 幂等键（键非空且两次打开不同）', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('refund', [{ order_no: ROW.order_no, ok: true, msg: '退款额已记' }]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'refundOrderBtn')
    lastPrompt().yes('20.00', 1)
    expect(lastConfirm().text).toContain('20.00')
    expect(lastConfirm().text).toContain('累计退款额')
    lastConfirm().yes(2)

    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    const first = lastActionBody()
    expect(first).toMatchObject({
      action: 'refund',
      order_nos: [ROW.order_no],
      refund_total: '20.00',
      reason: REFUND_REASON,
    })
    expect(String(first['request_id'])).not.toBe('')

    // 再开一次弹窗 ⇒ 新的幂等键（同一批重试才复用键）。
    await vi.waitFor(() =>
      expect(toolbarButton(holder, 'refundOrderBtn').hasAttribute('disabled')).toBe(false),
    )
    mockActionApi(
      actionOutcome('refund', [{ order_no: ROW.order_no, ok: true, msg: '退款额已记' }]),
    )
    clickToolbar(holder, 'refundOrderBtn')
    lastPrompt().yes('20.00', 1)
    lastConfirm().yes(2)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(2))
    expect(lastActionBody()['request_id']).not.toBe(first['request_id'])
  })

  it('记退款金额格式不对 ⇒ 只提示、零请求', async () => {
    selectRows(ROW.order_no)
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    clickToolbar(holder, 'refundOrderBtn')
    lastPrompt().yes('abc', 1)
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的退款金额（累计总额，最多两位小数）', {
      time: 6000,
    })
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(actionRequests()).toHaveLength(0)
  })

  it('更改标签：输入 → 确认 → 载荷 label；留空 = 清除标签', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('set_label', [{ order_no: ROW.order_no, ok: true, msg: '标签已更新' }]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'setOrderLabelBtn')
    lastPrompt().yes('微', 1)
    expect(lastConfirm().text).toContain('标签改为「微」')
    lastConfirm().yes(2)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'set_label',
      order_nos: [ROW.order_no],
      label: '微',
    })

    await vi.waitFor(() =>
      expect(
        toolbarButton(holder, 'setOrderLabelBtn').hasAttribute('disabled'),
      ).toBe(false),
    )
    mockActionApi(
      actionOutcome('set_label', [{ order_no: ROW.order_no, ok: true, msg: '标签已更新' }]),
    )
    clickToolbar(holder, 'setOrderLabelBtn')
    lastPrompt().yes('', 1)
    expect(lastConfirm().text).toContain('清除')
    lastConfirm().yes(2)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(2))
    expect(lastActionBody()).toMatchObject({ action: 'set_label', label: '' })
  })

  it('改标记：空内容 ⇒ 页面拦下、零请求；正常内容 ⇒ 确认后带 tag', async () => {
    selectRows(ROW.order_no)
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'setOrderTipsBtn')
    lastPrompt().yes('   ', 1)
    expect(LAYUI.msg).toHaveBeenCalledWith('标记内容不能为空，且不能超过 128 个字符', {
      time: 6000,
    })
    expect(actionRequests()).toHaveLength(0)

    mockActionApi(
      actionOutcome('set_tag', [{ order_no: ROW.order_no, ok: true, msg: '标记已更新' }]),
    )
    clickToolbar(holder, 'setOrderTipsBtn')
    lastPrompt().yes('客户催了', 1)
    lastConfirm().yes(2)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'set_tag',
      order_nos: [ROW.order_no],
      tag: '客户催了',
    })
  })

  it('改接单价：输入 → 确认 → 载荷 taker_price（可为 0）', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('change_taker_price', [
        { order_no: ROW.order_no, ok: true, msg: '接单价已改为 0' },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'changeOrderPriceBtn')
    lastPrompt().yes('0', 1)
    expect(lastConfirm().text).toContain('接单价改为 0')
    lastConfirm().yes(2)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'change_taker_price',
      order_nos: [ROW.order_no],
      taker_price: '0',
    })
  })

  it('改系统状态：弹层里 8 个状态；选中后带 target；选不到 ⇒ 只提示', async () => {
    selectRows(ROW.order_no)
    mockActionApi(
      actionOutcome('change_status', [
        { order_no: ROW.order_no, ok: true, msg: '系统状态已改为 代练中' },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'changeStaBtn')
    const dialog = lastConfirm()
    expect(dialog.text).toBe(statusDialogHtml())
    document.body.insertAdjacentHTML('beforeend', dialog.text)
    const select = document.getElementById(STATUS_SELECT_ID) as HTMLSelectElement
    expect(select).not.toBeNull()
    expect(select.options).toHaveLength(8)
    select.value = 'IN_PROGRESS'
    dialog.yes(1)

    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(lastActionBody()).toEqual({
      action: 'change_status',
      order_nos: [ROW.order_no],
      target: 'IN_PROGRESS',
    })

    // 选不到（DOM 里没有那个 select）⇒ 提示「请选择要改成的系统状态」，零请求。
    select.remove()
    mockActionApi(actionOutcome('change_status', []))
    clickToolbar(holder, 'changeStaBtn')
    lastConfirm().yes(2)
    expect(LAYUI.msg).toHaveBeenCalledWith('请选择要改成的系统状态', { time: 6000 })
    expect(actionRequests()).toHaveLength(1)
  })

  it('部分成功：如实报「成功 N 笔 / 失败 M 笔」+ 第一笔原因，并照样刷新列表', async () => {
    selectRows(ROW.order_no, '20261027000000000002')
    mockActionApi(
      actionOutcome('withdraw', [
        { order_no: ROW.order_no, ok: true, msg: '已删除并即时退款' },
        {
          order_no: '20261027000000000002',
          ok: false,
          msg: '这张单还没有抛到代练通（没有有效的推送记录），不能做平台操作。',
        },
      ]),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()

    clickToolbar(holder, 'cancelOrderBtn')
    lastConfirm().yes(1)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '批量撤回：成功 1 笔，失败 1 笔（20261027000000000002：这张单还没有抛到代练通（没有有效的推送记录），不能做平台操作。）',
        { time: 6000 },
      ),
    )
    // 有成功的笔 ⇒ 必须刷新（页面不能停在旧值）。
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
  })

  it('请求整体失败（后端中文原文）⇒ 只报错、不刷新', async () => {
    selectRows(ROW.order_no)
    requestMock.mockImplementation((config) =>
      (config as { url?: string }).url === PLATFORM_PUSH_ACTIONS_URL
        ? Promise.reject(new Error('你没有订单退款权限'))
        : Promise.resolve(pageOf()),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    clickToolbar(holder, 'refundOrderBtn')
    lastPrompt().yes('20.00', 1)
    lastConfirm().yes(2)
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('你没有订单退款权限', { time: 6000 }),
    )
    // 只有那一发动作请求：⛔ 不刷新列表、也不重取页签数字。
    expect(actionRequests()).toHaveLength(1)
    expect(listRequests()).toHaveLength(0)
    expect(countsRequests()).toHaveLength(0)
  })

  it('提交中：按钮禁用 + 在途闸（同一批发一次）', async () => {
    selectRows(ROW.order_no)
    let resolveAction: (value: Record<string, unknown>) => void = () => undefined
    requestMock.mockImplementation((config) =>
      (config as { url?: string }).url === PLATFORM_PUSH_ACTIONS_URL
        ? new Promise<Record<string, unknown>>((resolve) => {
            resolveAction = resolve
          })
        : Promise.resolve(pageOf()),
    )
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    const button = toolbarButton(holder, 'cancelOrderBtn')

    clickToolbar(holder, 'cancelOrderBtn')
    lastConfirm().yes(1)
    await vi.waitFor(() => expect(actionRequests()).toHaveLength(1))
    expect(button.getAttribute('disabled')).toBe('disabled')
    expect(button.className).toContain('layui-btn-disabled')

    // 在途时再点（即使开了新弹层）⇒ 不会再发第二发。
    clickToolbar(holder, 'cancelOrderBtn')
    lastConfirm().yes(2)
    expect(actionRequests()).toHaveLength(1)

    resolveAction(actionOutcome('withdraw', [{ order_no: ROW.order_no, ok: true, msg: 'ok' }]))
    await vi.waitFor(() => expect(button.hasAttribute('disabled')).toBe(false))
  })

  it('「刷新」真的重新取数（不是「尚未开放」）', async () => {
    const root = await mountPage()
    const holder = mountToolbar(root)
    requestMock.mockClear()
    clickToolbar(holder, 'refresh')
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
    // 刷新也把页签数字重取一次（数据可能变了；仍是一次请求填 14 个页签）。
    await vi.waitFor(() => expect(countsRequests()).toHaveLength(1))
    expect(requestConfig(0).url).toBe('/api/business/platform-pushes/')
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('发送短信 / 导出订单 / 平台消单数据统计 仍只提示「尚未开放」、零请求', async () => {
    const root = await mountPage()
    const holder = mountToolbar(root)
    selectRows(ROW.order_no)
    requestMock.mockClear()
    for (const [filter, label] of [
      ['sendSmsBtn', '发送短信'],
      ['exportBtn', '导出订单'],
      ['viewPlatformReportBtn', '平台消单数据统计'],
    ] as const) {
      LAYUI.msg.mockClear()
      clickToolbar(holder, filter)
      expect(LAYUI.msg).toHaveBeenCalledWith(`「${label}」尚未开放`, { time: 1500 })
    }
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(actionRequests()).toHaveLength(0)
  })
})


/**
 * 需求 2 / 需求 3 ——「系统单号」= 来源单号（蓝）、点开订单详情、
 * 右键 4 项菜单（查看详情 / 复制系统订单编号 / 复制原始订单内容 / 查看订单截图）。
 */
describe('需求 2/3：系统单号 = 来源单号（蓝）+ 点开详情 + 右键 4 项菜单', () => {
  const SOURCE = 'TB-2026-0001'

  /** 造一页真 DOM：把「系统单号」格的 templet 产物塞进 root（页面是委托在 root 上的）。 */
  function mountSourceCell(
    root: HTMLElement,
    row: PlatformPushListRow,
  ): HTMLElement {
    const cell = document.createElement('td')
    cell.innerHTML = sourceOrderTemplet(toDisplayRow(row))
    root.appendChild(cell)
    return cell.querySelector<HTMLElement>(ORDER_LINE_SELECTOR)!
  }

  /** 装一个假的 `navigator.clipboard`（jsdom 里它整个不存在）。 */
  function stubClipboard(
    writeText: (text: string) => Promise<void>,
  ): ReturnType<typeof vi.fn> {
    const fn = vi.fn(writeText)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: fn },
    })
    return fn
  }

  /** 菜单根节点（挂在 `document.body`）。 */
  function menu(): HTMLElement | null {
    return document.body.querySelector<HTMLElement>('.context-menu-list')
  }

  function menuItems(): HTMLElement[] {
    return Array.from(
      document.body.querySelectorAll<HTMLElement>('.context-menu-item'),
    )
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'clipboard')
  })

  it('取值规则：`source_order_no` 非空 ⇒ 显示它；空 / 缺键 ⇒ 回退 `order_no`', () => {
    expect(sourceOrderNoOf({ ...ROW, source_order_no: SOURCE })).toBe(SOURCE)
    expect(sourceOrderNoOf({ ...ROW, source_order_no: '' })).toBe(ROW.order_no)
    expect(sourceOrderNoOf({ ...ROW, source_order_no: '   ' })).toBe(ROW.order_no)
    expect(sourceOrderNoOf({ ...ROW })).toBe(ROW.order_no)
  })

  it('`t_orderid` 恒是**内部单号**（批量动作读它）；显示值走 `t_orderidView`，两者不许混', () => {
    const row = toDisplayRow({ ...ROW, source_order_no: SOURCE })
    expect(row['t_orderid']).toBe(ROW.order_no)
    expect(row['t_orderidView']).toBe(SOURCE)
    const fallback = toDisplayRow({ ...ROW })
    expect(fallback['t_orderid']).toBe(ROW.order_no)
    expect(fallback['t_orderidView']).toBe(ROW.order_no)
  })

  it('格子形状照对标：`.orderLine` 容器 + 蓝色 `.orderdetails` 链接 + 三个 title/data 属性', () => {
    const html = sourceOrderTemplet(toDisplayRow({ ...ROW, source_order_no: SOURCE }))
    expect(SOURCE_ORDER_NO_COLOR).toBe('#0094ff')
    expect(html).toContain(ORDER_LINE_CLASS)
    expect(html).toContain(`data-id="${ROW.order_no}"`)
    expect(html).toContain(`data-orderidview="${SOURCE}"`)
    expect(html).toContain(`style="color:${SOURCE_ORDER_NO_COLOR}"`)
    expect(html).toContain(`data-viewid="${SOURCE}"`)
    expect(html).toContain('点击查看系统订单详情，也可以鼠标右键试试看')
    expect(html).toContain('鼠标右键看看~')
    expect(html).toContain(`>${SOURCE}<`)
    // 扁平行（后端还没上 group_by）**没有**折叠三角。
    expect(html).not.toContain('tt-order-tree')
    // 回退那一档显示的仍是内部号
    expect(sourceOrderTemplet(toDisplayRow({ ...ROW }))).toContain(`>${ROW.order_no}<`)
  })

  it('格子内容过 escapeHtml（单号是接口/上游文本）', () => {
    const html = sourceOrderTemplet(toDisplayRow({ ...ROW, source_order_no: '<b>&"' }))
    expect(html).toContain('&lt;b&gt;&amp;&quot;')
    expect(html).not.toContain('<b>')
  })

  it('点单号 ⇒ `layer.open` 装我们自己的订单详情岛页（尺寸/皮肤取订单列表页同一份）', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW, source_order_no: SOURCE })
    el.querySelector<HTMLElement>(ORDER_DETAIL_LINK_SELECTOR)!.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.open).toHaveBeenCalledWith(
      expect.objectContaining({
        title: `${SOURCE}订单详情`,
        type: 2,
        skin: 'layui-layer-lan',
        anim: 8,
        maxmin: false,
        area: ['85%', '92%'],
        content:
          `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview` +
          `&order_no=${encodeURIComponent(ROW.order_no)}`,
      }),
    )
  })

  it('单号形状不对 ⇒ 不开层、只给固定文案（⛔ 不把不可信输入拼进 title）', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW, order_no: 'BAD' })
    el.querySelector<HTMLElement>(ORDER_DETAIL_LINK_SELECTOR)!.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(ORDER_NO_INVALID_MESSAGE, { time: 1500 })
  })

  it('右键系统单号 ⇒ 菜单恰好 4 项（文案 / key / 顺序逐条对上对标），并注入菜单样式', async () => {
    const root = await mountPage()
    expect(root.querySelector(`#${CONTEXT_MENU_STYLE_ID}`)).not.toBeNull()
    const el = mountSourceCell(root, { ...ROW, source_order_no: SOURCE })
    const event = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
    })
    el.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    const items = menuItems()
    expect(items).toHaveLength(4)
    expect(items.map((item) => item.dataset.key)).toEqual([
      'viewDetails',
      'copyOrderID',
      'copyOrder',
      'viewPic',
    ])
    expect(items.map((item) => (item.textContent ?? '').trim())).toEqual(
      CONTEXT_MENU_ITEMS.map((item) => item.label),
    )
    // 三项之间各一条分隔线（`sep1` / `sep2` / `sep5`），共 3 条。
    expect(
      document.body.querySelectorAll('.context-menu-separator'),
    ).toHaveLength(3)
    expect(menu()).not.toBeNull()
  })

  it('右键别处（不是那一格）⇒ 不出菜单（浏览器原生菜单照出）', async () => {
    const root = await mountPage()
    const other = document.createElement('span')
    other.textContent = '别的东西'
    root.appendChild(other)
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true })
    other.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(menu()).toBeNull()
  })

  it('菜单「查看详情」= 与点单号同一个出口', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW, source_order_no: SOURCE })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'viewDetails')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.layer.open).toHaveBeenCalledWith(
      expect.objectContaining({ title: `${SOURCE}订单详情` }),
    )
    expect(menu()).toBeNull()
  })

  it('菜单「复制系统订单编号」⇒ 复制显示的那个号 + 逐字成功提示', async () => {
    const root = await mountPage()
    const writeText = stubClipboard(async () => {})
    const el = mountSourceCell(root, { ...ROW, source_order_no: SOURCE })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'copyOrderID')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith(SOURCE))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith(
        `${COPY_ORDER_ID_SUCCESS_PREFIX}${SOURCE}${COPY_ORDER_ID_SUCCESS_SUFFIX}`,
        { time: 1500 },
      ),
    )
  })

  it('两条复制路都失败 ⇒ 说清楚「复制失败了」（⛔ 不静默、不假装成功）', async () => {
    const root = await mountPage()
    stubClipboard(async () => {
      throw new Error('NotAllowedError')
    })
    const el = mountSourceCell(root, { ...ROW })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'copyOrderID')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith(COPY_ORDER_ID_FAILED_MESSAGE, {
        time: 1500,
      }),
    )
  })

  it('菜单「复制原始订单内容」⇒ 取详情 → 复制 content（空则 demand）；文案逐字取对标', async () => {
    const root = await mountPage()
    const writeText = stubClipboard(async () => {})
    requestMock.mockImplementation((config) => {
      const url = (config as { url?: string }).url ?? ''
      if (url === PLATFORM_PUSH_COUNTS_URL) return Promise.resolve(COUNTS)
      if (url === `/api/business/orders/${ROW.order_no}/`) {
        return Promise.resolve({ content: '客户要求：打到星耀', demand: '' })
      }
      return Promise.resolve(pageOf())
    })
    const el = mountSourceCell(root, { ...ROW })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'copyOrder')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('客户要求：打到星耀'),
    )
    expect(LAYUI.msg).toHaveBeenCalledWith(COPY_ORDER_CONTENT_SUCCESS_MESSAGE, {
      time: 1500,
    })
  })

  it('取详情失败 ⇒ 原样报后端中文（⛔ 不假装复制成功）', async () => {
    const root = await mountPage()
    const writeText = stubClipboard(async () => {})
    requestMock.mockImplementation((config) => {
      const url = (config as { url?: string }).url ?? ''
      if (url === PLATFORM_PUSH_COUNTS_URL) return Promise.resolve(COUNTS)
      if (url === `/api/business/orders/${ROW.order_no}/`) {
        return Promise.reject(new Error('订单不存在'))
      }
      return Promise.resolve(pageOf())
    })
    const el = mountSourceCell(root, { ...ROW })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'copyOrder')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 6000 }),
    )
    expect(writeText).not.toHaveBeenCalled()
    expect(LAYUI.msg).not.toHaveBeenCalledWith(COPY_ORDER_CONTENT_SUCCESS_MESSAGE, {
      time: 1500,
    })
    expect(COPY_ORDER_CONTENT_FAILED_MESSAGE).toBe('复制订单内容失败了，请手动复制')
  })

  it('菜单「查看订单截图」⇒ 开截图管理岛页（尺寸/anim 取订单列表页那颗）', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'viewPic')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.layer.open).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '查看订单截图',
        type: 2,
        anim: 1,
        area: ['80%', '85%'],
        content:
          `${import.meta.env.BASE_URL}layui-island/page.html?page=${SCREENSHOT_MANAGE_PAGE}` +
          `&order_no=${encodeURIComponent(ROW.order_no)}`,
      }),
    )
  })

  it('截图入口的单号形状闸：不合法 ⇒ 不开层 + 固定文案', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW, order_no: 'BAD' })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    menuItems()
      .find((item) => item.dataset.key === 'viewPic')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(SCREENSHOT_ORDER_NO_INVALID_MESSAGE, {
      time: 1500,
    })
  })

  it('菜单关掉的三个触发：点任意处 / Esc', async () => {
    const root = await mountPage()
    const el = mountSourceCell(root, { ...ROW })
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    expect(menu()).not.toBeNull()
    document.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(menu()).toBeNull()

    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    expect(menu()).not.toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(menu()).toBeNull()
  })

  it('整页渲染：显示值走来源单号，而 `t_orderid`（批量动作的寻址键）仍是内部单号', async () => {
    mockApi(pageOf([{ ...ROW, source_order_no: SOURCE }]))
    await mountPage()
    const rows = renderedRows()
    expect(rows[0]!['t_orderidView']).toBe(SOURCE)
    expect(rows[0]!['t_orderid']).toBe(ROW.order_no)
    // 拿**页面真的交给 layui 的那份列定义**里「系统单号」那一列的 templet，喂页面真的交给
    // layui 的那行数据 ⇒ 断言的是生产代码这条链（列定义 ↔ 显示值）。
    const cols = tableOptions()['cols'] as Array<Array<Record<string, unknown>>>
    const col = cols[0]!.find((item) => item['field'] === 't_orderid')!
    const html = (col['templet'] as (r: CoreOrderDisplayRow) => string)(rows[0]!)
    expect(html).toContain(`>${SOURCE}<`)
    expect(html).toContain(`color:${SOURCE_ORDER_NO_COLOR}`)
    expect(html).not.toContain(`>${ROW.order_no}<`)
  })
})

/* ── 需求 1：聚合折叠（后端 group_by=order 生效时的形态） ─────────────── */

const PUSH_A: PlatformPushSubRow = {
  platform: 10,
  serial_no: '19000000000000000101',
  state: 'sent',
  remote_status: 12,
  remote_cancel_status: null,
  price: '5.00',
}
const PUSH_B: PlatformPushSubRow = {
  platform: 10,
  serial_no: '19000000000000000102',
  state: 'sent',
  remote_status: 13,
  remote_cancel_status: null,
  price: '6.00',
}
const GROUP: PlatformPushGroupItem = {
  order_no: ROW.order_no,
  source_order_no: 'TB-2026-0001',
  order_status: 'IN_PROGRESS',
  game: '原神',
  server: '天空岛',
  pushes: [PUSH_A, PUSH_B],
}

function groupedPageOf(
  items: PlatformPushGroupItem[] = [GROUP],
  total: number = items.length,
): Page<PlatformPushGroupItem> {
  return { items, total, page: 1, page_size: 30 }
}

/** 造一个「layui 渲染出来」的表格骨架：主表一行 + 右侧固定列表一行（操作列 fixed）。 */
function mountTableRows(
  root: HTMLElement,
  row: CoreOrderDisplayRow,
): { mainTr: HTMLTableRowElement; fixedTr: HTMLTableRowElement } {
  const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
  const view = document.createElement('div')
  view.className = 'layui-table-view'
  const mainTable = document.createElement('table')
  const mainBody = document.createElement('tbody')
  const mainTr = document.createElement('tr')
  mainTr.dataset.index = '0'
  for (const col of cols) {
    const field = String(col['field'] ?? '')
    if (field === 't_change') continue // 操作列在右侧固定表里
    const td = document.createElement('td')
    td.dataset.field = field
    const cell = document.createElement('div')
    cell.className = 'layui-table-cell'
    const templet = col['templet'] as
      | ((r: CoreOrderDisplayRow) => string)
      | undefined
    cell.innerHTML = templet ? templet(row) : String(row[field] ?? '')
    td.appendChild(cell)
    mainTr.appendChild(td)
  }
  mainBody.appendChild(mainTr)
  mainTable.appendChild(mainBody)
  // ⚠️ 结构与 layui 真实渲染**一致**：主表那层是
  // `.layui-table-body.layui-table-main`，右侧固定列是
  // `.layui-table-fixed.layui-table-fixed-r > .layui-table-body` —— 固定列对齐补丁按这个结构找行。
  const main = document.createElement('div')
  main.className = 'layui-table-body layui-table-main'
  main.appendChild(mainTable)

  const fixed = document.createElement('div')
  fixed.className = 'layui-table-fixed layui-table-fixed-r'
  const fixedBodyWrap = document.createElement('div')
  fixedBodyWrap.className = 'layui-table-body'
  const fixedTable = document.createElement('table')
  const fixedBody = document.createElement('tbody')
  const fixedTr = document.createElement('tr')
  fixedTr.dataset.index = '0'
  const fixedTd = document.createElement('td')
  fixedTd.dataset.field = 't_change'
  const fixedCell = document.createElement('div')
  fixedCell.className = 'layui-table-cell'
  const changeCol = cols.find((item) => item['field'] === 't_change')!
  fixedCell.innerHTML = (
    changeCol['templet'] as (r: CoreOrderDisplayRow) => string
  )(row)
  fixedTd.appendChild(fixedCell)
  fixedTr.appendChild(fixedTd)
  fixedBody.appendChild(fixedTr)
  fixedTable.appendChild(fixedBody)
  fixedBodyWrap.appendChild(fixedTable)
  fixed.appendChild(fixedBodyWrap)

  const box = document.createElement('div')
  box.className = 'layui-table-box'
  box.append(main, fixed)
  view.appendChild(box)
  root.appendChild(view)
  return { mainTr, fixedTr }
}

describe('需求 1：同一订单的多条平台单聚合成一行（group_by=order）', () => {
  it('一个订单两条推送 ⇒ 折叠行只显示订单级事实 + 平台数量', () => {
    const row = toGroupDisplayRow(GROUP)
    expect(row['t_orderid']).toBe(ROW.order_no)
    expect(row['t_orderidView']).toBe('TB-2026-0001')
    expect(row['t_groupSize']).toBe('2')
    // 平台数量画在「平台交易单号」那一格（⛔ 不新增列）。
    expect(row['t_tid']).toBe('代练通 2 条')
    expect(row['t_gameServer']).toBe('原神 / 天空岛')
    expect(row['t_sta']).toBe('代练中')
    // 父行发单平台 / 发单价 / 交易状态 / 发单时间取组内**最新一笔**推送（此夹具两条都无
    // `created_at` / `is_active` ⇒ 按后端顺序，第一条就是「最新」）；推送行没有的值仍「—」（⛔ 不编）。
    expect(row['t_pushPltm']).toBe('代练通')
    expect(row['t_throw']).toBe('5.00')
    expect(row['t_pltSta']).toBe('正在代练')
    expect(row['t_pltStaClass']).toBe('layui-badge layui-bg-green')
    expect(row['t_safeeff']).toBe('—')
    expect(row['profit']).toBe('—')
    expect(row['t_shours']).toBe('—')
    expect(row['t_hour']).toBe('—')
  })

  /**
   * 聚合父行的「交易状态」= 组内**最新的一笔**代练通订单的状态：按 `created_at` 降序、
   * `id` 降序取第一条，⛔ **不看 `is_active`**，⛔ 不显示「—」。
   */
  it('⭐ 父行交易状态：最新的那条未接手（旧的已关闭）⇒「未接手」', () => {
    const closedOld: PlatformPushSubRow = {
      ...PUSH_B,
      state: 'closed',
      remote_status: 11,
      remote_cancel_status: 10,
      is_active: false,
      created_at: '2026-10-27T09:00:00+08:00',
    }
    const sentNew: PlatformPushSubRow = {
      ...PUSH_A,
      remote_status: 11,
      remote_cancel_status: 10,
      is_active: true,
      created_at: '2026-10-28T10:00:00+08:00',
    }
    // 组内顺序 = 后端给的「新在前」。
    const row = toGroupDisplayRow({ ...GROUP, pushes: [sentNew, closedOld] })
    expect(row['t_pltSta']).toBe('未接手')
    expect(row['t_pltStaClass']).toBe('layui-badge layui-bg-gray')
  })

  it('⭐ 父行交易状态：两条都已关闭 ⇒ 按最新一条显示「已关闭」（⛔ 不是「—」）', () => {
    const older: PlatformPushSubRow = {
      ...PUSH_A,
      state: 'closed',
      remote_status: 11,
      remote_cancel_status: 10,
      is_active: false,
      created_at: '2026-10-27T09:00:00+08:00',
    }
    const newer: PlatformPushSubRow = {
      ...PUSH_B,
      state: 'closed',
      remote_status: 18,
      remote_cancel_status: 10,
      is_active: false,
      created_at: '2026-10-28T11:00:00+08:00',
    }
    // 故意把**旧的放前面**：判据必须是「按时间取最新」，不是「取数组第一个」。
    const row = toGroupDisplayRow({ ...GROUP, pushes: [older, newer] })
    expect(row['t_pltSta']).toBe('已关闭')
    expect(row['t_pltStaClass']).toBe('layui-badge layui-bg-gray')
  })

  it('⭐ 父行交易状态：**按时间取最新那条**（业务方 2026-10-28 更正：⛔ 不再「活跃优先」）', () => {
    const stillCancelling: PlatformPushSubRow = {
      ...PUSH_A,
      state: 'sent',
      remote_status: 16,
      remote_cancel_status: 11,
      is_active: true,
      created_at: '2026-10-27T09:00:00+08:00',
    }
    const settledNewer: PlatformPushSubRow = {
      ...PUSH_B,
      state: 'sent',
      remote_status: 17,
      remote_cancel_status: 10,
      is_active: false,
      created_at: '2026-10-28T11:00:00+08:00',
    }
    // 新的那条（已结算）虽然 `is_active=false`，但它是**最新一笔代练通订单** ⇒ 父行就该显示它。
    const row = toGroupDisplayRow({ ...GROUP, pushes: [settledNewer, stillCancelling] })
    expect(row['t_pltSta']).toBe('已结算')
  })

  it('⭐ 父行交易状态：后端没下发 `display_status_label` 时也要带上 `CancelStatus` 判', () => {
    const push: PlatformPushSubRow = {
      ...PUSH_A,
      state: 'sent',
      remote_status: 16,
      remote_cancel_status: 12,
      is_active: true,
      created_at: '2026-10-28T11:00:00+08:00',
    }
    expect(toGroupDisplayRow({ ...GROUP, pushes: [push] })['t_pltSta']).toBe('已撤销')
    // 后端下发了就用后端的（它是当次事实）。
    expect(
      toGroupDisplayRow({
        ...GROUP,
        pushes: [{ ...push, display_status: 'revoked', display_status_label: '已撤销（后端）' }],
      })['t_pltSta'],
    ).toBe('已撤销（后端）')
  })

  it('⭐ 组内没有推送（后端不该造这种组）⇒ 状态仍「—」，⛔ 不编一个状态', () => {
    expect(toGroupDisplayRow({ ...GROUP, pushes: [] })['t_pltSta']).toBe('—')
  })

  it('平台数量文案：多平台去重、用「、」分隔（未知平台标「平台 N」）', () => {
    expect(groupPlatformCountText([PUSH_A, PUSH_B])).toBe('代练通 2 条')
    expect(groupPlatformCountText([PUSH_A, { ...PUSH_B, platform: 30 }])).toBe(
      '代练通、代练妈妈 2 条',
    )
    expect(platformLabelOf(10)).toBe('代练通')
    expect(platformLabelOf(999)).toBe('平台 999')
    expect(groupPlatformCountText([])).toBe('—')
  })

  it('子行每一格取这条推送自己的事实（平台 / 平台单号 / 交易状态 / 发单价 / 操作）', () => {
    expect(childCellHtml('t_tid', PUSH_A)).toBe(PUSH_A.serial_no)
    expect(childCellHtml('t_pushPltm', PUSH_A)).toBe('代练通')
    expect(childCellHtml('t_pltSta', PUSH_A)).toContain('正在代练')
    expect(childCellHtml('t_pltSta', PUSH_A)).toContain('layui-badge layui-bg-green')
    expect(childCellHtml('t_throw', PUSH_A)).toBe('5.00')
    // 没有目标（没给单号 / 键）⇒ 画「—」；给了目标 ⇒ 按该推送状态画按钮。
    expect(childCellHtml('t_change', PUSH_A)).toBe('—')
    expect(
      childCellHtml('t_change', PUSH_A, { orderNo: ROW.order_no, pushKey: 'p:0' }),
    ).toContain(`data-platform-action="${ROW_ACTION_NEGOTIATE}"`)
    // 订单级的列不在子行重复。
    expect(childCellHtml('t_gameServer', PUSH_A)).toBe('—')
    // 没有单号 / 没有价格 ⇒「—」
    expect(
      childCellHtml('t_tid', { ...PUSH_A, serial_no: null }),
    ).toBe('—')
    expect(childCellHtml('t_throw', { ...PUSH_A, price: '' })).toBe('—')
  })

  it('挂载聚合回包 ⇒ 一个订单**只渲染 1 个折叠行**（默认折叠、三角向右）', async () => {
    mockApi(groupedPageOf())
    const root = await mountPage()
    const rows = renderedRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]!['t_groupSize']).toBe('2')
    expect(rows[0]!['t_tid']).toBe('代练通 2 条')
    // 父行的 templet 带三角（`data-open="N"` = 默认折叠）。
    const cols = tableOptions()['cols'] as Array<Array<Record<string, unknown>>>
    const orderCol = cols[0]!.find((item) => item['field'] === 't_orderid')!
    const html = (orderCol['templet'] as (r: CoreOrderDisplayRow) => string)(
      rows[0]!,
    )
    expect(html).toContain('tt-order-tree')
    expect(html).toContain('data-open="N"')
    expect(html).toContain('layui-icon-triangle-r')
    // 订单号 / 系统单号两行都在（来源单号 + 内部单号）。
    expect(html).toContain('TB-2026-0001')
    expect(html).toContain(`系统单号：${ROW.order_no}`)
    // ⭐ 父行也要挂操作按钮，目标 = 该单**最新一笔**推送。
    const changeCol = cols[0]!.find((item) => item['field'] === 't_change')!
    const opsHtml = (changeCol['templet'] as (r: CoreOrderDisplayRow) => string)(rows[0]!)
    expect(opsHtml).toContain(`data-platform-action="${ROW_ACTION_NEGOTIATE}"`)
    expect(opsHtml).toContain(`data-push="${ROW_TARGET_LATEST}"`)
    expect(opsHtml).toContain(`data-order="${ROW.order_no}"`)
    void root
  })

  it('点三角 ⇒ 展开 **2 条平台子行**（主表 + 右侧固定列表各一遍）；再点收起', async () => {
    mockApi(groupedPageOf())
    const root = await mountPage()
    const { mainTr, fixedTr } = mountTableRows(root, renderedRows()[0]!)
    const triangle = mainTr.querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
    triangle.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    const mainChildren = mainTr.parentElement!.querySelectorAll(
      ORDER_CHILD_ROW_SELECTOR,
    )
    const fixedChildren = fixedTr.parentElement!.querySelectorAll(
      ORDER_CHILD_ROW_SELECTOR,
    )
    expect(mainChildren).toHaveLength(2)
    expect(fixedChildren).toHaveLength(2)
    // 子行内容是**每条推送自己的事实**（不是父行重复）。
    expect((mainChildren[0] as HTMLElement).textContent).toContain(PUSH_A.serial_no!)
    expect((mainChildren[1] as HTMLElement).textContent).toContain(PUSH_B.serial_no!)
    expect((mainChildren[0] as HTMLElement).querySelector('[data-field="t_pushPltm"]')!.textContent).toBe('代练通')
    expect((mainChildren[1] as HTMLElement).querySelector('[data-field="t_throw"]')!.textContent).toBe('6.00')
    // 「操作」列是 `fixed:'right'` ⇒ 子行的操作按钮在右侧固定表那一份里。
    // PUSH_A 是「正在代练(12)」⇒ 子行只画「协商撤销 + 加款」两颗。
    expect(
      (fixedChildren[0] as HTMLElement).querySelectorAll('[data-platform-action]'),
    ).toHaveLength(2)
    expect(triangle.dataset.open).toBe('Y')

    // 再点一次 ⇒ 收起（两张表的子行都删干净）。
    triangle.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(mainTr.parentElement!.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(0)
    expect(fixedTr.parentElement!.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(0)
    expect(triangle.dataset.open).toBe('N')
    void root
  })

  it('翻页 / 刷新后折叠状态复位：重渲染前清掉所有子行', async () => {
    mockApi(groupedPageOf())
    const root = await mountPage()
    const { mainTr } = mountTableRows(root, renderedRows()[0]!)
    const triangle = mainTr.querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
    triangle.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 主表 2 条 + 右侧固定列表 2 条（两张表各插一遍，否则固定列会错行）。
    expect(root.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(4)

    // 翻页（第 2 页）→ 重取 → 重渲染
    const jump = pagerOptions()['jump'] as (obj: { curr?: number }) => void
    jump({ curr: 2 })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    // 重渲染发生在新回包落地之后 ⇒ 等子行被清掉（⛔ 不能只看「请求发出去了」）。
    await vi.waitFor(() =>
      expect(root.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(0),
    )
    // 新一屏的父行仍然是默认折叠。
    const cols = tableOptions()['cols'] as Array<Array<Record<string, unknown>>>
    const orderCol = cols[0]!.find((item) => item['field'] === 't_orderid')!
    expect(
      (orderCol['templet'] as (r: CoreOrderDisplayRow) => string)(renderedRows()[0]!),
    ).toContain('data-open="N"')
  })

  it('后端未升级（回包没有 pushes）⇒ 退回扁平渲染：一行一条推送、无三角、无折叠', async () => {
    mockApi(pageOf([ROW]))
    const root = await mountPage()
    const rows = renderedRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]!['t_groupSize']).toBeUndefined()
    const cols = tableOptions()['cols'] as Array<Array<Record<string, unknown>>>
    const orderCol = cols[0]!.find((item) => item['field'] === 't_orderid')!
    expect(
      (orderCol['templet'] as (r: CoreOrderDisplayRow) => string)(rows[0]!),
    ).not.toContain('tt-order-tree')
    expect(root.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(0)
  })

  it('一行也不丢：两个订单各 2 条推送 ⇒ 渲染 2 个折叠行（订单数 = 分页 total）', async () => {
    const second: PlatformPushGroupItem = {
      ...GROUP,
      order_no: '20261027000000000002',
      source_order_no: 'TB-2026-0002',
    }
    mockApi(groupedPageOf([GROUP, second], 2))
    await mountPage()
    expect(renderedRows()).toHaveLength(2)
    expect(pagerOptions()['count']).toBe(2)
  })
})

/* ── 需求 4：行内订单标签设 / 清 ─────────────────────────────── */

describe('⭐ N7：行内「设标签」控件**已整条删除**（业务方 2026-10-28）', () => {
  it('「平台交易单号」格只剩单号文本：字面与 class 都不在（逐档：有标签 / 无标签）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const serialTemplet = cols[1]!['templet'] as (row: CoreOrderDisplayRow) => string
    for (const label of ['', '包上分']) {
      const html = serialTemplet(toDisplayRow({ ...ROW, order_label: label }))
      // 字面
      expect(html).not.toContain('设标签')
      // class（连「标签块」那一档也不许再出现）
      expect(html).not.toContain('selectOrderLabelBtn')
      // 也不许留一个空壳 `<br/>`（原先是「单号 + <br/> + 控件」）
      expect(html).not.toContain('<br/>')
      // 单号本身还在。
      expect(html).toContain(ROW.serial_no!)
    }
  })

  it('挂载整页后，每一行的操作格 / 单号格里都没有「设标签」', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { mainTr, fixedTr } = mountTableRows(root, renderedRows()[0]!)
    for (const tr of [mainTr, fixedTr]) {
      expect(tr.innerHTML).not.toContain('设标签')
      expect(tr.innerHTML).not.toContain('selectOrderLabelBtn')
    }
    // 子行同样（展开后再查一遍）。
    mainTr
      .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    for (const child of Array.from(
      mainTr.parentElement!.querySelectorAll<HTMLElement>(ORDER_CHILD_ROW_SELECTOR),
    )) {
      expect(child.innerHTML).not.toContain('设标签')
      expect(child.innerHTML).not.toContain('selectOrderLabelBtn')
    }
    // 点那一片区域也不再弹任何输入层（零请求 / 零弹层）。
    requestMock.mockClear()
    mainTr
      .querySelector<HTMLElement>(`td[data-field="t_tid"] .layui-table-cell`)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(requestMock).not.toHaveBeenCalled()
  })
})

/* ── 需求 6：筛选控件「后端未支持」登记 ───────────────────────── */

describe('需求 6：筛选控件逐条登记「后端未支持」（⛔ 不做假控件）', () => {
  it('登记表恰好这 9 组；`#key1`（能搜）与隐藏的 `#operator` 不在表里', () => {
    expect(UNSUPPORTED_FILTERS.map((item) => item.id)).toEqual([
      'gameName',
      'dateType',
      'orderSoures',
      'pushPlatm',
      'orderSta',
      'projectType',
      'seachType',
      'orderLabel',
      'orderBy',
    ])
    expect(UNSUPPORTED_FILTERS.some((item) => item.id === 'key1')).toBe(false)
    expect(UNSUPPORTED_FILTERS.some((item) => item.id === 'operator')).toBe(false)
  })

  it('挂载后控件 `title` 写明原因（日期那一组的两个控件都要登记）', async () => {
    const root = await mountPage()
    for (const id of ['gameName', 'orderSta', 'pushPlatm', 'seachType', 'orderBy']) {
      expect(root.querySelector<HTMLElement>(`#${id}`)!.title).toContain(
        UNSUPPORTED_FILTER_PREFIX,
      )
    }
    expect(root.querySelector<HTMLElement>('#dateType')!.title).toContain(
      UNSUPPORTED_FILTER_PREFIX,
    )
    expect(root.querySelector<HTMLElement>('#datetime')!.title).toContain(
      UNSUPPORTED_FILTER_PREFIX,
    )
    // 能搜的控件不登记。
    expect(root.querySelector<HTMLElement>('#key1')!.title).not.toContain(
      UNSUPPORTED_FILTER_PREFIX,
    )
  })

  it('默认值不算「改过」；改了系统状态 / 选了日期才算', async () => {
    const root = await mountPage()
    expect(unsupportedFilterHits(root)).toEqual([])

    const orderSta = root.querySelector<HTMLSelectElement>('#orderSta')!
    orderSta.selectedIndex = 1
    expect(unsupportedFilterHits(root).map((hit) => hit.label)).toEqual(['系统状态'])

    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-10-01'
    expect(unsupportedFilterHits(root).map((hit) => hit.label)).toEqual([
      '发单时间',
      '系统状态',
    ])
  })

  it('点搜索：先给一句中文提示；请求参数**只有真支持的**（⛔ 不偷偷传没实现的键）', async () => {
    const root = await mountPage()
    const orderSta = root.querySelector<HTMLSelectElement>('#orderSta')!
    orderSta.selectedIndex = 1
    const game = root.querySelector<HTMLSelectElement>('#gameName')!
    game.selectedIndex = 1
    root
      .querySelector('button.sechbtn')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    expect(LAYUI.msg).toHaveBeenCalledWith(
      unsupportedFiltersMessage(unsupportedFilterHits(root)),
      { time: 1500 },
    )
    expect(requestConfig(1).params).toEqual({
      platform: 10,
      page: 1,
      page_size: 30,
      group_by: 'order',
    })
    expect(countsRequests()[1]!.params).toEqual({ platform: 10 })
  })

  it('`registerUnsupportedFilters` 不改控件的值 / option（证据件一字不改）', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<select id="orderSta"><option value="">全部</option><option value="待上号">待上号</option></select>' +
      '<input id="sDate" type="hidden" value="" />'
    const select = root.querySelector<HTMLSelectElement>('#orderSta')!
    const before = select.innerHTML
    registerUnsupportedFilters(root)
    expect(select.innerHTML).toBe(before)
    expect(select.selectedIndex).toBe(0)
    expect(select.title).toContain(UNSUPPORTED_FILTER_PREFIX)
  })
})

/* ── 需求 7：没有数据源的不自造 ─────────────────────────────── */

describe('需求 7：未读消息角标 / 平台交易详情（读面没有数据源，登记不做）', () => {
  it('平台交易单号格只有单号 + 同步 tooltip：不造假角标、不摆假链接', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const templet = cols[1]!['templet'] as (row: CoreOrderDisplayRow) => string
    const html = templet(toDisplayRow({ ...ROW }))
    expect(html).not.toContain('viewPlatmTid')
    expect(html).not.toContain('newMsgCount')
    expect(html).not.toContain('未读')
    // 仍是真的单号 + 同步信息 tooltip（已有的一面不许丢）。
    expect(html).toContain(ROW.serial_no!)
    expect(html).toContain('最近同步：')
  })
})

/* ── 只有 1 条平台单 ⇒ 正常平铺（不折叠、不出三角） ──── */

/** 单条推送的订单（一条推送 = 普通一行）。 */
const PUSH_ONE: PlatformPushSubRow = {
  platform: 10,
  serial_no: '19000000000000000301',
  state: 'sent',
  remote_status: 11,
  remote_cancel_status: 10,
  price: '7.50',
  id: 301,
  created_at: '2026-10-27T09:30:00+08:00',
  last_synced_at: '2026-10-27T10:00:00+08:00',
  last_error: '',
  is_active: true,
}
const GROUP_ONE: PlatformPushGroupItem = {
  order_no: '20261027000000000003',
  source_order_no: 'TB-2026-0003',
  order_status: 'IN_PROGRESS',
  game: '原神',
  server: '天空岛',
  pushes: [PUSH_ONE],
}

describe('h6x R1：一条推送的订单 ⇒ 正常平铺（不出三角、不折叠）', () => {
  it('toSinglePushDisplayRow：推送自己的事实照实铺满列（t_groupSize 不出现）', () => {
    const row = toSinglePushDisplayRow(GROUP_ONE)
    expect(row['t_groupSize']).toBeUndefined()
    expect(row['t_tid']).toBe(PUSH_ONE.serial_no)
    expect(row['t_throw']).toBe('7.50')
    expect(row['t_pushPltm']).toBe('代练通')
    expect(row['t_pltSta']).toBe('未接手')
    expect(row['t_pltStaClass']).toBe('layui-badge layui-bg-gray')
    expect(row['t_gameServer']).toBe('原神 / 天空岛')
    expect(row['t_sta']).toBe('代练中')
    expect(row['t_hour']).toBe('2026-10-27 09:30')
    expect(row['t_orderid']).toBe(GROUP_ONE.order_no)
    expect(row['t_orderidView']).toBe('TB-2026-0003')
    // 操作按钮要能定位到**这条推送**（行内动作接线靠这个键）。
    expect(row['t_pushKey']).toBe('p:0')
  })

  it('只有 1 条的订单挂载后就是普通一行：无三角、操作按钮在行内', async () => {
    mockApi(groupedPageOf([GROUP_ONE]))
    const root = await mountPage()
    const rows = renderedRows()
    expect(rows).toHaveLength(1)
    expect(rows[0]!['t_groupSize']).toBeUndefined()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const orderCol = cols.find((item) => item['field'] === 't_orderid')!
    const idHtml = (orderCol['templet'] as (r: CoreOrderDisplayRow) => string)(rows[0]!)
    expect(idHtml).not.toContain(ORDER_TREE_CLASS)
    expect(idHtml).not.toContain('data-open=')
    const changeCol = cols.find((item) => item['field'] === 't_change')!
    const opsHtml = (changeCol['templet'] as (r: CoreOrderDisplayRow) => string)(rows[0]!)
    expect(opsHtml).toContain(`data-platform-action="${ROW_ACTION_EDIT}"`)
    void root
  })

  it('同一屏里 1 条 / 2 条混排：1 条平铺无三角，2 条才折叠', async () => {
    mockApi(groupedPageOf([GROUP_ONE, GROUP], 2))
    const root = await mountPage()
    const rows = renderedRows()
    expect(rows).toHaveLength(2)
    expect(rows[0]!['t_groupSize']).toBeUndefined()
    expect(rows[1]!['t_groupSize']).toBe('2')
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const orderCol = cols.find((item) => item['field'] === 't_orderid')!
    const templet = orderCol['templet'] as (r: CoreOrderDisplayRow) => string
    expect(templet(rows[0]!)).not.toContain(ORDER_TREE_CLASS)
    expect(templet(rows[1]!)).toContain(ORDER_TREE_CLASS)
    // 只有 2 条那条能展开出子行。
    const { mainTr } = mountTableRows(root, rows[1]!)
    mainTr
      .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(mainTr.parentElement!.querySelectorAll(ORDER_CHILD_ROW_SELECTOR)).toHaveLength(2)
  })

  it('0 条推送的异常契约：照订单级事实平铺、不画三角、操作列不挂按钮', async () => {
    const empty: PlatformPushGroupItem = { ...GROUP_ONE, pushes: [] }
    expect(toSinglePushDisplayRow(empty)['t_pushKey']).toBe('')
    mockApi(groupedPageOf([empty]))
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const changeCol = cols.find((item) => item['field'] === 't_change')!
    expect(
      (changeCol['templet'] as (r: CoreOrderDisplayRow) => string)(renderedRows()[0]!),
    ).toBe('—')
  })
})


/* ── 右侧固定列与主表逐行对齐 ──────────── */

describe('h6x N5：右侧固定列（fixed:right）与主表逐行对齐 + 点第 N 行作用第 N 行', () => {
  /**
   * 给「表格行」桩一个高度：jsdom 没有布局引擎，`getBoundingClientRect()` 恒 0，
   * 而这段补丁的判据就是**高度**⇒ 只能用 spy 造高（真实数值由 Playwright 实测，见报告）。
   */
  function stubRowHeights(childHeight = 37, rowHeight = 67): { restore: () => void } {
    const spy = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: HTMLElement): DOMRect {
        const height =
          this instanceof HTMLTableRowElement
            ? this.classList.contains(ORDER_CHILD_ROW_CLASS)
              ? childHeight
              : rowHeight
            : 0
        return {
          height,
          width: 100,
          top: 0,
          bottom: height,
          left: 0,
          right: 100,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        } as DOMRect
      })
    return { restore: () => spy.mockRestore() }
  }

  /** 造一份**与 layui 真实结构一致**的表格骨架（主表 + 右侧固定列）。 */
  function buildSkeleton(rows: number): {
    root: HTMLElement
    mainRows: HTMLTableRowElement[]
    fixedRows: HTMLTableRowElement[]
  } {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const view = document.createElement('div')
    view.className = 'layui-table-view'
    const box = document.createElement('div')
    box.className = 'layui-table-box'
    const main = document.createElement('div')
    main.className = 'layui-table-body layui-table-main'
    const fixed = document.createElement('div')
    fixed.className = 'layui-table-fixed layui-table-fixed-r'
    const fixedBody = document.createElement('div')
    fixedBody.className = 'layui-table-body'
    const mainTable = document.createElement('table')
    const mainTbody = document.createElement('tbody')
    const fixedTable = document.createElement('table')
    const fixedTbody = document.createElement('tbody')
    for (let i = 0; i < rows; i += 1) {
      const mtr = document.createElement('tr')
      mtr.dataset.index = String(i)
      const mtd = document.createElement('td')
      mtd.dataset.field = 't_tid'
      const mcell = document.createElement('div')
      mcell.className = 'layui-table-cell'
      mcell.textContent = `r${i}`
      mtd.appendChild(mcell)
      mtr.appendChild(mtd)
      mainTbody.appendChild(mtr)

      const ftr = document.createElement('tr')
      ftr.dataset.index = String(i)
      const ftd = document.createElement('td')
      ftd.dataset.field = 't_change'
      const fcell = document.createElement('div')
      fcell.className = 'layui-table-cell'
      fcell.textContent = `b${i}`
      ftd.appendChild(fcell)
      ftr.appendChild(ftd)
      fixedTbody.appendChild(ftr)
    }
    mainTable.appendChild(mainTbody)
    main.appendChild(mainTable)
    fixedTable.appendChild(fixedTbody)
    fixedBody.appendChild(fixedTable)
    fixed.appendChild(fixedBody)
    box.append(main, fixed)
    view.appendChild(box)
    root.appendChild(view)
    return {
      root,
      mainRows: Array.from(mainTbody.querySelectorAll('tr')),
      fixedRows: Array.from(fixedTbody.querySelectorAll('tr')),
    }
  }

  it('syncFixedRowHeights：主表每行的高度**逐行**写给固定列，并把操作格内容垂直居中', () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const view = document.createElement('div')
    view.className = 'layui-table-view'
    const box = document.createElement('div')
    box.className = 'layui-table-box'
    const main = document.createElement('div')
    main.className = 'layui-table-body layui-table-main'
    main.innerHTML =
      '<table><tbody>' +
      '<tr><td><div class="layui-table-cell">r0</div></td></tr>' +
      '<tr><td><div class="layui-table-cell">r1</div></td></tr>' +
      '<tr><td><div class="layui-table-cell">r2</div></td></tr>' +
      '</tbody></table>'
    const fixed = document.createElement('div')
    fixed.className = 'layui-table-fixed layui-table-fixed-r'
    fixed.innerHTML =
      '<div class="layui-table-body"><table><tbody>' +
      '<tr><td><div class="layui-table-cell">b0</div></td></tr>' +
      '<tr><td><div class="layui-table-cell">b1</div></td></tr>' +
      '<tr><td><div class="layui-table-cell">b2</div></td></tr>' +
      '</tbody></table></div>'
    box.append(main, fixed)
    view.appendChild(box)
    root.appendChild(view)

    const heights = [67, 37, 65]
    Array.from(main.querySelectorAll<HTMLElement>('tbody tr')).forEach((tr, i) => {
      Object.defineProperty(tr, 'getBoundingClientRect', {
        configurable: true,
        value: () => ({ height: heights[i] ?? 0 }) as DOMRect,
      })
    })

    syncFixedRowHeights(root)

    const fixedRows = Array.from(fixed.querySelectorAll<HTMLTableRowElement>('tbody tr'))
    expect(fixedRows.map((tr) => tr.style.height)).toEqual(['67px', '37px', '65px'])
    const cell = fixedRows[0]!.querySelector<HTMLElement>('.layui-table-cell')!
    expect(cell.style.height).toBe('100%')
    expect(cell.style.display).toBe('flex')
    expect(cell.style.alignItems).toBe('center')
  })

  it('量不到高度（0px，例如行还没布局）⇒ 不写 `0px` 把行压扁', () => {
    const { root, fixedRows } = buildSkeleton(2)
    syncFixedRowHeights(root)
    expect(fixedRows.map((tr) => tr.style.height)).toEqual(['', ''])
  })

  it('挂载 → 展开 → 收起 → 翻页：固定列行数 == 主表行数，且每行都被对上高度', async () => {
    const { restore } = stubRowHeights()
    try {
      mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
      const root = await mountPage()
      const { mainTr } = mountTableRows(root, renderedRows()[0]!)
      /** 读本次骨架里主表 / 右侧固定列的行数与固定列行高（模拟 layui 的两份 `<tr>`）。 */
      const snapshot = (): { main: number; fixed: number; heights: string[] } => {
        const view = mainTr.closest('.layui-table-view')!
        const main = view.querySelectorAll('.layui-table-main tbody > tr')
        const fixed = Array.from(
          view.querySelectorAll<HTMLTableRowElement>(
            '.layui-table-fixed-r .layui-table-body tbody > tr',
          ),
        )
        return {
          main: main.length,
          fixed: fixed.length,
          heights: fixed.map((tr) => tr.style.height),
        }
      }
      // 页面的 renderRows.done 会调补丁 ⇒ 手动跑一次 done（单测里 table.render 是替身）。
      ;(tableOptions()['done'] as () => void)()
      expect(snapshot()).toEqual({ main: 1, fixed: 1, heights: ['67px'] })

      // 展开 ⇒ 主表 +2、固定列 +2，且子行高度按子行高度对齐。
      mainTr
        .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(snapshot()).toEqual({
        main: 3,
        fixed: 3,
        heights: ['67px', '37px', '37px'],
      })

      // 收起 ⇒ 两边都回到 1 行。
      mainTr
        .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(snapshot()).toEqual({ main: 1, fixed: 1, heights: ['67px'] })

      // 翻页 ⇒ 重渲染后仍是「一份主表 = 一份固定列」。
      const jump = pagerOptions()['jump'] as (obj: { curr?: number }) => void
      jump({ curr: 2 })
      await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
      await vi.waitFor(() => expect(mainTr.closest('.layui-table-view')).not.toBeNull())
    } finally {
      restore()
    }
  })

  it('点第 2 条子行的按钮 ⇒ 作用在**第 2 条**推送（不串行）', async () => {
    // 三条推送，各自的 `pay_level_bal` 不同，用弹层默认值认人。
    const three: PlatformPushGroupItem = {
      ...GROUP,
      pushes: [
        { ...PUSH_A, id: 901, created_at: '2026-10-27T09:00:00+08:00', pay_level_bal: '1.00', rep_ensure_bal: '1.00' },
        { ...PUSH_A, id: 902, created_at: '2026-10-27T10:00:00+08:00', pay_level_bal: '2.00', rep_ensure_bal: '2.00' },
        { ...PUSH_A, id: 903, created_at: '2026-10-27T11:00:00+08:00', pay_level_bal: '3.00', rep_ensure_bal: '3.00' },
      ],
    }
    mockGroupedWrites(() => pushRowResult(), [three])
    const root = await mountPage()
    const { mainTr } = mountTableRows(root, renderedRows()[0]!)
    mainTr
      .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const fixedBody = mainTr
      .closest('.layui-table-view')!
      .querySelector<HTMLElement>('.layui-table-fixed-r .layui-table-body')!
    const childRows = Array.from(
      fixedBody.querySelectorAll<HTMLTableRowElement>(ORDER_CHILD_ROW_SELECTOR),
    )
    expect(childRows).toHaveLength(3)
    // 第 2 条子行（下标 1）的「协商撤销」⇒ 弹层默认值必须是 2.00。
    childRows[1]!
      .querySelector<HTMLElement>(`[data-platform-action="${ROW_ACTION_NEGOTIATE}"]`)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const html = String(lastLayerOpen()['content'])
    expect(html).toContain('value="2.00"')
    expect(html).not.toContain('value="1.00"')
    expect(html).not.toContain('value="3.00"')
  })
})


/* ── 写动作失败分档 / 弹层闸释放 ──────────────── */

describe('评审 C1：行内写动作失败要**分档**（未知档必须刷新，⛔ 不许让人盲目重试）', () => {
  /** 走一遍「加款」：点按钮 → 输入 5 → 确认 → 提交。 */
  async function submitAmount(
    write: (url: string, data: Record<string, unknown>) => unknown,
  ): Promise<void> {
    mockGroupedWrites(write, [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_AMOUNT)
    lastPrompt().yes('5', 1)
    requestMock.mockClear()
    lastConfirm().yes(1)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
  }

  it('网络 / 超时（结果未知）⇒「已发出，请刷新核对」+ **强制刷新列表** + 不自动重发', async () => {
    await submitAmount(() => Promise.reject(new NetworkError('请求超时')))
    // 加款是**增量**动作、没有幂等键 ⇒ 不能只说一句「网络异常」让人再点一次。
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenLastCalledWith(`请求超时（${ROW_WRITE_UNKNOWN_NOTE}）`, {
        time: 6000,
      }),
    )
    expect(ROW_WRITE_UNKNOWN_NOTE).toContain('已发出，请刷新核对')
    // 强制刷新：列表 + 页签数字各重取一次（让客服看到钱到底加没加）。
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
    await vi.waitFor(() => expect(countsRequests()).toHaveLength(1))
    // ⛔ 不自动重发（重发 = 重复加钱）。
    expect(pushWrites()).toHaveLength(1)
  })

  it('5xx 也算「结果未知」⇒ 同样刷新（可能已经写进上游，只是回包坏了）', async () => {
    await submitAmount(() => Promise.reject(new HttpError(502, '网关错误')))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenLastCalledWith(`网关错误（${ROW_WRITE_UNKNOWN_NOTE}）`, {
        time: 6000,
      }),
    )
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
  })

  it('业务失败（后端明确拒绝）⇒ 原样显示后端中文、**不刷新**、不自动重发', async () => {
    const msg = '补款只能给「正在代练」或「待验收」的代练通订单做'
    await submitAmount(() => Promise.reject(new BizError(1002, msg)))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenLastCalledWith(msg, { time: 6000 }),
    )
    // 什么都没变 ⇒ 不必刷一屏（也不该顺手改掉页面上的数据）。
    expect(listRequests()).toHaveLength(0)
    expect(pushWrites()).toHaveLength(1)
  })
})

describe('评审 I1：弹层闸必须在**任何关闭路径**上释放（否则后续按钮全静默失效）', () => {
  it('confirm 的选项带 `end`：layui 的 `layer.msg` 会静默关掉它且不回调 `cancel`', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_MIXED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    const end = lastConfirm().options['end'] as (() => void) | undefined
    expect(typeof end).toBe('function')
    // 模拟「layer.msg 把它静默关掉」：只跑 end，不走 cancel / btn2。
    end?.()
    // 闸放开了 ⇒ 再点同一颗按钮还能再开一层。
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    expect(LAYUI.confirm).toHaveBeenCalledTimes(2)
  })

  it('prompt 的选项也带 `end`（加款 / 记退款这类输入层同罪）', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_AMOUNT)
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    const end = lastPrompt().options['end'] as (() => void) | undefined
    expect(typeof end).toBe('function')
    end?.()
    clickRowAction(fixedTr, ROW_ACTION_AMOUNT)
    expect(LAYUI.prompt).toHaveBeenCalledTimes(2)
  })

  it('`layer.open` 的两个表单弹层也带 `end`（任何关闭路径都释放闸）', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const end = lastLayerOpen()['end'] as (() => void) | undefined
    expect(typeof end).toBe('function')
    end?.()
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    expect(LAYUI.layerOpen).toHaveBeenCalledTimes(2)
  })

  /**
   * 迟到的 `layer.msg`（`closeAll('dialog')`）静默关层时只回调 `end` 不回调 `cancel`
   * ⇒ 弹层漏挂 `end` 会让闸永真、整页按钮失灵。判据与上面三条同形。
   */
  it('「改系统状态」弹层也带 `end`（M8：漏挂它会让整页按钮永久失效）', async () => {
    selectRows(ROW.order_no)
    const root = await mountPage()
    const holder = mountToolbar(root)
    clickToolbar(holder, 'changeStaBtn')
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    const end = lastConfirm().options['end'] as (() => void) | undefined
    expect(typeof end).toBe('function')
    // 模拟 `layer.msg` 把它静默关掉：只跑 end，不走 cancel / btn2 / yes。
    end?.()
    clickToolbar(holder, 'changeStaBtn')
    expect(LAYUI.confirm).toHaveBeenCalledTimes(2)
  })
})


/* ── 删 5 列 / 补 4 列 / 新增 4 列 ──────────── */

describe('h6x N7：列集合**恰好**是这 24 列（删的 5 列不许留空列）', () => {
  /** 期望的表头（顺序 = 渲染顺序；第 1 列是勾选列、没有标题）。 */
  const EXPECTED_TITLES = [
    '',
    '平台交易单号',
    '系统单号',
    '游戏区服',
    '发单平台',
    '发单价',
    '接单价',
    '利润',
    '剩余时间',
    '交易状态',
    '接手人',
    '完成单数',
    '投诉/取消',
    '接单时间',
    '系统状态',
    '验收倒计',
    '订单来源',
    '发单时间',
    '接手时间',
    '完单时间',
    '录单时间',
    '撤销时间',
    '发单人',
    '操作',
  ]

  it('表头逐列名对拍（恰好 24 列）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    expect(cols.map((col) => String(col['title'] ?? ''))).toEqual(EXPECTED_TITLES)
    expect(cols).toHaveLength(24)
  })

  it('删掉的 5 列（订单备注 / 项目分类 / 发单标题 / 角色名 / 旺旺-WX）连同 field / 排序键一起消失', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    for (const gone of ['订单备注', '项目分类', '发单标题', '角色名', '旺旺/WX']) {
      expect(cols.some((col) => col['title'] === gone)).toBe(false)
    }
    for (const gone of ['t_remark', 't_projectTypeName', 't_orderTitle', 't_role', 't_ww']) {
      expect(cols.some((col) => col['field'] === gone)).toBe(false)
    }
    // 只剩 24 列 ⇒ 不许留一个空标题的占位列（勾选列除外）。
    expect(cols.filter((col) => col['type'] !== 'checkbox')).toHaveLength(23)
  })

  it('新增的四列展示在「交易状态」附近（接手人 / 完成单数 / 投诉-取消 / 接单时间）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const indexOf = (title: string): number =>
      cols.findIndex((col) => col['title'] === title)
    const trade = indexOf('交易状态')
    for (const title of ['接手人', '完成单数', '投诉/取消', '接单时间']) {
      const at = indexOf(title)
      expect(at).toBeGreaterThan(trade)
      expect(at - trade).toBeLessThanOrEqual(4)
    }
  })
})

describe('h6x N7：补上的列取真值（接单价 / 利润 / 订单来源 / 验收倒计时）', () => {
  /** 一条带新契约键的推送（后端同期在下发；缺键一律降级「—」）。 */
  const PUSH_NEW: PlatformPushSubRow = {
    ...PUSH_A,
    id: 1101,
    created_at: '2026-10-28T09:00:00+08:00',
    accept_price: '8.00',
    order_source: '代练通',
    verify_deadline_at: '2026-10-28T15:00:00+08:00',
    verify_remaining_seconds: 10800,
    accept_user_name: '打手小王',
    accept_uid: 'UID-7788',
    accept_over_count: 42,
    cust_cancel_count: 3,
    remote_accepted_at: '2026-10-28T09:05:30+08:00',
  }

  it('toDisplayRow：接单价 / 利润（接单价−发单价）/ 订单来源 / 接手人 / 完成单数 / 投诉-取消 / 接单时间都取真值', () => {
    const row = toDisplayRow({
      id: 1,
      platform: 10,
      serial_no: '19000000000000000101',
      order_no: ROW.order_no,
      game: '原神',
      server: '天空岛',
      price: '5.00',
      state: 'sent',
      remote_status: 12,
      remote_cancel_status: null,
      order_status: 'IN_PROGRESS',
      created_at: '2026-10-28T09:00:00+08:00',
      last_synced_at: null,
      last_error: '',
      accept_price: '8.00',
      order_source: '代练通',
      accept_user_name: '打手小王',
      accept_uid: 'UID-7788',
      accept_over_count: 42,
      cust_cancel_count: 3,
      remote_accepted_at: '2026-10-28T09:05:30+08:00',
    })
    expect(row['t_safeeff']).toBe('8.00')
    // 利润 = 接单价 − 发单价 = 8.00 − 5.00
    expect(row['profit']).toBe('3.00')
    expect(row['t_soure']).toBe('代练通')
    expect(row['t_acceptUser']).toBe('打手小王')
    expect(row['t_acceptOver']).toBe('42')
    expect(row['t_custCancel']).toBe('3')
    expect(row['t_remoteAccept']).toBe('2026-10-28 09:05')
  })

  it('利润：负数照实显示并标红（⛔ 不吞成 0）', () => {
    expect(profitTextOf('3.00', '5.00')).toBe('-2.00')
    expect(isNegativeMoney(profitTextOf('3.00', '5.00'))).toBe(true)
    expect(isNegativeMoney(profitTextOf('8.00', '5.00'))).toBe(false)
    // 缺一边 ⇒「—」（⛔ 不拿 0 充数）
    expect(profitTextOf('', '5.00')).toBe('—')
    expect(profitTextOf('3.00', '')).toBe('—')
    expect(profitTextOf(null, '5.00')).toBe('—')
    expect(profitTextOf('3.00', null)).toBe('—')
  })

  it('订单来源：有值就显示、缺键才「—」（⭐ N8 起后端保证只给中文名，数字串也照显）', () => {
    expect(orderSourceText('代练通')).toBe('代练通')
    expect(orderSourceText('')).toBe('—')
    expect(orderSourceText(null)).toBe('—')
    expect(orderSourceText(undefined)).toBe('—')
    // 纯数字串的取舍在 N8 那条用例里钉（后端 `f5ecd335` 之后**照显**）。
  })

  it('新增列的「空值」一律降级「—」（后端未升级时不报错、不空白）', () => {
    const bare = toDisplayRow({ ...ROW })
    for (const field of [
      't_safeeff',
      'profit',
      't_soure',
      't_acceptUser',
      't_acceptOver',
      't_custCancel',
      't_remoteAccept',
      't_exAccpetTime',
    ]) {
      expect(bare[field]).toBe('—')
    }
  })

  it('投诉-取消 > 0 才标红（0 / 缺键不标）', () => {
    expect(custCancelAlert(3)).toBe(true)
    expect(custCancelAlert(0)).toBe(false)
    expect(custCancelAlert(null)).toBe(false)
    expect(custCancelAlert(undefined)).toBe(false)
  })

  it('父行（折叠）取**最新一笔**推送的这些字段；子行各取自己那条', () => {
    const older = { ...PUSH_NEW, id: 1201, accept_price: '1.00', accept_user_name: '旧打手', created_at: '2026-10-27T09:00:00+08:00' }
    const newer = { ...PUSH_NEW, id: 1202, created_at: '2026-10-28T12:00:00+08:00' }
    const row = toGroupDisplayRow({ ...GROUP, pushes: [newer, older] })
    expect(row['t_safeeff']).toBe(newer.accept_price)
    expect(row['t_acceptUser']).toBe(newer.accept_user_name)
    // 子行：旧那条显示**它自己**的接单价（不是父行 latest 的）
    expect(childCellHtml('t_safeeff', older)).toBe('1.00')
    // 子行的验收倒计也是**可跳动**的节点（带倒计时 class 与 data-*，父行/子行同一套）
    const childCountdown = childCellHtml('t_exAccpetTime', older)
    expect(childCountdown).toContain(VERIFY_COUNTDOWN_CLASS)
    expect(childCountdown).toContain('data-verify-deadline=')
    // 没有截止时间 / 快照的推送 ⇒「—」（⛔ 不画一个空节点）
    expect(
      childCellHtml('t_exAccpetTime', { ...older, verify_deadline_at: null, verify_remaining_seconds: null }),
    ).toBe('—')
    // 子行的「接手人」带平台 UID 的 `title`（有 UID 才有 title）
    expect(childCellHtml('t_acceptUser', older)).toContain('>旧打手<')
    expect(childCellHtml('t_acceptUser', older)).toContain('平台 UID：UID-7788')
  })
})

describe('h6x N7：验收倒计时（只在页面里算，⛔ 不轮询后端）', () => {
  it('格式：剩 X 小时 Y 分 / 已超时 X 小时 Y 分 / 没有值「—」', () => {
    expect(formatVerifyCountdown(3 * 3600 + 20 * 60)).toBe('剩 3 小时 20 分')
    expect(formatVerifyCountdown(59 * 60)).toBe('剩 0 小时 59 分')
    expect(formatVerifyCountdown(0)).toBe('已超时 0 小时 0 分')
    expect(formatVerifyCountdown(-(2 * 3600 + 5 * 60))).toBe('已超时 2 小时 5 分')
    expect(formatVerifyCountdown(null)).toBe('—')
  })

  it('剩余秒数：优先用绝对截止时间，没有截止时间才用后端快照 + 已过时间', () => {
    const now = Date.parse('2026-10-28T12:00:00+08:00')
    // 绝对截止时间 15:00 ⇒ 还剩 3 小时
    expect(verifyRemainingSeconds('2026-10-28T15:00:00+08:00', null, null, now)).toBe(3 * 3600)
    // 只有快照：抓取时刻 11:00、当时剩 3 小时 ⇒ 现在 12:00 ⇒ 剩 2 小时
    expect(
      verifyRemainingSeconds(
        null,
        3 * 3600,
        String(Date.parse('2026-10-28T11:00:00+08:00')),
        now,
      ),
    ).toBe(2 * 3600)
    // 都没有 ⇒ null（画「—」）
    expect(verifyRemainingSeconds(null, null, null, now)).toBeNull()
  })

  it('tick：按「现在」重算并就地改文字（过期转红、回来时去掉红）', () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    root.innerHTML =
      `<span class="${VERIFY_COUNTDOWN_CLASS}" data-verify-deadline="2026-10-28T15:00:00+08:00">占位</span>`
    const el = root.querySelector<HTMLElement>(`.${VERIFY_COUNTDOWN_CLASS}`)!
    tickVerifyCountdowns(root, Date.parse('2026-10-28T12:00:00+08:00'))
    expect(el.textContent).toBe('剩 3 小时 0 分')
    expect(el.style.color).toBe('')
    tickVerifyCountdowns(root, Date.parse('2026-10-28T16:30:00+08:00'))
    expect(el.textContent).toBe('已超时 1 小时 30 分')
    // jsdom 会把颜色归一成 `rgb(...)` ⇒ 两处都比归一后的值（同一份判据）。
    const normalize = (css: string): string => {
      const probe = document.createElement('span')
      probe.style.color = css
      return probe.style.color
    }
    expect(el.style.color).toBe(normalize(ROW_DANGER_COLOR))
    // 数据被重取 / 时间回到未过期 ⇒ 红色去掉（⛔ 不留假红）。
    tickVerifyCountdowns(root, Date.parse('2026-10-28T12:00:00+08:00'))
    expect(el.style.color).toBe('')
  })

  it('挂载后按 1 秒起一个本地定时器：只改文本、**零请求**；卸载时清掉', async () => {
    const setIntervalSpy = vi.spyOn(window, 'setInterval')
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval')
    try {
      await mountPage()
      const index = setIntervalSpy.mock.calls.findIndex((call) => call[1] === VERIFY_TICK_MS)
      expect(index).toBeGreaterThanOrEqual(0)
      const timerId = setIntervalSpy.mock.results[index]?.value
      // 手动跑一次定时器的回调：⛔ 不轮询后端（零请求）。
      requestMock.mockClear()
      ;(setIntervalSpy.mock.calls[index]![0] as () => void)()
      expect(requestMock).not.toHaveBeenCalled()
      // 卸载 ⇒ 定时器必须被清掉（否则页面切走后还在跑）。
      unmountLast()
      expect(clearIntervalSpy).toHaveBeenCalledWith(timerId)
    } finally {
      setIntervalSpy.mockRestore()
      clearIntervalSpy.mockRestore()
    }
  })
})


/* ── 订单来源直接显示 / 剩余时间用新键 ──── */

describe('h6x N8：订单来源**直接显示后端给的字符串**（后端保证是中文名）', () => {
  it('非空就照显（**纯数字串也照显**）；空串 / null / 缺键才「—」', () => {
    expect(orderSourceText('代练通')).toBe('代练通')
    // ⚠️ 后端已保证只下发名字（快照 → 字典名 → 空串）⇒ 前端⛔ 不再「像数字就丢掉」。
    expect(orderSourceText('9')).toBe('9')
    expect(orderSourceText(9)).toBe('9')
    expect(orderSourceText(' 淘宝店铺 ')).toBe('淘宝店铺')
    expect(orderSourceText('')).toBe('—')
    expect(orderSourceText('   ')).toBe('—')
    expect(orderSourceText(null)).toBe('—')
    expect(orderSourceText(undefined)).toBe('—')
  })

  it('映射进「订单来源」列：有值显示名字、缺键「—」', () => {
    expect(toDisplayRow({ ...ROW, order_source: '小红书' })['t_soure']).toBe('小红书')
    expect(toDisplayRow({ ...ROW })['t_soure']).toBe('—')
  })
})

describe('h6x N8：剩余时间用后端新键 `remaining_seconds`（⛔ 不自己从时间戳算）', () => {
  it('三态：有值「剩 X 小时 Y 分」/ 超过 1 天「剩 N 天 X 小时」/ 0 已超时 / 空「—」', () => {
    expect(formatShoursRemaining(3 * 3600 + 20 * 60)).toBe('剩 3 小时 20 分')
    expect(formatShoursRemaining(59)).toBe('剩 0 小时 0 分')
    expect(formatShoursRemaining(90000)).toBe('剩 1 天 1 小时')
    // 边界：正好 24 小时**不算**「超过 1 天」⇒ 仍按小时显示（业务方原话是「超过 1 天」）。
    expect(formatShoursRemaining(86400)).toBe('剩 24 小时 0 分')
    expect(formatShoursRemaining(0)).toBe('已超时')
    expect(formatShoursRemaining(-5)).toBe('已超时')
    expect(formatShoursRemaining(null)).toBe('—')
    expect(formatShoursRemaining(undefined)).toBe('—')
  })

  it('t_shours 取 `remaining_seconds`；⛔ 只有时间戳（没有这个键）时**不自己算** ⇒「—」', () => {
    expect(toDisplayRow({ ...ROW, remaining_seconds: 5400 })['t_shours']).toBe('剩 1 小时 30 分')
    expect(toDisplayRow({ ...ROW, remaining_seconds: 0 })['t_shours']).toBe('已超时')
    // 关键判据：只给了 `created_at` / `last_synced_at` 也**不许**倒推剩余时间。
    expect(
      toDisplayRow({
        ...ROW,
        created_at: '2026-10-27T09:30:00+08:00',
        last_synced_at: '2026-10-27T10:00:00+08:00',
      })['t_shours'],
    ).toBe('—')
    expect(toDisplayRow({ ...ROW, remaining_seconds: null })['t_shours']).toBe('—')
  })

  it('父行取最新一笔的 `remaining_seconds`；子行各取自己那条', () => {
    const older = { ...PUSH_A, id: 1301, remaining_seconds: 600, created_at: '2026-10-27T09:00:00+08:00' }
    const newer = { ...PUSH_A, id: 1302, remaining_seconds: 7200, created_at: '2026-10-28T12:00:00+08:00' }
    const row = toGroupDisplayRow({ ...GROUP, pushes: [newer, older] })
    expect(row['t_shours']).toBe('剩 2 小时 0 分')
    expect(childCellHtml('t_shours', older)).toContain('剩 0 小时 10 分')
    expect(childCellHtml('t_shours', { ...older, remaining_seconds: null })).toBe('—')
  })

  it('剩余时间也在页面本地 tick（每秒递减，过期转「已超时」+ 红，零请求）', () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const at = Date.parse('2026-10-28T12:00:00+08:00')
    root.innerHTML = shoursCountdownHtml(7200, String(at), at)
    const el = root.querySelector<HTMLElement>(`.${VERIFY_COUNTDOWN_CLASS}`)!
    expect(el.textContent).toBe('剩 2 小时 0 分')
    tickVerifyCountdowns(root, at + 1000)
    expect(el.textContent).toBe('剩 1 小时 59 分')
    // 走到 0 之后：文案换成「已超时」并转红（判据用的是同一份 tick）。
    tickVerifyCountdowns(root, at + 7200 * 1000)
    expect(el.textContent).toBe('已超时')
    const normalize = (css: string): string => {
      const probe = document.createElement('span')
      probe.style.color = css
      return probe.style.color
    }
    expect(el.style.color).toBe(normalize(ROW_DANGER_COLOR))
  })

  it('「剩余时间」列挂的是倒计时 templet（不是纯文字）', async () => {
    await mountPage()
    const cols = (tableOptions()['cols'] as Array<Array<Record<string, unknown>>>)[0]!
    const shours = cols.find((col) => col['field'] === 't_shours')!
    expect(typeof shours['templet']).toBe('function')
    const html = (shours['templet'] as (r: CoreOrderDisplayRow) => string)({
      t_shours: '剩 1 小时 0 分',
      t_shoursBase: '3600',
      t_shoursAt: String(Date.now()),
    })
    expect(html).toContain(VERIFY_COUNTDOWN_CLASS)
  })
})

/* ── 行内按钮接真 + 「协商撤销」真提交 ────────────── */

/**
 * 行内写动作的端点（页面**不自己拼** URL，这里只给断言用 —— 所以按**单号**现拼，
 * 免得「换一单的用例」拿上一单的 URL 当期望值）。
 */
function pushUrlOf(orderNo: string, action: string): string {
  return `/api/business/orders/${orderNo}/platform-push/${action}/`
}

/** 一次写动作的回包（后端返回推送行；`state` 决定页面怎么说结果）。 */
function pushRowResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7,
    order: 1,
    platform: 10,
    serial_no: ROW.serial_no,
    state: 'closed',
    state_display: '已关闭',
    price: '5.00',
    ensure1: '',
    ensure2: '',
    time_limit_hours: 720,
    ex_order_no: '',
    remote_status: 16,
    remote_cancel_status: 12,
    accept_uid: '',
    last_synced_at: null,
    last_error: '',
    attempts: 1,
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

/** 列表回聚合一屏 + counts；`…/platform-push/*` 交给 `write`（返回结果或 Promise）。 */
function mockGroupedWrites(
  write: (url: string, data: Record<string, unknown>) => unknown,
  groups: PlatformPushGroupItem[] = [GROUP],
): void {
  requestMock.mockImplementation((config) => {
    const url = (config as { url?: string }).url ?? ''
    const data = ((config as { data?: Record<string, unknown> }).data ?? {}) as Record<
      string,
      unknown
    >
    if (url === PLATFORM_PUSH_COUNTS_URL) return Promise.resolve(COUNTS)
    if (url === '/api/business/platform-pushes/') return Promise.resolve(groupedPageOf(groups))
    return Promise.resolve(write(url, data))
  })
}

/** 行内写请求（按 URL 前缀过滤；列表 / counts 不算）。 */
function pushWrites(): Array<{ url: string; data: Record<string, unknown> }> {
  return requestMock.mock.calls
    .map((call) => call[0] as { url?: string; data?: Record<string, unknown> })
    .filter((config) => (config.url ?? '').includes('/platform-push/'))
    .map((config) => ({ url: config.url ?? '', data: config.data ?? {} }))
}


function lastPushWrite(): { url: string; data: Record<string, unknown> } {
  const calls = pushWrites()
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('没有发出推送写请求')
  return call
}

/** 最后一次 `layer.open` 的选项。 */
function lastLayerOpen(): Record<string, unknown> {
  const calls = LAYUI.layerOpen.mock.calls
  const call = calls[calls.length - 1] as unknown as [Record<string, unknown>]
  if (call === undefined) throw new Error('没有打开过弹层')
  return call[0]
}

/**
 * 把弹层正文塞进文档（真浏览器这一步由 layer 做；`layer.open` 是替身 ⇒ 测试自己插）。
 * 返回正文根节点，便于填值 / 勾选框。
 */
function mountLayerContent(): HTMLElement {
  const holder = document.createElement('div')
  holder.innerHTML = String(lastLayerOpen()['content'] ?? '')
  document.body.appendChild(holder)
  return holder
}

/** 点某个弹层的「确定 / 提交」（layui 的 `yes(index, layero)`）。 */
function lastLayerYes(index = 1, layero?: unknown): void {
  const yes = lastLayerOpen()['yes'] as ((i: number, l?: unknown) => void) | undefined
  if (yes === undefined) throw new Error('弹层没有 yes 回调')
  yes(index, layero)
}

/** 造一个假 `layero`（只带 layui 会用到的那个 `.layui-layer-btn0`）。 */
function fakeLayerBtn(): {
  layero: { find: () => Record<string, ReturnType<typeof vi.fn>> }
  node: Record<string, ReturnType<typeof vi.fn>>
} {
  const node = {
    addClass: vi.fn(),
    removeClass: vi.fn(),
    attr: vi.fn(),
    removeAttr: vi.fn(),
  }
  return { layero: { find: () => node }, node }
}

/** 点行内按钮（操作列是 `fixed:'right'` ⇒ 按钮在右侧固定表那一行里）。 */
function clickRowAction(tr: HTMLTableRowElement, key: string): void {
  const el = tr.querySelector<HTMLElement>(`[data-platform-action="${key}"]`)
  if (el === null) throw new Error(`行里没有动作 ${key}`)
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

/* 推送夹具：一条「最新且活跃的未接手」+ 一条「更早的已关闭」，用来验「取活跃那条」。 */
const PUSH_UNACCEPTED: PlatformPushSubRow = {
  platform: 10,
  serial_no: '19000000000000000401',
  state: 'sent',
  remote_status: 11,
  remote_cancel_status: 10,
  price: '8.00',
  id: 401,
  created_at: '2026-10-27T12:00:00+08:00',
  is_active: true,
  pay_level_bal: '12.00',
  rep_ensure_bal: '3.00',
}
const PUSH_OLD_CLOSED: PlatformPushSubRow = {
  platform: 10,
  serial_no: '19000000000000000402',
  state: 'closed',
  remote_status: 16,
  remote_cancel_status: 12,
  price: '4.00',
  id: 402,
  created_at: '2026-10-27T08:00:00+08:00',
}
const GROUP_MIXED: PlatformPushGroupItem = {
  ...GROUP,
  pushes: [PUSH_UNACCEPTED, PUSH_OLD_CLOSED],
}
/** 两条都「已接手」、最新那条是正在代练(12)（协商撤销的形态）。 */
const GROUP_ACCEPTED: PlatformPushGroupItem = {
  ...GROUP,
  pushes: [
    {
      ...PUSH_A,
      id: 501,
      created_at: '2026-10-27T12:00:00+08:00',
      is_active: true,
      pay_level_bal: '12.00',
      rep_ensure_bal: '3.00',
    },
    { ...PUSH_B, id: 502, created_at: '2026-10-27T09:00:00+08:00' },
  ],
}

describe('h6x R2：折叠父行的操作按钮（作用于「最新一笔推送」）', () => {
  it('groupStatusPush：按 `created_at` 降序、`id` 降序取**最新一条**（⛔ 不看 is_active）；空 ⇒ null', () => {
    const olderActive = { ...PUSH_UNACCEPTED, created_at: '2026-10-27T08:00:00+08:00' }
    const newerInactive = { ...PUSH_OLD_CLOSED, created_at: '2026-10-27T23:00:00+08:00' }
    // `is_active` **不参与**选条 —— 谁更新就是谁。
    expect(groupStatusPush([newerInactive, olderActive])).toBe(newerInactive)
    expect(groupStatusPush([olderActive, newerInactive])).toBe(newerInactive)
    // `created_at` 并列 ⇒ 用 `id` 决（大的那条更新）。
    const sameTimeSmall = {
      ...PUSH_A,
      id: 11,
      created_at: '2026-10-28T10:00:00+08:00',
      remote_status: 11,
    }
    const sameTimeBig = {
      ...PUSH_B,
      id: 22,
      created_at: '2026-10-28T10:00:00+08:00',
      remote_status: 17,
    }
    expect(groupStatusPush([sameTimeBig, sameTimeSmall])).toBe(sameTimeBig)
    expect(groupStatusPush([sameTimeSmall, sameTimeBig])).toBe(sameTimeBig)
    // 没有 `created_at` 但 `id` 有值 ⇒ 仍按 `id` 降序决（= 后端 `-created_at, -id` 的同序）。
    const idSmall = { ...PUSH_A, id: 5, created_at: undefined, remote_status: 11 }
    const idBig = { ...PUSH_B, id: 9, created_at: undefined, remote_status: 17 }
    expect(groupStatusPush([idBig, idSmall])).toBe(idBig)
    expect(groupStatusPush([idSmall, idBig])).toBe(idBig)
    // 时间和 id 都没有（最旧的回包）⇒ 保持后端给的顺序。
    const noStampA = { ...PUSH_A, id: undefined, created_at: undefined }
    const noStampB = { ...PUSH_B, id: undefined, created_at: undefined }
    expect(groupStatusPush([noStampA, noStampB])).toBe(noStampA)
    expect(groupStatusPush([])).toBeNull()
    // 别名 `latestActivePush` 必须与它同一条判据（别名不许漂移）。
    expect(latestActivePush([newerInactive, olderActive])).toBe(groupStatusPush([newerInactive, olderActive]))
  })

  it('三条推送（旧 sent 11 / 旧 closed / 新 failed）⇒ 父行显示「发送失败」（⛔ 不回退到更早的成功单）', () => {
    const olderSent: PlatformPushSubRow = {
      ...PUSH_A,
      id: 31,
      state: 'sent',
      remote_status: 11,
      remote_cancel_status: 10,
      created_at: '2026-10-27T09:00:00+08:00',
    }
    const olderClosed: PlatformPushSubRow = {
      ...PUSH_B,
      id: 32,
      state: 'closed',
      remote_status: 16,
      remote_cancel_status: 12,
      created_at: '2026-10-27T10:00:00+08:00',
    }
    const newestFailed: PlatformPushSubRow = {
      platform: 10,
      serial_no: null,
      state: 'failed',
      remote_status: null,
      remote_cancel_status: null,
      price: '9.00',
      id: 33,
      created_at: '2026-10-28T11:00:00+08:00',
      last_error: '代练通返回：余额不足',
    }
    const row = toGroupDisplayRow({
      ...GROUP,
      pushes: [newestFailed, olderClosed, olderSent],
    })
    expect(row['t_pltSta']).toBe('发送失败')
    // 有数据的列也取同一条（同一来源）：失败态没有平台单号 / 价格仍是这次发单的价格。
    expect(row['t_throw']).toBe('9.00')
    expect(row['t_hour']).toBe('2026-10-28 11:00')

    // 最新那条是「正在代练」⇒ 就显示「正在代练」。
    const newestOk: PlatformPushSubRow = {
      ...newestFailed,
      id: 34,
      state: 'sent',
      remote_status: 12,
      created_at: '2026-10-28T12:00:00+08:00',
    }
    expect(
      toGroupDisplayRow({ ...GROUP, pushes: [newestOk, olderClosed, olderSent] })['t_pltSta'],
    ).toBe('正在代练')

    // 最新那条是「结果未知」⇒ 就显示「状态未知」（⛔ 不拿更早的成功单冒充）。
    const newestUnknown: PlatformPushSubRow = {
      ...newestFailed,
      id: 35,
      state: 'unknown',
      created_at: '2026-10-28T13:00:00+08:00',
    }
    expect(
      toGroupDisplayRow({ ...GROUP, pushes: [newestUnknown, olderClosed, olderSent] })[
        't_pltSta'
      ],
    ).toBe('状态未知')
  })

  it('父行的发单平台 / 发单价 / 交易状态 / 发单时间取**最新一笔**推送（旧 closed + 新 未接手）', () => {
    const row = toGroupDisplayRow(GROUP_MIXED)
    expect(row['t_pushPltm']).toBe('代练通')
    expect(row['t_throw']).toBe('8.00')
    expect(row['t_pltSta']).toBe('未接手')
    expect(row['t_hour']).toBe('2026-10-27 12:00')
    expect(row['t_pushToken']).toBe('unaccepted')
    // 推送行没有这三个值 ⇒ 仍然「—」（⛔ 不编）。
    expect(row['t_safeeff']).toBe('—')
    expect(row['profit']).toBe('—')
    expect(row['t_shours']).toBe('—')
    expect(row['t_pushKey']).toBe(ROW_TARGET_LATEST)
  })

  it('父行按钮带定位（data-push="latest"）；活跃那条未接手 ⇒ 撤回只需确认，⛔ 不发协商撤销', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_MIXED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    // 父行只画「编辑 + 撤回」（未接手）。
    expect(
      fixedTr.querySelectorAll('[data-platform-action]'),
    ).toHaveLength(2)
    requestMock.mockClear()
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    // 未接手 ⇒ 先一个确认框（写清后果），⛔ 不是三格弹窗。
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(lastConfirm().text).toBe(ROW_WITHDRAW_CONFIRM_TEXT)
    expect(lastConfirm().text).toContain('删除')
    expect(lastConfirm().text).toContain('退款')
    expect(LAYUI.layerOpen).not.toHaveBeenCalled()
    expect(pushWrites()).toHaveLength(0)

    lastConfirm().yes(1)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().url).toBe(pushUrlOf(ROW.order_no, 'withdraw'))
    // 逐平台撤回带 `platform`（后端按它选平台客户端）。
    expect(lastPushWrite().data).toEqual({ platform: 10 })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
  })

  it('活跃那条已接手(12) ⇒ 父行只画「协商撤销 + 加款」（⛔ 撤回 / 编辑不出现）', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    const keys = Array.from(
      fixedTr.querySelectorAll<HTMLElement>('[data-platform-action]'),
    ).map((el) => el.dataset.platformAction)
    expect(keys).toEqual([ROW_ACTION_NEGOTIATE, ROW_ACTION_AMOUNT])
    requestMock.mockClear()
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(LAYUI.layerOpen).toHaveBeenCalledTimes(1)
    expect(String(lastLayerOpen()['title'])).toBe(ROW_NEGOTIATE_TITLE)
    expect(pushWrites()).toHaveLength(0)
  })

  it('展开后的**子行**按钮作用于它自己那条推送（⛔ 不是父行那条）', async () => {
    // GROUP_ACCEPTED：第 0 条有 `pay_level_bal=12.00`，第 1 条没有。
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { mainTr } = mountTableRows(root, renderedRows()[0]!)
    mainTr
      .querySelector<HTMLElement>(ORDER_TREE_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const fixedBody = mainTr
      .closest('.layui-table-view')!
      .querySelector<HTMLElement>('.layui-table-fixed-r tbody')!
    const fixedChildren = Array.from(
      fixedBody.querySelectorAll<HTMLTableRowElement>(ORDER_CHILD_ROW_SELECTOR),
    )
    expect(fixedChildren).toHaveLength(2)

    // 第 1 条（没有现值）⇒ 弹层两格是默认 `0`（不是第 0 条那个 12.00）。
    fixedChildren[1]!
      .querySelector<HTMLElement>(`[data-platform-action="${ROW_ACTION_NEGOTIATE}"]`)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const emptyHtml = String(lastLayerOpen()['content'])
    expect(emptyHtml).not.toContain('value="12.00"')
    expect(emptyHtml).toContain(`value="${ROW_NEGOTIATE_DEFAULT_BALANCE}"`)
    ;(lastLayerOpen()['btn2'] as () => void)()

    // 第 0 条（有现值）⇒ 弹层默认值 = 它自己的 12.00。
    fixedChildren[0]!
      .querySelector<HTMLElement>(`[data-platform-action="${ROW_ACTION_NEGOTIATE}"]`)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(String(lastLayerOpen()['content'])).toContain('value="12.00"')
  })

  it('翻页后**目标表复位**：上一屏残留的按钮点下去明确报错、零写请求（⛔ 不拿旧数据去写）', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_MIXED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    // 翻到第 2 页：新一屏换成**另一单**（旧单不在这一屏）⇒ 旧单的目标被清掉。
    // ⚠️ 替身必须在 `jump` **之前**换（`jump` 同步就发出取数请求）。
    const otherGroup: PlatformPushGroupItem = {
      ...GROUP_ACCEPTED,
      order_no: '20261027000000000077',
      source_order_no: 'TB-2026-0077',
    }
    mockGroupedWrites(() => pushRowResult(), [otherGroup])
    const jump = pagerOptions()['jump'] as (obj: { curr?: number }) => void
    jump({ curr: 2 })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(2))
    await vi.waitFor(() =>
      expect(renderedRows()[0]!['t_orderid']).toBe(otherGroup.order_no),
    )
    requestMock.mockClear()
    // 上一屏那一行还留在测试 DOM 里（真浏览器会被 layui 重画掉）。
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_TARGET_MISSING_MESSAGE, { time: 6000 })
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(pushWrites()).toHaveLength(0)
  })

  it('连点「撤回」只发一次（在途闸 + 提交中禁用）', async () => {
    let release: (value: unknown) => void = () => undefined
    mockGroupedWrites(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
      [GROUP_MIXED],
    )
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    requestMock.mockClear()
    lastConfirm().yes(1)
    // 在途时再点（真浏览器里按钮已被禁用；这里再点一次走同一条闸）。
    clickRowAction(fixedTr, ROW_ACTION_WITHDRAW)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    release(pushRowResult())
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
  })
})

describe('h6x R3：协商撤销三格弹窗必须真能提交', () => {
  it('弹层三格 + 自动客服介入勾选框（默认不勾）；两格默认值来自推送行', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const options = lastLayerOpen()
    const html = String(options['content'])
    expect(html).toContain('支付代练费')
    expect(html).toContain('赔保证金')
    expect(html).toContain('撤销理由')
    expect(html).toContain(`id="${ROW_NEGOTIATE_PAY_ID}"`)
    expect(html).toContain(`id="${ROW_NEGOTIATE_ENSURE_ID}"`)
    expect(html).toContain(`id="${ROW_NEGOTIATE_COMMENT_ID}"`)
    // 默认值 = 该推送行的 `pay_level_bal` / `rep_ensure_bal`（**现值优先于默认 0**）。
    expect(html).toContain('value="12.00"')
    expect(html).toContain('value="3.00"')
    expect(html).not.toContain(`value="${ROW_NEGOTIATE_DEFAULT_BALANCE}"`)
    // 勾选框默认**不勾**（业务方 2026-10-28 定案）。
    expect(html).toContain(`id="${ROW_NEGOTIATE_AUTO_ID}"`)
    expect(html).not.toContain('checked')
    const holder = mountLayerContent()
    const box = holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_AUTO_ID}`)!
    expect(box.checked).toBe(false)
  })

  it('推送行没有现值 ⇒ 两格**默认填 0**（业务方 2026-10-28：不再留空），且能直接提交', async () => {
    const noDefaults: PlatformPushGroupItem = {
      ...GROUP,
      pushes: [
        { ...PUSH_A, id: 601, created_at: '2026-10-27T12:00:00+08:00', is_active: true },
        { ...PUSH_B, id: 602, created_at: '2026-10-27T09:00:00+08:00' },
      ],
    }
    mockGroupedWrites(() => pushRowResult({ state: 'sent', remote_cancel_status: 11 }), [
      noDefaults,
    ])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const html = String(lastLayerOpen()['content'])
    expect(html).toContain(`id="${ROW_NEGOTIATE_PAY_ID}"`)
    const holder = mountLayerContent()
    // 两个框都显示 `0`（不是空串、也不是 placeholder）。
    expect(holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_PAY_ID}`)!.value).toBe(
      ROW_NEGOTIATE_DEFAULT_BALANCE,
    )
    expect(holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_ENSURE_ID}`)!.value).toBe(
      ROW_NEGOTIATE_DEFAULT_BALANCE,
    )
    expect(ROW_NEGOTIATE_DEFAULT_BALANCE).toBe('0')
    // **直接提交**（不改任何值）能过校验，载荷里就是 0。
    requestMock.mockClear()
    lastLayerYes()
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().data).toEqual({
      pay_level_bal: '0',
      rep_ensure_bal: '0',
      comment: '',
      auto_arbitrate: false,
      platform: 10,
    })
  })

  it('后端给了空串也算「没有现值」⇒ 仍然默认填 0', async () => {
    const blank: PlatformPushGroupItem = {
      ...GROUP,
      pushes: [
        {
          ...PUSH_A,
          id: 701,
          created_at: '2026-10-27T12:00:00+08:00',
          is_active: true,
          pay_level_bal: '',
          rep_ensure_bal: '',
        },
        { ...PUSH_B, id: 702, created_at: '2026-10-27T09:00:00+08:00' },
      ],
    }
    mockGroupedWrites(() => pushRowResult(), [blank])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const holder = mountLayerContent()
    expect(holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_PAY_ID}`)!.value).toBe(
      ROW_NEGOTIATE_DEFAULT_BALANCE,
    )
    expect(holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_ENSURE_ID}`)!.value).toBe(
      ROW_NEGOTIATE_DEFAULT_BALANCE,
    )
  })

  it('提交载荷**恰好** 5 个键（三格 + auto_arbitrate=false + platform）', async () => {
    mockGroupedWrites(() => pushRowResult({ state: 'sent', remote_cancel_status: 11 }), [
      GROUP_ACCEPTED,
    ])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const holder = mountLayerContent()
    holder.querySelector<HTMLTextAreaElement>(`#${ROW_NEGOTIATE_COMMENT_ID}`)!.value =
      '客户要求撤销'
    requestMock.mockClear()
    lastLayerYes()
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().url).toBe(pushUrlOf(ROW.order_no, 'withdraw'))
    expect(lastPushWrite().data).toEqual({
      pay_level_bal: '12.00',
      rep_ensure_bal: '3.00',
      comment: '客户要求撤销',
      auto_arbitrate: false,
      platform: 10,
    })
  })

  it('勾上自动客服介入 ⇒ auto_arbitrate=true（⛔ 不是省略这个键）', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const holder = mountLayerContent()
    holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_AUTO_ID}`)!.checked = true
    requestMock.mockClear()
    lastLayerYes()
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().data['auto_arbitrate']).toBe(true)
  })

  it('成功 ⇒ 关弹层 + 刷新列表 + 一行结果提示（按返回行判：只提交了协商）', async () => {
    mockGroupedWrites(() => pushRowResult({ state: 'sent', remote_cancel_status: 11 }), [
      GROUP_ACCEPTED,
    ])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    mountLayerContent()
    requestMock.mockClear()
    lastLayerYes(3)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
    expect(LAYUI.layerClose).toHaveBeenCalledWith(3)
    expect(LAYUI.msg).toHaveBeenLastCalledWith('已提交协商撤销，等打手/客服处理', {
      time: 1500,
    })
  })

  it('失败：**不关弹层**、原样显示后端中文、可再提交一次', async () => {
    let attempt = 0
    mockGroupedWrites(() => {
      attempt += 1
      if (attempt === 1) return Promise.reject(new BizError(1002, '协商撤销 1 小时内只能申请一次'))
      return pushRowResult()
    }, [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    mountLayerContent()
    requestMock.mockClear()
    lastLayerYes(5)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    // 弹层没被关掉 ⇒ 操作人可以改完再提交。
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenLastCalledWith('协商撤销 1 小时内只能申请一次', {
        time: 6000,
      }),
    )
    expect(LAYUI.layerClose).not.toHaveBeenCalled()
    // 弹层**还占着**：再点行内那颗按钮不会再开第二层（`dialogOpen` 不许在失败时被复位）。
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    expect(LAYUI.layerOpen).toHaveBeenCalledTimes(1)
    // 再提交一次真的又发了一发（弹层还活着）。
    lastLayerYes(5)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(2))
  })

  it('两个金额格式不对 ⇒ 提示 + 零请求、弹层不关', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    const holder = mountLayerContent()
    const pay = holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_PAY_ID}`)!
    requestMock.mockClear()

    pay.value = ''
    lastLayerYes()
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_NEGOTIATE_PAY_INVALID, { time: 6000 })
    pay.value = '12.00'
    holder.querySelector<HTMLInputElement>(`#${ROW_NEGOTIATE_ENSURE_ID}`)!.value = 'abc'
    lastLayerYes()
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_NEGOTIATE_ENSURE_INVALID, { time: 6000 })
    expect(pushWrites()).toHaveLength(0)
    expect(LAYUI.layerClose).not.toHaveBeenCalled()
  })

  it('提交中禁用**弹层自己的**提交按钮（.layui-layer-btn0），失败后复位可再点', async () => {
    let release: (value: unknown) => void = () => undefined
    mockGroupedWrites(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
      [GROUP_ACCEPTED],
    )
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    mountLayerContent()
    const { layero, node } = fakeLayerBtn()
    lastLayerYes(1, layero)
    expect(node.addClass).toHaveBeenCalledWith('layui-btn-disabled')
    expect(node.attr).toHaveBeenCalledWith('disabled', 'disabled')
    release(pushRowResult())
    await vi.waitFor(() =>
      expect(node.removeClass).toHaveBeenCalledWith('layui-btn-disabled'),
    )
  })

  it('连点「提交申请」只发一次', async () => {
    let release: (value: unknown) => void = () => undefined
    mockGroupedWrites(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
      [GROUP_ACCEPTED],
    )
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_NEGOTIATE)
    mountLayerContent()
    requestMock.mockClear()
    lastLayerYes()
    lastLayerYes()
    lastLayerYes()
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    release(pushRowResult())
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
  })
})

describe('h6x R2：编辑 / 加款（状态闸 + 只送改动项）', () => {
  it('编辑：只在「未接手(11)」出现（后端 edit_push_order 写前只放行 Status=11）', () => {
    // 读码结论，不是「全都放行」。
    expect(ROW_ACTION_STATUSES[ROW_ACTION_EDIT]).toEqual(['unaccepted'])
    for (const token of ['in_progress', 'waiting_review', 'abnormal', 'locked']) {
      expect(rowActionKeysFor(token)).not.toContain(ROW_ACTION_EDIT)
    }
  })

  it('编辑：表单弹层只送**改动项**（留空的键不送）+ 成功后关层刷新', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ONE])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_EDIT)
    const html = String(lastLayerOpen()['content'])
    expect(html).toContain(`id="${ROW_EDIT_TITLE_ID}"`)
    expect(html).toContain(`id="${ROW_EDIT_MEMO_ID}"`)
    const holder = mountLayerContent()
    holder.querySelector<HTMLInputElement>(`#${ROW_EDIT_TITLE_ID}`)!.value = '新标题'
    requestMock.mockClear()
    lastLayerYes()
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().url).toBe(pushUrlOf(GROUP_ONE.order_no, 'edit'))
    // 载荷**恰好**一个键：只送用户填了的标题。
    expect(lastPushWrite().data).toEqual({ title: '新标题' })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
    expect(LAYUI.layerClose).toHaveBeenCalled()
  })

  it('编辑：一格都没填 ⇒ 提示 + 零请求、弹层不关', async () => {
    mockGroupedWrites(() => pushRowResult(), [GROUP_ONE])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_EDIT)
    mountLayerContent()
    requestMock.mockClear()
    lastLayerYes()
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_EDIT_EMPTY_MESSAGE, { time: 6000 })
    expect(pushWrites()).toHaveLength(0)
    expect(LAYUI.layerClose).not.toHaveBeenCalled()
  })

  it('编辑失败 ⇒ 不关弹层 + 后端中文原文', async () => {
    mockGroupedWrites(
      () => Promise.reject(new BizError(1002, '只有「未接手」的代练通订单才能编辑')),
      [GROUP_ONE],
    )
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_EDIT)
    const holder = mountLayerContent()
    holder.querySelector<HTMLInputElement>(`#${ROW_EDIT_TITLE_ID}`)!.value = '新标题'
    requestMock.mockClear()
    lastLayerYes()
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenLastCalledWith('只有「未接手」的代练通订单才能编辑', {
        time: 6000,
      }),
    )
    expect(LAYUI.layerClose).not.toHaveBeenCalled()
  })

  it('加款：只在 12 / 13 出现（后端 AMOUNT_STATUSES）', () => {
    expect(ROW_ACTION_STATUSES[ROW_ACTION_AMOUNT]).toEqual(['in_progress', 'waiting_review'])
    for (const token of ['unaccepted', 'abnormal', 'locked', 'settled', 'closed']) {
      expect(rowActionKeysFor(token)).not.toContain(ROW_ACTION_AMOUNT)
    }
  })

  it('加款：先输入（整数元 ≥1）→ 再确认 → POST 只有 amount → 成功刷新', async () => {
    mockGroupedWrites(() => pushRowResult({ state: 'sent', price: '6.00' }), [GROUP_ACCEPTED])
    const root = await mountPage()
    const { fixedTr } = mountTableRows(root, renderedRows()[0]!)
    clickRowAction(fixedTr, ROW_ACTION_AMOUNT)
    expect(String(lastPrompt().options.title)).toContain('加款')
    requestMock.mockClear()

    lastPrompt().yes('1.5', 1)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_AMOUNT_INVALID_MESSAGE, { time: 6000 })
    lastPrompt().yes('0', 1)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(ROW_AMOUNT_INVALID_MESSAGE, { time: 6000 })
    expect(pushWrites()).toHaveLength(0)
    expect(LAYUI.confirm).not.toHaveBeenCalled()

    clickRowAction(fixedTr, ROW_ACTION_AMOUNT)
    lastPrompt().yes('5', 1)
    expect(lastConfirm().text).toContain('5')
    lastConfirm().yes(1)
    await vi.waitFor(() => expect(pushWrites()).toHaveLength(1))
    expect(lastPushWrite().url).toBe(pushUrlOf(GROUP_ACCEPTED.order_no, 'amount'))
    expect(lastPushWrite().data).toEqual({ amount: '5' })
    await vi.waitFor(() => expect(listRequests()).toHaveLength(1))
  })
})

/* 本页的**平台写动作**渲染挂 `order.platform_write`（fail-closed）：平台写按钮没权限就不画。
 * 后端另一条线才加这个节点 ⇒ 今天走**降级档**（按 `order.list` 判、身份取不到时照画），
 * 判据表见 `@/layui-island/perms::canPlatformWrite`。
 * ⚠️ 本组直接驱动**两个分支**：把 `setPlatformWriteNodeShipped(true)` 打开等于模拟「后端已上线」。
 */
describe('⭐ 2026-10-28：平台写动作按 `order.platform_write` 渲染（fail-closed）', () => {
  /**
   * 权限探针走**裸 `fetch`**（理由见 `@/layui-island/perms::resolvePlatformWritePermission`：
   * 本文件的用例把 `request()` 当响应队列 / 甚至桩成永不 resolve）⇒ 这里只替换全局 `fetch`，
   * 证据件那一发照旧。
   */
  function stubFetchWithMe(user: Record<string, unknown> | null): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        const u = String(url)
        if (u.includes('order-coreordercenter.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
        }
        if (u.includes('/api/auth/me')) {
          if (user === null) throw new Error('网络异常')
          return {
            ok: true,
            status: 200,
            json: async () => ({ code: 0, data: { user } }),
          } as unknown as Response
        }
        throw new Error(`未预期的 fetch：${u}`)
      }),
    )
  }

  /** 后端已上线该节点时的一次挂载：`me.menu_permissions` 由用例给。 */
  async function mountWithNodes(menuPermissions: string[]): Promise<HTMLElement> {
    setPlatformWriteNodeShipped(true)
    stubFetchWithMe({ is_tenant_admin: false, is_account_admin: false, menu_permissions: menuPermissions })
    return mountPage()
  }

  it('没有该节点 ⇒ 行内 4 颗一颗都不画（操作列「—」），工具栏平台写按钮**真被摘掉**', async () => {
    const root = await mountWithNodes(['order.list'])
    expect(platformWriteAllowedNow()).toBe(false)

    // ① 行内：同一档状态本来该有「编辑 + 撤回」两颗，现在只画「—」。
    expect(
      actionRowTemplate({ orderNo: ROW.order_no, pushKey: ROW_TARGET_LATEST }, 'unaccepted'),
    ).toBe('—')
    // 反向哨兵：有该节点时这两颗照旧（证明上面那条不是因为状态档位不对而空）
    applyPlatformWritePermission({ menu_permissions: ['order.platform_write'] })
    expect(
      actionRowTemplate({ orderNo: ROW.order_no, pushKey: ROW_TARGET_LATEST }, 'unaccepted'),
    ).toContain(`data-platform-action="${ROW_ACTION_EDIT}"`)

    // ② 工具栏：按 layui 的真实落点把工具栏建出来，再跑 `table.render` 的 `done`（生产代码的时机）。
    applyPlatformWritePermission({ menu_permissions: ['order.list'] })
    const tool = document.createElement('div')
    tool.className = 'layui-table-tool'
    tool.innerHTML = root.querySelector<HTMLElement>('#toolbarDemo')!.textContent ?? ''
    root.appendChild(tool)
    ;(tableOptions()['done'] as () => void)()
    for (const filter of PLATFORM_WRITE_TOOLBAR_FILTERS) {
      // ⚠️ 断言消息只放一个字面量（`vitest/valid-expect` 限 1 参）：拼上 filter 便于定位。
      expect(tool.querySelector(`button[lay-filter="${filter}"]`), `不该还在：${filter}`).toBeNull()
    }
    // 本地写动作那三颗**不受牵连**（它们写我方库，不是「交易平台写动作」）
    for (const filter of ['changeStaBtn', 'refundOrderBtn', 'changeOrderPriceBtn']) {
      expect(
        tool.querySelector(`button[lay-filter="${filter}"]`),
        `应当还在：${filter}`,
      ).not.toBeNull()
    }
  })

  it('有该节点 ⇒ 行内按钮与工具栏照旧（与升级前逐字一致）', async () => {
    const root = await mountWithNodes(['order.list', 'order.platform_write'])
    expect(platformWriteAllowedNow()).toBe(true)
    const html = actionRowTemplate(
      { orderNo: ROW.order_no, pushKey: ROW_TARGET_LATEST },
      'unaccepted',
    )
    expect(html).toContain(`data-platform-action="${ROW_ACTION_EDIT}"`)
    expect(html).toContain(`data-platform-action="${ROW_ACTION_WITHDRAW}"`)

    const tool = document.createElement('div')
    tool.className = 'layui-table-tool'
    tool.innerHTML = root.querySelector<HTMLElement>('#toolbarDemo')!.textContent ?? ''
    root.appendChild(tool)
    ;(tableOptions()['done'] as () => void)()
    for (const filter of PLATFORM_WRITE_TOOLBAR_FILTERS) {
      expect(
        tool.querySelector(`button[lay-filter="${filter}"]`),
        `应当还在：${filter}`,
      ).not.toBeNull()
    }
  })

  it('身份取不到（/api/auth/me 探针失败）⇒ 上线后 fail-closed（不画）；降级档仍照画（升级前行为）', async () => {
    // 降级档：**根本不发这一发探针**（少一次往返），照画。
    const fetchSpy = vi.fn(async (url: unknown) => {
      const u = String(url)
      if (u.includes('order-coreordercenter.html')) {
        return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
      }
      throw new Error(`未预期的 fetch：${u}`)
    })
    vi.stubGlobal('fetch', fetchSpy)
    const rootA = await mountPage()
    expect(platformWriteAllowedNow()).toBe(true)
    expect(fetchSpy.mock.calls.some((c) => String(c[0]).includes('/api/auth/me'))).toBe(false)
    expect(
      actionRowTemplate({ orderNo: ROW.order_no, pushKey: ROW_TARGET_LATEST }, 'unaccepted'),
    ).not.toBe('—')
    unmountLast()
    rootA.remove()

    // 上线后：探针失败 ⇒ fail-closed
    setPlatformWriteNodeShipped(true)
    stubFetchWithMe(null)
    const rootB = await mountPage()
    expect(platformWriteAllowedNow()).toBe(false)
    expect(
      actionRowTemplate({ orderNo: ROW.order_no, pushKey: ROW_TARGET_LATEST }, 'unaccepted'),
    ).toBe('—')
    expect(rootB).not.toBeNull()
  })

  it('降级档（后端还没这个节点）⇒ 按 `order.list` 判：有 ⇒ 画；明确没有 ⇒ 不画', () => {
    // 纯函数判据（不依赖渲染）：降级档就是「旧行为 = 按 order.list」。
    setPlatformWriteNodeShipped(false)
    expect(canPlatformWrite(undefined)).toBe(true)
    expect(canPlatformWrite({ menu_permissions: ['order.list'] })).toBe(true)
    expect(canPlatformWrite({ menu_permissions: [] })).toBe(false)
    // 上线后：只认 order.platform_write（管理员仍短路）
    setPlatformWriteNodeShipped(true)
    expect(canPlatformWrite(undefined)).toBe(false)
    expect(canPlatformWrite({ menu_permissions: ['order.list'] })).toBe(false)
    expect(canPlatformWrite({ menu_permissions: ['order.platform_write'] })).toBe(true)
    expect(canPlatformWrite({ is_tenant_admin: true })).toBe(true)
  })
})
