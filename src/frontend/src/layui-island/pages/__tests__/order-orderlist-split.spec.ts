// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * ⭐ 拆单关联组：订单号前的三角入口 + **行内展开**。
 *
 * 子行不重复显示展开入口，避免重复插入同组成员；订单号保持详情入口。
 *
 * ## 判据分两半，都在**真实出口**上
 *
 * 1. **templet 层**（`buildOrderCols` 的真 templet）：只有组代表有三角、位置在订单号之前、
 *    **非拆单行逐字不变**（整串相等，不是「大概像」）。
 * 2. **交互层**（挂真页面 + 真 click 委托）：点「拆」⇒ 走**真** `fetchSplitMembers`
 *    （断言 `request()` 收到的 URL 与 `params`）⇒ 在**该行下方**插同组其余成员行
 *    （缩进 + ⛔ 子行没有展开开关）；再点 ⇒ 收起。
 *
 * ⚠️ layui 的表格在 jsdom 里是**替身**（`table.render` 不画 DOM）⇒ 「那一行」由本文件用
 * **页面自己的 templet** 造出来再插进一个假表体：这是 `AGENTS.md` §5-G 坑③ 要求的
 * 「先 bootstrap（页面挂载自己会做）、**再塞标记**」那一半（真 layui 渲染出来的行与它同形）。
 *
 * ⛔ 类名与选择器分成两个常量（§5-G 坑①）：类名一律引页面导出的常量，选择器另写；
 * ⛔ 以 `(` 开头的语句一律前置分号。
 */
const EVIDENCE = readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/order-orderlist.html',
  ),
  'utf8',
)

/** layui 的可控替身（形状照 `order-orderlist-phase1.spec.ts`；本文件只驱动那条 click 委托）。 */
const LAYUI = vi.hoisted(() => ({
  table: {
    render: vi.fn(),
    on: vi.fn(),
    reload: vi.fn(),
    reloadData: vi.fn(),
    checkStatus: vi.fn((): { data: unknown[] } => ({ data: [] })),
    getOptions: vi.fn((): { page: { limit: number } } => ({ page: { limit: 20 } })),
  },
  form: { render: vi.fn(), on: vi.fn(), val: vi.fn(() => ({})) },
  layer: {
    msg: vi.fn(),
    open: vi.fn(),
    close: vi.fn(),
    confirm: vi.fn(),
    prompt: vi.fn(),
    tips: vi.fn(),
    alert: vi.fn(),
    load: vi.fn(() => 9001),
  },
  laydate: { render: vi.fn() },
  element: { init: vi.fn() },
  $: (el: Element) => el,
}))

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/layui/loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/loader')>()
  return { ...actual, loadLayui: async () => LAYUI }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/**
 * ⚠️ **只替身化那两发挂载期取数**（候选名单 / 来源字典）——`fetchSplitMembers` **保持真身**：
 * 本文件的核心判据就是「点展开打的是哪条 URL、带什么参数」，把它也换成替身等于没测。
 */
vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchFilterOptions: vi.fn(async () => ({
      takers: [],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
    // ⭐ `mount` 会多取一次分组字典（筛选栏「订单分组」的候选）
    // ⇒ 不替身化会多一发真请求（本文件多处用 `mockResolvedValueOnce` 排队响应 ⇒
    //   队首被它吃掉，批量动作读到错形状，`[orderBatch] … length` 当场红）。
    fetchOrderSourceGroups: vi.fn(async () => ({
      items: [],
      total: 0,
      page: 1,
      page_size: 200,
    })),
  }
})

// ⚠️ 工厂用**不 importOriginal** 的形态（理由见 `order-orderlist-phase1.spec.ts` 的同一处注释：
// `http.ts` 与 `auth.ts` 之间有一处有意为之的循环依赖，拉真身会让 `request` 换不掉）。
vi.mock('@/api/http', () => ({ request: vi.fn(), http: { get: vi.fn() } }))

import { request } from '@/api/http'
import type { OrderListItem } from '@/api/orders'
import {
  SPLIT_CHILD_ROW_CLASS,
  buildOrderCols,
  mountOrderListPage,
} from '../order-orderlist'

const mockedRequest = vi.mocked(request)

/** ⛔ 类名（页面导出）与**选择器**（本文件）分成两个常量：改名时这里会红，而不是静默匹配不到。 */
const CHILD_ROW_SELECTOR = `tr.${SPLIT_CHILD_ROW_CLASS}`

/** 一组三笔：主单（下面那个 `MAIN`）+ 两笔子单。 */
const GROUP_ID = '90000000000000000001'
const MAIN_NO = '20260920213215233029'

/** 载荷基线（字段集 = 后端 `OrderListSerializer` 的键；值都是合成值）。 */
const BASE: OrderListItem = {
  order_no: MAIN_NO,
  source_order_no: '',
  source: '微信',
  taker_name: '',
  status: 'UNASSIGNED',
  game: 2,
  server: null,
  project_text: '深渊满星',
  total_price: '100.00',
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-09-20T21:32:15+08:00',
  game_role_name: '',
  customer_ww: '',
  hall_price: null,
  sales_agent: '',
  created_by: '',
  remark: '',
  net_received: '100.00',
  hall_publisher_name: '',
  completed_at: null,
  time_requirement: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
  // ⭐ 列表载荷带「打手绩效是否已结账」。
  taker_settled: false,
}

/** ⛔ 非拆单行（两个新键是 `''` / `0`——后端对非拆单恒回这两个值）。 */
const PLAIN: OrderListItem = { ...BASE, split_id: '', split_members: 0 }

/** 主单行（组内成员 3）。 */
const MAIN: OrderListItem = { ...BASE, split_id: GROUP_ID, split_members: 3 }

/** 两笔子单（同组、成员总数同样是 3）。 */
const CHILD_A: OrderListItem = {
  ...BASE,
  order_no: '20260920213215233030',
  project_text: '子项目甲',
  split_id: GROUP_ID,
  split_members: 3,
}
const CHILD_B: OrderListItem = {
  ...BASE,
  order_no: '20260920213215233031',
  project_text: '子项目乙',
  split_id: GROUP_ID,
  split_members: 3,
}

/** 取「订单号」列的**真 templet**（⛔ 不手抄标记：与页面同源）。 */
function orderIdCell(row: OrderListItem, splitChild = false): string {
  const col = buildOrderCols(
    () => '原神',
    () => '',
    { splitChild },
  ).find((c) => c.field === '_orderid')
  expect(col, '列定义里找不到 field=_orderid').toBeDefined()
  return (col!.templet as (d: OrderListItem) => string)(row)
}

/**
 * 把「layui 渲染出来的那一行」塞进 root（§5-G 坑③ 的「再塞标记」那一半）。
 *
 * 结构照 vendored layui 的 `renderData`：主表体一张 + **右侧固定列另画一张**
 * （本页「操作」列是 `fixed:'right'`）⇒ 两张都要造，展开时页面也会两张都插。
 */
function installFakeTable(
  root: HTMLElement,
  row: OrderListItem,
): { tbody: HTMLElement; fixedTbody: HTMLElement; parentTr: HTMLTableRowElement; fixedTr: HTMLTableRowElement } {
  const view = document.createElement('div')
  view.className = 'layui-table-view'
  const box = document.createElement('div')
  box.className = 'layui-table-box'
  const body = document.createElement('div')
  body.className = 'layui-table-body layui-table-main'
  const tbody = document.createElement('tbody')
  const tr = document.createElement('tr')
  tr.dataset.index = '0'
  for (const col of buildOrderCols(() => '原神', () => '')) {
    const td = document.createElement('td')
    if (col.field !== undefined) td.dataset.field = col.field
    td.className = 'layui-table-col-special'
    const cell = document.createElement('div')
    cell.className = 'layui-table-cell layui-table-cell1-0-1'
    if (col.align) cell.setAttribute('align', col.align)
    cell.innerHTML = col.field === '_orderid' ? orderIdCell(row) : ''
    td.appendChild(cell)
    tr.appendChild(td)
  }
  tbody.appendChild(tr)
  const table = document.createElement('table')
  table.appendChild(tbody)

  const fixed = document.createElement('div')
  fixed.className = 'layui-table-fixed layui-table-fixed-r'
  const fixedBody = document.createElement('div')
  fixedBody.className = 'layui-table-body'
  const fixedTbody = document.createElement('tbody')
  const fixedTr = document.createElement('tr')
  fixedTr.dataset.index = '0'
  for (const col of buildOrderCols(() => '原神', () => '').filter((item) => item.fixed === 'right')) {
    const fixedTd = document.createElement('td')
    if (col.field !== undefined) fixedTd.dataset.field = col.field
    fixedTd.className = 'layui-table-col-special'
    const cell = document.createElement('div')
    cell.className = 'layui-table-cell layui-table-cell1-0-28'
    fixedTd.appendChild(cell)
    fixedTr.appendChild(fixedTd)
  }
  fixedTbody.appendChild(fixedTr)
  const fixedTable = document.createElement('table')
  fixedTable.appendChild(fixedTbody)

  body.appendChild(table)
  fixedBody.appendChild(fixedTable)
  fixed.appendChild(fixedBody)
  box.appendChild(body)
  box.appendChild(fixed)
  view.appendChild(box)
  root.appendChild(view)
  // 非空哨兵：真塞进去了（不能对着空 DOM 断言）
  expect(root.querySelector(`${CHILD_ROW_SELECTOR}`)).toBeNull()
  return { tbody, fixedTbody, parentTr: tr, fixedTr }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-orderlist.html')) {
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
  mockedRequest.mockImplementation((async (config: { url?: string; params?: unknown; data?: { order_nos: string[]; dry_run: boolean } }) => {
    if (config.url === '/api/auth/me') {
      return { user: { username: 'u', display_name: 'u', game_ids: [], menu_permissions: [] } }
    }
    if (config.url === '/api/business/orders/') {
      // 展开那一发（`?split_id=`）：后端**不折叠** ⇒ 回来的是整组（含主单）。
      return { items: [MAIN, CHILD_A, CHILD_B], total: 3, page: 1, page_size: 200 }
    }
    if (config.url === '/api/business/orders/batch/delete/' && config.data) {
      const { order_nos, dry_run } = config.data
      return { dry_run, eligible: order_nos, ineligible: [], applied: dry_run ? [] : order_nos, effects: [] }
    }
    return { items: [], total: 0, page: 1, page_size: 200 }
  }) as never)
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function mountPage(): Promise<{ root: HTMLElement }> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountOrderListPage(root)
  mockedRequest.mockClear()
  return { root }
}

describe('拆单：并列组入口与「非拆单行不变」（templet 层）', () => {
  it('主单保留组标记，子行在订单号前显示连接线', () => {
    const html = orderIdCell(MAIN)
    expect(html).toContain(`data-split-id="${GROUP_ID}"`)
    expect(html).not.toContain('splitToggle')
    const childHtml = orderIdCell(CHILD_A, true)
    expect(childHtml).toContain('class="orderLine sontr"')
    expect(childHtml).toContain('class="split-child-connector"')
    expect(childHtml).not.toContain('splitToggle')
    const plainHtml = orderIdCell(PLAIN)
    expect(plainHtml).toContain('class="split-toggle-slot"')
    expect(plainHtml).not.toContain('class="splitToggle"')
    const urgentHtml = orderIdCell({ ...PLAIN, is_urgent: true })
    expect(urgentHtml.indexOf('class="orderdetails"')).toBeLessThan(urgentHtml.indexOf('background-color:#FF5722'))
  })

  it('非拆单行不画展开标记，但保留固定宽度空槽让订单号对齐', () => {
    expect(orderIdCell(PLAIN)).toBe(
      '<div class="orderLine" id="order_20260920213215233029">' +
        '<span class="split-toggle-slot" style="display:inline-flex;align-items:center;justify-content:center;flex:0 0 14px;width:14px;height:18px"></span>' +
        '<a href="javascript:void(0);" class="orderdetails"' +
        ' data-order="20260920213215233029" data-viewId="20260920213215233029"' +
        ' style="color:#0094ff">20260920213215233029</a>' +
        '</div>',
    )
  })

  it('订单号表头保留文本，并与固定三角槽预留宽度对齐', async () => {
    const { root } = await mountPage()
    const style = root.querySelector<HTMLStyleElement>('#tt-order-id-header-style')
    expect(style?.textContent).toContain('th[data-field="_orderid"] .layui-table-cell{padding-left:29px}')
    expect(buildOrderCols(() => '原神', () => '').find((col) => col.field === '_orderid')?.title).toBe('订单号')
  })

  it('组内只有一笔（`split_members === 1`）不画展开入口（判据是 `> 1`）', () => {
    expect(orderIdCell({ ...BASE, split_id: GROUP_ID, split_members: 1 })).not.toContain(
      '>拆</a>',
    )
  })
})

describe('拆单：列表加载后直接显示同组成员', () => {
  it('取一次拆单组，并把子行插入主表和固定列', async () => {
    const { root } = await mountPage()
    const { tbody, fixedTbody, parentTr, fixedTr } = installFakeTable(root, MAIN)

    const options = LAYUI.table.render.mock.calls[0]![0] as {
      done: (res: { data: OrderListItem[] }, page: number, count: number) => void
    }
    options.done({ data: [MAIN] }, 1, 1)
    await vi.waitFor(() => {
      expect(tbody.querySelectorAll(CHILD_ROW_SELECTOR)).toHaveLength(2)
    })

    // ⚠️ **真实出口**：点开打的是 `GET /api/business/orders/?split_id=<组>&page_size=200`
    //    （`fetchSplitMembers` 是真身 ⇒ 这里断言的是 `api/orders.ts` 的产物）。
    const call = mockedRequest.mock.calls
      .map((c) => c[0] as { method?: string; url?: string; params?: Record<string, unknown> })
      .find((c) => c.url === '/api/business/orders/')
    expect(call, '展开那一发没有打订单列表端点').toBeDefined()
    expect(call!.method).toBe('get')
    expect(call!.params).toEqual({ split_id: GROUP_ID, page_size: 200 })

    // 插进来的是**同组其余成员**（主单自己不再插一遍），顺序照后端
    const childNos = Array.from(tbody.querySelectorAll<HTMLTableRowElement>(CHILD_ROW_SELECTOR)).map(
      (tr) => tr.querySelector<HTMLElement>('.orderLine')?.id,
    )
    expect(childNos).toEqual([
      `order_${CHILD_A.order_no}`,
      `order_${CHILD_B.order_no}`,
    ])

    // 子行没有重复展开开关
    const firstChild = tbody.querySelector<HTMLTableRowElement>(CHILD_ROW_SELECTOR)!
    expect(firstChild.querySelector('.orderdetails')?.getAttribute('data-order')).toBe(CHILD_A.order_no)
    const line = firstChild.querySelector<HTMLElement>('.orderLine')!
    expect(line.classList.contains('sontr')).toBe(true)
    expect(line.querySelector('.split-toggle-slot')?.getAttribute('style')).toContain('width:14px')
    expect(line.querySelector('.split-toggle-slot')?.textContent?.trim()).toBe('')
    expect(line.querySelector('.orderdetails')?.previousElementSibling?.classList.contains('split-child-connector')).toBe(true)
    expect(parentTr.querySelector('[data-split-line]')?.getAttribute('data-split-line')).toBe('start')
    expect(firstChild.querySelector('[data-split-line]')?.getAttribute('data-split-line')).toBe('branch')
    expect(tbody.querySelectorAll(CHILD_ROW_SELECTOR)[1]?.querySelector('[data-split-line]')?.getAttribute('data-split-line')).toBe('end')

    firstChild.querySelector<HTMLElement>('.orderdetails')!.click()
    const detailConfig = LAYUI.layer.open.mock.calls[LAYUI.layer.open.mock.calls.length - 1]?.[0] as
      | { content?: string }
      | undefined
    expect(detailConfig?.content).toContain(`order_no=${CHILD_A.order_no}`)

    // ⚠️ 右侧固定列那张表**也要插**（layui 为 `fixed:'right'` 另画一张表）：不插会与主行错行。
    expect(fixedTbody.querySelectorAll(CHILD_ROW_SELECTOR)).toHaveLength(2)
    const mainChild = tbody.querySelector<HTMLTableRowElement>(CHILD_ROW_SELECTOR)!
    const mainCols = buildOrderCols(() => '原神', () => '')
    expect(Array.from(mainChild.cells).map((cell) => cell.dataset.field)).toEqual(
      mainCols.map((col) => col.field),
    )
    expect(Array.from(mainChild.cells).map((cell) => cell.firstElementChild?.className)).toEqual(
      Array.from(parentTr.cells).map((cell) => cell.firstElementChild?.className),
    )
    expect(mainChild.querySelector('[data-field="_throw"] > div')?.getAttribute('align')).toBe('center')
    expect(Array.from(mainChild.cells).map((cell) => cell.firstElementChild?.getAttribute('align'))).toEqual(
      Array.from(parentTr.cells).map((cell) => cell.firstElementChild?.getAttribute('align')),
    )
    for (const col of mainCols.filter((item) => item.hide)) {
      expect(mainChild.querySelector(`[data-field="${col.field}"]`)?.classList.contains('layui-hide')).toBe(true)
    }
    const fixedChild = fixedTbody.querySelector<HTMLTableRowElement>(CHILD_ROW_SELECTOR)!
    expect(Array.from(fixedChild.cells).map((cell) => cell.dataset.field)).toEqual(
      mainCols.filter((col) => col.fixed === 'right').map((col) => col.field),
    )
    expect(Array.from(fixedChild.cells).map((cell) => cell.firstElementChild?.className)).toEqual(
      Array.from(fixedTr.cells).map((cell) => cell.firstElementChild?.className),
    )

    const childCheckbox = firstChild.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    const selectRows = LAYUI.table.on.mock.calls.find(([name]) => name === 'checkbox(datatable)')![1] as
      (event: { type: 'all'; checked: boolean }) => void
    childCheckbox.click()
    selectRows({ type: 'all', checked: false })
    expect(childCheckbox.checked).toBe(false)
    childCheckbox.click()
    root.querySelector<HTMLButtonElement>('[data-tb="delOrderBtn"]')!.click()
    const firstConfirm = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
    firstConfirm(1)
    const secondConfirm = LAYUI.layer.confirm.mock.calls[1]![2] as (index: number) => void
    secondConfirm(2)
    await vi.waitFor(() => expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(3))
    expect(LAYUI.layer.confirm.mock.calls[2]![0]).toContain(CHILD_A.order_no)
    ;(LAYUI.layer.confirm.mock.calls[2]![2] as (index: number) => void)(3)
    await vi.waitFor(() => {
      const deletes = mockedRequest.mock.calls.map(([config]) => config)
        .filter((config) => config.url === '/api/business/orders/batch/delete/')
      expect(deletes.map((config) => config.data)).toEqual([
        { order_nos: [CHILD_A.order_no], isDelEmp: 'Y', dry_run: true, confirmed_effects: [] },
        { order_nos: [CHILD_A.order_no], isDelEmp: 'Y', dry_run: false, confirmed_effects: [] },
      ])
    })
  })

})
