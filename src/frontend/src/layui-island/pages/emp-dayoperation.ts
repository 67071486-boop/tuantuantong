/**
 * 【经营报表】页（island key = `emp-dayoperation`，路由 `/agents/day-report`）。
 *
 * ## 这一页是自有实现，但**功能照抄对标**（`/Emp/dayoperation`）
 *
 * 2026-10-17 之前本页**没有页面**：`config/replicaRoutes.ts` 里有它的登记、`router/index.ts` 的
 * `REPLICA_SKIP` 把它排除、菜单里也没有入口。业务方 2026-10-17 原话（spec
 * `docs/specs/2026-10-17-support-stats-and-business-report-design.md` §0）：
 * 「鸡蛋铺对标有一个页面 经营报表 我们直接把这个也做进来 经营报表直接照抄功能就行。」
 *
 * ⇒ 页面结构、6 个数字卡的文案 / 提示 / 渐变配色、4 张图的标题 / 副标题 / 轴名 / 系列类型
 * 都照对标离线产物（`.tools/recon/emp-pages/html/dayoperation.html` 与
 * `.tools/recon/emp-pages/js/dayoperation.js`，只读、不进 git）；**数据换成我们自己的**
 * `GET /api/business/business-report`（spec §6.2）。
 *
 * ## 布局（四块）
 *
 * 1. **筛选栏**：选择游戏（`fetchGames()`，第一项「全部游戏」）/ 统计时间（两个日期框，
 *    默认最近 30 天）/ 接单客服（formSelects 多选，选项来自接口的 `kefu_options`）/
 *    两个 radio（按接单时间 / 按交单时间，`lay-filter="statType"`）/「搜索」按钮；
 * 2. **营业利润统计卡**：2 个 radio + 6 个数字卡（标题 / tooltip / 渐变照对标）；
 * 3. **两张饼图**：订单主要来源分布图 / 利润来源分布图（各 400px 高）；
 * 4. **客服接单统计**（柱 + 柱 + 线）与 **订单数量、总价趋势图**（12 个自然月，4 条系列）。
 *
 * ## ECharts 从哪来
 *
 * `@/layui-island/echarts` 的 `loadECharts()`：动态插 `vendor/echarts.min.js`
 * （vendored 静态文件，**不加 npm 依赖**，spec §0 第 2 条 / §9 第 6 条）。
 * 加载失败 ⇒ `layer.msg` 一句中文、**不白屏**（数字卡与筛选栏仍然可用）。
 *
 * ## 与对标**有意**的三处不同（如实登记，⛔ 别当成漏了）
 *
 * 1. **数字卡不做自增动画**：对标 `number_plus()` 用 `setInterval` 从 0 数到目标值。
 *    我们的金额是接口给的**两位小数字符串**（spec §2：`Decimal` 序列化成字符串，避免 JS
 *    浮点误差），逐帧自增必然要过 `Number`，那正是要避开的东西；而且动画会引入定时器
 *    （`AGENTS.md` §5-G 陷阱 ④ 要求每个用例收口，代价与收益不成比例）⇒ 直接显示字符串。
 * 2. **图表的 `max` 不写死**：对标从服务端多取 `sdata.maxnumber` / `maxprice` / `maxrnumber`
 *    当轴上限，我们的接口**不提供**这三个值（spec §6.2 的契约里没有），所以让 ECharts
 *    自适应。⛔ 别自己按数据算一个 max 填进去——那会和对标不一致、又没有判据。
 * 3. **趋势图「总数量」轴的标签单位是 `{value}`（不是对标那个 `{value} 元`）**：对标把
 *    **订单数量**轴的标签写成「元」是它自己的笔误（一条数量轴标金额单位），照抄会让
 *    用户读错数。轴**名**（`总数量`）与系列类型仍照对标。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G）
 *
 * ① 类名与选择器分成两个常量（`PANEL_CLASS` / `PANEL_SELECTOR`）；
 * ② 以 `(` 开头的语句一律前置分号（`;(form.render as …)(…)`）；
 * ③ 顺序：`await bootstrapIsland(...)` → `root.innerHTML = …` → `initIslandElements(layui)`；
 *    `laydate.render` / `form.render` / `initFormSelects` 都必须在塞标记**之后**。
 *
 * ⚠️ `echarts.init` 还要求容器**已经进 DOM 且可见**（不然宽高是 0、图画成一条线）⇒ 图表
 * 渲染只能发生在 `root.innerHTML` 之后——本页的顺序天然满足，⛔ 别把 `renderCharts` 提前。
 */
import {
  DEFAULT_STAT_TYPE,
  fetchBusinessReport,
  type BusinessReportCardKey,
  type BusinessReportCards,
  type BusinessReportKefuOption,
  type BusinessReportPayload,
  type BusinessReportQuery,
  type BusinessReportSourceRow,
  type BusinessReportStatType,
} from '@/api/businessReport'
import { fetchGames } from '@/api/config'
import { formatDateOnly } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import { loadECharts, type EChartsInstance, type EChartsNamespace } from '@/layui-island/echarts'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { getFormSelects, initFormSelects, type FormSelectsInstance } from '@/layui/vendor'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'emp-dayoperation'
/** 页面标题：对标 `<title>` 是**空串**（`replicaRoutes.ts` 的 note）⇒ 取对标菜单名「经营报表」。 */
export const TITLE = '经营报表'

/** 页面容器 **class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'dayReportPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 控件 `id`（**照抄对标**：`sDate` / `eDate` / `gameName` / `inputOrderKefu`）。 */
export const GAME_SELECT_ID = 'gameName'
export const DATE_FROM_INPUT_ID = 'sDate'
export const DATE_TO_INPUT_ID = 'eDate'
export const KEFU_SELECT_ID = 'inputOrderKefu'
/** 「搜索」按钮的 `id`。 */
export const SEARCH_BUTTON_ID = 'dayReportSearch'
/** 两个 radio 的 `lay-filter`（`form.on('radio(statType)')` 用它认领事件）。 */
export const STAT_TYPE_RADIO_FILTER = 'statType'

/** 4 张图的容器 `id`（**照抄对标**，含它自己的拼写：`soureOpertion` / `financialWageRep`）。 */
export const CHART_ID_SOURCE = 'soureOpertion'
export const CHART_ID_PROFIT = 'financialWageRep'
export const CHART_ID_SUPPORT = 'KeFuOperation'
export const CHART_ID_TREND = 'totalPriceTrend'

/** 4 张图容器的高度（spec §5「容器高 400px」）。 */
export const CHART_HEIGHT = '400px'

/** 默认时间窗的**天数**（spec §5：统计时间默认最近 30 天，含今天）。 */
export const DEFAULT_RANGE_DAYS = 30

/** 6 个数字卡的 tooltip 文案（spec §5：前 5 个同一句，第 6 个单独一句）。 */
export const CARD_TIP_PROFIT = '计算方式：搜索条件范围内的总接单价-总发单价绩效-总记录退款金额'
export const CARD_TIP_REFUND = '只统计搜索条件范围内的记录的退款金额'

/** 一个数字卡的静态规格（标题 / tooltip / 渐变色 / 阴影色，逐条取自对标 `dayoperation.html`）。 */
export type BusinessReportCardSpec = {
  key: BusinessReportCardKey
  title: string
  tip: string
  /** `-webkit-linear-gradient(top,#起,#止)` 的两个色标。 */
  gradient: [string, string]
  /** 对标那颗 `box-shadow: 0 3px 14px 2px <shadow>` 的颜色。 */
  shadow: string
}

/**
 * 6 个数字卡（顺序 = 对标页面上的显示顺序：`num5` → `num1` → `num2` → `num3` → `num4` → `num6`）。
 *
 * ⚠️ 色值逐条来自 `.tools/recon/emp-pages/html/dayoperation.html`（只读证据件），
 * 业务方要求「颜色/渐变样式照那 6 个卡」⇒ ⛔ 别用 layui 主题色替换。
 */
export const CARD_SPECS: BusinessReportCardSpec[] = [
  {
    key: 'search_profit',
    title: '搜索结果利润(不含已退单状态)',
    tip: CARD_TIP_PROFIT,
    gradient: ['#F86738', '#f34912'],
    shadow: '#ffe4db',
  },
  {
    key: 'last_month_profit',
    title: '近一月利润(不含已退单状态)',
    tip: CARD_TIP_PROFIT,
    gradient: ['#23d4d4', '#16d1b6'],
    shadow: '#d1f6f1',
  },
  {
    key: 'prev_month_profit',
    title: '上月利润(不含已退单状态)',
    tip: CARD_TIP_PROFIT,
    gradient: ['#73D08D', '#5FB878'],
    shadow: '#cff8db',
  },
  {
    key: 'three_month_profit',
    title: '近三月利润(不含已退单状态)',
    tip: CARD_TIP_PROFIT,
    gradient: ['#30A1F5', '#1E9FFF'],
    shadow: '#cee8fc',
  },
  {
    key: 'year_profit',
    title: '近一年利润(不含已退单状态)',
    tip: CARD_TIP_PROFIT,
    gradient: ['#FFC93B', '#FFB800'],
    shadow: '#ffd4e0',
  },
  {
    key: 'search_refund',
    title: '搜索结果退款(不含已退单状态)',
    tip: CARD_TIP_REFUND,
    gradient: ['#CB5DEF', '#9825D1'],
    shadow: '#f1d0fd',
  },
]

/** 两个统计口径的 radio（值 / 文案照 spec §5；默认「按接单时间统计」）。 */
export const STAT_TYPE_OPTIONS: Array<{ value: BusinessReportStatType; title: string }> = [
  { value: 'claim', title: '按接单时间统计' },
  { value: 'submit', title: '按交单时间统计' },
]

/** 数字卡的 DOM `id`（前缀 + 接口里的 key；`__tests__` 用它逐卡核对）。 */
export function cardId(key: BusinessReportCardKey): string {
  return `dayReportCard_${key}`
}

/** 「全部游戏」那一项的 `value`（空串 = 不发 `game` 参数，spec §6.2）。 */
export const ALL_GAMES_VALUE = ''

/** 图表配色（照对标 `dayoperation.js` 的 `colors`）。 */
export const CHART_COLORS = ['#5793f3', '#d14a61', '#675bba'] as const

/**
 * 默认时间范围：**今天与往前 29 个自然日**（共 30 个自然日，含今天），形状 `yyyy-MM-dd`。
 *
 * ⚠️ 一律用 `new Date(y, m, d)` 的**本地**构造器做日期加减（`toISOString()` 是 UTC，
 * 东八区下会把日期推前一天）。口径与「客服接发单统计」页（`pages/emp-salesmanstatistics.ts`
 * 的 `defaultDateRange`）一致：两边都是 spec 点名的「最近 30 天」。
 */
export function defaultReportDateRange(now: Date = new Date()): {
  dateFrom: string
  dateTo: string
} {
  const from = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() - (DEFAULT_RANGE_DAYS - 1),
  )
  return { dateFrom: formatDateOnly(from), dateTo: formatDateOnly(now) }
}

/** 把接口的两位小数字符串折成 ECharts 能画的数字（空 / 畸形一律 0，不抛）。 */
function toNumber(value: string | number | undefined): number {
  const parsed = typeof value === 'number' ? value : Number.parseFloat(String(value ?? ''))
  return Number.isFinite(parsed) ? parsed : 0
}

/**
 * 饼图数据映射（**纯函数**）：来源分布那一张表同时给了 `amount` / `profit` 两列
 * （spec §6.2 末尾：同一次 group by，避免同一份数据查两遍）⇒ 两张饼各取一列。
 *
 * ⚠️ **利润饼必须先滤掉负值**（`profitSlices`）：ECharts 3.6.2 的饼图把负数算进
 * `getSum` 的分母、却把角度夹成 0 ⇒ 正项角度和 > 2π，后面的扇区转过一圈压在前面
 * 扇区上、`{d}%` 还会变成负数（独立评审 2026-10-17 读 minified `pieLayout` 确认）。
 * 亏损来源**不画进饼**，由 `profitPieOption` 在副标题里说明。
 */
export function pieSlices(
  rows: BusinessReportSourceRow[],
  valueKey: 'amount' | 'profit',
): Array<{ name: string; value: number }> {
  return rows.map((row) => ({ name: row.name, value: toNumber(row[valueKey]) }))
}

/** 利润饼的切片：只保留 `profit > 0` 的来源（负数 / 0 不画，理由见 `pieSlices`）。 */
export function profitSlices(
  rows: BusinessReportSourceRow[],
): Array<{ name: string; value: number }> {
  return pieSlices(rows, 'profit').filter((slice) => slice.value > 0)
}

/** 是否存在「利润为负」的来源（决定副标题要不要加那句说明）。 */
export function hasLossSource(rows: BusinessReportSourceRow[]): boolean {
  return pieSlices(rows, 'profit').some((slice) => slice.value < 0)
}


/**
 * 环比展示（**纯函数**）：接口给的是**两位小数字符串**（`"12.50"` / `"0.00"`），
 * 页面上要带百分号。空串 / 畸形值按 `0.00%` 显示（不抛、也不显示 `NaN%`）。
 */
export function formatGrowth(value: string | number | undefined): string {
  return `${toNumber(value).toFixed(2)}%`
}

/**
 * 饼图的 tooltip（**必须自己转义**）：
 * ECharts 3.6.2 的 tooltip 是 HTML（`setContent` 走 innerHTML），而 `source` / 客服名
 * 是租户可写文本 ⇒ 不转义就是存储型 XSS（独立评审 2026-10-17 I2）。
 * 形如对标 `{a} <br/>{b} : {c} ({d}%)`，但 a/b 都过 `escapeHtml`。
 */
const PIE_TOOLTIP = {
  trigger: 'item',
  formatter: (params: {
    seriesName?: string
    name?: string
    value?: number
    percent?: number
  }): string =>
    `${escapeHtml(String(params.seriesName ?? ''))} <br/>` +
    `${escapeHtml(String(params.name ?? ''))} : ${params.value} (${params.percent}%)`,
}
const PIE_ITEM_STYLE = {
  emphasis: { shadowBlur: 10, shadowOffsetX: 0, shadowColor: 'rgba(0, 0, 0, 0.5)' },
}

/**
 * 轴触发类图表的 tooltip：x 轴值（客服名）是租户可写文本 ⇒ 同样必须转义
 * （理由同 `PIE_TOOLTIP`；ECharts 3.6.2 的 tooltip 是 innerHTML）。
 */
function axisTooltipFormatter(params: unknown): string {
  const list = (Array.isArray(params) ? params : [params]) as Array<Record<string, unknown>>
  const head = escapeHtml(String(list[0]?.axisValue ?? ''))
  const lines = list.map(
    (item) => `${escapeHtml(String(item.seriesName ?? ''))}: ${item.value}`,
  )
  return [head, ...lines].join('<br/>')
}

/** 图 1「订单主要来源分布图」：按 `source` 汇总**接单价**（spec §5 第 1 张）。 */
export function sourcePieOption(payload: BusinessReportPayload): Record<string, unknown> {
  return {
    title: {
      text: '订单来源分布图',
      subtext: `订单主要各个来源分析，已排除已退单的部分，[总计：${payload.source_total.amount}元]`,
      x: 'center',
    },
    tooltip: PIE_TOOLTIP,
    legend: {
      orient: 'vertical',
      left: 'left',
      data: payload.source_distribution.map((row) => row.name),
    },
    series: [
      {
        name: '订单来源',
        type: 'pie',
        radius: '55%',
        center: ['50%', '60%'],
        data: pieSlices(payload.source_distribution, 'amount'),
        itemStyle: PIE_ITEM_STYLE,
      },
    ],
  }
}

/** 图 2「利润来源分布图」：按 `source` 汇总**利润**（spec §5 第 2 张）。 */
export function profitPieOption(payload: BusinessReportPayload): Record<string, unknown> {
  const lossNote = hasLossSource(payload.source_distribution)
    ? '；亏损来源（利润为负）不计入本图'
    : ''
  return {
    title: {
      text: '利润来源图',
      subtext: '已排除已退单的订单，其它所有状态均包含在内' + lossNote,
      x: 'center',
    },
    tooltip: PIE_TOOLTIP,
    legend: {
      orient: 'vertical',
      left: 'left',
      data: payload.source_distribution.map((row) => row.name),
    },
    series: [
      {
        name: '订单来源',
        type: 'pie',
        radius: '55%',
        center: ['50%', '60%'],
        data: profitSlices(payload.source_distribution),
        itemStyle: PIE_ITEM_STYLE,
      },
    ],
  }
}

/** 图 3「客服接单统计」：柱（订单数）+ 柱（总金额）+ 线（退单量），x 轴 = 操作人员（spec §5）。 */
export function supportOption(payload: BusinessReportPayload): Record<string, unknown> {
  const stats = payload.support_stats
  return {
    color: [...CHART_COLORS],
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: axisTooltipFormatter,
    },
    grid: { right: '20%' },
    title: {
      text: '客服接单统计',
      subtext:
        '统计筛选条件范围内有记录的客服接单数量、总金额、退单量，其中订单金额和数据已排除已退单的，' +
        `[总计：${payload.source_total.amount}元]`,
      x: 'center',
    },
    toolbox: {
      feature: {
        dataView: { show: false, readOnly: false },
        restore: { show: true },
        saveAsImage: { show: true },
      },
    },
    xAxis: [
      {
        type: 'category',
        axisTick: { alignWithLabel: true },
        data: [...stats.names],
      },
    ],
    yAxis: [
      {
        type: 'value',
        name: '单数',
        min: 0,
        position: 'right',
        axisLine: { lineStyle: { color: CHART_COLORS[0] } },
        axisLabel: { formatter: '{value}' },
      },
      {
        type: 'value',
        name: '总金额',
        min: 0,
        position: 'right',
        offset: 80,
        axisLine: { lineStyle: { color: CHART_COLORS[1] } },
        axisLabel: { formatter: '{value}元' },
      },
      {
        type: 'value',
        name: '退单量',
        min: 0,
        position: 'left',
        axisLine: { lineStyle: { color: CHART_COLORS[2] } },
        axisLabel: { formatter: '{value}' },
      },
    ],
    series: [
      { name: '订单数', type: 'bar', data: [...stats.order_counts] },
      { name: '总金额', type: 'bar', yAxisIndex: 1, data: stats.amounts.map(toNumber) },
      { name: '退单量', type: 'line', yAxisIndex: 2, data: [...stats.refund_counts] },
    ],
  }
}

/** 趋势图 tooltip 的入参形状（ECharts 交回来的那一份里我们用到的最小集）。 */
type TrendTooltipParam = { axisValue?: string; seriesName?: string; value?: number }

/**
 * 趋势图 tooltip（**纯函数**，给 `formatGrowth` 一个真实出口）。
 *
 * 对标是一句字符串模板 `'{b}<br>{a0}:{c0},环比:{c2}%<br>{a1}:{c1},环比：{c3}%'`；
 * 我们改成函数只是为了用同一套 `formatGrowth` 把环比格式化成 `12.50%`
 * （接口给的就是两位小数，`{c2}%` 也能显示，但两处格式化会漂移）。
 */
export function trendTooltip(params: TrendTooltipParam[]): string {
  const pick = (name: string): TrendTooltipParam | undefined =>
    params.find((item) => item.seriesName === name)
  const line = (label: string, series: string, growth: string): string =>
    `${label}:${pick(series)?.value ?? 0}，环比:${formatGrowth(pick(growth)?.value)}`
  return [
    params[0]?.axisValue ?? '',
    line('订单总数量', '订单总数量', '订单总数量环比增长率'),
    line('订单总金额', '订单总金额', '订单总金额环比增长率'),
  ].join('<br>')
}

/** 图 4「订单数量、总价趋势图（已排除已退单部分）」：最近 12 个自然月、4 条系列（spec §5）。 */
export function trendOption(payload: BusinessReportPayload): Record<string, unknown> {
  const trend = payload.trend
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: trendTooltip,
    },
    grid: { right: '20%' },
    toolbox: {
      feature: {
        dataView: { show: true, readOnly: false },
        restore: { show: true },
        saveAsImage: { show: true },
      },
    },
    legend: {
      data: ['订单总数量', '订单总数量环比增长率', '订单总金额', '订单总金额环比增长率'],
    },
    xAxis: [
      {
        type: 'category',
        axisTick: { alignWithLabel: true },
        // ⚠️ 标签直接显示接口给的 `yyyy-MM`：对标是 `name + '月'`，而我们的 name 本身就是
        //    `2025-11` 这种形状 ⇒ 照抄会读成 `2025-11月`。
        data: [...trend.months],
      },
    ],
    yAxis: [
      {
        type: 'value',
        name: '总数量',
        position: 'right',
        axisLine: { show: true, lineStyle: { color: CHART_COLORS[0] } },
        // 对标这里写的是 `'{value} 元'`——一条**数量**轴标金额单位是它自己的笔误，不照抄
        // （见文件头「与对标有意的三处不同」第 3 条）。
        axisLabel: { formatter: '{value}' },
      },
      {
        type: 'value',
        name: '总金额',
        position: 'right',
        offset: 80,
        axisLine: { show: true, lineStyle: { color: CHART_COLORS[1] } },
        axisLabel: { formatter: '{value} 元' },
      },
      {
        type: 'value',
        name: '环比增长',
        position: 'left',
        axisLine: { show: true, lineStyle: { color: CHART_COLORS[2] } },
        axisLabel: { formatter: '{value} %' },
      },
    ],
    series: [
      { name: '订单总数量', type: 'bar', data: [...trend.order_counts] },
      { name: '订单总金额', type: 'bar', yAxisIndex: 1, data: trend.amounts.map(toNumber) },
      {
        name: '订单总数量环比增长率',
        type: 'line',
        yAxisIndex: 2,
        data: trend.count_growth.map(toNumber),
      },
      {
        name: '订单总金额环比增长率',
        type: 'line',
        yAxisIndex: 2,
        data: trend.amount_growth.map(toNumber),
      },
    ],
  }
}

/** 页内 `<style>`：照抄对标 `dayoperation.html` 里那 4 条 `.tongji` 规则（只影响本页）。 */
const PAGE_STYLE = `
.tongji .layadmin-shortcut li .layui-icon {height:auto;padding-bottom:15px;color:#FFBB00;font-size:45px;}
.tongji .layui-input-block {margin-left:0;}
.tongji .layadmin-shortcut li .layui-icon .tit {font-size:16px;color:#333;}
.tongji .layui-col-lg3.width5 {width:16.5% !important;}
`

/** 6 个数字卡的标记（顺序 = `CARD_SPECS`）。 */
function cardsHtml(): string {
  return CARD_SPECS.map(
    (spec) => `
            <li class="layui-col-xs6 layui-col-sm6 layui-col-md3 layui-col-lg3 width5">
              <div class="layui-icon" style="background: -webkit-linear-gradient(top,${spec.gradient[0]},${spec.gradient[1]}); box-shadow: 0 3px 14px 2px ${spec.shadow}; border-radius:10px">
                <div class="tit" style="color:#ffffff">${escapeHtml(spec.title)}</div>
                <div id="${cardId(spec.key)}" style="color:#ffffff" title="${escapeHtml(spec.tip)}">0</div>
              </div>
            </li>`,
  ).join('')
}

/** 一张 400px 高的图表卡片（标题照对标 `layui-card-header`）。 */
function chartCard(title: string, id: string, widthClass: string): string {
  return `
        <div class="${widthClass}">
          <section class="layui-card">
            <div class="layui-card-header">${escapeHtml(title)}</div>
            <div class="layui-card-body">
              <div id="${id}" style="width: 100%;height:${CHART_HEIGHT};"></div>
            </div>
          </section>
        </div>`
}

/** 页面标记（**没有**任何写入口：这一页是只读看板）。 */
export function buildHtml(): string {
  const statRadios = STAT_TYPE_OPTIONS.map(
    (option) => `
                <input type="radio" name="${STAT_TYPE_RADIO_FILTER}" value="${option.value}"
                       lay-filter="${STAT_TYPE_RADIO_FILTER}" title="${escapeHtml(option.title)}"${
                         option.value === DEFAULT_STAT_TYPE ? ' checked' : ''
                       }>`,
  ).join('')
  return `
<style>${PAGE_STYLE}</style>
<div class="${PANEL_CLASS}" style="padding-left:5px;padding-top:10px">
  <form class="layui-form" lay-filter="dayReportForm" action="" style="background:#f2f2f2">
    <div style="margin-bottom:10px">
      <div class="layui-form-pane">
        <div class="layui-inline">
          <label class="layui-form-label">选择游戏</label>
          <div class="layui-input-inline" style="width:140px">
            <select id="${GAME_SELECT_ID}" name="${GAME_SELECT_ID}"></select>
          </div>
        </div>
        <div class="layui-inline">
          <label class="layui-form-label">统计时间</label>
          <div class="layui-input-inline">
            <input type="text" id="${DATE_FROM_INPUT_ID}" class="layui-input"
                   placeholder="开始日期" autocomplete="off">
          </div>
          <div class="layui-input-inline" style="width:auto">-</div>
          <div class="layui-input-inline">
            <input type="text" id="${DATE_TO_INPUT_ID}" class="layui-input"
                   placeholder="结束日期" autocomplete="off">
          </div>
        </div>
        <div class="layui-inline">
          <label class="layui-form-label">接单客服</label>
          <div class="layui-input-inline">
            <select id="${KEFU_SELECT_ID}" name="${KEFU_SELECT_ID}" xm-select="${KEFU_SELECT_ID}"
                    xm-select-skin="default" xm-select-height="36px" class="" title=""></select>
          </div>
        </div>
        <div class="layui-inline">
          <div class="layui-input-inline">
            <button type="button" class="layui-btn layui-btn-normal" id="${SEARCH_BUTTON_ID}"
                    style="width: 80px">搜索</button>
          </div>
        </div>
      </div>
    </div>

    <div class="layui-row tongji" style="margin-bottom: 10px;">
      <div class="layui-col-lg12 layui-col-md12 layui-col-sm12 layui-col-xs12">
        <section class="layui-card">
          <div class="layui-card-header">营业利润统计</div>
          <div class="layui-card-body">
            <div class="layadmin-shortcut">
              <div class="layui-form-item">
                <div class="layui-input-block">${statRadios}
                </div>
              </div>
              <ul class="layui-row layui-col-space10">${cardsHtml()}
              </ul>
            </div>
          </div>
        </section>
      </div>
    </div>

    <div class="layui-row layui-col-space10 layui-echarts">
${chartCard('订单主要来源分布图', CHART_ID_SOURCE, 'layui-col-lg6 layui-col-md6 layui-col-sm12 layui-col-xs12')}
${chartCard('利润来源分布图', CHART_ID_PROFIT, 'layui-col-lg6 layui-col-md6 layui-col-sm12 layui-col-xs12')}
    </div>
    <div class="layui-row layui-echarts">
${chartCard('客服接单统计', CHART_ID_SUPPORT, 'layui-col-lg12 layui-col-md12 layui-col-sm12 layui-col-xs12')}
    </div>
    <div class="layui-row layui-echarts">
${chartCard('订单数量、总价趋势图（已排除已退单部分）', CHART_ID_TREND, 'layui-col-lg12 layui-col-md12 layui-col-sm12 layui-col-xs12')}
    </div>
  </form>
</div>`
}

/** 纯函数出口（测试直接用；不依赖 layui / ECharts）。 */
export const __testables = {
  buildHtml,
  cardId,
  defaultReportDateRange,
  formatGrowth,
  hasLossSource,
  pieSlices,
  profitPieOption,
  profitSlices,
  sourcePieOption,
  supportOption,
  trendOption,
  trendTooltip,
}

/** `formSelects` 里我们用到的方法（`layui/vendor` 的公开类型只声明了 `render` / `value`）。 */
type FormSelectsLike = FormSelectsInstance & {
  data?: (id: string, type: string, opts: unknown) => void
}

export default {
  mount: async (root) => {
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer', 'laydate'])
    const form = layui.form as LayuiModule
    const layer = layui.layer as LayuiModule
    const laydate = layui.laydate as LayuiModule
    const msg = (text: string) => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）。
    document.title = TITLE
    root.innerHTML = buildHtml()
    initIslandElements(layui)
    // ⚠️ **必须不带类型调一次 `form.render()`**（独立评审 2026-10-17 B1）：
    // layui 的 `form.render(type)` 按 `{input,select,checkbox,radio}[type]` 分派，
    // 只调 `form.render('select')` 时两个 radio 仍是 `.layui-form input[type=radio]{display:none}`
    // ⇒ 页面上根本看不到「按接单时间 / 按交单时间」，click 也不会被 radio renderer 绑上、
    // `form.on('radio(statType)')` 永不触发。不带类型 = 全部渲染一遍（同仓先例
    // `emp-settlementemp.ts` 的同一句）。
    ;(form.render as () => void)()

    const fromInput = root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)
    const toInput = root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)
    const gameSelect = root.querySelector<HTMLSelectElement>(`#${GAME_SELECT_ID}`)

    // 日期默认值必须在 `laydate.render` **之前**填（插件初始化时读的是当时的值）。
    const { dateFrom, dateTo } = defaultReportDateRange()
    if (fromInput) fromInput.value = dateFrom
    if (toInput) toInput.value = dateTo
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_FROM_INPUT_ID}`, type: 'date' })
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_TO_INPUT_ID}`, type: 'date' })

    // 接单客服：先渲染**空壳**，候选等第一次响应回来再用 `formSelects.data(...)` 灌
    // （对标也是这个顺序；插件 `render()` 那一刻读 `<option>`，空壳正好）。
    initFormSelects(`select[xm-select="${KEFU_SELECT_ID}"]`)
    const formSelects = getFormSelects() as FormSelectsLike | undefined

    /** 当前统计口径（radio 的 DOM 是真相；这里缓存只是为了让 `readQuery` 不依赖 layui 的渲染结果）。 */
    let statType: BusinessReportStatType = DEFAULT_STAT_TYPE

    /** 读游戏下拉：空串 = 全部游戏（不发 `game`）。 */
    function selectedGameId(): number | null {
      const raw = (gameSelect?.value ?? '').trim()
      if (raw === ALL_GAMES_VALUE) return null
      const id = Number.parseInt(raw, 10)
      return Number.isFinite(id) && id > 0 ? id : null
    }

    /**
     * 读接单客服多选：`formSelects` 把选中项给成**逗号串**（`valStr`）⇒ 拆成主键数组。
     *
     * ⚠️ 值只能从插件读：它渲染时把 `<select>` 的 `name` 摘掉了（`layui/vendor.ts` 的注释）。
     * ⚠️ 非法段一律丢掉（`Number.isFinite` + `> 0`），⛔ 不把 `NaN` 发出去。
     */
    function selectedKefuIds(): number[] {
      const raw = formSelects?.value(KEFU_SELECT_ID, 'valStr')
      if (typeof raw !== 'string' || raw.trim() === '') return []
      return raw
        .split(',')
        .map((part) => Number.parseInt(part.trim(), 10))
        .filter((id) => Number.isFinite(id) && id > 0)
    }

    /** 当前筛选（**唯一**的参数形状出口；`stat_type` 永远显式发）。 */
    function readQuery(): BusinessReportQuery {
      return {
        dateFrom: fromInput?.value ?? '',
        dateTo: toInput?.value ?? '',
        statType,
        game: selectedGameId(),
        kefu: selectedKefuIds(),
      }
    }

    // —— 游戏下拉：`fetchGames()`（已按查看者游戏权限收窄），第一项「全部游戏」。
    //    ⚠️ 失败**不挡整页**：msg 说清楚，报表仍然用「全部游戏」出数。
    async function fillGames(): Promise<void> {
      if (!gameSelect) return
      try {
        const games = await fetchGames()
        gameSelect.innerHTML = [
          `<option value="${ALL_GAMES_VALUE}">全部游戏</option>`,
          ...games.map((game) => `<option value="${game.id}">${escapeHtml(game.name)}</option>`),
        ].join('')
        gameSelect.selectedIndex = 0
        ;(form.render as (type?: string) => void)('select')
      } catch (error) {
        msg((error as Error).message)
      }
    }

    // —— 6 个数字卡：接口给的就是两位小数字符串 ⇒ 原样上屏（⛔ 不做自增动画，见文件头）。
    function applyCards(cards: BusinessReportCards): void {
      for (const spec of CARD_SPECS) {
        const el = root.querySelector<HTMLElement>(`#${cardId(spec.key)}`)
        if (el) el.textContent = cards[spec.key]
      }
    }

    /**
     * 接单客服候选：**只灌一次**（对标也是页面加载时取一次）。
     *
     * ⚠️ 每次响应都重灌会**重置用户当前的选择**（选完点搜索 ⇒ 选择被清掉、筛选静默失效），
     * 代价与 `pages/order-tradingorderactionloglist.ts` 的 `fillActorsOnce` 同一条：
     * 候选是首次那个日期窗的快照，换窗口后新出现的客服要刷新整页才看得到。
     */
    let kefuFilled = false
    function fillKefuOnce(options: BusinessReportKefuOption[]): void {
      if (kefuFilled || options.length === 0) return
      formSelects?.data?.(KEFU_SELECT_ID, 'local', {
        arr: options.map((option) => ({ name: option.name, value: String(option.id) })),
      })
      kefuFilled = true
    }

    /** 每张图只 `init` 一次（重复 init 同一个容器会叠加实例，ECharts 会告警且内存泄漏）。 */
    const chartInstances = new Map<string, EChartsInstance>()

    function chartOf(echarts: EChartsNamespace, id: string): EChartsInstance | null {
      const cached = chartInstances.get(id)
      if (cached) return cached
      const el = root.querySelector<HTMLElement>(`#${id}`)
      if (!el) return null
      const instance = echarts.init(el)
      chartInstances.set(id, instance)
      return instance
    }

    /** 4 张图（`setOption(option, true)`：换数据时**不合并**旧配置）。 */
    function renderCharts(echarts: EChartsNamespace, payload: BusinessReportPayload): void {
      const plan: Array<[string, Record<string, unknown>]> = [
        [CHART_ID_SOURCE, sourcePieOption(payload)],
        [CHART_ID_PROFIT, profitPieOption(payload)],
        [CHART_ID_SUPPORT, supportOption(payload)],
        [CHART_ID_TREND, trendOption(payload)],
      ]
      for (const [id, option] of plan) {
        chartOf(echarts, id)?.setOption(option, true)
      }
    }

    /**
     * 取数 → 上屏。
     *
     * ⚠️ 加载态是 `layer.load(1,{shade:false})`（对标同款）、`finally` 里关掉——**失败也必须关**，
     * 否则界面永远转圈（那比报错更难查）。
     * ⚠️ ECharts **在数据之后**加载：数据都用不上时不必白下载 629 KB 的脚本。
     * 脚本失败会走 `catch` 提示一句中文，数字卡与筛选栏照常可用（不白屏）。
     */
    let loadSeq = 0
    async function load(): Promise<void> {
      const seq = ++loadSeq
      const loading = (layer.load as (type?: number, opts?: unknown) => number)(1, {
        shade: false,
      })
      try {
        const payload = await fetchBusinessReport(readQuery())
        // ⚠️ 连点 / 点完搜索立刻切口径时，后返回的旧条件响应**不许覆盖**新结果
        //（独立评审 2026-10-17 I4）。ECharts 是第二次 await，回来时也要再判一次。
        if (seq !== loadSeq) return
        applyCards(payload.cards)
        fillKefuOnce(payload.kefu_options)
        const echarts = await loadECharts()
        if (seq !== loadSeq) return
        renderCharts(echarts, payload)
      } catch (error) {
        if (seq === loadSeq) msg((error as Error).message)
      } finally {
        ;(layer.close as (index: number) => void)(loading)
      }
    }

    root.querySelector<HTMLButtonElement>(`#${SEARCH_BUTTON_ID}`)!.addEventListener('click', () => {
      void load()
    })

    // —— 统计口径切换（对标 `form.on('radio(statictype)')`）：换口径 ⇒ 整页刷新（不复用旧数据）。
    ;(
      form.on as (event: string, cb: (obj: { value?: string }) => void) => void
    )(`radio(${STAT_TYPE_RADIO_FILTER})`, (obj) => {
      const next = obj?.value
      statType = next === 'submit' ? 'submit' : 'claim'
      void load()
    })

    await fillGames()
    await load()

    return () => {
      for (const instance of chartInstances.values()) instance.dispose?.()
      chartInstances.clear()
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
