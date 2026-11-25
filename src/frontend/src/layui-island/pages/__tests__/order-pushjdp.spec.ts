// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【发单到交易平台（2026）】island 页（`pages/order-pushjdp.ts`）的**真交互**页测试。
 *
 * 本文件钉两件事：
 *
 * A. **复刻部分没漂**：证据件整份渲染（27 个 `<label>` / 底部主按钮文案逐字）、已配置态三个
 *    `layui-hide` 开关、右栏平台表按对标 cols 渲染成**空表**、点 `<select>` 不被总闸吃掉。
 *
 * B. **T9d 提交接真**（`AGENTS.md` §5-G 的四条 island 陷阱）：
 *    1. **载荷恰好**：成功提交的 `data` 键集合 = `price` / `ensure1` / `ensure2` /
 *       `time_limit_hours` / `zone_server_id` / `power_level` / `limit_accept` +
 *       接口固定的 `platform=10`——多一个少一个都红；
 *    2. 三态真展示：`sent` 显示平台单号 + 关层提示；`failed` / `unknown` 的界面文案**来自后端**
 *       `last_error` 原文；前置闸 `BizError` 的 `msg` 也原样展示；⛔ 都不假装成功；
 *    3. 未选区服 ⇒ **零请求** + 中文提示；
 *    4. 双击 ⇒ 只发一次请求（真钱风险）；
 *    5. 区服搜索 **300ms 防抖**、选中后回显 `server_name`。
 *
 * 手法（照 `system-systemsetting.spec.ts` / `emp-salesmanstatistics.spec.ts`）：
 * - `vi.mock('@/layui/island')` 换掉真 layui（jsdom 里加载不了），`bootstrapIsland` /
 *   `initIslandElements` 的**先后顺序**用运行期 DOM 自证；
 * - `vi.mock('@/api/http')` 只换**网络出口**：`@/api/dailiantongPush` 跑真实现 ⇒
 *   「URL 与载荷键集合」这条判据才有意义（把 api 模块也替身化只会断言自己传了一个对象）；
 * - 证据件走**真实文件**（不另造一份标记）。
 *
 * 三条 island 陷阱（`AGENTS.md` §5-G）：① 类名 / 选择器各有常量；② 以 `(` 开头的语句前置 `;`；
 * ③ 先 `bootstrapIsland` 再塞标记，`initIslandElements` 必须在塞标记之后。
 * ④ 页里有定时器（区服搜索防抖）：`beforeEach` 里先 `vi.useFakeTimers()`，`afterEach` 收口。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const renderTable = vi.fn()
  const reloadData = vi.fn()
  const formRender = vi.fn()
  // 运行期顺序判据（陷阱 ③）：挂样式时不该有证据件；init 时证据件必须已经在。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  /** `layer.open` 的记录（F4 的模板编辑弹层要搬节点）。 */
  const opens: Array<Record<string, unknown>> = []
  /** `layer.confirm` 的文案。 */
  const confirms: string[] = []
  /** `layer.close` 收到的 index。 */
  const closes: number[] = []

  function layerContent(): HTMLElement {
    let host = document.querySelector<HTMLElement>('.layui-layer-content')
    if (!host) {
      host = document.createElement('div')
      host.className = 'layui-layer-content'
      document.body.appendChild(host)
    }
    return host
  }

  function closeLayer(index: number): void {
    closes.push(index)
    const config = opens[index - 1]
    const node = config?.content as HTMLElement | undefined
    if (node instanceof HTMLElement) {
      node.style.display = 'none'
      ;(config?.end as (() => void) | undefined)?.()
    }
    document.querySelector('.layui-layer-content')?.remove()
  }

  const formHandlers = new Map<string, (obj?: unknown) => unknown>()

  return {
    msg,
    renderTable,
    reloadData,
    formRender,
    formHandlers,
    seenByBootstrap,
    seenByInit,
    opens,
    confirms,
    closes,
    form: {
      render: formRender,
      on: vi.fn((event: string, cb: (obj?: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: { render: renderTable, reloadData },
    layer: {
      msg,
      open: vi.fn((options: Record<string, unknown>) => {
        opens.push(options)
        const node = options.content as HTMLElement | undefined
        if (node instanceof HTMLElement) {
          node.style.display = ''
          layerContent().appendChild(node)
        }
        return opens.length
      }),
      close: vi.fn((index: number) => closeLayer(index)),
      confirm: vi.fn((text: string, _options: unknown, onConfirm: (i: number) => void) => {
        confirms.push(text)
        onConfirm(confirms.length)
      }),
    },
    /** layui 的 jQuery 替身：只把节点交给 `layer.open`。 */
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => {
      // 选择器写字面值（本工厂在页面模块求值前就会跑，⛔ 不能引页面常量）。
      LAYUI.seenByBootstrap.push(document.querySelector('#orderID') !== null)
      return LAYUI
    }) as unknown as typeof actual.bootstrapIsland,
    // 本页没有页签；替换成 no-op 让「挂标记之后才调」这条秩序不被真实现干扰。
    initIslandElements: vi.fn(() => {
      LAYUI.seenByInit.push(document.querySelector('#orderID') !== null)
    }),
  }
})

/** 只换网络出口（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { BizError, HttpError, NetworkError } from '@/api/errors'
import { request } from '@/api/http'
import {
  PUSH_JDP_SENT_MESSAGE_TYPE,
  PUSH_JDP_SENT_TEXT,
} from '@/layui-island/platformPush'
import {
  PLATFORM_ZONES_URL,
  platformPushPreviewUrl,
  pushToPlatformUrl,
  type DailiantongPushRow,
  type PlatformPushPreview,
  type PlatformZone,
} from '@/api/dailiantongPush'
import {
  PLATFORM_INTEGRATION_SETTINGS_URL,
  PLATFORM_INTEGRATION_SUMMARY_URL,
  type PlatformIntegrationSummary,
  type PlatformPushSettings,
} from '@/api/platformIntegrations'
import {
  DAILIANTONG_ORDER_TEMPLATES_URL,
  type OrderTemplateList,
} from '@/api/dailiantongExtra'
import {
  ACCEPT_LIMIT_SELECT,
  ACCEPT_POWER_SELECT,
  ACCEPT_SAVE_SELECTOR,
  ARTIFACT_URL,
  balanceCell,
  CLOSE_HINT,
  DEFAULTS_SETTINGS_FAILED_PREFIX,
  DEFAULTS_WARNING_SELECTOR,
  DELAY_DIV_SELECTOR,
  DELAY_NOTE_SELECTOR,
  DELAY_UNSUPPORTED_MESSAGE,
  GROUPS_CHANNEL_FILTER,
  PUSH_CHANNEL_UNSUPPORTED_PREFIX,
  UG_DEFAULT_LABEL,
  UG_HISTORY_SELECTOR,
  UG_LABEL_SELECTOR,
  UG_PASSWORD_LABEL,
  UGDIV_SELECTOR,
  OFF_SELL_DAYS_MAX,
  FAILED_PREFIX,
  IN_FLIGHT_MESSAGE,
  isNotOpenTarget,
  LIVE_NOTE_TEXT,
  mountPushJdpPage,
  NEW_TEMPLATE_SELECTOR,
  NOTE_SELECTOR,
  NOT_OPEN_SELECTOR,
  ORDER_NO_INVALID_MESSAGE,
  PREVIEW_BLOCK_SELECTOR,
  PREVIEW_BLOCKED_FALLBACK,
  PREVIEW_FAILED_PREFIX,
  ORDER_NO_PARAM,
  PENDING_MESSAGE,
  unexpectedStateMessage,
  PLATFORM_COLS,
  PRICE_LOW_MESSAGE,
  PRICE_REQUIRED_MESSAGE,
  REQUEST_RESULT_UNKNOWN_MESSAGE,
  RESULT_SELECTOR,
  RESULT_STATE_ATTR,
  SAVE_SUCCESS_TEXT,
  SENT_MESSAGE,
  SUBMIT_SELECTOR,
  SUBMIT_TEXT,
  SUMMARY_NO_CONFIG_MESSAGE,
  SUMMARY_NOTE_SELECTOR,
  SUMMARY_UNAVAILABLE_PREFIX,
  summaryPanelState,
  summaryReason,
  TABLE_ELEM,
  TEMPLATE_CONTENT_NAME,
  TEMPLATE_LIST_DEFAULT_PREFIX,
  TEMPLATE_LIST_EMPTY_VALUE,
  TEMPLATE_LIST_FAILED_PREFIX,
  TEMPLATE_LIST_FILTER,
  TEMPLATE_LIST_SELECTOR,
  TEMPLATE_PANEL_SELECTOR,
  TEMPLATE_SAVE_SELECTOR,
  TITLE,
  UNKNOWN_STATE_MESSAGE,
  PREVIEW_WARNINGS_SELECTOR,
  PUSH_MEMO_EMPTY_TEXT,
  PUSH_MEMO_FAILED_TEXT,
  PUSH_MEMO_SELECTOR,
  PUSH_MEMO_TITLE,
  ZONE_NO_GAME_MATCH_MESSAGE,
  ZONE_LIST_FAILED_PREFIX,
  ZONE_REQUIRED_MESSAGE,
  ZONE_SELECT_FILTER,
  ZONE_SELECT_SELECTOR,
  ZONE_TIP_SELECTOR,
  readAcceptPayload,
  readOffSellDays,
  readTimeLimitHours,
} from '../order-pushjdp'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-pushjdp.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-pushjdp.html'),
  'utf8',
)

/** 20 位内部订单号（合成值，形状与 `Order.order_no` 一致）。 */
const ORDER_NO = '20261026042504499387'
const ORDER_SEARCH = `&${ORDER_NO_PARAM}=${ORDER_NO}`

/** 一条区服（合成值）。 */
const ZONE: PlatformZone = {
  zone_server_id: '100100010000000',
  server_name: '英雄联盟/电信/艾欧尼亚',
}

/**
 * 代练通侧订单要求模板两条（合成值；一条默认、一条非默认）。
 * 2026-10-30 修复：发单页「常用模板」下拉 `#tempList` 的数据源（M5 管理页同一份上游库）。
 */
const TEMPLATES: OrderTemplateList = {
  items: [
    { id: 60024, title: '代练要求甲', temp: '接单后联系客服获取账号', is_default: true },
    { id: 60025, title: '陪玩要求乙', temp: '时间不够可以加，接单后联系客服', is_default: false },
  ],
  record_count: 2,
}

/** 后端 `last_error` 原文（三条用例各用一句可区分的合成文案）。 */
const FAILED_REASON = '-4 订单不能低于2.00元（后端原文）'
const UNKNOWN_REASON = '请求超时，结果未知（后端原文）'
const GATE_REASON = '当前租户还没有启用代练通对接参数（后端原文）'

const mockedRequest = vi.mocked(request)

/** 一份完整的推送行（字面量完整 ⇒ 后端字段改名时这里会编译报错）。 */
function pushRow(overrides: Partial<DailiantongPushRow>): DailiantongPushRow {
  return {
    id: 1,
    order: 1,
    platform: 10,
    serial_no: null,
    state: 'sent',
    state_display: '已发单',
    price: '12.50',
    ensure1: '5.00',
    ensure2: '2.50',
    time_limit_hours: 26,
    ex_order_no: ORDER_NO,
    remote_status: 11,
    remote_cancel_status: null,
    accept_uid: '',
    last_synced_at: null,
    last_error: '',
    attempts: 1,
    created_at: '2026-10-26T12:00:00+08:00',
    updated_at: '2026-10-26T12:00:00+08:00',
    ...overrides,
  }
}

type RequestConfig = {
  method?: string
  url?: string
  params?: Record<string, unknown>
  data?: Record<string, unknown>
}

/* ───────── F1–F5 的页面级取数：按 URL 路由（一次挂载会并发 5–6 发请求）───────── */

/** 9 组全局设置 + 计划 §2.4 新增的三列（F3 / F4 的默认值）。 */
const SETTINGS: PlatformPushSettings = {
  quick_push_enabled: true,
  legacy_simple_mode: 0,
  check_game_region: 0,
  forbid_push_when_double_gold_zero: false,
  duplicate_push_check: 0,
  select_jidanpu_center_by_default: false,
  auto_zero_refund_on_conflict: true,
  block_push_completed_orders: 0,
  auto_send_group_qr_after_accept: 1,
  default_power_level: 1,
  default_limit_accept: null,
  require_template: '模板甲：禁止写账号信息',
}

/** F2 右面板的正常载荷（余额都是两位小数字符串）。 */
const SUMMARY: PlatformIntegrationSummary = {
  platform: 10,
  platform_label: '代练通',
  account: 'acct-a',
  configured: true,
  available_balance: '100.00',
  frozen_balance: '5.50',
  error: '',
}

/**
 * 发单预览（F1 / F5 的唯一默认值来源）：所有值都是后端「将要真正发送」的那份。
 * 刻意与证据件默认值不同，回填漏了哪一格一眼可见。
 */
const PREVIEW: PlatformPushPreview = {
  can_push: true,
  reason: '',
  game_id: 2,
  warnings: [],
  memo: '账号：系统按订单生成\n要求：按单号核对后再提交',
  price: '12.50',
  ensure1: '0.00',
  ensure2: '0.00',
  time_limit_hours: 720,
  zone_server_id: ZONE.zone_server_id,
  server_name: ZONE.server_name,
  zone_source: 'map',
  title: '英雄联盟排位赛',
  game_account: 'ttabc123',
  game_password: 'ttpass01',
  game_role: '待定角色',
  current_info: '其它:无',
  requirement: SETTINGS.require_template,
  contact_qq: '123456789',
  contact_phone: '13800138000',
  power_level: 1,
  limit_accept: null,
}

type RouteValue<T> = T | Promise<T> | Error | (() => T | Promise<T> | Error)

/** 每一次挂载都会读这些出口；测试按需替换其中的一项。 */
type Routes = {
  me: RouteValue<{ user: { menu_permissions: string[] } }>
  preview: RouteValue<PlatformPushPreview>
  settings: RouteValue<PlatformPushSettings>
  /** `PUT …/settings/` 的响应。 */
  sendSettings: RouteValue<PlatformPushSettings>
  summary: RouteValue<PlatformIntegrationSummary>
  /** 该游戏的全部区服（F5 的下拉数据源）。 */
  gameZones: RouteValue<PlatformZone[]>
  /** 全区服字典（B3 兜底：按游戏名匹配不到时列全部让人手选）。 */
  allZones: RouteValue<PlatformZone[]>
  /** 代练通侧订单要求模板（「常用模板」下拉的数据源，2026-10-30）。 */
  templates: RouteValue<OrderTemplateList>
  push: RouteValue<DailiantongPushRow>
}

let routes: Routes

function freshRoutes(): Routes {
  return {
    me: { user: { menu_permissions: ['order.send_to_grab_hall'] } },
    preview: { ...PREVIEW },
    settings: { ...SETTINGS },
    sendSettings: { ...SETTINGS },
    summary: { ...SUMMARY },
    gameZones: [ZONE],
    allZones: [ZONE],
    templates: { items: TEMPLATES.items.map((item) => ({ ...item })), record_count: 2 },
    push: pushRow({ state: 'sent', serial_no: 'DL20261026000001' }),
  }
}

function respond<T>(value: RouteValue<T>): Promise<T> {
  const resolved = typeof value === 'function' ? (value as () => T | Promise<T> | Error)() : value
  if (resolved instanceof Error) return Promise.reject(resolved)
  return Promise.resolve(resolved as T)
}

function installRequestRouter(): void {
  mockedRequest.mockImplementation(((config: RequestConfig) => {
    const url = String(config?.url ?? '')
    const method = String(config?.method ?? 'get').toLowerCase()
    if (url === '/api/auth/me') return respond(routes.me)
    if (url.includes('/platform-push/preview/')) return respond(routes.preview)
    if (url.includes('/push-to-platform/')) return respond(routes.push)
    if (url.includes('/platform-integrations/summary/')) return respond(routes.summary)
    if (url.includes('/platform-integrations/zones/')) {
      // B3：按游戏取（带 game_id）与「全区服兜底」（不带 game_id）是两发不同的请求。
      const params = (config.params ?? {}) as Record<string, unknown>
      return respond('game_id' in params ? routes.gameZones : routes.allZones)
    }
    if (url.includes('/platform-integrations/order-templates/')) {
      return respond(routes.templates)
    }
    if (url.includes('/platform-integrations/settings/')) {
      return respond(method === 'put' ? routes.sendSettings : routes.settings)
    }
    return Promise.reject(new Error(`未预期的请求：${method} ${url}`))
  }) as unknown as typeof request)
}

function settingsCalls(): Array<RequestConfig> {
  return mockedRequest.mock.calls
    .map(([config]) => config as RequestConfig)
    .filter((config) => String(config.url ?? '').includes('/platform-integrations/settings/'))
}


function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页；`search` 直接拼在 `?` 后面（如 `&order_no=2026…`）。 */
async function mountPage(
  search = '',
): Promise<{ root: HTMLElement; cleanup: () => void }> {
  window.history.replaceState({}, '', `/layui-island/page.html?page=order-pushjdp${search}`)
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountPushJdpPage(root, { onNavigate: vi.fn() })
  // 默认值装载是挂载后异步跑的（订单 / 设置 / 默认区服 / 摘要）⇒ 让它们先落定。
  await flushAsync()
  return { root, cleanup }
}

/** 推进 0ms 并冲掉已经 resolve 的 promise 链（假定时器下收口用）。 */
async function flushAsync(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0)
  for (let i = 0; i < 24; i += 1) await Promise.resolve()
}

function input(root: HTMLElement, name: string): HTMLInputElement {
  const el = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)
  if (!el) throw new Error(`证据件里应当有 input[name=${name}]`)
  return el
}

function select(root: HTMLElement, name: string): HTMLSelectElement {
  const el = root.querySelector<HTMLSelectElement>(`select[name="${name}"]`)
  if (!el) throw new Error(`证据件里应当有 select[name=${name}]`)
  return el
}

function fillSubmitFields(
  root: HTMLElement,
  values: { price?: string; safe?: string; eff?: string; day?: string; hours?: string } = {},
): void {
  input(root, 'throwFee').value = values.price ?? '12.50'
  input(root, 'safeAmount').value = values.safe ?? '5.00'
  input(root, 'effAmount').value = values.eff ?? '2.50'
  select(root, 'day').value = values.day ?? '1'
  select(root, 'hours').value = values.hours ?? '2'
}

/** F5：像用户一样在下拉里选一条（选项必须已经在列表里）。 */
async function chooseZone(root: HTMLElement, zone: PlatformZone = ZONE): Promise<void> {
  const select = root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)
  expect(select, '运行期应当把「游戏/平台」那一格改成下拉').not.toBeNull()
  const option = Array.from(select!.options).find((item) => item.value === zone.zone_server_id)
  expect(option, `下拉里应当有区服 ${zone.zone_server_id}`).not.toBeUndefined()
  select!.value = zone.zone_server_id
  // layui 的原生 select 被隐藏、只在 `form.on('select')` 里回值 ⇒ 模拟它的真实两跳。
  LAYUI.formHandlers.get(`select(${ZONE_SELECT_FILTER})`)?.({ value: zone.zone_server_id })
  await flushAsync()
}

function clickSubmit(root: HTMLElement): HTMLButtonElement {
  const btn = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)
  if (!btn) throw new Error('证据件里应当有底部主按钮')
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  return btn
}

function pushCalls(): Array<[RequestConfig]> {
  return mockedRequest.mock.calls.filter(([config]) =>
    String((config as RequestConfig).url ?? '').includes('/push-to-platform/'),
  ) as unknown as Array<[RequestConfig]>
}

/** F5 区服下拉的取数（`zones/?game_id=`）。 */
function gameZoneCalls(): RequestConfig[] {
  return mockedRequest.mock.calls
    .map(([config]) => config as RequestConfig)
    .filter((config) => String(config.url ?? '') === PLATFORM_ZONES_URL)
}

/** 发单预览的调用（F1 / F5）。 */
function previewCalls(): RequestConfig[] {
  return mockedRequest.mock.calls
    .map(([config]) => config as RequestConfig)
    .filter((config) => String(config.url ?? '') === platformPushPreviewUrl(ORDER_NO))
}

/** 右面板摘要的调用。 */
function summaryCalls(): RequestConfig[] {
  return mockedRequest.mock.calls
    .map(([config]) => config as RequestConfig)
    .filter((config) => config.url === PLATFORM_INTEGRATION_SUMMARY_URL)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  mockedRequest.mockReset()
  routes = freshRoutes()
  installRequestRouter()
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  LAYUI.formHandlers.clear()
  LAYUI.opens.length = 0
  LAYUI.confirms.length = 0
  LAYUI.closes.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes(ARTIFACT_URL)) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('发单页（order-pushjdp）：证据件渲染 + 已配置态', () => {
  it('页 title / 左侧 24 项表单 / 底部主按钮文案逐字', async () => {
    const { root, cleanup } = await mountPage()
    expect(document.title).toBe(TITLE)
    // 页面共 27 个 label：左侧 24 项表单 + 右栏「本系统订单内容」1 个 + 模板弹层 2 个（侦察报告 §3）。
    expect(root.querySelectorAll('label').length).toBe(27)
    for (const name of [
      'gameAcc',
      'gamePwd',
      'gameRole',
      'orderTitle',
      'day',
      'hours',
      'throwFee',
      'safeAmount',
      'effAmount',
      'throwFeeHome',
      'moblie',
      'moblie2',
      'qq',
      'offSellDays',
      'pushChannel',
      'ugtext',
      'label1',
      'limitAccept',
      'dlmmlimitAccept',
      'gaemTf',
      'requestContent',
      'remark',
    ]) {
      expect(root.querySelector(`[name="${name}"]`), `证据件里应当有 name=${name}`).not.toBeNull()
    }
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)
    expect(submit?.textContent?.trim()).toBe(SUBMIT_TEXT)
    cleanup()
  })

  it('**已配置态**：提示条显示 / 未配置提示卡隐藏 / 表单块显示（三个 layui-hide 开关）', async () => {
    const { root, cleanup } = await mountPage()
    const quote = root.querySelector('blockquote.layui-elem-quote')
    expect(quote).not.toBeNull()
    expect(quote!.classList.contains('layui-hide')).toBe(false)
    const card = root.querySelector('.layui-card')
    expect(card, '「本系统还未配置交易平台对接参数」提示卡应当还在标记里').not.toBeNull()
    expect(card!.classList.contains('layui-hide')).toBe(true)
    const row = root.querySelector('.layui-row')
    expect(row).not.toBeNull()
    expect(row!.classList.contains('layui-hide')).toBe(false)
    cleanup()
  })

  it('PII 已清空：真实单号 / 账号 / 联系方式的输入值是空串（证据件契约）', async () => {
    // 证据件里 `requestContent` 是空的；本用例要钉的是「证据件不带 PII」，
    // 所以把全局模板也设成空串（否则 F1 会按配置把模板填进去）。
    routes.settings = { ...SETTINGS, require_template: '' }
    const { root, cleanup } = await mountPage()
    const orderId = root.querySelector<HTMLInputElement>('#orderID')
    expect(orderId?.value).toBe('')
    for (const selector of ['input[name="orderTitle"]', 'input[name="moblie2"]', 'input[name="qq"]']) {
      expect(root.querySelector<HTMLInputElement>(selector)?.value).toBe('')
    }
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')?.value).toBe('')
    cleanup()
  })

  it('`order_no` 查询参数是 20 位 ⇒ 回填隐藏域 `#orderID`；形状不对 ⇒ 不回填', async () => {
    const ok = await mountPage(ORDER_SEARCH)
    expect(ok.root.querySelector<HTMLInputElement>('#orderID')?.value).toBe(ORDER_NO)
    ok.cleanup()

    const bad = await mountPage(`&${ORDER_NO_PARAM}=<img src=x>`)
    expect(bad.root.querySelector<HTMLInputElement>('#orderID')?.value).toBe('')
    bad.cleanup()
  })

  it('按钮上方的如实说明 + 「先挂样式、塞标记、再 init」的顺序自证', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const note = root.querySelector<HTMLElement>(NOTE_SELECTOR)
    expect(note, '按钮上方应当有那句如实说明').not.toBeNull()
    expect(note!.textContent).toBe(LIVE_NOTE_TEXT)
    // 说明必须**点名**列出不提交的字段（⛔ 「其余由系统自动生成」会把可填但不发的格子藏起来）：
    // 至少要有「同发内部抢单价」——那一格用户能填，后端却不接收。
    expect(note!.textContent).toContain('同发内部抢单价')
    expect(note!.textContent).toContain('游戏账号')
    // ⭐ P9：「当前订单说明」现在**会**提交（发到平台的代练内容）⇒ 说明里必须点名它，
    //    ⛔ 不能还把「备注」笼统写成不提交。判据 = 说明与提交载荷两边说的是同一件事。
    expect(note!.textContent).toContain('当前订单说明')
    expect(note!.textContent).not.toBe('本版只提交 价格 / 保证金 / 时限 / 区服，其余由系统按订单自动生成')
    const submit = root.querySelector<HTMLElement>(SUBMIT_SELECTOR)!
    // `DOCUMENT_POSITION_FOLLOWING` = 按钮在说明之后（即说明在按钮上方）。
    expect(note!.compareDocumentPosition(submit) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    // 陷阱 ③：bootstrap 时证据件还没塞；init 时已经塞好。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    cleanup()
  })
})

describe('发单页（order-pushjdp）：右栏平台摘要表（F2 四列）', () => {
  it('`table.render` 收到 elem / id / 分页参数；列定义按 F2 = 平台 / 账号 / 可用余额 / 冻结余额', async () => {
    const { cleanup } = await mountPage(ORDER_SEARCH)
    expect(LAYUI.renderTable).toHaveBeenCalledTimes(1)
    const opts = LAYUI.renderTable.mock.calls[0]![0] as {
      elem: string
      id: string
      data: unknown[]
      maxHeight: number
      page: boolean
      limit: number
      cols: Array<Array<{ title?: string; field?: string; width?: number; hide?: boolean; type?: string }>>
    }
    expect(opts.elem).toBe(TABLE_ELEM)
    expect(opts.id).toBe('tableList')
    expect(opts.data).toEqual([])
    expect(opts.maxHeight).toBe(390)
    expect(opts.page).toBe(false)
    expect(opts.limit).toBe(100)
    const cols = opts.cols[0]!
    expect(cols.map((c) => c.title)).toEqual(['发往平台', '平台发单账号', '可用余额', '冻结余额'])
    expect(cols.map((c) => c.field)).toEqual([
      'platformName',
      'platformAcc',
      'availableBalance',
      'frozenBalance',
    ])
    // 判据对外也一致（导出的常量就是交给 layui 的那一份）。
    expect(PLATFORM_COLS).toHaveLength(4)
    cleanup()
  })

  it('余额格式化：有值补两位小数；null / 空串显示 `—`（⛔ 不伪造 0.00）', () => {
    expect(balanceCell('5')).toBe('5.00')
    expect(balanceCell('5.50')).toBe('5.50')
    expect(balanceCell(null)).toBe('—')
    expect(balanceCell('')).toBe('—')
    expect(balanceCell(undefined)).toBe('—')
  })
})

describe('发单页（order-pushjdp）：其余控件仍是「尚未开放」（主按钮 / 门槛保存 / 模板入口除外）', () => {
  it('「新建模板」不再是「尚未开放」：它打开 F4 的单模板编辑弹层', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const btn = root.querySelector<HTMLElement>(NEW_TEMPLATE_SELECTOR)
    expect(btn, '证据件里应当有「新建模板」那颗按钮').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.opens).toHaveLength(1)
    expect(String(LAYUI.opens[0]!.title)).toContain('模板')
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('页面上**除已接真的那几颗外**每一颗可见可点控件点一遍都出「尚未开放」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    /**
     * 可见性：jsdom 没有布局，`offsetParent` 恒 null ⇒ 按「祖先链上有 `.layui-hide` 或内联
     * `display:none`」判。⛔ 不把隐藏块里的控件也算进来（隐藏的「获取代练通参考价」/「保存模板」
     * 本来就不该被用户点到；点它们仍会弹提示，但那条判据在这里没意义）。
     * 已接真的三处**不在这份清单里**：主按钮（T9d 真提交）、「保存介入率和完单数设置」（F3）、
     * 「新建模板」（F4 打开编辑弹层）——它们各有自己的判据。
     */
    const controls = Array.from(
      root.querySelectorAll<HTMLElement>(NOT_OPEN_SELECTOR),
    ).filter((el) => {
      if (el.matches(SUBMIT_SELECTOR)) return false
      if (el.matches(ACCEPT_SAVE_SELECTOR)) return false
      if (el.matches(NEW_TEMPLATE_SELECTOR)) return false
      if (el.matches('select, input, textarea, option')) return false
      if (el.closest('.layui-hide')) return false
      for (let node: HTMLElement | null = el; node; node = node.parentElement) {
        if (node.style?.display === 'none') return false
      }
      return true
    })
    // 前置哨兵：这两颗是评审核对抓到的真 bug 现场——对标的 `<div class="layui-btn">`，
    // 只写 `a.layui-btn` 时它们**点了完全没反应**（I1）。必须真的被收进这份清单。
    for (const cls of ['getPrivtePhone', 'saveContactBtn']) {
      expect(
        controls.some((el) => el.classList.contains(cls)),
        `可见控件清单里应当有 .${cls}`,
      ).toBe(true)
    }
    // 「保存介入率和完单数设置」已接真（F3）⇒ 必须**不在**这份清单里。
    expect(
      controls.some((el) => el.classList.contains('saveAcceptSettingBtn')),
      '已接真的门槛保存按钮不该再走「尚未开放」',
    ).toBe(false)
    expect(controls.length).toBeGreaterThanOrEqual(2)
    for (const el of controls) {
      LAYUI.msg.mockClear()
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.msg, `${el.tagName}.${el.className} 点了没提示`).toHaveBeenCalledTimes(1)
      expect(String(LAYUI.msg.mock.calls[0]![0]), `${el.className}`).toMatch(/^「.+」尚未开放$/)
    }
    cleanup()
  })

  it('点 `<select lay-filter="pushChannel">` **不**弹提示（总闸不许吃掉表单控件）', async () => {
    const { root, cleanup } = await mountPage()
    const selectEl = root.querySelector<HTMLSelectElement>('select[name="pushChannel"]')!
    expect(isNotOpenTarget(selectEl)).toBe(false)
    selectEl.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('卸载后总闸摘掉（再点主按钮不再弹、也不再发请求）', async () => {
    const { root, cleanup } = await mountPage()
    const submit = root.querySelector<HTMLElement>(SUBMIT_SELECTOR)!
    cleanup()
    expect(root.innerHTML).toBe('')
    // 把同一颗按钮挂回**同一个 root**：监听器已摘，点击不应再弹（证明卸载真的解绑了）。
    root.appendChild(submit)
    submit.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushAsync()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(pushCalls()).toHaveLength(0)
  })
})

describe('发单页（order-pushjdp）：T9d 提交接真（只发已接真的业务字段 + platform）', () => {
  it('成功（state=sent）：载荷**恰好** 10 个键；显示平台单号与关层提示', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'sent', state_display: '已发单', serial_no: 'DL20261026000001' })

    clickSubmit(root)
    await flushAsync()

    const calls = pushCalls()
    expect(calls).toHaveLength(1)
    const config = calls[0]![0]
    expect(config.method).toBe('post')
    expect(config.url).toBe(pushToPlatformUrl(ORDER_NO))
    // 9 个业务字段（含 I3 的 `requirement` 与 P9 的 `memo`；`off_sell_days` 在「不设置」那一档
    // **不上送**）+ 接口固定的 platform=10 ⇒ **恰好 10 个键**；多一个（如 gameAcc）少一个都红。
    // 两个门槛显式带上：完单数 1（全局默认）、介入率 null（不设置）。
    expect(Object.keys(config.data ?? {}).sort()).toEqual(
      [
        'ensure1',
        'ensure2',
        'groups',
        'limit_accept',
        'memo',
        'platform',
        'power_level',
        'price',
        'requirement',
        'time_limit_hours',
        'zone_server_id',
      ].sort(),
    )
    expect(config.data).toEqual({
      platform: 10,
      price: '12.50',
      ensure1: '5.00',
      ensure2: '2.50',
      time_limit_hours: 26,
      zone_server_id: ZONE.zone_server_id,
      requirement: PREVIEW.requirement,
      // ⭐ P9：发出去的代练内容 = 「当前订单说明」框里的当前值（打开时由 preview.memo 带出）。
      memo: PREVIEW.memo,
      power_level: 1,
      limit_accept: null,
      // ⭐ G4：发单频道（证据件默认「公共频道」）也真发；它是 groups 的唯一来源。
      groups: 'PUBLIC',
    })

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('sent')
    expect(result.textContent).toContain(SENT_MESSAGE)
    expect(result.textContent).toContain('DL20261026000001')
    expect(result.textContent).toContain(CLOSE_HINT)
    cleanup()
  })

  it('业务失败（state=failed）：界面原文来自后端 `last_error`，⛔ 不显示成功', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'failed', state_display: '发单失败', last_error: FAILED_REASON })

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('failed')
    expect(result.textContent).toContain(FAILED_PREFIX)
    expect(result.textContent).toContain(FAILED_REASON)
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('状态未知（state=unknown）：醒目提示 + 后端 `last_error` 原文，⛔ 不显示成功', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'unknown', state_display: '状态未知', last_error: UNKNOWN_REASON })

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('unknown')
    expect(result.textContent).toContain(UNKNOWN_STATE_MESSAGE)
    expect(result.textContent).toContain(UNKNOWN_REASON)
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('`state=sent` 却没有 `serial_no` ⇒ 仍按 **unknown**（没有单号就不能叫成功）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'sent', state_display: '已发单', serial_no: null, last_error: '' })

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('unknown')
    expect(result.textContent).toContain(UNKNOWN_STATE_MESSAGE)
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('未走专门分支的 state ⇒ 明说实际状态，⛔ 不说「已提交，平台处理中」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'closed', state_display: '已关闭', serial_no: 'DL20261026000004' })

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    // `closed` 是**已知**状态（已在平台删除）⇒ 不能说「未识别」。
    expect(result.textContent).toBe(unexpectedStateMessage('closed'))
    expect(unexpectedStateMessage('closed')).toBe('推送已关闭（closed），请核对')
    expect(result.textContent).not.toContain('未识别')
    expect(result.textContent).not.toContain('平台处理中')
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('真正没见过的 state 码 ⇒ 「未预期的状态：<state>，请核对」', () => {
    expect(unexpectedStateMessage('future_code')).toBe('未预期的状态：future_code，请核对')
  })

  it('前置闸失败（BizError）：后端 `msg` 原样展示，⛔ 不假装成功', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = new BizError(1002, GATE_REASON)

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('failed')
    expect(result.textContent).toContain(GATE_REASON)
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('网络错误 / 超时（NetworkError）⇒ **unknown**，⛔ 不许说「发单失败」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = new NetworkError('请求超时')

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('unknown')
    expect(result.textContent).toContain(REQUEST_RESULT_UNKNOWN_MESSAGE)
    expect(result.textContent).toContain('请求超时')
    expect(result.textContent).not.toContain(FAILED_PREFIX)
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('5xx（HttpError 500）也按 **unknown**：写动作可能已经到达上游', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = new HttpError(500, '服务器内部错误')

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('unknown')
    expect(result.textContent).toContain(REQUEST_RESULT_UNKNOWN_MESSAGE)
    expect(result.textContent).toContain('服务器内部错误')
    expect(result.textContent).not.toContain(FAILED_PREFIX)
    cleanup()
  })

  it('4xx（HttpError 400）仍按 **failed**：后端明确挡下了（参数 / 权限 / 会话）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = new HttpError(400, '抛单价格格式不对（后端原文）')

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('failed')
    expect(result.textContent).toContain(FAILED_PREFIX)
    expect(result.textContent).toContain('抛单价格格式不对（后端原文）')
    expect(result.textContent).not.toContain(SENT_MESSAGE)
    cleanup()
  })

  it('未选区服 ⇒ **零发单请求** + 中文提示（不靠后端那道必填闸兜底）', async () => {
    // F5：默认区服匹配不到（source=none）⇒ 页面必须要求手选，⛔ 不自动挑一个。
    routes.preview = {
      ...PREVIEW,
      zone_server_id: null,
      server_name: null,
      zone_source: 'none',
    }
    // 下拉也没有任何区服可选 ⇒ 页面必须要求手选（不替用户挑一个）。
    routes.gameZones = []
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    fillSubmitFields(root)

    const submit = clickSubmit(root)
    await flushAsync()

    expect(pushCalls()).toHaveLength(0)
    expect(LAYUI.msg).toHaveBeenCalledTimes(1)
    expect(String(LAYUI.msg.mock.calls[0]![0])).toBe(ZONE_REQUIRED_MESSAGE)
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.textContent).toBe(ZONE_REQUIRED_MESSAGE)
    // 按钮没有被错误地锁死（还能改完再点）。
    expect(submit.disabled).toBe(false)
    cleanup()
  })

  it('价格为空的客户端拦截也是零请求（同一道口子，不给假请求）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root, { price: '   ' })

    clickSubmit(root)
    await flushAsync()

    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[0]![0])).toBe(PRICE_REQUIRED_MESSAGE)
    cleanup()
  })

  it('空 / 非法 `order_no` ⇒ **零请求** + 中文提示（形状闸在发请求之前）', async () => {
    for (const search of ['', `&${ORDER_NO_PARAM}=abc`, `&${ORDER_NO_PARAM}=<img src=x>`]) {
      const { root, cleanup } = await mountPage(search)
      // 不选区服：`order_no` 的检查排在区服检查**之前**，所以这里连区服请求都不会有。
      fillSubmitFields(root)
      clickSubmit(root)
      await flushAsync()

      expect(pushCalls(), `search=${JSON.stringify(search)} 不许发单`).toHaveLength(0)
      expect(String(LAYUI.msg.mock.calls[0]![0])).toBe(ORDER_NO_INVALID_MESSAGE)
      const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
      expect(result.textContent).toBe(ORDER_NO_INVALID_MESSAGE)
      cleanup()
      vi.clearAllMocks()
    }
  })

  it('成功 ⇒ 通知父窗口关窗/刷新（postMessage）+ 提示「发单成功」+ 清空残留输入 + 防重复发', async () => {
    const postSpy = vi.spyOn(window.parent, 'postMessage').mockImplementation((() => {}) as never)
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'sent', state_display: '已发单', serial_no: 'DL20261028000001' })

    clickSubmit(root)
    await flushAsync()

    // 父窗口通道：一条消息同时表达「关窗」与「刷新列表」（父页面收到后做这两件事）。
    expect(postSpy).toHaveBeenCalledWith(
      { type: PUSH_JDP_SENT_MESSAGE_TYPE, order_no: ORDER_NO },
      window.location.origin,
    )
    // 弹窗里先显示成功 / 提示。
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('sent')
    expect(result.textContent).toContain('DL20261028000001')
    expect(LAYUI.msg.mock.calls.map((call) => String(call[0]))).toContain(PUSH_JDP_SENT_TEXT)
    // 清空残留输入；按钮保持禁用（⛔ 不再发第二单）。
    expect(input(root, 'throwFee').value).toBe('')
    expect(input(root, 'gameAcc').value).toBe('')
    expect(input(root, 'orderTitle').value).toBe('')
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(true)
    postSpy.mockClear()
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    expect(postSpy).not.toHaveBeenCalled()
    cleanup()
  })

  it('业务失败 ⇒ **不关窗**（零 postMessage）、后端原文在弹窗里、表单值还在', async () => {
    const postSpy = vi.spyOn(window.parent, 'postMessage').mockImplementation((() => {}) as never)
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = pushRow({ state: 'failed', state_display: '发单失败', last_error: FAILED_REASON })

    clickSubmit(root)
    await flushAsync()

    expect(postSpy).not.toHaveBeenCalled()
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.textContent).toContain(FAILED_REASON)
    // 表单保留，允许改完再提交；按钮可再点。
    expect(input(root, 'throwFee').value).toBe('12.50')
    expect(root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!.disabled).toBe(false)
    cleanup()
  })

  it('状态未知（网络 / 5xx）⇒ **不关窗**、提示「已发出，请刷新核对」', async () => {
    const postSpy = vi.spyOn(window.parent, 'postMessage').mockImplementation((() => {}) as never)
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    routes.push = new NetworkError('请求超时')

    clickSubmit(root)
    await flushAsync()

    expect(postSpy).not.toHaveBeenCalled()
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('unknown')
    expect(result.textContent).toContain(REQUEST_RESULT_UNKNOWN_MESSAGE)
    expect(root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!.disabled).toBe(false)
    cleanup()
  })

  it('双击 ⇒ 只发一次请求（按钮在途时 disabled）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)

    let resolvePush: (row: DailiantongPushRow) => void = () => undefined
    routes.push = () =>
      new Promise<DailiantongPushRow>((resolve) => {
        resolvePush = resolve
      })

    const btn = clickSubmit(root)
    // 第二次点击在第一次的同步部分之后、请求 resolve 之前 ⇒ 必须被 `submitting` 旗标挡住。
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushAsync()

    expect(pushCalls()).toHaveLength(1)
    expect(btn.disabled).toBe(true)

    resolvePush(pushRow({ state: 'sent', serial_no: 'DL20261026000002' }))
    await flushAsync()

    expect(pushCalls()).toHaveLength(1)
    // 成功后按钮**保持禁用**（父窗口关窗前也⛔ 不能重复发第二单）。
    expect(btn.disabled).toBe(true)
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('sent')
    cleanup()
  })

  it('在途只显示「提交中…」：⛔ 不许提前说「已提交 / 平台处理中」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)

    let resolvePush: (row: DailiantongPushRow) => void = () => undefined
    routes.push = () =>
      new Promise<DailiantongPushRow>((resolve) => {
        resolvePush = resolve
      })

    clickSubmit(root)
    await flushAsync()

    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('pending')
    expect(result.textContent).toBe(IN_FLIGHT_MESSAGE)
    expect(result.textContent).not.toBe(PENDING_MESSAGE)
    expect(result.textContent).not.toContain('已提交')
    expect(result.textContent).not.toContain('平台处理中')

    resolvePush(pushRow({ state: 'sent', serial_no: 'DL20261026000003' }))
    await flushAsync()
    expect(result.getAttribute(RESULT_STATE_ATTR)).toBe('sent')
    cleanup()
  })
})

describe('发单页（order-pushjdp）：区服下拉（按游戏列全部区服）', () => {
  it('打开时按 preview.game_id 取该游戏全部区服，默认选中预览给的那条', async () => {
    const zoneB: PlatformZone = { zone_server_id: 'zone-bbb', server_name: '英雄联盟/网通/比尔吉沃特' }
    routes.gameZones = [ZONE, zoneB]
    const { root, cleanup } = await mountPage(ORDER_SEARCH)

    const calls = gameZoneCalls()
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('get')
    expect(calls[0]!.params).toEqual({ game_id: PREVIEW.game_id })

    const selectEl = root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!
    expect(selectEl.tagName).toBe('SELECT')
    expect(Array.from(selectEl.options).map((option) => option.value)).toEqual([
      ZONE.zone_server_id,
      zoneB.zone_server_id,
    ])
    // 默认 = 预览给的那条（不是「永远第一条」：map 优先，可能不是列表第一条）。
    expect(selectEl.value).toBe(PREVIEW.zone_server_id)
    cleanup()
  })

  it('预览说没有默认区服（none）⇒ 默认选下拉第一条，并带它重新取预览', async () => {
    const zoneB: PlatformZone = { zone_server_id: 'zone-bbb', server_name: '英雄联盟/网通/比尔吉沃特' }
    routes.gameZones = [zoneB, ZONE]
    routes.preview = {
      ...PREVIEW,
      zone_server_id: null,
      server_name: null,
      zone_source: 'none',
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 第一条被选中，并触发一次带 zone_server_id 的预览。
    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      zoneB.zone_server_id,
    )
    const previewReqs = previewCalls()
    expect(previewReqs.length).toBeGreaterThanOrEqual(2)
    expect(previewReqs[previewReqs.length - 1]!.params).toEqual({
      zone_server_id: zoneB.zone_server_id,
    })
    cleanup()
  })

  it('按游戏名匹配不到 ⇒ 列**全部区服**让人手选（B3：⛔ 不静默空下拉、⛔ 不替用户选）', async () => {
    routes.gameZones = []
    routes.allZones = [ZONE, { zone_server_id: '9999', server_name: '别的游戏/某区' }]
    routes.preview = {
      ...PREVIEW,
      zone_server_id: null,
      server_name: null,
      zone_source: 'none',
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const tip = root.querySelector<HTMLElement>(ZONE_TIP_SELECTOR)!
    expect(tip.style.display).toBe('')
    expect(tip.textContent).toBe(ZONE_NO_GAME_MATCH_MESSAGE)
    // 全部区服进了下拉，但**没有**被自动选中（那是「猜」，B3 明确禁止）。
    const select = root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!
    expect(select.options.length).toBe(2)
    expect(
      Array.from(select.options).some((option) => option.value === ZONE.zone_server_id),
    ).toBe(true)
    // 没有替用户选 ⇒ 直接提交必须被「请先选区服」拦下（原生 select 会默认显示第一条，
    // 判据只能看**产品行为**：没选中就发不出去）。
    fillSubmitFields(root)
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      ZONE_REQUIRED_MESSAGE,
    )
    cleanup()
  })

  it('区服列表接口失败 ⇒ 中文原因；预览给的区服仍保留（不丢选中）', async () => {
    routes.gameZones = new BizError(1002, '区服字典取不到（后端原文）')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const tip = root.querySelector<HTMLElement>(ZONE_TIP_SELECTOR)!
    expect(tip.textContent).toBe(`${ZONE_LIST_FAILED_PREFIX}区服字典取不到（后端原文）`)
    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      PREVIEW.zone_server_id,
    )
    cleanup()
  })

  it('手选另一条区服 ⇒ 带 zone_server_id 重新取预览，并采用返回的区服名', async () => {
    const zoneB: PlatformZone = { zone_server_id: 'zone-bbb', server_name: '英雄联盟/网通/比尔吉沃特' }
    routes.gameZones = [ZONE, zoneB]
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    routes.preview = { ...PREVIEW, zone_server_id: zoneB.zone_server_id, server_name: zoneB.server_name }
    await chooseZone(root, zoneB)

    const calls = previewCalls()
    expect(calls).toHaveLength(2)
    expect(calls[1]!.params).toEqual({ zone_server_id: zoneB.zone_server_id })
    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      zoneB.zone_server_id,
    )
    cleanup()
  })

  it('重新取预览只更新区服 / can_push，⛔ 不覆盖用户已改的价格与标题', async () => {
    const zoneB: PlatformZone = { zone_server_id: 'zone-bbb', server_name: '英雄联盟/网通/比尔吉沃特' }
    routes.gameZones = [ZONE, zoneB]
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    input(root, 'throwFee').value = '7.00'
    input(root, 'orderTitle').value = '用户标题'
    routes.preview = {
      ...PREVIEW,
      price: '99.00',
      title: '后端另一个标题',
      zone_server_id: zoneB.zone_server_id,
      server_name: zoneB.server_name,
    }
    await chooseZone(root, zoneB)
    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      zoneB.zone_server_id,
    )
    expect(input(root, 'throwFee').value).toBe('7.00')
    expect(input(root, 'orderTitle').value).toBe('用户标题')
    cleanup()
  })

  it('迟到的首次预览**不覆盖**用户已手选的区服（序号防串台）', async () => {
    const deferreds: Array<(preview: PlatformPushPreview) => void> = []
    routes.preview = () =>
      new Promise<PlatformPushPreview>((resolve) => {
        deferreds.push(resolve)
      })
    const zoneB: PlatformZone = { zone_server_id: 'zone-bbb', server_name: '用户选的区服' }
    routes.gameZones = [ZONE]
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 首次预览还没回：下拉列表也还没取（要等 game_id）⇒ 先塞一条可选区服再手选。
    const selectEl = root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!
    const manual = document.createElement('option')
    manual.value = zoneB.zone_server_id
    manual.textContent = zoneB.server_name
    selectEl.appendChild(manual)
    selectEl.value = zoneB.zone_server_id
    LAYUI.formHandlers.get(`select(${ZONE_SELECT_FILTER})`)?.({ value: zoneB.zone_server_id })
    await flushAsync()
    expect(deferreds.length).toBeGreaterThanOrEqual(2)

    // 手选后的预览先回（后端按用户选的区服算）。
    deferreds[deferreds.length - 1]!({
      ...PREVIEW,
      zone_server_id: zoneB.zone_server_id,
      server_name: zoneB.server_name,
    })
    await flushAsync()
    // 首次预览后回（默认区服）⇒ 必须被丢弃，不能把用户选的换回去。
    deferreds[0]!({ ...PREVIEW, zone_server_id: 'zone-default', server_name: '默认区服' })
    await flushAsync()

    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      zoneB.zone_server_id,
    )
    cleanup()
  })
})

describe('发单页（order-pushjdp）：时限换算（day 小时 + hours）', () => {
  it('day*24+hours；0 小时与非整数/超限都拦（返回 null）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    select(root, 'day').value = '1'
    select(root, 'hours').value = '2'
    expect(readTimeLimitHours(root)).toBe(26)
    select(root, 'day').value = '30'
    select(root, 'hours').value = '0'
    expect(readTimeLimitHours(root)).toBe(720)
    select(root, 'day').value = '0'
    select(root, 'hours').value = '0'
    expect(readTimeLimitHours(root)).toBeNull()
    select(root, 'day').value = '30'
    select(root, 'hours').value = '1'
    expect(readTimeLimitHours(root)).toBeNull()
    cleanup()
  })

  it('时限超限时提交也是零请求 + 中文提示', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root, { day: '30', hours: '1' })
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[0]![0])).toContain('1–720')
    cleanup()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
 * F1–F5（2026-10-27 实施计划 §2.3 / §2.4）——一键抛单
 * ══════════════════════════════════════════════════════════════════════════ */

/** 点那颗「保存介入率和完单数设置」（证据件里是 `<div class="layui-btn …">`）。 */
function clickAcceptSave(root: HTMLElement): void {
  const button = root.querySelector<HTMLElement>(ACCEPT_SAVE_SELECTOR)
  if (!button) throw new Error('证据件里应当有「保存介入率和完单数设置」')
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

/** 打开 F4 的单模板编辑弹层（走真实点击：`a.edittemp`）。 */
function openTemplateEditor(root: HTMLElement): HTMLElement {
  const link = root.querySelector<HTMLElement>('a.edittemp')
  if (!link) throw new Error('证据件里应当有「编辑选中的模板」')
  link.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  const panel = document.querySelector<HTMLElement>(`.layui-layer-content ${TEMPLATE_PANEL_SELECTOR}`)
  if (!panel) throw new Error('模板编辑弹层没有打开')
  return panel
}

function templateTextarea(panel: HTMLElement): HTMLTextAreaElement {
  const area = panel.querySelector<HTMLTextAreaElement>(`textarea[name="${TEMPLATE_CONTENT_NAME}"]`)
  if (!area) throw new Error('模板弹层里应当有模板内容输入框')
  return area
}

function saveTemplate(panel: HTMLElement): void {
  const button = panel.querySelector<HTMLElement>(TEMPLATE_SAVE_SELECTOR)
  if (!button) throw new Error('模板弹层里应当有「保存模板」')
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('F1：一键默认填充（默认值全部来自后端 preview）', () => {
  it('打开即带预览值；只改价格就能提交（F1 的核心验收）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 预览请求：不带 zone_server_id（让后端给默认区服）。
    const previewReqs = previewCalls()
    expect(previewReqs).toHaveLength(1)
    expect(previewReqs[0]!.method).toBe('get')
    expect(previewReqs[0]!.params).toEqual({})

    // 价格 / 保证金 / 时限。
    expect(input(root, 'throwFee').value).toBe(PREVIEW.price)
    expect(input(root, 'safeAmount').value).toBe(PREVIEW.ensure1)
    expect(input(root, 'effAmount').value).toBe(PREVIEW.ensure2)
    expect(readTimeLimitHours(root)).toBe(PREVIEW.time_limit_hours)
    // 区服（F5）：预览给的默认值已选中。
    expect(root.querySelector<HTMLSelectElement>(ZONE_SELECT_SELECTOR)!.value).toBe(
      PREVIEW.zone_server_id,
    )
    // 标题 / 账号资料五段 / 联系 QQ·电话。
    expect(input(root, 'orderTitle').value).toBe(PREVIEW.title)
    expect(input(root, 'gameAcc').value).toBe(PREVIEW.game_account)
    expect(input(root, 'gamePwd').value).toBe(PREVIEW.game_password)
    expect(input(root, 'gameRole').value).toBe(PREVIEW.game_role)
    // ⭐ P8/P9：「当前订单说明」那一格打开时**自动带出代练内容**（= preview.memo）。
    //    ⛔ 不再填 Actors 第三段占位（`current_info` =「其它:无」）——那会让客服以为订单没内容。
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!.value).toBe(
      PREVIEW.memo,
    )
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!.value).toBe(
      PREVIEW.requirement,
    )
    expect(input(root, 'moblie2').value).toBe(PREVIEW.contact_phone)
    expect(input(root, 'qq').value).toBe(PREVIEW.contact_qq)
    // 门槛（F3）：预览说本单将发 1 / 不设置。
    expect(root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!.value).toBe('1')
    expect(root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!.value).toBe('0')

    // 只改价格 ⇒ 一律能提交：载荷里其余默认值与页面显示一致。
    input(root, 'throwFee').value = '6.00'
    clickSubmit(root)
    await flushAsync()
    const calls = pushCalls()
    expect(calls).toHaveLength(1)
    expect(calls[0]![0].data).toEqual({
      platform: 10,
      price: '6.00',
      ensure1: '0.00',
      ensure2: '0.00',
      time_limit_hours: 720,
      zone_server_id: PREVIEW.zone_server_id,
      // ⭐ I3：代练要求那一格也要真发（页面值 = 预览填的全局模板）。
      requirement: PREVIEW.requirement,
      // ⭐ P9：代练内容 = 「当前订单说明」框（打开时 preview.memo 带出）。
      memo: PREVIEW.memo,
      power_level: 1,
      limit_accept: null,
      // ⭐ G4：发单频道也随单提交。
      groups: 'PUBLIC',
    })
    cleanup()
  })

  it('预览值与证据件默认不同 ⇒ 真的按预览填（价格 / 保证金 / 26h / 标题 / 五段）', async () => {
    routes.preview = {
      ...PREVIEW,
      price: '8.88',
      ensure1: '1.00',
      ensure2: '2.00',
      time_limit_hours: 26,
      title: '王者荣耀巅峰赛',
      game_account: 'tt000001',
      game_password: 'pwd-xyz',
      game_role: '角色乙',
      memo: '安卓:微信\n代练内容乙',
      current_info: '安卓:微信',
      requirement: '新模板乙',
      contact_qq: '987654321',
      contact_phone: '13900139000',
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(input(root, 'throwFee').value).toBe('8.88')
    expect(input(root, 'safeAmount').value).toBe('1.00')
    expect(input(root, 'effAmount').value).toBe('2.00')
    expect(select(root, 'day').value).toBe('1')
    expect(select(root, 'hours').value).toBe('2')
    expect(readTimeLimitHours(root)).toBe(26)
    expect(input(root, 'orderTitle').value).toBe('王者荣耀巅峰赛')
    expect(input(root, 'gameAcc').value).toBe('tt000001')
    expect(input(root, 'gamePwd').value).toBe('pwd-xyz')
    expect(input(root, 'gameRole').value).toBe('角色乙')
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!.value).toBe(
      '安卓:微信\n代练内容乙',
    )
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!.value).toBe(
      '新模板乙',
    )
    expect(input(root, 'qq').value).toBe('987654321')
    expect(input(root, 'moblie2').value).toBe('13900139000')
    cleanup()
  })

  it('预览返回 can_push=false ⇒ 页面显示 reason 并禁用提交（⛔ 不发请求、⛔ 不假装能发）', async () => {
    routes.preview = { ...PREVIEW, can_push: false, reason: '当前订单状态不允许发单（后端原文）' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(true)
    const note = root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!
    expect(note.style.display).toBe('')
    expect(note.textContent).toBe('当前订单状态不允许发单（后端原文）')

    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      '当前订单状态不允许发单（后端原文）',
    )
    cleanup()
  })

  it('can_push=false 但后端没给 reason ⇒ 用固定兜底文案（⛔ 不编具体原因）', async () => {
    routes.preview = { ...PREVIEW, can_push: false, reason: '' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.textContent).toBe(
      PREVIEW_BLOCKED_FALLBACK,
    )
    cleanup()
  })

  it('can_push=true ⇒ 提交按钮可用（解锁）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(false)
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.style.display).toBe('none')
    cleanup()
  })

  it('预览读取失败 ⇒ 中文原因可见；价格留空（⛔ 不发证据件的 0 元占位）', async () => {
    routes.preview = new BizError(1002, '订单不存在或无权查看（后端原文）')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const warning = root.querySelector<HTMLElement>('.tt-defaults-warning')!
    expect(warning.style.display).toBe('')
    expect(warning.textContent).toBe(`${PREVIEW_FAILED_PREFIX}订单不存在或无权查看（后端原文）`)
    // 预览没回来 ⇒ 不猜 can_push，按钮仍可按；但价格是空的，客户端必填闸会拦（不发 0.00）。
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(false)
    expect(input(root, 'throwFee').value).toBe('')
    // 预览失败 ⇒ 没有 game_id，区服列表也取不到（下拉为空）⇒ 提交被「先选区服」拦下。
    // 价格留空这条仍钉住：⛔ 不把证据件的 0 当默认价发出去。
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      ZONE_REQUIRED_MESSAGE,
    )
    cleanup()
  })

  it('门槛 / 模板读取失败 ⇒ 显示中文原因；此时两道保存闸都关着（见 F3/F4）', async () => {
    routes.settings = new BizError(1002, '设置读取失败（后端原文）')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const warning = root.querySelector<HTMLElement>('.tt-defaults-warning')!
    expect(warning.textContent).toContain(DEFAULTS_SETTINGS_FAILED_PREFIX)
    expect(warning.textContent).toContain('设置读取失败（后端原文）')
    cleanup()
  })

  it('迟到的预览**不覆盖用户已改过的字段**（价格/标题以用户为准）', async () => {
    let resolvePreview: (preview: PlatformPushPreview) => void = () => undefined
    routes.preview = () =>
      new Promise<PlatformPushPreview>((resolve) => {
        resolvePreview = resolve
      })
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 预览还没回来，用户已经改了价格与标题。
    input(root, 'throwFee').value = '7.77'
    input(root, 'orderTitle').value = '我自己写的标题'
    resolvePreview({ ...PREVIEW, price: '12.50', title: '后端标题' })
    await flushAsync()
    expect(input(root, 'throwFee').value).toBe('7.77')
    expect(input(root, 'orderTitle').value).toBe('我自己写的标题')
    // 没被改过的格子仍按预览填。
    expect(input(root, 'gameAcc').value).toBe(PREVIEW.game_account)
    cleanup()
  })

  it('preview.price=null（订单没定价）⇒ 价格留空 + 提示「请填写发平台价格（最低 2 元）」，⛔ 不显示 0.00', async () => {
    routes.preview = { ...PREVIEW, price: null }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(input(root, 'throwFee').value).toBe('')
    const hint = root.querySelector<HTMLElement>('.tt-price-hint')!
    expect(hint.style.display).toBe('')
    expect(hint.textContent).toBe(PRICE_REQUIRED_MESSAGE)
    expect(PRICE_REQUIRED_MESSAGE).toBe('请填写发平台价格（最低 2 元）')

    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      PRICE_REQUIRED_MESSAGE,
    )
    cleanup()
  })

  it('preview.warnings（客服能改的问题）⇒ 页面上只提示、按钮保持可点', async () => {
    routes.preview = {
      ...PREVIEW,
      warnings: ['抛单价格低于 2.00 元，请改价后再提交。', '还没有可用的代练通区服，请手动选择区服。'],
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const note = root.querySelector<HTMLElement>(PREVIEW_WARNINGS_SELECTOR)!
    expect(note.style.display).toBe('')
    expect(note.textContent).toContain('请改价后再提交')
    expect(note.textContent).toContain('请手动选择区服')
    // 只是提示：⛔ 不据此禁用提交。
    expect(root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!.disabled).toBe(false)
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.style.display).toBe('none')
    cleanup()
  })

  it('「将发到代练通的内容」只读预览：显示 memo；空 memo ⇒「订单没有填写内容」', async () => {
    const first = await mountPage(ORDER_SEARCH)
    const box = first.root.querySelector<HTMLElement>(PUSH_MEMO_SELECTOR)!
    expect(box.textContent).toContain(PUSH_MEMO_TITLE)
    expect(box.textContent).toContain(String(PREVIEW.memo))
    // 只读：块里没有任何 input / textarea。
    expect(box.querySelector('input, textarea, select')).toBeNull()
    first.cleanup()

    routes.preview = { ...PREVIEW, memo: '   ' }
    const second = await mountPage(ORDER_SEARCH)
    expect(
      second.root.querySelector<HTMLElement>(PUSH_MEMO_SELECTOR)!.textContent,
    ).toContain(PUSH_MEMO_EMPTY_TEXT)
    second.cleanup()
  })

  /**
   * ⭐ P8 / P9（2026-10-27 业务方真机 bug）：「当前订单说明」框与底部预览必须**同一份内容**。
   *
   * 现场：改了框里的字，发出去还是默认「其它:无」；底部那块却显示 preview.memo ⇒ 页面在说假话。
   * 判据落三处：① 打开自动带出代练内容；② 提交载荷取**框里当前值**；③ 改框时底部预览跟着变。
   */
  it('P8：打开即把 preview.memo 带进「当前订单说明」框（⛔ 不是 Actors 占位「其它:无」）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const area = root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!
    expect(area.value).toBe(PREVIEW.memo)
    expect(area.value).not.toBe(PREVIEW.current_info)
    // 底部那块显示的也是同一份内容。
    expect(root.querySelector<HTMLElement>(PUSH_MEMO_SELECTOR)!.textContent).toContain(PREVIEW.memo)
    cleanup()
  })

  it('P9：改了「当前订单说明」⇒ 提交载荷的 memo 用**框里的新值**（不是 preview 的旧值）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    const area = root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!
    const edited = '手改后的代练内容：打手按这个做'
    area.value = edited
    area.dispatchEvent(new Event('input', { bubbles: true }))

    clickSubmit(root)
    await flushAsync()
    const data = pushCalls()[0]![0].data!
    expect(data.memo).toBe(edited)
    expect(data.memo).not.toBe(PREVIEW.memo)
    cleanup()
  })

  it('P9：改框时底部「将发到代练通的内容」同步更新（两块不许各说各话）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const area = root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!
    const box = root.querySelector<HTMLElement>(PUSH_MEMO_SELECTOR)!
    expect(box.textContent).toContain(String(PREVIEW.memo))

    const edited = '新内容：三局两胜'
    area.value = edited
    area.dispatchEvent(new Event('input', { bubbles: true }))
    expect(box.textContent).toContain(edited)
    expect(box.textContent).not.toContain(String(PREVIEW.memo))

    // 清空框 ⇒ 预览如实说「订单没有填写内容」，提交的 memo 就是空串（用户清掉了）。
    area.value = ''
    area.dispatchEvent(new Event('input', { bubbles: true }))
    expect(box.textContent).toContain(PUSH_MEMO_EMPTY_TEXT)
    await chooseZone(root)
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()[0]![0].data!.memo).toBe('')
    cleanup()
  })

  it('P9：用户清空框后再换区服（会重取预览）⇒ 框**不被预览值填回去**', async () => {
    // 换到**另一条**区服才真的会再取一次预览（`preview.load(zone_server_id)`，见 F5）
    // ——那一发不许把用户清空的框填回去。下拉候选在挂载时按 `gameZones` 填 ⇒ 必须先设。
    const other: PlatformZone = { zone_server_id: '200200020000000', server_name: '英雄联盟/电信/黑色玫瑰' }
    routes.gameZones = [ZONE, other]
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const area = root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')!
    area.value = ''
    area.dispatchEvent(new Event('input', { bubbles: true }))
    await chooseZone(root, other)
    expect(previewCalls()).toHaveLength(2)
    expect(area.value).toBe('')
    cleanup()
  })

  it('P9：预览读取失败 ⇒ 底部预览**如实**说读取失败（⛔ 不一直停在「正在读取」、⛔ 不谎称没有内容）', async () => {
    routes.preview = new Error('预览接口 500')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const box = root.querySelector<HTMLElement>(PUSH_MEMO_SELECTOR)!
    expect(box.textContent).toContain(PUSH_MEMO_FAILED_TEXT)
    cleanup()
  })

  it('价格 <2 元：按钮**可点**，提交时拦下并提示「代练通最低 2 元，请改价」', async () => {
    routes.preview = { ...PREVIEW, price: '1.00' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const price = input(root, 'throwFee')
    expect(price.value).toBe('1.00')
    expect(root.querySelector<HTMLElement>('.tt-price-hint')!.style.display).toBe('')
    expect(root.querySelector<HTMLElement>('.tt-price-hint')!.textContent).toBe(PRICE_LOW_MESSAGE)

    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    // 价格是客服能改的 ⇒ 按钮保持可点（⛔ 不拿 can_push 永久禁用）。
    expect(submit.disabled).toBe(false)
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(PRICE_LOW_MESSAGE)
    const result = root.querySelector<HTMLElement>(RESULT_SELECTOR)!
    expect(result.textContent).toBe(PRICE_LOW_MESSAGE)

    // 改成 2 元 ⇒ 提示消失、可以正常提交。
    price.value = '2.00'
    price.dispatchEvent(new Event('input', { bubbles: true }))
    expect(root.querySelector<HTMLElement>('.tt-price-hint')!.style.display).toBe('none')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    cleanup()
  })

  it('价格<2 **不会**遮住硬原因：can_push=false 时页面显示硬原因并禁用提交（I2）', async () => {
    // ⭐ I2：判序改成「先看硬件原因（没配置 / 状态不允许 / 已抛过），再给价格提示」。
    // 旧实现是 `priceBelowMinimum || can_push ⇒ 解锁` ⇒ 「价格<2」会把「这单不能发」遮住，
    // 客服改完价仍然发不出去。现在以 `can_push` 为准，价格提示不参与解锁。
    routes.preview = {
      ...PREVIEW,
      price: '1.50',
      can_push: false,
      reason: '只有「未分配」或「待抢单」的订单才能抛到代练通',
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(true)
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.style.display).toBe('')
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.textContent).toBe(
      '只有「未分配」或「待抢单」的订单才能抛到代练通',
    )
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      '只有「未分配」或「待抢单」的订单才能抛到代练通',
    )
    cleanup()
  })

  it('价格<2 且 can_push=true ⇒ 只提示、按钮仍可点（价格是客服能改的）', async () => {
    routes.preview = { ...PREVIEW, price: '1.50', can_push: true, reason: '' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const submit = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)!
    expect(submit.disabled).toBe(false)
    expect(root.querySelector<HTMLElement>(PREVIEW_BLOCK_SELECTOR)!.style.display).toBe('none')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(PRICE_LOW_MESSAGE)
    cleanup()
  })
})

describe('F2：右面板（summary）三态 + 中文原因', () => {
  it('正常：reloadData 一行（平台 / 账号 / 可用余额 / 冻结余额），表格显示、提示隐藏', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(summaryCalls()).toHaveLength(1)
    const last = LAYUI.reloadData.mock.calls[LAYUI.reloadData.mock.calls.length - 1]!
    expect(last[0]).toBe('tableList')
    expect((last[1] as { data: unknown[] }).data).toEqual([
      {
        platformName: '代练通',
        platformAcc: 'acct-a',
        availableBalance: '100.00',
        frozenBalance: '5.50',
      },
    ])
    const note = root.querySelector<HTMLElement>(SUMMARY_NOTE_SELECTOR)!
    expect(note.style.display).toBe('none')
    cleanup()
  })

  it('没配置账号：显示中文原因，⛔ 不显示「暂无数据」', async () => {
    routes.summary = {
      platform: 10,
      platform_label: '代练通',
      account: '',
      configured: false,
      available_balance: null,
      frozen_balance: null,
      error: '',
    }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const note = root.querySelector<HTMLElement>(SUMMARY_NOTE_SELECTOR)!
    expect(note.style.display).toBe('')
    expect(note.textContent).toBe(SUMMARY_NO_CONFIG_MESSAGE)
    expect(note.textContent).not.toContain('暂无数据')
    cleanup()
  })

  it('后端在成功载荷里回了 `error` ⇒ 原样显示那句中文原因', async () => {
    routes.summary = { ...SUMMARY, error: '代练通返回的余额不是有效金额' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(root.querySelector<HTMLElement>(SUMMARY_NOTE_SELECTOR)!.textContent).toBe(
      '代练通返回的余额不是有效金额',
    )
    cleanup()
  })

  it('业务前提不满足（BizError）⇒「平台账户不可用：」+ 后端原文；菜单里不出现「暂无数据」', async () => {
    routes.summary = new BizError(1002, GATE_REASON)
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const note = root.querySelector<HTMLElement>(SUMMARY_NOTE_SELECTOR)!
    expect(note.textContent).toBe(`${SUMMARY_UNAVAILABLE_PREFIX}${GATE_REASON}`)
    expect(note.textContent).not.toContain('暂无数据')
    cleanup()
  })

  it('网络 / 超时 ⇒「平台账户查询失败：」+ 传输层原文', async () => {
    routes.summary = new NetworkError('请求超时')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(root.querySelector<HTMLElement>(SUMMARY_NOTE_SELECTOR)!.textContent).toBe(
      '平台账户查询失败：请求超时',
    )
    cleanup()
  })

  it('三态分类（纯函数）：正常 / 没配账号 / 查询失败', () => {
    expect(summaryPanelState(SUMMARY)).toBe('ok')
    expect(summaryPanelState({ ...SUMMARY, account: '', configured: false, error: '' })).toBe(
      'no_config',
    )
    expect(summaryPanelState({ ...SUMMARY, error: '上游报错' })).toBe('failed')
    expect(summaryReason({ ...SUMMARY, account: '', configured: false, error: '' })).toBe(
      SUMMARY_NO_CONFIG_MESSAGE,
    )
  })
})

describe('F3：门槛两格默认（完单≥1 / 不设置）+「保存介入率和完单数设置」', () => {
  it('两格默认值来自全局设置：完单数 1、介入率 = 不设置（value=0）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!.value).toBe('1')
    expect(root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!.value).toBe('0')
    cleanup()
  })

  it('点保存 → PUT settings **只带两个键**（null = 不设置）→ 提示「保存成功」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    clickAcceptSave(root)
    await flushAsync()
    const calls = settingsCalls().filter((call) => String(call.method).toLowerCase() === 'put')
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(PLATFORM_INTEGRATION_SETTINGS_URL)
    expect(calls[0]!.data).toEqual({ default_power_level: 1, default_limit_accept: null })
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toBe(
      SAVE_SUCCESS_TEXT,
    )
    cleanup()
  })

  it('改了两格再保存 → 载荷就是改后的两个值（0 = 不设置）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!.value = '3'
    root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!.value = '5'
    clickAcceptSave(root)
    await flushAsync()
    const call = settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')[0]!
    expect(call.data).toEqual({ default_power_level: 3, default_limit_accept: 5 })
    cleanup()
  })

  it('保存失败 ⇒ 显示后端原文（⛔ 不报「保存成功」）', async () => {
    routes.sendSettings = new BizError(1000, '默认完单数必须是整数（后端原文）')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    clickAcceptSave(root)
    await flushAsync()
    const messages = LAYUI.msg.mock.calls.map((call) => String(call[0]))
    expect(messages).toContain('默认完单数必须是整数（后端原文）')
    expect(messages).not.toContain(SAVE_SUCCESS_TEXT)
    cleanup()
  })

  it('设置还没读到 ⇒ 点保存零请求 + 明确提示（⛔ 不拿证据件默认值覆盖真配置）', async () => {
    routes.settings = new NetworkError('设置接口超时')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    clickAcceptSave(root)
    await flushAsync()
    const puts = settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')
    expect(puts).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toContain('还没读取完成')
    cleanup()
  })

  it('保存连点 ⇒ 只发一次 PUT（在途闸 + 按钮禁用）', async () => {
    let resolveSave: (value: PlatformPushSettings) => void = () => undefined
    routes.sendSettings = () =>
      new Promise<PlatformPushSettings>((resolve) => {
        resolveSave = resolve
      })
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    clickAcceptSave(root)
    clickAcceptSave(root)
    await flushAsync()
    const puts = settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')
    expect(puts).toHaveLength(1)
    const button = root.querySelector<HTMLElement>(ACCEPT_SAVE_SELECTOR)!
    expect(button.classList.contains('layui-btn-disabled')).toBe(true)
    resolveSave({ ...SETTINGS })
    await flushAsync()
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
    cleanup()
  })

  it('本单改了门槛 ⇒ 发单载荷带改后的值（只影响本单；介入率 0 = null）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!.value = '3'
    root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!.value = '5'
    clickSubmit(root)
    await flushAsync()
    const data = pushCalls()[0]![0].data!
    expect(data.power_level).toBe(3)
    expect(data.limit_accept).toBe(5)
    // 完单数选「不设置」(0) = 上游的不限制；介入率选「不设置」(0) = 发 null（⛔ 不是 0%）。
    expect(readAcceptPayload(root)).toEqual({ power_level: 3, limit_accept: 5 })
    root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!.value = '0'
    root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!.value = '0'
    expect(readAcceptPayload(root)).toEqual({ power_level: 0, limit_accept: null })
    cleanup()
  })

  it('设置没读到真值 ⇒ 发单**不带**两个门槛键，且页面如实写明「门槛读取失败」（I1）', async () => {
    routes.settings = new NetworkError('设置接口超时')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // I1：提示里必须明确「门槛读取失败」+「本次提交不会带上」，⛔ 不把证据件的 5 当真实值。
    const warning = root.querySelector<HTMLElement>(DEFAULTS_WARNING_SELECTOR)!
    expect(warning.style.display).toBe('')
    expect(warning.textContent).toContain('门槛读取失败')
    expect(warning.textContent).toContain('不会')
    clickSubmit(root)
    await flushAsync()
    const data = pushCalls()[0]![0].data!
    // `requirement` 仍会带上（预览接口本身就有全局模板，与 settings 读失败无关）；
    // `memo` 也一样（它就是「当前订单说明」框里的内容，不依赖 settings）。
    expect(Object.keys(data).sort()).toEqual(
      [
        'ensure1',
        'ensure2',
        // G4：发单频道与 settings 无关，照发。
        'groups',
        'memo',
        'platform',
        'price',
        'requirement',
        'time_limit_hours',
        'zone_server_id',
      ].sort(),
    )
    expect(data).not.toHaveProperty('power_level')
    expect(data).not.toHaveProperty('limit_accept')
    cleanup()
  })

  it('设置读失败但用户手改了门槛 ⇒ 发单带上用户选的值（I1）', async () => {
    routes.settings = new NetworkError('设置接口超时')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const power = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)!
    const limit = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)!
    power.value = '3'
    power.dispatchEvent(new Event('change', { bubbles: true }))
    limit.value = '5'
    limit.dispatchEvent(new Event('change', { bubbles: true }))
    clickSubmit(root)
    await flushAsync()
    const data = pushCalls()[0]![0].data!
    expect(data.power_level).toBe(3)
    expect(data.limit_accept).toBe(5)
    cleanup()
  })
})

describe('F4：代练要求只有一份全局模板（编辑保存即全局生效）', () => {
  it('「编辑选中的模板」打开弹层：预填全局模板、隐藏模板标题行', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const panel = openTemplateEditor(root)
    expect(templateTextarea(panel).value).toBe(SETTINGS.require_template)
    expect(panel.querySelector<HTMLElement>('input[name="template_title"]')!.closest<HTMLElement>(
      '.layui-form-item',
    )!.style.display).toBe('none')
    cleanup()
  })

  it('保存模板 → PUT 只带 `require_template` → 写回本页「代练/陪玩要求」并提示成功', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 后端 `partial=True`：保存后回那份完整设置（这里必须回**新值**，否则页面会按旧值写回）。
    routes.sendSettings = { ...SETTINGS, require_template: '新模板：按单号核对' }
    const panel = openTemplateEditor(root)
    templateTextarea(panel).value = '新模板：按单号核对'
    saveTemplate(panel)
    await flushAsync()
    const puts = settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')
    expect(puts).toHaveLength(1)
    expect(puts[0]!.data).toEqual({ require_template: '新模板：按单号核对' })
    expect(root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!.value).toBe(
      '新模板：按单号核对',
    )
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toContain('保存成功')
    expect(LAYUI.closes.length).toBeGreaterThan(0)
    cleanup()
  })

  it('用户已经手改过「代练/陪玩要求」⇒ 保存模板不覆盖那一格', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const field = root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!
    field.value = '本单特殊要求，不要覆盖'
    const panel = openTemplateEditor(root)
    templateTextarea(panel).value = '新的全局模板'
    saveTemplate(panel)
    await flushAsync()
    expect(field.value).toBe('本单特殊要求，不要覆盖')
    cleanup()
  })

  it('设置还没读到 ⇒ 模板保存零请求 + 明确提示', async () => {
    routes.settings = new NetworkError('设置接口超时')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const panel = openTemplateEditor(root)
    templateTextarea(panel).value = '不该被保存'
    saveTemplate(panel)
    await flushAsync()
    const puts = settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')
    expect(puts).toHaveLength(0)
    expect(String(LAYUI.msg.mock.calls[LAYUI.msg.mock.calls.length - 1]![0])).toContain('还没读取完成')
    cleanup()
  })

  it('「设为默认模板」/「删除选中的模板」只解释「只有一份」、零请求（⛔ 不做多模板）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    LAYUI.msg.mockClear()
    root.querySelector<HTMLElement>('a.defualttemp')!.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    root.querySelector<HTMLElement>('a.deltemp')!.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
    await flushAsync()
    const messages = LAYUI.msg.mock.calls.map((call) => String(call[0]))
    expect(messages.some((text) => text.includes('只有一份全局模板'))).toBe(true)
    expect(settingsCalls().filter((c) => String(c.method).toLowerCase() === 'put')).toHaveLength(0)
    cleanup()
  })
})

describe('I3（2026-10-27）：代练要求 / 自动下架天数真的提交', () => {
  it('页面填的代练要求随载荷发出去（`requirement`）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    const content = root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!
    content.value = '按单号核对，晚上在线'
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()[0]![0].data!.requirement).toBe('按单号核对，晚上在线')
    cleanup()
  })

  it('代练要求留空 ⇒ **不发**这个键（由后端用全局模板）', async () => {
    // 全局模板也空 ⇒ 页面那格真的是空串（否则 settings 的 apply 会把它填成模板）。
    routes.settings = { ...SETTINGS, require_template: '' }
    routes.preview = { ...PREVIEW, requirement: '' }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    expect(
      root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!.value,
    ).toBe('')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()[0]![0].data).not.toHaveProperty('requirement')
    cleanup()
  })

  it('自动下架天数选了 7 天 ⇒ 载荷带 `off_sell_days: 7`', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    const offSell = root.querySelector<HTMLSelectElement>('select[name="offSellDays"]')!
    offSell.value = '7'
    expect(readOffSellDays(root)).toBe(7)
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()[0]![0].data!.off_sell_days).toBe(7)
    cleanup()
  })

  it('自动下架天数「不设置」(0) ⇒ **不发**这个键（⛔ 不发 0；上游默认 7 天）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    const offSell = root.querySelector<HTMLSelectElement>('select[name="offSellDays"]')!
    offSell.value = '0'
    expect(readOffSellDays(root)).toBeNull()
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()[0]![0].data).not.toHaveProperty('off_sell_days')
    cleanup()
  })

  it('`readOffSellDays` 的上界与后端同值（30 天）', () => {
    expect(OFF_SELL_DAYS_MAX).toBe(30)
  })
})

/* ═══════════ G4（2026-10-28）：延迟发平台 / 发单频道 / 指定人·组历史 ═══════════ */

describe('G4：延迟发平台块（价格与大厅价都 >0 才显示；后端未支持 ⇒ 明确拒）', () => {
  function setValue(root: HTMLElement, name: string, value: string): void {
    const el = input(root, name)
    el.value = value
    el.dispatchEvent(new Event('input', { bubbles: true }))
  }

  function delayDiv(root: HTMLElement): HTMLElement {
    const el = root.querySelector<HTMLElement>(DELAY_DIV_SELECTOR)
    if (!el) throw new Error('证据件里应当有 .delayDiv')
    return el
  }

  function selectDelay(root: HTMLElement, value: 'Y' | 'N'): void {
    // 真 layui 的 radio 点击就是把原生 input 置 checked（`form.on('radio')` 只是通知）；
    // 提交时读的就是这个 `:checked` ⇒ 用它模拟真实操作。
    const radio = root.querySelector<HTMLInputElement>(`input[name="isDelay"][value="${value}"]`)!
    radio.checked = true
  }

  it('默认隐藏；只有「发平台价 >0」且「同发内部抢单价 >0」才显示（对标 delayDivShow）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    // 预览填了 12.50，大厅价仍是证据件的 0 ⇒ 不显示。
    expect(delayDiv(root).style.display).toBe('none')
    setValue(root, 'throwFeeHome', '8.00')
    expect(delayDiv(root).style.display).toBe('')

    // 发平台价清零 ⇒ 隐藏（两个条件缺一不可）。
    setValue(root, 'throwFee', '0')
    expect(delayDiv(root).style.display).toBe('none')
    setValue(root, 'throwFee', '12.50')
    expect(delayDiv(root).style.display).toBe('')

    // 大厅价不是数字 / 为 0 ⇒ 隐藏（对标 `parseFloat(...) > 0 && parseFloat(...)`）。
    setValue(root, 'throwFeeHome', '0')
    expect(delayDiv(root).style.display).toBe('none')
    setValue(root, 'throwFeeHome', 'abc')
    expect(delayDiv(root).style.display).toBe('none')
    cleanup()
  })

  it('块里有「后端尚未支持」的可见说明（⛔ 不让人以为勾了「是」就会延迟）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    setValue(root, 'throwFeeHome', '8.00')
    const note = delayDiv(root).querySelector<HTMLElement>(DELAY_NOTE_SELECTOR)
    expect(note).not.toBeNull()
    expect(note!.textContent).toContain('尚未支持')
    expect(note!.textContent).toContain('延迟')
    cleanup()
  })

  it('选「是」⇒ 提交被拦下、零请求，并说出原因（⛔ 不拿「立即发平台」冒充延迟）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    setValue(root, 'throwFeeHome', '8.00')
    selectDelay(root, 'Y')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    const messages = LAYUI.msg.mock.calls.map((call) => String(call[0]))
    expect(messages.some((text) => text.includes(DELAY_UNSUPPORTED_MESSAGE))).toBe(true)
    cleanup()
  })

  it('选回「否」⇒ 照常提交，且载荷**不带** `isDelay` / `delaySec`（后端没有这两个入参）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    setValue(root, 'throwFeeHome', '8.00')
    selectDelay(root, 'Y')
    selectDelay(root, 'N')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    const data = pushCalls()[0]![0].data!
    expect(data).not.toHaveProperty('isDelay')
    expect(data).not.toHaveProperty('delaySec')
    cleanup()
  })
})

describe('G4：发单频道联动「指定人或组 / 接单密码」+ 历史下拉', () => {
  function selectChannel(root: HTMLElement, value: string): void {
    const selectEl = select(root, 'pushChannel')
    selectEl.value = value
    LAYUI.formHandlers.get(`select(${GROUPS_CHANNEL_FILTER})`)?.({ value, elem: selectEl })
  }

  function ugdiv(root: HTMLElement): HTMLElement {
    return root.querySelector<HTMLElement>(UGDIV_SELECTOR)!
  }

  it('默认（公共频道）隐藏；选「指定打手 / 指定群组」⇒ 显示并聚焦「指定人或组」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(ugdiv(root).style.display).toBe('none')

    selectChannel(root, 'USER')
    expect(ugdiv(root).style.display).toBe('')
    expect(root.querySelector(UG_LABEL_SELECTOR)!.textContent?.trim()).toBe(UG_DEFAULT_LABEL)
    expect(input(root, 'ugtext').placeholder).toBe(UG_DEFAULT_LABEL)
    expect(document.activeElement).toBe(input(root, 'ugtext'))

    selectChannel(root, 'PUBLIC')
    expect(ugdiv(root).style.display).toBe('none')
    cleanup()
  })

  it('选「接单密码」⇒ 同一个格子改叫「指定接单密码」', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    selectChannel(root, 'RecPwd')
    expect(ugdiv(root).style.display).toBe('')
    expect(root.querySelector(UG_LABEL_SELECTOR)!.textContent?.trim()).toBe(UG_PASSWORD_LABEL)
    expect(input(root, 'ugtext').placeholder).toBe(UG_PASSWORD_LABEL)
    cleanup()
  })

  it('离开「指定人/组」两种频道 ⇒ 隐藏并清空已填的值（对标 else 分支）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    selectChannel(root, 'GROUP')
    input(root, 'ugtext').value = '合成打手甲'
    selectChannel(root, 'PUBLIC')
    expect(ugdiv(root).style.display).toBe('none')
    expect(input(root, 'ugtext').value).toBe('')
    expect(root.querySelector(UG_LABEL_SELECTOR)!.textContent?.trim()).toBe(UG_DEFAULT_LABEL)
    cleanup()
  })

  it('历史下拉：聚焦「指定人或组」⇒ 显示「尚未开放」、**零请求**（后端没有历史端点）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const history = root.querySelector<HTMLElement>(UG_HISTORY_SELECTOR)!
    expect(history.style.display).toBe('none')
    const before = mockedRequest.mock.calls.length
    // 选「指定打手」⇒ 那格显示并自动聚焦（对标 `$("#ugtext").focus()`）⇒ 历史下拉给空态。
    selectChannel(root, 'USER')
    expect(history.style.display).toBe('')
    expect(history.textContent).toContain('尚未开放')
    // 零请求：没有偷偷打一个「历史」端点。
    expect(mockedRequest.mock.calls.length).toBe(before)

    // 点外面收起（对标 `$(document).bind("click")`）。
    document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(history.style.display).toBe('none')
    cleanup()
  })

  it('频道是「指定打手」⇒ 提交被拦下、零请求（发单接口没有「指定对象」字段）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    selectChannel(root, 'USER')
    input(root, 'ugtext').value = '合成打手甲'
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    const messages = LAYUI.msg.mock.calls.map((call) => String(call[0]))
    expect(messages.some((text) => text.includes(PUSH_CHANNEL_UNSUPPORTED_PREFIX))).toBe(true)
    cleanup()
  })

  it('「丸子专用」频道不适用于代练通 ⇒ 提交被拦下、零请求', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    selectChannel(root, 'DLWZXS')
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(0)
    cleanup()
  })
})

describe('G4：同发内部抢单价 / 发单频道 / 标签真的随单提交（载荷恰好）', () => {
  function setValue(root: HTMLElement, name: string, value: string): void {
    input(root, name).value = value
  }

  it('无大厅发单权限时只填写并提交平台发单', async () => {
    routes.me = { user: { menu_permissions: ['order.platform_write'] } }
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    expect(root.querySelector('#throwFeeHome')).toBeNull()
    expect(root.querySelector(NOTE_SELECTOR)?.textContent).not.toContain('同发内部抢单价')

    await chooseZone(root)
    fillSubmitFields(root)
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    expect(pushCalls()[0]![0].data).not.toHaveProperty('hall_price_same')
    cleanup()
  })

  it('大厅价 >0 + 选了优质频道 + 填了标签 ⇒ 三个键都在，值就是页面上的', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    setValue(root, 'throwFeeHome', '8.00')
    input(root, 'label1').value = '  标签甲  '
    const channel = select(root, 'pushChannel')
    channel.value = 'GOODCHANNEL'
    LAYUI.formHandlers.get(`select(${GROUPS_CHANNEL_FILTER})`)?.({ value: 'GOODCHANNEL' })

    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    const data = pushCalls()[0]![0].data!
    expect(data.hall_price_same).toBe('8.00')
    expect(data.groups).toBe('GOODCHANNEL')
    expect(data.label1).toBe('标签甲')
    // 载荷恰好：没有延迟字段、也没有「指定对象」（后端没有这两个入参）。
    expect(data).not.toHaveProperty('isDelay')
    expect(data).not.toHaveProperty('delaySec')
    expect(data).not.toHaveProperty('ugtext')
    cleanup()
  })

  it('大厅价 0 / 标签空 ⇒ 两个键都**不发**（⛔ 不拿 0 冒充「同发内部」、不送空标签）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await chooseZone(root)
    fillSubmitFields(root)
    setValue(root, 'throwFeeHome', '0')
    input(root, 'label1').value = '   '
    clickSubmit(root)
    await flushAsync()
    expect(pushCalls()).toHaveLength(1)
    const data = pushCalls()[0]![0].data!
    expect(data).not.toHaveProperty('hall_price_same')
    expect(data).not.toHaveProperty('label1')
    // 频道永远有值（证据件默认 PUBLIC）⇒ 它照发。
    expect(data.groups).toBe('PUBLIC')
    cleanup()
  })
})

describe('常用模板下拉 #tempList：接代练通侧订单要求模板（2026-10-30 修复）', () => {
  /** 模板行合成的两条 id（与 TEMPLATES 一致）。 */
  const DEFAULT_ID = '60024'
  const OTHER_ID = '60025'

  function tempList(root: HTMLElement): HTMLSelectElement {
    const el = root.querySelector<HTMLSelectElement>(TEMPLATE_LIST_SELECTOR)
    if (!el) throw new Error('证据件里应当有 select#tempList')
    return el
  }

  function templateListCalls(): RequestConfig[] {
    return mockedRequest.mock.calls
      .map(([config]) => config as RequestConfig)
      .filter((config) =>
        String(config.url ?? '').includes('/platform-integrations/order-templates/'),
      )
  }

  /** 像用户一样在下拉里选一条（layui 原生 select 被隐藏，只在 `form.on('select')` 里回值）。 */
  async function chooseTemplate(root: HTMLElement, value: string): Promise<void> {
    tempList(root).value = value
    LAYUI.formHandlers.get(`select(${TEMPLATE_LIST_FILTER})`)?.({ value })
    await flushAsync()
  }

  it('打开页面即 GET 代练通侧模板列表，下拉出现「不需要」之外的选项（默认项带 [默认] 前缀）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const calls = templateListCalls()
    expect(calls).toHaveLength(1)
    expect(String(calls[0]!.method).toLowerCase()).toBe('get')
    expect(calls[0]!.url).toBe(DAILIANTONG_ORDER_TEMPLATES_URL)
    const options = Array.from(tempList(root).options)
    expect(options[0]!.value).toBe(TEMPLATE_LIST_EMPTY_VALUE)
    expect(options[0]!.textContent).toBe('不需要')
    const texts = options.map((option) => option.textContent ?? '')
    expect(texts).toContain(`${TEMPLATE_LIST_DEFAULT_PREFIX}代练要求甲`)
    expect(texts).toContain('陪玩要求乙')
    expect(options.map((option) => option.value)).toContain(DEFAULT_ID)
    expect(options.map((option) => option.value)).toContain(OTHER_ID)
    cleanup()
  })

  it('选中一条模板 ⇒ requestContent 填成该模板正文；换选另一条 ⇒ 覆盖（对标语义）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const content = root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!
    await chooseTemplate(root, DEFAULT_ID)
    expect(content.value).toBe('接单后联系客服获取账号')
    await chooseTemplate(root, OTHER_ID)
    expect(content.value).toBe('时间不够可以加，接单后联系客服')
    cleanup()
  })

  it('选回「不需要」⇒ 正文不动（⛔ 不清空用户已选的内容）', async () => {
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    const content = root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')!
    await chooseTemplate(root, DEFAULT_ID)
    expect(content.value).toBe('接单后联系客服获取账号')
    await chooseTemplate(root, TEMPLATE_LIST_EMPTY_VALUE)
    expect(content.value).toBe('接单后联系客服获取账号')
    cleanup()
  })

  it('列表读取失败 ⇒ 下拉保持只有「不需要」+ 中文失败提示（⛔ 不静默装作没有模板）', async () => {
    routes.templates = new NetworkError('上游超时（合成）')
    const { root, cleanup } = await mountPage(ORDER_SEARCH)
    await flushAsync()
    expect(templateListCalls()).toHaveLength(1)
    const options = Array.from(tempList(root).options)
    expect(options).toHaveLength(1)
    expect(options[0]!.textContent).toBe('不需要')
    const messages = LAYUI.msg.mock.calls.map((call) => String(call[0]))
    expect(messages.some((text) => text.includes(TEMPLATE_LIST_FAILED_PREFIX))).toBe(true)
    cleanup()
  })
})
