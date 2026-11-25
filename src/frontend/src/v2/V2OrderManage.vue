<script setup lang="ts">
/**
 * 「管理订单」v2（`/v2/order-manage`；路由 name `v2-order-manage`，侧栏「订单管理 → 管理订单」进）。
 *
 * 旧页 = island 复刻页 `order-orderlist.ts`（路由 `orders`）。本页按同一套功能重建为炭火页：
 * **布局三段 = 搜索区 → 按钮排 → 订单列表**（业务方 2026-11-04 指令），数据零新增端点：
 *
 * | 旧页功能 | 本页落点 |
 * |---|---|
 * | 筛选三行（游戏 / 时间类型+区间 / 价格口径+区间 / 订单来源 / 订单状态多选 / 接手人档位+人 / 订单类型 / 客服档位+人 / 收款状态 / 搜索1 / 搜索2 / 加急 / 未安排） | 同字段全保留，控件 EP 化（`.cv2-field` 标签左置）；映射与优先级照 island `toOrderSearch`（key1 有值只发 keyword、否则 key2 发 project_text） |
 * | 时间类型 | 五档照渲染；**只有「录单时间」接了后端**（与 island 同口径），选其它档查询时提示一句 |
 * | 批量搜单号 | 「搜索1」切到批量档后点查询 → 弹层收单号（支持系统单号和来源单号） |
 * | 工具栏 11 颗 | 刷新 / 指派打手 / 改状态 / 改类型 / 记退款 / 导出 / 改接单价 / 改发单价 / 收款状态 / 重算客服提成 / 删除，全部接**批量端点**（`api/orders.ts` 的 `batch*`）；配色按 D-0371 按钮 口诀 |
 * | 统计句 | `data.summary`（后端对当前筛选全集的聚合）真值渲染，红字（`.cv2-total-line`）；「状态词+数量」段可点 = 只筛该状态 / 再点清除回全部（状态下拉同步） |
 * | 列 | 旧页可见列全保留（含 v1 默认隐藏的「完单时间」）；截断 44px 单行 + 列宽拖拽（`useColumnWidths`） |
 * | 行内动作 | 编辑 / 发到内部大厅 / 从大厅撤回 / 一键发交易平台（本页暖米表）/ 订单详情 / 截图看·传 / 点状态签·价格·退款额快捷改 |
 *
 * 已知简化（迭代在途，如实登记）：接单价里「未/中/结」收款状态小徽标不挂（三档配色待业务方
 * 拍板）；指派弹层不带 v1 的「该订单的打手账单」表（需绩效读面，下一轮）；排团按钮、平台子单
 * 三角展开、右键菜单、角色名/内容/备注的行内快捷编辑——后续迭代补。
 *
 * ⭐ 2026-11-09（业务方拍板「打手/下家下拉全改关键词搜，v2 也带上」）：「代练打手」「发单人」
 * 两个拾人面板的候选从**预载 ≤200 + 本地过滤**改为**关键词远程搜索**——挂载/切游戏/重置零
 * `filter-options` 请求；面板里输入关键词 400ms 防抖请求 `?search=<词>&game=<当前游戏>`（后端
 * 显示名 icase 包含；不传时行为逐字不变），回包即候选；空关键词不发请求（面板显示提示句）。
 * 已选人名缓存在 `pickedNames`（候选被搜索替换后按钮标签不丢）。指派弹层早已带 `key` 服务端
 * 搜索，不在本轮。
 *
 * ⭐ 2026-11-04 第二轮（业务方逐条圈注）：工具条与行内动作改「按功能实底 + 白字」套件
 * （--tt2-btn-solid-*，色值与对比度见 DRAFT §1）；操作列 sticky 冻结；表头右上列显隐选择器
 * （默认藏 系统内置单号/完单时间，localStorage 记忆）；接手人格有值也可点开指派弹窗改派；
 * 指派弹窗照 v1 三块结构（职位档/搜索/左列候选单选 + 右侧分配格子，清除接手人两步确认）；
 * 改状态弹窗拉长 + 问题单/已暂停/已退单选中时「说明原因」必填（进订单日志，D-0337）；
 * 编辑弹窗内容换 v2 录单页组件编辑态（V2OrderNew `:order-no`），弹层以右侧内容区居中。
 *
 * 权限门走会话 store `canNode`，批量预检和校验复用旧页面的共享流程。
 */
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import FinanceExportJobStatus from '@/v2/FinanceExportJobStatus.vue'
import { useFinanceExportJob } from '@/v2/useFinanceExportJob'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createOrderNote,
  batchAssignOrders,
  clearTaker,
  downloadOrderExport,
  fetchAssigneeOptions,
  fetchBatchTransitionTargets,
  fetchFilterOptions,
  fetchOrder,
  fetchOrderAudit,
  fetchPenaltyChildren,
  fetchSplitMembers,
  splitPeerOrder,
  fetchKefuOptions,
  isOrderNoShape,
  startXgjReceiptSync,
  toLocalIso,
  toOrderParams,
  updateOrder,
  type AssigneeOption,
  type AssignOrderPayload,
  type AuditEntry,
  type BatchOutcome,
  type Order,
  type OrderListItem,
  type OrderSearchParams,
} from '@/api/orders'
import { fetchFavoriteAssignees, setFavoriteAssignee } from '@/api/orderAssigneeFavorites'
import {
  assignJidanpuTaker, fetchJidanpuAssignmentStatus, fetchJidanpuDirectAssignment, searchJidanpuTakers, verifyJidanpuAssignment,
  type JidanpuDirectAssignment, type JidanpuTaker, type JidanpuTakerSearch,
} from '@/api/jidanpuDirectAssignment'
import { BizError, HttpError, NetworkError } from '@/api/errors'
import {
  assignPeerTaker, fetchPeerDirectAssignment, searchPeerTakers,
  type PeerDirectAssignment, type PeerTakerMatch,
} from '@/api/peerDirectAssignment'
import { throwToHall, withdrawThrow } from '@/api/hall'
import { fetchPerformances, type PerformanceListItem } from '@/api/performances'
import { fetchOrderDictionaries, type OrderDictionaries } from '@/api/orderDictionaries'
import { fetchOrderSources, fetchOrderSourceGroups, type OrderSourceRow } from '@/api/orderSources'
import { filterSourcesByGroup } from './orderSourceFilter'
import {
  ORDER_STATUS_VALUES,
  V2_ORDER_STATUS_LABELS,
  v2OrderStatusLabel,
  type OrderStatus,
} from '@/config/orderStatus'
import { isBlankLikePython, validateMoneyInput } from '@/config/money'
import { useSessionStore } from '@/stores/session'
import { runBatchFlow, type BatchFlowOptions, type BatchPreview } from '@/layui-island/orderBatch'
import { projectTextSyncLimitError } from '@/views/orders/projectText'

import { loadColumnWidths, useColumnWidths } from '@/v2/tableResize'
import { fetchOrdersWithSummary, fetchPushAvailability, type OrderListSummary } from '@/v2/orderManageApi'
import V2OrderNew from '@/v2/V2OrderNew.vue'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2DetailFace from '@/v2/V2DetailFace.vue'
import V2PenaltyDialog from '@/v2/V2PenaltyDialog.vue'
import { APPROVAL_APPLY_NODE } from '@/api/approvals'
import V2ScreenshotUpload from '@/v2/V2ScreenshotUpload.vue'
import ElDialog from '@/v2/OrderTabDialog.vue'

const previewOrderGroups = import.meta.env.DEV && import.meta.env.VITE_INTERNAL_CHAT_ORDER_GROUPS === '1'
const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/* ── 文案（出处 = island 页同语义常量；本地重写不引模块）──────────────────── */

const SELECT_RECORDS_FIRST_TEXT = '请选中要操作的记录'
/** 收款状态三档（option 文案照证据件；值送后端码）。 */
const PAY_STATUS_OPTIONS = [
  { code: 'UNSETTLED', label: '未收款' },
  { code: 'SETTLING', label: '结算中' },
  { code: 'SETTLED', label: '已结款' },
] as const
/** 价格口径五档（文案照证据件；值送后端 0..4）。 */
const PRICE_TIERS = [
  { value: '0', label: '接单价格' },
  { value: '1', label: '发单价格' },
  { value: '2', label: '退款金额' },
  { value: '3', label: '接发组合' },
  { value: '4', label: '搜亏本单' },
] as const
// el-option 的 value prop 类型不含 null；运行时仍以 null 表示「全部」（与 filters.gameId 等同值比较）。
const NULL_OPTION_VALUE = null as unknown as number
const DATE_TYPES = [
  { value: '10', label: '录单时间' },
  { value: '20', label: '开始时间' },
  { value: '30', label: '完成时间' },
  { value: '40', label: '收款时间' },
  { value: '50', label: '退款时间' },
] as const
const DATE_TYPE_UNWIRED_TEXT = '「录单时间」以外的日期口径尚未开放，本次按录单时间筛选'
/** 统计句的状态段顺序照证据件那句（已暂停在问题单前）；⛔ 与 ORDER_STATUS_VALUES 声明序不同。 */
const SUMMARY_STATUS_ORDER: readonly OrderStatus[] = [
  'UNASSIGNED',
  'WAITING_GRAB',
  'IN_PROGRESS',
  'PENDING_REVIEW',
  'COMPLETED',
  'PAUSED',
  'ISSUE',
  'REFUNDED',
]

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/** 新老客标签的中文短标（列表/详情共用；空串/未知值 ⇒ 不画）。 */
function customerKindLabel(kind: string | undefined): string {
  return kind === 'new' ? '新' : kind === 'old' ? '老' : ''
}

/* ── 提示（页面内自绘 toast，同 V2MyOrders；ElMessage 拿不到 .ttv2 皮）────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  // 时长按文案长度自适应：基线 3 秒、每字 +50ms、封顶 8 秒（长文案读得完，短文案不留过久）。
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, Math.min(8000, 3000 + text.length * 50))
}

/* ── 权限门（fail-closed；节点清单 = island 页 setManagePermissions 同口径）────── */

const canAssignTaker = computed(() => session.canNode(['order.assign_taker', 'order.submit_payout_price']))
const canDirectAssignTaker = computed(() => session.canNode(['order.assign_taker']))
const canClearTaker = computed(() => session.canNode(['order.clear_taker']))
const canChangePayout = computed(() => session.canNode(['order.submit_payout_price']))
const canChangeStatus = computed(() => session.canNode(['order.change_status', 'order.change_status_completed']))
const canUpdateOrder = computed(() => session.canNode(['order.add_edit', 'order.submit_edit']))
const canRefund = computed(() => session.canNode(['order.online_refund']))
const canChangePrice = computed(() => session.canNode(['order.submit_payout_price', 'order.edit_payout_price']))
const canChangeHallPrice = computed(() => session.canNode(['order.set_offer_price']))
const canChangePaymentStatus = computed(() => session.canNode(['order.change_payment_status']))
const canRecomputeCommission = computed(() => session.canNode(['finance.change_settlement_status']))
const canApplyPenalty = computed(() => session.canNode([APPROVAL_APPLY_NODE]))
const canDeleteOrders = computed(() => session.canNode(['order.delete']))
const canExportOrders = computed(() => session.canNode(['order.export']))
const canViewScreenshots = computed(() => session.canNode(['order.view_screenshot']))
const canUploadScreenshots = computed(() => session.canNode(['order.upload_screenshot']))
const canEditOrder = computed(() => session.canNode(['order.view_detail', 'order.view_content']))
const canThrowToHall = computed(() => session.canNode(['order.send_to_grab_hall']))
const canWithdrawThrow = computed(() => session.canNode(['order.withdraw_from_grab_hall']))
const canPlatformWrite = computed(() => session.canNode(['order.platform_write']))

/* ── 字典：游戏 / 区服 / 订单来源 / 客服候选 / 接手人候选 / 发平台探针 ────────── */

const games = ref<OrderDictionaries["games"]>([])
const servers = ref<OrderDictionaries["servers"]>([])
const sources = ref<OrderDictionaries["order_sources"]>([])
const sourceCatalog = ref<OrderSourceRow[]>([])
const sourceCatalogLoaded = ref(false)
const sourceGroups = ref<{ id: number; name: string }[]>([])
/** 来源候选带分组归属；分组选中时只列组内来源。字典接口只给 id/name，故另取一次来源列表。 */
type SourceOption = { id: number; name: string; group?: number | null; groups?: { id: number }[] }
const availableSources = computed<SourceOption[]>(() => {
  if (!sourceCatalogLoaded.value) return sources.value
  return filterSourcesByGroup(sourceCatalog.value, filters.sourceGroupId)
})
const kefuItems = ref<{ id: number; name: string }[]>([])
/**
 * 接手人 / 发单人候选（filter-options）。⭐ 2026-11-09 起改**关键词远程搜索**（业务方拍板
 * 「下拉全部改关键词搜，v2 也带上」）：候选不再预载（挂载 / 切游戏 / 重置都不拉名单），
 * 打开拾人面板输入关键词时防抖请求 `?search=<词>&game=<当前游戏>`，回包即候选；
 * 空关键词不发请求（面板只剩「全部」入口与提示句）。已选人名另存 `pickedNames`——
 * 搜索会整体替换两份候选，筛选按钮上已选人的名字不能跟着丢。
 */
const takerOptions = ref<{ id: number; name: string }[]>([])
const publisherOptions = ref<{ id: number; name: string }[]>([])
/** 已选接手人 / 发单人的名字缓存（id → name；候选被搜索替换后标签从这里取）。 */
const pickedNames = ref(new Map<number, string>())
let pickSearchTimer: ReturnType<typeof setTimeout> | null = null
let pickSearchSeq = 0

const gameNameOf = computed(() => {
  const map = new Map<number, string>()
  for (const g of games.value) map.set(g.id, g.name)
  return (id: number): string => map.get(id) ?? ''
})
const serverNameOf = computed(() => {
  const map = new Map<number, string>()
  for (const s of servers.value) map.set(s.id, s.name)
  return (id: number | null): string => (id === null ? '' : (map.get(id) ?? ''))
})

/** 拾人面板的关键词搜索（taker / publisher 各取 filter-options 自己那份；防抖在 watch 侧）。 */
async function runPickSearch(slot: 'taker' | 'publisher', query: string): Promise<void> {
  const trimmed = query.trim()
  const seq = ++pickSearchSeq
  const clear = (): void => {
    if (slot === 'taker') takerOptions.value = []
    else publisherOptions.value = []
  }
  if (trimmed === '') {
    // 空关键词不发请求（去预选的自然结果）：候选清空即可。
    clear()
    return
  }
  try {
    const opts = await fetchFilterOptions({ game: filters.gameId, search: trimmed })
    if (seq !== pickSearchSeq) return // 过期回包：已有更新的搜索，丢弃
    if (slot === 'taker') takerOptions.value = opts.takers
    else publisherOptions.value = opts.publishers
  } catch {
    // 候选取不到：清空（这一格筛不了），面板里如实落「没有匹配的人」；⛔ 不弹警告（辅助筛选）。
    if (seq === pickSearchSeq) clear()
  }
}

/** 「一键发单到交易平台」入口探针：失败/不可用一律不画（fail-closed）。 */
const pushAvailable = ref(false)

/* ── 筛选（服务端筛选；映射 = island toOrderSearch 的逐条重写）────────────────── */

type ManageFilters = {
  gameId: number | null
  dateType: string
  sDate: string
  eDate: string
  priceTier: string
  priceMin: string
  priceMax: string
  hallPriceMin: string
  hallPriceMax: string
  sourceId: number | null
  sourceGroupId: number | null
  statuses: string[]
  recType: '0' | '10'
  takerId: number | null
  orderType: '' | 'BOOST' | 'COMPANION'
  kefuType: '0' | '5' | '10'
  kefuId: number | null
  payStatus: string
  customerKind: '' | 'new' | 'old'
  search1Mode: '0' | '10'
  key1: string
  key2: string
  urgent: boolean
  leak: boolean
}

const START_OF_DAY = '00:00:00'
const END_OF_DAY = '23:59:59'
const START_PICKER_TIME = new Date(2000, 0, 1, 0, 0, 0)
const END_PICKER_TIME = new Date(2000, 0, 1, 23, 59, 59)
const DATE_TIME_FORMAT = 'YYYY-MM-DD HH:mm:ss'
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

function toDateInputValue(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 日期默认 = 近 3 个自然月（管理订单页口径；指派给我的订单页是 6 个月，两页不同）。 */
function defaultDateRange(now: Date = new Date()): [string, string] {
  return [
    `${toDateInputValue(new Date(now.getFullYear(), now.getMonth() - 3, now.getDate()))} ${START_OF_DAY}`,
    `${toDateInputValue(now)} ${END_OF_DAY}`,
  ]
}

const [defaultFrom, defaultTo] = defaultDateRange()

const filters = reactive<ManageFilters>({
  gameId: null,
  dateType: '10',
  sDate: defaultFrom,
  eDate: defaultTo,
  priceTier: '0',
  priceMin: '',
  priceMax: '',
  hallPriceMin: '',
  hallPriceMax: '',
  sourceId: null,
  sourceGroupId: null,
  statuses: [],
  recType: '0',
  takerId: null,
  orderType: '',
  kefuType: '0',
  kefuId: null,
  payStatus: '',
  customerKind: '',
  search1Mode: '0',
  key1: '',
  key2: '',
  urgent: false,
  leak: false,
})

watch([() => filters.sourceGroupId, sourceCatalogLoaded], () => {
  if (!sourceCatalogLoaded.value) return
  const allowed = new Set(availableSources.value.map((source) => source.id))
  if (filters.sourceId !== null && !allowed.has(filters.sourceId)) filters.sourceId = null
})

function setSDate(value: unknown): void {
  filters.sDate = typeof value === 'string' ? value : ''
}

let typedEndDateOnly = ''

function rememberTypedEndDate(event: Event): void {
  const input = event.target
  if (!(input instanceof HTMLInputElement) || input.getAttribute('aria-label') !== '结束时间') return
  const value = input.value.trim()
  typedEndDateOnly = DATE_ONLY.test(value) ? value : ''
}

function setEDate(value: unknown): void {
  // Element Plus 把直接输入的裸日期解析成午夜，结束边界仍应包含整天。
  const bareDate = typedEndDateOnly
  typedEndDateOnly = ''
  if (typeof value !== 'string') {
    filters.eDate = ''
    return
  }
  filters.eDate = bareDate !== '' && value === `${bareDate} ${START_OF_DAY}` ? `${bareDate} ${END_OF_DAY}` : value
}

function dateBoundary(value: string, fallbackTime: string): string {
  const local = DATE_ONLY.test(value) ? `${value}T${fallbackTime}` : value.replace(' ', 'T')
  return toLocalIso(new Date(local))
}

/** 已提交的批量单号，供分页、刷新和导出沿用。 */
const batchNos = ref<string | null>(null)
const dashboardOrderNo = ref<string | null>(null)

/** 筛选 → 检索参数（island toOrderSearch 逐条重写；空值由 toOrderParams 丢弃）。 */
function buildSearchParams(): OrderSearchParams {
  const params: OrderSearchParams = {}
  if (filters.gameId !== null) params.game = filters.gameId
  // key1 有值只发 keyword；否则 key2 发 project_text（island 的优先级，别改成两个都发）。
  if (batchNos.value !== null) params.batch_lookup_nos = batchNos.value
  else if (filters.key1.trim() !== '') params.keyword = filters.key1.trim()
  else if (filters.key2.trim() !== '') params.project_text = filters.key2.trim()
  if (filters.statuses.length > 0) params.status = filters.statuses.join(',')
  if (filters.payStatus !== '') params.payment_status = filters.payStatus
  if (filters.customerKind !== '') params.customer_kind = filters.customerKind
  if (filters.urgent) params.is_urgent = true
  if (filters.leak) params.is_leak = true
  if (filters.sourceId !== null) params.order_source = filters.sourceId
  if (filters.sourceGroupId !== null) params.source_group = filters.sourceGroupId
  if (filters.orderType !== '') params.order_type = filters.orderType
  if (filters.kefuId !== null) params.sales_user = filters.kefuId
  params.kefu_type = filters.kefuType
  if (filters.takerId !== null) {
    if (filters.recType === '10') params.hall_publisher_user = filters.takerId
    else params.taker_user = filters.takerId
  }
  const tier = filters.priceTier
  if (tier === '3') {
    // 接发组合：主栏两格 = 接单价区间，第二对 = 发单价区间（⛔ 不复用主栏参数名装发单价）。
    params.show_price_type = '3'
    if (filters.priceMin !== '') params.price_min = filters.priceMin
    if (filters.priceMax !== '') params.price_max = filters.priceMax
    if (filters.hallPriceMin !== '') params.hall_price_min = filters.hallPriceMin
    if (filters.hallPriceMax !== '') params.hall_price_max = filters.hallPriceMax
  } else if (tier === '4') {
    params.show_price_type = '4'
  } else {
    params.show_price_type = tier as '0' | '1' | '2'
    if (filters.priceMin !== '') params.price_min = filters.priceMin
    if (filters.priceMax !== '') params.price_max = filters.priceMax
  }
  if (batchNos.value === null && dashboardOrderNo.value !== null) params.order_nos = dashboardOrderNo.value
  if (filters.sDate !== '') {
    params.date_from = dateBoundary(filters.sDate, START_OF_DAY)
  }
  if (filters.eDate !== '') {
    params.date_to = dateBoundary(filters.eDate, END_OF_DAY)
  }
  return params
}

function onGameChange(): void {
  filters.takerId = null
  page.value = 1
}

function clearFilters(): void {
  filters.gameId = null
  filters.dateType = '10'
  filters.sDate = defaultFrom
  filters.eDate = defaultTo
  filters.priceTier = '0'
  filters.priceMin = ''
  filters.priceMax = ''
  filters.hallPriceMin = ''
  filters.hallPriceMax = ''
  filters.sourceId = null
  filters.sourceGroupId = null
  filters.statuses = []
  filters.recType = '0'
  filters.takerId = null
  filters.orderType = ''
  filters.kefuType = '0'
  filters.kefuId = null
  filters.payStatus = ''
  filters.customerKind = ''
  filters.search1Mode = '0'
  filters.key1 = ''
  filters.key2 = ''
  filters.urgent = false
  filters.leak = false
  batchNos.value = null
  dashboardOrderNo.value = null
}

function resetFilters(): void {
  clearFilters()
  void reload(1)
}

/* ── 取数（服务端分页；统计句 = data.summary 真值）──────────────────────────── */

const rows = ref<OrderListItem[]>([])
const total = ref(0)
const summary = ref<OrderListSummary | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const penaltyLoadFailed = ref(false)

const PAGE_LIMITS = [10, 20, 30, 40, 50]
const page = ref(1)
const pageSize = ref(20)
const splitMembersCache = ref<Record<string, OrderListItem[]>>({})
const penaltyChildrenCache = ref<Record<string, OrderListItem[]>>({})
const PENALTY_REQUEST_BATCH_SIZE = 5

type DisplayOrder = OrderListItem & {
  __splitLine?: 'start' | 'branch' | 'end'
  __underPenaltyParent?: true
  __penaltyPreviewFor?: string
}

const primaryGroups = computed(() => {
  const groups: OrderListItem[][] = []
  const displayedGroups = new Set<string>()
  for (const row of rows.value) {
    const splitId = row.split_id ?? ''
    const members = splitMembersCache.value[splitId] ?? []
    if (members.length < 2) {
      groups.push([row])
      continue
    }
    if (displayedGroups.has(splitId)) continue
    displayedGroups.add(splitId)
    const main = members.find((member) => member.split_is_balance_anchor) ?? row
    groups.push([main, ...members.filter((member) => member.order_no !== main.order_no)])
  }
  return groups
})

const displayRows = computed(() => {
  const result: DisplayOrder[] = []
  const primaryOrderNos = new Set(primaryGroups.value.flat().map((row) => row.order_no))
  const displayedOrders = new Set<string>()

  function append(groupRows: DisplayOrder[], row: DisplayOrder): void {
    if (displayedOrders.has(row.order_no)) return
    displayedOrders.add(row.order_no)
    groupRows.push(row)
    for (const child of penaltyChildrenCache.value[row.order_no] ?? []) {
      if (child.split_id || primaryOrderNos.has(child.order_no) || displayedOrders.has(child.order_no)) {
        // 单列订单和普通拆单组保留自己的操作行，此处只链接到详情。
        groupRows.push({ ...child, __underPenaltyParent: true, __penaltyPreviewFor: row.order_no })
      } else {
        append(groupRows, { ...child, __underPenaltyParent: true })
      }
    }
  }

  for (const group of primaryGroups.value) {
    const groupRows: DisplayOrder[] = []
    group.forEach((row) => append(groupRows, row))
    groupRows.forEach((row, index) => {
      const line = groupRows.length === 1 ? undefined : index === 0 ? 'start' : index === groupRows.length - 1 ? 'end' : 'branch'
      result.push({ ...row, __splitLine: line })
    })
  }
  return result
})

let reloadSequence = 0

async function loadPenaltyChildren(searchParams: OrderSearchParams, sequence: number): Promise<void> {
  const primaryRows = primaryGroups.value.flat()
  const primaryOrderNos = new Set(primaryRows.map((row) => row.order_no))
  const queued = new Set<string>()
  const queue: string[] = []

  function enqueue(row: OrderListItem): void {
    if (!row.penalty_child_count || queued.has(row.order_no)) return
    queued.add(row.order_no)
    queue.push(row.order_no)
  }

  primaryRows.forEach(enqueue)
  for (let offset = 0; offset < queue.length;) {
    const batch = queue.slice(offset, offset + PENALTY_REQUEST_BATCH_SIZE)
    offset += batch.length
    const responses = await Promise.allSettled(batch.map((no) => fetchPenaltyChildren(no, searchParams)))
    if (sequence !== reloadSequence) return
    responses.forEach((response, index) => {
      if (response.status === 'rejected') {
        if (!penaltyLoadFailed.value) showToast(errText(response.reason), 'error')
        penaltyLoadFailed.value = true
        return
      }
      const children = response.value
      penaltyChildrenCache.value[batch[index]!] = children
      children.filter((child) => !child.split_id && !primaryOrderNos.has(child.order_no)).forEach(enqueue)
    })
  }
}

let openedDashboardOrder = ''
async function reload(targetPage = 1): Promise<void> {
  const sequence = ++reloadSequence
  checkedNos.value = []
  penaltyChildrenCache.value = {}
  penaltyLoadFailed.value = false
  const searchParams = buildSearchParams()
  const includeUnmatchedSplitMembers = route.query.self_sales !== '1' && batchNos.value === null
  loading.value = true
  try {
    const result = await fetchOrdersWithSummary({
      ...searchParams,
      group_penalty_children: true,
      include_page_relations: true,
      include_unmatched_split_members: includeUnmatchedSplitMembers,
      page: targetPage,
      page_size: pageSize.value,
    })
    if (sequence !== reloadSequence) return
    rows.value = result.items
    const targetOrder = typeof route.query.open_order === 'string' ? route.query.open_order : ''
    if (route.query.self_sales === '1' && isOrderNoShape(targetOrder) && openedDashboardOrder !== route.fullPath) {
      const target = result.items.find((row) => row.order_no === targetOrder)
      if (target) {
        openedDashboardOrder = route.fullPath
        void openDetail(target)
      }
    }
    splitMembersCache.value = {}
    total.value = result.total
    summary.value = result.summary
    page.value = result.page
    if (result.page_relations) {
      splitMembersCache.value = result.page_relations.split_members_by_id
      penaltyChildrenCache.value = result.page_relations.penalty_children_by_parent
      loadError.value = null
      return
    }
    // 旧后端仍可用原来的拆单与罚款展开接口。
    const splitIds = [...new Set(result.items
      .filter((row) => (row.split_members ?? 0) > 1 && row.split_id)
      .map((row) => row.split_id as string))]
    const membersByGroup: Record<string, OrderListItem[]> = {}
    await Promise.all(splitIds.map(async (splitId) => {
      try {
        membersByGroup[splitId] = await fetchSplitMembers(splitId, {
          ...searchParams,
          group_penalty_children: true,
          include_unmatched_split_members: includeUnmatchedSplitMembers,
        })
      } catch (e) {
        if (sequence === reloadSequence) showToast(errText(e), 'error')
      }
    }))
    if (sequence !== reloadSequence) return
    splitMembersCache.value = membersByGroup
    loadError.value = null
    void loadPenaltyChildren(searchParams, sequence)
  } catch (e) {
    if (sequence !== reloadSequence) return
    rows.value = []
    total.value = 0
    summary.value = null
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    if (sequence === reloadSequence) loading.value = false
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

/** 查询：时间类型未接线的档位提示一句（island collectSearch 同文案），其余照发。 */
function onSearch(): void {
  dashboardOrderNo.value = null
  if (filters.dateType !== '10') showToast(DATE_TYPE_UNWIRED_TEXT)
  if (filters.search1Mode === '10') {
    openBatchSearch()
    return
  }
  batchNos.value = null
  void reload(1)
}

/* ── 统计句（后端聚合真值；金额原样两位小数，⛔ 前端不格式化不再算）────────── */

/** 句子前半（到「。其中：」为止）。整句拆成「前半 + 状态段」是为了把状态词变可点
 * （onSummaryStatusClick）；两段拼出的可见文本与原整句逐字一致。 */
const summaryLead = computed(() => {
  const s = summary.value
  if (s === null) return ''
  // ⚠️ 总退款/含已退单部分两字段是后端新加的：旧后端 summary 不带 ⇒ 那两段整段省去
  // （⛔ 不显示 undefined、也不编 0.00），新后端自动出全句。
  const refundPart = s.total_refund_fee === undefined ? '' : `总退款${s.total_refund_fee}元，`
  const profitPart =
    s.refunded_diff === undefined
      ? `预计利润${s.diff}元`
      : `预计利润${s.diff}元(含已退单部分${s.refunded_diff}元)`
  return (
    `本次搜索统计共${s.count}笔，预计总发单价${s.total_hall_price}元(不含接单提成)，` +
    `总接单价${s.total_price}元，${refundPart}${profitPart}。其中：`
  )
})

/** 「状态词+数量」段（顺序照 SUMMARY_STATUS_ORDER；每段可点，见 onSummaryStatusClick）。 */
const summarySegments = computed(() => {
  const s = summary.value
  if (s === null) return []
  const counts = s.status_counts ?? {}
  return SUMMARY_STATUS_ORDER.map((code) => ({
    code,
    text: `${v2OrderStatusLabel(code)}${counts[code] ?? 0}笔`,
  }))
})

/** 状态段点击三态：点 = 只筛该状态；再点同一个 = 清除回全部；点别的 = 切到那个状态。
 * filters.statuses 同时是「订单状态」下拉的 v-model ⇒ 下拉显示自动同步。 */
function onSummaryStatusClick(code: OrderStatus): void {
  const isActive = filters.statuses.length === 1 && filters.statuses[0] === code
  filters.statuses = isActive ? [] : [code]
  void reload(1)
}

function isSummaryStatusActive(code: OrderStatus): boolean {
  return filters.statuses.length === 1 && filters.statuses[0] === code
}

/* ── 勾选（工具栏动作只吃勾选的行；表头勾 = 当前页全部）──────────────────────── */

const checkedNos = ref<string[]>([])
let selectionVersion = 0

function isChecked(no: string): boolean {
  return checkedNos.value.includes(no)
}

function toggleRow(no: string): void {
  if (loading.value) return
  selectionVersion++
  const i = checkedNos.value.indexOf(no)
  if (i === -1) checkedNos.value.push(no)
  else checkedNos.value.splice(i, 1)
}

const pageCheckableRows = computed(() => displayRows.value.filter(
  (row) => row.__splitLine !== 'branch' && row.__splitLine !== 'end',
))
const pageAllChecked = computed(
  () => pageCheckableRows.value.length > 0 && pageCheckableRows.value.every((row) => isChecked(row.order_no)),
)

function togglePageAll(): void {
  if (loading.value) return
  selectionVersion++
  if (pageAllChecked.value) {
    checkedNos.value = []
  } else {
    for (const r of pageCheckableRows.value) {
      if (!isChecked(r.order_no)) checkedNos.value.push(r.order_no)
    }
  }
}

const checkedRows = computed(() => displayRows.value.filter((r) => !r.__penaltyPreviewFor && isChecked(r.order_no)))

/** 有活跃平台订单的单（isRoll 判据 `platform_push.is_active === true`；这类单不能手工改状态）。 */
function hasActivePush(d: OrderListItem): boolean {
  return d.platform_push?.is_active === true
}

const hoverPreview = ref<{ text: string; x: number; y: number } | null>(null)
const hoverCard = ref<HTMLElement | null>(null)
let hoverCloseTimer: ReturnType<typeof setTimeout> | null = null
function keepProjectPreview(): void {
  if (hoverCloseTimer !== null) clearTimeout(hoverCloseTimer)
  hoverCloseTimer = null
}
function closeProjectPreview(): void {
  keepProjectPreview()
  hoverCloseTimer = setTimeout(() => { hoverPreview.value = null; hoverCloseTimer = null }, 150)
}
onUnmounted(keepProjectPreview)
function showProjectPreview(event: MouseEvent, text: string): void {
  if (!text || orderTextDlg.value !== null) return
  keepProjectPreview()
  const clientX = event.clientX
  const clientY = event.clientY
  hoverPreview.value = {
    text,
    x: clientX + 14,
    y: 8,
  }
  void nextTick(() => {
    if (hoverPreview.value === null || hoverPreview.value.text !== text || hoverCard.value === null) return
    const { width, height } = hoverCard.value.getBoundingClientRect()
    hoverPreview.value.x = clientX + 14 + width > window.innerWidth - 8
      ? Math.max(8, clientX - width - 14)
      : clientX + 14
    hoverPreview.value.y = Math.max(8, Math.min(clientY + 16, window.innerHeight - height - 8))
  })
}

const ORDER_TEXT_LABELS = { project_text: '代练/陪玩内容', remark: '重要备注' } as const
type OrderTextField = keyof typeof ORDER_TEXT_LABELS
const orderTextDlg = ref<{ orderNo: string; splitId: string; field: OrderTextField; value: string } | null>(null)
const orderTextSaving = ref(false)

function openOrderText(row: OrderListItem, field: OrderTextField): void {
  if (orderTextSaving.value) return
  keepProjectPreview()
  hoverPreview.value = null
  orderTextDlg.value = { orderNo: row.order_no, splitId: row.split_id ?? '', field, value: row[field] ?? '' }
}

function closeOrderText(): void {
  if (!orderTextSaving.value) orderTextDlg.value = null
}

async function copyOrderText(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    showToast('已复制')
  } catch {
    showToast('复制失败，请手动选中复制', 'error')
  }
}

async function saveOrderText(): Promise<void> {
  const draft = orderTextDlg.value
  if (draft === null || orderTextSaving.value || !canUpdateOrder.value) return
  const { field, orderNo, splitId, value } = draft
  if (field === 'project_text') {
    const error = value.trim() === '' ? '代练/陪玩内容不能为空' : projectTextSyncLimitError(value.trim())
    if (error !== '') {
      showToast(error, 'error')
      return
    }
  }

  orderTextSaving.value = true
  try {
    const saved = await updateOrder(orderNo, { [field]: value })
    const row = rows.value.find((item) => item.order_no === orderNo)
    const member = splitMembersCache.value[splitId]?.find((item) => item.order_no === orderNo)
    if (row !== undefined) row[field] = saved[field]
    if (member !== undefined) member[field] = saved[field]
    for (const children of Object.values(penaltyChildrenCache.value)) {
      const child = children.find((item) => item.order_no === orderNo)
      if (child !== undefined) child[field] = saved[field]
    }
    orderTextDlg.value = null
    showToast('保存成功')
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    orderTextSaving.value = false
  }
}

/* ── 列宽拖拽（共享 tableResize；时间列 16 字形给 168、状态签列 128 防截断）+ 列显隐 ── */

const MANAGE_COLUMNS = [
  'check', 'order', 'sysno', 'role', 'project', 'info', 'status', 'hour', 'ww', 'source', 'taker',
  'hallprice', 'price', 'net', 'refund', 'remark', 'shots', 'kefu', 'pub', 'by', 'create', 'complete', 'actions',
] as const

/** 列显隐选择器的标题表（顺序 = 表头顺序，照原版字段选择清单；check 列不进选择器）。 */
/** ⛔ 'project'（代练/陪玩内容）刻意不在清单：该列**钉死常显**（业务方两轮追「代练内容根本
 * 看不见了」）——不进选择器、任何存储状态都过滤，列必显。 */
const COL_TITLES: Record<string, string> = {
  order: '订单号',
  sysno: '系统内置单号',
  role: '角色名',
  info: '游戏区服',
  status: '状态',
  hour: '时间',
  ww: '旺旺/来源',
  source: '订单来源',
  taker: '接手人',
  hallprice: '发单价',
  price: '接单价',
  net: '实收款',
  refund: '退款',
  remark: '重要备注',
  shots: '订单截图',
  kefu: '销售客服',
  pub: '发单人',
  by: '录单人',
  create: '录单时间',
  complete: '完单时间',
  actions: '操作',
}

const HIDDEN_COLS_KEY = 'tt.v2.order-manage.hidden-cols.v3'

/** 默认藏系统单号、完单时间、角色名、时间和实收款；默认集变更时升级存储键。 */
const HIDDEN_COLS_DEFAULT = ['sysno', 'complete', 'role', 'hour', 'net']

function loadHiddenCols(): string[] {
  try {
    const raw = localStorage.getItem(HIDDEN_COLS_KEY)
    if (raw === null) return [...HIDDEN_COLS_DEFAULT]
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return [...HIDDEN_COLS_DEFAULT]
    // 只认合法列键（存储可能被手改）；actions 勾不允许藏（没有操作列就没法恢复入口之外的操作）。
    return parsed.filter(
      (k): k is string => typeof k === 'string' && k in COL_TITLES && k !== 'actions',
    )
  } catch {
    return [...HIDDEN_COLS_DEFAULT]
  }
}

/** 默认藏 = 系统内置单号 + 完单时间（照原版字段选择器的默认勾选）。 */
const hiddenCols = ref<string[]>(loadHiddenCols())

function saveHiddenCols(): void {
  try {
    localStorage.setItem(HIDDEN_COLS_KEY, JSON.stringify(hiddenCols.value))
  } catch { /* 存储禁写：本次会话仍生效 */ }
}

function colVisible(key: string): boolean {
  return !hiddenCols.value.includes(key)
}

/** 显隐生效后的列序列（colgroup / 空行 colspan 共用）。 */
const showScreenshotColumn = computed(() => colVisible('shots') && (canViewScreenshots.value || canUploadScreenshots.value))
const visibleColumns = computed(() => MANAGE_COLUMNS.filter((k) =>
  k === 'shots' ? showScreenshotColumn.value : colVisible(k),
))

/** 列显隐 = el-checkbox-group（EP 组件化，业务方「换一种组件展示」）：v-model = 可见列键，
 * set 时换算回 hiddenCols（localStorage 键与语义不变）。 */
const visibleColKeys = computed<string[]>({
  get: () => (Object.keys(COL_TITLES) as string[]).filter((k) => colVisible(k)),
  set: (keys) => {
    hiddenCols.value = (Object.keys(COL_TITLES) as string[]).filter((k) => !keys.includes(k))
    saveHiddenCols()
  },
})

/* ── 人选「面板内搜索」选择器（面板里搜索框 + 列表）──
 * 接手人/发单人：候选 = 关键词远程搜索（见 `runPickSearch`）；客服：候选预载 ≤200、本地 includes。 */
type PickSlot = 'taker' | 'publisher' | 'kefu'
const pickOpen = ref<PickSlot | null>(null)
const pickQuery = ref('')

const takerLabel = computed(() => {
  if (filters.recType !== '0' || filters.takerId === null) return '请选择'
  return pickedNames.value.get(filters.takerId) ?? '请选择'
})

const publisherLabel = computed(() => {
  if (filters.recType !== '10' || filters.takerId === null) return '请选择'
  return pickedNames.value.get(filters.takerId) ?? '请选择'
})

const kefuLabel = computed(() => {
  if (filters.kefuId === null) return '请选择'
  return kefuItems.value.find((o) => o.id === filters.kefuId)?.name ?? (filters.kefuId === session.user?.id ? session.user.display_name || session.user.username : '请选择')
})

const pickedFiltered = computed(() => {
  if (pickOpen.value === 'taker' || pickOpen.value === 'publisher') {
    // 打手 / 发单人：候选 = 服务端关键词搜索的结果（后端已按词筛 ⇒ 不再本地过滤）。
    return pickOpen.value === 'taker' ? takerOptions.value : publisherOptions.value
  }
  const q = pickQuery.value.trim()
  return q === '' ? kefuItems.value : kefuItems.value.filter((o) => o.name.includes(q))
})

function togglePick(slot: PickSlot): void {
  if (pickOpen.value === slot) {
    pickOpen.value = null
    return
  }
  pickOpen.value = slot
  pickQuery.value = ''
  // 打开即清上一轮候选（回到空词态）+ 撤掉还没发的防抖请求：候选 = 关键词搜索的结果，
  // 上一次搜索的残留不该在新面板里闪一下（空关键词不发请求，见 runPickSearch）。
  if (pickSearchTimer !== null) {
    clearTimeout(pickSearchTimer)
    pickSearchTimer = null
  }
  if (slot === 'taker') takerOptions.value = []
  else if (slot === 'publisher') publisherOptions.value = []
}

/** 关键词输入 → 防抖 400ms → 远程搜索（与管理订单旧页同款拍板值；空词不发，候选即清空）。 */
watch(pickQuery, (q) => {
  const slot = pickOpen.value
  if (slot !== 'taker' && slot !== 'publisher') return
  if (pickSearchTimer !== null) clearTimeout(pickSearchTimer)
  pickSearchTimer = setTimeout(() => {
    pickSearchTimer = null
    void runPickSearch(slot, q)
  }, 400)
})

function pickPerson(id: number | null): void {
  const slot = pickOpen.value
  if (slot === 'taker') {
    filters.recType = '0'
    filters.takerId = id
  } else if (slot === 'publisher') {
    filters.recType = '10'
    filters.takerId = id
  } else if (slot === 'kefu') {
    filters.kefuId = id
  }
  // 记住选中人的名字：搜索会整体替换候选，按钮上的标签从缓存取（⛔ 不能跟着候选丢）。
  if (id !== null && (slot === 'taker' || slot === 'publisher')) {
    const name = (slot === 'taker' ? takerOptions : publisherOptions).value.find(
      (o) => o.id === id,
    )?.name
    if (name !== undefined) pickedNames.value.set(id, name)
  }
  pickOpen.value = null
}

function onWindowClickClosePick(event: MouseEvent): void {
  if (route.fullPath !== router.currentRoute.value.fullPath) return
  const target = event.target as HTMLElement | null
  if (target?.closest('.flt-pickcell, .cv2-tab') !== null) return
  pickOpen.value = null
}

const { widths, colStyle, startResize, resetColumn } = useColumnWidths('order-manage', {
  check: 36,
  // 来源号 20 位 mono + 复制钮（my-orders 2026-11-06 起单独收 206，本页维持 216）。
  order: 216,
  // ⛔ 代练内容必须有实体默认宽：它曾是唯一弹性列，而定宽列总和(≈2520)远超容器 ⇒
  // fixed 布局把弹性列挤成 0px =「代练内容看不见」的真凶（storage 怀疑是错判）。
  project: 320,
  sysno: 150,
  role: 96,
  info: 120,
  status: 112,
  hour: 90,
  ww: 84,
  source: 84,
  taker: 84,
  hallprice: 88,
  price: 96,
  net: 84,
  refund: 60,
  remark: 110,
  shots: 88,
  kefu: 76,
  pub: 76,
  create: 168,
  complete: 168,
  by: 76,
  actions: 114,
})

// 旧版拖动任一列都会把当时的操作列默认宽一起存下来。
const savedWidths = loadColumnWidths('order-manage')
if (savedWidths.actions === 176 && MANAGE_COLUMNS.every((key) => key === 'check' || savedWidths[key] !== undefined)) {
  resetColumn('actions')
}

const visibleTableWidth = computed(() => visibleColumns.value.reduce((sum, key) => sum + (widths[key] ?? 0), 0))
// 把多余的宽度留给内容列，避免固定操作列跟着表格一起摊宽。
const fillColumn = computed(() => colVisible('project') ? 'project' : colVisible('order') ? 'order' : 'check')
function manageColStyle(key: string) {
  if (key !== fillColumn.value) return colStyle(key)
  const width = widths[key] ?? 0
  return { width: `max(${width}px, calc(100% - ${visibleTableWidth.value - width}px))` }
}

/* ── 单元格展示（island 各 row templet 的同口径重写）──────────────────────────── */

function viewNoOf(d: OrderListItem): string {
  return d.source_order_no || d.order_no
}

function timeText(iso: string | null): string {
  return iso === null ? '' : iso.replace('T', ' ').slice(0, 16)
}

function moneyText(v: string | null): string {
  return v === null ? '0.00' : v
}

function statusText(status: string): string {
  return v2OrderStatusLabel(status)
}

/** 一键复制（剪贴板 + toast 回执；不可用静默）。 */
async function copyOrderNo(no: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(no)
    showToast('已复制单号')
  } catch {
    /* 剪贴板不可用 */
  }
}

/* ── 弹层：截图 / 发平台仍走 island 页；详情 / 编辑是本页自己的表 ────────────── */

const ISLAND_PAGE_BASE = `${import.meta.env.BASE_URL}layui-island/page.html`

function islandUrl(page: string, orderNo: string): string {
  return `${ISLAND_PAGE_BASE}?page=${page}&order_no=${encodeURIComponent(orderNo)}`
}

const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'

/** 发平台 iframe 高度自适应（同源读内部文档；延迟两拍等字体/图片）。 */
function fitIslandFrame(e: Event): void {
  const f = e.target as HTMLIFrameElement
  const fit = (): void => {
    try {
      const doc = f.contentDocument
      if (doc?.documentElement) f.style.height = doc.documentElement.scrollHeight + 'px'
    } catch { /* 非同源时静默 */ }
  }
  fit()
  window.setTimeout(fit, 300)
  window.setTimeout(fit, 900)
}

/** 只换发单页的颜色和按钮皮，格子、按钮位置、字段顺序都不动。色从 .ttv2 现算，不写死色值。 */
function skinPushFrame(e: Event): void {
  const frame = e.target as HTMLIFrameElement
  const doc = frame.contentDocument
  const host = document.querySelector('.ttv2')
  if (!doc?.head || host === null) return
  const pick = (name: string): string => getComputedStyle(host).getPropertyValue(name).trim()
  const ink = pick('--tt2-ink')
  const line = pick('--tt2-filter-line')
  const rice = pick('--tt2-filter-label-bg')
  const panel = pick('--tt2-panel')
  const money = pick('--tt2-money-red')
  const primary = pick('--tt2-primary')
  const onPrimary = pick('--tt2-on-primary')
  const accept = pick('--tt2-st-accept')
  if (ink === '' || line === '' || rice === '') return
  let style = doc.getElementById('ttv2-push-skin')
  if (style === null) {
    style = doc.createElement('style')
    style.id = 'ttv2-push-skin'
    doc.head.appendChild(style)
  }
  style.textContent = `
    body { background: ${panel}; color: ${ink}; }
    .layui-elem-quote { background: ${rice} !important; border-color: ${line} !important; color: ${ink} !important; }
    .layui-elem-quote span[style*="ff6a00"] { color: ${primary} !important; }
    .layui-elem-quote span[style*="ff0000"] { color: ${money} !important; }
    .layui-form-pane .layui-form-label { background: ${rice} !important; border-color: ${line} !important; color: ${ink} !important; }
    .layui-form-pane .layui-form-label.rd,
    label[style*="ff6a00"] { color: ${money} !important; }
    .layui-input, .layui-textarea, .layui-form-select .layui-input, .tt-zone-select {
      border-color: ${line} !important; background: ${panel} !important; box-shadow: none !important; color: ${ink};
    }
    #throwFee, #safeAmount, #effAmount, #throwFeeHome { color: ${money} !important; }
    .tt-zone-tip, .tt-price-hint, .tt-delay-note, .tt-push-note { color: ${money} !important; }
    .tt-preview-block, .tt-preview-warnings, .tt-defaults-warning, .tt-platform-summary {
      color: ${money} !important; background: ${rice} !important; border-color: ${line} !important;
    }
    .layui-btn { background-image: none !important; box-shadow: none !important; }
    .layui-btn-normal { background: ${primary} !important; color: ${onPrimary} !important; }
    .layui-btn-lg.layui-btn-normal { background: ${accept} !important; }
    .layui-btn-warm { background: ${panel} !important; color: ${ink} !important; box-shadow: inset 0 0 0 1px ${line} !important; }
    .layui-btn-danger, a.newtemp { background: ${panel} !important; color: ${money} !important; box-shadow: inset 0 0 0 1px ${money} !important; }
    .layui-layer-msg, .layui-layer-msg .layui-layer-content { background: ${rice} !important; color: ${money} !important; }
  `
}

/** 订单详情 = 原生双栏（左订单内容 / 右日志），弃 island 旧页 iframe（业务方 2026-11-05
 * 「订单详情页面也需要重新设计一下 左侧展示订单内容 右侧订单日志相关的东西」）。
 * 日志数据 = `/audit/` 一份两签（订单日志/操作日志同源，island 同口径）；添加走 `createOrderNote`。 */
const detailDlg = ref<{ row: OrderListItem } | null>(null)
let detailSequence = 0
const detailOrder = ref<Order | null>(null)
/** 详情行显示的新老客标签：优先最新详情响应，回落到列表行快照（两者都是同一列的读面）。 */
const detailCustomerKind = computed(
  () => detailOrder.value?.customer_kind ?? detailDlg.value?.row.customer_kind ?? '',
)
const detailLogs = ref<AuditEntry[]>([])
const detailLogsLoading = ref(false)
const detailTab = ref<'note' | 'action'>('note')
const noteText = ref('')
const noteAdding = ref(false)
const canAddLog = computed(() => session.canNode(['order.add_log']))

/** `created_at`（UTC ISO）→ 本地 `MM-DD HH:mm`（island formatLogTime 同式；⛔ 不直切字符串）。 */
function logTime(iso: string): string {
  const d = new Date(iso)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function openDetail(d: OrderListItem): Promise<void> {
  if (!isOrderNoShape(d.order_no)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  const sequence = ++detailSequence
  detailDlg.value = { row: d }
  detailOrder.value = null
  detailLogs.value = []
  detailTab.value = 'note'
  noteText.value = ''
  detailLogsLoading.value = true
  try {
    const [order, logs] = await Promise.all([
      fetchOrder(d.order_no),
      fetchOrderAudit(d.order_no),
    ])
    // 行可能是旧一拍的快照：弹窗开着就以最新请求回填（比对单号防串）。
    if (sequence === detailSequence && detailDlg.value !== null) {
      detailOrder.value = order
      detailLogs.value = logs
    }
  } catch (e) {
    if (sequence === detailSequence) showToast(errText(e), 'error')
  } finally {
    if (sequence === detailSequence) detailLogsLoading.value = false
  }
}

function closeDetail(): void {
  detailSequence++
  detailDlg.value = null
  detailLogsLoading.value = false
}

const SPLIT_PROJECT_AUTOSIZE = { minRows: 1, maxRows: 6 }

type SplitDraft = { project_text: string; total_price: string }
const splitDlg = ref<{ order: Order; members: OrderListItem[]; mainOrderNo: string } | null>(null)
const splitMainMember = computed(() => splitDlg.value?.members.find((item) => item.order_no === splitDlg.value?.mainOrderNo) ?? null)
const splitOtherMembers = computed(() => splitDlg.value?.members.filter((item) => item.order_no !== splitDlg.value?.mainOrderNo && item.order_no !== splitDlg.value?.order.order_no) ?? [])
const splitRemainingText = ref('')
const splitSourcePrice = ref('')
const splitDrafts = ref<SplitDraft[]>([])
const splitExistingDrafts = ref<Record<string, string>>({})
const splitExistingPrices = ref<Record<string, string>>({})
const splitSaving = ref(false)
const canAddSplitOrder = computed(() => session.canNode(['order.add_edit']))
const splitTotals = computed(() => {
  const centsOf = (raw: string): bigint | null => {
    const match = /^(-?)(\d+)(?:\.(\d{0,2}))?$/.exec(raw.trim())
    if (match === null) return null
    const frac = (match[3] ?? '').padEnd(2, '0')
    const amount = BigInt(match[2] ?? '0') * 100n + BigInt(frac || '0')
    return match[1] === '-' ? -amount : amount
  }
  let group = 0n
  for (const member of splitDlg.value?.members ?? []) group += centsOf(member.total_price) ?? 0n
  let adjustment = 0n
  const sourceOrder = splitDlg.value?.order
  const main = splitMainMember.value
  if (sourceOrder !== undefined && main !== null && sourceOrder.order_no !== main.order_no) {
    adjustment += (centsOf(splitSourcePrice.value) ?? centsOf(sourceOrder.total_price) ?? 0n) - (centsOf(sourceOrder.total_price) ?? 0n)
  }
  for (const member of splitOtherMembers.value) {
    adjustment += (centsOf(splitExistingPrices.value[member.order_no] ?? member.total_price) ?? centsOf(member.total_price) ?? 0n) - (centsOf(member.total_price) ?? 0n)
  }
  let newCents = 0n
  for (const item of splitDrafts.value) {
    newCents += centsOf(item.total_price) ?? 0n
  }
  const sourceIsAnchor = sourceOrder?.order_no === main?.order_no
  const projectedMain = (centsOf(main?.total_price ?? sourceOrder?.total_price ?? '0') ?? 0n) - adjustment - (sourceIsAnchor ? newCents : 0n)
  const projectedSource = sourceIsAnchor
    ? projectedMain
    : (centsOf(splitSourcePrice.value) ?? centsOf(sourceOrder?.total_price ?? '0') ?? 0n) - newCents
  const money = (value: bigint): string => `${value < 0n ? '-' : ''}${(value < 0n ? -value : value) / 100n}.${String((value < 0n ? -value : value) % 100n).padStart(2, '0')}`
  return { group: money(group), added: money(newCents), projectedMain: money(projectedMain), projectedSource: money(projectedSource), finalGroup: money(group) }
})

async function openSplitDialog(order: Order): Promise<void> {
  try {
    const members = order.split_id ? await fetchSplitMembers(order.split_id) : [{ ...order, split_is_balance_anchor: true } as unknown as OrderListItem]
    const mainOrderNo = members.find((item) => item.split_is_balance_anchor)?.order_no ?? order.order_no
    splitDlg.value = { order, members, mainOrderNo }
    splitRemainingText.value = order.project_text
    splitSourcePrice.value = order.total_price
    splitDrafts.value = []
    splitExistingDrafts.value = Object.fromEntries(
      members.filter((member) => member.order_no !== order.order_no).map((member) => [member.order_no, member.project_text]),
    )
    splitExistingPrices.value = Object.fromEntries(
      members.filter((member) => member.order_no !== order.order_no && member.order_no !== mainOrderNo).map((member) => [member.order_no, member.total_price]),
    )
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

function openRowEdit(row: OrderListItem): void {
  openEdit(row)
}

function addSplitDraft(): void {
  splitDrafts.value.push({ project_text: '', total_price: '0.00' })
}

function removeSplitDraft(index: number): void {
  splitDrafts.value.splice(index, 1)
}

async function saveSplit(): Promise<void> {
  const draft = splitDlg.value
  if (draft === null || splitSaving.value) return
  splitSaving.value = true
  try {
    const newOrders = splitDrafts.value.map((item) => ({
      project_text: item.project_text.trim(),
      total_price: item.total_price.trim(),
    }))
    if (newOrders.some((item) => item.project_text === '' || item.total_price === '')) {
      throw new Error('请填写每条拆出项目和接单价')
    }
    const existingOrders = draft.members.filter((member) => member.order_no !== draft.order.order_no).flatMap((member) => {
      const projectText = splitExistingDrafts.value[member.order_no] ?? member.project_text
      const totalPrice = splitExistingPrices.value[member.order_no] ?? member.total_price
      return projectText !== member.project_text || totalPrice !== member.total_price
        ? [{ order_no: member.order_no, ...(projectText !== member.project_text ? { project_text: projectText } : {}), ...(totalPrice !== member.total_price ? { total_price: totalPrice } : {}) }]
        : []
    })
    const sourceContentChanged = draft.order.split_id !== '' && splitRemainingText.value !== draft.order.project_text
    const sourcePriceChanged = draft.order.order_no !== draft.mainOrderNo && splitSourcePrice.value !== draft.order.total_price
    if (newOrders.length === 0 && existingOrders.length === 0 && !sourceContentChanged && !sourcePriceChanged) {
      throw new Error('请修改发起订单内容、已有子单内容或新增拆出订单')
    }
    await splitPeerOrder(draft.order.order_no, {
      remaining_project_text: splitRemainingText.value,
      new_orders: newOrders,
      ...(sourcePriceChanged ? { source_total_price: splitSourcePrice.value.trim() } : {}),
      ...(existingOrders.length > 0 ? { existing_orders: existingOrders } : {}),
    })
    showToast('拆分已保存')
    splitDlg.value = null
    await reload(page.value)
    if (detailDlg.value !== null) {
      const updatedOrder = await fetchOrder(detailDlg.value.row.order_no)
      detailOrder.value = updatedOrder
      detailDlg.value = {
        row: {
          ...detailDlg.value.row,
          project_text: updatedOrder.project_text,
          total_price: updatedOrder.total_price,
        },
      }
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    splitSaving.value = false
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
    const row = await createOrderNote(dlg.row.order_no, noteText.value)
    detailLogs.value = [row, ...detailLogs.value]
    noteText.value = ''
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    noteAdding.value = false
  }
}

/** 编辑弹层 = v2 录单页组件的编辑态（`:order-no` 回填、保存走 PATCH、`saved` 事件关层）。 */
const editDlg = ref<{ orderNo: string; viewNo: string; allowSplitPrice: boolean } | null>(null)

function openEdit(d: OrderListItem): void {
  if (!isOrderNoShape(d.order_no)) {
    showToast('订单号格式不正确，无法打开编辑', 'error')
    return
  }
  editDlg.value = {
    orderNo: d.order_no,
    viewNo: viewNoOf(d),
    allowSplitPrice: Boolean(d.split_id) && d.split_is_balance_anchor !== true && canChangePrice.value,
  }
}

/** 编辑保存成功（子组件 saved 事件）→ 关层 + 刷新列表。 */
function onEditSaved(message: string): void {
  editDlg.value = null
  showToast(message || '保存成功')
  void reload(page.value)
}

/** 发平台子页发单成功 postMessage → 关层 + 刷新列表。 */
const PUSH_JDP_SENT_MESSAGE_TYPE = 'island:push-jdp-sent'

function onPushMessage(event: MessageEvent): void {
  if (!pushFrame.value?.contentWindow || event.source !== pushFrame.value.contentWindow || event.origin !== window.location.origin) return
  const data = event.data as { type?: string } | null
  if (data === null || typeof data !== 'object' || data.type !== PUSH_JDP_SENT_MESSAGE_TYPE) return
  pushDlg.value = null
  showToast('发单成功')
  void reload(page.value)
}

const shotDlg = ref<{ page: string; title: string; orderNo: string } | null>(null)
const shotBusy = ref(false)
const shotUrl = computed(() =>
  shotDlg.value === null ? '' : islandUrl(shotDlg.value.page, shotDlg.value.orderNo),
)

function openScreenshot(pageName: string, title: string, d: OrderListItem): void {
  if (!isOrderNoShape(d.order_no)) {
    showToast('订单号格式不正确，无法打开订单截图', 'error')
    return
  }
  shotDlg.value = { page: pageName, title, orderNo: d.order_no }
}

/** 一键发交易平台（原 island 页，只换皮，布局不动）。 */
const pushDlg = ref<{ orderNo: string; viewNo: string } | null>(null)
const pushFrame = ref<HTMLIFrameElement | null>(null)
const pushUrl = computed(() =>
  pushDlg.value === null ? '' : islandUrl('order-pushjdp', pushDlg.value.orderNo),
)

function openPushJdp(d: OrderListItem): void {
  if (!isOrderNoShape(d.order_no)) {
    showToast('订单号格式不正确，无法打开发单页', 'error')
    return
  }
  pushDlg.value = { orderNo: d.order_no, viewNo: viewNoOf(d) }
}

/* ── 批量动作通用：outcome → toast 文案 ──────────────────────────────────────── */

function outcomeText(label: string, o: BatchOutcome): { text: string; bad: boolean } {
  const skipped = o.ineligible.length
  const base = `${label}：已处理 ${o.applied.length} 笔${skipped > 0 ? `，跳过 ${skipped} 笔` : ''}`
  if (skipped === 0) return { text: base, bad: false }
  const reasons = Array.from(new Set(o.ineligible.map((i) => i.msg).filter((m) => m !== '')))
  return { text: `${base}${reasons.length > 0 ? `：${reasons[0]}${reasons.length > 1 ? '等' : ''}` : ''}`, bad: true }
}

const busy = ref(false)

async function runBatch(label: string, run: () => Promise<BatchOutcome>): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const outcome = await run()
    await reload(page.value)
    const t = outcomeText(label, outcome)
    showToast(t.text, t.bad ? 'error' : 'info')
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    busy.value = false
  }
}

function requireChecked(): OrderListItem[] | null {
  if (loading.value || checkedRows.value.length === 0) {
    showToast(SELECT_RECORDS_FIRST_TEXT)
    return null
  }
  return checkedRows.value
}

/* ── 指派打手 ── */

const ASSIGN_KEY_MAXLENGTH = 128
const ASSIGN_SEARCH_DELAY = 400

type AssignDlg = {
  selection: BatchSelection
  fromToolbar: boolean
  currentTaker: string
  key: string
  searchState: 'idle' | 'loading' | 'error'
  candidates: AssigneeOption[]
  favorites: AssigneeOption[]
  favoritesLoading: boolean
  favoritesError: boolean
  selected: AssigneeOption | null
  chatFollowUp: boolean
  chatFollowUpAvailable: boolean
  price: string
  currentPrice: string
  perfTaker: PerformanceListItem[]
  perfSales: PerformanceListItem[]
  tab: 'local' | 'peer' | 'jidanpu'
  peer: PeerDirectAssignment | null
  peerLoading: boolean
  peerLoadError: string
  peerStudioId: number | ''
  peerAccountName: string
  peerMatches: PeerTakerMatch[]
  peerSelection: (PeerTakerMatch & { studio_id: number }) | null
  peerSearched: boolean
  peerSearching: boolean
  peerSearchSerial: number
  peerSubmitting: boolean
  peerError: string
  peerPriceDraft: string
  jidanpu: JidanpuDirectAssignment | null
  jidanpuLoading: boolean
  jidanpuLoadError: string
  jidanpuStudioId: number | ''
  jidanpuOptionSerial: number
  jidanpuSearchSerial: number
  jidanpuPriceDraft: string
  jidanpuQuery: string
  jidanpuResults: JidanpuTakerSearch | null
  jidanpuSelection: JidanpuTaker | null
  jidanpuFeeDraft: string
  jidanpuConfirming: boolean
  jidanpuCloseConfirm: boolean
  jidanpuSearching: boolean
  jidanpuSubmitting: boolean
  jidanpuOperationId: string
  jidanpuError: string
}

const assignDlg = ref<AssignDlg | null>(null)
const assignDlgOpen = ref(false)
const assignFavoritesSaving = ref(false)
const assignLocalTabButton = ref<HTMLButtonElement>()
const assignPeerTabButton = ref<HTMLButtonElement>()
const assignJidanpuTabButton = ref<HTMLButtonElement>()
const assignSearchKey = computed(() => assignDlg.value?.key.trim() ?? '')
const showAssignResults = computed(() => assignSearchKey.value !== '')
const showPeerAssignTab = computed(() => canDirectAssignTaker.value && assignDlg.value?.selection.orderNos.length === 1)
const showJidanpuAssignTab = computed(() => showPeerAssignTab.value && (
  assignDlg.value?.jidanpu?.bound || assignDlg.value?.jidanpuLoadError || assignDlg.value?.tab === 'jidanpu'
))
const assignSubmitting = computed(() => !!(assignDlg.value?.peerSubmitting || assignDlg.value?.jidanpuSubmitting))
const jidanpuAssignmentWorking = computed(() => !!(
  assignDlg.value?.jidanpuSubmitting || assignDlg.value?.jidanpuOperationId
))
const peerTarget = computed(() => assignDlg.value?.peer?.pending ?? assignDlg.value?.peerSelection ?? null)
const peerAssigneeName = computed(() => {
  const peer = assignDlg.value?.peer
  if (peer?.assignee_lookup_failed) return '暂时查不到'
  if (peer?.assignee_history_unverified) return '历史单号暂无法核对'
  return peer?.assignee?.account_name ?? '暂无接手人账号名称'
})
const peerStudioName = computed(() => {
  const peer = assignDlg.value?.peer
  return peer?.pending?.studio_name ?? peer?.studios.find((studio) => studio.id === peerTarget.value?.studio_id)?.name ?? ''
})
const peerPriceDraft = computed(() => canChangePayout.value ? assignDlg.value?.peerPriceDraft.trim() ?? '' : '')
const peerPrice = computed(() => {
  const dlg = assignDlg.value
  if (!dlg || !dlg.peer) return ''
  return dlg.peer.pending?.price ?? (peerPriceDraft.value || dlg.peer.price || '')
})

watch([assignDlgOpen, canAssignTaker, assignSearchKey], ([open, allowed, key], _previous, onCleanup) => {
  const dlg = assignDlg.value
  if (dlg === null || !open || !allowed) return
  dlg.candidates = []
  dlg.searchState = key === '' ? 'idle' : 'loading'
  if (key === '') return

  let active = true
  const timer = setTimeout(() => {
    void fetchAssigneeOptions({ key, limit: 200 })
      .then(({ items }) => {
        if (!active) return
        dlg.candidates = items
        dlg.searchState = 'idle'
      })
      .catch(() => {
        if (active) dlg.searchState = 'error'
      })
  }, ASSIGN_SEARCH_DELAY)
  // 旧请求在开始下一次防抖时已经失效。
  onCleanup(() => {
    active = false
    clearTimeout(timer)
  })
}, { flush: 'sync' })

async function loadAssignFavorites(): Promise<void> {
  const dlg = assignDlg.value
  if (dlg === null || dlg.favoritesLoading || !canAssignTaker.value) return
  dlg.favoritesLoading = true
  dlg.favoritesError = false
  try {
    const favorites = await fetchFavoriteAssignees()
    if (assignDlgOpen.value && assignDlg.value === dlg) dlg.favorites = favorites
  } catch {
    dlg.favoritesError = true
  } finally {
    dlg.favoritesLoading = false
  }
}

function isFavoriteAssignee(dlg: AssignDlg, id: number): boolean {
  return dlg.favorites.some((item) => item.id === id)
}

async function toggleFavoriteAssignee(a: AssigneeOption): Promise<void> {
  const dlg = assignDlg.value
  if (dlg === null || dlg.favoritesLoading || dlg.favoritesError || assignFavoritesSaving.value || !canAssignTaker.value) return
  assignFavoritesSaving.value = true
  try {
    const favorites = await setFavoriteAssignee(a.id, !isFavoriteAssignee(dlg, a.id))
    if (assignDlgOpen.value && assignDlg.value === dlg) dlg.favorites = favorites
  } catch (e) {
    if (assignDlgOpen.value && assignDlg.value === dlg) showToast(errText(e), 'error')
  } finally {
    assignFavoritesSaving.value = false
    // 如果请求期间关窗又打开，新弹窗要等保存完成后再刷新。
    if (assignDlgOpen.value && assignDlg.value !== dlg) void loadAssignFavorites()
  }
}

function openAssign(rows: OrderListItem[], fromToolbar: boolean): void {
  if (assignDlg.value !== null) return
  const selection = captureBatchSelection(rows, '指派打手', fromToolbar)
  const context = rows.length === 1 ? rows[0] : undefined
  const dlg: AssignDlg = {
    selection,
    fromToolbar,
    currentTaker: context?.taker_name ?? '',
    key: '',
    searchState: 'idle',
    candidates: [],
    favorites: [],
    favoritesLoading: false,
    favoritesError: false,
    selected: null,
    chatFollowUp: false,
    chatFollowUpAvailable: previewOrderGroups && rows.every((row) => row.chat_follow_up_available === true),
    price: '',
    currentPrice: context?.hall_price ?? '',
    perfTaker: [],
    perfSales: [],
    tab: 'local',
    peer: null,
    peerLoading: false,
    peerLoadError: '',
    peerStudioId: '',
    peerAccountName: '',
    peerMatches: [],
    peerSelection: null,
    peerSearched: false,
    peerSearching: false,
    peerSearchSerial: 0,
    peerSubmitting: false,
    peerError: '',
    peerPriceDraft: '',
    jidanpu: null,
    jidanpuLoading: false,
    jidanpuLoadError: '',
    jidanpuStudioId: '',
    jidanpuOptionSerial: 0,
    jidanpuSearchSerial: 0,
    jidanpuPriceDraft: '',
    jidanpuQuery: '',
    jidanpuResults: null,
    jidanpuSelection: null,
    jidanpuFeeDraft: '',
    jidanpuConfirming: false,
    jidanpuCloseConfirm: false,
    jidanpuSearching: false,
    jidanpuSubmitting: false,
    jidanpuOperationId: '',
    jidanpuError: '',
  }
  assignDlg.value = dlg
  const current = assignDlg.value
  assignDlgOpen.value = true
  if (!assignFavoritesSaving.value) void loadAssignFavorites()
  // 等指派弹窗的打开动画结束，再填入单笔绩效。
  if (selection.orderNos.length !== 1) return
  if (canAssignTaker.value || canClearTaker.value) void loadPeerAssignment(current)
  if (canDirectAssignTaker.value && (session.isTenantAdmin || !session.menuPermissions.has('order.restrict_view_taker'))) {
    void loadJidanpuAssignment(current)
  }
  const orderNo = selection.orderNos[0]!
  window.setTimeout(() => {
    if (!assignDlgOpen.value || assignDlg.value !== current) return
    Promise.all([
      fetchPerformances({ order_no: orderNo, emp_type: '打单工资' }),
      fetchPerformances({ order_no: orderNo, emp_type: '销售提成' }),
    ])
      .then(([taker, sales]) => {
        if (!assignDlgOpen.value || assignDlg.value !== current) return
        current.perfTaker = taker.items
        current.perfSales = sales.items
      })
      .catch(() => undefined)
  }, 450)
}

async function loadPeerAssignment(dlg: AssignDlg): Promise<void> {
  const orderNo = dlg.selection.orderNos[0]
  if (!orderNo || dlg.peerLoading) return
  dlg.peerLoading = true
  dlg.peerLoadError = ''
  try {
    dlg.peer = await fetchPeerDirectAssignment(orderNo)
    if (!dlg.peer.studios.some((studio) => studio.id === dlg.peerStudioId)) {
      dlg.peerStudioId = dlg.peer.studios[0]?.id ?? ''
    }
    if (dlg.peer.pending !== null && canDirectAssignTaker.value) dlg.tab = 'peer'
  } catch (e) {
    dlg.peerLoadError = errText(e)
  } finally {
    dlg.peerLoading = false
  }
}

function focusAssignTab(tab: AssignDlg['tab']): void {
  if (assignDlg.value === null || assignSubmitting.value) return
  assignDlg.value.tab = tab
  assignDlg.value.jidanpuConfirming = false
  const button = { local: assignLocalTabButton, peer: assignPeerTabButton, jidanpu: assignJidanpuTabButton }[tab]
  button.value?.focus()
}

function moveAssignTab(direction: -1 | 1): void {
  const tabs: AssignDlg['tab'][] = showJidanpuAssignTab.value ? ['local', 'peer', 'jidanpu'] : ['local', 'peer']
  const index = tabs.indexOf(assignDlg.value?.tab ?? 'local')
  focusAssignTab(tabs[(index + direction + tabs.length) % tabs.length]!)
}

async function loadJidanpuAssignment(dlg: AssignDlg, refresh = false): Promise<void> {
  const orderNo = dlg.selection.orderNos[0]
  if (!orderNo) return
  const studioId = dlg.jidanpuStudioId || undefined
  const serial = ++dlg.jidanpuOptionSerial
  dlg.jidanpuLoading = true
  dlg.jidanpuLoadError = ''
  try {
    const remembered = rememberedJidanpuAssignment(orderNo)
    if (remembered) {
      dlg.jidanpuOperationId = ''
      const operation = await fetchJidanpuAssignmentStatus(orderNo, remembered)
      if (serial !== dlg.jidanpuOptionSerial) return
      if (operation) showJidanpuOperation(dlg, operation)
      else {
        dlg.tab = 'jidanpu'
        dlg.jidanpuOperationId = remembered
      }
      return
    }
    const result = await (refresh
      ? fetchJidanpuDirectAssignment(orderNo, studioId, true)
      : fetchJidanpuDirectAssignment(orderNo, studioId))
    if (serial !== dlg.jidanpuOptionSerial) return
    if (result.operation_status) {
      showJidanpuOperation(dlg, result)
      return
    }
    dlg.jidanpu = result
    dlg.jidanpuFeeDraft = ''
    dlg.jidanpuConfirming = false
    const studio = result.studios?.find((item) => item.id === studioId) ?? result.studios?.[0]
    dlg.jidanpuStudioId = studio?.id ?? ''
    if ((result.pending || result.reservation) && result.studios?.length) dlg.tab = 'jidanpu'
    if (dlg.tab === 'jidanpu' && !result.bound) dlg.tab = 'local'
    if (studio && studio.id !== studioId && !result.pending && result.assignable) await loadJidanpuAssignment(dlg)
  } catch (e) {
    if (serial === dlg.jidanpuOptionSerial) dlg.jidanpuLoadError = errText(e)
  } finally {
    if (serial === dlg.jidanpuOptionSerial) dlg.jidanpuLoading = false
  }
}

function jidanpuOperationKey(orderNo: string): string {
  return `v2-jidanpu-assignment:${session.user?.id ?? ''}:${orderNo}`
}

function rememberedJidanpuAssignment(orderNo: string, includeUnaccepted = false): string {
  try {
    const stored = window.sessionStorage.getItem(jidanpuOperationKey(orderNo)) ?? ''
    if (stored.startsWith('unaccepted:')) return includeUnaccepted ? stored.slice('unaccepted:'.length) : ''
    return stored
  } catch {
    return ''
  }
}

function rememberJidanpuAssignment(orderNo: string, operationId: string, unaccepted = false): void {
  try {
    if (operationId) window.sessionStorage.setItem(jidanpuOperationKey(orderNo), `${unaccepted ? 'unaccepted:' : ''}${operationId}`)
    else window.sessionStorage.removeItem(jidanpuOperationKey(orderNo))
  } catch {
    // Pending operations can still be recovered from the order.
  }
}

function newJidanpuOperationId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function showJidanpuOperation(dlg: AssignDlg, result: JidanpuDirectAssignment): void {
  const orderNo = dlg.selection.orderNos[0]
  if (!orderNo) return
  dlg.jidanpu = result
  dlg.jidanpuLoadError = ''
  dlg.tab = 'jidanpu'
  dlg.jidanpuOperationId = result.operation_status === 'working' ? result.operation_id ?? '' : ''
  rememberJidanpuAssignment(orderNo, dlg.jidanpuOperationId)
  if (result.studios?.[0]) dlg.jidanpuStudioId = result.studios[0].id
  if (result.operation_status === 'working') {
    return
  }
  if (result.operation_status === 'failed') {
    dlg.jidanpuError = ''
  } else if (result.operation_status === 'done' && result.current) {
    dlg.jidanpuError = ''
    dlg.jidanpuStudioId = ''
    showToast(`已指派给鸡蛋铺打手 ${result.current.name}（${result.current.number}）`)
    void reload(page.value)
  }
}

watch(() => [assignDlgOpen.value, assignDlg.value, assignDlg.value?.jidanpuOperationId] as const,
  ([open, dlg, operationId], _, onCleanup) => {
    const orderNo = dlg?.selection.orderNos[0]
    if (!open || !dlg || !orderNo || !operationId) return
    let stopped = false
    let missing = 0
    let unreadable = 0
    let timer: number | undefined
    async function poll(): Promise<void> {
      const result = await fetchJidanpuAssignmentStatus(orderNo!, operationId!).catch(() => undefined)
      if (stopped) return
      if (result) {
        missing = 0
        unreadable = 0
        showJidanpuOperation(dlg!, result)
        if (result.operation_status !== 'working') return
      } else if (result === null) {
        unreadable = 0
        if (++missing >= 8) {
          rememberJidanpuAssignment(orderNo!, operationId!, true)
          dlg!.jidanpuOperationId = ''
          dlg!.jidanpuError = '暂未查到这次指派，可以重新提交。'
          void loadJidanpuAssignment(dlg!)
          return
        }
      } else {
        missing = 0
        if (++unreadable >= 8) {
          dlg!.jidanpuLoadError = '暂时无法查到这次指派的状态，请重新核对。'
          return
        }
      }
      timer = window.setTimeout(() => void poll(), 1200)
    }
    timer = window.setTimeout(() => void poll(), 1200)
    onCleanup(() => {
      stopped = true
      window.clearTimeout(timer)
    })
  },
)

function changeJidanpuStudio(dlg: AssignDlg): void {
  dlg.jidanpuQuery = ''
  dlg.jidanpuPriceDraft = ''
  dlg.jidanpuFeeDraft = ''
  resetJidanpuSearch(dlg)
  void loadJidanpuAssignment(dlg)
}

async function checkJidanpuAssignment(dlg: AssignDlg, closeUnconfirmed = false): Promise<void> {
  const orderNo = dlg.selection.orderNos[0]
  const pending = dlg.jidanpu?.pending ?? dlg.jidanpu?.reservation
  if (!orderNo || dlg.jidanpuLoading || !pending) return
  dlg.jidanpuLoading = true
  dlg.jidanpuError = ''
  try {
    const wasFirst = Boolean(dlg.jidanpu?.studios?.length)
    dlg.jidanpu = await verifyJidanpuAssignment(orderNo, pending, closeUnconfirmed)
    if (wasFirst && dlg.jidanpu.current) void reload(page.value)
    if (dlg.jidanpu.studios?.length && !dlg.jidanpu.pending) await loadJidanpuAssignment(dlg)
    dlg.jidanpuFeeDraft = ''
    dlg.jidanpuCloseConfirm = false
    resetJidanpuSearch(dlg)
    showToast(closeUnconfirmed ? '已处理这次指派的等待状态' : '已核对鸡蛋铺指派结果')
  } catch (e) {
    dlg.jidanpuError = errText(e)
  } finally {
    dlg.jidanpuLoading = false
  }
}

function resetJidanpuSearch(dlg: AssignDlg): void {
  dlg.jidanpuSearchSerial += 1
  dlg.jidanpuSearching = false
  dlg.jidanpuResults = null
  dlg.jidanpuSelection = null
  dlg.jidanpuConfirming = false
  dlg.jidanpuError = ''
}

async function searchJidanpuAssignment(dlg: AssignDlg): Promise<void> {
  const orderNo = dlg.selection.orderNos[0]
  const query = dlg.jidanpuQuery.trim()
  if (!orderNo || !query || !dlg.jidanpu?.assignable || dlg.jidanpuLoading || dlg.jidanpuSearching || busy.value) return
  const studioId = dlg.jidanpuStudioId
  resetJidanpuSearch(dlg)
  const serial = dlg.jidanpuSearchSerial
  dlg.jidanpuSearching = true
  try {
    const result = await (studioId === '' ? searchJidanpuTakers(orderNo, query) : searchJidanpuTakers(orderNo, query, studioId))
    if (serial === dlg.jidanpuSearchSerial) {
      dlg.jidanpuResults = result
      if (result.fee !== undefined && dlg.jidanpu && !dlg.jidanpu.reservation) dlg.jidanpu.fee = result.fee
    }
  } catch (e) {
    if (serial === dlg.jidanpuSearchSerial) dlg.jidanpuError = errText(e)
  } finally {
    if (serial === dlg.jidanpuSearchSerial) dlg.jidanpuSearching = false
  }
}

async function doJidanpuAssign(dlg: AssignDlg): Promise<void> {
  const orderNo = dlg.selection.orderNos[0]
  const target = dlg.jidanpuSelection
  if (!orderNo || !target || !dlg.jidanpu?.assignable || dlg.jidanpuLoading || dlg.jidanpuOperationId || dlg.peer?.pending || busy.value) return
  if (dlg.fromToolbar && !dlg.selection.confirmation.isSelectionCurrent()) {
    showToast('订单列表或勾选已变化，请重新勾选后再操作', 'error')
    return
  }
  const fee = canChangePayout.value ? dlg.jidanpuFeeDraft.trim() : ''
  if (fee !== '') {
    const validation = validateMoneyInput(fee)
    if (!validation.ok) {
      dlg.jidanpuError = validation.msg
      return
    }
  }
  const first = Boolean(dlg.jidanpu.studios?.length)
  const price = canChangePayout.value && first ? dlg.jidanpuPriceDraft.trim() : ''
  if (price !== '') {
    const validation = validateMoneyInput(price)
    if (!validation.ok) {
      dlg.jidanpuError = validation.msg
      return
    }
  }
  const studioToken = dlg.jidanpuResults?.studio_token
  if (first && (!studioToken || dlg.jidanpuStudioId === '' || (dlg.jidanpu.price == null && price === ''))) return
  if (!first && !dlg.jidanpuConfirming) {
    dlg.jidanpuConfirming = true
    return
  }
  const operationId = rememberedJidanpuAssignment(orderNo, true) || newJidanpuOperationId()
  dlg.jidanpuSubmitting = true
  dlg.jidanpuError = ''
  busy.value = true
  rememberJidanpuAssignment(orderNo, operationId)
  try {
    const assignment = {
      operation_id: operationId, number: target.number, name: target.name, expected_number: dlg.jidanpu.current?.number ?? '',
      expected_fee: dlg.jidanpu.fee, ...(fee !== '' ? { fee } : {}),
    }
    const result = await assignJidanpuTaker(orderNo, first && studioToken && dlg.jidanpuStudioId !== '' ? {
      ...assignment, studio_id: dlg.jidanpuStudioId, studio_token: studioToken,
      expected_price: dlg.jidanpu.price ?? null, ...(price !== '' ? { price } : {}),
    } : assignment)
    dlg.jidanpu = result
    dlg.jidanpuStudioId = ''
    dlg.jidanpuFeeDraft = ''
    dlg.jidanpuPriceDraft = ''
    resetJidanpuSearch(dlg)
    if (result.operation_status) showJidanpuOperation(dlg, result)
    else {
      rememberJidanpuAssignment(orderNo, '')
      if (first) void reload(page.value)
      if (result.current) showToast(`已指派给鸡蛋铺打手 ${result.current.name}（${result.current.number}）`)
    }
  } catch (e) {
    const conflict = e instanceof BizError && (e.data as { operation_conflict?: boolean } | null)?.operation_conflict
    if (conflict || e instanceof NetworkError || (e instanceof HttpError && e.status >= 500)) {
      dlg.jidanpuOperationId = operationId
      if (conflict) dlg.jidanpuError = errText(e)
    } else {
      rememberJidanpuAssignment(orderNo, '')
      dlg.jidanpuError = errText(e)
      await loadJidanpuAssignment(dlg, true)
      if (dlg.jidanpu?.pending) dlg.jidanpuSelection = null
    }
  } finally {
    dlg.jidanpuSubmitting = false
    busy.value = false
  }
}

watch(() => [assignDlg.value?.peerStudioId, assignDlg.value?.peerAccountName], ([studioId], previous) => {
  const dlg = assignDlg.value
  if (dlg === null) return
  if (studioId !== previous?.[0]) dlg.peerPriceDraft = ''
  dlg.peerSearchSerial += 1
  dlg.peerSearching = false
  dlg.peerMatches = []
  dlg.peerSelection = null
  dlg.peerSearched = false
  dlg.peerError = ''
}, { flush: 'sync' })

async function searchPeerAccount(): Promise<void> {
  const dlg = assignDlg.value
  const orderNo = dlg?.selection.orderNos[0]
  if (!dlg || !orderNo || dlg.peerStudioId === '' || dlg.peerSearching || busy.value) return
  const studioId = dlg.peerStudioId
  const query = dlg.peerAccountName.trim()
  if (query === '') return
  const serial = ++dlg.peerSearchSerial
  const isCurrent = () => assignDlgOpen.value && assignDlg.value === dlg && dlg.peerSearchSerial === serial
  dlg.peerSearching = true
  dlg.peerError = ''
  dlg.peerMatches = []
  dlg.peerSelection = null
  dlg.peerSearched = false
  try {
    const { items } = await searchPeerTakers(orderNo, { studio_id: studioId, query })
    if (isCurrent()) {
      dlg.peerMatches = items
      dlg.peerSearched = true
    }
  } catch (e) {
    if (isCurrent()) dlg.peerError = errText(e)
  } finally {
    if (isCurrent()) dlg.peerSearching = false
  }
}

function selectPeerMatch(match: PeerTakerMatch): void {
  const dlg = assignDlg.value
  if (!dlg || dlg.peerStudioId === '' || dlg.peerSubmitting) return
  dlg.peerSelection = { studio_id: dlg.peerStudioId, ...match }
  dlg.peerError = ''
}

async function doPeerAssign(): Promise<void> {
  const dlg = assignDlg.value
  const orderNo = dlg?.selection.orderNos[0]
  const target = peerTarget.value
  if (!dlg?.peer || !orderNo || !target || busy.value || !canDirectAssignTaker.value || dlg.peerLoading || peerPrice.value === '') return
  if (dlg.fromToolbar && !dlg.selection.confirmation.isSelectionCurrent()) {
    showToast('订单列表或勾选已变化，请重新勾选后再操作', 'error')
    return
  }
  const expectedPrice = dlg.peer.pending?.price ?? dlg.peer.price
  const newPrice = !dlg.peer.pending && peerPriceDraft.value !== '' ? peerPriceDraft.value : null
  if (newPrice !== null) {
    const validation = validateMoneyInput(newPrice)
    if (!validation.ok) {
      dlg.peerError = `发单价需要修正：${validation.msg}`
      return
    }
  }
  busy.value = true
  dlg.peerSubmitting = true
  dlg.peerError = ''
  try {
    const result = await assignPeerTaker(orderNo, {
      studio_id: target.studio_id,
      account_name: target.account_name,
      expected_price: expectedPrice,
      ...(dlg.peer.pending || dlg.peerSelection === null ? {} : { match_token: dlg.peerSelection.match_token }),
      ...(newPrice === null ? {} : { price: newPrice }),
    })
    if (assignDlg.value === dlg) assignDlgOpen.value = false
    await reload(page.value)
    showToast(`已指派给${result.studio_name} · ${result.account_name}`)
  } catch (e) {
    dlg.peerSelection = null
    dlg.peerMatches = []
    dlg.peerSearched = false
    dlg.peerError = errText(e)
    await loadPeerAssignment(dlg)
  } finally {
    dlg.peerSubmitting = false
    busy.value = false
  }
}

function onAssignClosed(): void {
  if (!assignDlgOpen.value) assignDlg.value = null
}

function onToolbarAssign(): void {
  const picked = requireChecked()
  if (picked !== null) openAssign(picked, true)
}

/** 接手人格点击（空态按钮 = 指派；有名字 = 点名字改派）——同一个弹窗。 */
function onTakerClick(d: OrderListItem): void {
  if (!canAssignTaker.value && !canClearTaker.value) return
  openAssign([d], false)
}

function pickAssignee(a: AssigneeOption): void {
  if (assignDlg.value !== null) assignDlg.value.selected = a
}

async function doAssign(): Promise<void> {
  const dlg = assignDlg.value
  if (busy.value || !canAssignTaker.value) return
  if (dlg === null || dlg.selected === null) {
    showToast('请先选中一位接手人', 'error')
    return
  }
  if (dlg.fromToolbar && !dlg.selection.confirmation.isSelectionCurrent()) {
    showToast('订单列表或勾选已变化，请重新勾选后再操作', 'error')
    return
  }
  const payload: AssignOrderPayload = { user_id: dlg.selected.id }
  if (dlg.chatFollowUpAvailable && dlg.chatFollowUp) payload.chat_follow_up = true
  if (canChangePayout.value && !isBlankLikePython(dlg.price)) {
    const validation = validateMoneyInput(dlg.price)
    if (!validation.ok) {
      showToast(`发单价：${validation.msg}`, 'error')
      return
    }
    payload.price = dlg.price
  }
  await runBatch('指派打手', async () => {
    const outcome = await batchAssignOrders({ ...payload, order_nos: dlg.selection.orderNos, dry_run: false })
    if (assignDlg.value === dlg && outcome.applied.length > 0) {
      assignDlgOpen.value = false
    }
    return outcome
  })
}

const clearDlg = ref<AssignDlg | null>(null)

function onClearTaker(): void {
  if (assignDlg.value !== null && canClearTaker.value) {
    clearDlg.value = assignDlg.value
  }
}

async function doClearTaker(isDelEmp: 'Y' | 'N'): Promise<void> {
  const dlg = clearDlg.value
  if (busy.value || dlg === null || !canClearTaker.value || (isDelEmp === 'Y' && !canChangePayout.value)) return
  if (dlg.fromToolbar && !dlg.selection.confirmation.isSelectionCurrent()) {
    showToast('订单列表或勾选已变化，请重新勾选后再操作', 'error')
    return
  }
  const { orderNos } = dlg.selection
  clearDlg.value = null
  assignDlgOpen.value = false
  await runBatch('清除接手人', async () => {
    const applied: string[] = []
    const issues: BatchOutcome['ineligible'] = []
    for (const no of orderNos) {
      try {
        await clearTaker(no, { isDelEmp })
        applied.push(no)
      } catch (e) {
        issues.push({ order_no: no, code: 0, msg: errText(e) })
      }
    }
    return { dry_run: false, eligible: orderNos, ineligible: issues, applied }
  })
}

/* ── 改状态（读取形态拿可选目标；活跃平台订单整批拒绝；三状态要求填原因进日志）────── */

const PLATFORM_STATUS_NOTICE = '已抛到代练通的单，状态由平台决定并自动同步，不能手工修改'
/** 选中这三档时必须填「说明原因」（D-0337 口径：原因经后端落订单日志摘要与 reason 列；≤200、trim）。 */
const REASON_TARGETS: readonly string[] = ['ISSUE', 'PAUSED', 'REFUNDED']
const REASON_REQUIRED_TEXT = '请填写变更原因（将记入订单日志）'
const NEED_TARGET_TEXT = '请选择目标状态'

const statusDlg = ref<BatchSelection & { target: string; targets: string[]; reason: string } | null>(null)

/** 可选目标**只能**来自后端读取形态（dry_run 不传 target）；⛔ 前端不镜像状态机。 */
async function loadStatusTargets(rowsById: OrderListItem[], fromToolbar: boolean): Promise<void> {
  if (rowsById.some((r) => hasActivePush(r))) {
    showToast(`${PLATFORM_STATUS_NOTICE}（单号：${rowsById.filter((r) => hasActivePush(r)).map((r) => viewNoOf(r)).join('、')}）`, 'error')
    return
  }
  try {
    const selection = captureBatchSelection(rowsById, '更改状态', fromToolbar)
    const outcome = await fetchBatchTransitionTargets(selection.orderNos)
    if (!selection.confirmation.isSelectionCurrent()) return
    const targets = outcome.targets ?? []
    if (targets.length === 0) {
      showToast('当前账号没有可执行的流转（职位权限可能不足）', 'error')
      return
    }
    statusDlg.value = { ...selection, target: '', targets, reason: '' }
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

function onToolbarChangeStatus(): void {
  const picked = requireChecked()
  if (picked !== null) void loadStatusTargets(picked, true)
}

function onCellStatus(d: OrderListItem): void {
  if (hasActivePush(d)) {
    showToast(PLATFORM_STATUS_NOTICE, 'error')
    return
  }
  void loadStatusTargets([d], false)
}

async function doChangeStatus(): Promise<void> {
  const dlg = statusDlg.value
  if (dlg === null || dlg.target === '') {
    showToast(NEED_TARGET_TEXT, 'error')
    return
  }
  const needsReason = REASON_TARGETS.includes(dlg.target)
  if (needsReason && dlg.reason.trim() === '') {
    showToast(REASON_REQUIRED_TEXT, 'error')
    return
  }
  const target = dlg.target
  // 非三状态不带 reason 键（载荷与旧形状一致，D-0337 口径）。
  const reason = needsReason ? { reason: dlg.reason.trim() } : {}
  statusDlg.value = null
  await runConfirmedBatch(dlg, 'transition', { target, ...reason })
}

/* ── 改类型 / 记退款 / 改接单价 / 改发单价 / 收款状态 / 重算 / 删除 ───────────── */

const typeDlg = ref<BatchSelection & { orderType: '' | 'BOOST' | 'COMPANION' } | null>(null)

function onToolbarChangeType(): void {
  const picked = requireChecked()
  if (picked !== null) typeDlg.value = { ...captureBatchSelection(picked, '更改类型', true), orderType: '' }
}

async function doChangeType(): Promise<void> {
  const dlg = typeDlg.value
  if (dlg === null || dlg.orderType === '') {
    showToast('请选择订单类型', 'error')
    return
  }
  const orderType = dlg.orderType
  typeDlg.value = null
  await runConfirmedBatch(dlg, 'types', { order_type: orderType })
}

const refundDlg = ref<BatchSelection & { refundTotal: string; reason: string } | null>(null)

function onToolbarRefund(): void {
  const picked = requireChecked()
  if (picked !== null) refundDlg.value = { ...captureBatchSelection(picked, '记退款', true), refundTotal: '', reason: '' }
}

function onCellRefund(d: OrderListItem): void {
  refundDlg.value = { ...captureBatchSelection([d], '记退款', false), refundTotal: d.refund_fee, reason: '' }
}

async function doRefund(): Promise<void> {
  const dlg = refundDlg.value
  if (dlg === null) return
  if (dlg.refundTotal.trim() === '') {
    showToast('请填写累计退款总额', 'error')
    return
  }
  if (dlg.reason.trim() === '') {
    showToast('请填写退款原因', 'error')
    return
  }
  const payload = { refund_total: dlg.refundTotal.trim(), reason: dlg.reason.trim() }
  const keyByOrderNo = new Map(dlg.orderNos.map((no) => [no, crypto.randomUUID()]))
  const payloadFor = (orderNos: string[]) => ({
    ...payload,
    request_ids: orderNos.map((no) => keyByOrderNo.get(no)),
  })
  refundDlg.value = null
  await runConfirmedBatch(dlg, 'refunds', payloadFor(dlg.orderNos), payloadFor)
}

type BatchSelection = Pick<BatchFlowOptions, 'orderNos'> & {
  confirmation: NonNullable<BatchFlowOptions['confirmation']>
}

type PriceDialog = BatchSelection & { value: string }

const batchPreview = ref<BatchPreview | null>(null)
let resolveBatchConfirmation: ((confirmed: boolean) => void) | null = null

function confirmBatchPreview(preview: BatchPreview): Promise<boolean> {
  return new Promise((resolve) => {
    resolveBatchConfirmation = resolve
    batchPreview.value = preview
  })
}

function closeBatchPreview(confirmed: boolean): void {
  const resolve = resolveBatchConfirmation
  resolveBatchConfirmation = null
  batchPreview.value = null
  resolve?.(confirmed)
}

function captureBatchSelection(rows: OrderListItem[], actionLabel: string, fromToolbar: boolean): BatchSelection {
  const orderNos = rows.map((row) => row.order_no)
  const sourceOrderNos = new Map(rows.map((row) => [row.order_no, row.source_order_no]))
  const sequence = reloadSequence
  const version = selectionVersion
  return {
    orderNos,
    confirmation: {
      actionLabel,
      sourceOrderNos,
      isSelectionCurrent: () => sequence === reloadSequence && (
        !fromToolbar || (version === selectionVersion && orderNos.every((orderNo) => isChecked(orderNo)))
      ),
    },
  }
}

async function runConfirmedBatch(
  selection: BatchSelection,
  action: BatchFlowOptions['action'],
  payload: BatchFlowOptions['payload'],
  applyPayload?: BatchFlowOptions['applyPayload'],
): Promise<boolean> {
  if (busy.value) return false
  busy.value = true
  const label = selection.confirmation.actionLabel
  try {
    return await runBatchFlow({
      orderNos: selection.orderNos,
      action,
      payload,
      applyPayload,
      confirmation: selection.confirmation,
      confirm: confirmBatchPreview,
      onDone: () => { void reload(page.value) },
      successMessage: (count) => `${label}：已处理 ${count} 笔`,
      msg: showToast,
    })
  } finally {
    busy.value = false
  }
}

const priceDlg = ref<PriceDialog | null>(null)
const hallPriceDlg = ref<PriceDialog | null>(null)

function onToolbarPrice(): void {
  const picked = requireChecked()
  if (picked !== null) priceDlg.value = { ...captureBatchSelection(picked, '更改接单价', true), value: '' }
}

function onCellPrice(d: OrderListItem): void {
  priceDlg.value = { ...captureBatchSelection([d], '更改接单价', false), value: d.total_price }
}

const PRICE_EMPTY_TEXT = '更改接单价，可为0，但不能为空'

async function doChangePrice(): Promise<void> {
  const dlg = priceDlg.value
  if (dlg === null) return
  const value = dlg.value.trim()
  if (value === '') {
    showToast(PRICE_EMPTY_TEXT, 'error')
    return
  }
  priceDlg.value = null
  const confirmation = { ...dlg.confirmation, actionLabel: `接单价改为 ¥${value}` }
  await runConfirmedBatch({ ...dlg, confirmation }, 'price', { total_price: value })
}

function onToolbarHallPrice(): void {
  const picked = requireChecked()
  if (picked !== null) hallPriceDlg.value = { ...captureBatchSelection(picked, '更改发单价', true), value: '' }
}

function onCellHallPrice(d: OrderListItem): void {
  hallPriceDlg.value = { ...captureBatchSelection([d], '更改发单价', false), value: d.hall_price ?? '' }
}

async function doChangeHallPrice(): Promise<void> {
  const dlg = hallPriceDlg.value
  if (dlg === null) return
  const value = dlg.value.trim()
  if (value === '') {
    showToast('请填写发单价（金额或百分比，如 50%）', 'error')
    return
  }
  hallPriceDlg.value = null
  const confirmation = { ...dlg.confirmation, actionLabel: `发单价改为 ${value}` }
  await runConfirmedBatch({ ...dlg, confirmation }, 'hall-price', { price: value })
}

const payDlg = ref<BatchSelection & { code: string } | null>(null)

/** 单笔改收款状态（点接单价格徽标/金额；弹窗三选一）。 */
function onCellPayStatus(d: OrderListItem): void {
  payDlg.value = { ...captureBatchSelection([d], '更改收款状态', false), code: '' }
}

function onToolbarPayStatus(): void {
  const picked = requireChecked()
  if (picked !== null) payDlg.value = { ...captureBatchSelection(picked, '更改收款状态', true), code: '' }
}

async function doChangePayStatus(): Promise<void> {
  const dlg = payDlg.value
  if (dlg === null || dlg.code === '') {
    showToast('请选择收款状态', 'error')
    return
  }
  const code = dlg.code
  payDlg.value = null
  await runConfirmedBatch(dlg, 'payment-status', { payment_status: code })
}

const recomputeDlg = ref<BatchSelection | null>(null)

function onToolbarRecompute(): void {
  const picked = requireChecked()
  if (picked !== null) recomputeDlg.value = captureBatchSelection(picked, '重算客服提成', true)
}

async function doRecompute(): Promise<void> {
  const dlg = recomputeDlg.value
  if (dlg === null) return
  recomputeDlg.value = null
  await runConfirmedBatch(dlg, 'recompute-commission', {})
}

const RECEIPT_SYNC_MAX_DAYS = 31
const receiptSyncDlg = ref<{ from: string; to: string } | null>(null)
const receiptSyncing = ref(false)
const receiptSyncError = ref('')

function onToolbarReceiptSync(): void {
  if (!canChangePaymentStatus.value || receiptSyncing.value) return
  const today = new Date()
  const from = new Date(today)
  from.setDate(from.getDate() - 6)
  receiptSyncError.value = ''
  receiptSyncDlg.value = { from: toDateInputValue(from), to: toDateInputValue(today) }
}

function closeReceiptSync(): void {
  if (!receiptSyncing.value) receiptSyncDlg.value = null
}

async function submitReceiptSync(): Promise<void> {
  const range = receiptSyncDlg.value
  if (!canChangePaymentStatus.value || range === null || receiptSyncing.value) return
  const { from, to } = range
  if (from === '' || to === '') {
    receiptSyncError.value = '请选择开始日期和结束日期'
    return
  }
  const days = (Date.parse(to) - Date.parse(from)) / 86_400_000 + 1
  if (!Number.isFinite(days) || from.length !== 10 || to.length !== 10) {
    receiptSyncError.value = '日期格式不正确'
    return
  }
  if (days < 1) {
    receiptSyncError.value = '开始日期不能晚于结束日期'
    return
  }
  if (days > RECEIPT_SYNC_MAX_DAYS) {
    receiptSyncError.value = `时间范围不能超过 ${RECEIPT_SYNC_MAX_DAYS} 天（当前 ${days} 天），请缩小时间范围`
    return
  }
  receiptSyncError.value = ''
  receiptSyncing.value = true
  try {
    await startXgjReceiptSync({ date_from: from, date_to: to })
    receiptSyncDlg.value = null
    showToast('已开始同步收款，请稍后刷新查看')
  } catch (e) {
    receiptSyncError.value = errText(e)
  } finally {
    receiptSyncing.value = false
  }
}

const penaltyOrder = ref<string | null>(null)

function onToolbarPenalty(): void {
  const picked = requireChecked()
  if (picked === null) return
  const [row] = picked
  if (!row || picked.length !== 1) {
    showToast('发起罚款时请选择一张订单')
    return
  }
  penaltyOrder.value = row.order_no
}

function onPenaltySubmitted(): void {
  penaltyOrder.value = null
  showToast('罚款审批已提交，可在审批中心查看')
}

const DELETE_REASON_THRESHOLD = 3
const DELETE_REASON_MAX_LENGTH = 200
const deleteDlg = ref<BatchSelection | null>(null)
const deleteReasonDlg = ref<BatchSelection & { reason: string } | null>(null)

function onToolbarDelete(): void {
  const picked = requireChecked()
  if (picked !== null) deleteDlg.value = captureBatchSelection(picked, '删除', true)
}

function confirmDelete(): void {
  const selection = deleteDlg.value
  if (selection === null) return
  deleteDlg.value = null
  if (selection.orderNos.length > DELETE_REASON_THRESHOLD) {
    deleteReasonDlg.value = { ...selection, reason: '' }
    return
  }
  void doDelete(selection)
}

function confirmDeleteWithReason(): void {
  const dlg = deleteReasonDlg.value
  if (dlg === null) return
  const reason = dlg.reason.trim()
  if (reason === '' || reason.length > DELETE_REASON_MAX_LENGTH) return
  deleteReasonDlg.value = null
  void doDelete(dlg, reason)
}

async function doDelete(selection: BatchSelection, reason?: string): Promise<void> {
  await runConfirmedBatch(selection, 'delete-v2', { isDelEmp: 'Y', ...(reason === undefined ? {} : { reason }) })
}

/* ── 发到内部接单大厅 / 撤回（行内）──────────────────────────────────────────── */

const throwDlg = ref<{ orderNo: string; price: string } | null>(null)

function openThrow(d: OrderListItem): void {
  throwDlg.value = { orderNo: d.order_no, price: d.hall_price ?? '' }
}

async function doThrow(): Promise<void> {
  const dlg = throwDlg.value
  if (dlg === null) return
  const { orderNo, price } = dlg
  throwDlg.value = null
  await runBatch('发到内部接单大厅', async () => {
    await throwToHall(orderNo, price.trim() === '' ? undefined : price.trim())
    return { dry_run: false, eligible: [orderNo], ineligible: [], applied: [orderNo] }
  })
}

const withdrawDlg = ref<{ orderNo: string } | null>(null)

function openWithdraw(d: OrderListItem): void {
  withdrawDlg.value = { orderNo: d.order_no }
}

async function doWithdraw(): Promise<void> {
  const dlg = withdrawDlg.value
  if (dlg === null) return
  const { orderNo } = dlg
  withdrawDlg.value = null
  await runBatch('从内部接单大厅撤回', async () => {
    await withdrawThrow(orderNo)
    return { dry_run: false, eligible: [orderNo], ineligible: [], applied: [orderNo] }
  })
}

/* ── 批量搜单号弹层（系统单号和来源单号）────────────────── */

const batchDlg = ref<{ text: string } | null>(null)
const BATCH_SEARCH_LIMIT = 200

function openBatchSearch(): void {
  batchDlg.value = { text: batchNos.value?.split(',').join('\n') ?? '' }
}

async function doBatchSearch(): Promise<void> {
  const dlg = batchDlg.value
  if (dlg === null) return
  const tokens = [...new Set(dlg.text.split(/[\r\n\t,，、;；]/).map((t) => t.trim()).filter(Boolean))]
  if (tokens.length === 0) {
    showToast('请输入要搜索的订单号', 'error')
    return
  }
  if (tokens.length > BATCH_SEARCH_LIMIT) {
    showToast(`批量单号一次最多 ${BATCH_SEARCH_LIMIT} 个`, 'error')
    return
  }
  dashboardOrderNo.value = null
  batchNos.value = tokens.join(',')
  batchDlg.value = null
  await reload(1)
}

/* ── 导出（服务端 xlsx；走当前筛选，不带分页）────────────────────────────────── */

const exporting = ref(false)
const exportJob = useFinanceExportJob('orders')
const { job: recentExport, jobProblem: exportProblem, busy: exportBusy } = exportJob

function saveOrderExport({ blob, filename }: { blob: Blob; filename: string }): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  showToast('已导出')
}

async function onExport(): Promise<void> {
  if (exporting.value || exportBusy.value) return
  exporting.value = true
  try {
    const filters = buildSearchParams()
    if (!await exportJob.start(toOrderParams(filters))) saveOrderExport(await downloadOrderExport(filters))
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    exporting.value = false
  }
}

async function downloadReadyExport(): Promise<void> {
  try {
    const file = await exportJob.download()
    if (file) saveOrderExport(file)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

function applyServiceDashboardQuery(): boolean {
  if (route.query.self_sales !== '1' || !session.user?.id) return false
  clearFilters()
  filters.kefuId = session.user.id
  filters.sDate = ''
  filters.eDate = ''
  const status = typeof route.query.status === 'string' ? route.query.status.split(',') : []
  filters.statuses = status.filter((value) => (ORDER_STATUS_VALUES as readonly string[]).includes(value))
  if (typeof route.query.keyword === 'string') filters.key1 = route.query.keyword
  if (typeof route.query.open_order === 'string' && isOrderNoShape(route.query.open_order)) {
    dashboardOrderNo.value = route.query.open_order
  }
  return true
}

watch(() => route.query, (_next, previous) => {
  if (previous.open_order && previous.open_order !== route.query.open_order) closeDetail()
  openedDashboardOrder = ''
  if (applyServiceDashboardQuery()) void reload(1)
  else if (previous.self_sales === '1' && route.name === 'v2-order-manage') resetFilters()
})

onMounted(() => {
  applyServiceDashboardQuery()
  void (async () => {
    try {
      const dictionaries = await fetchOrderDictionaries()
      games.value = dictionaries.games
      servers.value = dictionaries.servers
      sources.value = dictionaries.order_sources
    } catch { /* 字典取不到：列降级显示主键，列表照常 */ }
    const [groupsResult, sourcesResult] = await Promise.allSettled([
      fetchOrderSourceGroups(), fetchOrderSources(),
    ])
    sourceGroups.value = groupsResult.status === 'fulfilled' ? groupsResult.value.items : []
    if (sourcesResult.status === 'fulfilled') {
      sourceCatalog.value = sourcesResult.value.items
      sourceCatalogLoaded.value = true
    } else {
      sourceCatalog.value = []
      sourceCatalogLoaded.value = false
    }
    try {
      kefuItems.value = (await fetchKefuOptions()).items
    } catch { kefuItems.value = [] }
    // 接手人/发单人候选不预载（关键词远程搜索，见 runPickSearch）；挂载零 filter-options 请求。
    void fetchPushAvailability().then((ok) => { pushAvailable.value = ok })
    await reload(1)
  })()
  window.addEventListener('message', onPushMessage)
  window.addEventListener('click', onWindowClickClosePick)
})

onUnmounted(() => {
  reloadSequence++
  closeBatchPreview(false)
  detailSequence++
  window.removeEventListener('message', onPushMessage)
  window.removeEventListener('click', onWindowClickClosePick)
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-manage">
    <!-- 搜索区：字段照旧页三行，全部服务端筛选（控件 EP 化、标签左置） -->
    <!-- 筛选区 = 连体网格组（业务方：仿录入订单 v2 的「标签格 + 值格」连体，原版 layui form-pane 形态）；
         每组等高、组内零间距、组间留缝自动换行；数据字段与过滤语义不变 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="flt-row">
        <div class="flt-group" data-v2-filter-group="game">
          <span class="flt-label">游戏</span>
          <span class="flt-cell"><el-select v-model="filters.gameId" filterable placeholder="全部游戏" class="flt-w150" :class="{ 'flt-ph': filters.gameId === null }" data-v2-filter="game" @change="onGameChange" :teleported="false">
            <el-option :value="NULL_OPTION_VALUE" label="全部游戏" />
            <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="time">
          <span class="flt-cell"><el-select v-model="filters.dateType" class="flt-w106 flt-prefix" data-v2-filter="date-type" :teleported="false">
            <el-option v-for="t in DATE_TYPES" :key="t.value" :value="t.value" :label="t.label" />
          </el-select></span>
          <span class="flt-cell flt-datecell" data-v2-filter="sdate"><el-date-picker
            :model-value="filters.sDate"
            type="datetime"
            :format="DATE_TIME_FORMAT"
            :value-format="DATE_TIME_FORMAT"
            :default-time="START_PICKER_TIME"
            placeholder="开始时间"
            aria-label="开始时间"
            :teleported="false"
            @update:model-value="setSDate"
          /></span>
          <span class="flt-dash">–</span>
          <span class="flt-cell flt-datecell" data-v2-filter="edate" @input.capture="rememberTypedEndDate"><el-date-picker
            :model-value="filters.eDate"
            type="datetime"
            :format="DATE_TIME_FORMAT"
            :value-format="DATE_TIME_FORMAT"
            :default-time="END_PICKER_TIME"
            placeholder="结束时间"
            aria-label="结束时间"
            :teleported="false"
            @update:model-value="setEDate"
          /></span>
        </div>
        <div class="flt-group" data-v2-filter-group="price">
          <span class="flt-cell"><el-select v-model="filters.priceTier" class="flt-w106 flt-prefix" data-v2-filter="price-type" :teleported="false">
            <el-option v-for="t in PRICE_TIERS" :key="t.value" :value="t.value" :label="t.label" />
          </el-select></span>
          <span class="flt-cell"><el-input v-model="filters.priceMin" placeholder="最小金额" class="flt-w86" data-v2-filter="price-min" /></span>
          <span class="flt-dash">–</span>
          <span class="flt-cell"><el-input v-model="filters.priceMax" placeholder="最大金额" class="flt-w86" data-v2-filter="price-max" /></span>
          <template v-if="filters.priceTier === '3'">
            <span class="flt-cell"><el-input v-model="filters.hallPriceMin" placeholder="发单价最小" class="flt-w92" data-v2-filter="hall-price-min" /></span>
            <span class="flt-dash">–</span>
            <span class="flt-cell"><el-input v-model="filters.hallPriceMax" placeholder="发单价最大" class="flt-w92" data-v2-filter="hall-price-max" /></span>
          </template>
        </div>
        <div class="flt-group" data-v2-filter-group="source">
          <span class="flt-label">订单来源</span>
          <span class="flt-cell"><el-select v-model="filters.sourceId" filterable placeholder="全部来源" class="flt-w150" :class="{ 'flt-ph': filters.sourceId === null }" data-v2-filter="source" :teleported="false">
            <el-option :value="NULL_OPTION_VALUE" label="全部来源" />
            <el-option v-for="s in availableSources" :key="s.id" :value="s.id" :label="s.name" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="source-group">
          <span class="flt-label">订单分组</span>
          <span class="flt-cell"><el-select v-model="filters.sourceGroupId" filterable placeholder="全部分组" class="flt-w150" :class="{ 'flt-ph': filters.sourceGroupId === null }" data-v2-filter="source-group" :teleported="false">
            <el-option :value="NULL_OPTION_VALUE" label="全部分组" />
            <el-option v-for="g in sourceGroups" :key="g.id" :value="g.id" :label="g.name" />
          </el-select></span>
        </div>
      </div>
      <div class="flt-row">
        <div class="flt-group" data-v2-filter-group="status">
          <span class="flt-label">订单状态</span>
          <span class="flt-cell"><el-select v-model="filters.statuses" multiple collapse-tags collapse-tags-limit="2" placeholder="全部状态" class="flt-w160" :class="{ 'flt-ph': filters.statuses.length === 0 }" data-v2-filter="status" :teleported="false">
            <el-option v-for="s in ORDER_STATUS_VALUES" :key="s" :value="s" :label="V2_ORDER_STATUS_LABELS[s]" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="taker">
          <span class="flt-label">代练打手</span>
          <span class="flt-cell flt-pickcell">
            <button type="button" class="flt-pick" data-v2-filter="taker" @click.stop="togglePick('taker')">
              <span :class="{ 'flt-pick-ph': filters.recType !== '0' || filters.takerId === null }">{{ takerLabel }}</span>
              <svg viewBox="0 0 12 8" class="flt-caret" aria-hidden="true"><path d="M1 1.5 6 6.5 11 1.5" /></svg>
            </button>
            <div v-if="pickOpen === 'taker'" class="flt-pop" data-v2-pop="taker">
              <el-input v-model="pickQuery" placeholder="输入名字搜索" data-v2-pop-search="taker" />
              <div class="flt-pop-list">
                <button v-if="pickQuery.trim() === ''" type="button" class="flt-pop-item" :class="{ 'flt-pop-item--on': filters.takerId === null }" @click="pickPerson(null)">全部</button>
                <button
                  v-for="o in pickedFiltered"
                  :key="o.id"
                  type="button"
                  class="flt-pop-item"
                  :class="{ 'flt-pop-item--on': filters.recType === '0' && filters.takerId === o.id }"
                  @click="pickPerson(o.id)"
                >{{ o.name }}</button>
                <!-- 候选 = 关键词远程搜索：空词 = 「输入关键词搜索」（不发请求）；有词无命中才是「没有匹配的人」。 -->
                <p v-if="pickedFiltered.length === 0" class="flt-pop-empty">{{ pickQuery.trim() === '' ? '输入关键词搜索' : '没有匹配的人' }}</p>
              </div>
              <button type="button" class="flt-pop-clear" @click="pickPerson(null)">清空</button>
            </div>
          </span>
        </div>
        <div class="flt-group" data-v2-filter-group="publisher">
          <span class="flt-label">发单人</span>
          <span class="flt-cell flt-pickcell">
            <button type="button" class="flt-pick" data-v2-filter="publisher" @click.stop="togglePick('publisher')">
              <span :class="{ 'flt-pick-ph': filters.recType !== '10' || filters.takerId === null }">{{ publisherLabel }}</span>
              <svg viewBox="0 0 12 8" class="flt-caret" aria-hidden="true"><path d="M1 1.5 6 6.5 11 1.5" /></svg>
            </button>
            <div v-if="pickOpen === 'publisher'" class="flt-pop" data-v2-pop="publisher">
              <el-input v-model="pickQuery" placeholder="输入名字搜索" data-v2-pop-search="publisher" />
              <div class="flt-pop-list">
                <button v-if="pickQuery.trim() === ''" type="button" class="flt-pop-item" :class="{ 'flt-pop-item--on': filters.takerId === null }" @click="pickPerson(null)">全部</button>
                <button
                  v-for="o in pickedFiltered"
                  :key="o.id"
                  type="button"
                  class="flt-pop-item"
                  :class="{ 'flt-pop-item--on': filters.recType === '10' && filters.takerId === o.id }"
                  @click="pickPerson(o.id)"
                >{{ o.name }}</button>
                <!-- 候选 = 关键词远程搜索：空词 = 「输入关键词搜索」（不发请求）；有词无命中才是「没有匹配的人」。 -->
                <p v-if="pickedFiltered.length === 0" class="flt-pop-empty">{{ pickQuery.trim() === '' ? '输入关键词搜索' : '没有匹配的人' }}</p>
              </div>
              <button type="button" class="flt-pop-clear" @click="pickPerson(null)">清空</button>
            </div>
          </span>
        </div>
        <div class="flt-group" data-v2-filter-group="order-type">
          <span class="flt-label">订单类型</span>
          <span class="flt-cell"><el-select v-model="filters.orderType" class="flt-w140" :class="{ 'flt-ph': filters.orderType === '' }" data-v2-filter="order-type" :teleported="false">
            <el-option value="" label="全部" />
            <el-option value="BOOST" label="代练订单" />
            <el-option value="COMPANION" label="陪玩订单" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="kefu">
          <span class="flt-cell"><el-select v-model="filters.kefuType" class="flt-w106 flt-prefix" data-v2-filter="kefu-type" :teleported="false">
            <el-option value="0" label="销售客服" />
            <el-option value="5" label="录单人" />
            <el-option value="10" label="记录退款者" />
          </el-select></span>
          <span class="flt-cell flt-pickcell">
            <button type="button" class="flt-pick" data-v2-filter="kefu" @click.stop="togglePick('kefu')">
              <span :class="{ 'flt-pick-ph': filters.kefuId === null }">{{ kefuLabel }}</span>
              <svg viewBox="0 0 12 8" class="flt-caret" aria-hidden="true"><path d="M1 1.5 6 6.5 11 1.5" /></svg>
            </button>
            <div v-if="pickOpen === 'kefu'" class="flt-pop" data-v2-pop="kefu">
              <el-input v-model="pickQuery" placeholder="输入名字过滤" data-v2-pop-search="kefu" />
              <div class="flt-pop-list flt-pop-list--kefu" role="group" aria-label="客服候选" tabindex="0">
                <button v-if="pickQuery.trim() === ''" type="button" class="flt-pop-item" :class="{ 'flt-pop-item--on': filters.kefuId === null }" @click="pickPerson(null)">全部</button>
                <button
                  v-for="o in pickedFiltered"
                  :key="o.id"
                  type="button"
                  class="flt-pop-item"
                  :class="{ 'flt-pop-item--on': filters.kefuId === o.id }"
                  @click="pickPerson(o.id)"
                >{{ o.name }}</button>
                <p v-if="pickedFiltered.length === 0" class="flt-pop-empty">没有匹配的人</p>
              </div>
            </div>
          </span>
        </div>
        <div class="flt-group" data-v2-filter-group="pay">
          <span class="flt-label">收款状态</span>
          <span class="flt-cell"><el-select v-model="filters.payStatus" placeholder="全部" class="flt-w140" :class="{ 'flt-ph': filters.payStatus === '' }" data-v2-filter="pay-status" :teleported="false">
            <el-option value="" label="全部" />
            <el-option v-for="p in PAY_STATUS_OPTIONS" :key="p.code" :value="p.code" :label="p.label" />
          </el-select></span>
        </div>
        <div class="flt-group" data-v2-filter-group="customer-kind">
          <span class="flt-label">新老客</span>
          <span class="flt-cell"><el-select v-model="filters.customerKind" placeholder="全部" class="flt-w140" :class="{ 'flt-ph': filters.customerKind === '' }" data-v2-filter="customer-kind" :teleported="false">
            <el-option value="" label="全部" />
            <el-option value="new" label="新客" />
            <el-option value="old" label="老客" />
          </el-select></span>
        </div>
      </div>
      <!-- 第三排：搜索。窄窗口时这一排自己换行。 -->
      <div class="flt-row flt-searchrow">
        <div class="flt-group flt-grow" data-v2-filter-group="key1">
          <span class="flt-cell"><el-select v-model="filters.search1Mode" class="flt-w84 flt-prefix" data-v2-filter="search1-mode" :teleported="false">
            <el-option value="0" label="搜索1" />
            <el-option value="10" label="批量搜单号" />
          </el-select></span>
          <span class="flt-cell flt-cell--grow">
            <button v-if="filters.search1Mode === '10'" type="button" class="cv2-btn cv2-batch-search-trigger" data-v2="open-batch-search" @click="openBatchSearch">
              {{ batchNos === null ? '输入要查的单号' : `已输入 ${batchNos.split(',').length} 个单号` }}
            </button>
            <el-input
              v-else
              v-model="filters.key1"
              placeholder="系统订单号/来源单号/角色名/电话"
              data-v2-filter="key1"
              @keydown.enter="onSearch"
            />
          </span>
        </div>
        <div class="flt-group flt-grow" data-v2-filter-group="key2">
          <span class="flt-label">搜索2</span>
          <span class="flt-cell flt-cell--grow"><el-input
            v-model="filters.key2"
            placeholder="项目/标题/微信/旺旺"
            data-v2-filter="key2"
            @keydown.enter="onSearch"
          /></span>
        </div>
        <div class="flt-checks">
          <el-checkbox v-model="filters.urgent" data-v2-filter="urgent" @change="void reload(1)">加急单</el-checkbox>
          <el-checkbox v-model="filters.leak" data-v2-filter="leak" @change="void reload(1)">未安排</el-checkbox>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" @click="onSearch">查询</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--lg cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip" data-v2="toolbar-strip">
      <button type="button" class="cv2-btn" data-v2="refresh" :disabled="loading" @click="reload(page)">刷新</button>
      <button v-if="canAssignTaker || canClearTaker" type="button" class="cv2-btn" data-v2="assign" @click="onToolbarAssign">{{ canAssignTaker ? '指派打手' : '清除接手人' }}</button>
      <button v-if="canChangeStatus" type="button" class="cv2-btn" data-v2="change-status" @click="onToolbarChangeStatus">改状态</button>
      <button v-if="canUpdateOrder" type="button" class="cv2-btn" data-v2="change-type" @click="onToolbarChangeType">改类型</button>
      <button v-if="canRefund" type="button" class="cv2-btn cv2-btn--solid-red" data-v2="refund" @click="onToolbarRefund">记退款</button>
      <button v-if="canExportOrders" type="button" class="cv2-btn" data-v2="export" :disabled="exporting || exportBusy" @click="onExport">导出</button>
      <FinanceExportJobStatus v-if="canExportOrders && recentExport" :job="recentExport" :problem="exportProblem" @download="downloadReadyExport" @retry="exportJob.retryProgress" />
      <button v-if="canChangePrice" type="button" class="cv2-btn" data-v2="change-price" @click="onToolbarPrice">改接单价</button>
      <button v-if="canChangeHallPrice" type="button" class="cv2-btn" data-v2="change-hall-price" @click="onToolbarHallPrice">改发单价</button>
      <button v-if="canChangePaymentStatus" type="button" class="cv2-btn" data-v2="pay-status" @click="onToolbarPayStatus">收款状态</button>
      <button v-if="canRecomputeCommission" type="button" class="cv2-btn" data-v2="recompute" @click="onToolbarRecompute">重算客服提成</button>
      <button v-if="canChangePaymentStatus" type="button" class="cv2-btn" data-v2="receipt-sync" :disabled="receiptSyncing" @click="onToolbarReceiptSync">同步收款</button>
      <button v-if="canApplyPenalty" type="button" class="cv2-btn cv2-btn--solid-penalty" data-v2="penalty" @click="onToolbarPenalty">罚款</button>
      <button v-if="canDeleteOrders" type="button" class="cv2-btn cv2-btn--solid-yellow" data-v2="delete" @click="onToolbarDelete">删除</button>
      </div>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedRows.length }} 行</span>
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

    <!-- 统计句：后端对当前筛选全集的聚合（data.summary），真值红字；无统计返回时整句不出现。
         「状态词+数量」段可点（点 = 只筛该状态 / 再点 = 清除回全部），句子其余文字逐字不变。 -->
    <p v-if="summaryLead !== ''" class="cv2-total-line" data-v2="total"
      >{{ summaryLead }}<template v-for="(seg, i) in summarySegments" :key="seg.code"><span v-if="i > 0">，</span><button
        type="button"
        class="cv2-sum-st"
        :class="{ 'cv2-sum-st--on': isSummaryStatusActive(seg.code) }"
        :data-v2-summary-status="seg.code"
        @click="onSummaryStatusClick(seg.code)"
      >{{ seg.text }}</button></template></p>

    <!-- 订单列表：服务端分页；列 = 旧页可见列全集；<colgroup> 承接列宽拖拽 -->
    <p v-if="penaltyLoadFailed" class="cv2-total-line" role="status">部分子单加载失败，请刷新重试。</p>
    <section class="cv2-panel cv2-table-panel" aria-label="订单列表">
      <div class="cv2-table-scroll">
      <table class="cv2-table" :style="{ minWidth: `${visibleTableWidth}px` }">
        <!-- colgroup 必须与渲染的单元格**同序同数**（table-layout:fixed 按位置映射；
             隐藏列不铺 col，否则后面所有列错位一格、操作列挤成别的宽）。 -->
        <colgroup>
          <col v-for="key in visibleColumns" :key="key" :data-v2-col="key" :style="manageColStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input type="checkbox" data-v2="check-page-all" :disabled="loading" :checked="pageAllChecked" aria-label="勾选本页普通单和拆单组首行" title="勾选本页普通单和拆单组首行，取消时清除本页勾选" @change="togglePageAll" />
              <span class="cv2-th-resizer" data-v2-col="check" @mousedown="startResize($event, 'check')" @dblclick="resetColumn('check')" />
            </th>
            <th v-if="colVisible('order')">
              <span class="cv2-order-header">订单号</span>
              <span class="cv2-th-resizer" data-v2-col="order" @mousedown="startResize($event, 'order')" @dblclick="resetColumn('order')" />
            </th>
            <th v-if="colVisible('sysno')" class="cv2-th-center">
              系统内置单号
              <span class="cv2-th-resizer" data-v2-col="sysno" @mousedown="startResize($event, 'sysno')" @dblclick="resetColumn('sysno')" />
            </th>
            <th v-if="colVisible('role')">
              角色名
              <span class="cv2-th-resizer" data-v2-col="role" @mousedown="startResize($event, 'role')" @dblclick="resetColumn('role')" />
            </th>
            <th v-if="colVisible('project')">
              代练/陪玩内容
              <span class="cv2-th-resizer" data-v2-col="project" @mousedown="startResize($event, 'project')" @dblclick="resetColumn('project')" />
            </th>
            <th v-if="colVisible('info')">
              游戏区服
              <span class="cv2-th-resizer" data-v2-col="info" @mousedown="startResize($event, 'info')" @dblclick="resetColumn('info')" />
            </th>
            <th v-if="colVisible('status')" class="cv2-th-center">状态</th>
            <th v-if="colVisible('hour')" class="cv2-th-center">
              时间
              <span class="cv2-th-resizer" data-v2-col="hour" @mousedown="startResize($event, 'hour')" @dblclick="resetColumn('hour')" />
            </th>
            <th v-if="colVisible('ww')" class="cv2-th-center">
              旺旺/来源
              <span class="cv2-th-resizer" data-v2-col="ww" @mousedown="startResize($event, 'ww')" @dblclick="resetColumn('ww')" />
            </th>
            <th v-if="colVisible('source')" class="cv2-th-center">
              订单来源
              <span class="cv2-th-resizer" data-v2-col="source" @mousedown="startResize($event, 'source')" @dblclick="resetColumn('source')" />
            </th>
            <th v-if="colVisible('taker')" class="cv2-th-center">
              接手人
              <span class="cv2-th-resizer" data-v2-col="taker" @mousedown="startResize($event, 'taker')" @dblclick="resetColumn('taker')" />
            </th>
            <th v-if="colVisible('hallprice')" class="cv2-th-num">
              发单价
              <span class="cv2-th-resizer" data-v2-col="hallprice" @mousedown="startResize($event, 'hallprice')" @dblclick="resetColumn('hallprice')" />
            </th>
            <th v-if="colVisible('price')" class="cv2-th-num">
              接单价
              <span class="cv2-th-resizer" data-v2-col="price" @mousedown="startResize($event, 'price')" @dblclick="resetColumn('price')" />
            </th>
            <th v-if="colVisible('net')" class="cv2-th-num">
              实收款
              <span class="cv2-th-resizer" data-v2-col="net" @mousedown="startResize($event, 'net')" @dblclick="resetColumn('net')" />
            </th>
            <th v-if="colVisible('refund')" class="cv2-th-num">
              退款
              <span class="cv2-th-resizer" data-v2-col="refund" @mousedown="startResize($event, 'refund')" @dblclick="resetColumn('refund')" />
            </th>
            <th v-if="colVisible('remark')" class="cv2-th-center">
              重要备注
              <span class="cv2-th-resizer" data-v2-col="remark" @mousedown="startResize($event, 'remark')" @dblclick="resetColumn('remark')" />
            </th>
            <th v-if="showScreenshotColumn" class="cv2-th-center">订单截图</th>
            <th v-if="colVisible('kefu')" class="cv2-th-center">
              销售客服
              <span class="cv2-th-resizer" data-v2-col="kefu" @mousedown="startResize($event, 'kefu')" @dblclick="resetColumn('kefu')" />
            </th>
            <th v-if="colVisible('pub')" class="cv2-th-center">
              发单人
              <span class="cv2-th-resizer" data-v2-col="pub" @mousedown="startResize($event, 'pub')" @dblclick="resetColumn('pub')" />
            </th>
            <th v-if="colVisible('by')" class="cv2-th-center">
              录单人
              <span class="cv2-th-resizer" data-v2-col="by" @mousedown="startResize($event, 'by')" @dblclick="resetColumn('by')" />
            </th>
            <th v-if="colVisible('create')">
              录单时间
              <span class="cv2-th-resizer" data-v2-col="create" @mousedown="startResize($event, 'create')" @dblclick="resetColumn('create')" />
            </th>
            <th v-if="colVisible('complete')">
              完单时间
              <span class="cv2-th-resizer" data-v2-col="complete" @mousedown="startResize($event, 'complete')" @dblclick="resetColumn('complete')" />
            </th>
            <th class="cv2-th-fixed">
              操作
              <span class="cv2-th-resizer" data-v2-col="actions" @mousedown="startResize($event, 'actions')" @dblclick="resetColumn('actions')" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in displayRows"
            :key="row.__penaltyPreviewFor ? `${row.__penaltyPreviewFor}:${row.order_no}` : row.order_no"
            class="cv2-row"
            :class="{ 'cv2-row--checked': !row.__penaltyPreviewFor && isChecked(row.order_no), 'cv2-row--platform': hasActivePush(row), 'cv2-row--split-child': row.__splitLine === 'branch' || row.__splitLine === 'end' }"
            :data-v2-row="row.__penaltyPreviewFor ? undefined : row.order_no"
            :data-v2-penalty-preview="row.__penaltyPreviewFor ? row.order_no : undefined"
          >
            <td class="cv2-th-check">
              <input
                v-if="!row.__penaltyPreviewFor"
                type="checkbox"
                :data-v2-row-check="row.order_no"
                :disabled="loading"
                :checked="isChecked(row.order_no)"
                :aria-label="`勾选 ${viewNoOf(row)}`"
                @change="toggleRow(row.order_no)"
              />
            </td>
            <td v-if="colVisible('order')" class="cv2-td-order" :data-split-line="row.__splitLine">
              <span class="cv2-order-line">
                <span class="cv2-split-slot" aria-hidden="true"></span>
                <span v-if="row.__splitLine === 'branch' || row.__splitLine === 'end'" class="cv2-split-child-connector" aria-hidden="true"></span>
                <a href="#" class="cv2-order-link cv2-ono" :class="{ 'cv2-order-link--platform': hasActivePush(row) }" :title="hasActivePush(row) ? '已抛到代练通' : viewNoOf(row)" :data-v2-detail="row.__penaltyPreviewFor ? undefined : row.order_no" @click.prevent="openDetail(row)">{{ viewNoOf(row) }}</a>
                <span v-if="!row.__penaltyPreviewFor && row.is_urgent === true" class="cv2-urgent" title="加急订单，优先安排">急</span>
                <button
                  v-if="!row.__penaltyPreviewFor"
                  type="button"
                  class="cv2-copybtn"
                  :data-v2-copy="row.order_no"
                  :aria-label="`复制单号 ${viewNoOf(row)}`"
                  title="复制单号"
                  @click.stop="copyOrderNo(viewNoOf(row))"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <rect x="5.5" y="5.5" width="8" height="9" rx="1.5" />
                    <path d="M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v7" />
                  </svg>
                </button>
              </span>
              <span v-if="!row.__underPenaltyParent && row.penalty_parent_order_no" class="cv2-order-origin" :title="`原单 ${row.penalty_parent_order_no}`">原单 {{ row.penalty_parent_order_no }}</span>
            </td>
            <td v-if="row.__penaltyPreviewFor" :colspan="visibleColumns.length - (colVisible('order') ? 2 : 1)">
              <a v-if="!colVisible('order')" href="#" class="cv2-order-link cv2-ono" @click.prevent="openDetail(row)">{{ viewNoOf(row) }}</a>
            </td>
            <template v-else>
            <td v-if="colVisible('sysno')" class="cv2-td-center cv2-push-text"><span class="cv2-num cv2-sysno">{{ row.order_no }}</span></td>
            <td v-if="colVisible('role')" class="cv2-push-text">{{ row.game_role_name === '' ? '无' : row.game_role_name }}</td>
            <td v-if="colVisible('project')" class="cv2-td-project cv2-td-order-text cv2-push-text" :data-v2-project="row.order_no" @mouseenter="showProjectPreview($event, row.project_text)" @mousemove="showProjectPreview($event, row.project_text)" @mouseleave="closeProjectPreview">
              <button type="button" class="cv2-order-text" aria-haspopup="dialog" :aria-label="row.project_text ? undefined : '代练/陪玩内容：无'" :data-v2-project-open="row.order_no" @click="openOrderText(row, 'project_text')">{{ row.project_text || '无' }}</button>
            </td>
            <td v-if="colVisible('info')" class="cv2-td-info cv2-push-text">[{{ gameNameOf(row.game) }}]{{ serverNameOf(row.server) }}</td>
            <td v-if="colVisible('status')" class="cv2-td-center">
              <component
                :is="canChangeStatus ? 'button' : 'span'"
                :type="canChangeStatus ? 'button' : undefined"
                class="cv2-st cv2-order-status"
                :class="[orderStatusClass(row.status), { 'cv2-st--mark': canChangeStatus }]"
                :data-v2-status="row.order_no"
                :data-status="row.status"
                :title="!canChangeStatus ? undefined : hasActivePush(row) ? '已抛平台的单显示平台状态，不能手工修改' : `${viewNoOf(row)}点击可以更改订单状态`"
                @click="canChangeStatus && onCellStatus(row)"
              >
                <i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}
              </component>
            </td>
            <td v-if="colVisible('hour')" class="cv2-td-center cv2-push-text">{{ row.time_requirement }}</td>
            <td v-if="colVisible('ww')" class="cv2-td-center cv2-push-text cv2-compact" :data-v2-ww="row.order_no"><span v-if="customerKindLabel(row.customer_kind)" class="cv2-kind-tag" :class="`cv2-kind-tag--${row.customer_kind}`" :data-v2-kind-tag="row.order_no">{{ customerKindLabel(row.customer_kind) }}</span>{{ row.customer_ww }}</td>
            <td v-if="colVisible('source')" class="cv2-td-center cv2-push-text cv2-compact">{{ row.source_name ?? row.source }}</td>
            <td v-if="colVisible('taker')" class="cv2-td-center cv2-push-text cv2-compact">
              <button
                v-if="row.taker_name === '' && canAssignTaker"
                type="button"
                class="cv2-assign-btn"
                :data-v2-assign="row.order_no"
                title="指派打手"
                @click="onTakerClick(row)"
              >指派打手</button>
              <button
                v-else-if="(canAssignTaker || canClearTaker) && row.taker_name && row.taker_name !== '无权查看'"
                type="button"
                class="cv2-taker-name"
                :data-v2-reassign="row.order_no"
                :title="canAssignTaker ? '点击改派接手人' : '点击清除接手人'"
                @click="onTakerClick(row)"
              >{{ row.taker_name }}</button>
              <span v-else>{{ row.taker_name }}</span>
            </td>
            <td v-if="colVisible('hallprice')" class="cv2-td-num cv2-compact">
              <span class="cv2-price-line">
                <span v-if="row.taker_settled" class="cv2-paychip cv2-paychip--settled" title="该单打手绩效已结账">结</span>
                <component :is="canChangeHallPrice ? 'a' : 'span'" :href="canChangeHallPrice ? '#' : undefined" class="cv2-money cv2-num cv2-price-amt" :class="{ 'cv2-money-link': canChangeHallPrice }" :data-v2-hall-price="row.order_no" @click.prevent="canChangeHallPrice && onCellHallPrice(row)">{{ moneyText(row.hall_price) }}</component>
              </span>
            </td>
            <td v-if="colVisible('price')" class="cv2-td-num cv2-compact">
              <span class="cv2-price-line">
                <component
                  :is="canChangePaymentStatus ? 'button' : 'span'"
                  :type="canChangePaymentStatus ? 'button' : undefined"
                  class="cv2-paychip"
                  :class="row.payment_status === 'SETTLED' ? 'cv2-paychip--paid' : row.payment_status === 'SETTLING' ? 'cv2-paychip--settling' : 'cv2-paychip--unpaid'"
                  :data-v2-pay-badge="row.order_no"
                  :title="row.payment_status === 'SETTLED' ? '已结款' : row.payment_status === 'SETTLING' ? '结算中' : '未收款'"
                  @click="canChangePaymentStatus && onCellPayStatus(row)"
                >{{ row.payment_status === 'SETTLED' ? '收' : row.payment_status === 'SETTLING' ? '中' : '未' }}</component>
                <component :is="canChangePrice ? 'a' : 'span'" :href="canChangePrice ? '#' : undefined" class="cv2-money cv2-num cv2-price-amt" :class="{ 'cv2-money-link': canChangePrice }" :data-v2-price="row.order_no" @click.prevent="canChangePrice && onCellPrice(row)">{{ moneyText(row.total_price) }}</component>
              </span>
            </td>
            <td v-if="colVisible('net')" class="cv2-td-num cv2-push-text" data-v2-net><span class="cv2-num">{{ row.net_received }}</span></td>
            <td v-if="colVisible('refund')" class="cv2-td-num cv2-compact">
              <component
                :is="canRefund ? 'a' : 'span'"
                :href="canRefund ? '#' : undefined"
                class="cv2-num"
                :class="[Number(row.refund_fee) > 0 ? 'cv2-money' : 'cv2-refund-zero', { 'cv2-refund-link': canRefund }]"
                :data-v2-refund="row.order_no"
                @click.prevent="canRefund && onCellRefund(row)"
              >{{ row.refund_fee }}</component>
            </td>
            <td v-if="colVisible('remark')" class="cv2-td-center cv2-td-project cv2-td-order-text cv2-push-text" :title="row.remark">
              <button type="button" class="cv2-order-text" aria-haspopup="dialog" :aria-label="row.remark ? undefined : '重要备注：无'" :data-v2-remark-open="row.order_no" @click="openOrderText(row, 'remark')">{{ row.remark || '无' }}</button>
            </td>
            <td v-if="showScreenshotColumn" class="cv2-td-shots cv2-td-center">
              <button
                v-if="canUploadScreenshots"
                type="button"
                class="cv2-iconact cv2-shot-upload"
                :data-v2-shot-upload="row.order_no"
                title="上传订单截图"
                aria-label="上传订单截图"
                @click="openScreenshot('order-screenshots-upload', '上传订单截图', row)"
              >
                <i class="layui-icon layui-icon-upload" aria-hidden="true"></i>
              </button>
              <button
                v-if="canViewScreenshots"
                type="button"
                class="cv2-iconact cv2-shot-view"
                :data-v2-shot-view="row.order_no"
                title="查看订单截图"
                aria-label="查看订单截图"
                @click="openScreenshot('order-screenshots-manage', '查看订单截图', row)"
              >
                <i class="layui-icon layui-icon-picture" aria-hidden="true"></i>
              </button>
            </td>
            <td v-if="colVisible('kefu')" class="cv2-td-center cv2-push-text">{{ row.sales_agent }}</td>
            <td v-if="colVisible('pub')" class="cv2-td-center cv2-push-text">{{ row.hall_publisher_name === '' ? '无' : row.hall_publisher_name }}</td>
            <td v-if="colVisible('by')" class="cv2-td-center cv2-push-text">{{ row.created_by_name || row.created_by }}</td>
            <td v-if="colVisible('create')" class="cv2-td-time cv2-push-text">{{ timeText(row.created_at) }}</td>
            <td v-if="colVisible('complete')" class="cv2-td-time cv2-push-text">{{ timeText(row.completed_at) }}</td>
            <td class="cv2-td-fixed">
              <span v-if="!colVisible('order') && !row.__underPenaltyParent && row.penalty_parent_order_no" class="cv2-order-origin" :title="`原单 ${row.penalty_parent_order_no}`">原单 {{ row.penalty_parent_order_no }}</span>
              <div class="cv2-row-actions">
                <button
                  v-if="canEditOrder"
                  type="button"
                  class="cv2-opbtn cv2-opbtn--red"
                  :data-v2-edit="row.order_no"
                  title="编辑订单"
                  aria-label="编辑订单"
                  @click="openRowEdit(row)"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M11.2 2.3l2.5 2.5L5.2 13.3l-3.2.7.7-3.2z" /></svg>
                </button>
                <button
                  v-if="canThrowToHall && row.status !== 'WAITING_GRAB'"
                  type="button"
                  class="cv2-opbtn cv2-opbtn--teal"
                  :data-v2-throw="row.order_no"
                  title="发到内部接单大厅"
                  aria-label="发到内部接单大厅"
                  @click="openThrow(row)"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8 8 2.8 13.5 8M4.2 6.8v6.4h7.6V6.8" /></svg>
                </button>
                <button
                  v-if="canWithdrawThrow && row.status === 'WAITING_GRAB'"
                  type="button"
                  class="cv2-opbtn cv2-opbtn--teal"
                  :data-v2-withdraw="row.order_no"
                  title="从内部接单大厅撤回"
                  aria-label="从内部接单大厅撤回"
                  @click="openWithdraw(row)"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M11.8 3 6.5 8l5.3 5M7.4 3 2.1 8l5.3 5" /></svg>
                </button>
                <button
                  v-if="canPlatformWrite && pushAvailable"
                  type="button"
                  class="cv2-opbtn cv2-opbtn--blue"
                  :data-v2-push-jdp="row.order_no"
                  title="把这笔订单一键发到各大交易平台"
                  aria-label="一键发单到交易平台"
                  @click="openPushJdp(row)"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.6" /><path d="M2.4 8h11.2M8 2.4c2.1 1.9 2.1 9.3 0 11.2-2.1-1.9-2.1-9.3 0-11.2z" /></svg>
                </button>
              </div>
            </td>
            </template>
          </tr>
          <tr v-if="rows.length === 0">
            <td :colspan="visibleColumns.length" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
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
          size="small"
          @current-change="onPageChange"
        />
      </div>
    </section>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>

    <!-- 批量搜单号（搜索1切到批量档后点查询弹出；换行/逗号分隔） -->
    <el-dialog
      :model-value="batchDlg !== null"
      title="批量搜单号"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-batch-search"
      @update:model-value="batchDlg = null"
    >
      <p class="cv2-dlg-text">支持系统单号和来源单号。每行一个，也可用逗号分隔，最多 200 个。</p>
      <el-input
        v-if="batchDlg !== null"
        v-model="batchDlg.text"
        type="textarea"
        :rows="6"
        placeholder="粘贴系统单号或来源单号"
        aria-label="批量单号"
      />
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="batchDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="confirm-batch-search" @click="void doBatchSearch()">搜索</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="assignDlgOpen"
      :title="canAssignTaker ? '指派打手' : '清除接手人'"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      :close-on-click-modal="!assignSubmitting"
      :close-on-press-escape="!assignSubmitting"
      :show-close="!assignSubmitting"
      data-v2="dlg-assign"
      @close="assignDlgOpen = false"
      @closed="onAssignClosed"
    >
      <div v-if="assignDlgOpen && assignDlg !== null">
        <p class="cv2-dlg-text">
          已选 {{ assignDlg.selection.orderNos.length }} 笔订单<span v-if="assignDlg.currentTaker !== ''">（当前接手人：{{ assignDlg.currentTaker }}）</span>。
        </p>
        <ul v-if="assignDlg.fromToolbar" class="cv2-batch-preview" aria-label="待指派订单">
          <li v-for="no in assignDlg.selection.orderNos" :key="no">{{ no }}</li>
        </ul>
        <p v-if="assignDlg.peer?.assignee" class="as-peer-assignee" data-v2="peer-assignee">
          下家接手人 <strong>{{ assignDlg.peer.assignee.studio_name }} · {{ peerAssigneeName }}</strong>
          <button v-if="assignDlg.peer.assignee_lookup_failed" type="button" class="as-list-action" :disabled="assignDlg.peerLoading" @click="void loadPeerAssignment(assignDlg)">重试</button>
        </p>
        <template v-if="assignDlg.currentTaker && !assignDlg.peer?.assignee && assignDlg.tab !== 'jidanpu'">
          <p v-if="assignDlg.jidanpuLoading" class="as-peer-assignee" role="status" data-v2="jidanpu-assignee-loading">正在加载下家接手人…</p>
          <p v-else-if="assignDlg.jidanpuLoadError" class="as-peer-error" role="alert" data-v2="jidanpu-assignee-error">
            暂时无法加载下家接手人。
            <button type="button" class="as-list-action" @click="void loadJidanpuAssignment(assignDlg, true)">重试</button>
          </p>
        </template>
        <p v-if="assignDlg.jidanpu?.current && !assignDlg.jidanpuLoading && !assignDlg.jidanpuLoadError" class="as-peer-assignee" data-v2="jidanpu-current-assignee">
          鸡蛋铺接手人 <strong>{{ assignDlg.jidanpu.current.name }}（编号 {{ assignDlg.jidanpu.current.number }}）</strong>
        </p>
        <div
          v-if="showPeerAssignTab"
          class="as-tabs" role="tablist" aria-label="选择指派工作室"
          @keydown.left.prevent="moveAssignTab(-1)"
          @keydown.right.prevent="moveAssignTab(1)"
          @keydown.home.prevent="focusAssignTab('local')" @keydown.end.prevent="focusAssignTab(showJidanpuAssignTab ? 'jidanpu' : 'peer')"
        >
          <button
            id="assign-local-tab" ref="assignLocalTabButton" type="button" role="tab" aria-controls="assign-local-panel"
            :aria-selected="assignDlg.tab === 'local'" :tabindex="assignDlg.tab === 'local' ? 0 : -1"
            :disabled="assignSubmitting" @click="focusAssignTab('local')"
          >本工作室</button>
          <button
            id="assign-peer-tab" ref="assignPeerTabButton" type="button" role="tab" aria-controls="assign-peer-panel"
            :aria-selected="assignDlg.tab === 'peer'" :tabindex="assignDlg.tab === 'peer' ? 0 : -1"
            :disabled="assignSubmitting" data-v2="peer-assign-tab" @click="focusAssignTab('peer')"
          >团团通下家</button>
          <button
            v-if="showJidanpuAssignTab" id="assign-jidanpu-tab" ref="assignJidanpuTabButton" type="button" role="tab" aria-controls="assign-jidanpu-panel"
            :aria-selected="assignDlg.tab === 'jidanpu'" :tabindex="assignDlg.tab === 'jidanpu' ? 0 : -1"
            :disabled="assignSubmitting" data-v2="jidanpu-assign-tab" @click="focusAssignTab('jidanpu')"
          >鸡蛋铺下家</button>
        </div>
        <div
          v-show="assignDlg.tab === 'local'"
          id="assign-local-panel"
          :role="showPeerAssignTab ? 'tabpanel' : undefined"
          :aria-labelledby="showPeerAssignTab ? 'assign-local-tab' : undefined"
          class="as-columns" :class="{ 'as-columns--single': !canAssignTaker || assignDlg.selection.orderNos.length !== 1 }"
        >
          <section v-if="canAssignTaker" class="as-picker" aria-label="选择打手">
            <div class="as-search">
              <el-input
                v-model="assignDlg.key"
                :maxlength="ASSIGN_KEY_MAXLENGTH"
                placeholder="用户名/名称/标记"
                aria-label="搜索打手"
                clearable
                data-v2="assign-key"
              />
            </div>
            <div class="as-list-heading">
              <h3 class="as-form-title">{{ showAssignResults ? '搜索结果' : '常用打手' }}</h3>
              <button v-if="showAssignResults" type="button" class="as-list-action" data-v2="assign-show-favorites" @click="assignDlg.key = ''">查看常用</button>
              <button v-else-if="assignDlg.favoritesError" type="button" class="as-list-action" @click="void loadAssignFavorites()">重试</button>
            </div>
            <p v-if="showAssignResults && assignDlg.searchState === 'loading'" class="as-empty" role="status">正在搜索…</p>
            <p v-else-if="showAssignResults && assignDlg.searchState === 'error'" class="as-empty" role="status">搜索失败，请重新输入关键词。</p>
            <p v-else-if="!showAssignResults && assignDlg.favoritesLoading" class="as-empty" role="status">正在读取常用打手…</p>
            <p v-else-if="!showAssignResults && assignDlg.favoritesError" class="as-empty" role="status">常用打手读取失败</p>
            <p v-else-if="(showAssignResults ? assignDlg.candidates : assignDlg.favorites).length === 0" class="as-empty" role="status">{{ showAssignResults ? '没有匹配的打手' : '暂无常用打手，搜索后可添加。' }}</p>
            <ul v-else class="as-users" :class="{ 'as-favorite-grid': !showAssignResults }" :data-v2="showAssignResults ? 'assign-candidates' : 'assign-favorites'">
              <li v-for="a in (showAssignResults ? assignDlg.candidates : assignDlg.favorites)" :key="a.id" class="as-user-row">
                <button
                  type="button"
                  class="as-user"
                  :class="{ 'as-user--on': assignDlg.selected?.id === a.id }"
                  :data-v2-assignee="a.id"
                  :aria-pressed="assignDlg.selected?.id === a.id"
                  :title="`${a.name}（职位：${a.role ?? ''}，标记：${a.tag}，备注：${a.remark}）`"
                  @click="pickAssignee(a)"
                >
                  <span class="as-user-name">{{ a.name }}</span>
                  <span v-if="a.tag !== ''" class="as-user-tag">{{ a.tag.slice(0, 1) }}</span>
                  <span v-if="showAssignResults && a.role !== null" class="as-user-role">{{ a.role }}</span>
                </button>
                <button
                  type="button"
                  class="as-list-action"
                  :class="{ 'as-favorite-remove': !showAssignResults }"
                  :title="!showAssignResults ? '移除常用打手' : undefined"
                  :data-v2-toggle-favorite="a.id"
                  :aria-label="`${isFavoriteAssignee(assignDlg, a.id) ? '移除常用打手' : '添加常用打手'} ${a.name}`"
                  :disabled="assignDlg.favoritesLoading || assignDlg.favoritesError || assignFavoritesSaving"
                  @click="void toggleFavoriteAssignee(a)"
                >
                  <span v-if="!showAssignResults" aria-hidden="true">×</span>
                  <template v-else>{{ isFavoriteAssignee(assignDlg, a.id) ? '移除常用' : '加常用' }}</template>
                </button>
              </li>
            </ul>
            <p v-if="assignDlg.selected" class="as-picked" data-v2="assign-picked">已选打手 {{ assignDlg.selected.name }}</p>
            <label v-if="canChangePayout && assignDlg.selected" class="as-price">
              发单价
              <el-input
                v-model="assignDlg.price"
                inputmode="decimal"
                aria-label="发单价"
                :placeholder="assignDlg.currentPrice === '' ? '留空沿用原价' : `原价 ${assignDlg.currentPrice} 元，留空沿用`"
                data-v2="assign-price"
                :disabled="busy"
              />
            </label>
            <label v-if="assignDlg.chatFollowUpAvailable && assignDlg.selected" class="as-chat-follow-up">
              <input v-model="assignDlg.chatFollowUp" type="checkbox" :disabled="busy" />
              我负责跟进，加入订单群
            </label>
          </section>
          <section v-if="assignDlg.selection.orderNos.length === 1" class="as-perfs" data-v2="assign-perfs" aria-label="订单绩效">
            <div class="as-perf">
              <p class="as-form-title">打手绩效</p>
              <table class="as-perf-table">
                <colgroup><col class="as-c1" /><col class="as-c2" /><col class="as-c3" /></colgroup>
                <thead><tr><th>用户名</th><th>预计绩效</th><th>生效绩效</th></tr></thead>
                <tbody>
                  <tr v-for="rowp in assignDlg.perfTaker" :key="rowp.id">
                    <td>{{ rowp.display_name || rowp.username || '—' }}</td>
                    <td class="as-money">{{ rowp.temp_amount }}</td>
                    <td class="as-money">{{ rowp.bonus_amount }}</td>
                  </tr>
                  <tr v-if="assignDlg.perfTaker.length === 0"><td colspan="3" class="as-perf-empty">暂无打手绩效行</td></tr>
                </tbody>
              </table>
            </div>
            <div class="as-perf">
              <p class="as-form-title">客服提成</p>
              <table class="as-perf-table">
                <colgroup><col class="as-c1" /><col class="as-c2" /><col class="as-c3" /></colgroup>
                <thead><tr><th>用户名</th><th>预计绩效</th><th>生效绩效</th></tr></thead>
                <tbody>
                  <tr v-for="rowp in assignDlg.perfSales" :key="rowp.id">
                    <td>{{ rowp.display_name || rowp.username || '—' }}</td>
                    <td class="as-money">{{ rowp.temp_amount }}</td>
                    <td class="as-money">{{ rowp.bonus_amount }}</td>
                  </tr>
                  <tr v-if="assignDlg.perfSales.length === 0"><td colspan="3" class="as-perf-empty">暂无客服提成行</td></tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
        <section v-if="showPeerAssignTab" v-show="assignDlg.tab === 'peer'" id="assign-peer-panel" class="as-peer-panel" role="tabpanel" aria-labelledby="assign-peer-tab">
          <p v-if="assignDlg.peerLoading" class="as-peer-hint" role="status">正在读取下家信息…</p>
          <p v-else-if="assignDlg.peerLoadError" class="as-peer-hint" role="alert">
            {{ assignDlg.peerLoadError }}
            <button type="button" class="as-list-action" @click="void loadPeerAssignment(assignDlg)">重试</button>
          </p>
          <template v-if="assignDlg.peer?.pending">
            <p class="as-peer-hint" role="status">这笔订单正在等下家确认，可以重试同一次指派。</p>
          </template>
          <form v-else-if="assignDlg.peer?.studios.length" class="as-peer-form" @submit.prevent="void searchPeerAccount()">
            <label class="as-peer-field">
              <span>团团通工作室</span>
              <select v-model="assignDlg.peerStudioId" class="as-peer-input" data-v2="peer-assign-studio" :disabled="assignDlg.peerSubmitting">
                <option value="" disabled>请选择工作室</option>
                <option v-for="studio in assignDlg.peer.studios" :key="studio.id" :value="studio.id">{{ studio.name }}</option>
              </select>
            </label>
            <label class="as-peer-field">
              <span>打手账号名称</span>
              <input v-model="assignDlg.peerAccountName" class="as-peer-input" maxlength="150" placeholder="输入部分账号名称" data-v2="peer-assign-account" :disabled="assignDlg.peerSearching || assignDlg.peerSubmitting" />
            </label>
            <button type="submit" class="cv2-btn" data-v2="peer-assign-search" :disabled="assignDlg.peerStudioId === '' || assignDlg.peerAccountName.trim() === '' || assignDlg.peerSearching || busy">{{ assignDlg.peerSearching ? '查找中…' : '查找打手' }}</button>
          </form>
          <p v-else-if="assignDlg.peer && !assignDlg.peer.assignee" class="as-peer-hint">当前订单没有可直派的团团通下家。</p>
          <div v-if="assignDlg.peerMatches.length && !assignDlg.peer?.pending" class="as-peer-matches" role="group" aria-label="匹配打手">
            <button
              v-for="match in assignDlg.peerMatches" :key="match.match_token"
              type="button" class="cv2-btn" data-v2="peer-assign-match"
              :aria-pressed="assignDlg.peerSelection?.match_token === match.match_token"
              :disabled="busy" @click="selectPeerMatch(match)"
            >{{ match.account_name }}</button>
          </div>
          <p v-else-if="assignDlg.peerSearched && !assignDlg.peer?.pending" class="as-peer-hint" role="status">没有匹配的可接单打手。</p>
          <p v-if="assignDlg.peerError" class="as-peer-error" role="alert">{{ assignDlg.peerError }}</p>
          <dl v-if="peerTarget" class="as-peer-preview" data-v2="peer-assign-preview">
            <div><dt>团团通工作室</dt><dd>{{ peerStudioName }}</dd></div>
            <div><dt>打手账号名称</dt><dd>{{ peerTarget.account_name }}</dd></div>
            <div v-if="!canChangePayout || assignDlg.peer?.pending"><dt>发单价</dt><dd>{{ peerPrice === '' ? '请先设置发单价' : `${peerPrice} 元` }}</dd></div>
            <div v-else>
              <dt><label for="assign-peer-price">发单价</label></dt>
              <dd>
                <input
                  id="assign-peer-price" v-model="assignDlg.peerPriceDraft" class="as-peer-input as-peer-price" inputmode="decimal"
                  :placeholder="assignDlg.peer?.price == null ? '输入给下家的价格' : `${assignDlg.peer.price} 元，留空沿用`"
                  data-v2="peer-assign-price" :disabled="busy"
                />
              </dd>
            </div>
          </dl>
        </section>
        <section v-if="showJidanpuAssignTab" v-show="assignDlg.tab === 'jidanpu'" id="assign-jidanpu-panel" class="as-peer-panel" role="tabpanel" aria-labelledby="assign-jidanpu-tab">
          <label v-if="assignDlg.jidanpu?.studios?.length && !assignDlg.jidanpu.pending && !jidanpuAssignmentWorking" class="as-peer-field">
            <span>鸡蛋铺工作室</span>
            <select v-model="assignDlg.jidanpuStudioId" class="as-peer-input" data-v2="jidanpu-assign-studio" :disabled="busy" @change="changeJidanpuStudio(assignDlg)">
              <option v-for="studio in assignDlg.jidanpu.studios" :key="studio.id" :value="studio.id">{{ studio.name }}</option>
            </select>
          </label>
          <p v-if="assignDlg.jidanpuLoading" class="as-peer-hint" role="status">正在读取鸡蛋铺指派信息…</p>
          <p v-else-if="assignDlg.jidanpuLoadError" class="as-peer-error" role="alert">
            {{ assignDlg.jidanpuLoadError }}
            <button type="button" class="as-list-action" @click="void loadJidanpuAssignment(assignDlg, true)">重试</button>
          </p>
          <p v-else-if="jidanpuAssignmentWorking" class="as-peer-hint" role="status">指派中，正在等待鸡蛋铺确认。</p>
          <div v-else-if="assignDlg.jidanpu?.pending" class="as-peer-hint">
            <p role="status">{{ assignDlg.jidanpu.reason || '正在等待鸡蛋铺确认这次指派。' }}</p>
            <button type="button" class="as-list-action" data-v2="jidanpu-verify" @click="void checkJidanpuAssignment(assignDlg)">核对指派结果</button>
            <button v-if="!assignDlg.jidanpuCloseConfirm" type="button" class="as-list-action" @click="assignDlg.jidanpuCloseConfirm = true">结束等待</button>
            <template v-else>
              <p>请先在鸡蛋铺核实原指派。结束等待后，可以重新选择打手。</p>
              <button type="button" class="as-list-action" data-v2="jidanpu-verify-close" @click="void checkJidanpuAssignment(assignDlg, true)">我已核实，结束等待</button>
            </template>
          </div>
          <p v-else-if="assignDlg.peer?.pending" class="as-peer-hint">正在等待下家工作室确认，请先处理已有指派。</p>
          <p v-else-if="assignDlg.jidanpu?.operation_status === 'failed'" class="as-peer-error" role="alert">
            {{ assignDlg.jidanpu.reason }}
            <button type="button" class="as-list-action" data-v2="jidanpu-assign-retry" @click="void loadJidanpuAssignment(assignDlg, true)">重新选择打手</button>
          </p>
          <p v-else-if="assignDlg.jidanpu?.reason" class="as-peer-hint">
            {{ assignDlg.jidanpu.reason }}
            <button type="button" class="as-list-action" @click="void loadJidanpuAssignment(assignDlg, true)">{{ assignDlg.jidanpu.studios?.length ? '重试' : '重新核对' }}</button>
          </p>
          <template v-else-if="assignDlg.jidanpu?.assignable">
            <p v-if="assignDlg.jidanpu.reservation" class="as-peer-hint" role="status">
              已在鸡蛋铺录单，可以重新选择打手。
              <button v-if="!assignDlg.jidanpuCloseConfirm" type="button" class="as-list-action" @click="assignDlg.jidanpuCloseConfirm = true">结束此次指派</button>
              <button v-else type="button" class="as-list-action" @click="void checkJidanpuAssignment(assignDlg, true)">确认结束</button>
            </p>
            <p v-if="!assignDlg.jidanpu.current && !assignDlg.jidanpu.studios?.length" class="as-peer-hint">鸡蛋铺订单暂无接手人。</p>
            <form class="as-peer-form" @submit.prevent="void searchJidanpuAssignment(assignDlg)">
              <label class="as-peer-field">
                <span>鸡蛋铺打手</span>
                <input v-model="assignDlg.jidanpuQuery" class="as-peer-input" maxlength="100" placeholder="输入姓名或打手编号" data-v2="jidanpu-assign-query" :disabled="assignDlg.jidanpuSearching || busy" @input="resetJidanpuSearch(assignDlg)" />
              </label>
              <button type="submit" class="cv2-btn" data-v2="jidanpu-assign-search" :disabled="!assignDlg.jidanpuQuery.trim() || assignDlg.jidanpuSearching || busy">{{ assignDlg.jidanpuSearching ? '查找中…' : '查找打手' }}</button>
            </form>
            <div v-if="assignDlg.jidanpuResults?.items.length" class="as-peer-matches" role="group" aria-label="匹配的鸡蛋铺打手">
              <button v-for="item in assignDlg.jidanpuResults.items" :key="item.number" type="button" class="cv2-btn" data-v2="jidanpu-assign-match"
                :aria-pressed="assignDlg.jidanpuSelection?.number === item.number" :disabled="busy" @click="assignDlg.jidanpuSelection = item; assignDlg.jidanpuConfirming = false">
                {{ item.name }}（{{ item.number }}）
              </button>
            </div>
            <p v-else-if="assignDlg.jidanpuResults" class="as-peer-hint" role="status">没有匹配的可指派打手。</p>
            <p v-if="assignDlg.jidanpuResults?.more" class="as-peer-hint" role="status">还有更多结果，请输入更完整的姓名或编号。</p>
            <dl v-if="assignDlg.jidanpuSelection" class="as-peer-preview">
              <div><dt>选中打手</dt><dd>{{ assignDlg.jidanpuSelection.name }}（{{ assignDlg.jidanpuSelection.number }}）</dd></div>
              <div v-if="assignDlg.jidanpu.studios?.length">
                <dt><label v-if="canChangePayout && !assignDlg.jidanpu.reservation" for="jidanpu-assignment-price">团团通发单价</label><template v-else>团团通发单价</template></dt>
                <dd v-if="canChangePayout && !assignDlg.jidanpu.reservation">
                  <input id="jidanpu-assignment-price" v-model="assignDlg.jidanpuPriceDraft" class="as-peer-input as-peer-price" inputmode="decimal"
                    :placeholder="assignDlg.jidanpu.price == null ? '请输入发单价' : `留空沿用 ${assignDlg.jidanpu.price} 元`" :disabled="busy" />
                </dd>
                <dd v-else>{{ assignDlg.jidanpu.price == null ? '请联系有权限的同事填写' : `${assignDlg.jidanpu.price} 元` }}</dd>
              </div>
              <div>
                <dt><label v-if="canChangePayout" for="jidanpu-assignment-fee">付给下家打手的金额</label><template v-else>付给下家打手的金额</template></dt>
                <dd v-if="canChangePayout">
                  <input id="jidanpu-assignment-fee" v-model="assignDlg.jidanpuFeeDraft" class="as-peer-input as-peer-price" inputmode="decimal"
                    :placeholder="assignDlg.jidanpu.fee === null ? '请输入金额' : `留空沿用 ${assignDlg.jidanpu.fee} 元`" :disabled="busy" @input="assignDlg.jidanpuConfirming = false" />
                </dd>
                <dd v-else>{{ assignDlg.jidanpu.fee === null ? '暂未读到金额，请联系有权限的同事填写' : `${assignDlg.jidanpu.fee} 元` }}</dd>
              </div>
            </dl>
            <p v-if="assignDlg.jidanpuSelection && assignDlg.jidanpuSelection.number === assignDlg.jidanpu.current?.number" class="as-peer-hint">这位打手已接手，无需重复指派。</p>
            <p v-if="assignDlg.jidanpuConfirming && assignDlg.jidanpuSelection" class="as-peer-hint" role="status">
              确认指派给 {{ assignDlg.jidanpuSelection.name }}，付给该打手 {{ (canChangePayout && assignDlg.jidanpuFeeDraft.trim()) || assignDlg.jidanpu.fee }} 元。
            </p>
          </template>
          <p v-if="assignDlg.jidanpuError && !assignDlg.jidanpuLoadError" class="as-peer-error" role="alert">{{ assignDlg.jidanpuError }}</p>
        </section>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="assignSubmitting" @click="assignDlgOpen = false">{{ (assignDlg?.tab === 'peer' && assignDlg.peer?.assignee) || (assignDlg?.tab === 'jidanpu' && assignDlg.jidanpu?.current) ? '关闭' : '取消' }}</button>
          <template v-if="assignDlg?.tab === 'peer'">
            <button
              v-if="assignDlg.peer?.studios.length || assignDlg.peer?.pending"
              type="button" class="cv2-btn cv2-btn--commit cv2-btn--fill" data-v2="peer-assign-confirm"
              :disabled="busy || jidanpuAssignmentWorking || !!assignDlg.jidanpu?.pending || assignDlg.peerLoading || !peerTarget || peerPrice === ''"
              @click="void doPeerAssign()"
            >{{ assignDlg.peerSubmitting ? '等待下家确认…' : assignDlg.peer?.pending ? '重试指派' : '确认指派' }}</button>
          </template>
          <template v-else-if="assignDlg?.tab === 'jidanpu'">
            <button v-if="assignDlg.jidanpu?.operation_status === 'done'" type="button" class="cv2-btn cv2-btn--commit cv2-btn--fill"
              data-v2="jidanpu-assign-again" :disabled="assignDlg.jidanpuLoading" @click="void loadJidanpuAssignment(assignDlg, true)">更换打手</button>
            <button v-if="assignDlg.jidanpu?.assignable && !assignDlg.jidanpuOperationId && !assignDlg.jidanpuLoadError && !assignDlg.peer?.pending" type="button" class="cv2-btn cv2-btn--commit cv2-btn--fill" data-v2="jidanpu-assign-confirm"
              :disabled="busy || assignDlg.peerLoading || assignDlg.jidanpuLoading || !assignDlg.jidanpuSelection
                || assignDlg.jidanpuSelection.number === assignDlg.jidanpu.current?.number
                || (assignDlg.jidanpu.fee === null && (!canChangePayout || !assignDlg.jidanpuFeeDraft.trim()))
                || (!!assignDlg.jidanpu.studios?.length && (!assignDlg.jidanpuResults?.studio_token
                  || (assignDlg.jidanpu.price == null && (!canChangePayout || !assignDlg.jidanpuPriceDraft.trim()))))"
              @click="void doJidanpuAssign(assignDlg)">{{ assignDlg.jidanpuSubmitting ? '指派中…' : assignDlg.jidanpuConfirming || assignDlg.jidanpu.studios?.length ? '确认指派' : '核对指派' }}</button>
          </template>
          <template v-else>
            <button v-if="canAssignTaker" type="button" class="cv2-btn cv2-btn--commit cv2-btn--fill" data-v2="confirm-assign" :disabled="busy || jidanpuAssignmentWorking || !!assignDlg?.peer?.pending || !!assignDlg?.jidanpu?.pending || !!assignDlg?.jidanpu?.reservation" @click="void doAssign()">{{ canChangePayout && assignDlg?.price ? '保存指派和发单价' : '保存指派' }}</button>
            <button v-if="canClearTaker" type="button" class="cv2-btn as-clear-action" data-v2="assign-clear" :disabled="jidanpuAssignmentWorking || !!assignDlg?.peer?.pending || !!assignDlg?.jidanpu?.pending || !!assignDlg?.jidanpu?.reservation" @click="onClearTaker">清除当前所有接手人</button>
          </template>
        </div>
      </template>
    </el-dialog>

    <!-- 清除接手人的确认与绩效选择共用一个弹窗。 -->
    <el-dialog
      :model-value="clearDlg !== null"
      title="清除当前所有接手人"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-clear"
      @update:model-value="clearDlg = null"
    >
      <p v-if="clearDlg !== null" class="cv2-dlg-text">将清除选中的 {{ clearDlg.selection.orderNos.length }} 笔订单的接手人，订单回到未分配。</p>
      <ul v-if="clearDlg?.fromToolbar" class="cv2-batch-preview" aria-label="待清除接手人的订单">
        <li v-for="no in clearDlg.selection.orderNos" :key="no">{{ no }}</li>
      </ul>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="clearDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-clear" @click="void doClearTaker('N')">清除并保留绩效金额</button>
          <button v-if="canChangePayout" type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-clear-and-remove-performance" @click="void doClearTaker('Y')">清除并将绩效金额归零</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="receiptSyncDlg !== null"
      title="同步收款"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="!receiptSyncing"
      :close-on-press-escape="!receiptSyncing"
      :show-close="!receiptSyncing"
      data-v2="dlg-receipt-sync"
      @update:model-value="closeReceiptSync"
    >
      <template v-if="receiptSyncDlg !== null">
        <label class="cv2-field">
          <span class="cv2-field-label">开始日期</span>
          <input v-model="receiptSyncDlg.from" class="cv2-input" type="date" :disabled="receiptSyncing" data-v2="receipt-sync-from" />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">结束日期</span>
          <input v-model="receiptSyncDlg.to" class="cv2-input" type="date" :disabled="receiptSyncing" data-v2="receipt-sync-to" />
        </label>
        <p class="cv2-dlg-text">按录单时间统计，单次最多 {{ RECEIPT_SYNC_MAX_DAYS }} 天。同步在后台进行。</p>
        <p v-if="receiptSyncError" class="receipt-sync-error" role="alert">{{ receiptSyncError }}</p>
      </template>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="receiptSyncing" @click="closeReceiptSync">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" :disabled="receiptSyncing" data-v2="confirm-receipt-sync" @click="void submitReceiptSync()">开始同步</button>
        </div>
      </template>
    </el-dialog>

    <!-- 更改订单状态：拉长一档（lg，radio 一行排开）；可选项 = 后端读取形态的并集；radio 不预选；
         选中 问题单/已暂停/已退单 时多出「说明原因」填空（原因经后端落订单日志） -->
    <el-dialog
      :model-value="statusDlg !== null"
      title="更改订单状态"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-status"
      @update:model-value="statusDlg = null"
    >
      <div class="cv2-status-radios" data-v2="status-targets">
        <label v-for="code in statusDlg?.targets ?? []" :key="code" class="cv2-radio">
          <input v-if="statusDlg !== null" v-model="statusDlg.target" type="radio" name="v2-manage-target" :value="code" />
          {{ statusText(code) }}
        </label>
      </div>
      <div v-if="statusDlg !== null && REASON_TARGETS.includes(statusDlg.target)" class="cv2-field cv2-reason" data-v2="status-reason">
        <span class="cv2-field-label">说明原因</span>
        <el-input
          v-model="statusDlg.reason"
          maxlength="200"
          show-word-limit
          placeholder="原因将记入订单日志（必填）"
          data-v2="status-reason-input"
        />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="statusDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-status" @click="void doChangeStatus()">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 更改订单类型 -->
    <el-dialog
      :model-value="typeDlg !== null"
      title="更改订单类型"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-type"
      @update:model-value="typeDlg = null"
    >
      <div class="cv2-dlg-radios" data-v2="type-targets">
        <label class="cv2-radio">
          <input v-if="typeDlg !== null" v-model="typeDlg.orderType" type="radio" name="v2-manage-ordertype" value="BOOST" />
          代练订单
        </label>
        <label class="cv2-radio">
          <input v-if="typeDlg !== null" v-model="typeDlg.orderType" type="radio" name="v2-manage-ordertype" value="COMPANION" />
          陪玩订单
        </label>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="typeDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-type" @click="void doChangeType()">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 记退款：refund_total = 累计退款总额（口径照 island） -->
    <el-dialog
      :model-value="refundDlg !== null"
      title="记录退款"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-refund"
      @update:model-value="refundDlg = null"
    >
      <p v-if="refundDlg !== null" class="cv2-dlg-text">将为选中的 {{ refundDlg.orderNos.length }} 笔订单记录退款，填写的是<b>累计退款总额</b>。</p>
      <div class="cv2-field">
        <span class="cv2-field-label">退款总额</span>
        <el-input v-if="refundDlg !== null" v-model="refundDlg.refundTotal" placeholder="累计退款总额（元）" data-v2="refund-total" />
      </div>
      <div class="cv2-field">
        <span class="cv2-field-label">退款原因</span>
        <el-input v-if="refundDlg !== null" v-model="refundDlg.reason" placeholder="退款原因（必填）" data-v2="refund-reason" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="refundDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-refund" @click="void doRefund()">确定记录</button>
        </div>
      </template>
    </el-dialog>

    <!-- 改接单价 -->
    <el-dialog
      :model-value="priceDlg !== null"
      title="更改接单价"
      class="cv2-dialog cv2-dialog--sm cv2-dialog--narrow"
      destroy-on-close
      data-v2="dlg-price"
      @update:model-value="priceDlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">接单价</span>
        <el-input v-if="priceDlg !== null" v-model="priceDlg.value" placeholder="可为0，不能为空" data-v2="price-value" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="priceDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-price" @click="void doChangePrice()">查看订单名单</button>
        </div>
      </template>
    </el-dialog>

    <!-- 改发单价：金额直写或按接单价的百分比（如 50%），换算在后端 -->
    <el-dialog
      :model-value="hallPriceDlg !== null"
      title="更改发单价"
      class="cv2-dialog cv2-dialog--sm cv2-dialog--narrow"
      destroy-on-close
      data-v2="dlg-hall-price"
      @update:model-value="hallPriceDlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">发单价</span>
        <el-input v-if="hallPriceDlg !== null" v-model="hallPriceDlg.value" placeholder="金额，或按接单价百分比（如 50%）" data-v2="hall-price-value" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="hallPriceDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-hall-price" @click="void doChangeHallPrice()">查看订单名单</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="batchPreview !== null"
      title="确认订单名单"
      class="cv2-dialog"
      data-v2="dlg-batch-preview"
      @update:model-value="closeBatchPreview(false)"
    >
      <div v-if="batchPreview" class="cv2-batch-preview">
        <p><strong>{{ batchPreview.actionLabel }} · 将处理 {{ batchPreview.applying.length }} 笔订单</strong></p>
        <ul v-if="batchPreview.applying.length">
          <li v-for="item in batchPreview.applying" :key="item">{{ item }}</li>
        </ul>
        <template v-if="batchPreview.extras.length">
          <p><strong>另有 {{ batchPreview.extras.length }} 笔联动订单</strong></p>
          <ul><li v-for="item in batchPreview.extras" :key="item">{{ item }}</li></ul>
        </template>
        <template v-if="batchPreview.skipped.length">
          <p><strong>不会处理 {{ batchPreview.skipped.length }} 笔订单</strong></p>
          <ul><li v-for="item in batchPreview.skipped" :key="item">{{ item }}</li></ul>
        </template>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="cancel-batch-preview" @click="closeBatchPreview(false)">
            {{ batchPreview?.applying.length ? '取消' : '知道了' }}
          </button>
          <button
            v-if="batchPreview?.applying.length"
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="confirm-batch-preview"
            @click="closeBatchPreview(true)"
          >确认处理 {{ batchPreview.applying.length + batchPreview.extras.length }} 笔</button>
        </div>
      </template>
    </el-dialog>

    <!-- 更改收款状态（三档码，radio 不预选） -->
    <el-dialog
      :model-value="payDlg !== null"
      title="更改收款状态"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-pay"
      @update:model-value="payDlg = null"
    >
      <div class="cv2-dlg-radios" data-v2="pay-targets">
        <label v-for="p in PAY_STATUS_OPTIONS" :key="p.code" class="cv2-radio">
          <input v-if="payDlg !== null" v-model="payDlg.code" type="radio" name="v2-manage-pay" :value="p.code" />
          {{ p.label }}
        </label>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="payDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-pay" @click="void doChangePayStatus()">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 重算客服提成 -->
    <el-dialog
      :model-value="recomputeDlg !== null"
      title="重算客服提成"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-recompute"
      @update:model-value="recomputeDlg = null"
    >
      <p v-if="recomputeDlg !== null" class="cv2-dlg-text">将按当前的接单销售客服，重新计算选中的 {{ recomputeDlg.orderNos.length }} 笔订单的客服销售提成账单。</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="recomputeDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-recompute" @click="void doRecompute()">确定重算</button>
        </div>
      </template>
    </el-dialog>

    <V2PenaltyDialog
      v-if="penaltyOrder !== null"
      :original-order-no="penaltyOrder"
      @close="penaltyOrder = null"
      @submitted="onPenaltySubmitted"
    />

    <!-- 删除（软删，可在回收站还原；可选连带删除绩效记录） -->
    <el-dialog
      :model-value="deleteDlg !== null"
      title="删除订单"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete"
      @update:model-value="deleteDlg = null"
    >
      <!-- 删除默认连带删除绩效记录（业务方 2026-11-05「不需要给选择」），isDelEmp 固定 Y -->
      <p v-if="deleteDlg !== null" class="cv2-dlg-text">将删除选中的 {{ deleteDlg.orderNos.length }} 笔订单（软删除，可在回收站还原，连带删除绩效记录）。</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="deleteDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-delete" @click="confirmDelete">{{ deleteDlg && deleteDlg.orderNos.length > DELETE_REASON_THRESHOLD ? '下一步' : '确定删除' }}</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="deleteReasonDlg !== null"
      title="再次确认删除订单"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete-reason"
      @update:model-value="deleteReasonDlg = null"
    >
      <template v-if="deleteReasonDlg !== null">
        <p class="cv2-dlg-text">将删除选中的 {{ deleteReasonDlg.orderNos.length }} 笔订单。删除原因将记入操作日志。</p>
        <label class="cv2-field cv2-reason">
          <span class="cv2-field-label">删除原因</span>
          <el-input
            v-model="deleteReasonDlg.reason"
            type="textarea"
            :rows="3"
            :maxlength="DELETE_REASON_MAX_LENGTH"
            show-word-limit
            aria-label="删除原因"
            placeholder="请输入删除原因（必填）"
            data-v2="delete-reason-input"
          />
        </label>
      </template>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="deleteReasonDlg = null">取消</button>
          <button
            type="button"
            class="cv2-btn cv2-btn--danger"
            data-v2="confirm-delete-reason"
            :disabled="!deleteReasonDlg?.reason.trim()"
            @click="confirmDeleteWithReason"
          >确定删除</button>
        </div>
      </template>
    </el-dialog>

    <!-- 发到内部接单大厅（行内；价留空 = 用当前发单价） -->
    <el-dialog
      :model-value="throwDlg !== null"
      title="发到内部接单大厅"
      class="cv2-dialog cv2-dialog--sm cv2-dialog--narrow"
      destroy-on-close
      data-v2="dlg-throw"
      @update:model-value="throwDlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">发单价</span>
        <el-input v-if="throwDlg !== null" v-model="throwDlg.price" placeholder="打手接单时看到的价（元）" data-v2="throw-price" />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="throwDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="confirm-throw" @click="void doThrow()">确定发单</button>
        </div>
      </template>
    </el-dialog>

    <!-- 从内部接单大厅撤回（行内确认） -->
    <el-dialog
      :model-value="withdrawDlg !== null"
      title="从内部接单大厅撤回"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-withdraw"
      @update:model-value="withdrawDlg = null"
    >
      <p class="cv2-dlg-text">确定从内部接单大厅撤回这笔订单吗？</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="withdrawDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-withdraw" @click="void doWithdraw()">确定撤回</button>
        </div>
      </template>
    </el-dialog>

    <!-- 订单详情：原生双栏（左=订单内容字段区+内容全文，右=订单日志/操作日志+添加框）；
         弹窗档 = xl（body 零内边距，两栏各自滚动） -->
    <OrderDetailDialog
      :model-value="detailDlg !== null"
      :title="detailDlg === null ? '' : `${viewNoOf(detailDlg.row)}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      destroy-on-close
      data-v2="dlg-detail"
      @update:model-value="closeDetail()"
    >
      <V2DetailFace v-if="detailDlg !== null" :order-no="detailDlg.row.order_no" :source-order-no="detailDlg.row.source_order_no" :has-dailiantong-history="detailOrder?.has_dailiantong_history">
      <div class="dtl-wrap">
        <div class="dtl-left">
          <div class="dtl-fields">
            <div class="dtl-row"><span class="dtl-label">订单号</span><span class="cv2-num dtl-strong">{{ viewNoOf(detailDlg.row) }}</span></div>
            <div class="dtl-row"><span class="dtl-label">系统单号</span><span class="cv2-num dtl-dim">{{ detailDlg.row.order_no }}</span></div>
            <div v-if="detailOrder?.penalty_parent_order_no" class="dtl-row dtl-row--wide">
              <span class="dtl-label">原订单</span>
              <a class="cv2-order-link cv2-num" :href="`/orders/${detailOrder.penalty_parent_order_no}`" target="_blank" rel="noopener">{{ detailOrder.penalty_parent_order_no }}</a>
            </div>
            <div v-for="child in detailOrder?.penalty_children ?? []" :key="child.order_no" class="dtl-row dtl-row--wide">
              <span class="dtl-label">罚款子单</span>
              <span><a class="cv2-order-link cv2-num" :href="`/orders/${child.order_no}`" target="_blank" rel="noopener">{{ child.order_no }}</a> {{ statusText(child.status) }}</span>
            </div>
            <div class="dtl-row"><span class="dtl-label">游戏区服</span><span>{{ gameNameOf(detailDlg.row.game) }}{{ serverNameOf(detailDlg.row.server) ? ' / ' + serverNameOf(detailDlg.row.server) : '' }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.row.game_role_name === '' }"><span class="dtl-label">角色名</span><span>{{ detailDlg.row.game_role_name }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': detailDlg.row.project_text === '' }"><span class="dtl-label">代练项目</span><span>{{ detailDlg.row.project_text }}</span></div>
            <div class="dtl-row"><span class="dtl-label">订单状态</span>
              <span class="cv2-st cv2-order-status cv2-st--mark" :class="orderStatusClass(detailDlg.row.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ statusText(detailDlg.row.status) }}</span>
            </div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.row.time_requirement === '' }"><span class="dtl-label">时间要求</span><span>{{ detailDlg.row.time_requirement }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.row.taker_name === '' }"><span class="dtl-label">当前接手</span><span>{{ detailDlg.row.taker_name }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_account }"><span class="dtl-label">游戏账号</span><span>{{ detailOrder?.game_account }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.game_sub_account }"><span class="dtl-label">子账号</span><span>{{ detailOrder?.game_sub_account }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': !detailOrder?.customer_phone }"><span class="dtl-label">联系电话</span><span>{{ detailOrder?.customer_phone }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.row.customer_ww === '' && detailCustomerKind === '' }"><span class="dtl-label">旺旺/来源</span><span><span v-if="detailCustomerKind" class="cv2-kind-tag" :class="`cv2-kind-tag--${detailCustomerKind}`" :data-v2-detail-kind="detailDlg.row.order_no">{{ customerKindLabel(detailCustomerKind) }}</span>{{ detailDlg.row.customer_ww }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': detailDlg.row.sales_agent === '' }"><span class="dtl-label">销售客服</span><span>{{ detailDlg.row.sales_agent }}</span></div>
            <div class="dtl-row"><span class="dtl-label">发单价</span><span class="cv2-num">{{ moneyText(detailDlg.row.hall_price) }}</span></div>
            <div class="dtl-row"><span class="dtl-label">接单价</span><span class="cv2-money">{{ moneyText(detailDlg.row.total_price) }}</span></div>
            <div class="dtl-row"><span class="dtl-label">实收款</span><span class="cv2-num">{{ detailDlg.row.net_received }}</span></div>
            <div class="dtl-row"><span class="dtl-label">退款</span><span class="cv2-num">{{ detailDlg.row.refund_fee }}</span></div>
            <div class="dtl-row"><span class="dtl-label">录单时间</span><span class="cv2-num">{{ timeText(detailDlg.row.created_at) }}</span></div>
            <div class="dtl-row" :class="{ 'dtl-skip': timeText(detailDlg.row.completed_at) === '' }"><span class="dtl-label">完单时间</span><span class="cv2-num">{{ timeText(detailDlg.row.completed_at) }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': detailDlg.row.remark === '' }"><span class="dtl-label">重要备注</span><span>{{ detailDlg.row.remark }}</span></div>
            <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': !detailOrder?.content }"><span class="dtl-label">订单内容</span><span>{{ detailOrder?.content }}</span></div>
          </div>
          <button
            v-if="detailOrder !== null && (detailOrder.status !== 'COMPLETED' || detailOrder.can_continue_split === true) && canAddSplitOrder"
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="continue-split"
            @click="void openSplitDialog(detailOrder)"
          >继续拆分订单</button>
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
              <!-- summary 服务端已拼好「【操作人】动作」整句（spec §5.9），前端只补时间头，⛔ 不再拼 actor -->
              <p v-for="row in detailLogs" :key="row.id" class="dtl-log">
                <span class="dtl-log-time">【{{ logTime(row.created_at) }}】</span>{{ row.summary }}
              </p>
              <p v-if="detailLogs.length === 0" class="dtl-logs-empty">暂无日志记录</p>
            </template>
          </div>
        </div>
      </div>
      </V2DetailFace>
    </OrderDetailDialog>

    <el-dialog
      :model-value="splitDlg !== null"
      :title="splitDlg === null ? '拆分订单' : `${splitDlg.order.order_no} · 拆分订单`"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-split"
      @update:model-value="splitDlg = null"
    >
      <div v-if="splitDlg !== null" class="cv2-split-dialog">
        <div class="cv2-split-members cv2-split-unified">
            <div class="cv2-split-columns-heading" aria-hidden="true"><span>接单价</span></div>
            <div v-if="splitMainMember !== null" class="cv2-split-member cv2-split-main-row">
              <span class="cv2-num">{{ viewNoOf(splitMainMember) }}</span>
              <el-input v-if="splitDlg.order.order_no === splitMainMember.order_no" v-model="splitRemainingText" type="textarea" :autosize="SPLIT_PROJECT_AUTOSIZE" resize="none" aria-label="主单项目内容" data-v2="split-remaining" />
              <el-input v-else v-model="splitExistingDrafts[splitMainMember.order_no]" type="textarea" :autosize="SPLIT_PROJECT_AUTOSIZE" resize="none" aria-label="主单项目内容" :data-v2="`split-existing-${splitMainMember.order_no}`" />
              <strong>{{ moneyText(splitDlg.order.order_no === splitMainMember.order_no ? splitDlg.order.total_price : splitMainMember.total_price) }}</strong>
              <span>组主单</span>
            </div>
            <div v-if="splitDlg.order.order_no !== splitDlg.mainOrderNo" class="cv2-split-member cv2-split-source-row">
              <span class="cv2-num">{{ splitDlg.order.source_order_no || splitDlg.order.order_no }}</span>
              <el-input v-model="splitRemainingText" type="textarea" :autosize="SPLIT_PROJECT_AUTOSIZE" resize="none" aria-label="发起单项目内容" data-v2="split-remaining" />
              <el-input v-if="canChangePrice" v-model="splitSourcePrice" inputmode="decimal" aria-label="本单接单价" data-v2="split-source-price" />
              <strong v-else>{{ moneyText(splitDlg.order.total_price) }}</strong>
              <span>本次拆分来源</span>
            </div>
            <div v-for="member in splitOtherMembers" :key="member.order_no" class="cv2-split-member">
              <span class="cv2-num">{{ viewNoOf(member) }}</span>
              <el-input v-model="splitExistingDrafts[member.order_no]" type="textarea" :autosize="SPLIT_PROJECT_AUTOSIZE" resize="none" aria-label="已有子单内容" :data-v2="`split-existing-${member.order_no}`" />
              <el-input v-if="canChangePrice" v-model="splitExistingPrices[member.order_no]" inputmode="decimal" aria-label="子单接单价" :data-v2="`split-existing-price-${member.order_no}`" />
              <strong v-else>{{ moneyText(member.total_price) }}</strong>
              <span>已有订单 · <span class="cv2-st cv2-order-status" :class="orderStatusClass(member.status)"><i class="cv2-dot" aria-hidden="true"></i>{{ statusText(member.status) }}</span></span>
            </div>
            <div v-for="(item, index) in splitDrafts" :key="`new-${index}`" class="cv2-split-member cv2-split-new-row">
              <span class="cv2-num">新增订单</span>
              <el-input v-model="item.project_text" type="textarea" :autosize="SPLIT_PROJECT_AUTOSIZE" resize="none" aria-label="新增子单项目内容" :data-v2="`split-project-${index}`" />
              <el-input v-model="item.total_price" inputmode="decimal" aria-label="新增子单接单价" :disabled="!canAddSplitOrder" :data-v2="`split-price-${index}`" />
              <button type="button" class="cv2-btn cv2-btn--quiet" @click="removeSplitDraft(index)">删除</button>
            </div>
            <button v-if="canAddSplitOrder" type="button" class="cv2-btn cv2-btn--quiet" data-v2="split-add-row" @click="addSplitDraft">＋ 新增子单</button>
        </div>
        <div class="cv2-split-total">组总价 {{ splitTotals.group }} → 保存后 {{ splitTotals.finalGroup }}（保持不变） · 新增 {{ splitTotals.added }} · 原单预计 {{ splitTotals.projectedMain }} · 发起单预计 {{ splitTotals.projectedSource }}</div>
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="splitDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="split-save" :disabled="splitSaving" @click="void saveSplit()">保存拆分</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="orderTextDlg !== null"
      :title="orderTextDlg === null ? '' : ORDER_TEXT_LABELS[orderTextDlg.field]"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-order-text"
      :show-close="!orderTextSaving"
      :close-on-click-modal="!orderTextSaving"
      :close-on-press-escape="!orderTextSaving"
      @update:model-value="closeOrderText"
    >
      <el-input
        v-if="orderTextDlg !== null"
        v-model="orderTextDlg.value"
        type="textarea"
        :rows="5"
        :readonly="!canUpdateOrder || orderTextSaving"
        :aria-label="ORDER_TEXT_LABELS[orderTextDlg.field]"
        data-v2="order-text-input"
      />
      <template #footer>
        <div v-if="orderTextDlg !== null" class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="orderTextSaving" @click="closeOrderText">{{ canUpdateOrder ? '取消' : '关闭' }}</button>
          <button type="button" class="cv2-btn" data-v2="order-text-copy" :disabled="orderTextDlg.value === ''" @click="void copyOrderText(orderTextDlg.value)">复制</button>
          <button v-if="canUpdateOrder" type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="order-text-save" :disabled="orderTextSaving" @click="void saveOrderText()">保存</button>
        </div>
      </template>
    </el-dialog>

    <!-- 编辑订单：内容 = v2 录单页组件的编辑态（同一份表单，保存走 PATCH）；
         弹层以右侧内容区为基准居中（scoped 覆盖 el-overlay 几何），宽 1000 -->
    <el-dialog
      :model-value="editDlg !== null"
      :title="editDlg === null ? '' : `编辑订单 ${editDlg.viewNo}`"
      class="cv2-dialog cv2-dialog--lg cv2-edit-dlg"
      destroy-on-close
      data-v2="dlg-edit"
      @update:model-value="editDlg = null"
    >
      <V2OrderNew v-if="editDlg !== null" :key="editDlg.orderNo" :order-no="editDlg.orderNo" :allow-split-price="editDlg.allowSplitPrice" @saved="onEditSaved" />
    </el-dialog>

    <!-- 订单截图（上传 / 查看共用壳；弹窗档 = lg 看图类） -->
    <el-dialog
      :model-value="shotDlg !== null"
      :title="shotDlg === null ? '' : shotDlg.title"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-shot"
      :show-close="!shotBusy"
      :close-on-click-modal="!shotBusy"
      :close-on-press-escape="!shotBusy"
      @update:model-value="!shotBusy && (shotDlg = null)"
    >
      <V2ScreenshotUpload v-if="shotDlg !== null && shotDlg.page === 'order-screenshots-upload'" :order-no="shotDlg.orderNo" @busy-change="shotBusy = $event" />
      <iframe v-else-if="shotDlg !== null" :src="shotUrl" class="cv2-island-frame cv2-island-frame--short" :title="shotDlg.title"></iframe>
    </el-dialog>

    <!-- 一键发交易平台（原页布局不动，打开后只换皮） -->
    <el-dialog
      :model-value="pushDlg !== null"
      :title="pushDlg === null ? '' : `发到交易平台 ${pushDlg.viewNo}`"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-push"
      @update:model-value="pushDlg = null"
    >
      <iframe v-if="pushDlg !== null" ref="pushFrame" :src="pushUrl" class="cv2-island-frame" title="发到交易平台" @load="(e) => { fitIslandFrame(e); skinPushFrame(e) }"></iframe>
    </el-dialog>
    <div v-if="hoverPreview" ref="hoverCard" class="cv2-hovercard cv2-project-preview" :style="{ left: hoverPreview.x + 'px', top: hoverPreview.y + 'px' }" data-v2="hover-preview" @mouseenter="keepProjectPreview" @mouseleave="closeProjectPreview">{{ hoverPreview.text }}</div>
  </div>
</template>

<style scoped>
.cv2-toolbar .ttb-strip .cv2-btn.cv2-btn--solid-penalty {
  border-radius: 6px;
  color: var(--tt2-on-primary);
  background: var(--tt2-btn-solid-penalty);
}

.cv2-toolbar .ttb-strip .cv2-btn.cv2-btn--solid-penalty:hover {
  box-shadow: none;
  filter: brightness(0.88);
}

.cv2-batch-preview {
  max-height: 55vh;
  overflow: auto;
  overflow-wrap: anywhere;
}

.cv2-project-preview { box-sizing: border-box; max-width: min(420px, calc(50vw - 22px)); max-height: calc(100vh - 16px); overflow-y: auto; pointer-events: auto; }
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。 */

/* 「通 / 结」小徽标：复用状态签族但压小一号（尺寸非色值）。 */
.cv2-st--pushed {
  padding: 1px 6px;
  font-size: 11px;
  margin-right: 6px;
  vertical-align: 1px;
}

/* 数字列等宽（DRAFT §2：等宽是一切数据的身份；尺寸/字体非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 系统内置单号列（默认隐藏）：12px 等宽，比主单号列轻一档。 */
.cv2-sysno {
  font-size: 12px;
  color: var(--tt2-dim);
}

/* ── 筛选区：每组是一颗圆角字段，标签是浅色前缀，组内不再画竖线。
 * ⚠️ EP 内部件不携带本组件 scoped 属性 ⇒ 必须 :deep()；组框接管边界后控件自身去描边。 */
.flt-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.flt-row + .flt-row {
  margin-top: 8px;
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

/* ⛔ 不用 overflow:hidden 裁圆角——它会把下拉展开面板一起裁掉（面板挂在组内）。
 * 首尾单元格自己带同族圆角。 */
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

/* 这四个下拉本身就是字段名，跟旁边的标签同一档。 */
.flt-group :deep(.flt-prefix .el-select__placeholder:not(.is-transparent)),
.flt-group :deep(.flt-prefix .el-select__placeholder:not(.is-transparent) span) {
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
  font-weight: 500;
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

/* 给日期、时分秒和清除图标预留空间。 */
.flt-group .flt-cell.flt-datecell {
  width: 186px;
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

/* 加急单/未安排：一簇收拢（间距 ~12px），字 14px、勾框 16px，与组条等高。
 * ⚠️ EP 勾选框默认 margin-right:30px 是「隔太远」的真凶，必须清零。 */
.flt-checks {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  height: 32px;
}

.flt-checks :deep(.el-checkbox) {
  height: 34px;
  align-items: center;
  gap: 7px;
  margin-right: 0;
}

.flt-checks :deep(.el-checkbox__label) {
  font-size: 14px;
}

.flt-checks :deep(.el-checkbox__inner) {
  width: 16px;
  height: 16px;
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 30px（32 − 上下边线）。 */
.flt-group :deep(.el-input__wrapper),
.flt-group :deep(.el-select__wrapper) {
  height: 30px;
  min-height: 30px;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

.flt-group :deep(.el-select__wrapper.is-focused),
.flt-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}

/* 宽度档（挂在 EP 组件根上，根继承本组件 scoped 属性）。 */
.flt-w84 { width: 84px; }
.flt-w86 { width: 86px; }
.flt-w92 { width: 92px; }
.flt-w96 { width: 96px; }
.flt-w106 { width: 106px; }
.flt-w108 { width: 108px; }
.flt-w150 { width: 150px; }
.flt-w160 { width: 160px; }

/* 搜索排（最常用行）：独立于换行池之外，永远从最左侧起排。 */
.flt-searchrow {
  align-items: center;
}

/* 搜索1/搜索2 输入框加长（业务方「稍微加长一点点」）。 */
.flt-searchrow .flt-cell--grow {
  min-width: 230px;
}

/* ── 人选「面板内搜索」选择器：组内一格按钮 + 下方面板（搜索框/列表/清空）── */
.flt-pickcell {
  position: relative;
}

.flt-pick {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 148px;
  height: 30px;
  padding: 0 10px;
  border: none;
  background: var(--tt2-panel);
  font: inherit;
  font-size: 13px;
  color: var(--tt2-ink);
  cursor: pointer;
}

.flt-pick-ph {
  color: var(--tt2-filter-ph);
}

.flt-group :deep(.el-input__inner::placeholder),
.flt-group :deep(.el-select__placeholder.is-transparent),
.flt-group :deep(.flt-ph .el-select__placeholder),
.flt-group :deep(.flt-ph .el-select__selected-item) {
  color: var(--tt2-filter-ph);
}

.flt-caret {
  width: 11px;
  height: 7px;
  margin-left: auto;
  flex: none;
  fill: none;
  stroke: var(--tt2-dim);
  stroke-width: 1.5;
}

.flt-pop {
  position: absolute;
  left: 0;
  top: calc(100% + 6px);
  z-index: 30;
  width: 230px;
  padding: 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-panel);
  box-shadow: 0 4px 14px rgba(43, 42, 40, 0.12);
}

.flt-pop-list {
  max-height: 240px;
  overflow-y: auto;
  margin-top: 6px;
}

.flt-pop-list--kefu { scrollbar-width: none; }
.flt-pop-list--kefu::-webkit-scrollbar { display: none; }

.flt-pop-item {
  display: block;
  width: 100%;
  padding: 7px 10px;
  border: none;
  border-radius: 4px;
  background: transparent;
  font: inherit;
  font-size: 13px;
  color: var(--tt2-ink);
  text-align: left;
  cursor: pointer;
}

.flt-pop-item:hover {
  background: var(--tt2-hover-wash);
}

.flt-pop-item--on {
  background: var(--tt2-row-checked);
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.flt-pop-empty {
  margin: 8px;
  font-size: 13px;
  color: var(--tt2-dim);
}

.flt-pop-clear {
  width: 100%;
  margin-top: 6px;
  padding: 6px 0;
  border: none;
  border-top: 1px solid var(--tt2-line);
  background: transparent;
  font: inherit;
  font-size: 12px;
  color: var(--tt2-dim);
  cursor: pointer;
}

.flt-pop-clear:hover {
  color: var(--tt2-primary-deep);
}

/* 查询 / 重置与筛选框同高。查询是实心主按钮，重置是白底细框，避免两颗一样重。 */
.cv2-filter-actions .cv2-btn--lg {
  height: 32px;
  padding: 0 16px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1;
}

.cv2-filter-actions .cv2-btn--tool {
  border-color: var(--tt2-line);
  background: var(--tt2-panel);
  color: color-mix(in srgb, var(--tt2-ink) 62%, var(--tt2-dim));
  font-weight: 500;
}

.cv2-filter-actions .cv2-btn--tool:hover {
  border-color: var(--tt2-primary);
  background: var(--tt2-accent-box-bg);
  color: var(--tt2-primary-deep);
}

/* 收款「收」徽标换对标亮绿（业务方「换一个亮点的颜色 试试用对标那样的绿色」）；
 * token --tt2-pay-badge-green #15803D，白字 5.02:1。本页范围（收入页结算签不动）。 */
.cv2-table-panel .cv2-st--settled {
  background: var(--tt2-pay-badge-green);
}

/* 统计句 = 原版橙（对标证据件字面色 #FF6A00，业务方「颜色改成原版一样的」；对标同款豁免 4.5）。 */
.cv2-total-line {
  color: var(--tt2-summary-orange);
}

/* 统计句状态段可点（业务方指令）：句底色不变（整句仍橙、色继承），可点态 = pointer +
 * 悬停下划线；选中段常驻下划线，标记当前筛选落在哪个状态。 */
.cv2-sum-st {
  padding: 0;
  border: none;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.cv2-sum-st:hover {
  text-decoration: underline;
}

.cv2-sum-st--on {
  text-decoration: underline;
  font-weight: 600;
}

/* ── 订单详情原生双栏（左=订单内容，右=日志；xl 弹窗 body 零内边距）── */
.dtl-wrap {
  display: flex;
  align-items: stretch;
  /* 详情内容高 = 弹窗上限(内容区−48) − 弹窗头 ~56；容器已钉成内容区高，居中对称。 */
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

/* 长文本字段（代练项目/重要备注）独占一段，防长文本打乱整列排版（实测错排根因）。 */
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

.dtl-log-time,
.dtl-log-actor {
  color: var(--tt2-dim);
}

.dtl-logs-empty {
  margin: 16px 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 全局提示 = 对标 layer.msg 居中深条（业务方「那种全局提示 弹到中间展示」）：
 * 覆盖 carbon.css 的顶部白卡片形态；深底 = --tt2-toast-bg（ink 85% 透明派生）。
 * pointer-events: none：toast 是纯文字无按钮，显示期间页面照常可点。 */
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
  pointer-events: none;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（cv2-actlink 的锚点版）。 */
a.cv2-actlink {
  text-decoration: none;
}

a.cv2-actlink:hover {
  text-decoration: underline;
}

/* 金额格的链接态：色由 cv2-money / dim token 承担，这里只去掉下划线、悬停补回。 */
.cv2-money-link,
.cv2-refund-link {
  text-decoration: none;
}

.cv2-money-link:hover,
.cv2-refund-link:hover {
  text-decoration: underline;
}

/* 退款列 0 元那支压灰（色 token：dim 辅助文字）。 */
.cv2-refund-zero {
  color: var(--tt2-dim);
}

.cv2-taker-name {
  padding: 0;
  border: none;
  background: transparent;
  font: inherit;
  cursor: pointer;
  color: inherit;
}

.cv2-taker-name:hover {
  color: var(--tt2-primary-deep);
  text-decoration: underline;
}

.cv2-assign-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  height: 24px;
  padding: 0 7px;
  border: 1px solid #d9c2ab;
  border-radius: 4px;
  background: var(--tt2-panel);
  color: #805334;
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}

.cv2-assign-btn:hover {
  background: #faf5ef;
  border-color: #b89470;
}

/* ── 操作列冻结（业务方「需要冻结 始终展现出来」）：sticky 右钉，
 *    底色随行状态补齐（面板白 / hover 晕 / 勾选青绿底，逐态同碳火行底）。 */
.cv2-th-fixed,
.cv2-td-fixed {
  position: sticky;
  right: 0;
  background: var(--tt2-panel);
}

.cv2-th-fixed {
  z-index: 3;
}

.cv2-td-fixed {
  z-index: 2;
}

tbody tr:hover .cv2-td-fixed,
.cv2-table tbody tr:hover .cv2-td-fixed {
  /* 晕是半透明的：冻结列先垫白底，滚过去的字才不会透出来。 */
  background: var(--tt2-panel);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-row--checked .cv2-td-fixed,
.cv2-table tbody tr.cv2-row--checked .cv2-td-fixed {
  background: var(--tt2-row-checked);
}

.cv2-row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.cv2-opbtn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: none;
  border-radius: 4px;
  color: var(--tt2-on-primary);
  cursor: pointer;
}

.cv2-opbtn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.cv2-opbtn--red { background: var(--tt2-st-accept-dot); }
.cv2-opbtn--teal { background: var(--tt2-btn-solid-yellow); }
.cv2-opbtn--blue { background: var(--tt2-btn-solid-push); }

.cv2-opbtn:hover { filter: brightness(0.92); }

.cv2-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  height: 38px;
  padding: 0 12px;
  background: var(--tt2-panel);
  color: var(--tt2-dim);
  font-size: 12px;
  font-weight: 500;
}

.cv2-table .cv2-th-fixed {
  z-index: 4;
  padding-inline: 8px;
  text-align: right;
  background: var(--tt2-panel);
}

.cv2-table td {
  height: 44px;
  padding: 0 12px;
  color: var(--tt2-dim);
  transition: background-color 200ms ease;
}

@media (prefers-reduced-motion: reduce) {
  .cv2-table td {
    transition: none;
  }
}

.cv2-table td.cv2-td-fixed {
  padding-inline: 8px;
}

.cv2-table .cv2-td-project {
  color: var(--tt2-ink);
}

.cv2-table .cv2-td-order-text { padding: 0; }
.cv2-order-text {
  display: block;
  width: 100%;
  min-height: 42px;
  padding: 0 12px;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: inherit;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.cv2-order-text:hover { text-decoration: underline; }
.cv2-table .cv2-order-text:focus-visible { outline-offset: -2px; }

.cv2-table .cv2-compact,
.cv2-table th:has(> [data-v2-col='ww']),
.cv2-table th:has(> [data-v2-col='source']),
.cv2-table th:has(> [data-v2-col='taker']),
.cv2-table th:has(> [data-v2-col='hallprice']),
.cv2-table th:has(> [data-v2-col='price']),
.cv2-table th:has(> [data-v2-col='refund']) { padding-left: 4px; padding-right: 4px; }

.cv2-table .cv2-row--platform .cv2-push-text,
.cv2-table .cv2-row--platform .cv2-push-text .cv2-taker-name,
.cv2-table .cv2-row--platform .cv2-push-text .cv2-sysno { color: var(--tt2-st-paused); }

.cv2-table tbody tr:hover td {
  background: var(--tt2-hover-wash);
}

.cv2-table tbody tr.cv2-row--checked td {
  background: var(--tt2-row-checked);
}

.cv2-table .cv2-shot-upload { color: var(--tt2-primary-deep); }
.cv2-table .cv2-shot-view { color: var(--tt2-btn-solid-push); }

.cv2-btn.as-clear-action,
.cv2-btn.as-clear-action:hover {
  border-color: transparent;
  background: transparent;
  color: var(--tt2-btn-danger);
}

.cv2-btn.as-clear-action:hover { text-decoration: underline; }

.cv2-table .cv2-th-num,
.cv2-table .cv2-td-num {
  text-align: center;
}

/* 已抛平台：单号用暂停档的暖褐，跟普通单的青绿拉开，不再加「通」字。 */
.cv2-order-link--platform {
  color: var(--tt2-st-paused);
  font-weight: 600;
}

.cv2-order-link--platform:hover {
  color: var(--tt2-st-paused);
  text-decoration: underline;
}

/* 省略号只打在单号上。打在整格上时，超长单号会把行尾的复制钮一起裁掉。 */
.cv2-table td.cv2-td-order {
  --cv2-order-cell-inset: 4px;
  --cv2-order-number-slot: 16px;
  padding-left: var(--cv2-order-cell-inset);
  text-overflow: clip;
}

.cv2-td-order .cv2-order-line {
  display: flex;
  align-items: center;
  width: 100%;
  min-width: 0;
}

.cv2-order-origin {
  display: block;
  padding-inline-start: var(--cv2-order-number-slot, 0px);
  color: var(--tt2-dim);
  font-size: 11px;
  white-space: normal;
  overflow-wrap: anywhere;
}

.cv2-table th:has(.cv2-order-header) { padding-left: 4px; }
.cv2-order-header { display: inline-block; padding-left: 16px; }
.cv2-td-order .cv2-split-slot {
  display: inline-flex;
  flex: 0 0 var(--cv2-order-number-slot);
  height: 22px;
  align-items: center;
  justify-content: center;
}

.cv2-td-order .cv2-order-link {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.cv2-td-order .cv2-urgent {
  flex: none;
  margin-left: 4px;
  margin-right: 0;
}

.cv2-td-order .cv2-st--pushed {
  margin-left: 0;
  margin-right: 6px;
}

.cv2-td-order .cv2-copybtn {
  flex: none;
  opacity: 0;
}

.cv2-table tbody tr:hover .cv2-copybtn {
  opacity: 1;
}

.cv2-table .cv2-st--pushed {
  padding: 0 5px;
  border: none;
  border-radius: 999px;
  background: var(--tt2-link-blue);
  color: var(--tt2-on-primary);
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
}

/* 价格数字在列正中；收款字挂在数字左侧，不占列宽，所以不会把数字挤偏。 */
.cv2-price-line {
  display: inline-flex;
  align-items: center;
  position: relative;
  max-width: 100%;
}

.cv2-price-amt {
  text-align: center;
}

.cv2-paychip {
  position: absolute;
  right: calc(100% + 2px);
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 3px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
}

button.cv2-paychip { cursor: pointer; }

.cv2-paychip--paid {
  color: var(--tt2-on-primary);
  background: var(--tt2-pay-badge-green);
  border-color: var(--tt2-pay-badge-green);
}

.cv2-paychip--settling {
  color: var(--tt2-st-paused-dot);
  border-color: var(--tt2-st-paused-dot);
}

.cv2-paychip--unpaid {
  color: var(--tt2-placeholder-dim);
  border-color: var(--tt2-line);
  font-weight: 500;
}

.cv2-paychip--settled {
  color: var(--tt2-st-doing);
  background: var(--tt2-st-doing-bg);
  border-color: var(--tt2-st-doing-bg);
  cursor: default;
}

/* ── 列显隐选择器（照原版字段选择清单；面板挂 .cv2-colpick 下，
 *    外点关闭由 window click 监听做，closest('.cv2-colpick') 内的点击不算外点）── */
.cv2-colpick {
  position: relative;
  margin-left: 4px;
}

.cv2-colpick-btn {
  width: 34px;
  height: 34px; /* 与工具条钮等高 */
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
  /* 420 → 320：满屏布局下页面 overflow:hidden，面板下拉太高会在矮窗口被裁尾（320 保证 ~720px 窗口也放得下）。 */
  max-height: 320px;
  overflow-y: auto;
}

.cv2-colpick-group :deep(.el-checkbox) {
  margin-right: 0;
  height: 28px;
}

/* ── 弹层 ──────────────────────────────────────────────────────────────────── */

/* 编辑弹层（装 v2 录单页组件）：比 lg 档再宽一点，容下 880 限宽表单。 */
.cv2-page :deep(.el-dialog.cv2-edit-dlg) {
  width: min(1000px, 94vw);
}

/* ── 满屏填充布局（业务方 2026-11-07）：页 = 视口内的 flex 纵向列，页面层面不出滚动条。
 * 高度账：固定顶栏 50 + 固定页签条 42 → 内容区自 y=92 起（--tt2-topbar-h / --tt2-tabs-h）；
 * 壳 .cv2-main 顶部另有 10px 附加空档、底部 48px 留白（carbon.css padding），用负 margin
 * 原地吃掉 ⇒ 页面框 = 100vh − 92px，上缘贴页签条下沿（筛选贴顶）、下缘贴视口底。
 * 段序：筛选(flex:none) → 工具条(flex:none) → 统计句(flex:none) → 表格面板(flex:1 吃满)；
 * 统计句在滚动区外常驻，分页条 sticky 钉面板底（carbon.css）常驻。 */
.cv2-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--tt2-topbar-h) - var(--tt2-tabs-h));
  margin: -10px 0 -48px;
  overflow: hidden;
}

/* 筛选区贴顶，外框拿掉：控件自己是一颗颗圆角字段，不再套一张卡片。 */
.cv2-page .cv2-filter {
  margin-top: 0;
  flex: none;
  padding: 0 0 2px;
  border: none;
  background: transparent;
  border-radius: 0;
}

.cv2-toolbar,
.cv2-total-line {
  flex: none;
}

/* 表格区撑到屏幕底，行不够就空着；分页条固定贴在这块的底边，不跟最后一行走。 */
.cv2-table-panel {
  flex: 1 1 auto;
  min-height: 0;
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
}

.cv2-table-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--tt2-scroll-thumb) transparent;
}

.cv2-batch-search-trigger {
  width: 100%;
  text-align: left;
}

.cv2-table-scroll::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.cv2-table-scroll::-webkit-scrollbar-thumb {
  background: var(--tt2-scroll-thumb);
  border-radius: 3px;
}

.cv2-table-scroll::-webkit-scrollbar-track {
  background: transparent;
}

/* 底部分页条压薄。它在滚动层外面，跟着撑满的表格区走，贴屏幕底。 */
.cv2-page .cv2-pager {
  position: static;
  flex: none;
  gap: 8px;
  padding: 4px 12px;
}

.cv2-pager :deep(.el-pagination) {
  --el-pagination-button-width: 24px;
  --el-pagination-button-height: 24px;
  --el-pagination-font-size: 12px;
  --el-pagination-item-gap: 6px;
}

.cv2-pager .cv2-input--inline {
  height: 24px;
  padding: 0 4px;
  font-size: 12px;
}

/* 弹层表单行距（弹窗内字段纵向排；尺寸非色值）。 */
.cv2-dialog .cv2-field {
  margin-bottom: 12px;
}

/* 改状态弹层：radio 一行排开（业务方「拉长一点」）；原因填空只在三状态出现。 */
.cv2-status-radios {
  display: flex;
  gap: 22px;
  padding: 6px 0 2px;
}

.cv2-status-radios .cv2-radio {
  white-space: nowrap;
}

.cv2-reason {
  margin-top: 12px;
}

/* 收款状态等小弹层的 radio 放不下换行、标签不竖切。 */
.cv2-dlg-radios {
  flex-wrap: wrap;
  row-gap: 10px;
}

.cv2-dlg-radios .cv2-radio {
  white-space: nowrap;
}

/* ── 指派弹窗 ── */
.as-tabs {
  display: flex;
  gap: 16px;
  margin-top: 12px;
  border-bottom: 1px solid var(--tt2-line);
}

.as-tabs button {
  padding: 6px 4px 8px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--tt2-dim);
  font: inherit;
  cursor: pointer;
}

.as-tabs button[aria-selected='true'] {
  border-bottom-color: var(--tt2-primary);
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.as-peer-assignee {
  margin: 8px 0;
  color: var(--tt2-dim);
}

.as-peer-assignee strong { color: var(--tt2-ink); }

.as-peer-panel { padding-top: 12px; }

.as-peer-form {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 10px;
}

.as-peer-field {
  display: grid;
  flex: 1 1 150px;
  gap: 4px;
  min-width: 0;
}

.as-peer-input {
  box-sizing: border-box;
  width: 100%;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--tt2-filter-line);
  border-radius: 4px;
  background: var(--tt2-panel);
  color: var(--tt2-ink);
  font: inherit;
}

.as-peer-input:focus-visible {
  outline: 1px solid var(--tt2-primary);
  box-shadow: 0 0 0 3px var(--tt2-focus-ring);
}

.as-peer-hint, .as-peer-error { margin: 8px 0; }
.as-peer-hint { color: var(--tt2-dim); }
.as-peer-error { color: var(--tt2-st-error); }
.as-peer-matches { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.as-peer-matches .cv2-btn { overflow-wrap: anywhere; }
.as-peer-matches .cv2-btn[aria-pressed="true"] {
  background: var(--tt2-primary);
  color: var(--tt2-on-primary);
}

.as-peer-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  margin: 12px 0 0;
  padding: 8px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.as-peer-preview div { display: flex; gap: 8px; }
.as-peer-preview dt { color: var(--tt2-dim); }
.as-peer-preview dd { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
.as-peer-preview div:has(.as-peer-price) { align-items: center; }
.as-peer-price { width: 155px; font-weight: 400; }

.as-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.12fr);
  gap: 16px;
  margin-top: 12px;
}

.as-columns--single { grid-template-columns: minmax(0, 1fr); }

@media (max-width: 760px) {
  .as-columns { grid-template-columns: minmax(0, 1fr); }
}

.as-picker {
  align-self: start;
  min-width: 0;
  padding: 10px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}

.as-search,
.as-price,
.as-list-heading,
.as-user-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.as-search :deep(.el-input), .as-price :deep(.el-input) { flex: 1; min-width: 0; }

.as-price { white-space: nowrap; }
.as-chat-follow-up {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-block-start: 10px;
  color: var(--tt2-ink);
  cursor: pointer;
}
.as-chat-follow-up input { accent-color: var(--tt2-primary); }

.as-list-heading {
  justify-content: space-between;
  min-height: 30px;
  border-top: 1px solid var(--tt2-line);
  margin-top: 10px;
  padding-top: 6px;
}

.as-list-heading .as-form-title { margin: 0; }

.as-users {
  max-height: 240px;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
}

.as-user-row { gap: 2px; }

.as-favorite-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 128px), 1fr));
  gap: 6px;
}

.as-favorite-grid .as-user-row {
  min-width: 0;
  gap: 0;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-btn-soft-bg);
}

.as-favorite-grid .as-user {
  min-height: 34px;
  gap: 4px;
  padding-inline: 8px 4px;
}

.as-list-action {
  flex: none;
  padding: 4px 6px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--tt2-primary-deep);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.as-list-action:hover { background: var(--tt2-hover-wash); }
.as-list-action:disabled { opacity: .6; cursor: default; }

.as-favorite-remove {
  align-self: stretch;
  min-width: 28px;
  padding: 0;
  font-size: 16px;
}

.as-favorite-grid button:focus-visible {
  outline: 2px solid var(--tt2-primary);
  outline-offset: -2px;
}

.as-user {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  padding: 7px 10px;
  border: none;
  border-radius: 4px;
  background: transparent;
  font: inherit;
  font-size: 13px;
  color: var(--tt2-ink);
  text-align: left;
  cursor: pointer;
}

.as-user:hover {
  background: var(--tt2-hover-wash);
}

.as-user--on {
  background: var(--tt2-row-checked);
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.as-user-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.as-user-tag {
  flex: none;
  padding: 0 6px;
  border-radius: 6px;
  background: var(--tt2-st-paused-bg);
  color: var(--tt2-st-paused);
  font-size: 11px;
}

.as-user-role {
  max-width: 42%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--tt2-dim);
}

.as-empty {
  margin: 8px;
  font-size: 13px;
  color: var(--tt2-dim);
}

.as-form-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--tt2-ink);
}

.as-picked {
  margin: 10px 0 4px;
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.as-perfs {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.as-perf-table { table-layout: fixed; }
.as-c1 { width: 46%; }
.as-c2, .as-c3 { width: 27%; }
.as-money { color: var(--tt2-money-red); font-weight: 600; }

.as-perf {
  min-width: 0;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  padding: 8px 10px;
}

.as-perf-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.as-perf-table th {
  padding: 4px 8px;
  border: 1px solid var(--tt2-line);
  background: var(--tt2-label-bg);
  color: var(--tt2-dim);
  font-weight: 500;
  text-align: left;
}

.as-perf-table td {
  padding: 4px 8px;
  border: 1px solid var(--tt2-line);
  color: var(--tt2-ink);
  overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums;
}

.as-perf-empty {
  color: var(--tt2-dim);
  text-align: center;
}
.receipt-sync-error { color: var(--tt2-danger-hover); }

.cv2-td-order[data-split-line] {
  position: relative;
  overflow: visible;
  --cv2-split-line-left: calc(var(--cv2-order-cell-inset) + var(--cv2-order-number-slot) + .5ch);
}
.cv2-td-order[data-split-line]::before,
.cv2-td-order:is([data-split-line='branch'], [data-split-line='end'])::after {
  position: absolute;
  left: var(--cv2-split-line-left);
  font: 12px var(--tt2-mono);
  pointer-events: none;
  content: '';
}
.cv2-td-order[data-split-line]::before {
  top: 0;
  bottom: -1px;
  border-left: 1px solid #83aaa5;
}
.cv2-td-order[data-split-line='start']::before { top: calc(50% + 8px); }
.cv2-td-order[data-split-line='end']::before { bottom: 50%; }
.cv2-td-order:is([data-split-line='branch'], [data-split-line='end'])::after {
  top: 50%;
  width: calc(1.25ch - 2px);
  border-top: 1px solid #83aaa5;
}
.cv2-split-child-connector {
  flex: 0 0 1.75ch;
  font-family: var(--tt2-mono);
  font-size: 12px;
}
.cv2-row--split-child td { background: #fbf8f1; }
.cv2-split-dialog { display: grid; gap: 16px; }
.cv2-split-members { display: grid; gap: 6px; padding: 10px; border: 1px solid var(--tt2-line); border-radius: 8px; }
.cv2-split-unified { --cv2-split-columns: 190px minmax(0, 1fr) 106px 90px; }
.cv2-split-columns-heading,
.cv2-split-member { display: grid; grid-template-columns: var(--cv2-split-columns); gap: 10px; align-items: center; font-size: 12px; }
.cv2-split-columns-heading { padding: 0 9px; color: var(--tt2-muted); }
.cv2-split-columns-heading > span { grid-column: 3; }
.cv2-split-unified .cv2-split-member { min-height: 54px; padding: 8px; border: 1px solid var(--tt2-line); border-radius: 7px; }
.cv2-split-unified .cv2-split-source-row { background: var(--tt2-surface-soft); }
.cv2-split-unified .cv2-split-new-row { border-style: dashed; background: var(--tt2-surface); }
.cv2-split-new-row > button { justify-self: center; }
.cv2-split-unified .cv2-split-member > span:nth-child(2) { overflow-wrap: anywhere; }
.cv2-split-dialog :deep(textarea) { width: min(100%, 620px); }
@media (max-width: 760px) {
  .cv2-split-unified { --cv2-split-columns: minmax(0, 1fr); }
  .cv2-split-columns-heading { display: none; }
  .cv2-split-new-row > button { justify-self: end; }
  .cv2-split-dialog :deep(textarea) { width: 100%; }
}
.cv2-split-total { padding: 12px 14px; border-radius: 8px; color: var(--tt2-ink); background: var(--tt2-rice); font-weight: 600; }
</style>
