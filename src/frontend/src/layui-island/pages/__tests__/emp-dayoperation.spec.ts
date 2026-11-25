// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BusinessReportPayload } from '@/api/businessReport'

/**
 * 【经营报表】island 页的契约测试（2026-10-17，客服统计 + 经营报表线 T4）。
 *
 * 这一页是 2026-10-17 新增的（`REPLICA_SKIP` 里那条 `emp-dayoperation` 同一次摘掉），
 * 功能照抄对标 `/Emp/dayoperation`、数据接我们自己的 `GET /api/business/business-report`。
 * 本文件钉的是**有真实后果**的约定：
 *
 * 1. **6 个数字卡的文本就是接口给的那 6 个值**（键名照 spec §6.2；⛔ 不是页面自己算的）；
 * 2. **4 张图各 `setOption` 一次**，且第二个参数是 `true`（换数据不合并旧配置）；
 * 3. **查询里 `stat_type` 永远显式发**（`claim` 默认），**radio 切换会重发**（spec §5）；
 * 4. 加载态 `layer.load(1,{shade:false})` **一定要关**（失败也关，`finally`）；
 * 5. 失败 `layer.msg` 一句人话，不白屏。
 *
 * ## 手法（照 `system-perfanalysis.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui；
 * - `vi.mock('@/layui/vendor')`：formSelects 是第三方资产，换成可控替身后才能驱动「选了客服」；
 * - `vi.mock('@/api/businessReport', …importOriginal)`：**只换取数函数**，同一模块里的
 *   `DEFAULT_STAT_TYPE` / `businessReportUrl` 跑**真实现** ⇒ 「查询串长什么样」那条判据才有意义；
 * - `vi.mock('@/layui-island/echarts')`：**jsdom 不加载真脚本**（629 KB、且没有 canvas），
 *   换成记录 `init` / `setOption` 的替身。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：客服名 / 店铺名都不是真租户数据。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const layerLoad = vi.fn(() => 250)
  const layerClose = vi.fn()
  // ⚠️ 挂载顺序的运行期判据（`AGENTS.md` §5-G 陷阱 ③）。选择器写字面值：
  //    本工厂在页面模块求值**之前**就跑了，引页面常量会读到未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  const formOn: Array<{ event: string; cb: (obj: { value?: string }) => void }> = []
  return {
    msg,
    layerLoad,
    layerClose,
    seenByBootstrap,
    seenByInit,
    formOn,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: (obj: { value?: string }) => void) => {
        formOn.push({ event, cb })
      }),
    },
    laydate: { render: vi.fn() },
    layer: { msg: vi.fn(), load: layerLoad, close: layerClose, closeAll: vi.fn() },
    table: { render: vi.fn(), reloadData: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#dayReportSearch') !== null)
    return {
      form: LAYUI.form,
      table: LAYUI.table,
      laydate: LAYUI.laydate,
      layer: LAYUI.layer,
      $: LAYUI.$,
    }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#dayReportSearch') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

/** formSelects 替身：`data()` 灌候选、`value()` 读选中值（值形如 `"4,9"`）。 */
const FORM_SELECTS = vi.hoisted(() => ({
  data: vi.fn(),
  value: vi.fn((_id: string, _type?: string) => ''),
  render: vi.fn(),
}))
const INIT_FORM_SELECTS = vi.hoisted(() => vi.fn())
vi.mock('@/layui/vendor', () => ({
  initFormSelects: INIT_FORM_SELECTS,
  getFormSelects: () => FORM_SELECTS,
}))

/** 游戏下拉的字典（`fetchGames()` 走 `@/api/config`，已按查看者游戏权限收窄）。 */
const FETCH_GAMES = vi.hoisted(() => vi.fn())
vi.mock('@/api/config', () => ({ fetchGames: FETCH_GAMES }))

/** 只换**取数函数**（见文件头）。 */
const FETCH_REPORT = vi.hoisted(() => vi.fn())
vi.mock('@/api/businessReport', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/businessReport')>()
  return { ...actual, fetchBusinessReport: FETCH_REPORT }
})

/** ECharts 替身（jsdom 不加载真脚本）。 */
const ECHARTS = vi.hoisted(() => {
  const setOptions: Array<{ id: string; option: Record<string, unknown>; notMerge?: boolean }> = []
  const instances: Array<{ id: string; dispose: ReturnType<typeof vi.fn> }> = []
  const init = vi.fn((el: HTMLElement) => {
    const instance = {
      id: el.id,
      setOption: vi.fn((option: Record<string, unknown>, notMerge?: boolean) => {
        setOptions.push({ id: el.id, option, notMerge })
      }),
      dispose: vi.fn(),
    }
    instances.push(instance)
    return instance
  })
  return { setOptions, instances, init }
})
vi.mock('@/layui-island/echarts', () => ({
  loadECharts: vi.fn(async () => ({ init: ECHARTS.init })),
  __resetForTest: vi.fn(),
}))

import { HttpError } from '@/api/errors'
import { loadECharts } from '@/layui-island/echarts'
import { BUSINESS_REPORT_URL, businessReportUrl } from '@/api/businessReport'
import page, {
  CARD_SPECS,
  CHART_ID_PROFIT,
  CHART_ID_SOURCE,
  CHART_ID_SUPPORT,
  CHART_ID_TREND,
  DATE_FROM_INPUT_ID,
  DATE_TO_INPUT_ID,
  DEFAULT_RANGE_DAYS,
  GAME_SELECT_ID,
  KEFU_SELECT_ID,
  PANEL_CLASS,
  PANEL_SELECTOR,
  SEARCH_BUTTON_ID,
  STAT_TYPE_RADIO_FILTER,
  __testables,
  cardId,
} from '../emp-dayoperation'

const {
  buildHtml,
  defaultReportDateRange,
  formatGrowth,
  pieSlices,
  profitSlices,
  profitPieOption,
  sourcePieOption,
  supportOption,
  trendOption,
  trendTooltip,
} = __testables

const mockedFetchReport = vi.mocked(FETCH_REPORT)

/** 12 个自然月（`yyyy-MM`，连续、含本月）。 */
const MONTHS = [
  '2025-11',
  '2025-12',
  '2026-01',
  '2026-02',
  '2026-03',
  '2026-04',
  '2026-05',
  '2026-06',
  '2026-07',
  '2026-08',
  '2026-09',
  '2026-10',
]

/**
 * 一份响应（键名照 spec §6.2，**合成值**）。
 *
 * ⚠️ 显式类型标注是**承重的**：它就是「§6.2 那 7 个键一个不少」的编译期判据。
 * ⛔ 别把标注删掉换成 `as`。
 */
const PAYLOAD: BusinessReportPayload = {
  range: { date_from: '2026-09-18', date_to: '2026-10-17', stat_type: 'claim' },
  cards: {
    search_profit: '100.00',
    last_month_profit: '200.00',
    prev_month_profit: '0.00',
    three_month_profit: '300.00',
    year_profit: '400.00',
    search_refund: '10.00',
  },
  source_distribution: [
    { name: '淘宝店', amount: '200.00', profit: '70.00' },
    { name: '未指定', amount: '50.00', profit: '-10.00' },
  ],
  source_total: { amount: '250.00', profit: '60.00' },
  support_stats: {
    names: ['客服A', '客服B'],
    order_counts: [2, 1],
    amounts: ['200.00', '50.00'],
    refund_counts: [0, 1],
  },
  trend: {
    months: MONTHS,
    order_counts: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    amounts: MONTHS.map((_, i) => `${(i + 1) * 100}.00`),
    count_growth: MONTHS.map(() => '0.00'),
    amount_growth: MONTHS.map(() => '-12.50'),
  },
  kefu_options: [{ id: 4, name: '客服A' }],
}

/** 第二次查询的响应：数字全换 ⇒ 「卡文本真的来自响应」这条判据才分得开。 */
const PAYLOAD_2: BusinessReportPayload = {
  ...PAYLOAD,
  range: { date_from: '2026-10-01', date_to: '2026-10-02', stat_type: 'submit' },
  cards: {
    search_profit: '111.00',
    last_month_profit: '222.00',
    prev_month_profit: '333.00',
    three_month_profit: '444.00',
    year_profit: '555.00',
    search_refund: '66.00',
  },
  source_distribution: [{ name: '拼多多店', amount: '111.00', profit: '55.00' }],
  source_total: { amount: '111.00', profit: '55.00' },
  support_stats: { names: ['客服C'], order_counts: [7], amounts: ['111.00'], refund_counts: [2] },
  trend: {
    ...PAYLOAD.trend,
    order_counts: MONTHS.map(() => 7),
    amounts: MONTHS.map(() => '111.00'),
  },
  kefu_options: [{ id: 9, name: '客服C' }],
}

function flush(): Promise<void> {
  return (async () => {
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

/** 6 个卡的当前文本（按 `CARD_SPECS` 的顺序）。 */
function cardTexts(root: HTMLElement): string[] {
  return CARD_SPECS.map(
    (spec) => root.querySelector<HTMLElement>(`#${cardId(spec.key)}`)?.textContent ?? '',
  )
}

/** 最近一次取数用的查询对象。 */
function lastQuery(): Record<string, unknown> {
  const calls = mockedFetchReport.mock.calls
  return calls[calls.length - 1]![0] as Record<string, unknown>
}

describe('经营报表 island 页', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    LAYUI.formOn.length = 0
    ECHARTS.setOptions.length = 0
    ECHARTS.instances.length = 0
    FORM_SELECTS.value.mockReturnValue('')
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    // 默认日期钉在**确定的一天**：2026-10-17 往回 29 个自然日 = 2026-09-18。
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 9, 17, 13, 0, 0))
    mockedFetchReport.mockResolvedValue(PAYLOAD)
    FETCH_GAMES.mockResolvedValue([
      { id: 7, name: '游戏A', sort_order: 1, is_active: true, created_at: '' },
      { id: 8, name: '<b>游戏B</b>', sort_order: 2, is_active: true, created_at: '' },
    ])
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  async function mount(): Promise<void> {
    await page.mount(root)
    await flush()
  }

  // ── 纯函数 ──────────────────────────────────────────────────

  it(`默认时间范围 = 最近 ${DEFAULT_RANGE_DAYS} 天（含今天：today-29d ~ today）`, () => {
    expect(defaultReportDateRange(new Date(2026, 9, 17))).toEqual({
      dateFrom: '2026-09-18',
      dateTo: '2026-10-17',
    })
    // 跨月（2 月 28 天）：2026-03-02 → 2026-02-01。
    expect(defaultReportDateRange(new Date(2026, 2, 2))).toEqual({
      dateFrom: '2026-02-01',
      dateTo: '2026-03-02',
    })
    // 跨年：2026-01-05 → 2025-12-07。
    expect(defaultReportDateRange(new Date(2026, 0, 5))).toEqual({
      dateFrom: '2025-12-07',
      dateTo: '2026-01-05',
    })
  })

  it('饼图映射：两张饼取同一张来源表的**不同列**（amount / profit），字符串折成数字', () => {
    expect(pieSlices(PAYLOAD.source_distribution, 'amount')).toEqual([
      { name: '淘宝店', value: 200 },
      { name: '未指定', value: 50 },
    ])
    expect(pieSlices(PAYLOAD.source_distribution, 'profit')).toEqual([
      { name: '淘宝店', value: 70 },
      { name: '未指定', value: -10 },
    ])
    // 空 / 畸形一律 0（不抛、也不给 ECharts 一个 `NaN`）。
    expect(pieSlices([{ name: 'x', amount: '', profit: 'abc' }], 'amount')).toEqual([
      { name: 'x', value: 0 },
    ])
    expect(pieSlices([], 'amount')).toEqual([])
  })

  it('环比格式化：两位小数 + 百分号；空 / 畸形按 0.00%（不显示 NaN%）', () => {
    expect(formatGrowth('12.50')).toBe('12.50%')
    expect(formatGrowth('12.5')).toBe('12.50%')
    expect(formatGrowth('-3.25')).toBe('-3.25%')
    expect(formatGrowth('0.00')).toBe('0.00%')
    expect(formatGrowth('')).toBe('0.00%')
    expect(formatGrowth(undefined)).toBe('0.00%')
    expect(formatGrowth('abc')).toBe('0.00%')
  })

  it('4 张图的 option：标题 / 副标题 / 轴名 / 系列类型照 spec §5', () => {
    const source = sourcePieOption(PAYLOAD) as {
      title: { text: string; subtext: string }
      series: Array<{ type: string; data: unknown }>
    }
    expect(source.title.text).toBe('订单来源分布图')
    expect(source.title.subtext).toContain('[总计：250.00元]')
    expect(source.series[0]!.type).toBe('pie')
    expect(source.series[0]!.data).toEqual([
      { name: '淘宝店', value: 200 },
      { name: '未指定', value: 50 },
    ])

    const profit = profitPieOption(PAYLOAD) as {
      title: { text: string; subtext: string }
      series: Array<{ type: string; data: unknown }>
    }
    expect(profit.title.text).toBe('利润来源图')
    expect(profit.title.subtext).toBe(
      '已排除已退单的订单，其它所有状态均包含在内；亏损来源（利润为负）不计入本图',
    )
    // 负利润不进饼图（ECharts 3.6.2 的负数会画错，见页面注释 / 独立评审 B2）。
    expect(profit.series[0]!.data).toEqual([{ name: '淘宝店', value: 70 }])
    expect(profitSlices(PAYLOAD.source_distribution)).toEqual([
      { name: '淘宝店', value: 70 },
    ])

    const support = supportOption(PAYLOAD) as {
      title: { text: string }
      xAxis: Array<{ data: string[] }>
      yAxis: Array<{ name: string }>
      series: Array<{ name: string; type: string }>
    }
    expect(support.title.text).toBe('客服接单统计')
    expect(support.xAxis[0]!.data).toEqual(['客服A', '客服B'])
    expect(support.yAxis.map((axis) => axis.name)).toEqual(['单数', '总金额', '退单量'])
    expect(support.series.map((s) => s.type)).toEqual(['bar', 'bar', 'line'])
    expect(support.series.map((s) => s.name)).toEqual(['订单数', '总金额', '退单量'])

    const trend = trendOption(PAYLOAD) as {
      xAxis: Array<{ data: string[] }>
      yAxis: Array<{ name: string }>
      legend: { data: string[] }
      series: Array<{ name: string; type: string; data: number[] }>
    }
    // 12 个自然月、原样显示（不照抄对标的 `name + '月'`）。
    expect(trend.xAxis[0]!.data).toEqual(MONTHS)
    expect(trend.yAxis.map((axis) => axis.name)).toEqual(['总数量', '总金额', '环比增长'])
    expect(trend.legend.data).toEqual([
      '订单总数量',
      '订单总数量环比增长率',
      '订单总金额',
      '订单总金额环比增长率',
    ])
    expect(trend.series.map((s) => s.type)).toEqual(['bar', 'bar', 'line', 'line'])
    expect(trend.series[1]!.data[0]).toBe(100)
  })

  it('趋势图 tooltip 用 formatGrowth 显示环比（`{c2}%` 那种模板不再照抄）', () => {
    const html = trendTooltip([
      { axisValue: '2026-10', seriesName: '订单总数量', value: 12 },
      { axisValue: '2026-10', seriesName: '订单总金额', value: 1200 },
      { axisValue: '2026-10', seriesName: '订单总数量环比增长率', value: 12.5 },
      { axisValue: '2026-10', seriesName: '订单总金额环比增长率', value: -3.25 },
    ])
    expect(html).toContain('2026-10')
    expect(html).toContain('订单总数量:12，环比:12.50%')
    expect(html).toContain('订单总金额:1200，环比:-3.25%')
  })

  it('URL 拼装：参数名照 spec §6.2（date_from / date_to / stat_type / game / kefu），空值不发', () => {
    expect(businessReportUrl({})).toBe(BUSINESS_REPORT_URL)
    expect(businessReportUrl({ dateTo: '' })).toBe(BUSINESS_REPORT_URL)
    expect(
      businessReportUrl({ dateFrom: '2026-09-18', dateTo: '2026-10-17', statType: 'submit' }),
    ).toBe(`${BUSINESS_REPORT_URL}?date_from=2026-09-18&date_to=2026-10-17&stat_type=submit`)
    // `game` / `kefu` 是可选的收窄；`kefu` 是**逗号分隔的主键串**（URLSearchParams 把它编码成 %2C）。
    expect(
      businessReportUrl({
        dateFrom: '2026-09-18',
        dateTo: '2026-10-17',
        statType: 'claim',
        game: 7,
        kefu: [4, 9],
      }),
    ).toBe(
      `${BUSINESS_REPORT_URL}?date_from=2026-09-18&date_to=2026-10-17&stat_type=claim&game=7&kefu=4%2C9`,
    )
  })

  // ── 首屏 ────────────────────────────────────────────────────

  it('挂载：首屏取一次数、6 个卡上屏、4 张图各 setOption 一次（notMerge=true）', async () => {
    await mount()

    expect(mockedFetchReport).toHaveBeenCalledTimes(1)
    // 默认窗 + 默认口径（`claim`）都显式发。
    expect(lastQuery()).toMatchObject({
      dateFrom: '2026-09-18',
      dateTo: '2026-10-17',
      statType: 'claim',
      game: null,
      kefu: [],
    })

    expect(cardTexts(root)).toEqual([
      '100.00',
      '200.00',
      '0.00',
      '300.00',
      '400.00',
      '10.00',
    ])
    // 卡标题与 tooltip 照 spec §5。
    expect(root.querySelector<HTMLElement>(`#${cardId('search_refund')}`)!.getAttribute('title')).toBe(
      '只统计搜索条件范围内的记录的退款金额',
    )
    expect(
      root.querySelector<HTMLElement>(`#${cardId('search_profit')}`)!.getAttribute('title'),
    ).toBe('计算方式：搜索条件范围内的总接单价-总发单价绩效-总记录退款金额')

    expect(ECHARTS.setOptions.map((o) => o.id)).toEqual([
      CHART_ID_SOURCE,
      CHART_ID_PROFIT,
      CHART_ID_SUPPORT,
      CHART_ID_TREND,
    ])
    expect(ECHARTS.setOptions.every((o) => o.notMerge === true)).toBe(true)
    // 4 个容器都真的渲染出 400px 高（spec §5）。
    for (const id of [CHART_ID_SOURCE, CHART_ID_PROFIT, CHART_ID_SUPPORT, CHART_ID_TREND]) {
      expect(root.querySelector<HTMLElement>(`#${id}`)!.style.height).toBe('400px')
    }
  })

  it('加载态：`layer.load(1,{shade:false})` 起、`finally` 里关（且只关自己的那个句柄）', async () => {
    await mount()
    expect(LAYUI.layerLoad).toHaveBeenCalledTimes(1)
    expect(LAYUI.layerLoad).toHaveBeenCalledWith(1, { shade: false })
    expect(LAYUI.layerClose).toHaveBeenCalledTimes(1)
    expect(LAYUI.layerClose).toHaveBeenCalledWith(250)
  })

  // ── 驱动真交互 ──────────────────────────────────────────────

  it('改日期 + 点「搜索」⇒ 用新日期再取一次，卡文本换成新响应', async () => {
    await mount()
    mockedFetchReport.mockClear()
    mockedFetchReport.mockResolvedValue(PAYLOAD_2)
    ECHARTS.setOptions.length = 0
    root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value = '2026-10-01'
    root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value = '2026-10-02'
    root.querySelector<HTMLButtonElement>(`#${SEARCH_BUTTON_ID}`)!.click()
    await flush()

    expect(mockedFetchReport).toHaveBeenCalledTimes(1)
    expect(lastQuery()).toMatchObject({
      dateFrom: '2026-10-01',
      dateTo: '2026-10-02',
      statType: 'claim',
    })
    expect(cardTexts(root)).toEqual([
      '111.00',
      '222.00',
      '333.00',
      '444.00',
      '555.00',
      '66.00',
    ])
    // 4 张图**再各画一次**（复用同 4 个实例，不重复 init）。
    expect(ECHARTS.setOptions).toHaveLength(4)
    expect(ECHARTS.instances).toHaveLength(4)
  })

  it('radio「按交单时间统计」⇒ 整页重发，`stat_type=submit`', async () => {
    await mount()
    mockedFetchReport.mockClear()
    mockedFetchReport.mockResolvedValue(PAYLOAD_2)
    ECHARTS.setOptions.length = 0
    // 标记里两个 radio 的 name / value 照 spec §5。
    expect(STAT_TYPE_OPTIONS_VALUES()).toEqual(['claim', 'submit'])
    const handler = LAYUI.formOn.find((entry) => entry.event === `radio(${STAT_TYPE_RADIO_FILTER})`)
    expect(handler, '页面没有注册 form.on("radio(statType)")').toBeDefined()
    handler!.cb({ value: 'submit' })
    await flush()

    expect(mockedFetchReport).toHaveBeenCalledTimes(1)
    expect(lastQuery()).toMatchObject({ statType: 'submit' })
    // 换口径后还是 4 张图各一次。
    expect(ECHARTS.setOptions).toHaveLength(4)
  })

  it('接单客服多选：候选来自接口 `kefu_options`（只灌一次），选中的主键进查询串', async () => {
    await mount()
    expect(FORM_SELECTS.data).toHaveBeenCalledTimes(1)
    expect(FORM_SELECTS.data).toHaveBeenCalledWith(KEFU_SELECT_ID, 'local', {
      arr: [{ name: '客服A', value: '4' }],
    })

    // 选两个人 ⇒ 查询里是**逗号分隔的主键串**（spec §6.2）。
    FORM_SELECTS.value.mockReturnValue('4,9')
    mockedFetchReport.mockClear()
    root.querySelector<HTMLButtonElement>(`#${SEARCH_BUTTON_ID}`)!.click()
    await flush()
    expect(lastQuery()).toMatchObject({ kefu: [4, 9] })
    // 真实 URL 拼装（同一模块的真实现）：逗号被百分号编码。
    expect(
      businessReportUrl({
        dateFrom: '2026-09-18',
        dateTo: '2026-10-17',
        statType: 'claim',
        kefu: [4, 9],
      }),
    ).toContain('kefu=4%2C9')
  })

  it('游戏下拉：第一项「全部游戏」，其余来自 `fetchGames()`（转义、默认选第一项）', async () => {
    await mount()
    const select = root.querySelector<HTMLSelectElement>(`#${GAME_SELECT_ID}`)!
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      '全部游戏',
      '游戏A',
      '<b>游戏B</b>',
    ])
    expect(select.selectedIndex).toBe(0)
    // 游戏名的尖括号**不当 HTML**：`<option>` 是 innerHTML 拼出来的。
    expect(select.innerHTML).toContain('&lt;b&gt;游戏B&lt;/b&gt;')
    expect(LAYUI.form.render).toHaveBeenCalledWith('select')
  })

  it('统计口径 radio 必须被 layui 渲染：`form.render()` 不带类型调一次（B1）', async () => {
    await mount()
    // 只调 `form.render('select')` 时 radio 仍 display:none、click 也不会绑 ⇒ 本页的
    // 「按交单时间统计」在真实浏览器里不存在。裸调一次才会渲染 radio。
    expect(LAYUI.form.render).toHaveBeenCalledWith()
  })

  it('tooltip 转义用户可写的来源名 / 客服名（ECharts 3.6.2 的 tooltip 是 innerHTML，I2）', () => {
    const hostile = '<img src=x onerror=alert(1)>'
    const source = sourcePieOption({
      ...PAYLOAD,
      source_distribution: [{ name: hostile, amount: '1.00', profit: '1.00' }],
    }) as { tooltip: { formatter: (p: Record<string, unknown>) => string } }
    const pieText = source.tooltip.formatter({
      seriesName: '订单来源',
      name: hostile,
      value: 1,
      percent: 100,
    })
    expect(pieText).toContain('&lt;img')
    expect(pieText).not.toContain('<img')

    const support = supportOption({
      ...PAYLOAD,
      support_stats: {
        names: [hostile],
        order_counts: [1],
        amounts: ['1.00'],
        refund_counts: [0],
      },
    }) as { tooltip: { formatter: (p: unknown) => string } }
    const axisText = support.tooltip.formatter([
      { axisValue: hostile, seriesName: '订单数', value: 1 },
    ])
    expect(axisText).toContain('&lt;img')
    expect(axisText).not.toContain('<img')
  })

  it('ECharts 加载失败 ⇒ `layer.msg`、卡仍上屏、图不画（I7）', async () => {
    vi.mocked(loadECharts).mockRejectedValueOnce(new Error('图表库加载失败，请刷新页面重试'))
    await mount()
    expect(LAYUI.msg).toHaveBeenCalledWith('图表库加载失败，请刷新页面重试')
    expect(ECHARTS.setOptions).toEqual([])
    expect(cardTexts(root)[0]).toBe('100.00')
    expect(LAYUI.layerClose).toHaveBeenCalledTimes(1)
  })

  it('卸载时 dispose 每个图表实例（I7）', async () => {
    const unmount = await page.mount(root)
    await flush()
    expect(ECHARTS.instances.length).toBe(4)
    unmount()
    for (const instance of ECHARTS.instances) {
      expect(instance.dispose).toHaveBeenCalledTimes(1)
    }
  })

  // ── 失败形态 ────────────────────────────────────────────────

  it('取数失败 ⇒ `layer.msg` 说人话、加载态仍然关掉、卡不清空（不把失败伪装成 0）', async () => {
    await mount()
    mockedFetchReport.mockRejectedValue(new HttpError(403, '没有权限执行该操作'))
    root.querySelector<HTMLButtonElement>(`#${SEARCH_BUTTON_ID}`)!.click()
    await flush()

    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
    // 失败也要关加载态（否则界面永远转圈）。
    expect(LAYUI.layerClose).toHaveBeenCalledTimes(2)
    // 卡里还是上一次的数（没被清成 0）。
    expect(cardTexts(root)[0]).toBe('100.00')
  })

  // ── 三条 island 陷阱（`AGENTS.md` §5-G）────────────────────

  it('三条 island 陷阱：先挂样式 → 再塞标记 → 最后 initIslandElements；类名与选择器分开', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `按 ${PANEL_SELECTOR} 匹配不到页面容器（class 里多半混进了点）`).not.toBeNull()
    expect(panel!.classList.contains(PANEL_CLASS)).toBe(true)
    expect(panel!.className).not.toContain('.')

    // ② 前置分号那批语句（laydate / form.render / initFormSelects）都真的执行到了。
    expect(LAYUI.laydate.render).toHaveBeenCalledTimes(2)
    expect(INIT_FORM_SELECTS).toHaveBeenCalledWith(`select[xm-select="${KEFU_SELECT_ID}"]`)

    // 只读：标记里没有写入口。
    const html = buildHtml()
    for (const word of ['导出', '删除', '新增', '保存']) {
      expect(html, `标记里出现了不该有的「${word}」入口`).not.toContain(word)
    }
  })
})

/** 标记里两个 radio 的 `value` 序列（用 DOM 读，不用页面闭包里的常量）。 */
function STAT_TYPE_OPTIONS_VALUES(): string[] {
  const html = buildHtml()
  return Array.from(html.matchAll(/name="statType" value="([a-z]+)"/g)).map((m) => m[1]!)
}
