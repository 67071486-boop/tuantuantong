// @vitest-environment jsdom
/**
 * 订单详情页 **T3「两块日志」** 的挂载层接线测试（layui 换可控替身，手法同
 * `system-systemlog-mount.spec.ts`）。
 *
 * 为什么单开一个文件：`order-orderview.spec.ts` 里有 4 条**页内 `<style>` 的逐字钉子**
 * （评审 I-1），本轮的用例与它们无关；混在一个文件里会让「哪条红了」更难读，
 * 也容易在改动夹具时碰坏那几段字面量。
 *
 * 断言都打在**真实出口**上（AGENTS.md §5 / E-0009）：
 *  - 列定义 → `table.render` 的实参；
 *  - 「操作日志」表的数据 → `table.reloadData(id, {data})` 的实参（**不再发请求**）；
 *  - 「订单日志」行 → 挂载后**真 DOM** 里 `#logdiv > p.linespan` 的文本与顺序；
 *  - 「添加」→ `request()` 的实参（POST 的 URL 与 body）、`layer.msg` 的实参；
 *  - 自动刷新 → `window.setInterval` 真的会**重新取数并整块重绘**；卸载后不再取数。
 *
 * ⚠️ `created_at` 的时区口径见 `formatLogTime` 的注释：后端 `/orders/<单号>/audit/` 回的是
 * **UTC**（`+00:00`）⇒ 本文件既有 `+08:00` 的夹具，也有一条 `+00:00` 的**分支钉**。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const EVIDENCE = readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/order-orderview.html',
  ),
  'utf8',
)

/** layui 的可控替身（`vi.hoisted`：`vi.mock` 的工厂会被提升到 import 之前）。 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    formHandlers,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: { render: vi.fn(), reloadData: vi.fn(), on: vi.fn() },
    layer: { msg: vi.fn() },
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import { BizError, HttpError } from '@/api/errors'
import type { AuditEntry, Order } from '@/api/orders'
import {
  buildActionLogCols,
  formatLogTime,
  LOG_REFRESH_MS,
  mountOrderOrderViewPage,
  toActionLogRow,
} from '../order-orderview'

const mockedRequest = vi.mocked(request)

/** 20 位系统单号（**合成值**）。 */
const ORDER_NO = '20260922120000000001'

/** 一单真数据（形状同后端 `OrderSerializer`；值只用于让挂载跑通）。 */
function makeOrder(): Order {
  return {
    order_no: ORDER_NO,
    status: 'COMPLETED',
    order_type: 'BOOST',
    game: 11,
    server: 22,
    category: 33,
    project: 44,
    project_text: '三角洲行动 排位上分',
    // ⭐ `Order` 有这一列（`OrderSerializer` 是 `exclude = []`）。
    split_id: '',
    content: '',
    demand: '',
    time_requirement: '',
    game_account: 'acc_demo_01',
    game_sub_account: '',
    game_role_name: '角色甲',
    current_info_1: '',
    current_info_2: '',
    customer_phone: '',
    customer_qq: '',
    customer_ww: '',
    total_price: '199.50',
    received_price: null,
    platform_price: null,
    hall_price: null,
    refund_fee: '0.00',
    fee: null,
    payment_method: '',
    // T8 起 `Order.payment_status` 是必填键（后端三档码，default `UNSETTLED`）⇒ 夹具补齐。
    payment_status: 'UNSETTLED',
    source: '',
    order_source: null,
    source_order_no: '',
    sales_agent: '',
    is_urgent: false,
    remark: '',
    tag: '',
    created_by: '',
    updated_by: '',
    created_at: '2026-09-20T21:32:15+08:00',
    updated_at: '2026-09-20T21:32:15+08:00',
    allowed_transitions: [],
    // 批 F / F3 起详情响应多一个只读布尔键（后端 `OrderSerializer` 恒回它）。
    completed_requires_review: false,
  }
}

/** 两条留痕夹具（**后端顺序 = 新在前**，本文件一律按这个顺序断言）。 */
const AUDIT_ROWS: AuditEntry[] = [
  {
    action: 'order.transition',
    actor: '客服甲',
    reason: '',
    from: '未分配',
    to: '待验收',
    created_at: '2026-09-22T21:06:00+08:00',
    id: 2,
    summary: '将订单状态改为：待验收',
    log_type: 10,
  },
  {
    action: 'order.create',
    actor: '客服乙',
    reason: '',
    from: '',
    to: '199.50',
    created_at: '2026-09-20T09:05:00+08:00',
    id: 1,
    summary: '将订单录入系统',
    log_type: 10,
  },
]

/** `POST /notes/` 成功时后端回的那一行（`order.note`；`log_type` = 10 订单相关）。 */
const NOTE_ROW: AuditEntry = {
  action: 'order.note',
  actor: '客服甲',
  reason: '客户要求今晚 10 点前完成',
  from: '',
  to: '',
  created_at: '2026-09-22T21:30:00+08:00',
  id: 3,
  summary: '添加订单日志：客户要求今晚 10 点前完成',
  log_type: 10,
}

type ReqConfig = { method?: string; url?: string; data?: unknown }

/** 可变的取数状态（`audit` 每次请求都读它，用来证明「到点真的重新取数」）。 */
let auditRows: AuditEntry[] = AUDIT_ROWS
let auditCalls = 0
let noteReply: () => Promise<AuditEntry> = async () => NOTE_ROW

function installRequest(): void {
  mockedRequest.mockImplementation((async (config: ReqConfig) => {
    const url = config.url ?? ''
    if (url.endsWith('/audit/')) {
      auditCalls += 1
      return auditRows
    }
    if (url.endsWith('/notes/')) return noteReply()
    if (url === `/api/business/orders/${ORDER_NO}/`) return makeOrder()
    // Task 5：`fetchGames()` 先读 `/api/auth/me` 的 `user.game_ids`（本文件不关心游戏名，
    // 给空集即可让字典照常为空、回落原始 id）。
    // ⭐ 平台写按钮的节点门（降级档按 `order.list`，见 perms.ts）。
    if (url === '/api/auth/me') return { user: { game_ids: [], menu_permissions: ['order.list'] } }
    // 4 个 FK 字典接口：空集 ⇒ 回落原始 id，不影响本文件的判据。
    if (url.startsWith('/api/business/')) return { items: [], total: 0, page: 1, page_size: 200 }
    throw new Error(`未预期的 request：${url}`)
  }) as never)
}

/** 只推进微任务（`submitNote` 是 fire-and-forget，没有可 await 的句柄）。 */
async function flush(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve()
}

async function mountPage(): Promise<{ root: HTMLElement; unmount: () => void }> {
  window.history.replaceState({}, '', `/layui-island/page.html?order_no=${ORDER_NO}`)
  const root = document.createElement('div')
  document.body.appendChild(root)
  const unmount = await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
  return { root, unmount }
}

/** `#logdiv` 里的行（顺序 = DOM 顺序）。 */
function logLines(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>('#logdiv > p'))
}

/**
 * 最近一次 `table.reloadData(id, …)` 的**第一个实参**（layui 实例 id）。
 *
 * ⛔ 这是**真出口的钉子**（评审 Important-1）：`reloadData` 的第一个实参是**实例 id**，
 * 传 `'#id'`（带 `#`）是**静默 no-op**（E-0035 #5）——生产后果是操作日志表首屏 / 添加后 /
 * 2 分钟刷新**都静默不更新**，而 `#logdiv` 正常（两个页签自相矛盾）。
 * ⛔ 期望值写字面值、不写 `ACTIONLOG_TABLE_ID`（自指断言，E-0024）。
 */
function lastReloadId(): unknown {
  const calls = LAYUI.table.reloadData.mock.calls
  expect(calls.length, '`table.reloadData` 一次都没调').toBeGreaterThan(0)
  return calls[calls.length - 1]![0]
}

/** 最近一次 `table.reloadData(id, {data})` 的 `data`。 */
function lastTableData(): unknown {
  const calls = LAYUI.table.reloadData.mock.calls
  expect(calls.length, '`table.reloadData` 一次都没调').toBeGreaterThan(0)
  return (calls[calls.length - 1]![1] as { data: unknown }).data
}

/** 提交「添加」框（layui 的 `form.on('submit(addpbtn)')` 载荷形状）。 */
function submitRemark(remark: string): void {
  const handler = LAYUI.formHandlers.get('submit(addpbtn)')
  expect(handler, '页面没有注册 submit(addpbtn)').toBeTypeOf('function')
  handler!({ field: { orderId: ORDER_NO, remark } })
}

beforeEach(() => {
  vi.clearAllMocks()
  // 事件处理器表挂在 `Map` 上，`vi.clearAllMocks()` 清不掉（同 `system-systemlog-mount.spec.ts`）：
  // 不清的话上一次挂载注册的处理器会留下 ⇒「页面注册了某事件」这类断言会靠残留假通过。
  LAYUI.formHandlers.clear()
  auditRows = AUDIT_ROWS
  auditCalls = 0
  noteReply = async () => NOTE_ROW
  window.history.replaceState({}, '', '/')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-orderview.html')) {
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
  installRequest()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('formatLogTime：ISO → 对标的 `MM-DD HH:mm`（按串里自带的偏移换算到项目时区）', () => {
  it('带 +08:00 的串原样取墙钟', () => {
    expect(formatLogTime('2026-09-22T21:06:00+08:00')).toBe('09-22 21:06')
  })

  it('带 +00:00 / Z 的串换算到 +08:00（后端 `/audit/` 实测就是这一支）', () => {
    // 实测（只读探针，跑完即删）：开发库里真实行 `created_at.isoformat()`
    // = `2026-09-23T00:14:40.110591+00:00` ⇒ 本地墙钟应是 `09-23 08:14`。
    expect(formatLogTime('2026-09-23T00:14:40.110591+00:00')).toBe('09-23 08:14')
    expect(formatLogTime('2026-09-22T16:06:00Z')).toBe('09-23 00:06')
  })

  it('跨日进位（不能只把小时加 8）', () => {
    expect(formatLogTime('2026-08-31T20:00:00+00:00')).toBe('09-01 04:00')
  })

  it('偏移**不是 +08:00** 的串也按串里自带的偏移换算（+09:00 / -05:00）', () => {
    // `08:14+09:00` ⇒ `2026-09-22T23:14Z` ⇒ +08:00 = `09-23 07:14`。
    expect(formatLogTime('2026-09-23T08:14:00+09:00')).toBe('09-23 07:14')
    // `20:00-05:00` ⇒ `2026-09-23T01:00Z` ⇒ +08:00 = `09-23 09:00`（跨日）。
    expect(formatLogTime('2026-09-22T20:00:00-05:00')).toBe('09-23 09:00')
  })

  it('**只用 UTC getter**：本地 getter（getMonth/getDate/getHours/getMinutes）一次都不许被调', () => {
    // ⚠️ 为什么不能只靠上面「成对的偏移用例」抓「把 `getUTC*` 换成本地 getter」这个变异
    //    （评审 Minor-4）：在 **`TZ=UTC`** 的 runner 上，本地 getter 与 UTC getter 对**同一个
    //    `Date` 恒等** ⇒ 无论输入是 `+00:00` / `+08:00` / `+09:00`，两种写法都给出**同一个字符串**。
    //    **修复前（当时本文件 21 条用例）实测**：`TZ=UTC` 下对「本地 getter」变异是
    //    **21 条全绿（survived）**，同一变异在 `TZ=Asia/Shanghai` 下红 **10** 条。
    //    ⭐ 复评实测（本文件 23 条用例）：
    //    `TZ=UTC` ⇒ **1 红 / 22 绿**（唯一红的就是下面这条零调用 spy 钉）；`TZ=Asia/Shanghai` ⇒
    //    **12 红 / 11 绿**。结论不变——**纯黑箱输入在 UTC runner 上钉不住它**（无法用输入
    //    构造出差异，因为差异本身就是 runner 时区）；多的那几条红是用例数从 21 涨到 23 带来的。
    //    所以这里补一条**与时区无关**的钉子：
    //    装「记录型」spy（**保留原行为**，不改返回），断言本地 getter 零调用。
    //    两个 runner 的合取：① 本条（任意 TZ 下都红）；② 上面的字面值用例（非 UTC runner 下红）。
    const getMonth = vi.spyOn(Date.prototype, 'getMonth')
    const getDate = vi.spyOn(Date.prototype, 'getDate')
    const getHours = vi.spyOn(Date.prototype, 'getHours')
    const getMinutes = vi.spyOn(Date.prototype, 'getMinutes')
    let counts: number[] = []
    try {
      expect(formatLogTime('2026-09-23T08:14:00+09:00')).toBe('09-23 07:14')
    } finally {
      counts = [getMonth, getDate, getHours, getMinutes].map((s) => s.mock.calls.length)
      getMonth.mockRestore()
      getDate.mockRestore()
      getHours.mockRestore()
      getMinutes.mockRestore()
    }
    expect(counts).toEqual([0, 0, 0, 0])
  })

  it('没有偏移的串按「已经是本地墙钟」原样取（与 formatCreatedAt 同口径）', () => {
    expect(formatLogTime('2026-09-22T21:06:00')).toBe('09-22 21:06')
  })

  it('认不出的形状给空串（绝不把原始 ISO 串填进列表）', () => {
    expect(formatLogTime('')).toBe('')
    expect(formatLogTime('not-a-date')).toBe('')
  })
})

describe('操作日志表：`table.render` 的复刻值（对标 `orderview.js:1183-1202`）', () => {
  it('elem / height / defaultToolbar / page 逐条照对标，且**不带 `url`**（数据我们自取）', async () => {
    await mountPage()
    expect(LAYUI.table.render).toHaveBeenCalledTimes(1)
    const opts = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.elem).toBe('#tableList_actionlog')
    // `reloadData` / `table.on` 要的是**实例 id**（不带 `#`），传 `#id` 是静默 no-op（E-0035 #5）。
    // ⛔ 期望值写**字面值**、不写 `ACTIONLOG_TABLE_ID`（自指断言，E-0024 / E-0009）：
    //    把那个常量改成 `'#tableList_actionlog'` 时，本条**必须**红。
    expect(opts.id).toBe('tableList_actionlog')
    expect(opts.height).toBe(320)
    expect(opts.defaultToolbar).toEqual([])
    expect(opts.page).toBe(false)
    // 不带 `url`：只要 config 里还有 `url`，`reloadData(id, {data})` 就走 ajax 分支、
    // 忽略我们传的 `data`（house 口径，先例 `system-sourelist.ts:448-463`）。
    expect(opts.url).toBeUndefined()
    // 先渲染空壳（layui 的实例在 render 时注册；不先 render 就 reloadData 是静默 no-op）。
    expect(opts.data).toEqual([])
  })

  it('三列表头逐字 `时间` / `操作说明` / `操作人`，宽度 / 对齐 / sort 与对标一致', async () => {
    await mountPage()
    const cols = (LAYUI.table.render.mock.calls[0]![0] as { cols: Record<string, unknown>[][] })
      .cols[0]!
    expect(cols.map((c) => c.title)).toEqual(['时间', '操作说明', '操作人'])
    expect(cols.map((c) => c.field)).toEqual(['createTime', 'remark', 'userName'])
    expect(cols).toEqual([
      { field: 'createTime', title: '时间', width: 90, align: 'center', unresize: false, sort: true },
      // ⛔ `minwidth`（小写 w）**逐字照对标** `orderview.js:1193`：layui 2.13.9 只认 `minWidth`
      // ⇒ 这一个属性两边都被忽略、两边都是「自动列」。按 `replicaPage.ts:38-49` 的规矩**不要**
      // "修好"成 `minWidth`（那会把自动列撑开、总宽超过视口）。
      { field: 'remark', title: '操作说明', minwidth: 100, unresize: false },
      { field: 'userName', title: '操作人', align: 'center', width: 70, sort: false },
    ])
  })

  it('`sort: true` 是**对标的复刻值**：本页数据一次全给 + `page:false` ⇒ layui 本地排序就是对标行为，不关掉', async () => {
    await mountPage()
    const opts = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    // ⛔ 与 `system-systemlog.ts` **刻意不同**：那一页有分页、本地排序只排当前页 30 行（所以传
    // `autoSort: false` 改服务端排序）；本页 `page:false`、一次全给 ⇒ 本地排序 = 对标行为。
    expect(opts.autoSort).toBeUndefined()
    expect(buildActionLogCols().find((c) => c.field === 'createTime')!.sort).toBe(true)
  })

  it('接口行 → 列数据：`summary`→`remark`、`actor`→`userName`、`created_at`→`createTime`（本地墙钟）', () => {
    expect(toActionLogRow(AUDIT_ROWS[1]!)).toEqual({
      createTime: '09-20 09:05',
      remark: '将订单录入系统',
      userName: '客服乙',
    })
  })
})

/**
 * 判据 6（审计日志线 spec §3.7 / §4）：操作人列取**显示名**。
 *
 * ⚠️ 三个方向缺一不可：`actor_display` 有 ⇒ 取它（**不是**登录账号）；缺席 ⇒ 回落 `actor`
 * （部署窗口「前端新、后端旧」时后端根本不回这个键）；两者都缺 ⇒ 空串（⛔ 不出现 `undefined`）。
 */
describe('操作人列取显示名（`actor_display`）', () => {
  it('`actor_display` 存在 ⇒ 取它（昵称），**不是**登录账号', () => {
    const row = toActionLogRow({
      ...AUDIT_ROWS[0]!,
      actor: 'kefu-a',
      actor_display: '测试昵称甲',
    })
    expect(row.userName).toBe('测试昵称甲')
    // 反向也断：退回 `entry.actor` 的实现会让这一条红（否则「取了昵称」证明不了）
    expect(row.userName).not.toBe('kefu-a')
  })

  it('`actor_display` 缺席（部署窗口：前端新、后端旧）⇒ 回落 `actor`', () => {
    const legacy = { ...AUDIT_ROWS[0]!, actor: 'kefu-a' }
    delete (legacy as { actor_display?: string }).actor_display
    // 自证「缺席」真的成立了（否则这一条会退化成上面的正向用例）
    expect('actor_display' in legacy).toBe(false)
    expect(toActionLogRow(legacy).userName).toBe('kefu-a')
  })

  it('两个来源都缺（脏数据）⇒ 空串，⛔ 不出现 `undefined`', () => {
    const broken = {
      ...AUDIT_ROWS[0]!,
      actor: undefined,
      actor_display: undefined,
    } as unknown as AuditEntry
    const row = toActionLogRow(broken)
    expect(row.userName).toBe('')
    expect(row.userName).not.toBeUndefined()
  })
})

describe('订单日志行列表（`#logdiv`）：形状 / 顺序 / 整块重绘', () => {
  it('挂载后真的渲染出后端返回的行，顺序 = 后端顺序（新在前，**不 reverse**）', async () => {
    const { root } = await mountPage()
    const lines = logLines(root)
    expect(lines).toHaveLength(2)
    expect(lines.map((p) => p.getAttribute('class'))).toEqual(['linespan', 'linespan'])
    expect(lines.map((p) => p.textContent)).toEqual([
      '【09-22 21:06】将订单状态改为：待验收',
      '【09-20 09:05】将订单录入系统',
    ])
    // 内联样式逐字照对标（默认灰 `#999`；本页不做染色分档）。
    expect(lines[0]!.getAttribute('style')).toBe(
      'line-height:28px;margin-left:5px; color:#999',
    )
  })

  it('同一份数据也进了操作日志表的**本地数据**（不再多发一次请求）', async () => {
    await mountPage()
    expect(auditCalls).toBe(1)
    expect(lastTableData()).toEqual([
      { createTime: '09-22 21:06', remark: '将订单状态改为：待验收', userName: '客服甲' },
      { createTime: '09-20 09:05', remark: '将订单录入系统', userName: '客服乙' },
    ])
  })

  it('summary 里的用户输入不会变成 HTML（用文本节点，不拼串）', async () => {
    auditRows = [
      {
        ...AUDIT_ROWS[0]!,
        summary: '添加订单日志：<img src=x onerror="alert(1)">',
      },
    ]
    const { root } = await mountPage()
    const line = logLines(root)[0]!
    expect(line.querySelector('img')).toBeNull()
    expect(line.textContent).toBe('【09-22 21:06】添加订单日志：<img src=x onerror="alert(1)">')
  })

  it('取数失败：原样弹后端 msg（不自造文案）', async () => {
    mockedRequest.mockImplementation((async (config: ReqConfig) => {
      const url = config.url ?? ''
      if (url.endsWith('/audit/')) throw new BizError(1002, '订单不存在')
      if (url === `/api/business/orders/${ORDER_NO}/`) return makeOrder()
      if (url.startsWith('/api/business/')) return { items: [], total: 0, page: 1, page_size: 200 }
      throw new Error(`未预期的 request：${url}`)
    }) as never)
    await mountPage()
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('订单不存在', { time: 6000 })
  })
})

/**
 * 判据 7（审计日志线 spec §3.7 / §4 裁定 3）：「查看游戏账号密码及详情」**离开「订单日志」页签**，
 * 但**仍留在「操作日志」页签**。
 *
 * ⚠️ 两个方向必须在**同一条用例**里都断：只断「不在 `#logdiv`」的话，
 * 「顺手把取数也过滤掉」或「把整块日志清空」这类坏实现照样绿。
 * ⚠️ 夹具里的 `action` 写字面值 `'order.view_credentials'`（后端动作 code 的原文）——
 * 生产代码侧用模块常量 `VIEW_CREDENTIALS_ACTION`；常量被改坏时这一条**当场红**。
 */
describe('「查看账号密码」离开「订单日志」页签（操作日志页签照旧）', () => {
  const CREDENTIALS_ROW: AuditEntry = {
    action: 'order.view_credentials',
    actor: '客服甲',
    reason: '',
    from: '',
    to: '',
    created_at: '2026-09-22T21:10:00+08:00',
    id: 9,
    summary: '查看游戏账号密码及详情',
    log_type: 10,
  }

  it('该行**不在** `#logdiv`，但在「操作日志」表的行数据里**一条不少**', async () => {
    // 三条：被过滤的那条在最前（后端顺序 = 新在前）+ 两条普通流水。
    auditRows = [CREDENTIALS_ROW, ...AUDIT_ROWS]
    const { root } = await mountPage()

    // ① 「订单日志」页签：只剩原来的两条（过滤生效）
    const lines = logLines(root)
    expect(lines.map((p) => p.textContent)).toEqual([
      '【09-22 21:06】将订单状态改为：待验收',
      '【09-20 09:05】将订单录入系统',
    ])
    expect(lines.some((p) => p.textContent?.includes('查看游戏账号密码'))).toBe(false)

    // ② 「操作日志」页签：**三条都在**（含被过滤的那条）——只断 ① 会漏掉「两块一起被过滤」
    expect(lastTableData()).toEqual([
      { createTime: '09-22 21:10', remark: '查看游戏账号密码及详情', userName: '客服甲' },
      { createTime: '09-22 21:06', remark: '将订单状态改为：待验收', userName: '客服甲' },
      { createTime: '09-20 09:05', remark: '将订单录入系统', userName: '客服乙' },
    ])
  })

  it('2 分钟整块重绘后仍只过滤「订单日志」页签（两条腿都重跑）', async () => {
    vi.useFakeTimers()
    auditRows = [CREDENTIALS_ROW, ...AUDIT_ROWS]
    const { root } = await mountPage()
    expect(logLines(root)).toHaveLength(2)

    await vi.advanceTimersByTimeAsync(LOG_REFRESH_MS)
    expect(auditCalls).toBe(2)
    // 重绘这条路也走同一个出口：`#logdiv` 两条、「操作日志」三条。
    expect(logLines(root).map((p) => p.textContent)).toEqual([
      '【09-22 21:06】将订单状态改为：待验收',
      '【09-20 09:05】将订单录入系统',
    ])
    expect(lastTableData()).toHaveLength(3)
  })
})

describe('「添加」框：`POST /orders/<单号>/notes/`', () => {
  it('非空 ⇒ 调一次 POST，body 是**原样**输入（含首尾空白，不 trim）', async () => {
    const { root } = await mountPage()
    const input = root.querySelector<HTMLInputElement>('#remark')!
    input.value = '  客户要求今晚 10 点前完成  '
    submitRemark(input.value)
    await flush()

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${ORDER_NO}/notes/`,
      data: { content: '  客户要求今晚 10 点前完成  ' },
    })
    // 只有这一次 POST（没有顺带重取 audit / 没有第二次写）。
    expect(
      mockedRequest.mock.calls.filter((c) => (c[0] as ReqConfig).method === 'post'),
    ).toHaveLength(1)
  })

  it('成功后 `#logdiv` **第一行**是新行、输入框被清空，且两块同源同步（不再发请求）', async () => {
    const { root } = await mountPage()
    const input = root.querySelector<HTMLInputElement>('#remark')!
    input.value = '客户要求今晚 10 点前完成'
    submitRemark(input.value)
    await flush()

    const lines = logLines(root)
    expect(lines).toHaveLength(3)
    expect(lines[0]!.textContent).toBe('【09-22 21:30】添加订单日志：客户要求今晚 10 点前完成')
    expect(lines[1]!.textContent).toBe('【09-22 21:06】将订单状态改为：待验收')
    expect(input.value).toBe('')
    // 操作日志表的**本地数据**也同步加在最前（同源，否则两个页签自相矛盾）。
    expect(lastTableData()).toEqual([
      { createTime: '09-22 21:30', remark: '添加订单日志：客户要求今晚 10 点前完成', userName: '客服甲' },
      { createTime: '09-22 21:06', remark: '将订单状态改为：待验收', userName: '客服甲' },
      { createTime: '09-20 09:05', remark: '将订单录入系统', userName: '客服乙' },
    ])
    // ⛔ 真出口钉子（评审 Important-1）：`reloadData` 的**第一个实参**必须是实例 id **字面量**，
    //    传 `'#id'` 是静默 no-op（E-0035 #5）。旧 helper 只断言 `data`（第二个实参）时这条能溜过去。
    expect(lastReloadId()).toBe('tableList_actionlog')
    // 同步走的是本地 `data`，**没有**再取一次 audit。
    expect(auditCalls).toBe(1)
  })

  it('空 / 纯空白 ⇒ **零请求**且弹对标的原文 `备注内容不能为空！`', async () => {
    const { root } = await mountPage()
    const before = mockedRequest.mock.calls.length
    submitRemark('')
    submitRemark('   ')
    await flush()
    expect(mockedRequest.mock.calls.length).toBe(before)
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('备注内容不能为空！', { time: 1000 })
    expect(LAYUI.layer.msg).toHaveBeenCalledTimes(2)
    expect(root.querySelector<HTMLInputElement>('#remark')!.value).toBe('')
  })

  it('失败 ⇒ 弹出的是**后端 msg 原文**（6000ms）', async () => {
    noteReply = () => Promise.reject(new HttpError(403, '您没有执行该操作的权限。'))
    await mountPage()
    submitRemark('随便写点什么')
    await flush()
    expect(LAYUI.layer.msg).toHaveBeenCalledWith('您没有执行该操作的权限。', { time: 6000 })
  })

  it('失败时不改本地状态（不假装成功：输入框不清空、行数不变）', async () => {
    noteReply = () => Promise.reject(new BizError(1000, '订单日志内容不能为空'))
    const { root } = await mountPage()
    const input = root.querySelector<HTMLInputElement>('#remark')!
    input.value = '随便写点什么'
    submitRemark(input.value)
    await flush()
    expect(logLines(root)).toHaveLength(2)
    expect(input.value).toBe('随便写点什么')
    expect(lastTableData()).toHaveLength(2)
  })
})

describe('2 分钟自动刷新（对标 `setInterval(getOrderLogs, 1000*60*2)`，recon §7.1）', () => {
  it('`LOG_REFRESH_MS` 就是 120000', () => {
    expect(LOG_REFRESH_MS).toBe(120000)
  })

  it('到点真的重新取数并**整块重绘**两个块（不是只断言常量）', async () => {
    vi.useFakeTimers()
    const { root } = await mountPage()
    expect(auditCalls).toBe(1)
    expect(logLines(root)).toHaveLength(2)

    // 第二次后端只回 1 行 ⇒ 整块重绘后 `#logdiv` 必须**只剩 1 行**（append 的话会是 3 行）。
    auditRows = [AUDIT_ROWS[1]!]
    await vi.advanceTimersByTimeAsync(LOG_REFRESH_MS)
    expect(auditCalls).toBe(2)
    expect(logLines(root)).toHaveLength(1)
    expect(logLines(root)[0]!.textContent).toBe('【09-20 09:05】将订单录入系统')
    expect(lastTableData()).toEqual([
      { createTime: '09-20 09:05', remark: '将订单录入系统', userName: '客服乙' },
    ])
    // 定时器那条路也走同一个出口 ⇒ 一并钉住第一个实参（评审 Important-1）。
    expect(lastReloadId()).toBe('tableList_actionlog')
  })

  it('卸载函数调用后**不再发请求**（`clearInterval` 真的生效）', async () => {
    vi.useFakeTimers()
    const { unmount } = await mountPage()
    expect(auditCalls).toBe(1)
    unmount()
    await vi.advanceTimersByTimeAsync(LOG_REFRESH_MS * 3)
    expect(auditCalls).toBe(1)
  })
})
