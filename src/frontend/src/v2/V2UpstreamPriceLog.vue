<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
/**
 * 「上家订单改价记录」v2（`/v2` 线；v1 = island `order-soureorderchangepricelog.ts`）。
 *
 * 数据面 = `GET /api/business/upstream-price-change-logs/`（共享模块没有 ⇒ 本页内声明
 * URL + 行类型 + 参数组装；传输/信封/错误分类复用 `@/api/http::request`；排序写死在后端
 * `-occurred_at,-id`，前端不传排序参数）。布局照 `V2OrderManage.vue` 骨架，单表 8 列。
 * 与 v1 的偏离（均有意）：① v1 首列 checkbox 无批量动作，v2 整列去掉；
 * ② 「变动金额」v1 恒橙，v2 按 +绿/−红/0 灰落 token 档；金额空值显示「—」，⛔ 不造 0.00。
 * 「说明」列照 v1 = `note`（v1 行映射 str5 ← note，`source_name` 在 v1 不渲染，照抄）。
 * 列表横向铺满，超长内容可悬停查看；默认每页 50 条。
 * 权限门 fail-closed：`session.canNode('order.upstream_price_change_logs')`（V2OrderAudit 先例）。
 * ⛔ 不 import `layui-island/**`；色只走 `--tt2-*` token。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { request } from '@/api/http'
import type { Page } from '@/api/unwrap'
import { formatCreatedAt } from '@/api/auditLogs'
import { formatMoney } from '@/config/money'
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

const UPSTREAM_PRICE_CHANGE_LOGS_URL = '/api/business/upstream-price-change-logs/'
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'
// 2026-11-06 统一布局口径：默认 50、档位 [20,50,100]。
const PAGE_LIMITS = [20, 50, 100]

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 契约类型（后端一行的恰好 10 键；共享模块没有 ⇒ 本页唯一声明处）────────────── */

type UpstreamPriceChangeLogRow = {
  order_no: string
  /** 实时回查 `Order` 的来源单号；订单查不到 ⇒ null（v1 D-0301 同键）。 */
  source_order_no: string | null
  game_name: string
  game_role_name: string
  /** 十进制字符串或 null（首次从「无价」变有价时改前为 null）。 */
  before_amount: string | null
  after_amount: string | null
  source_name: string
  note: string
  occurred_at: string
  /** 该单当前是否已被软删（读时回查）——「删」徽标的判据。 */
  is_deleted: boolean
}

/** 端点接受的 3 个筛选（stime/etime `yyyy-MM-dd`，etime 含当天；key 订单号模糊、同时命中来源单号）。 */
type PriceLogSearchParams = {
  stime?: string
  etime?: string
  key?: string
  page?: number
  page_size?: number
}

/** 组装查询参数：空值 / 空串不发（判据是「空」不是真值，E-0021 口径）。 */
function toPriceLogParams(params: PriceLogSearchParams): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.stime !== undefined && params.stime.trim() !== '') out.stime = params.stime.trim()
  if (params.etime !== undefined && params.etime.trim() !== '') out.etime = params.etime.trim()
  const key = params.key?.trim() ?? ''
  if (key !== '') out.key = key
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  return out
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
const canView = computed(() => session.canNode(['order.upstream_price_change_logs']))

/* ── 筛选（v1 语义：时间区间默认今天 −6 自然月 ~ 今天）───────────────────────── */

type PriceLogFilters = {
  sDate: string
  eDate: string
  key: string
}

function toDateInputValue(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 默认区间 = 今天 −6 个自然月 ~ 今天（v1 同口径；日夹到目标月月末防 1/31 溢出）。 */
function defaultDateRange(now: Date = new Date()): [string, string] {
  const target = new Date(now.getFullYear(), now.getMonth() - 6, 1)
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()
  target.setDate(Math.min(now.getDate(), lastDay))
  return [toDateInputValue(target), toDateInputValue(now)]
}

const [defaultFrom, defaultTo] = defaultDateRange()

const filters = reactive<PriceLogFilters>({
  sDate: defaultFrom,
  eDate: defaultTo,
  key: '',
})

function setSDate(v: unknown): void {
  filters.sDate = typeof v === 'string' ? v : ''
}

function setEDate(v: unknown): void {
  filters.eDate = typeof v === 'string' ? v : ''
}

/* ── 取数（服务端分页；统计句 = 服务端 total）────────────────────────────────── */

const rows = ref<UpstreamPriceChangeLogRow[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(50)

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const data = await request<Page<UpstreamPriceChangeLogRow>>({
      method: 'get',
      url: UPSTREAM_PRICE_CHANGE_LOGS_URL,
      params: toPriceLogParams({
        stime: filters.sDate,
        etime: filters.eDate,
        key: filters.key,
        page: targetPage,
        page_size: pageSize.value,
      }),
    })
    rows.value = data.items ?? []
    total.value = data.total ?? 0
    page.value = data.page ?? targetPage
    loadError.value = null
  } catch (e) {
    rows.value = []
    total.value = 0
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
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

function onSearch(): void {
  void reload(1)
}

function resetFilters(): void {
  filters.sDate = defaultFrom
  filters.eDate = defaultTo
  filters.key = ''
  void reload(1)
}

/* ── 单元格展示（v1 各 templet 同口径重写）───────────────────────────────────── */

const EMPTY_VALUE = '—'

/** 展示单号 = 来源号非空取它、空回退内置号（v1 D-0301）；寻址键恒为内置号。 */
function viewNoOf(row: UpstreamPriceChangeLogRow): string {
  return row.source_order_no || row.order_no
}

function moneyText(v: string | null): string {
  return formatMoney(v)
}

/** 金额字符串 → 整数分（非两位小数以内 ⇒ null）；用分算差值避开浮点尾数。 */
function moneyToCents(value: string | null | undefined): number | null {
  const text = (value ?? '').trim()
  if (!/^-?\d+(\.\d{1,2})?$/.test(text)) return null
  const negative = text.startsWith('-')
  const digits = negative ? text.slice(1) : text
  const dot = digits.indexOf('.')
  const yuan = dot === -1 ? digits : digits.slice(0, dot)
  const frac = dot === -1 ? '' : digits.slice(dot + 1)
  const cents = Number(yuan) * 100 + Number((frac + '00').slice(0, 2))
  if (!Number.isFinite(cents)) return null
  return negative ? -cents : cents
}

function centsToMoney(cents: number): string {
  const abs = Math.abs(Math.round(cents))
  return `${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** 「变动金额」：+差额 绿 / −差额 红 / 0 灰 / 任一侧空「—」。符号由比较结果决定。 */
function deltaView(before: string | null, after: string | null): { text: string; cls: string } {
  const b = moneyToCents(before)
  const a = moneyToCents(after)
  if (b === null || a === null) return { text: EMPTY_VALUE, cls: 'pl-delta--zero' }
  const diff = a - b
  if (diff > 0) return { text: `+${centsToMoney(diff)}`, cls: 'pl-delta--up' }
  if (diff < 0) return { text: `-${centsToMoney(diff)}`, cls: 'pl-delta--down' }
  return { text: '0', cls: 'pl-delta--zero' }
}

function timeText(iso: string): string {
  return formatCreatedAt(iso).slice(0, 16)
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

/** 已删单「删」徽标 + 纯文本不可点（v1 同判据 is_deleted）；正常单蓝链开详情。 */
async function openDetail(row: UpstreamPriceChangeLogRow): Promise<void> {
  const orderNo = row.order_no ?? ''
  // 开窗前的单号形状闸（20 位纯数字；⛔ 不把不可信输入拼进提示）。
  if (!isOrderNoShape(orderNo)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  detailDlg.value = { orderNo, viewNo: viewNoOf(row), gameName: row.game_name ?? '' }
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

onMounted(() => {
  if (canView.value) void reload(1)
})

onUnmounted(() => {
  if (toastTimer !== null) {
    clearTimeout(toastTimer)
    toastTimer = null
  }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="upstream-price-log">
    <!-- 权限门：fail-closed，会话取不到节点就只画这一句（V2OrderAudit 先例） -->
    <div v-if="!canView" class="cv2-empty" data-v2="no-permission">
      你的账号没有「上家订单改价记录」权限，看不了这页数据；需要的话找管理员开通。
    </div>

    <template v-else>
      <!-- 筛选区：记录日期区间（默认 −6 自然月）+ 关键词 + 查询/重置（连体网格照同族页） -->
      <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
        <div class="flt-wrap">
          <div class="flt-group" data-v2-filter-group="time">
            <span class="flt-label">记录日期</span>
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
          <div class="flt-group flt-grow" data-v2-filter-group="keyword">
            <span class="flt-cell flt-cell--grow"><el-input
              v-model="filters.key"
              placeholder="订单号 / 来源单号"
              data-v2-filter="key"
              @keydown.enter="onSearch"
            /></span>
          </div>
          <div class="cv2-filter-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
            <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
          </div>
        </div>
      </section>

      <div class="cv2-toolbar" data-v2="toolbar">
        <div class="ttb-strip">
          <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="reload(page)">刷新</button>
        </div>
        <p class="cv2-total-line" data-v2="total">本次搜索统计共{{ total }}条记录</p>
      </div>

      <!-- 列表：8 列（v1 9 列去掉无批量动作的勾选列）；排序写死在后端 -occurred_at,-id；
           列宽随内容自适应（统一布局口径见脚本头），超长列 cut-* 省略 -->
      <section class="cv2-panel cv2-table-panel" aria-label="上家订单改价记录列表">
        <table class="cv2-table">
          <thead>
            <tr>
              <th>订单号</th>
              <th class="cv2-th-center">游戏名称</th>
              <th>角色名</th>
              <th class="cv2-th-center">金额变动</th>
              <th class="cv2-th-center">本次金额</th>
              <th class="cv2-th-center">变动金额</th>
              <th class="cv2-th-center">说明</th>
              <th class="cv2-th-center">变动时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in rows" :key="`${row.order_no}:${row.occurred_at}:${idx}`" class="cv2-row" :data-v2-row="row.order_no">
              <td class="cv2-td-order cut-order">
                <!-- 已删单 = 红「删」徽标 + 纯文本不可点（v1 同判据 is_deleted）；正常单蓝链开详情 -->
                <template v-if="row.is_deleted">
                  <span class="pl-del" title="该订单已被删除了">删</span><span :title="viewNoOf(row)" :data-v2-view-no="viewNoOf(row)">{{ viewNoOf(row) }}</span>
                </template>
                <a
                  v-else
                  href="#"
                  class="cv2-order-link cv2-ono"
                  :data-v2-detail="row.order_no"
                  :title="`${viewNoOf(row)} · 点击查看订单详情`"
                  @click.prevent="openDetail(row)"
                >{{ viewNoOf(row) }}</a>
              </td>
              <td class="cv2-td-center">{{ row.game_name }}</td>
              <td>{{ row.game_role_name }}</td>
              <td class="cv2-td-center cut-change" :title="`从${moneyText(row.before_amount)}元变成${moneyText(row.after_amount)}元`">
                {{ moneyText(row.before_amount) }}&gt;&gt;{{ moneyText(row.after_amount) }}
              </td>
              <td class="cv2-td-center cv2-td-num pl-current">{{ moneyText(row.after_amount) }}</td>
              <td class="cv2-td-center cv2-td-num">
                <span :class="deltaView(row.before_amount, row.after_amount).cls">{{ deltaView(row.before_amount, row.after_amount).text }}</span>
              </td>
              <!-- 说明照 v1 = note（source_name 在 v1 不渲染，照抄） -->
              <td class="cv2-td-center cut-note" :title="row.note">{{ row.note }}</td>
              <td class="cv2-td-time">{{ timeText(row.occurred_at) }}</td>
            </tr>
            <tr v-if="rows.length === 0">
              <td colspan="8" class="cv2-empty" data-v2="empty">
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
            <div class="dtl-row"><span class="dtl-label">订单号</span><span class="cv2-num dtl-strong">{{ detailDlg.viewNo }}</span></div>
            <div class="dtl-row"><span class="dtl-label">系统单号</span><span class="cv2-num dtl-dim">{{ detailDlg.orderNo }}</span></div>
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

/* 记录日期格：格子定宽、控件撑满（10 位日期 13px 才放得下，同管理订单页实测值）。 */
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

/* 统计句 = 原版橙（carbon.css 基类是红，本页覆盖，同 V2OrderRecycle）。 */
.cv2-total-line {
  color: var(--tt2-summary-orange);
  margin: 0;
}

.cv2-table-panel {
  width: 100%;
  max-width: 100%;
  min-height: 320px;
  overflow-x: auto;
}

.cv2-table {
  width: 100%;
  min-width: 760px;
  table-layout: auto;
}

/* min-width:0 防共享层/后续样式给列下限，把省略档顶穿。 */
.cut-order,
.cut-change,
.cut-note {
  min-width: 0;
}

.cut-order { max-width: 150px; }
.cut-change { max-width: 140px; }
.cut-note { max-width: 130px; }

/* ── 操作列 sticky 右钉（纪律件：carbon.css 零命中，本页重声明；本页无操作列、留作骨架件）── */
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

/* ── 本页单元格色档：本次金额橙（对标橙 token）、变动金额 +绿/−红/0 灰、删徽标红 ── */
.pl-current {
  color: var(--tt2-summary-orange);
}

.pl-delta--up {
  color: var(--tt2-st-doing);
}

.pl-delta--down {
  color: var(--tt2-money-red);
}

.pl-delta--zero {
  color: var(--tt2-dim);
}

.pl-del {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--tt2-money-red);
  color: var(--tt2-on-primary);
  font-size: 11px;
  line-height: 16px;
  vertical-align: 1px;
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

.dtl-dim {
  color: var(--tt2-dim);
  font-size: 12px;
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
