// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【淘宝发货记录】复刻页的**门禁**：既有「表头 / 列宽 / 分页」的防漂移断言**一条不删**，
 * 另加接真后端（spec §3.7/§3.8）之后的**真交互**用例——三颗工具栏按钮各一条。
 *
 * ## 为什么必须有「驱动真交互」的用例（`AGENTS.md` §5-G）
 *
 * 本页已**脱离 `artifactPage`**（那个共享渲染器在捕获阶段把所有 `[lay-event]` 点击收口成
 * 「尚未开放」并 `stopPropagation` ⇒ 在它下面接 `table.on('toolbar(…)')` 是死路，
 * 详见页文件头）。代价：`typecheck` 看不见 DOM 选择器对不对、`lint` 看不见类名里多了个点、
 * `parity` 在空表时看不到表体 ⇒ **本文件是唯一防线**，而「点了没反应」是**静默**的。
 *
 * 所以三颗按钮都由 `table.on('toolbar(tableList)')` **登记的那个真回调**驱动
 * （= 真人点那颗按钮时 layui 会调的函数），列表取数走真 `table.render` 的那份配置、
 * 写请求打真 `api/tbSendLogs.ts` 拼出来的请求（`@/api/http` 的 `request` 被替身化）。
 *
 * ## 三条 island 陷阱（§5-G）在本文件里各有判据
 *
 * ① 类名与选择器分两个常量 —— 页里是 `TABLE_ELEM`（`'#tableList'`）与 `TABLE_ID`（`'tableList'`），
 *    本文件用「`table.reload` 的第一个实参必须是 `TABLE_ID`」把它钉住（传 `#id` 是静默 no-op）；
 * ② 以 `(` 开头的语句前置分号 —— 页代码风格，由 `pnpm lint` 守；
 * ③ **先 `await bootstrapIsland(...)` 再塞标记**、而 `initIslandElements(layui)` 留在塞标记**之后**
 *    —— 本文件用 `LAYUI.state` 两个快照（`bootstrapSawMarkup` / `initSawMarkup`）钉住顺序。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-tbsendrecordlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = fs.readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-tbsendrecordlist.html'),
  'utf8',
)

/**
 * 一条发货记录**恰好**的键集合（= 后端序列化器的输出键，spec §3.1 的模型字段）。
 *
 * ⚠️ 这是**前端侧的契约副本**：后端序列化器的字段集若与它不同，`buildSendRow` 会把单元格
 * 渲染成空白而**不会报错**（`url` 模式把 items 原样交给表格）。所以夹具**恰好**等于这份清单
 * ——⛔ 不自带后端不返回的键（那会让本文件在**说谎**，同 `order-orderrecycle.spec.ts` 的
 * `RECYCLE_ROW_KEYS` 教训）。后端那份才是权威；两侧要同步改。
 */
const TB_SEND_LOG_ROW_KEYS: readonly string[] = [
  'id',
  'order_no',
  'tb_order_no',
  'send_params',
  'store_name',
  'result',
  'sent_at',
  'operator',
  'remark',
  'created_at',
]

/**
 * 合成夹具（PII 纪律：不抄取证记录里的真实店铺名 / 真实单号；单号是 20 位数字的**形状**而已，
 * 店铺名带「测试…甲」的合成后缀）。
 */
const ROW: Record<string, unknown> = {
  id: 11,
  order_no: '20261003000000000501',
  tb_order_no: '20261003000000000502',
  send_params: '{"order_no":"20261003000000000502","send_way":1}',
  store_name: '测试店铺甲',
  result: 1,
  sent_at: '2026-10-03T12:55:00+08:00',
  operator: '系统',
  remark: '',
  created_at: '2026-10-03T12:55:01+08:00',
}

/** 失败行（`result=0`，备注里是上游原文）——「发货结果」列那一档的判据。 */
const FAIL_ROW: Record<string, unknown> = {
  ...ROW,
  id: 12,
  result: 0,
  remark: '上游拒绝：测试原因甲',
}

/** layui 的**可控替身**。真 layui 在 jsdom 里加载不了（证据：`order-orderrecycle.spec.ts` 同形）。 */
const LAYUI = vi.hoisted(() => {
  const toolHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const msgs: string[] = []
  let rows: Array<Record<string, unknown>> = []
  /** 三条陷阱之③的两个快照：那两次调用发生时，页面的表格标记**在不在** document 里。 */
  const state = { bootstrapSawMarkup: true, initSawMarkup: false }
  const table = {
    render: vi.fn(),
    reload: vi.fn(),
    on: vi.fn((e: string, cb: (o: unknown) => unknown) => {
      toolHandlers.set(e, cb)
    }),
    checkStatus: vi.fn(() => ({ data: rows })),
    // 真 layui：`table.getOptions(id)` 回 `render` 时那份配置（用户选的「条/页」就地写在 `page` 上）。
    getOptions: vi.fn(() => ({ page: { limit: 50 } })),
  }
  const form = {
    render: vi.fn(),
    on: vi.fn((e: string, cb: (o: unknown) => unknown) => {
      formHandlers.set(e, cb)
    }),
  }
  const layer = {
    msg: vi.fn((t: string) => {
      msgs.push(t)
    }),
    alert: vi.fn(),
    open: vi.fn(),
    close: vi.fn(),
    closeAll: vi.fn(),
  }
  return {
    toolHandlers,
    formHandlers,
    msgs,
    state,
    table,
    form,
    layer,
    setRows: (next: Array<Record<string, unknown>>): void => {
      rows = next
    },
    laydate: { render: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    // ⚠️ 陷阱 ③ 前半：`bootstrapIsland()` 跑的时候页面标记**还不该在** document 里。
    LAYUI.state.bootstrapSawMarkup = document.querySelector('#tableList') !== null
    return {
      form: LAYUI.form,
      table: LAYUI.table,
      layer: LAYUI.layer,
      laydate: LAYUI.laydate,
      $: LAYUI.$,
    }
  }),
  initIslandElements: vi.fn(() => {
    // ⚠️ 陷阱 ③ 后半：`initIslandElements(layui)` 必须在**塞完标记之后**才被调用。
    LAYUI.state.initSawMarkup = document.querySelector('#tableList') !== null
  }),
  islandMsg: (_l: unknown, t: string) => LAYUI.msgs.push(t),
  // `@/layui/artifactPage`（本页只借它的 `fetchArtifactHtml`）在模块顶层 import 这个符号
  // ⇒ mock 里缺席会让 vitest 直接报「没有这个导出」。
  islandTabTarget: vi.fn(() => null),
}))

// 传输层替身：断言的是**真 `api/tbSendLogs.ts`** 拼出来的请求配置（不是页面自造的中间态）。
// ⚠️ `request<T>()` 回的是**解包后的 `data`**（信封的 `{code,msg}` 由 `unwrap()` 吃掉）
// ⇒ 这个替身回的就是重试结果本身，⛔ 不是 `{code,msg,data}` 信封。
vi.mock('@/api/http', () => ({
  request: vi.fn(async (config: { method?: string; url?: string; data?: unknown }) => {
    const url = String(config.url ?? '')
    if (url.includes('/tb-send-logs/retry/')) return { success: 1, fail: 1 }
    throw new Error(`未预期的 request：${url}`)
  }),
}))

import { request } from '@/api/http'
import { escapeHtml } from '@/layui-island/escape'
import page, {
  buildRenderCols,
  buildSendRow,
  COLS_FOR_TEST,
  defaultSendDateRange,
  SEND_IMPORT_NOT_OPEN_TEXT,
  SEND_ROW_SELECT_TEXT,
  SPEC,
  TABLE_ELEM,
  TABLE_ID,
  TOOLBAR_ELEM,
  toSendLogSearch,
  type SendCellRow,
} from '../order-tbsendrecordlist'

const CTX = { onNavigate: vi.fn() }

/** 排空已 resolve 的 promise 链（`request` 是 async 替身）。 */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
}

/** 挂载整页（证据件走全局 `fetch` 替身）。`root` 必须**在 document 里**——陷阱 ③ 的判据要查它。 */
async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, CTX)
  await flush()
  return root
}

/** 驱动 `table.on('toolbar(tableList)')` 登记的那个**真回调**（= 真人点工具栏按钮）。 */
async function driveToolbar(event: string): Promise<void> {
  const handler = LAYUI.toolHandlers.get(`toolbar(${TABLE_ID})`)
  expect(handler, '本页没有注册 table 的 toolbar 处理器').toBeDefined()
  await (handler as (o: unknown) => unknown)({ event })
  await flush()
}

type RenderOpts = {
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
    data?: { items?: Array<Record<string, unknown>>; total?: number }
  }) => { code: number; msg: string; count: number; data: SendCellRow[] }
}

/** `table.render` 收到的唯一那份配置（**真正交给 layui 的出口**，不是页面自造的中间变量）。 */
function renderOpts(): RenderOpts {
  const call = LAYUI.table.render.mock.calls[0]
  expect(call, 'table.render 没有被调用').toBeTruthy()
  return call![0] as RenderOpts
}

/** 最近一次 `table.reload` 的入参（页面重新取数的**真实出口**）。 */
function lastReload(): { where: Record<string, unknown>; page: { curr?: number; limit?: number } } {
  const calls = LAYUI.table.reload.mock.calls
  const call = calls[calls.length - 1]
  expect(call, 'table.reload 没有被调用').toBeTruthy()
  // ⚠️ 陷阱 ①：第一个实参必须是**实例 id**（`tableList`），传 `#tableList` 是静默 no-op。
  expect(call![0]).toBe(TABLE_ID)
  return call![1] as { where: Record<string, unknown>; page: { curr?: number; limit?: number } }
}

type ReqConfig = { method?: string; url?: string; data?: unknown }
const requestCalls = (): ReqConfig[] =>
  vi.mocked(request).mock.calls.map(([config]) => config as unknown as ReqConfig)

beforeEach(() => {
  document.body.innerHTML = ''
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.table.on.mockClear()
  LAYUI.table.getOptions.mockClear()
  LAYUI.table.getOptions.mockReturnValue({ page: { limit: 50 } })
  LAYUI.form.on.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.laydate.render.mockClear()
  LAYUI.toolHandlers.clear()
  LAYUI.formHandlers.clear()
  LAYUI.msgs.length = 0
  LAYUI.state.bootstrapSawMarkup = true
  LAYUI.state.initSawMarkup = false
  LAYUI.setRows([])
  vi.mocked(request).mockClear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).includes('order-tbsendrecordlist.html')) {
        return { ok: true, text: async () => EVIDENCE } as unknown as Response
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('淘宝发货记录：列声明与渲染参数（**既有断言，一条不删**）', () => {
  it('10 列，顺序与文案对齐 tbsendrecordmang.js L49-87', () => {
    expect(COLS_FOR_TEST.map((c) => c.title)).toEqual([
      undefined,
      '记录ID',
      '系统订单号',
      '淘宝订单号',
      '发货原参数',
      '发货店铺',
      '发货结果',
      '发货时间',
      '发货人',
      '备注说明',
    ])
  })

  it('第 1 列是 checkbox/fixed:left/width 30（L50 是**定值**，本页没有 device 分支）', () => {
    expect(COLS_FOR_TEST[0]).toMatchObject({ type: 'checkbox', fixed: 'left', width: 30 })
  })

  it('运行期百分比列宽原样透传（桌面分支：3/10/10/25/5/3/7/3/7 %）', () => {
    expect(COLS_FOR_TEST.map((c) => c.width)).toEqual([
      30,
      '3%',
      '10%',
      '10%',
      '25%',
      '5%',
      '3%',
      '7%',
      '3%',
      '7%',
    ])
  })

  it('9 个数据列**全部** align:center（L54-86 每一列都写了）', () => {
    expect(COLS_FOR_TEST.slice(1).every((c) => c.align === 'center')).toBe(true)
  })

  it("表格级：page(limit 50, limits [50] 只一项) + 生效的 defaultToolbar ['filter']", () => {
    expect(SPEC.tables).toHaveLength(1)
    const t = SPEC.tables![0]!
    expect(t.elem).toBe(TABLE_ELEM)
    expect(t.toolbar).toBe(TOOLBAR_ELEM)
    expect(t.defaultToolbar).toEqual(['filter'])
    // ⛔ 钉**字面值**、不钉 `PAGE_LIMITS` 那个常量：拿常量比自己会让「常量被改成 [20]」照样绿
    //    （自指断言，E-0024）。`[50]` 是对标 `tbsendrecordmang.js` L89-91 的实测值。
    expect(t.page).toEqual({ limit: 50, limits: [50] })
  })

  it('`SPEC` 仍是**纯声明式证据**：它自己不带 `done`（活渲染在 mount 里，见下一条 describe）', () => {
    expect(SPEC.tables![0]!.done).toBeUndefined()
  })

  /**
   * ⚠️ **本线改准了这一条**：原断言是「只有「发货结果」那一列挂 `templet`」
   * ——那是**旧行为**，也正是本页「二次转义」（`A&B` → `A&amp;B`）的成因：layui 2.13.9 的表
   * 原型配置是 `escape:!0`，**不挂 `templet` 的数据列被当纯文本转义**，而本页的单元格值
   * 早已由 `buildSendRow()` 转义拼好（读码见 `@/layui-island/rawCell` 的模块 docstring）。
   *
   * 新断言**没有放宽**：列顺序仍逐列对拍，且把每个数据列的 `templet` **逐个**钉住
   * （`SendResult` 用自己的 `resultTemplet`；其余数据列**原样**透传 `buildSendRow(行)[field]`）。
   */
  it('⭐ `buildRenderCols()`：`SendResult` 保留自己的 `resultTemplet`，其余数据列挂**原样** templet', () => {
    const render = buildRenderCols()
    // 列顺序 / `field` 序列与证据列逐字相同（派生不动证据）。
    expect(render.map((c) => c.field)).toEqual(COLS_FOR_TEST.map((c) => c.field))

    const mapped = buildSendRow({ ...ROW, remark: 'A&B', send_params: 'A&B' } as never)
    // ⚠️ 循环里**不调 `expect`**（`vitest/no-conditional-expect`）：先把「不合格的列」收集成
    //    清单，循环外再断言清单为空——判别力不变（漏挂 / 被转义加工都会落到某个清单里）。
    const rawFields: string[] = []
    const fieldlessWithTemplet: string[] = []
    const withoutTemplet: string[] = []
    const notVerbatim: string[] = []
    for (const [index, col] of render.entries()) {
      const field = col.field
      if (typeof field !== 'string' || field === '') {
        // 勾选列：挂 templet 只会取到 `undefined`。
        if ('templet' in col) fieldlessWithTemplet.push(`第 ${index + 1} 列`)
        continue
      }
      // ⚠️ 这一列**不是**原样透传：它有自己的 `resultTemplet`（下面单独断，见相邻用例）。
      if (field === 'SendResult') continue
      rawFields.push(field)
      if (typeof col.templet !== 'function') {
        withoutTemplet.push(field)
        continue
      }
      const value = (col.templet as (r: never) => unknown)(mapped as never)
      if (value !== mapped[field as keyof SendCellRow]) notVerbatim.push(`${field}: ${String(value)}`)
    }
    // 非空哨兵：8 个原样透传的数据列**逐个**都在（少一列 / 多一列这里立刻红）。
    expect(rawFields).toEqual([
      'TaobaoOrderSendLogID',
      'Order_Id',
      'TbOrderNo',
      'SendResult_s',
      'TbStoreName',
      'SendDateEx',
      'UserID',
      'Remark',
    ])
    expect(fieldlessWithTemplet).toEqual([])
    // 承重：其余数据列漏挂 templet ⇒ 这一条红（真 layui 会把它当纯文本转义）。
    expect(withoutTemplet).toEqual([])
    // 承重：templet 必须**逐字**等于行映射的产物（再转一次义就不等）。
    expect(notVerbatim).toEqual([])
    // ⚠️ 「发货结果」那一列**保留自己的** `resultTemplet`（不是原样透传）：这里直接钉它两档
    // （相邻用例有同款断言，两处都在，任一处被误改都会红）。
    const sendCol = render.find((c) => c.field === 'SendResult')!
    expect(typeof sendCol.templet).toBe('function')
    expect((sendCol.templet as (d: Record<string, unknown>) => string)({ SendResult: 1 })).toBe(
      '<span style="color:#5BB75B">成功</span>',
    )
    expect((sendCol.templet as (d: Record<string, unknown>) => string)({ SendResult: 0 })).toBe(
      '<span style="color:#ff0000">失败</span>',
    )
    // 行映射的产物里已经有 `&amp;`（`A&B` → `A&amp;B`），templet **原样**透传
    // ⇒ 真 layui 不会把 `&` 再转一次（那正是「二次转义」的症状）。
    expect(mapped.Remark).toBe('A&amp;B')
    expect(mapped.SendResult_s).toBe('A&amp;B')
    const remarkCol = render.find((c) => c.field === 'Remark')!
    expect((remarkCol.templet as (r: never) => unknown)(mapped as never)).toBe('A&amp;B')
  })

  it('「发货结果」templet 逐字照 L70-77：1 ⇒ 绿字成功、0/其它 ⇒ 红字失败', () => {
    const col = buildRenderCols().find((c) => c.field === 'SendResult')!
    const templet = col.templet as (d: Record<string, unknown>) => string
    expect(templet({ SendResult: 1 })).toBe('<span style="color:#5BB75B">成功</span>')
    expect(templet({ SendResult: 0 })).toBe('<span style="color:#ff0000">失败</span>')
  })
})

describe('淘宝发货记录：挂载、三条陷阱与表格接线（spec §3.7/§3.8）', () => {
  it('挂载后：表格 / 工具栏 / 4 个筛选控件都在 DOM 里，且日期控件按对标 `type: date` 渲染两次', async () => {
    const root = await mountPage()
    expect(root.querySelector(TABLE_ELEM)).not.toBeNull()
    expect(root.querySelector(TOOLBAR_ELEM)).not.toBeNull()
    for (const name of ['sDate', 'eDate', 'result', 'key']) {
      expect(root.querySelector(`[name="${name}"]`), `缺筛选控件 ${name}`).not.toBeNull()
    }
    expect(LAYUI.laydate.render).toHaveBeenCalledTimes(2)
    // ⚠️ 本页是**日期**（`tbsendrecordmang.js` L21-29），⛔ 不是回收站页那种 `datetime`。
    expect(LAYUI.laydate.render.mock.calls.map((c) => c[0])).toEqual([
      { elem: '#sDate', type: 'date' },
      { elem: '#eDate', type: 'date' },
    ])
  })

  it('陷阱 ③：`bootstrapIsland()` 跑在塞标记**之前**、`initIslandElements()` 跑在**之后**', async () => {
    await mountPage()
    expect(
      LAYUI.state.bootstrapSawMarkup,
      'bootstrapIsland() 跑的时候标记已经在了 ⇒ 样式注入晚于标记（FOUC）',
    ).toBe(false)
    expect(
      LAYUI.state.initSawMarkup,
      'initIslandElements() 跑的时候标记还没进 DOM ⇒ 页签/折叠面板绑了个空',
    ).toBe(true)
  })

  it('两个日期框的默认值是**运行期算**的（证据件里是快照当天的死值）', async () => {
    const root = await mountPage()
    const range = defaultSendDateRange()
    expect(root.querySelector<HTMLInputElement>('#sDate')!.value).toBe(range.sDate)
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe(range.eDate)
    // 非空哨兵：两端真有个 `YYYY-MM-DD` 形状（全空的话上面两条会以「空 == 空」变恒真）。
    expect(range.sDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(range.eDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('默认区间 = 今天 −2 天 ~ 今天（含跨月/跨年归一化）', () => {
    expect(defaultSendDateRange(new Date(2026, 9, 3))).toEqual({
      sDate: '2026-10-01',
      eDate: '2026-10-03',
    })
    // 跨月：10 月 1 日 −2 天 = 9 月 29 日；跨年：1 月 1 日 −2 天 = 上一年的 12 月 30 日。
    expect(defaultSendDateRange(new Date(2026, 9, 1)).sDate).toBe('2026-09-29')
    expect(defaultSendDateRange(new Date(2026, 0, 1)).sDate).toBe('2025-12-30')
  })

  it('`table.render` 拿到的接线参数：url/id/method/request/toolbar/page/cols（层数也钉住）', async () => {
    await mountPage()
    const opts = renderOpts()
    // ⚠️ 陷阱 ①：`elem` 是选择器（带 `#`），`id` 是实例 id（不带）——两个常量各司其职。
    expect(opts.elem).toBe(TABLE_ELEM)
    expect(opts.id).toBe(TABLE_ID)
    // ⛔ 钉**字面值**、不钉 `TB_SEND_LOGS_URL` 那个常量：拿常量比自己会让「常量被改成错的路径」
    //    这条用例**照样绿**（自指断言，E-0024）。vitest 里 `import.meta.env.BASE_URL` 是 `'/'`。
    expect(opts.url).toBe('/api/business/tb-send-logs/')
    expect(opts.method).toBe('get')
    // 后端分页参数名是 `page` / `page_size`（`EnvelopePagination.page_size_query_param`）。
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(opts.toolbar).toBe(TOOLBAR_ELEM)
    expect(opts.defaultToolbar).toEqual(['filter'])
    // ⛔ 同样钉字面值 `[50]`（不是 `PAGE_LIMITS`）：见上面那条注释（E-0024）。
    expect(opts.page).toEqual({ limit: 50, limits: [50] })
    // ⛔ `cols` 必须是「数组的数组」（layui 契约）：扁平数组会在真浏览器里抛
    //    `Cannot create property 'key' on string 'checkbox'` ⇒ 整页只剩那句错。
    expect(opts.cols).toHaveLength(1)
    expect(opts.cols![0]).toEqual(buildRenderCols())
    // ⛔ 本页**没有**本地 `data`：有它 layui 会走本地数据分支、分页条的 count 变成本页行数。
    expect('data' in opts).toBe(false)
  })

  it('首屏 `where` 就带页面上那 4 个筛选（两个日期框的默认值 + result=-1）', async () => {
    const root = await mountPage()
    const where = renderOpts().where!
    const range = defaultSendDateRange()
    expect(where.sDate).toBe(root.querySelector<HTMLInputElement>('#sDate')!.value)
    expect(where.eDate).toBe(root.querySelector<HTMLInputElement>('#eDate')!.value)
    expect(String(where.sDate)).toBe(range.sDate)
    expect(where.result).toBe('-1')
    // `key` 是空串 ⇒ **不出现**（空值不发；`result` 的 `'0'` 那种合法值反过来必须照发）。
    expect('key' in where).toBe(false)
  })

  it('`parseData` 吃**统一信封**：`count` 取 `data.total`、`data` 取 `data.items` 并映射成表格行', async () => {
    await mountPage()
    const parseData = renderOpts().parseData!
    const parsed = parseData({
      code: 0,
      msg: '',
      data: { items: [ROW as never, FAIL_ROW as never], total: 2 },
    })
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(2)
    expect(parsed.data).toHaveLength(2)
    // 定位键（不发往列）与「记录ID」列都取后端 `id`。
    expect(parsed.data[0]!.send_log_id).toBe(11)
    expect(parsed.data[0]!.TaobaoOrderSendLogID).toBe(11)
    expect(parsed.data[0]!.SendResult).toBe(1)
    // 发货时间按本仓约定格式化成 `YYYY-MM-DD HH:mm`。
    expect(parsed.data[0]!.SendDateEx).toBe('2026-10-03 12:55')
    // 缺 `data` 时按 0 行（不抛）。
    expect(parseData({ code: 0, msg: '' }).data).toEqual([])
    expect(parseData({ code: 0, msg: '' }).count).toBe(0)
  })

  it('`toSendLogSearch`：「空值不发」但 `result` 的 `0` / `-1` 照发（E-0021），`key` 去掉首尾空白', () => {
    expect(toSendLogSearch({ result: '0' })).toEqual({ result: '0' })
    expect(toSendLogSearch({ result: '-1' })).toEqual({ result: '-1' })
    expect(toSendLogSearch({ sDate: '', eDate: '  ', key: '   ' })).toEqual({})
    expect(toSendLogSearch({ key: ' 20261003000000000502 ' })).toEqual({
      key: '20261003000000000502',
    })
  })

  it('夹具的键 = 后端序列化器契约（spec §3.1；⛔ 不自带后端不返回的键）', () => {
    expect([...Object.keys(ROW)].sort()).toEqual([...TB_SEND_LOG_ROW_KEYS].sort())
    // 行映射真的把每个后端字段读出来了（缺键时是空串，不是字面 `undefined`）。
    const mapped = buildSendRow(ROW as never)
    expect(mapped.Order_Id).toBe(ROW.order_no)
    expect(mapped.TbOrderNo).toBe(ROW.tb_order_no)
    // 「发货原参数」是 JSON 文本 ⇒ 里面的 `"` 被 `escapeHtml` 转成 `&quot;`（这一格的数据列
    // 挂了**原样透传的 `templet`** ⇒ layui 把行映射转义后的值原样插入，见下一条用例）。
    expect(mapped.SendResult_s).toBe(escapeHtml(ROW.send_params as string))
    expect(mapped.SendResult_s).toContain('&quot;send_way&quot;')
    expect(mapped.TbStoreName).toBe(ROW.store_name)
    expect(mapped.UserID).toBe(ROW.operator)
    expect(mapped.Remark).toBe('')
    // `sent_at` 为 `null` 时不该拼出字面 `null`。
    expect(buildSendRow({ ...ROW, sent_at: null } as never).SendDateEx).toBe('')
  })

  it('行映射对请求数据过 `escapeHtml`（数据列挂原样 `templet` ⇒ 不转义就是存储型 XSS 入口）', () => {
    const mapped = buildSendRow({
      ...ROW,
      remark: '<img src=x onerror=alert(1)>',
    } as never)
    expect(mapped.Remark).toBe('&lt;img src=x onerror=alert(1)&gt;')
  })
})

describe('淘宝发货记录：三颗工具栏按钮的**真交互**（spec §3.8 / §4-6）', () => {
  it('「刷新数据」：按**当前筛选**重新拉**第一页**（where 就是筛选控件现在的值）', async () => {
    const root = await mountPage()
    // 真人改筛选：两个日期框 + 结果选「失败」(0) + 搜索词带首尾空白。
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-10-01'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-10-02'
    root.querySelector<HTMLSelectElement>('#result')!.value = '0'
    root.querySelector<HTMLInputElement>('#key')!.value = ' 20261003000000000502 '
    LAYUI.table.reload.mockClear()

    await driveToolbar('refresh')

    const reloaded = lastReload()
    expect(reloaded.where).toEqual({
      sDate: '2026-10-01',
      eDate: '2026-10-02',
      // ⚠️ `'0'`（失败）是合法值 ⇒ 必须照发（真值判断会把它静默丢掉，E-0021）。
      result: '0',
      key: '20261003000000000502',
    })
    // 「重新拉第一页」（spec §3.8）。
    expect(reloaded.page.curr).toBe(1)
    // 每页条数取 layui 维护的实时值（用户改过「条/页」后不许被常量顶掉）。
    expect(reloaded.page.limit).toBe(50)
    // 刷新**不发任何写请求**。
    expect(requestCalls()).toEqual([])
  })

  it('「重试发货」：无勾选 ⇒ 逐字提示 + **零请求**；有勾选 ⇒ POST retry(ids) + 反馈 + 刷新列表', async () => {
    await mountPage()

    // ① 一笔都没勾（对标 `tbsendrecordmang.js` L113-118 的文案，逐字）。
    await driveToolbar('send')
    expect(LAYUI.msgs).toContain(SEND_ROW_SELECT_TEXT)
    expect(requestCalls(), '没勾选就发请求 = 让用户撞后端 1000').toEqual([])

    // ② 勾两笔 ⇒ 发它们的**记录 id**。
    LAYUI.msgs.length = 0
    LAYUI.setRows([{ send_log_id: 11 }, { send_log_id: 12 }])
    LAYUI.table.reload.mockClear()
    await driveToolbar('send')

    expect(requestCalls()).toEqual([
      {
        method: 'post',
        url: '/api/business/tb-send-logs/retry/',
        data: { ids: [11, 12] },
      },
    ])
    // 成功反馈：计数键取对标那句 alert 的载荷形状（`success` / `fail`）。
    expect(LAYUI.msgs).toContain('成功1条，失败1条')
    // 成功后刷新列表（本页约定：就地刷新当前页）。
    expect(LAYUI.table.reload).toHaveBeenCalled()
    expect(lastReload().page.curr).toBe(1)
  })

  it('「重试发货」失败：原样显示错误、且**不刷新**列表（不许刷新成假成功）', async () => {
    await mountPage()
    LAYUI.setRows([{ send_log_id: 11 }])
    LAYUI.table.reload.mockClear()
    vi.mocked(request).mockRejectedValueOnce(new Error('没有权限执行该操作'))

    await driveToolbar('send')

    expect(LAYUI.msgs).toContain('没有权限执行该操作')
    expect(LAYUI.table.reload, '写请求失败却刷新了列表（会把失败读成成功）').not.toHaveBeenCalled()
  })

  it('「导入文件发货」：照对标渲染（不置灰、不隐藏）、点了提示尚未开放、**零请求**', async () => {
    const root = await mountPage()
    // ⚠️ 工具栏是**模板**（`<script type="text/html" id="toolbarDemo">`，对标 HTML L168-175）——
    // 真 layui 渲染它之后才有活的 `<button>`，jsdom 里（layui 是替身）只能在**模板原文**上判。
    // 判据仍然实义：这颗按钮**在页面的标记里**、带对标那三个 class、且**没有** `disabled`、
    // 也没有 `layui-hide`（D-0025：不置灰、不隐藏）。
    const template = root.querySelector<HTMLScriptElement>('script#toolbarDemo')
    expect(template, '工具栏模板必须随证据件一起注入').not.toBeNull()
    const markup = template!.textContent ?? ''
    expect(markup, '「导入文件发货」按钮必须照对标渲染出来').toContain('lay-event="import"')
    expect(markup).toContain('layui-btn layui-btn-xs layui-btn-normal')
    expect(markup).not.toContain('disabled')
    expect(markup).not.toContain('layui-hide')

    await driveToolbar('import')

    expect(LAYUI.msgs).toContain(SEND_IMPORT_NOT_OPEN_TEXT)
    // 无后端就只提示：⛔ 不发请求、也⛔ 不开弹层（`#hidDiv` 那套上传本轮不做）。
    expect(requestCalls()).toEqual([])
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
  })

  it('`done` 只做对标那件事（`page = curr`）：`#total` 恒空、页码被记下来给就地刷新用', async () => {
    const root = await mountPage()
    // ⚠️ `#total` 也躺在**工具栏模板**里（对标 HTML L174）⇒ 真 layui 渲染后才有活的那个 div。
    // 这里手工放一个同名 div 当**探针**：`done` 若去写它，下面这条会红（等价于「真页面里那个
    // div 会显示统计文案」——而对标 `done` 从不写它，JS L92-94）。
    const total = document.createElement('div')
    total.id = 'total'
    root.appendChild(total)
    const done = renderOpts().done!
    done(undefined, 3)
    expect(total.innerHTML, '对标 `done` 不往 `#total` 写任何文案 ⇒ 我们也不写').toBe('')
    // 页码被记下来了：重试成功后 `reload(currentPage)` 用的就是它（3，不是 1）。
    LAYUI.setRows([{ send_log_id: 11 }])
    LAYUI.table.reload.mockClear()
    await driveToolbar('send')
    expect(lastReload().page.curr).toBe(3)
  })
})
