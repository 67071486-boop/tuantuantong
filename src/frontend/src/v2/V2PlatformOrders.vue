<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「第三方平台订单管理」v2（`/v2/platform-orders`；侧栏「订单管理 → 第三方平台订单管理」进）。
 *
 * 旧页 = island 复刻页 `order-coreordercenter.ts`（路由 `orders-platform-core`）。本页按同一套
 * 功能重建为炭火页，布局孪生 V2OrderManage（页头 bare → 页签条 → 筛选 → 工具条 → 表格面板 → 弹窗群）：
 *
 * | 旧页功能 | 本页落点 |
 * |---|---|
 * | 14 页签 | 只画**有数据源的 9 个**（TAB_STATUS_MAP 同映射；5 个无码页签不画，登记 DRAFT） |
 * | 页签计数 | `fetchPlatformPushCounts` 一次填齐；失败「—」+ toast；counts=推送行口径（与聚合 total 有差，不修） |
 * | 筛选 | 只有**关键词**接真（匹配我方单号/平台单号/来源单号）；其余死控件不搬 |
 * | 工具栏 | 刷新、批量撤回/验收/仲裁及同步交易状态；平台写操作按权限显示 |
 * | 24 列表 | 聚合折叠（`pushes>=2` 父行、展开零请求）；父行取**最新一条**推送（created_at/id 降序，不看 is_active）；扁平回包退扁平 |
 * | 行内 4 颗 | 编辑/撤回/协商撤销/加款，可见性照 ROW_ACTION_STATUSES；无权限整列「—」 |
 * | 剩余/验收倒计 | 本地秒级 tick（setInterval + onUnmounted 清理），⛔ 不发请求 |
 *
 * ⛔ 不 import layui-island/**（会把 layui 装载链拉进 v2 模块图）；v1 的函数与文案逐条照抄、
 * 本地重写（色相意图 → v2 token 档，见 DISP_STATUS_CLASS 注释）；类型全走 `@/api/dailiantongPush`。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  DAILIANTONG_PLATFORM,
  addOrderPlatformPushAmount,
  editOrderPlatformPush,
  fetchPlatformPushCounts,
  fetchPlatformPushGroups,
  fetchPlatformPushes,
  isGroupedPushItem,
  newPushActionRequestId,
  runPlatformPushAction,
  withdrawOrderPlatformPush,
  type DailiantongPushRow,
  type PlatformPushActionPayload,
  type PlatformPushBatchAction,
  type PlatformPushCounts,
  type PlatformPushGroupItem,
  type PlatformPushListItem,
  type PlatformPushListParams,
  type PlatformPushListRow,
  type PlatformPushSubRow,
} from '@/api/dailiantongPush'
import { AuthExpiredError, BizError, HttpError, NetworkError } from '@/api/errors'
import {
  createOrderNote,
  fetchOrder,
  fetchOrderAudit,
  isOrderNoShape,
  type AuditEntry,
  type Order,
} from '@/api/orders'
import {
  ORDER_STATUS_VALUES,
  V2_ORDER_STATUS_LABELS,
  v2OrderStatusLabel,
  type OrderStatus,
} from '@/config/orderStatus'
import { PLATFORMS } from '@/config/platforms'
import { useSessionStore } from '@/stores/session'
import { useColumnWidths } from '@/v2/tableResize'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2DetailFace from '@/v2/V2DetailFace.vue'

const session = useSessionStore()

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面自绘 toast，同 V2OrderManage；ElMessage 拿不到 .ttv2 皮）────────── */

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

/* ── 权限门（fail-closed；行内 4 颗与工具栏 7 颗全是平台写动作）──────────────── */

const canPlatformWrite = computed(() => session.canNode(['order.platform_write']))
const canAddLog = computed(() => session.canNode(['order.add_log']))

/* ── 文案与判据（出处 = island order-coreordercenter.ts 同名常量；本地重写不引模块）── */

const EMPTY_VALUE = '—'
const FIXED_PLATFORM = DAILIANTONG_PLATFORM
/** 没勾选就点批量按钮的提示（逐字照 v1 SELECT_REQUIRED_MSG）。 */
const SELECT_REQUIRED_MSG = '请选择要操作的记录！'
const LOAD_FAILED_PREFIX = '加载失败：'
const TAB_COUNTS_FAILED_PREFIX = '页签数字加载失败：'
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'

/** 页签（key 沿 v1 lay-id）→ 后端 `remote_status`；「全部」不带 status（v1 TAB_STATUS_MAP 同表）。 */
type PlatformTab = { key: string; label: string; status?: number }
const TABS: readonly PlatformTab[] = [
  { key: '0', label: '全部' },
  { key: '10', label: '待接单', status: 11 },
  { key: '20', label: '正在代练', status: 12 },
  { key: '25', label: '已锁定', status: 15 },
  { key: '30', label: '订单异常', status: 14 },
  { key: '40', label: '等待验收', status: 13 },
  { key: '50', label: '撤销中', status: 16 },
  { key: '60', label: '已结算', status: 17 },
  { key: '75', label: '已下架', status: 18 },
]

const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]
const page = ref(1)
const pageSize = ref(30)

/* ── 综合状态（交易状态）：表与推导逐条照抄 v1 `platformPush.ts`（⛔ 不能 import island ⇒ 本地镜像）；
 * 色档按 v1 色相意图落 v2 token：灰→--unassigned / 绿→--doing / 蓝→--done / 红→--error /
 * 橙→--accept、已结算黑档→--settled 实底绿签（对照表登记 docs/FRONTEND-V2-DRAFT.md）。 */
const DISPLAY_STATUS_LABELS: Readonly<Record<string, string>> = {
  temp_order: '临时订单',
  unaccepted: '未接手',
  in_progress: '正在代练',
  waiting_review: '等待验收',
  abnormal: '订单异常',
  locked: '已锁定',
  cancelling: '撤销中',
  settled: '已结算',
  revoked: '已撤销',
  force_revoked: '已强制撤销',
  arbitrating: '客服介入中',
  off_shelf: '已下架',
}

const TOKEN_BY_REMOTE_STATUS: Readonly<Record<number, string>> = {
  10: 'temp_order',
  11: 'unaccepted',
  12: 'in_progress',
  13: 'waiting_review',
  14: 'abnormal',
  15: 'locked',
  16: 'cancelling',
  17: 'settled',
  18: 'off_shelf',
}

const PUSH_STATE_LABELS: Readonly<Record<string, string>> = {
  pending: '发送中',
  sent: '已发出',
  failed: '发送失败',
  unknown: '状态未知',
  closed: '已关闭',
}

/** display_status token → v2 状态签档（色相出处 = v1 DISPLAY_STATUS_BADGE_CLASSES）。 */
const DISP_STATUS_CLASS: Readonly<Record<string, string>> = {
  temp_order: 'cv2-st--unassigned',
  unaccepted: 'cv2-st--unassigned',
  revoked: 'cv2-st--unassigned',
  force_revoked: 'cv2-st--unassigned',
  off_shelf: 'cv2-st--unassigned',
  closed: 'cv2-st--unassigned',
  in_progress: 'cv2-st--doing',
  waiting_review: 'cv2-st--done',
  abnormal: 'cv2-st--error',
  locked: 'cv2-st--error',
  cancelling: 'cv2-st--accept',
  arbitrating: 'cv2-st--accept',
  settled: 'cv2-st--settled',
}

/** 本地推送 state 在综合状态缺席时的兜底档（v1：failed 红 / unknown 橙 / 其余灰）。 */
const PUSH_STATE_CLASS: Readonly<Record<string, string>> = {
  failed: 'cv2-st--error',
  unknown: 'cv2-st--accept',
}

type PushStatusFields = Pick<PlatformPushSubRow, 'state' | 'remote_status' | 'remote_cancel_status'> & {
  display_status?: string | null
  display_status_label?: string | null
}

/** 综合状态 `{token, label}`；没有任何可判状态 ⇒ null（画「—」）。优先级照 v1 platformDisplayStatus。 */
function displayStatusOf(push: PushStatusFields): { token: string; label: string } | null {
  if (push.state === 'closed') return { token: 'closed', label: '已关闭' }
  const backendToken = typeof push.display_status === 'string' ? push.display_status.trim() : ''
  if (backendToken !== '') {
    const backendLabel =
      typeof push.display_status_label === 'string' ? push.display_status_label.trim() : ''
    return {
      token: backendToken,
      label: backendLabel !== '' ? backendLabel : (DISPLAY_STATUS_LABELS[backendToken] ?? backendToken),
    }
  }
  const status = push.remote_status
  const cancel = push.remote_cancel_status
  if (status === 17) return { token: 'settled', label: '已结算' }
  if (cancel === 12 || cancel === 15) return { token: 'revoked', label: '已撤销' }
  if (cancel === 16) return { token: 'force_revoked', label: '已强制撤销' }
  if (cancel === 11) return { token: 'cancelling', label: '撤销中' }
  if (cancel === 14) return { token: 'arbitrating', label: '客服介入中' }
  const token = status === null ? undefined : TOKEN_BY_REMOTE_STATUS[status]
  if (token === undefined) return null
  return { token, label: DISPLAY_STATUS_LABELS[token] ?? token }
}

/** 「交易状态」格的签（label + v2 档）；无可判状态时按本地 state 兜底（failed 红 / unknown 橙）。 */
function dispStatusBadge(push: PushStatusFields): { label: string; cls: string } | null {
  const display = displayStatusOf(push)
  if (display !== null) {
    return { label: display.label, cls: DISP_STATUS_CLASS[display.token] ?? 'cv2-st--unassigned' }
  }
  const label = PUSH_STATE_LABELS[push.state] ?? push.state
  if (label === '') return null
  return { label, cls: PUSH_STATE_CLASS[push.state] ?? 'cv2-st--unassigned' }
}

/** 行内按钮的可见性判据 token（v1：`platformDisplayStatus(push)?.token ?? ''`）。 */
function pushTokenOf(push: PushStatusFields): string {
  return displayStatusOf(push)?.token ?? ''
}

/* ── 系统状态列：code → v2 中文 + 九状态档（全站同状态同色；纯展示不可点）────── */

/* ── 展示函数（v1 同名函数的本地重写；纯展示、缺键一律「—」）────────────────── */

function formatDateTime(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  return value.replace('T', ' ').slice(0, 16)
}

function acceptPriceText(value: string | null | undefined): string {
  const text = (value ?? '').trim()
  return text === '' ? EMPTY_VALUE : text
}

function acceptUserText(value: string | null | undefined): string {
  const text = (value ?? '').trim()
  return text === '' ? EMPTY_VALUE : text
}

function countText(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : EMPTY_VALUE
}

function custCancelAlert(value: number | null | undefined): boolean {
  return typeof value === 'number' && value > 0
}

function orderSourceText(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const text = String(value).trim()
  return text === '' ? EMPTY_VALUE : text
}

function sourceOrderNoOf(row: { order_no: string; source_order_no?: string }): string {
  const source = (row.source_order_no ?? '').trim()
  return source !== '' ? source : row.order_no
}

/** 金额字符串 → 整数分（非两位小数以内 ⇒ null）；用分算差值避开浮点尾数（v1 moneyToCents）。 */
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
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(Math.round(cents))
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** 利润 = 接单价 − 发单价；缺一边显示「—」。 */
function profitTextOf(
  acceptPrice: string | null | undefined,
  pushPrice: string | null | undefined,
): string {
  const accept = moneyToCents(acceptPrice)
  const push = moneyToCents(pushPrice)
  if (accept === null || push === null) return EMPTY_VALUE
  return centsToMoney(accept - push)
}

/** 非负、最多两位小数（「可为 0」）（v1 isValidMoneyText）。 */
function isValidMoneyText(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value)
}

/** 标记：非空且 ≤128（v1 isValidTagText）。 */
function isValidTagText(value: string): boolean {
  return value.trim() !== '' && value.trim().length <= 128
}

/** 加款：整数元、≥1（v1 isValidAmountYuan）。 */
function isValidAmountYuan(value: string): boolean {
  if (!/^\d+$/.test(value)) return false
  return Number(value) >= 1
}

/** 平台 id → 中文（字典唯一出处 `@/config/platforms`；未知 id 原样标「平台 N」，v1 platformLabelOf 同款）。 */
function platformLabelOf(id: number): string {
  return PLATFORMS.find((item) => item.id === id)?.label ?? `平台 ${id}`
}

function groupPlatformCountText(pushes: readonly PlatformPushSubRow[]): string {
  if (pushes.length === 0) return EMPTY_VALUE
  const labels: string[] = []
  for (const push of pushes) {
    const label = platformLabelOf(push.platform)
    if (!labels.includes(label)) labels.push(label)
  }
  return `${labels.join('、')} ${pushes.length} 条`
}

function groupCountTitle(count: number): string {
  return `该订单下有 ${count} 条平台单，点左侧三角展开`
}

/** 「平台交易单号」格的悬停提示（照 v1 platformOrderHint：最近同步/状态/错误，不新增列）。 */
function serialHintOf(push: {
  state: string
  last_synced_at?: string | null
  last_error?: string
}): string {
  const parts = [
    `最近同步：${formatDateTime(push.last_synced_at ?? null)}`,
    `状态：${PUSH_STATE_LABELS[push.state] ?? push.state}`,
  ]
  const lastError = push.last_error ?? ''
  if (lastError !== '') parts.push(`错误：${lastError}`)
  return parts.join(' · ')
}

/** 聚合父行的目标推送 = **最新一条**（created_at 降序、id 降序，⛔ 不看 is_active；v1 groupStatusPush）。 */
function groupStatusPush(pushes: readonly PlatformPushSubRow[]): PlatformPushSubRow | null {
  if (pushes.length === 0) return null
  const timeOf = (push: PlatformPushSubRow): number => {
    const parsed = push.created_at === undefined ? Number.NaN : Date.parse(push.created_at)
    return Number.isNaN(parsed) ? Number.NaN : parsed
  }
  return (
    [...pushes].sort((a, b) => {
      const left = timeOf(a)
      const right = timeOf(b)
      const byId = (b.id ?? 0) - (a.id ?? 0)
      if (Number.isNaN(left) && Number.isNaN(right)) return byId
      if (Number.isNaN(left)) return 1
      if (Number.isNaN(right)) return -1
      if (left !== right) return right - left
      return byId
    })[0] ?? null
  )
}

/** 扁平推送行 → 子行形状（行内动作 / 子行渲染要的键；缺键按原值带过去，不补假值）。 */
function subRowOfListRow(row: PlatformPushListRow): PlatformPushSubRow {
  return {
    platform: row.platform,
    serial_no: row.serial_no,
    state: row.state,
    remote_status: row.remote_status,
    remote_cancel_status: row.remote_cancel_status,
    display_status: row.display_status,
    display_status_label: row.display_status_label,
    price: row.price,
    pay_level_bal: row.pay_level_bal,
    rep_ensure_bal: row.rep_ensure_bal,
    is_active: row.is_active,
    id: row.id,
    created_at: row.created_at,
    last_synced_at: row.last_synced_at,
    last_error: row.last_error,
    accept_price: row.accept_price,
    order_source: row.order_source,
    verify_deadline_at: row.verify_deadline_at,
    verify_remaining_seconds: row.verify_remaining_seconds,
    accept_user_name: row.accept_user_name,
    accept_uid: row.accept_uid,
    accept_over_count: row.accept_over_count,
    cust_cancel_count: row.cust_cancel_count,
    remote_accepted_at: row.remote_accepted_at,
    remaining_seconds: row.remaining_seconds,
  }
}

/* ── 倒计时（本地秒级 tick；⛔ 不重算时间戳、⛔ 不发请求；v1 同口径）──────────── */

const nowTick = ref(Date.now())

/** 现在还剩多少秒：① 绝对截止时间优先；② 抓取时刻快照秒数 − 已流逝；③ 都没有 ⇒ null（v1 verifyRemainingSeconds）。 */
function verifyRemainingSeconds(
  deadline: string | null | undefined,
  baseSeconds: number | null | undefined,
  fetchedAtMs: string | null | undefined,
  nowMs: number,
): number | null {
  const deadlineText = (deadline ?? '').trim()
  if (deadlineText !== '') {
    const deadlineMs = Date.parse(deadlineText)
    if (!Number.isNaN(deadlineMs)) return Math.round((deadlineMs - nowMs) / 1000)
  }
  if (typeof baseSeconds === 'number' && Number.isFinite(baseSeconds)) {
    const fetchedAt =
      fetchedAtMs === null || fetchedAtMs === undefined || fetchedAtMs.trim() === ''
        ? null
        : Number(fetchedAtMs)
    const elapsed = fetchedAt === null || !Number.isFinite(fetchedAt) ? 0 : (nowMs - fetchedAt) / 1000
    return Math.round(baseSeconds - elapsed)
  }
  return null
}

/** 「剩余时间」列（直接用后端秒数；null ⇒「—」；≤0 ⇒「已超时」红；>1 天换天）（v1 formatShoursRemaining）。 */
function shoursParts(row: RenderRow): { text: string; overdue: boolean } {
  if (row.remBase === null) return { text: EMPTY_VALUE, overdue: false }
  const elapsed = (nowTick.value - row.fetchedAt) / 1000
  const total = Math.floor(row.remBase - elapsed)
  if (total <= 0) return { text: '已超时', overdue: true }
  if (total > 86400) {
    const days = Math.floor(total / 86400)
    const hours = Math.floor((total % 86400) / 3600)
    return { text: `剩 ${days} 天 ${hours} 小时`, overdue: false }
  }
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return { text: `剩 ${hours} 小时 ${minutes} 分`, overdue: false }
}

/** 「验收倒计」列（过期红：`已超时 X 小时 Y 分`）（v1 formatVerifyCountdown）。 */
function verifyParts(row: RenderRow): { text: string; overdue: boolean } {
  const remaining = verifyRemainingSeconds(
    row.verifyDeadline,
    row.verifyBase,
    String(row.fetchedAt),
    nowTick.value,
  )
  if (remaining === null) return { text: EMPTY_VALUE, overdue: false }
  const overdue = remaining <= 0
  const abs = Math.abs(Math.trunc(remaining))
  const hours = Math.floor(abs / 3600)
  const minutes = Math.floor((abs % 3600) / 60)
  return {
    text: overdue ? `已超时 ${hours} 小时 ${minutes} 分` : `剩 ${hours} 小时 ${minutes} 分`,
    overdue,
  }
}

/* ── 行渲染模型：父行 / 子行 / 扁平行统一成一种格子来源 ─────────────────────── */

type RenderRow = {
  key: string
  isChild: boolean
  orderNo: string
  viewNo: string
  orderStatus: string
  serverText: string
  /** ≥2 ⇒ 可折叠父行（画三角）；null = 单条平铺。 */
  groupSize: number | null
  serialText: string
  serialTitle: string
  /** 这一行展示/操作作用的推送（父行 = 最新一条；子行 = 自己；0 条组 = null）。 */
  push: PlatformPushSubRow | null
  remBase: number | null
  fetchedAt: number
  verifyDeadline: string
  verifyBase: number | null
  children: RenderRow[]
}

function pushCellsOf(push: PlatformPushSubRow | null, now: number) {
  return {
    push,
    remBase: typeof push?.remaining_seconds === 'number' ? Math.floor(push.remaining_seconds) : null,
    fetchedAt: now,
    verifyDeadline: (push?.verify_deadline_at ?? '').trim(),
    verifyBase:
      typeof push?.verify_remaining_seconds === 'number' ? push.verify_remaining_seconds : null,
  }
}

function childRowOfPush(orderNo: string, push: PlatformPushSubRow, index: number, now: number): RenderRow {
  const serial = push.serial_no === null || push.serial_no === '' ? EMPTY_VALUE : push.serial_no
  return {
    key: `c:${orderNo}:${index}`,
    isChild: true,
    orderNo,
    viewNo: EMPTY_VALUE,
    orderStatus: '',
    serverText: EMPTY_VALUE,
    groupSize: null,
    serialText: serial,
    serialTitle: serialHintOf(push),
    ...pushCellsOf(push, now),
    children: [],
  }
}

function parentRowOfGroup(group: PlatformPushGroupItem, now: number): RenderRow {
  const server =
    group.server === null || group.server === '' ? group.game : `${group.game} / ${group.server}`
  const collapsible = group.pushes.length >= 2
  const latest = groupStatusPush(group.pushes)
  const single = group.pushes.length === 1 ? group.pushes[0] : undefined
  const serialText = collapsible
    ? groupPlatformCountText(group.pushes)
    : single === undefined
      ? EMPTY_VALUE
      : (single.serial_no === null || single.serial_no === '' ? EMPTY_VALUE : single.serial_no)
  const serialTitle = collapsible
    ? groupCountTitle(group.pushes.length)
    : single === undefined
      ? ''
      : serialHintOf(single)
  return {
    key: `o:${group.order_no}`,
    isChild: false,
    orderNo: group.order_no,
    viewNo: sourceOrderNoOf(group),
    orderStatus: group.order_status,
    serverText: server,
    groupSize: collapsible ? group.pushes.length : null,
    serialText,
    serialTitle,
    ...pushCellsOf(latest, now),
    children: group.pushes.map((p, i) => childRowOfPush(group.order_no, p, i, now)),
  }
}

function flatRowOfListRow(row: PlatformPushListRow, now: number): RenderRow {
  const push = subRowOfListRow(row)
  const server =
    row.server === null || row.server === '' ? row.game : `${row.game} / ${row.server}`
  const serial = row.serial_no === null || row.serial_no === '' ? EMPTY_VALUE : row.serial_no
  return {
    key: `f:${row.id}`,
    isChild: false,
    orderNo: row.order_no,
    viewNo: sourceOrderNoOf(row),
    orderStatus: row.order_status,
    serverText: server,
    groupSize: null,
    serialText: serial,
    serialTitle: serialHintOf(push),
    ...pushCellsOf(push, now),
    children: [],
  }
}

/* ── 页签与计数（counts 一次取回；失败「—」+ toast，⛔ 不写 0）──────────────── */

const activeTab = ref('0')
const counts = ref<PlatformPushCounts | null>(null)

function tabCountText(tab: PlatformTab): string {
  const c = counts.value
  if (c === null) return EMPTY_VALUE
  if (tab.status === undefined) return typeof c.total === 'number' ? String(c.total) : EMPTY_VALUE
  const raw = c.counts[String(tab.status)]
  return typeof raw === 'number' ? String(raw) : EMPTY_VALUE
}

async function loadCounts(): Promise<void> {
  const kw = filters.keyword.trim()
  try {
    counts.value = await fetchPlatformPushCounts({
      platform: FIXED_PLATFORM,
      ...(kw === '' ? {} : { keyword: kw }),
    })
  } catch (e) {
    // 先复位再填（v1 同口径）：失败的轮次全部页签保持「—」。
    counts.value = null
    showToast(`${TAB_COUNTS_FAILED_PREFIX}${errText(e)}`, 'error')
  }
}

/* ── 筛选（后端只收 platform/status/keyword/分页 ⇒ 只有关键词接真）──────────── */

const filters = reactive({ keyword: '' })

function buildParams(targetPage: number): PlatformPushListParams {
  const params: PlatformPushListParams = {
    platform: FIXED_PLATFORM,
    page: targetPage,
    page_size: pageSize.value,
  }
  const tab = TABS.find((t) => t.key === activeTab.value)
  if (tab !== undefined && tab.status !== undefined) params.status = tab.status
  const kw = filters.keyword.trim()
  if (kw !== '') params.keyword = kw
  return params
}

/* ── 取数（默认按订单聚合；后端未升级时 BizError ⇒ 退扁平，v1 fetchPage 同口径）── */

const rows = ref<RenderRow[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const expandedNos = ref<Set<string>>(new Set())

async function fetchPage(
  params: PlatformPushListParams,
): Promise<{ items: PlatformPushListItem[]; total: number; grouped: boolean }> {
  try {
    const result = await fetchPlatformPushGroups(params)
    const items = result.items as PlatformPushListItem[]
    const grouped = items.length > 0 && items.every(isGroupedPushItem)
    return { items, total: result.total, grouped }
  } catch (error) {
    if (!(error instanceof BizError)) throw error
    const result = await fetchPlatformPushes(params)
    return { items: result.items, total: result.total, grouped: false }
  }
}

async function reload(targetPage = 1): Promise<void> {
  loading.value = true
  try {
    const result = await fetchPage(buildParams(targetPage))
    const now = Date.now()
    // 每次重取先清子行 ⇒ 折叠状态复位（照 v1）。
    expandedNos.value = new Set()
    rows.value = result.grouped
      ? (result.items as PlatformPushGroupItem[]).map((g) => parentRowOfGroup(g, now))
      : (result.items as PlatformPushListRow[]).map((r) => flatRowOfListRow(r, now))
    total.value = result.total
    page.value = targetPage
    loadError.value = null
  } catch (e) {
    rows.value = []
    total.value = 0
    loadError.value = errText(e)
    showToast(`${LOAD_FAILED_PREFIX}${loadError.value}`, 'error')
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

function onTabClick(tab: PlatformTab): void {
  if (tab.key === activeTab.value) return
  activeTab.value = tab.key
  void reload(1)
}

function onSearch(): void {
  // 搜索条件变了 ⇒ 旧计数不再可信：先复位成「—」，再用一次 counts 请求填齐（v1 同口径）。
  counts.value = null
  void reload(1)
  void loadCounts()
}

function onRefresh(): void {
  void reload(page.value)
  void loadCounts()
}

function resetFilters(): void {
  filters.keyword = ''
  counts.value = null
  void reload(1)
  void loadCounts()
}

function toggleChildren(orderNo: string): void {
  const next = new Set(expandedNos.value)
  if (next.has(orderNo)) next.delete(orderNo)
  else next.add(orderNo)
  expandedNos.value = next
}

/** 渲染列表 = 父行 + （展开了的）子行；展开零请求（子单来自同一发聚合回包）。 */
const renderRows = computed<RenderRow[]>(() => {
  const out: RenderRow[] = []
  for (const row of rows.value) {
    out.push(row)
    if (row.groupSize !== null && expandedNos.value.has(row.orderNo)) {
      out.push(...row.children)
    }
  }
  return out
})

/* ── 勾选（只收集父行 order_no；子行不设勾选框）────────────────────────────── */

const checkedNos = ref<string[]>([])

function isChecked(no: string): boolean {
  return checkedNos.value.includes(no)
}

function toggleRow(no: string): void {
  const i = checkedNos.value.indexOf(no)
  if (i === -1) checkedNos.value.push(no)
  else checkedNos.value.splice(i, 1)
}

const pageAllChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => isChecked(r.orderNo)),
)

function togglePageAll(): void {
  if (pageAllChecked.value) {
    checkedNos.value = checkedNos.value.filter((no) => !rows.value.some((r) => r.orderNo === no))
  } else {
    for (const r of rows.value) {
      if (!isChecked(r.orderNo)) checkedNos.value.push(r.orderNo)
    }
  }
}

/* ── 列宽拖拽 + 列显隐（24 列架子；宽值照 v1 列定义比例）────────────────────── */

const PLATFORM_COLUMNS = [
  'check', 'serial', 'sysno', 'game', 'platform', 'price', 'accept', 'profit', 'remaining',
  'dispstatus', 'accepter', 'orderstatus', 'over', 'cancel', 'remoteacc', 'verify', 'source',
  'created', 'rectime', 'completetime', 'createtime', 'revoketime', 'pushman', 'actions',
] as const

const COL_TITLES: Record<string, string> = {
  serial: '平台交易单号',
  sysno: '系统单号',
  game: '游戏区服',
  platform: '发单平台',
  price: '发单价',
  accept: '接单价',
  profit: '利润',
  remaining: '剩余时间',
  dispstatus: '交易状态',
  accepter: '接手人',
  orderstatus: '系统状态',
  over: '完成单数',
  cancel: '投诉/取消',
  remoteacc: '接单时间',
  verify: '验收倒计',
  source: '订单来源',
  created: '发单时间',
  rectime: '接手时间',
  completetime: '完单时间',
  createtime: '录单时间',
  revoketime: '撤销时间',
  pushman: '发单人',
  actions: '操作',
}

/** 默认藏：五个无数据源的恒「—」列（保留列位，配列显隐）。 */
const HIDDEN_COLS_DEFAULT = ['rectime', 'completetime', 'createtime', 'revoketime', 'pushman']
const HIDDEN_COLS_KEY = 'tt.v2.platform-orders.hidden-cols.v1'

function loadHiddenCols(): string[] {
  try {
    const raw = localStorage.getItem(HIDDEN_COLS_KEY)
    if (raw === null) return [...HIDDEN_COLS_DEFAULT]
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return [...HIDDEN_COLS_DEFAULT]
    return parsed.filter(
      (k): k is string => typeof k === 'string' && k in COL_TITLES && k !== 'actions',
    )
  } catch {
    return [...HIDDEN_COLS_DEFAULT]
  }
}

const hiddenCols = ref<string[]>(loadHiddenCols())

function saveHiddenCols(): void {
  try {
    localStorage.setItem(HIDDEN_COLS_KEY, JSON.stringify(hiddenCols.value))
  } catch { /* 存储禁写：本次会话仍生效 */ }
}

function colVisible(key: string): boolean {
  return !hiddenCols.value.includes(key)
}

const visibleColumns = computed(() => PLATFORM_COLUMNS.filter((k) => colVisible(k)))

const visibleColKeys = computed<string[]>({
  get: () => (Object.keys(COL_TITLES) as string[]).filter((k) => colVisible(k)),
  set: (keys) => {
    hiddenCols.value = (Object.keys(COL_TITLES) as string[]).filter((k) => !keys.includes(k))
    saveHiddenCols()
  },
})

const { colStyle, startResize, resetColumn } = useColumnWidths('platform-orders', {
  check: 36,
  serial: 180,
  sysno: 190,
  game: 130,
  platform: 76,
  price: 76,
  accept: 76,
  profit: 80,
  remaining: 110,
  dispstatus: 100,
  accepter: 90,
  over: 80,
  cancel: 90,
  remoteacc: 130,
  orderstatus: 96,
  verify: 130,
  source: 100,
  created: 130,
  rectime: 130,
  completetime: 130,
  createtime: 130,
  revoketime: 130,
  pushman: 90,
  actions: 110,
})

/* ── 工具栏 11 颗批量（照 v1 TOOLBAR_ACTION_FILTERS / 文案逐字）──────────────── */

type ToolbarButton = { action: PlatformPushBatchAction; label: string; gated: boolean }

/** gated=true = 交易平台写动作（挂 order.platform_write，v1 PLATFORM_WRITE_TOOLBAR_FILTERS 七颗）；其余四颗照 v1 不挂节点（后端 403 兜底）。 */
const TOOLBAR_BUTTONS: readonly ToolbarButton[] = [
  { action: 'withdraw', label: '批量撤回', gated: true },
  { action: 'accept', label: '批量验收', gated: true },
  { action: 'arbitrate', label: '批量仲裁', gated: true },
  { action: 'sync', label: '同步交易状态', gated: false },
  { action: 'change_status', label: '改系统状态', gated: false },
  { action: 'refund', label: '记退款', gated: false },
  { action: 'set_label', label: '更改标签', gated: true },
  { action: 'set_tag', label: '改标记', gated: true },
  { action: 'clear_tag', label: '删标记', gated: true },
  { action: 'urgent', label: '加急', gated: true },
  { action: 'change_taker_price', label: '改接单价', gated: false },
]

const TOOLBAR_ACTION_LABELS = Object.fromEntries(
  TOOLBAR_BUTTONS.map((b) => [b.action, b.label]),
) as Record<PlatformPushBatchAction, string>

const VISIBLE_TOOLBAR_ACTIONS = new Set<PlatformPushBatchAction>(['withdraw', 'accept', 'arbitrate', 'sync'])
const visibleToolbarButtons = TOOLBAR_BUTTONS.filter((button) => VISIBLE_TOOLBAR_ACTIONS.has(button.action))

/* 确认文案（逐字照 v1 同名常量）。 */
const WITHDRAW_CONFIRM_TEXT =
  '未接手的单会直接删除并即时退款；已被接手的单只能申请协商撤销，能否撤回由对方决定。确定要撤回这些订单吗？'
const ACCEPT_CONFIRM_TEXT =
  '确认完单会按订单价从代练通余额扣款，且不可撤销。代练通确认结算后，团团通会同步为已完成。确定要验收这些订单吗？'
const SYNC_CONFIRM_TEXT =
  '会立即逐笔读取代练通的最新交易状态并写回本地（不改变远端）。确定要同步这些订单的交易状态吗？'
const URGENT_CONFIRM_TEXT =
  '加急只是本地标记，不会改动代练通上的发布时间。确定把这些订单设为加急吗？'
const CLEAR_TAG_CONFIRM_TEXT = '确定要清除这些订单的自定义标记吗？'
const STATUS_DIALOG_NOTE =
  '改系统状态只改本地，不会改代练通；已抛到代练通且远端尚未结束的订单会被拒绝。'
const REFUND_PROMPT_TITLE =
  '请输入累计退款总额（不是本次新增；只有「已完成」的订单能改，最多两位小数）'
const REFUND_REASON = '第三方平台订单管理页记退款'
const TAKER_PRICE_PROMPT_TITLE = '更改接单价，可为0，但不能为空'
const TAG_PROMPT_TITLE = '请输入自定义标记（最多 128 字；要清空请用「删标记」）'
const LABEL_PROMPT_TITLE = '请输入订单标签（留空表示清除标签）'
const ARBITRATION_PROMPT_TITLE =
  '请输入申请仲裁理由（最多 100 字；本版只在本地记「待仲裁」，不会提交到代练通）'
const ARBITRATION_CONFIRM_TEXT =
  '确定把这些订单标记为「待仲裁」吗？本版只在本地订单日志记一条，不会提交到代练通；请到代练通后台申请客服介入。'

/** 在途闸：同一次批量动作只发一发（连点不会重复扣款 / 重复退款）。 */
const actionBusy = ref(false)

/** 通用确认层（批量第二步 / 行内撤回 / 加款；文案逐字照 v1）。 */
const confirmDlg = ref<null | { title: string; text: string; run: () => Promise<void> }>(null)

/** 输入层（仲裁理由 / 改状态 / 退款 / 标签 / 标记 / 接单价；确定 = v1 的「校验 → 确认框」）。 */
type BatchDlg = {
  action: PlatformPushBatchAction
  title: string
  inputKind: 'none' | 'text' | 'area' | 'status'
  inputLabel: string
  inputValue: string
  inputMax: number
  radioValue: string
  /** 退款幂等键：每次打开弹窗生成一个，同一批复用（D-0312 口径）。 */
  requestId: string
  orderNos: string[]
}
const batchDlg = ref<BatchDlg | null>(null)

function openBatchDlg(action: PlatformPushBatchAction, orderNos: string[]): void {
  const base: BatchDlg = {
    action,
    title: TOOLBAR_ACTION_LABELS[action],
    inputKind: 'none',
    inputLabel: '',
    inputValue: '',
    inputMax: 0,
    radioValue: '',
    requestId: '',
    orderNos,
  }
  switch (action) {
    case 'withdraw':
      confirmDlg.value = { title: base.title, text: WITHDRAW_CONFIRM_TEXT, run: async () => { await runBatchAction(action, orderNos, {}) } }
      return
    case 'accept':
      confirmDlg.value = { title: base.title, text: ACCEPT_CONFIRM_TEXT, run: async () => { await runBatchAction(action, orderNos, {}) } }
      return
    case 'sync':
      confirmDlg.value = { title: base.title, text: SYNC_CONFIRM_TEXT, run: async () => { await runBatchAction(action, orderNos, {}) } }
      return
    case 'urgent':
      confirmDlg.value = { title: base.title, text: URGENT_CONFIRM_TEXT, run: async () => { await runBatchAction(action, orderNos, { urgent: true }) } }
      return
    case 'clear_tag':
      confirmDlg.value = { title: base.title, text: CLEAR_TAG_CONFIRM_TEXT, run: async () => { await runBatchAction(action, orderNos, {}) } }
      return
    case 'arbitrate':
      batchDlg.value = { ...base, inputKind: 'area', inputLabel: ARBITRATION_PROMPT_TITLE, inputMax: 100 }
      return
    case 'change_status':
      // 说明（STATUS_DIALOG_NOTE）与 8 状态单选同层；确定直接提交（v1 单层同口径）。
      batchDlg.value = { ...base, inputKind: 'status' }
      return
    case 'refund':
      // 幂等键每次打开弹窗生成一个（弹窗内复用 ⇒ 重复点确定不会记两笔）。
      batchDlg.value = { ...base, inputKind: 'text', inputLabel: REFUND_PROMPT_TITLE, inputMax: 12, requestId: newPushActionRequestId() }
      return
    case 'set_label':
      batchDlg.value = { ...base, inputKind: 'text', inputLabel: LABEL_PROMPT_TITLE, inputMax: 64 }
      return
    case 'set_tag':
      batchDlg.value = { ...base, inputKind: 'text', inputLabel: TAG_PROMPT_TITLE, inputMax: 128 }
      return
    case 'change_taker_price':
      batchDlg.value = { ...base, inputKind: 'text', inputLabel: TAKER_PRICE_PROMPT_TITLE, inputMax: 12 }
      return
  }
}

function onToolbarAction(action: PlatformPushBatchAction): void {
  if (actionBusy.value) return
  const orderNos = rows.value.filter((r) => isChecked(r.orderNo)).map((r) => r.orderNo)
  if (orderNos.length === 0) {
    showToast(SELECT_REQUIRED_MSG)
    return
  }
  openBatchDlg(action, orderNos)
}

/** 输入层的「确定」：先页面校验（v1 同文案），再进确认框（后果文案逐字照 v1）。 */
function confirmBatch(): void {
  const dlg = batchDlg.value
  if (dlg === null) return
  const { action, orderNos } = dlg
  const value = dlg.inputValue.trim()

  if (action === 'change_status') {
    if (!ORDER_STATUS_VALUES.includes(dlg.radioValue as OrderStatus)) {
      showToast('请选择要改成的系统状态', 'error')
      return
    }
    batchDlg.value = null
    void runBatchAction(action, orderNos, { target: dlg.radioValue })
    return
  }

  let text: string
  let payload: PlatformPushActionPayload
  switch (action) {
    case 'arbitrate':
      text = ARBITRATION_CONFIRM_TEXT
      payload = { comment: value }
      break
    case 'refund':
      if (!isValidMoneyText(value)) {
        showToast('请输入正确的退款金额（累计总额，最多两位小数）', 'error')
        return
      }
      text = `确认把这些订单的累计退款额记为 ${value} 元吗？后续客服提成会按这个金额冲减。`
      payload = { refund_total: value, reason: REFUND_REASON, request_id: dlg.requestId }
      break
    case 'set_label':
      text = value === '' ? '确认清除这些订单的标签吗？' : `确认把这些订单的标签改为「${value}」吗？`
      payload = { label: value }
      break
    case 'set_tag':
      if (!isValidTagText(value)) {
        showToast('标记内容不能为空，且不能超过 128 个字符', 'error')
        return
      }
      text = `确认把这些订单的自定义标记改为「${value}」吗？`
      payload = { tag: value }
      break
    case 'change_taker_price':
      if (!isValidMoneyText(value)) {
        showToast('请输入正确的金额（非负、最多两位小数）', 'error')
        return
      }
      text = `确认把这些订单的接单价改为 ${value} 元吗？`
      payload = { taker_price: value }
      break
    default:
      return
  }
  const title = TOOLBAR_ACTION_LABELS[action]
  batchDlg.value = null
  confirmDlg.value = {
    title,
    text,
    run: async () => {
      await runBatchAction(action, orderNos, payload)
    },
  }
}

/** 发一次批量动作并如实报结果（全部成功 / 部分成功都重取列表 + 计数；请求本身失败不刷新）。 */
async function runBatchAction(
  action: PlatformPushBatchAction,
  orderNos: string[],
  payload: PlatformPushActionPayload,
): Promise<void> {
  if (actionBusy.value) return
  actionBusy.value = true
  const label = TOOLBAR_ACTION_LABELS[action]
  try {
    const outcome = await runPlatformPushAction(action, orderNos, payload)
    if (outcome.fail_count === 0) {
      showToast(`${label}已完成：成功 ${outcome.ok_count} 笔`)
    } else {
      const first = outcome.items.find((item) => !item.ok)
      showToast(
        `${label}：成功 ${outcome.ok_count} 笔，失败 ${outcome.fail_count} 笔` +
          (first === undefined ? '' : `（${first.order_no}：${first.msg}）`),
        'error',
      )
    }
    await reload(page.value)
    void loadCounts()
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    actionBusy.value = false
  }
}

/* ── 行内 4 颗（编辑 / 撤回 / 协商撤销 / 加款；可见性照 v1 ROW_ACTION_STATUSES）── */

/** 每颗在哪些**交易状态 token** 上出现（逐条取自后端实现；终态与中间态一颗都不出现 ⇒ 操作列「—」）。 */
const ROW_ACTION_STATUSES: Readonly<Record<string, readonly string[]>> = {
  edit: ['unaccepted'],
  withdraw: ['unaccepted', 'off_shelf'],
  negotiate: ['in_progress', 'waiting_review', 'abnormal', 'locked'],
  amount: ['in_progress', 'waiting_review'],
}
const ROW_ACTION_ORDER = ['edit', 'withdraw', 'negotiate', 'amount'] as const
type RowActionKey = (typeof ROW_ACTION_ORDER)[number]

function rowActionsFor(row: RenderRow): RowActionKey[] {
  if (row.push === null || !canPlatformWrite.value) return []
  const token = pushTokenOf(row.push)
  return ROW_ACTION_ORDER.filter((key) => (ROW_ACTION_STATUSES[key] ?? []).includes(token))
}

/** 行内写动作失败分档（⛔ 不能 import island ⇒ 按 v1 classifyPushError 同表本地重写）。 */
function classifyWriteError(error: unknown): 'failed' | 'unknown' {
  if (error instanceof BizError) return 'failed'
  if (error instanceof HttpError) return error.status >= 500 ? 'unknown' : 'failed'
  if (error instanceof AuthExpiredError) return 'failed'
  if (error instanceof NetworkError) return 'unknown'
  return 'unknown'
}

const ROW_WRITE_UNKNOWN_NOTE =
  '已发出，请刷新核对；不要重复提交 —— 加款是增量，重复提交会重复加钱'
const ROW_WITHDRAW_CONFIRM_TEXT =
  '未接手的平台单会直接从代练通删除并即时退款；删掉之后不可恢复。确定要撤回这笔平台单吗？'
const ROW_EDIT_OK_MESSAGE = '已提交修改'
const ROW_EDIT_EMPTY_MESSAGE = '请至少填写一项要修改的内容（留空 = 不修改）'
const ROW_EDIT_DIALOG_NOTE =
  '只有填写了的项会提交（留空 = 不改这一项）；价格请走「改价 / 加款」。' +
  '代练通详情里的现值不在本页读面里，所以这里不预填，请只填你要改的那一项。'
const ROW_NEGOTIATE_AUTO_LABEL = '协商 1 小时后自动申请客服介入'
const ROW_NEGOTIATE_NOTE =
  '提交后只能等打手 / 客服协商，能否撤销由对方决定；支付密码由后端自动带，无需填写。' +
  '两个金额默认取该推送行的代练通现值；拿不到现值时默认 0，请核对后再提交。'
const ROW_NEGOTIATE_DEFAULT_BALANCE = '0'
const ROW_NEGOTIATE_PAY_INVALID = '请填写「支付代练费」（不小于 0，最多两位小数）'
const ROW_NEGOTIATE_ENSURE_INVALID = '请填写「赔保证金」（不小于 0，最多两位小数）'
const PUSH_WITHDRAW_CLOSED_MESSAGE = '已撤回并退款'
const PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE = '已提交协商撤销，等打手/客服处理'
const ROW_AMOUNT_PROMPT_TITLE = '请输入加款金额（整数元，至少 1 元）'
const ROW_AMOUNT_INVALID_MESSAGE = '加款金额必须是不小于 1 的整数元'

/** 撤回 / 协商撤销成功后按**返回行**说结果（state=closed ⇒ 已删退款；其余 ⇒ 只是提交了申请）。 */
function rowWithdrawVerdict(updated: DailiantongPushRow): { ok: boolean; message: string } {
  return updated.state === 'closed'
    ? { ok: true, message: PUSH_WITHDRAW_CLOSED_MESSAGE }
    : { ok: true, message: PUSH_WITHDRAW_CANCEL_PENDING_MESSAGE }
}

/** 行内写动作统一收口：在途闸 + 成功刷新列表/计数 + 失败分档（unknown ⇒ 强制刷新核对）。 */
async function runRowWrite(
  call: () => Promise<DailiantongPushRow>,
  judge: (updated: DailiantongPushRow) => { ok: boolean; message: string },
): Promise<boolean> {
  if (actionBusy.value) return false
  actionBusy.value = true
  try {
    const updated = await call()
    const verdict = judge(updated)
    showToast(verdict.message, verdict.ok ? 'info' : 'error')
    await reload(page.value)
    void loadCounts()
    return true
  } catch (error) {
    if (classifyWriteError(error) === 'unknown') {
      showToast(`${errText(error)}（${ROW_WRITE_UNKNOWN_NOTE}）`, 'error')
      await reload(page.value)
      void loadCounts()
    } else {
      showToast(errText(error), 'error')
    }
    return false
  } finally {
    actionBusy.value = false
  }
}

/** 编辑弹层（不预填：读面不下发现值；只提交非空项；失败不关弹层）。 */
const editDlg = ref<null | { orderNo: string; title: string; memo: string; requirement: string; label1: string }>(null)

function openRowEdit(row: RenderRow): void {
  editDlg.value = { orderNo: row.orderNo, title: '', memo: '', requirement: '', label1: '' }
}

async function submitRowEdit(): Promise<void> {
  const dlg = editDlg.value
  if (dlg === null) return
  // 只带非空的键（⛔ 不送空串覆盖上游现值）。
  const payload: { title?: string; memo?: string; requirement?: string; label1?: string } = {}
  const title = dlg.title.trim()
  const memo = dlg.memo.trim()
  const requirement = dlg.requirement.trim()
  const label1 = dlg.label1.trim()
  if (title !== '') payload.title = title
  if (memo !== '') payload.memo = memo
  if (requirement !== '') payload.requirement = requirement
  if (label1 !== '') payload.label1 = label1
  if (Object.keys(payload).length === 0) {
    showToast(ROW_EDIT_EMPTY_MESSAGE, 'error')
    return
  }
  const ok = await runRowWrite(
    () => editOrderPlatformPush(dlg.orderNo, payload),
    () => ({ ok: true, message: ROW_EDIT_OK_MESSAGE }),
  )
  if (ok) editDlg.value = null
}

/** 协商撤销弹层（三格 + 自动客服介入勾选框默认不勾；失败不关弹层）。 */
const negotiateDlg = ref<null | {
  orderNo: string
  platform: number
  pay: string
  ensure: string
  comment: string
  auto: boolean
}>(null)

function openRowNegotiate(row: RenderRow): void {
  const push = row.push
  const balanceOf = (value: string | null | undefined): string => {
    const text = (value ?? '').trim()
    return text === '' ? ROW_NEGOTIATE_DEFAULT_BALANCE : text
  }
  negotiateDlg.value = {
    orderNo: row.orderNo,
    platform: push?.platform ?? FIXED_PLATFORM,
    pay: balanceOf(push?.pay_level_bal),
    ensure: balanceOf(push?.rep_ensure_bal),
    comment: '',
    auto: false,
  }
}

async function submitRowNegotiate(): Promise<void> {
  const dlg = negotiateDlg.value
  if (dlg === null) return
  const pay = dlg.pay.trim()
  const ensure = dlg.ensure.trim()
  if (!isValidMoneyText(pay)) {
    showToast(ROW_NEGOTIATE_PAY_INVALID, 'error')
    return
  }
  if (!isValidMoneyText(ensure)) {
    showToast(ROW_NEGOTIATE_ENSURE_INVALID, 'error')
    return
  }
  // auto_arbitrate 勾与不勾都显式发（后端需要一个明确的 false）。
  const ok = await runRowWrite(
    () =>
      withdrawOrderPlatformPush(dlg.orderNo, {
        pay_level_bal: pay,
        rep_ensure_bal: ensure,
        comment: dlg.comment.trim(),
        auto_arbitrate: dlg.auto,
        platform: dlg.platform,
      }),
    rowWithdrawVerdict,
  )
  if (ok) negotiateDlg.value = null
}

/** 撤回（未接手 ⇒ 直接删除 + 即时退款；先确认后果）。 */
function openRowWithdraw(row: RenderRow): void {
  const orderNo = row.orderNo
  const platform = row.push?.platform ?? FIXED_PLATFORM
  confirmDlg.value = {
    title: '撤回',
    text: ROW_WITHDRAW_CONFIRM_TEXT,
    run: async () => {
      await runRowWrite(
        () => withdrawOrderPlatformPush(orderNo, { platform }),
        rowWithdrawVerdict,
      )
    },
  }
}

/** 加款（整数元 ≥1；输入 → 确认后果 → 提交）。 */
const amountDlg = ref<null | { orderNo: string; value: string }>(null)

function openRowAmount(row: RenderRow): void {
  amountDlg.value = { orderNo: row.orderNo, value: '' }
}

function confirmRowAmount(): void {
  const dlg = amountDlg.value
  if (dlg === null) return
  const value = dlg.value.trim()
  if (!isValidAmountYuan(value)) {
    showToast(ROW_AMOUNT_INVALID_MESSAGE, 'error')
    return
  }
  const orderNo = dlg.orderNo
  amountDlg.value = null
  confirmDlg.value = {
    title: '加款',
    text: `确认给这笔平台单加款 ${value} 元吗？加款会直接抬高代练通上的发单价，且不可撤销。`,
    run: async () => {
      await runRowWrite(
        () => addOrderPlatformPushAmount(orderNo, value),
        (updated) => ({
          ok: true,
          message:
            updated.price === ''
              ? `已加款 ${value} 元`
              : `已加款 ${value} 元，平台发单价现为 ${updated.price} 元`,
        }),
      )
    },
  }
}

function onRowAction(key: RowActionKey, row: RenderRow): void {
  if (key === 'edit') openRowEdit(row)
  else if (key === 'withdraw') openRowWithdraw(row)
  else if (key === 'negotiate') openRowNegotiate(row)
  else openRowAmount(row)
}

const ROW_ACTION_TITLES: Readonly<Record<RowActionKey, string>> = {
  edit: '编辑代练通订单',
  withdraw: '撤回（未接手直接删除并即时退款）',
  negotiate: '协商撤销',
  amount: '加款（抬高平台发单价）',
}
const ROW_ACTION_SHORT: Readonly<Record<RowActionKey, string>> = {
  edit: '编辑',
  withdraw: '撤回',
  negotiate: '撤销',
  amount: '加款',
}

/* ── 订单详情（xl 原生双栏：左字段区右日志；fetchOrder + fetchOrderAudit，⛔ 不用 iframe）── */

const detailDlg = ref<null | { orderNo: string; viewNo: string }>(null)
const detailOrder = ref<Order | null>(null)
const detailLogs = ref<AuditEntry[]>([])
const detailLogsLoading = ref(false)
const detailTab = ref<'note' | 'action'>('note')
const noteText = ref('')
const noteAdding = ref(false)

function logTime(iso: string): string {
  const d = new Date(iso)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function openDetail(orderNo: string, viewNo: string): Promise<void> {
  // 开窗前的单号形状闸（/^\d{20}$/；⛔ 不把不可信输入拼进提示）。
  if (!isOrderNoShape(orderNo)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  detailDlg.value = { orderNo, viewNo }
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

/* ── 详情左栏的行内上下文（统一兜底「—」，模板不再写空值分支）────────────────────── */

const detailRow = computed<RenderRow | null>(() => {
  const dlg = detailDlg.value
  if (dlg === null) return null
  return rows.value.find((r) => r.orderNo === dlg.orderNo) ?? null
})

const detailServerText = computed(() => {
  const r = detailRow.value
  return r === null || r.isChild ? EMPTY_VALUE : r.serverText
})

const detailOrderStatus = computed(() => {
  const r = detailRow.value
  return r !== null && !r.isChild && r.orderStatus !== '' ? r.orderStatus : ''
})

const detailPush = computed(() => detailRow.value?.push ?? null)
const detailBadge = computed(() => (detailPush.value === null ? null : dispStatusBadge(detailPush.value)))
const detailSerialText = computed(() => {
  const serial = detailPush.value?.serial_no
  return serial === null || serial === undefined || serial === '' ? EMPTY_VALUE : serial
})
const detailPriceText = computed(() => {
  const price = detailPush.value?.price
  return price === undefined || price === '' ? EMPTY_VALUE : price
})
const detailAcceptText = computed(() => acceptPriceText(detailPush.value?.accept_price))
const detailTakerText = computed(() => acceptUserText(detailPush.value?.accept_user_name))
const detailCreatedText = computed(() => formatDateTime(detailPush.value?.created_at))
const detailRemoteAccText = computed(() => formatDateTime(detailPush.value?.remote_accepted_at))

/* ── 面板限高自适应 + 挂载 ─────────────────────────────────────────────────── */

const tablePanelRef = ref<HTMLElement | null>(null)

function fitPanelHeight(): void {
  const el = tablePanelRef.value
  if (el === null) return
  el.style.maxHeight = `${Math.max(320, window.innerHeight - el.getBoundingClientRect().top - 24)}px`
}

function onWindowResizeFit(): void {
  fitPanelHeight()
}

/** 倒计时的本地秒级 tick（v2 壳无 keep-alive，切签即卸载 ⇒ 只需 onUnmounted 清理）。 */
let tickTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void loadCounts()
  void reload(1)
  void nextTick(fitPanelHeight)
  window.setTimeout(fitPanelHeight, 600)
  window.addEventListener('resize', onWindowResizeFit)
  tickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  window.removeEventListener('resize', onWindowResizeFit)
  // ⛔ 定时器必须在卸载时清掉（E-0198 同族老坑）。
  if (tickTimer !== null) {
    clearInterval(tickTimer)
    tickTimer = null
  }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="platform-orders">
    <!-- 状态页签：只画有数据源的 9 个（照 v1 TAB_STATUS_MAP 映射）；计数一次取回 -->
    <nav class="cv2-ptabs" aria-label="交易状态页签" data-v2="ptabs">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': tab.key === activeTab }"
        :data-v2-ptab="tab.key"
        @click="onTabClick(tab)"
      >
        {{ tab.label }}<span class="cv2-ptab-count" :data-v2-tab-count="tab.key">{{ tabCountText(tab) }}</span>
      </button>
    </nav>

    <!-- 筛选：只有关键词接真（匹配我方单号/平台单号/来源单号）；其余死控件不搬 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-wrap">
        <div class="flt-group flt-group--key" data-v2-filter-group="keyword">
          <span class="flt-label">关键词</span>
          <span class="flt-cell"><el-input
            v-model="filters.keyword"
            placeholder="我方单号 / 平台单号 / 来源单号"
            data-v2-filter="keyword"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 平台操作仍按权限与选中状态执行。 -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新</button>
      <div class="ttb-strip" data-v2="toolbar-strip">
        <template v-for="b in visibleToolbarButtons" :key="b.action">
          <button
            v-if="!b.gated || canPlatformWrite"
            type="button"
            class="cv2-btn cv2-btn--primary"
            :data-v2-action="b.action"
            :disabled="actionBusy"
            @click="onToolbarAction(b.action)"
          >{{ b.label }}</button>
        </template>
      </div>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedNos.length }} 行</span>
      <div class="cv2-colpick" data-v2="colpick">
        <el-popover trigger="click" :teleported="false" :show-arrow="false" :width="188" popper-class="cv2-colpick-pop">
          <template #reference>
            <button
              type="button"
              class="cv2-btn cv2-btn--primary cv2-colpick-btn"
              title="选择展现的字段"
              aria-label="选择展现的字段"
            >
              <svg viewBox="0 0 16 16" aria-hidden="true" class="cv2-colpick-ico">
                <rect x="2" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="9" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="2" y="9" width="5" height="4.5" rx="1" />
                <rect x="9" y="9" width="5" height="4.5" rx="1" />
              </svg>
            </button>
          </template>
          <p class="cv2-colpick-title">选择展现的字段</p>
          <el-checkbox-group v-model="visibleColKeys" class="cv2-colpick-group" data-v2="colpick-group">
            <el-checkbox
              v-for="key in (Object.keys(COL_TITLES) as (keyof typeof COL_TITLES)[])"
              :key="key"
              :value="key"
              :disabled="key === 'actions'"
              :data-v2-colcheck="key"
            >{{ COL_TITLES[key] }}</el-checkbox>
          </el-checkbox-group>
        </el-popover>
      </div>
    </div>

    <!-- 表格：聚合折叠（父行取最新一条推送；展开零请求）；列宽拖拽；操作列 sticky 右钉 -->
    <section ref="tablePanelRef" class="cv2-panel cv2-table-panel" aria-label="平台订单列表">
      <table class="cv2-table">
        <!-- colgroup 与渲染的单元格同序同数（table-layout:fixed 按位置映射） -->
        <colgroup>
          <col v-for="key in visibleColumns" :key="key" :style="colStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input type="checkbox" data-v2="check-page-all" :checked="pageAllChecked" aria-label="勾选当前页全部行" @change="togglePageAll" />
              <span class="cv2-th-resizer" data-v2-col="check" @mousedown="startResize($event, 'check')" @dblclick="resetColumn('check')" />
            </th>
            <th v-if="colVisible('serial')" class="cv2-th-center">
              平台交易单号
              <span class="cv2-th-resizer" data-v2-col="serial" @mousedown="startResize($event, 'serial')" @dblclick="resetColumn('serial')" />
            </th>
            <th v-if="colVisible('sysno')">
              系统单号
              <span class="cv2-th-resizer" data-v2-col="sysno" @mousedown="startResize($event, 'sysno')" @dblclick="resetColumn('sysno')" />
            </th>
            <th v-if="colVisible('game')" class="cv2-th-center">
              游戏区服
              <span class="cv2-th-resizer" data-v2-col="game" @mousedown="startResize($event, 'game')" @dblclick="resetColumn('game')" />
            </th>
            <th v-if="colVisible('platform')" class="cv2-th-center">发单平台</th>
            <th v-if="colVisible('price')" class="cv2-th-center">
              发单价
              <span class="cv2-th-resizer" data-v2-col="price" @mousedown="startResize($event, 'price')" @dblclick="resetColumn('price')" />
            </th>
            <th v-if="colVisible('accept')" class="cv2-th-center">
              接单价
              <span class="cv2-th-resizer" data-v2-col="accept" @mousedown="startResize($event, 'accept')" @dblclick="resetColumn('accept')" />
            </th>
            <th v-if="colVisible('profit')" class="cv2-th-center">
              利润
              <span class="cv2-th-resizer" data-v2-col="profit" @mousedown="startResize($event, 'profit')" @dblclick="resetColumn('profit')" />
            </th>
            <th v-if="colVisible('remaining')" class="cv2-th-center">
              剩余时间
              <span class="cv2-th-resizer" data-v2-col="remaining" @mousedown="startResize($event, 'remaining')" @dblclick="resetColumn('remaining')" />
            </th>
            <th v-if="colVisible('dispstatus')" class="cv2-th-center">交易状态</th>
            <th v-if="colVisible('accepter')" class="cv2-th-center">接手人</th>
            <th v-if="colVisible('orderstatus')" class="cv2-th-center">系统状态</th>
            <th v-if="colVisible('over')" class="cv2-th-center">完成单数</th>
            <th v-if="colVisible('cancel')" class="cv2-th-center">投诉/取消</th>
            <th v-if="colVisible('remoteacc')" class="cv2-th-center">
              接单时间
              <span class="cv2-th-resizer" data-v2-col="remoteacc" @mousedown="startResize($event, 'remoteacc')" @dblclick="resetColumn('remoteacc')" />
            </th>
            <th v-if="colVisible('verify')" class="cv2-th-center">
              验收倒计
              <span class="cv2-th-resizer" data-v2-col="verify" @mousedown="startResize($event, 'verify')" @dblclick="resetColumn('verify')" />
            </th>
            <th v-if="colVisible('source')" class="cv2-th-center">订单来源</th>
            <th v-if="colVisible('created')" class="cv2-th-center">
              发单时间
              <span class="cv2-th-resizer" data-v2-col="created" @mousedown="startResize($event, 'created')" @dblclick="resetColumn('created')" />
            </th>
            <th v-if="colVisible('rectime')" class="cv2-th-center">接手时间</th>
            <th v-if="colVisible('completetime')" class="cv2-th-center">完单时间</th>
            <th v-if="colVisible('createtime')" class="cv2-th-center">录单时间</th>
            <th v-if="colVisible('revoketime')" class="cv2-th-center">撤销时间</th>
            <th v-if="colVisible('pushman')" class="cv2-th-center">发单人</th>
            <th class="cv2-th-center cv2-th-fixed">
              操作
              <span class="cv2-th-resizer" data-v2-col="actions" @mousedown="startResize($event, 'actions')" @dblclick="resetColumn('actions')" />
            </th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in renderRows" :key="row.key">
            <tr
              class="cv2-row"
              :class="{ 'cv2-row--checked': !row.isChild && isChecked(row.orderNo), 'cv2-child-row': row.isChild }"
              :data-v2-row="row.orderNo"
              :data-v2-child="row.isChild ? 'y' : undefined"
            >
              <td class="cv2-th-check">
                <input
                  v-if="!row.isChild"
                  type="checkbox"
                  :data-v2-row-check="row.orderNo"
                  :checked="isChecked(row.orderNo)"
                  :aria-label="`勾选 ${row.viewNo}`"
                  @change="toggleRow(row.orderNo)"
                />
              </td>
              <td v-if="colVisible('serial')" class="cv2-td-center" :title="row.serialTitle">
                <span class="cv2-num">{{ row.serialText }}</span>
              </td>
              <td v-if="colVisible('sysno')">
                <template v-if="!row.isChild">
                  <button
                    v-if="row.groupSize !== null"
                    type="button"
                    class="cv2-tree"
                    :class="{ 'cv2-tree--open': expandedNos.has(row.orderNo) }"
                    :data-v2-tree="row.orderNo"
                    :title="groupCountTitle(row.groupSize)"
                    aria-label="展开 / 收起该订单的平台单"
                    @click.stop="toggleChildren(row.orderNo)"
                  >
                    <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M4 2.5 8.5 6 4 9.5" /></svg>
                  </button>
                  <a href="#" class="cv2-order-link cv2-platform-order-link" :data-v2-detail="row.orderNo" @click.prevent="openDetail(row.orderNo, row.viewNo)">{{ row.viewNo }}</a>
                  <span v-if="row.groupSize !== null && row.viewNo !== row.orderNo" class="cv2-innerno">系统单号：{{ row.orderNo }}</span>
                </template>
                <template v-else>—</template>
              </td>
              <td v-if="colVisible('game')" class="cv2-td-center">{{ row.isChild ? '—' : row.serverText }}</td>
              <td v-if="colVisible('platform')" class="cv2-td-center">
                {{ row.push === null ? '—' : platformLabelOf(row.push.platform) }}
              </td>
              <td v-if="colVisible('price')" class="cv2-td-center">
                <span class="cv2-money">{{ row.push === null || row.push.price === '' ? '—' : row.push.price }}</span>
              </td>
              <td v-if="colVisible('accept')" class="cv2-td-center"><span class="cv2-money">{{ acceptPriceText(row.push?.accept_price) }}</span></td>
              <td v-if="colVisible('profit')" class="cv2-td-center">
                <span class="cv2-money">{{ profitTextOf(row.push?.accept_price, row.push?.price) }}</span>
              </td>
              <td v-if="colVisible('remaining')" class="cv2-td-center">
                <span :class="{ 'cv2-overdue': shoursParts(row).overdue }">{{ shoursParts(row).text }}</span>
              </td>
              <td v-if="colVisible('dispstatus')" class="cv2-td-center">
                <span
                  v-if="row.push !== null && dispStatusBadge(row.push) !== null"
                  class="cv2-st cv2-st--static cv2-st--mark"
                  :class="dispStatusBadge(row.push)?.cls"
                  title="该笔订单在平台的交易状态"
                ><i class="cv2-dot" aria-hidden="true"></i>{{ dispStatusBadge(row.push)?.label }}</span>
                <template v-else>—</template>
              </td>
              <td v-if="colVisible('accepter')" class="cv2-td-center" :title="row.push?.accept_uid ? `平台 UID：${row.push.accept_uid}` : undefined">
                {{ acceptUserText(row.push?.accept_user_name) }}
              </td>
              <td v-if="colVisible('orderstatus')" class="cv2-td-center">
                <span
                  v-if="!row.isChild && row.orderStatus !== ''"
                  class="cv2-st cv2-order-status cv2-st--static cv2-st--mark"
                  :class="orderStatusClass(row.orderStatus)"
                  :data-status="row.orderStatus"
                ><i class="cv2-dot" aria-hidden="true"></i>{{ v2OrderStatusLabel(row.orderStatus) }}</span>
                <template v-else>—</template>
              </td>
              <td v-if="colVisible('over')" class="cv2-td-center">{{ countText(row.push?.accept_over_count) }}</td>
              <td v-if="colVisible('cancel')" class="cv2-td-center">
                <span :class="{ 'cv2-neg': custCancelAlert(row.push?.cust_cancel_count) }">{{ countText(row.push?.cust_cancel_count) }}</span>
              </td>
              <td v-if="colVisible('remoteacc')" class="cv2-td-center cv2-td-time">{{ formatDateTime(row.push?.remote_accepted_at) }}</td>
              <td v-if="colVisible('verify')" class="cv2-td-center">
                <span :class="{ 'cv2-overdue': verifyParts(row).overdue }">{{ verifyParts(row).text }}</span>
              </td>
              <td v-if="colVisible('source')" class="cv2-td-center">{{ orderSourceText(row.push?.order_source) }}</td>
              <td v-if="colVisible('created')" class="cv2-td-center cv2-td-time">{{ formatDateTime(row.push?.created_at) }}</td>
              <td v-if="colVisible('rectime')" class="cv2-td-center cv2-td-time">—</td>
              <td v-if="colVisible('completetime')" class="cv2-td-center cv2-td-time">—</td>
              <td v-if="colVisible('createtime')" class="cv2-td-center cv2-td-time">—</td>
              <td v-if="colVisible('revoketime')" class="cv2-td-center cv2-td-time">—</td>
              <td v-if="colVisible('pushman')" class="cv2-td-center">—</td>
              <td class="cv2-td-center cv2-td-fixed">
                <!-- 行内 4 颗：按这条推送的交易状态出现；无权限 / 终态 ⇒ 「—」（fail-closed） -->
                <template v-if="rowActionsFor(row).length > 0">
                  <button
                    v-for="act in rowActionsFor(row)"
                    :key="act"
                    type="button"
                    class="cv2-rowact"
                    :class="{ 'cv2-rowact--danger': act === 'withdraw' }"
                    :data-v2-row-action="act"
                    :data-v2-row-order="row.orderNo"
                    :title="ROW_ACTION_TITLES[act]"
                    @click.stop="onRowAction(act, row)"
                  >{{ ROW_ACTION_SHORT[act] }}</button>
                </template>
                <template v-else>—</template>
              </td>
            </tr>
          </template>
          <tr v-if="rows.length === 0">
            <td :colspan="visibleColumns.length" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>无数据</template>
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

    <!-- 炭火 toast（页面自绘；居中深条，覆盖 carbon.css 顶部白卡片形态） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>

    <!-- 通用确认层（批量第二步 / 行内撤回 / 加款；文案逐字照 v1） -->
    <el-dialog
      :model-value="confirmDlg !== null"
      :title="confirmDlg === null ? '' : confirmDlg.title"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-confirm"
      @update:model-value="confirmDlg = null"
    >
      <p v-if="confirmDlg !== null" class="cv2-dlg-text">{{ confirmDlg.text }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="confirmDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-yes" :disabled="actionBusy" @click="void confirmDlg?.run(); confirmDlg = null">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 批量输入层（仲裁理由 / 改系统状态 / 退款 / 标签 / 标记 / 接单价） -->
    <el-dialog
      :model-value="batchDlg !== null"
      :title="batchDlg === null ? '' : batchDlg.title"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-batch"
      @update:model-value="batchDlg = null"
    >
      <template v-if="batchDlg !== null">
        <p v-if="batchDlg.inputLabel !== ''" class="cv2-dlg-text">{{ batchDlg.inputLabel }}</p>
        <p v-else-if="batchDlg.inputKind === 'status'" class="cv2-dlg-text">{{ STATUS_DIALOG_NOTE }}</p>
        <el-input
          v-if="batchDlg.inputKind === 'text'"
          v-model="batchDlg.inputValue"
          :maxlength="batchDlg.inputMax"
          data-v2="batch-input"
        />
        <el-input
          v-else-if="batchDlg.inputKind === 'area'"
          v-model="batchDlg.inputValue"
          type="textarea"
          :rows="3"
          :maxlength="batchDlg.inputMax"
          data-v2="batch-area"
        />
        <div v-else-if="batchDlg.inputKind === 'status'" class="cv2-status-radios" data-v2="status-targets">
          <label v-for="code in ORDER_STATUS_VALUES" :key="code" class="cv2-radio">
            <input v-model="batchDlg.radioValue" type="radio" name="v2-platform-target" :value="code" />
            {{ V2_ORDER_STATUS_LABELS[code] }}
          </label>
        </div>
      </template>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="batchDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-batch" :disabled="actionBusy" @click="confirmBatch">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 行内编辑（不预填：只提交非空项；失败不关弹层） -->
    <el-dialog
      :model-value="editDlg !== null"
      title="编辑代练通订单"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-row-edit"
      @update:model-value="editDlg = null"
    >
      <template v-if="editDlg !== null">
        <div class="cv2-field"><span class="cv2-field-label">发单标题</span><el-input v-model="editDlg.title" placeholder="留空 = 不修改标题" /></div>
        <div class="cv2-field"><span class="cv2-field-label">代练内容</span><el-input v-model="editDlg.memo" type="textarea" :rows="3" placeholder="留空 = 不修改代练内容" /></div>
        <div class="cv2-field"><span class="cv2-field-label">代练要求</span><el-input v-model="editDlg.requirement" type="textarea" :rows="3" placeholder="留空 = 不修改代练要求" /></div>
        <div class="cv2-field"><span class="cv2-field-label">标签</span><el-input v-model="editDlg.label1" placeholder="留空 = 不修改标签" /></div>
        <p class="dlg-note">{{ ROW_EDIT_DIALOG_NOTE }}</p>
      </template>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="editDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-row-edit" :disabled="actionBusy" @click="void submitRowEdit()">提交修改</button>
        </div>
      </template>
    </el-dialog>

    <!-- 行内协商撤销（金额默认取现值、缺省 0；自动客服介入默认不勾；失败不关弹层） -->
    <el-dialog
      :model-value="negotiateDlg !== null"
      title="协商撤销代练通订单"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-row-negotiate"
      @update:model-value="negotiateDlg = null"
    >
      <template v-if="negotiateDlg !== null">
        <div class="cv2-field"><span class="cv2-field-label">支付代练费</span><el-input v-model="negotiateDlg.pay" placeholder="支付给接单打手的金额（有现值用现值，没有默认 0）" /></div>
        <div class="cv2-field"><span class="cv2-field-label">赔保证金</span><el-input v-model="negotiateDlg.ensure" placeholder="接单打手赔偿给发单方的金额（有现值用现值，没有默认 0）" /></div>
        <div class="cv2-field"><span class="cv2-field-label">撤销理由</span><el-input v-model="negotiateDlg.comment" type="textarea" :rows="2" placeholder="撤销原因（打手 / 客服会看到）" /></div>
        <label class="cv2-check" data-v2="negotiate-auto">
          <input v-model="negotiateDlg.auto" type="checkbox" />
          {{ ROW_NEGOTIATE_AUTO_LABEL }}
        </label>
        <p class="dlg-note">{{ ROW_NEGOTIATE_NOTE }}</p>
      </template>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="negotiateDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-row-negotiate" :disabled="actionBusy" @click="void submitRowNegotiate()">提交申请</button>
        </div>
      </template>
    </el-dialog>

    <!-- 行内加款（整数元 ≥1；确定后进确认层说后果） -->
    <el-dialog
      :model-value="amountDlg !== null"
      title="加款"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-row-amount"
      @update:model-value="amountDlg = null"
    >
      <p class="cv2-dlg-text">{{ ROW_AMOUNT_PROMPT_TITLE }}</p>
      <div v-if="amountDlg !== null" class="cv2-field">
        <span class="cv2-field-label">加款金额</span>
        <el-input v-model="amountDlg.value" :maxlength="10" data-v2="amount-input" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="amountDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-row-amount" :disabled="actionBusy" @click="confirmRowAmount">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 订单详情：xl 原生双栏（左=订单字段区+内容，右=订单/操作日志+添加框） -->
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
            <div class="dtl-row"><span class="dtl-label">游戏区服</span><span>{{ detailServerText }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_role_name }"><span class="dtl-label">角色名</span><span>{{ detailOrder?.game_role_name }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': !detailOrder?.project_text }"><span class="dtl-label">代练项目</span><span>{{ detailOrder?.project_text }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailOrderStatus === '' }"><span class="dtl-label">系统状态</span>
              <span v-if="detailOrderStatus !== ''" class="cv2-st cv2-order-status cv2-st--static cv2-st--mark" :class="orderStatusClass(detailOrderStatus)"><i class="cv2-dot" aria-hidden="true"></i>{{ v2OrderStatusLabel(detailOrderStatus) }}</span>
            </div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailSerialText === EMPTY_VALUE }"><span class="dtl-label">平台单号</span><span class="cv2-num">{{ detailSerialText }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailBadge === null }"><span class="dtl-label">交易状态</span>
              <span v-if="detailBadge !== null" class="cv2-st cv2-st--static cv2-st--mark" :class="detailBadge.cls"><i class="cv2-dot" aria-hidden="true"></i>{{ detailBadge.label }}</span>
            </div>
            <div class="dtl-row"><span class="dtl-label">发单价</span><span class="cv2-money">{{ detailPriceText }}</span></div>
            <div class="dtl-row"><span class="dtl-label">接单价</span><span class="cv2-money">{{ detailAcceptText }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailTakerText === '' || detailTakerText === EMPTY_VALUE }"><span class="dtl-label">接手人</span><span>{{ detailTakerText }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_account }"><span class="dtl-label">游戏账号</span><span>{{ detailOrder?.game_account }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.customer_phone }"><span class="dtl-label">联系电话</span><span>{{ detailOrder?.customer_phone }}</span></div>
            <div class="dtl-row"><span class="dtl-label">发单时间</span><span class="cv2-num">{{ detailCreatedText }}</span></div>
            <div class="dtl-row"><span class="dtl-label">接单时间</span><span class="cv2-num">{{ detailRemoteAccText }}</span></div>
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
              <!-- summary 服务端已拼好整句，前端只补时间头（spec §5.9） -->
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
  </div>
</template>

<style scoped>
.cv2-platform-order-link,
.cv2-innerno { color: var(--tt2-st-paused); }
.cv2-platform-order-link { font-weight: 600; }
.cv2-platform-order-link:hover { color: var(--tt2-st-paused); text-decoration: underline; }

/* 本页新增的形状收在这里（carbon.css 没有 → 各自重声明；色一律 var(--tt2-*)，⛔ 不写裸色值）。 */

/* 数字列等宽（等宽是一切数据的身份；尺寸/字体非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 纯展示签（系统状态 / 交易状态不可点）：压掉 .cv2-st 全局 pointer。 */
.cv2-st--static {
  cursor: default;
}

/* 平台交易状态在悬停时显示浅底。 */
.cv2-page[data-v2-page="platform-orders"] .cv2-st--mark:not(.cv2-order-status) {
  gap: 6px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--st-ink, var(--tt2-st-unassigned));
  font-size: 12px;
  font-weight: 600;
  transition: background-color 120ms ease;
}
.cv2-page[data-v2-page="platform-orders"] .cv2-st--mark:not(.cv2-order-status) .cv2-dot {
  width: 8px;
  height: 8px;
  background: var(--st-dot, var(--tt2-st-unassigned-dot));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-dot, var(--tt2-st-unassigned-dot)) 12%, transparent);
}
.cv2-page[data-v2-page="platform-orders"] .cv2-st--mark:not(.cv2-order-status):hover {
  background: var(--st-hover, transparent);
}
.cv2-page[data-v2-page="platform-orders"] .cv2-st--unassigned.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-unassigned); --st-hover: var(--tt2-st-unassigned-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--pending.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-pending); --st-hover: var(--tt2-st-pending-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--doing.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-doing); --st-hover: var(--tt2-st-doing-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--accept.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-accept); --st-hover: var(--tt2-st-accept-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--done.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-done); --st-hover: var(--tt2-st-done-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--error.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-error); --st-hover: var(--tt2-st-error-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--paused.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-paused); --st-hover: var(--tt2-st-paused-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--refund.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-refund); --st-dot: var(--tt2-st-refund-dot); --st-hover: var(--tt2-st-refund-bg); }
.cv2-page[data-v2-page="platform-orders"] .cv2-st--settled.cv2-st--mark:not(.cv2-order-status) { --st-ink: var(--tt2-st-settled); --st-dot: var(--tt2-st-settled); --st-hover: var(--tt2-st-doing-bg); }


/* ── 页签计数徽标（counts 一次取回；失败「—」）── */
.cv2-ptab-count {
  margin-left: 6px;
  padding: 0 6px;
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  background: var(--tt2-tool-bg);
  color: var(--tt2-dim);
  font-size: 11px;
  line-height: 16px;
  font-variant-numeric: tabular-nums;
}

.cv2-ptab--on .cv2-ptab-count {
  color: var(--tt2-primary-deep);
  border-color: var(--tt2-primary);
  background: var(--tt2-hover-wash);
}

/* ── 折叠三角（聚合父行；展开转 90°）── */
.cv2-tree {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  margin-right: 6px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--tt2-dim);
  cursor: pointer;
  vertical-align: -3px;
}

.cv2-tree svg {
  width: 10px;
  height: 10px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.15s;
}

.cv2-tree--open svg {
  transform: rotate(90deg);
}

.cv2-tree:hover {
  color: var(--tt2-primary-deep);
  background: var(--tt2-hover-wash);
}

/* 折叠父行第二行的小字（来源单号 ≠ 内部单号时）。 */
.cv2-innerno {
  display: block;
  font-size: 11px;
  color: var(--tt2-dim);
}

/* 展开的平台子行：浅工具底与父行区分（hover 同全表晕）。 */
tbody tr.cv2-child-row {
  background: var(--tt2-tool-bg);
}

/* 红（负利润 / 投诉取消>0 / 倒计超时）；token 同「金额红」档。 */
.cv2-neg,
.cv2-overdue {
  color: var(--tt2-money-red);
}

/* ── 筛选区连体网格组（照 V2OrderManage scoped 同款：标签格 + 值格零间距）── */
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

/* 关键词组定宽（输入框撑满）。 */
.flt-group--key {
  width: 340px;
}

.flt-group--key :deep(.el-input) {
  width: 100%;
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px（34 − 上下边线）。 */
.flt-group :deep(.el-input__wrapper) {
  height: 32px;
  min-height: 32px;
  border-radius: 0;
  background: var(--tt2-panel);
  box-shadow: none;
}

.flt-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}

/* ── 列显隐选择器（表头右上）── */
.cv2-colpick {
  position: relative;
  margin-left: 4px;
}

.cv2-colpick-btn {
  width: 34px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.cv2-colpick-ico {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
}

.cv2-colpick-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--tt2-dim);
}

.cv2-colpick-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 420px;
  overflow-y: auto;
}

.cv2-colpick-group :deep(.el-checkbox) {
  margin-right: 0;
  height: 28px;
}

/* ── 操作列冻结（sticky 右钉；底色随行状态补齐：白 / hover 晕 / 勾选青绿底 / 子行工具底）── */
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
  /* 晕是 rgba：垫底色再叠同色晕 = 与其它单元格的复合结果一致 */
  background: var(--tt2-panel);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-child-row .cv2-td-fixed {
  background: var(--tt2-tool-bg);
}

tbody tr.cv2-child-row:hover .cv2-td-fixed {
  background: var(--tt2-tool-bg);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-row--checked .cv2-td-fixed {
  background: var(--tt2-row-checked);
}

/* 行内操作：同一套小字按钮，横排不换行。 */
td.cv2-td-fixed {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: nowrap;
  gap: 4px;
  min-width: 132px;
}

/* ── 面板改内部滚动（Excel 冻结窗格形态；V2OrderManage :3124 同款）── */
.cv2-table-panel {
  max-height: calc(100vh - 470px);
  min-height: 320px;
  overflow: auto;
}

/* 弹层表单行距。 */
.cv2-dialog .cv2-field {
  margin-bottom: 12px;
}

/* 改系统状态弹层：radio 换行排开。 */
.cv2-status-radios {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 22px;
  padding: 6px 0 2px;
}

.cv2-status-radios .cv2-radio {
  white-space: nowrap;
}

/* 弹层里的后果说明块（编辑 / 协商撤销）。 */
.dlg-note {
  margin: 10px 0 0;
  padding: 8px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-label-bg);
  font-size: 12px;
  line-height: 1.6;
  color: var(--tt2-dim);
}

/* 全局提示 = 对标 layer.msg 居中深条（覆盖 carbon.css 顶部白卡片形态）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}

/* ── 订单详情原生双栏（左=订单内容，右=日志；xl 弹窗 body 零内边距）── */
.dtl-wrap {
  display: flex;
  align-items: stretch;
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
</style>
