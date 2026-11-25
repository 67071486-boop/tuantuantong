// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【已删除的订单】页从 `artifactPage` 改成**自建 island 页 + 接真后端**之后的门禁（spec §7.3 / D9）。
 *
 * 三条理由（D9 的代价，逐条）：
 *  ① 这一页**不再**落在共享渲染器「表格恒空、不发写请求」的护栏里；
 *  ② `artifactPage` 那套「点任何按钮都提示尚未开放」的 catch-all **随之失效** ⇒ 本页必须**显式**
 *     给每个可点元素一条去向（接线 / 提示 / 不接），否则会变成「点了没反应」（**静默**失效）；
 *  ③ 表格有真数据之后本页的表体才第一次有内容，差异按 §7.4 登记。
 *
 * ⚠️ 保留既有 6 条「表头 / 列宽 / 分页 / 不搬 nresize」的断言（它们是对的，只是当年钉在 `SPEC` 上）
 * ——本文件把它们**改钉在 `buildRecycleCols()` 与四个导出常量**上（⛔ 不删、不放宽）。
 *
 * ⚠️ 驱动方式（**真交互**，不是钉常量）：`table` 是替身，所以工具栏 / 行内事件由
 * `table.on('toolbar(tableList)')` / `table.on('tool(tableList)')` **登记的那两个真回调**驱动
 * ——它们正是真 layui 点击时会调的函数。列表取数与还原打的是**真 `api/orders.ts`** 拼出来的
 * 请求配置（`@/api/http` 的 `request` 被替身化），所以断言的是「真请求的形状」。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-orderrecycle.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = fs.readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderrecycle.html'),
  'utf8',
)

/**
 * 回收站行**恰好**的键集合（= 后端 `RecycleBinListSerializer` 的输出键）。
 *
 * ⚠️ 它是 `tests/test_order_recycle.py::RecycleBinListTests::RECYCLE_ROW_KEYS` 的**前端副本**，
 * 那份后端用例按「**恰好集合**」钉住真实载荷；这里钉的是**夹具**——夹具一旦自带后端不返回的
 * 键，页测试就会在**说谎**（T6 的致命缺陷正是这样溜过去的：夹具自带 `tag` / `game_account`，
 * 而后端当时不回这两个键 ⇒ 24 条用例全绿、页面却一行都渲染不出来）。
 * ⛔ 两个清单要同步改；后端那份才是权威。
 */
const RECYCLE_ROW_KEYS: readonly string[] = [
  'completed_at',
  'created_at',
  'created_by',
  'customer_phone',
  'customer_ww',
  'deleted_at',
  'deleted_by',
  'deleted_by_name',
  'game',
  'game_account',
  'game_role_name',
  'hall_price',
  'hall_publisher_name',
  'is_urgent',
  'net_received',
  'order_no',
  'payment_method',
  'payment_status',
  'project_text',
  'refund_fee',
  'remark',
  'sales_agent',
  'server',
  'source',
  'source_order_no',
  'status',
  'tag',
  'taker_name',
  'time_requirement',
  'total_price',
]

/**
 * 合成夹具（PII 纪律：**不抄**取证记录里的真实姓名 / 手机号 / 真实单号；
 * 单号是 20 位数字的**形状**而已）。
 *
 * ⚠️ 键**逐字**对齐后端真实载荷（见 `RECYCLE_ROW_KEYS`）：多带一个后端不回的键 = 夹具在说谎
 * （页面读它会拿到 `undefined`，而测试因为是夹具自己造的键而全绿）。
 */
const ROW: RecycleBinRow & Record<string, unknown> = {
  order_no: '20260924000000000501',
  source_order_no: '',
  source: '测试来源甲',
  status: 'COMPLETED',
  game: 7,
  server: 3,
  project_text: '测试项目甲',
  total_price: '100.00',
  customer_phone: '',
  is_urgent: true,
  created_at: '2026-09-24T09:00:00+08:00',
  refund_fee: '0.00',
  taker_name: '',
  game_role_name: '测试角色甲',
  customer_ww: '',
  hall_price: '88.00',
  sales_agent: '',
  created_by: '',
  remark: '测试备注甲',
  completed_at: null,
  time_requirement: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
  net_received: '100.00',
  hall_publisher_name: '',
  game_account: 'zhanghao_test_001',
  tag: '标记甲',
  deleted_at: '2026-09-24T10:00:00+08:00',
  deleted_by: '测试操作人甲',
  // 删除人的**账号名称**（昵称）——与登录账号**不同形**，否则「显示昵称」这条判据会假绿。
  deleted_by_name: '测试昵称甲',
}

/** 旧形状：后端补 `tag` / `game_account` **之前**的载荷（这两个键缺席）。 */
function legacyShapeRow(): Record<string, unknown> {
  const row: Record<string, unknown> = { ...ROW }
  delete row.tag
  delete row.game_account
  return row
}

/** 字典夹具（同样合成）。 */
const GAMES = [
  { id: 7, name: '测试游戏甲', sort_order: 0, is_active: true, created_at: '' },
]
const SERVERS = [
  { id: 3, name: '测试区服甲', game: 7, sort_order: 0, is_active: true, created_at: '' },
]
const SOURCES = [
  {
    id: 5,
    name: '测试来源甲',
    remark: '',
    order_by: 0,
    shop_type: 0,
    created_at: '',
    updated_at: '',
  },
]

/** layui 的**可控替身**。真 layui 在 jsdom 里加载不了（证据：`order-orderlist.spec.ts` 同形）。 */
const LAYUI = vi.hoisted(() => {
  const toolHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const msgs: string[] = []
  let rows: Array<{ order_no: string }> = []
  const table = {
    render: vi.fn(),
    // `url` 模式：重新取数的唯一出口是 `table.reload`（本地数据模式的 `reloadData` 已删）。
    reload: vi.fn(),
    cache: {},
    on: vi.fn((e: string, cb: (o: unknown) => unknown) => {
      toolHandlers.set(e, cb)
    }),
    checkStatus: vi.fn(() => ({ data: rows })),
    // 真 layui：`table.getOptions(id)` 回 `render` 时那份配置对象（`F.getOptions=l`）——
    // 用户选的「条/页」由 laypage **就地**写在同一个 `page` 子对象上。
    getOptions: vi.fn(() => ({ page: { limit: 20 } })),
  }
  const form = {
    render: vi.fn(),
    on: vi.fn((e: string, cb: (o: unknown) => unknown) => {
      formHandlers.set(e, cb)
    }),
    val: vi.fn(() => ({})),
  }
  const layer = {
    msg: vi.fn((t: string) => {
      msgs.push(t)
    }),
    confirm: vi.fn(),
    open: vi.fn(),
    close: vi.fn(),
    closeAll: vi.fn(),
    alert: vi.fn(),
  }
  return {
    toolHandlers,
    formHandlers,
    msgs,
    table,
    form,
    layer,
    setRows: (next: Array<{ order_no: string }>): void => {
      rows = next
    },
    laydate: { render: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
    laydate: LAYUI.laydate,
    $: LAYUI.$,
  })),
  initIslandElements: vi.fn(),
  islandMsg: (_l: unknown, t: string) => LAYUI.msgs.push(t),
}))

// 传输层替身：断言的是**真 `api/orders.ts`** 拼出来的请求配置（不是页面自造的中间态）。
// ⚠️ `request<T>()` 回的是**解包后的 `data`**（信封的 `{code,msg}` 由 `unwrap()` 吃掉）
// ⇒ 这个替身要回 `Page<RecycleBinRow>` / `{order_no}` 本身，⛔ 不是 `{code,msg,data}` 信封。
vi.mock('@/api/http', () => ({
  request: vi.fn(async (config: { method?: string; url?: string }) => {
    const url = String(config.url ?? '')
    if (url.includes('/orders/recycle-bin/')) {
      return { items: [ROW], total: 1, page: 1, page_size: 20 }
    }
    if (url.includes('/restore/')) {
      return { order_no: ROW.order_no }
    }
    throw new Error(`未预期的 request：${url}`)
  }),
}))

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return {
    ...actual,
    fetchGames: vi.fn(async () => GAMES),
    fetchServers: vi.fn(async () => SERVERS),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: SOURCES, total: 1, page: 1, page_size: 200 })),
  }
})

import { request } from '@/api/http'
import type { RecycleBinRow } from '@/api/orders'
import page, {
  buildRecycleCols,
  buildRecycleRenderCols,
  buildRecycleRow,
  defaultRecycleDateRange,
  RECYCLE_API_PATH,
  RECYCLE_API_URL,
  RECYCLE_BATCH_NOT_OPEN_TEXT,
  RECYCLE_DETAIL_NOT_OPEN_TEXT,
  RECYCLE_PAGE_LIMIT,
  RECYCLE_PAGE_LIMITS,
  RECYCLE_ROW_SELECT_TEXT,
  RECYCLE_TABLE_ELEM,
  RECYCLE_TABLE_ID,
  RECYCLE_TOOLBAR_ELEM,
  RECYCLE_TOTAL_TEMPLATE,
  toRecycleSearch,
} from '../order-orderrecycle'

const CTX = { onNavigate: vi.fn() }

/** 排空已 resolve 的 promise 链（`request` 是 async 替身）。 */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
}

/** 挂载整页（证据件走全局 `fetch`，字典走 `@/api/config` 的替身）。 */
async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, CTX)
  await flush()
  return root
}

/** 驱动 `table.on('toolbar(…)')` 登记的那个**真回调**。 */
async function driveToolbar(event: string): Promise<void> {
  const handler = LAYUI.toolHandlers.get(`toolbar(${RECYCLE_TABLE_ID})`)
  expect(handler, '本页没有注册 table 的 toolbar 处理器').toBeDefined()
  await (handler as (o: unknown) => unknown)({ event })
  await flush()
}

/** 驱动 `table.on('tool(…)')`（行内「还原该订单」）。 */
async function driveTool(row: Record<string, unknown>): Promise<void> {
  const handler = LAYUI.toolHandlers.get(`tool(${RECYCLE_TABLE_ID})`)
  expect(handler, '本页没有注册 table 的 tool 处理器 ⇒ 行内「还原」点了没反应').toBeDefined()
  await (handler as (o: unknown) => unknown)({ event: 'refOrder', data: row })
  await flush()
}

type ReqConfig = { method?: string; url?: string; params?: Record<string, unknown> }

const requestCalls = (): ReqConfig[] =>
  vi.mocked(request).mock.calls.map(([config]) => config as unknown as ReqConfig)

const callsByMethod = (method: string): ReqConfig[] =>
  requestCalls().filter((config) => config.method === method)

/** `table.render` 收到的唯一那份配置（**真正交给 layui 的出口**，不是页面自造的中间变量）。 */
type RecycleRenderOpts = {
  elem?: string
  id?: string
  url?: string
  method?: string
  request?: Record<string, string>
  where?: Record<string, unknown>
  data?: unknown
  toolbar?: string
  defaultToolbar?: unknown
  page?: { limit?: number; limits?: number[] }
  cols?: unknown[]
  done?: (res: unknown, curr?: number, count?: number) => void
  parseData?: (r: {
    code: number
    msg: string
    // 统一信封的分页体（`apps/core/envelope.py::paginated`）：`items`/`total`/`page`/`page_size`。
    data?: {
      items?: Array<Record<string, unknown>>
      total?: number
      page?: number
      page_size?: number
    }
  }) => { code: number; msg: string; count: number; data: Array<Record<string, string>> }
}

function renderOpts(): RecycleRenderOpts {
  const call = LAYUI.table.render.mock.calls[0]
  expect(call, 'table.render 没有被调用').toBeTruthy()
  return call![0] as RecycleRenderOpts
}

/** 最近一次 `table.reload` 的入参（`url` 模式下页面重新取数的**真实出口**）。 */
function lastReload(): { where: Record<string, unknown>; page: { curr?: number; limit?: number } } {
  const calls = LAYUI.table.reload.mock.calls
  const call = calls[calls.length - 1]
  expect(call, 'table.reload 没有被调用').toBeTruthy()
  expect(call![0]).toBe(RECYCLE_TABLE_ID)
  return call![1] as { where: Record<string, unknown>; page: { curr?: number; limit?: number } }
}

/** 驱动 `form.on('submit(seachBtn)')` 登记的**真回调**（= 真人点「搜索」）。 */
async function submitSearch(): Promise<void> {
  const submit = LAYUI.formHandlers.get('submit(seachBtn)')
  expect(submit, '本页没有注册搜索按钮的 submit 处理器').toBeDefined()
  ;(submit as () => unknown)()
  await flush()
}

beforeEach(() => {
  document.body.innerHTML = ''
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.table.on.mockClear()
  // 「条/页」的实时值由 `getOptions` 读（laypage 就地写进 `render` 那份配置的 `page`）。
  LAYUI.table.getOptions.mockClear()
  LAYUI.table.getOptions.mockReturnValue({ page: { limit: 20 } })
  LAYUI.form.on.mockClear()
  LAYUI.laydate.render.mockClear()
  LAYUI.toolHandlers.clear()
  LAYUI.formHandlers.clear()
  LAYUI.msgs.length = 0
  LAYUI.setRows([])
  vi.mocked(request).mockClear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).includes('order-orderrecycle.html')) {
        return { ok: true, text: async () => EVIDENCE } as unknown as Response
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('回收站页：列声明与渲染参数（保留既有断言，改钉在导出上）', () => {
  it('12 列，顺序与文案逐字对齐 orderrecycle.js L59-172', () => {
    expect(buildRecycleCols().map((c) => c.title)).toEqual([
      undefined,
      '订单号',
      '角色名',
      '阵营/平台',
      '代练/陪玩项目',
      '角色信息',
      '订单状态',
      '旺旺/来源',
      '重要备注',
      '删除时间',
      '删除人',
      '操作',
    ])
  })

  it('第 1 列是 checkbox 且 fixed:left、宽度取桌面分支 2%（L59 的三元表达式）', () => {
    expect(buildRecycleCols()[0]).toMatchObject({ type: 'checkbox', fixed: 'left', width: '2%' })
  })

  it('末列带 toolbar=#baroperating（L172），不带 fixed（L172 的空串 fixed 是 falsy）', () => {
    const cols = buildRecycleCols()
    expect(cols[11]).toMatchObject({ title: '操作', width: 120, toolbar: '#baroperating' })
    expect('fixed' in (cols[11] as object)).toBe(false)
  })

  it('运行期百分比列宽**原样透传**（桌面分支：11%/4%/12%/13%/4%/5%/5%）', () => {
    expect(buildRecycleCols().map((c) => c.width)).toEqual([
      '2%',
      '11%',
      130,
      '4%',
      '12%',
      '13%',
      '4%',
      '5%',
      '5%',
      140,
      90,
      120,
    ])
  })

  it('对标 `nresize`（L94，小写 w，无效属性）没有被搬进来', () => {
    expect(buildRecycleCols().some((c) => 'nresize' in (c as object))).toBe(false)
  })

  /**
   * ⭐ **「渲染列 = 证据列 + 原样 templet」**（spec §2.2 / §3.2 的判据）。
   *
   * 为什么这条是承重的（实测，spec §2.1 的真 Chromium 探针）：layui 的 `table` 原型配置是
   * `escape:!0`，而「列上没有 `escape` 键时取表配置」⇒ **不挂 `templet` 的数据列被当纯文本转义**
   * ——本页的单元格值是 `buildRecycleRow()` 拼好的 HTML，于是用户看到的是
   * `<div class="orderLine" …` 这串源码。挂了 `templet` 的列由 templet 返回值**原样**使用。
   *
   * 判别力自证（评审可复跑）：把 `withRawCells` 换成恒等函数 ⇒ 本条的 `typeof templet` 立刻红。
   */
  it('⭐ 每个数据列都挂了原样 templet，且 `templet(行)` 与 `buildRecycleRow(行)[field]` 逐字相等', () => {
    const evidence = buildRecycleCols()
    const render = buildRecycleRenderCols()
    // 派生**不动证据**：列数 / 顺序 / `field` / 标题一个不差（只多出 templet）。
    expect(render).toHaveLength(evidence.length)
    expect(render.map((c) => c.title)).toEqual(evidence.map((c) => c.title))
    expect(render.map((c) => c.field)).toEqual(evidence.map((c) => c.field))

    // ⚠️ 行映射是**转义出口**：`A&B` 在这里就变成 `A&amp;B`（本页 `remark` 列逐字进模板）。
    const mapped = buildRecycleRow({ ...ROW, remark: 'A&B' }, () => '测试区服甲')
    // ⚠️ 循环里**不调 `expect`**（`vitest/no-conditional-expect`）：先把「不合格的列」收集成
    //    清单，循环外再断言清单为空——判别力不变（少一列 / 少一个 templet 都会落到某个清单里）。
    const dataFields: string[] = []
    const fieldlessWithTemplet: string[] = []
    const withoutTemplet: string[] = []
    const notVerbatim: string[] = []
    for (const [index, col] of render.entries()) {
      const field = col.field
      if (typeof field !== 'string' || field === '') {
        // 勾选列 / 只有 toolbar 的操作列：挂 templet 只会取到 `undefined`。
        if ('templet' in col) fieldlessWithTemplet.push(`第 ${index + 1} 列`)
        continue
      }
      dataFields.push(field)
      if (typeof col.templet !== 'function') {
        withoutTemplet.push(field)
        continue
      }
      const value = (col.templet as (r: never) => unknown)(mapped as never)
      if (value !== mapped[field]) notVerbatim.push(`${field}: ${String(value)}`)
    }
    // 非空哨兵：10 个数据列**逐个**都在（少一列 / 多一列这里立刻红）。
    expect(dataFields).toEqual([
      'orderIdView',
      'roleName',
      'gameGroup',
      'project',
      'gameInfo',
      'orderSta',
      'orderSoure',
      'orderRemark',
      'delTime',
      'delUserName',
    ])
    expect(fieldlessWithTemplet).toEqual([])
    // 承重：任一数据列漏挂 templet ⇒ 这一条红（真 layui 会把它当纯文本转义）。
    expect(withoutTemplet).toEqual([])
    // 承重：templet 必须**逐字**等于行映射的产物（做了转义/加工就不等）。
    expect(notVerbatim).toEqual([])
    // 行映射的产物里**已经有** `&amp;`，而 templet **原样**透传 ⇒ 不会有第二次转义。
    expect(mapped.orderRemark).toContain('A&amp;B')
    // 拼 HTML 的列真的带着 HTML（不是「恰好等于空串」那种恒真形状）。
    expect(mapped.orderIdView).toContain('class="orderLine"')
  })

  it('四个导出常量与证据件逐字一致（elem / toolbar / limit / limits）', () => {
    // 值一律取自证据件 `orderrecycle.js` L53/L56/L174-176 与 HTML L169-176。
    expect(RECYCLE_TABLE_ELEM).toBe('#tableList')
    expect(RECYCLE_TOOLBAR_ELEM).toBe('#toolbarDemo')
    expect(RECYCLE_PAGE_LIMIT).toBe(20)
    expect([...RECYCLE_PAGE_LIMITS]).toEqual([10, 20, 30])
  })
})

describe('回收站页：挂载后的 DOM 与表格配置（D9 代价①②）', () => {
  it('mount 后：表格 / 工具栏 / 7 个筛选控件都在 DOM 里，且分页是 limit 20 + limits 三项', async () => {
    const root = await mountPage()
    expect(root.querySelector(RECYCLE_TABLE_ELEM)).not.toBeNull()
    expect(root.querySelector(RECYCLE_TOOLBAR_ELEM)).not.toBeNull()
    for (const name of ['gameName', 'orderSta', 'sDate', 'eDate', 'orderSoures', 'key1', 'gameAcc']) {
      expect(root.querySelector(`[name="${name}"]`), `缺筛选控件 ${name}`).not.toBeNull()
    }
    // 既有那条第 6 项断言（表级 elem/toolbar/defaultToolbar/page）改钉在真 `table.render` 的入参上。
    const opts = LAYUI.table.render.mock.calls[0]![0] as {
      elem?: string
      toolbar?: string
      defaultToolbar?: unknown
      page?: unknown
      cols?: unknown[]
    }
    expect(opts.elem).toBe(RECYCLE_TABLE_ELEM)
    expect(opts.toolbar).toBe(RECYCLE_TOOLBAR_ELEM)
    expect(opts.defaultToolbar).toEqual(['filter'])
    expect(opts.page).toEqual({ limit: RECYCLE_PAGE_LIMIT, limits: [...RECYCLE_PAGE_LIMITS] })
    // `cols` 必须是数组的数组。扁平数组在浏览器里会抛
    // `Cannot create property 'key' on string 'checkbox'`，整页白屏。
    // 外面套一层同时钉住内容和层数；`buildRecycleCols()` 自己仍是扁平。
    //    ⚠️ 调用点是 `buildRecycleRenderCols()`（= 证据列 + 原样 templet，
    //    见上面那条「渲染列 = 证据列 + templet」用例）；
    //    ⛔ 仍是 `toEqual` 的**恰好相等**，不是放宽成 `expect.any(Function)`。
    expect(opts.cols).toEqual([buildRecycleRenderCols()])
    expect(opts.cols).toHaveLength(1)
  })

  it('「游戏名称」下拉按我方游戏重建（「全部游戏」用空值）；「订单状态」选项用共享枚举', async () => {
    const root = await mountPage()
    const game = root.querySelector<HTMLSelectElement>('select#gameName')!
    expect(Array.from(game.options).map((o) => [o.value, o.textContent])).toEqual([
      ['', '全部游戏'],
      ['7', '测试游戏甲'],
    ])
    const sta = root.querySelector<HTMLSelectElement>('select#orderSta')!
    // 值 = 我方枚举**码**（送后端的就是它），文案 = 共享枚举的中文 label。
    expect(Array.from(sta.options).map((o) => [o.value, o.textContent])).toEqual([
      ['', '全部'],
      ['UNASSIGNED', '未分配'],
      ['WAITING_GRAB', '待抢单'],
      ['IN_PROGRESS', '代练中'],
      ['PENDING_REVIEW', '待验收'],
      ['COMPLETED', '已完成'],
      ['ISSUE', '问题单'],
      ['PAUSED', '已暂停'],
      ['REFUNDED', '已退单'],
    ])
  })

  it('删除时间的 4 个值改成**运行期**（证据件里是快照当天的死值）', async () => {
    const root = await mountPage()
    const value = (id: string): string => root.querySelector<HTMLInputElement>(`#${id}`)!.value
    const expected = defaultRecycleDateRange()
    expect([value('minD'), value('maxD'), value('sDate'), value('eDate')]).toEqual([
      expected.minD,
      expected.maxD,
      expected.sDate,
      expected.eDate,
    ])
    // 日期控件也照对标渲染（`orderrecycle.js` L20-38 的两次 `laydate.render`）。
    expect(LAYUI.laydate.render).toHaveBeenCalledTimes(2)
  })

  it('⭐ 首屏 `table.render` 的 `where` 就带默认删除时间区间（⛔ 不是先用无筛选结果画一帧）', async () => {
    const root = await mountPage()
    const where = renderOpts().where
    // 判别力：把 `table.render` 的 `where` 去掉（或把它挪到 `done` 里补一次 `reload`）⇒
    // 本行红。⚠️ 只钉**删除时间**两条，不钉「恰好只有这两条」：`order_source` 会被
    // `<select>` 的单选项自动选中而带上，那是另一件事（不在本修复的范围内）。
    expect(where).toBeDefined()
    // 值 = 页面上那两个日期框（`#sDate` / `#eDate`）的当前值，逐字相等：
    // `applyDefaultDateRange` 在 `table.render` **之前**跑，所以首屏那一发就带着区间。
    expect(where!.deleted_from).toBe(root.querySelector<HTMLInputElement>('#sDate')!.value)
    expect(where!.deleted_to).toBe(root.querySelector<HTMLInputElement>('#eDate')!.value)
    // 非空哨兵：区间真的是个区间（两端空串的话上面两条会以「空 == 空」变恒真）。
    expect(String(where!.deleted_from)).toMatch(/^\d{4}-\d{2}-\d{2} 00:00:00$/)
    expect(String(where!.deleted_to)).toMatch(/^\d{4}-\d{2}-\d{2} 23:59:59$/)
  })
})

describe('回收站页：载荷键契约（T6 致命缺陷的回归网）', () => {
  it('夹具的键 = 后端真实键集合（⛔ 不自带后端不返回的键，也不漏掉本页要读的键）', () => {
    // ⚠️ 这条钉的是**测试自己**：夹具自带后端没有的键时，页测试会全绿而页面在真实载荷下崩。
    expect([...Object.keys(ROW)].sort()).toEqual([...RECYCLE_ROW_KEYS].sort())
    // 本页行映射**真的读**这两个键（`buildRecycleRow` 的 `row.tag` / `row.game_account`）——
    // 后端曾经不回它们 ⇒ `escapeHtml(undefined)` 在 `parseData` 里抛、整表一行都不渲染。
    expect(RECYCLE_ROW_KEYS).toContain('tag')
    expect(RECYCLE_ROW_KEYS).toContain('game_account')
  })

  it('⭐ 真实键集合（含 `tag` / `game_account`）⇒ `parseData` 不抛，行按值渲染', async () => {
    await mountPage()
    const opts = renderOpts()
    const parsed = opts.parseData!({
      code: 0,
      msg: '',
      data: { items: [ROW], total: 1, page: 1, page_size: 20 },
    })
    const cell = parsed.data[0]!
    expect(cell.order_no).toBe(ROW.order_no)
    // 标记徽标取首字（`tag` 在载荷里 ⇒ 徽标真的出现）
    expect(cell.orderIdView).toContain('标记：标记甲')
    // 角色信息末段 = 游戏账号
    expect(cell.gameInfo).toContain('/测试区服甲/测试角色甲/zhanghao_test_001')
  })

  it('⭐ **缺** `tag` / `game_account` 的旧形状 ⇒ 也不抛，且 `gameInfo` / `orderIdView` 不含字面 `undefined`', async () => {
    await mountPage()
    const opts = renderOpts()
    const legacy = legacyShapeRow()
    let parsed: { data: Array<Record<string, string>> } | undefined
    // 判别力（**承重**）：把 `buildRecycleRow` 里的 `row.tag ?? ''` / `row.game_account ?? ''`
    // 去掉 ⇒ `escapeHtml(undefined)` 抛 `TypeError`、本行 `not.toThrow()` 立刻红。
    expect(() => {
      parsed = opts.parseData!({
        code: 0,
        msg: '',
        data: { items: [legacy], total: 1, page: 1, page_size: 20 },
      })
    }).not.toThrow()
    const cell = parsed!.data[0]!
    expect(cell.gameInfo).not.toContain('undefined')
    expect(cell.orderIdView).not.toContain('undefined')
    // 缺键 = 该段**为空**（`/` 分隔符照留），不是 `undefined`；title 比正文多 `level` 那个空段。
    expect(cell.gameInfo).toBe(
      '<span title="/测试区服甲/测试角色甲//">/测试区服甲/测试角色甲/</span>',
    )
    // 缺 `tag` ⇒ 不渲染标记徽标（⛔ 不是渲染一个写着 `undefined` 的徽标）。
    expect(cell.orderIdView).not.toContain('标记')
  })
})

describe('回收站页：取数与统计文案', () => {
  it('列表取数接在 layui 的 `url` 模式上：url / method / request 映射逐字正确（本地 data 模式已删）', async () => {
    await mountPage()
    const opts = renderOpts()
    expect(opts.url).toBe(RECYCLE_API_URL)
    expect(String(opts.url)).toContain('/orders/recycle-bin/')
    expect(opts.method).toBe('get')
    // layui 的 `page`/`limit` → 我们后端的 `page`/`page_size`（逐字取 `RecycleBinSearchSerializer`）。
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    // ⛔ 不再有本地 `data`：带 `data` 会让 layui 走本地数据分支，分页条的 count = 本页行数
    //    （`public/layui/layui.js` 偏移 ~292215：`o.url ? … : o.data.length`）——那正是本修复的缺口。
    expect('data' in opts).toBe(false)
    expect(RECYCLE_API_PATH).toBe('/api/business/orders/recycle-bin/')
  })

  it('⭐ `parseData` 吃真信封：`count` = 服务端 total(57)、`data` = 映射后的行；`done` 把 `#total` 写成服务端 total', async () => {
    const root = await mountPage()
    // layui 的工具栏会把 `#toolbarDemo` 模板渲染成工具栏（`#total` 在里面）；jsdom 里 table 是替身
    // ⇒ 手工补上那一个容器，再按**真 layui 的顺序**驱动：`parseData(响应体)` → `done(产物, curr, count)`。
    root.insertAdjacentHTML('beforeend', '<div id="total"></div>')
    const opts = renderOpts()
    const parsed = opts.parseData!({
      code: 0,
      msg: '',
      data: { items: [ROW], total: 57, page: 1, page_size: 20 },
    })
    // 分页条拿到的 `count` 就是**服务端 total**（⛔ 不是本页那 1 行）⇒ 「共 57 条」成立、翻页有意义。
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(57)
    expect(parsed.data).toHaveLength(1)
    expect(parsed.data[0]!.order_no).toBe(ROW.order_no)
    expect(parsed.data[0]!.roleName).toBe('测试角色甲')
    expect(parsed.data[0]!.gameGroup).toBe('')
    expect(parsed.data[0]!.delUserName).toBe('测试昵称甲')
    expect(parsed.data[0]!.delTime).toBe('2026-09-24 10:00')
    expect(parsed.data[0]!.gameInfo).toContain('测试区服甲')
    expect(parsed.data[0]!.orderIdView).toContain(ROW.order_no)
    expect(parsed.data[0]!.orderSta).toContain('已完成')
    expect(parsed.data[0]!.orderRemark).toContain('测试备注甲')

    opts.done!(parsed, 1, parsed.count)
    expect(root.querySelector('#total')!.textContent).toBe('【统计】共57笔被删除的订单')
    expect(RECYCLE_TOTAL_TEMPLATE(57)).toBe('【统计】共57笔被删除的订单')
    // 后端漏 items/total 时也不抛（回落成空页 + count 0）。
    expect(opts.parseData!({ code: 0, msg: '', data: undefined })).toMatchObject({
      count: 0,
      data: [],
    })
  })

  it('⭐ 「删除人」优先 `deleted_by_name`（账号名称）；旧后端缺这个键 ⇒ 回落 `deleted_by`（登录账号）', async () => {
    await mountPage()
    const opts = renderOpts()
    const parse = (row: Record<string, unknown>): Array<Record<string, string>> =>
      opts.parseData!({ code: 0, msg: '', data: { items: [row], total: 1, page: 1, page_size: 20 } })
        .data

    // 两个值**不同形**（昵称 vs 登录账号）⇒ 读错键就立刻红，不会「碰巧相同」。
    expect(parse(ROW)[0]!.delUserName).toBe('测试昵称甲')
    // 部署窗口（前端新、后端旧）：后端**不回**这个键 ⇒ 回落登录账号快照，⛔ 不是空白。
    const legacy: Record<string, unknown> = { ...ROW }
    delete legacy.deleted_by_name
    expect('deleted_by_name' in legacy).toBe(false)
    expect(parse(legacy)[0]!.delUserName).toBe('测试操作人甲')
  })

  it('⭐ `where` 里 7 个筛选参数名与值逐字正确；`0` 不被丢、空串不发', async () => {
    const root = await mountPage()
    root.querySelector<HTMLSelectElement>('select#gameName')!.value = '7'
    root.querySelector<HTMLSelectElement>('select#orderSta')!.value = 'COMPLETED'
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-09-01 00:00:00'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-09-24 23:59:59'
    root.querySelector<HTMLSelectElement>('select#orderSoures')!.innerHTML =
      '<option value="5">测试来源甲</option>'
    root.querySelector<HTMLSelectElement>('select#orderSoures')!.value = '5'
    root.querySelector<HTMLInputElement>('#key1')!.value = ' 测试关键字甲 '
    root.querySelector<HTMLInputElement>('#gameAcc')!.value = ' zhanghao_test_001 '
    LAYUI.table.reload.mockClear()
    await submitSearch()
    expect(lastReload().where).toEqual({
      game: 7,
      status: 'COMPLETED',
      deleted_from: '2026-09-01 00:00:00',
      deleted_to: '2026-09-24 23:59:59',
      order_source: 5,
      keyword: '测试关键字甲',
      game_account: 'zhanghao_test_001',
    })
    // 页码/页长**不在** `where` 里——layui 自己带 `page`/`page_size`，`where` 里那份会盖掉它。
    expect('page' in lastReload().where).toBe(false)
    expect('page_size' in lastReload().where).toBe(false)

    // ⭐ 边界：`0` 是「给了」，不许被真值判据丢掉（E-0021）；空串 / 纯空白 = 没选 ⇒ 不发。
    const game = root.querySelector<HTMLSelectElement>('select#gameName')!
    // 控件平时选不出 0（option 来自真实主键）⇒ 合成一项；判据不能靠「控件选不出来」。
    game.insertAdjacentHTML('beforeend', '<option value="0">零号游戏</option>')
    game.value = '0'
    root.querySelector<HTMLSelectElement>('select#orderSta')!.value = ''
    root.querySelector<HTMLInputElement>('#key1')!.value = '   '
    LAYUI.table.reload.mockClear()
    await submitSearch()
    const where = lastReload().where
    expect(where.game).toBe(0)
    expect('status' in where).toBe(false)
    expect('keyword' in where).toBe(false)
  })

  it('⭐ 翻页 / 改「条/页」由 layui 的 url 通道负责；页面记住真实页码与实时页长，下一次取数带对', async () => {
    await mountPage()
    const opts = renderOpts()
    // ① 分页条的两处**真实出口**：`request` 把 layui 的 page/limit 映射成后端的 page/page_size；
    //    `page.limits` 逐字取对标（`orderrecycle.js:174-176`）⇒ 点页码 / 改「条/页」layui 自己会取数。
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(opts.page).toEqual({ limit: 20, limits: [10, 20, 30] })

    // ② 用户点第 3 页：layui 取完数后调 `done(res, 3, 57)`（第 2 参 = 刚加载的页码）。
    LAYUI.table.reload.mockClear()
    opts.done!(undefined, 3, 57)
    // 用户把「条/页」改成 30：laypage **就地**把它写进 `config.page.limit`（`getOptions` 读到的就是 30）。
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 30 } })

    // ③ 下一次由**页面**发起的取数（工具栏「刷新列表」）必须带对刚翻到的页码与用户选的页长——
    //    带错就会把用户的选择顶掉（订单列表 E-0037 同族）。
    await driveToolbar('refresh')
    expect(lastReload().page.curr).toBe(3)
    expect(lastReload().page.limit).toBe(30)
  })
})

describe('回收站页：还原（D9 代价②，驱动真交互）', () => {
  it('工具栏「批量还原」勾 >1 笔 ⇒ 零请求 + 批量未开放提示', async () => {
    await mountPage()
    vi.mocked(request).mockClear()
    LAYUI.setRows([
      { order_no: '20260924000000000501' },
      { order_no: '20260924000000000502' },
    ])
    await driveToolbar('refOrder')
    expect(LAYUI.msgs).toContain(RECYCLE_BATCH_NOT_OPEN_TEXT)
    expect(RECYCLE_BATCH_NOT_OPEN_TEXT).toBe('批量操作尚未开放，请只勾选一笔订单')
    expect(request).not.toHaveBeenCalled()
  })

  it('工具栏「批量还原」勾 0 笔 ⇒ 零请求 + 「请选中要操作的记录」', async () => {
    await mountPage()
    vi.mocked(request).mockClear()
    LAYUI.setRows([])
    await driveToolbar('refOrder')
    expect(LAYUI.msgs).toContain(RECYCLE_ROW_SELECT_TEXT)
    expect(RECYCLE_ROW_SELECT_TEXT).toBe('请选中要操作的记录')
    expect(request).not.toHaveBeenCalled()
  })

  it('工具栏「批量还原」勾**恰好 1** 笔 ⇒ 一发 POST .../restore/ 并让 layui 重取列表', async () => {
    await mountPage()
    vi.mocked(request).mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.setRows([{ order_no: ROW.order_no }])
    await driveToolbar('refOrder')
    const posts = callsByMethod('post')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.url).toBe(`/api/business/orders/${ROW.order_no}/restore/`)
    // 成功后就地重取：走 layui 的 `url` 通道（`table.reload`），⛔ 不是页内自己再发一发 `request()`。
    expect(callsByMethod('get')).toHaveLength(0)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    expect(lastReload().page.curr).toBe(1)
  })

  it('行内「还原该订单」⇒ 一发 POST .../restore/ 并让 layui 重取列表（`tool(tableList)` 真回调）', async () => {
    await mountPage()
    vi.mocked(request).mockClear()
    LAYUI.table.reload.mockClear()
    await driveTool({ order_no: ROW.order_no })
    const posts = callsByMethod('post')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.url).toBe(`/api/business/orders/${ROW.order_no}/restore/`)
    expect(callsByMethod('get')).toHaveLength(0)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
  })

  it('还原失败：显示后端 msg、**不**重取列表（零请求）', async () => {
    await mountPage()
    vi.mocked(request).mockClear()
    LAYUI.table.reload.mockClear()
    vi.mocked(request).mockRejectedValueOnce(new Error('您没有执行该操作的权限。') as never)
    await driveTool({ order_no: ROW.order_no })
    expect(LAYUI.msgs).toContain('您没有执行该操作的权限。')
    expect(callsByMethod('get')).toHaveLength(0)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
  })
})

describe('回收站页：三个不可点的去处（每个可点元素都要有去向）', () => {
  it('点订单号那一格（.orderLine 里的 `.orderdetails` 与徽标）⇒ 提示「已删除的订单不能查看详情」，**不跳转**', async () => {
    const root = await mountPage()
    const onNavigate = CTX.onNavigate
    onNavigate.mockClear()
    // 真 DOM：`.orderdetails` 与两个徽标都长在 `.orderLine` 里（见 `buildRecycleRow`）。
    const cell = document.createElement('div')
    cell.className = 'orderLine'
    const badge = document.createElement('a')
    badge.className = 'lableb'
    badge.textContent = '标'
    cell.appendChild(badge)
    const link = document.createElement('a')
    link.className = 'orderdetails'
    cell.appendChild(link)
    root.appendChild(cell)
    link.click()
    expect(LAYUI.msgs).toContain(RECYCLE_DETAIL_NOT_OPEN_TEXT)
    expect(RECYCLE_DETAIL_NOT_OPEN_TEXT).toBe('已删除的订单不能查看详情')
    expect(onNavigate).not.toHaveBeenCalled()
    // 徽标（对标是 `javascript:void(0)` 的裸 `<a>`）也收口到同一句，不留「点了没反应」。
    LAYUI.msgs.length = 0
    badge.click()
    expect(LAYUI.msgs).toContain(RECYCLE_DETAIL_NOT_OPEN_TEXT)
  })

  it('点状态徽标（.setdlsta）与备注（.orderRemark）⇒ 提示「尚未开放」', async () => {
    const root = await mountPage()
    const sta = document.createElement('a')
    sta.className = 'lableb setdlsta'
    root.appendChild(sta)
    const remark = document.createElement('a')
    remark.className = 'orderRemark'
    root.appendChild(remark)
    sta.click()
    expect(LAYUI.msgs).toContain('尚未开放')
    LAYUI.msgs.length = 0
    remark.click()
    expect(LAYUI.msgs).toContain('尚未开放')
  })

  it('⭐ 订单号那一格的 `title` 不再承诺「右键」：本页没有 contextmenu 处理，对标本页也没有', async () => {
    // 断言打在**真出口**上：`buildRecycleRow` 产出的 HTML 就是页面交给 layui 渲染的那一份。
    const cell = buildRecycleRow({ ...ROW }, () => '测试区服甲')
    // 对标原文那两句（`orderrecycle.js:83/87`：「鼠标右键看看~」/「点击查看订单详情，也可以
    // 鼠标右键试试看」）承诺的右键在**本页没有实现**（页内 0 个 `contextmenu` 监听；
    // 对标本页也没加载 jQuery / contextmenu 插件）⇒ 留着就是「说了做不到」，已去掉。
    expect(cell.orderIdView).not.toContain('右键')
    // 改准后的两个 `title` 都与**点击出口同源**（`RECYCLE_DETAIL_NOT_OPEN_TEXT`）⇒
    // 提示与行为不会分叉（左键的真实去向就是 `onRootClick` 的那句固定文案）。
    expect(cell.orderIdView).toContain(
      `data-orderIdView="${ROW.order_no}" title="${RECYCLE_DETAIL_NOT_OPEN_TEXT}"`,
    )
    expect(cell.orderIdView).toContain(
      `data-viewId="${ROW.order_no}" data-id="${ROW.order_no}" style="color:#0094ff" title="${RECYCLE_DETAIL_NOT_OPEN_TEXT}"`,
    )
  })
})

describe('回收站页：纯函数', () => {
  it('toRecycleSearch：空值不发、空格 trim、状态直接送码', () => {
    expect(toRecycleSearch({})).toEqual({})
    expect(
      toRecycleSearch({
        gameName: '',
        orderSta: 'COMPLETED',
        sDate: '',
        eDate: '',
        orderSoures: '',
        key1: '   ',
        gameAcc: '',
      }),
    ).toEqual({ status: 'COMPLETED' })
    expect(
      toRecycleSearch({ gameName: '7', orderSoures: '5', key1: '甲', gameAcc: 'acc' }),
    ).toEqual({ game: 7, order_source: 5, keyword: '甲', game_account: 'acc' })
  })

  it('defaultRecycleDateRange：今天 −2 个自然月 00:00:00 ~ 今天 23:59:59（本地时区）', () => {
    expect(defaultRecycleDateRange(new Date(2026, 8, 20, 15, 30, 0))).toEqual({
      minD: '2026-07-20',
      maxD: '2026-09-20',
      sDate: '2026-07-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
    })
  })

  it('defaultRecycleDateRange：目标月没有那一天时**夹到月末**（⛔ 不滚到下月 1 日）', () => {
    // 朴素写法 `new Date(y, m-2, d)` 在这三天会**溢出**到目标月的下一天：
    // 1/31 → 12/1（11/31 不存在）、8/31 → 7/1（6/31 不存在）、4/29 → 3/1（2/29 不存在）。
    // 溢出后默认区间会**偏移**（起点跑到目标月的下个月），把本该看得见的已删单挡在默认视图外。
    expect(defaultRecycleDateRange(new Date(2026, 0, 31, 12, 0, 0))).toEqual({
      minD: '2025-11-30', // 1/31 减 2 个自然月 = 11/31 不存在 ⇒ 夹到 11/30
      maxD: '2026-01-31',
      sDate: '2025-11-30 00:00:00',
      eDate: '2026-01-31 23:59:59',
    })
    expect(defaultRecycleDateRange(new Date(2026, 7, 31, 12, 0, 0)).minD).toBe('2026-06-30')
    expect(defaultRecycleDateRange(new Date(2026, 3, 29, 12, 0, 0)).minD).toBe('2026-02-28')
    // 目标月有那一天时**不夹**（避免把上面三条实现成「一律月末」）。
    expect(defaultRecycleDateRange(new Date(2026, 8, 20, 12, 0, 0)).minD).toBe('2026-07-20')
  })

  it('buildRecycleRow：阵营列恒空、角色信息按对标拼法缺段留空、备注空值显示「无」', () => {
    const serverName = (id: number | null): string => (id === null ? '' : '测试区服甲')
    const row = buildRecycleRow(
      { ...ROW, customer_ww: '', remark: '', tag: '', is_urgent: false },
      serverName,
    )
    expect(row.gameGroup).toBe('')
    // 对标表达式（`orderrecycle.js:106-113`）：`area + server + '/' + job + '/' + itemRemark`，
    // 其中 `area = gameArea + '/'`（我方无 `gameArea` ⇒ 代入空串 ⇒ area 就是 `/`）；
    // `title` 比正文多 `levelMark` 那一段（我方无该值 ⇒ 空段）。
    expect(row.gameInfo).toBe(
      '<span title="/测试区服甲/测试角色甲//zhanghao_test_001">/测试区服甲/测试角色甲/zhanghao_test_001</span>',
    )
    expect(row.orderRemark).toContain('无')
    expect(row.orderSoure).toBe('<span title="该订单来源：测试来源甲">测试来源甲</span>')
    expect(row.orderIdView).not.toContain('急')
    expect(row.orderIdView).not.toContain('标记')
  })

  it('buildRecycleRow：旺旺非空走旺旺链接；有 tag 时渲染标记徽标（只取首字）', () => {
    const row = buildRecycleRow(
      { ...ROW, customer_ww: '测试旺旺甲', tag: '标记甲' },
      () => '测试区服甲',
    )
    expect(row.orderSoure).toContain('amos.alicdn.com/getcid.aw')
    expect(row.orderSoure).toContain('测试旺旺甲')
    expect(row.orderIdView).toContain('标记：标记甲')
    expect(row.orderIdView).toContain('>标</a>')
    expect(row.orderIdView).toContain('>急</span>')
  })
})
