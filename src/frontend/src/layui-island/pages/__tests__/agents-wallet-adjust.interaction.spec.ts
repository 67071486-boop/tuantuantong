// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import {
  DEPOSIT_RULES_URL,
  TAKER_WALLET_NODE,
  WALLET_TRANSACTIONS_URL,
  WALLETS_URL,
  type DepositRulePage,
  type DepositRuleRow,
  type WalletKindOption,
  type WalletListPage,
  type WalletRow,
  type WalletTransactionPage,
  type WalletTransactionRow,
} from '@/api/takerWallets'

/**
 * 【财务相关 → 打手钱包管理】island 页的**交互**测试（2026-10-20，`feat/taker-wallet-adjust`）。
 *
 * 这一页是**自有功能**（对标物没有「客服给打手调账 / 按游戏改押金」），所以没有证据件可对拍；
 * 本文件钉的是**有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有一条能红的交互用例）：
 *    挂载 → 点「调账」→ 弹层里填金额 / 原因 → 保存 → **二次确认**（把「调整前 → 调整后」摆出来）
 *    → 真发 `POST …/adjust/` → **列表被重拉**；押金页：切 tab → 点「修改金额」→ `layer.prompt`
 *    → 二次确认 → 真发 `PUT …/deposit-rules/<id>/` → **规则列表被重拉**。
 * 2. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量；以 `(` 开头的语句前置分号；
 *    挂载顺序以**运行期 DOM** 判断（`bootstrapIsland` 看不到标记、`initIslandElements` 看得到）。
 * 3. ⭐ **权限门 fail-closed**：拿不到 `finance.taker_wallet_adjust` ⇒ 一句提示 + **不注入标记** +
 *    **不发任何钱包 / 押金请求**（`fetchMe()` 那一次身份请求照发——它就是这道门本身）。
 * 4. ⭐ **幂等**：同一个弹层里点两次「保存」必须带**同一个** `client_request_no`；
 *    关掉重开才换新的（⛔ 那等于没有幂等）。
 * 5. ⭐ **扣成负数在页面就被挡住**（不发请求）；后端还有同一道闸。
 * 6. ⭐ **押金页必须写明「改押金会直接影响谁能接单」**（业务方硬要求）。
 *
 * ## 手法（照 `agents-withdraw-review.interaction.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/takerWallets` 跑的是**真实现**；
 * - 服务端响应是**活的**：写动作会改 `walletsOnServer` / `rulesOnServer`。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：打手名 / 手机号 / 游戏名都是编的。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const renders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const confirms: Array<{ text: string; options: unknown; ok: () => void }> = []
  const prompts: Array<{ options: Record<string, unknown>; cb: (value: string, index: number) => void }> = []
  const closes: number[] = []
  const handlers: Record<string, (obj: unknown) => void> = {}
  const formRenders: string[] = []
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    renders,
    reloads,
    opens,
    confirms,
    prompts,
    closes,
    handlers,
    formRenders,
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
      open: vi.fn((o: Record<string, unknown>) => {
        opens.push(o)
        // 把弹层内容真的塞进 DOM：页面用 `document.getElementById(ADJUST_FORM_ID)` 读字段。
        const host = document.createElement('div')
        host.innerHTML = String(o.content ?? '')
        document.body.appendChild(host)
        return opens.length
      }),
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
    form: {
      render: vi.fn((type?: string) => {
        formRenders.push(type ?? '')
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#walletAdjustTabs') !== null)
    return { table: LAYUI.table, layer: LAYUI.layer, form: LAYUI.form }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#walletAdjustTabs') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  ADJUST_FORM_ID,
  DEPOSIT_TABLE_ID,
  DEPOSIT_WARNING,
  EVENTS,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PANEL_CLASS,
  PANEL_SELECTOR,
  SEARCH_BUTTON_ID,
  SEARCH_INPUT_ID,
  TAB_ATTR,
  TABS_ID,
  TRANSACTION_TABLE_ID,
  TX_DATE_FROM_ID,
  TX_KIND_SELECT_ID,
  TX_HINT_ID,
  TX_NEXT_PAGE_ID,
  TX_SEARCH_INPUT_ID,
  WALLET_HINT_ID,
  WALLET_NEXT_PAGE_ID,
  WALLET_PREV_PAGE_ID,
  WALLET_TABLE_ID,
  __testables,
} from '../agents-wallet-adjust'

const {
  amountCents,
  buildHtml,
  centsText,
  depositEnoughText,
  depositRequiredText,
  depositSourceText,
  kindOptionsHtml,
  newClientRequestNo,
  pageHint,
  previewAdjustment,
  previewText,
} = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

type Cfg = { method?: string; url?: string; data?: unknown; params?: Record<string, unknown> }

const ADJUST_URL = (userId: number): string => `${WALLETS_URL}${userId}/adjust/`
const RULE_URL = (gameId: number): string => `${DEPOSIT_RULES_URL}${gameId}/`

/** 一个打手（**合成值**）。 */
function walletRow(over: Partial<WalletRow> & { user_id: number }): WalletRow {
  return {
    username: `synthetic_taker_${over.user_id}`,
    display_name: `合成打手${over.user_id}`,
    mobile: `1370000${String(over.user_id).padStart(4, '0')}`,
    balance: '100.00',
    frozen: '50.00',
    deposit_required: '50.00',
    deposit_enough: true,
    updated_at: '2026-10-20T10:00:00+08:00',
    ...over,
  }
}

const WALLET_A = walletRow({ user_id: 101 })
const WALLET_B = walletRow({
  user_id: 102,
  display_name: '合成打手乙',
  balance: '0.00',
  frozen: '0.00',
  deposit_enough: false,
})

function ruleRow(over: Partial<DepositRuleRow> & { game_id: number }): DepositRuleRow {
  return {
    game_name: `合成游戏${over.game_id}`,
    amount: '50.00',
    source: 'default',
    rule_id: null,
    updated_at: null,
    ...over,
  }
}

/** 一条流水（**合成值**）。 */
function txRow(over: Partial<WalletTransactionRow> & { id: number }): WalletTransactionRow {
  return {
    user_id: 101,
    username: 'synthetic_taker_101',
    display_name: '合成打手101',
    mobile: '13700000101',
    kind: 'recharge',
    kind_label: '充值',
    amount: '100.00',
    balance_after: '100.00',
    frozen_after: '0.00',
    note: '合成测试：充值',
    ref_no: 'R-1001',
    created_at: '2026-10-20T10:00:00+08:00',
    ...over,
  }
}

/** 类型下拉的候选（后端把 `KIND_CHOICES` 随每页响应带回来；这里只取三条）。 */
const KINDS: WalletKindOption[] = [
  { value: 'recharge', label: '充值' },
  { value: 'adjust_add', label: '后台加款' },
  { value: 'frozen_sub', label: '后台减押金' },
]

const TX_A = txRow({ id: 9001 })
const TX_B = txRow({
  id: 9002,
  user_id: 102,
  username: 'synthetic_taker_102',
  display_name: '合成打手102',
  kind: 'adjust_add',
  kind_label: '后台加款',
  amount: '50.00',
  balance_after: '150.00',
  note: '合成测试：补发',
  ref_no: 'R-1002',
  created_at: '2026-10-20T11:00:00+08:00',
})

const RULE_A = ruleRow({ game_id: 201 })
const RULE_B = ruleRow({
  game_id: 202,
  game_name: '合成三角洲',
  amount: '300.00',
  source: 'default_delta',
  rule_id: 9,
  updated_at: '2026-10-19T09:00:00+08:00',
})

let routes: Record<string, (data?: unknown) => unknown>

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

function flush(): Promise<void> {
  return (async () => {
    for (let i = 0; i < 8; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

function callTo(method: string, url: string): Cfg {
  const found = mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url)
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

function callCount(method: string, url?: string): number {
  return mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method)
    .filter((c) => (url === undefined ? true : c.url === url))
    .length
}

function tableRows(id: string): Array<Record<string, unknown>> {
  const last = [...LAYUI.reloads].reverse().find((r) => r.id === id)
  if (!last) throw new Error(`没有对 ${id} 调用过 reloadData`)
  return last.data as Array<Record<string, unknown>>
}

function rowTool(tableId: string, row: unknown, event: string): void {
  LAYUI.handlers[`tool(${tableId})`]!({ data: row, event })
}

/** 操作列的 templet（从页面真正 render 出去的那份 cols 上取，⛔ 不自己再拼一份判据）。 */
function actionTemplet(tableId: string): (row: unknown) => string {
  const cfg = LAYUI.renders.find((r) => r.id === tableId)
  if (!cfg) throw new Error(`页面没有 table.render(${tableId})`)
  const cols = cfg.cols as Array<Array<{ title?: string; templet?: unknown }>>
  const cell = cols[0]?.find((c) => c.title === '操作')
  if (!cell || typeof cell.templet !== 'function') throw new Error('操作列没有函数 templet')
  return cell.templet as (row: unknown) => string
}

function fillAdjustForm(amount: string, reason: string, target = 'balance', direction = 'increase'): void {
  const host = document.getElementById(ADJUST_FORM_ID)!
  ;(host.querySelector('[name="amount"]') as HTMLInputElement).value = amount
  ;(host.querySelector('[name="reason"]') as HTMLTextAreaElement).value = reason
  ;(host.querySelector('[name="target"]') as HTMLSelectElement).value = target
  ;(host.querySelector('[name="direction"]') as HTMLSelectElement).value = direction
}

/** 把弹层里最后那个 `yes` 回调点下去（`layer.open` 的「保存」）。 */
function clickSave(): void {
  const open = LAYUI.opens[LAYUI.opens.length - 1]!
  ;(open.yes as (index: number) => void)(LAYUI.opens.length)
}

function userWithoutNode() {
  return {
    id: 2,
    username: 'synthetic-no-node',
    display_name: '合成无权限',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    menu_permissions: [],
    permissions: [],
  }
}

describe('打手钱包管理 island 页（agents-wallet-adjust）', () => {
  let root: HTMLElement
  let walletsOnServer: WalletRow[]
  let rulesOnServer: DepositRuleRow[]
  let txsOnServer: WalletTransactionRow[]
  /** 下一次针对该页码的钱包请求抛业务码 1002（模拟「人数变少后页码越界」）。 */
  let failNextWalletPage: number | null

  beforeEach(() => {
    vi.clearAllMocks()
    for (const bucket of [
      LAYUI.messages, LAYUI.renders, LAYUI.opens, LAYUI.confirms, LAYUI.prompts,
      LAYUI.closes, LAYUI.formRenders, LAYUI.seenByBootstrap, LAYUI.seenByInit,
    ]) bucket.length = 0
    for (const key of Object.keys(LAYUI.handlers)) delete LAYUI.handlers[key]
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    mockedFetchMe.mockResolvedValue({
      id: 1,
      username: 'synthetic-cs',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: [TAKER_WALLET_NODE],
      permissions: [],
    })

    routes = {}
    walletsOnServer = [WALLET_A, WALLET_B]
    rulesOnServer = [RULE_A, RULE_B]
    txsOnServer = [TX_A, TX_B]
    failNextWalletPage = null

    ready(`POST ${ADJUST_URL(WALLET_A.user_id)}`, (data) => {
      const body = data as {
        target: string
        direction: string
        amount: string
        reason: string
      }
      const delta = Number(body.amount)
      const next = body.direction === 'increase' ? delta : -delta
      walletsOnServer = walletsOnServer.map((row) =>
        row.user_id === WALLET_A.user_id
          ? body.target === 'balance'
            ? { ...row, balance: (Number(row.balance) + next).toFixed(2) }
            : { ...row, frozen: (Number(row.frozen) + next).toFixed(2) }
          : row,
      )
      return {
        user_id: WALLET_A.user_id,
        username: WALLET_A.username,
        display_name: WALLET_A.display_name,
        transaction_id: 7001,
        kind: 'adjust_add',
        amount: '50.00',
        reason: body.reason,
        balance_before: '100.00',
        balance_after: '150.00',
        frozen_before: '50.00',
        frozen_after: '50.00',
        duplicate: false,
        created_at: '2026-10-20T12:00:00+08:00',
      }
    })
    ready(`PUT ${RULE_URL(RULE_A.game_id)}`, (data) => {
      const amount = String((data as { amount?: string }).amount ?? '')
      rulesOnServer = rulesOnServer.map((row) =>
        row.game_id === RULE_A.game_id
          ? { ...row, amount, source: 'rule' as const, rule_id: 31, updated_at: '2026-10-20T12:00:00+08:00' }
          : row,
      )
      return {
        game_id: RULE_A.game_id,
        game_name: RULE_A.game_name,
        amount,
        source: 'rule',
        rule_id: 31,
        updated_at: '2026-10-20T12:00:00+08:00',
        created: true,
      }
    })

    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        if (key === `GET ${WALLETS_URL}`) {
          // 服务端分页 / 排序：测试服务端**真的按参数**切页与排序（⛔ 不是永远回全量）。
          const q = String(cfg.params?.q ?? '')
          const sort = String(cfg.params?.sort ?? 'frozen')
          const order = String(cfg.params?.order ?? 'desc')
          const pageNo = Number(cfg.params?.page ?? 1)
          const pageSize = Number(cfg.params?.page_size ?? 20)
          if (failNextWalletPage !== null && pageNo === failNextWalletPage) {
            failNextWalletPage = null
            throw new BizError(1002, '记录不存在或已被删除')
          }
          const filtered =
            q === ''
              ? [...walletsOnServer]
              : walletsOnServer.filter(
                  (row) =>
                    row.username.includes(q) ||
                    row.display_name.includes(q) ||
                    row.mobile.includes(q),
                )
          const direction = order === 'asc' ? 1 : -1
          const sorted = [...filtered].sort((a, b) => {
            if (sort === 'username') return a.username.localeCompare(b.username) * direction
            const key = sort === 'balance' ? 'balance' : 'frozen'
            const diff = Number(a[key]) - Number(b[key])
            if (diff !== 0) return diff * direction
            const other = key === 'frozen' ? 'balance' : 'frozen'
            return Number(b[other]) - Number(a[other]) || a.username.localeCompare(b.username)
          })
          const start = (pageNo - 1) * pageSize
          const pageData: WalletListPage = {
            items: sorted.slice(start, start + pageSize),
            total: sorted.length,
            page: pageNo,
            page_size: pageSize,
            adjust_max_amount: '1000.00',
          }
          return pageData
        }
        if (key === `GET ${WALLET_TRANSACTIONS_URL}`) {
          const q = String(cfg.params?.q ?? '')
          const kind = String(cfg.params?.kind ?? '')
          const dateFrom = String(cfg.params?.date_from ?? '')
          const dateTo = String(cfg.params?.date_to ?? '')
          const pageNo = Number(cfg.params?.page ?? 1)
          const pageSize = Number(cfg.params?.page_size ?? 20)
          const filtered = txsOnServer.filter((row) => {
            if (kind !== '' && row.kind !== kind) return false
            if (
              q !== '' &&
              !row.username.includes(q) &&
              !row.display_name.includes(q) &&
              !row.mobile.includes(q)
            ) {
              return false
            }
            const day = row.created_at.slice(0, 10)
            if (dateFrom !== '' && day < dateFrom) return false
            if (dateTo !== '' && day > dateTo) return false
            return true
          })
          const start = (pageNo - 1) * pageSize
          const pageData: WalletTransactionPage = {
            items: filtered.slice(start, start + pageSize),
            total: filtered.length,
            page: pageNo,
            page_size: pageSize,
            kinds: KINDS,
          }
          return pageData
        }
        if (key === `GET ${DEPOSIT_RULES_URL}`) {
          const pageData: DepositRulePage = {
            items: [...rulesOnServer],
            default_amount: '50.00',
            delta_amount: '300.00',
          }
          return pageData
        }
        const fn = routes[key]
        if (!fn) throw new Error(`测试没有为 ${key} 准备响应`)
        return fn(cfg.data)
      }),
    )
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('挂载：先挂样式、再塞标记、最后 init；钱包列表上屏', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    // 陷阱 ③：运行期判据——bootstrap 时看不到标记，init 时看得到。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    expect(root.innerHTML).toContain(PANEL_CLASS)
    // 标记形状：三个 tab（打手钱包 / 押金金额 / 流水明细）+ 三张表。
    expect(root.querySelectorAll(`[${TAB_ATTR}]`).length).toBe(3)
    expect(root.querySelector(`#${WALLET_TABLE_ID}`)).not.toBeNull()
    expect(tableRows(WALLET_TABLE_ID).map((r) => r.user_id)).toEqual([101, 102])
    expect(LAYUI.messages).toEqual([])
  })

  it('搜索：按关键词打 `?q=` 并重拉列表', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    ;(root.querySelector(`#${SEARCH_INPUT_ID}`) as HTMLInputElement).value = '101'
    ;(root.querySelector(`#${SEARCH_BUTTON_ID}`) as HTMLElement).click()
    await flush()

    const cfg = callTo('get', WALLETS_URL)
    // 服务端分页 + 排序：搜索带上页码（回第 1 页）、page_size 20 与缺省 frozen desc。
    expect(cfg.params).toEqual({
      q: '101',
      page: 1,
      page_size: 20,
      sort: 'frozen',
      order: 'desc',
    })
    expect(tableRows(WALLET_TABLE_ID).map((r) => r.user_id)).toEqual([101])
    expect(callCount('get', WALLETS_URL)).toBe(2)
  })

  it('排序：两张表头带 sort:true，`table.on(sort)` 把 sort/order 传给后端并回第 1 页', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    // 只有「可用余额」「押金冻结」两列可排序（其余列 sort 不是 true）。
    const cfg = LAYUI.renders.find((r) => r.id === WALLET_TABLE_ID)!
    const cols = cfg.cols as Array<Array<{ field?: string; sort?: boolean }>>
    expect(cols[0]!.filter((c) => c.sort === true).map((c) => c.field)).toEqual([
      'balance',
      'frozen',
    ])

    LAYUI.handlers[`sort(${WALLET_TABLE_ID})`]!({ field: 'balance', type: 'asc' })
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({
      sort: 'balance',
      order: 'asc',
      page: 1,
    })

    LAYUI.handlers[`sort(${WALLET_TABLE_ID})`]!({ field: 'frozen', type: 'desc' })
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({
      sort: 'frozen',
      order: 'desc',
      page: 1,
    })
  })

  it('服务端分页：翻到第 2 页请求 page=2，页码提示与末页禁用都对', async () => {
    walletsOnServer = Array.from({ length: 25 }, (_, index) =>
      walletRow({
        user_id: 1000 + index,
        username: `synthetic_page_${String(index).padStart(2, '0')}`,
      }),
    )
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(tableRows(WALLET_TABLE_ID).length).toBe(20)
    expect(root.querySelector(`#${WALLET_HINT_ID}`)!.textContent).toBe('第 1 / 2 页，共 25 人')
    expect((root.querySelector(`#${WALLET_PREV_PAGE_ID}`) as HTMLButtonElement).disabled).toBe(
      true,
    )

    ;(root.querySelector(`#${WALLET_NEXT_PAGE_ID}`) as HTMLElement).click()
    await flush()

    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 2, page_size: 20 })
    expect(tableRows(WALLET_TABLE_ID).length).toBe(5)
    expect(root.querySelector(`#${WALLET_HINT_ID}`)!.textContent).toBe('第 2 / 2 页，共 25 人')
    expect((root.querySelector(`#${WALLET_NEXT_PAGE_ID}`) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('翻到第 2 页后：排序 / 搜索 / 切 tab 都回第 1 页', async () => {
    walletsOnServer = Array.from({ length: 25 }, (_, index) =>
      walletRow({
        user_id: 1000 + index,
        username: `synthetic_page_${String(index).padStart(2, '0')}`,
      }),
    )
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    const clickNext = (): void => {
      ;(root.querySelector(`#${WALLET_NEXT_PAGE_ID}`) as HTMLElement).click()
    }

    clickNext()
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 2 })

    // 排序 ⇒ 回第 1 页
    LAYUI.handlers[`sort(${WALLET_TABLE_ID})`]!({ field: 'balance', type: 'asc' })
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({
      page: 1,
      sort: 'balance',
      order: 'asc',
    })

    // 再翻到第 2 页 ⇒ 搜索回第 1 页
    clickNext()
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 2 })
    ;(root.querySelector(`#${SEARCH_BUTTON_ID}`) as HTMLElement).click()
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 1 })

    // 再翻到第 2 页 ⇒ 切走再切回（进入钱包 tab 回第 1 页）
    clickNext()
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 2 })
    ;(root.querySelector(`[${TAB_ATTR}="transactions"]`) as HTMLElement).click()
    await flush()
    ;(root.querySelector(`[${TAB_ATTR}="wallets"]`) as HTMLElement).click()
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 1 })
  })

  it('第三次点同一列表头（layui 给 type=null）回默认 frozen desc', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    LAYUI.handlers[`sort(${WALLET_TABLE_ID})`]!({ field: 'balance', type: 'asc' })
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({
      sort: 'balance',
      order: 'asc',
    })

    // ⚠️ null 不是 asc：回缺省 frozen desc（与首屏 `initSort` 一致）。
    LAYUI.handlers[`sort(${WALLET_TABLE_ID})`]!({ field: 'balance', type: null })
    await flush()
    expect(callTo('get', WALLETS_URL).params).toMatchObject({
      sort: 'frozen',
      order: 'desc',
      page: 1,
    })
  })

  it('页码越界（业务码 1002）：自动退一页重拉，且不弹错误', async () => {
    walletsOnServer = Array.from({ length: 25 }, (_, index) =>
      walletRow({
        user_id: 1000 + index,
        username: `synthetic_page_${String(index).padStart(2, '0')}`,
      }),
    )
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    // 第 2 页那一次请求回业务码 1002（真实形态：HTTP 200 + 信封）。
    failNextWalletPage = 2
    ;(root.querySelector(`#${WALLET_NEXT_PAGE_ID}`) as HTMLElement).click()
    await flush()

    const pages = mockedRequest.mock.calls
      .map((call) => call[0] as Cfg)
      .filter((cfg) => cfg.url === WALLETS_URL)
      .map((cfg) => Number(cfg.params?.page))
    // 先 page=2（失败）→ 自动退一页 page=1（成功）。
    expect(pages.slice(-2)).toEqual([2, 1])
    expect(callTo('get', WALLETS_URL).params).toMatchObject({ page: 1 })
    expect(tableRows(WALLET_TABLE_ID).length).toBe(20)
    expect(root.querySelector(`#${WALLET_HINT_ID}`)!.textContent).toBe('第 1 / 2 页，共 25 人')
    // 越界是**可恢复**的分页情况：不弹错误、不把 1002 当权限问题。
    expect(LAYUI.messages).toEqual([])
  })

  it('流水 tab：类型下拉来自响应、筛选改选即查、翻页用独立页码', async () => {
    txsOnServer = Array.from({ length: 25 }, (_, index) =>
      txRow({
        id: 9100 + index,
        kind: index % 2 === 0 ? 'recharge' : 'adjust_add',
        kind_label: index % 2 === 0 ? '充值' : '后台加款',
        created_at: `2026-10-${String(1 + (index % 20)).padStart(2, '0')}T10:00:00+08:00`,
      }),
    )
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    ;(root.querySelector(`[${TAB_ATTR}="transactions"]`) as HTMLElement).click()
    await flush()

    const first = callTo('get', WALLET_TRANSACTIONS_URL)
    expect(first.params).toMatchObject({ page: 1, page_size: 20 })
    expect(tableRows(TRANSACTION_TABLE_ID).length).toBe(20)
    expect(root.querySelector(`#${TX_HINT_ID}`)!.textContent).toBe('第 1 / 2 页，共 25 笔')

    // 类型下拉的选项**来自后端响应的 kinds**（+「全部类型」），前端不另抄词表。
    const options = Array.from(
      root.querySelectorAll(`#${TX_KIND_SELECT_ID} option`),
    ).map((option) => option.textContent)
    expect(options).toEqual(['全部类型', '充值', '后台加款', '后台减押金'])

    // 改选类型 ⇒ 回第 1 页并带 kind。
    const kindSelect = root.querySelector(`#${TX_KIND_SELECT_ID}`) as HTMLSelectElement
    kindSelect.value = 'adjust_add'
    kindSelect.dispatchEvent(new Event('change'))
    await flush()
    expect(callTo('get', WALLET_TRANSACTIONS_URL).params).toMatchObject({
      kind: 'adjust_add',
      page: 1,
    })
    expect(tableRows(TRANSACTION_TABLE_ID).every((row) => row.kind === 'adjust_add')).toBe(true)

    // 日期改选 ⇒ 也回第 1 页并带 date_from。
    kindSelect.value = ''
    kindSelect.dispatchEvent(new Event('change'))
    await flush()
    const from = root.querySelector(`#${TX_DATE_FROM_ID}`) as HTMLInputElement
    from.value = '2026-10-02'
    from.dispatchEvent(new Event('change'))
    await flush()
    expect(callTo('get', WALLET_TRANSACTIONS_URL).params).toMatchObject({
      date_from: '2026-10-02',
      page: 1,
    })

    // 搜索框按回车 ⇒ 带 q 重查。
    const search = root.querySelector(`#${TX_SEARCH_INPUT_ID}`) as HTMLInputElement
    search.value = 'synthetic_taker_102'
    search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await flush()
    expect(callTo('get', WALLET_TRANSACTIONS_URL).params).toMatchObject({
      q: 'synthetic_taker_102',
      page: 1,
    })

    // 清掉搜索（否则只剩 0 行、没有第 2 页），再翻到第 2 页（流水的页码与钱包独立）。
    search.value = ''
    search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await flush()
    ;(root.querySelector(`#${TX_NEXT_PAGE_ID}`) as HTMLElement).click()
    await flush()
    expect(callTo('get', WALLET_TRANSACTIONS_URL).params).toMatchObject({
      page: 2,
      page_size: 20,
    })
  })

  it('调账：弹层里摆出「调整前 → 调整后」，二次确认后真发请求并重拉', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    const templet = actionTemplet(WALLET_TABLE_ID)
    expect(templet(WALLET_A)).toContain(`lay-event="${EVENTS.adjust}"`)
    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)

    // 弹层开了，里面已经写明「当前」并带预览位。
    expect(LAYUI.opens.length).toBe(1)
    expect(String(LAYUI.opens[0]!.content)).toContain('当前：可用余额 100.00 元')
    fillAdjustForm('50.00', '合成测试：补发活动奖励')
    // 输入即预览（事件委托）。
    document.getElementById(ADJUST_FORM_ID)!.dispatchEvent(new Event('input'))
    expect(document.getElementById('walletAdjustPreview')!.textContent).toContain('100.00 → 150.00')
    // `form.render('select')` 真的被调了（layui 的下拉要它才有样式）。
    expect(LAYUI.formRenders).toContain('select')

    clickSave()
    // 第一次点保存：只弹二次确认，**还没发请求**。
    expect(LAYUI.confirms.length).toBe(1)
    expect(LAYUI.confirms[0]!.text).toContain('调整前 → 调整后')
    expect(LAYUI.confirms[0]!.text).toContain('100.00 → 150.00')
    expect(callCount('post', ADJUST_URL(WALLET_A.user_id))).toBe(0)

    LAYUI.confirms[0]!.ok()
    await flush()

    const cfg = callTo('post', ADJUST_URL(WALLET_A.user_id))
    expect(cfg.data).toEqual({
      target: 'balance',
      direction: 'increase',
      amount: '50.00',
      reason: '合成测试：补发活动奖励',
      client_request_no: expect.stringMatching(/^[0-9a-zA-Z-]+$/),
    })
    // 写成功 ⇒ 重新拉一次钱包列表（次数从 1 变 2）。
    expect(callCount('get', WALLETS_URL)).toBe(2)
    expect(LAYUI.messages.some((m) => m.includes('已调账'))).toBe(true)
  })

  it('XSS：display_name 含 `<img onerror>` 时，二次确认框正文已转义（G3）', async () => {
    // display_name 没有字符白名单（后端序列化器只限长度）⇒ 持「账号编辑」节点的人写一个昵称，
    // 管理员一打开调账确认就会在**管理员会话**里执行脚本。判据打在真正交给 `layer.confirm`
    // 的那段文本上（⛔ 不是页面里某处 innerHTML）：确认框 content 被 vendored layui 当 HTML 渲染。
    const evil = walletRow({
      user_id: 103,
      display_name: '<img src=x onerror=alert(1)>',
    })
    walletsOnServer = [evil]
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    rowTool(WALLET_TABLE_ID, evil, EVENTS.adjust)
    fillAdjustForm('1.00', '合成原因 <script>alert(2)</script>')
    clickSave()

    expect(LAYUI.confirms.length).toBe(1)
    const text = LAYUI.confirms[0]!.text
    // 两处都转义：打手名（display_name）与客服填的原因。
    expect(text).not.toContain('<img')
    expect(text).not.toContain('<script>')
    expect(text).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(text).toContain('&lt;script&gt;alert(2)&lt;/script&gt;')
  })

  it('调账：缺原因 / 扣成负数都在页面挡住，不发请求', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)

    fillAdjustForm('50.00', '   ')
    clickSave()
    expect(LAYUI.messages).toContain(MESSAGES.reasonRequired)
    expect(LAYUI.confirms.length).toBe(0)

    LAYUI.messages.length = 0
    fillAdjustForm('999.00', '合成测试：扣太多', 'balance', 'decrease')
    clickSave()
    expect(LAYUI.messages).toContain(MESSAGES.balanceNotEnough)
    expect(LAYUI.confirms.length).toBe(0)
    expect(callCount('post', ADJUST_URL(WALLET_A.user_id))).toBe(0)
  })

  it('幂等键：每次打开弹层生成一个客户端单号（关掉重开换新的）', async () => {
    // ⚠️ 真幂等（同一单号重复提交只记一次账）由后端用例
    // `tests.test_taker_wallet_admin_api.TestWalletAdjustApi.test_same_client_request_no_is_idempotent`
    // 钉住；本用例只钉**前端这一半**：请求体必须带一个非空单号，且**新的一笔**换新单号
    // （⛔ 不许写成常量——那会让所有调账被后端判成重复提交）。
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)
    fillAdjustForm('50.00', '合成测试：第一笔')
    clickSave()
    LAYUI.confirms[0]!.ok()
    await flush()

    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)
    fillAdjustForm('50.00', '合成测试：第二笔')
    clickSave()
    LAYUI.confirms[1]!.ok()
    await flush()

    const calls = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .filter((c) => c.url === ADJUST_URL(WALLET_A.user_id))
    expect(calls.length).toBe(2)
    const firstNo = (calls[0]!.data as { client_request_no: string }).client_request_no
    const secondNo = (calls[1]!.data as { client_request_no: string }).client_request_no
    expect(firstNo).not.toBe('')
    expect(secondNo).not.toBe('')
    expect(firstNo).not.toBe(secondNo)
    expect(newClientRequestNo()).not.toBe(newClientRequestNo())
  })

  it('幂等命中：`duplicate=true` 时如实提示「没有重复记账」', async () => {
    ready(`POST ${ADJUST_URL(WALLET_A.user_id)}`, () => ({
      user_id: WALLET_A.user_id,
      username: WALLET_A.username,
      display_name: WALLET_A.display_name,
      transaction_id: 7001,
      kind: 'adjust_add',
      amount: '50.00',
      reason: '合成测试',
      balance_before: '100.00',
      balance_after: '150.00',
      frozen_before: '50.00',
      frozen_after: '50.00',
      duplicate: true,
      created_at: '2026-10-20T12:00:00+08:00',
    }))
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)
    fillAdjustForm('50.00', '合成测试')
    clickSave()
    LAYUI.confirms[0]!.ok()
    await flush()

    expect(LAYUI.messages).toContain(MESSAGES.adjustDuplicate)
  })

  it('押金页：写明「影响谁能接单」；改金额走 prompt + 确认 + PUT 并重拉', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    // 标记里就有那句必读提示（业务方硬要求）。
    expect(root.innerHTML).toContain(DEPOSIT_WARNING)

    // 切 tab（页面按 tab 懒加载）。
    ;(root.querySelector(`[${TAB_ATTR}="deposit-rules"]`) as HTMLElement).click()
    await flush()
    expect(callCount('get', DEPOSIT_RULES_URL)).toBe(1)
    expect(tableRows(DEPOSIT_TABLE_ID).map((r) => r.game_id)).toEqual([201, 202])
    expect(actionTemplet(DEPOSIT_TABLE_ID)(RULE_A)).toContain(`lay-event="${EVENTS.editRule}"`)
    // 来源的中文说明（机器值 → 人话）。
    expect(depositSourceText('rule')).toBe('后台规则')
    expect(depositSourceText('default')).toBe('默认 50')

    rowTool(DEPOSIT_TABLE_ID, RULE_A, EVENTS.editRule)
    expect(LAYUI.prompts.length).toBe(1)
    expect(String(LAYUI.prompts[0]!.options.title)).toContain(RULE_A.game_name)
    LAYUI.prompts[0]!.cb('80.00', 7)
    expect(LAYUI.confirms.length).toBe(1)
    expect(LAYUI.confirms[0]!.text).toContain('谁能接')
    expect(callCount('put', RULE_URL(RULE_A.game_id))).toBe(0)

    LAYUI.confirms[0]!.ok()
    await flush()
    expect(callTo('put', RULE_URL(RULE_A.game_id)).data).toEqual({ amount: '80.00' })
    expect(callCount('get', DEPOSIT_RULES_URL)).toBe(2)
    expect(LAYUI.messages.some((m) => m.includes('押金改为 80.00'))).toBe(true)
  })

  it('押金页：非法金额（负数 / 三位小数）当场提示，不发请求', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    ;(root.querySelector(`[${TAB_ATTR}="deposit-rules"]`) as HTMLElement).click()
    await flush()

    rowTool(DEPOSIT_TABLE_ID, RULE_A, EVENTS.editRule)
    LAYUI.prompts[0]!.cb('-5', 1)
    expect(LAYUI.messages).toContain(MESSAGES.ruleAmountInvalid)
    expect(LAYUI.confirms.length).toBe(0)

    LAYUI.prompts[0]!.cb('1.234', 1)
    expect(LAYUI.messages).toContain(MESSAGES.ruleAmountInvalid)
    expect(callCount('put', RULE_URL(RULE_A.game_id))).toBe(0)
  })

  it('无节点：一句提示、不注入标记、不发任何钱包 / 押金请求', async () => {
    mockedFetchMe.mockResolvedValue(userWithoutNode())
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(root.innerHTML).toBe('')
    expect(LAYUI.renders.length).toBe(0)
    // 只有 fetchMe 那一次身份请求（它就是门本身），业务请求一个都没有。
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('后端失败：如实显示后端 msg（`BizError`），不编第二句', async () => {
    ready(`POST ${ADJUST_URL(WALLET_A.user_id)}`, () => {
      throw new BizError(1000, '单笔调账金额不能超过 1000 元')
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    rowTool(WALLET_TABLE_ID, WALLET_A, EVENTS.adjust)
    fillAdjustForm('50.00', '合成测试')
    clickSave()
    LAYUI.confirms[0]!.ok()
    await flush()

    expect(LAYUI.messages).toContain('单笔调账金额不能超过 1000 元')
    // ⭐ 失败**不关层**：再点一次「保存」仍带同一个客户端单号（上一次可能其实已记账，
    //    只是回包丢了 → 后端靠幂等键不会重复记第二笔）。
    expect(document.getElementById(ADJUST_FORM_ID)).not.toBeNull()
    clickSave()
    LAYUI.confirms[1]!.ok()
    await flush()
    const bodies = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .filter((c) => c.url === ADJUST_URL(WALLET_A.user_id))
      .map((c) => (c.data as { client_request_no: string }).client_request_no)
    expect(bodies.length).toBe(2)
    expect(bodies[0]).toBe(bodies[1])
  })

  it('纯函数：金额走整数分、预览与上限、客户端单号形状', () => {
    expect(amountCents('0.29')).toBe(29)
    expect(amountCents('12.30')).toBe(1230)
    expect(amountCents('-1')).toBeNull()
    expect(amountCents('1.234')).toBeNull()
    expect(centsText(1230)).toBe('12.30')
    expect(centsText(5)).toBe('0.05')

    const up = previewAdjustment(WALLET_A, 'balance', 'increase', '50.00', 100000)
    expect(up.error).toBeNull()
    expect(up.balanceAfter).toBe('150.00')
    expect(previewText(up)).toContain('100.00 → 150.00')

    const down = previewAdjustment(WALLET_A, 'frozen', 'decrease', '10.00', 100000)
    expect(down.frozenAfter).toBe('40.00')

    // 扣成负数 / 超上限：预览就带 error（页面据此不发请求）。
    expect(previewAdjustment(WALLET_A, 'balance', 'decrease', '100.01', 100000).error).toBe(
      MESSAGES.balanceNotEnough,
    )
    expect(previewAdjustment(WALLET_A, 'balance', 'increase', '1000.01', 100000).error).toBe(
      MESSAGES.amountTooLarge('1000.00'),
    )

    expect(pageHint(2, 25, 20, '人')).toBe('第 2 / 2 页，共 25 人')
    expect(pageHint(1, 0, 20, '笔')).toBe('第 1 / 1 页，共 0 笔')
    expect(kindOptionsHtml(KINDS, 'adjust_add')).toContain(
      '<option value="adjust_add" selected>后台加款</option>',
    )
    expect(buildHtml()).toContain('流水明细')
    expect(newClientRequestNo()).toMatch(/^(adj-)?[0-9a-zA-Z-]{8,}$/)
    // 标记里不许出现 `class=".xxx"`（陷阱 ①：类名与选择器是两个常量）。
    expect(buildHtml()).toContain(`class="${PANEL_CLASS}"`)
    expect(buildHtml()).not.toContain(`class="${PANEL_SELECTOR}"`)
    expect(document.querySelector('#walletAdjustTabs')).toBeNull()
    expect(String(TABS_ID)).toBe('walletAdjustTabs')
  })

  it('押金要求/能否接单：未知（没勾游戏）写「未知」，0 元写「够」', () => {
    // 「未知」与「不够」/「0 元」是三件事：后端在没勾游戏时回 `null`（⛔ 不是 0）。
    expect(depositRequiredText({ deposit_required: null })).toBe('未知')
    expect(depositEnoughText({ deposit_enough: null })).toBe('未知')
    // 0 元额度 = 接单不需要冻结 ⇒ 「够」，⛔ 不许显示成「未知」。
    expect(depositRequiredText({ deposit_required: '0.00' })).toBe('0.00')
    expect(depositEnoughText({ deposit_enough: true })).toBe('够')
    expect(depositEnoughText({ deposit_enough: false })).toBe('不够')
  })
})
