// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【结算记录】页（D-0319 按天聚合重设计，2026-10-28）的单元 + 挂载测试。
 *
 * 旧版（对标复刻页，1880 行 spec）的断言面随界面一起下线；本文件按**新形态**重写，
 * 断言密度不缩水（计划 §2.2 的要求）。驱**真交互**（真 DOM 点击 / 键盘事件）的那部分
 * 在 `emp-billsummary.interaction.spec.ts`；本文件盯：
 *
 * 1. 列与渲染参数声明（主表 4 列 / 明细 7 列、`page` 真假、`totalRow`）；
 * 2. 纯函数（`datePart` / `toDailyParams` / `toDetailParams` / `settledOrPayable` /
 *    `formatSettledAt` / `defaultDateRange` / `buildDisplayRows` / `sourceDisplayName` /
 *    `sourceParamValue`）；
 * 3. 单元格渲染（函数 templet 的转义 / 回落 / 空态；D-0323 的天行展开开关与来源子行）；
 * 4. 挂载行为（首屏取数、服务端分页的 laypage、403 的可见失败、明细展开、导出、卸载、
 *    D-0323 的金额列展开 / lazy 缓存 / 失败重试 / 换页作废）。
 *
 * ## 手法
 *
 * - `vi.mock('@/api/http')`：取数走**真的** `api/settlements.ts`（分页循环是真的），
 *   底层 `request` 打桩、按 URL 分流（`daily-summary/sources` / `daily-summary` /
 *   `/settlements`）；
 * - `vi.mock('@/layui/island')`（部分）：jsdom 里加载不了真 layui，`bootstrapIsland`
 *   换成可控替身 ⇒ `table.render` / `table.reload` / `laypage.render` 的**入参**可断言；
 * - `vi.mock('@/api/performances')`（部分）：只替 `downloadPerformanceExport`
 *   （导出是真下载，不走 `request()`）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：来源单号故意不是 19 位数字（真值形状会命中
 * `pii-guard` 的 19 位模式）。
 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/**
 * layui 的**可控替身**（与 `emp-myemp.spec.ts` / `agents-taker-wallet.interaction.spec.ts`
 * 同一手法）：`layer.msg` / `layer.open` / `table.render` / `table.reload` /
 * `laypage.render` 的入参在这里被断言；layui 自己的事件（toolbar）由测试经
 * `tableHandlers` 手动触发（真交互版在 interaction spec 里走真 DOM）。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const open = vi.fn()
  const reload = vi.fn()
  const render = vi.fn()
  const laydateRender = vi.fn()
  const laypageRender = vi.fn()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    msg,
    open,
    reload,
    render,
    laydateRender,
    laypageRender,
    tableHandlers,
    table: {
      render,
      reload,
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: { open, msg },
    laydate: { render: laydateRender },
    laypage: { render: laypageRender },
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

/**
 * `@/api/performances` 的**部分替身**：只替 `downloadPerformanceExport`
 * （导出是真下载：这条路不走 `request()`），其余具名导出照旧。
 *
 * ⚠️ 默认实现**必须回一个真 `{blob, filename}`**：页面会 `URL.createObjectURL(blob)` 落文件，
 * 回 `undefined` 会让 `.then` 当场 TypeError（那是替身造的假红，不是页面错）。
 */
vi.mock('@/api/performances', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/performances')>()
  return {
    ...actual,
    downloadPerformanceExport: vi.fn(async () => ({
      blob: new Blob(['PK\u0003\u0004 fake-xlsx'], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      }),
      filename: 'performances-20261028.xlsx',
    })),
  }
})

import { request } from '@/api/http'
import { BizError } from '@/api/errors'
import type { SettlementDailyItem, SettlementListItem, SettlementSourceItem } from '@/api/settlements'
import { downloadPerformanceExport } from '@/api/performances'
import {
  AMOUNT_TOGGLE_CLASS,
  buildDailyCols,
  buildDetailCols,
  buildDisplayRows,
  DAILY_COLS,
  DAILY_PAGE_LIMITS,
  DAILY_PAGE_SIZE,
  DAILY_TABLE,
  DATE_SHAPE,
  datePart,
  defaultDateRange,
  DETAIL_COLS,
  DETAIL_PAGE_LIMIT,
  DETAIL_PAGE_LIMITS,
  DETAIL_TABLE,
  formatSettledAt,
  PANEL_CLASS,
  PANEL_SELECTOR,
  SOURCE_VIEW_CLASS,
  SOURCE_RETRY_CLASS,
  sourceDisplayName,
  sourceParamValue,
  TABLE2_ID,
  TABLE_ID,
  TITLE,
  toDailyParams,
  toDetailParams,
  settledOrPayable,
  UNSPECIFIED_SOURCE_LABEL,
  VIEW_DETAIL_CLASS,
  VIEW_DETAIL_SELECTOR,
  buildHtml,
  type DailyRow,
  type SourcesState,
} from '../emp-billsummary'

const mockedRequest = vi.mocked(request)

/** 一行按天汇总（字段与后端 `SettlementDailySummaryView` 出参同形）。 */
function day(over: Partial<SettlementDailyItem>): SettlementDailyItem {
  return { date: '2026-10-28', order_count: 3, total_amount: '20.00', ...over }
}

const DAYS: SettlementDailyItem[] = [
  day({ date: '2026-10-28', order_count: 3, total_amount: '20.00' }),
  day({ date: '2026-10-27', order_count: 1, total_amount: '5.00' }),
  day({ date: '2026-10-26', order_count: 2, total_amount: '-15.00' }),
]

/** 一行结算明细（字段与后端 `SettlementListSerializer` 同形，金额是两位小数字符串）。 */
function row(over: Partial<SettlementListItem> & { id: number; user: number | null }): SettlementListItem {
  return {
    order_no: '20260920213215233029',
    // ⚠️ 合成值、故意不是 19 位数字（真值形状会命中 pii-guard 的 19 位模式）；
    // 与 `order_no` 不同形是**承重**的（「正文取哪个字段」的断言靠它分辨）。
    source_order_no: 'TB-合成-结算-来源甲',
    username: null,
    emp_type: '打单工资',
    temp_amount: '10.00',
    bonus_amount: '10.00',
    deduct_mode: '',
    deduct_value: '0.00',
    taker_price: null,
    review_status: '已通过',
    settle_status: '已结账',
    created_at: '2026-09-20T21:32:15+08:00',
    payable_net: '10.00',
    // 默认造「已结账行」：本页明细只收已结账行（`toDetailParams` 必带 `settle_status`），
    // 未结账的形状只在「空态渲染」那组用例里显式传 `settled_amount: null`。
    settled_amount: '10.00',
    project: '深渊满星',
    settled_at: '2026-10-28T21:00:00+08:00',
    settled_by: '财务甲',
    ...over,
  }
}

/** 明细的合成数据：同一天、两个人、三行（跨页取全的用例靠它分辨「只取一页」）。 */
const ROWS: SettlementListItem[] = [
  row({ id: 1, user: 7, username: '小李', display_name: '合成小李' }),
  row({ id: 2, user: 7, username: '小李', display_name: '合成小李', order_no: '20260920213215233030' }),
  row({ id: 3, user: 8, username: '小王', display_name: '合成小王', order_no: '20260920213215233031' }),
]

/** 一行来源分解（字段与后端 `SettlementDailySourcesView` 出参同形）。 */
function sourceItem(over: Partial<SettlementSourceItem>): SettlementSourceItem {
  return { source_id: 5, source_name: '甲店铺', order_count: 2, total_amount: '15.00', ...over }
}

/** 来源分解的合成数据：一个指定来源 + 一个「未指定」行（`null`/`null`）。 */
const SOURCES: SettlementSourceItem[] = [
  sourceItem({ source_id: 5, source_name: '甲店铺', order_count: 2, total_amount: '15.00' }),
  sourceItem({ source_id: null, source_name: null, order_count: 1, total_amount: '5.00' }),
]

/** 天行的联合包装（D-0323 起主表 templet 吃 `DailyRow`）。 */
function dayRow(d: SettlementDailyItem): DailyRow {
  return { kind: 'day', day: d }
}

type ParamValue = string | number | boolean
type ReqConfig = { method?: string; url?: string; params?: Record<string, ParamValue> }
type ReqImpl = (config: ReqConfig) => Promise<unknown>

/**
 * 默认后端：**按 URL 分流**——`/settlements/daily-summary/sources` 回来源分解、
 * `/settlements/daily-summary` 回按天页、`/settlements` 回明细行页
 * （`fetchAllSettlements` 的分页循环是真的跑）。
 * ⚠️ `sources` 那条分支**必须**排在 `daily-summary` 之前判（前者的 URL 也 includes
 * `'daily-summary'`，判晚了会被吃掉）。
 */
function defaultImpl(
  rows: SettlementListItem[] = ROWS,
  days: SettlementDailyItem[] = DAYS,
  sources: SettlementSourceItem[] = SOURCES,
): ReqImpl {
  return async (config) => {
    const url = String(config.url)
    if (url.includes('/settlements/daily-summary/sources')) {
      return { items: sources }
    }
    if (url.includes('/settlements/daily-summary')) {
      return { items: days, total: days.length, page: 1, page_size: 20 }
    }
    return { items: rows, total: rows.length, page: 1, page_size: 200 }
  }
}

/** 只认一次调用的取数实现（第 N 次之后抛错，用于「失败可见」那组）。 */
function failingImpl(error: Error): ReqImpl {
  let called = false
  return async () => {
    if (!called) {
      called = true
      throw error
    }
    throw error
  }
}

/** 挂整页（取数走**真的** `api/settlements.ts` → 打桩的 `request`）。 */
async function mountPage(impl: ReqImpl = defaultImpl()): Promise<HTMLElement> {
  mockedRequest.mockImplementation(impl as never)
  const root = document.createElement('div')
  document.body.appendChild(root)
  const { mountBillsummaryPage } = await import('../emp-billsummary')
  await mountBillsummaryPage(root, { onNavigate: vi.fn() })
  return root
}

/** `table.render` 的某一次入参（0 = 主表首渲染，之后的看用例自己数）。 */
type RenderOpts = {
  elem?: string
  id?: string
  data?: unknown[]
  cols?: { title?: string; width?: number | string; templet?: (r: never) => string }[][]
  page?: unknown
  toolbar?: string
  totalRow?: boolean
  text?: { none?: string }
}
const renderOpts = (i: number): RenderOpts => LAYUI.render.mock.calls[i]?.[0] as RenderOpts

/** `laypage.render` 的某一次入参。 */
type PageOpts = {
  elem?: string
  count?: number
  limit?: number
  limits?: number[]
  curr?: number
  jump?: (obj: { curr: number; limit: number }, first: boolean) => void
}
const pageOpts = (i: number): PageOpts => LAYUI.laypageRender.mock.calls[i]?.[0] as PageOpts

/** 取数请求清单（method + url + params 的窄视图）。 */
type SeenReq = { method: string; url: string; params: Record<string, ParamValue> | undefined }
const seenReqs = (): SeenReq[] =>
  mockedRequest.mock.calls.map(([c]) => ({
    method: String((c as ReqConfig).method),
    url: String((c as ReqConfig).url),
    params: (c as ReqConfig).params,
  }))

/**
 * 点主表某一行的「查看明细」（真 layui 下这行由 templet 渲染，jsdom 里用**页面自己的
 * templet** 造——`buildDailyCols()[3].templet(dayRow(row))` 正是 layui 会插进 DOM 的那串）。
 */
async function clickViewDetail(root: HTMLElement, r: SettlementDailyItem): Promise<void> {
  const html = buildDailyCols()[3]!.templet!(dayRow(r))
  const host = document.createElement('div')
  host.innerHTML = html
  const link = host.firstElementChild as HTMLElement
  root.appendChild(link)
  link.click()
  await vi.waitFor(() => {
    expect(LAYUI.render.mock.calls.length).toBeGreaterThanOrEqual(2)
  })
}

/**
 * 点某天的「总结算金额」格（D-0323 展开开关）：用页面自己的金额列 templet 造节点并真点击。
 * `expanded` 决定 templet 里的指示方向（页面上由谓词读活状态，这里显式给）。
 */
function clickAmountToggle(root: HTMLElement, r: SettlementDailyItem, expanded = false): void {
  const html = buildDailyCols(() => expanded)[2]!.templet!(dayRow(r))
  const host = document.createElement('div')
  host.innerHTML = html
  const span = host.firstElementChild as HTMLElement
  root.appendChild(span)
  span.click()
}

/** 点来源子行的「查看明细」 / 失败行的「重试」（用页面自己的操作列 templet 造节点）。 */
function clickSourceRow(
  root: HTMLElement,
  r: Extract<DailyRow, { kind: 'source' | 'source-error' }>,
): void {
  const html = buildDailyCols()[3]!.templet!(r)
  const host = document.createElement('div')
  host.innerHTML = html
  const link = host.firstElementChild as HTMLElement
  root.appendChild(link)
  link.click()
}

/** 触发 layui 工具栏事件（真交互版在 interaction spec；这里直接调注册好的回调）。 */
function fireToolbar(event: string, tableId: string): void {
  const handler = LAYUI.tableHandlers.get(`toolbar(${tableId})`)
  expect(handler, `toolbar(${tableId}) 没有注册`).toBeDefined()
  ;(handler as (obj: { event?: string }) => void)({ event })
}

beforeEach(() => {
  mockedRequest.mockReset()
  LAYUI.msg.mockClear()
  LAYUI.open.mockClear()
  LAYUI.reload.mockClear()
  LAYUI.render.mockClear()
  LAYUI.laydateRender.mockClear()
  LAYUI.laypageRender.mockClear()
  LAYUI.tableHandlers.clear()
  vi.mocked(downloadPerformanceExport).mockClear()
  // 屏蔽真实网络：本页只该通过 `request()`（已打桩）取数。
  vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
    throw new Error(`未预期的 fetch：${String(url)}`)
  }))
  // ⭐ jsdom 没有 `URL.createObjectURL` / `revokeObjectURL`（导出要落盘那条路会炸）
  //    ⇒ 与 `emp-emplist.spec.ts` 同一处置打桩；断言「真的走到了落文件这一步」。
  vi.stubGlobal(
    'URL',
    Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  document.body.innerHTML = ''
})

/* ═══════════════════════ 声明：列与渲染参数 ═══════════════════════ */

describe('结算记录（按天聚合）列与渲染参数声明', () => {
  it('主表 4 列，文案序列 = 结账日期 / 总单数 / 总结算金额 / 操作', () => {
    expect(DAILY_COLS.map((c) => c.title)).toEqual(['结账日期', '总单数', '总结算金额', '操作'])
    expect(DAILY_COLS).toHaveLength(4)
  })

  it('主表**全部列都不开 sort**（服务端分页：layui 只排当前页，排了会骗人）', () => {
    expect(DAILY_COLS.map((c) => c.sort ?? false)).toEqual([false, false, false, false])
  })

  it('主表后三列宽 20%、居中；结账日期列不写宽（自动列）', () => {
    expect(DAILY_COLS[0]!.width).toBeUndefined()
    expect(DAILY_COLS.slice(1).map((c) => c.width)).toEqual(['20%', '20%', '20%'])
    expect(DAILY_COLS.map((c) => c.align)).toEqual(['center', 'center', 'center', 'center'])
  })

  it('主表 table 参数：elem #tableList、toolbar #toolbarDemo、defaultToolbar ["filter"]、page:false（服务端分页）', () => {
    expect(DAILY_TABLE.elem).toBe('#tableList')
    expect(DAILY_TABLE.toolbar).toBe('#toolbarDemo')
    expect(DAILY_TABLE.defaultToolbar).toEqual(['filter'])
    expect(DAILY_TABLE.page).toBe(false)
  })

  it('明细 7 列（沿用重设计前右表形态）：文案 / 宽度 / 对齐 / fixed / sort / totalRow 逐项是字面值', () => {
    expect(DETAIL_COLS).toHaveLength(7)
    expect(DETAIL_COLS.map((c) => c.title)).toEqual([
      '结算对象人员', '预计结算金额', '实际结算金额', '关联订单', '项目', '结算时间', '结算人',
    ])
    expect(DETAIL_COLS.map((c) => c.width)).toEqual([
      '12%', '10%', '10%', '15%', undefined, '12%', 100,
    ])
    expect(DETAIL_COLS.map((c) => c.align)).toEqual([
      'center', 'center', 'center', 'center', 'left', 'center', 'center',
    ])
    expect(DETAIL_COLS[0]!.fixed).toBe('left')
    expect(DETAIL_COLS[0]!.totalRowText).toBe('合计')
    expect(DETAIL_COLS.map((c) => c.sort ?? false)).toEqual([
      false, true, true, false, false, true, false,
    ])
    expect(DETAIL_COLS.map((c) => c.totalRow ?? false)).toEqual([
      false, true, true, false, false, false, false,
    ])
  })

  it('明细 table 参数：elem #tableList2、toolbar #toolbarDemo2、defaultToolbar ["filter"]、totalRow:true', () => {
    expect(DETAIL_TABLE.elem).toBe('#tableList2')
    expect(DETAIL_TABLE.toolbar).toBe('#toolbarDemo2')
    expect(DETAIL_TABLE.defaultToolbar).toEqual(['filter'])
    expect(DETAIL_TABLE.totalRow).toBe(true)
  })

  it('主表 / 明细分页档位是两组不同的常量（20/50/100 vs 20/30/40/50/100）', () => {
    expect(DAILY_PAGE_SIZE).toBe(20)
    expect(DAILY_PAGE_LIMITS).toEqual([20, 50, 100])
    expect(DETAIL_PAGE_LIMIT).toBe(20)
    expect(DETAIL_PAGE_LIMITS).toEqual([20, 30, 40, 50, 100])
  })

  it('buildHtml：两张表 / 两个工具栏模板 / 筛选控件 / 分页容器的 id 齐全', () => {
    const html = buildHtml()
    for (const needle of [
      `class="${PANEL_CLASS}"`,
      'id="sDate"', 'id="eDate"', 'id="searchBtn"',
      'id="key1"', 'id="detailSearchBtn"',
      `id="${TABLE_ID}"`, `id="${TABLE2_ID}"`, 'id="dailyPager"',
      'id="toolbarDemo"', 'lay-event="refresh"',
      'id="toolbarDemo2"', 'lay-event="exportBtn"',
      '结账时间', '订单号', '搜索明细',
    ]) {
      expect(html, `buildHtml 缺 ${needle}`).toContain(needle)
    }
  })

  it('陷阱 ①：标记里不许出现 `class=".` 这种选择器形态；类名与选择器是两个常量', () => {
    expect(buildHtml()).not.toContain('class=".')
    expect(PANEL_SELECTOR).toBe(`.${PANEL_CLASS}`)
    expect(VIEW_DETAIL_SELECTOR).toBe(`.${VIEW_DETAIL_CLASS}`)
  })

  it('页面标题常量 = 结算记录（写 document.title 用）', () => {
    expect(TITLE).toBe('结算记录')
  })
})

/* ═══════════════════════ 纯函数 ═══════════════════════ */

describe('筛选与取数参数的纯函数', () => {
  it('`datePart` 只取日期段，形状不认识的输入返回 undefined（不发这个参数）', () => {
    expect(datePart('2026-10-28 00:00:00')).toBe('2026-10-28')
    expect(datePart('  2026-10-28T21:00:00+08:00')).toBe('2026-10-28')
    expect(datePart('2026-10-28')).toBe('2026-10-28')
    expect(datePart('2026-10')).toBeUndefined()
    expect(datePart('')).toBeUndefined()
    expect(datePart('garbage')).toBeUndefined()
  })

  it('`toDailyParams`：两个日期框 → date_from / date_to；坏形状一律不发', () => {
    expect(toDailyParams({ sDate: '2026-07-28 00:00:00', eDate: '2026-10-28 23:59:59' })).toEqual({
      date_from: '2026-07-28',
      date_to: '2026-10-28',
    })
    expect(toDailyParams({ sDate: '半截', eDate: '' })).toEqual({})
  })

  it('`toDailyParams` 不带分页键（分页由 laypage 管，混进来会两处改同一个键）', () => {
    const out = toDailyParams({ sDate: '2026-10-01', eDate: '2026-10-28' }) as Record<string, unknown>
    expect(out.page).toBeUndefined()
    expect(out.page_size).toBeUndefined()
  })

  it('`toDetailParams`：必带 settle_status=已结账 + 当天两端；订单号 trim 非空才带；不带来源', () => {
    expect(toDetailParams('2026-10-28', '')).toEqual({
      settle_status: '已结账', date_from: '2026-10-28', date_to: '2026-10-28',
    })
    expect(toDetailParams('2026-10-28', '  20260920213215233029  ')).toEqual({
      settle_status: '已结账', date_from: '2026-10-28', date_to: '2026-10-28',
      order_no: '20260920213215233029',
    })
    expect('user' in toDetailParams('2026-10-28', '')).toBe(false)
    expect('game' in toDetailParams('2026-10-28', '')).toBe(false)
    expect('order_source' in toDetailParams('2026-10-28', '')).toBe(false)
  })

  it('`toDetailParams` 第三参（D-0323）：来源 id ⇒ order_source=数字；`\'none\'` ⇒ 字面量原样', () => {
    expect(toDetailParams('2026-10-28', '', 5)).toMatchObject({ order_source: 5 })
    expect(toDetailParams('2026-10-28', '', 'none')).toMatchObject({ order_source: 'none' })
    // ⚠️ `'none'` 不能被 Number() 吃成 NaN——字面量是后端两态参数的一档。
    expect(toDetailParams('2026-10-28', '', 'none').order_source).toBe('none')
    // 来源 + 订单号并存（来源视图里搜单）。
    expect(toDetailParams('2026-10-28', 'X1', 5)).toMatchObject({ order_source: 5, order_no: 'X1' })
  })

  it('`sourceDisplayName`：null ⇒ 未指定；有名 ⇒ 原样（⛔ 不把 null 渲染成空串）', () => {
    expect(sourceDisplayName(sourceItem({ source_name: '甲店铺' }))).toBe('甲店铺')
    expect(sourceDisplayName(sourceItem({ source_name: null, source_id: null }))).toBe(UNSPECIFIED_SOURCE_LABEL)
  })

  it('`sourceParamValue`：null ⇒ \'none\'（后端「未指定」档）；有 id ⇒ 数字', () => {
    expect(sourceParamValue(sourceItem({ source_id: 5 }))).toBe(5)
    expect(sourceParamValue(sourceItem({ source_id: null }))).toBe('none')
  })

  it('`DATE_SHAPE`：`YYYY-MM-DD` 通过；带时间 / 半截 / 乱码都不通过（开明细前的形状闸）', () => {
    expect(DATE_SHAPE.test('2026-10-28')).toBe(true)
    expect(DATE_SHAPE.test('2026-10-28 21:00:00')).toBe(false)
    expect(DATE_SHAPE.test('2026/10/28')).toBe(false)
    expect(DATE_SHAPE.test('garbage')).toBe(false)
    expect(DATE_SHAPE.test('')).toBe(false)
  })

  it('`defaultDateRange` = 今天 − 3 个自然月 00:00:00 ~ 今天 23:59:59（字面值）', () => {
    const now = new Date('2026-10-28T12:00:00+08:00')
    expect(defaultDateRange(now)).toEqual({
      sDate: '2026-07-28 00:00:00',
      eDate: '2026-10-28 23:59:59',
    })
  })

  it('`formatSettledAt`：ISO-8601 → `YYYY-MM-DD HH:mm:ss`（取后端渲染串的墙钟时间，字面值）', () => {
    expect(formatSettledAt('2026-10-28T21:00:00.123456+08:00')).toBe('2026-10-28 21:00:00')
    expect(formatSettledAt('2026-10-28 21:00:00')).toBe('2026-10-28 21:00:00')
  })

  it('`formatSettledAt` 空态：null / undefined / 空串 / 坏形状 ⇒ 空串（不抛、不出 undefined）', () => {
    expect(formatSettledAt(null)).toBe('')
    expect(formatSettledAt(undefined)).toBe('')
    expect(formatSettledAt('')).toBe('')
    expect(formatSettledAt('not a date')).toBe('')
  })
})

describe('「实际结算金额」= 已结账快照 settled_amount，其余回落 payable_net', () => {
  const pick = (settled: string | null, payable: string) =>
    settledOrPayable({ settled_amount: settled, payable_net: payable })

  it('T1 已结账行（快照 70.00、活值 100.00）⇒ 取快照 70.00，不是 100.00', () => {
    expect(pick('70.00', '100.00')).toBe('70.00')
  })

  it('T2 未结账行（settled_amount=null）⇒ 回落 payable_net 100.00', () => {
    expect(pick(null, '100.00')).toBe('100.00')
  })

  it('T3 存量已结账行（快照为 null）⇒ 回落 payable_net；缺字段（部署窗口）同样回落', () => {
    expect(pick(null, '30.00')).toBe('30.00')
    expect(settledOrPayable({ payable_net: '30.00' } as never)).toBe('30.00')
  })

  it('T5 边界 `settled_amount="0.00"` ⇒ 原样取出（防「先 parse 成数字再判真值」那类写法）', () => {
    expect(pick('0.00', '100.00')).toBe('0.00')
  })

  it('T6 防御性分支：`settled_amount=""` 空串 ⇒ `??` 原样返回空串（后端 DecimalField 不会出空串）', () => {
    expect(pick('', '100.00')).toBe('')
  })

  it('负数快照原样取出（D-0015：负金额合法）', () => {
    expect(pick('-15.00', '3.00')).toBe('-15.00')
  })
})

/* ═══════════════════════ 展开行派生（buildDisplayRows，D-0323） ═══════════════════════ */

describe('buildDisplayRows：天行 + 展开态 + 分解状态 → 渲染行数组', () => {
  const days = [day({ date: '2026-10-28' }), day({ date: '2026-10-27', order_count: 1 })]
  const okState: SourcesState = { status: 'ok', items: SOURCES }

  it('全部收起 ⇒ 只有天行（缓存里有数据也不渲染——展开是唯一开关）', () => {
    const sources = new Map([['2026-10-28', okState]])
    expect(buildDisplayRows(days, new Set(), sources)).toEqual(days.map(dayRow))
  })

  it('展开某天 ⇒ 该天行下方依序插来源子行（后端序，前端不重排）', () => {
    const rows = buildDisplayRows(days, new Set(['2026-10-28']), new Map([['2026-10-28', okState]]))
    expect(rows).toEqual([
      dayRow(days[0]!),
      { kind: 'source', date: '2026-10-28', item: SOURCES[0]! },
      { kind: 'source', date: '2026-10-28', item: SOURCES[1]! },
      dayRow(days[1]!),
    ])
  })

  it('loading / error 状态 ⇒ 一条状态行；error 只报状态（message 由 toast 出，不进表）', () => {
    const loading = buildDisplayRows(days, new Set(['2026-10-28']), new Map([
      ['2026-10-28', { status: 'loading' }],
    ]))
    expect(loading).toEqual([dayRow(days[0]!), { kind: 'source-loading', date: '2026-10-28' }, dayRow(days[1]!)])
    const error = buildDisplayRows(days, new Set(['2026-10-28']), new Map([
      ['2026-10-28', { status: 'error', message: '超时' }],
    ]))
    expect(error).toEqual([dayRow(days[0]!), { kind: 'source-error', date: '2026-10-28' }, dayRow(days[1]!)])
  })

  it('`ok` 且 items 为空（防御形态）⇒ 0 条子行（不插入状态行冒充）', () => {
    const rows = buildDisplayRows(days, new Set(['2026-10-27']), new Map([
      ['2026-10-27', { status: 'ok', items: [] }],
    ]))
    expect(rows).toEqual(days.map(dayRow))
  })
})

/* ═══════════════════════ 单元格渲染（函数 templet） ═══════════════════════ */

describe('主表单元格渲染（buildDailyCols，D-0323 起 templet 按 kind 分派）', () => {
  it('天行三格：日期原样、单数成串、金额 = 红字 + 展开开关（class/data-date/指示符）', () => {
    const cols = buildDailyCols()
    expect(cols[0]!.templet!(dayRow(day({ date: '2026-10-28' })))).toBe('<span>2026-10-28</span>')
    expect(cols[1]!.templet!(dayRow(day({ order_count: 3 })))).toBe('<span>3</span>')
    const amountHtml = cols[2]!.templet!(dayRow(day({ total_amount: '-15.00', date: '2026-10-27' })))
    expect(amountHtml).toContain(`class="${AMOUNT_TOGGLE_CLASS}"`)
    expect(amountHtml).toContain('data-date="2026-10-27"')
    expect(amountHtml).toContain('>-15.00<')
    expect(amountHtml).toContain('▼')
    expect(amountHtml).toContain('cursor:pointer')
    expect(amountHtml).toContain('title="点击展开 / 收起按订单来源分解"')
  })

  it('天行金额格：展开态指示符翻成 ▲（谓词由挂载处传活状态）', () => {
    const html = buildDailyCols((d) => d === '2026-10-28')[2]!.templet!(
      dayRow(day({ date: '2026-10-28', total_amount: '20.00' })),
    )
    expect(html).toContain('▲')
    expect(html).not.toContain('▼')
  })

  it('天行操作列：`查看明细` 链接的 class / data-date / 颜色 / title 逐字（一字不动）', () => {
    const html = buildDailyCols()[3]!.templet!(dayRow(day({ date: '2026-10-27' })))
    expect(html).toContain(`class="${VIEW_DETAIL_CLASS}"`)
    expect(html).toContain('data-date="2026-10-27"')
    expect(html).toContain('style="color:#1E9FFF"')
    expect(html).toContain('title="查看这一天的结算明细"')
    expect(html).toContain('>查看明细</a>')
  })

  it('来源子行：名字列 = 缩进 + 来源名（null ⇒ 未指定）；笔数 / 金额（红字）各归其列', () => {
    const cols = buildDailyCols()
    const named: DailyRow = { kind: 'source', date: '2026-10-28', item: sourceItem({ source_name: '甲店铺', order_count: 2, total_amount: '15.00' }) }
    const unnamed: DailyRow = { kind: 'source', date: '2026-10-28', item: sourceItem({ source_id: null, source_name: null }) }
    expect(cols[0]!.templet!(named)).toBe('<span style="padding-left:24px">甲店铺</span>')
    expect(cols[0]!.templet!(unnamed)).toBe(`<span style="padding-left:24px">${UNSPECIFIED_SOURCE_LABEL}</span>`)
    expect(cols[1]!.templet!(named)).toBe('<span>2</span>')
    expect(cols[2]!.templet!(named)).toBe('<span style="color:#FF5722">15.00</span>')
  })

  it('来源子行操作列：`查看明细` 带 data-source（id / none）与 data-label（标题上下文用）', () => {
    const named = buildDailyCols()[3]!.templet!({
      kind: 'source', date: '2026-10-27', item: sourceItem({ source_id: 5, source_name: '甲店铺' }),
    })
    expect(named).toContain(`class="${SOURCE_VIEW_CLASS}"`)
    expect(named).toContain('data-date="2026-10-27"')
    expect(named).toContain('data-source="5"')
    expect(named).toContain('data-label="甲店铺"')
    const unnamed = buildDailyCols()[3]!.templet!({
      kind: 'source', date: '2026-10-27', item: sourceItem({ source_id: null, source_name: null }),
    })
    expect(unnamed).toContain("data-source=\"none\"")
    expect(unnamed).toContain(`data-label="${UNSPECIFIED_SOURCE_LABEL}"`)
  })

  it('loading / error 状态行：文案在名字列，操作列 error = 「重试」链接 / loading = 空', () => {
    const cols = buildDailyCols()
    const loading: DailyRow = { kind: 'source-loading', date: '2026-10-28' }
    const error: DailyRow = { kind: 'source-error', date: '2026-10-28' }
    expect(cols[0]!.templet!(loading)).toContain('正在加载来源分解…')
    expect(cols[0]!.templet!(error)).toContain('来源分解加载失败')
    expect(cols[1]!.templet!(loading)).toBe('<span></span>')
    expect(cols[2]!.templet!(error)).toBe('<span></span>')
    const retryHtml = cols[3]!.templet!(error)
    expect(retryHtml).toContain(`class="${SOURCE_RETRY_CLASS}"`)
    expect(retryHtml).toContain('data-date="2026-10-28"')
    expect(retryHtml).toContain('>重试</a>')
    expect(cols[3]!.templet!(loading)).toBe('<span></span>')
  })

  it('日期里的特殊字符被转义（templet 返回值被 layui 当 HTML 插入）', () => {
    const html = buildDailyCols()[0]!.templet!(dayRow(day({ date: '"><script>' })))
    expect(html).not.toContain('<script>')
  })
})

describe('明细单元格渲染（buildDetailCols，与重设计前逐列同形）', () => {
  it('人员列取 `display_name`（昵称优先），缺席回落 `username`；两档都转义', () => {
    const t = buildDetailCols()[0]!.templet!
    expect(t(row({ id: 1, user: 7, display_name: '合成<b>小李', username: 'raw-a' }))).not.toContain('<b>')
    // 缺席回落：不带 display_name、但 username 有值（fixture 默认 username 是 null，须显式给）。
    expect(t(row({ id: 2, user: 7, username: 'raw-a' }))).toContain('raw-a')
  })

  it('预计列 = temp_amount 红字；实际列走 settledOrPayable（快照优先）红字', () => {
    const cols = buildDetailCols()
    expect(cols[1]!.templet!(row({ id: 1, user: 7, temp_amount: '12.00' }))).toBe(
      '<span style="color:#FF5722">12.00</span>',
    )
    expect(cols[2]!.templet!(row({ id: 2, user: 7, settled_amount: '70.00', payable_net: '100.00' }))).toContain('70.00')
    expect(cols[2]!.templet!(row({ id: 3, user: 7, settled_amount: null, payable_net: '100.00' }))).toContain('100.00')
  })

  it('关联订单：正文 = 来源单号、`data-id` = 系统单号（两个不同字段，2026-10-09 改准）', () => {
    const html = buildDetailCols()[3]!.templet!(
      row({ id: 1, user: 7, order_no: '20260920213215233029', source_order_no: 'TB-合成-结算-来源甲' }),
    )
    expect(html).toContain('>TB-合成-结算-来源甲</a>')
    expect(html).toContain('data-id="20260920213215233029"')
    expect(html).toContain('data-orderIdView="TB-合成-结算-来源甲"')
  })

  it('D-0301：来源单号为空 ⇒ 正文回退内置单号（不渲染空链接）', () => {
    const html = buildDetailCols()[3]!.templet!(
      row({ id: 1, user: 7, order_no: '20260920213215233029', source_order_no: '' }),
    )
    expect(html).toContain('>20260920213215233029</a>')
  })

  it('项目 / 结算时间 / 结算人三列：项目原样、时间格式化、结算人原样', () => {
    const cols = buildDetailCols()
    expect(cols[4]!.templet!(row({ id: 1, user: 7, project: '深渊满星' }))).toBe('<span>深渊满星</span>')
    expect(cols[5]!.templet!(row({ id: 2, user: 7, settled_at: '2026-10-28T21:00:00+08:00' }))).toBe(
      '<span>2026-10-28 21:00:00</span>',
    )
    expect(cols[6]!.templet!(row({ id: 3, user: 7, settled_by: '财务甲' }))).toBe('<span>财务甲</span>')
  })

  it('未结账形状（settled_at=null / settled_by 空串）⇒ 时间与结算人渲染空，且不出 undefined', () => {
    const cols = buildDetailCols()
    const r = row({ id: 1, user: 7, settled_at: null, settled_by: '', settled_amount: null })
    expect(cols[5]!.templet!(r)).toBe('<span></span>')
    expect(cols[6]!.templet!(r)).toBe('<span></span>')
  })

  it('载荷缺 `project` / `payable_net` / `source_order_no`（部署窗口）⇒ 空单元格，不抛错', () => {
    const partial = { id: 1, user: 7, order_no: '20260920213215233029', settled_amount: null } as SettlementListItem
    const cols = buildDetailCols()
    expect(() => cols[4]!.templet!(partial)).not.toThrow()
    expect(cols[4]!.templet!(partial)).toBe('<span></span>')
    expect(() => cols[3]!.templet!(partial)).not.toThrow()
  })

  it('订单号 / 结算人里的特殊字符被转义（来自数据库的值不当 HTML）', () => {
    const orderHtml = buildDetailCols()[3]!.templet!(
      row({ id: 1, user: 7, order_no: '"><img src=x>', source_order_no: '' }),
    )
    expect(orderHtml).not.toContain('"><img')
    const byHtml = buildDetailCols()[6]!.templet!(row({ id: 2, user: 7, settled_by: '<b>甲' }))
    expect(byHtml).not.toContain('<b>')
  })
})

/* ═══════════════════════ 挂载：主表（按天列表） ═══════════════════════ */

describe('主表取数：GET /api/business/settlements/daily-summary + 服务端分页', () => {
  it('挂载即发 GET（URL 字面值），参数含 3 个月默认日期 + page=1 + page_size=20', async () => {
    await mountPage()
    const reqs = seenReqs()
    expect(reqs[0]).toEqual({
      method: 'get',
      url: '/api/business/settlements/daily-summary',
      params: { date_from: expect.any(String), date_to: expect.any(String), page: 1, page_size: 20 },
    })
  })

  it('挂载渲染主表一次（elem/id/cols/text.none），data = 天行包装（kind:day + 原字段）', async () => {
    await mountPage()
    const opts = renderOpts(0)
    expect(opts.elem).toBe('#tableList')
    expect(opts.id).toBe(TABLE_ID)
    expect(opts.text).toEqual({ none: '暂无数据' })
    expect(opts.data).toEqual(DAYS.map(dayRow))
    expect(opts.cols![0]!.map((c) => c.title)).toEqual(['结账日期', '总单数', '总结算金额', '操作'])
  })

  it('laypage：count = 服务端 total、limit/limits/curr 对上；初始渲染（first=true）**不重取**', async () => {
    await mountPage()
    const opts = pageOpts(0)
    expect(opts.elem).toBe('#dailyPager')
    expect(opts.count).toBe(3)
    expect(opts.limit).toBe(20)
    expect(opts.limits).toEqual([20, 50, 100])
    expect(opts.curr).toBe(1)
    const before = mockedRequest.mock.calls.length
    opts.jump!({ curr: 1, limit: 20 }, true)
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('jump(第 2 页) ⇒ 再发一次请求（page=2）并 `table.reload`（render 不再来一次）', async () => {
    await mountPage()
    pageOpts(0).jump!({ curr: 2, limit: 50 }, false)
    await vi.waitFor(() => {
      expect(mockedRequest.mock.calls.length).toBe(2)
    })
    expect(seenReqs()[1]!.params).toMatchObject({ page: 2, page_size: 50 })
    await vi.waitFor(() =>
      expect(LAYUI.reload).toHaveBeenCalledWith(TABLE_ID, expect.objectContaining({ data: DAYS.map(dayRow) })),
    )
    expect(LAYUI.render.mock.calls.length).toBe(1)
    // 翻页后的 laypage curr 跟到第 2 页。
    expect(pageOpts(1).curr).toBe(2)
    expect(pageOpts(1).limit).toBe(50)
  })

  it('403（无 finance.payroll）→ 后端 msg **原样**提示 6000ms，且表被清空 + laypage count=0（不是静默空表）', async () => {
    await mountPage(failingImpl(new Error('你没有查看结算汇总的权限')))
    expect(LAYUI.msg).toHaveBeenCalledWith('你没有查看结算汇总的权限', { time: 6000 })
    expect(renderOpts(0).data).toEqual([])
    expect(pageOpts(0).count).toBe(0)
  })

  it('工具栏「刷新数据」：用**当前页**重取（先翻到第 2 页再刷新 ⇒ page 仍是 2）', async () => {
    await mountPage()
    pageOpts(0).jump!({ curr: 2, limit: 50 }, false)
    // laypage 被重渲染（curr=2）说明第 2 页那次 loadDaily 已收口（currentPage 已更新）。
    await vi.waitFor(() => expect(pageOpts(1).curr).toBe(2))
    fireToolbar('refresh', TABLE_ID)
    await vi.waitFor(() => expect(mockedRequest.mock.calls.length).toBe(3))
    expect(seenReqs()[2]!.params).toMatchObject({ page: 2, page_size: 50 })
  })

  it('`LAYTABLE_COLS`（右上角内置「筛选列」图标）静默放行——那是我们自己开了的功能', async () => {
    await mountPage()
    const before = mockedRequest.mock.calls.length
    fireToolbar('LAYTABLE_COLS', TABLE_ID)
    expect(mockedRequest.mock.calls.length).toBe(before)
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })

  it('「搜索」按钮：带新日期回第 1 页重取（真交互版在 interaction spec）', async () => {
    const root = await mountPage()
    ;(root.querySelector<HTMLInputElement>('#sDate')!).value = '2026-10-01 00:00:00'
    ;(root.querySelector<HTMLInputElement>('#eDate')!).value = '2026-10-02 23:59:59'
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    await vi.waitFor(() => expect(mockedRequest.mock.calls.length).toBe(2))
    expect(seenReqs()[1]!.params).toMatchObject({
      date_from: '2026-10-01', date_to: '2026-10-02', page: 1,
    })
  })

  it('两个日期框挂载时被写入「今天 − 3 个月 ~ 今天」并交给 laydate（type:datetime）×2', async () => {
    const root = await mountPage()
    const s = root.querySelector<HTMLInputElement>('#sDate')!
    expect(s.value).toBe(defaultDateRange().sDate)
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe(defaultDateRange().eDate)
    expect(LAYUI.laydateRender).toHaveBeenCalledTimes(2)
    expect(LAYUI.laydateRender).toHaveBeenCalledWith({ elem: '#sDate', type: 'datetime' })
    expect(LAYUI.laydateRender).toHaveBeenCalledWith({ elem: '#eDate', type: 'datetime' })
  })

  it('挂载写 document.title = 结算记录', async () => {
    await mountPage()
    expect(document.title).toBe('结算记录')
  })
})

/* ═══════════════════════ 挂载：金额列展开按来源分解（D-0323） ═══════════════════════ */

/** 主表 `table.reload` 的最近一次 data（展开 / 收起 / 翻页都走这条路）。 */
function lastDailyReloadData(): unknown[] {
  const calls = LAYUI.reload.mock.calls.filter(([id]) => id === TABLE_ID)
  expect(calls.length, '主表还没被 reload 过').toBeGreaterThan(0)
  return (calls[calls.length - 1]![1] as { data?: unknown[] }).data ?? []
}

const sourcesReqs = () =>
  seenReqs().filter((r) => r.url === '/api/business/settlements/daily-summary/sources')

describe('金额列展开：lazy 取数 + 子行渲染（D-0323）', () => {
  it('首次展开 ⇒ GET …/daily-summary/sources（params 恰好 {date}），主表 reload 出来源子行', async () => {
    const root = await mountPage()
    expect(sourcesReqs()).toHaveLength(0) // lazy：不点不拉
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    expect(sourcesReqs()[0]!.params).toEqual({ date: '2026-10-27' })
    await vi.waitFor(() => {
      expect(lastDailyReloadData()).toEqual([
        dayRow(DAYS[0]!),
        dayRow(DAYS[1]!),
        { kind: 'source', date: '2026-10-27', item: SOURCES[0]! },
        { kind: 'source', date: '2026-10-27', item: SOURCES[1]! },
        dayRow(DAYS[2]!),
      ])
    })
    // 主表没有再 render（同一实例 reload），render 计数仍是首屏那 1 次。
    expect(LAYUI.render.mock.calls.length).toBe(1)
  })

  it('收起 ⇒ 子行撤掉；再展开 ⇒ **用缓存**（sources 请求不重发）', async () => {
    const root = await mountPage()
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    clickAmountToggle(root, day({ date: '2026-10-27' })) // 收起
    await vi.waitFor(() => expect(lastDailyReloadData()).toEqual(DAYS.map(dayRow)))
    clickAmountToggle(root, day({ date: '2026-10-27' })) // 再展开（缓存）
    await vi.waitFor(() =>
      expect(lastDailyReloadData().filter((r) => (r as DailyRow).kind === 'source')).toHaveLength(2),
    )
    expect(sourcesReqs()).toHaveLength(1)
  })

  it('取数失败 ⇒ 原样 toast（6s）+ 错误状态行；点「重试」⇒ 重新拉取并可成功', async () => {
    let failSources = true
    const impl: ReqImpl = async (config) => {
      const url = String(config.url)
      if (url.includes('/settlements/daily-summary/sources')) {
        if (failSources) throw new Error('来源分解拉取失败')
        return { items: SOURCES }
      }
      if (url.includes('/settlements/daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      return { items: ROWS, total: ROWS.length, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('来源分解拉取失败', { time: 6000 })
    await vi.waitFor(() =>
      expect(lastDailyReloadData()).toContainEqual({ kind: 'source-error', date: '2026-10-27' }),
    )
    failSources = false
    clickSourceRow(root, { kind: 'source-error', date: '2026-10-27' }) // 「重试」链接
    await vi.waitFor(() => {
      expect(sourcesReqs()).toHaveLength(2)
      expect(lastDailyReloadData().filter((r) => (r as DailyRow).kind === 'source')).toHaveLength(2)
    })
  })

  it('取数中 ⇒ loading 状态行（不发 toast、不等结果就有反馈）', async () => {
    let release: (() => void) | undefined
    const impl: ReqImpl = async (config) => {
      const url = String(config.url)
      if (url.includes('/settlements/daily-summary/sources')) {
        await new Promise<void>((resolve) => { release = resolve })
        return { items: SOURCES }
      }
      if (url.includes('/settlements/daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      return { items: ROWS, total: ROWS.length, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() =>
      expect(lastDailyReloadData()).toContainEqual({ kind: 'source-loading', date: '2026-10-27' }),
    )
    expect(LAYUI.msg).not.toHaveBeenCalled()
    release!()
    await vi.waitFor(() =>
      expect(lastDailyReloadData().filter((r) => (r as DailyRow).kind === 'source')).toHaveLength(2),
    )
  })

  it('换页 ⇒ 全部收起 + 缓存作废（同一日期再展开要重拉）', async () => {
    const root = await mountPage()
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    pageOpts(0).jump!({ curr: 2, limit: 20 }, false)
    // 等第 2 页那次 loadDaily **收口**（reload 落表），不是等请求发出。
    await vi.waitFor(() =>
      expect(lastDailyReloadData().every((r) => (r as DailyRow).kind === 'day')).toBe(true),
    )
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(2)) // 缓存已作废
  })

  it('「搜索」（改筛选）⇒ 同样收起 + 作废缓存', async () => {
    const root = await mountPage()
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    await vi.waitFor(() =>
      expect(lastDailyReloadData().every((r) => (r as DailyRow).kind === 'day')).toBe(true),
    )
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(2))
  })

  it('迟到响应（straggler）不写新代际的缓存：放行后再展开必须重拉、渲染的是新载荷（评审 Minor-2）', async () => {
    // 上面「换页 / 搜索作废」两条都是在 loadDaily **收口之后**才动作，从不让 straggler
    // 真正落地 ⇒ 代际守卫（loadSources 里 `generation !== dailyGeneration` 即丢）没有
    // 会红的用例。本条构造：挂住第 1 代的 sources 响应 → 翻页（代际 +1、
    // 缓存清空、loadDaily 收口）→ **这时才放行** straggler → 新代际里再展开同一天。
    // 判别力（摘守卫变异已证红）：摘掉那两行守卫 ⇒ straggler 会把
    // 「第 1 代的旧载荷」写进**已清空**的缓存 ⇒ 再展开命中陈旧缓存、**不重发**请求
    // ⇒ 下面两条断言（重发 + 新载荷）都红——「防御性写法要么有会红的用例、要么别当承重」。
    let releaseStraggler: (() => void) | undefined
    let sourcesCall = 0
    const stale: SettlementSourceItem[] = SOURCES
    const fresh: SettlementSourceItem[] = [
      sourceItem({ source_id: 9, source_name: '乙店铺', order_count: 1, total_amount: '99.00' }),
    ]
    const impl: ReqImpl = async (config) => {
      const url = String(config.url)
      if (url.includes('/settlements/daily-summary/sources')) {
        sourcesCall += 1
        if (sourcesCall === 1) {
          await new Promise<void>((resolve) => { releaseStraggler = resolve })
          return { items: stale }
        }
        return { items: fresh }
      }
      if (url.includes('/settlements/daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      return { items: ROWS, total: ROWS.length, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    // ① 第 1 代：展开 2026-10-27 ⇒ sources 请求挂住（loading 行可见 = 已进 loadSources）
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    await vi.waitFor(() =>
      expect(lastDailyReloadData()).toContainEqual({ kind: 'source-loading', date: '2026-10-27' }),
    )
    // ② 翻页：loadDaily 收口（只剩天行 = 展开态与缓存已被清）
    pageOpts(0).jump!({ curr: 2, limit: 20 }, false)
    await vi.waitFor(() =>
      expect(lastDailyReloadData().every((r) => (r as DailyRow).kind === 'day')).toBe(true),
    )
    // ③ 放行 straggler，再等一拍宏任务 ⇒ 它的续体（含守卫判断）必然已跑完
    releaseStraggler!()
    await new Promise((resolve) => setTimeout(resolve, 0))
    // ④ 新代际里再展开同一天：守卫活着 ⇒ straggler 没把旧载荷写成缓存 ⇒ 必须重发
    clickAmountToggle(root, day({ date: '2026-10-27' }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(2))
    expect(sourcesReqs()[1]!.params).toEqual({ date: '2026-10-27' })
    // ⑤ 渲染的是**新拉**的载荷（乙店铺 99.00），不是 straggler 带回的旧载荷（甲店铺 15.00）
    await vi.waitFor(() => {
      expect(lastDailyReloadData()).toContainEqual({
        kind: 'source',
        date: '2026-10-27',
        item: fresh[0]!,
      })
    })
    expect(lastDailyReloadData()).not.toContainEqual({
      kind: 'source',
      date: '2026-10-27',
      item: stale[0]!,
    })
  })

  it('金额格 `data-date` 坏形状 ⇒ 零动作零请求（形状闸）', async () => {
    const root = await mountPage()
    clickAmountToggle(root, day({ date: 'garbage' }))
    clickAmountToggle(root, day({ date: '' }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(sourcesReqs()).toHaveLength(0)
    expect(seenReqs()).toHaveLength(1) // 只有首屏那次 daily-summary
  })
})

/* ═══════════════════════ 挂载：来源行「查看明细」（D-0323） ═══════════════════════ */

describe('来源行「查看明细」：当天 + 该来源（复用明细卡）', () => {
  async function expandThenClickSource(root: HTMLElement, date: string, item: SettlementSourceItem): Promise<void> {
    clickAmountToggle(root, day({ date }))
    await vi.waitFor(() => expect(sourcesReqs()).toHaveLength(1))
    // 等子行真的渲染出来再点（reload 收口之后才有可点的链接）。
    await vi.waitFor(() =>
      expect(lastDailyReloadData()).toContainEqual(
        expect.objectContaining({ kind: 'source', date, item }),
      ),
    )
    clickSourceRow(root, { kind: 'source', date, item })
    // 等明细**取数收口**（标题写入 = currentDetailDay/Source 已认值，后续断言不抢跑）。
    await vi.waitFor(() => {
      expect(root.querySelector('.detailTitle')!.textContent).toContain(date)
    })
  }

  it('指定来源行 ⇒ /settlements 参数 = settle_status + 当天两端 + order_source=<id>（无 order_no）', async () => {
    const root = await mountPage()
    await expandThenClickSource(root, '2026-10-27', SOURCES[0]!)
    const reqs = seenReqs().filter((r) => r.url === '/api/business/settlements')
    expect(reqs).toHaveLength(1)
    expect(reqs[0]!.params).toEqual({
      settle_status: '已结账', date_from: '2026-10-27', date_to: '2026-10-27',
      order_source: 5, page: 1, page_size: 200,
    })
    expect(root.querySelector('.detailTitle')!.textContent).toBe('2026-10-27的结算明细（来源：甲店铺）')
  })

  it('未指定行 ⇒ order_source=\'none\'，标题来源 = 未指定', async () => {
    const root = await mountPage()
    await expandThenClickSource(root, '2026-10-27', SOURCES[1]!)
    const reqs = seenReqs().filter((r) => r.url === '/api/business/settlements')
    expect(reqs[0]!.params).toMatchObject({ order_source: 'none' })
    expect(root.querySelector('.detailTitle')!.textContent).toBe(
      `2026-10-27的结算明细（来源：${UNSPECIFIED_SOURCE_LABEL}）`,
    )
  })

  it('来源视图里搜订单号 ⇒ **保持**来源上下文（order_source 仍带）', async () => {
    const root = await mountPage()
    await expandThenClickSource(root, '2026-10-27', SOURCES[0]!)
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '  20260920213215233029  '
    root.querySelector<HTMLElement>('#detailSearchBtn')!.click()
    await vi.waitFor(() => {
      expect(seenReqs().filter((r) => r.url === '/api/business/settlements').length).toBe(2)
    })
    expect(seenReqs().filter((r) => r.url === '/api/business/settlements')[1]!.params).toMatchObject({
      order_source: 5, order_no: '20260920213215233029',
    })
  })

  it('来源明细之后再点**天级**「查看明细」⇒ 回到无来源路径（不带 order_source、标题无来源）', async () => {
    const root = await mountPage()
    await expandThenClickSource(root, '2026-10-27', SOURCES[0]!)
    clickViewDetail(root, day({ date: '2026-10-26' }))
    // ⚠️ clickViewDetail 只等 render 计数（来源明细那次已满足）⇒ 这里自己等标题收口。
    await vi.waitFor(() => {
      expect(root.querySelector('.detailTitle')!.textContent).toBe('2026-10-26的结算明细')
    })
    const reqs = seenReqs().filter((r) => r.url === '/api/business/settlements')
    expect(reqs[reqs.length - 1]!.params).not.toHaveProperty('order_source')
  })

  it('「搜索」还原后来源上下文一并失效（明细搜索回到「请先选天」）', async () => {
    const root = await mountPage()
    await expandThenClickSource(root, '2026-10-27', SOURCES[0]!)
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    await vi.waitFor(() => expect(root.querySelector('.detailTitle')!.textContent).toBe(''))
    root.querySelector<HTMLElement>('#detailSearchBtn')!.click()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('请先在上方列表点击「查看明细」选择日期', { time: 1500 })
  })
})

/* ═══════════════════════ 挂载：明细（点「查看明细」展开当天） ═══════════════════════ */

describe('明细：点「查看明细」→ 当天明细（复用 /settlements 取全）', () => {
  it('点行内链接 ⇒ GET /api/business/settlements，参数 = settle_status=已结账 + 当天两端；**不带** user/game/order_no', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-27' }))
    const reqs = seenReqs().filter((r) => r.url === '/api/business/settlements')
    expect(reqs).toHaveLength(1)
    expect(reqs[0]!.params).toEqual({
      settle_status: '已结账', date_from: '2026-10-27', date_to: '2026-10-27',
      page: 1, page_size: 200,
    })
  })

  it('明细渲染：data = 取全后的行（跨页也取齐）、page={limit:20,limits:[…]}、totalRow', async () => {
    // 第一页只回 2 行、total=3 ⇒ fetchAllSettlements 必须再取第 2 页（缺行 = 明细不完整）。
    const impl: ReqImpl = async (config) => {
      const url = String(config.url)
      if (url.includes('daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      const page = Number(config.params?.['page'] ?? 1)
      return page === 1
        ? { items: ROWS.slice(0, 2), total: 3, page: 1, page_size: 200 }
        : { items: ROWS.slice(2), total: 3, page: 2, page_size: 200 }
    }
    const root = await mountPage(impl)
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    const opts = renderOpts(1)
    expect(opts.elem).toBe('#tableList2')
    expect(opts.data).toEqual(ROWS)
    expect(opts.page).toEqual({ limit: 20, limits: [20, 30, 40, 50, 100] })
    expect(opts.totalRow).toBe(true)
    expect(opts.text).toEqual({ none: '暂无数据' })
    expect(opts.cols![0]!.map((c) => c.title)).toEqual([
      '结算对象人员', '预计结算金额', '实际结算金额', '关联订单', '项目', '结算时间', '结算人',
    ])
  })

  it('明细卡标题 = `${date}的结算明细`（textContent，非 innerHTML）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-27' }))
    expect(root.querySelector('.detailTitle')!.textContent).toBe('2026-10-27的结算明细')
  })

  it('点第二天：换数据 `table.reload`，不再 `table.render`（layui 实例只有一个）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    // 第二次点击：render 计数已满足 clickViewDetail 的等待 ⇒ 自己等 reload。
    const link = document.createElement('a')
    link.className = VIEW_DETAIL_CLASS
    link.dataset.date = '2026-10-26'
    root.appendChild(link)
    link.click()
    expect(LAYUI.render.mock.calls.length).toBe(2)
    await vi.waitFor(() =>
      expect(LAYUI.reload).toHaveBeenCalledWith(
        TABLE2_ID,
        expect.objectContaining({ data: ROWS, page: { curr: 1, limit: 20 } }),
      ),
    )
  })

  it('`data-date` 非日期形状 ⇒ 提示且**零请求**（形状闸）', async () => {
    const root = await mountPage()
    const link = document.createElement('a')
    link.className = VIEW_DETAIL_CLASS
    link.dataset.date = 'garbage'
    root.appendChild(link)
    link.click()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('日期格式不正确，无法查看结算明细', { time: 6000 })
    expect(seenReqs().filter((r) => r.url === '/api/business/settlements')).toHaveLength(0)
  })

  it('明细请求失败 ⇒ 原样报错（6s）；若失败前正在看的就是这一天 ⇒ 明细卡还原', async () => {
    const impl: ReqImpl = async (config) => {
      if (String(config.url).includes('daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      throw new Error('结算分页取全中断：已取到 2 条，后端 total 报 3 条，但第 2 页返回 0 条')
    }
    const root = await mountPage(impl)
    const link = document.createElement('a')
    link.className = VIEW_DETAIL_CLASS
    link.dataset.date = '2026-10-28'
    root.appendChild(link)
    link.click()
    // 第一次明细取数失败（上面那支 throw）⇒ 原样提示（明细永远不 render）。
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith(
      expect.stringContaining('结算分页取全中断'),
      { time: 6000 },
    )
    expect(LAYUI.render.mock.calls.length).toBe(1) // 只有主表
    // `currentDetailDay` 仍是 null（失败不认值）⇒ 标题没写过。
    expect(root.querySelector('.detailTitle')!.textContent).toBe('')
  })

  it('「取全中断」对**正在看的那一天**的还原：先成功打开 A 天，再点 B 天失败 ⇒ A 天的明细被清掉', async () => {
    let failDetail = false
    const impl: ReqImpl = async (config) => {
      if (String(config.url).includes('daily-summary')) {
        return { items: DAYS, total: DAYS.length, page: 1, page_size: 20 }
      }
      if (failDetail) throw new Error('取全中断')
      return { items: ROWS, total: ROWS.length, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    failDetail = true
    const link = document.createElement('a')
    link.className = VIEW_DETAIL_CLASS
    link.dataset.date = '2026-10-27'
    root.appendChild(link)
    link.click()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    // 失败的不是「正在看的那天」（还没认值）⇒ 标题保留 A 天，不清。
    expect(root.querySelector('.detailTitle')!.textContent).toBe('2026-10-28的结算明细')
  })
})

describe('明细内的订单号搜索（精确匹配）', () => {
  it('还没选天就点「搜索明细」⇒ 提示 + 零明细请求', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>('#detailSearchBtn')!.click()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('请先在上方列表点击「查看明细」选择日期', { time: 1500 })
    expect(seenReqs().filter((r) => r.url === '/api/business/settlements')).toHaveLength(0)
  })

  it('选天后填订单号点「搜索明细」⇒ 请求带 order_no（trim 后）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '  20260920213215233029  '
    root.querySelector<HTMLElement>('#detailSearchBtn')!.click()
    await vi.waitFor(() => {
      expect(seenReqs().filter((r) => r.url === '/api/business/settlements').length).toBe(2)
    })
    expect(seenReqs().filter((r) => r.url === '/api/business/settlements')[1]!.params).toMatchObject({
      order_no: '20260920213215233029',
      date_from: '2026-10-28',
      date_to: '2026-10-28',
    })
  })

  it('订单号框空着（或纯空格）⇒ 不带 order_no（「没填」不是「筛空串」）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '   '
    root.querySelector<HTMLElement>('#detailSearchBtn')!.click()
    await vi.waitFor(() => {
      expect(seenReqs().filter((r) => r.url === '/api/business/settlements').length).toBe(2)
    })
    expect(seenReqs().filter((r) => r.url === '/api/business/settlements')[1]!.params).not.toHaveProperty('order_no')
  })
})

describe('搜索会把明细卡还原（旧的一天不再对应新筛选）', () => {
  it('点过明细再「搜索」⇒ 标题清空、明细表 reload 空数据、订单号框清空、主表回第 1 页', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '20260920213215233029'
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    // 等「搜索那次」daily-summary 请求落地（mount 1 + 明细 1 + 搜索 1 = 共 3 次）。
    await vi.waitFor(() =>
      expect(seenReqs().filter((r) => r.url.includes('daily-summary')).length).toBe(2),
    )
    expect(root.querySelector('.detailTitle')!.textContent).toBe('')
    expect(root.querySelector<HTMLInputElement>('#key1')!.value).toBe('')
    expect(LAYUI.reload).toHaveBeenCalledWith(
      TABLE2_ID,
      expect.objectContaining({ data: [], page: { curr: 1, limit: 20 } }),
    )
    expect(seenReqs()[seenReqs().length - 1]!.params).toMatchObject({ page: 1 })
  })

  it('还原后再点「导出记录」⇒ 回到「请先选天」分支，不带旧的天', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    await vi.waitFor(() =>
      expect(seenReqs().filter((r) => r.url.includes('daily-summary')).length).toBe(2),
    )
    fireToolbar('exportBtn', TABLE2_ID)
    expect(LAYUI.msg).toHaveBeenCalledWith('请先点击「查看明细」选择日期', { time: 1500 })
    expect(vi.mocked(downloadPerformanceExport)).not.toHaveBeenCalled()
  })
})

/* ═══════════════════════ 导出 ═══════════════════════ */

describe('导出记录：走绩效导出的既有端点（date_type=settled 同天口径）', () => {
  it('点导出 ⇒ 参数 = settle_status:已结账 + date_type:"settled" + 当天两端，且不含 order_no/user/game；并真的落了文件', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-27' }))
    const requestsBefore = mockedRequest.mock.calls.length
    fireToolbar('exportBtn', TABLE2_ID)
    await vi.waitFor(() => expect(vi.mocked(downloadPerformanceExport)).toHaveBeenCalledTimes(1))
    const arg = vi.mocked(downloadPerformanceExport).mock.calls[0]![0] as Record<string, unknown>
    expect(arg).toEqual({
      settle_status: '已结账',
      date_type: 'settled',
      date_from: '2026-10-27',
      date_to: '2026-10-27',
    })
    // ⛔ 不带 order_no（对标导出也不带 key；业务方 2026-09-22 裁定「不带就行」）；
    // 也不带 user / game（这两个筛选已随界面下线）。
    expect(arg).not.toHaveProperty('order_no')
    expect(arg).not.toHaveProperty('user')
    expect(arg).not.toHaveProperty('game')
    // 「真落一个 xlsx」在 jsdom 里的判据 = 走到了 `URL.createObjectURL` 那一步
    // （与旧版 spec 同一判据；导出走 `http.get` + Blob，**不经 `request()`**）。
    await vi.waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1))
    expect(mockedRequest.mock.calls.length).toBe(requestsBefore)
  })

  it('导出的天 = 当前正在看的那一天（不是筛选栏的日期框——它们可能已被改过）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-26' }))
    ;(root.querySelector<HTMLInputElement>('#sDate')!).value = '2099-01-01 00:00:00'
    fireToolbar('exportBtn', TABLE2_ID)
    await vi.waitFor(() => expect(vi.mocked(downloadPerformanceExport)).toHaveBeenCalled())
    expect(vi.mocked(downloadPerformanceExport).mock.calls[0]![0]).toMatchObject({
      date_from: '2026-10-26',
      date_to: '2026-10-26',
    })
  })

  it('导出抛 BizError（超上限）⇒ 页面弹后端 msg（6s）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    vi.mocked(downloadPerformanceExport).mockRejectedValueOnce(
      new BizError(1000, '超过导出上限，请先收窄筛选'),
    )
    fireToolbar('exportBtn', TABLE2_ID)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('超过导出上限，请先收窄筛选', { time: 6000 })
  })
})

/* ═══════════════════════ 关联订单开层（沿用重设计前的交互） ═══════════════════════ */

describe('「关联订单」原地开层（layer.open 装我们的复刻页）', () => {
  /** 造一个 `a.orderdetails`（真 layui 下由明细 templet 渲染）并点它。 */
  function clickOrderLink(root: HTMLElement, r: SettlementListItem): void {
    const host = document.createElement('div')
    host.innerHTML = buildDetailCols()[3]!.templet!(r)
    const link = host.firstElementChild as HTMLElement
    root.appendChild(link)
    link.click()
  }

  it('层参数逐字（title / shade / shadeClose / type / skin / anim / area = 本页自己的 1125px×90%）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    clickOrderLink(root, row({ id: 1, user: 7, source_order_no: 'TB-合成-结算-来源甲' }))
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts['title']).toBe('TB-合成-结算-来源甲订单详情')
    expect(opts['shade']).toEqual([0.3, '#333'])
    expect(opts['shadeClose']).toBe(true)
    expect(opts['type']).toBe(2)
    expect(opts['skin']).toBe('layui-layer-lan')
    expect(opts['anim']).toBe(8)
    expect(opts['area']).toEqual(['1125px', '90%'])
  })

  it('`content` = 我们自己的复刻页（page=order-orderview + order_no = 系统单号），且不走 vue-router', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    clickOrderLink(root, row({ id: 1, user: 7 }))
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(String(opts['content'])).toBe(
      `${import.meta.env.BASE_URL}layui-island/page.html?page=order-orderview&order_no=20260920213215233029`,
    )
  })

  it('D-0301：来源单号非空 ⇒ 标题前缀 = 展示单号；来源为空 ⇒ 回退内置单号', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    clickOrderLink(root, row({ id: 1, user: 7, source_order_no: '' }))
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts['title']).toBe('20260920213215233029订单详情')
  })

  it('⛔ 开层前的单号形状闸：非 20 位数字 ⇒ 不开层 + 固定文案提示 + 零新请求', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    const host = document.createElement('div')
    host.innerHTML = '<a href="javascript:void(0);" class="orderdetails" data-id="not-a-number">x</a>'
    const link = host.firstElementChild as HTMLElement
    root.appendChild(link)
    const before = mockedRequest.mock.calls.length
    link.click()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单详情', { time: 1500 })
    expect(mockedRequest.mock.calls.length).toBe(before)
  })
})

/* ═══════════════════════ 键盘与卸载 ═══════════════════════ */

describe('键盘与卸载', () => {
  it('回车在日期框上 = 主表搜索（回第 1 页）；回车在 #key1 上 = 搜明细（按目标分流）', async () => {
    const root = await mountPage()
    await clickViewDetail(root, day({ date: '2026-10-28' }))
    // #key1 上的 Enter → searchDetail()（此刻有一天在看 ⇒ 真取数，带 order_no）。
    // ⚠️ 顺序必须在日期框那次 Enter **之前**：主表搜索会把明细还原（天失效 ⇒ 后者只剩提示）。
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '20260920213215233029'
    root.querySelector<HTMLInputElement>('#key1')!.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await vi.waitFor(() => {
      expect(seenReqs().filter((r) => r.url === '/api/business/settlements').length).toBeGreaterThanOrEqual(2)
    })
    const detailReqs = seenReqs().filter((r) => r.url === '/api/business/settlements')
    expect(detailReqs[detailReqs.length - 1]!.params).toMatchObject({
      order_no: '20260920213215233029',
    })
    // 日期框上的 Enter → search()（主表回第 1 页 + 明细还原）。
    root.querySelector<HTMLInputElement>('#sDate')!.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await vi.waitFor(() => {
      expect(seenReqs().filter((r) => r.url.includes('daily-summary')).length).toBe(2)
    })
    const dailyReqs = seenReqs().filter((r) => r.url.includes('daily-summary'))
    expect(dailyReqs[dailyReqs.length - 1]!.params).toMatchObject({ page: 1 })
    expect(root.querySelector('.detailTitle')!.textContent).toBe('')
  })

  it('非 Enter / 非 INPUT 目标不触发搜索', async () => {
    const root = await mountPage()
    const before = mockedRequest.mock.calls.length
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    root.querySelector<HTMLElement>('#searchBtn')!.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('卸载：监听移除 + 标记清空；卸载后再点「查看明细」零请求', async () => {
    const root = await mountPage()
    const { mountBillsummaryPage } = await import('../emp-billsummary')
    // 重新挂一次拿 unmount（mountPage 不返回它）。
    const root2 = document.createElement('div')
    document.body.appendChild(root2)
    const unmount = await mountBillsummaryPage(root2, { onNavigate: vi.fn() })
    unmount()
    expect(root2.innerHTML).toBe('')
    const link = document.createElement('a')
    link.className = VIEW_DETAIL_CLASS
    link.dataset.date = '2026-10-28'
    root2.appendChild(link)
    const before = mockedRequest.mock.calls.length
    link.click()
    expect(mockedRequest.mock.calls.length).toBe(before)
    expect(root.querySelector('#tableList')).not.toBeNull() // 第一份 root 不受影响
  })

  it('页面上没有「尚未开放」的派发（新形态查标记与文案）', () => {
    expect(buildHtml()).not.toContain('尚未开放')
  })
})
