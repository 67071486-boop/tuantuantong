<script setup lang="ts">
/**
 * 「闲管家收款明细」v2（`/v2/xgj-receipts`；财务族第一批 2026-11-06，计划
 * `docs/plans/2026-11-06-finance-batch1.md` §3）。旧页 = island `xgj-receipts.ts`。
 * 重排版：口径声明逐字 → 筛选行（默认当天 + 分组下拉，改选即查）→
 * 上部左店铺列表½+右实收扇形½（`v2/charts.ts`）→ 下方逐笔明细（点行加载、50/页、
 * 缺失灰字「缺失」）；筛选（含分组）变更明细区重置为未选中态。
 * API 照旧页三件（`api/xgjReceipts` + `api/orderSources`）；`source_group` 空值不发键、
 * 仅 >0 整数发；`shop_identity` 空串=未识别桶照样发；合计读后端 `totals`（⛔ 前端加法）；
 * 字典失败不拦页；页内深链门照旧页 fail-closed。⛔ 不 import island 页模块。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { fetchOrderSourceGroups } from '@/api/orderSources'
import {
  fetchReceiptList,
  fetchReceiptSummary,
  XGJ_RECEIPT_NODE,
  type ReceiptDetailRow,
  type ReceiptRange,
  type ReceiptSummaryRow,
} from '@/api/xgjReceipts'
import { useSessionStore } from '@/stores/session'
import {
  buildDonutData,
  createDonut,
  loadECharts,
  type DonutController,
  type DonutData,
} from '@/v2/charts'

const session = useSessionStore()

/* ── 逐字文案（出处 = island 页 MESSAGES 常量）───────────────────────────── */

const NO_ACCESS_MESSAGE = '无权限查看闲管家收款明细'
const UNKNOWN_SHOP_LABEL = '未识别店铺'
const DETAIL_PAGE_SIZE = 50
const MESSAGES = {
  summaryEmpty: '这个区间里还没有交易成功的记录',
  detailIdle: '点上面的一行店铺，看它的逐笔明细',
  detailEmpty: '这家店在这个区间里没有明细',
  caliberNote:
    '统计口径：按「交易成功」记录；金额=买家实付减已退（按详情回查时点），'
    + '不含交易成功之后又退款的冲减（退款走自动退单线）。'
    + '「订单分组」按店铺当前所属来源统计（历史行会随归组变化，未识别店铺不计入任何分组）。',
} as const

function errText(e: unknown): string {
  return e instanceof Error && e.message !== '' ? e.message : '加载失败'
}

/* ── 提示（炭火 toast；info 1500ms / error 6000ms）───────────────────────── */

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

/* ── 权限门（fail-closed，照旧页；无节点 ⇒ 一句话、一个请求都不发）────────────── */

const gateDenied = ref(false)

/* ── 默认窗与筛选 ────────────────────────────────────────────────────────── */

const pad2 = (n: number): string => String(n).padStart(2, '0')

function todayStr(now: Date = new Date()): string {
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`
}

const initialDay = todayStr()
const dateFrom = ref(initialDay)
const dateTo = ref(initialDay)
/** 分组下拉的值（'' = 全部分组；发参时仅 >0 整数才发键）。 */
const group = ref('')
const groups = ref<{ id: number; name: string }[]>([])

/** 当前筛选（改选即变；形状照旧页 range：空分组不发键）。 */
function buildRange(): ReceiptRange {
  const next: ReceiptRange = { date_from: dateFrom.value, date_to: dateTo.value }
  const groupId = Number(group.value)
  if (group.value !== '' && Number.isFinite(groupId) && groupId > 0) {
    next.source_group = groupId
  }
  return next
}

let range: ReceiptRange = buildRange()

/* ── 店铺列表（summary）──────────────────────────────────────────────────── */

const summaryRows = ref<ReceiptSummaryRow[]>([])
type Totals = { shop_count: number; count: number; amount: string; missing_count: number }
const totals = ref<Totals | null>(null)
const hint = ref('')
let summarySeq = 0

/** 汇总行展示名：后端兜到组键原文；名字与组键都空 ⇒「未识别店铺」（旧页 shopDisplayName 同式）。 */
function shopDisplayName(row: Pick<ReceiptSummaryRow, 'shop_identity' | 'shop_name'>): string {
  if (row.shop_name !== '') return row.shop_name
  return row.shop_identity === '' ? UNKNOWN_SHOP_LABEL : row.shop_identity
}

async function loadSummary(): Promise<void> {
  const seq = ++summarySeq
  try {
    const data = await fetchReceiptSummary(range)
    if (seq !== summarySeq) return
    summaryRows.value = data.items
    totals.value = data.totals
    hint.value =
      `共 ${data.totals.shop_count} 家店铺 / ${data.totals.count} 笔 / ` +
      `${data.totals.amount} 元（缺失 ${data.totals.missing_count} 笔）`
    await nextTick()
    await drawChart()
  } catch (error) {
    if (seq === summarySeq) showToast(errText(error), 'error')
  }
}

/** 筛选（含分组）变更：整个重建 range + 明细区回到未选中态（旧店明细与新口径不是一回事）。 */
function onFilterChange(): void {
  range = buildRange()
  currentShop.value = null
  detailPage.value = 1
  detailTotal.value = 0
  detailRows.value = []
  void loadSummary()
}

/* ── 扇形（实收分布；缺失笔数不进扇形）───────────────────────────────────── */

const chartEl = ref<HTMLElement | null>(null)
let controller: DonutController | null = null

const donutData = computed<DonutData | null>(() =>
  buildDonutData(summaryRows.value.map((row) => ({ name: shopDisplayName(row), amount: row.amount }))),
)

/** 图例点击显隐（Metabase 行为）：按名字记隐藏态，重画时过滤 slices（中心总计不变）。 */
const hiddenSlices = reactive<string[]>([])

function visibleData(): DonutData | null {
  const data = donutData.value
  if (data === null) return null
  return { ...data, slices: data.slices.filter((slice) => !hiddenSlices.includes(slice.name)) }
}

async function drawChart(): Promise<void> {
  const data = visibleData()
  if (data === null) {
    controller?.dispose()
    controller = null
    return
  }
  const el = chartEl.value
  if (el === null) return
  try {
    const echarts = await loadECharts()
    if (controller === null) {
      controller = createDonut(el, echarts, { hideSliceLabels: true, hideTipOnLeave: true })
    }
    controller.update(data, totals.value?.amount ?? '', '店铺')
    controller.resize()
  } catch (error) {
    showToast(errText(error), 'error')
  }
}

function onLegendEnter(name: string): void {
  controller?.highlight(name)
}
function onLegendLeave(): void {
  controller?.downplay()
}
function onLegendToggle(name: string): void {
  const at = hiddenSlices.indexOf(name)
  if (at >= 0) hiddenSlices.splice(at, 1)
  else hiddenSlices.push(name)
  const data = visibleData()
  if (controller !== null && data !== null) {
    controller.update(data, totals.value?.amount ?? '', '店铺')
  }
}

function onWindowResize(): void {
  controller?.resize()
}

/* ── 逐笔明细（主从：点店铺行加载）───────────────────────────────────────── */

const currentShop = ref<{ shop_identity: string; shop_name: string } | null>(null)
const detailPage = ref(1)
const detailTotal = ref(0)
const detailRows = ref<ReceiptDetailRow[]>([])
let detailSeq = 0

const detailTitle = computed(() => {
  if (currentShop.value === null) return MESSAGES.detailIdle
  return `明细：${currentShop.value.shop_name}（第 ${detailPage.value} 页 / 共 ${detailTotal.value} 笔）`
})

function formatSuccessTime(value: string | null): string {
  return (value ?? '').replace('T', ' ').slice(0, 19)
}

async function loadDetail(): Promise<void> {
  const shop = currentShop.value
  if (shop === null) return
  const seq = ++detailSeq
  try {
    // 空串 shop_identity = 「未识别店铺」桶，参数照样发（后端按 identity 为空筛）。
    const page = await fetchReceiptList({
      ...range,
      shop_identity: shop.shop_identity,
      page: detailPage.value,
      page_size: DETAIL_PAGE_SIZE,
    })
    if (seq !== detailSeq || currentShop.value !== shop) return
    detailTotal.value = page.total
    detailRows.value = page.items
  } catch (error) {
    if (seq === detailSeq) showToast(errText(error), 'error')
  }
}

function onRowClick(row: ReceiptSummaryRow): void {
  currentShop.value = { shop_identity: row.shop_identity, shop_name: shopDisplayName(row) }
  detailPage.value = 1
  void loadDetail()
}

function goPage(delta: number): void {
  const next = detailPage.value + delta
  if (next < 1 || (detailTotal.value > 0 && next > Math.ceil(detailTotal.value / DETAIL_PAGE_SIZE))) {
    return
  }
  detailPage.value = next
  void loadDetail()
}

/* ── 装载 ───────────────────────────────────────────────────────────────── */

onMounted(async () => {
  if (!session.canNode([XGJ_RECEIPT_NODE])) {
    gateDenied.value = true
    return
  }
  window.addEventListener('resize', onWindowResize)
  // 分组字典取失败不拦页：只剩「全部分组」（这一项筛不了，页面照常可用）。
  groups.value = await fetchOrderSourceGroups()
    .then((page) => (page.items ?? []).map((item) => ({ id: item.id, name: item.name })))
    .catch(() => [])
  await loadSummary()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  controller?.dispose()
  controller = null
})
</script>

<template>
  <div class="cv2-page" data-v2-page="xgj-receipts">
    <template v-if="!gateDenied">
      <!-- 筛选行：起止日期 + 订单分组；改选即查（R7），无查询钮 -->
      <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
        <div class="flt-row">
          <label class="cv2-field">
            <span class="cv2-field-label">开始日期</span>
            <input v-model="dateFrom" class="cv2-input" type="date" data-v2-filter="date-from" @change="onFilterChange" />
          </label>
          <span class="flt-dash" aria-hidden="true">~</span>
          <label class="cv2-field">
            <input v-model="dateTo" class="cv2-input" type="date" data-v2-filter="date-to" @change="onFilterChange" />
          </label>
          <label class="cv2-field">
            <span class="cv2-field-label">订单分组</span>
            <select v-model="group" class="cv2-input" data-v2-filter="source-group" @change="onFilterChange">
              <option value="">全部分组</option>
              <option v-for="g in groups" :key="g.id" :value="String(g.id)">{{ g.name }}</option>
            </select>
          </label>
          <span class="flt-hint cv2-num" data-v2="hint">{{ hint }}</span>
        </div>
      </section>

      <!-- 统计口径声明（逐字） -->
      <p class="cv2-note" data-v2="caliber">{{ MESSAGES.caliberNote }}</p>

      <!-- 上部：左店铺列表 ½ + 右实收分布扇形 ½ -->
      <div class="split" data-v2-pane="summary">
        <section class="split-left cv2-panel" aria-label="按店铺汇总">
          <table class="cv2-table" data-v2="summary-table">
            <thead>
              <tr>
                <th class="xr-name">店铺名称</th>
                <th class="xr-num">交易成功笔数</th>
                <th class="xr-num">实收总金额（元）</th>
                <th class="xr-num">金额缺失笔数</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in summaryRows"
                :key="row.shop_identity === '' ? '__unknown__' : row.shop_identity"
                class="xr-row"
                :data-v2-shop="row.shop_name !== '' ? row.shop_name : row.shop_identity"
                @click="onRowClick(row)"
              >
                <td class="xr-name">{{ shopDisplayName(row) }}</td>
                <td class="xr-num cv2-num">{{ row.count }}</td>
                <td class="xr-num cv2-money">{{ row.amount }}</td>
                <td class="xr-num cv2-num">{{ row.missing_count }}</td>
              </tr>
            </tbody>
            <tfoot v-if="totals !== null" class="xr-total">
              <tr>
                <td class="xr-name">合计（{{ totals.shop_count }} 家店铺）</td>
                <td class="xr-num cv2-num">{{ totals.count }} 笔</td>
                <td class="xr-num cv2-money">{{ totals.amount }} 元</td>
                <td class="xr-num cv2-num">{{ totals.missing_count }} 笔金额缺失</td>
              </tr>
            </tfoot>
          </table>
          <p v-if="summaryRows.length === 0" class="xr-empty" data-v2="summary-empty">{{ MESSAGES.summaryEmpty }}</p>
        </section>
        <section class="split-right cv2-panel" aria-label="实收分布" data-v2="chart-pane">
          <div v-if="donutData !== null" class="donut-wrap">
            <div class="donut-legend" data-v2="legend">
              <button
                v-for="slice in donutData.slices"
                :key="slice.name"
                type="button"
                class="donut-legend-row"
                :class="{ 'donut-legend-row--off': hiddenSlices.includes(slice.name) }"
                :data-v2-legend="slice.name"
                @mouseenter="onLegendEnter(slice.name)"
                @mouseleave="onLegendLeave()"
                @click="onLegendToggle(slice.name)"
              >
                <span class="donut-dot" :style="{ background: slice.color }" aria-hidden="true"></span>
                <span class="donut-legend-name">{{ slice.name }}</span>
                <span class="donut-legend-pct cv2-num">{{ slice.percentText }}</span>
              </button>
            </div>
            <div ref="chartEl" class="donut-canvas" data-v2="chart"></div>
          </div>
          <p v-else class="xr-empty" data-v2="chart-empty">{{ MESSAGES.summaryEmpty }}</p>
        </section>
      </div>

      <!-- 下方：逐笔明细区（点店铺行加载） -->
      <section class="cv2-panel xr-detail" aria-label="逐笔明细" data-v2="detail">
        <p class="xr-detail-title" data-v2="detail-title">{{ detailTitle }}</p>
        <template v-if="currentShop !== null">
          <table class="cv2-table" data-v2="detail-table">
            <thead>
              <tr>
                <th class="xr-name">订单号</th>
                <th class="xr-name">买家会员名</th>
                <th class="xr-num">实收金额（元）</th>
                <th class="xr-name">交易成功时间</th>
                <th class="xr-name">匹配结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in detailRows" :key="row.id">
                <td class="xr-name cv2-num">{{ row.order_no }}</td>
                <td class="xr-name">{{ row.user_name }}</td>
                <td class="xr-num">
                  <span v-if="row.amount_missing" class="xr-missing" data-v2="amount-missing">缺失</span>
                  <span v-else class="cv2-money">{{ row.amount }}</span>
                </td>
                <td class="xr-name cv2-num">{{ formatSuccessTime(row.success_time) }}</td>
                <td class="xr-name">{{ row.match_summary }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="detailRows.length === 0" class="xr-empty" data-v2="detail-empty">{{ MESSAGES.detailEmpty }}</p>
          <div class="xr-pager">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="prev-page" @click="goPage(-1)">上一页</button>
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="next-page" @click="goPage(1)">下一页</button>
          </div>
        </template>
        <p v-else class="xr-empty" data-v2="detail-idle">{{ MESSAGES.detailIdle }}</p>
      </section>
    </template>
    <p v-else class="cv2-note xr-empty" data-v2="no-access">{{ NO_ACCESS_MESSAGE }}</p>

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
/* 本页形状收在这里；色一律走 charts.ts 常量（扇形）与 --tt2-* token（CSS），⛔ 不写裸色值。 */

/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、面板内边距 10/12），控件档位不动。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}
.cv2-page .cv2-filter .cv2-field { border-color: var(--tt2-filter-line); }
.cv2-page .cv2-filter .cv2-field-label { color: var(--tt2-dim); }

/* 筛选行与分栏：窄屏时上下排列。 */
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
.flt-hint {
  padding-bottom: 8px;
  color: var(--tt2-dim);
}
.split {
  display: flex;
  align-items: stretch;
  gap: 12px;
  margin-top: 12px;
}
.split-left,
.split-right {
  flex: 1 1 340px;
  min-width: 0;
}
.split-left {
  overflow: auto;
}
@media (max-width: 900px) {
  .split {
    flex-direction: column;
  }
}

.xr-num {
  text-align: right;
}
.xr-name {
  text-align: left;
  min-width: 6em;
}

/* 点行加载明细：主行可点（hover 晕由 .cv2-table 全局提供）。 */
.xr-row {
  cursor: pointer;
}

/* 合计行（值 = 后端 totals）：分隔线 + 加粗。 */
.xr-total td {
  border-top: 1px solid var(--tt2-line);
  font-weight: 600;
}

.xr-empty {
  margin: 12px 0;
  color: var(--tt2-dim);
}

/* 缺失金额：灰字「缺失」（⛔ 不画 0.00）。 */
.xr-missing {
  color: var(--tt2-dim);
}

/* 扇形区：图例竖排在左、canvas 在右（与客服接发单页同款）。图例行按 Metabase 规格：
   8px 圆角点(32%) + 4px + 加粗名字 + 右对齐百分比；行距 4px；最大宽 200px；点行显隐该切片。 */
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 16px;
  min-height: 260px;
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
.donut-canvas {
  flex: 1 1 auto;
  min-width: 0;
  height: 260px;
}

/* 明细区。 */
.xr-detail {
  margin-top: 12px;
}
.xr-detail-title {
  margin: 0 0 8px;
  font-weight: 600;
}
.xr-pager {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
</style>
