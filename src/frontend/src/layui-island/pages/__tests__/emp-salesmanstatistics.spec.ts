// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SupportStatsPayload } from '@/api/supportStats'

/**
 * 【客服接发单统计】island 页的契约测试（2026-10-17，客服统计 + 经营报表线 T3）。
 *
 * 这一页 2026-10-17 起是**自有实现**（原先走 `artifactPage` 渲染证据件、零 API 调用），
 * 所以没有证据件可对拍；本文件钉的是**有真实后果**的约定：
 *
 * 1. **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    改两个日期框 + 点「查询」⇒ **真发一条 GET**，且**查询串逐字正确**（键名是 spec §6.1 冻结的
 *    `date_from` / `date_to`），响应接着**真灌进两张表**（`table.reloadData` 的 `data` 就是返回值）；
 * 2. **首屏也要有**：默认窗 = 最近 30 天（含今天，spec §4）；
 * 3. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量、先挂样式→再塞标记→最后
 *    `initIslandElements`（以**运行期 DOM** 判断，不是读源码）；
 * 4. 两张表的列 / 空数据文案 / `page:false` / `totalRow` 都是 spec §4 点名的东西。
 *
 * ## 手法（照 `system-perfanalysis.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `table.render` /
 *   `table.reloadData` / `laydate.render` / `layer.msg` 的**入参**才能被断言；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/supportStats`（URL 与查询串的**唯一**
 *   拼装处）跑的是**真实现** ⇒ 「查询串对不对」这条判据才有意义（把 api 模块也换成替身的话，
 *   它只是在断言自己传了一个对象）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：客服名 / 店铺名都不是真租户数据。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const laydateRenders: Array<Record<string, unknown>> = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    msg,
    tableRenders,
    reloads,
    laydateRenders,
    seenByBootstrap,
    seenByInit,
    form: { render: vi.fn() },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
    },
    laydate: {
      render: vi.fn((opts: Record<string, unknown>) => {
        laydateRenders.push(opts)
      }),
    },
    layer: { msg: vi.fn(), closeAll: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#empStatsQueryBtn') !== null)
    return {
      form: LAYUI.form,
      table: LAYUI.table,
      laydate: LAYUI.laydate,
      layer: LAYUI.layer,
      $: LAYUI.$,
    }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#empStatsQueryBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { HttpError } from '@/api/errors'
import { request } from '@/api/http'
import { SUPPORT_STATS_URL, supportStatsUrl } from '@/api/supportStats'
import page, {
  DATE_FROM_INPUT_ID,
  DATE_TO_INPUT_ID,
  DEFAULT_RANGE_DAYS,
  EMPTY_TEXT,
  OPERATOR_TABLE_ELEM,
  OPERATOR_TABLE_ID,
  PANEL_CLASS,
  PANEL_SELECTOR,
  QUERY_BUTTON_ID,
  SOURCE_TABLE_ELEM,
  SOURCE_TABLE_ID,
  __testables,
} from '../emp-salesmanstatistics'

const {
  buildHtml,
  defaultDateRange,
  operatorCols,
  operatorTotalValues,
  sourceCols,
  sourceTotalValues,
} = __testables
const mockedRequest = vi.mocked(request)

/**
 * 一份响应（键名照 spec §6.1，**合成值**）。
 *
 * ⚠️ 显式类型标注是**承重的**：它就是「§6.1 那 5 个键一个不少」的编译期判据——
 * 后端契约里改键名（或有人把 `operators` 写成 `users`）时，这份完整字面量会缺键而报错。
 * ⛔ 别把标注删掉换成 `as`。
 */
const PAYLOAD: SupportStatsPayload = {
  range: { date_from: '2026-09-18', date_to: '2026-10-17' },
  operators: [
    {
      name: '客服A',
      order_count: 3,
      total_price: '300.00',
      total_hall_price: '180.00',
      performance: '120.00',
    },
    {
      name: '客服B',
      order_count: 1,
      total_price: '50.00',
      total_hall_price: '60.00',
      performance: '-10.00',
    },
  ],
  operators_total: {
    name: '合计',
    order_count: 4,
    total_price: '350.00',
    total_hall_price: '240.00',
    performance: '110.00',
  },
  sources: [
    { name: '淘宝店', order_count: 2, total_price: '200.00', total_hall_price: '120.00' },
    { name: '未指定', order_count: 2, total_price: '150.00', total_hall_price: '120.00' },
  ],
  sources_total: {
    name: '合计',
    order_count: 4,
    total_price: '350.00',
    total_hall_price: '240.00',
  },
}

/** 第二次查询的响应：行与金额都不同 ⇒ 「响应真的灌进表里」这条判据才分得开。 */
const PAYLOAD_2: SupportStatsPayload = {
  ...PAYLOAD,
  range: { date_from: '2026-10-01', date_to: '2026-10-02' },
  operators: [
    {
      name: '客服C',
      order_count: 9,
      total_price: '900.00',
      total_hall_price: '800.00',
      performance: '100.00',
    },
  ],
  sources: [
    { name: '拼多多店', order_count: 9, total_price: '900.00', total_hall_price: '800.00' },
  ],
}

function flush(): Promise<void> {
  // 页面的取数链是**多跳**的（点击 → request → 铺两张表）：连放几轮宏任务把整条链跑完。
  return (async () => {
    for (let i = 0; i < 5; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

/** 某张表的 `table.render` 配置（按 `elem` 认领；没有就抛，避免静默空判据）。 */
function renderOpts(elem: string): Record<string, unknown> {
  const found = LAYUI.tableRenders.find((r) => r.elem === elem)
  if (!found) throw new Error(`没有对 ${elem} 调用过 table.render`)
  return found
}

/** 某张表的列（`cols[0]`）。 */
function colsOf(elem: string): Array<Record<string, unknown>> {
  const cols = renderOpts(elem).cols as Array<Array<Record<string, unknown>>>
  return cols[0]!
}

/** 某列标题序列。 */
function titles(elem: string): Array<string | undefined> {
  return colsOf(elem).map((c) => c.title as string | undefined)
}

/** 某列 `templet`（用它渲染一行，钉「服务端文本怎么上屏」）。 */
function templetOf(elem: string, index: number): (row: never) => string {
  const fn = colsOf(elem)[index]!.templet as ((row: never) => string) | undefined
  if (fn === undefined) throw new Error(`${elem} 的第 ${index} 列没有 templet`)
  return fn
}

/** 最近一次查询实际发出的 URL。 */
function lastUrl(): string {
  const calls = mockedRequest.mock.calls
  const config = calls[calls.length - 1]![0] as { method?: string; url?: string }
  return config.url ?? ''
}

describe('客服接发单统计 island 页', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.laydateRenders.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    // 默认日期必须在**确定的一天**上断言字面值（否则用例会随运行日期漂移）：
    // 往回 29 个自然日 = 2026-09-18。
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 9, 17, 13, 0, 0))
    mockedRequest.mockResolvedValue(PAYLOAD)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  async function mount(): Promise<void> {
    await page.mount(root)
    await flush()
  }

  // ── 默认时间窗（纯函数 + 上屏值）─────────────────────────────

  it(`默认时间范围 = 最近 ${DEFAULT_RANGE_DAYS} 天（含今天：today-29d ~ today）`, () => {
    // 用**固定的一天**算，钉字面值：2026-10-17 → 2026-09-18（跨月，9 月 30 天）。
    expect(defaultDateRange(new Date(2026, 9, 17))).toEqual({
      dateFrom: '2026-09-18',
      dateTo: '2026-10-17',
    })
    // 跨月边界再来一个：2026-03-02 → 2026-02-01（2 月 28 天，按自然日回退 29 天）。
    expect(defaultDateRange(new Date(2026, 2, 2))).toEqual({
      dateFrom: '2026-02-01',
      dateTo: '2026-03-02',
    })
    // 跨年边界：2026-01-05 → 2025-12-07。
    expect(defaultDateRange(new Date(2026, 0, 5))).toEqual({
      dateFrom: '2025-12-07',
      dateTo: '2026-01-05',
    })
  })

  it('挂载时两个日期框填上默认值，且首屏就用它取了一次数', async () => {
    await mount()
    expect(root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value).toBe('2026-09-18')
    expect(root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value).toBe('2026-10-17')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(lastUrl()).toBe(`${SUPPORT_STATS_URL}?date_from=2026-09-18&date_to=2026-10-17`)
  })

  // ── 驱动真交互（本文件的核心）──────────────────────────────

  it('改日期框 + 点「查询」⇒ 真发 GET 且**查询串逐字正确**（键名 = spec §6.1 冻结的 date_from / date_to）', async () => {
    await mount()
    mockedRequest.mockClear()
    LAYUI.reloads.length = 0
    mockedRequest.mockResolvedValue(PAYLOAD_2)

    root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value = '2026-10-01'
    root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value = '2026-10-02'
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()

    expect(mockedRequest).toHaveBeenCalledTimes(1)
    const config = mockedRequest.mock.calls[0]![0] as { method?: string; url?: string }
    expect(config.method).toBe('get')
    // ⚠️ 字面值（不引 `supportStatsUrl(...)` 现算）：现算会让这条断言在**参数名被改坏时**
    //    跟着一起改、照样绿（E-0024 族）。这里的期望是**手写**的。
    expect(config.url).toBe(`${SUPPORT_STATS_URL}?date_from=2026-10-01&date_to=2026-10-02`)
  })

  it('响应的两张表**原样灌进**两张表（`reloadData` 的 data 就是返回值）', async () => {
    await mount()
    expect(LAYUI.reloads).toEqual([
      { id: OPERATOR_TABLE_ID, data: PAYLOAD.operators },
      { id: SOURCE_TABLE_ID, data: PAYLOAD.sources },
    ])
    // 再查一次：换一份响应 ⇒ 表里换的是**新返回值**（不是夹具里那批）。
    LAYUI.reloads.length = 0
    mockedRequest.mockResolvedValue(PAYLOAD_2)
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    expect(LAYUI.reloads).toEqual([
      { id: OPERATOR_TABLE_ID, data: PAYLOAD_2.operators },
      { id: SOURCE_TABLE_ID, data: PAYLOAD_2.sources },
    ])
  })

  it('连点查询：后返回的旧响应不覆盖新结果（I4）', async () => {
    await mount()
    LAYUI.reloads.length = 0
    let resolveFirst!: (payload: SupportStatsPayload) => void
    let resolveSecond!: (payload: SupportStatsPayload) => void
    mockedRequest
      .mockImplementationOnce(
        () => new Promise<SupportStatsPayload>((resolve) => { resolveFirst = resolve }) as never,
      )
      .mockImplementationOnce(
        () => new Promise<SupportStatsPayload>((resolve) => { resolveSecond = resolve }) as never,
      )
    const button = root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!
    button.click()
    button.click()
    // 第二发先回来 ⇒ 上屏；第一发后回来 ⇒ 必须被序号闸丢掉。
    resolveSecond(PAYLOAD_2)
    await flush()
    resolveFirst(PAYLOAD)
    await flush()
    expect(LAYUI.reloads).toEqual([
      { id: OPERATOR_TABLE_ID, data: PAYLOAD_2.operators },
      { id: SOURCE_TABLE_ID, data: PAYLOAD_2.sources },
    ])
  })

  it('两个日期框都清空 ⇒ **一个参数都不发**（后端按它自己的默认窗回，spec §6.1）', async () => {
    await mount()
    mockedRequest.mockClear()
    root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value = ''
    root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value = ''
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    // 不带 `?`：这是**空参数**的形状（与「发两个空串参数」是两件事——后者后端会判畸形）。
    expect(lastUrl()).toBe(SUPPORT_STATS_URL)
  })

  it('取数失败 ⇒ 如实提示、且页面不清空（不把失败伪装成空结果）', async () => {
    await mount()
    mockedRequest.mockRejectedValue(new HttpError(403, '没有权限执行该操作'))
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
    // 按钮还在（不是「点了没反应」那种静默），表里还是上一次的数据（没被清成空表）。
    expect(root.querySelector(`#${QUERY_BUTTON_ID}`)).not.toBeNull()
    expect(LAYUI.reloads[LAYUI.reloads.length - 1]!.data).toEqual(PAYLOAD.sources)
  })

  it('空结果不是失败：一行都不弹提示，空表靠 `text.none` 文案说「这段时间没有订单」', async () => {
    mockedRequest.mockResolvedValue({
      ...PAYLOAD,
      operators: [],
      sources: [],
    })
    await mount()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(renderOpts(OPERATOR_TABLE_ELEM).text).toEqual({ none: EMPTY_TEXT })
    expect(renderOpts(SOURCE_TABLE_ELEM).text).toEqual({ none: EMPTY_TEXT })
    expect(LAYUI.reloads).toEqual([
      { id: OPERATOR_TABLE_ID, data: [] },
      { id: SOURCE_TABLE_ID, data: [] },
    ])
  })

  // ── 两张表的形状（spec §4）───────────────────────────────────

  it('两张表的列标题就是 spec §4 那几列（卡 2 **没有**预计绩效列）', async () => {
    await mount()
    expect(titles(OPERATOR_TABLE_ELEM)).toEqual([
      '操作人员',
      '总订单数',
      '总接单价',
      '总发单价',
      '预计绩效',
    ])
    expect(titles(SOURCE_TABLE_ELEM)).toEqual(['店铺', '总订单数', '总接单价', '总发单价'])
    // 不翻页（一次给全量）、要合计行、工具栏只留列筛选。
    for (const elem of [OPERATOR_TABLE_ELEM, SOURCE_TABLE_ELEM]) {
      expect(renderOpts(elem).page).toBe(false)
      expect(renderOpts(elem).totalRow).toBe(true)
      expect(renderOpts(elem).defaultToolbar).toEqual(['filter'])
      // ⛔ 不给 `url`：给了之后 `reloadData(id, {data})` 会走 ajax 分支、忽略我们传的 data。
      expect(renderOpts(elem).url).toBeUndefined()
    }
  })

  it('合计行用后端总计：0 也显示 "0.00"（layui 本地求和在 0 时会留空，I1）', () => {
    expect(
      operatorTotalValues({
        name: '合计',
        order_count: 0,
        total_price: '0.00',
        total_hall_price: '0.00',
        performance: '0.00',
      }),
    ).toEqual(['合计', '0', '0.00', '0.00', '0.00'])
    expect(
      sourceTotalValues({
        name: '合计',
        order_count: 2,
        total_price: '30.00',
        total_hall_price: '10.00',
      }),
    ).toEqual(['合计', '2', '30.00', '10.00'])
  })

  it('合计行：金额 / 数量列都 `totalRow:true`，第一列只写「合计」不参与求和', () => {
    // 表级 `totalRow:true` 只是开关；真正求和靠**列级** `totalRow`（layui `renderTotal` 读它）。
    // 漏一列的症状是「合计行里那一格是空的」，而页面上看不出来是配置漏了。
    const op = operatorCols()
    expect(op.map((c) => c.totalRow ?? false)).toEqual([false, true, true, true, true])
    expect(op[0]!.totalRowText).toBe('合计')
    const src = sourceCols()
    expect(src.map((c) => c.totalRow ?? false)).toEqual([false, true, true, true])
    expect(src[0]!.totalRowText).toBe('合计')
  })

  it('金额列不做 `templet`（两位小数字符串原样显示，⛔ 不再 toFixed 一次）', async () => {
    await mount()
    const op = colsOf(OPERATOR_TABLE_ELEM)
    // 第 0 列（操作人员）有 templet（要转义）；四个数字 / 金额列都没有。
    expect(op[0]!.templet).toBeTypeOf('function')
    for (const i of [1, 2, 3, 4]) expect(op[i]!.templet).toBeUndefined()
    const src = colsOf(SOURCE_TABLE_ELEM)
    expect(src[0]!.templet).toBeTypeOf('function')
    for (const i of [1, 2, 3]) expect(src[i]!.templet).toBeUndefined()
  })

  it('操作人员 / 店铺名那一格过 escapeHtml（服务端文本不当 HTML 拼）', async () => {
    await mount()
    const opCell = templetOf(OPERATOR_TABLE_ELEM, 0)
    expect(opCell(PAYLOAD.operators[0] as never)).toBe('客服A')
    // 名字来自服务端（显示名 / sales_agent 快照）⇒ 尖括号不许当 HTML。
    expect(opCell({ ...PAYLOAD.operators[0], name: '<b>x</b>' } as never)).toBe(
      '&lt;b&gt;x&lt;/b&gt;',
    )
    const srcCell = templetOf(SOURCE_TABLE_ELEM, 0)
    expect(srcCell({ name: '<img src=x>' } as never)).toBe('&lt;img src=x&gt;')
  })

  // ── 三条 island 陷阱（`AGENTS.md` §5-G）────────────────────

  it('三条 island 陷阱：先挂样式 → 再塞标记 → 最后 initIslandElements；类名与选择器分开', async () => {
    await mount()
    // ③a：`bootstrapIsland` 跑的时候页面标记**还不在** DOM 里（顺序反了会先画出没样式的骨架 = FOUC）。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    // ③b：`initIslandElements` 相反，必须在塞标记**之后**（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    expect(LAYUI.seenByInit).toEqual([true])
    // ①：类名常量拼进 `class=`、选择器常量用来查节点——混用会造出 `class=".x"`、**永远匹配不到**。
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `按 ${PANEL_SELECTOR} 匹配不到页面容器（class 里多半混进了点）`).not.toBeNull()
    expect(panel!.classList.contains(PANEL_CLASS)).toBe(true)
    expect(panel!.className).not.toContain('.')
    // ②：以 `(` 开头的语句必须前置分号——这条判据在**本页真跑起来**时由上面那些用例承载
    //     （解析失败会在 mount 阶段直接抛）。这里额外钉住「两个 laydate / 两个 table.render 都真的
    //     被调过」，证明那些前置分号的语句**执行到了**（不是被 ASI 吃掉后面那一句）。
    expect(LAYUI.laydateRenders.map((o) => o.elem)).toEqual([
      `#${DATE_FROM_INPUT_ID}`,
      `#${DATE_TO_INPUT_ID}`,
    ])
    expect(LAYUI.tableRenders.map((o) => o.elem)).toEqual([
      OPERATOR_TABLE_ELEM,
      SOURCE_TABLE_ELEM,
    ])
    // laydate 一律 `type:'date'`（值就是 `yyyy-MM-dd`，与页面的查询串同形状）。
    expect(LAYUI.laydateRenders.every((o) => o.type === 'date')).toBe(true)
  })

  it('只读：标记里没有导出 / 写入口，且从未发过非 GET 请求', async () => {
    await mount()
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    const methods = mockedRequest.mock.calls.map(
      (call) => (call[0] as { method?: string }).method,
    )
    expect(methods.length).toBeGreaterThan(0)
    expect(methods.every((m) => m === 'get')).toBe(true)
    const html = buildHtml()
    for (const word of ['导出', '删除', '新增', '保存']) {
      expect(html, `标记里出现了不该有的「${word}」入口`).not.toContain(word)
    }
  })

  // ── 纯函数（URL）─────────────────────────────────────────────

  it('URL 拼装：参数名是 spec §6.1 那两个，空值不发、顺序稳定', () => {
    expect(supportStatsUrl({})).toBe(SUPPORT_STATS_URL)
    expect(supportStatsUrl({ dateFrom: '', dateTo: '' })).toBe(SUPPORT_STATS_URL)
    expect(supportStatsUrl({ dateFrom: ' 2026-09-18 ' })).toBe(
      `${SUPPORT_STATS_URL}?date_from=2026-09-18`,
    )
    expect(supportStatsUrl({ dateFrom: '2026-09-18', dateTo: '2026-10-17' })).toBe(
      `${SUPPORT_STATS_URL}?date_from=2026-09-18&date_to=2026-10-17`,
    )
  })
})
