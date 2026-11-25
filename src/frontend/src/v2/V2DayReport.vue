<script setup lang="ts">
/**
 * 「经营报表」v2（`/v2/day-report`；财务族第二批 2026-11-06，计划
 * `docs/plans/2026-11-06-finance-batch2.md` §4）。旧页 = island 自建页
 * `layui-island/pages/emp-dayoperation.ts`（功能照抄对标 /Emp/dayoperation）。本页按
 * `docs/FRONTEND-V2-FINANCE-STANDARD.md` §7 的炭火拍板重建：6 个利润/退款数字卡改炭火
 * KPI 卡（白面板 + 3px 语义顶条 + mono 数字，负值红；⛔ 不抄旧页彩虹渐变）；4 张图：
 * 两张饼走 `v2/charts.ts` 整环扇形基座（中心总计 = 后端 `source_total.amount/profit`，
 * ⛔ 前端求和；利润饼负值不进扇形——`buildDonutData` 内建滤 ≤0）；「客服接单统计」
 * bar+bar+line 三系列三轴、「订单数量、总价趋势图（已排除已退单部分）」bar+bar+line+line
 * 四系列三轴，多色板用 `DONUT_PALETTE`（2026-11-06 业务方拍板），`setOption(option, true)`
 * 不合并（3.6.2 语法）；口径句 / 系列名 / 轴名 / tooltip 逐字照旧页（总数量轴标签不带
 * 「元」——对标把它标成金额单位是它自己的笔误，轴名照抄）。加载态 = 查询钮禁用 + 区块
 * 降透明（⛔ 无 layer.load），失败解除并弹后端 msg；数值 = 接口字符串原样上屏无动画；
 * 接单客服候选只灌一次（fillKefuOnce）；游戏下拉失败不挡整页。⛔ 不 import island 页模块。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { fetchGames, type ConfigRow } from '@/api/config'
import {
  fetchBusinessReport,
  DEFAULT_STAT_TYPE,
  type BusinessReportCards,
  type BusinessReportKefuOption,
  type BusinessReportPayload,
  type BusinessReportSourceRow,
  type BusinessReportStatType,
} from '@/api/businessReport'
import {
  AXIS_LABEL_COLOR,
  buildDonutData,
  createDonut,
  DONUT_PALETTE,
  loadECharts,
  type DonutController,
  type DonutData,
  type EChartsInstance,
  type EChartsNamespace,
} from '@/v2/charts'

/* ── 逐字文案（出处 = island 页同名常量，本地重写不引模块）────────────────── */

/** 5 张利润卡同一句口径（旧页 CARD_TIP_PROFIT）。 */
const CARD_TIP_PROFIT = '计算方式：搜索条件范围内的总接单价-总发单价绩效-总记录退款金额'
/** 退款卡口径（旧页 CARD_TIP_REFUND）。 */
const CARD_TIP_REFUND = '只统计搜索条件范围内的记录的退款金额'

/** 6 张数字卡（顺序 = 旧页 CARD_SPECS = 页面显示顺序；key = 接口 cards 的键）。 */
const CARD_SPECS: ReadonlyArray<{
  key: keyof BusinessReportCards
  title: string
  tip: string
  refund: boolean
}> = [
  { key: 'search_profit', title: '搜索结果利润(不含已退单状态)', tip: CARD_TIP_PROFIT, refund: false },
  { key: 'last_month_profit', title: '近一月利润(不含已退单状态)', tip: CARD_TIP_PROFIT, refund: false },
  { key: 'prev_month_profit', title: '上月利润(不含已退单状态)', tip: CARD_TIP_PROFIT, refund: false },
  { key: 'three_month_profit', title: '近三月利润(不含已退单状态)', tip: CARD_TIP_PROFIT, refund: false },
  { key: 'year_profit', title: '近一年利润(不含已退单状态)', tip: CARD_TIP_PROFIT, refund: false },
  { key: 'search_refund', title: '搜索结果退款(不含已退单状态)', tip: CARD_TIP_REFUND, refund: true },
]

/** 两个统计口径（值/文案照旧页 STAT_TYPE_OPTIONS；默认按接单时间）。 */
const STAT_TYPE_OPTIONS: ReadonlyArray<{ value: BusinessReportStatType; title: string }> = [
  { value: 'claim', title: '按接单时间统计' },
  { value: 'submit', title: '按交单时间统计' },
]

/** 错误对象 → 用户可见文案（后端 msg 原文；不自造）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '加载失败'
}

/* ── toast（炭火自绘；info 1500ms / error 6000ms）─────────────────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 筛选 ──────────────────────────────────────────────────────────────── */

const DEFAULT_RANGE_DAYS = 30
const pad2 = (n: number): string => String(n).padStart(2, '0')
const ymd = (d: Date): string => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`

/** 默认窗 = 今天与往前 29 个自然日（共 30 天，含今天；旧页 defaultReportDateRange 同款）。 */
function defaultReportDateRange(now: Date = new Date()): { dateFrom: string; dateTo: string } {
  const from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (DEFAULT_RANGE_DAYS - 1))
  return { dateFrom: ymd(from), dateTo: ymd(now) }
}

const initialRange = defaultReportDateRange()
const dateFrom = ref(initialRange.dateFrom)
const dateTo = ref(initialRange.dateTo)
const game = ref('')
const kefuIds = ref<string[]>([])
const statType = ref<BusinessReportStatType>(DEFAULT_STAT_TYPE)

const games = ref<ConfigRow[]>([])

/** 游戏下拉：失败不挡整页（msg 说清，报表仍按「全部游戏」出数——旧页同口径）。 */
async function fillGames(): Promise<void> {
  try {
    games.value = await fetchGames()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/** 接单客服候选只灌一次（旧页 fillKefuOnce：重灌会重置用户当前的选择）。 */
const kefuOptions = ref<BusinessReportKefuOption[]>([])
let kefuFilled = false
function fillKefuOnce(options: BusinessReportKefuOption[]): void {
  if (kefuFilled || options.length === 0) return
  kefuOptions.value = options
  kefuFilled = true
}

/* ── 取数与渲染 ─────────────────────────────────────────────────────────── */

const payload = ref<BusinessReportPayload | null>(null)
const cards = computed<BusinessReportCards | null>(() => payload.value?.cards ?? null)
const loading = ref(false)
/** 竞态闸（旧页 loadSeq 同款）：只有最新一次取数允许上屏/报错/画图。 */
let loadSeq = 0

async function load(): Promise<void> {
  const seq = ++loadSeq
  loading.value = true
  try {
    const data = await fetchBusinessReport({
      dateFrom: dateFrom.value,
      dateTo: dateTo.value,
      statType: statType.value,
      game: game.value === '' ? null : Number(game.value),
      kefu: kefuIds.value.map((v) => Number(v)),
    })
    if (seq !== loadSeq) return
    payload.value = data
    fillKefuOnce(data.kefu_options)
    const echarts = await loadECharts()
    if (seq !== loadSeq) return
    renderCharts(echarts, data)
  } catch (error) {
    if (seq === loadSeq) showToast(errText(error), 'error')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function onQuery(): void {
  if (loading.value) return
  void load()
}

function onStatTypeChange(): void {
  // 换口径 = 整页刷新（不复用旧数据——旧页 radio 同款）。
  void load()
}

/* ── 两张饼（整环基座；中心总计 = 后端 source_total）──────────────────────── */

const sourceChartEl = ref<HTMLElement | null>(null)
const profitChartEl = ref<HTMLElement | null>(null)
let sourceDonut: DonutController | null = null
let profitDonut: DonutController | null = null

/** 来源分布副标题口径句（旧页 sourcePieOption 的 title.subtext，逐字）。 */
const sourcePieNote = computed(() => {
  const total = payload.value?.source_total.amount ?? ''
  return `订单主要各个来源分析，已排除已退单的部分，[总计：${total}元]`
})

/** 利润分布副标题口径句（旧页 profitPieOption；存在利润为负的来源时追加剧句）。 */
const profitPieNote = computed(() => {
  const rows = payload.value?.source_distribution ?? []
  const hasLoss = rows.some((row) => Number(row.profit) < 0)
  return '已排除已退单的订单，其它所有状态均包含在内' + (hasLoss ? '；亏损来源（利润为负）不计入本图' : '')
})

/** 两张饼的扇形数据（图例与几何同源；利润饼负值/0 由 parseDonutAmount 滤掉不进扇形）。 */
const sourceDonutData = computed(() => {
  const rows = payload.value?.source_distribution ?? []
  return buildDonutData(rows.map((row: BusinessReportSourceRow) => ({ name: row.name, amount: row.amount })))
})
const profitDonutData = computed(() => {
  const rows = payload.value?.source_distribution ?? []
  return buildDonutData(rows.map((row: BusinessReportSourceRow) => ({ name: row.name, amount: row.profit })))
})

function drawDonuts(echarts: EChartsNamespace, data: BusinessReportPayload): void {
  const sourceData = sourceDonutData.value
  if (sourceData !== null && sourceChartEl.value !== null) {
    if (sourceDonut === null)
      sourceDonut = createDonut(sourceChartEl.value, echarts, { hideSliceLabels: true, hideTipOnLeave: true })
    sourceDonut.update(visibleOf('source', sourceData), data.source_total.amount, '订单来源')
    sourceDonut.resize()
  }
  // 利润饼：负值/0 不进扇形；中心总计照后端 source_total.profit 原值（⛔ 前端求和）。
  const profitData = profitDonutData.value
  if (profitData !== null && profitChartEl.value !== null) {
    if (profitDonut === null)
      profitDonut = createDonut(profitChartEl.value, echarts, { hideSliceLabels: true, hideTipOnLeave: true })
    profitDonut.update(visibleOf('profit', profitData), data.source_total.profit, '订单来源')
    profitDonut.resize()
  }
}

/** 图例点击显隐（Metabase 行为）：按名字记隐藏态，重画时过滤 slices（中心总计/口径句不变）。 */
const hiddenSlices = reactive<{ source: string[]; profit: string[] }>({ source: [], profit: [] })

function visibleOf(which: 'source' | 'profit', data: DonutData): DonutData {
  const hidden = hiddenSlices[which]
  return { ...data, slices: data.slices.filter((slice) => !hidden.includes(slice.name)) }
}

function onLegendEnter(which: 'source' | 'profit', name: string): void {
  ;(which === 'source' ? sourceDonut : profitDonut)?.highlight(name)
}
function onLegendLeave(which: 'source' | 'profit'): void {
  ;(which === 'source' ? sourceDonut : profitDonut)?.downplay()
}
function onLegendToggle(which: 'source' | 'profit', name: string): void {
  const list = hiddenSlices[which]
  const at = list.indexOf(name)
  if (at >= 0) list.splice(at, 1)
  else list.push(name)
  const controller = which === 'source' ? sourceDonut : profitDonut
  const data = which === 'source' ? sourceDonutData.value : profitDonutData.value
  const totalText =
    which === 'source'
      ? (payload.value?.source_total.amount ?? '')
      : (payload.value?.source_total.profit ?? '')
  if (controller !== null && data !== null) {
    controller.update(visibleOf(which, data), totalText, '订单来源')
  }
}

/* ── 两张多轴图（bar+bar+line；option 照旧页，色换多色板）──────────────────── */

const supportChartEl = ref<HTMLElement | null>(null)
const trendChartEl = ref<HTMLElement | null>(null)
const barCharts = new Map<string, EChartsInstance>()

function chartOf(echarts: EChartsNamespace, key: 'support' | 'trend', el: HTMLElement | null): EChartsInstance | null {
  if (el === null) return null
  const cached = barCharts.get(key)
  if (cached) return cached
  const instance = echarts.init(el)
  barCharts.set(key, instance)
  return instance
}

/** HTML 转义（ECharts 3.6.2 的 tooltip 是 innerHTML；客服名是租户可写文本）。 */
function esc(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 金额字符串 → 数字（只喂几何；上屏一律原字符串）。 */
function toNumber(value: string | number | undefined): number {
  const parsed = typeof value === 'number' ? value : Number.parseFloat(String(value ?? ''))
  return Number.isFinite(parsed) ? parsed : 0
}

/** 轴触发类 tooltip（旧页 axisTooltipFormatter 同款：头行 = 客服名，行 = 系列名: 值）。 */
function axisTooltipFormatter(params: unknown): string {
  const list = (Array.isArray(params) ? params : [params]) as Array<Record<string, unknown>>
  const head = esc(String(list[0]?.axisValue ?? ''))
  const lines = list.map((item) => `${esc(String(item.seriesName ?? ''))}: ${item.value}`)
  return [head, ...lines].join('<br/>')
}

/** 趋势图环比展示（旧页 formatGrowth：接口两位小数 + %；空/畸形按 0.00%）。 */
function formatGrowth(value: string | number | undefined): string {
  return `${toNumber(value).toFixed(2)}%`
}

/** 趋势图 tooltip（旧页 trendTooltip 逐字：两行「订单总X：值，环比:增长率」）。 */
function trendTooltip(params: Array<{ axisValue?: string; seriesName?: string; value?: number }>): string {
  const pick = (name: string): { value?: number } | undefined =>
    params.find((item) => item.seriesName === name)
  const line = (label: string, series: string, growth: string): string =>
    `${label}:${pick(series)?.value ?? 0}，环比:${formatGrowth(pick(growth)?.value)}`
  return [
    params[0]?.axisValue ?? '',
    line('订单总数量', '订单总数量', '订单总数量环比增长率'),
    line('订单总金额', '订单总金额', '订单总金额环比增长率'),
  ].join('<br>')
}

/** 「客服接单统计」option（旧页 supportOption：bar 订单数 + bar 总金额 + line 退单量、三轴）。 */
function supportOption(data: BusinessReportPayload): Record<string, unknown> {
  const stats = data.support_stats
  const c0 = DONUT_PALETTE[0]!
  const c1 = DONUT_PALETTE[1]!
  const c5 = DONUT_PALETTE[5]!
  return {
    color: [c0, c1, c5],
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: axisTooltipFormatter,
    },
    grid: { right: '20%' },
    legend: { data: ['订单数', '总金额', '退单量'] },
    xAxis: [
      {
        type: 'category',
        axisTick: { alignWithLabel: true },
        axisLabel: { color: AXIS_LABEL_COLOR },
        data: [...stats.names],
      },
    ],
    yAxis: [
      {
        type: 'value',
        name: '单数',
        min: 0,
        position: 'right',
        axisLine: { lineStyle: { color: c0 } },
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value}' },
      },
      {
        type: 'value',
        name: '总金额',
        min: 0,
        position: 'right',
        offset: 80,
        axisLine: { lineStyle: { color: c1 } },
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value}元' },
      },
      {
        type: 'value',
        name: '退单量',
        min: 0,
        position: 'left',
        axisLine: { lineStyle: { color: c5 } },
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value}' },
      },
    ],
    series: [
      { name: '订单数', type: 'bar', data: [...stats.order_counts] },
      { name: '总金额', type: 'bar', yAxisIndex: 1, data: stats.amounts.map(toNumber) },
      { name: '退单量', type: 'line', yAxisIndex: 2, data: [...stats.refund_counts] },
    ],
  }
}

/** 「订单数量、总价趋势图」option（旧页 trendOption：4 系列 3 轴；月标签 = 接口 yyyy-MM 原样）。 */
function trendOption(data: BusinessReportPayload): Record<string, unknown> {
  const trend = data.trend
  const c0 = DONUT_PALETTE[0]!
  const c1 = DONUT_PALETTE[1]!
  const c2 = DONUT_PALETTE[2]!
  const c3 = DONUT_PALETTE[3]!
  return {
    color: [c0, c1, c2, c3],
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: trendTooltip,
    },
    grid: { right: '20%' },
    legend: { data: ['订单总数量', '订单总数量环比增长率', '订单总金额', '订单总金额环比增长率'] },
    xAxis: [
      {
        type: 'category',
        axisTick: { alignWithLabel: true },
        axisLabel: { color: AXIS_LABEL_COLOR },
        data: [...trend.months],
      },
    ],
    yAxis: [
      {
        type: 'value',
        name: '总数量',
        position: 'right',
        axisLine: { show: true, lineStyle: { color: c0 } },
        // 对标把这条数量轴的标签写成「{value} 元」是它自己的笔误；轴名照抄、单位不带。
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value}' },
      },
      {
        type: 'value',
        name: '总金额',
        position: 'right',
        offset: 80,
        axisLine: { show: true, lineStyle: { color: c1 } },
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value} 元' },
      },
      {
        type: 'value',
        name: '环比增长',
        position: 'left',
        axisLine: { show: true, lineStyle: { color: c2 } },
        axisLabel: { color: AXIS_LABEL_COLOR, formatter: '{value} %' },
      },
    ],
    series: [
      { name: '订单总数量', type: 'bar', data: [...trend.order_counts] },
      { name: '订单总金额', type: 'bar', yAxisIndex: 1, data: trend.amounts.map(toNumber) },
      { name: '订单总数量环比增长率', type: 'line', yAxisIndex: 2, data: trend.count_growth.map(toNumber) },
      { name: '订单总金额环比增长率', type: 'line', yAxisIndex: 2, data: trend.amount_growth.map(toNumber) },
    ],
  }
}

/** 4 张图（setOption(option, true)：换数据时不合并旧配置——3.6.2 语法）。 */
function renderCharts(echarts: EChartsNamespace, data: BusinessReportPayload): void {
  drawDonuts(echarts, data)
  const support = chartOf(echarts, 'support', supportChartEl.value)
  support?.setOption(supportOption(data), true)
  support?.resize?.()
  const trend = chartOf(echarts, 'trend', trendChartEl.value)
  trend?.setOption(trendOption(data), true)
  trend?.resize?.()
}

function onWindowResize(): void {
  sourceDonut?.resize()
  profitDonut?.resize()
  for (const instance of barCharts.values()) instance.resize?.()
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
  void fillGames().then(() => load())
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  sourceDonut?.dispose()
  profitDonut?.dispose()
  for (const instance of barCharts.values()) instance.dispose?.()
  barCharts.clear()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="day-report">
    <!-- 筛选：选择游戏 / 统计时间（默认最近 30 天）/ 接单客服多选 / 统计口径 radio / 搜索 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-row">
        <label class="cv2-field">
          <span class="cv2-field-label">选择游戏</span>
          <select v-model="game" class="cv2-input" data-v2-filter="game">
            <option value="">全部游戏</option>
            <option v-for="g in games" :key="g.id" :value="String(g.id)">{{ g.name }}</option>
          </select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">统计时间</span>
          <input v-model="dateFrom" class="cv2-input" type="date" data-v2-filter="date-from" />
        </label>
        <span class="flt-dash" aria-hidden="true">~</span>
        <label class="cv2-field">
          <input v-model="dateTo" class="cv2-input" type="date" data-v2-filter="date-to" />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">接单客服</span>
          <el-select
            v-model="kefuIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="请选择"
            data-v2-filter="kefu"
          >
            <el-option v-for="o in kefuOptions" :key="o.id" :value="String(o.id)" :label="o.name" />
          </el-select>
        </label>
        <el-radio-group v-model="statType" class="dr-radios" data-v2="stat-type" @change="onStatTypeChange">
          <el-radio v-for="o in STAT_TYPE_OPTIONS" :key="o.value" :value="o.value">{{ o.title }}</el-radio>
        </el-radio-group>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onQuery">搜索</button>
        </div>
      </div>
    </section>

    <!-- 6 个数字卡：炭火 KPI 卡（白面板 + 3px 语义顶条；利润组青绿、退款卡红；负值数字红） -->
    <section class="cv2-panel dr-cards-panel" :class="{ 'is-busy': loading }" aria-label="营业利润统计" data-v2="cards">
      <p class="cv2-panel-title" data-v2="cards-title">营业利润统计</p>
      <div class="dr-cards">
        <div
          v-for="spec in CARD_SPECS"
          :key="spec.key"
          class="dr-card"
          :class="spec.refund ? 'dr-card--refund' : 'dr-card--profit'"
          :data-v2-card="spec.key"
          :title="spec.tip"
        >
          <p class="dr-card-label">{{ spec.title }}</p>
          <p
            class="dr-card-value cv2-num"
            :class="{ 'dr-card-value--neg': (cards?.[spec.key] ?? '').startsWith('-') }"
            :data-v2-card-value="spec.key"
          >{{ cards?.[spec.key] ?? '—' }}</p>
        </div>
      </div>
    </section>

    <!-- 两张饼（整环扇形基座；图例竖排左、悬停 ⇄ 高亮；口径句逐字在卡头下方） -->
    <div class="dr-charts" :class="{ 'is-busy': loading }" data-v2="pie-charts">
      <section class="cv2-panel dr-chart-half" aria-label="订单主要来源分布图" data-v2="source-chart-pane">
        <p class="cv2-panel-title">订单主要来源分布图</p>
        <p class="cv2-note dr-chart-note" data-v2="source-note">{{ sourcePieNote }}</p>
        <div v-if="sourceDonutData !== null" class="donut-wrap">
          <div class="donut-legend" data-v2="source-legend">
            <button
              v-for="slice in sourceDonutData.slices"
              :key="slice.name"
              type="button"
              class="donut-legend-row"
              :class="{ 'donut-legend-row--off': hiddenSlices.source.includes(slice.name) }"
              :data-v2-legend="slice.name"
              @mouseenter="onLegendEnter('source', slice.name)"
              @mouseleave="onLegendLeave('source')"
              @click="onLegendToggle('source', slice.name)"
            >
              <span class="donut-dot" :style="{ background: slice.color }" aria-hidden="true"></span>
              <span class="donut-legend-name">{{ slice.name }}</span>
              <span class="donut-legend-pct cv2-num">{{ slice.percentText }}</span>
            </button>
          </div>
          <div ref="sourceChartEl" class="dr-canvas" data-v2="source-chart"></div>
        </div>
        <p v-else class="dr-empty" data-v2="chart-empty">暂无数据</p>
      </section>
      <section class="cv2-panel dr-chart-half" aria-label="利润来源分布图" data-v2="profit-chart-pane">
        <p class="cv2-panel-title">利润来源分布图</p>
        <p class="cv2-note dr-chart-note" data-v2="profit-note">{{ profitPieNote }}</p>
        <div v-if="profitDonutData !== null" class="donut-wrap">
          <div class="donut-legend" data-v2="profit-legend">
            <button
              v-for="slice in profitDonutData.slices"
              :key="slice.name"
              type="button"
              class="donut-legend-row"
              :class="{ 'donut-legend-row--off': hiddenSlices.profit.includes(slice.name) }"
              :data-v2-legend="slice.name"
              @mouseenter="onLegendEnter('profit', slice.name)"
              @mouseleave="onLegendLeave('profit')"
              @click="onLegendToggle('profit', slice.name)"
            >
              <span class="donut-dot" :style="{ background: slice.color }" aria-hidden="true"></span>
              <span class="donut-legend-name">{{ slice.name }}</span>
              <span class="donut-legend-pct cv2-num">{{ slice.percentText }}</span>
            </button>
          </div>
          <div ref="profitChartEl" class="dr-canvas" data-v2="profit-chart"></div>
        </div>
        <p v-else class="dr-empty" data-v2="profit-chart-empty">暂无数据</p>
      </section>
    </div>

    <!-- 客服接单统计（bar+bar+line 三系列三轴） -->
    <section class="cv2-panel" :class="{ 'is-busy': loading }" aria-label="客服接单统计" data-v2="support-chart-pane">
      <p class="cv2-panel-title">客服接单统计</p>
      <p class="cv2-note dr-chart-note" data-v2="support-note">
        统计筛选条件范围内有记录的客服接单数量、总金额、退单量，其中订单金额和数据已排除已退单的，[总计：{{ payload?.source_total.amount ?? '' }}元]
      </p>
      <div ref="supportChartEl" class="dr-canvas" data-v2="support-chart"></div>
    </section>

    <!-- 订单数量、总价趋势图（bar+bar+line+line 四系列三轴；最近 12 个自然月） -->
    <section class="cv2-panel" :class="{ 'is-busy': loading }" aria-label="订单数量、总价趋势图（已排除已退单部分）" data-v2="trend-chart-pane">
      <p class="cv2-panel-title">订单数量、总价趋势图（已排除已退单部分）</p>
      <div ref="trendChartEl" class="dr-canvas" data-v2="trend-chart"></div>
    </section>

    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 --tt2-* token 与 charts.ts JS 常量（⛔ 裸色值）。 */

/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、面板内边距 10/12），控件档位不动。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.flt-row {
  display: flex;
  align-items: flex-end;
  gap: 6px 8px;
  flex-wrap: wrap;
}
.flt-dash {
  padding-bottom: 8px;
  color: var(--tt2-dim);
}
.dr-radios {
  padding-bottom: 6px;
}

/* 查询期间区块降透明（失败解除；⛔ 不用 layer.load）。 */
.is-busy {
  opacity: 0.45;
}

/* 数字等宽（V2OrderManage 同款）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 炭火 KPI 卡：白面板 + 1px 线框 + 顶部 3px 语义顶条（§7；利润组青绿、退款卡红）。 */
.dr-cards-panel {
  margin-top: 12px;
}
.dr-cards {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}
@media (max-width: 1100px) {
  .dr-cards {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 560px) {
  .dr-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.dr-card {
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  padding: 10px 12px 12px;
  background: var(--tt2-panel);
}
.dr-card--profit {
  border-top: 3px solid var(--tt2-primary);
}
.dr-card--refund {
  border-top: 3px solid var(--tt2-st-error-dot);
}
.dr-card-label {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--tt2-dim);
}
.dr-card-value {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
  color: var(--tt2-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dr-card-value--neg {
  color: var(--tt2-money-red);
}

/* 图表区：两饼各半（窄屏堆叠），轴图整宽。 */
.dr-charts {
  display: flex;
  gap: 12px;
  margin-top: 12px;
}
.dr-chart-half {
  flex: 1 1 340px;
  min-width: 0;
}
@media (max-width: 900px) {
  .dr-charts {
    flex-direction: column;
  }
}
.dr-charts + section,
.dr-cards-panel + .dr-charts,
section + section {
  margin-top: 12px;
}
.dr-chart-note {
  margin: 2px 0 8px;
}
.dr-canvas {
  width: 100%;
  height: 400px;
}
.dr-empty {
  margin: 12px 0;
  color: var(--tt2-dim);
}

/* 扇形区：图例竖排在左、canvas 在右。图例行按 Metabase 规格：
   8px 圆角点(32%) + 4px + 加粗名字 + 右对齐百分比；行距 4px；最大宽 200px；点行显隐该切片。 */
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 16px;
  min-height: 400px;
}
.donut-legend {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: none;
  max-width: 200px;
}
.donut-legend-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  color: inherit;
  text-align: left;
  width: 100%;
}
.donut-legend-row--off {
  opacity: 0.35;
}
.donut-dot {
  width: 8px;
  height: 8px;
  border-radius: 32%;
  flex: none;
}
.donut-legend-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 700;
  font-size: 13px;
  color: var(--tt2-ink);
}
.donut-legend-pct {
  color: var(--tt2-dim);
  flex: none;
  font-size: 13px;
}
</style>
