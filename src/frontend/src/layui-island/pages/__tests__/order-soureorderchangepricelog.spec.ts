// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【上家订单改价记录】页的**门禁**（2026-10-17 线：从 `artifactPage` 静态页改成
 * 自建 island 页 + 真后端，spec §D4 / §5 判据 19–20）。
 *
 * ## 为什么必须有「驱动真交互」的用例
 *
 * 本页**脱离了 `artifactPage`**：那个共享渲染器在捕获阶段把所有 `[lay-event]` / `[lay-filter]`
 * 的点击收口成「尚未开放」并 `stopPropagation` ⇒ 在它下面接 `table.on('toolbar(…)')` 是死路。
 * 自挂载之后，**「点了没反应」是静默的**：`typecheck` 看不见 DOM 选择器对不对、`lint` 看不见
 * 类名里多了个点 ⇒ 本文件是唯一防线。
 *
 * 所以下面用 `table.on` / `form.on` **登记的那个真回调**驱动（= 真人点击时 layui 会调的函数），
 * 行内「订单号」链接则用**列的 `templet` 真产物**造 DOM 再 `.click()`（连「templet 写哪个
 * data 属性」与「处理器读哪个属性」一起钉住）。
 *
 * ## 既有断言一条不删
 *
 * 改造前的 6 条（9 列表头/列宽、checkbox、`d1` 两列复用、`height:'full-110'`、
 * `toolbar`/`defaultToolbar`/`page`、`#total` 的 `done`）**全部保留**，只是「渲染参数」那一组
 * 从 `SPEC.tables[0]` 改钉在真 `TABLE_OPTIONS` 上（同一个出口：`mount` 展开的就是它）。
 */
const EVIDENCE = fs.readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/order-soureorderchangepricelog.html',
  ),
  'utf8',
)

/**
 * layui 的**可控替身**。真 layui 在 jsdom 里加载不了（证据：`order-orderrecycle.spec.ts` 同形）。
 * `vi.hoisted` 是因为 `vi.mock` 的工厂会被提升到 import 之前。
 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const msgs: string[] = []
  return {
    formHandlers,
    tableHandlers,
    msgs,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: {
      render: vi.fn(),
      reload: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: {
      msg: vi.fn((t: string) => {
        msgs.push(t)
      }),
      open: vi.fn(),
    },
    laydate: { render: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(),
  }
})

// 证据件走替身（真实现是运行期 `fetch('replica/…')`）。
vi.mock('@/layui-island/config-replica', () => ({
  loadConfigArtifact: async () => EVIDENCE,
}))

import page, {
  COLS_FOR_TEST,
  defaultPriceLogDateRange,
  formatDelta,
  ORDER_NO_SHAPE_TEXT,
  PAGE_LIMITS,
  TABLE_ELEM,
  TABLE_FILTER,
  TABLE_HEIGHT,
  TABLE_ID,
  TABLE_OPTIONS,
  TOOLBAR_ELEM,
  TOTAL_TEMPLATE,
  toPriceLogRow,
  toUpstreamPriceChangeParams,
  toUpstreamPriceChangeSearchParams,
  UPSTREAM_PRICE_CHANGE_LOGS_PATH,
  UPSTREAM_PRICE_CHANGE_LOGS_URL,
  type PriceCol,
  type PriceLogRow,
  type UpstreamPriceChangeLogRow,
} from '../order-soureorderchangepricelog'

/** 合成夹具（PII 纪律：单号只是 20 位数字的**形状**，人名/游戏名带「测试…甲」的合成后缀）。 */
const ROW: UpstreamPriceChangeLogRow = {
  order_no: '20261017000000000001',
  // ⚠️ 基准行来源号为空串（回落变体）；「来源号非空 ⇒ 展示来源号」的主变体见 D-0301 那组。
  source_order_no: '',
  game_name: '测试游戏甲',
  game_role_name: '测试角色甲',
  before_amount: '10.00',
  after_amount: '12.00',
  source_name: '测试上家甲',
  note: '上家改价',
  occurred_at: '2026-10-17T12:30:45+08:00',
  is_deleted: false,
}

type RenderOpts = Record<string, unknown> & {
  cols?: PriceCol[][]
  where?: Record<string, unknown>
  parseData?: (r: unknown) => { code: number; msg: string; count: number; data: PriceLogRow[] }
  done?: () => void
}

/** 挂载整页。 */
async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, { onNavigate: vi.fn() })
  return root
}

/** `table.render` 收到的唯一那份配置（**真正交给 layui 的出口**，不是页面自造的中间变量）。 */
function renderOpts(): RenderOpts {
  const call = LAYUI.table.render.mock.calls[0]
  expect(call, 'table.render 没有被调用').toBeTruthy()
  return call![0] as RenderOpts
}

/** 最近一次 `table.reload` 的入参；没有就当场红。⚠️ 第一个实参必须是实例 id（陷阱 ①）。 */
function lastReload(): [string, { where: Record<string, unknown>; page: { curr?: number } }] {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  const call = calls[calls.length - 1] as unknown[]
  expect(call[0]).toBe(TABLE_ID)
  return call as [string, { where: Record<string, unknown>; page: { curr?: number } }]
}

/** 取某一列（按 `title`）。 */
function colByTitle(cols: PriceCol[], title: string): PriceCol {
  const col = cols.find((c) => c.title === title)
  expect(col, `没有标题为「${title}」的列`).toBeTruthy()
  return col!
}

/** 把某一列的 `templet` 真产物变成 DOM 节点（这样 `data-order-no` 这类属性也被驱动到）。 */
function renderCell(col: PriceCol, row: PriceLogRow): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = (col.templet as (d: PriceLogRow) => string)(row)
  return host
}

/** 驱动 `form.on('submit(seachBtn)')` 登记的**真回调**（= 真人点「搜索」）。 */
function submitSearch(): void {
  const submit = LAYUI.formHandlers.get('submit(seachBtn)')
  expect(submit, '本页没有注册搜索按钮的 submit 处理器').toBeTypeOf('function')
  submit!()
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  // 事件处理器表挂在 `Map` 上，`clearAllMocks()` 清不掉——不清的话上一次挂载注册的处理器
  // 会留在表里，「页面是否注册了某事件」这类断言会靠残留**假通过**。
  LAYUI.formHandlers.clear()
  LAYUI.tableHandlers.clear()
  LAYUI.msgs.length = 0
})

describe('A. 列声明与渲染参数（既有断言，一条不删）', () => {
  it('9 列，顺序与文案对齐 SoureOrderChangePriceLog.js L40-84', () => {
    expect(COLS_FOR_TEST.map((c) => c.title)).toEqual([
      undefined,
      '订单号',
      '游戏名称',
      '角色名',
      '金额变动',
      '本次金额',
      '变动金额',
      '说明',
      '变动时间',
    ])
    expect(COLS_FOR_TEST.map((c) => c.width)).toEqual([30, 180, 120, 150, 120, 100, 100, 150, 150])
  })

  it('第 1 列是 checkbox/fixed:left/width 30（L40）', () => {
    expect(COLS_FOR_TEST[0]).toMatchObject({ type: 'checkbox', fixed: 'left', width: 30 })
  })

  it('9 列的 `field` / `align` 与证据件逐条对拍（spec §5 判据 19）', () => {
    // `field` 序列里 `d1` 出现两次是**复刻值**（见下一条）；空 `align` = 证据件没写（左对齐）。
    expect(COLS_FOR_TEST.map((c) => c.field)).toEqual([
      undefined,
      'orderId',
      'str2',
      'str3',
      'd1',
      'd2',
      'd1',
      'str5',
      'str4',
    ])
    expect(COLS_FOR_TEST.map((c) => c.align)).toEqual([
      undefined,
      undefined,
      'center',
      undefined,
      'center',
      'center',
      'center',
      'center',
      'center',
    ])
  })

  it('`d1` 被**两列复用**（金额变动 L58 与 变动金额 L70 都是 field:\'d1\'）——对标原样，别改成 d3', () => {
    expect(COLS_FOR_TEST.filter((c) => c.field === 'd1').map((c) => c.title)).toEqual([
      '金额变动',
      '变动金额',
    ])
    // 「本次金额」是 **d2**（L64），不是 d1。
    expect(COLS_FOR_TEST.find((c) => c.title === '本次金额')?.field).toBe('d2')
  })

  it('`height` 逐字取对标 L34 的 `full-110`', () => {
    expect(TABLE_OPTIONS.height).toBe(TABLE_HEIGHT)
    expect(TABLE_HEIGHT).toBe('full-110')
  })

  it('表格级：elem/toolbar/defaultToolbar(生效的 filter)/page(limit 20, limits 五档)', () => {
    expect(TABLE_OPTIONS.elem).toBe(TABLE_ELEM)
    expect(TABLE_OPTIONS.toolbar).toBe(TOOLBAR_ELEM)
    expect(TABLE_OPTIONS.defaultToolbar).toEqual(['filter'])
    // ⛔ 钉**字面值**、不钉 `PAGE_LIMITS` 那个常量：拿常量比自己会让「常量被改成 [20]」照样绿。
    expect(TABLE_OPTIONS.page).toEqual({ limit: 20, limits: [20, 30, 40, 50, 100] })
    expect([...PAGE_LIMITS]).toEqual([20, 30, 40, 50, 100])
  })

  it('`#total` 的 `done` 存在（对标 L92 往 `#total` 写统计文案）', async () => {
    await mountPage()
    // `done` 是 `mount` 里每次渲染时给的（要读闭包里的 `lastTotal`），不在静态 `TABLE_OPTIONS` 里
    // ⇒ 读真 `table.render` 的入参（与生产同一个出口，不另造中间量）。
    expect(renderOpts().done).toBeTypeOf('function')
  })
})

describe('B. 行映射：后端 9 键 → 证据件字段', () => {
  it('9 个键逐个落到列字段上（orderId/str2/str3/d1/d2/str5/str4/str1/str6）', () => {
    expect(toPriceLogRow(ROW)).toEqual({
      orderId: '20261017000000000001',
      // ⭐ D-0301：展示单号 = `source_order_no || order_no`（基准行来源号空 ⇒ 回落）。
      orderIdView: '20261017000000000001',
      str2: '测试游戏甲',
      str3: '测试角色甲',
      d1: '10.00',
      d2: '12.00',
      str5: '上家改价',
      // `formatCreatedAt`（`@/api/auditLogs`）的既定形状。
      str4: '2026-10-17 12:30:45',
      str1: '',
      str6: '',
    })
  })

  it('`is_deleted` ⇒ `str6` 为 Y；`null` 金额 ⇒ 空串（由 templet 显示 `—`）', () => {
    const row = toPriceLogRow({ ...ROW, before_amount: null, after_amount: null, is_deleted: true })
    expect(row.str6).toBe('Y')
    expect(row.d1).toBe('')
    expect(row.d2).toBe('')
  })
})

describe('C. 三个金额/单号 templet（逐字照证据件的视觉语义）', () => {
  it('「订单号」正常单：蓝链 + `data-order-no` + `title`，点开详情走这个属性', () => {
    const cell = renderCell(colByTitle(COLS_FOR_TEST, '订单号'), toPriceLogRow(ROW))
    const a = cell.querySelector('a')!
    expect(a.className).toBe('orderdetails')
    expect(a.getAttribute('style')).toBe('color:#0094ff')
    expect(a.getAttribute('title')).toBe('点击查看订单详情')
    expect(a.dataset.orderNo).toBe('20261017000000000001')
    expect(a.textContent).toBe('20261017000000000001')
  })

  it('⭐ D-0301（2026-10-24）「订单号」：来源号非空 ⇒ 正文与 `data-order-id-view` 逐字是来源号，寻址键照旧内置号', () => {
    const cell = renderCell(
      colByTitle(COLS_FOR_TEST, '订单号'),
      toPriceLogRow({ ...ROW, source_order_no: 'SRC-TEST-1' }),
    )
    const a = cell.querySelector('a')!
    expect(a.textContent).toBe('SRC-TEST-1')
    expect(a.dataset.orderIdView).toBe('SRC-TEST-1')
    // ⛔ 寻址键不换（详情开层的形状闸只认 20 位）。
    expect(a.dataset.orderNo).toBe('20261017000000000001')
  })

  it('「订单号」已删单：前置红色「删」徽标、**没有链接**（不可点）', () => {
    const cell = renderCell(
      colByTitle(COLS_FOR_TEST, '订单号'),
      toPriceLogRow({ ...ROW, is_deleted: true }),
    )
    const badge = cell.querySelector<HTMLElement>('span.lableb')!
    expect(badge.textContent).toBe('删')
    expect(badge.getAttribute('style')).toBe('background-color:#FF534D')
    expect(badge.getAttribute('title')).toBe('该订单已被删除了')
    expect(cell.firstElementChild).toBe(badge)
    // 单号仍在（纯文本），但**不是**链接 ⇒ 行内点击处理器找不到 `a.orderdetails`。
    expect(cell.textContent).toBe('删20261017000000000001')
    expect(cell.querySelector('a')).toBeNull()
  })

  it('「订单号」单元格里的 HTML 被转义（单号是请求来源的文本）', () => {
    const html = (
      colByTitle(COLS_FOR_TEST, '订单号').templet as (d: PriceLogRow) => string
    )(toPriceLogRow({ ...ROW, order_no: '<img src=x onerror=alert(1)>' }))
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })

  it('「金额变动」= `{d1}>>{d2}` + `title="从…元变成…元"`（`#2f363c`）', () => {
    const cell = renderCell(colByTitle(COLS_FOR_TEST, '金额变动'), toPriceLogRow(ROW))
    const span = cell.querySelector('span')!
    expect(span.textContent).toBe('10.00>>12.00')
    expect(span.getAttribute('title')).toBe('从10.00元变成12.00元')
    expect(span.getAttribute('style')).toBe('color:#2f363c')
  })

  it('「金额变动」空值 ⇒ 该侧 `—`（`formatMoney` 的既有口径）', () => {
    const missingBefore = renderCell(
      colByTitle(COLS_FOR_TEST, '金额变动'),
      toPriceLogRow({ ...ROW, before_amount: null }),
    )
    expect(missingBefore.querySelector('span')!.textContent).toBe('—>>12.00')
    const missingAfter = renderCell(
      colByTitle(COLS_FOR_TEST, '金额变动'),
      toPriceLogRow({ ...ROW, after_amount: null }),
    )
    expect(missingAfter.querySelector('span')!.textContent).toBe('10.00>>—')
  })

  it('「本次金额」= `{d2}`（`#FF5722`）；空 ⇒ `—`', () => {
    const cell = renderCell(colByTitle(COLS_FOR_TEST, '本次金额'), toPriceLogRow(ROW))
    const span = cell.querySelector('span')!
    expect(span.textContent).toBe('12.00')
    expect(span.getAttribute('style')).toBe('color:#FF5722')
    const empty = renderCell(
      colByTitle(COLS_FOR_TEST, '本次金额'),
      toPriceLogRow({ ...ROW, after_amount: null }),
    )
    expect(empty.querySelector('span')!.textContent).toBe('—')
  })

  it('「变动金额」三档：涨 ⇒ `+差额`、跌 ⇒ `-差额`、相等 ⇒ `0`（全部 `#FF5722`）', () => {
    const delta = (before: string, after: string): string =>
      (colByTitle(COLS_FOR_TEST, '变动金额').templet as (d: PriceLogRow) => string)(
        toPriceLogRow({ ...ROW, before_amount: before, after_amount: after }),
      )
    expect(delta('10.00', '12.50')).toBe('<span style="color:#FF5722">+2.50</span>')
    expect(delta('12.50', '10.00')).toBe('<span style="color:#FF5722">-2.50</span>')
    expect(delta('10.00', '10.00')).toBe('<span style="color:#FF5722">0</span>')
    expect(formatDelta('10.00', '12.50')).toBe('+2.50')
  })

  it('「变动金额」任一为空 ⇒ `—`（两个方向都钉）', () => {
    expect(formatDelta('', '12.00')).toBe('—')
    expect(formatDelta('10.00', '')).toBe('—')
    expect(formatDelta('', '')).toBe('—')
    const cell = renderCell(
      colByTitle(COLS_FOR_TEST, '变动金额'),
      toPriceLogRow({ ...ROW, after_amount: null }),
    )
    expect(cell.querySelector('span')!.textContent).toBe('—')
  })
})

describe('D. 默认区间：今天 − 6 个自然月 ~ 今天（固定 now 注入）', () => {
  it('快照式的固定日：2026-10-17 ⇒ 2026-04-17 / 2026-10-17（不带时分秒）', () => {
    expect(defaultPriceLogDateRange(new Date(2026, 9, 17, 12, 34, 56))).toEqual({
      sDate: '2026-04-17',
      eDate: '2026-10-17',
    })
  })

  it('跨年也按自然月回退（2026-01-15 ⇒ 2025-07-15），⛔ 不是「减 180 天」', () => {
    expect(defaultPriceLogDateRange(new Date(2026, 0, 15, 0, 0, 0))).toEqual({
      sDate: '2025-07-15',
      eDate: '2026-01-15',
    })
  })

  it('挂载后两个日期框 = 同一个运行期区间，且交给 laydate 的 type 是 `date`', async () => {
    const root = await mountPage()
    const expected = defaultPriceLogDateRange()
    expect([root.querySelector<HTMLInputElement>('#sDate')!.value, root.querySelector<HTMLInputElement>('#eDate')!.value]).toEqual([
      expected.sDate,
      expected.eDate,
    ])
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#sDate', type: 'date' })
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#eDate', type: 'date' })
  })
})

describe('E. 筛选参数映射（唯一形状出口）', () => {
  it('`sDate→stime` / `eDate→etime` / `key1→key`', () => {
    expect(
      toUpstreamPriceChangeSearchParams({
        sDate: '2026-04-17',
        eDate: '2026-10-17',
        key1: ' 20261017000000000001 ',
      }),
    ).toEqual({
      stime: '2026-04-17',
      etime: '2026-10-17',
      key: '20261017000000000001',
    })
  })

  it('空值 / 空串**一律不发**（键集合钉死）', () => {
    expect(toUpstreamPriceChangeParams({})).toEqual({})
    expect(
      toUpstreamPriceChangeParams(
        toUpstreamPriceChangeSearchParams({ sDate: '', eDate: '   ', key1: '' }),
      ),
    ).toEqual({})
    expect(
      toUpstreamPriceChangeParams(
        toUpstreamPriceChangeSearchParams({ sDate: '2026-04-17', eDate: '', key1: '' }),
      ),
    ).toEqual({ stime: '2026-04-17' })
  })

  it('接口路径与 layui 的 `url`/`method`/分页参数名逐字正确（BASE_URL 拼出来，⛔ 不写死 `/`）', () => {
    expect(UPSTREAM_PRICE_CHANGE_LOGS_PATH).toBe('/api/business/upstream-price-change-logs/')
    expect(UPSTREAM_PRICE_CHANGE_LOGS_URL).toBe(UPSTREAM_PRICE_CHANGE_LOGS_PATH)
    expect(TABLE_OPTIONS.url).toBe(UPSTREAM_PRICE_CHANGE_LOGS_URL)
    expect(TABLE_OPTIONS.method).toBe('get')
    // layui 的 `page`/`limit` → 我们后端的 `page`/`page_size`（逐字取 `EnvelopePagination`）。
    expect(TABLE_OPTIONS.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    // ⛔ 本地 `data` 模式会让分页条的 count = 本页行数（分页条假的）。
    expect('data' in TABLE_OPTIONS).toBe(false)
  })
})

describe('F. 挂载与真交互（layui 换可控替身；本页已脱离 artifactPage）', () => {
  it('首屏 `where` 就带两个默认日期（不是全表最新 20 条）', async () => {
    const root = await mountPage()
    const opts = renderOpts()
    const sDate = root.querySelector<HTMLInputElement>('#sDate')!.value
    const eDate = root.querySelector<HTMLInputElement>('#eDate')!.value
    expect(opts.where).toEqual({ stime: sDate, etime: eDate })
    // 值是日期形状（证据件的 `value="2026-03-20"` 同形），⛔ 不是 datetime。
    expect(sDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(eDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('点「搜索」⇒ `table.reload` 带当前筛选的线上参数（回第 1 页）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-01-01'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-06-30'
    root.querySelector<HTMLInputElement>('#key1')!.value = ' 20261017000000000001 '
    submitSearch()
    expect(LAYUI.table.reload).toHaveBeenCalledWith(TABLE_ID, {
      where: { stime: '2026-01-01', etime: '2026-06-30', key: '20261017000000000001' },
      page: { curr: 1 },
    })
  })

  it('清空筛选再搜索 ⇒ `where` 只剩日期（空值不发）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('#sDate')!.value = ''
    root.querySelector<HTMLInputElement>('#eDate')!.value = ''
    root.querySelector<HTMLInputElement>('#key1')!.value = '   '
    submitSearch()
    expect(Object.keys(lastReload()[1].where)).toEqual([])
  })

  it('「回车 = 搜索」：对 `#key1` 派发 Enter ⇒ 重取并回第 1 页；别的键不触发', async () => {
    const root = await mountPage()
    const key1 = root.querySelector<HTMLInputElement>('input#key1')!
    key1.value = '20261017000000000009'
    const before = LAYUI.table.reload.mock.calls.length
    key1.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    )
    expect(LAYUI.table.reload.mock.calls.length).toBe(before + 1)
    const [id, opts] = lastReload()
    expect(id).toBe(TABLE_ID)
    expect(opts.page).toEqual({ curr: 1 })
    expect(opts.where.key).toBe('20261017000000000009')
    key1.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    expect(LAYUI.table.reload.mock.calls.length).toBe(before + 1)
  })

  it('⛔ 回车**不绑**两个日期框（laydate 自己把 Enter 当「确定」）', async () => {
    const root = await mountPage()
    const before = LAYUI.table.reload.mock.calls.length
    for (const selector of ['input#sDate', 'input#eDate']) {
      root
        .querySelector<HTMLInputElement>(selector)!
        .dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
        )
    }
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
  })

  it('工具栏「刷新列表」⇒ 回第 1 页重取；别的事件不重取', async () => {
    await mountPage()
    const toolbar = LAYUI.tableHandlers.get(`toolbar(${TABLE_FILTER})`)
    expect(toolbar).toBeTypeOf('function')
    toolbar!({ event: 'refresh' })
    expect(lastReload()[1].page).toEqual({ curr: 1 })
    const before = LAYUI.table.reload.mock.calls.length
    toolbar!({ event: 'something_else' })
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
  })

  it('`parseData` 吃统一信封：`count` = 服务端 total(57)、`data` = 映射后的行', async () => {
    await mountPage()
    const opts = renderOpts()
    const parsed = opts.parseData!({
      code: 0,
      msg: '',
      data: { items: [ROW], total: 57, page: 1, page_size: 20 },
    })
    expect(parsed.code).toBe(0)
    // 分页条拿到的就是**服务端 total**（⛔ 不是本页那 1 行）。
    expect(parsed.count).toBe(57)
    expect(parsed.data).toEqual([toPriceLogRow(ROW)])
    // 后端漏 items/total 时也不抛（回落成空页 + count 0）。
    expect(opts.parseData!({ code: 0, msg: '', data: undefined })).toMatchObject({
      count: 0,
      data: [],
    })
  })

  it('`done` 把 `#total` 写成 `本次搜索统计共N条记录`（N = 服务端 total）', async () => {
    const root = await mountPage()
    // jsdom 里 table 是替身 ⇒ 手工补上工具栏里那个容器，再按**真 layui 的顺序**驱动。
    root.insertAdjacentHTML('beforeend', '<div id="total"></div>')
    const opts = renderOpts()
    opts.parseData!({ code: 0, msg: '', data: { items: [ROW], total: 57, page: 1, page_size: 20 } })
    opts.done!()
    expect(root.querySelector('#total')!.textContent).toBe('本次搜索统计共57条记录')
    expect(TOTAL_TEMPLATE(57)).toBe('本次搜索统计共57条记录')
    // 换一页/换筛选后再来一发：文案跟着**新响应**的 total 走，不是首屏那个数。
    opts.parseData!({ code: 0, msg: '', data: { items: [], total: 3, page: 1, page_size: 20 } })
    opts.done!()
    expect(root.querySelector('#total')!.textContent).toBe('本次搜索统计共3条记录')
  })

  it('「订单号」用**列的 templet 真产物**点击：20 位才开层，开的是订单详情岛页', async () => {
    const root = await mountPage()
    const cell = renderCell(colByTitle(COLS_FOR_TEST, '订单号'), toPriceLogRow(ROW))
    const anchor = cell.querySelector('a')!
    root.appendChild(anchor)
    anchor.click()
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.type).toBe(2)
    expect(String(opts.content)).toContain(
      'layui-island/page.html?page=order-orderview&order_no=20261017000000000001',
    )
    expect(String(opts.title)).toContain('订单详情')
    expect(LAYUI.msgs).toEqual([])
  })

  it('「订单号」形状闸：单号形状不对 ⇒ **不开层**、只给固定文案（⛔ 不把输入拼进提示）', async () => {
    const root = await mountPage()
    const bad = '<img src=x onerror=alert(1)>'
    const cell = renderCell(
      colByTitle(COLS_FOR_TEST, '订单号'),
      toPriceLogRow({ ...ROW, order_no: bad }),
    )
    const anchor = cell.querySelector('a')!
    // `dataset` 会把 attribute 实体解码回 `<`/`>`——那正是「必须有形状闸」的理由：
    // `layer.open` 的 `title` 是 HTML 上下文，templet 里那次 escape 在这条链上不构成保护。
    expect(anchor.dataset.orderNo).toBe(bad)
    root.appendChild(anchor)
    anchor.click()
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toEqual([ORDER_NO_SHAPE_TEXT])
    expect(LAYUI.msgs.join('')).not.toContain('<img')
  })

  it('已删单那一格**没有锚** ⇒ 点击不会开详情层（只有「删」徽标 + 纯文本）', async () => {
    const root = await mountPage()
    const cell = renderCell(
      colByTitle(COLS_FOR_TEST, '订单号'),
      toPriceLogRow({ ...ROW, is_deleted: true }),
    )
    root.appendChild(cell)
    cell.click()
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toEqual([])
  })

  it('证据件的表格 / 工具栏模板（按钮文案「刷新列表」逐字）与 3 个筛选控件都在页面上', async () => {
    const root = await mountPage()
    expect(root.querySelector('#tableList')).toBeTruthy()
    // 收集成清单再断言（⛔ 不在循环里调 `expect`，`vitest/no-conditional-expect`）。
    const missing = ['sDate', 'eDate', 'key1'].filter(
      (id) => root.querySelector(`#${id}`) === null,
    )
    expect(missing).toEqual([])
    // 工具栏是 `<script type="text/html" id="toolbarDemo">`（layui 的**模板**）：它里面的标记
    // **不进 DOM 树**，所以按模板原文断言——layui 渲染工具栏时读的正是这段字符串。
    const toolbarTpl = document.getElementById('toolbarDemo')!
    expect(toolbarTpl.textContent).toContain('lay-event="refresh"')
    expect(toolbarTpl.textContent).toContain('刷新列表')
    expect(toolbarTpl.textContent).toContain('id="total"')
  })
})
