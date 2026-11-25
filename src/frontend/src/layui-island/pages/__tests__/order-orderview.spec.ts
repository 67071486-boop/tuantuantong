// @vitest-environment jsdom
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
const splitMembersMock = vi.hoisted(() => vi.fn())
vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return { ...actual, fetchSplitMembers: splitMembersMock }
})

/**
 * layui 的**可控替身**（与 `emp-myemp.spec.ts` / `order-myorderlist.spec.ts` 同一手法）：
 * jsdom 里加载不了真的 layui，所以只把 `bootstrapIsland` 换成替身，`layer.msg` 的**入参**才可断言。
 *
 * ⚠️ **`initIslandElements` 与 `islandMsg` 用真实现**（`@/layui/island` 的
 * `importOriginal` 展开）：页签类名切换必须由**真 DOM 事件**驱动才算「驱动真交互」
 * （AGENTS.md §5 的 E-0061）——把它们也 mock 掉就只剩「看起来接线了」。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  /** 挂载期两个动作**真实发生的次序**（`bootstrap` = 挂样式，`artifact` = 取证据件）。 */
  const order: string[] = []
  const render = vi.fn()
  /**
   * T11b：代练通页签的四颗动作按钮要开 `layer.confirm` / `layer.prompt`，
   * 「去发单」要开 `layer.open`。替身**只记入参**、**不自动回调**——确认 / 取消由每条用例自己驱动，
   * 否则「取消 ⇒ 零请求」这条判据就没法测（回调一进来就等于用户点了确定）。
   */
  const confirm = vi.fn()
  const prompt = vi.fn()
  const open = vi.fn()
  const close = vi.fn()
  /**
   * `layer.open({type:1, content})` 的 `content` **必须是 jQuery 形状**
   * （vendored layui 对 object 内容会调 `.parents()` / `.data()`）；传裸 DOM 节点会在 layer 内部
   * 抛错、弹层建不完整（表现成「点了没反应」）。替身照真 layui 的形状返回 `{el, parents, data}`，
   * 让用例能钉住「content 不是裸节点」。
   */
  const $ = vi.fn((el: Element) => ({
    el,
    parents: () => ({ 0: undefined, length: 0 }),
    data: () => undefined,
  }))
  /** g3：从所有平台撤回的逐平台明细用 `layer.alert` 展示。 */
  const alert = vi.fn()
  /**
   * ⭐ 受限模式：行内「复制」键走 `layui.lay.clipboard.writeText`
   * （与全量模式 `#copybtn` 同一个出口，见生产代码 `mountRestrictedOrderViewPage`）。
   */
  const lay = { clipboard: { writeText: vi.fn() } }
  return {
    $,
    msg,
    order,
    render,
    confirm,
    prompt,
    open,
    close,
    alert,
    lay,
    form: { render, on: vi.fn() },
    layer: { msg, confirm, prompt, open, close, alert },
    // 本页要 `layui.table`（操作日志表）。本文件只钉「挂载不炸 + 字段区/页签」，
    // 表中数据的接线由 `order-orderview-logs.spec.ts` 断言 ⇒ 这里给最小替身。
    table: { render: vi.fn(), reloadData: vi.fn(), on: vi.fn() },
    element: { init: vi.fn() },
  }
})

/**
 * ⭐ 受限模式：403 失败块的「关闭」键走 `closeIslandLayer`（关掉承载本页的
 * 弹层）。这里整体 mock 掉——jsdom 里 `window.parent` 上没有 `layer`，真实现只会静默
 * 返回 false，「有没有被调」这条判据就钉不住。
 */
vi.mock('@/layui-island/layerHost', () => ({ closeIslandLayer: vi.fn() }))

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // ⚠️ 写成**真正被调用时才执行**的函数（不是立即求值的 Promise），否则 `order` 记不到时刻。
    bootstrapIsland: (() => {
      LAYUI.order.push('bootstrap')
      return Promise.resolve(LAYUI)
    }) as unknown as typeof actual.bootstrapIsland,
  }
})

import { BizError, HttpError, NetworkError } from '@/api/errors'
import { request } from '@/api/http'
// ⭐ 真机实测：18 = 已下架的码表字面值由本 spec 钉住（后端同值，见
// `src/tests/test_dailiantong_actions.py::RemoteStatusLabelTests`）。
import {
  DISPLAY_STATUS_LABELS,
  REMOTE_STATUS_LABELS,
} from '@/layui-island/platformPush'
import type { DailiantongPushRow } from '@/api/dailiantongPush'
import type { MyOrderDetail } from '@/api/myOrders'
import { fetchSplitMembers, type Order } from '@/api/orders'
import { closeIslandLayer } from '@/layui-island/layerHost'
import {
  applyPlatformWritePermission,
  resetPlatformWriteAllowedForTests,
  setPlatformWriteNodeShipped,
} from '@/layui-island/perms'
import {
  APPEND_PRICE_SUCCESS_MSG,
  PLATFORM_ACTION_SELECTORS,
  PLATFORM_UPLOAD_DRAG_SELECTOR,
  displayOrderNo,
  syncPlatformActionVisibility,
  fieldText,
  emptyNameDicts,
  LOCK_REASON_PROMPT_TITLE,
  mountOrderOrderViewPage,
  orderNoFromSearch,
  PLATFORM_ACTION_UNKNOWN_MSG,
  REPRICE_ALL_INVALID_MSG,
  REPRICE_ALL_PROMPT_TITLE,
  REPRICE_ALL_SUCCESS_MSG,
  REPUSH_LAYER_TITLE,
  RESTRICTED_CLOSE_BTN_CLASS,
  RESTRICTED_COPY_BTN_CLASS,
  RESTRICTED_COPY_FAIL_MSG,
  RESTRICTED_COPY_SUCCESS_MSG,
  RESTRICTED_FORBIDDEN_TEXT,
  RESTRICTED_QR_EMPTY_TEXT,
  RESTRICTED_RETRY_BTN_CLASS,
  restrictedHasGameAccount,
  STANDARD_ROW_LABELS,
  WITHDRAW_CANCEL_PENDING_MSG,
  WITHDRAW_CLOSED_MSG,
} from '../order-orderview'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderview.html'),
  'utf8',
)

const mockedRequest = vi.mocked(request)

/** 一条 20 位系统单号（**合成值**，不是任何真实单号）。 */
const ORDER_NO = '20260922120000000001'

/**
 * 一单真数据（字段与后端 `OrderSerializer`（`exclude = []`）同形）。
 * 值是**字面值夹具**：下面所有断言都写这些字面值，不写常量（AGENTS.md §5「断言要钉值」）。
 */
function makeOrder(overrides: Partial<Order> = {}): Order {
  return {
    order_no: ORDER_NO,
    has_dailiantong_history: true,
    status: 'COMPLETED',
    order_type: 'BOOST',
    game: 11,
    server: 22,
    category: 33,
    project: 44,
    project_text: '三角洲行动 排位上分',
    // ⭐ `Order` 有这一列（`OrderSerializer` 是 `exclude = []`）。
    split_id: '',
    content: '客户要求：从黄金打到星耀',
    demand: '优先晚上打',
    time_requirement: '3 天内完成',
    game_account: 'acc_demo_01',
    game_sub_account: 'sub_demo_02',
    game_role_name: '角色甲',
    current_info_1: 'info-1',
    current_info_2: 'info-2',
    customer_phone: '13000000000',
    customer_qq: 'qq_demo',
    customer_ww: 'ww_demo',
    total_price: '199.50',
    received_price: null,
    platform_price: '210.00',
    hall_price: null,
    refund_fee: '0.00',
    fee: null,
    payment_method: '微信',
    // `Order.payment_status` 是必填键（后端三档码，default `UNSETTLED`）⇒ 夹具补齐。
    payment_status: 'UNSETTLED',
    source: '淘宝',
    order_source: 7,
    source_order_no: '',
    sales_agent: '客服甲',
    is_urgent: true,
    remark: '客户要求保密',
    tag: 'VIP',
    created_by: '客服甲',
    updated_by: '打手乙',
    created_at: '2026-09-20T21:32:15+08:00',
    updated_at: '2026-09-21T09:05:00+08:00',
    allowed_transitions: [],
    // 批 F / F3 起详情响应多一个只读布尔键（后端 `OrderSerializer` 恒回它）。
    completed_requires_review: false,
    ...overrides,
  }
}

const ORDER = makeOrder()

/**
 * 一条**合成**的平台推送行（形状 = 后端 `DailiantongPushSerializer`；值全是字面夹具）。
 *
 * ⚠️ 平台单号用 `DLT-TEST-0001` 这种**明显合成**的形状：真单号是 19 位数字，
 * 进 `src/**` 会被 PII 守卫打红（AGENTS.md §5-C / E-0077）。
 */
function makePush(overrides: Partial<DailiantongPushRow> = {}): DailiantongPushRow {
  return {
    id: 51,
    order: 9001,
    platform: 10,
    serial_no: 'DLT-TEST-0001',
    state: 'sent',
    state_display: '已发出',
    price: '88.50',
    ensure1: '10.00',
    ensure2: '5.00',
    time_limit_hours: 24,
    ex_order_no: '',
    remote_status: 12,
    remote_cancel_status: 10,
    accept_uid: 'USR-SYNTH-TAKER',
    last_synced_at: '2026-10-27T09:30:00+08:00',
    last_error: '',
    attempts: 1,
    created_at: '2026-10-27T09:30:00+08:00',
    updated_at: '2026-10-27T09:30:00+08:00',
    ...overrides,
  }
}

const CONFIG_ROWS = {
  '/api/business/games/': [{ id: 11, name: '三角洲行动' }],
  '/api/business/servers/': [{ id: 22, name: '安卓微信区' }],
  '/api/business/categories/': [{ id: 33, name: '排位上分' }],
  '/api/business/projects/': [{ id: 44, name: '钻石到星耀' }],
}

/**
 * ⭐ 受限模式：一条 `MyOrderDetail` 夹具（形状 = 后端
 * `MyOrderDetailSerializer` 的**恰好 16 键**；值全为合成字面值，断言钉这些字面值）。
 */
function makeMyOrderDetail(overrides: Partial<MyOrderDetail> = {}): MyOrderDetail {
  return {
    order_no: ORDER_NO,
    source_order_no: '',
    status: 'IN_PROGRESS',
    game_name: '三角洲行动',
    server_name: '安卓微信区',
    project_text: '排位上分',
    content: '从黄金打到星耀',
    demand: '优先晚上打',
    time_requirement: '3 天内完成',
    game_account: 'acc_demo_01',
    game_sub_account: 'sub_demo_02',
    game_role_name: '角色甲',
    is_urgent: true,
    remark: '客户要求保密',
    claimed_at: '2026-10-28T09:30:00+08:00',
    temp_amount: '88.00',
    group_qr_url: null,
    ...overrides,
  }
}

type ReqConfig = { method?: string; url?: string; params?: Record<string, unknown>; data?: Record<string, unknown> }
type ReqImpl = (config: ReqConfig) => Promise<unknown>

/**
 * 默认后端：详情回夹具，4 个配置字典各回一行（FK 显示名字）。
 *
 * `pushes` = 该单的平台推送行（`GET …/platform-push/` 的响应）：缺省空数组 = 「本单尚未发到平台」。
 * 传 `Error` ⇒ 该端点按失败抛（用来钉「取数失败不许静默空白」）。
 */
function defaultImpl(
  order: Order | Error,
  pushes: DailiantongPushRow[] | Error = [],
): ReqImpl {
  return async (config) => {
    const url = config.url ?? ''
    // ⚠️ **必须排在详情那条前缀之前**：`/orders/<单号>/audit/` 与 `/platform-push/…` 也都以
    // `/api/business/orders/` 开头，不先接住它们就会把**订单对象**喂给日志 / 平台面板。
    if (url.endsWith('/audit/')) return []
    // 两块只读列表（留言历史 / 进度图）默认回**空**（= 确实没有），
    // 免得与「取数失败」混淆；要测失败 / 有值的用例在 `platformImpl` 里按后缀覆盖。
    if (url.endsWith('/platform-push/messages/')) return { messages: [], error: '' }
    if (url.endsWith('/platform-push/progress/')) return { progress: [], error: '' }
    // g3：截图返回形状与推送行不同（`{img, caption, url}`），必须排在通用动作正则之前。
    if (url.endsWith('/platform-push/screenshot/')) {
      return { img: 'tt/x/uploaded.png', caption: '', url: '' }
    }
    // g3：从所有平台撤回（路径是 `platform-pushes/` 复数，与推送行读面不同族）。
    if (url.endsWith('/platform-pushes/withdraw-all/')) {
      return { results: [], succeeded: 0, failed: 0, total: 0 }
    }
    if (
      /\/platform-push\/(withdraw|price|time|message|up-price|arbitration|value|off-sell|lock|blacklist|finish|edit|modify|amount)\/$/.test(
        url,
      )
    ) {
      return makePush()
    }
    if (url.endsWith('/platform-push/')) {
      if (pushes instanceof Error) throw pushes
      return pushes
    }
    if (url.startsWith('/api/business/orders/')) {
      if (order instanceof Error) throw order
      return order
    }
    // `fetchGames()` 先读 `/api/auth/me` 的 `user.game_ids` 再过滤全量字典。
    // 本夹具的游戏字典只有 id=11（上面 `CONFIG_ROWS`）⇒ 权限钉字面 [11]，
    // 否则游戏名映射为空、「游戏区服」那一格的字面值断言会红。
    // ⭐ 平台写按钮挂 `order.platform_write`；后端节点未上线时前端按
    //    `order.list` 降级（见 `@/layui-island/perms::canPlatformWrite`）⇒ 本夹具给这个节点。
    if (url === '/api/auth/me') return { user: { game_ids: [11], menu_permissions: ['order.list'] } }
    const rows = (CONFIG_ROWS as Record<string, unknown>)[url]
    if (rows !== undefined) return { items: rows, total: 1, page: 1, page_size: 200 }
    throw new Error(`未预期的 request：${url}`)
  }
}

/**
 * 在默认后端之上按 URL **后缀**覆盖若干出口（四颗动作按钮的 POST 都是 `/…/platform-push/xxx/`）。
 * 先匹配覆盖、再走 `defaultImpl`。
 */
function platformImpl(
  pushes: DailiantongPushRow[] | Error,
  overrides: Record<string, () => Promise<unknown>> = {},
): ReqImpl {
  const base = defaultImpl(ORDER, pushes)
  return async (config) => {
    const url = config.url ?? ''
    for (const suffix of Object.keys(overrides)) {
      if (url.endsWith(suffix)) return overrides[suffix]!()
    }
    return base(config)
  }
}

/** 一个可手动放行的 promise（钉「提交中按钮禁用 / 双击只发一次」）。 */
function deferredResponse(): { promise: Promise<unknown>; resolve: (v: unknown) => void } {
  let resolve!: (v: unknown) => void
  const promise = new Promise<unknown>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

/** 所有 `request` 调用里 URL 命中 `match` 的那些配置（断言方法 / 路径 / 载荷用）。 */
function sentRequests(match: (url: string) => boolean): ReqConfig[] {
  return mockedRequest.mock.calls
    .map((call) => call[0] as ReqConfig)
    .filter((config) => match(String(config.url ?? '')))
}

/** 平台页签那一条 `GET …/platform-push/` 的调用数（动作端点都以 `/xxx/` 结尾，不会混进来）。 */
function pushDetailCalls(): number {
  return sentRequests((url) => url.endsWith('/platform-push/')).length
}

/** 取最后一次 `layer.confirm` 的 (文案, 选项, 确定回调)。⚠️ 不用 `.at()`（TS lib 没到 ES2022）。 */
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

/** 取最后一次 `layer.prompt` 的 (选项, 确定回调)。 */
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

/** 取最后一次 `layer.open` 的入参（编辑表单 / 进度图大图都用它）。 */
function lastOpen(): Record<string, unknown> {
  const calls = LAYUI.open.mock.calls
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('layer.open 没有被调用')
  return (call[0] ?? {}) as Record<string, unknown>
}

/**
 * 取出 `layer.open` 的 `content` 里的 DOM 节点。
 *
 * 生产代码传的是 **jQuery 形状**（`layui.$(node)`），不是裸节点；
 * 这里两种形状都接住，但**只用于取节点**——「必须是 jQuery 形状」由专门的用例钉。
 */
function layerContentNode(content: unknown): HTMLElement {
  if (content instanceof HTMLElement) return content
  const el = (content as { el?: unknown } | null | undefined)?.el
  if (el instanceof HTMLElement) return el
  throw new Error('layer.open 的 content 既不是节点也不是 jQuery 形状')
}

/**
 * 断言 `layer.open` 的 `content` 是 layui 要的 **jQuery 形状**（有 `.parents` / `.data`），
 * 并返回那个节点。裸 DOM 节点会让 vendored layui 在 `creat` 里抛错（弹层点了没反应）。
 */
function expectLayerContentNode(content: unknown): HTMLElement {
  const obj = content as { parents?: unknown; data?: unknown } | null | undefined
  if (typeof obj?.parents !== 'function' || typeof obj?.data !== 'function') {
    throw new Error(
      'layer.open 的 content 不是 jQuery 形状（裸 DOM 节点会让 layui 抛错、弹层不出现）',
    )
  }
  return layerContentNode(content)
}

/** 取最后一次 `layer.alert` 的 (正文, 选项)。 */
function lastAlert(): { content: string; options: Record<string, unknown> } {
  const calls = LAYUI.alert.mock.calls
  const call = calls[calls.length - 1]
  if (call === undefined) throw new Error('layer.alert 没有被调用')
  return {
    content: String(call[0]),
    options: (call[1] ?? {}) as Record<string, unknown>,
  }
}

/**
 * 挂整页（取数走**真的** `@/api/orders` → 打桩的 `request`）。
 * `search` 默认带上页面真正会读的那个参数名（`order_no`）；
 * `impl` 缺省时 = `defaultImpl(order)`（不碰平台推送端点）。
 */
async function mountPage(
  order: Order | Error = ORDER,
  search = `?page=order-orderview&order_no=${ORDER_NO}`,
  impl?: ReqImpl,
): Promise<HTMLElement> {
  mockedRequest.mockImplementation((impl ?? defaultImpl(order)) as never)
  window.history.replaceState({}, '', `/layui-island/page.html${search}`)
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
  return root
}

/** 点「代练通」外层页签（真 DOM 事件；类名切换由 `initIslandElements` 的真实现负责）。 */
function clickPlatformTab(root: HTMLElement): void {
  const li = root.querySelector<HTMLElement>(
    '.layui-tab[lay-filter="tab1"] > .layui-tab-title > li[lay-id="dlt"]',
  )
  if (li === null) throw new Error('外层页签里没有 li[lay-id="dlt"]')
  li.click()
}

/**
 * 平台面板里某一行的**值**（第 1 个 `span.left` 的文本）。
 *
 * ⚠️ 按**证据件的中文标签**找行（与生产代码 `rowByLabel` 同一口径），而不是按 class 猜：
 * 标签就是用户在屏幕上看得到的那个键。
 */
function platformSpan(root: HTMLElement, label: string): string {
  const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')
  if (panel === null) throw new Error('页面上没有代练通面板')
  for (const el of Array.from(panel.querySelectorAll<HTMLElement>('label.itemTitle'))) {
    if ((el.textContent ?? '').trim() !== label) continue
    // ⚠️ 保证金两行在**同一个** `.layui-form-item` 里的两个 `.layui-inline` 中 ⇒ 先收到
    // `.layui-inline`（有就走它），否则「效率保证金」会读到同一行里「安全保证金」的 span。
    const scope =
      el.closest<HTMLElement>('.layui-inline') ?? el.closest<HTMLElement>('.layui-form-item')
    return (scope?.querySelector<HTMLElement>('span.left')?.textContent ?? '').trim()
  }
  throw new Error(`平台面板里找不到这一行：${label}`)
}

/** 某一行（按证据件的标签找）的**值 span** 文本。 */
function spanTexts(root: HTMLElement, label: string): string[] {
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('label.itemTitle'))) {
    if ((el.textContent ?? '').trim() !== label) continue
    const row = el.closest<HTMLElement>('.layui-form-item')
    return Array.from(row?.querySelectorAll<HTMLElement>('span.left') ?? []).map((s) =>
      (s.textContent ?? '').trim(),
    )
  }
  throw new Error(`证据件里找不到这一行：${label}`)
}

beforeEach(() => {
  mockedRequest.mockReset()
  vi.mocked(fetchSplitMembers).mockReset().mockResolvedValue([])
  LAYUI.msg.mockClear()
  LAYUI.render.mockClear()
  LAYUI.confirm.mockClear()
  LAYUI.prompt.mockClear()
  LAYUI.open.mockClear()
  LAYUI.close.mockClear()
  LAYUI.alert.mockClear()
  LAYUI.lay.clipboard.writeText.mockClear()
  vi.mocked(closeIslandLayer).mockClear()
  LAYUI.order.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-orderview.html')) {
        LAYUI.order.push('artifact')
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  // ⭐ 平台写动作的权限门是**模块级状态**（`@/layui-island/perms`）⇒ 每个用例收口，
  // 否则「没权限」那一支会把后面所有用例的按钮一起藏掉（还伪装成实现回归）。
  setPlatformWriteNodeShipped(false)
  resetPlatformWriteAllowedForTests()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('挂载顺序：**先挂 layui 样式，再塞页面标记**（E-0067）', () => {
  it('bootstrapIsland 先于取证据件（写反了就会闪一下没样式的页面）', async () => {
    await mountPage()
    expect(LAYUI.order).toEqual(['bootstrap', 'artifact'])
  })
})

describe('参数：从 URL 取 20 位系统单号（键名 order_no）', () => {
  it('orderNoFromSearch 只认 order_no，认不出的一律空串', () => {
    expect(orderNoFromSearch(`?page=order-orderview&order_no=${ORDER_NO}`)).toBe(ORDER_NO)
    expect(orderNoFromSearch('?page=order-orderview&orderId=123')).toBe('')
    expect(orderNoFromSearch('')).toBe('')
  })

  it('取数打的是 GET /api/business/orders/<单号>/（URL 里就是 URL 上那个单号）', async () => {
    await mountPage()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: `/api/business/orders/${ORDER_NO}/`,
    })
  })

  it('URL 上没有单号 ⇒ 不请求 + 明说缺参（不是静默白屏）', async () => {
    await mountPage(ORDER, '?page=order-orderview')
    expect(mockedRequest).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('缺少订单号参数（order_no）', { time: 6000 })
  })
})

describe('页头与「对标行」的字段区：按给定响应渲染（字面值）', () => {
  it('页头单号 = source_order_no 非空时用它，为空回落系统单号（对标那格是 19 位平台单号）', async () => {
    const root = await mountPage()
    expect(spanTexts(root, STANDARD_ROW_LABELS.orderNo)).toEqual([ORDER_NO])
    expect(document.title).toBe(`${ORDER_NO}订单详情`)

    document.body.innerHTML = ''
    const root2 = await mountPage(makeOrder({ source_order_no: '5111111111111111111' }))
    expect(spanTexts(root2, STANDARD_ROW_LABELS.orderNo)).toEqual(['5111111111111111111'])
    expect(document.title).toBe('5111111111111111111订单详情')
  })

  it('displayOrderNo 的两支（判据是「来源单号非空」，不是真值）', () => {
    expect(displayOrderNo(makeOrder())).toBe(ORDER_NO)
    expect(displayOrderNo(makeOrder({ source_order_no: 'X-1' }))).toBe('X-1')
  })

  it('游戏区服 / 角色信息 / 账号 / 项目 / 状态 逐格字面值', async () => {
    const root = await mountPage()
    // 游戏名 + `/` + 区服名，后三格留空（对标后四格语义未考证）
    expect(spanTexts(root, STANDARD_ROW_LABELS.gameServer)).toEqual([
      '三角洲行动',
      '/安卓微信区',
      '',
      '',
      '',
    ])
    // 角色信息：只填第 1 格
    expect(spanTexts(root, STANDARD_ROW_LABELS.roleInfo)).toEqual(['角色甲', '', '', ''])
    expect(spanTexts(root, STANDARD_ROW_LABELS.gameAccount)).toEqual(['acc_demo_01'])
    expect(spanTexts(root, STANDARD_ROW_LABELS.subAccount)).toEqual(['sub_demo_02'])
    // 项目分类取字典名；代练项目取 project_text（权威值）
    expect(spanTexts(root, STANDARD_ROW_LABELS.category)).toEqual(['排位上分'])
    expect(spanTexts(root, STANDARD_ROW_LABELS.project)).toEqual(['三角洲行动 排位上分'])
    // 徽章 = order_type 的显示值（本页唯一一枚徽章）
    expect(
      Array.from(root.querySelectorAll('span.layui-badge')).map((b) => b.textContent),
    ).toEqual(['代练'])
    // 订单状态取我方词表
    expect(spanTexts(root, STANDARD_ROW_LABELS.status)).toEqual(['已完成'])
  })

  it('我方没有对应数据的四行 + #desc 一律留空（不编）', async () => {
    const root = await mountPage()
    expect(spanTexts(root, STANDARD_ROW_LABELS.password)).toEqual([''])
    expect(spanTexts(root, STANDARD_ROW_LABELS.secret)).toEqual([''])
    // 订单计时：**时长格**恒空；第 2 格里的「还剩」是**对标原文的字面量**（不是我们填的值），
    // 证据件里那两格是 `<span class="left"></span>` + `<span class="left">还剩<span …></span></span>`
    // ⇒ 期望值就是 `['', '还剩']`。⛔ 填值逻辑不许去写这一行（写了就会把「还剩」抹掉）。
    expect(spanTexts(root, STANDARD_ROW_LABELS.timing)).toEqual(['', '还剩'])
    expect(spanTexts(root, STANDARD_ROW_LABELS.taker)).toEqual([]) // 那一行的值节点是 <a>，不是 span.left
    expect(
      root.querySelector('.layui-form-item a[href="javascript:void(0)"]')?.textContent,
    ).toBe('')
    // `指定电话` 那一行的 span 是证据件原文的 `class=""`（没有 `.left`）⇒ 收不到 span.left；
    // 但它本来就是空的，下面按整行文本再确认一次。
    expect(spanTexts(root, STANDARD_ROW_LABELS.assignPhone)).toEqual([])
    const assignRow = Array.from(root.querySelectorAll<HTMLElement>('label.itemTitle')).find(
      (el) => (el.textContent ?? '').trim() === STANDARD_ROW_LABELS.assignPhone,
    )?.closest<HTMLElement>('.layui-form-item')
    expect((assignRow?.textContent ?? '').trim()).toBe(STANDARD_ROW_LABELS.assignPhone)
    expect(root.querySelector<HTMLTextAreaElement>('#desc')?.value).toBe('')
  })

  it('代练项目的徽章：BOOST → 代练，COMPANION → 陪玩（枚举名 → 中文）', async () => {
    const root = await mountPage()
    expect(root.querySelector('span.layui-badge')?.textContent).toBe('代练')
    document.body.innerHTML = ''
    const root2 = await mountPage(makeOrder({ order_type: 'COMPANION' }))
    expect(root2.querySelector('span.layui-badge')?.textContent).toBe('陪玩')
  })

  it('联系电话那一格**留空**，且**不等于** customer_phone（同名不同义，编排方 2026-09-22 裁定）', async () => {
    // 夹具特意让 `customer_phone` 非空、且与「空」不同 —— 否则「不等于它」就是恒真、没有判别力
    // （本仓「跨租户的否定断言」同族教训：判据不能用恒真/恒假的取值构造）。
    const CUSTOMER_PHONE = '13777778888'
    const root = await mountPage(makeOrder({ customer_phone: CUSTOMER_PHONE }))
    // 正向：字面值空串（对标那一格装的是**虚拟电话**，我方没有虚拟号）
    expect(spanTexts(root, STANDARD_ROW_LABELS.contactPhone)).toEqual([''])
    // 反向：确实不是 `customer_phone` 的值（把填值写回 `order.customer_phone` ⇒ 上面一条与这一条都红）
    expect(spanTexts(root, STANDARD_ROW_LABELS.contactPhone)).not.toEqual([CUSTOMER_PHONE])
    // 本轮客户电话**不在别处显示**（业务方同日「完全照对标」；⛔ 不许为了「可见」搬到别的格）
    expect(root.textContent ?? '').not.toContain(CUSTOMER_PHONE)
  })

  it('order_type 为空时整枚徽章移除（不留绿色空壳）', async () => {
    const root = await mountPage(makeOrder({ order_type: '' }))
    expect(root.querySelectorAll('span.layui-badge').length).toBe(0)
  })

  it('`#orderId` / `#copybtn` 的标识被填成**系统单号**（不是页头那个平台单号）', async () => {
    const root = await mountPage(makeOrder({ source_order_no: '5111111111111111111' }))
    expect(root.querySelector<HTMLInputElement>('#orderId')?.value).toBe(ORDER_NO)
    expect(root.querySelector<HTMLElement>('#copybtn')?.dataset.id).toBe(ORDER_NO)
    // `data-clipboard-text` **永远保持证据件的空串**（对标实测也是空串，recon §9）：
    // T4 起复制动作走 **`layui.lay.clipboard.writeText`**（⚠️ **不是** `layui.util.clipboard`，
    // 实测 `util` 没有该键）、文本是**运行期自拼**的，
    // 所以这颗属性不需要（也不该）被写值——见 `order-orderview-buttons.spec.ts`。
    expect(root.querySelector<HTMLElement>('#copybtn')?.dataset.clipboardText).toBe('')
  })

  it('订单直达详情显示关联的罚款子单，链接仍走正常订单详情', async () => {
    const child = '20261116091500000001'
    const root = await mountPage(makeOrder({
      penalty_children: [{ order_no: child, status: 'COMPLETED' }],
    }))
    const links = root.querySelector('[data-penalty-order-links]')
    expect(links?.textContent ?? '').toContain('罚款子单')
    expect(links?.querySelector('a')?.getAttribute('href')).toBe(`/orders/${child}`)
  })

  it('页签：恰好两个 + 第一个 layui-this（「订单进度图」不注入）', async () => {
    const root = await mountPage()
    const lis = root.querySelectorAll(
      '.layui-tab[lay-filter="logFilter"] > .layui-tab-title > li',
    )
    expect(Array.from(lis).map((li) => (li.textContent ?? '').trim())).toEqual([
      '订单日志',
      '操作日志',
    ])
    expect(lis[0]?.classList.contains('layui-this')).toBe(true)
    expect(lis[1]?.classList.contains('layui-this')).toBe(false)
  })

  it('点第二个页签：**真 DOM** 的类名 / 面板切换（initIslandElements 的真实现）', async () => {
    const root = await mountPage()
    const tab = root.querySelector('.layui-tab[lay-filter="logFilter"]')!
    const lis = Array.from(tab.querySelectorAll<HTMLElement>(':scope > .layui-tab-title > li'))
    const items = Array.from(tab.querySelectorAll<HTMLElement>(':scope > .layui-tab-content > .layui-tab-item'))
    expect(lis.length).toBe(2)
    expect(items.length).toBe(2)

    lis[1]!.click()
    expect(lis[1]!.classList.contains('layui-this')).toBe(true)
    expect(lis[0]!.classList.contains('layui-this')).toBe(false)
    expect(items[1]!.classList.contains('layui-show')).toBe(true)
    expect(items[0]!.classList.contains('layui-show')).toBe(false)

    lis[0]!.click()
    expect(lis[0]!.classList.contains('layui-this')).toBe(true)
    expect(items[0]!.classList.contains('layui-show')).toBe(true)
  })
})

/**
 * 「订单详情」的「代练通」页签。
 *
 * 逐条钉：外层 2 个页签、面板内 2 个子页签、28 个字段标签**逐字**（含「最近同步 / 失败原因」两行）、动作按钮只提示「尚未开放」、
 * 值一律留空（⛔ 不用 0.00 / 假账号冒充）。
 */
describe('代练通页签（T8 静态标记 + T11b 接真）', () => {
  /** 代练通面板里 26 个 `.itemTitle` 标签（逐字）。 */
  const PLATFORM_LABELS = [
    '交易平台单号：',
    '交易平台：',
    '平台账号：',
    '系统单号：',
    '订单来源：',
    '旺旺：',
    '标题：',
    '发/接时间：',
    '订单价格：',
    '安全保证金：',
    '效率保证金：',
    '已结代练费：',
    '已赔保证金：',
    '撤销申请结算：',
    '撤销申请赔付：',
    '交易状态：',
    '撤销状态：',
    '要求时限：',
    '剩余时间：',
    // ⭐ 推送行里有、但对标那张平台详情页**没有**渲染的两行：
    // （`最近同步` / `失败原因` 是我方对账要用的信息）——登记在文件头「已知会不同」。
    '最近同步：',
    '失败原因：',
    '游戏密码：',
    '角色名：',
    '发单说明：',
    '是否锁定：',
    '对方昵称：',
    '对方QQ：',
    '对方手机：',
  ]

  it('外层页签恰好 2 个（本系统订单 / 代练通），点「代练通」切到它自己的面板（真 DOM）', async () => {
    const root = await mountPage()
    const outer = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tab1"]')!
    const titles = Array.from(outer.querySelectorAll<HTMLElement>(':scope > .layui-tab-title > li'))
    const items = Array.from(outer.querySelectorAll<HTMLElement>(':scope > .layui-tab-content > .layui-tab-item'))
    expect(titles.map((li) => (li.textContent ?? '').trim())).toEqual(['本系统订单', '代练通'])
    expect(titles[1]!.getAttribute('lay-id')).toBe('dlt')
    expect(titles[1]!.style.display).toBe('')
    expect(items).toHaveLength(2)

    titles[1]!.click()
    expect(titles[1]!.classList.contains('layui-this')).toBe(true)
    expect(items[1]!.classList.contains('layui-show')).toBe(true)
    expect(items[0]!.classList.contains('layui-show')).toBe(false)

    titles[0]!.click()
    expect(items[0]!.classList.contains('layui-show')).toBe(true)
  })

  it('旧版直达保留本系统订单页签，只在发过代练通的订单展示代练通页签', async () => {
    const root = await mountPage(makeOrder({ has_dailiantong_history: false }))
    const titles = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab[lay-filter="tab1"] > .layui-tab-title > li'))
    expect(titles.filter((title) => title.style.display !== 'none').map((title) => title.textContent?.trim())).toEqual(['本系统订单'])
  })

  it('面板内两个子页签：交易平台订单信息 / 截图（可切）', async () => {
    const root = await mountPage()
    const inner = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    const titles = Array.from(inner.querySelectorAll<HTMLElement>(':scope > .layui-tab-title > li'))
    const items = Array.from(inner.querySelectorAll<HTMLElement>(':scope > .layui-tab-content > .layui-tab-item'))
    expect(titles.map((li) => (li.textContent ?? '').trim())).toEqual(['交易平台订单信息', '截图'])
    expect(items).toHaveLength(2)
    // 「截图」页签里是上传区（对标原文的 `#uploadInput` / `.layui-upload-drag`）。
    expect(items[1]!.querySelector('#uploadInput')).not.toBeNull()
    expect(items[1]!.querySelector('.layui-upload-drag')).not.toBeNull()
    titles[1]!.click()
    expect(items[1]!.classList.contains('layui-show')).toBe(true)
  })

  it('28 个字段标签逐字对（顺序 = 对标渲染顺序 + T11b 追加的两行）', async () => {
    const root = await mountPage()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    const labels = Array.from(panel.querySelectorAll<HTMLElement>('label.itemTitle')).map((el) =>
      (el.textContent ?? '').trim(),
    )
    expect(labels).toEqual(PLATFORM_LABELS)
  })

  it('值一律留空：所有 `span.left` 都是空串（不拿 0.00 / 假账号冒充数据）', async () => {
    const root = await mountPage()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    const values = Array.from(panel.querySelectorAll<HTMLElement>('span.left')).map((span) =>
      (span.textContent ?? '').trim(),
    )
    expect(values.length).toBeGreaterThan(20)
    expect(values.every((v) => v === '')).toBe(true)
    // 也没有隐藏域（对标那 5 个隐藏域属于另一个 iframe 页的外壳，本版不搬）。
    expect(panel.querySelectorAll('input[type="hidden"]')).toHaveLength(0)
  })

  it('本版没接的动作按钮点了只提示「尚未开放」；「点我刷新当前页」保持原生 reload（不拦）', async () => {
    const root = await mountPage()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    LAYUI.msg.mockClear()
    // ⭐ 本用例钉两半：**文案**与「不假装成功」（零请求）。
    // ⚠️ `从平台撤回` / `改价` / `补时` / 留言「发送」/ `补款` / `全平台改价` / `重发此单` /
    //    `确认完单` / `编辑当前平台订单` / `全平台编辑订单` / `从所有平台撤回` / 截图上传区
    //    都已接真（判据各在各的用例组）⇒ 本用例只留**仍然没接**的：
    //    提交异常 / 申请撤销… / 修改密码 / 解除黑名单。
    // 从挂载完成后的这一刻开始计数（挂载本身会真的取详情 / 审计 / 四个配置字典）。
    mockedRequest.mockClear()

    // 仍未接的那颗：提交异常
    panel.querySelector<HTMLElement>('a.exception')!.click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('「提交异常」尚未开放', { time: 1500 })
    // ⚠️ dlt-d：`a.applyArbitration` 已接真（申请客服介入）⇒ 本用例改用
    // 仍未接的「解除黑名单」继续钉「没接的按钮只提示、零请求」这条口径。
    panel.querySelector<HTMLElement>('a.removeBlackRnameBtn')!.click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('「解除黑名单」尚未开放', { time: 1500 })
    // 上游没有「取消仲裁」动作 ⇒ 仍然是「尚未开放」（g3 不改它）。
    panel.querySelector<HTMLElement>('a.unapplyArbitration')!.click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('「取消仲裁」尚未开放', { time: 1500 })

    // 刷新那颗**不**被截：`defaultPrevented === false`（href 原样交给浏览器 / island iframe）。
    // ⚠️ 用 document 冒泡阶段探针读 `defaultPrevented`，然后在**探针里** preventDefault：
    //    探针在页面的 root 捕获处理器之后跑 ⇒ 读到的就是「页面有没有拦」；顺手拦住 jsdom 的
    //    「Not implemented: navigation」噪音（那行是 jsdom 对 `javascript:` href 的限制，不是页面问题）。
    LAYUI.msg.mockClear()
    const refresh = panel.querySelector<HTMLAnchorElement>('a[href^="javascript:location.reload"]')!
    let pagePrevented: boolean | null = null
    const probe = (event: Event): void => {
      pagePrevented = event.defaultPrevented
      event.preventDefault()
    }
    document.addEventListener('click', probe)
    const event = new MouseEvent('click', { bubbles: true, cancelable: true })
    refresh.dispatchEvent(event)
    document.removeEventListener('click', probe)
    expect(pagePrevented).toBe(false)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    // ⭐ 零请求：确认完单 / 截图上传 / 刷新当前页
    // 点了都**不得发任何 API 请求**——「只提示、不改本地状态、不打后端」（D-0025 §5）。
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('底部 6 颗按钮的文案逐字（平台动作一颗不落）', async () => {
    const root = await mountPage()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    const bottom = panel.querySelector<HTMLElement>('.btnbox')!
    const labels = Array.from(bottom.querySelectorAll<HTMLElement>('a.layui-btn')).map((a) =>
      (a.textContent ?? '').trim(),
    )
    expect(labels).toEqual([
      '点我刷新当前页',
      '编辑当前平台订单',
      '全平台编辑订单',
      '全平台改价',
      '重发此单',
      '从所有平台撤回此单',
    ])
  })
})

/**
 * ⭐ T11b：代练通页签**接真** —— 切页签取一次推送行、四个动作接真。
 *
 * 断言口径（AGENTS.md §5-G / E-0009、E-0024）：
 *  - **每条都用字面值**（单号 / 金额 / 中文 / 载荷），⛔ 不 import 生产常量当期望值；
 *  - 断言打在**真实出口**上：`request()` 的 method / url / data、`layer.*` 的入参；
 *  - 切页签与点按钮都走**真 DOM 事件**（`initIslandElements` 用的是真实现）。
 */
describe('代练通页签接真（T11b）', () => {
  /**
   * 挂页 → 点「代练通」→ 等到那一条 `GET …/platform-push/` 回来。
   *
   * ⚠️ 挂载本身不取推送行（口径是「**切到页签时**才取」）⇒ `mockClear()` 放在点页签之前，
   * 下面每条用例的请求计数都从「点页签」这一刻算起。
   */
  async function mountAndLoad(
    pushes: DailiantongPushRow[] | Error,
    overrides: Record<string, () => Promise<unknown>> = {},
  ): Promise<HTMLElement> {
    const root = await mountPage(ORDER, undefined, platformImpl(pushes, overrides))
    mockedRequest.mockClear()
    clickPlatformTab(root)
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(1)
    })
    // ⚠️ 「请求被调用」≠「已经渲染进 DOM」：必须等到**这一次取数的结果**落到页面上，
    // 否则下面那些字面值断言会读到还没填的空 span（假红）。
    await vi.waitFor(() => {
      if (pushes instanceof Error) {
        expect(LAYUI.msg).toHaveBeenCalledWith(pushes.message, { time: 6000 })
        return
      }
      if (pushes.length > 0) {
        expect(platformSpan(root, '交易平台单号：')).not.toBe('')
        return
      }
      expect(statusBlock(root).style.display).not.toBe('none')
    })
    return root
  }

  /** 平台面板顶部那条状态区（空态 / 取数失败共用）。 */
  function statusBlock(root: HTMLElement): HTMLElement {
    const el = root.querySelector<HTMLElement>('.dltStatus')
    if (el === null) throw new Error('代练通面板里没有状态区（.dltStatus）')
    return el
  }

  it('切到「代练通」页签 ⇒ 恰好一次 GET，十个字段逐格取自响应字面值', async () => {
    const root = await mountAndLoad([makePush()])

    const gets = sentRequests((url) => url.endsWith('/platform-push/'))
    expect(gets).toHaveLength(1)
    expect(gets[0]!.method).toBe('get')
    expect(gets[0]!.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/`)

    // 值全部来自 `makePush()` 的字面夹具（⛔ 不是「元素存在」那种假断言）
    expect(platformSpan(root, '交易平台单号：')).toBe('DLT-TEST-0001')
    expect(platformSpan(root, '交易平台：')).toBe('代练通')
    expect(platformSpan(root, '订单价格：')).toBe('88.50')
    expect(platformSpan(root, '安全保证金：')).toBe('10.00')
    expect(platformSpan(root, '效率保证金：')).toBe('5.00')
    expect(platformSpan(root, '要求时限：')).toBe('24 小时')
    expect(platformSpan(root, '交易状态：')).toBe('正在代练')
    expect(platformSpan(root, '撤销状态：')).toBe('未撤销')
    expect(platformSpan(root, '最近同步：')).toBe('2026-10-27 09:30')
    expect(platformSpan(root, '失败原因：')).toBe('')
    // 有数据时那条空态 / 错误区不出现
    expect(statusBlock(root).style.display).toBe('none')
  })

  it('交易状态 / 撤销状态复用 platformPush.ts 的码表；未知码与 null 如实回落', async () => {
    // ⭐ h1：`CancelStatus=10`（未撤销）⇒ 交易状态仍按 `Status` 显示。
    const root = await mountAndLoad([
      makePush({ remote_status: 14, remote_cancel_status: 10, last_error: '上游返回：余额不足' }),
    ])
    expect(platformSpan(root, '交易状态：')).toBe('订单异常')
    expect(platformSpan(root, '撤销状态：')).toBe('未撤销')
    // 失败原因有值 ⇒ 红字（标签行自带 `color:#ff0000`），并显示上游原文
    expect(platformSpan(root, '失败原因：')).toBe('上游返回：余额不足')
    const errorRow = Array.from(root.querySelectorAll<HTMLElement>('label.itemTitle'))
      .find((el) => (el.textContent ?? '').trim() === '失败原因：')
      ?.closest<HTMLElement>('.layui-form-item')
    const errorSpan = errorRow?.querySelector<HTMLElement>('span.left')
    expect(errorSpan?.getAttribute('style')).toContain('color:#ff0000')

    // 还没回读过远端（remote_status=null）⇒ 用**本地 state 的中文**兜底（真数据，不编）
    document.body.innerHTML = ''
    const root2 = await mountAndLoad([
      makePush({
        state: 'failed',
        state_display: '发送失败',
        remote_status: null,
        remote_cancel_status: null,
      }),
    ])
    expect(platformSpan(root2, '交易状态：')).toBe('发送失败')
    expect(platformSpan(root2, '撤销状态：')).toBe('—')
  })

  /**
   * 远端 `Status=16`（撤销中）+
   * `CancelStatus=12`（协商已处理 = 打手已同意撤销）时，「交易状态」必须按**综合状态**显示，
   * ⛔ 不能只读 `remote_status`（只读它会显示「撤销中」）。
   *
   * 判据：**没有 `display_status_label` 这个键时**（旧回包 / 后端未升级），本地推导也必须把
   * `remote_cancel_status` 一起判：12/15/16 ⇒ 已撤销 / 已强制撤销；11 ⇒ 撤销中；
   * 14 ⇒ 客服介入中；13（撤销已取消）⇒ 按 `Status`。
   */
  it('h1：交易状态跟着 CancelStatus 走（12/15/16 ⇒ 已撤销；11/14 在途；13 按 Status）', async () => {
    const cases: Array<[number, number, string]> = [
      [16, 12, '已撤销'],
      [13, 15, '已撤销'],
      [16, 16, '已强制撤销'],
      [16, 11, '撤销中'],
      [16, 14, '客服介入中'],
      // 13 撤销已取消 ⇒ 回到按 Status 显示
      [16, 13, '撤销中'],
      [12, 13, '正在代练'],
      // 没有撤销流程（10 未撤销）⇒ 按 Status
      [16, 10, '撤销中'],
    ]
    for (const [remote, cancel, expected] of cases) {
      document.body.innerHTML = ''
      const root = await mountAndLoad([
        makePush({ remote_status: remote, remote_cancel_status: cancel }),
      ])
      expect(
        platformSpan(root, '交易状态：'),
        `Status=${remote} / CancelStatus=${cancel}`,
      ).toBe(expected)
    }
  })

  it('h1：后端下发 display_status_label 时优先用它（⛔ 页面不再自译）', async () => {
    const root = await mountAndLoad([
      makePush({
        remote_status: 16,
        remote_cancel_status: 12,
        display_status: 'revoked',
        display_status_label: '已撤销（后端下发）',
      }),
    ])
    expect(platformSpan(root, '交易状态：')).toBe('已撤销（后端下发）')
    // 「撤销状态」那一格仍逐字取码表（它表达的是原始码，不是综合状态）。
    expect(platformSpan(root, '撤销状态：')).toBe('协商已处理')
  })

  it('本地状态中文取后端下发的 `state_display`（前端不再自译第三份词表）', async () => {
    const root = await mountAndLoad([
      makePush({
        state: 'failed',
        state_display: '平台回读失败（后端下发）',
        remote_status: null,
      }),
    ])
    // ⛔ 不是前端 PUSH_STATE_LABELS 里的「发送失败」——后端已经给了中文就用它。
    expect(platformSpan(root, '交易状态：')).toBe('平台回读失败（后端下发）')
  })

  it('没有推送行 ⇒ 空态文案 + 「去发单」；点它开我们自己的发单页（零 API 请求）', async () => {
    const root = await mountAndLoad([])
    const status = statusBlock(root)
    expect(status.style.display).not.toBe('none')
    expect(
      (status.querySelector<HTMLElement>('.dltStatusText')?.textContent ?? '').trim(),
    ).toBe('本单尚未发到交易平台')
    const pushBtn = status.querySelector<HTMLElement>('a.dltPushBtn')
    expect(pushBtn, '空态里没有「去发单」按钮').not.toBeNull()
    expect((pushBtn!.textContent ?? '').trim()).toBe('去发单')

    mockedRequest.mockClear()
    pushBtn!.click()
    // 打开方式与订单列表那颗地球按钮一致：`layer.open` 装我们自己的 `order-pushjdp` 岛页
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.open.mock.calls[0]![0] as {
      title: string
      type: number
      skin: string
      area: [string, string]
      content: string
    }
    expect(opts.title).toBe(`${ORDER_NO}发单到交易平台：请确认发单内容`)
    expect(opts.type).toBe(2)
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.area[0]).toBe('1280px')
    expect(opts.content).toContain('layui-island/page.html?page=order-pushjdp')
    expect(opts.content).toContain(`order_no=${ORDER_NO}`)
    // 开层本身**不发接口**（发单页的提交在 iframe 里，归 `order-pushjdp`）
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('没有推送行时点四颗动作按钮 ⇒ 提示「先发单」、零请求（也不进弹层）', async () => {
    const root = await mountAndLoad([])
    mockedRequest.mockClear()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    for (const selector of [
      'a.cancelPlatorder',
      'a.changePlatorderPrice',
      'a.appenddPlatorderHour',
      'a.dltmsgBtn',
    ]) {
      panel.querySelector<HTMLElement>(selector)!.click()
      expect(LAYUI.msg).toHaveBeenLastCalledWith('本单尚未发到交易平台，请先发单', {
        time: 1500,
      })
    }
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('取数失败 ⇒ 可见错误提示（后端中文原文），⛔ 不静默空白', async () => {
    const root = await mountAndLoad(new BizError(1002, '订单不存在或不可见'))
    expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在或不可见', { time: 6000 })
    const status = statusBlock(root)
    expect(status.style.display).not.toBe('none')
    expect((status.textContent ?? '').trim()).toContain('加载失败：订单不存在或不可见')
  })

  it('已填过值之后重取失败 ⇒ 字段清空（不留旧值）+ 面板显示「加载失败：…」', async () => {
    // ⚠️ 「清空」的判据必须先成功填一次值（在从未填过的空字段上断「是空」是空转）。
    let detailCalls = 0
    const root = await mountPage(
      ORDER,
      undefined,
      platformImpl([], {
        '/platform-push/': async () => {
          detailCalls += 1
          if (detailCalls === 1) return [makePush()]
          throw new BizError(1002, '订单不存在或不可见')
        },
      }),
    )
    clickPlatformTab(root)
    await vi.waitFor(() => {
      expect(platformSpan(root, '交易平台单号：')).toBe('DLT-TEST-0001')
    })
    mockedRequest.mockClear()
    // 再切一次 = 重新取数 ⇒ 这一次失败
    clickPlatformTab(root)
    await vi.waitFor(() => {
      expect(platformSpan(root, '交易平台单号：')).toBe('')
    })
    expect(platformSpan(root, '订单价格：')).toBe('')
    expect(platformSpan(root, '交易状态：')).toBe('')
    expect(platformSpan(root, '失败原因：')).toBe('')
    expect((statusBlock(root).textContent ?? '').trim()).toContain('加载失败：订单不存在或不可见')
  })

  it('加载中（loading）点四颗动作按钮 ⇒ 提示「正在读取，请稍候」、零请求、不叠弹层', async () => {
    const deferred = deferredResponse()
    const root = await mountPage(
      ORDER,
      undefined,
      platformImpl([], { '/platform-push/': () => deferred.promise }),
    )
    clickPlatformTab(root) // 取数在途（deferred 还没放行）
    mockedRequest.mockClear()
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    panel.querySelector<HTMLElement>('a.changePlatorderPrice')!.click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('正在读取，请稍候', { time: 1500 })
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(mockedRequest).not.toHaveBeenCalled()
    deferred.resolve([])
  })

  it('取数失败（failed）点四颗动作按钮 ⇒ 提示「读取失败，请刷新后重试」、零请求', async () => {
    const root = await mountAndLoad(new BizError(1002, '订单不存在或不可见'))
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    mockedRequest.mockClear()
    panel.querySelector<HTMLElement>('a.cancelPlatorder')!.click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('读取失败，请刷新后重试', { time: 1500 })
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('连点页签两次也只发一次 GET（在途去重）', async () => {
    const deferred = deferredResponse()
    let calls = 0
    const root = await mountPage(
      ORDER,
      undefined,
      platformImpl([], {
        '/platform-push/': () => {
          calls += 1
          return deferred.promise
        },
      }),
    )
    clickPlatformTab(root)
    clickPlatformTab(root)
    expect(calls).toBe(1)
    deferred.resolve([])
    await vi.waitFor(() => {
      expect(statusBlock(root).style.display).not.toBe('none')
    })
  })

  it('弹层开着时连点同一颗按钮 ⇒ **只开一层**（评审 M-2：别叠两个弹层）', async () => {
    const root = await mountAndLoad([makePush()])
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')!
    const withdraw = panel.querySelector<HTMLElement>('a.cancelPlatorder')!
    withdraw.click()
    withdraw.click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)

    // 关掉那层确认（右上角 / 取消都会走 options.cancel）⇒ 闸放回去，再验证输入层同样只开一层
    ;(lastConfirm().options.cancel as () => void)()
    const time = panel.querySelector<HTMLElement>('a.appenddPlatorderHour')!
    time.click()
    time.click()
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
  })

  describe('从平台撤回', () => {
    it('先确认（文案说清「未接手删除退款 / 已接手协商撤销」）；取消 ⇒ 一个请求都不发', async () => {
      const root = await mountAndLoad([makePush()])
      root.querySelector<HTMLElement>('a.cancelPlatorder')!.click()
      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
      expect(lastConfirm().text).toBe(
        '未接手的单会直接删除并即时退款；已被接手的单只能申请协商撤销，能否撤回由对方决定。确定要从平台撤回这张单吗？',
      )
      // 用户点「取消」= 不调用确定回调 ⇒ 零请求
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(0)
    })

    it('未接手成功（后端回 state=closed）⇒ POST withdraw 载荷 {} → 说「已撤回并退款」→ 重新拉取并显示空态', async () => {
      let detailCalls = 0
      const root = await mountPage(
        ORDER,
        undefined,
        platformImpl([makePush()], {
          // 未接手撤回 = 后端删除成功、推送行落 `state=closed`；这里按**返回行**判话术。
          '/withdraw/': async () => makePush({ state: 'closed' }),
          // 第一次（切页签）有行；撤回成功后那一次回来是空的
          '/platform-push/': async () => {
            detailCalls += 1
            return detailCalls === 1 ? [makePush()] : []
          },
        }),
      )
      clickPlatformTab(root)
      await vi.waitFor(() => {
        expect(platformSpan(root, '交易平台单号：')).toBe('DLT-TEST-0001')
      })
      mockedRequest.mockClear()

      root.querySelector<HTMLElement>('a.cancelPlatorder')!.click()
      lastConfirm().yes(3)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/withdraw/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/withdraw/`)
      // 载荷**恰好**空对象（页面没有收事由；⛔ 不多送键）
      expect(call.data).toEqual({})
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(WITHDRAW_CLOSED_MSG)
      })
      expect(WITHDRAW_CLOSED_MSG).toBe('已撤回并退款')
      // 成功后重新拉一次：行没了 ⇒ 空态文案（不把已撤回的那行继续留在屏幕上）
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
      })
      expect(platformSpan(root, '交易平台单号：')).toBe('')
      expect((statusBlock(root).textContent ?? '').trim()).toContain('本单尚未发到交易平台')
    })

    it('已接手成功（返回行进协商态）⇒ 说「已提交协商撤销，等打手/客服处理」，⛔ 不说「已撤回」', async () => {
      const root = await mountAndLoad([makePush()], {
        '/withdraw/': async () => makePush({ state: 'sent', remote_cancel_status: 11 }),
      })
      root.querySelector<HTMLElement>('a.cancelPlatorder')!.click()
      lastConfirm().yes(3)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(WITHDRAW_CANCEL_PENDING_MSG)
      expect(WITHDRAW_CANCEL_PENDING_MSG).toBe('已提交协商撤销，等打手/客服处理')
      })
      const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
      expect(texts).not.toContain(WITHDRAW_CLOSED_MSG)
      expect(texts).not.toContain('撤回已完成')
      // 协商撤销不是终态 ⇒ 面板必须重新取一次（用户看到最新撤销状态）。
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(2)
      })
    })

    it('网络错误（不知道成没成）⇒ 说「已发出，请刷新核对」并强制重拉面板，⛔ 不谎报失败', async () => {
      const root = await mountAndLoad([makePush()], {
        '/time/': async () => {
          throw new NetworkError('网络断了')
        },
      })
      root.querySelector<HTMLElement>('a.appenddPlatorderHour')!.click()
      lastPrompt().yes('2', 13)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(PLATFORM_ACTION_UNKNOWN_MSG, { time: 6000 })
      })
      expect(PLATFORM_ACTION_UNKNOWN_MSG).toBe('已发出，请刷新核对')
      // 不知道后端有没有执行 ⇒ 必须重新取一次面板，让人看到真实状态。
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(2)
      })
      // 弹的是「请核对」而不是后端那句网络错误——否则用户会以为失败再点一次。
      const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
      expect(texts).not.toContain('网络断了')
    })

    it('失败 ⇒ 原样显示后端中文原文（不弹成功），按钮恢复可点', async () => {
      const root = await mountAndLoad([makePush()], {
        '/withdraw/': async () => {
          throw new BizError(1002, '该单已被接手，只能申请协商撤销')
        },
      })
      const btn = root.querySelector<HTMLElement>('a.cancelPlatorder')!
      btn.click()
      lastConfirm().yes(3)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('该单已被接手，只能申请协商撤销', { time: 6000 })
      })
      const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
      expect(texts.includes('撤回已完成')).toBe(false)
      // 后端「已提交但没核验到」也是同一个 1002 码 ⇒ 所有 failed 都必须重拉面板，
      // 否则屏幕停在旧值上，用户没法判断该不该重试。
      // 计数 = 切页签那一发 + 失败后重拉那一发。
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(2)
      })
      expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
    })

    it('提交中禁用按钮；连点两次只发一次 POST', async () => {
      const deferred = deferredResponse()
      const root = await mountAndLoad([makePush()], { '/withdraw/': () => deferred.promise })
      const btn = root.querySelector<HTMLElement>('a.cancelPlatorder')!
      btn.click()
      lastConfirm().yes(3)
      expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      // 第二次连点：弹层闸挡住（不叠第二层确认）；就算回调被再调一次也不得再发
      btn.click()
      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
      lastConfirm().yes(3)
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
      expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      deferred.resolve(makePush())
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
    })
  })

  describe('改价', () => {
    it('开输入层（取消 ⇒ 零请求）→ 确认后 POST price，载荷恰好 {"price":"2.00"}，成功提示 + 刷新', async () => {
      const root = await mountAndLoad([makePush()])
      const btn = root.querySelector<HTMLElement>('a.changePlatorderPrice')!
      btn.click()
      expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
      expect(lastPrompt().options.title).toBe('改价：请输入新的代练通价格（大于 0，最多两位小数）')
      // 还没点确定 = 取消 ⇒ 零请求
      expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(0)

      mockedRequest.mockClear()
      lastPrompt().yes('2.00', 11)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/price/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/price/`)
      expect(call.data).toEqual({ price: '2.00' })
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('改价已完成')
      })
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
      })
    })

    it.each(['0', '0.00', '-1', '1.234', 'abc'])(
      '金额「%s」不合法 ⇒ 零请求 + 页内提示',
      async (value) => {
        const root = await mountAndLoad([makePush()])
        mockedRequest.mockClear()
        root.querySelector<HTMLElement>('a.changePlatorderPrice')!.click()
        lastPrompt().yes(value, 12)
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '改价金额必须是大于 0 的数字（最多两位小数）',
          { time: 6000 },
        )
        expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(0)
      },
    )

    it('失败 ⇒ 原样显示后端中文原文', async () => {
      const root = await mountAndLoad([makePush()], {
        '/price/': async () => {
          throw new BizError(1002, '上游返回：只有未接手状态才能修改价格')
        },
      })
      root.querySelector<HTMLElement>('a.changePlatorderPrice')!.click()
      lastPrompt().yes('2.00', 12)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('上游返回：只有未接手状态才能修改价格', {
          time: 6000,
        })
      })
    })

    it('提交中禁用按钮；连点两次只发一次 POST', async () => {
      const deferred = deferredResponse()
      const root = await mountAndLoad([makePush()], { '/price/': () => deferred.promise })
      const btn = root.querySelector<HTMLElement>('a.changePlatorderPrice')!
      btn.click()
      lastPrompt().yes('2.00', 12)
      expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      btn.click()
      expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
      lastPrompt().yes('2.50', 13)
      expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(1)
      deferred.resolve(makePush())
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
    })
  })

  describe('补时', () => {
    it('开输入层（取消 ⇒ 零请求）→ 确认后 POST time，载荷恰好 {"hours":2}，成功提示 + 刷新', async () => {
      const root = await mountAndLoad([makePush()])
      const btn = root.querySelector<HTMLElement>('a.appenddPlatorderHour')!
      btn.click()
      expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
      expect(lastPrompt().options.title).toBe('补时：请输入要补的小时数（正整数）')
      expect(sentRequests((url) => url.endsWith('/time/'))).toHaveLength(0)

      mockedRequest.mockClear()
      lastPrompt().yes('2', 21)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/time/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/time/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/time/`)
      // 小时数必须是**数字**（后端 `IntegerField`），不是字符串
      expect(call.data).toEqual({ hours: 2 })
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('补时已完成')
      })
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
      })
    })

    it.each(['0', '-2', '1.5', 'abc'])(
      '小时数「%s」不合法 ⇒ 零请求 + 页内提示',
      async (value) => {
        const root = await mountAndLoad([makePush()])
        mockedRequest.mockClear()
        root.querySelector<HTMLElement>('a.appenddPlatorderHour')!.click()
        lastPrompt().yes(value, 22)
        expect(LAYUI.msg).toHaveBeenCalledWith('补时小时数必须是正整数', { time: 6000 })
        expect(sentRequests((url) => url.endsWith('/time/'))).toHaveLength(0)
      },
    )

    it('失败 ⇒ 原样显示后端中文原文', async () => {
      const root = await mountAndLoad([makePush()], {
        '/time/': async () => {
          throw new BizError(1002, '补时小时数不能超过 720 小时')
        },
      })
      root.querySelector<HTMLElement>('a.appenddPlatorderHour')!.click()
      lastPrompt().yes('900', 22)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('补时小时数不能超过 720 小时', { time: 6000 })
      })
    })

    it('提交中禁用按钮；连点两次只发一次 POST（补时是**增量**，重复会多加小时）', async () => {
      const deferred = deferredResponse()
      const root = await mountAndLoad([makePush()], { '/time/': () => deferred.promise })
      const btn = root.querySelector<HTMLElement>('a.appenddPlatorderHour')!
      btn.click()
      lastPrompt().yes('2', 22)
      expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      // 第二次连点：输入层不会再开（M-2 的弹层闸）；就算回调被再调一次也不得再发
      btn.click()
      expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
      lastPrompt().yes('3', 23)
      expect(sentRequests((url) => url.endsWith('/time/'))).toHaveLength(1)
      deferred.resolve(makePush())
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
    })
  })

  describe('留言', () => {
    it('直接读面板输入框 → POST message，载荷恰好 {"msg":"请尽快上号"}；成功后清空输入框并重取留言列表', async () => {
      const root = await mountAndLoad([makePush()])
      const input = root.querySelector<HTMLInputElement>('#dltmsginput')!
      input.value = '请尽快上号'
      mockedRequest.mockClear()
      root.querySelector<HTMLElement>('a.dltmsgBtn')!.click()
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/message/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/message/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/message/`)
      expect(call.data).toEqual({ msg: '请尽快上号' })
      // 不开第二层输入层（对标 `searchDltmsg` 是直接读输入框发）。
      expect(LAYUI.prompt).not.toHaveBeenCalled()
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('留言已提交')
      })
      // 成功后的两件事：**清空输入框** + **重取留言列表**（⛔ 不是重取推送行）。
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/messages/'))).toHaveLength(1)
      })
      expect(input.value).toBe('')
      expect(pushDetailCalls()).toBe(0)
    })

    it.each(['', '   '])('留言内容「%s」为空 ⇒ 零请求 + 页内提示', async (value) => {
      const root = await mountAndLoad([makePush()])
      root.querySelector<HTMLInputElement>('#dltmsginput')!.value = value
      mockedRequest.mockClear()
      root.querySelector<HTMLElement>('a.dltmsgBtn')!.click()
      expect(LAYUI.msg).toHaveBeenCalledWith('留言内容不能为空', { time: 6000 })
      expect(sentRequests((url) => url.endsWith('/message/'))).toHaveLength(0)
    })

    it('失败 ⇒ 原样显示后端中文原文；输入框保留内容（改完就能再发）', async () => {
      const root = await mountAndLoad([makePush()], {
        '/message/': async () => {
          throw new BizError(1002, '上游返回：留言内容不能超过 512 个字符')
        },
      })
      const input = root.querySelector<HTMLInputElement>('#dltmsginput')!
      input.value = '你好'
      mockedRequest.mockClear()
      root.querySelector<HTMLElement>('a.dltmsgBtn')!.click()
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('上游返回：留言内容不能超过 512 个字符', {
          time: 6000,
        })
      })
      expect(input.value).toBe('你好')
      // 失败不谎报成功、也不白花一次列表请求
      expect(LAYUI.msg).not.toHaveBeenCalledWith('留言已提交')
      expect(sentRequests((url) => url.endsWith('/messages/'))).toHaveLength(0)
    })

    it('提交中禁用按钮；连点两次只发一次 POST', async () => {
      const deferred = deferredResponse()
      const root = await mountAndLoad([makePush()], { '/message/': () => deferred.promise })
      const input = root.querySelector<HTMLInputElement>('#dltmsginput')!
      input.value = '你好'
      const btn = root.querySelector<HTMLElement>('a.dltmsgBtn')!
      btn.click()
      expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      // 在途时再点：不叠输入层、也不多发一发
      btn.click()
      expect(sentRequests((url) => url.endsWith('/message/'))).toHaveLength(1)
      deferred.resolve(makePush())
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
      await vi.waitFor(() => {
        expect(input.value).toBe('')
      })
    })
  })
})

/**
 * 平台留言历史 + 进度图两块读面接真。
 *
 * 断言口径与 T11b 相同：请求打到真出口、渲染用**字面值**；三态（加载中 / 空 / 失败）
 * 都必须**看得见**——后端读面失败是 HTTP 200 + `{error: 中文}`，所以「200 但 error 非空」
 * 也必须走失败态（⛔ 不许当成「没有留言」）。
 */
describe('平台留言历史 + 进度图（2026-10-28）', () => {
  const MESSAGES = [
    {
      time: '2026-10-27 10:00:00',
      text: '打手：已上号',
      is_system: false,
      sender: '接单者',
      uid: 'USR-SYNTH-GRABBER',
      nickname: '合成打手',
    },
    {
      time: '2026-10-27 10:05:00',
      text: '（系统自动回复·订单）进度已提交',
      is_system: true,
      sender: '发单者',
      uid: 'USR-SYNTH-PUBLISHER',
      nickname: '合成发单',
    },
  ]
  const PROGRESS = [
    {
      time: '2026-10-27 10:00:00',
      caption: '第一张进度',
      img: 'tt/x/one.png',
      url: 'https://example.test/one.png',
    },
    {
      time: '2026-10-27 10:05:00',
      caption: '第二张进度',
      img: 'tt/x/two.png',
      url: '',
    },
  ]

  function messagesBox(root: HTMLElement): HTMLElement {
    const box = root.querySelector<HTMLElement>('#dltlogdiv')
    if (box === null) throw new Error('代练通面板里没有留言容器 #dltlogdiv')
    return box
  }

  function messageImagesBox(root: HTMLElement): HTMLElement {
    const box = root.querySelector<HTMLElement>('.dltMsgImageList')
    if (box === null) throw new Error('代练通面板里没有留言图片容器 .dltMsgImageList')
    return box
  }

  function progressList(root: HTMLElement): HTMLElement {
    const list = root.querySelector<HTMLElement>('.platfromOrderImgList')
    if (list === null) throw new Error('代练通面板里没有进度图列表 .platfromOrderImgList')
    return list
  }

  it('切页签取一次留言读面：发送方 + 时间 + 正文逐条渲染，is_system 加「系统」标记', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => ({ messages: MESSAGES, error: '' }),
    })
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain('打手：已上号')
    })
    // 一次切页签 = 一发；两发就是重复取数
    expect(sentRequests((url) => url.endsWith('/messages/'))).toHaveLength(1)

    const box = messagesBox(root)
    // ⭐ 留言要显示**发送方**（后端 `sender`）。
    expect(
      Array.from(box.querySelectorAll<HTMLElement>('.dltMsgSender')).map((el) =>
        (el.textContent ?? '').trim(),
      ),
    ).toEqual(['接单者', '发单者'])
    expect(
      Array.from(box.querySelectorAll<HTMLElement>('.dltMsgTime')).map((el) =>
        (el.textContent ?? '').trim(),
      ),
    ).toEqual(['接单者 2026-10-27 10:00:00', '发单者 2026-10-27 10:05:00 系统'])
    expect(
      Array.from(box.querySelectorAll<HTMLElement>('.dltMsgText')).map((el) =>
        (el.textContent ?? '').trim(),
      ),
    ).toEqual(['打手：已上号', '（系统自动回复·订单）进度已提交'])
    // 只有系统那条带标记；普通留言不许被误标
    expect(box.querySelectorAll('.dltMsgSystem')).toHaveLength(1)
    expect(
      (box.querySelector<HTMLElement>('.dltMsgSystem')?.textContent ?? '').trim(),
    ).toBe('系统')
  })

  it('留言同一响应里的 images ⇒ 缩略图；点开看大图，url 为空显示「无预览」', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => ({
        messages: MESSAGES,
        images: PROGRESS,
        error: '',
      }),
    })
    await vi.waitFor(() => {
      expect(messageImagesBox(root).querySelectorAll('img.dltMsgImage')).toHaveLength(1)
    })
    // 有 url 的那张是可点缩略图，src 用后端给的现签 / 原样链接
    const thumb = messageImagesBox(root).querySelector<HTMLImageElement>(
      'img.dltMsgImage',
    )!
    expect(thumb.getAttribute('src')).toBe('https://example.test/one.png')
    // url 为空的那张是占位块（⛔ 不画 `<img src="">` 破图标）
    expect(messageImagesBox(root).querySelectorAll('.dltNoPreview')).toHaveLength(1)
    expect(
      (messageImagesBox(root).querySelector<HTMLElement>('.dltNoPreview')?.textContent ?? '').trim(),
    ).toBe('无预览')
    LAYUI.open.mockClear()
    thumb.click()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(lastOpen().title).toBe('第一张进度')
  })

  it('没有图片 ⇒ 显示「暂无图片」（⛔ 不静默空白）', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => ({ messages: MESSAGES, images: [], error: '' }),
    })
    await vi.waitFor(() => {
      expect((messageImagesBox(root).textContent ?? '').trim()).toBe('暂无图片')
    })
  })

  it('图片读失败 ⇒ 中文原因 + 「重试」；留言仍照常显示', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => ({
        messages: MESSAGES,
        images: [],
        images_error: '读取代练通进度图失败（上游提示：超时）。',
        error: '',
      }),
    })
    await vi.waitFor(() => {
      expect(messageImagesBox(root).textContent).toContain(
        '图片加载失败：读取代练通进度图失败（上游提示：超时）。',
      )
    })
    expect(messageImagesBox(root).querySelector('a.dltRetryBtn')).not.toBeNull()
    expect(messagesBox(root).textContent).toContain('打手：已上号')
  })

  it('没有留言 ⇒ 显示「暂无留言」（⛔ 不静默空白）', async () => {
    const root = await mountLoaded([makePush()])
    await vi.waitFor(() => {
      expect((messagesBox(root).textContent ?? '').trim()).toBe('暂无留言')
    })
  })

  it('加载中 ⇒ 显示「正在加载留言…」', async () => {
    const deferred = deferredResponse()
    const root = await mountLoaded([makePush()], {
      '/messages/': () => deferred.promise,
    })
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain('正在加载留言…')
    })
    deferred.resolve({ messages: [], error: '' })
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain('暂无留言')
    })
  })

  it('读面抛错 ⇒ 可见的中文原因 + 「重试」；⛔ 不显示「暂无留言」', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => {
        throw new BizError(1002, '读取代练通留言失败（上游提示：超时）。')
      },
    })
    const expected = '留言加载失败：读取代练通留言失败（上游提示：超时）。'
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain(expected)
    })
    expect(messagesBox(root).textContent).not.toContain('暂无留言')
    expect(messagesBox(root).querySelector('a.dltRetryBtn')).not.toBeNull()
  })

  it('读面 HTTP 200 但 error 非空 ⇒ 也算失败（⛔ 不许当成「没有留言」）', async () => {
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => ({
        messages: [],
        error: '这张单还没有抛到代练通（没有有效的推送记录），不能做平台读取。',
      }),
    })
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain('不能做平台读取')
    })
    expect(messagesBox(root).textContent).not.toContain('暂无留言')
  })

  it('失败后点「重试」⇒ 重新取数并渲染（重试按钮不是摆设）', async () => {
    let calls = 0
    const root = await mountLoaded([makePush()], {
      '/messages/': async () => {
        calls += 1
        if (calls === 1) {
          throw new BizError(1002, '读取代练通留言失败（上游提示：超时）。')
        }
        return { messages: MESSAGES, error: '' }
      },
    })
    await vi.waitFor(() => {
      expect(messagesBox(root).querySelector('a.dltRetryBtn')).not.toBeNull()
    })
    messagesBox(root).querySelector<HTMLElement>('a.dltRetryBtn')!.click()
    await vi.waitFor(() => {
      expect(messagesBox(root).textContent).toContain('打手：已上号')
    })
    expect(calls).toBe(2)
  })

  it('进度图：时间 / 说明 / 图片逐项渲染；url 为空显示「无预览」而不是破图标', async () => {
    const root = await mountLoaded([makePush()], {
      '/progress/': async () => ({ progress: PROGRESS, error: '' }),
    })
    const list = progressList(root)
    await vi.waitFor(() => {
      expect(list.querySelectorAll('li.dltProgressItem')).toHaveLength(2)
    })
    const items = Array.from(list.querySelectorAll<HTMLElement>('li.dltProgressItem'))
    // 有 url ⇒ 真 <img src="…">
    expect(items[0]!.querySelector('img')!.getAttribute('src')).toBe(
      'https://example.test/one.png',
    )
    expect((items[0]!.querySelector('.dltProgressTime')?.textContent ?? '').trim()).toBe(
      '2026-10-27 10:00:00',
    )
    expect((items[0]!.querySelector('.dltProgressCaption')?.textContent ?? '').trim()).toBe(
      '第一张进度',
    )
    // url 为空 ⇒ 占位文案，且**没有** img（`<img src="">` 会画破图标）
    expect(items[1]!.querySelector('img')).toBeNull()
    expect((items[1]!.querySelector('.dltNoPreview')?.textContent ?? '').trim()).toBe('无预览')
    expect(items[1]!.textContent).toContain('第二张进度')
    expect(items[1]!.textContent).toContain('2026-10-27 10:05:00')
  })

  it('没有进度图 ⇒ 显示「暂无进度图」', async () => {
    const root = await mountLoaded([makePush()])
    await vi.waitFor(() => {
      expect(progressList(root).textContent).toContain('暂无进度图')
    })
  })

  it('进度图读失败 ⇒ 中文原因 + 「重试」（不静默）', async () => {
    const root = await mountLoaded([makePush()], {
      '/progress/': async () => ({
        progress: [],
        error: '读取代练通进度图失败（上游提示：超时）。',
      }),
    })
    await vi.waitFor(() => {
      expect(progressList(root).textContent).toContain(
        '进度图加载失败：读取代练通进度图失败（上游提示：超时）。',
      )
    })
    expect(progressList(root).querySelector('a.dltRetryBtn')).not.toBeNull()
  })

  it('连点「代练通」页签两次 ⇒ 留言 / 进度图各只取一次（整块面板在途闸）', async () => {
    const deferred = deferredResponse()
    let messages = 0
    let progress = 0
    // 推送行那发挂住，两次点击都落在「整块还没跑完」的窗口里。
    const root = await mountPage(
      ORDER,
      undefined,
      platformImpl([makePush()], {
        '/platform-push/': () => deferred.promise,
        '/messages/': async () => {
          messages += 1
          return { messages: [], error: '' }
        },
        '/progress/': async () => {
          progress += 1
          return { progress: [], error: '' }
        },
      }),
    )
    clickPlatformTab(root)
    clickPlatformTab(root)
    // 三发在第一次点击里同步发出（第二次被整块在途闸挡掉）。
    expect(messages).toBe(1)
    expect(progress).toBe(1)
    expect(pushDetailCalls()).toBe(1)
    deferred.resolve([makePush()])
    await vi.waitFor(() => {
      expect(platformSpan(root, '交易平台单号：')).toBe('DLT-TEST-0001')
    })
    // 再放行一个回合：第二轮若真起了，这里会变成 2
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(messages).toBe(1)
    expect(progress).toBe(1)
  })
})

/**
 * ⭐ dlt-d：「有、我们没用上」的那批动作接真。
 *
 * 判据口径与 T11b 相同：载荷**恰好**、成功提示 + 重取面板、失败原样显示后端原文；
 * 另外钉「可见性按远端状态」——不该出现的方向不摆出来（状态闸仍由后端兜）。
 */
/** 挂页 + 切到「代练通」并等取数落到 DOM（dlt-d 与「第三组」两批动作共用）。 */
async function mountLoaded(
  pushes: DailiantongPushRow[] = [makePush()],
  overrides: Record<string, () => Promise<unknown>> = {},
): Promise<HTMLElement> {
  const root = await mountPage(ORDER, undefined, platformImpl(pushes, overrides))
  mockedRequest.mockClear()
  clickPlatformTab(root)
  await vi.waitFor(() => {
    expect(pushDetailCalls()).toBe(1)
  })
  await vi.waitFor(() => {
    if (pushes.length === 0) {
      expect(platformSpan(root, '交易平台单号：')).toBe('')
      return
    }
    expect(platformSpan(root, '交易平台单号：')).not.toBe('')
  })
  return root
}

function button(root: HTMLElement, selector: string): HTMLElement {
  const el = root.querySelector<HTMLElement>(selector)
  if (el === null) throw new Error(`按钮不存在：${selector}`)
  return el
}

function visible(root: HTMLElement, selector: string): boolean {
  return button(root, selector).style.display !== 'none'
}

/** 按钮是不是被置灰（提交中 / 后端说不能点）。 */
function disabled(root: HTMLElement, selector: string): boolean {
  return button(root, selector).classList.contains('layui-btn-disabled')
}

describe('代练通这批「有、我们没用上」的动作（dlt-d）', () => {
  /**
   * ⭐ 上游 `Status=18`（已下架）**不在**上游码表（`docs/DAILIANTONG-API.md` §5 只有 10–17）
   * ⇒ 码表必须补这一档，否则界面显示「未知（18）」。
   *
   * 判据 = **字面值**：`REMOTE_STATUS_LABELS[18]` 必须是「已下架」（⛔ 不许只改后端；
   * 后端 `dailiantong_actions.py::REMOTE_STATUS_LABELS` 由 `test_dailiantong_actions.py` 钉同值）。
   */
  it('⭐ 18 = 已下架：码表字面值 + 「交易状态」显示中文（⛔ 不是未知码）', async () => {
    expect(REMOTE_STATUS_LABELS[18]).toBe('已下架')
    const root = await mountLoaded([
      makePush({ remote_status: 18, remote_cancel_status: 10 }),
    ])
    expect(platformSpan(root, '交易状态：')).toBe('已下架')
  })

  /**
   * ⭐ `Status=15` 的语义是**被我们锁定**
   * （`LevelOrderLock(Locked=1)` 在 12/13/14 上执行 ⇒ 状态变 15；`Locked=0` 解锁回到
   * 锁定前的 12/13/14），**不是「帐号被平台锁」**。判据 = **字面值**：前后端两处码表都必须是「已锁定」。
   */
  it('⭐ 15 = 已锁定：码表字面值 + 「交易状态」显示中文', async () => {
    expect(REMOTE_STATUS_LABELS[15]).toBe('已锁定')
    expect(DISPLAY_STATUS_LABELS.locked).toBe('已锁定')
    const root = await mountLoaded([
      makePush({ remote_status: 15, remote_cancel_status: 10 }),
    ])
    expect(platformSpan(root, '交易状态：')).toBe('已锁定')
  })

  /**
   * ⭐ 18（已下架）时**只露「上架」**——它已经不在可接列表里，再下架没有
   * 意义（后端下架闸也只放 11）；「上架」正是把它放回可接列表的那个动作。
   * 同时 18 **不再算「已接手」** ⇒ 仲裁 / 拉黑不露；加价仍只在 11。
   */
  it('⭐ 18（已下架）只露「上架」：下架 / 加价 / 仲裁 / 拉黑都不露', async () => {
    const root = await mountLoaded([makePush({ remote_status: 18 })])
    expect(visible(root, 'a.onSellPlatorder')).toBe(true)
    expect(visible(root, 'a.offSellPlatorder')).toBe(false)
    expect(visible(root, 'a.upPlatorderPrice')).toBe(false)
    expect(visible(root, 'a.applyArbitration')).toBe(false)
    expect(visible(root, 'a.addBlackRnameBtn')).toBe(false)
  })

  it('⭐ 18：点「上架」发 flag=1（真机用同一方向把单上架回 11）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 18 })])
    button(root, 'a.onSellPlatorder').click()
    expect(lastConfirm().options.title).toBe('上架')
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/off-sell/'))).toHaveLength(1)
    })
    expect(sentRequests((url) => url.endsWith('/off-sell/'))[0]!.data).toEqual({
      flag: 1,
    })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('上架已完成')
    })
  })

  it('可见性：未接手只露加价 / 上下架；正在代练只露锁定 / 仲裁 / 拉黑；已结算只露评价', async () => {
    const unaccepted = await mountLoaded([makePush({ remote_status: 11 })])
    expect(visible(unaccepted, 'a.upPlatorderPrice')).toBe(true)
    expect(visible(unaccepted, 'a.offSellPlatorder')).toBe(true)
    expect(visible(unaccepted, 'a.onSellPlatorder')).toBe(true)
    expect(visible(unaccepted, 'a.lockBtn')).toBe(false)
    expect(visible(unaccepted, 'a.applyArbitration')).toBe(false)
    expect(visible(unaccepted, 'a.valuePlatorder')).toBe(false)
    expect(visible(unaccepted, 'a.addBlackRnameBtn')).toBe(false)

    // ⭐ h14：仲裁只在「协商撤销中（CancelStatus=11）」露出来
    // （`CancelStatus=10` 时撤销区只露「申请撤销」）。
    const inProgress = await mountLoaded([
      makePush({ remote_status: 12, remote_cancel_status: 11 }),
    ])
    expect(visible(inProgress, 'a.lockBtn')).toBe(true)
    expect(visible(inProgress, 'a.unlockBtn')).toBe(false)
    expect(visible(inProgress, 'a.applyArbitration')).toBe(true)
    expect(visible(inProgress, 'a.addBlackRnameBtn')).toBe(true)
    expect(visible(inProgress, 'a.upPlatorderPrice')).toBe(false)

    // ⭐ 真机口径：等待验收（13）/ 订单异常（14）**也露「锁定」**（14 上锁 ⇒ 变 15）。
    const acceptanceForLock = await mountLoaded([makePush({ remote_status: 13 })])
    expect(visible(acceptanceForLock, 'a.lockBtn')).toBe(true)
    expect(visible(acceptanceForLock, 'a.unlockBtn')).toBe(false)
    const abnormal = await mountLoaded([makePush({ remote_status: 14 })])
    expect(visible(abnormal, 'a.lockBtn')).toBe(true)
    expect(visible(abnormal, 'a.unlockBtn')).toBe(false)

    const locked = await mountLoaded([makePush({ remote_status: 15 })])
    expect(visible(locked, 'a.lockBtn')).toBe(false)
    expect(visible(locked, 'a.unlockBtn')).toBe(true)

    const settled = await mountLoaded([makePush({ remote_status: 17 })])
    expect(visible(settled, 'a.valuePlatorder')).toBe(true)
    expect(visible(settled, 'a.applyArbitration')).toBe(false)
    // 确认完单：证据件 `platfromorderview.js:1048-1051` 只在 Status=13（等待验收）时露出来
    const acceptance = await mountLoaded([makePush({ remote_status: 13 })])
    expect(visible(acceptance, 'a.completePlatOrder')).toBe(true)
    expect(visible(inProgress, 'a.completePlatOrder')).toBe(false)
    expect(visible(settled, 'a.completePlatOrder')).toBe(false)
  })

  it('拉黑：推送行没有 accept_uid 时不露按钮（后端也会拒，但不必先摆出来）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12, accept_uid: '' })])
    expect(visible(root, 'a.addBlackRnameBtn')).toBe(false)
  })

  it('加价：页内先挡非法金额；成功后载荷恰好 {"up_price":"2.50"}', async () => {
    const root = await mountLoaded([makePush({ remote_status: 11 })])
    const btn = button(root, 'a.upPlatorderPrice')
    btn.click()
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    expect(lastPrompt().options.title).toBe(
      '加价：请输入要加的金额（大于 0，最多两位小数）',
    )
    expect(sentRequests((url) => url.endsWith('/up-price/'))).toHaveLength(0)

    lastPrompt().yes('0', 1)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(
      '加价金额必须是大于 0 的数字（最多两位小数）',
      { time: 6000 },
    )
    expect(sentRequests((url) => url.endsWith('/up-price/'))).toHaveLength(0)

    btn.click()
    lastPrompt().yes('2.50', 2)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/up-price/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/up-price/'))[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/up-price/`)
    expect(call.data).toEqual({ up_price: '2.50' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('加价已完成')
    })
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(2)
    })
  })

  it('申请客服介入：确认后 POST 空载荷；失败原样显示后端中文', async () => {
    const root = await mountLoaded(
      [makePush({ remote_status: 12, remote_cancel_status: 11 })],
      {
        '/arbitration/': async () => {
          throw new BizError(1002, '订单异常，无法申请仲裁介入')
        },
      },
    )
    button(root, 'a.applyArbitration').click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(lastConfirm().options.title).toBe('申请客服介入')
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/arbitration/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/arbitration/'))[0]!
    expect(call.data).toEqual({})
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('订单异常，无法申请仲裁介入', { time: 6000 })
    })
  })

  it('评价：两步入参（满意度 10 + 内容），成功后载荷恰好两键', async () => {
    const root = await mountLoaded([makePush({ remote_status: 17 })])
    button(root, 'a.valuePlatorder').click()
    expect(lastPrompt().options.title).toContain('请输入满意度编号')
    lastPrompt().yes('10', 1)
    expect(lastPrompt().options.title).toBe('评价打手：请输入评价内容（可留空）')
    lastPrompt().yes('  打得不错  ', 2)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/value/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/value/'))[0]!
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/value/`)
    expect(call.data).toEqual({ eval_type: 10, comment: '打得不错' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('评价已提交')
    })
  })

  it('评价：内容留空时**不送 comment 键**（载荷恰好）；码值非法则零请求', async () => {
    const root = await mountLoaded([makePush({ remote_status: 17 })])
    const btn = button(root, 'a.valuePlatorder')
    btn.click()
    lastPrompt().yes('99', 1)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(
      '满意度编号只能是 10 / 11 / 12 / 13',
      { time: 6000 },
    )
    expect(sentRequests((url) => url.endsWith('/value/'))).toHaveLength(0)

    btn.click()
    lastPrompt().yes('12', 2)
    lastPrompt().yes('   ', 3)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/value/'))).toHaveLength(1)
    })
    expect(sentRequests((url) => url.endsWith('/value/'))[0]!.data).toEqual({
      eval_type: 12,
    })
  })

  it('上架 / 下架：confirm 后 POST flag 1 / 0，成功提示按方向', async () => {
    const root = await mountLoaded([makePush({ remote_status: 11 })])
    button(root, 'a.offSellPlatorder').click()
    expect(lastConfirm().options.title).toBe('下架')
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/off-sell/'))).toHaveLength(1)
    })
    expect(sentRequests((url) => url.endsWith('/off-sell/'))[0]!.data).toEqual({ flag: 0 })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('下架已完成')
    })

    button(root, 'a.onSellPlatorder').click()
    expect(lastConfirm().options.title).toBe('上架')
    lastConfirm().yes(2)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/off-sell/'))).toHaveLength(2)
    })
    expect(sentRequests((url) => url.endsWith('/off-sell/'))[1]!.data).toEqual({ flag: 1 })
  })

  it('锁定 / 解锁：原因可留空；填了就送 reason，没填就不送这个键（载荷恰好）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12 })])
    button(root, 'a.lockBtn').click()
    // 输入层的标题里把后果说清（锁定后打手不能继续提交进度 / 完单）
    expect(lastPrompt().options.title).toBe(LOCK_REASON_PROMPT_TITLE[1])
    lastPrompt().yes('   ', 1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/lock/'))).toHaveLength(1)
    })
    // 空 / 纯空白 ⇒ ⛔ 不送 `reason` 键（后端 reason 可选；送空串等于替后端做决定）
    expect(sentRequests((url) => url.endsWith('/lock/'))[0]!.data).toEqual({ locked: 1 })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已锁定')
    })

    const lockedRoot = await mountLoaded([makePush({ remote_status: 15 })])
    button(lockedRoot, 'a.unlockBtn').click()
    expect(lastPrompt().options.title).toBe(LOCK_REASON_PROMPT_TITLE[0])
    lastPrompt().yes('  客户确认已处理  ', 2)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/lock/'))).toHaveLength(1)
    })
    // 原因前后空白由 trim 收掉（后端也 trim；页面不留一份带空白的载荷）
    expect(sentRequests((url) => url.endsWith('/lock/'))[0]!.data).toEqual({
      locked: 0,
      reason: '客户确认已处理',
    })
  })

  it('⭐ 14（订单异常）也能锁定：点「锁定」发 locked=1（真机：14 上锁 ⇒ 15）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 14 })])
    button(root, 'a.lockBtn').click()
    expect(lastPrompt().options.title).toBe(LOCK_REASON_PROMPT_TITLE[1])
    lastPrompt().yes('', 9)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/lock/'))).toHaveLength(1)
    })
    expect(sentRequests((url) => url.endsWith('/lock/'))[0]!.data).toEqual({ locked: 1 })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已锁定')
    })
  })

  it('锁定 / 解锁：原因超过 128 字 ⇒ 页内先挡、零请求（与后端上限同值）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12 })])
    mockedRequest.mockClear()
    button(root, 'a.lockBtn').click()
    lastPrompt().yes('长'.repeat(129), 3)
    expect(LAYUI.msg).toHaveBeenCalledWith('锁定原因不能超过 128 个字符', { time: 6000 })
    expect(sentRequests((url) => url.endsWith('/lock/'))).toHaveLength(0)
  })

  it('锁定 / 解锁：失败原样显示后端中文原文', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12 })], {
      '/lock/': async () => {
        throw new BizError(
          1002,
          '只有「正在代练 / 等待验收 / 订单异常」的代练通订单才能锁定',
        )
      },
    })
    button(root, 'a.lockBtn').click()
    lastPrompt().yes('', 3)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '只有「正在代练 / 等待验收 / 订单异常」的代练通订单才能锁定',
        { time: 6000 },
      )
    })
  })

  it('拉黑打手：confirm 后 POST flag 0；载荷不带 UID（UID 由后端从推送行取）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12 })])
    button(root, 'a.addBlackRnameBtn').click()
    expect(lastConfirm().options.title).toBe('拉黑打手')
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/blacklist/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/blacklist/'))[0]!
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/blacklist/`)
    expect(call.data).toEqual({ flag: 0 })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已拉黑该打手')
    })
  })

  it('没有推送行时点新按钮 ⇒ 提示先发单、零请求（不进弹层）', async () => {
    const root = await mountLoaded([])
    button(root, 'a.applyArbitration').click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('本单尚未发到交易平台，请先发单', {
      time: 1500,
    })
    expect(LAYUI.confirm).not.toHaveBeenCalled()
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(sentRequests((url) => url.endsWith('/arbitration/'))).toHaveLength(0)
  })
})

/**
 * 第三组：证据件里**对标有、我方还没接**的那几个动作。
 *
 * 三个没有后端契约的（确认完单 / 编辑平台订单 / 截图上传）继续只提示「尚未开放」+ 零请求，
 * 契约缺口写在本组测试上方；能接的三颗（补款 / 全平台改价 / 重发此单）走真出口。
 */
describe('第三组：补款（amount/）/ 全平台改价 / 重发此单', () => {
  it('补款：开输入层 → POST amount，载荷恰好 {"amount":"2"}；成功提示 + 重取面板', async () => {
    const root = await mountLoaded([makePush({ remote_status: 13 })])
    const btn = button(root, 'a.appenddPlatorderPrice')
    btn.click()
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    // g3：补款改走 `amount/`，口径是**整数元 ≥ 1**（与改价 / 加价的两位小数不同）。
    expect(lastPrompt().options.title).toBe('补款：请输入要补的金额（整数元，至少 1 元）')
    expect(sentRequests((url) => url.endsWith('/amount/'))).toHaveLength(0)

    mockedRequest.mockClear()
    lastPrompt().yes('2', 1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/amount/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/amount/'))[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/amount/`)
    // 载荷**恰好** `{amount}`：⛔ 不再打 `up-price/`、也不送 `price` / `up_price`。
    expect(call.data).toEqual({ amount: '2' })
    expect(sentRequests((url) => url.endsWith('/up-price/'))).toHaveLength(0)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(APPEND_PRICE_SUCCESS_MSG)
    })
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(1)
    })
  })

  it.each(['0', '-1', '1.5', '1.234', 'abc', ''])(
    '补款金额「%s」不是整数元 / 小于 1 ⇒ 零请求 + 页内提示',
    async (value) => {
      const root = await mountLoaded([makePush({ remote_status: 12 })])
      mockedRequest.mockClear()
      button(root, 'a.appenddPlatorderPrice').click()
      lastPrompt().yes(value, 2)
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '补款金额必须是大于等于 1 的整数元（代练通不接受小数）',
        { time: 6000 },
      )
      expect(sentRequests((url) => url.endsWith('/amount/'))).toHaveLength(0)
      expect(sentRequests((url) => url.endsWith('/up-price/'))).toHaveLength(0)
    },
  )

  it('补款失败 ⇒ 原样显示后端中文原文 + 仍然重取面板（不把旧价留在屏幕上）', async () => {
    const root = await mountLoaded([makePush({ remote_status: 12 })], {
      '/amount/': async () => {
        throw new BizError(1002, '补款只能给「正在代练」或「待验收」的代练通订单做')
      },
    })
    button(root, 'a.appenddPlatorderPrice').click()
    lastPrompt().yes('2', 3)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '补款只能给「正在代练」或「待验收」的代练通订单做',
        { time: 6000 },
      )
    })
  })

  it('全平台改价：输入层标题逐字照证据件；按**平台**去重后 POST price，载荷恰好 {price}', async () => {
    // 撤回后重抛会留下**多条同平台**推送行（只有最新一条有效）⇒ 去重后只发一发
    const root = await mountLoaded([
      makePush({ id: 51, platform: 10 }),
      makePush({ id: 52, platform: 10 }),
    ])
    const btn = button(root, 'a.reChangePrice')
    btn.click()
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    // 证据件 `changePlatorderPriceAll(20)` 的 prompt 标题（逐字）
    expect(lastPrompt().options.title).toBe(REPRICE_ALL_PROMPT_TITLE)
    expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(0)

    mockedRequest.mockClear()
    lastPrompt().yes('3.00', 11)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/price/'))[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/price/`)
    expect(call.data).toEqual({ price: '3.00' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(REPRICE_ALL_SUCCESS_MSG)
    })
    // ⚠️ 这一条必须放在**成功之后**：`waitFor(长度=1)` 会在第一发刚发出时就放行
    // （两行同平台时不去重会发两发，只看中间态是测不出来的——变异实测过）。
    expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(1)
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(1)
    })
  })

  it.each(['0', '-3', '2.345', 'x'])(
    '全平台改价金额「%s」不合法 ⇒ 零请求 + 页内提示',
    async (value) => {
      const root = await mountLoaded([makePush()])
      button(root, 'a.reChangePrice').click()
      lastPrompt().yes(value, 12)
      expect(LAYUI.msg).toHaveBeenCalledWith(REPRICE_ALL_INVALID_MSG, { time: 6000 })
      expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(0)
    },
  )

  it('全平台改价失败 ⇒ 原样显示后端中文原文；提交中按钮禁用、连点只发一次', async () => {
    const deferred = deferredResponse()
    const root = await mountLoaded([makePush()], { '/price/': () => deferred.promise })
    const btn = button(root, 'a.reChangePrice')
    btn.click()
    lastPrompt().yes('3.00', 13)
    expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
    // 已开着层 / 在途时再点：不再开第二个输入层，也不会多发一发
    btn.click()
    expect(LAYUI.prompt).toHaveBeenCalledTimes(1)
    expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(1)
    deferred.resolve(makePush())
    await vi.waitFor(() => {
      expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
    })
  })

  it('重发此单：打开发单层（page=order-pushjdp + 20 位单号），标题照证据件；本页零 API', async () => {
    const root = await mountLoaded([makePush({ remote_status: 11 })])
    mockedRequest.mockClear()
    button(root, 'a.repushorder').click()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const options = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    // 证据件 `platfromorderview.js:783-804`：重发 = 打开 `/order/pushjdp`，标题「重发平台订单」
    expect(options.title).toBe(REPUSH_LAYER_TITLE)
    expect(String(options.content)).toContain('page=order-pushjdp')
    expect(String(options.content)).toContain(`order_no=${ORDER_NO}`)
    // 重发走的是发单页那套载荷（价格 / 双金 / 时限 / 区服由预览填）⇒ 本页**不发**任何请求
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('没有推送行时：全平台改价 / 重发 都只提示先发单、零请求（也不开层）', async () => {
    const root = await mountLoaded([])
    button(root, 'a.reChangePrice').click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('本单尚未发到交易平台，请先发单', {
      time: 1500,
    })
    button(root, 'a.repushorder').click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith('本单尚未发到交易平台，请先发单', {
      time: 1500,
    })
    expect(LAYUI.prompt).not.toHaveBeenCalled()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(sentRequests((url) => url.endsWith('/price/'))).toHaveLength(0)
  })
})

describe('g3：确认完单 / 从所有平台撤回 / 编辑平台订单 / 进度图大图 / 截图上传', () => {
  /** 合法 PNG 的**字节魔数**（后两个字节只是填充；前端只读前 8 字节）。 */
  function pngFile(name = '进度.png'): File {
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01, 0x02])
    return new File([bytes], name, { type: 'image/png' })
  }

  /** 把文件塞进页面运行期补的隐藏 file input，并触发 change。 */
  function pickFile(root: HTMLElement, file: File): void {
    const input = root.querySelector<HTMLInputElement>('input.dltUploadInput')
    if (input === null) throw new Error('页面上没有隐藏的截图 file input')
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change', { bubbles: true }))
  }

  /** 编辑弹窗里的表单（`layer.open` 的 `content`；生产代码传的是 jQuery 形状）。 */
  function editForm(): HTMLFormElement {
    const node = expectLayerContentNode(lastOpen().content)
    if (!(node instanceof HTMLFormElement)) throw new Error('编辑弹层里没有表单')
    return node
  }

  function setField(form: HTMLFormElement, name: string, value: string): void {
    const control = form.querySelector<HTMLInputElement | HTMLTextAreaElement>(
      `[name="${name}"]`,
    )
    if (control === null) throw new Error(`编辑表单里没有字段：${name}`)
    control.value = value
  }

  describe('确认完单', () => {
    it('确认框提示扣款和同步完成，取消时零请求', async () => {
      const root = await mountLoaded([makePush({ remote_status: 13 })])
      mockedRequest.mockClear()
      button(root, 'a.completePlatOrder').click()
      const confirm = lastConfirm()
      expect(confirm.text).toContain('从代练通余额扣款')
      expect(confirm.text).toContain('团团通会同步为已完成')
      expect(confirm.options.title).toBe('确认完单')
      ;(confirm.options.cancel as () => void)()
      expect(mockedRequest).not.toHaveBeenCalled()
    })

    it('确认完单后重取平台和本地订单，详情显示已完成', async () => {
      let accepted = false
      const orderUrl = `/api/business/orders/${ORDER_NO}/`
      const root = await mountLoaded([makePush({ remote_status: 13 })], {
        [orderUrl]: async () =>
          makeOrder({ status: accepted ? 'COMPLETED' : 'PENDING_REVIEW' }),
        '/platform-push/': async () => [
          makePush(accepted ? { remote_status: 17, state: 'closed' } : { remote_status: 13 }),
        ],
        '/finish/': async () => {
          accepted = true
          return makePush({ remote_status: 17, state: 'closed' })
        },
      })
      expect(spanTexts(root, STANDARD_ROW_LABELS.status)).toEqual(['待验收'])
      mockedRequest.mockClear()
      button(root, 'a.completePlatOrder').click()
      lastConfirm().yes(1)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/finish/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/platform-push/finish/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/finish/`)
      expect(call.data).toEqual({})
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('确认完单已完成')
      })
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
        expect(sentRequests((url) => url === orderUrl)).toHaveLength(1)
        expect(spanTexts(root, STANDARD_ROW_LABELS.status)).toEqual(['已完成'])
      })
    })

    it('平台已结算但本地回写失败时，提示同步错误并保留本地状态', async () => {
      const error = '本地订单状态回写失败：同步暂时失败'
      const root = await mountLoaded([makePush({ remote_status: 13 })], {
        [`/api/business/orders/${ORDER_NO}/`]: async () => makeOrder({ status: 'PENDING_REVIEW' }),
        '/finish/': async () => makePush({ remote_status: 17, last_error: error }),
      })
      button(root, 'a.completePlatOrder').click()
      lastConfirm().yes(1)

      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(`代练通已结算，${error}`, { time: 6000 })
        expect(spanTexts(root, STANDARD_ROW_LABELS.status)).toEqual(['待验收'])
      })
      expect(LAYUI.msg).not.toHaveBeenCalledWith('确认完单已完成')
    })

    it('失败 ⇒ 原样显示后端中文原文；按钮恢复可点', async () => {
      const root = await mountLoaded([makePush({ remote_status: 13 })], {
        '/finish/': async () => {
          throw new BizError(1002, '只有「等待验收」的代练通订单才能确认完单')
        },
      })
      const btn = button(root, 'a.completePlatOrder')
      btn.click()
      lastConfirm().yes(2)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '只有「等待验收」的代练通订单才能确认完单',
          { time: 6000 },
        )
      })
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
    })
  })

  describe('从所有平台撤回', () => {
    const PARTIAL = {
      results: [
        {
          platform: 10,
          platform_label: '代练通',
          serial_no: 'DLT-1',
          ok: true,
          state: 'closed',
          message: '撤回已完成：订单已删除并即时退款',
        },
        {
          platform: 20,
          platform_label: '某平台',
          serial_no: '',
          ok: false,
          state: 'sent',
          message: '本系统目前只对接了代练通，平台「某平台」的推送暂时不能从这里撤回。',
        },
      ],
      succeeded: 1,
      failed: 1,
      total: 2,
    }

    it('确认文案逐字照对标；取消 ⇒ 零请求', async () => {
      const root = await mountLoaded([makePush()])
      mockedRequest.mockClear()
      button(root, 'a.cancelAllPlatorder').click()
      const confirm = lastConfirm()
      expect(confirm.text).toBe('确定将该订单从所有平台撤回吗？')
      ;(confirm.options.cancel as () => void)()
      expect(mockedRequest).not.toHaveBeenCalled()
    })

    it('部分成功 ⇒ POST withdraw-all 载荷 {}；摘要如实说「部分平台撤回成功」+ 逐平台明细', async () => {
      const root = await mountLoaded([makePush()], {
        '/withdraw-all/': async () => PARTIAL,
      })
      mockedRequest.mockClear()
      button(root, 'a.cancelAllPlatorder').click()
      lastConfirm().yes(1)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-pushes/withdraw-all/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/platform-pushes/withdraw-all/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(
        `/api/business/orders/${ORDER_NO}/platform-pushes/withdraw-all/`,
      )
      expect(call.data).toEqual({})
      // 部分成功：摘要如实、⛔ 不谎称全部撤回。
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('部分平台撤回成功（成功 1 / 失败 1）')
      })
      const alert = lastAlert()
      expect(alert.options.title).toBe('撤回结果')
      expect(alert.content).toContain('代练通（DLT-1）：成功')
      expect(alert.content).toContain('撤回已完成：订单已删除并即时退款')
      expect(alert.content).toContain('某平台：失败')
      expect(alert.content).toContain('暂时不能从这里撤回')
      // 成功之后重取整块面板（推送行可能全消失 / 变协商撤销态）。
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
      })
    })

    it('整单失败（没有推送等）⇒ 原样显示后端中文；不弹「撤回结果」', async () => {
      const root = await mountLoaded([makePush()], {
        '/withdraw-all/': async () => {
          throw new BizError(1002, '这张单没有抛到任何平台，没有可撤回的推送。')
        },
      })
      button(root, 'a.cancelAllPlatorder').click()
      lastConfirm().yes(2)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '这张单没有抛到任何平台，没有可撤回的推送。',
          { time: 6000 },
        )
      })
      expect(LAYUI.alert).not.toHaveBeenCalled()
    })

    it('没有推送行 ⇒ 只提示先发单、零请求（也不开确认框）', async () => {
      const root = await mountLoaded([])
      mockedRequest.mockClear()
      button(root, 'a.cancelAllPlatorder').click()
      expect(LAYUI.msg).toHaveBeenLastCalledWith('本单尚未发到交易平台，请先发单', {
        time: 1500,
      })
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
    })
  })

  describe('编辑当前平台订单 / 全平台编辑订单', () => {
    it('当前平台：弹窗表单 → 只提交填了的项，载荷恰好 → POST edit', async () => {
      const root = await mountLoaded([makePush({ remote_status: 11 })])
      mockedRequest.mockClear()
      button(root, 'a.editPlatformOrder').click()
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      expect(lastOpen().title).toBe('编辑当前平台订单')
      const form = editForm()
      setField(form, 'title', '  新标题  ')
      setField(form, 'memo', '打手注意：晚上上号')
      ;(lastOpen().yes as (index: number) => void)(1)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/edit/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/platform-push/edit/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/edit/`)
      // 载荷**恰好**：只送填了的两项（标题 trim；没填的不送）。
      expect(call.data).toEqual({ title: '新标题', memo: '打手注意：晚上上号' })
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('编辑已提交')
      })
      await vi.waitFor(() => {
        expect(pushDetailCalls()).toBe(1)
      })
    })

    it('全平台：同一个表单 → POST modify（后端同一视图），载荷恰好', async () => {
      const root = await mountLoaded([makePush({ remote_status: 11 })])
      mockedRequest.mockClear()
      button(root, 'a.editPlatformOrderAll').click()
      expect(lastOpen().title).toBe('全平台编辑订单')
      const form = editForm()
      setField(form, 'requirement', '必须直播打')
      setField(form, 'zone_server_id', 'zone-9')
      ;(lastOpen().yes as (index: number) => void)(2)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/modify/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/platform-push/modify/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/modify/`)
      expect(call.data).toEqual({
        requirement: '必须直播打',
        zone_server_id: 'zone-9',
      })
    })

    it('一项都没填 ⇒ 页内中文提示、零请求、**层不关**（改完还能继续提交）', async () => {
      const root = await mountLoaded([makePush({ remote_status: 11 })], {
        '/edit/': async () => makePush({ remote_status: 11 }),
      })
      mockedRequest.mockClear()
      button(root, 'a.editPlatformOrder').click()
      ;(lastOpen().yes as (index: number) => void)(3)
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '这次没有任何要修改的内容，请至少填一项（标题 / 订单说明 / 代练要求 / 区服 / 频道 / 标签）',
        { time: 6000 },
      )
      expect(mockedRequest).not.toHaveBeenCalled()
      // ⭐ 空载荷**不许先关层**——用户可能只是还没填完 / 想补一项。
      // 层还在（`layer.close` 没被调、表单节点仍在 body）⇒ 填一项后同一个弹层还能提交。
      expect(LAYUI.close).not.toHaveBeenCalled()
      const form = editForm()
      expect(document.body.contains(form)).toBe(true)
      setField(form, 'title', '补一项标题')
      ;(lastOpen().yes as (index: number) => void)(4)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/edit/'))).toHaveLength(1)
      })
    })

    it('提交中按钮禁用；连点不会开第二个弹层 / 只发一次', async () => {
      const deferred = deferredResponse()
      const root = await mountLoaded([makePush({ remote_status: 11 })], {
        '/edit/': () => deferred.promise,
      })
      const btn = button(root, 'a.editPlatformOrder')
      btn.click()
      const form = editForm()
      setField(form, 'title', '新标题')
      ;(lastOpen().yes as (index: number) => void)(4)
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(true)
      })
      btn.click()
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      deferred.resolve(makePush())
      await vi.waitFor(() => {
        expect(btn.classList.contains('layui-btn-disabled')).toBe(false)
      })
      expect(sentRequests((url) => url.endsWith('/platform-push/edit/'))).toHaveLength(1)
    })

    it('后端拒绝 ⇒ 原样显示后端中文原文', async () => {
      const root = await mountLoaded([makePush({ remote_status: 12 })], {
        '/edit/': async () => {
          throw new BizError(1002, '只有未接手或已下架的订单才可以修改')
        },
      })
      button(root, 'a.editPlatformOrder').click()
      setField(editForm(), 'title', '新标题')
      ;(lastOpen().yes as (index: number) => void)(5)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('只有未接手或已下架的订单才可以修改', {
          time: 6000,
        })
      })
    })
  })

  describe('申请客服介入的可用条件（后端下发字段）', () => {
    const BLOCKED = '转入协商撤销状态1小时内才能申请仲裁介入（还需 37 分钟）'

    it('arbitration_available=false ⇒ 按钮置灰；点了显示后端原因、零请求', async () => {
      const root = await mountLoaded([
        makePush({
          remote_status: 12,
          remote_cancel_status: 11,
          arbitration_available: false,
          arbitration_blocked_reason: BLOCKED,
        }),
      ])
      const btn = button(root, 'a.applyArbitration')
      expect(visible(root, 'a.applyArbitration')).toBe(true)
      expect(disabled(root, 'a.applyArbitration')).toBe(true)
      mockedRequest.mockClear()
      btn.click()
      // 原因原文（含剩余分钟）由后端给，⛔ 页面不自己算。
      expect(LAYUI.msg).toHaveBeenCalledWith(BLOCKED, { time: 6000 })
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
    })

    it('arbitration_available=true ⇒ 按钮可点（点了开确认框）', async () => {
      const root = await mountLoaded([
        makePush({
          remote_status: 12,
          remote_cancel_status: 11,
          arbitration_available: true,
          arbitration_blocked_reason: '',
        }),
      ])
      expect(disabled(root, 'a.applyArbitration')).toBe(false)
      button(root, 'a.applyArbitration').click()
      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    })

    it('字段没下发（后端未升级）⇒ 保持可点，⛔ 不本地禁用', async () => {
      const push = makePush({ remote_status: 12, remote_cancel_status: 11 })
      delete (push as { arbitration_available?: boolean }).arbitration_available
      const root = await mountLoaded([push])
      expect(disabled(root, 'a.applyArbitration')).toBe(false)
    })
  })

  describe('进度图点开大图', () => {
    const PROGRESS = [
      {
        time: '2026-10-27 10:00:00',
        caption: '第一张进度',
        img: 'tt/x/one.png',
        url: 'https://example.test/one.png',
      },
    ]

    it('点缩略图 ⇒ layer.open 开大图（src / 标题来自该条记录）；零请求', async () => {
      const root = await mountLoaded([makePush()], {
        '/progress/': async () => ({ progress: PROGRESS, error: '' }),
      })
      const thumb = root.querySelector<HTMLImageElement>('img.dltProgressImage')
      expect(thumb).not.toBeNull()
      expect(thumb!.getAttribute('src')).toBe('https://example.test/one.png')
      LAYUI.open.mockClear()
      mockedRequest.mockClear()
      thumb!.click()
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      const options = lastOpen()
      expect(options.type).toBe(1)
      expect(options.title).toBe('第一张进度')
      const box = expectLayerContentNode(options.content)
      expect(box.querySelector('img')!.getAttribute('src')).toBe(
        'https://example.test/one.png',
      )
      expect(mockedRequest).not.toHaveBeenCalled()
    })

    it('url 为空的那一格是「无预览」占位块，点了不开大图', async () => {
      const root = await mountLoaded([makePush()], {
        '/progress/': async () => ({
          progress: [{ ...PROGRESS[0]!, url: '' }],
          error: '',
        }),
      })
      const item = root.querySelector<HTMLElement>('li.dltProgressItem')!
      expect(item.querySelector('img')).toBeNull()
      LAYUI.open.mockClear()
      item.querySelector<HTMLElement>('.dltNoPreview')!.click()
      expect(LAYUI.open).not.toHaveBeenCalled()
    })
  })

  describe('截图上传', () => {
    it('选择文件 ⇒ multipart 送 image + caption；成功提示 + 只重取进度图', async () => {
      const root = await mountLoaded([makePush()])
      const file = pngFile()
      const caption = root.querySelector<HTMLInputElement>('#imgRemark')!
      caption.value = '第三张进度'
      mockedRequest.mockClear()
      pickFile(root, file)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(1)
      })
      const call = sentRequests((url) => url.endsWith('/platform-push/screenshot/'))[0]!
      expect(call.method).toBe('post')
      expect(call.url).toBe(
        `/api/business/orders/${ORDER_NO}/platform-push/screenshot/`,
      )
      const form = call.data as unknown as FormData
      expect(form.get('image')).toBe(file)
      expect(form.get('caption')).toBe('第三张进度')
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('截图已追加')
      })
      // 成功只重取进度图；⛔ 不重取推送行。
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/progress/'))).toHaveLength(1)
      })
      expect(pushDetailCalls()).toBe(0)
      expect(caption.value).toBe('')
    })

    it('说明留空 ⇒ 不送 caption 键（载荷恰好，让后端用默认说明）', async () => {
      const root = await mountLoaded([makePush()])
      mockedRequest.mockClear()
      pickFile(root, pngFile())
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(1)
      })
      const form = sentRequests((url) => url.endsWith('/platform-push/screenshot/'))[0]!
        .data as unknown as FormData
      expect(form.get('image')).not.toBeNull()
      expect(form.get('caption')).toBeNull()
    })

    it('点上传区 ⇒ 打开隐藏的文件选择器（不再提示「尚未开放」）', async () => {
      const root = await mountLoaded([makePush()])
      const input = root.querySelector<HTMLInputElement>('input.dltUploadInput')!
      const clickSpy = vi.spyOn(input, 'click')
      LAYUI.msg.mockClear()
      button(root, '.layui-upload-drag').click()
      expect(clickSpy).toHaveBeenCalledTimes(1)
      expect(LAYUI.msg).not.toHaveBeenCalledWith('「截图上传」尚未开放', { time: 1500 })
    })

    it('拖拽落下 ⇒ 走同一条上传路（第一个文件）', async () => {
      const root = await mountLoaded([makePush()])
      mockedRequest.mockClear()
      const drop = new Event('drop', { bubbles: true, cancelable: true })
      Object.defineProperty(drop, 'dataTransfer', { value: { files: [pngFile('拖来的.png')] } })
      button(root, '.layui-upload-drag').dispatchEvent(drop)
      await vi.waitFor(() => {
        expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(1)
      })
    })

    it('不是 jpg / png（按字节魔数，⛔ 不信 File.type）⇒ 页内中文原因、零请求', async () => {
      const root = await mountLoaded([makePush()])
      mockedRequest.mockClear()
      const fake = new File([new Uint8Array([0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])], 'x.png', {
        type: 'image/png',
      })
      pickFile(root, fake)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('只支持 jpg / png 图片', { time: 6000 })
      })
      expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(0)
    })

    it('超过 18 MB ⇒ 页内先挡、零请求', async () => {
      const root = await mountLoaded([makePush()])
      mockedRequest.mockClear()
      const huge = new File([new Uint8Array(18 * 1024 * 1024 + 1)], 'huge.png', {
        type: 'image/png',
      })
      pickFile(root, huge)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('图片不能超过 18 MB', { time: 6000 })
      })
      expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(0)
    })

    it('上传中禁用上传区；在途时再选一个文件只发一次', async () => {
      const deferred = deferredResponse()
      const root = await mountLoaded([makePush()], {
        '/screenshot/': () => deferred.promise,
      })
      const drag = button(root, '.layui-upload-drag')
      mockedRequest.mockClear()
      pickFile(root, pngFile())
      await vi.waitFor(() => {
        expect(drag.classList.contains('layui-btn-disabled')).toBe(true)
      })
      pickFile(root, pngFile('第二张.png'))
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(sentRequests((url) => url.endsWith('/platform-push/screenshot/'))).toHaveLength(1)
      deferred.resolve({ img: 'tt/x/uploaded.png', caption: '', url: '' })
      await vi.waitFor(() => {
        expect(drag.classList.contains('layui-btn-disabled')).toBe(false)
      })
    })

    it('后端拒绝 ⇒ 原样显示后端中文原文', async () => {
      const root = await mountLoaded([makePush()], {
        '/screenshot/': async () => {
          throw new BizError(1002, '追加进度截图失败：上游提示：图片过大')
        },
      })
      pickFile(root, pngFile())
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('追加进度截图失败：上游提示：图片过大', {
          time: 6000,
        })
      })
    })
  })

  describe('补款可见性（12 与 13 都允许）', () => {
    it.each([12, 13])('remote_status=%i 时补款按钮仍然可见', async (status) => {
      const root = await mountLoaded([makePush({ remote_status: status })])
      expect(visible(root, 'a.appenddPlatorderPrice')).toBe(true)
    })
  })
})

/**
 * ⭐ 订单详情「代练通」页签的**撤销区**（撤销状态下那三颗）。
 *
 * 规则（按后端契约 `cancel_initiator` / `agree_cancel_available` / `arbitration_available`）：
 *  - `CancelStatus=10` 且远端可协商（`Status` 12–15）⇒ 只露「申请撤销」；
 *  - `CancelStatus=11`：打手发起 ⇒「同意撤销」（主按钮）+「申请仲裁」；我们发起 ⇒ 只「申请仲裁」
 *    并说明「等打手同意」；发起方判不出 ⇒ 两颗都露（点了由后端判，原样回中文）；
 *  - `CancelStatus ∈ {12,15,16}` ⇒ 两颗都收起，显示结果文案。
 * ⛔ **没有「拒绝协商」**（上游没有这个动作）——判据顺带钉「DOM 里也没有这颗」。
 */
describe('撤销区：申请撤销 / 同意撤销 / 申请仲裁（h14）', () => {
  const AGREE = 'a.unRevokeBtn'
  const APPLY = 'a.revokeBtn'
  const ARBITRATION = 'a.applyArbitration'
  /** ⭐ 「取消撤销」（取消我们自己发起的协商撤销申请）。 */
  const CANCEL_REQUEST = 'a.cancelUnRevokeBtn'

  /** 撤销区那句可读说明（我方追加的 `.dltCancelHint`）。 */
  function cancelHint(root: HTMLElement): string {
    const panel = root.querySelector<HTMLElement>('.layui-tab[lay-filter="tabimg"]')
    if (panel === null) throw new Error('页面上没有代练通面板')
    return (panel.querySelector<HTMLElement>('.dltCancelHint')?.textContent ?? '').trim()
  }

  /**
   * 撤销状态那一行里**可见**的按钮文案（按 DOM 顺序）——「四种组合」的判据就钉这个
   * **恰好集合**：多一颗（例如「拒绝协商」）或少一颗都会红。
   */
  function visibleCancelButtons(root: HTMLElement): string[] {
    const row = button(root, AGREE).closest<HTMLElement>('.layui-form-item')
    if (row === null) throw new Error('撤销区那一行找不到')
    return Array.from(row.querySelectorAll<HTMLElement>('a.layui-btn'))
      .filter((el) => el.style.display !== 'none')
      .map((el) => (el.textContent ?? '').trim())
  }

  /** 最近一次 `layer.open` 里的表单（「申请撤销」弹窗；生产代码传的是 jQuery 形状）。 */
  function lastOpenForm(): HTMLFormElement {
    const node = expectLayerContentNode(lastOpen().content)
    if (!(node instanceof HTMLFormElement)) throw new Error('弹层里没有表单')
    return node
  }

  /** 把弹层里的 HTML 正文还原成**屏幕上看到的文字**（确认框正文是转义过的 HTML）。 */
  function renderedText(html: string): string {
    const box = document.createElement('div')
    box.innerHTML = html
    return box.textContent ?? ''
  }

  /** 表单里的某个字段（读默认值 / 改值）。 */
  function formField(
    form: HTMLFormElement,
    name: string,
  ): HTMLInputElement | HTMLTextAreaElement {
    const el = form.querySelector<HTMLInputElement | HTMLTextAreaElement>(
      `[name="${name}"]`,
    )
    if (el === null) throw new Error(`表单里没有字段：${name}`)
    return el
  }

  it('CancelStatus=10 + 远端可协商（Status=12）⇒ 只露「申请撤销」', async () => {
    const root = await mountLoaded([
      makePush({ remote_status: 12, remote_cancel_status: 10 }),
    ])
    expect(visible(root, APPLY)).toBe(true)
    expect(visible(root, AGREE)).toBe(false)
    expect(visible(root, ARBITRATION)).toBe(false)
    expect(visibleCancelButtons(root)).toEqual(['申请撤销'])
    expect(cancelHint(root)).toBe('')
  })

  it('CancelStatus=11 + 打手发起 ⇒「同意撤销」（主按钮）+「申请仲裁」；写清不同意只能等 1 小时', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
        arbitration_available: false,
        arbitration_blocked_reason:
          '转入协商撤销状态1小时内才能申请仲裁介入（还需 37 分钟）',
      }),
    ])
    expect(visible(root, AGREE)).toBe(true)
    // 「同意撤销」是主按钮（证据件里它就是 `layui-btn-danger`）
    expect(button(root, AGREE).className).toContain('layui-btn-danger')
    expect(visible(root, APPLY)).toBe(false)
    expect(visible(root, ARBITRATION)).toBe(true)
    // **恰好两颗**：上游没有「拒绝协商」⇒ 页面不摆一颗点了没用的按钮
    expect(visibleCancelButtons(root)).toEqual(['同意撤销', '申请仲裁'])
    // 改成一句可读说明（没有 cancel_cmt / cancel_reason ⇒ 兜底文案 + 不同意怎么办）
    expect(cancelHint(root)).toContain('打手申请撤销，条件见留言')
    expect(cancelHint(root)).toContain('不同意只能等协商满 1 小时后申请客服介入')

    // 置灰 + 倒计时文案：只按后端字段，⛔ 页面不自己算时间
    expect(disabled(root, ARBITRATION)).toBe(true)
    mockedRequest.mockClear()
    button(root, ARBITRATION).click()
    expect(LAYUI.msg).toHaveBeenLastCalledWith(
      '转入协商撤销状态1小时内才能申请仲裁介入（还需 37 分钟）',
      { time: 6000 },
    )
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('CancelStatus=11 + 我们发起 ⇒「取消撤销」（次按钮）+「申请仲裁」，并说明「等打手同意」', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'us',
        arbitration_available: true,
      }),
    ])
    expect(visible(root, AGREE)).toBe(false)
    expect(visible(root, APPLY)).toBe(false)
    // 我们发起的协商撤销**可以自己撤回**（`LevelOrderCancel(Flag=1)`）
    expect(visible(root, CANCEL_REQUEST)).toBe(true)
    expect(button(root, CANCEL_REQUEST).className).not.toContain('layui-btn-danger')
    expect(visible(root, ARBITRATION)).toBe(true)
    expect(visibleCancelButtons(root)).toEqual(['取消撤销', '申请仲裁'])
    expect(cancelHint(root)).toBe(
      '已提交协商撤销，等打手同意；不同意只能等协商满 1 小时后申请客服介入',
    )
  })

  it('CancelStatus=11 + 打手发起 ⇒ 不给「取消撤销」（那是等我们同意 / 仲裁）', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
      }),
    ])
    expect(visible(root, CANCEL_REQUEST)).toBe(false)
    expect(visibleCancelButtons(root)).toEqual(['同意撤销', '申请仲裁'])
  })

  it('CancelStatus=11 + 发起方判不出 ⇒ 也不给「取消撤销」', async () => {
    const root = await mountLoaded([
      makePush({ remote_status: 16, remote_cancel_status: 11, cancel_initiator: '' }),
    ])
    expect(visible(root, CANCEL_REQUEST)).toBe(false)
  })

  it('CancelStatus=11 + 发起方判不出（空串）⇒ 两颗都露；「同意撤销」点了照常进确认框（由后端判）', async () => {
    const root = await mountLoaded([
      makePush({ remote_status: 16, remote_cancel_status: 11, cancel_initiator: '' }),
    ])
    expect(visible(root, AGREE)).toBe(true)
    expect(visible(root, ARBITRATION)).toBe(true)
    expect(visibleCancelButtons(root)).toEqual(['同意撤销', '申请仲裁'])
    button(root, AGREE).click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(lastConfirm().options.title).toBe('同意撤销')
  })

  it('后端明确 agree_cancel_available=false ⇒ 不摆「同意撤销」', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
        agree_cancel_available: false,
      }),
    ])
    expect(visible(root, AGREE)).toBe(false)
    expect(visible(root, ARBITRATION)).toBe(true)
  })

  it('CancelStatus ∈ {12,15,16} ⇒ 两颗都收起，显示结果文案', async () => {
    const cases: Array<[number, string]> = [
      [12, '撤销已处理：协商已处理'],
      [15, '撤销已处理：客服已处理'],
      [16, '撤销已处理：客服强制撤销'],
    ]
    for (const [cancel, hint] of cases) {
      document.body.innerHTML = ''
      const root = await mountLoaded([
        makePush({ remote_status: 16, remote_cancel_status: cancel }),
      ])
      expect(visible(root, AGREE), `CancelStatus=${cancel}`).toBe(false)
      expect(visible(root, APPLY), `CancelStatus=${cancel}`).toBe(false)
      expect(visible(root, ARBITRATION), `CancelStatus=${cancel}`).toBe(false)
      expect(visible(root, CANCEL_REQUEST), `CancelStatus=${cancel}`).toBe(false)
      expect(visibleCancelButtons(root), `CancelStatus=${cancel}`).toEqual([])
      expect(cancelHint(root), `CancelStatus=${cancel}`).toBe(hint)
    }
  })

  it('点「同意撤销」⇒ 确认框写清后果；取消零请求；确定后 POST agree-cancel 空载荷 → 成功刷新', async () => {
    const root = await mountLoaded(
      [
        makePush({
          remote_status: 16,
          remote_cancel_status: 11,
          cancel_initiator: 'taker',
        }),
      ],
      {
        '/agree-cancel/': async () =>
          makePush({ remote_status: 16, remote_cancel_status: 12 }),
      },
    )
    button(root, AGREE).click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(lastConfirm().options.title).toBe('同意撤销')
    // 确认框正文是 HTML（原文转义 + 可滚动排版）⇒ 断言屏幕上看到的文字
    expect(renderedText(lastConfirm().text)).toContain(
      '同意后平台会按详情现值支付代练费 / 赔偿保证金，且撤销不可撤回。确定要同意这次的撤销申请吗？',
    )
    // 取消 ⇒ 一个请求都不发
    expect(sentRequests((url) => url.endsWith('/agree-cancel/'))).toHaveLength(0)

    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/agree-cancel/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/agree-cancel/'))[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/agree-cancel/`)
    // 载荷**恰好**空对象（端点不收任何入参）
    expect(call.data).toEqual({})
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已同意撤销')
    })
    // 成功必须重取：撤销状态已经变了
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(2)
    })
  })

  it('同意撤销失败 ⇒ 原样显示后端中文，且**不刷新**面板', async () => {
    const root = await mountLoaded(
      [
        makePush({
          remote_status: 16,
          remote_cancel_status: 11,
          cancel_initiator: 'taker',
        }),
      ],
      {
        '/agree-cancel/': async () => {
          throw new BizError(1002, '订单状态异常，无法同意撤销')
        },
      },
    )
    button(root, AGREE).click()
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('订单状态异常，无法同意撤销', { time: 6000 })
    })
    // 业务方口径：失败原样显示后端中文、**不刷新**（只有成功才重取）
    expect(pushDetailCalls()).toBe(1)
    // 收尾后按钮恢复可点（别把一次失败留成永久禁用）
    expect(button(root, AGREE).classList.contains('layui-btn-disabled')).toBe(false)
  })

  it('点「取消撤销」⇒ 确认框写清后果；取消零请求；确定后 POST cancel-request 空载荷 → 成功刷新', async () => {
    const root = await mountLoaded(
      [
        makePush({
          remote_status: 16,
          remote_cancel_status: 11,
          cancel_initiator: 'us',
          arbitration_available: true,
        }),
      ],
      {
        '/cancel-request/': async () =>
          makePush({ remote_status: 14, remote_cancel_status: 10 }),
      },
    )
    button(root, CANCEL_REQUEST).click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    expect(lastConfirm().options.title).toBe('取消撤销')
    // 后果写清：撤销申请作废、订单回到原状态（真机实测回到 14/10，异常单原样还在）
    expect(lastConfirm().text).toBe(
      '取消后撤销申请作废，订单回到原状态。确定要取消这次撤销申请吗？',
    )
    // 取消 ⇒ 一个请求都不发
    expect(sentRequests((url) => url.endsWith('/cancel-request/'))).toHaveLength(0)

    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/cancel-request/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/cancel-request/'))[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(
      `/api/business/orders/${ORDER_NO}/platform-push/cancel-request/`,
    )
    // 载荷**恰好**空对象（`Flag=1` 不需要支付密码、不需要金额）
    expect(call.data).toEqual({})
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已取消撤销申请')
    })
    // 成功必须重取：撤销状态已经回到未撤销
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(2)
    })
  })

  it('取消撤销失败 ⇒ 原样显示后端中文，且**不刷新**面板', async () => {
    const root = await mountLoaded(
      [
        makePush({
          remote_status: 16,
          remote_cancel_status: 11,
          cancel_initiator: 'us',
        }),
      ],
      {
        '/cancel-request/': async () => {
          throw new BizError(1002, '当前状态不允许取消撤销')
        },
      },
    )
    button(root, CANCEL_REQUEST).click()
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('当前状态不允许取消撤销', { time: 6000 })
    })
    // 失败不刷新（只有成功才重取）
    expect(pushDetailCalls()).toBe(1)
    expect(button(root, CANCEL_REQUEST).classList.contains('layui-btn-disabled')).toBe(false)
  })

  it('两个新端点还没上线（HTTP 404）⇒ 说「已发出，请刷新核对」，⛔ 不谎报成功', async () => {
    // 后端另一条线还没合入时点下去就是 404/405 ⇒ 归入「结果未知」，不能被说成成功。
    const notDeployed = async (): Promise<unknown> => {
      throw new HttpError(404, 'Not Found')
    }
    const agreeRoot = await mountLoaded(
      [makePush({ remote_status: 16, remote_cancel_status: 11, cancel_initiator: 'taker' })],
      { '/agree-cancel/': notDeployed },
    )
    button(agreeRoot, AGREE).click()
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已发出，请刷新核对', { time: 6000 })
    })

    document.body.innerHTML = ''
    const cancelRoot = await mountLoaded(
      [makePush({ remote_status: 16, remote_cancel_status: 11, cancel_initiator: 'us' })],
      { '/cancel-request/': notDeployed },
    )
    button(cancelRoot, CANCEL_REQUEST).click()
    lastConfirm().yes(1)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已发出，请刷新核对', { time: 6000 })
    })
  })

  it('「撤销申请结算 / 赔付」两格：有值显示两位小数，没值显示「—」', async () => {
    const withValues = await mountLoaded([
      makePush({ pay_level_bal: '12.5', rep_ensure_bal: '5' }),
    ])
    expect(platformSpan(withValues, '撤销申请结算：')).toBe('12.50')
    expect(platformSpan(withValues, '撤销申请赔付：')).toBe('5.00')

    // 后端没下发这两个键（旧回包 / 还没回读）⇒ 不编 0.00，显示「—」
    document.body.innerHTML = ''
    const missing = await mountLoaded([makePush()])
    expect(platformSpan(missing, '撤销申请结算：')).toBe('—')
    expect(platformSpan(missing, '撤销申请赔付：')).toBe('—')

    // 显式 null / 空串同样回落「—」（判据是 is not None，不是真值）
    document.body.innerHTML = ''
    const blank = await mountLoaded([makePush({ pay_level_bal: '', rep_ensure_bal: null })])
    expect(platformSpan(blank, '撤销申请结算：')).toBe('—')
    expect(platformSpan(blank, '撤销申请赔付：')).toBe('—')
  })

  it('「已结代练费 / 已赔保证金」两格：有值显示两位小数，null / 缺键显示「—」', async () => {
    // ⭐ 后端新增 `settled_pay_level_bal` / `settled_rep_ensure_bal`
    // （只在 Status 17 / 16 给值，其它 null）⇒ 有值就显示、没值不编 0.00。
    const withValues = await mountLoaded([
      makePush({ settled_pay_level_bal: '66.5', settled_rep_ensure_bal: '3' }),
    ])
    expect(platformSpan(withValues, '已结代练费：')).toBe('66.50')
    expect(platformSpan(withValues, '已赔保证金：')).toBe('3.00')

    // 显式 null（Status 不是 17/16 时后端就是这么给的）⇒「—」
    document.body.innerHTML = ''
    const nulls = await mountLoaded([
      makePush({ settled_pay_level_bal: null, settled_rep_ensure_bal: null }),
    ])
    expect(platformSpan(nulls, '已结代练费：')).toBe('—')
    expect(platformSpan(nulls, '已赔保证金：')).toBe('—')

    // 键整个缺席（后端未升级 / 旧回包）⇒ 同样「—」，且页面不炸（走到这里就是没抛）
    document.body.innerHTML = ''
    const missing = await mountLoaded([makePush()])
    expect(platformSpan(missing, '已结代练费：')).toBe('—')
    expect(platformSpan(missing, '已赔保证金：')).toBe('—')
  })

  it('CancelStatus=11 + 打手发起 ⇒ 说明行与「同意撤销」确认框都看得到他要多少', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
        pay_level_bal: '12.50',
        rep_ensure_bal: '5.00',
      }),
    ])
    // 不用点任何东西就能看到（说明行）
    expect(cancelHint(root)).toContain('支付代练费 12.50')
    expect(cancelHint(root)).toContain('赔偿保证金 5.00')
    // 「同意撤销」这个入口也必须带金额（⛔ 不能只给一颗同意按钮、让人不知道在同意什么）
    button(root, AGREE).click()
    expect(lastConfirm().text).toContain('支付代练费 12.50')
    expect(lastConfirm().text).toContain('赔偿保证金 5.00')
    expect(lastConfirm().text).toContain('同意后平台会按详情现值支付代练费 / 赔偿保证金')
  })

  /**
   * ⭐ 代练通会拼一段「撤销要求原文」下发（`cancel_cmt`）。
   * 点「同意撤销」时必须**原样**看到它（⛔ 不改写、不截断；长文可滚动、默认可见前两行）；
   * 撤销区说明行也显示同一段。缺 `cancel_cmt` 退回 `cancel_reason`；都没有才用兜底文案。
   */
  const CANCEL_CMT =
    '接单者"新手USR20*****806325"申请撤销,需发单者支出:0.00, 接单者支出:0.00. 请尽快协商解决.尽量在代练通交互栏协商.撤销原因：好的'

  it('11 + 打手发起：说明行与「同意撤销」确认框逐字显示 cancel_cmt 原文', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
        cancel_cmt: CANCEL_CMT,
        pay_level_bal: '12.50',
        rep_ensure_bal: '5.00',
      }),
    ])
    // 撤销区说明行也显示这段（不用点开弹窗）
    expect(cancelHint(root)).toContain(CANCEL_CMT)
    // 确认框逐字（引号 / 逗号 / 句点一个不少；HTML 转义后屏幕上仍是原文）
    button(root, AGREE).click()
    const text = renderedText(lastConfirm().text)
    expect(text).toContain(CANCEL_CMT)
    // 金额与原文并列，方便核对
    expect(text).toContain('支付代练费 12.50')
    expect(text).toContain('赔偿保证金 5.00')
    // 确认框正文是**转义过**的 HTML（上游文本可含任意字符 ⇒ 不许拼成标签）
    expect(lastConfirm().text).not.toContain('<script')
  })

  it('缺 cancel_cmt ⇒ 退回 cancel_reason', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
        cancel_reason: '好的',
      }),
    ])
    expect(cancelHint(root)).toContain('好的')
    button(root, AGREE).click()
    const text = renderedText(lastConfirm().text)
    expect(text).toContain('好的')
    expect(text).not.toContain('打手申请撤销，条件见留言')
  })

  it('cancel_cmt / cancel_reason 都没有 ⇒ 兜底文案', async () => {
    const root = await mountLoaded([
      makePush({
        remote_status: 16,
        remote_cancel_status: 11,
        cancel_initiator: 'taker',
      }),
    ])
    expect(cancelHint(root)).toContain('打手申请撤销，条件见留言')
    button(root, AGREE).click()
    expect(renderedText(lastConfirm().text)).toContain('打手申请撤销，条件见留言')
  })

  /**
   * ⭐ 「申请撤销」必须真的开层。
   *
   * 根因形态 = `layer.open({type:1, content: 裸HTMLElement})`：vendored layui 对 object 内容走
   * jQuery 分支（`.parents()` / `.data()`）⇒ 裸节点直接抛错、层建不完整。
   * 判据（两条都要）：content 是 **jQuery 形状**；关层后节点从 `document.body` **移除**。
   */
  it('点「申请撤销」⇒ content 是 jQuery 形状；关层后节点从 body 移除（不残留）', async () => {
    const root = await mountLoaded([
      makePush({ remote_status: 12, remote_cancel_status: 10 }),
    ])
    button(root, APPLY).click()
    const options = lastOpen()
    expect(options.type).toBe(1)
    const node = expectLayerContentNode(options.content)
    // 生产代码先把节点挂进 body 再交给 layer（jQuery 分支要能 parents() 到东西）
    expect(document.body.contains(node)).toBe(true)
    // 三格是真表单（客服能填）
    expect(node.querySelectorAll('input')).toHaveLength(3)
    // layui 关层只 unwrap、不删节点 ⇒ 必须靠 `end` 清掉，否则每点一次就多一个残留
    ;(options.end as () => void)()
    expect(document.body.contains(node)).toBe(false)
  })

  it('点「申请撤销」⇒ 弹窗预填这两格；提交带上这两个值 + 事由 → 成功刷新', async () => {
    const root = await mountLoaded(
      [
        makePush({
          remote_status: 12,
          remote_cancel_status: 10,
          pay_level_bal: '12.50',
          rep_ensure_bal: '5.00',
        }),
      ],
      {
        '/withdraw/': async () =>
          makePush({ remote_status: 16, remote_cancel_status: 11 }),
      },
    )
    button(root, APPLY).click()
    expect(lastOpen().title).toBe('申请撤销')
    const form = lastOpenForm()
    // 默认值来自推送行的两个现值（客服看得到、也能改）
    expect(formField(form, 'pay_level_bal').value).toBe('12.50')
    expect(formField(form, 'rep_ensure_bal').value).toBe('5.00')
    expect(formField(form, 'comment').value).toBe('')
    expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(0)

    // 客服改一下金额 + 填事由 ⇒ 载荷恰好三键
    formField(form, 'pay_level_bal').value = '20.00'
    formField(form, 'rep_ensure_bal').value = '0'
    formField(form, 'comment').value = '  打手要求退回  '
    ;(lastOpen().yes as (index: number) => void)(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/withdraw/'))[0]!
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/withdraw/`)
    expect(call.data).toEqual({
      pay_level_bal: '20.00',
      rep_ensure_bal: '0',
      comment: '打手要求退回',
    })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已提交协商撤销，等打手/客服处理')
    })
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(2)
    })
  })

  it('两格没有下发值 ⇒「申请撤销」弹窗留空、提交仍是空载荷（后端按详情现值）', async () => {
    const root = await mountLoaded(
      [makePush({ remote_status: 12, remote_cancel_status: 10 })],
      {
        '/withdraw/': async () =>
          makePush({ remote_status: 16, remote_cancel_status: 11 }),
      },
    )
    button(root, APPLY).click()
    const form = lastOpenForm()
    expect(formField(form, 'pay_level_bal').value).toBe('')
    expect(formField(form, 'rep_ensure_bal').value).toBe('')
    ;(lastOpen().yes as (index: number) => void)(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
    })
    // 页面不替用户编金额：两个键都不送，交给后端用详情现值
    expect(sentRequests((url) => url.endsWith('/withdraw/'))[0]!.data).toEqual({})
  })

  /**
   * ⭐ 预填必须与后端「**最新活跃一笔**」对齐。
   *
   * 读面按 `-created_at, -id` 回（最新在前），但 `pay_level_bal` / `rep_ensure_bal` **只挂在
   * 该平台最新一条**有效**推送行**（`state ∈ pending/sent/unknown`）上，历史 / 结束行两格是
   * `null`。所以「最新一笔」恰好是历史行时，预填会凭空落空。
   * 判据：两行（新的一行已关闭、旧的一行仍活跃且有金额）⇒ 预填取**活跃那一行**。
   */
  it('⭐ L8：最新一笔是已关闭的历史行 ⇒ 撤销金额预填取**最新活跃那一笔**（不落空）', async () => {
    const olderActive = makePush({
      id: 50,
      state: 'sent',
      created_at: '2026-10-26T09:30:00+08:00',
      remote_status: 12,
      remote_cancel_status: 10,
      pay_level_bal: '12.50',
      rep_ensure_bal: '5.00',
    })
    const newestClosed = makePush({
      id: 52,
      state: 'closed',
      created_at: '2026-10-27T09:30:00+08:00',
      remote_status: 12,
      remote_cancel_status: 10,
      // 后端只给「最新活跃那一笔」下发这两格；历史 / 结束行是 null
      pay_level_bal: null,
      rep_ensure_bal: null,
    })
    const root = await mountLoaded([newestClosed, olderActive])
    button(root, APPLY).click()
    const form = lastOpenForm()
    expect(formField(form, 'pay_level_bal').value).toBe('12.50')
    expect(formField(form, 'rep_ensure_bal').value).toBe('5.00')
  })

  it('⭐ L8：一条活跃行都没有 ⇒ 两格留空要求手填（⛔ 不编 0、提交也不照发 0）', async () => {
    const root = await mountLoaded([
      makePush({
        state: 'closed',
        remote_status: 12,
        remote_cancel_status: 10,
        pay_level_bal: null,
        rep_ensure_bal: null,
      }),
    ])
    button(root, APPLY).click()
    const form = lastOpenForm()
    expect(formField(form, 'pay_level_bal').value).toBe('')
    expect(formField(form, 'rep_ensure_bal').value).toBe('')
    ;(lastOpen().yes as (index: number) => void)(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
    })
    // 两格都没填 ⇒ 载荷里不许出现任何一个金额键（更不许出现 `0`）
    const payload = sentRequests((url) => url.endsWith('/withdraw/'))[0]!.data as Record<
      string,
      unknown
    >
    expect(payload).not.toHaveProperty('pay_level_bal')
    expect(payload).not.toHaveProperty('rep_ensure_bal')
  })

  it('「申请撤销」弹窗里金额非法 ⇒ 页内先挡、零请求', async () => {
    const root = await mountLoaded(
      [makePush({ remote_status: 12, remote_cancel_status: 10 })],
    )
    button(root, APPLY).click()
    const form = lastOpenForm()
    formField(form, 'pay_level_bal').value = 'abc'
    ;(lastOpen().yes as (index: number) => void)(1)
    expect(LAYUI.msg).toHaveBeenLastCalledWith(
      '撤销申请金额必须是 0 或正数（最多两位小数）',
      { time: 6000 },
    )
    expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(0)
  })

  it('CancelStatus=10 点「申请撤销」⇒ 确认后 POST withdraw（载荷见上）→ 成功刷新', async () => {
    const root = await mountLoaded(
      [makePush({ remote_status: 12, remote_cancel_status: 10 })],
      {
        '/withdraw/': async () =>
          makePush({ remote_status: 16, remote_cancel_status: 11 }),
      },
    )
    button(root, APPLY).click()
    expect(lastOpen().title).toBe('申请撤销')
    expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(0)
    ;(lastOpen().yes as (index: number) => void)(1)
    await vi.waitFor(() => {
      expect(sentRequests((url) => url.endsWith('/withdraw/'))).toHaveLength(1)
    })
    const call = sentRequests((url) => url.endsWith('/withdraw/'))[0]!
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/platform-push/withdraw/`)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已提交协商撤销，等打手/客服处理')
    })
    await vi.waitFor(() => {
      expect(pushDetailCalls()).toBe(2)
    })
  })

  /**
   * ⭐ 弹层闸必须在**任何关闭路径**释放。
   *
   * 真 layui 的 `layer.msg` 内部会 `closeAll('dialog')`（type:0）⇒ 静默关掉 confirm，
   * **不回调** `cancel` / `yes`。只挂 `cancel` ⇒ `platformDialogOpen` 永真，
   * 之后所有平台动作按钮点了都会静默失效（处理器第一句 `if (platformDialogOpen) return`）。
   */
  it('confirm 开着时收到一次 layer.msg（layui 会静默关掉它）⇒ 闸释放、按钮还能再点', async () => {
    const root = await mountLoaded([
      makePush({ remote_status: 12, remote_cancel_status: 11, arbitration_available: true }),
    ])
    button(root, ARBITRATION).click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
    const options = lastConfirm().options as { end?: () => void }
    // 任何关闭路径都会回调 `end` ⇒ 闸必须挂在那里（只挂 cancel 会被 closeAll 绕过）
    expect(typeof options.end).toBe('function')
    // 模拟真 layui：这一次 `layer.msg` 把开着的 confirm 静默关掉（只回调 end、不走取消）
    LAYUI.msg.mockImplementationOnce(() => {
      options.end!()
    })
    LAYUI.msg('一条无关的提示')
    // 闸释放 ⇒ 同一颗按钮还能再开一层（否则用户看到的就是「点了没反应」）
    mockedRequest.mockClear()
    button(root, ARBITRATION).click()
    expect(LAYUI.confirm).toHaveBeenCalledTimes(2)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  /**
   * ⭐ 对「正在代练」的单点申请撤销，上游回的就是一句 `参数错误.`。
   * 后端把它原样包成「代练通没有接受这次撤销申请（代练通回复：参数错误.）。」⇒
   * 翻成一句人话；**其它原因照旧透出原文**（⛔ 不吞、也不把所有失败都说成「状态不允许」）。
   * ⚠️「状态不允许」是**推断**（上游只说参数错误）⇒ 文案用中性表述：
   * 「代练通没有接受这次撤销（原话：参数错误）；常见原因是当前状态不允许」。
   */
  it('申请撤销被上游拒绝「参数错误」⇒ 翻成中性人话（不把推断当事实）；其它原因原样透出', async () => {
    const cases: Array<[string, string]> = [
      [
        '代练通没有接受这次撤销申请（代练通回复：参数错误.）。',
        '代练通没有接受这次撤销（原话：参数错误）；常见原因是当前状态不允许',
      ],
      [
        '代练通没有接受这次撤销申请（代练通回复：无效的参数.）。',
        '代练通没有接受这次撤销（原话：无效的参数）；常见原因是当前状态不允许',
      ],
      // 其它原因：一个字节都不改，原样显示后端中文
      [
        '代练通没有接受这次撤销申请（代练通回复：订单已结算.）。',
        '代练通没有接受这次撤销申请（代练通回复：订单已结算.）。',
      ],
    ]
    for (const [backendText, expected] of cases) {
      document.body.innerHTML = ''
      const root = await mountLoaded(
        [makePush({ remote_status: 12, remote_cancel_status: 10 })],
        {
          '/withdraw/': async () => {
            throw new BizError(1002, backendText)
          },
        },
      )
      button(root, APPLY).click()
      ;(lastOpen().yes as (index: number) => void)(1)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(expected, { time: 6000 })
      })
    }
  })
})

describe('业务方点名的「不做」区块：一个都不在 DOM（按主词本身搜）', () => {
  it('15 颗左列按钮 + 3 个整块 都不在', async () => {
    const root = await mountPage()
    // 选择器判据（有一批按钮的中文只有两个字，用文本搜会与「复制订单内容」撞车）
    const goneSelectors = [
      'a.copyacc',
      'a.copysmurl',
      'a.copypwd',
      'a.setpwd',
      'a.setdlsta',
      'a.jsr',
      'a.getphone',
      'a.unbindPrphone',
      'a.setSphone',
      'a.sendCustomerSH',
      'a.sendCustomerXH',
      'a.upGameVerImg',
      'a.sendCustomerLoginVerCode',
      '#groupCode',
      '#groupChat',
      '.copyurl',
      '.interfaceDiv',
      '#tableList3',
      '#tableList4',
      '#tableList',
      '#mytips',
      '.upVerImgDiv',
      '.upverimg',
    ]
    for (const selector of goneSelectors) {
      expect(root.querySelector(selector), `证据件里还有 ${selector}`).toBeNull()
    }
    // 按**主词本身**搜（AGENTS.md §5：不能只搜派生短语）
    const goneTexts = [
      '取扫码链接',
      '更改密码',
      '更改订单状态',
      '指派代练员',
      '提取虚拟电话',
      '解绑虚拟号',
      '开始上号通知',
      '已下号通知',
      '上传登号验证二维码',
      '通知玩家提供上号验证码',
      '打手进客户群聊码',
      '客户群聊',
      '平台单接手预览',
      '发单操作记录',
      '订单进度图',
    ]
    const text = root.textContent ?? ''
    for (const word of goneTexts) {
      expect(text.includes(word), `页面上还能看到「${word}」`).toBe(false)
    }
  })

  it('要留的 3 颗按钮都在（刷新本界面 / 复制订单内容 / 继续拆分订单）', async () => {
    const root = await mountPage()
    const text = root.textContent ?? ''
    for (const word of ['刷新本界面', '复制订单内容', '继续拆分订单']) {
      expect(text.includes(word), `少了「${word}」`).toBe(true)
    }
  })

  it.each([
    { eligibility: true, canEdit: true, visible: true },
    { eligibility: false, canEdit: true, visible: false },
    { eligibility: undefined, canEdit: true, visible: false },
    { eligibility: true, canEdit: false, visible: false },
  ])('旧详情按资格和权限显示已完成订单拆分入口（%j）', async ({ eligibility, canEdit, visible }) => {
    const permissions = canEdit ? ['order.view_detail', 'order.add_edit'] : ['order.view_detail']
    vi.stubGlobal('fetch', vi.fn(async (raw: unknown) => {
      const url = String(raw)
      if (url.includes('order-orderview.html')) return { ok: true, status: 200, text: async () => EVIDENCE }
      if (url.includes('/api/auth/me')) return {
        ok: true, status: 200, json: async () => ({ code: 0, data: { user: { menu_permissions: permissions } } }),
      }
      throw new Error(`未预期的 fetch：${url}`)
    }))
    const root = await mountPage(makeOrder({ status: 'COMPLETED', can_continue_split: eligibility }))
    expect(root.querySelector<HTMLElement>('.spliteorder')?.style.display === '').toBe(visible)
  })

  it('继续拆分打开并保存并列订单弹窗，提交剩余内容和拆出接单价', async () => {
    const order = makeOrder({ status: 'IN_PROGRESS', total_price: '80.00', split_id: '' })
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      if ((config.url ?? '').endsWith('/audit/')) return []
      if ((config.url ?? '').endsWith('/split-peer/')) {
        return { split_id: 'peer-group-1', split_kind: 'PEER', source_total_price: '60.00', created: [{ order_no: '20261112000000000002' }] }
      }
      if ((config.url ?? '').includes('/platform-push/')) return []
      if ((config.url ?? '').startsWith('/api/business/orders/')) return order
      return { items: [], total: 0, page: 1, page_size: 200 }
    }) as never)
    const root = await mountPage(order)
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
    const options = lastOpen()
    const content = String(options.content)
    document.body.innerHTML = `<div class="layui-layer-content">${content}</div>`
    ;(options.success as (() => void) | undefined)?.()
    const panel = document.querySelector<HTMLElement>('[data-peer-split]')!
    expect(panel.querySelector('[data-remaining-project]')).not.toBeNull()
    expect(panel.querySelector('[data-new-project]')).toBeNull()
    panel.querySelector<HTMLElement>('[data-add-new]')!.click()
    expect(panel.querySelector('[data-new-project]')).not.toBeNull()
    panel.querySelector<HTMLTextAreaElement>('[data-remaining-project]')!.value = '剩余项目甲'
    panel.querySelector<HTMLTextAreaElement>('[data-new-project]')!.value = '拆出项目乙'
    panel.querySelector<HTMLInputElement>('[data-new-price]')!.value = '20.00'
    panel.querySelector<HTMLElement>('[data-save-split]')!.click()
    await Promise.resolve()
    await vi.waitFor(() => expect(mockedRequest.mock.calls.map((call) => (call[0] as ReqConfig).url)).toContain(`/api/business/orders/${order.order_no}/split-peer/`))
    const save = mockedRequest.mock.calls.map((call) => call[0] as ReqConfig).find((call) => call.url?.endsWith('/split-peer/'))!
    expect(save.method).toBe('post')
    expect(save.data).toEqual({
      remaining_project_text: '剩余项目甲',
      new_orders: [{ project_text: '拆出项目乙', total_price: '20.00' }],
    })
  })

  it('LEGACY 组沿用统一金额表单并调用金额拆分接口', async () => {
    const existingNo = '20261112000000000004'
    const order = makeOrder({
      status: 'IN_PROGRESS', total_price: '80.00', split_id: 'legacy-group', split_kind: 'LEGACY',
    })
    const existing = makeOrder({
      order_no: existingNo, status: 'IN_PROGRESS', total_price: '20.00', split_id: 'legacy-group', split_kind: 'LEGACY', project_text: '旧组已有内容',
    })
    const untouched = makeOrder({
      order_no: '20261112000000000005', status: 'IN_PROGRESS', total_price: '15.00', split_id: 'legacy-group', split_kind: 'LEGACY', project_text: '不修改的已有内容',
    })
    vi.mocked(fetchSplitMembers).mockResolvedValue([order, existing, untouched] as never)
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      if ((config.url ?? '').endsWith('/audit/')) return []
      if (config.url === '/api/business/orders/') {
        return { items: [order, existing, untouched], total: 3, page: 1, page_size: 200 }
      }
      if ((config.url ?? '').endsWith('/split-peer/')) {
        return { split_id: 'legacy-group', split_kind: 'LEGACY', source_total_price: '60.00', created: [{ order_no: '20261112000000000003' }] }
      }
      if ((config.url ?? '').includes('/platform-push/')) return []
      if ((config.url ?? '').startsWith('/api/business/orders/')) return order
      return { items: [], total: 0, page: 1, page_size: 200 }
    }) as never)
    const root = await mountPage(order)
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
    const options = lastOpen()
    document.body.innerHTML = `<div class="layui-layer-content">${String(options.content)}</div>`
    ;(options.success as (() => void) | undefined)?.()
    const panel = document.querySelector<HTMLElement>('[data-peer-split]')!
    expect(panel.querySelector('[data-legacy-projects]')).toBeNull()
    expect(panel.querySelector('[data-new-price]')).toBeNull()
    expect(panel.querySelector('[data-existing-row] strong')?.textContent).toBe('20.00')
    expect(panel.querySelector('[data-existing-row] input')).toBeNull()
    panel.querySelector<HTMLTextAreaElement>('[data-existing-project]')!.value = '修订后的旧组内容'
    panel.querySelector<HTMLElement>('[data-save-split]')!.click()
    await Promise.resolve()
    await vi.waitFor(() => expect(mockedRequest.mock.calls.map((call) => (call[0] as ReqConfig).url)).toContain(`/api/business/orders/${order.order_no}/split-peer/`))
    const save = mockedRequest.mock.calls.map((call) => call[0] as ReqConfig).find((call) => call.url?.endsWith('/split-peer/'))!
    expect(save.data).toEqual({
      remaining_project_text: order.project_text,
      new_orders: [],
      existing_orders: [{ order_no: existingNo, project_text: '修订后的旧组内容' }],
    })
  })

  it('有改价权限时已有子单接单价可编辑', async () => {
    const existingNo = '20261112000000000007'
    const order = makeOrder({ status: 'IN_PROGRESS', total_price: '80.00', split_id: 'legacy-price-edit', split_kind: 'LEGACY' })
    const existing = makeOrder({
      order_no: existingNo, status: 'IN_PROGRESS', total_price: '20.00', split_id: 'legacy-price-edit',
      split_kind: 'LEGACY', project_text: '有价格权限的子单',
    })
    vi.stubGlobal('fetch', vi.fn(async (raw: unknown) => {
      const url = String(raw)
      if (url.includes('order-orderview.html')) return { ok: true, status: 200, text: async () => EVIDENCE }
      if (url.includes('/api/auth/me')) return {
        ok: true, status: 200,
        json: async () => ({ code: 0, data: { user: { menu_permissions: ['order.view_detail', 'order.edit_payout_price'] } } }),
      }
      throw new Error(`未预期的 fetch：${url}`)
    }))
    vi.mocked(fetchSplitMembers).mockResolvedValue([
      { ...order, split_is_balance_anchor: true },
      { ...existing, split_is_balance_anchor: false },
    ] as never)
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      if ((config.url ?? '').endsWith('/audit/')) return []
      if ((config.url ?? '').includes('/platform-push/')) return []
      if ((config.url ?? '').startsWith('/api/business/orders/')) return order
      return { items: [], total: 0, page: 1, page_size: 200 }
    }) as never)
    const root = await mountPage(order)
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
    const options = lastOpen()
    document.body.innerHTML = `<div class="layui-layer-content">${String(options.content)}</div>`
    ;(options.success as (() => void) | undefined)?.()
    const price = document.querySelector<HTMLInputElement>('[data-existing-row] [data-existing-price]')
    expect(price).not.toBeNull()
    expect(price!.disabled).toBe(false)
    expect(price!.value).toBe('20.00')
  })

  it('已拆组只修改顶部发起单内容时允许保存，不要求新增或编辑其他成员', async () => {
    const order = makeOrder({ status: 'IN_PROGRESS', split_id: 'peer-source-only', split_kind: 'PEER' })
    const existing = makeOrder({
      order_no: '20261112000000000006', split_id: 'peer-source-only', split_kind: 'PEER', project_text: '其他组员内容',
    })
    vi.mocked(fetchSplitMembers).mockResolvedValue([order, existing] as never)
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      if ((config.url ?? '').endsWith('/audit/')) return []
      if ((config.url ?? '').endsWith('/split-peer/')) return { split_id: 'peer-source-only', split_kind: 'PEER', source_total_price: '199.50', created: [] }
      if ((config.url ?? '').includes('/platform-push/')) return []
      if ((config.url ?? '').startsWith('/api/business/orders/')) return order
      return { items: [], total: 0, page: 1, page_size: 200 }
    }) as never)
    const root = await mountPage(order)
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
    const options = lastOpen()
    document.body.innerHTML = `<div class="layui-layer-content">${String(options.content)}</div>`
    ;(options.success as (() => void) | undefined)?.()
    const panel = document.querySelector<HTMLElement>('[data-peer-split]')!
    panel.querySelector<HTMLTextAreaElement>('[data-remaining-project]')!.value = '只改发起单内容'
    panel.querySelector<HTMLElement>('[data-save-split]')!.click()
    await vi.waitFor(() => expect(mockedRequest.mock.calls.some((call) => (call[0] as ReqConfig).url?.endsWith('/split-peer/'))).toBe(true))
    const save = mockedRequest.mock.calls.map((call) => call[0] as ReqConfig).find((call) => call.url?.endsWith('/split-peer/'))!
    expect(save.data).toEqual({ remaining_project_text: '只改发起单内容', new_orders: [] })
  })

  it('「订单日志」输入条 + 「添加」按钮照对标留着（T3 接线）', async () => {
    const root = await mountPage()
    expect(root.querySelector<HTMLInputElement>('#remark')?.getAttribute('maxlength')).toBe('50')
    expect(
      root.querySelector('button[lay-filter="addpbtn"]')?.textContent?.trim(),
    ).toBe('添加')
    expect(root.querySelector('#logdiv')).not.toBeNull()
    expect(root.querySelector('#tableList_actionlog')).not.toBeNull()
  })

  it('页面里**没有**运行期追加的卡片（业务方 2026-09-22 裁定：完全照对标 ⇒ 删掉多出来的 7 张）', async () => {
    const root = await mountPage()
    // 把 `insertAdjacentHTML(... extraCardsHtml ...)` 那一句加回去 ⇒ 这条**当场变红**
    expect(root.querySelectorAll('[data-extra-card]').length).toBe(0)
    // 证据件里本来就只有这 3 张 fieldset（订单详情 / 订单日志 / 详细记录）；
    // 追加的 7 张卡片会再多出「订单内容 / 基本 / 价格 / 记录…」的 legend ⇒ 换一种写法加回来也抓得到。
    // ⚠️ 代练通页签自带一张 `平台留言` 的 fieldset——
    //    它**不在**「本系统订单」那半边，本条的判据面收到平台面板之外。
    const mainLegends = Array.from(
      root.querySelectorAll<HTMLElement>('fieldset.layui-elem-field > legend'),
    )
      .filter((el) => el.closest('.layui-tab[lay-filter="tabimg"]') === null)
      .map((el) => (el.textContent ?? '').trim())
    expect(mainLegends).toEqual(['订单详情', '订单日志', '详细记录'])
  })
})

describe('fieldText / NameDicts：字段显示值（「对标行」自己在用）', () => {
  it('fieldText：字典查不到 FK 时回落原始 id（不显示空白）', () => {
    // 空字典 ⇒ 回落 `String(raw)`；有字典 ⇒ 显示名字
    expect(fieldText(makeOrder(), 'game', emptyNameDicts())).toBe('11')
    const dicts = emptyNameDicts()
    dicts.games.set(11, '三角洲行动')
    expect(fieldText(makeOrder(), 'game', dicts)).toBe('三角洲行动')
    // 空串 / null 一律空（金额除外：null ⇒ `—`）
    expect(fieldText(makeOrder({ source: '' }), 'source', dicts)).toBe('')
    expect(fieldText(makeOrder({ hall_price: null }), 'hall_price', dicts)).toBe('—')
    // 布尔走中文
    expect(fieldText(makeOrder({ is_urgent: false }), 'is_urgent', dicts)).toBe('否')
  })
})

describe('取数失败：原样弹后端 msg（不自造文案）', () => {
  it('业务码失败（1002 订单不存在）⇒ 后端那句 msg', async () => {
    await mountPage(new BizError(1002, '订单不存在'))
    expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 6000 })
  })

  it('403（无权限）⇒ 后端那句 msg', async () => {
    await mountPage(new HttpError(403, '没有权限执行该操作'))
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作', { time: 6000 })
  })

  it('失败时**不**写任何字段值（不拿旧值/空值冒充）', async () => {
    const root = await mountPage(new BizError(1002, '订单不存在'))
    expect(spanTexts(root, STANDARD_ROW_LABELS.orderNo)).toEqual([''])
    expect(spanTexts(root, STANDARD_ROW_LABELS.status)).toEqual([''])
  })

  it('配置字典拉取失败**不挡订单本身**（FK 回落原始 id，不弹错）', async () => {
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      const url = config.url ?? ''
      // 同 `defaultImpl`：`/audit/` 必须排在详情前缀之前（本用例断言的是「日志取数失败**不弹错**」）。
      if (url.endsWith('/audit/')) return []
      if (url.startsWith('/api/business/orders/')) return ORDER
      throw new HttpError(500, '配置接口炸了')
    }) as never)
    window.history.replaceState({}, '', `/layui-island/page.html?order_no=${ORDER_NO}`)
    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(spanTexts(root, STANDARD_ROW_LABELS.gameServer)[0]).toBe('11')
  })
})

describe('⭐ 受限模式（2026-10-29，fix/taker-web-order-detail）：无 view_detail/view_content 的账号', () => {
  /**
   * 把全局 `fetch` 换成「证据件 + `/api/auth/me` 两跳」的桩（生产代码探身份走**裸 fetch**，
   * 与 `resolvePlatformWritePermission` 同一条通道——理由见 `@/layui-island/perms`）。
   * `me.user` 决定判据结果：默认空 `menu_permissions` = 打手（两颗节点都没有）。
   */
  function stubMe(meUser: Record<string, unknown>): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        const u = String(url)
        if (u.includes('order-orderview.html')) {
          LAYUI.order.push('artifact')
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        if (u.includes('/api/auth/me')) {
          return { ok: true, status: 200, json: async () => ({ code: 0, data: { user: meUser } }) }
        }
        throw new Error(`未预期的 fetch：${u}`)
      }),
    )
  }

  /**
   * 挂整页，走**受限分支**：`request` 只接 `/api/business/my-orders/<单号>/detail/`
   * 一条出口（其它 URL 一律「未预期」——**全量端点 / 日志 / 字典任何一条打过来都会当场红**，
   * 这正是「打手不该碰那些端点」的判据）。
   */
  async function mountRestricted(
    detail: MyOrderDetail | Error,
    meUser: Record<string, unknown> = { menu_permissions: [] },
  ): Promise<HTMLElement> {
    stubMe(meUser)
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      const url = config.url ?? ''
      if (url === `/api/business/my-orders/${ORDER_NO}/detail/`) {
        if (detail instanceof Error) throw detail
        return detail
      }
      throw new Error(`未预期的 request：${url}`)
    }) as never)
    window.history.replaceState({}, '', `/layui-island/page.html?page=order-orderview&order_no=${ORDER_NO}`)
    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
    return root
  }

  /** 受限详情某一行的**值**（按标签找；标签 = 行里第 1 个 span）。 */
  function restrictedRowValue(root: HTMLElement, label: string): string {
    for (const row of Array.from(root.querySelectorAll<HTMLElement>('.ttRestrictedRow'))) {
      const name = row.querySelector<HTMLElement>('span')
      if ((name?.textContent ?? '').trim() === label) {
        const spans = row.querySelectorAll<HTMLElement>('span')
        return (spans[1]?.textContent ?? '').trim()
      }
    }
    throw new Error(`受限详情里找不到这一行：${label}`)
  }

  /** 受限详情**全部行标签**（恰好集合断言用——比「逐个 not.toContain」强一档）。 */
  function restrictedLabels(root: HTMLElement): string[] {
    return Array.from(root.querySelectorAll<HTMLElement>('.ttRestrictedRow')).map(
      (row) => (row.querySelector<HTMLElement>('span')?.textContent ?? '').trim(),
    )
  }

  it('无节点 ⇒ 取数打 my-orders 受限端点（全量端点 / 日志零请求），证据件骨架也不取', async () => {
    await mountRestricted(makeMyOrderDetail())
    // 出口判据：恰好一条 GET 打在受限端点上（真实 request 出口，不是 mock 的「被调过」）。
    const gets = sentRequests((u) => u === `/api/business/my-orders/${ORDER_NO}/detail/`)
    expect(gets).toHaveLength(1)
    expect(gets[0]!.method).toBe('get')
    // 全量端点（/api/business/orders/<no>/）与日志（/audit/）一条都不许发。
    expect(
      sentRequests((u) => u.startsWith('/api/business/orders/')),
      '受限模式不许打全量订单端点',
    ).toHaveLength(0)
    expect(sentRequests((u) => u.endsWith('/audit/')), '受限模式不许拉日志').toHaveLength(0)
    // 证据件也没取 ⇒ 页面上根本没有对标那 16 行骨架（挂载序只剩 bootstrap）。
    expect(LAYUI.order).toEqual(['bootstrap'])
  })

  it('脱敏字段集渲染：16 键载荷 → 五张卡的行值（字面值；与 H5 OrderDetailView 同一字段集）', async () => {
    const root = await mountRestricted(
      makeMyOrderDetail({ source_order_no: 'TB-DEMO-0007', group_qr_url: 'https://img.example/qr.png' }),
    )
    // 基本信息（单号 = 展示回退 D-0301：来源号非空取它）
    expect(restrictedRowValue(root, '单号')).toBe('TB-DEMO-0007')
    expect(restrictedRowValue(root, '游戏')).toBe('三角洲行动')
    expect(restrictedRowValue(root, '区服')).toBe('安卓微信区')
    expect(restrictedRowValue(root, '项目')).toBe('排位上分')
    expect(restrictedRowValue(root, '状态')).toBe('代练中')
    expect(restrictedRowValue(root, '加急')).toBe('加急')
    expect(restrictedRowValue(root, '接手时间')).toBe('2026-10-28 09:30')
    // 要求
    expect(restrictedRowValue(root, '订单内容')).toBe('从黄金打到星耀')
    expect(restrictedRowValue(root, '代练要求')).toBe('优先晚上打')
    expect(restrictedRowValue(root, '时间要求')).toBe('3 天内完成')
    expect(restrictedRowValue(root, '备注')).toBe('客户要求保密')
    // 游戏账号（三字段有值 ⇒ 卡在）
    expect(restrictedRowValue(root, '游戏账号')).toBe('acc_demo_01')
    expect(restrictedRowValue(root, '子账号')).toBe('sub_demo_02')
    expect(restrictedRowValue(root, '角色信息')).toBe('角色甲')
    // 我的报酬（DecimalField 字符串原样拼「元」）
    expect(restrictedRowValue(root, '接单金额')).toBe('88.00 元')
    // 群聊二维码（有 URL ⇒ 图；alt 是 H5 同一句）
    const qr = root.querySelector<HTMLImageElement>('.ttRestrictedQr')
    expect(qr?.getAttribute('src')).toBe('https://img.example/qr.png')
    expect(qr?.getAttribute('alt')).toBe('群聊二维码')
  })

  it('空值人话化：区服 null / 内容空白 ⇒ 「—」；三账号字段全空 ⇒ 整卡不渲染；群码 null ⇒ 暂无群码', async () => {
    const root = await mountRestricted(
      makeMyOrderDetail({
        server_name: null,
        content: '   ',
        game_account: '',
        game_sub_account: '',
        game_role_name: '',
      }),
    )
    expect(restrictedRowValue(root, '区服')).toBe('—')
    expect(restrictedRowValue(root, '订单内容')).toBe('—')
    // 账号卡整张不在（不是渲染三行空标签）
    expect(restrictedLabels(root)).not.toContain('游戏账号')
    expect(root.textContent ?? '').not.toContain('acc_demo_01')
    expect(root.textContent).toContain(RESTRICTED_QR_EMPTY_TEXT)
    // 谓词函数自己钉一条（纯函数出口）
    expect(restrictedHasGameAccount(makeMyOrderDetail())).toBe(true)
    expect(
      restrictedHasGameAccount(
        makeMyOrderDetail({ game_account: '', game_sub_account: '', game_role_name: '' }),
      ),
    ).toBe(false)
  })

  it('客户联系方式行不存在于 DOM（不渲染，不是渲染成空）——行标签恰好集合', async () => {
    const root = await mountRestricted(makeMyOrderDetail())
    // 恰好集合：多一行 / 少一行 / 换一个标签都会红（防「悄悄加回联系方式行」）。
    expect(restrictedLabels(root)).toEqual([
      '单号',
      '游戏',
      '区服',
      '项目',
      '状态',
      '加急',
      '接手时间',
      '订单内容',
      '代练要求',
      '时间要求',
      '备注',
      '游戏账号',
      '子账号',
      '角色信息',
      '接单金额',
    ])
    // 对标骨架里那些「打手不该看到」的标签一个都不许出现。
    for (const banned of ['联系电话', '指定电话', '登录密码', '暗号/口令', '当前接手', '订单计时']) {
      expect(root.textContent ?? '', `受限页面出现了不该有的标签：${banned}`).not.toContain(banned)
    }
    // 证据件的日志段（两块）在受限模式下整段不存在。
    expect(root.querySelector('#logdiv')).toBeNull()
    expect(root.querySelector('#tableList_actionlog')).toBeNull()
  })

  it('受限端点也 403 ⇒ 居中人话 + 「关闭」键（关层），⛔ 不再是空骨架', async () => {
    const root = await mountRestricted(new HttpError(403, '该单不是你接的，不能查看详情'))
    expect(root.textContent).toContain(RESTRICTED_FORBIDDEN_TEXT)
    // 没有任何一行「标签在、值全空」的数据行
    expect(root.querySelectorAll('.ttRestrictedRow')).toHaveLength(0)
    // 证据件骨架也没装（对照全量模式的 403：骨架在、值全空 + 只有一条 toast）
    expect(LAYUI.order).toEqual(['bootstrap'])
    // 「关闭」键点了走 closeIslandLayer（关掉承载本页的弹层）
    const closeBtn = root.querySelector<HTMLElement>(`.${RESTRICTED_CLOSE_BTN_CLASS}`)
    expect(closeBtn, '403 失败块要有「关闭」键').not.toBeNull()
    closeBtn!.click()
    expect(closeIslandLayer).toHaveBeenCalledWith(window)
  })

  it('非 403 失败（1002 单不存在）⇒ 后端 msg 原样 + 「重试」键重发一次', async () => {
    let calls = 0
    stubMe({ menu_permissions: [] })
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      const url = config.url ?? ''
      if (url === `/api/business/my-orders/${ORDER_NO}/detail/`) {
        calls += 1
        if (calls === 1) throw new BizError(1002, '记录不存在或已被删除')
        return makeMyOrderDetail()
      }
      throw new Error(`未预期的 request：${url}`)
    }) as never)
    window.history.replaceState({}, '', `/layui-island/page.html?order_no=${ORDER_NO}`)
    const root = document.createElement('div')
    document.body.appendChild(root)
    await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
    expect(root.textContent).toContain('记录不存在或已被删除')
    expect(LAYUI.msg).toHaveBeenCalledWith('记录不存在或已被删除', { time: 6000 })
    // 「重试」重新走一次取数 ⇒ 第二次成功后失败块被详情替换
    root.querySelector<HTMLElement>(`.${RESTRICTED_RETRY_BTN_CLASS}`)!.click()
    await vi.waitFor(() => {
      expect(root.textContent).toContain('排位上分')
    })
    expect(calls).toBe(2)
  })

  it('行内「复制」键：data-copy 带显示值，出口 = layui.lay.clipboard（两句 H5 白名单提示）', async () => {
    const root = await mountRestricted(
      makeMyOrderDetail({ source_order_no: 'TB-DEMO-0007' }),
    )
    const copyBtn = root.querySelector<HTMLElement>(`.${RESTRICTED_COPY_BTN_CLASS}`)
    expect(copyBtn, '单号行要有「复制」键').not.toBeNull()
    expect(copyBtn!.dataset.copy).toBe('TB-DEMO-0007')
    copyBtn!.click()
    const options = LAYUI.lay.clipboard.writeText.mock.calls[0]![0] as {
      text: string
      done: () => void
      error: () => void
    }
    expect(options.text).toBe('TB-DEMO-0007')
    // done / error 两句 = H5 详情页 T4 自拟白名单的同两句（两端共用）
    LAYUI.msg.mockClear()
    options.done()
    expect(LAYUI.msg).toHaveBeenCalledWith(RESTRICTED_COPY_SUCCESS_MSG, { time: 1500 })
    LAYUI.msg.mockClear()
    options.error()
    expect(LAYUI.msg).toHaveBeenCalledWith(RESTRICTED_COPY_FAIL_MSG, { time: 1500 })
  })

  it('有 `order.view_detail` ⇒ 仍走全量模式（证据件 + 全量端点，既有行为一字不动）', async () => {
    stubMe({ menu_permissions: ['order.view_detail'] })
    const root = await mountPage(ORDER)
    // 全量端点被打了、证据件被装了（与受限分支同测文件里互为对照）
    expect(sentRequests((u) => u === `/api/business/orders/${ORDER_NO}/`)).toHaveLength(1)
    expect(LAYUI.order).toEqual(['bootstrap', 'artifact'])
    expect(spanTexts(root, STANDARD_ROW_LABELS.orderNo)[0]).toBe(ORDER_NO)
  })

  it('身份探不到（/api/auth/me 失败）⇒ 按全量降级（后端 403 兜底仍在，与判据存在前同行为）', async () => {
    // beforeEach 的默认 fetch 桩只接证据件——/api/auth/me 一跳直接抛 = 探针失败。
    const root = await mountPage(new HttpError(403, '没有权限执行该操作'))
    // 全量取数那一条（`fetchOrder` 的 URL 带尾斜杠；/audit/ 不在其内）真实发出过。
    expect(sentRequests((u) => u === `/api/business/orders/${ORDER_NO}/`)).toHaveLength(1)
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作', { time: 6000 })
    expect(root.querySelectorAll('.ttRestrictedRow')).toHaveLength(0)
  })
})

describe('证据件与 TS 的标签清单必须逐字一致（防「某些行静默不填」）', () => {
  it('STANDARD_ROW_LABELS 的 14 个标签都能在证据件里原样找到', () => {
    const labels = Object.values(STANDARD_ROW_LABELS)
    expect(labels.length).toBe(14)
    for (const label of labels) {
      expect(EVIDENCE.includes(`>${label}</label>`), `证据件里没有「${label}」`).toBe(true)
    }
  })

  it('挂载后每个「对标行」标签在 DOM 里恰好命中一行', async () => {
    const root = await mountPage()
    for (const label of Object.values(STANDARD_ROW_LABELS)) {
      const hits = Array.from(root.querySelectorAll<HTMLElement>('label.itemTitle')).filter(
        (el) => (el.textContent ?? '').trim() === label,
      )
      expect(hits.length, `「${label}」命中了 ${hits.length} 行`).toBe(1)
    }
  })
})

/**
 * ⛔ **页内两段 `<style>` 的逐字钉子**
 *
 * 为什么要有它：这两段 `<style>` 是**复刻的硬要求**（视觉值一律从对标产物取，D-0025），
 * 但既有测试只守标签 / 按钮 / 页签属性——把第 2 段整段删掉，其余全部测试**仍然全绿**
 * ⇒ 误删 / 手改是**静默**的。
 *
 * 判据（期望值全部是**字面值**，⛔ 不是「从证据件读出来再跟自己比」——见 E-0024「自指断言」）：
 *  ① 两段 `<style>` 的 **sha256 字面量**：任何字符的增 / 删 / 改都会红（不是只钉长度 / 存在性）；
 *  ② **关键选择器的完整规则文本**逐字包含：删 / 改这些规则同样红，且红时能一眼看出是哪一条。
 */
describe('证据件的两段页内 <style>：逐字钉死（评审 I-1）', () => {
  /** 两段 `<style>` 的**原文**（含 `<style>` / `</style>` 标签与内部缩进，取自文件头注释之后）。 */
  const STYLE_BLOCKS: string[] = Array.from(
    EVIDENCE.slice(EVIDENCE.indexOf('-->') + 3).matchAll(/<style>[\s\S]*?<\/style>/g),
    (m) => m[0],
  )

  it('恰好两段（整段删掉 ⇒ 这一条与下面两条一起红）', () => {
    expect(STYLE_BLOCKS.length).toBe(2)
  })

  it('两段的 sha256 与字面量一致（改一个字符就红）', () => {
    expect(
      STYLE_BLOCKS.map((b) => createHash('sha256').update(b, 'utf8').digest('hex')),
    ).toEqual([
      'aff90842f42343367a7f0675afa743999dc17ce5d4af7002e13594d22de11d7c',
      '504b0dfcdb042866b50139baff09d9b62e0793ef51a382e82825be00bfa87306',
    ])
  })

  it('第 1 段的关键规则（`.layadmin-pagetabs` 本体 + 它的选中 / hover 态）', () => {
    const block = STYLE_BLOCKS[0] ?? ''
    expect(block).toContain(
      [
        '        .layadmin-pagetabs {',
        '            height: 40px;',
        '            line-height: 40px;',
        '            padding: 0 80px 0 40px;',
        '            background-color: #fafafa;',
        '            box-sizing: border-box;',
        '            box-shadow: 0 1px 2px 0 rgba(0,0,0,.1);',
        '            border-bottom: 1px solid #1AA094;',
        '        }',
      ].join('\n'),
    )
    expect(block).toContain(
      [
        '            .layadmin-pagetabs .layui-tab-title li.layui-this, .layadmin-pagetabs .layui-tab-title li:hover {',
        '                background-color: #009688;',
        '                color: #fff;',
        '            }',
      ].join('\n'),
    )
  })

  it('第 2 段的关键规则逐字（含对标自己没用到的 `.platfromOrderImgList` / `.myscale` / `.chatPop` 死样式）', () => {
    const block = STYLE_BLOCKS[1] ?? ''
    expect(block).toContain(
      [
        '    .itemTitle {',
        '        float: left;',
        '        width: 75px;',
        '        text-align: right;',
        '    }',
      ].join('\n'),
    )
    expect(block).toContain(['    .linespan {', '        color: #808080', '    }'].join('\n'))
    expect(block).toContain(
      [
        '    .lableb {',
        '        display: inline-block;',
        '        padding: 2px 4px;',
        '        font-size: 12px;',
        '        line-height: 14px;',
        '        color: #fff;',
        '        white-space: nowrap;',
        '        vertical-align: middle;',
        '        overflow: hidden;',
        '        border-radius: 3px;',
        '        margin-right: 2px;',
        '    }',
      ].join('\n'),
    )
    expect(block).toContain(
      [
        '    .layui-table-cell {',
        '        height: 40px;',
        '        line-height: 40px;',
        '        padding: 0 5px;',
        '        position: relative;',
        '        box-sizing: border-box;',
        '        font-size: 12px',
        '    }',
      ].join('\n'),
    )
    expect(block).toContain(
      [
        '    .layui-form-item {',
        '        margin-bottom: 12px;',
        '        clear: both;',
        '        *zoom: 1;',
        '    }',
      ].join('\n'),
    )
    expect(block).toContain(
      ['    .layui-tab-item {', '        height: 100%;', '        background: #fff;', '    }'].join('\n'),
    )
    expect(block).toContain(
      [
        '    .platfromOrderImgList li {',
        '        height: 255px;',
        '        float: left;',
        '        display: inline-block;',
        '        margin-left: 10px;',
        '        padding: 2px 2px;',
        '        margin-top: 20px;',
        '        border-color: #e2dede;',
        '        border-width: 1px;',
        '        border-style: solid',
        '    }',
      ].join('\n'),
    )
    expect(block).toContain(['    .myscale {', '        transform: scale(0.2)', '    }'].join('\n'))
    expect(block).toContain(
      [
        '    .chatPop {',
        '        top: 50% !important;',
        '        left: 50% !important;',
        '        transform: translate(-50%,-50%) !important',
        '    }',
      ].join('\n'),
    )
  })
})

/* ⭐ 本页「代练通」页签的**平台写动作**渲染挂 `order.platform_write`。
 *
 * 平台写动作按钮（撤回 / 改价 / 补时 / 补款 / 加价 / 协商撤销 / 同意撤销 / 取消撤销 / 申请仲裁 /
 * 锁定 / 解锁 / 确认完单 / 编辑平台单 / 重发 / 截图上传 / 拉黑）没权限就不画（fail-closed）。
 * 后端另一条线才加这个节点 ⇒ 今天走**降级档**（按 `order.list`、身份取不到时照画），
 * 见 `@/layui-island/perms::canPlatformWrite`。
 *
 * 本组直接驱动「后端已上线该节点」那一档（`setPlatformWriteNodeShipped(true)`），
 * 判据打在**平台面板所有写按钮的唯一可见性出口** `syncPlatformActionVisibility` 上。
 */
describe('⭐ 2026-10-28：平台写动作按 `order.platform_write` 渲染（fail-closed）', () => {
  it('没有该节点 ⇒ 平台写按钮全部收起（含截图上传拖拽区 / 去发单），读面照常', async () => {
    const push = makePush({ remote_status: 13, remote_cancel_status: 10 })
    const root = await mountLoaded([push])
    // 先钉「有权限时确实有按钮露着」（否则下面的全收起可能是状态档位本身就不露）
    setPlatformWriteNodeShipped(true)
    applyPlatformWritePermission({ menu_permissions: ['order.platform_write'] })
    syncPlatformActionVisibility(root, push)
    expect(visible(root, PLATFORM_ACTION_SELECTORS.complete)).toBe(true)

    // 没有该节点 ⇒ 一颗都不露
    applyPlatformWritePermission({ menu_permissions: [] })
    syncPlatformActionVisibility(root, push)
    for (const selector of Object.values(PLATFORM_ACTION_SELECTORS)) {
      expect(visible(root, selector), `写按钮没被收起：${selector}`).toBe(false)
    }
    // 截图上传（拖拽区）也是平台写动作
    expect(root.querySelector<HTMLElement>(PLATFORM_UPLOAD_DRAG_SELECTOR)!.style.display).toBe(
      'none',
    )
    // 读面照常（不是写动作，照旧显示）
    expect(platformSpan(root, '交易平台单号：')).not.toBe('')
  })

  it('有该节点 ⇒ 照旧按远端状态露（13 ⇒ 「确认完单」可点）', async () => {
    const push = makePush({ remote_status: 13, remote_cancel_status: 10 })
    const root = await mountLoaded([push])
    setPlatformWriteNodeShipped(true)
    applyPlatformWritePermission({ menu_permissions: ['order.list', 'order.platform_write'] })
    syncPlatformActionVisibility(root, push)
    expect(visible(root, PLATFORM_ACTION_SELECTORS.complete)).toBe(true)
    expect(root.querySelector<HTMLElement>(PLATFORM_UPLOAD_DRAG_SELECTOR)!.style.display).not.toBe(
      'none',
    )
  })

  it('降级档（后端还没这个节点）⇒ 按 `order.list`：有就照画（升级前行为）', async () => {
    const push = makePush({ remote_status: 13, remote_cancel_status: 10 })
    const root = await mountLoaded([push])
    // mount 期已按夹具里的 `order.list` 判过一次；这里再显式驱动一遍，判据是「按钮仍在」
    applyPlatformWritePermission({ menu_permissions: ['order.list'] })
    syncPlatformActionVisibility(root, push)
    expect(visible(root, PLATFORM_ACTION_SELECTORS.complete)).toBe(true)
  })
})
