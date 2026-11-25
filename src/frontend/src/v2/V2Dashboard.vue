<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 后台首页 v2 = 管理员经营数据看板（Metabase 浅色卡片风，炭火壳不动）。
 * 像素对照 = `docs/plans/2026-11-06-v2-admin-dashboard-mockup.html`（r4 定稿）；
 * 计划 = 同名 .md（§0 拍板 / §2 数据面 / §3 前端要点）。
 *
 * 数据面（全部真接口）：状态环 / 排行榜 / 客服统计 / 待处理三行 / 在途 / App / 代练通 /
 * 通知 = 现成接口；KPI×5 = `home/overview/` 双窗口（当前窗 + 等长前移窗，§0.12）；
 * 趋势 / 游戏 / 大厅停留 / 同步健康 = `api/dashboard.ts` 的四条聚合端点；
 * 来源环 = `support-stats` 的 `sources[]`（与客服统计同一份载荷，不另发请求）；
 * 待结算金额 = `performances` summary 的生效绩效槽。区间驱动的六处联动用请求序号
 * latest-wins（§3）；图表全部经 `v2/charts.ts` 控制器（⛔ 页面不裸写 SVG）。
 * 非管理员零看板请求。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch, type Ref } from 'vue'
import V2RoleDashboard from '@/v2/V2RoleDashboard.vue'
import { useSessionStore } from '@/stores/session'
import { request } from '@/api/http'
import type { Page } from '@/api/unwrap'
import { toLocalIso } from '@/api/orders'
import { fetchNotifications, markNotificationRead, type Notification } from '@/api/notifications'
import { fetchSystemNotices, type SystemNotice } from '@/api/systemNotices'
import { fetchOrdersWithSummary } from '@/v2/orderManageApi'
import { fetchSupportStats } from '@/api/supportStats'
import { fetchAppStatsOverview, type AppStatsOverview } from '@/api/appStats'
import { fetchPlatformIntegrationSummary } from '@/api/platformIntegrations'
import {
  fetchDailiantongStats,
  STATS_SIDE_PUBLISH,
  type DailiantongStatRow,
} from '@/api/dailiantongExtra'
import { fetchHomeOverview, type HomeOverview } from '@/api/home'
import {
  fetchDashboardTrend,
  fetchDashboardGameDistribution,
  fetchDashboardHallStay,
  fetchDashboardSyncHealth,
  type DashboardHallStay,
  type DashboardSyncChannel,
  type DashboardTrendPoint,
} from '@/api/dashboard'
import { fetchPerformancesWithSummary } from '@/api/performances'
import { fetchHall } from '@/api/hall'
import { V2_ORDER_STATUS_LABELS, type OrderStatus } from '@/config/orderStatus'
import {
  buildDonutData,
  createBarLineCombo,
  createDonut,
  DONUT_OTHER_COLOR,
  loadECharts,
  STATUS_RING_COLORS,
  type BarLineComboController,
  type BarLineComboData,
  type BarLinePoint,
  type DonutController,
  type DonutData,
} from '@/v2/charts'
import { RANGE_LABELS, type DashboardDistRow, type RangeKey } from '@/v2/placeholders'

const session = useSessionStore()

/** 管理员判据只有这一份（`/api/auth/me` 的 `is_account_admin`，fail-closed；§2.3）。 */
const dashboardAllowed = computed(() => session.isAccountAdmin)
const roleLayout = computed(() => {
  const layout = session.user?.dashboard_type
  return layout === 'worker' || layout === 'work' ? layout : null
})

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '暂时取不到数据，请稍后刷新重试'
}

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** 本地日期 → `yyyy-MM-dd`（⛔ 不用 `toISOString()`：那是 UTC，会把两地日期差一天）。 */
function isoDay(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/* ── 区间选择（四预设 + 自定义；默认今天）────────────────────────────── */

const RANGE_PRESETS: readonly { key: RangeKey; label: string }[] = [
  { key: 'yesterday', label: '昨天' },
  { key: 'today', label: '今天' },
  { key: 'd7', label: '最近7天' },
  { key: 'd30', label: '最近30天' },
]

type DateWindow = { start: string; end: string }

const rangeKey = ref<RangeKey>('today')
const customStart = ref('')
const customEnd = ref('')
const showCal = ref(false)
const rangeEl = ref<HTMLElement | null>(null)

function windowFor(key: RangeKey): DateWindow {
  const now = new Date()
  if (key === 'yesterday') {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
    return { start: isoDay(d), end: isoDay(d) }
  }
  if (key === 'd7') {
    return {
      start: isoDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)),
      end: isoDay(now),
    }
  }
  if (key === 'd30') {
    return {
      start: isoDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29)),
      end: isoDay(now),
    }
  }
  if (key === 'custom') {
    const start = customStart.value
    const end = customEnd.value
    // 起 > 止自动对调（§3）。
    return start !== '' && end !== '' && start > end ? { start: end, end: start } : { start, end }
  }
  const today = isoDay(now)
  return { start: today, end: today }
}

const windowText = computed(() => {
  const win = windowFor(rangeKey.value)
  return `${win.start} ~ ${win.end}`
})

/** 区间行自带的日界（本地时区 00:00 / 23:59，带偏移 ISO——口径同 `toLocalIso` 的注释）。 */
function isoWindow(win: DateWindow): { date_from: string; date_to: string } {
  const [sy, sm, sd] = win.start.split('-').map(Number)
  const [ey, em, ed] = win.end.split('-').map(Number)
  return {
    date_from: toLocalIso(new Date(sy!, sm! - 1, sd!, 0, 0, 0)),
    date_to: toLocalIso(new Date(ey!, em! - 1, ed!, 23, 59, 59)),
  }
}

function onPreset(key: RangeKey): void {
  rangeKey.value = key
  showCal.value = false
}

function onCalApply(): void {
  if (customStart.value === '' || customEnd.value === '') return
  rangeKey.value = 'custom'
  showCal.value = false
}

function onDocClick(event: MouseEvent): void {
  const el = rangeEl.value
  if (el && event.target instanceof Node && el.contains(event.target)) return
  showCal.value = false
}

/* ── latest-wins 竞态闸（§3）：只接受最新一轮区间请求的落点 ───────────── */

let rangeEpoch = 0

/* ── 区间驱动的六处联动 ─────────────────────────────────────────────── */

// ① KPI×5（home/overview 双窗口）与 ③④ 来源 / 游戏同步换档。
/** KPI 用 home_overview_data 的五个槽（后端已把率算成一位小数字符串，前端拼 %）。 */
type KpiWindow = Pick<
  HomeOverview,
  'add_count' | 'order_price' | 'profit_amount' | 'finish_order_rate' | 'refund_order_rate'
>

const kpiData = ref<{ cur: KpiWindow; prev: KpiWindow } | null>(null)
const kpiLoading = ref(false)
const kpiError = ref('')
const srcRows = ref<DashboardDistRow[]>([])
const gameRows = ref<DashboardDistRow[]>([])

/** 等长前移窗（§0.12 同比口径）：窗口 N = end−start+1 天，整体前移 N 天。 */
function prevWindowFor(win: DateWindow): DateWindow {
  const [sy, sm, sd] = win.start.split('-').map(Number)
  const [ey, em, ed] = win.end.split('-').map(Number)
  const startDate = new Date(sy!, sm! - 1, sd!)
  const endDate = new Date(ey!, em! - 1, ed!)
  const days = Math.round((endDate.getTime() - startDate.getTime()) / 86_400_000) + 1
  return {
    start: isoDay(new Date(sy!, sm! - 1, sd! - days)),
    end: isoDay(new Date(ey!, em! - 1, ed! - days)),
  }
}

async function loadKpi(win: DateWindow, epoch: number): Promise<void> {
  kpiLoading.value = true
  kpiError.value = ''
  try {
    const prevWin = prevWindowFor(win)
    const [cur, prev] = await Promise.all([
      fetchHomeOverview({ start: win.start, end: win.end }),
      fetchHomeOverview({ start: prevWin.start, end: prevWin.end }),
    ])
    if (epoch !== rangeEpoch) return
    kpiData.value = { cur, prev }
  } catch (e) {
    if (epoch !== rangeEpoch) return
    kpiData.value = null
    kpiError.value = errText(e)
  } finally {
    if (epoch === rangeEpoch) kpiLoading.value = false
  }
}

/** 「▲▼ 比上一周期」：涨跌颜色跟着好坏走（退单率反向），箭头跟着方向走（样稿 §0.12）。 */
type DeltaView = { text: string; cls: 'good' | 'bad' }
function deltaOf(cur: number, prev: number, lowerIsBetter = false): DeltaView | null {
  if (!Number.isFinite(cur) || !Number.isFinite(prev) || prev === 0) return null
  const pct = ((cur - prev) / prev) * 100
  const rising = pct >= 0
  const good = lowerIsBetter ? !rising : rising
  return { text: `${rising ? '▲' : '▼'} ${Math.abs(pct).toFixed(1)}% 比上一周期`, cls: good ? 'good' : 'bad' }
}

type KpiCardView = { key: string; label: string; value: string; delta: DeltaView | null; error: string }

const kpiCards = computed<KpiCardView[]>(() => {
  const pair = kpiData.value
  const cur = pair?.cur
  const prev = pair?.prev
  const delta = (
    pick: (window: KpiWindow) => number,
    lowerIsBetter = false,
  ): DeltaView | null =>
    cur !== undefined && prev !== undefined ? deltaOf(pick(cur), pick(prev), lowerIsBetter) : null
  const fail = kpiError.value
  const value = (pick: (window: KpiWindow) => string): string => (cur === undefined ? '—' : pick(cur))
  return [
    { key: 'add', label: '录单笔数', value: value((w) => String(w.add_count)), delta: delta((w) => w.add_count), error: fail },
    { key: 'price', label: '订单总额', value: value((w) => `¥${w.order_price}`), delta: delta((w) => Number(w.order_price)), error: fail },
    { key: 'profit', label: '预计利润', value: value((w) => w.profit_amount), delta: delta((w) => Number(w.profit_amount)), error: fail },
    { key: 'finish', label: '完成率', value: value((w) => `${w.finish_order_rate}%`), delta: delta((w) => Number(w.finish_order_rate)), error: fail },
    { key: 'refund', label: '退单率', value: value((w) => `${w.refund_order_rate}%`), delta: delta((w) => Number(w.refund_order_rate), true), error: fail },
  ]
})

// ② 状态环（现成接口：订单列表 summary 的 status_counts，随区间）。
const statusData = ref<DonutData | null>(null)
const statusMergedRows = ref<{ name: string; count: number }[]>([])
const statusLoading = ref(false)
const statusError = ref('')

/** 环上「具名五态」与恒并「其他」的三态（合并语义与 buildDonutData 不同，§3）。 */
const RING_NAMED: readonly OrderStatus[] = ['COMPLETED', 'IN_PROGRESS', 'PENDING_REVIEW', 'WAITING_GRAB', 'UNASSIGNED']
const RING_MERGED: readonly OrderStatus[] = ['ISSUE', 'PAUSED', 'REFUNDED']

function statusName(code: OrderStatus): string {
  return V2_ORDER_STATUS_LABELS[code]
}

function applyStatusCounts(counts: Partial<Record<string, number>>): void {
  const named = RING_NAMED.map((code) => ({
    code,
    name: statusName(code),
    count: counts[code] ?? 0,
  }))
  const merged = RING_MERGED.map((code) => ({
    code,
    name: statusName(code),
    count: counts[code] ?? 0,
  }))
  statusMergedRows.value = merged.map(({ name, count }) => ({ name, count }))
  const otherCount = merged.reduce((acc, row) => acc + row.count, 0)
  const rows: { code: string; name: string; count: number }[] = named.filter((row) => row.count > 0)
  rows.sort((a, b) => b.count - a.count)
  if (otherCount > 0) rows.push({ code: '', name: '其他', count: otherCount })
  const total = rows.reduce((acc, row) => acc + row.count, 0)
  const slices = rows.map((row) => ({
    name: row.name,
    value: row.count,
    amountText: String(row.count),
    color: row.code === '' ? DONUT_OTHER_COLOR : (STATUS_RING_COLORS[row.code] ?? DONUT_OTHER_COLOR),
    percentText: `${((row.count / total) * 100).toFixed(1)}%`,
  }))
  statusData.value = total === 0 ? null : { slices, rank: slices, totalValue: total }
}

const statusSubLine = computed(() => {
  if (statusMergedRows.value.length === 0) return ''
  return `其他 = ${statusMergedRows.value.map((row) => `${row.name} ${row.count}`).join(' · ')}`
})

// ⑤ 排行榜（现成接口 order-stats）。
type BoardRow = {
  idx: number
  uid: number
  userName: string
  orderCount: number
  noSettAmount: number
  complateRate: number
  createUserDate: string
  lastRecOrderDate: string
}

const ORDER_STATS_URL = '/api/business/order-stats'
const BOARD_SIZE = 8

const boardRows = ref<BoardRow[]>([])
const boardLoading = ref(false)
const boardError = ref('')

const boardNote = computed(
  () => `${RANGE_LABELS[rangeKey.value]} · 按接单量 Top ${BOARD_SIZE} · 点行看详情`,
)

const boardBarMax = computed(() =>
  boardRows.value.reduce((max, row) => Math.max(max, row.orderCount), 0),
)

// ⑥ 客服接单统计（现成接口 support-stats）。
const supportData = ref<BarLineComboData | null>(null)
const supportLoading = ref(false)
const supportError = ref('')

/* ── 不随区间的卡（§1 标签）────────────────────────────────────────── */

// 订单趋势（真接口 dashboard/trend，固定近 30 天，不随区间）。
const trendData = ref<BarLineComboData | null>(null)
const trendLoading = ref(false)
const trendError = ref('')

/** 端点只回**有订单的日子**；图要连续 30 天 ⇒ 按窗口补零日（缺日 = 0，不是跳过）。 */
function fillTrendPoints(
  days: readonly DashboardTrendPoint[],
  start: string,
  end: string,
): BarLinePoint[] {
  const byDate = new Map(days.map((day) => [day.date, day]))
  const points: BarLinePoint[] = []
  const [sy, sm, sd] = start.split('-').map(Number)
  const [ey, em, ed] = end.split('-').map(Number)
  const endAt = new Date(ey!, em! - 1, ed!)
  for (let at = new Date(sy!, sm! - 1, sd!); at <= endAt; at.setDate(at.getDate() + 1)) {
    const key = isoDay(at)
    const day = byDate.get(key)
    points.push({
      label: key.slice(5),
      barValue: day?.count ?? 0,
      lineValue: Number(day?.amount ?? 0),
      lineText: day?.amount ?? '0.00',
    })
  }
  return points
}

async function loadTrend(): Promise<void> {
  trendLoading.value = true
  trendError.value = ''
  try {
    const now = new Date()
    const end = isoDay(now)
    const start = isoDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29))
    const days = await fetchDashboardTrend({ start, end })
    trendData.value = {
      points: fillTrendPoints(days, start, end),
      barName: '单量',
      lineName: '金额',
    }
    drawTrend()
  } catch (e) {
    trendData.value = null
    trendError.value = errText(e)
  } finally {
    trendLoading.value = false
  }
}

// 大厅实时（真接口三源：hall 列表 total / order-stats 今日全行求和 / dashboard/hall-stay）。
const HALL_GRAB_LIMIT = 2000

const hallInHall = ref<number | null>(null)
const hallGrabbedToday = ref<number | null>(null)
const hallStay = ref<DashboardHallStay | null>(null)
const hallError = ref('')

/** 分钟数 → 展示串：不足一小时「N 分钟」，否则「HhMm」（null = 无样本 → 「—」）。 */
function stayText(minutes: number | null): string {
  if (minutes === null) return '—'
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`
}

async function loadHallLive(): Promise<void> {
  hallError.value = ''
  const today = isoDay(new Date())
  try {
    const [hallPage, stay, grabbed] = await Promise.all([
      fetchHall({ page: 1, page_size: 1 }),
      fetchDashboardHallStay(),
      request<Page<BoardRow>>({
        method: 'get',
        url: ORDER_STATS_URL,
        params: { page: 1, limit: HALL_GRAB_LIMIT, stime: today, etime: today },
      }),
    ])
    hallInHall.value = hallPage.total
    hallStay.value = stay
    hallGrabbedToday.value = grabbed.items.reduce((acc, row) => acc + row.orderCount, 0)
  } catch (e) {
    hallInHall.value = null
    hallStay.value = null
    hallGrabbedToday.value = null
    hallError.value = errText(e)
  }
}

// 同步健康（真接口 dashboard/sync-health；闲管家口径未拍板 → 三键 null → 显示「—」）。
const syncChannels = ref<DashboardSyncChannel[]>([])
const syncError = ref('')

/** ISO 时刻 → 相对时间展示串（分钟粒度；跨天回落绝对时刻）。 */
function syncLastText(ch: DashboardSyncChannel): string {
  if (ch.last_time === null) return '—'
  const at = new Date(ch.last_time)
  if (Number.isNaN(at.getTime())) return '—'
  const diffMin = Math.round((Date.now() - at.getTime()) / 60_000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  if (diffMin < 24 * 60) return `${Math.floor(diffMin / 60)} 小时前`
  return `${pad2(at.getMonth() + 1)}-${pad2(at.getDate())} ${pad2(at.getHours())}:${pad2(at.getMinutes())}`
}

function syncStateOf(ch: DashboardSyncChannel): { cls: 'ok' | 'warn'; note: string } {
  // 三键 null = 闲管家（口径未拍板，见 api/dashboard.ts）：显示「—」，title 说明原因。
  if (ch.pending === null || ch.failed === null) return { cls: 'ok', note: '—' }
  const last = syncLastText(ch)
  if (ch.failed > 0) return { cls: 'warn', note: `${last} · 失败 ${ch.failed}` }
  if (ch.pending > 0) return { cls: 'warn', note: `${last} · 积压 ${ch.pending}` }
  return { cls: 'ok', note: `${last} · 积压 0` }
}

async function loadSyncHealth(): Promise<void> {
  syncError.value = ''
  try {
    const payload = await fetchDashboardSyncHealth()
    syncChannels.value = payload.channels
  } catch (e) {
    syncChannels.value = []
    syncError.value = errText(e)
  }
}

// 待处理事项（当前态）：三行现成（orders summary 无窗口 = 当前在库全集）、待结算金额挂账。
const currentCounts = ref<Partial<Record<string, number>>>({})
const currentLoading = ref(false)
const currentError = ref('')

const TODO_ROWS: readonly { code: OrderStatus; label: string; action: string }[] = [
  { code: 'PENDING_REVIEW', label: '待验收单', action: '去处理 →' },
  { code: 'ISSUE', label: '问题单', action: '去处理 →' },
  { code: 'UNASSIGNED', label: '未分配', action: '去指派 →' },
]

function countOf(code: OrderStatus): number {
  return currentCounts.value[code] ?? 0
}

const TRANSIT_CODES: readonly OrderStatus[] = ['IN_PROGRESS', 'PENDING_REVIEW', 'WAITING_GRAB', 'UNASSIGNED']
const transitTotal = computed(() => TRANSIT_CODES.reduce((acc, code) => acc + countOf(code), 0))
const transitSub = computed(() => {
  const parts = TRANSIT_CODES.map((code) => `${statusName(code)} ${countOf(code)}`)
  return `当前在库 · ${parts.join(' · ')}`
})

// App / 小程序（现成接口；PV/UV = 微信统计 T+1，仅可用租户有值——§2.1 口径更正）。
const appStats = ref<AppStatsOverview | null>(null)
const appLoading = ref(false)
const appError = ref('')

const appWechatNote = computed(() => {
  const wechat = appStats.value?.wechat
  if (wechat === undefined || wechat === null) return ''
  return wechat.available ? `微信数据 T+1 · 截至 ${wechat.date_to}` : '微信数据次日更新'
})

// 代练通（现成接口：余额 = summary、发布单数 = stats、预警线 = balance-alert）。
const BALANCE_ALERT_URL = '/api/business/platform-integrations/balance-alert/'

const dltBalance = ref<string | null>(null)
const dltReason = ref('')
const dltMonthCount = ref<number | null>(null)
const dltTodayCount = ref<number | null>(null)
const dltAlertLine = ref('')

/** 上游统计行的形状由上游决定（`DailiantongStatRow`）；本页只认 `QDate` / `PubCount` 两键。 */
function statPubCount(row: DailiantongStatRow): number | null {
  const n = Number(row['PubCount'])
  return Number.isFinite(n) && n >= 0 ? n : null
}

function statDate(row: DailiantongStatRow): string {
  const raw = row['QDate']
  return typeof raw === 'string' ? raw.slice(0, 10) : ''
}

/** 上游按日统计只有「有数据的日期」⇒ 窗口内逐行累加（无行 = 0，不是「没查到」）。 */
function sumPubCounts(rows: readonly DailiantongStatRow[], start: string, end: string): number {
  let sum = 0
  for (const row of rows) {
    const date = statDate(row)
    if (date === '' || date < start || date > end) continue
    const n = statPubCount(row)
    if (n !== null) sum += n
  }
  return sum
}

/* ── 通知与公告（唯一管理员/非管理员都取的卡）───────────────────────── */

const NOTICE_PAGE_SIZE = 3

type NoticeRow = {
  key: string
  time: string
  title: string
  content?: string
  unreadNoticeId?: number
}

const notices = ref<NoticeRow[]>([])
const selectedNotice = ref<NoticeRow | null>(null)

async function loadNotices(): Promise<void> {
  const viewer = session.user
  const [systemPage, notificationPage] = await Promise.all([
    fetchSystemNotices({ page: 1, page_size: NOTICE_PAGE_SIZE }).catch((): null => null),
    fetchNotifications({ page: 1, page_size: NOTICE_PAGE_SIZE }).catch((): null => null),
  ])
  if (session.user !== viewer) return
  notices.value = [
    ...(systemPage?.items ?? []).map(
      (notice: SystemNotice): NoticeRow => ({
        key: `s:${notice.id}`,
        time: notice.created_at.slice(5, 10),
        title: `【公告】${notice.title || '（无标题公告）'}`,
        content: notice.content,
      }),
    ),
    ...(notificationPage?.items ?? []).map((notice: Notification): NoticeRow => {
      const orderNo = notice.source_order_no || notice.order_no || notice.payload?.order_no
      const content = [
        orderNo ? `订单号 ${orderNo}` : `通知类型 ${notice.kind}`,
        notice.payload?.from_status ? `来源状态 ${notice.payload.from_status}` : '',
      ].filter(Boolean).join('\n')
      return {
        key: `n:${notice.id}`,
        time: notice.created_at.slice(5, 10),
        title: orderNo ? `【${notice.kind}】订单 ${orderNo}` : `【${notice.kind}】`,
        content,
        unreadNoticeId: notice.read_at === null ? notice.id : undefined,
      }
    }),
  ].slice(0, NOTICE_PAGE_SIZE)
}

function openNotice(row: NoticeRow): void {
  selectedNotice.value = row
  if (row.unreadNoticeId !== undefined) {
    void markNotificationRead(row.unreadNoticeId)
      .then(() => { row.unreadNoticeId = undefined })
      .catch(() => undefined)
  }
}

/* ── 图表（全部经 charts.ts 控制器）────────────────────────────────── */

const trendEl = ref<HTMLElement | null>(null)
const statusEl = ref<HTMLElement | null>(null)
const srcEl = ref<HTMLElement | null>(null)
const gameEl = ref<HTMLElement | null>(null)
const supportEl = ref<HTMLElement | null>(null)

const chartsError = ref(false)
let trendChart: BarLineComboController | null = null
let supportChart: BarLineComboController | null = null
const donuts: Partial<Record<'status' | 'src' | 'game', DonutController>> = {}

const srcDonut = computed<DonutData | null>(() =>
  buildDonutData(srcRows.value.map((row) => ({ name: row.name, amount: String(row.count) }))),
)
const gameDonut = computed<DonutData | null>(() =>
  buildDonutData(gameRows.value.map((row) => ({ name: row.name, amount: String(row.count) }))),
)

const srcTotal = computed(() => String(srcDonut.value?.totalValue ?? 0))
const gameTotal = computed(() => String(gameDonut.value?.totalValue ?? 0))
const statusTotalText = computed(() => String(statusData.value?.totalValue ?? 0))

async function withECharts(draw: (echarts: Awaited<ReturnType<typeof loadECharts>>) => void): Promise<void> {
  try {
    const echarts = await loadECharts()
    await nextTick()
    draw(echarts)
  } catch {
    chartsError.value = true
  }
}

function drawTrend(): void {
  const data = trendData.value
  if (data === null) return
  void withECharts((echarts) => {
    const el = trendEl.value
    if (el === null) return
    trendChart ??= createBarLineCombo(el, echarts)
    trendChart.update(data)
    trendChart.resize()
  })
}

type DonutSlot = 'status' | 'src' | 'game'

/** 图例点击显隐（Metabase 行为）：按名字记隐藏态，重画时过滤 slices（占比/明细仍按全量算）。 */
const hiddenSlices = reactive<Record<DonutSlot, string[]>>({ status: [], src: [], game: [] })

function onLegendToggle(slot: DonutSlot, name: string): void {
  const list = hiddenSlices[slot]
  const at = list.indexOf(name)
  if (at >= 0) list.splice(at, 1)
  else list.push(name)
  if (slot === 'status') drawStatus()
  else if (slot === 'src') drawSrc()
  else drawGame()
}

/** 画一张环形：容器引用在 nextTick 之后才读（数据到达当帧 DOM 还没切换，⛔ 提前读会拿到 null）。 */
function drawDonut(
  slot: DonutSlot,
  elRef: Ref<HTMLElement | null>,
  data: DonutData | null,
  totalText: string,
): void {
  if (data === null) {
    donuts[slot]?.dispose()
    delete donuts[slot]
    return
  }
  const visible: DonutData = {
    slices: data.slices.filter((slice) => !hiddenSlices[slot].includes(slice.name)),
    rank: data.rank,
    totalValue: data.totalValue,
  }
  void withECharts((echarts) => {
    const el = elRef.value
    if (el === null) return
    let controller = donuts[slot]
    if (controller === undefined) {
      controller = createDonut(el, echarts, {
        centerLabel: '区间单量',
        hideSliceLabels: true,
        hideTipOnLeave: true,
        // 3.6.2 只认 itemStyle.normal 嵌套形态：不嵌套时切片落回内置位置色板 ⇒ 与图例点异色。
        applySliceColors: true,
      })
      donuts[slot] = controller
    }
    controller.update(visible, totalText, '区间单量')
    controller.resize()
  })
}

function drawStatus(): void {
  drawDonut('status', statusEl, statusData.value, statusTotalText.value)
}

function drawSrc(): void {
  drawDonut('src', srcEl, srcDonut.value, srcTotal.value)
}

function drawGame(): void {
  drawDonut('game', gameEl, gameDonut.value, gameTotal.value)
}

function drawSupport(): void {
  const data = supportData.value
  if (data === null) return
  void withECharts((echarts) => {
    const el = supportEl.value
    if (el === null) return
    supportChart ??= createBarLineCombo(el, echarts)
    supportChart.update(data)
    supportChart.resize()
  })
}

/** 待处理行墨点的取色类（值见 carbon.css 看板段；状态码 → 同族色）。 */
function todoDotClass(code: OrderStatus): string {
  if (code === 'PENDING_REVIEW') return 'cv2-dsh-dot--accept'
  if (code === 'ISSUE') return 'cv2-dsh-dot--issue'
  return 'cv2-dsh-dot--unassigned'
}

function onLegendEnter(slot: DonutSlot, name: string): void {
  donuts[slot]?.highlight(name)
}

function onLegendLeave(slot: DonutSlot): void {
  donuts[slot]?.downplay()
}

function onWindowResize(): void {
  trendChart?.resize()
  supportChart?.resize()
  for (const slot of Object.keys(donuts) as DonutSlot[]) donuts[slot]?.resize()
}

/* ── 排行榜弹窗（已接四格；金额/在途/近 7 天柱为待接口预留位）── */

const boardModalRow = ref<BoardRow | null>(null)

/* ── 取数（区间驱动 + 固定两批）────────────────────────────────────── */

async function loadStatusRing(win: DateWindow, epoch: number): Promise<void> {
  statusLoading.value = true
  statusError.value = ''
  try {
    const page = await fetchOrdersWithSummary({ ...isoWindow(win), page: 1, page_size: 1 })
    if (epoch !== rangeEpoch) return
    applyStatusCounts(page.summary?.status_counts ?? {})
    await drawStatus()
  } catch (e) {
    if (epoch !== rangeEpoch) return
    statusData.value = null
    statusError.value = errText(e)
  } finally {
    if (epoch === rangeEpoch) statusLoading.value = false
  }
}

async function loadBoard(win: DateWindow, epoch: number): Promise<void> {
  boardLoading.value = true
  boardError.value = ''
  try {
    const page = await request<Page<BoardRow>>({
      method: 'get',
      url: ORDER_STATS_URL,
      params: { page: 1, limit: BOARD_SIZE, stime: win.start, etime: win.end },
    })
    if (epoch !== rangeEpoch) return
    boardRows.value = page.items
  } catch (e) {
    if (epoch !== rangeEpoch) return
    boardRows.value = []
    boardError.value = errText(e)
  } finally {
    if (epoch === rangeEpoch) boardLoading.value = false
  }
}

async function loadSupport(win: DateWindow, epoch: number): Promise<void> {
  supportLoading.value = true
  supportError.value = ''
  try {
    const payload = await fetchSupportStats({ dateFrom: win.start, dateTo: win.end })
    if (epoch !== rangeEpoch) return
    supportData.value = {
      points: payload.operators.map((row) => ({
        label: row.name,
        barValue: row.order_count,
        lineValue: Number(row.total_price),
        lineText: row.total_price,
      })).sort((a, b) => b.barValue - a.barValue),
      barName: '接单量',
      lineName: '金额',
    }
    // ⚠️ 口径：来源环吃**同一份载荷**的 sources[]（店铺级来源字典名快照、
    // 空名后端已归「未指定」）——不是样稿里的五个渠道档。不另发请求。
    srcRows.value = payload.sources.map((row) => ({ name: row.name, count: row.order_count }))
    drawSupport()
    drawSrc()
  } catch (e) {
    if (epoch !== rangeEpoch) return
    supportData.value = null
    srcRows.value = []
    supportError.value = errText(e)
    drawSrc()
  } finally {
    if (epoch === rangeEpoch) supportLoading.value = false
  }
}

// 游戏单量分布（真接口 dashboard/game-distribution，随区间）。
const gameLoading = ref(false)
const gameError = ref('')

async function loadGame(win: DateWindow, epoch: number): Promise<void> {
  gameLoading.value = true
  gameError.value = ''
  try {
    const rows = await fetchDashboardGameDistribution({ start: win.start, end: win.end })
    if (epoch !== rangeEpoch) return
    gameRows.value = rows
    drawGame()
  } catch (e) {
    if (epoch !== rangeEpoch) return
    gameRows.value = []
    gameError.value = errText(e)
    drawGame()
  } finally {
    if (epoch === rangeEpoch) gameLoading.value = false
  }
}

const updatedAt = ref('')

function stampNow(): void {
  const now = new Date()
  updatedAt.value = `更新于 ${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} ${pad2(now.getHours())}:${pad2(now.getMinutes())}`
}

/** 区间切换：KPI 双窗口 + 来源 / 游戏 / 状态 / 排行 / 客服 五路并发，旧轮响应一律丢弃。 */
function loadRangeWindow(): void {
  const epoch = rangeEpoch + 1
  rangeEpoch = epoch
  const win = windowFor(rangeKey.value)
  void loadKpi(win, epoch)
  void loadStatusRing(win, epoch)
  void loadBoard(win, epoch)
  void loadSupport(win, epoch)
  void loadGame(win, epoch)
  stampNow()
}

watch(rangeKey, () => {
  loadRangeWindow()
})

async function loadCurrentState(): Promise<void> {
  currentLoading.value = true
  currentError.value = ''
  try {
    // 不带窗口 = 当前在库全集（待处理三行 + 在途订单的同一份来源，§2.1）。
    const page = await fetchOrdersWithSummary({ page: 1, page_size: 1 })
    currentCounts.value = page.summary?.status_counts ?? {}
  } catch (e) {
    currentError.value = errText(e)
  } finally {
    currentLoading.value = false
  }
}

// 待结算金额 = performances summary 的**生效绩效**槽（bonus_amount）。
// ⚠️ 口径差（如实）：这是「全部未结账绩效行」的 bonus_amount 原始列合计——
// 不等于结算页的「应付净额」（那边还要扣固定扣点 / 调整记录），也没有时间窗。
const settleAmount = ref<string | null>(null)

async function loadSettleAmount(): Promise<void> {
  try {
    const page = await fetchPerformancesWithSummary({ settle_status: '未结账', page_size: 1 })
    settleAmount.value = page.summary.bonus_amount
  } catch {
    settleAmount.value = null
  }
}

async function loadApp(): Promise<void> {
  appLoading.value = true
  appError.value = ''
  try {
    appStats.value = await fetchAppStatsOverview(1)
  } catch (e) {
    appStats.value = null
    appError.value = errText(e)
  } finally {
    appLoading.value = false
  }
}

async function loadDailiantong(): Promise<void> {
  const now = new Date()
  const today = isoDay(now)
  const monthStart = isoDay(new Date(now.getFullYear(), now.getMonth(), 1))
  fetchPlatformIntegrationSummary()
    .then((summary) => {
      dltBalance.value = summary.available_balance
      dltReason.value = summary.configured ? summary.error : '未配置代练通发单账号'
    })
    .catch((e) => {
      dltBalance.value = null
      dltReason.value = errText(e)
    })
  fetchDailiantongStats({ start: monthStart, end: today, is_pub: STATS_SIDE_PUBLISH })
    .then((stats) => {
      dltMonthCount.value = sumPubCounts(stats.items, monthStart, today)
      dltTodayCount.value = sumPubCounts(stats.items, today, today)
    })
    .catch(() => {
      dltMonthCount.value = null
      dltTodayCount.value = null
    })
  interface BalanceAlert {
    configured: boolean
    needed: string
    available: string | null
    is_low: boolean
    error: string
  }
  request<BalanceAlert>({ method: 'get', url: BALANCE_ALERT_URL })
    .then((alert) => {
      if (alert.error !== '') dltAlertLine.value = alert.error
      else if (!alert.configured) dltAlertLine.value = '未配置代练通发单账号'
      else if (alert.is_low) dltAlertLine.value = `余额低于未来 24h 待发所需 ¥${alert.needed}`
      else dltAlertLine.value = `余额可覆盖未来 24h 待发所需 ¥${alert.needed}`
    })
    .catch((e) => {
      dltAlertLine.value = errText(e)
    })
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  window.addEventListener('resize', onWindowResize)
  if (!dashboardAllowed.value) return
  void loadTrend()
  loadRangeWindow()
  void loadCurrentState()
  void loadApp()
  void loadDailiantong()
  void loadHallLive()
  void loadSyncHealth()
  void loadSettleAmount()
})

watch(() => session.user, (user) => {
  notices.value = []
  selectedNotice.value = null
  if (user) void loadNotices()
}, { immediate: true })

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('resize', onWindowResize)
  trendChart?.dispose()
  supportChart?.dispose()
  for (const slot of Object.keys(donuts) as DonutSlot[]) donuts[slot]?.dispose()
})
</script>

<template>
  <div class="cv2-page cv2-dsh">
    <header class="cv2-dsh-head">
      <h1 class="cv2-dsh-title">{{ dashboardAllowed ? '经营总览' : roleLayout === 'worker' ? '我的工作台' : roleLayout === 'work' ? '客服工作台' : '后台首页' }}</h1>
      <span v-if="dashboardAllowed" class="cv2-dsh-updated" data-v2="updated">{{ updatedAt }}</span>
    </header>

    <V2RoleDashboard v-if="!dashboardAllowed && roleLayout && session.user" :key="JSON.stringify(session.user)" :layout="roleLayout">
      <template #notices>
        <section class="cv2-dsh-card" data-v2="notices">
        <h3 class="cv2-dsh-card-title">通知与公告</h3>
        <div v-if="notices.length > 0" data-v2="notice-rows">
          <div v-for="row in notices" :key="row.key" class="cv2-dsh-nt-row">
            <span class="cv2-dsh-nt-date">{{ row.time }}</span>
            <button v-if="!dashboardAllowed || row.key.startsWith('n:')" type="button" class="cv2-dsh-notice-button"
              @click="openNotice(row)">{{ row.title }}</button>
            <span v-else class="cv2-dsh-nt-plain">{{ row.title }}</span>
          </div>
        </div>
        <p v-else class="cv2-dsh-note">暂时没有通知和公告。</p>
        </section>
      </template>
    </V2RoleDashboard>
    <p v-else-if="!dashboardAllowed" class="cv2-dsh-locked" data-v2="locked">欢迎回来，{{ session.user?.display_name || session.user?.username }}。</p>

    <div v-if="dashboardAllowed" ref="rangeEl" class="cv2-dsh-range" data-v2="range">
      <button
        v-for="preset in RANGE_PRESETS"
        :key="preset.key"
        type="button"
        class="cv2-dsh-rbtn"
        :class="{ 'cv2-dsh-rbtn--on': rangeKey === preset.key }"
        :data-v2-range="preset.key"
        @click="onPreset(preset.key)"
      >{{ preset.label }}</button>
      <button type="button" class="cv2-dsh-rbox" data-v2="range-box" @click="showCal = !showCal">
        {{ windowText }} ▾
      </button>
      <div v-if="showCal" class="cv2-dsh-cal" data-v2="cal">
        <input v-model="customStart" class="cv2-input" type="date" data-v2="cal-start" aria-label="开始日期" />
        <span class="cv2-dsh-cal-dash" aria-hidden="true">~</span>
        <input v-model="customEnd" class="cv2-input" type="date" data-v2="cal-end" aria-label="结束日期" />
        <button type="button" class="cv2-btn cv2-btn--primary cv2-dsh-cal-apply cv2-btn--commit" data-v2="cal-apply" @click="onCalApply">应用</button>
        <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="cal-cancel" @click="showCal = false">取消</button>
      </div>
    </div>

    <div class="cv2-dsh-grid" :class="{ 'cv2-dsh-grid--worker': !dashboardAllowed && roleLayout === 'worker' }">
      <template v-if="dashboardAllowed">
        <!-- KPI×6：前五项随区间（home/overview 双窗口真接口），在途订单 = 当前在库态 -->
        <section
          v-for="card in kpiCards"
          :key="card.key"
          class="cv2-dsh-card cv2-dsh-kpi"
          :data-v2-kpi="card.key"
        >
          <h3 class="cv2-dsh-card-title">{{ card.label }}</h3>
          <div v-if="kpiLoading" class="cv2-dsh-skel cv2-dsh-skel--short" data-v2="kpi-skel"></div>
          <b v-else class="cv2-dsh-kpi-num">{{ card.value }}</b>
          <div v-if="!kpiLoading && card.error !== ''" class="cv2-dsh-kpi-sub cv2-dsh-fail" data-v2="kpi-fail">{{ card.error }}</div>
          <div v-else-if="!kpiLoading && card.delta" class="cv2-dsh-kpi-sub" :class="card.delta.cls === 'good' ? 'cv2-dsh-up' : 'cv2-dsh-down'">
            {{ card.delta.text }}
          </div>
          <div v-else-if="!kpiLoading" class="cv2-dsh-kpi-sub">比上一周期 —</div>
        </section>
        <section class="cv2-dsh-card cv2-dsh-kpi" data-v2-kpi="transit">
          <h3 class="cv2-dsh-card-title">在途订单</h3>
          <template v-if="currentError !== ''">
            <b class="cv2-dsh-kpi-num">—</b>
            <div class="cv2-dsh-kpi-sub cv2-dsh-fail" data-v2="transit-fail">{{ currentError }}</div>
          </template>
          <template v-else-if="currentLoading">
            <div class="cv2-dsh-skel cv2-dsh-skel--short" data-v2="transit-skel"></div>
          </template>
          <template v-else>
            <b class="cv2-dsh-kpi-num" data-v2="transit-total">{{ transitTotal }}</b>
            <div class="cv2-dsh-kpi-sub" data-v2="transit-sub">{{ transitSub }}</div>
          </template>
        </section>

        <!-- 订单趋势：固定近 30 天（dashboard/trend 真接口），不随区间 -->
        <section class="cv2-dsh-card cv2-dsh-card--sp2" data-v2="trend">
          <h3 class="cv2-dsh-card-title">
            订单趋势
            <span class="cv2-dsh-tag cv2-dsh-tag--gray">固定近 30 天 · 不随区间</span>
          </h3>
          <p class="cv2-dsh-note">左轴=每日单量（柱），右轴=每日金额（线，已排除已退单部分）· 悬停看明细</p>
          <div class="cv2-dsh-mini-legend" aria-hidden="true">
            <span><i class="cv2-dsh-dot-bar"></i>单量</span>
            <span><i class="cv2-dsh-dot-line"></i>金额</span>
          </div>
          <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
          <p v-else-if="trendError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="trend-fail">{{ trendError }}</p>
          <div v-else-if="trendLoading" class="cv2-dsh-skel cv2-dsh-skel--wide" data-v2="trend-skel"></div>
          <div v-show="!chartsError && trendError === '' && !trendLoading" ref="trendEl" class="cv2-dsh-chart cv2-dsh-chart--trend" data-v2="trend-chart"></div>
        </section>

        <!-- 订单状态分布：随区间（真接口 status_counts）。图表容器恒挂载（v-show）：
             v-if 链会让容器节点随加载态反复重建，ECharts 控制器会绑到已脱离的旧节点上。 -->
        <section class="cv2-dsh-card" data-v2="status-card">
          <h3 class="cv2-dsh-card-title">订单状态分布<span class="cv2-dsh-tag">随区间</span></h3>
          <p class="cv2-dsh-note">选中区间内新建订单的状态构成</p>
          <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
          <p v-else-if="statusError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="status-fail">{{ statusError }}</p>
          <div v-else-if="statusLoading" class="cv2-dsh-skel" data-v2="status-skel"></div>
          <p v-else-if="statusData === null" class="cv2-dsh-note" data-v2="status-empty">区间内暂无新建订单</p>
          <div v-show="statusData !== null" class="cv2-dsh-dl">
            <div ref="statusEl" class="cv2-dsh-chart cv2-dsh-chart--donut" data-v2="status-chart"></div>
            <div class="cv2-dsh-legend" data-v2="status-legend">
              <button
                v-for="slice in (statusData ? statusData.slices : [])"
                :key="slice.name"
                type="button"
                class="cv2-dsh-legend-row"
                :class="{ 'cv2-dsh-legend-row--off': hiddenSlices.status.includes(slice.name) }"
                :data-v2-legend="slice.name"
                @mouseenter="onLegendEnter('status', slice.name)"
                @mouseleave="onLegendLeave('status')"
                @click="onLegendToggle('status', slice.name)"
              >
                <i class="cv2-dsh-legend-dot" :style="{ background: slice.color }"></i>
                <span class="cv2-dsh-legend-name">{{ slice.name }}</span>
                <span class="cv2-dsh-legend-val">{{ slice.percentText }}</span>
              </button>
              <p class="cv2-dsh-legend-sub" data-v2="status-sub">{{ statusSubLine }}</p>
            </div>
          </div>
        </section>

        <!-- 订单来源分布（support-stats 的 sources[]，与客服统计同一份载荷随区间）。
             ⚠️ 口径：店铺级来源字典名快照（空名归「未指定」），非样稿的渠道档；
             <2.5% 小片 ≥2 个才并「其他」、>8 段收前 7 + 其他（buildDonutData）。 -->
        <section class="cv2-dsh-card" data-v2="src-card">
          <h3 class="cv2-dsh-card-title">订单来源分布<span class="cv2-dsh-tag">随区间</span></h3>
          <p class="cv2-dsh-note">按订单来源字典名 · 空名归「未指定」</p>
          <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
          <p v-else-if="supportError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="src-fail">{{ supportError }}</p>
          <div v-else-if="supportLoading" class="cv2-dsh-skel" data-v2="src-skel"></div>
          <p v-else-if="srcDonut === null" class="cv2-dsh-note" data-v2="src-empty">区间内暂无来源数据</p>
          <div v-show="srcDonut !== null" class="cv2-dsh-dl">
            <div class="cv2-dsh-legend" data-v2="src-legend">
              <button
                v-for="slice in (srcDonut ? srcDonut.slices : [])"
                :key="slice.name"
                type="button"
                class="cv2-dsh-legend-row"
                :class="{ 'cv2-dsh-legend-row--off': hiddenSlices.src.includes(slice.name) }"
                :data-v2-legend="slice.name"
                @mouseenter="onLegendEnter('src', slice.name)"
                @mouseleave="onLegendLeave('src')"
                @click="onLegendToggle('src', slice.name)"
              >
                <i class="cv2-dsh-legend-dot" :style="{ background: slice.color }"></i>
                <span class="cv2-dsh-legend-name">{{ slice.name }}</span>
                <span class="cv2-dsh-legend-val">{{ slice.percentText }}</span>
              </button>
            </div>
            <div ref="srcEl" class="cv2-dsh-chart cv2-dsh-chart--donut" data-v2="src-chart"></div>
          </div>
        </section>

        <!-- 游戏单量分布（dashboard/game-distribution 真接口，随区间） -->
        <section class="cv2-dsh-card" data-v2="game-card">
          <h3 class="cv2-dsh-card-title">游戏单量分布<span class="cv2-dsh-tag">随区间</span></h3>
          <p class="cv2-dsh-note">区间内单量按游戏 · 空名归「未分组」</p>
          <p v-if="chartsError" class="cv2-dsh-note cv2-dsh-fail">图表库加载失败，请刷新页面重试</p>
          <p v-else-if="gameError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="game-fail">{{ gameError }}</p>
          <div v-else-if="gameLoading" class="cv2-dsh-skel" data-v2="game-skel"></div>
          <p v-else-if="gameDonut === null" class="cv2-dsh-note" data-v2="game-empty">区间内暂无游戏单量数据</p>
          <div v-show="gameDonut !== null" class="cv2-dsh-dl">
            <div class="cv2-dsh-legend" data-v2="game-legend">
              <button
                v-for="slice in (gameDonut ? gameDonut.slices : [])"
                :key="slice.name"
                type="button"
                class="cv2-dsh-legend-row"
                :class="{ 'cv2-dsh-legend-row--off': hiddenSlices.game.includes(slice.name) }"
                :data-v2-legend="slice.name"
                @mouseenter="onLegendEnter('game', slice.name)"
                @mouseleave="onLegendLeave('game')"
                @click="onLegendToggle('game', slice.name)"
              >
                <i class="cv2-dsh-legend-dot" :style="{ background: slice.color }"></i>
                <span class="cv2-dsh-legend-name">{{ slice.name }}</span>
                <span class="cv2-dsh-legend-val">{{ slice.percentText }}</span>
              </button>
            </div>
            <div ref="gameEl" class="cv2-dsh-chart cv2-dsh-chart--donut" data-v2="game-chart"></div>
          </div>
        </section>

        <!-- 接单排行榜（真接口 order-stats；行可点弹详情） -->
        <section class="cv2-dsh-card" data-v2="board">
          <h3 class="cv2-dsh-card-title">接单排行榜<span class="cv2-dsh-tag cv2-dsh-tag--gray">点击行看详情</span></h3>
          <p class="cv2-dsh-note" data-v2="board-note">{{ boardNote }}</p>
          <p v-if="boardError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="board-fail">{{ boardError }}</p>
          <div v-else-if="boardLoading" class="cv2-dsh-skel" data-v2="board-skel"></div>
          <p v-else-if="boardRows.length === 0" class="cv2-dsh-note" data-v2="board-empty">区间内暂无接单数据</p>
          <div v-else class="cv2-dsh-board" data-v2="board-rows">
            <div
              v-for="(row, index) in boardRows"
              :key="row.uid"
              class="cv2-dsh-board-row"
              :data-v2-board-row="row.uid"
              @click="boardModalRow = row"
            >
              <span class="cv2-dsh-board-rank">{{ index + 1 }}</span>
              <span class="cv2-dsh-board-name">{{ row.userName }}</span>
              <span class="cv2-dsh-board-bar"><i :style="{ width: `${(row.orderCount / boardBarMax) * 100}%` }"></i></span>
              <span class="cv2-dsh-board-ct">{{ row.orderCount }} 单 · {{ row.complateRate }}%</span>
            </div>
          </div>
        </section>

        <!-- 客服接单统计（真接口 support-stats；span3，随区间） -->
        <section class="cv2-dsh-card cv2-dsh-card--sp3" data-v2="support">
          <h3 class="cv2-dsh-card-title">客服接单统计<span class="cv2-dsh-tag cv2-dsh-tag--gray">跟随区间</span></h3>
          <p class="cv2-dsh-note">柱=接单量（左轴），线=订单金额（右轴）</p>
          <div class="cv2-dsh-mini-legend" aria-hidden="true">
            <span><i class="cv2-dsh-dot-bar"></i>接单量</span>
            <span><i class="cv2-dsh-dot-line"></i>金额</span>
          </div>
          <p v-if="supportError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="support-fail">{{ supportError }}</p>
          <div v-else-if="supportLoading" class="cv2-dsh-skel cv2-dsh-skel--wide" data-v2="support-skel"></div>
          <p v-else-if="supportData === null" class="cv2-dsh-note" data-v2="support-empty">区间内暂无客服接单记录</p>
          <div v-show="!chartsError && supportData !== null" ref="supportEl" class="cv2-dsh-chart cv2-dsh-chart--wide" data-v2="support-chart"></div>
        </section>

        <!-- 待处理事项（当前态）：三行真接口；待结算金额占位待接口 -->
        <section class="cv2-dsh-card" data-v2="todo">
          <h3 class="cv2-dsh-card-title">待处理事项</h3>
          <p class="cv2-dsh-note">当前态 · 现成接口</p>
          <p v-if="currentError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="todo-fail">{{ currentError }}</p>
          <div v-else-if="currentLoading" class="cv2-dsh-skel cv2-dsh-skel--short" data-v2="todo-skel"></div>
          <div v-else data-v2="todo-rows">
            <div v-for="row in TODO_ROWS" :key="row.code" class="cv2-dsh-row">
              <span class="cv2-dsh-dot" :class="todoDotClass(row.code)"></span>
              <b>{{ countOf(row.code) }}</b> {{ row.label }}
              <span class="cv2-dsh-row-val">{{ row.action }}</span>
            </div>
            <div
              class="cv2-dsh-row"
              data-v2="todo-settle"
              title="口径：全部未结账绩效行的生效绩效（bonus_amount）合计，非结算页应付净额"
            >
              <span class="cv2-dsh-dot cv2-dsh-dot--settle"></span>
              <b data-v2="settle-amount">{{ settleAmount === null ? '—' : `¥${settleAmount}` }}</b> 待结算金额
              <span class="cv2-dsh-row-val">去结算 →</span>
            </div>
          </div>
        </section>

        <!-- 大厅实时（hall total + order-stats 今日求和 + dashboard/hall-stay 真接口） -->
        <section class="cv2-dsh-card" data-v2="hall">
          <h3 class="cv2-dsh-card-title">大厅实时<span class="cv2-dsh-tag cv2-dsh-tag--gray">当前时点快照</span></h3>
          <p v-if="hallError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="hall-fail">{{ hallError }}</p>
          <template v-else>
            <div class="cv2-dsh-metrics" data-v2="hall-metrics">
              <div><span class="cv2-dsh-note">当前在厅</span><b class="cv2-dsh-stat-num" data-v2="hall-in">{{ hallInHall === null ? '—' : hallInHall }}</b></div>
              <div><span class="cv2-dsh-note">今日被抢走</span><b class="cv2-dsh-stat-num" data-v2="hall-grabbed">{{ hallGrabbedToday === null ? '—' : hallGrabbedToday }}</b></div>
              <div><span class="cv2-dsh-note">待抢单均停留</span><b class="cv2-dsh-stat-num" data-v2="hall-avg">{{ stayText(hallStay === null ? null : hallStay.avg_stay_minutes) }}</b></div>
            </div>
            <p class="cv2-dsh-kpi-sub" data-v2="hall-sub">最快被抢：{{ stayText(hallStay === null ? null : hallStay.today_fastest_grab_minutes) }} · 最久滞留：{{ stayText(hallStay === null ? null : hallStay.max_stay_minutes) }}</p>
          </template>
        </section>

        <!-- 各渠道同步健康（dashboard/sync-health 真接口；闲管家口径未拍板 → 显示「—」） -->
        <section class="cv2-dsh-card" data-v2="sync">
          <h3 class="cv2-dsh-card-title">各渠道同步健康</h3>
          <p class="cv2-dsh-note">最近心跳与积压</p>
          <p v-if="syncError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="sync-fail">{{ syncError }}</p>
          <div v-else data-v2="sync-rows">
            <div
              v-for="ch in syncChannels"
              :key="ch.name"
              class="cv2-dsh-row cv2-dsh-row--sm"
              :data-v2-sync-row="ch.name"
              :title="ch.pending === null ? '口径待拍板：闲管家发货队列在内存不落库' : undefined"
            >
              <span
                class="cv2-dsh-dot"
                :class="{ 'cv2-dsh-dot--warn': syncStateOf(ch).cls === 'warn' }"
              ></span>
              {{ ch.name }}
              <span class="cv2-dsh-row-val">{{ syncStateOf(ch).note }}</span>
            </div>
          </div>
        </section>

        <!-- App / 小程序（真接口；PV/UV 是微信 T+1 统计） -->
        <section class="cv2-dsh-card" data-v2="app-card">
          <h3 class="cv2-dsh-card-title">App / 小程序</h3>
          <p class="cv2-dsh-note">今日 · 现成接口（app-stats）</p>
          <p v-if="appError !== ''" class="cv2-dsh-note cv2-dsh-fail" data-v2="app-fail">{{ appError }}</p>
          <div v-else-if="appLoading" class="cv2-dsh-skel" data-v2="app-skel"></div>
          <template v-else-if="appStats !== null">
            <div class="cv2-dsh-metrics" data-v2="app-metrics">
              <div>
                <span class="cv2-dsh-note">访问 PV</span>
                <b class="cv2-dsh-stat-num" data-v2="app-pv">{{ appStats.wechat.available && appStats.wechat.totals ? appStats.wechat.totals.visit_pv : '—' }}</b>
              </div>
              <div>
                <span class="cv2-dsh-note">访客 UV</span>
                <b class="cv2-dsh-stat-num" data-v2="app-uv">{{ appStats.wechat.available && appStats.wechat.totals ? appStats.wechat.totals.visit_uv : '—' }}</b>
              </div>
              <div>
                <span class="cv2-dsh-note">新注册</span>
                <b class="cv2-dsh-stat-num" data-v2="app-reg">{{ appStats.registrations.totals.total }}</b>
              </div>
            </div>
            <p class="cv2-dsh-kpi-sub">
              <span data-v2="app-wechat-note">{{ appWechatNote }}</span>
              <span> · 押金冻结 ¥{{ appStats.deposit.total_frozen }} · {{ appStats.deposit.taker_count }} 名打手</span>
            </p>
          </template>
        </section>

        <!-- 代练通（真接口：余额/发布单数/预警线） -->
        <section class="cv2-dsh-card" data-v2="dlt-card">
          <h3 class="cv2-dsh-card-title">代练通</h3>
          <p class="cv2-dsh-note">余额与发布单量 · 现成接口</p>
          <div class="cv2-dsh-metrics" data-v2="dlt-metrics">
            <div>
              <span class="cv2-dsh-note">账户余额</span>
              <b class="cv2-dsh-stat-num" data-v2="dlt-balance">{{ dltBalance === null ? '—' : `¥${dltBalance}` }}</b>
            </div>
            <div>
              <span class="cv2-dsh-note">本月发布</span>
              <b class="cv2-dsh-stat-num" data-v2="dlt-month">{{ dltMonthCount === null ? '—' : `${dltMonthCount} 单` }}</b>
            </div>
            <div>
              <span class="cv2-dsh-note">今日发布</span>
              <b class="cv2-dsh-stat-num" data-v2="dlt-today">{{ dltTodayCount === null ? '—' : `${dltTodayCount} 单` }}</b>
            </div>
          </div>
          <p class="cv2-dsh-kpi-sub" :class="{ 'cv2-dsh-fail': dltReason !== '' }" data-v2="dlt-note">
            {{ dltReason !== '' ? dltReason : dltAlertLine }}
          </p>
        </section>
      </template>

      <!-- 通知与公告：管理员/非管理员都渲染（管理员下是最后一排的第三格） -->
      <section v-if="dashboardAllowed || roleLayout !== 'work'" class="cv2-dsh-card" data-v2="notices">
        <h3 class="cv2-dsh-card-title">通知与公告</h3>
        <div v-if="notices.length > 0" data-v2="notice-rows">
          <div v-for="row in notices" :key="row.key" class="cv2-dsh-nt-row">
            <span class="cv2-dsh-nt-date">{{ row.time }}</span>
            <button v-if="!dashboardAllowed || row.key.startsWith('n:')" type="button" class="cv2-dsh-notice-button"
              @click="openNotice(row)">{{ row.title }}</button>
            <span v-else class="cv2-dsh-nt-plain">{{ row.title }}</span>
          </div>
        </div>
        <p v-else class="cv2-dsh-note">暂时没有通知和公告。</p>
      </section>
    </div>

    <el-dialog
      v-if="selectedNotice"
      model-value
      :title="selectedNotice.title"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="notice-detail"
      @update:model-value="selectedNotice = null"
    ><p v-if="selectedNotice.content" class="cv2-dsh-notice-content">{{ selectedNotice.content }}</p></el-dialog>

    <el-dialog
      v-if="boardModalRow"
      model-value
      :title="`打手详情 · ${boardModalRow.userName}`"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="board-modal"
      @update:model-value="boardModalRow = null"
    >
      <div class="cv2-dsh-mgrid" data-v2="mm-grid">
        <div><span class="cv2-dsh-note">接单量（选中区间）</span><b class="cv2-dsh-mnum" data-v2="mm-count">{{ boardModalRow.orderCount }} 单</b></div>
        <div><span class="cv2-dsh-note">完单率</span><b class="cv2-dsh-mnum" data-v2="mm-rate">{{ boardModalRow.complateRate }}%</b></div>
        <div><span class="cv2-dsh-note">未结算金额</span><b class="cv2-dsh-mnum" data-v2="mm-unsettled">¥{{ boardModalRow.noSettAmount }}</b></div>
        <div><span class="cv2-dsh-note">最近接单</span><b class="cv2-dsh-mnum cv2-dsh-mnum--sm" data-v2="mm-last">{{ boardModalRow.lastRecOrderDate || '无' }}</b></div>
      </div>
      <p class="cv2-dsh-note" data-v2="mm-pending">订单金额 / 在途与问题单 / 近 7 天接单柱 —— 待接口</p>
    </el-dialog>
  </div>
</template>

<style scoped>
.cv2-dsh-notice-button { border: 0; padding: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.cv2-dsh-notice-content { white-space: pre-wrap; overflow-wrap: anywhere; }
.cv2-dsh-grid--worker { grid-template-columns: minmax(0, 1fr); }
.cv2-dsh-grid--worker .cv2-dsh-card { padding: 18px 20px; }
/* 环形图例按 Metabase 复刻规格重排（carbon.css 的 .cv2-dsh-legend* 基础形不动，本页覆盖视觉）：
   8px 圆角点(32%) + 4px + 加粗名字 + 右对齐百分比列；行距 4px；最大宽 200px；点行显隐该切片。 */
.cv2-dsh-legend {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  max-width: 200px;
}
.cv2-dsh-legend-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.cv2-dsh-legend-row--off {
  opacity: 0.35;
}
.cv2-dsh-legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 32%;
}
.cv2-dsh-legend-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 700;
  font-size: 13px;
  color: var(--tt2-ink);
}
.cv2-dsh-legend-val {
  flex: none;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
</style>
