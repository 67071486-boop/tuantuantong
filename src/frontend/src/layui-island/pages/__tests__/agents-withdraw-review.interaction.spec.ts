// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import { TAKER_WITHDRAW_NODE, type WithdrawListPage, type WithdrawRow } from '@/api/withdrawals'

/**
 * 【财务相关 → 打手提现审核】island 页的**交互**测试
 * （计划 `docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §5 最后一行：
 * 「必须**驱动真交互**（点「通过」→ 真发请求 → 真刷新）」）。
 *
 * 这一页是**自有功能**（对标物里没有），所以没有证据件可对拍；本文件钉的是**有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有一条能红的交互用例）：
 *    挂载 → 点「通过」→ `layer.confirm` → 确认 → **真发 `POST …/approve/`** → **列表被重拉**
 *    （请求条数从 1 变 2、且表里的行随服务端状态变化而消失）。同样驱动驳回（原因必填）、
 *    发起代付（`confirm_amount` 是**原样字符串**）、登记打款（单号必填）、刷新状态。
 * 2. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量（标记里不许出现 `class=".xxx"`）；
 *    以 `(` 开头的语句前置分号（页面里那一批 `;(table.render as …)` / `;(layer.confirm as …)`）；
 *    挂载顺序以**运行期 DOM** 判断（`bootstrapIsland` 看不到标记、`initIslandElements` 看得到）。
 *    ⚠️ 本页**没有定时器**（陷阱 ④ 不适用），所以各用例自己 `document.body.innerHTML = ''` 收口。
 * 3. ⭐ **权限门 fail-closed**：拿不到 `finance.taker_withdraw_review` ⇒ 一句提示 +
 *    **不注入标记** + **不发任何提现审核请求**（列表与 5 个写动作一次都不发；
 *    `fetchMe()` 那一次身份请求照发——它就是这道门本身），于是也一个操作按钮都不画。
 * 4. ⭐ **操作列按状态与开关出现**（计划 §5）：`actionCell` 是纯函数、判据只有一处；
 *    这里逐状态 + 开关开/关 + 金额在上限内/外各钉一遍（⛔ 不只测一条）。
 * 5. ⛔ **金额不换算**：代付请求里的 `confirm_amount` 必须**逐字等于**行里的 `amount`
 *    （计划 §4.3.1：「金额一律字符串……客户端别 parseFloat 再算」；§4.2 的硬闸靠它）。
 *
 * ## 手法（照 `__tests__/open-apps.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `table.render` /
 *   `table.reloadData` / `layer.confirm` / `layer.prompt` 的**入参**才能被断言；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/withdrawals` 跑的是**真实现** ⇒
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义；
 * - 服务端响应是**活的**：写动作会改 `rowsOnServer`，所以「重拉之后看得见变化」不是只断言夹具。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：打手名 / 支付宝账号 / 单号都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const renders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const confirms: Array<{ text: string; options: unknown; ok: () => void }> = []
  const prompts: Array<{ options: Record<string, unknown>; cb: (value: string, index: number) => void }> = []
  const closes: number[] = []
  const handlers: Record<string, (obj: unknown) => void> = {}
  // ⚠️ 这两个数组是**挂载顺序**的运行期判据（§5-G 陷阱 ③）。⚠️ 选择器写**字面值**：
  //    本工厂在页面模块求值**之前**就会跑（它是那条 import 链的一环）⇒ 引常量会读到未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    renders,
    reloads,
    confirms,
    prompts,
    closes,
    handlers,
    seenByBootstrap,
    seenByInit,
    table: {
      render: vi.fn((o: Record<string, unknown>) => {
        renders.push(o)
      }),
      reloadData: vi.fn((id: string, o: { data: unknown }) => {
        reloads.push({ id, data: o.data })
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => void) => {
        handlers[event] = cb
      }),
    },
    layer: {
      confirm: vi.fn((text: string, options: unknown, ok: () => void) => {
        confirms.push({ text, options, ok })
      }),
      prompt: vi.fn((options: Record<string, unknown>, cb: (value: string, index: number) => void) => {
        prompts.push({ options, cb })
      }),
      close: vi.fn((index: number) => {
        closes.push(index)
      }),
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#withdrawReviewTabs') !== null)
    return { table: LAYUI.table, layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#withdrawReviewTabs') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 身份：本页第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  DEFAULT_TAB_STATE,
  EVENTS,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PANEL_CLASS,
  PANEL_SELECTOR,
  STATUS_TABS,
  TAB_ATTR,
  TABS_ID,
  TABLE_ID,
  TRANSFER_FLAG_MISSING_HINT,
  TRANSFER_INFLIGHT_HINT,
  __testables,
} from '../agents-withdraw-review'

const {
  actionCell,
  amountCents,
  blocksManualPay,
  buildHtml,
  canAutoTransfer,
  fetchAllWithdrawRows,
  formatTime,
  markPaidPromptOptions,
  payingHint,
  rejectPromptOptions,
  statusCell,
  subjectText,
  toContext,
  transferResultText,
  workerText,
} = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这四个）。 */
type Cfg = { method?: string; url?: string; data?: unknown; params?: Record<string, unknown> }

const WITHDRAWALS_URL = '/api/business/withdrawals/'

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

/** 造一行；只写用例关心的字段，其余给安全默认值。 */
function withdrawRow(over: Partial<WithdrawRow> & { id: number }): WithdrawRow {
  return {
    user_id: over.id + 100,
    username: `synthetic_taker_${over.id}`,
    display_name: `合成打手${over.id}`,
    amount: '88.00',
    alipay_account: `139000000${over.id}`,
    alipay_name: `合成收款人${over.id}`,
    state: 'pending',
    state_text: '待审核',
    channel: 'manual',
    transfer_id: '',
    transfer_error: '',
    // 评审阻断项 1 新增的契约字段：默认「没有在途代付」。
    transfer_inflight: false,
    // ⭐ 契约字段：默认不是快路、没有预计打款时间、没有中断。
    fast_lane: false,
    payout_after: null,
    interrupted_at: null,
    interrupt_reason: '',
    remark: '',
    created_at: '2026-10-20T10:00:00+08:00',
    reviewed_at: null,
    paid_at: null,
    ...over,
  }
}

/** 待审核（点「通过」用的那一条）。 */
const PENDING = withdrawRow({ id: 11 })
/** 已通过、金额在上限内（开关打开时该出现「发起代付」）。 */
const APPROVED = withdrawRow({
  id: 12,
  state: 'approved',
  state_text: '已通过',
  amount: '120.50',
  channel: 'jeepay_alipay',
})
/** 已通过、金额**超过**上限（开关打开也不该出现「发起代付」）。 */
const APPROVED_BIG = withdrawRow({
  id: 13,
  state: 'approved',
  state_text: '已通过',
  amount: '1200.00',
})
/** 转账中、**没有在途代付**（`transfer_inflight=false`）⇒ 可登记人工打款收场 + 刷新；带上游失败原因。 */
const PAYING = withdrawRow({
  id: 14,
  state: 'paying',
  state_text: '转账中',
  channel: 'jeepay_alipay',
  transfer_id: 'SYNTH-TRANSFER-14',
  transfer_error: '上游返回：状态待确认',
  transfer_inflight: false,
})
/** 转账中、**我方代付请求在途**（`transfer_inflight=true`）⇒ 只给「刷新状态」＋ 在途提示。 */
const PAYING_INFLIGHT = withdrawRow({
  id: 18,
  state: 'paying',
  state_text: '转账中',
  channel: 'jeepay_alipay',
  transfer_inflight: true,
})
/** 已打款 / 已驳回 / 失败：都**不该**出现任何动作。 */
const PAID = withdrawRow({ id: 15, state: 'paid', state_text: '已打款', paid_at: '2026-10-20T11:00:00+08:00' })
const REJECTED = withdrawRow({
  id: 16,
  state: 'rejected',
  state_text: '已驳回',
  remark: '账号与实名不一致',
  reviewed_at: '2026-10-20T11:10:00+08:00',
})
const FAILED = withdrawRow({
  id: 17,
  state: 'failed',
  state_text: '转账失败（已退余额）',
  transfer_error: '上游返回：账户异常',
})

/** 响应路由：按 `METHOD URL` 认领。**没有备好响应就抛**（⛔ 不是静默回 undefined）。 */
let routes: Record<string, (data?: unknown) => unknown>

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

function actionUrl(id: number, action: string): string {
  return `${WITHDRAWALS_URL}${id}/${action}/`
}

function flush(): Promise<void> {
  // 页面的取数链是多跳的（点击 → 确认 → request → 再 request → 上屏）：连放几轮宏任务。
  return (async () => {
    for (let i = 0; i < 8; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

/** 最近一次发往某条 URL 的请求（没有就抛，避免静默空判据）。 */
function callTo(method: string, url: string): Cfg {
  const found = mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url)
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

/** 打到某个方法的请求条数（用于「重新拉了一次」这种判据）。 */
function callCount(method: string): number {
  return mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method)
    .length
}

/** 表里最后一次灌进去的数据（= 页面渲染的全部行）。 */
function tableRows(): WithdrawRow[] {
  const last = LAYUI.reloads[LAYUI.reloads.length - 1]
  if (!last) throw new Error('没有对列表调用过 reloadData')
  return last.data as WithdrawRow[]
}

/** 行内动作（layui `table.on('tool(<id>)')` 的回调就是这一形的入参）。 */
function rowTool(row: WithdrawRow, event: WithdrawEvent | string): void {
  LAYUI.handlers[`tool(${TABLE_ID})`]!({ data: row, event })
}

/** 「操作」列的 templet（**从页面真正 render 出去的那份 cols 上取**，⛔ 不自己再拼一份判据）。 */
function actionTemplet(): (row: WithdrawRow) => string {
  const first = LAYUI.renders[0]
  if (!first) throw new Error('页面没有调用 table.render')
  const cols = first.cols as Array<Array<{ title?: string; templet?: unknown }>>
  const cell = cols[0]?.find((c) => c.title === '操作')
  if (!cell || typeof cell.templet !== 'function') throw new Error('操作列没有函数 templet')
  return cell.templet as (row: WithdrawRow) => string
}

type WithdrawEvent = 'approve' | 'reject' | 'transfer' | 'refresh' | 'markpaid' | 'interrupt'

describe('打手提现审核 island 页（agents-withdraw-review）', () => {
  let root: HTMLElement
  /** 服务端**此刻**那份单据（写动作会改它 ⇒ 重拉之后能看出变化）。 */
  let rowsOnServer: WithdrawRow[]
  /** 列表响应里那两个开关位（逐用例可改）。 */
  let autoEnabled: boolean
  let maxAmount: string

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
    LAYUI.renders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.confirms.length = 0
    LAYUI.prompts.length = 0
    LAYUI.closes.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    for (const key of Object.keys(LAYUI.handlers)) delete LAYUI.handlers[key]
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    // 持节点（走「正常挂载」那一支）；无权限那一支在它自己的用例里改。
    mockedFetchMe.mockResolvedValue({
      id: 1,
      username: 'synthetic-cs',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: [TAKER_WITHDRAW_NODE],
      permissions: [],
    })

    routes = {}
    rowsOnServer = [PENDING, APPROVED]
    autoEnabled = false
    maxAmount = '1000.00'

    ready(`POST ${actionUrl(PENDING.id, 'approve')}`, () => {
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === PENDING.id
          ? { ...row, state: 'approved' as const, state_text: '已通过', channel: 'jeepay_alipay' }
          : row,
      )
      return { id: PENDING.id, state: 'approved', state_text: '已通过' }
    })
    ready(`POST ${actionUrl(PENDING.id, 'reject')}`, (data) => {
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === PENDING.id
          ? { ...row, state: 'rejected' as const, state_text: '已驳回', remark: String((data as { remark?: string }).remark ?? '') }
          : row,
      )
      return { id: PENDING.id, state: 'rejected', state_text: '已驳回' }
    })
    ready(`POST ${actionUrl(APPROVED.id, 'transfer')}`, () => {
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === APPROVED.id
          ? { ...row, state: 'paying' as const, state_text: '转账中', transfer_id: 'SYNTH-TRANSFER-12' }
          : row,
      )
      return {
        id: APPROVED.id,
        state: 'paying',
        state_text: '转账中',
        transfer_id: 'SYNTH-TRANSFER-12',
        transfer_error: '',
      }
    })
    ready(`POST ${actionUrl(APPROVED.id, 'mark-paid')}`, (data) => {
      const transferNo = String((data as { transfer_no?: string }).transfer_no ?? '')
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === APPROVED.id
          ? { ...row, state: 'paid' as const, state_text: '已打款', channel: 'manual', transfer_id: transferNo }
          : row,
      )
      return { id: APPROVED.id, state: 'paid', state_text: '已打款' }
    })
    // 转账中**不在途**时人工打款的出口（评审阻断项 1 的「能收场」）。
    ready(`POST ${actionUrl(PAYING.id, 'mark-paid')}`, (data) => {
      const transferNo = String((data as { transfer_no?: string }).transfer_no ?? '')
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === PAYING.id
          ? { ...row, state: 'paid' as const, state_text: '已打款', channel: 'manual', transfer_id: transferNo }
          : row,
      )
      return { id: PAYING.id, state: 'paid', state_text: '已打款' }
    })
    ready(`POST ${actionUrl(PAYING.id, 'refresh')}`, () => {
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === PAYING.id
          ? { ...row, state: 'paid' as const, state_text: '已打款', transfer_error: '' }
          : row,
      )
      return {
        id: PAYING.id,
        state: 'paid',
        state_text: '已打款',
        transfer_id: 'SYNTH-TRANSFER-14',
        transfer_error: '',
      }
    })

    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        // 列表：按 `state` 筛 + 分页（响应是**活的**，写动作改的是 `rowsOnServer`）。
        if (key === `GET ${WITHDRAWALS_URL}`) {
          const state = cfg.params?.state as string | undefined
          const filtered =
            state === undefined ? [...rowsOnServer] : rowsOnServer.filter((row) => row.state === state)
          const page: WithdrawListPage = {
            items: filtered,
            total: filtered.length,
            page: 1,
            page_size: 20,
            auto_transfer_enabled: autoEnabled,
            transfer_max_amount: maxAmount,
          }
          return page
        }
        const fn = routes[key]
        if (!fn) throw new Error(`测试没有为 ${key} 准备响应`)
        return fn(cfg.data)
      }),
    )
  })

  afterEach(() => {
    // 每个用例自己收口（`AGENTS.md` §5-G 陷阱 ④ 的同款纪律：上一个用例挂载的页面会继续打同一个替身）。
    document.body.innerHTML = ''
  })

  async function mount(): Promise<void> {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  // ── 三条 island 陷阱（运行期判据）────────────────────────────────

  it('陷阱③ 先挂样式再塞标记：bootstrapIsland 看不到标记、initIslandElements 看得到', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('陷阱① 类名与选择器是两个常量：标记里是 `class="withdrawReviewPanel"`（⛔ 不是 `.withdrawReviewPanel`）', async () => {
    await mount()
    expect(PANEL_CLASS).toBe('withdrawReviewPanel')
    expect(PANEL_SELECTOR).toBe('.withdrawReviewPanel')
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(root.innerHTML).not.toContain('class=".withdrawReviewPanel"')
    // 状态 tab 的属性名也是一个常量（⛔ 不在标记里另写一份字面属性名）。
    expect(TAB_ATTR).toBe('data-withdraw-state')
    // 六个 tab 都在标记里（缺哪个就把哪个状态名收集出来 —— 空数组 = 一个都不缺）。
    const missingTabs = STATUS_TABS.filter(
      (tab) => root.querySelector(`[${TAB_ATTR}="${tab.state}"]`) === null,
    ).map((tab) => tab.state)
    expect(missingTabs).toEqual([])
  })

  it('tab 顺序与文案：待审核 / 已通过 / 转账中 / 已打款 / 转账失败 / 已驳回 / 已中断', () => {
    // 业务方 2026-10-20：「转账失败」排在「已打款」之后、「已驳回」之前，⛔ 不许省掉。
    // 财务中断含「已中断」档，排最后。
    expect(STATUS_TABS.map((tab) => tab.label)).toEqual([
      '待审核',
      '已通过',
      '转账中',
      '已打款',
      '转账失败',
      '已驳回',
      '已中断',
    ])
    // 标记里的顺序也照它（`data-*` 属性的出现顺序 = DOM 顺序）。
    const html = buildHtml()
    const states = Array.from(html.matchAll(new RegExp(`${TAB_ATTR}="([a-z]+)"`, 'g'))).map(
      (m) => m[1],
    )
    expect(states).toEqual(STATUS_TABS.map((tab) => tab.state))
  })

  // ── 首屏取数 ──────────────────────────────────────────────────

  it('首屏取「待审核」第一页，并把行原样灌进表', async () => {
    await mount()
    const list = callTo('get', WITHDRAWALS_URL)
    expect(list.params).toEqual({ state: DEFAULT_TAB_STATE, page: 1 })
    expect(DEFAULT_TAB_STATE).toBe('pending')
    // 「待审核」桶里只有待审核那一行（`APPROVED` 在「已通过」桶，不在这一桶）。
    expect(tableRows().map((r) => r.id)).toEqual([PENDING.id])
  })

  it('列表取不到时如实显示后端文案（⛔ 不静默空表）', async () => {
    mockedRequest.mockImplementationOnce(() =>
      Promise.reject(new BizError(1000, '没有权限查看打手提现审核')),
    )
    await mount()
    expect(LAYUI.messages).toContain('没有权限查看打手提现审核')
  })

  // ── ⭐ 真交互：点「通过」→ 真发请求 → 真刷新 ─────────────────────

  it('⭐ 点「通过」→ 二次确认（金额 + 账号 + 姓名）→ POST approve → 列表被重拉、行随状态离开当前 tab', async () => {
    await mount()
    expect(callCount('get')).toBe(1)
    expect(LAYUI.confirms).toHaveLength(0)

    rowTool(PENDING, EVENTS.approve)
    expect(LAYUI.confirms).toHaveLength(1)
    const confirm = LAYUI.confirms[0]!
    // 计划 §5：弹窗里把**金额 + 支付宝账号 + 姓名**摆出来（三个都要，缺一个这行就红）。
    expect(confirm.text).toContain(PENDING.amount)
    expect(confirm.text).toContain(PENDING.alipay_account)
    expect(confirm.text).toContain(PENDING.alipay_name)
    expect(callCount('post')).toBe(0) // 确认之前**一个写请求都没有**

    confirm.ok()
    await flush()

    const post = callTo('post', actionUrl(PENDING.id, 'approve'))
    expect(post.data).toBeUndefined() // 通过不带载荷（§4.3 的 approve 只认路径里的 id）
    // **列表被重拉**：GET 从 1 → 2，且待审核桶里已经没有这一条（服务端状态变了）。
    expect(callCount('get')).toBe(2)
    // 这一行离开「待审核」桶（服务端状态已变）⇒ 重拉之后表里**空**，不是「还留着旧行」。
    expect(tableRows()).toEqual([])
    expect(LAYUI.messages).toContain(MESSAGES.approved)
  })

  it('点「通过」后写请求失败：如实显示后端 msg，且**不**重拉列表', async () => {
    ready(`POST ${actionUrl(PENDING.id, 'approve')}`, () => {
      throw new BizError(1000, '只有待审核的提现可以审核')
    })
    await mount()
    rowTool(PENDING, EVENTS.approve)
    LAYUI.confirms[0]!.ok()
    await flush()

    expect(LAYUI.messages).toContain('只有待审核的提现可以审核')
    expect(callCount('get')).toBe(1)
  })

  it('同一行连点两次「通过」只发一个请求（在途闸门）', async () => {
    await mount()
    rowTool(PENDING, EVENTS.approve)
    LAYUI.confirms[0]!.ok()
    // 第二个确认框照开（闸门不包住确认，⛔ 不让「点了取消」之后的下一次点击静默无效）……
    rowTool(PENDING, EVENTS.approve)
    LAYUI.confirms[1]!.ok()
    await flush()
    // ……但同一行的同一个动作在途时只发一次。
    expect(callCount('post')).toBe(1)
  })

  // ── 驳回：原因必填 ────────────────────────────────────────────

  it('点「驳回」→ 原因必填：空串只提示、不发请求；填了才 POST 且带上 remark', async () => {
    await mount()
    rowTool(PENDING, EVENTS.reject)
    expect(LAYUI.prompts).toHaveLength(1)
    const options = LAYUI.prompts[0]!.options
    const title = String(options.title)
    expect(title).toContain(PENDING.amount)
    expect(title).toContain(PENDING.alipay_account)
    expect(title).toContain(PENDING.alipay_name)

    // 只输空白 ⇒ 提示 + **不关层、不发请求**。
    LAYUI.prompts[0]!.cb('   ', 7)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.rejectReasonRequired)
    expect(LAYUI.closes).toEqual([])
    expect(callCount('post')).toBe(0)

    // 填了原因 ⇒ 关层 + POST（原因**原样**发出去）。
    LAYUI.prompts[0]!.cb('  账号与实名不一致  ', 7)
    await flush()
    expect(LAYUI.closes).toEqual([7])
    expect(callTo('post', actionUrl(PENDING.id, 'reject')).data).toEqual({
      remark: '账号与实名不一致',
    })
    expect(callCount('get')).toBe(2)
    expect(LAYUI.messages).toContain(MESSAGES.rejected)
  })

  // ── 财务中断：原因必填──────────────────────────

  it('点「中断」→ 原因必填：空串只提示、不发请求；填了才 POST 且带上 reason', async () => {
    await mount()
    rowTool(APPROVED, EVENTS.interrupt)
    expect(LAYUI.prompts).toHaveLength(1)
    const options = LAYUI.prompts[0]!.options
    expect(String(options.title)).toContain(APPROVED.amount)
    expect(String(options.title)).toContain(APPROVED.alipay_account)

    LAYUI.prompts[0]!.cb('   ', 7)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.interruptReasonRequired)
    expect(LAYUI.closes).toEqual([])
    expect(callCount('post')).toBe(0)

    LAYUI.prompts[0]!.cb('  风控复核  ', 7)
    await flush()
    expect(LAYUI.closes).toEqual([7])
    expect(callTo('post', actionUrl(APPROVED.id, 'interrupt')).data).toEqual({
      reason: '风控复核',
    })
  })

  // ── 发起代付：开关 + 上限 + 金额原样 ──────────────────────────

  it('⭐ 点「发起代付」：开关开着才画按钮；POST 的 confirm_amount 是行里 amount 的**原样字符串**', async () => {
    autoEnabled = true
    rowsOnServer = [APPROVED]
    await mount()

    const cell = actionTemplet()(APPROVED)
    expect(cell).toContain(EVENTS.transfer)
    expect(cell).toContain(EVENTS.markPaid)
    // ⭐ 打款前可以中断（钱退回余额）。
    expect(cell).toContain(EVENTS.interrupt)
    expect(cell).not.toContain(EVENTS.approve)
    expect(cell).not.toContain(EVENTS.reject)

    rowTool(APPROVED, EVENTS.transfer)
    const confirm = LAYUI.confirms[0]!
    expect(confirm.text).toContain(APPROVED.amount)
    expect(confirm.text).toContain(APPROVED.alipay_account)
    expect(confirm.text).toContain(APPROVED.alipay_name)
    confirm.ok()
    await flush()

    const post = callTo('post', actionUrl(APPROVED.id, 'transfer'))
    // ⛔ 不是 120.5 / 不是 number：**字符串、一个字符都不动**（§4.3.1 + §4.2 第 2 条）。
    expect(post.data).toEqual({ confirm_amount: '120.50' })
    expect(typeof (post.data as { confirm_amount: unknown }).confirm_amount).toBe('string')
    expect(callCount('get')).toBe(2)
    // ⭐ 评审 A7：提示按**返回的 `state`** 说人话（这个桩返回 `paying`）。
    expect(LAYUI.messages).toContain(MESSAGES.transferredPaying)
  })

  it('⭐ 代付**当场失败**（返回 state=failed）⇒ 提示「金额已退回打手余额」，不说「请求已接受」', async () => {
    autoEnabled = true
    rowsOnServer = [APPROVED]
    // 后端 §3 的状态机：上游失败 ⇒ `failed` + **已退余额**（钱没打出去）。
    ready(`POST ${actionUrl(APPROVED.id, 'transfer')}`, () => {
      rowsOnServer = rowsOnServer.map((row) =>
        row.id === APPROVED.id
          ? { ...row, state: 'failed' as const, state_text: '转账失败（已退余额）', transfer_error: '上游返回：账户异常', transfer_inflight: false }
          : row,
      )
      return {
        id: APPROVED.id,
        state: 'failed',
        state_text: '转账失败（已退余额）',
        transfer_id: '',
        transfer_error: '上游返回：账户异常',
      }
    })
    await mount()
    rowTool(APPROVED, EVENTS.transfer)
    LAYUI.confirms[0]!.ok()
    await flush()

    expect(callCount('get')).toBe(2)
    expect(LAYUI.messages).toContain(MESSAGES.transferredFailed)
    // ⛔ 不能再说成「已接受 / 在路上」——那是这条评审项要堵的误导。
    expect(LAYUI.messages).not.toContain(MESSAGES.transferredPaying)
    expect(LAYUI.messages).not.toContain(MESSAGES.transferredSubmitted)
  })

  it('代付返回 paid ⇒ 提示「已打款」；返回未知 state ⇒ 只说「已提交，请刷新」', async () => {
    // 纯函数（三档 + 兜底），不依赖挂载：判据只住在这一个出口。
    expect(transferResultText({ state: 'paid' })).toBe(MESSAGES.transferredPaid)
    expect(transferResultText({ state: 'paying' })).toBe(MESSAGES.transferredPaying)
    expect(transferResultText({ state: 'failed' })).toBe(MESSAGES.transferredFailed)
    expect(transferResultText({ state: 'brand_new_state' })).toBe(MESSAGES.transferredSubmitted)
    // 返回体不是对象 / 缺 state ⇒ 兜底（⛔ 不猜结论）。
    expect(transferResultText(undefined)).toBe(MESSAGES.transferredSubmitted)
    expect(transferResultText({})).toBe(MESSAGES.transferredSubmitted)
  })

  it('开关关着 / 金额超上限 ⇒ 不画「发起代付」，只留「登记打款」（fail-closed）', async () => {
    await mount()
    // 默认 `autoEnabled = false`。
    const closed = actionTemplet()(APPROVED)
    expect(closed).not.toContain(EVENTS.transfer)
    expect(closed).toContain(EVENTS.markPaid)

    autoEnabled = true
    maxAmount = '100.00' // APPROVED 是 120.50 ⇒ 超上限
    await mount()
    const overLimit = actionTemplet()(APPROVED)
    expect(overLimit).not.toContain(EVENTS.transfer)
    expect(overLimit).toContain(EVENTS.markPaid)
  })

  // ── 登记人工打款：单号必填 ───────────────────────────────────

  it('点「登记打款」→ 单号必填：空串只提示；填了才 POST，且**不发** remark 键', async () => {
    await mount()
    rowTool(APPROVED, EVENTS.markPaid)
    expect(LAYUI.prompts).toHaveLength(1)
    const title = String(LAYUI.prompts[0]!.options.title)
    expect(title).toContain(APPROVED.amount)
    expect(title).toContain(APPROVED.alipay_account)
    expect(title).toContain(APPROVED.alipay_name)

    LAYUI.prompts[0]!.cb('', 3)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.transferNoRequired)
    expect(callCount('post')).toBe(0)

    LAYUI.prompts[0]!.cb('SYNTH-MANUAL-20261020', 3)
    await flush()
    const data = callTo('post', actionUrl(APPROVED.id, 'mark-paid')).data as Record<string, unknown>
    expect(data.transfer_no).toBe('SYNTH-MANUAL-20261020')
    // `remark` 可选：没填就**不发这个键**（空串与"没填"不是一回事，E-0021）。
    expect('remark' in data).toBe(false)
    expect(callCount('get')).toBe(2)
    expect(LAYUI.messages).toContain(MESSAGES.markPaidDone)
  })

  // ── 转账中：刷新状态 / 不在途时可登记打款收场 ─────────────────

  it('⭐ 转账中**不在途**（transfer_inflight=false）⇒「登记打款」在，且能真的收场', async () => {
    rowsOnServer = [PAYING]
    await mount()
    const cell = actionTemplet()(PAYING)
    expect(cell).toContain(EVENTS.markPaid)
    expect(cell).toContain(EVENTS.refresh)
    // 不在途时不该出现那句在途提示。
    expect(statusCell(PAYING)).not.toContain(TRANSFER_INFLIGHT_HINT)

    rowTool(PAYING, EVENTS.markPaid)
    expect(LAYUI.prompts).toHaveLength(1)
    LAYUI.prompts[0]!.cb('SYNTH-MANUAL-14', 5)
    await flush()
    expect(callTo('post', actionUrl(PAYING.id, 'mark-paid')).data).toEqual({
      transfer_no: 'SYNTH-MANUAL-14',
    })
    expect(callCount('get')).toBe(2)
  })

  it('⭐ 转账中**在途**（transfer_inflight=true）⇒ 只给刷新 + 提示「正在发起代付，请稍后刷新」', async () => {
    rowsOnServer = [PAYING_INFLIGHT]
    await mount()
    const cell = actionTemplet()(PAYING_INFLIGHT)
    expect(cell).toContain(EVENTS.refresh)
    expect(cell).not.toContain(EVENTS.markPaid)
    // 提示要出现在状态列（说明为什么没有登记打款），文案逐字钉住。
    expect(statusCell(PAYING_INFLIGHT)).toContain(TRANSFER_INFLIGHT_HINT)

    // 在途时也**没有**任何登记打款的入口（点了也不会弹窗）。
    rowTool(PAYING_INFLIGHT, EVENTS.markPaid)
    expect(LAYUI.prompts).toHaveLength(0)
  })

  it('`transfer_inflight` 缺键（前端新 / 后端旧）⇒ 不画登记打款，并如实说「暂不能登记打款」', async () => {
    // 契约里它是必填布尔；运行期缺键说明版本不一致 ⇒ 不能确认不在途（fail-closed，E-0021）。
    const missing = { ...PAYING_INFLIGHT } as Record<string, unknown>
    delete missing.transfer_inflight
    const row = missing as unknown as WithdrawRow
    expect(blocksManualPay(row)).toBe(true)
    expect(actionCell(row, { autoTransferEnabled: false, transferMaxAmount: '1000.00' })).not.toContain(
      EVENTS.markPaid,
    )
    expect(payingHint(row)).toBe(TRANSFER_FLAG_MISSING_HINT)
  })

  it('转账中点「刷新状态」：不弹二次确认，POST refresh 后重拉（上游失败原因那一格也显示）', async () => {
    rowsOnServer = [PAYING]
    await mount()
    expect(statusCell(PAYING)).toContain('上游返回：状态待确认')

    rowTool(PAYING, EVENTS.refresh)
    await flush()

    // 计划 §5 只要求四个写动作二次确认；刷新是查单（读语义）⇒ 不弹确认 / 输入框。
    expect(LAYUI.confirms).toHaveLength(0)
    expect(LAYUI.prompts).toHaveLength(0)
    expect(callTo('post', actionUrl(PAYING.id, 'refresh')).data).toBeUndefined()
    expect(callCount('get')).toBe(2)
    expect(LAYUI.messages).toContain(MESSAGES.refreshed)
  })

  // ── 操作列（纯函数，逐状态 + 开关 + 上限）────────────────────

  it('操作列按状态出现（判据只有 `actionCell` 一处）', () => {
    const on: Parameters<typeof actionCell>[1] = { autoTransferEnabled: true, transferMaxAmount: '1000.00' }
    const off: Parameters<typeof actionCell>[1] = { autoTransferEnabled: false, transferMaxAmount: '1000.00' }

    // 待审核：通过 + 驳回（与开关无关）。
    for (const ctx of [on, off]) {
      const cell = actionCell(PENDING, ctx)
      expect(cell).toContain(EVENTS.approve)
      expect(cell).toContain(EVENTS.reject)
      expect(cell).not.toContain(EVENTS.transfer)
    }
    // 已通过：登记打款恒在；代付只在开关开 + 金额 ≤ 上限时在。
    expect(actionCell(APPROVED, on)).toContain(EVENTS.transfer)
    expect(actionCell(APPROVED, off)).not.toContain(EVENTS.transfer)
    expect(actionCell(APPROVED_BIG, on)).not.toContain(EVENTS.transfer)
    expect(actionCell(APPROVED_BIG, on)).toContain(EVENTS.markPaid)
    // 转账中**不在途**：刷新 + 登记打款（能收场）；**在途**：只刷新（钱可能已在路上）。
    expect(actionCell(PAYING, on)).toContain(EVENTS.refresh)
    expect(actionCell(PAYING, on)).toContain(EVENTS.markPaid)
    expect(actionCell(PAYING_INFLIGHT, on)).toContain(EVENTS.refresh)
    expect(actionCell(PAYING_INFLIGHT, on)).not.toContain(EVENTS.markPaid)
    // 已打款 / 已驳回 / 失败：**没有**任何动作（fail-closed）。
    for (const row of [PAID, REJECTED, FAILED]) {
      expect(actionCell(row, on)).toBe('')
    }
  })

  it('金额比较只走整数分：非法值 / 超限 / 边界各钉一条', () => {
    expect(amountCents('88.00')).toBe(8800)
    expect(amountCents('0.29')).toBe(29) // `parseFloat('0.29') * 100` 会给 28.999999999999996
    expect(amountCents('1200')).toBe(120000)
    expect(amountCents('1.2')).toBe(120)
    // 非法输入一律回 `null`（空数组 = 一个都没混进来）。
    const accepted = ['', 'abc', '-1.00', '1.234', '1e3', '1,000.00', '.5'].filter(
      (value) => amountCents(value) !== null,
    )
    expect(accepted).toEqual([])
    const ctx = (max: string): Parameters<typeof canAutoTransfer>[1] => ({
      autoTransferEnabled: true,
      transferMaxAmount: max,
    })
    // 边界：等于上限 ⇒ **画**（计划 §9 第 1 条「超过上限」才拒）。
    expect(canAutoTransfer({ amount: '1000.00' }, ctx('1000.00'))).toBe(true)
    expect(canAutoTransfer({ amount: '1000.01' }, ctx('1000.00'))).toBe(false)
    // 开关不是真 `true`（例如字符串）⇒ 不画（fail-closed）。
    expect(
      canAutoTransfer({ amount: '1.00' }, { autoTransferEnabled: 'true' as unknown as boolean, transferMaxAmount: '1000.00' }),
    ).toBe(false)
    // 金额或上限解析不了 ⇒ 不画。
    expect(canAutoTransfer({ amount: 'x' }, ctx('1000.00'))).toBe(false)
  })

  it('开关位按真布尔读，上限原样保留（`toContext`）', () => {
    expect(
      toContext({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        auto_transfer_enabled: true,
        transfer_max_amount: '1000.00',
      }),
    ).toEqual({ autoTransferEnabled: true, transferMaxAmount: '1000.00' })
    expect(
      toContext({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        auto_transfer_enabled: false,
        transfer_max_amount: '0.00',
      }).autoTransferEnabled,
    ).toBe(false)
  })

  // ── tab 切换：只改 state 查询参数并重拉 ───────────────────────

  it('点「已打款」tab ⇒ 下一次列表请求带 state=paid，并灌入那一桶的行', async () => {
    await mount()
    expect(tableRows().map((r) => r.id)).toEqual([PENDING.id])

    const paidTab = root.querySelector<HTMLElement>(`[${TAB_ATTR}="paid"]`)
    expect(paidTab).not.toBeNull()
    paidTab!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    expect(callTo('get', WITHDRAWALS_URL).params).toEqual({ state: 'paid', page: 1 })
    // `rowsOnServer` 里没有 paid 行 ⇒ 这一桶是空的（空 ≠ 取数失败：失败会走 msg 那一支）。
    expect(tableRows()).toEqual([])

    // 再点同一桶：不重复发请求（只有那一次切换会重拉）。
    const before = callCount('get')
    paidTab!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(callCount('get')).toBe(before)
  })

  // ── ⭐「转账失败」tab（业务方 2026-10-20 补的只读桶）───────────────

  it('⭐「转账失败」只读：取 state=failed；状态列显示中文 + 失败原因；**一个操作按钮都没有**', async () => {
    rowsOnServer = [FAILED]
    await mount()

    const failedTab = root.querySelector<HTMLElement>(`[${TAB_ATTR}="failed"]`)
    expect(failedTab).not.toBeNull()
    failedTab!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    expect(callTo('get', WITHDRAWALS_URL).params).toEqual({ state: 'failed', page: 1 })
    expect(tableRows().map((row) => row.id)).toEqual([FAILED.id])
    // 状态列：中文名 + 失败原因（`transfer_error`），两样都要看得到。
    expect(statusCell(FAILED)).toContain('转账失败')
    expect(statusCell(FAILED)).toContain(FAILED.transfer_error)
    // ⛔ 钱已退回打手余额 ⇒ 这一桶**只读**：不重试、不登记打款、什么都不给。
    expect(actionTemplet()(FAILED)).toBe('')
    expect(LAYUI.confirms).toHaveLength(0)
    expect(LAYUI.prompts).toHaveLength(0)
  })

  // ── 权限门：fail-closed ─────────────────────────────────────

  it('⛔ 拿不到节点：一句提示 + 不注入标记 + 不发任何提现审核请求（于是也一个按钮都不画）', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-cs-2',
      display_name: '合成客服乙',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: [],
      permissions: [],
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(LAYUI.messages).toEqual([NO_ACCESS_MESSAGE])
    expect(LAYUI.messages).toEqual([MESSAGES.noAccess])
    expect(root.innerHTML).toBe('')
    expect(LAYUI.renders).toEqual([])
    // ⚠️ 准确的说法**不是**「零请求」：这道门本身就是一次 `fetchMe()`（身份请求，照发）。
    //    要钉的是「不发**提现审核**请求」——列表 0 次、5 个写动作 0 次。
    expect(mockedFetchMe).toHaveBeenCalledTimes(1)
    expect(callCount('get')).toBe(0)
    expect(callCount('post')).toBe(0)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  // ── 取全 + 上限（翻页取全，⛔ 不静默截断）────────────────────

  it('翻页取全：total 比一页多就继续翻第二页', async () => {
    const pages: Record<number, WithdrawListPage> = {
      1: {
        items: [PENDING],
        total: 2,
        page: 1,
        page_size: 20,
        auto_transfer_enabled: false,
        transfer_max_amount: '1000.00',
      },
      2: {
        items: [APPROVED],
        total: 2,
        page: 2,
        page_size: 20,
        auto_transfer_enabled: false,
        transfer_max_amount: '1000.00',
      },
    }
    const seen: number[] = []
    const data = await fetchAllWithdrawRows('pending', (params) => {
      seen.push(params.page)
      return Promise.resolve(pages[params.page]!)
    })
    expect(seen).toEqual([1, 2])
    expect(data.rows.map((r) => r.id)).toEqual([PENDING.id, APPROVED.id])
  })

  it('单据太多取不全 ⇒ 抛错（⛔ 不静默截断出错的账）', async () => {
    const fake = (params: { page: number }): Promise<WithdrawListPage> =>
      Promise.resolve({
        items: [PENDING],
        total: 99999,
        page: params.page,
        page_size: 20,
        auto_transfer_enabled: false,
        transfer_max_amount: '1000.00',
      })
    await expect(fetchAllWithdrawRows('pending', fake)).rejects.toThrow(MESSAGES.tooManyRows)
  })

  // ── 纯函数：展示 / 文案 ─────────────────────────────────────

  it('展示列：打手名空串回落登录账号；状态格带后端中文与失败原因；时间只截到秒', () => {
    expect(workerText({ display_name: '', username: 'synthetic_taker_x' })).toBe('synthetic_taker_x')
    expect(workerText({ display_name: '合成打手甲', username: 'ignored' })).toBe('合成打手甲')
    expect(statusCell(PAID)).toBe('<span>已打款</span>')
    expect(statusCell(PAYING)).toContain('上游返回：状态待确认')
    expect(formatTime('2026-10-20T10:00:00+08:00')).toBe('2026-10-20 10:00:00')
  })

  it('弹窗文案都带金额 + 账号 + 姓名（`subjectText` 是三处共用的唯一出处）', () => {
    const subject = subjectText(PENDING)
    expect(subject).toContain(PENDING.amount)
    expect(subject).toContain(PENDING.alipay_account)
    expect(subject).toContain(PENDING.alipay_name)
    expect(String(rejectPromptOptions(PENDING).title)).toContain(subject)
    expect(String(markPaidPromptOptions(PENDING).title)).toContain(subject)
    // 两个输入框的形态：驳回是多行文本域（formType 2）、单号是单行（formType 0）。
    expect(rejectPromptOptions(PENDING).formType).toBe(2)
    expect(markPaidPromptOptions(PENDING).formType).toBe(0)
    // 标记里只有一个面板、一个表、六个 tab。
    const html = buildHtml()
    expect(html).toContain(`class="${PANEL_CLASS}"`)
    expect(html).toContain(`id="${TABS_ID}"`)
    expect(html).toContain(`id="${TABLE_ID}"`)
    expect((html.match(new RegExp(TAB_ATTR, 'g')) ?? []).length).toBe(STATUS_TABS.length)
  })
})
