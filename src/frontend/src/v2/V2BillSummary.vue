<script setup lang="ts">
/**
 * 「结算记录」v2（`/v2/bill-summary`；财务族第二批 2026-11-06，计划
 * `docs/plans/2026-11-06-finance-batch2.md` §3）。旧页 = island 自建页
 * `layui-island/pages/emp-billsummary.ts`（D-0319 按天聚合 + D-0323 来源分解），本页按
 * 同一功能重建为炭火 Vue 页：主表 4 列（金额格 = 来源分解展开开关 ▼/▲，lazy 取数、
 * 收起保留缓存、换页/搜索/刷新作废）→ 明细卡 7 列 + totalRow + 客户端分页 + 订单号精确
 * 匹配（Enter 同发）+ 导出（date_type:'settled' 当天两端，⛔ 不带 order_no、端点带不了
 * order_source——照旧页如实）；「实际结算金额」取值唯一出口 settledOrPayable；
 * 关联订单蓝链开订单详情 iframe 弹窗（V2Income 先例，20 位形状闸）。金额一律后端字符串
 * 原样上屏（合计行 = 整数分求和，对齐旧页 layui totalRow 的口径：数据是当天全集）。
 * ⛔ 不 import island 页模块（纯函数为带出处注记的本地重写）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { isOrderNoShape } from '@/api/orders'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import FinanceExportJobStatus from '@/v2/FinanceExportJobStatus.vue'
import { useFinanceExportJob } from '@/v2/useFinanceExportJob'
import { useSessionStore } from '@/stores/session'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'
import { downloadExport } from '@/api/performances'
import {
  fetchAllSettlements,
  fetchSettlementDailySources,
  fetchSettlementDailySummary,
  type SettlementDailyItem,
  type SettlementListItem,
  type SettlementSourceItem,
} from '@/api/settlements'

/* ── 逐字文案（出处 = island 页同名常量/字面量，本地重写不引模块）────────────── */

/** 来源为空（source_id / source_name 都 null）时的显示（旧页 UNSPECIFIED_SOURCE_LABEL）。 */
const UNSPECIFIED_SOURCE_LABEL = '未指定'
const BAD_DATE_TEXT = '日期格式不正确，无法查看结算明细'
const NO_DAY_FOR_SEARCH_TEXT = '请先在上方列表点击「查看明细」选择日期'
const NO_DAY_FOR_EXPORT_TEXT = '请先点击「查看明细」选择日期'
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'
const SOURCES_LOADING_TEXT = '正在加载来源分解…'
const SOURCES_ERROR_TEXT = '来源分解加载失败'
/** 明细日期形状闸（旧页 DATE_SHAPE；data-date 过 DOM 属性，形状不认识不取数）。 */
const DATE_SHAPE = /^\d{4}-\d{2}-\d{2}$/

/** 错误对象 → 用户可见文案（后端 msg 原文；不自造）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
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

const errMsg = (text: string): void => showToast(text, 'error')

/* ── 筛选（结账时间起止；默认今天 − 3 个自然月，运行期算）───────────────────── */

const pad2 = (n: number): string => String(n).padStart(2, '0')
const ymd = (d: Date): string => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`

/** 默认窗 = 今天 − 3 个自然月 ~ 今天（旧页 defaultDateRange 的日期形状版）。 */
function defaultDateRange(now: Date = new Date()): { dateFrom: string; dateTo: string } {
  const from = new Date(now.getFullYear(), now.getMonth() - 3, now.getDate())
  return { dateFrom: ymd(from), dateTo: ymd(now) }
}

const initial = defaultDateRange()
const dateFrom = ref(initial.dateFrom)
const dateTo = ref(initial.dateTo)

/* ── 主表（按天；服务端分页）+ 来源分解（D-0323）──────────────────────────── */

const activeType = ref<'打单工资' | '销售提成'>('打单工资')

const DAILY_PAGE_SIZE = 20
const DAILY_PAGE_LIMITS = [20, 50, 100]

const dailyItems = ref<SettlementDailyItem[]>([])
const dailyTotal = ref(0)
const page = ref(1)
const pageSize = ref(DAILY_PAGE_SIZE)
const loading = ref(false)
/** 主表数据的「代际」：取数 +1；展开途中的翻页/搜索/刷新让来源缓存整体作废，
 * 迟到的来源分解结果按代际丢弃（旧页 dailyGeneration 同款）。 */
let dailyGeneration = 0

/** 展开的日期集合 + 每天的来源分解缓存（收起保留；ok/error/loading）。 */
const expandedDays = ref(new Set<string>())
type SourcesState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ok'; items: SettlementSourceItem[] }
const sourcesByDay = ref(new Map<string, SourcesState>())

/** 展开区渲染行 = 天行 + （展开时）来源子行 / 状态行。 */
type DailyRow =
  | { kind: 'day'; day: SettlementDailyItem }
  | { kind: 'source'; date: string; item: SettlementSourceItem }
  | { kind: 'source-loading'; date: string }
  | { kind: 'source-error'; date: string }

const displayRows = computed<DailyRow[]>(() => {
  const out: DailyRow[] = []
  for (const day of dailyItems.value) {
    out.push({ kind: 'day', day })
    if (!expandedDays.value.has(day.date)) continue
    const state = sourcesByDay.value.get(day.date)
    if (state === undefined) {
      out.push({ kind: 'source-loading', date: day.date })
      continue
    }
    if (state.status === 'ok') {
      for (const item of state.items) out.push({ kind: 'source', date: day.date, item })
    } else if (state.status === 'loading') {
      out.push({ kind: 'source-loading', date: day.date })
    } else {
      out.push({ kind: 'source-error', date: day.date })
    }
  }
  return out
})

/** 来源子行的名字位：null ⇒ 「未指定」（后端契约；⛔ 不渲染成空串冒充某家店）。 */
function sourceDisplayName(item: SettlementSourceItem): string {
  return item.source_name ?? UNSPECIFIED_SOURCE_LABEL
}

/** 来源 id 的参数值唯一出口（旧页 sourceParamValue）：null ⇒ 字面 'none'（后端「未指定」档）。 */
function sourceParamValue(item: SettlementSourceItem): number | 'none' {
  return item.source_id ?? 'none'
}

async function loadDaily(nextPage = 1, nextSize = DAILY_PAGE_SIZE): Promise<void> {
  const generation = ++dailyGeneration
  loading.value = true
  dailyItems.value = []
  dailyTotal.value = 0
  // 主表数据一换 ⇒ 展开态与来源缓存全部作废（新的一页对不上旧展开）。
  expandedDays.value = new Set()
  sourcesByDay.value = new Map()
  try {
    const data = await fetchSettlementDailySummary({
      date_from: dateFrom.value,
      date_to: dateTo.value,
      emp_type: activeType.value,
      page: nextPage,
      page_size: nextSize,
    })
    if (generation !== dailyGeneration) return
    dailyItems.value = data.items
    dailyTotal.value = data.total
    page.value = nextPage
    pageSize.value = nextSize
  } catch (e) {
    if (generation !== dailyGeneration) return
    // 失败出空表：留着上一页的旧数字比空表更容易被误读（旧页同口径）。
    errMsg(errText(e))
    dailyItems.value = []
    dailyTotal.value = 0
    page.value = nextPage
    pageSize.value = nextSize
  } finally {
    if (generation === dailyGeneration) loading.value = false
  }
}

function onSearch(): void {
  resetDetail()
  void loadDaily(1, pageSize.value)
}

function onRefresh(): void {
  void loadDaily(page.value, pageSize.value)
}

function onPageChange(next: number): void {
  void loadDaily(next, pageSize.value)
}

function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  void loadDaily(1, size)
}

function selectType(type: '打单工资' | '销售提成'): void {
  if (activeType.value === type) return
  activeType.value = type
  resetDetail()
  void loadDaily(1, pageSize.value)
}

/** 拉某天的来源分解（展开首拉与失败重试共用；收起状态下迟到结果按代际丢弃）。 */
async function loadSources(date: string): Promise<void> {
  const generation = dailyGeneration
  sourcesByDay.value.set(date, { status: 'loading' })
  try {
    const data = await fetchSettlementDailySources(date, activeType.value)
    if (generation !== dailyGeneration) return
    sourcesByDay.value.set(date, { status: 'ok', items: data.items })
  } catch (e) {
    if (generation !== dailyGeneration) return
    sourcesByDay.value.set(date, { status: 'error', message: errText(e) })
    errMsg(errText(e))
  }
}

/** 点击金额格：toggle 展开 / 收起。收起保留缓存（再展开不重拉）；形状闸同旧页。 */
function toggleExpand(date: string): void {
  if (!DATE_SHAPE.test(date)) return
  const next = new Set(expandedDays.value)
  if (next.has(date)) {
    next.delete(date)
    expandedDays.value = next
    return
  }
  next.add(date)
  expandedDays.value = next
  if (sourcesByDay.value.has(date)) return
  void loadSources(date)
}

function onRetrySources(date: string): void {
  if (DATE_SHAPE.test(date)) void loadSources(date)
}

/* ── 明细卡（当天明细；客户端分页 + 订单号精确匹配 + 导出）──────────────────── */

const DETAIL_PAGE_LIMIT = 20
const DETAIL_PAGE_LIMITS = [20, 30, 40, 50, 100]

/** 实际结算金额的取值唯一出口（旧页 settledOrPayable：快照优先、回落活值；只选字段不算钱）。 */
function settledOrPayable(
  row: Pick<SettlementListItem, 'settled_amount' | 'payable_net'>,
): string {
  return row.settled_amount ?? row.payable_net
}

function settlementType(value: string): string {
  if (value === '打单工资') return '代练报酬'
  if (value === '销售提成') return '客服提成'
  return value
}

/** 结算时间：取后端渲染串里的墙钟时间，不按浏览器时区二次换算（旧页 formatSettledAt）。 */
function formatSettledAt(value: string | null | undefined): string {
  if (value === null || value === undefined) return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(String(value).trim())
  return m ? `${m[1]} ${m[2]}` : ''
}

const detailTitle = ref('')
const detailRows = ref<SettlementListItem[]>([])
const detailOrderNo = ref('')
const detailPage = ref(1)
const detailPageSize = ref(DETAIL_PAGE_LIMIT)
/** 正在看的那一天与来源上下文；null = 还没选天（导出/明细搜索据此拦）。 */
let detailGeneration = 0
let currentDetailDay: string | null = null
let currentDetailSource: { value: number | 'none'; label: string } | null = null
let currentDetailOrderNo = ''

const detailPaged = computed<SettlementListItem[]>(() => {
  const start = (detailPage.value - 1) * detailPageSize.value
  return detailRows.value.slice(start, start + detailPageSize.value)
})

/** totalRow 合计（旧页 layui totalRow 同口径：数据是 fetchAll 的当天全集；整数分求和）。 */
function sumCents(values: readonly string[]): string {
  const cents = values.reduce((acc, v) => acc + Math.round(Number(v) * 100), 0)
  return (cents / 100).toFixed(2)
}
const detailTempTotal = computed(() => sumCents(detailRows.value.map((r) => r.temp_amount)))
const detailRealTotal = computed(() =>
  sumCents(detailRows.value.map((r) => settledOrPayable(r))),
)

/** 明细取数参数（旧页 toDetailParams：必带已结账 + 当天两端；order_no 空不发；来源行带 order_source）。 */
function toDetailParams(
  day: string,
  orderNo: string,
  orderSource?: number | 'none',
): Record<string, string | number> {
  const out: Record<string, string | number> = {
    settle_status: '已结账',
    emp_type: activeType.value,
    date_from: day,
    date_to: day,
  }
  if (orderSource !== undefined) out.order_source = orderSource
  const no = orderNo.trim()
  if (no !== '') out.order_no = no
  return out
}

/** 还原明细卡为「还没选天」的形态（搜索后旧的一天不再对应新筛选）。 */
function resetDetail(): void {
  detailGeneration++
  currentDetailDay = null
  currentDetailSource = null
  currentDetailOrderNo = ''
  detailTitle.value = ''
  detailRows.value = []
  detailPage.value = 1
  detailOrderNo.value = ''
}

async function openDayDetail(
  day: string,
  orderNo = '',
  source?: { value: number | 'none'; label: string },
): Promise<void> {
  if (!DATE_SHAPE.test(day)) {
    errMsg(BAD_DATE_TEXT)
    return
  }
  const generation = ++detailGeneration
  let rows: SettlementListItem[] = []
  try {
    rows = await fetchAllSettlements(toDetailParams(day, orderNo, source?.value))
  } catch (e) {
    if (generation !== detailGeneration) return
    // 失败只弹错、不动正在看的明细（旧页 F-03 口径：重看同一天失败时卡里就是这一天的行）。
    errMsg(errText(e))
    return
  }
  if (generation !== detailGeneration) return
  currentDetailDay = day
  currentDetailSource = source ?? null
  currentDetailOrderNo = orderNo
  detailRows.value = rows
  detailPage.value = 1
  detailTitle.value = source ? `${day}的结算明细（来源：${source.label}）` : `${day}的结算明细`
}

function onSearchDetail(): void {
  if (currentDetailDay === null) {
    showToast(NO_DAY_FOR_SEARCH_TEXT)
    return
  }
  void openDayDetail(currentDetailDay, detailOrderNo.value, currentDetailSource ?? undefined)
}

function onDetailPageSizeChange(): void {
  detailPage.value = 1
}

function onDetailEnter(): void {
  onSearchDetail()
}

/* ── 导出当前明细 ── */

const exporting = ref(false)
const session = useSessionStore()
const canExportSettlements = computed(() => session.canNode(['finance.payroll']) && session.canNode(['finance.export_performance']))
const exportJob = useFinanceExportJob('settlements')
const { job: recentExport, jobProblem: exportProblem, busy: exportBusy } = exportJob

function saveSettlementExport(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

async function onExport(): Promise<void> {
  if (exporting.value || exportBusy.value) return
  if (currentDetailDay === null) {
    showToast(NO_DAY_FOR_EXPORT_TEXT)
    return
  }
  const exportDay = currentDetailDay
  const exportType = activeType.value
  exporting.value = true
  try {
    const params = toDetailParams(exportDay, currentDetailOrderNo, currentDetailSource?.value)
    if (await exportJob.start(params)) return
    const { blob } = await downloadExport('/api/business/settlements/export', params)
    saveSettlementExport(blob, `结算记录-${exportDay}-${exportType}.xlsx`)
    showToast(`已导出 ${exportDay} ${exportType} 结算记录`)
  } catch (e) {
    errMsg(`导出 ${exportDay} ${exportType} 失败：${errText(e)}`)
  } finally {
    exporting.value = false
  }
}

async function downloadReadyExport(): Promise<void> {
  try {
    const file = await exportJob.download()
    if (file) saveSettlementExport(file.blob, '结算记录.xlsx')
  } catch (e) {
    errMsg(errText(e))
  }
}

const detailDlg = ref<{ orderNo: string } | null>(null)

function openOrderDialog(orderNo: string, viewNo: string): void {
  if (!isOrderNoShape(orderNo)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  detailDlg.value = { orderNo }
  void viewNo
}

function onOrderLink(row: SettlementListItem): void {
  // 标题前缀 = 展示单号（来源单号优先，data-orderIdView 口径）；取数键 = 系统单号。
  const viewNo = row.source_order_no !== '' ? row.source_order_no : row.order_no
  openOrderDialog(row.order_no, viewNo)
}

watch(detailPageSize, onDetailPageSizeChange)

// 首屏按默认区间（今天 − 3 个自然月）自动查一次（旧页同行为）；「搜索」钮用于改区间后重查。
onMounted(() => {
  void loadDaily(1, pageSize.value)
})
</script>

<template>
  <div class="cv2-page" data-v2-page="bill-summary">
    <FinanceExportJobStatus v-if="canExportSettlements && recentExport" :job="recentExport" :problem="exportProblem" @download="downloadReadyExport" @retry="exportJob.retryProgress" />
    <div class="bs-tabs" role="tablist" aria-label="结算类型">
      <button v-for="tab in [{ type: '打单工资', label: '打手工资' }, { type: '销售提成', label: '销售提成' }]"
        :key="tab.type" type="button" role="tab" :aria-selected="activeType === tab.type"
        :class="{ 'bs-tab--active': activeType === tab.type }" :data-v2-settlement-type="tab.type"
        @click="selectType(tab.type as '打单工资' | '销售提成')">{{ tab.label }}</button>
    </div>
    <!-- 筛选行：结账时间起止（默认今天 − 3 个自然月）+ 搜索 + 刷新数据 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-row">
        <label class="cv2-field">
          <span class="cv2-field-label">结账时间</span>
          <input v-model="dateFrom" class="cv2-input" type="date" data-v2-filter="date-from" />
        </label>
        <span class="flt-dash" aria-hidden="true">~</span>
        <label class="cv2-field">
          <input v-model="dateTo" class="cv2-input" type="date" data-v2-filter="date-to" />
        </label>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onSearch">搜索</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">
            刷新数据
          </button>
        </div>
      </div>
    </section>

    <!-- 主表：一行 = 一天；金额格 = 来源分解展开开关（▼/▲）；来源子行缩进同表 -->
    <section class="cv2-panel cv2-table-panel" aria-label="按天结算汇总" data-v2="daily-panel">
      <table class="cv2-table" :class="{ 'is-busy': loading }">
        <thead>
          <tr>
            <th>结账日期</th>
            <th class="cv2-th-num cv2-th-center">总单数</th>
            <th class="cv2-th-num cv2-th-center">总结算金额</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in displayRows" :key="row.kind + (row.kind === 'day' ? row.day.date : row.date)">
            <tr v-if="row.kind === 'day'" class="cv2-row" :data-v2-day="row.day.date">
              <td>{{ row.day.date }}</td>
              <td class="cv2-td-num cv2-td-center cv2-num" :data-v2-day-count="row.day.date">{{ row.day.order_count }}</td>
              <td class="cv2-td-num cv2-td-center">
                <span
                  class="bs-toggle cv2-money"
                  :data-v2-amount-toggle="row.day.date"
                  title="点击展开 / 收起按订单来源分解"
                  @click="toggleExpand(row.day.date)"
                >{{ row.day.total_amount }}<span class="bs-caret" aria-hidden="true"> {{ expandedDays.has(row.day.date) ? '▲' : '▼' }}</span></span>
              </td>
              <td class="cv2-td-center">
                <a href="#" class="cv2-actlink" :data-v2-view-detail="row.day.date" title="查看这一天的结算明细" @click.prevent="openDayDetail(row.day.date)">查看明细</a>
              </td>
            </tr>
            <tr v-else-if="row.kind === 'source'" class="bs-subrow" :data-v2-source-row="row.date">
              <td><span class="bs-subname">{{ sourceDisplayName(row.item) }}</span></td>
              <td class="cv2-td-num cv2-td-center cv2-num">{{ row.item.order_count }}</td>
              <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ row.item.total_amount }}</span></td>
              <td class="cv2-td-center">
                <a
                  href="#"
                  class="cv2-actlink"
                  :data-v2-view-source="sourceParamValue(row.item)"
                  :title="`查看这一天该来源的结算明细`"
                  @click.prevent="openDayDetail(row.date, '', { value: sourceParamValue(row.item), label: sourceDisplayName(row.item) })"
                >查看明细</a>
              </td>
            </tr>
            <tr v-else-if="row.kind === 'source-loading'" :data-v2-source-loading="row.date">
              <td><span class="bs-subname bs-status">{{ SOURCES_LOADING_TEXT }}</span></td>
              <td /><td /><td />
            </tr>
            <tr v-else :data-v2-source-error="row.date">
              <td><span class="bs-subname bs-status bs-status--err">{{ SOURCES_ERROR_TEXT }}</span></td>
              <td /><td />
              <td class="cv2-td-center">
                <a href="#" class="cv2-actlink cv2-actlink--danger" :data-v2-retry-sources="row.date" title="重新拉取来源分解" @click.prevent="onRetrySources(row.date)">重试</a>
              </td>
            </tr>
          </template>
          <tr v-if="displayRows.length === 0">
            <td colspan="4" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ dailyTotal }} 条</span>
        <label class="cv2-pager-size">
          每页
          <select class="cv2-input cv2-input--inline" data-v2="page-size" :value="pageSize" @change="onPageSizeChange">
            <option v-for="n in DAILY_PAGE_LIMITS" :key="n" :value="n">{{ n }}</option>
          </select>
          条
        </label>
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :total="dailyTotal"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
    </section>

    <!-- 明细卡：点「查看明细」/ 展开来源行后才取数；订单号精确匹配（Enter 同发）+ 导出 -->
    <section class="cv2-panel" aria-label="结算明细" data-v2="detail-panel">
      <header class="cv2-panel-head">
        <p class="cv2-panel-title" data-v2="detail-title">{{ detailTitle }}</p>
      </header>
      <div class="flt-row bs-detail-tools">
        <label class="cv2-field">
          <span class="cv2-field-label">订单号</span>
          <input
            v-model="detailOrderNo"
            class="cv2-input"
            type="text"
            placeholder="完整订单号（当前明细内精确匹配）"
            data-v2="detail-order-no"
            @keydown.enter.prevent="onDetailEnter"
          />
        </label>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find cv2-btn--fill" data-v2="detail-search" @click="onSearchDetail">搜索明细</button>
          <button v-if="canExportSettlements" type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="export" :disabled="exporting || exportBusy" @click="onExport">导出记录</button>
        </div>
      </div>
      <section class="cv2-table-panel" aria-label="当天结算明细" data-v2="detail-table-panel">
        <table class="cv2-table">
          <thead>
            <tr>
              <th class="cv2-th-center">结算对象人员</th>
              <th class="cv2-th-center">报酬类型</th>
              <th class="cv2-th-num cv2-th-center">预计结算金额</th>
              <th class="cv2-th-num cv2-th-center">实际结算金额</th>
              <th class="cv2-th-center">关联订单</th>
              <th>项目</th>
              <th class="cv2-th-center">结算时间</th>
              <th class="cv2-th-center">结算人</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in detailPaged" :key="row.id" class="cv2-row" :data-v2-detail-row="row.id">
              <td class="cv2-td-center">{{ row.display_name ?? row.username ?? '' }}</td>
              <td class="cv2-td-center">{{ settlementType(row.emp_type) }}</td>
              <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ row.temp_amount }}</span></td>
              <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ settledOrPayable(row) }}</span></td>
              <td class="cv2-td-center">
                <a href="#" class="cv2-ono" :data-v2-detail-order="row.order_no" title="点击查看订单详情" @click.prevent="onOrderLink(row)">{{ row.source_order_no || row.order_no }}</a>
              </td>
              <td>{{ row.project }}</td>
              <td class="cv2-td-center">{{ formatSettledAt(row.settled_at) }}</td>
              <td class="cv2-td-center">{{ row.settled_by }}</td>
            </tr>
            <tr v-if="detailPaged.length === 0">
              <td colspan="8" class="cv2-empty" data-v2="detail-empty">暂无数据</td>
            </tr>
          </tbody>
          <tfoot v-if="detailRows.length > 0" class="bs-total">
            <tr>
              <td class="cv2-td-center">合计</td>
              <td />
              <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ detailTempTotal }}</span></td>
              <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ detailRealTotal }}</span></td>
              <td /><td /><td /><td />
            </tr>
          </tfoot>
        </table>
        <div class="cv2-pager" data-v2="detail-pager">
          <span class="cv2-pager-total" data-v2="detail-pager-total">共 {{ detailRows.length }} 条</span>
          <label class="cv2-pager-size">
            每页
            <select class="cv2-input cv2-input--inline" data-v2="detail-page-size" v-model.number="detailPageSize">
              <option v-for="n in DETAIL_PAGE_LIMITS" :key="n" :value="n">{{ n }}</option>
            </select>
            条
          </label>
          <el-pagination
            v-model:current-page="detailPage"
            :page-size="detailPageSize"
            :total="detailRows.length"
            layout="prev, pager, next"
            background
          />
        </div>
      </section>
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

    <OrderDetailDialog
      :model-value="detailDlg !== null"
      :title="detailDlg === null ? '' : `${detailDlg.orderNo}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      destroy-on-close
      data-v2="dlg-detail"
      @update:model-value="detailDlg = null"
    >
      <V2OrderDetailPane
        v-if="detailDlg !== null"
        :order-no="detailDlg.orderNo"
        :view-no="detailDlg.orderNo"
      />
    </OrderDetailDialog>
  </div>
</template>

<style scoped>
/* 本页形状收在这里；色一律走 --tt2-* token（⛔ 裸色值）；金额红 = .cv2-money（carbon.css）。 */

.bs-tabs { display: flex; gap: 8px; margin-bottom: 12px; }
.bs-tabs button { border: 1px solid var(--tt2-filter-line); border-radius: 4px; padding: 8px 16px; background: var(--tt2-panel); color: var(--tt2-dim); cursor: pointer; }
.bs-tabs .bs-tab--active { border-color: var(--tt2-primary); color: var(--tt2-primary); font-weight: 600; }

/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、面板内边距 10/12），控件档位不动。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}
.cv2-page .cv2-filter .cv2-field { border-color: var(--tt2-filter-line); }
.cv2-page .cv2-filter .cv2-field-label { color: var(--tt2-dim); }

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

/* 取数期间主表降透明（失败解除；⛔ 不用 layer.load）。 */
.is-busy {
  opacity: 0.55;
}

/* 数字列等宽（V2OrderManage 同款：字体/尺寸非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 金额格展开开关：整格可点；指示符小一号、弱色。 */
.bs-toggle {
  cursor: pointer;
}
.bs-caret {
  font-size: 12px;
  color: var(--tt2-dim);
}

/* 来源子行：缩进一级 + 弱分隔（自拟视觉，无对标出处——旧页同位是 padding-left:24px）。 */
.bs-subname {
  padding-left: 24px;
}
.bs-status {
  color: var(--tt2-dim);
}
.bs-status--err {
  color: var(--tt2-st-error);
}

/* 明细工具行与合计行。 */
.bs-detail-tools {
  margin-bottom: 8px;
}
.bs-detail-tools .cv2-field {
  display: flex;
  align-items: stretch;
  gap: 0;
  overflow: hidden;
  border: 1px solid var(--tt2-filter-line);
  border-radius: 4px;
}
.bs-detail-tools .cv2-field-label {
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  background: var(--tt2-filter-label-bg);
  color: var(--tt2-dim);
}
.bs-detail-tools .cv2-input {
  width: 300px;
  max-width: 60vw;
  border: 0;
  border-radius: 0;
}
.bs-total td {
  border-top: 1px solid var(--tt2-line);
  font-weight: 600;
}
</style>
