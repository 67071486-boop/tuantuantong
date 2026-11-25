// @vitest-environment jsdom
import { renderOrderRows } from './order-list-table-fixture'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 管理订单页「批量操作」接线的**驱动真交互**页测试。
 *
 * ⚠️ **本文件覆盖批量接线的 9 颗按钮**：`changeOrderPriceBtn` /
 * `changeOrderThrowPriceBtn` / `setOrderJSBtn` / `changetypesBtn` / `refundOrderBtn` /
 * `setRecMan` / `changeStaBtn` / `delOrderBtn` / `builderSellEmpOrderBtn`
 * （另有文末第 11 组的补充条目：勾 3 笔、取消零请求、全不合格只弹清单、`reload` 早于 `msg` 的顺序…）。
 * ⚠️ 「两发顺序（`dry_run` 先于 `apply`）」**不另立用例**：本文件既有的位置断言
 *    （`sent(0)` / `sent(1)`）已经把它钉住。
 *
 * 判据一律打在**真出口**上（`AGENTS.md` §5-G / E-0009 同族）：
 *  - `@/api/http` 的 `request()` 发了几发、URL / body 是什么（`@/api/orders.ts` 的
 *    `batchOrderAction` 与 9 个薄包装**保持真身**）；
 *  - `@/api/layui-island/orderBatch.ts` 的 `runBatchFlow` **保持真身** ⇒ 「两步式」本身
 *    （`dry_run` → `apply`）也在判据范围内；
 *  - 行内入口那几条是**回归哨兵**：接线前它们就应当是绿的，接线后必须仍然绿（`{kind:'single'}` 默认值）。
 *
 * 夹具单号是**合成值**（E-0077），**形状**一律过 20 位闸（spec §4.4）——**唯一例外**是
 * `ORDER_JUNK`：批量**删除****有意豁免**该形状闸（R-D1，清理存量垃圾行），
 * 那条页级用例刻意用 16 位形状串（见「删除」组最后一条的 docstring）。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

/** 合成 20 位单号（过 `assertOrderNo` 的形状闸）。 */
const ORDER_A = '20000000000000000001'
const ORDER_B = '20000000000000000002'

/** 合成 **16 位**单号（**形状**照本机演示库那条删不掉的垃圾行；⛔ 不是真单号，E-0077）。 */
const ORDER_JUNK = '9'.repeat(16)

/**
 * 「记退款」批量口径说明（spec §5.4 逐字；**我方自拟**，证据件里没有这句）。
 * ⛔ 只在 `orderNos.length > 1` 时渲染 ⇒ 单笔一个字都不加。
 */
const BATCH_REFUND_NOTE =
  '批量记退款时，每一笔各设置成这个累计额（可能调高也可能调低）。'

/**
 * layui 的**可控替身**（挂载整页用）。形状照 `order-orderlist-phase1.spec.ts`，只留本文件用到的成员：
 * `bootstrapIsland` 在 jsdom 里加载不了真 layui，换成替身后 `layer.*` 的入参才能被断言。
 *
 * ⚠️ `runBatchFlow` 走的是 `@/layui/loader` 的 `loadLayui(['layer'])`（不是 `bootstrapIsland`）
 * ⇒ 本文件把那个出口也指向**同一个**替身命名空间（否则两步式里缺省 `msg` 会落到真 layui）。
 */
const LAYUI = vi.hoisted(() => {
  let nextIndex = 1
  const open = vi.fn(() => nextIndex++)
  const msg = vi.fn()
  const prompt = vi.fn()
  const confirm = vi.fn()
  const alert = vi.fn()
  const close = vi.fn()
  /**
   * ⭐ T1：`runBatchFlow` 现在会 `layer.load(1, { shade: false })` 并 `layer.close(index)`。
   * 层号用**定值哨兵**（与用例注入的层号 1..12 不冲突）⇒ 本文件既有那些「`close(7)` 恰好一次」
   * 判据只需在**看得见 loading 的地方**过滤掉它（见 `dialogCloses()`），不必改期望值。
   */
  const loadIndex = 9001
  const load = vi.fn(() => loadIndex)
  return {
    open,
    msg,
    prompt,
    confirm,
    alert,
    close,
    load,
    loadIndex,
    form: { render: vi.fn(), on: vi.fn(), val: vi.fn(() => ({})) },
    table: {
      render: vi.fn(),
      on: vi.fn(),
      reload: vi.fn(),
      // 返回类型显式写成 `{ data: unknown[] }`：否则推断成 `never[]`，
      // 用例里的 `mockReturnValue({ data: [ROW] })` 过不了类型检查。
      checkStatus: vi.fn((): { data: Array<{ order_no: string } & Record<string, unknown>> } => ({ data: [] })),
      getOptions: vi.fn((): { page: { limit: number } } => ({ page: { limit: 20 } })),
    },
    layer: { open, msg, prompt, confirm, alert, close, load, tips: vi.fn() },
    laydate: { render: vi.fn() },
    element: { init: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // ⛔ 只有 `bootstrapIsland` 换成替身：`islandMsg` / `initIslandElements` 保持真身
    //    （它们只碰替身里已有的 `layer.msg` / `element.init`）。
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

/**
 * `runBatchFlow` 取 `layer` 的唯一出口（本模块没有 `layui` 导出）⇒ 指向同一个替身。
 * ⚠️ 用 `vi.fn` 而不是常量函数：F5 的判据要构造「**一次性**加载失败」(`mockRejectedValueOnce`)。
 */
const LOAD_LAYUI = vi.hoisted(() => vi.fn())
vi.mock('@/layui/loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/loader')>()
  return { ...actual, loadLayui: LOAD_LAYUI }
})

/**
 * ⚠️ mount 期本页会 `await fetchMe()`（一次 `/api/auth/me`）决定行内入口渲不渲染。
 * 这里把它替身化：本文件多处用 `mockResolvedValueOnce` **排队**两发批量响应，而 mount 那一发
 * 会把队首吃掉（读到 `undefined` ⇒ 「这一批未完成」）⇒ 替身化后 mount 零请求，队列完整。
 */
vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn(async () => undefined) }
})

vi.mock('@/api/orderDictionaries', () => ({
  fetchOrderDictionaries: vi.fn(async () => ({ games: [], servers: [], order_sources: [] })),
}))

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/**
 * ⚠️ mount 期本页会取候选名单（`fetchFilterOptions`）与订单来源字典（`fetchOrderSources`）各一发，
 * ⭐ 还有**一发**「销售客服」候选（`fetchKefuOptions`）。
 * 三个都替身化：本文件多处断言「这一发请求是批量动作」，那几发 GET 会污染 `request()` 计数
 * （判据本身不动）。候选名单那一面在 `order-orderlist-filters.spec.ts` 里测。
 * ⚠️ **新端点不替身化会静默改读数**：本文件多处先 `mockResolvedValueOnce` 排队两发批量响应、
 * 再 `mountPage()` ⇒ mount 那一发会把**队首**吃掉（实测症状：成功提示变成「已处理 0 笔订单」）。
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
    fetchKefuOptions: vi.fn(async () => ({ items: [], truncated: false })),
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

/**
 * 传输层替身：`request()` 被换掉，**`batchOrderAction` 保持真身** ⇒ URL / 方法 / body
 * 断言在真 `api/orders.ts` 的产物上。
 *
 * ⚠️ **工厂必须是「不 importOriginal」的形态**（`order-orderlist-phase1.spec.ts` 记过实测真因）：
 * `@/api/http` 与 `@/api/auth` 之间有一处**有意为之的循环依赖** ⇒ 写 `importOriginal` 会把
 * 真 `http.ts` 实例化、`auth.ts` 的 `request` 绑定落在真身上，页 mount 里 `fetchMe()` 会打真网络。
 */
vi.mock('@/api/http', () => ({
  request: vi.fn(),
  // ⭐ 单笔指派成功要读后端 `msg`（可能带「未能撤回」警告）⇒ 页面走 `requestEnvelope`。
  requestEnvelope: vi.fn(),
  http: { get: vi.fn() },
}))

import type { AxiosRequestConfig } from 'axios'
import { fetchMe } from '@/api/auth'
import { BizError } from '@/api/errors'
import { request, requestEnvelope } from '@/api/http'
import type { BatchOutcome } from '@/api/orders'
import { buildOrderCols, mountOrderListPage } from '../order-orderlist'
import { ASSIGN_PLATFORM_TAKEN_MESSAGE } from '../order-orderlist'

/** 一行真实形状的订单列表载荷（字段集 = `OrderListSerializer`）。 */
const ROW = {
  order_no: ORDER_A,
  source_order_no: '',
  source: '',
  status: 'UNASSIGNED',
  game: 1,
  server: null,
  project_text: '原神-深渊',
  total_price: '100.00',
  refund_fee: '0.00',
  customer_phone: '13800000000',
  is_urgent: false,
  created_at: '2026-09-22T10:00:00+08:00',
  taker_name: '',
  hall_price: null as string | null,
  hall_publisher_name: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
}

/** 后端 `data` 形状的一条批量响应（spec §D2）。 */
function outcome(over: Partial<BatchOutcome> = {}): BatchOutcome {
  return { dry_run: true, eligible: [], ineligible: [], applied: [], effects: [], ...over }
}

describe('管理订单页批量接线（Task 4a/4b：9 颗按钮）', () => {
  const mockedRequest = vi.mocked(request)

  beforeEach(() => {
    // ⚠️ **凡在断言里用到的替身，一律在这里 `mockClear()`**：
    //    `vi.hoisted()` 造出来的 `vi.fn()` 是**跨用例共享**的 ⇒ 少清一个，判据就可能靠**上一个用例
    //    的残留调用**凑出绿。实测踩过：`form.render` 原先没清 ⇒ 「改状态」组那条
    //    `toHaveBeenCalledWith('radio')` 在**整文件跑**时被更早的「指派打手」组（`openAssignDialog`
    //    的 `success` 里也 `form.render('radio')`）喂饱 ⇒ 把实现的 `form.render('radio')` 整行删掉
    //    也全绿（判据恒真）。⛔ 新增替身断言时同步在这里补一行。
    LAYUI.open.mockClear()
    LAYUI.msg.mockClear()
    LAYUI.prompt.mockClear()
    LAYUI.confirm.mockReset()
    LAYUI.confirm.mockImplementation((_text: string, options: unknown, yes?: (index: number) => void) => {
      if ((options as { title?: string } | undefined)?.title === '批量操作') yes?.(90)
      return 90
    })
    LAYUI.alert.mockClear()
    LAYUI.close.mockClear()
    // ⭐ T1：`runBatchFlow` 现在会调 `layer.load` ⇒ 它是**跨用例共享**的替身，
    //    少清一次，新用例的「只开过一次圈」就会靠上一个用例的残留调用凑出假绿。
    LAYUI.load.mockClear()
    LAYUI.form.render.mockClear()
    // 本文件今天**没有**断言 `form.on` / `form.val` / `element.init` / `laydate.render` /
    // `table.render` / `table.on` / `table.getOptions`，但它们是同一份**跨用例共享**的替身：
    // 补清一遍的成本是一行，而「后来者加了一条断言、忘了在这里清」的代价是一条**恒真**的判据。
    // ⚠️ `layer.*` 与顶层那几个（`open`/`msg`/…）是**同一个 `vi.fn()`**（见上面的字面量）⇒ 清一次就够。
    LAYUI.form.on.mockClear()
    LAYUI.form.val.mockClear()
    LAYUI.table.render.mockClear()
    LAYUI.table.on.mockClear()
    LAYUI.table.getOptions.mockClear()
    LAYUI.laydate.render.mockClear()
    LAYUI.element.init.mockClear()
    // ⚠️ 上面那句「`layer.*` 与顶层那几个是同一个 `vi.fn()`」对 **`layer.tips` 不成立**：
    //    它在替身字面量里是**单独一个** `vi.fn()`（`open`/`msg`/… 那六个是顶层函数的别名，它不在其内）
    //    ⇒ 清一次不够，得单独清。本文件今天零断言它，这条是「后来者加断言」的保险（E-0154 同族）。
    LAYUI.layer.tips.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.table.checkStatus.mockClear()
    LAYUI.table.checkStatus.mockReturnValue({ data: [] })
    // `runBatchFlow` 默认拿得到替身 `layer`；要构造加载失败的用例用 `mockRejectedValueOnce` 覆盖。
    LOAD_LAYUI.mockReset()
    LOAD_LAYUI.mockImplementation(async () => LAYUI)
    mockedRequest.mockReset()
    mockedRequest.mockResolvedValue({} as never)
    // ⭐ 默认把 `requestEnvelope` 转接到 `request` 替身（msg 空串），
    // 既有用例仍只配 `request` 就能驱动指派全流程。
    vi.mocked(requestEnvelope).mockReset()
    vi.mocked(requestEnvelope).mockImplementation(
      (async (config: unknown) => ({
        code: 0,
        msg: '',
        data: await mockedRequest(config as never),
      })) as never,
    )
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    // ⚠️ **必须在这里兜底卸载**：弹层的「保存 / 确定记录」是**文档级**捕获委托
    // （`document.addEventListener(..., true)`，因为 layui 会把节点搬出 `root`），
    // 而 `document.body.innerHTML = ''` **摘不掉文档级监听器**。用例中途断言失败时
    // （红态里必然发生）少调一次 `cleanup()`，上一个实例的监听器就会留在 document 上
    // ——下一次点击会被**两个**实例各处理一次，症状是「请求调用次数莫名多一」。
    teardown?.()
    teardown = null
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /** 当前用例挂载出来的页面卸载函数（`afterEach` 兜底用）。 */
  let teardown: (() => void) | null = null

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountOrderListPage(root)
    renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], LAYUI.table.checkStatus().data)
      .forEach((box) => { box.checked = true })
    LAYUI.table.reload.mockClear()
    teardown = cleanup
    // ⚠️ **抹掉 mount 期的那一发 `request()`**（页 mount 会取一次 `/api/auth/me`）。
    // 本文件多处判「恰好两发（dry_run + apply）」/「恰好一发（单笔）」，不抹掉会把 me 算进去。
    mockedRequest.mockClear()
    return { root, cleanup }
  }

  const rowA = { ...ROW, order_no: ORDER_A }
  const rowB = { ...ROW, order_no: ORDER_B }

  function setRows(rows: Array<{ order_no: string } & Record<string, unknown>>): void {
    LAYUI.table.checkStatus.mockReturnValue({ data: rows })
  }

  function clickToolbar(root: HTMLElement, tb: string): void {
    const btn = root.querySelector<HTMLElement>(`button[data-tb="${tb}"]`)
    expect(btn, `证据件里找不到 [data-tb=${tb}]`).not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  function click(el: Element | null, what: string): void {
    expect(el, `${what} 不存在`).not.toBeNull()
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /** 第 `n` 发 `request()` 的配置。缺了就**响亮失败**（⛔ 不用 `!` 把「请求根本没发」盖掉）。 */
  function sent(n: number): AxiosRequestConfig {
    const call = mockedRequest.mock.calls[n]
    if (!call) {
      throw new Error(`第 ${n} 发请求不存在（实际发了 ${mockedRequest.mock.calls.length} 发）`)
    }
    return call[0]
  }

  /**
   * 从页面**真实 templet** 渲一行的某一列，把整个宿主挂回 `root`（点击委托挂在 `root` 上）。
   *
   * ⚠️ 返回的是**宿主 `<div>`**、不是 `firstElementChild`：`_price` 那一格同时产出
   * 收款状态标记与金额链接，只挂第一个元素会让 `querySelector('a.editOrderPrice')` 取不到。
   */
  function attachCell(root: HTMLElement, field: string, row: Record<string, unknown>): HTMLElement {
    const cols = buildOrderCols(() => '原神', () => '', { isGroupGame: () => false })
    const col = cols.find((c) => c.field === field)
    expect(col, `列定义里找不到 field=${field}`).toBeDefined()
    const host = document.createElement('div')
    host.innerHTML = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
    expect(host.firstElementChild, `field=${field} 的 templet 没有产出节点`).not.toBeNull()
    root.appendChild(host)
    return host
  }

  /** 最近一次 `layer.prompt` 的调用（⛔ 不用 `.at(-1)`：本工程 TS lib 目标不支持它）。 */
  function lastPromptCall(): unknown[] {
    const calls = LAYUI.prompt.mock.calls
    const call = calls[calls.length - 1]
    expect(call, 'layer.prompt 没有开').toBeDefined()
    return call!
  }

  /** 「改接单价」的 prompt：回调在 `opts.yes` 里（自设 `btn` 覆盖了 layui 默认 yes），从 `layero` 取输入值。 */
  function submitPricePrompt(value: string): void {
    const opts = lastPromptCall()[0] as { yes?: (i: number, l: unknown) => void }
    expect(opts.yes, 'openPricePrompt 的 yes 回调不在 opts 里').toBeTypeOf('function')
    opts.yes!(7, { find: () => ({ val: () => value }) })
  }

  /** 「改发单价」的 prompt：回调是 `layer.prompt(opts, cb)` 的第二实参，形状 `(value, index)`。 */
  function submitHallPricePrompt(value: string): void {
    const cb = lastPromptCall()[1] as ((v: string, i: number) => void) | undefined
    expect(cb, 'openHallPricePrompt 的回调不在第二实参里').toBeTypeOf('function')
    cb!(value, 7)
  }

  function openPanel(root: HTMLElement, selector: string): HTMLElement {
    const node = root.querySelector<HTMLElement>(selector)
    expect(node, `证据件里找不到 ${selector}`).not.toBeNull()
    return node!
  }

  /**
   * 用页面自己交出的 `content` 造出弹层节点，再调它的 `success(layero, index)`——
   * 这正是真 layui 把内容插进 `.layui-layer-content` 之后走的那一步（替身不代劳插 DOM）。
   *
   * ⚠️ 节点**必须**挂进 `.layui-layer-content`：页面的 `panel()` 是**文档级优先**查找
   * （layui 会把节点搬出 `root`）⇒ 只 append 到 `root` 会让「保存」按钮的回调找不到节点。
   */
  function runLayerSuccess(id: string, index = 7): HTMLElement {
    // ⚠️ `LAYUI.open` 是 `vi.fn(() => nextIndex++)`（零形参）⇒ `mock.calls` 的元素类型被推成
    //    空元组；要取实参必须先过 `unknown`（同文件 `lastPromptCall` 用的是无参 `vi.fn()`，所以它不需要）。
    const calls = LAYUI.open.mock.calls as unknown as Array<
      [{ content: unknown; success?: (l: unknown, i: number) => void }]
    >
    const call = calls[calls.length - 1]
    expect(call, 'layer.open 没有开').toBeDefined()
    const opts = call![0]
    const dialog = document.createElement('div')
    dialog.id = id
    dialog.innerHTML = String(opts.content)
    const layer = document.createElement('div')
    layer.className = 'layui-layer'
    const content = document.createElement('div')
    content.className = 'layui-layer-content'
    layer.appendChild(content)
    content.appendChild(dialog)
    document.body.appendChild(layer)
    // `index` 是**用例注入的层号**（默认 7）：判「成功才关」的用例必须拿它当期望值——
    // `toHaveBeenCalled()` 是恒真判据，抓不到「关错层」。
    opts.success?.({ find: () => ({ 0: dialog }) }, index)
    return dialog
  }

  /**
   * 已发出的**写**请求（按 URL 片段过滤）。
   *
   * ⚠️ 指派弹层开层时会各自拉一次名单 / 账单（GET）⇒ 那些不能进「恰好两发」那类判据；
   * 本 helper 只留 `post`，并按片段点名动作，⛔ 不用「第 N 发」的位置指认。
   */
  function posted(fragment: string): AxiosRequestConfig[] {
    return mockedRequest.mock.calls
      .map((c) => c[0] as AxiosRequestConfig)
      .filter((c) => c.method === 'post' && String(c.url).includes(fragment))
  }

  /**
   * ⭐ T1：只看**弹层**的 `layer.close` 调用参数（过滤掉 `runBatchFlow` 关 loading
   * 用的那个哨兵层号）。本文件既有那些「成功才关层 / 失败不关层」判据表达的都是**弹层**语义
   * ⇒ 一律改用它；loading 自己的开/关由新加的 T1 用例断言（`LAYUI.load` / `LAYUI.close` 的哨兵值）。
   */
  function dialogCloses(): number[] {
    return LAYUI.close.mock.calls
      .map((call) => call[0] as number)
      .filter((index) => index !== LAYUI.loadIndex && index !== 90)
  }

  /**
   * 两跳 `layer.confirm` 的可控替身：第 1 跳（`confirm(text, cb)`）自动点「继续」，
   * 第 2 跳（`confirm(text, opts, yes, btn2)`）按 `second` 选那颗按钮。
   * ⚠️ 第 2 颗按钮是**第 4 个实参**（vendored layui 的 `confirm` 形状，见实现里那段注释）。
   */
  function confirmDeleteTwoHops(second: 'Y' | 'N'): void {
    LAYUI.confirm.mockImplementation((...args: unknown[]) => {
      if ((args[1] as { title?: string })?.title === '批量操作') {
        ;(args[2] as (i: number) => void)(90)
        return 90
      }
      if (typeof args[1] === 'function') {
        ;(args[1] as (i: number) => void)(11)
        return 11
      }
      const cb = (second === 'Y' ? args[2] : args[3]) as ((i: number) => void) | undefined
      cb?.(12)
      return 12
    })
  }

  /* ─────────── 1. 改接单价 changeOrderPriceBtn → openPricePrompt ─────────── */

  describe('改接单价', () => {
    it('确认同时列出另受影响的订单，并把看到的联动名单随执行原样提交', async () => {
      const effects = [
        { order_no: ORDER_A, source_order_no: '', operation: '重算合计' },
        { order_no: ORDER_B, source_order_no: '来源-乙', operation: '同步修改接单价' },
      ]
      mockedRequest.mockResolvedValueOnce(outcome({ eligible: [ORDER_A], effects }))
      mockedRequest.mockResolvedValueOnce(outcome({ dry_run: false, applied: [ORDER_A] }))
      LAYUI.confirm.mockImplementation(() => 91)
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      const [html, options, yes] = LAYUI.confirm.mock.calls[0]!
      expect(html).toContain(`单号 ${ORDER_A}（联动：重算合计）`)
      expect(html).toContain('另有 1 笔联动订单')
      expect(html).toContain(`来源单号 来源-乙（系统单号 ${ORDER_B}）：同步修改接单价`)
      expect(options).toMatchObject({ btn: ['确认处理 2 笔', '取消'] })
      expect(posted('/batch/price/').map((call) => call.data)).toEqual([
        { total_price: '50.00', order_nos: [ORDER_A], dry_run: true, confirmed_effects: [] },
      ])
      ;(yes as (index: number) => void)(91)

      await vi.waitFor(() => expect(posted('/batch/price/')).toHaveLength(2))
      expect(posted('/batch/price/')[1]?.data).toEqual({
        total_price: '50.00', order_nos: [ORDER_A], dry_run: false, confirmed_effects: effects,
      })
      cleanup()
    })

    it('不能直接操作但会联动的单显示在联动名单，附上直接操作被拦原因', async () => {
      const heldOrder = '20000000000000000003'
      const effects = [{ order_no: ORDER_B, source_order_no: '', operation: '同步修改接单价' }]
      mockedRequest.mockResolvedValueOnce(outcome({
        eligible: [ORDER_A],
        ineligible: [
          { order_no: ORDER_B, code: 1002, msg: '缺少 <改单> 条件' },
          { order_no: heldOrder, code: 1002, msg: '订单已结账' },
        ],
        effects,
      }))
      LAYUI.confirm.mockImplementation(() => 91)
      setRows([rowA, rowB, { ...ROW, order_no: heldOrder }])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      const [html, options, , cancel] = LAYUI.confirm.mock.calls[0]!
      expect(options).toMatchObject({ btn: ['确认处理 2 笔', '取消'] })
      const [applying, skipped] = String(html).split('<p><strong>不会处理 1 笔订单</strong></p>')
      expect(applying).toContain(`单号 ${ORDER_B}：同步修改接单价（不能直接操作：缺少 &lt;改单&gt; 条件）`)
      expect(skipped).toBe(`单号 ${heldOrder}：订单已结账`)
      ;(cancel as () => void)()
      await Promise.resolve()
      expect(posted('/batch/price/').map((call) => call.data.dry_run)).toEqual([true])
      cleanup()
    })

    it('全不合格即使预检返回联动项，仍无可执行名单也不写入', async () => {
      mockedRequest.mockResolvedValueOnce(outcome({
        ineligible: [{ order_no: ORDER_A, code: 1002, msg: '订单已结账' }],
        effects: [{ order_no: ORDER_B, source_order_no: '', operation: '同步修改接单价' }],
      }))
      LAYUI.confirm.mockImplementation(() => 91)
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      const [html, options, acknowledge] = LAYUI.confirm.mock.calls[0]!
      expect(html).toContain('将处理 0 笔订单')
      expect(html).toContain(`单号 ${ORDER_A}：订单已结账`)
      expect(html).not.toContain(ORDER_B)
      expect(options).toMatchObject({ btn: ['知道了'] })
      ;(acknowledge as (index: number) => void)(91)
      await Promise.resolve()
      expect(posted('/batch/price/').map((call) => call.data.dry_run)).toEqual([true])
      cleanup()
    })

    it('预检缺少联动名单时不显示执行按钮、不写入', async () => {
      mockedRequest.mockResolvedValueOnce(outcome({ eligible: [ORDER_A], effects: undefined }))
      setRows([rowA])
      const { root, cleanup } = await mountPage()
      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(
        '未能取得联动订单名单，请刷新页面后重试', { time: 1500 },
      ))
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      expect(posted('/batch/price/').map((call) => call.data.dry_run)).toEqual([true])
      cleanup()
    })

    it('工具栏勾 2 笔 ⇒ 两步式批量端点（dry_run → apply），成功文案「已处理 N 笔订单」', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: '/api/business/orders/batch/price/',
        data: { total_price: '50.00', order_nos: [ORDER_A, ORDER_B], dry_run: true, confirmed_effects: [] },
      })
      expect(sent(1)).toMatchObject({
        method: 'post',
        url: '/api/business/orders/batch/price/',
        data: { total_price: '50.00', order_nos: [ORDER_A, ORDER_B], dry_run: false, confirmed_effects: [] },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('工具栏勾 1 笔 ⇒ 仍走批量端点（勾 1 笔是长度 1 的批量），成功文案沿用原文案', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] }),
        )
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0).url).toBe('/api/business/orders/batch/price/')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('接单价已更新', { time: 1500 }))
      cleanup()
    })

    it('空 / NaN ⇒ 提示且零请求（既有校验保留）', async () => {
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('   ')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('请输入合法金额', { time: 1500 }))
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('行内点接单价金额链接 ⇒ 仍打**单笔**端点（默认目标 = single）', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachCell(root, '_price', { ...rowA, total_price: '88.00' })
      const link = el.querySelector<HTMLElement>('a.editOrderPrice')
      click(link, 'a.editOrderPrice')
      submitPricePrompt('99.00')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/price/`,
        data: { total_price: '99.00' },
      })
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('接单价已更新', { time: 1500 }),
      )
      cleanup()
    })

    /**
     * ⭐ R9 #13（spec §3.1 对照表下方那条 ⚠️）：改接单价是**既有的「先关后发」形态**
     * （`openPricePrompt` 的 `yes` 在发请求**之前**就 `layer.close`）——R9 **只要求有判据**，
     * ⛔ 不要求把它改成「成功才关」。
     *
     * ⇒ 判据是「确认 ⇒ `layer.close(index)` 恰好一次 ＋ 关层**早于**第一发请求」；
     * **顺序那半条是必须的**：把 `layer.close` 挪到 `.then()` 里时，「恰好一次」那半条仍绿。
     * ⛔ 这里**不写**「失败不关层」——按它的既定形态，失败时层也已经关了（写那条会与实现相反）。
     */
    it('D1-#13 改接单价：确认 ⇒ `layer.close` **恰好一次**，且**早于** `/price/` 请求（它是有意的「先关后发」形态）', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('99.00')

      // ⭐ 请求侧判据按 spec §3.1「确认 ⇒ `layer.close(index)` 恰好一次 **＋ 请求恰好一发**」写**准**：
      //    这一条走的是**批量**两步式（勾 2 笔）⇒ 恰好两发（`dry_run` + `apply`），不是「>0」。
      //    ⛔ 旧写法 `toBeGreaterThan(0)` 放行「多发 / 重复执行」；`toHaveLength(2)` 才钉得住发数。
      await vi.waitFor(() => expect(posted('/price/')).toHaveLength(2))
      // ⭐ T1 起 `LAYUI.close` 还会被 `runBatchFlow` 用来关 loading ⇒ 这里只看**弹层**那一关。
      expect(dialogCloses()).toEqual([7])
      const closeOrder = LAYUI.close.mock.invocationCallOrder[0]!
      const reqOrder = mockedRequest.mock.invocationCallOrder[0]!
      expect(closeOrder).toBeLessThan(reqOrder)
      cleanup()
    })

    /**
     * ⭐ R9 #13 的**单笔**档（spec §3.1「请求恰好一发」的字面口径）：行内 `.editOrderPrice`
     * 走单笔 `/price/`，确认 ⇒ `layer.close(7)` 恰好一次、`/price/` **恰好一发**、且关层早于请求。
     *
     * ⚠️ 与上一条互补：上一条是批量（`dry_run` + `apply` = 两发），这一条是单笔（一发）——
     * 发数按各自档位的实际值钉（批量 `toHaveLength(2)` / 单笔 `toHaveLength(1)`）。
     */
    it('D1-#13b 改接单价（单笔支）：确认 ⇒ `layer.close(7)` 恰好一次 + `/price/` **恰好一发**，且关层早于请求', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachCell(root, '_price', { ...rowA, total_price: '88.00' })
      click(el.querySelector<HTMLElement>('a.editOrderPrice'), 'a.editOrderPrice')
      submitPricePrompt('99.00')

      await vi.waitFor(() =>
        expect(posted(`/api/business/orders/${ORDER_A}/price/`)).toHaveLength(1),
      )
      expect(LAYUI.close).toHaveBeenCalledTimes(1)
      expect(LAYUI.close).toHaveBeenCalledWith(7)
      // 再断一次「发数」：`waitFor` 只等到 1 发，若之后又多出一发（重复提交）这里会红。
      expect(posted(`/api/business/orders/${ORDER_A}/price/`)).toHaveLength(1)
      const closeOrder = LAYUI.close.mock.invocationCallOrder[0]!
      const reqOrder = mockedRequest.mock.invocationCallOrder[0]!
      expect(closeOrder).toBeLessThan(reqOrder)
      cleanup()
    })
  })

  /* ─────────── 2. 改发单价 changeOrderThrowPriceBtn → openHallPricePrompt ─────────── */

  describe('改发单价', () => {
    function renderVisibleRows(root: HTMLElement, rows: Array<typeof rowA>, page = 1): HTMLInputElement[] {
      return renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], rows, page)
    }

    function confirmationButton(call: number, button: 2 | 3): (index: number) => void {
      return LAYUI.confirm.mock.calls[call]?.[button] as (index: number) => void
    }

    it('重新筛选后只提交当前页的可见勾选，核对来源和系统单号后才写', async () => {
      LAYUI.confirm.mockImplementation(() => 91)
      const { root, cleanup } = await mountPage()
      const [old] = renderVisibleRows(root, [rowA])
      old!.checked = true
      clickToolbar(root, 'seachBtnOk')
      const [hidden, shown] = renderVisibleRows(root, [rowA, { ...rowB, source_order_no: '来源<单>' }])
      hidden!.checked = true
      hidden!.closest('tr')!.hidden = true
      shown!.checked = true
      // 模拟 layui 内部缓存仍有旧值，批量选单按当前表中能看到的框取。
      setRows([rowA, rowB])
      mockedRequest.mockResolvedValueOnce(outcome({ eligible: [ORDER_B] }))
      mockedRequest.mockResolvedValueOnce(outcome({ dry_run: false, applied: [ORDER_B] }))

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('0')

      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      expect(posted('/batch/hall-price/').map((call) => call.data)).toEqual([
        { price: '0', order_nos: [ORDER_B], dry_run: true },
      ])
      const [body, options] = LAYUI.confirm.mock.calls[0]!
      expect(body).toContain('批量改发单价为 0 · 将处理 1 笔订单')
      expect(body).toContain(`来源单号 来源&lt;单&gt;（系统单号 ${ORDER_B}）`)
      expect(body).not.toContain(ORDER_A)
      expect(options).toMatchObject({ btn: ['确认处理 1 笔', '取消'] })

      confirmationButton(0, 2)(91)
      await vi.waitFor(() => expect(posted('/batch/hall-price/')).toHaveLength(2))
      expect(posted('/batch/hall-price/')[1]?.data).toEqual({ price: '0', order_nos: [ORDER_B], dry_run: false })
      expect(LAYUI.close).toHaveBeenCalledWith(91)
      cleanup()
    })

    it('部分不合格时分别给出实际要改和不会改的单，点取消只发送预检', async () => {
      LAYUI.confirm.mockImplementation(() => 91)
      const { root, cleanup } = await mountPage()
      renderVisibleRows(root, [rowA, rowB]).forEach((box) => { box.checked = true })
      mockedRequest.mockResolvedValueOnce(outcome({
        eligible: [ORDER_B],
        ineligible: [{ order_no: ORDER_A, code: 1002, msg: '已结账' }],
      }))

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('0')
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      const body = String(LAYUI.confirm.mock.calls[0]?.[0])
      expect(body).toContain(`将处理 1 笔订单</strong></p>单号 ${ORDER_B}`)
      expect(body).toContain(`不会处理 1 笔订单</strong></p>单号 ${ORDER_A}：已结账`)
      confirmationButton(0, 3)(91)
      await Promise.resolve()
      expect(posted('/batch/hall-price/').map((call) => call.data.dry_run)).toEqual([true])
      cleanup()
    })

    it('等待确认时列表若开始重取，旧确认按钮无法写入', async () => {
      LAYUI.confirm.mockImplementation(() => 91)
      const { root, cleanup } = await mountPage()
      renderVisibleRows(root, [rowA])[0]!.checked = true
      mockedRequest.mockResolvedValueOnce(outcome({ eligible: [ORDER_A] }))

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('0')
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      const options = LAYUI.table.render.mock.lastCall?.[0] as { before: () => void }
      options.before()
      confirmationButton(0, 2)(91)

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(
        '订单列表或勾选已变化，请重新勾选后再操作', { time: 1500 },
      ))
      expect(posted('/batch/hall-price/').map((call) => call.data.dry_run)).toEqual([true])
      cleanup()
    })

    it('工具栏勾 2 笔 ⇒ 批量端点 `/batch/hall-price/`，payload 恰好 `{ price }` + 两个批量键', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('50%')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      // ⚠️ 用例名声称「恰好」⇒ 必须精确 `toEqual`（`toMatchObject` 会放行夹带的额外键）。
      expect(sent(0)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/hall-price/',
        data: { price: '50%', order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(sent(1)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/hall-price/',
        data: { price: '50%', order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('工具栏勾 1 笔 ⇒ 批量端点 + 成功文案「发单价已更新」', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] }),
        )
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('7.00')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0).url).toBe('/api/business/orders/batch/hall-price/')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('发单价已更新', { time: 1500 }))
      cleanup()
    })

    it('空 ⇒ 提示且零请求（既有校验保留）', async () => {
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderThrowPriceBtn')
      submitHallPricePrompt('  ')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('发单价不能为空', { time: 1500 }))
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('行内点 `.changeThrowFeeEdit` ⇒ 仍打**单笔** `/hall-price/`', async () => {
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_throw', {
        ...rowA,
        hall_price: '66.00',
        hall_publisher_name: '',
      })
      const link = cell.querySelector<HTMLElement>('a.changeThrowFeeEdit')
      expect(link, '发单价列没产出 a.changeThrowFeeEdit').not.toBeNull()
      click(link, '.changeThrowFeeEdit')
      submitHallPricePrompt('50%')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/hall-price/`,
        data: { price: '50%' },
      })
      cleanup()
    })

    /**
     * ⭐ R9 #2 的**单笔支**（spec §3.1 对照表 #2 把「成功关」写在 `order-orderlist.ts:3573` 那一支）：
     * 上面两条批量档只钉批量 `onDone` 的 `close`；单笔 `.then(() => layer.close(index))` 今天**零判据**。
     * 层号 = `submitHallPricePrompt` 注入的 7（E-0024：期望值字面量）。
     */
    it('D1-#2a 改发单价（单笔支）：成功 ⇒ 关**它自己那层**（用例注入 7）', async () => {
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_throw', {
        ...rowA,
        hall_price: '66.00',
        hall_publisher_name: '',
      })
      const link = cell.querySelector<HTMLElement>('a.changeThrowFeeEdit')
      expect(link, '发单价列没产出 a.changeThrowFeeEdit').not.toBeNull()
      click(link, '.changeThrowFeeEdit')
      LAYUI.close.mockClear()
      submitHallPricePrompt('50%')

      await vi.waitFor(() =>
        expect(posted(`/api/business/orders/${ORDER_A}/hall-price/`)).toHaveLength(1),
      )
      await vi.waitFor(() => expect(LAYUI.close).toHaveBeenCalledWith(7))
      cleanup()
    })

    /** ⭐ R9 #2 失败方向：后端拒 ⇒ 只弹 `msg` 原文、**不关层**（用户能在原层改完重试）。 */
    it('D1-#2b 改发单价（单笔支）：失败 ⇒ 弹后端 `msg`、**不关层**', async () => {
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes(`${ORDER_A}/hall-price/`)) {
          throw new BizError(1002, '发单价改失败甲')
        }
        return {}
      }) as never)
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_throw', {
        ...rowA,
        hall_price: '66.00',
        hall_publisher_name: '',
      })
      const link = cell.querySelector<HTMLElement>('a.changeThrowFeeEdit')
      expect(link, '发单价列没产出 a.changeThrowFeeEdit').not.toBeNull()
      click(link, '.changeThrowFeeEdit')
      LAYUI.close.mockClear()
      submitHallPricePrompt('50%')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('发单价改失败甲', { time: 1500 }))
      expect(LAYUI.close).not.toHaveBeenCalled()
      cleanup()
    })
  })

  /* ─────────── 3. 收款状态 setOrderJSBtn → openPaymentStatusDialog ─────────── */

  describe('收款状态', () => {
    /** 打开 `.setDiv` 面板、勾「已结算」、点保存。 */
    function saveSettled(root: HTMLElement): void {
      const panel = openPanel(root, '.setDiv')
      panel.querySelector<HTMLInputElement>('input[name="jsst"][value="已结算"]')!.checked = true
      click(panel.querySelector('[lay-filter="saveSettbtn"]'), '[lay-filter=saveSettbtn]')
    }

    it('工具栏勾 2 笔 ⇒ 批量端点 `/batch/payment-status/`，body 送码', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setOrderJSBtn')
      saveSettled(root)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: '/api/business/orders/batch/payment-status/',
        data: { payment_status: 'SETTLED', order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('工具栏勾 1 笔 ⇒ 批量端点 + 成功文案「收款状态已更新」', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] }),
        )
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setOrderJSBtn')
      saveSettled(root)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0).url).toBe('/api/business/orders/batch/payment-status/')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('收款状态已更新', { time: 1500 }))
      cleanup()
    })

    it('未选 radio ⇒ `layer.alert("请选择一种结算状态！", { title: "提示" })`，零请求（既有校验保留）', async () => {
      // ⚠️ 开层时**批量支无条件传 `current=''`**（不是读这一行的 `payment_status`）⇒ 这一笔的
      //    `payment_status` 取什么值都落「未选 radio」分支，该字段与判据**无关**。
      setRows([{ ...rowA, payment_status: '' }])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setOrderJSBtn')
      const panel = openPanel(root, '.setDiv')
      click(panel.querySelector('[lay-filter="saveSettbtn"]'), '[lay-filter=saveSettbtn]')

      expect(LAYUI.alert).toHaveBeenCalledWith('请选择一种结算状态！', { title: '提示' })
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('行内点 `.jsSta` ⇒ 仍打**单笔** `/payment-status/`', async () => {
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_price', {
        ...rowA,
        total_price: '88.00',
        payment_status: 'SETTLING',
      })
      click(cell.querySelector('a.jsSta') ?? cell, '.jsSta')
      saveSettled(root)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/payment-status/`,
        data: { payment_status: 'SETTLED' },
      })
      cleanup()
    })

    /**
     * ⭐ R9 #3 的**单笔支**（spec §3.1 对照表 #3 把「单笔成功关」写在 `order-orderlist.ts:3661` 那一支）：
     * 批量档在 `phase1.spec.ts` 已钉（`close(7)`）；单笔 `.then` 里那句 `layer.close(index)` 今天**零判据**。
     *
     * ⚠️ 这一支的层号取自 `layer.open` 的**返回值**（`settLayerIndex = layer.open(...)`，不是 `success` 的
     * 第二参）⇒ 用 `mockReturnValueOnce(4)` 把这一发的返回值钉成字面量 4，再断 `close(4)`。
     */
    it('D1-#3a 收款状态（单笔支）：成功 ⇒ 关**它自己那层**（用例注入 4）', async () => {
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_price', {
        ...rowA,
        total_price: '88.00',
        payment_status: 'SETTLING',
      })
      LAYUI.open.mockReturnValueOnce(4)
      click(cell.querySelector('a.jsSta') ?? cell, '.jsSta')
      LAYUI.close.mockClear()
      saveSettled(root)

      await vi.waitFor(() =>
        expect(posted(`/api/business/orders/${ORDER_A}/payment-status/`)).toHaveLength(1),
      )
      await vi.waitFor(() => expect(LAYUI.close).toHaveBeenCalledWith(4))
      cleanup()
    })

    /** ⭐ R9 #3 失败方向：后端拒 ⇒ 只弹 `msg` 原文、**不关层**。 */
    it('D1-#3b 收款状态（单笔支）：失败 ⇒ 弹后端 `msg`、**不关层**', async () => {
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes(`${ORDER_A}/payment-status/`)) {
          throw new BizError(1002, '收款状态改失败甲')
        }
        return {}
      }) as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_price', {
        ...rowA,
        total_price: '88.00',
        payment_status: 'SETTLING',
      })
      LAYUI.open.mockReturnValueOnce(4)
      click(cell.querySelector('a.jsSta') ?? cell, '.jsSta')
      LAYUI.close.mockClear()
      saveSettled(root)

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('收款状态改失败甲', { time: 1500 }),
      )
      expect(LAYUI.close).not.toHaveBeenCalled()
      cleanup()
    })
  })

  /* ─────────── 4. 改类型 changetypesBtn → openTypeDialog（证据件 `.typebox`） ─────────── */

  describe('改类型', () => {
    function saveType(root: HTMLElement, value: '0' | '1'): void {
      const panel = openPanel(root, '.typebox')
      panel.querySelector<HTMLInputElement>(
        `input[name="selbussType"][value="${value}"]`,
      )!.checked = true
      click(panel.querySelector('.settypes'), '.settypes')
    }

    it('工具栏勾 2 笔 ⇒ 批量端点 `/batch/types/`，body 只有 `order_type` + 两个批量键', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      saveType(root, '0')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      // ⚠️ 用例名声称「只有」⇒ 必须精确 `toEqual`（批量 payload 夹带 `refund_fee` 时
      //    `toMatchObject` 会放行，只有精确比较能红）。
      expect(sent(0)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/types/',
        data: { order_type: 'BOOST', order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(sent(1)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/types/',
        data: { order_type: 'BOOST', order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('工具栏勾 1 笔 ⇒ 批量端点 + 成功文案「订单类型已更新」', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] }),
        )
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      saveType(root, '1')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(sent(0)).toMatchObject({
        url: '/api/business/orders/batch/types/',
        data: { order_type: 'COMPANION' },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('订单类型已更新', { time: 1500 }))
      cleanup()
    })

    it('未选类型 ⇒ 提示「请选择业务类型」，零请求（既有校验保留）', async () => {
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      const panel = openPanel(root, '.typebox')
      click(panel.querySelector('.settypes'), '.settypes')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('请选择业务类型', { time: 1500 }))
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })
  })

  /* ─────────── 5. 记退款 refundOrderBtn → openRefundDialog（⭐ 每笔一个幂等键） ─────────── */

  describe('记退款', () => {
    function fill(panel: HTMLElement, amount: string, reason: string): void {
      panel.querySelector<HTMLInputElement>('#refundFee')!.value = amount
      panel.querySelector<HTMLInputElement>('#refundRemark')!.value = reason
    }

    function clickSave(panel: HTMLElement): void {
      click(panel.querySelector('.saveReFundOrderBtn'), '.saveReFundOrderBtn')
    }

    it('工具栏勾 2 笔 ⇒ 批量端点；`request_ids` 与 `order_nos` 等长同序（每笔一个键）', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'refundOrderBtn')
      const panel = openPanel(root, '.setrefundOrderLog')
      // ⭐（D-0312）批量入口**不预填**（多张单没有「本单当前值」）。
      expect(panel.querySelector<HTMLInputElement>('#refundFee')!.value).toBe('')
      fill(panel, '50.00', '退货差价')
      clickSave(panel)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = sent(0)
      expect(first).toMatchObject({
        method: 'post',
        url: '/api/business/orders/batch/refunds/',
        data: {
          refund_total: '50.00',
          reason: '退货差价',
          order_nos: [ORDER_A, ORDER_B],
          dry_run: true,
        },
      })
      const firstIds = first.data.request_ids as string[]
      expect(firstIds).toHaveLength(2)
      expect(firstIds[0]).not.toBe(firstIds[1])
      for (const id of firstIds) {
        expect(typeof id).toBe('string')
        expect(id.length).toBeGreaterThan(0)
        expect(id.length).toBeLessThanOrEqual(64)
      }
      const second = sent(1)
      expect(second).toMatchObject({
        url: '/api/business/orders/batch/refunds/',
        data: { order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      expect(second.data.request_ids).toEqual(firstIds)
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('勾 >1 笔 ⇒ 在金额框附近多渲染一句口径说明（逐字）', async () => {
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()
      clickToolbar(root, 'refundOrderBtn')

      const panel = openPanel(root, '.setrefundOrderLog')
      expect(panel.textContent).toContain(BATCH_REFUND_NOTE)
      // 形状沿用同弹层既有的 `.layui-elem-quote`（值取自证据件那句，不新造配色）。
      const notes = panel.querySelectorAll('blockquote.layui-elem-quote')
      expect(notes).toHaveLength(2)
      expect(notes[1]!.getAttribute('style')).toBe('font-size:16px;color:#ff0000')
      cleanup()
    })

    it('恰好 1 笔 ⇒ **一个字都不加**（保持与对标一致）', async () => {
      setRows([rowA])
      const { root, cleanup } = await mountPage()
      clickToolbar(root, 'refundOrderBtn')

      const panel = openPanel(root, '.setrefundOrderLog')
      expect(panel.textContent).not.toContain(BATCH_REFUND_NOTE)
      expect(panel.querySelectorAll('blockquote.layui-elem-quote')).toHaveLength(1)
      expect(panel.querySelector<HTMLInputElement>('#refundFee')!.value).toBe('')
      cleanup()
    })

    it('部分不合格 + 点「继续处理」⇒ 第 2 发只带合格子集（合格项在**下标 1**），`request_ids` 按下标对齐', async () => {
      // ⭐ 合格项刻意放在**下标 1**：只有这样「按单号查表」（正确）与「取全量列表前 N 个」（错位）
      //    才**不同真** ⇒ 这条能钉住串号。
      mockedRequest
        .mockResolvedValueOnce(
          outcome({
            eligible: [ORDER_B],
            ineligible: [{ order_no: ORDER_A, code: 1007, msg: '订单当前状态不允许退款' }],
          }),
        )
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_B], applied: [ORDER_B] }),
        )
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'refundOrderBtn')
      const panel = openPanel(root, '.setrefundOrderLog')
      fill(panel, '50.00', '退货差价')
      clickSave(panel)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const firstIds = sent(0).data.request_ids as string[]
      const second = sent(1)
      expect(second.data.order_nos).toEqual([ORDER_B])
      // ⛔ 与 `order_nos` 必须等长且同序（后端会拒不等长）。
      expect(second.data.request_ids).toEqual([firstIds[1]])
      // ⭐ 串号的正面判据：⛔ 不许把「全量列表前 N 个」的键（那会把 A 的键退到 B 上）当答案。
      expect(second.data.request_ids).not.toEqual([firstIds[0]])
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已记录退款', { time: 1500 }))
      cleanup()
    })

    it('部分不合格 + 点「继续处理」⇒ 合格项在**下标 0**（首位）时第 2 发仍只带该笔、键取它的', async () => {
      // 与上一条互补：保留「合格项在首位」的覆盖（两条一起才把「等长同序」钉全）。
      mockedRequest
        .mockResolvedValueOnce(
          outcome({
            eligible: [ORDER_A],
            ineligible: [{ order_no: ORDER_B, code: 1007, msg: '订单当前状态不允许退款' }],
          }),
        )
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] }),
        )
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'refundOrderBtn')
      const panel = openPanel(root, '.setrefundOrderLog')
      fill(panel, '50.00', '退货差价')
      clickSave(panel)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const firstIds = sent(0).data.request_ids as string[]
      const second = sent(1)
      expect(second.data.order_nos).toEqual([ORDER_A])
      // ⛔ 与 `order_nos` 必须等长且同序（后端会拒不等长）。
      expect(second.data.request_ids).toEqual([firstIds[0]])
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已记录退款', { time: 1500 }))
      cleanup()
    })

    it('批量 apply 撞 `1010` ⇒ 弹后端 `msg` 原文、**不关层、不 reload**（⭐ 与行内单笔**有意不同**的处置，spec §9-R11）', async () => {
      // §9-R11 裁定：批量 `1010` = **整批回滚**（本批未做任何修改）⇒ 单笔那套「关层 + reload」
      // 的前提（「这一笔已落账」）不成立 ⇒ 走 §5.2 通用失败口径。⛔ 别当漏改补齐。
      const BACKEND_MSG = '该退款请求已处理，请勿重复提交'
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockRejectedValueOnce(new BizError(1010, BACKEND_MSG))
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'refundOrderBtn')
      const panel = openPanel(root, '.setrefundOrderLog')
      fill(panel, '50.00', '退货差价')
      clickSave(panel)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(BACKEND_MSG, { time: 1500 }))
      // ⛔ 失败后**不换键重发**：第二发的键与第一发逐笔相同（「重试」没被偷换成「新的一笔」）。
      expect(sent(1).data.request_ids).toEqual(sent(0).data.request_ids)
      // §9-R11：⛔ 不套用单笔的「关层 + reload」。
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      // ⛔ 只发该发的两发（dry_run + 一次 apply）——失败后不许多发。
      expect(mockedRequest).toHaveBeenCalledTimes(2)
      cleanup()
    })

    it('行内点退款金额链接 ⇒ 仍打**单笔** `/refunds`（`request_id` 一个）', async () => {
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_refund', { ...rowA, refund_fee: '30.00' })
      const link = cell.querySelector<HTMLElement>('a.refundFee')
      click(link, 'a.refundFee')
      const panel = openPanel(root, '.setrefundOrderLog')
      fill(panel, '50.00', '退货差价')
      clickSave(panel)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(sent(0)).toMatchObject({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/refunds`,
        data: { refund_total: '50.00', reason: '退货差价', request_id: expect.any(String) },
      })
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('已记录退款', { time: 1500 }),
      )
      cleanup()
    })
  })

  /* ─────────── 6. layui 加载失败的出口（F5：⛔ 不再静默只写 console） ─────────── */

  describe('layui 加载失败', () => {
    it('一次性 `loadLayui` reject ⇒ 页面弹一句后端/加载错误原文、零请求、确认层未开', async () => {
      const LOAD_ERR = 'layui 脚本加载失败（测试构造）'
      LOAD_LAYUI.mockRejectedValueOnce(new Error(LOAD_ERR))
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()
      // mount 期可能有过提示（筛选警告之类）⇒ 只判「这一次点击后**恰好**弹了这一句」。
      LAYUI.msg.mockClear()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      // 注入的页面 `msg` 收到**一句非空**（缺省那条路会退到 `logError` ⇒ 页面上零 UI）。
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledTimes(1))
      const [text] = LAYUI.msg.mock.calls[0] as [string, unknown]
      expect(text).toBe(LOAD_ERR)
      expect(mockedRequest).not.toHaveBeenCalled()
      // 批量确认层没开（`runBatchFlow` 在拿到 `layer` 之前就返回了）。
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      expect(LAYUI.open).not.toHaveBeenCalled()
      cleanup()
    })
  })

  /* ═══════════════════════════════════════════════════════════════════════
     以下 4 组接的是 `setRecMan` / `changeStaBtn` /
     `delOrderBtn` / `builderSellEmpOrderBtn`。上面几组是其余 5 颗，
     ⛔ 不改写那几条。判据一律打在**真出口**（`request()` 的 URL / body、
     真 `runBatchFlow` 的两步式）与**真 DOM 交互**上。
     ═══════════════════════════════════════════════════════════════════════ */

  /* ─────────── 7. 指派打手 setRecMan → openAssignDialog（批量） ─────────── */

  describe('指派打手', () => {
    /**
     * 在弹层里放一个**已勾上**的候选人 checkbox。
     *
     * ⚠️ 本组判的是「提交走哪条端点、载荷是什么」，不是名单渲染（那在
     * `order-orderlist-phase1.spec.ts` 里覆盖）⇒ 直接放一个勾上的 checkbox，不依赖名单接口。
     */
    function pickUser(dialog: HTMLElement, id: string): void {
      const input = document.createElement('input')
      input.type = 'checkbox'
      input.name = 'userList'
      input.value = id
      input.checked = true
      dialog.appendChild(input)
    }

    it('工具栏勾 2 笔 ⇒ 弹层照开；提交**恰好** `{user_id}` + 两个批量键（批量三个初值一律空）', async () => {
      // ⚠️ 工具栏那颗 `setRecMan` 是**按节点摘掉**的静态 DOM（`applyOrderListPermissionGates`）；
      //    本文件默认 `fetchMe()` → `undefined` ⇒ 它不在 DOM 里。给管理员身份让那颗按钮渲染。
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/assign/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setRecMan')
      // ⛔ 勾 >1 笔**不再**提示「尚未开放」（它是真批量）——弹层照开。
      expect(LAYUI.msg).not.toHaveBeenCalledWith('批量操作尚未开放，请只勾选一笔订单', {
        time: 1500,
      })
      const dialog = runLayerSuccess('tt-order-assign')
      pickUser(dialog, '7')
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() => expect(posted('/batch/assign/')).toHaveLength(2))
      // ⚠️ 「恰好」⇒ 必须精确 `toEqual`（`toMatchObject` 会放行夹带的键）：
      //    批量没有「本单」⇒ `currentTaker` / `orderProject` / `orderHallPrice` 一律传空，
      //    载荷里**不许**出现 `price` / `project` / `hours`（spec §5.2）。
      expect(posted('/batch/assign/')[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/assign/',
        data: { user_id: 7, order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(posted('/batch/assign/')[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/assign/',
        data: { user_id: 7, order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('「无权查看」**任一行**命中 ⇒ 只提示、不开层、零请求（fail-closed）', async () => {
      // 同上：那颗按钮按节点渲染 ⇒ 给管理员身份它才在 DOM 里。
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      // 命中项刻意放在**第 2 行**：只有「任一行命中」这一支会红，只看第一行的写法会漏。
      setRows([rowA, { ...rowB, taker_name: '无权查看' }])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setRecMan')

      expect(LAYUI.open).not.toHaveBeenCalled()
      // 文案逐字取 spec §9（「无权查看接手人」）——⛔ 不用 `stringContaining` 放宽。
      expect(LAYUI.msg).toHaveBeenCalledWith('无权查看接手人', { time: 1500 })
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * ⭐ **已被代练通接手（远端 12/13/14/15）的单，前端不提供指派入口**
     * （要换人必须先到代练通协商撤销）。判据只看行数据里的 `platform_push.remote_status`，
     * ⛔ 不看本地订单状态名。
     */
    const TAKEN_PUSH = {
      platform: 10,
      serial_no: 'SYNTH-SERIAL-1',
      state: 'sent',
      remote_status: 12,
      remote_cancel_status: 10,
      is_active: true,
      price: '5.00',
    }

    it('行内 `a.jsr`：远端 12（已接手）⇒ 只提示那句、不开层、零请求', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_rec', {
        ...rowA,
        taker_name: 'taker1',
        project_text: '',
        platform_push: TAKEN_PUSH,
      })
      const link = cell.querySelector<HTMLElement>('a.jsr')
      expect(link, '接手人列没产出 a.jsr').not.toBeNull()
      // 判据由渲染期带过来（不可见属性），点击处理不重新判状态
      expect(link!.dataset.pushTaken).toBe('1')
      click(link, 'a.jsr')
      expect(LAYUI.open).not.toHaveBeenCalled()
      // 文案逐字（业务方原话）——⛔ 不用 `stringContaining` 放宽
      expect(LAYUI.msg).toHaveBeenCalledWith(ASSIGN_PLATFORM_TAKEN_MESSAGE, { time: 1500 })
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('批量指派：勾选里**有一张**已被接手（命中在第 2 行）⇒ 整体拒绝、不开层、零请求', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([rowA, { ...rowB, platform_push: TAKEN_PUSH }])
      const { root, cleanup } = await mountPage()
      clickToolbar(root, 'setRecMan')
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(LAYUI.msg).toHaveBeenCalledWith(ASSIGN_PLATFORM_TAKEN_MESSAGE, { time: 1500 })
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('本地已关闭（`state=closed`）的推送行**不算**被接手 ⇒ 撤回来的单仍能换人（照常开层）', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_rec', {
        ...rowA,
        taker_name: 'taker1',
        project_text: '',
        // 远端码还停在 12，但那张平台单已经删了（closed）⇒ 不该继续挡指派
        platform_push: { ...TAKEN_PUSH, state: 'closed', is_active: false },
      })
      const link = cell.querySelector<HTMLElement>('a.jsr')
      expect(link!.dataset.pushTaken).toBeUndefined()
      click(link, 'a.jsr')
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      cleanup()
    })

    it('行内点 `a.jsr` ⇒ 仍打**单笔** `/assign/`（行内入口一字不改）', async () => {
      // ⚠️ 本文件默认把 `fetchMe()` 桩成 `undefined`（⇒ 六个入口全 false、行内 `.jsr` **不渲染**）。
      //    这一条要驱动行内那支 ⇒ 显式给一个管理员身份（`setOrderListPermissions` 短路）。
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      // `project_text` 留空 ⇒ 载荷里不带 `project`（「恰好」判据的两侧都干净）。
      const cell = attachCell(root, '_rec', { ...rowA, taker_name: 'taker1', project_text: '' })
      const link = cell.querySelector<HTMLElement>('a.jsr')
      expect(link, '接手人列没产出 a.jsr').not.toBeNull()
      click(link, 'a.jsr')

      const dialog = runLayerSuccess('tt-order-assign')
      pickUser(dialog, '9')
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() => expect(posted(`${ORDER_A}/assign/`)).toHaveLength(1))
      expect(posted(`${ORDER_A}/assign/`)[0]).toEqual({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/assign/`,
        data: { user_id: 9 },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('指派成功', { time: 1500 }))
      cleanup()
    })

    /**
     * ⭐ R9 #14（spec §7.1 第 5 条）：指派保存是**成功才关**（`submitAssign` 的单笔支）。
     * 层号用 `runLayerSuccess` 注入的 6（⛔ 不用恒真的 `toHaveBeenCalled()`）。
     */
    it('D1-#14a 指派保存：成功 ⇒ 关**指派层**（用例注入 6）', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_rec', { ...rowA, taker_name: 'taker1', project_text: '' })
      click(cell.querySelector<HTMLElement>('a.jsr'), 'a.jsr')

      const dialog = runLayerSuccess('tt-order-assign', 6)
      pickUser(dialog, '9')
      LAYUI.close.mockClear()
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() => expect(posted(`${ORDER_A}/assign/`)).toHaveLength(1))
      await vi.waitFor(() => expect(LAYUI.close).toHaveBeenCalledWith(6))
      cleanup()
    })

    /** ⭐ R9 #14b：失败 ⇒ 弹后端 `msg` 原文、**不关层**（用户能在原层改）。 */
    it('D1-#14b 指派保存：失败 ⇒ 弹后端 `msg`、**不关层**', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes(`${ORDER_A}/assign/`)) {
          throw new BizError(1002, '指派失败甲')
        }
        return {}
      }) as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_rec', { ...rowA, taker_name: 'taker1', project_text: '' })
      click(cell.querySelector<HTMLElement>('a.jsr'), 'a.jsr')

      const dialog = runLayerSuccess('tt-order-assign', 6)
      pickUser(dialog, '9')
      LAYUI.close.mockClear()
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('指派失败甲', { time: 1500 }))
      expect(LAYUI.close).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * ⭐ R9 #14 的**批量支**（spec §7.1 #5 的杀法明写「把批量 `onDone` 里那句 `layer.close` 删掉
     * ⇒ 成功支红」）：上面 #14a / #14b 只盖了**单笔支**，批量 `onDone` 里的 `layer.close(index)`
     * （`submitAssign` 的 `batchTarget.kind === 'batch'` 那一支）今天**零判据**。
     *
     * ⚠️ 层号仍是 `runLayerSuccess` 注入给 `success` 的那个（`submitAssign(index)` 收的就是它）
     * ⇒ 这里注入 5 并断 `close(5)`（E-0024：期望值字面量，⛔ 不用 `toHaveBeenCalled()`）。
     */
    it('D1-#14c 指派保存（批量支）：成功 ⇒ 关**指派层**（用例注入 5）', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/assign/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setRecMan')
      const dialog = runLayerSuccess('tt-order-assign', 5)
      pickUser(dialog, '7')
      LAYUI.close.mockClear()
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() => expect(posted('/batch/assign/')).toHaveLength(2))
      await vi.waitFor(() => expect(LAYUI.close).toHaveBeenCalledWith(5))
      cleanup()
    })

    /** ⭐ R9 #14 批量支的失败方向：校验发（`dry_run`）被拒 ⇒ 弹后端 `msg`、**不关层**。 */
    it('D1-#14d 指派保存（批量支）：失败 ⇒ 弹后端 `msg`、**不关层**', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/assign/') && config.data?.dry_run === true) {
          throw new BizError(1002, '批量指派失败甲')
        }
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setRecMan')
      const dialog = runLayerSuccess('tt-order-assign', 5)
      pickUser(dialog, '7')
      LAYUI.close.mockClear()
      click(dialog.querySelector('[data-act="assign-save"]'), '保存指派')

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('批量指派失败甲', { time: 1500 }),
      )
      expect(dialogCloses()).toEqual([])
      cleanup()
    })

    /**
     * ⭐ **R12**（spec §9-R12）：批量档**没有**「清除接手人」的批量能力
     * （Phase A 的 9 条端点里没有 `clear-taker`）⇒ 点它必须走页面既有的「尚未开放」出口。
     *
     * ⚠️ 用例名里带 `§9-R12` 是有意的：免得后人把「批量档点了没请求」当成漏改、又给它接一条端点。
     * ⚠️ 判据全打在**真出口**上（`msg` 文案逐字 + `layer.confirm` 零调用 + `request` 零调用）——
     *    ⛔ 修前实测是**两次确认 + 一句内部校验错**（`订单号格式不正确（应为 20 位数字）：`），
     *    所以「零确认」这一条不是形式主义：它正是「用户白确认两次」那个现场的反面。
     */
    it('§9-R12 批量档点「清除当前所有接手人」⇒ 弹「尚未开放」、`layer.confirm` 零调用、零请求', async () => {
      // 那颗按钮按节点渲染 ⇒ 给管理员身份它才在工具栏里（同上面第一条）。
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      // ⚠️ 这个替身**不会**自动点第一跳：`confirmClearTaker` 的第一跳是**两参**调用
      //    `layer.confirm(text, cb)` ⇒ 第三个形参 `yes` 恒为 `undefined`、`yes?.()` 是**空操作**
      //    （实测 `LAYUI.confirm.mock.calls` 的实参长度 = 2）。它只在**三参/四参**形态
      //    （如 `runBatchFlow` 的 `confirmList`）下才真的驱动回调。
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'setRecMan')
      const dialog = runLayerSuccess('tt-order-assign')
      // ⚠️ 开层那一瞬本页自己会发两发 GET（名单 + 账单）⇒ 先把计数清零，再判「点它之后零请求」。
      mockedRequest.mockClear()

      click(dialog.querySelector('[data-act="assign-clear"]'), '[data-act=assign-clear]')

      expect(LAYUI.msg).toHaveBeenCalledWith('「批量清除接手人」尚未开放', { time: 1500 })
      // R12 的硬要求：**连两跳确认都不进**。这一条是本回归的**鉴别判据**（护栏一删就红）。
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      // ⚠️ **这一条对本回归「护栏被删」没有鉴别力**（那档它照样绿）：修前行为下 `clearTaker('')`
      //    在 `assertOrderNo('')` 处**同步抛**、一个 HTTP 都不发 ⇒ 请求数本来就是 0。留它是当
      //    **交叉校验**（「点击处理器直接发请求」那种形态会被它抓到），⛔ 别把它当成本回归的判据
      //    ——真正钉住 G1 的就是上面那两句 `msg` 文案与 `LAYUI.confirm` 零调用。
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * R12 的另一半：「**行内/单笔那一支一字不改**」。同一条点击分支在**单笔档**下必须仍然进两跳确认
     * ——没有这条，护栏写成「无条件 `notOpen` + return」也能让上一条绿（那就把行内入口改坏了）。
     */
    it('§9-R12 的反面：单笔档（行内 `a.jsr`）点它仍进两跳确认（行内那一支一字不改）', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_rec', { ...rowA, taker_name: 'taker1', project_text: '' })
      click(cell.querySelector('a.jsr'), 'a.jsr')

      const dialog = runLayerSuccess('tt-order-assign')
      click(dialog.querySelector('[data-act="assign-clear"]'), '[data-act=assign-clear]')

      // 第 1 跳（真 `layer.confirm`）被调到了 ⇒ 行内那一支没被护栏拦住。
      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
      expect(LAYUI.msg).not.toHaveBeenCalledWith('「批量清除接手人」尚未开放', { time: 1500 })
      cleanup()
    })
  })

  /* ─────────── 8. 改状态 changeStaBtn → openStatusDialog（批量，选项来自读取形态） ─────────── */

  describe('改状态', () => {
    /** 行数据里的有效推送摘要：有它 = 已抛到代练通。 */
    const PUSH = {
      platform: 10,
      serial_no: '19000000000000000201',
      state: 'sent' as const,
      remote_status: 11,
      remote_cancel_status: 10,
      is_active: true,
    }

    /**
     * ⭐ **勾选里只要含一张已抛单就整体拒绝**，并列出被挡的单号。
     * ⛔ 不静默跳过已抛单只改剩下的 —— 那会让人以为都改了。
     *
     * 判据打在真出口上：`msg` 文案逐字 + 被挡单号 + `layer.open` 零调用 + `request` 零调用。
     * 夹具用**混合勾选**（A 已抛 / B 未抛）⇒ 正好钉住「不许跳过 A 去改 B」。
     */
    it('勾选里含已抛单 ⇒ 整体拒绝并列出被挡单号：不开弹层、零请求（⛔ 不静默跳过）', async () => {
      setRows([{ ...rowA, platform_push: PUSH }, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')

      expect(LAYUI.msg).toHaveBeenCalledWith(
        `以下订单已抛到代练通，状态由平台决定，不能批量修改：${ORDER_A}`,
        { time: 1500 },
      )
      const text = String(LAYUI.msg.mock.calls[0]?.[0] ?? '')
      expect(text).toContain(ORDER_A)
      // 未被挡的那张**不进列表**（否则提示会让人以为它也被挡了）。
      expect(text).not.toContain(ORDER_B)
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('全部结束（`is_active=false`）⇒ 不再拒：照常开弹层、走批量流程', async () => {
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes('/batch/transition/')) return outcome({ targets: ['ISSUE'] })
        return {}
      }) as never)
      // 终态推送行（is_active=false）不再是「还有平台订单」⇒ 按普通内部订单放行。
      setRows([{ ...rowA, platform_push: { ...PUSH, remote_status: 17, is_active: false } }, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')

      await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
      const asked = mockedRequest.mock.calls.map(([config]) => String(config?.url ?? ''))
      expect(asked.some((url) => url.includes('/batch/transition/'))).toBe(true)
      expect(LAYUI.msg).not.toHaveBeenCalledWith(
        `以下订单已抛到代练通，状态由平台决定，不能批量修改：${ORDER_A}`,
        { time: 1500 },
      )
      cleanup()
    })

    it('多张已抛单 ⇒ 单号全部列出（用「、」分隔）', async () => {
      setRows([
        { ...rowA, platform_push: PUSH },
        { ...rowB, platform_push: { ...PUSH, serial_no: '19000000000000000202' } },
      ])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')

      expect(LAYUI.msg).toHaveBeenCalledWith(
        `以下订单已抛到代练通，状态由平台决定，不能批量修改：${ORDER_A}、${ORDER_B}`,
        { time: 1500 },
      )
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('工具栏勾 2 笔 ⇒ 选项来自 `fetchBatchTransitionTargets`（读取形态），提交走批量端点', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/transition/')) {
          // `target` 缺省 = spec §D10 的**读取形态**（只回并集、一行不写）。
          if (config.data?.target === undefined) return outcome({ targets: ['ISSUE', 'PAUSED'] })
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')
      await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
      const dialog = runLayerSuccess('tt-orderlist-transition')

      // ⚠️ radio 是**运行期插进 layer** 的 ⇒ 不 `form.render('radio')` 用户只看到没文字的圆圈。
      expect(LAYUI.form.render).toHaveBeenCalledWith('radio')
      // 选项来自后端读取形态的并集（⛔ 前端不镜像状态机）——`statusLabel` 把码翻成文案。
      // ⚠️ 弹层里**没有**「当前状态」行、radio **不预选**（业务方裁定，既有口径）。
      expect(dialog.querySelector('input[name="status"]:checked')).toBeNull()

      dialog.querySelector<HTMLInputElement>('input[name="status"][value="ISSUE"]')!.checked = true
      // ⭐ D-0337（必填原因）：ISSUE 属「必填原因」三状态 ⇒ 先填原因再点确定，
      //    否则前端闸拦「请输入原因」、零 dry_run/apply 发出；载荷三状态带 `reason` 键。
      ;(dialog.querySelector('#remark') as HTMLInputElement).value = '批量置问题单原因'
      click(dialog.querySelector('[data-act="status-save"]'), '[data-act=status-save]')

      await vi.waitFor(() => expect(posted('/batch/transition/')).toHaveLength(3))
      const calls = posted('/batch/transition/')
      // 第 1 发 = **读取形态**（无 `target`、`dry_run:true` 必填）——这正是「选项来源换掉了」的判据。
      expect(calls[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/transition/',
        data: { order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(calls[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/transition/',
        data: { target: 'ISSUE', reason: '批量置问题单原因', order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(calls[2]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/transition/',
        data: { target: 'ISSUE', reason: '批量置问题单原因', order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('`targets` 为空 ⇒ 沿用既有文案、不开层、只发读取形态那一发', async () => {
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes('/batch/transition/')) return outcome({ targets: [] })
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('当前账号没有可执行的流转（职位权限可能不足）', {
          time: 1500,
        }),
      )
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(posted('/batch/transition/')).toHaveLength(1)
      cleanup()
    })

    it('行内点 `.setdlsta` ⇒ 仍用 `fetchOrder(单号)` + **单笔** `/transition/`', async () => {
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url) === `/api/business/orders/${ORDER_A}/`) {
          return { order_no: ORDER_A, status: 'IN_PROGRESS', allowed_transitions: ['ISSUE'] }
        }
        return {}
      }) as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_sta', { ...rowA, status: 'IN_PROGRESS' })
      click(cell.querySelector('a.setdlsta'), '.setdlsta')

      await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
      const dialog = runLayerSuccess('tt-orderlist-transition')
      dialog.querySelector<HTMLInputElement>('input[name="status"][value="ISSUE"]')!.checked = true
      // ⭐ D-0337（必填原因）：单笔路同样过「必填原因」闸，载荷带 `reason`。
      ;(dialog.querySelector('#remark') as HTMLInputElement).value = '行内置问题单原因'
      click(dialog.querySelector('[data-act="status-save"]'), '[data-act=status-save]')

      await vi.waitFor(() => expect(posted('/transition/')).toHaveLength(1))
      expect(posted('/transition/')[0]).toEqual({
        method: 'post',
        url: `/api/business/orders/${ORDER_A}/transition/`,
        data: { target: 'ISSUE', reason: '行内置问题单原因' },
      })
      cleanup()
    })
  })

  /* ─────────── 9. 删除 delOrderBtn → 两跳确认 + 批量 ─────────── */

  describe('删除', () => {
    it('勾 2 笔 ⇒ 两跳文案 / 按钮一字不改，第 2 跳之后才走批量端点且 `isDelEmp` 显式为 Y', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/delete/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      confirmDeleteTwoHops('Y')
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'delOrderBtn')

      await vi.waitFor(() => expect(posted('/batch/delete/')).toHaveLength(2))
      // 两跳的文案与按钮**逐字**钉住。
      expect(LAYUI.confirm.mock.calls[0]![0]).toBe('真的删除吗？警告：一旦删除，将无法找回，请谨慎处理')
      expect(LAYUI.confirm.mock.calls[1]![0]).toBe(
        '删除订单会自动删除接单客服的接单提成记录，是否需要同步删除代练接手人的绩效记录？',
      )
      expect((LAYUI.confirm.mock.calls[1]![1] as { btn: string[] }).btn).toEqual([
        '需要删除接手人绩效',
        '不需要删除接手人绩效',
      ])
      // ⭐ `isDelEmp` 必须**显式**进 `payload`（⛔ 不许靠后端默认 `'N'`）：漏传这条精确 `toEqual` 会红。
      expect(posted('/batch/delete/')[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'Y', order_nos: [ORDER_A, ORDER_B], dry_run: true, confirmed_effects: [] },
      })
      expect(posted('/batch/delete/')[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'Y', order_nos: [ORDER_A, ORDER_B], dry_run: false, confirmed_effects: [] },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('第 2 跳点「不需要删除接手人绩效」⇒ `isDelEmp` 显式为 N（整批一个值）', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/delete/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A] })
            : outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] })
        }
        return {}
      }) as never)
      confirmDeleteTwoHops('N')
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'delOrderBtn')

      await vi.waitFor(() => expect(posted('/batch/delete/')).toHaveLength(2))
      expect(posted('/batch/delete/')[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'N', order_nos: [ORDER_A], dry_run: true, confirmed_effects: [] },
      })
      expect(posted('/batch/delete/')[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'N', order_nos: [ORDER_A], dry_run: false, confirmed_effects: [] },
      })
      // 恰好 1 笔 ⇒ 沿用该动作原有文案（spec §D12），且它来自 `applied.length`（不是入参长度）。
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('删除成功', { time: 1500 }))
      cleanup()
    })

    /**
     * ⭐ **R-D1：批量删除是**唯一**豁免 20 位形状闸的动作**。
     *
     * 现场：本机演示库里有一条**非 20 位**的存量垃圾行 —— 它在界面上「打不开也删不掉」，
     * 因为前端在发请求**之前**用 `/^\d{20}$/` 拦下（那是**复制**后端详情路由
     * `lookup_value_regex` 的约束，本意是省一次往返）。而批量删除的**目的**正是把请求发出去
     * ⇒ 这里放开，让那一行真的能被清理掉。⛔ 本题**不是**「批量整体放开」：其余 8 个动作与
     * 「打开详情 / 截图」照旧（API 级反向用例见 `api/__tests__/orders.spec.ts` 的 ②）。
     *
     * 判据打在**真出口**上（AGENTS.md §5-G / E-0009 同族）：`request()` 真的收到两发
     * `POST …/batch/delete/`；本地那句形状校验错**一次都不许出现**（它正是今天删不掉的现场原因）。
     * 夹具是本文件唯一的 16 位合成单号 `ORDER_JUNK`（见文件头的例外说明）。
     */
    it('R-D1 勾 1 笔 **16 位**单号 ⇒ 真发出 `POST …/batch/delete/`，且**不**出现本地形状校验错', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/delete/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_JUNK] })
            : outcome({ dry_run: false, eligible: [ORDER_JUNK], applied: [ORDER_JUNK] })
        }
        return {}
      }) as never)
      confirmDeleteTwoHops('N')
      setRows([{ ...ROW, order_no: ORDER_JUNK }])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'delOrderBtn')

      await vi.waitFor(() => expect(posted('/batch/delete/')).toHaveLength(2))
      expect(posted('/batch/delete/')[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'N', order_nos: [ORDER_JUNK], dry_run: true, confirmed_effects: [] },
      })
      expect(posted('/batch/delete/')[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/delete/',
        data: { isDelEmp: 'N', order_nos: [ORDER_JUNK], dry_run: false, confirmed_effects: [] },
      })
      // ⛔ 本地形状闸那句一次都不许出现——豁免前它正是「点了删除只弹一句错、一发请求都不发」的原因。
      expect(LAYUI.msg).not.toHaveBeenCalledWith(
        `订单号格式不正确（应为 20 位数字）：${ORDER_JUNK}`,
        { time: 1500 },
      )
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('删除成功', { time: 1500 }))
      cleanup()
    })
  })

  /* ─────────── 10. 重算客服提成 builderSellEmpOrderBtn → 对标确认层 + 批量 ─────────── */

  describe('重算客服提成', () => {
    /** spec §5.5 **逐字**（对标 `builderSellEmpOrderFunctipsMsg`，读码；含两种 `<br>` 写法）。 */
    const CONFIRM_TEXT =
      '确定需要为选中的订单生成或更新客服销售绩效账单吗？<br/>提示：当确认这些订单没有客服销售绩效账单时再生成，一般情况下系统会自动生成。<br>如果系统发现账单存在，将会重新计算并更新该订单的客服销售账单'

    it('勾 2 笔 ⇒ 先弹对标那句确认（逐字 + 按钮），**确认之后**才发批量端点且 payload 只有两个批量键', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/recompute-commission/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      // ⛔ 不自动点「确定生成」：本用例要判「确认**之前**零请求」。
      let confirmYes: (() => void) | null = null
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        confirmYes = yes ?? null
        return 1
      })
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'builderSellEmpOrderBtn')

      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
      expect(LAYUI.confirm.mock.calls[0]![0]).toBe(CONFIRM_TEXT)
      expect((LAYUI.confirm.mock.calls[0]![1] as { btn: string[] }).btn).toEqual(['确定生成', '点错了'])
      // 还没点「确定生成」⇒ 一发请求都没有（确认层是**前置**动作）。
      expect(posted('/batch/recompute-commission/')).toHaveLength(0)

      ;(confirmYes as unknown as () => void)()
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(2))
      ;(confirmYes as unknown as (index: number) => void)(90)

      await vi.waitFor(() => expect(posted('/batch/recompute-commission/')).toHaveLength(2))
      // 「恰好」⇒ 精确 `toEqual`：这颗按钮不带任何业务值（spec §D15 的载荷只有两个键）。
      expect(posted('/batch/recompute-commission/')[0]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/recompute-commission/',
        data: { order_nos: [ORDER_A, ORDER_B], dry_run: true },
      })
      expect(posted('/batch/recompute-commission/')[1]).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/recompute-commission/',
        data: { order_nos: [ORDER_A, ORDER_B], dry_run: false },
      })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })

    it('勾 0 笔 ⇒ 沿用现行 0 笔文案、不开确认层、零请求', async () => {
      setRows([])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'builderSellEmpOrderBtn')

      expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
      expect(LAYUI.confirm).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * spec §D15 的**补记**：这颗按钮 1 笔的成功文案是
     * **页内自撰**、口径**逐字**对齐 §D15 第 5 条的两档如实回报「已生成 / 无需更新」。
     *
     * ⚠️ 这条判据**钉的是字面值本身**（⛔ 不从实现 import 那个常量——自指断言在被钉常量被改坏时
     * 不会红，`AGENTS.md` §5-G / E-0024）；`applied.length === 1` 那一档永远拿不到它（后端对
     * 「订单未完成」那一笔**不写金额**，`applied` 里就没有它）⇒ 只能拿**已完成且合格**的 1 笔来取这句。
     */
    it('勾 1 笔（已完成、合格）⇒ 1 笔成功文案**逐字**为 §D15 补记的「已生成或无需更新」', async () => {
      const MSG = '客服提成账单已生成或无需更新'
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/recompute-commission/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A] })
            : outcome({ dry_run: false, eligible: [ORDER_A], applied: [ORDER_A] })
        }
        return {}
      }) as never)
      // 确认层那颗「确定生成」替身：直接点它（本用例判的是**成功之后**那一句文案）。
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'builderSellEmpOrderBtn')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(MSG, { time: 1500 }))
      // 「恰好 1 笔」才用它：`batchSuccessMessage` 对 N>1 走 `已处理 N 笔订单`（§D12）。
      expect(LAYUI.msg).not.toHaveBeenCalledWith('已处理 1 笔订单', { time: 1500 })
      await vi.waitFor(() => expect(posted('/batch/recompute-commission/')).toHaveLength(2))
      cleanup()
    })

    /**
     * ⭐ D1 / R9 #12a（spec §7.1 第 3 条）：确认层的 `yes` 拿到 `index`，**那一批已执行成功**才关层。
     *
     * `runBatchFlow` 的 `onDone()` 只在 `true`（已执行）那一支跑 ⇒ 它就是要挂 `layer.close` 的位置。
     * 层号用**用例注入的 3**（⛔ 不用恒真的 `toHaveBeenCalled()`）。
     */
    it('D1-#12a 成功：确认「确定生成」+ 两发都成功 ⇒ 关**确认层**（用例注入 3）', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/recompute-commission/')) {
          return config.data?.dry_run === true
            ? outcome({ eligible: [ORDER_A, ORDER_B] })
            : outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] })
        }
        return {}
      }) as never)
      let confirmYes: ((i: number) => void) | null = null
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: (i: number) => void) => {
        confirmYes = yes ?? null
        return 1
      })
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'builderSellEmpOrderBtn')
      LAYUI.close.mockClear()
      ;(confirmYes as unknown as (i: number) => void)(3)
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(2))
      ;(confirmYes as unknown as (i: number) => void)(90)

      await vi.waitFor(() => expect(posted('/batch/recompute-commission/')).toHaveLength(2))
      await vi.waitFor(() => expect(LAYUI.close).toHaveBeenCalledWith(3))
      cleanup()
    })

    /** ⭐ D1 / R9 #12b（spec §7.1 第 3 条）：执行那一发被拒 ⇒ **不关层**，用户能重试。 */
    it('D1-#12b 失败：第二发（执行那发）被拒 ⇒ **不关层**', async () => {
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/recompute-commission/')) {
          if (config.data?.dry_run === true) return outcome({ eligible: [ORDER_A] })
          throw new BizError(1002, '这一批不能执行甲')
        }
        return {}
      }) as never)
      let confirmYes: ((i: number) => void) | null = null
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: (i: number) => void) => {
        confirmYes = yes ?? null
        return 1
      })
      setRows([rowA])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'builderSellEmpOrderBtn')
      LAYUI.close.mockClear()
      ;(confirmYes as unknown as (i: number) => void)(3)
      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(2))
      ;(confirmYes as unknown as (i: number) => void)(90)

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('这一批不能执行甲', { time: 1500 }))
      expect(dialogCloses()).toEqual([])
      cleanup()
    })
  })

  /* ═══════════════════════════════════════════════════════════════════════
     11. 补缺口的几条（审计后**只补真缺口**）

     本组只放「本文件（页级真交互）确实缺」的 5 条：
       ① 勾 **3 笔**（N>2；既有全是 1/2 笔）＋ 全合格那一档 `layer.confirm` **零调用**；
       ② 部分不合格 + **取消** ⇒ 恰 1 发、零 apply、`table.reload` 零调用（页级）；
       ③ **全不合格** ⇒ 只弹清单 `['知道了']`、点它之后仍零 apply（页级）；
       ④ **apply 撞通用业务失败**（1002，非退款 1010 那一档）⇒ 弹后端 `msg` 原文、**不关层不 reload**（页级）；
       ⑤ 成功顺序 **`table.reload` 早于 `msg`**（页级；`runBatchFlow` 的 `onDone` 在前）。
     ⛔ 不在本组重复的：「部分不合格 + 继续 ⇒ 第 2 发只含合格子集」已由「记退款」组两条覆盖
     （`:793` 合格项在下标 1、`:830` 在下标 0）；「行内回归」由 `:449` / `:1193` 等覆盖；
     「重算客服提成逐字确认」由 `:1308` 覆盖；「两发顺序」由本文件既有的位置断言覆盖（见文件头）。
     ═══════════════════════════════════════════════════════════════════════ */

  describe('Task 5：三条出口 / 勾 3 笔 / 通用失败 / 成功顺序（页级真交互）', () => {
    /** 第 3 笔的合成单号（形状同 `ORDER_A` / `ORDER_B`，过 20 位闸）。 */
    const ORDER_C = '20000000000000000003'
    const rowC = { ...ROW, order_no: ORDER_C }

    /**
     * 让「已 resolve 的异步链路」跑完（微任务 + 两轮宏任务）。
     *
     * ⚠️ 取消 / 全不合格那两条出口**成功时页面上什么都不发生**（零 apply、零 reload、零成功提示）
     * ⇒ 没有「等到某个事件」可等；不等会让「恰 1 发」变成**恒真**（第二发还在微任务队列里）。
     */
    async function flush(): Promise<void> {
      await new Promise<void>((resolve) => setTimeout(() => resolve(), 0))
      await new Promise<void>((resolve) => setTimeout(() => resolve(), 0))
    }

    it('勾 3 笔（全合格）⇒ 两发各恰一次、`order_nos` 恰为那 3 笔且同序；确认显示 3 笔；文案「已处理 3 笔订单」', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B, ORDER_C] }))
        .mockResolvedValueOnce(
          outcome({
            dry_run: false,
            eligible: [ORDER_A, ORDER_B, ORDER_C],
            applied: [ORDER_A, ORDER_B, ORDER_C],
          }),
        )
      setRows([rowA, rowB, rowC])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      // ⚠️ 用例名声称「恰好 / 同序」⇒ 两发都用**位置** `toEqual`：它同时钉住
      //    ① `order_nos` 恰好是那 3 笔（N>2 时没漏一个、没多一个）；② `dry_run` 先 `true` 后 `false`。
      expect(sent(0)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/price/',
        data: { total_price: '50.00', order_nos: [ORDER_A, ORDER_B, ORDER_C], dry_run: true, confirmed_effects: [] },
      })
      expect(sent(1)).toEqual({
        method: 'post',
        url: '/api/business/orders/batch/price/',
        data: { total_price: '50.00', order_nos: [ORDER_A, ORDER_B, ORDER_C], dry_run: false, confirmed_effects: [] },
      })
      expect(LAYUI.confirm.mock.calls[0]?.[1]).toMatchObject({ btn: ['确认处理 3 笔', '取消'] })
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 3 笔订单', { time: 1500 }))
      cleanup()
    })

    it('部分不合格 + 点「取消」⇒ 恰好 1 发（零 apply）、`table.reload` 零调用', async () => {
      mockedRequest.mockResolvedValueOnce(
        outcome({
          eligible: [ORDER_A],
          ineligible: [{ order_no: ORDER_B, code: 1002, msg: '订单当前状态不允许改价' }],
        }),
      )
      // ⛔ 替身**不自动点**任何一颗：第一跳（删除那类）是两参调用，这里 `runBatchFlow` 的
      //    `confirmList` 是四参 `confirm(text, opts, yes, btn2)` ⇒ 「取消」自己按真形状驱动。
      LAYUI.confirm.mockImplementation(() => 1)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      // 按钮逐字：`['继续处理其余 N 笔', '取消']`（`N` = `eligible.length`）。
      expect((LAYUI.confirm.mock.calls[0]![1] as { btn: string[] }).btn).toEqual([
        '确认处理 1 笔',
        '取消',
      ])
      // 「取消」= 第 2 颗按钮 = **第 4 个实参**（vendored layui 的 `confirm(text, opts, yes, btn2)`）。
      // ⚠️ 在驱动「取消」**之前**清一次 `msg` 的调用记录 ⇒ 紧接着那条
      //    `not.toHaveBeenCalled()` 覆盖的正是「取消之后那一段链路」，而不是把 mount / 开弹层期的提示算进来。
      LAYUI.msg.mockClear()
      ;(LAYUI.confirm.mock.calls[0]![3] as () => void)()
      await flush()

      expect(mockedRequest).toHaveBeenCalledTimes(1)
      expect(posted('/batch/price/')).toHaveLength(1)
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      // 取消 ⇒ **一次提示都没有**（判据补齐成与这句话一致；⛔ 下面两条精确断言保留、未放宽）。
      expect(LAYUI.msg).not.toHaveBeenCalled()
      expect(LAYUI.msg).not.toHaveBeenCalledWith('接单价已更新', { time: 1500 })
      expect(LAYUI.msg).not.toHaveBeenCalledWith('已处理 1 笔订单', { time: 1500 })
      cleanup()
    })

    it('全不合格 ⇒ 只弹清单（按钮恰为 `[\'知道了\']`、两笔逐字列出），点它之后仍零 apply、`table.reload` 零调用', async () => {
      mockedRequest.mockResolvedValueOnce(
        outcome({
          eligible: [],
          ineligible: [
            { order_no: ORDER_A, code: 1002, msg: '订单当前状态不允许改价' },
            { order_no: ORDER_B, code: 1002, msg: '订单当前状态不允许改价' },
          ],
        }),
      )
      LAYUI.confirm.mockImplementation(() => 1)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      // 清单正文**逐字**（单号 + 后端 `msg`，一行一条、顺序同 `order_nos`）。
      expect(LAYUI.confirm.mock.calls[0]![0]).toBe(
        `<p><strong>批量改接单价为 50.00 · 将处理 0 笔订单</strong></p><p><strong>不会处理 2 笔订单</strong></p>单号 ${ORDER_A}：订单当前状态不允许改价<br>单号 ${ORDER_B}：订单当前状态不允许改价`,
      )
      // 全不合格那一档的按钮**恰为** `['知道了']`（spec §D12 第三条出口）——⛔ 没有「继续处理其余 N 笔」。
      expect((LAYUI.confirm.mock.calls[0]![1] as { btn: string[] }).btn).toEqual(['知道了'])
      // 点「知道了」（第 3 个实参 = yes）之后**仍然**零 apply（这一发不是「执行」的许可）。
      ;(LAYUI.confirm.mock.calls[0]![2] as () => void)()
      await flush()

      expect(mockedRequest).toHaveBeenCalledTimes(1)
      expect(posted('/batch/price/')).toHaveLength(1)
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('apply 撞通用业务失败（1002，非退款 1010 那一档）⇒ 弹后端 `msg` 原文、**不关层不 reload**、仍恰 3 发', async () => {
      const BACKEND_MSG = '订单当前状态不允许流转'
      mockedRequest.mockImplementation((async (config: {
        url?: string
        data?: Record<string, unknown>
      }) => {
        if (String(config.url).includes('/batch/transition/')) {
          // 第 1 发 = 读取形态（无 `target`）：只回可选目标并集，一行不写。
          if (config.data?.target === undefined) return outcome({ targets: ['ISSUE'] })
          if (config.data?.dry_run === true) return outcome({ eligible: [ORDER_A, ORDER_B] })
          // 第 3 发（执行）撞**通用**业务错（spec §6 的 1002）——与退款 1010 是两条不同的用例。
          throw new BizError(1002, BACKEND_MSG)
        }
        return {}
      }) as never)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeStaBtn')
      await vi.waitFor(() => expect(LAYUI.open).toHaveBeenCalled())
      const dialog = runLayerSuccess('tt-orderlist-transition')
      dialog.querySelector<HTMLInputElement>('input[name="status"][value="ISSUE"]')!.checked = true
      // ⭐ D-0337（必填原因）：先填原因，闸才放行到三发批量。
      ;(dialog.querySelector('#remark') as HTMLInputElement).value = '批量失败原因'
      click(dialog.querySelector('[data-act="status-save"]'), '[data-act=status-save]')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(BACKEND_MSG, { time: 1500 }))
      // 通用失败口径（spec §5.2 / §D12）：只弹后端 `msg` 原文——⛔ 不关层、⛔ 不 reload。
      // （对照：改状态**成功**那一支的 `onDone` 里是 `close(index)` + `reload()`。）
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      // 恰好三发（读取形态 + dry_run + 一次 apply）：失败后**不重发**、不多发。
      expect(posted('/batch/transition/')).toHaveLength(3)
      cleanup()
    })

    it('成功顺序：`table.reload` 早于成功那句 `msg`（顺序反了用户只看得到筛选警告）', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()
      // 顺序判据只看「这一次点击」产生的调用（mount 期的提示 / 刷新不进判据）。
      LAYUI.msg.mockClear()
      LAYUI.table.reload.mockClear()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      const okIndex = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '已处理 2 笔订单')
      expect(okIndex).toBeGreaterThanOrEqual(0)
      expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]
      const okMsgOrder = LAYUI.msg.mock.invocationCallOrder[okIndex]
      expect(reloadOrder).toBeDefined()
      expect(okMsgOrder).toBeDefined()
      // ⛔ 判据是**先后**、不是**次数**（只看次数这条会恒真）。
      expect(reloadOrder!).toBeLessThan(okMsgOrder!)
      cleanup()
    })

    it('部分不合格 + 点右上角 X / 关层（`options.cancel`）⇒ 同样恰好 1 发（零 apply）、`table.reload` 零调用、零提示', async () => {
      // spec §T6 更正块追加的**第四个出口**（与 §D12 的三条并列）：`options.cancel`。
      // ⚠️ 它缺了不会多发请求，而是让 `confirmList` 那个 Promise **永不 settle**（页面上「点了没反应」，
      //    **静默**）⇒ **页级能观测到的只有**「`opts.cancel` 这个回调**确实被交出来了**」。
      mockedRequest.mockResolvedValueOnce(
        outcome({
          eligible: [ORDER_A],
          ineligible: [{ order_no: ORDER_B, code: 1002, msg: '订单当前状态不允许改价' }],
        }),
      )
      // ⛔ 仍不自动点任何一颗：第三跳（X / 关层）既不是 `yes`（第 3 实参）也不是 `btn2`（第 4 实参）。
      LAYUI.confirm.mockImplementation(() => 1)
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changeOrderPriceBtn')
      submitPricePrompt('50.00')

      await vi.waitFor(() => expect(LAYUI.confirm).toHaveBeenCalledTimes(1))
      // 驱动**真形状**：vendored layui 把「关层」接给 `confirm(text, opts, yes, btn2)` 的 `opts.cancel`
      // （`layui.js` 的 X 分支调 `o.cancel && o.cancel(...)`；见 `orderBatch.ts` 的 **`confirmList`** 上那段读码注释）。
      // ⚠️ 锚点用**函数名**、不写行号：`orderBatch.ts` 一改行号就漂。
      const opts = LAYUI.confirm.mock.calls[0]![1] as { cancel?: () => void }
      expect(opts.cancel, 'confirmList 的 opts 里没有 cancel ⇒ 点 X 之后那个 Promise 永不 settle').toBeTypeOf(
        'function',
      )
      LAYUI.msg.mockClear()
      opts.cancel!()
      await flush()

      // 关层 ≡ 取消：⛔ 不发 apply、⛔ 不 reload、⛔ 零成功提示。
      expect(mockedRequest).toHaveBeenCalledTimes(1)
      expect(posted('/batch/price/')).toHaveLength(1)
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      expect(LAYUI.msg).not.toHaveBeenCalled()
      cleanup()
    })

    it('勾 3 笔 + 记退款（抽掉中间那笔）⇒ 第 1 发 `request_ids` 恰 3 个且两两互异；第 2 发与收窄后的 `order_nos` **按下标**对齐', async () => {
      // F3（spec §T6 更正块「**在请求形状上**唯一真与 N 有关的一处」）：退款的每笔一个幂等键、与 `order_nos` 等长同序。
      // ⭐ 合格项刻意取**第 1 与第 3 笔**（抽掉中间那笔）：只有这样「按单号查表」（正确）与
      //    「取全量前 N 个」（错位）才**不同真** ⇒ 一条用例同时钉住「N=3 三个键」与「收窄后按下标对齐」。
      mockedRequest
        .mockResolvedValueOnce(
          outcome({
            eligible: [ORDER_A, ORDER_C],
            ineligible: [{ order_no: ORDER_B, code: 1007, msg: '订单当前状态不允许退款' }],
          }),
        )
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_C], applied: [ORDER_A, ORDER_C] }),
        )
      LAYUI.confirm.mockImplementation((_t: string, _o: unknown, yes?: () => void) => {
        yes?.()
        return 1
      })
      setRows([rowA, rowB, rowC])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'refundOrderBtn')
      const panel = openPanel(root, '.setrefundOrderLog')
      panel.querySelector<HTMLInputElement>('#refundFee')!.value = '50.00'
      panel.querySelector<HTMLInputElement>('#refundRemark')!.value = '退货差价'
      click(panel.querySelector('.saveReFundOrderBtn'), '.saveReFundOrderBtn')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = sent(0)
      expect(first.data.order_nos).toEqual([ORDER_A, ORDER_B, ORDER_C])
      const firstIds = first.data.request_ids as string[]
      // 第 1 发：**恰好 3 个**键、两两互异、形状过闸（幂等键列宽 **64**）。
      expect(firstIds).toHaveLength(3)
      expect(new Set(firstIds).size).toBe(3)
      for (const id of firstIds) {
        expect(typeof id).toBe('string')
        expect(id.length).toBeGreaterThan(0)
        expect(id.length).toBeLessThanOrEqual(64)
      }
      const second = sent(1)
      // 第 2 发：`order_nos` 收窄成合格子集，`request_ids` 必须**按下标**跟着收窄
      // （⛔ 不是「取全量前 N 个」——那会把 ORDER_B 的键退到 ORDER_C 上）。
      expect(second.data.order_nos).toEqual([ORDER_A, ORDER_C])
      expect(second.data.request_ids).toEqual([firstIds[0], firstIds[2]])
      expect(second.data.request_ids).not.toEqual([firstIds[0], firstIds[1]])
      // 多笔 ⇒ 文案是「已处理 N 笔订单」（`batchSuccessMessage` 的口径，N = `applied.length` = 2）。
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      cleanup()
    })
  })

  /* ═══════════════════════════════════════════════════════════════════════
     12. R9 全页盘点追加：批量单号搜索 / 撤回抛单 的关层判据（spec §3.1 追加行 #16 / #17）

     ⚠️ 这两处是按 spec 自己的判据
     （「所有『点确定 → 发请求』的弹层」）盘点时追加的；它们各自都是**先关后发**形态
     ⇒ 判据**只钉「关了一次 + 层号是注入的那个」**，⛔ 不写「失败不关层」。
     ═══════════════════════════════════════════════════════════════════════ */

  describe('R9 追加盘点：批量单号搜索 / 撤回抛单', () => {
    /**
     * 捕获页面 mount 期注册的 `select(selectSeach1)` 处理器。
     *
     * ⚠️ 本文件的 `form.on` 是**纯替身**（只记入参、不代跑 layui form 模块）⇒ 要驱动那格
     * 下拉只能在捕获到的处理器上直接调，而不是去改 `<select>` 的值（那不会触发任何东西）。
     */
    function batchOrderHandler(): (d: { value: string }) => void {
      const calls = LAYUI.form.on.mock.calls as unknown as Array<
        [string, (d: { value: string }) => void]
      >
      const hit = calls.filter((c) => c[0] === 'select(selectSeach1)').pop()
      expect(hit, '页面没有注册 select(selectSeach1)').toBeDefined()
      return hit![1]
    }

    it('R9-#16a 批量单号搜索：值非法 ⇒ 提示 + **关层**（注入 5）+ 零 reload', async () => {
      const { cleanup } = await mountPage()
      const handler = batchOrderHandler()

      handler({ value: '10' })
      const promptCall = lastPromptCall()
      expect((promptCall[0] as { title: string }).title).toBe(
        '按单号批量查询，每行一笔单号。注意：该搜索会排除其它搜索条件',
      )
      LAYUI.table.reload.mockClear()
      ;(promptCall[1] as (v: string, i: number) => void)('abc', 5)

      expect(LAYUI.msg).toHaveBeenCalledWith('单号格式不符合要求', { time: 1500 })
      expect(LAYUI.close).toHaveBeenCalledWith(5)
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('R9-#16b 批量单号搜索：值合法 ⇒ **关层**（注入 5）+ reload 恰好一次', async () => {
      const { cleanup } = await mountPage()
      const handler = batchOrderHandler()

      handler({ value: '10' })
      const promptCall = lastPromptCall()
      LAYUI.table.reload.mockClear()
      ;(promptCall[1] as (v: string, i: number) => void)('20000000000000000001', 5)

      expect(LAYUI.close).toHaveBeenCalledWith(5)
      expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
      cleanup()
    })

    it('R9-#17a 撤回抛单：确认 ⇒ 关该确认层**恰好一次**（注入 3）+ `/withdraw/` 恰好一发', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_change', { ...rowA, status: 'WAITING_GRAB' })
      click(cell.querySelector<HTMLElement>('a.cancelThrowHome'), 'a.cancelThrowHome')

      const confirmCall = LAYUI.confirm.mock.calls[LAYUI.confirm.mock.calls.length - 1]!
      // 确认语**逐字**（⛔ 不从实现 import 常量——自指断言在被钉常量被改坏时不会红）。
      expect(confirmCall[0]).toBe('确定从内部接单大厅撤销该订单吗？撤销后，内部接单大厅将看不到该订单')
      LAYUI.close.mockClear()
      ;(confirmCall[1] as (i: number) => void)(3)

      await vi.waitFor(() => expect(posted('/withdraw/')).toHaveLength(1))
      expect(LAYUI.close).toHaveBeenCalledTimes(1)
      expect(LAYUI.close).toHaveBeenCalledWith(3)
      cleanup()
    })

    /**
     * ⭐ 失败支的**如实登记**：撤回是「先关后发」（确认即关层）⇒ 失败时层**已经关了**，
     * 判据只能是「弹后端 `msg` 原文」（⛔ 不写「失败不关层」——那会与既定形态相反）。
     */
    it('R9-#17b 撤回抛单：请求失败 ⇒ 弹后端 `msg` 原文（层已关，如实登记）', async () => {
      vi.mocked(fetchMe).mockResolvedValueOnce({ is_tenant_admin: true } as never)
      mockedRequest.mockImplementation((async (config: { url?: string }) => {
        if (String(config.url).includes('/withdraw/')) {
          throw new BizError(1002, '只有待抢单可以撤回，当前状态：IN_PROGRESS')
        }
        return {}
      }) as never)
      setRows([])
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_change', { ...rowA, status: 'WAITING_GRAB' })
      click(cell.querySelector<HTMLElement>('a.cancelThrowHome'), 'a.cancelThrowHome')

      const confirmCall = LAYUI.confirm.mock.calls[LAYUI.confirm.mock.calls.length - 1]!
      LAYUI.close.mockClear()
      ;(confirmCall[1] as (i: number) => void)(3)

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '只有待抢单可以撤回，当前状态：IN_PROGRESS',
          { time: 1500 },
        ),
      )
      expect(LAYUI.close).toHaveBeenCalledWith(3)
      cleanup()
    })
  })

  /* ═══════════════════════════════════════════════════════════════════════
     **页级真交互**补充（单元层的 a–g 在 `src/layui-island/__tests__/orderBatch.spec.ts`）：
     本组只补**页面接线**上才看得见的两条：① 点「保存」后立刻有圈、且早于第一发请求；
     ② **连点两下**只发一组请求。
     走「改类型」那条链（工具栏 `changetypesBtn` → `.typebox` → `.settypes` ⇒ `runBatchFlow`）。
     ═══════════════════════════════════════════════════════════════════════ */

  describe('T1/T2：立刻转圈 + 防重复提交闸（页级真交互）', () => {
    function pickTypeAndSave(root: HTMLElement, value: '0' | '1'): HTMLElement {
      const panel = root.querySelector<HTMLElement>('.typebox')
      expect(panel, '证据件里找不到 .typebox').not.toBeNull()
      panel!.querySelector<HTMLInputElement>(
        `input[name="selbussType"][value="${value}"]`,
      )!.checked = true
      const btn = panel!.querySelector<HTMLElement>('.settypes')
      expect(btn, '.typebox 里找不到 .settypes').not.toBeNull()
      return btn!
    }

    it('T1：点「保存」立刻转圈，且 layer.load 早于第一发请求；成功后被关掉', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      const btn = pickTypeAndSave(root, '0')
      click(btn, '.settypes')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      expect(LAYUI.load).toHaveBeenCalledWith(1, { shade: false })
      const loadOrder = LAYUI.load.mock.invocationCallOrder[0]!
      const firstRequest = mockedRequest.mock.invocationCallOrder[0]!
      expect(loadOrder).toBeLessThan(firstRequest)
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      expect(LAYUI.close).toHaveBeenCalledWith(LAYUI.loadIndex)
      cleanup()
    })

    it('T2：连点两下「保存」⇒ 只发一组请求（1 dry_run + 1 write）、只报一次成功', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      const btn = pickTypeAndSave(root, '0')
      // 业务方现场的动作：以为没点上 ⇒ 再点一遍。
      click(btn, '.settypes')
      click(btn, '.settypes')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      // 同一批的检查和写入各开一次 loading。重复保存没有另开流程。
      expect(LAYUI.load).toHaveBeenCalledTimes(2)
      expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
      expect(posted('/batch/types/')).toHaveLength(2)
      expect(posted('/batch/types/')[0]!.data.dry_run).toBe(true)
      expect(posted('/batch/types/')[1]!.data.dry_run).toBe(false)
      // ⛔ 第二发不许额外弹任何消息（静默忽略）。
      expect(LAYUI.msg).toHaveBeenCalledTimes(1)
      cleanup()
    })

    /**
     * ⭐（规格 §3-T3）**成功才关窗**是既有行为，只补用例钉住
     * （防止后续把 loading / 闸的时序写进 `onDone` 里弄坏）。
     *
     * ⚠️ 判据打在**弹层**语义上（`dialogCloses()` 过滤 loading 哨兵）；loading 自己的关法
     * 由上面 T1 那两条与单元层 c/d 负责。
     */
    it('T3-a：成功 ⇒ 关窗（弹层恰一次）+ `reload` 恰一次 + loading 关掉', async () => {
      mockedRequest
        .mockResolvedValueOnce(outcome({ eligible: [ORDER_A, ORDER_B] }))
        .mockResolvedValueOnce(
          outcome({ dry_run: false, eligible: [ORDER_A, ORDER_B], applied: [ORDER_A, ORDER_B] }),
        )
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      const btn = pickTypeAndSave(root, '0')
      LAYUI.close.mockClear()
      LAYUI.table.reload.mockClear()
      click(btn, '.settypes')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('已处理 2 笔订单', { time: 1500 }))
      expect(dialogCloses()).toHaveLength(1)
      expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
      expect(LAYUI.close).toHaveBeenCalledWith(LAYUI.loadIndex)
      cleanup()
    })

    it('T3-b：失败（dry_run 被拒）⇒ 留窗（弹层零关闭）+ 零 `reload`，但 loading 仍关掉', async () => {
      mockedRequest.mockRejectedValueOnce(new BizError(1002, '预检失败甲'))
      setRows([rowA, rowB])
      const { root, cleanup } = await mountPage()

      clickToolbar(root, 'changetypesBtn')
      const btn = pickTypeAndSave(root, '0')
      LAYUI.close.mockClear()
      LAYUI.table.reload.mockClear()
      click(btn, '.settypes')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('预检失败甲', { time: 1500 }))
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      // ⭐ T1：失败出口也必须把圈关掉（否则页面永久转圈）。
      expect(LAYUI.close).toHaveBeenCalledWith(LAYUI.loadIndex)
      cleanup()
    })
  })
})
