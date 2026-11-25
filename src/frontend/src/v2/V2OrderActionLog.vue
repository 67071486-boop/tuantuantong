<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
/** 交易订单操作记录：明细按服务端分页，统计按筛选后的全集独立分页。 */
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { request } from '@/api/http'
import { fetchOrderTimeline, type OrderTimeline } from '@/api/auditTimeline'
import { BizError } from '@/api/errors'
import type { Page } from '@/api/unwrap'
import { formatCreatedAt } from '@/api/auditLogs'
import {
  ORDER_ACTION_LOGS_URL,
  toOrderActionLogParams,
  type OrderActionLogActionOption,
  type OrderActionLogActorOption,
  type OrderActionLogOrdering,
  type OrderActionLogRow,
  type OrderActionLogSearchParams,
  type OrderActionLogSummaryRow,
} from '@/api/orderActionLogs'
import {
  createOrderNote,
  fetchOrder,
  fetchOrderAudit,
  isOrderNoShape,
  type AuditEntry,
  type Order,
} from '@/api/orders'
import { v2OrderStatusLabel } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2DetailFace from '@/v2/V2DetailFace.vue'
import V2OrderTimeline from '@/v2/V2OrderTimeline.vue'

const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'
// 2026-11-06 统一布局口径：默认 50、档位 [20,50,100]。
const PAGE_LIMITS = [20, 50, 100]

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2OrderManage）──────────────────────────────── */

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

/* ── 权限门（fail-closed：会话取不到节点就只画一句，不拉数据）────────────────── */

const session = useSessionStore()
const route = useRoute()
const canView = computed(() => session.canNode(['order.platform_action_logs']))

/* ── 筛选（v1 语义：时间区间默认今天 −1 自然月 ~ 今天；空值由参数组装层丢弃）──── */

type ActionLogFilters = {
  sDate: string
  eDate: string
  action: string
  actor: string
  key1: string
}

function toDateInputValue(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 默认区间 = 今天 −1 个自然月 ~ 今天（v1 spec §3-D10 同口径；日夹到目标月月末防 1/31 溢出）。 */
function defaultDateRange(now: Date = new Date()): [string, string] {
  const target = new Date(now.getFullYear(), now.getMonth() - 1, 1)
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
  target.setDate(Math.min(now.getDate(), lastDay))
  return [toDateInputValue(target), toDateInputValue(now)]
}

const [defaultFrom, defaultTo] = defaultDateRange()

const linkedOrderNo = computed(() => {
  const orderNo = route.query.order_no
  return typeof orderNo === 'string' && isOrderNoShape(orderNo) ? orderNo : ''
})

const filters = reactive<ActionLogFilters>({
  sDate: defaultFrom,
  eDate: defaultTo,
  action: '',
  actor: '',
  key1: '',
})

function setSDate(v: unknown): void {
  filters.sDate = typeof v === 'string' ? v : ''
}

function setEDate(v: unknown): void {
  filters.eDate = typeof v === 'string' ? v : ''
}

/* ── 下拉候选（都来自列表响应的 data，后端是唯一出处，⛔ 前端不镜像词表）───────── */

const actionOptions = ref<OrderActionLogActionOption[]>([])
const actorOptions = ref<OrderActionLogActorOption[]>([])

/* ── 取数（服务端分页；summary/候选随同一发响应回来）──────────────────────────── */

type ActionLogData = Page<OrderActionLogRow> & {
  summary?: OrderActionLogSummaryRow[]
  actors?: string[]
  actor_options?: OrderActionLogActorOption[]
  actions?: OrderActionLogActionOption[]
}

const rows = ref<OrderActionLogRow[]>([])
const summaryRows = ref<OrderActionLogSummaryRow[]>([])
const summaryPage = ref(1)
const SUMMARY_PAGE_SIZE = 10
const summaryPageCount = computed(() => Math.max(1, Math.ceil(summaryRows.value.length / SUMMARY_PAGE_SIZE)))
const visibleSummaryRows = computed(() => summaryRows.value.slice((summaryPage.value - 1) * SUMMARY_PAGE_SIZE, summaryPage.value * SUMMARY_PAGE_SIZE))
watch(summaryPageCount, (count) => { if (summaryPage.value > count) summaryPage.value = count })
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(50)

/** 排序状态：undefined = 没点过（不发参数，后端默认最新在前）；点表头两态切换。 */
const sortOrdering = ref<OrderActionLogOrdering | undefined>(undefined)

let reloadId = 0
async function reload(targetPage = 1): Promise<void> {
  const requestId = ++reloadId
  loading.value = true
  try {
    const params: OrderActionLogSearchParams = {
      stime: filters.sDate,
      etime: filters.eDate,
      action: filters.action,
      actor: filters.actor,
      key1: filters.key1,
      ordering: sortOrdering.value,
      page: targetPage,
      page_size: pageSize.value,
    }
    const data = await request<ActionLogData>({
      method: 'get',
      url: ORDER_ACTION_LOGS_URL,
      params: toOrderActionLogParams(params),
    })
    if (requestId !== reloadId) return
    rows.value = data.items ?? []
    total.value = data.total ?? 0
    page.value = data.page ?? targetPage
    summaryRows.value = data.summary ?? []
    // 候选随时刷新（el-select 换 options 不动已选值，v1「只灌一次」的顾虑在这里不存在）。
    if (Array.isArray(data.actor_options) && data.actor_options.length > 0) {
      actorOptions.value = data.actor_options
    } else if (Array.isArray(data.actors) && data.actors.length > 0) {
      // 旧后端只回 actors（部署窗口）：文本 = 值 = 账号兜底，页面不因少一键变空。
      actorOptions.value = data.actors.map((a) => ({ actor: a, display: a }))
    }
    if (Array.isArray(data.actions) && data.actions.length > 0) actionOptions.value = data.actions
    loadError.value = null
  } catch (e) {
    if (requestId !== reloadId) return
    rows.value = []
    summaryRows.value = []
    total.value = 0
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    if (requestId === reloadId) loading.value = false
  }
}

/** 行数变化后当前页可能越界（服务端翻页不会自己夹）。 */
watch(total, (t) => {
  const last = Math.max(1, Math.ceil(t / pageSize.value))
  if (page.value > last) void reload(last)
})

function onPageChange(p: number): void {
  void reload(p)
}

function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    void reload(1)
  }
}

const searchedTimeline = ref<OrderTimeline | null>(null)
const timelineLoading = ref(false)
const timelineError = ref('')
let timelineLookupId = 0

async function loadTimeline(orderKey: string): Promise<void> {
  const lookupId = ++timelineLookupId
  const key = orderKey.trim()
  searchedTimeline.value = null
  timelineError.value = ''
  timelineLoading.value = key !== '' && canView.value
  if (!timelineLoading.value) return

  try {
    const timeline = await fetchOrderTimeline(key)
    if (lookupId === timelineLookupId) searchedTimeline.value = timeline
  } catch (error) {
    if (lookupId !== timelineLookupId) return
    timelineError.value = error instanceof BizError && error.code === 1002
      ? '没有找到订单时间线，请核对完整单号。'
      : '订单时间线查询失败，请重试。'
  } finally {
    if (lookupId === timelineLookupId) timelineLoading.value = false
  }
}

function onSearch(): void {
  summaryPage.value = 1
  void reload(1)
  void loadTimeline(filters.key1)
}

function refresh(): void {
  void reload(page.value)
  void loadTimeline(filters.key1)
}

function setDefaultFilters(orderNo = ''): void {
  summaryPage.value = 1
  filters.sDate = orderNo === '' ? defaultFrom : ''
  filters.eDate = orderNo === '' ? defaultTo : ''
  filters.action = ''
  filters.actor = ''
  filters.key1 = orderNo
  sortOrdering.value = undefined
}

function resetFilters(): void {
  setDefaultFilters()
  void reload(1)
  void loadTimeline('')
}

/** 点「操作时间」表头：初态 → 最新在前（-created_at），再点 → 最早在前，如此往复。 */
function onSortToggle(): void {
  sortOrdering.value = sortOrdering.value === 'created_at' ? '-created_at' : 'created_at'
  void reload(1)
}

const sortCaret = computed(() => (sortOrdering.value === 'created_at' ? '↑' : '↓'))

/* ── 单元格展示（v1 toLogRow / formatChange 同口径重写）───────────────────────── */

/** 「变更内容」= `from → to`；只一侧有值只显示那一侧（不渲染悬空箭头），两侧皆空「—」。 */
function changeText(from: string, to: string): string {
  const before = from.trim()
  const after = to.trim()
  if (before === '' && after === '') return '—'
  if (before === '') return after
  if (after === '') return before
  return `${before} → ${after}`
}

function actorNameOf(row: OrderActionLogRow): string {
  return row.actor_display || row.actor || ''
}

function timeText(iso: string): string {
  return formatCreatedAt(iso)
}

/* ── 订单详情弹窗（xl 原生双栏：fetchOrder + fetchOrderAudit；配方照 V2PlatformOrders）─ */

const detailDlg = ref<null | { orderNo: string; viewNo: string; gameName: string }>(null)
const detailOrder = ref<Order | null>(null)
const detailLogs = ref<AuditEntry[]>([])
const detailLogsLoading = ref(false)
const detailTab = ref<'note' | 'action'>('note')
const noteText = ref('')
const noteAdding = ref(false)
const canAddLog = computed(() => session.canNode(['order.add_log']))

function logTime(iso: string): string {
  const d = new Date(iso)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function openDetail(row: OrderActionLogRow): Promise<void> {
  const orderNo = row.order_no ?? ''
  // 开窗前的单号形状闸（20 位纯数字；⛔ 不把不可信输入拼进提示）。
  if (!isOrderNoShape(orderNo)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  detailDlg.value = { orderNo, viewNo: orderNo, gameName: row.game_name ?? '' }
  detailOrder.value = null
  detailLogs.value = []
  detailTab.value = 'note'
  noteText.value = ''
  detailLogsLoading.value = true
  try {
    const [order, logs] = await Promise.all([fetchOrder(orderNo), fetchOrderAudit(orderNo)])
    // 行可能是旧一拍的快照：弹窗开着就以最新请求回填（比对单号防串）。
    if (detailDlg.value !== null && detailDlg.value.orderNo === orderNo) {
      detailOrder.value = order
      detailLogs.value = logs
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    detailLogsLoading.value = false
  }
}

/** 添加订单日志（权限门 order.add_log；空内容对标同句拦截，零请求）。 */
async function onAddNote(): Promise<void> {
  const dlg = detailDlg.value
  if (dlg === null) return
  if (noteText.value.trim() === '') {
    showToast('备注内容不能为空！', 'error')
    return
  }
  noteAdding.value = true
  try {
    const row = await createOrderNote(dlg.orderNo, noteText.value)
    detailLogs.value = [row, ...detailLogs.value]
    noteText.value = ''
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    noteAdding.value = false
  }
}

/* ── 挂载 / 卸载 ─────────────────────────────────────────────────────────────── */

watch([linkedOrderNo, canView], ([orderNo, allowed]) => {
  // 从时间线进来时也能查到早于默认日期区间的操作。
  setDefaultFilters(orderNo)
  if (allowed) void reload(1)
  void loadTimeline(orderNo)
}, { immediate: true })

onUnmounted(() => {
  timelineLookupId += 1
  if (toastTimer !== null) {
    clearTimeout(toastTimer)
    toastTimer = null
  }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-action-log">
    <!-- 权限门：fail-closed，会话取不到节点就只画这一句（V2OrderAudit 先例） -->
    <div v-if="!canView" class="cv2-empty" data-v2="no-permission">
      你的账号没有「交易订单操作记录」权限，看不了这页数据；需要的话找管理员开通。
    </div>

    <template v-else>
      <!-- 筛选区：操作时间区间 / 操作动作 / 操作人员 / 关键词 + 查询/重置（连体网格照同族页） -->
      <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
        <div class="flt-wrap">
          <div class="flt-group" data-v2-filter-group="time">
            <span class="flt-label">操作时间</span>
            <span class="flt-cell flt-datecell" data-v2-filter="stime"><el-date-picker
              :model-value="filters.sDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="开始日期"
              :teleported="false"
              @update:model-value="setSDate"
            /></span>
            <span class="flt-dash">–</span>
            <span class="flt-cell flt-datecell" data-v2-filter="etime"><el-date-picker
              :model-value="filters.eDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="结束日期"
              :teleported="false"
              @update:model-value="setEDate"
            /></span>
          </div>
          <div class="flt-group" data-v2-filter-group="action">
            <span class="flt-label">操作动作</span>
            <span class="flt-cell"><el-select v-model="filters.action" filterable placeholder="全部" class="flt-w150" data-v2-filter="action" :teleported="false">
              <el-option value="" label="全部" />
              <el-option v-for="a in actionOptions" :key="a.code" :value="a.code" :label="a.label" />
            </el-select></span>
          </div>
          <div class="flt-group" data-v2-filter-group="actor">
            <span class="flt-label">操作人员</span>
            <span class="flt-cell"><el-select v-model="filters.actor" filterable placeholder="全部" class="flt-w150" data-v2-filter="actor" :teleported="false">
              <el-option value="" label="全部" />
              <el-option v-for="o in actorOptions" :key="o.actor" :value="o.actor" :label="o.display" />
            </el-select></span>
          </div>
          <div class="flt-group flt-grow" data-v2-filter-group="keyword">
            <span class="flt-cell flt-cell--grow"><el-input
              v-model="filters.key1"
              placeholder="系统单号或交易单号"
              data-v2-filter="key1"
              @keydown.enter="onSearch"
            /></span>
          </div>
          <div class="cv2-filter-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
            <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
          </div>
        </div>
      </section>

      <p v-if="timelineLoading" class="timeline-feedback" role="status" data-v2="timeline-loading">正在查询订单时间线…</p>
      <p v-else-if="timelineError" class="timeline-feedback" role="status" data-v2="timeline-error">{{ timelineError }}</p>
      <details v-else-if="searchedTimeline" class="cv2-panel timeline-panel" data-v2="searched-order-timeline" open>
        <summary>订单时间线</summary>
        <V2OrderTimeline :timeline="searchedTimeline" />
      </details>

      <!-- 工具条：刷新（v1 工具栏唯一按钮「刷新数据」的 v2 同义钮） -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <div class="ttb-strip">
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="refresh">刷新</button>
        </div>
      </div>

      <!-- 统计句：服务端 total（v1 无总句，v2 新增）；橙色档同 V2OrderRecycle -->
      <p class="cv2-total-line" data-v2="total">本次搜索统计共{{ total }}条操作记录</p>

      <!-- 明细和统计各自分页。 -->
      <div class="dual-wrap">
        <section class="cv2-panel cv2-table-panel dual-main" aria-label="操作明细">
          <table class="cv2-table">
            <thead>
              <tr>
                <th>系统单号</th>
                <th>交易单号</th>
                <th class="cv2-th-center">游戏名称</th>
                <th class="cv2-th-center">变更内容</th>
                <th class="cv2-th-center">操作说明</th>
                <th class="cv2-th-center">操作人员</th>
                <th class="cv2-th-center">
                  <button type="button" class="th-sort" data-v2="sort-time" title="点击切换操作时间正/逆序（服务端排序）" @click="onSortToggle">
                    操作时间<span class="th-caret" aria-hidden="true">{{ sortCaret }}</span>
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in rows" :key="`${row.order_no}:${row.created_at}:${idx}`" class="cv2-row" :data-v2-row="row.order_no">
                <td class="cv2-td-order cut-sysno">
                  <a href="#" class="cv2-order-link cv2-ono" :data-v2-detail="row.order_no" :title="`${row.order_no} · 点击查看订单详情`" @click.prevent="openDetail(row)">{{ row.order_no }}</a>
                </td>
                <td class="cut-tid" :title="row.source_order_no"><a v-if="row.source_order_no" href="#" class="cv2-order-link cv2-ono" :data-v2-source-detail="row.order_no" @click.prevent="openDetail(row)">{{ row.source_order_no }}</a><span v-else>—</span></td>
                <td class="cv2-td-center">{{ row.game_name }}</td>
                <td class="cv2-td-center cut-change" :title="changeText(row.from_value, row.to_value)">{{ changeText(row.from_value, row.to_value) }}</td>
                <td class="cv2-td-center">{{ row.action_label }}</td>
                <td class="cv2-td-center">{{ actorNameOf(row) }}</td>
                <td class="cv2-td-time">{{ timeText(row.created_at) }}</td>
              </tr>
              <tr v-if="rows.length === 0">
                <td colspan="7" class="cv2-empty" data-v2="empty">
                  <template v-if="loading">正在加载…</template>
                  <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
                  <template v-else>暂无数据</template>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="cv2-pager" data-v2="pager">
            <span class="cv2-pager-total" data-v2="pager-total">共 {{ total }} 条</span>
            <label class="cv2-pager-size">
              每页
              <select class="cv2-input cv2-input--inline" data-v2="page-size" :value="pageSize" @change="onPageSizeChange">
                <option v-for="n in PAGE_LIMITS" :key="n" :value="n">{{ n }}</option>
              </select>
              条
            </label>
            <el-pagination
              v-model:current-page="page"
              :page-size="pageSize"
              :total="total"
              layout="prev, pager, next"
              background
              @current-change="onPageChange"
            />
          </div>
        </section>

        <!-- 统计取筛选全集，不受明细页码影响。 -->
        <section class="cv2-panel cv2-table-panel dual-side" aria-label="操作统计">
          <p class="side-title" data-v2="side-title">操作统计（随筛选全集）</p>
          <table class="cv2-table">
            <colgroup>
              <col style="width: 110px" />
              <col style="width: 130px" />
              <col style="width: 84px" />
            </colgroup>
            <thead>
              <tr>
                <th class="cv2-th-center">操作</th>
                <th class="cv2-th-center">操作人</th>
                <th class="cv2-th-center">次数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in visibleSummaryRows" :key="`${s.action}:${s.actor}`" class="cv2-row" :data-v2-summary="`${s.action}:${s.actor}`">
                <td class="cv2-td-center">{{ s.action_label }}</td>
                <td class="cv2-td-center">{{ s.actor_display || s.actor }}</td>
                <td class="cv2-td-center cv2-td-num cv2-num">{{ s.count }}</td>
              </tr>
              <tr v-if="summaryRows.length === 0">
                <td colspan="3" class="cv2-empty" data-v2="summary-empty">
                  <template v-if="loading">正在加载…</template>
                  <template v-else>暂无数据</template>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="cv2-pager summary-pager" data-v2="summary-pager">
            <span>共 {{ summaryRows.length }} 条</span>
            <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--sm" :disabled="summaryPage <= 1" @click="summaryPage--">上一页</button>
            <span>{{ summaryPage }} / {{ summaryPageCount }}</span>
            <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--sm" :disabled="summaryPage >= summaryPageCount" @click="summaryPage++">下一页</button>
          </div>
        </section>
      </div>
    </template>

    <!-- 订单详情：xl 原生双栏（左=订单字段区+内容，右=订单/操作日志+添加框）；配方照 V2PlatformOrders -->
    <OrderDetailDialog
      :model-value="detailDlg !== null"
      :title="detailDlg === null ? '' : `${detailDlg.viewNo}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      destroy-on-close
      data-v2="dlg-detail"
      @update:model-value="detailDlg = null"
    >
      <V2DetailFace v-if="detailDlg !== null" :order-no="detailDlg.orderNo" :source-order-no="detailOrder?.source_order_no" :has-dailiantong-history="detailOrder?.has_dailiantong_history">
      <div class="dtl-wrap">
        <div class="dtl-left">
          <div class="dtl-fields">
            <div class="dtl-row"><span class="dtl-label">系统单号</span><span class="cv2-num dtl-strong">{{ detailDlg.orderNo }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.gameName === '' }"><span class="dtl-label">游戏名称</span><span>{{ detailDlg.gameName }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_role_name }"><span class="dtl-label">角色名</span><span>{{ detailOrder?.game_role_name }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': !detailOrder?.project_text }"><span class="dtl-label">代练项目</span><span>{{ detailOrder?.project_text }}</span></div>
            <div class="dtl-row"><span class="dtl-label">订单状态</span>
              <span v-if="detailOrder !== null" class="cv2-st cv2-order-status cv2-st--static cv2-st--mark" :class="orderStatusClass(detailOrder.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ v2OrderStatusLabel(detailOrder.status) }}</span>
              <span v-else>—</span>
            </div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.time_requirement }"><span class="dtl-label">时间要求</span><span>{{ detailOrder?.time_requirement }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_account }"><span class="dtl-label">游戏账号</span><span>{{ detailOrder?.game_account }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_sub_account }"><span class="dtl-label">子账号</span><span>{{ detailOrder?.game_sub_account }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.customer_phone }"><span class="dtl-label">联系电话</span><span>{{ detailOrder?.customer_phone }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.customer_ww }"><span class="dtl-label">旺旺/来源</span><span>{{ detailOrder?.customer_ww }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.sales_agent }"><span class="dtl-label">销售客服</span><span>{{ detailOrder?.sales_agent }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailOrder === null || detailOrder.hall_price === null }"><span class="dtl-label">发单价</span><span class="cv2-num">{{ detailOrder?.hall_price }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailOrder === null }"><span class="dtl-label">接单价</span><span class="cv2-money">{{ detailOrder?.total_price }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailOrder === null }"><span class="dtl-label">退款</span><span class="cv2-num">{{ detailOrder?.refund_fee }}</span></div>
            <div class="dtl-row"><span class="dtl-label">录单时间</span><span class="cv2-num">{{ detailOrder?.created_at ? detailOrder.created_at.replace('T', ' ').slice(0, 16) : '—' }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': !detailOrder?.remark }"><span class="dtl-label">重要备注</span><span>{{ detailOrder?.remark }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': !detailOrder?.content }"><span class="dtl-label">订单内容</span><span>{{ detailOrder?.content }}</span></div>
          </div>
        </div>
        <div class="dtl-right">
          <div class="dtl-tabs">
            <button type="button" class="dtl-tab" :class="{ 'dtl-tab--on': detailTab === 'note' }" data-v2="tab-note" @click="detailTab = 'note'">订单日志</button>
            <button type="button" class="dtl-tab" :class="{ 'dtl-tab--on': detailTab === 'action' }" data-v2="tab-action" @click="detailTab = 'action'">操作日志</button>
          </div>
          <div v-if="canAddLog" class="dtl-add">
            <el-input
              v-model="noteText"
              :maxlength="255"
              :placeholder="detailTab === 'note' ? '你可以在这里输入订单日志内容' : '当前在操作日志页签，切回订单日志可添加'"
              :disabled="detailTab !== 'note'"
              data-v2="note-input"
              @keydown.enter="void onAddNote()"
            />
            <button v-if="detailTab === 'note'" type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" data-v2="note-add" :disabled="noteAdding" @click="void onAddNote()">添加</button>
          </div>
          <div class="dtl-logs" data-v2="detail-logs">
            <p v-if="detailLogsLoading" class="dtl-logs-empty">正在加载日志…</p>
            <template v-else>
              <!-- summary 服务端已拼好整句，前端只补时间头 -->
              <p v-for="logRow in detailLogs" :key="logRow.id" class="dtl-log">
                <span class="dtl-log-time">【{{ logTime(logRow.created_at) }}】</span>{{ logRow.summary }}
              </p>
              <p v-if="detailLogs.length === 0" class="dtl-logs-empty">暂无日志记录</p>
            </template>
          </div>
        </div>
      </div>
      </V2DetailFace>
    </OrderDetailDialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
.timeline-feedback {
  margin: 12px 0;
  color: var(--tt2-dim);
}

.timeline-panel {
  margin: 12px 0;
  padding: 12px 16px 16px;
}

.timeline-panel summary {
  color: var(--tt2-ink);
  font-weight: 700;
  cursor: pointer;
}

/* 本页形状收在这里（carbon.css 零命中的三条纪律件各自重声明；色只走 var(--tt2-*)）。 */

.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 筛选区连体网格组（照 V2OrderRecycle 同名规则重声明；组框接管边界，EP 控件去描边）── */
/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、面板内边距 10/12），不压组高。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.flt-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
}

.flt-group {
  display: inline-flex;
  align-items: stretch;
  height: 32px;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-panel);
  transition: border-color 0.15s;
}

/* 不用 overflow:hidden 裁圆角——它会把下拉展开面板一起裁掉；首尾单元格自带圆角。 */
.flt-group > :first-child {
  border-radius: 5px 0 0 5px;
}

.flt-group > :last-child {
  border-radius: 0 5px 5px 0;
}

.flt-group:focus-within {
  border-color: var(--tt2-primary);
}

.flt-label {
  display: inline-flex;
  align-items: center;
  padding: 0 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
  white-space: nowrap;
}

.flt-cell {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.flt-cell + .flt-cell,
.flt-cell + .flt-dash,
.flt-dash + .flt-cell {
  border-left: none;
}

.flt-cell--grow {
  flex: 1;
}

.flt-grow {
  flex: 1 1 240px;
}

.flt-grow .flt-cell--grow {
  min-width: 230px;
}

/* 操作时间格：格子定宽、控件撑满（10 位日期 13px 才放得下，同管理订单页实测值）。 */
.flt-group .flt-cell.flt-datecell {
  width: 118px;
}

.flt-group .flt-cell.flt-datecell :deep(.el-date-editor.el-input) {
  width: 100%;
}

.flt-group .flt-cell.flt-datecell :deep(.el-input__inner) {
  font-size: 13px;
}

.flt-dash {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px（34 − 上下边线）。 */
.flt-group :deep(.el-input__wrapper),
.flt-group :deep(.el-select__wrapper) {
  height: 32px;
  min-height: 32px;
  border-radius: 0;
  background: var(--tt2-panel);
  box-shadow: none;
}

.flt-group :deep(.el-select__wrapper.is-focused),
.flt-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}

.flt-w150 { width: 150px; }

/* 统计句 = 原版橙（carbon.css 基类是红，本页覆盖，同 V2OrderRecycle）。 */
.cv2-total-line {
  color: var(--tt2-summary-orange);
}

/* 窄屏上下排列，宽屏两栏等高。 */
.dual-wrap {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(300px, 30%);
  gap: 12px;
  align-items: stretch;
}

.dual-main {
  min-width: 0;
  overflow-x: auto;
}

.dual-side {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.summary-pager { margin-top: auto; }
@media (max-width: 900px) { .dual-wrap { grid-template-columns: minmax(0, 1fr); } }

.side-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--tt2-ink);
}

/* 「操作时间」排序钮（表头内裸钮，继承表头字色；箭头随状态）。 */
.th-sort {
  padding: 0;
  border: none;
  background: transparent;
  font: inherit;
  color: inherit;
  cursor: pointer;
}

.th-caret {
  margin-left: 4px;
  font-size: 11px;
  color: var(--tt2-dim);
}

/* 左表容纳较多列，窄时允许横向滚动。 */
.cv2-table-panel {
  width: 100%;
  max-width: 100%;
  min-height: 320px;
}

.cv2-table {
  width: auto;
  table-layout: auto;
}

/* min-width:0 防共享层/后续样式给列下限，把省略档顶穿。 */
.cut-sysno,
.cut-tid,
.cut-change {
  min-width: 0;
}

.cut-sysno { max-width: 150px; }
.cut-tid { max-width: 150px; }
.cut-change { max-width: 170px; }

/* ── 操作列 sticky 右钉（纪律件：carbon.css 零命中，本页重声明；当前无操作列、留作骨架件）── */
.cv2-th-fixed,
.cv2-td-fixed {
  position: sticky;
  right: 0;
  background: var(--tt2-panel);
  border-left: 1px solid var(--tt2-line);
}

.cv2-th-fixed {
  z-index: 3;
}

.cv2-td-fixed {
  z-index: 2;
}

tbody tr:hover .cv2-td-fixed {
  background: var(--tt2-panel);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-row--checked .cv2-td-fixed {
  background: var(--tt2-row-checked);
}

/* ── 订单详情原生双栏（照 V2PlatformOrders 同名规则重声明）────────────────────── */
.dtl-wrap {
  display: flex;
  align-items: stretch;
  /* 详情内容高 = 弹窗上限(内容区−48) − 弹窗头 ~56。 */
  height: calc(100vh - var(--tt2-topbar-h) - var(--tt2-tabs-h) - 152px);
  min-height: 380px;
}

.dtl-left {
  flex: 1 1 56%;
  min-width: 0;
  overflow-y: auto;
  padding: 16px 20px 20px;
}

.dtl-fields {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.dtl-row {
  display: flex;
  gap: 14px;
  align-items: baseline;
}

.dtl-label {
  flex: 0 0 76px;
  text-align: right;
  font-size: 13px;
  color: var(--tt2-dim);
  white-space: nowrap;
}

.dtl-row > :last-child {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.dtl-row--wide {
  flex-direction: column;
  gap: 4px;
}

.dtl-row--wide .dtl-label {
  flex: none;
  text-align: left;
}

.dtl-row--wide > :last-child {
  max-height: 180px;
  overflow-y: auto;
  padding: 6px 10px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
}

.dtl-strong {
  font-weight: 600;
}

.dtl-content {
  margin-top: 14px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
}

.dtl-content-title {
  margin: 0;
  padding: 8px 12px;
  border-bottom: 1px solid var(--tt2-line);
  background: var(--tt2-label-bg);
  font-size: 13px;
  font-weight: 600;
  color: var(--tt2-ink);
}

.dtl-content-body {
  margin: 0;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.dtl-right {
  flex: 1 1 44%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--tt2-line);
  padding: 14px 18px;
}

.dtl-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--tt2-line);
}

.dtl-tab {
  padding: 7px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  font: inherit;
  font-size: 13px;
  color: var(--tt2-dim);
  cursor: pointer;
}

.dtl-tab--on {
  color: var(--tt2-primary-deep);
  font-weight: 600;
  border-bottom-color: var(--tt2-primary);
}

.dtl-add {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 12px;
}

.dtl-add :deep(.el-input) {
  flex: 1;
}

.dtl-logs {
  flex: 1;
  overflow-y: auto;
  margin-top: 12px;
}

.dtl-log {
  margin: 0 0 9px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--tt2-dim);
  overflow-wrap: anywhere;
}

.dtl-log-time {
  color: var(--tt2-dim);
}

.dtl-logs-empty {
  margin: 16px 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 详情里的状态签是纯展示（不可点）。 */
.cv2-st--static {
  cursor: default;
}
</style>
