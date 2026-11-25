<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「打手与客服报酬」v2（`/v2/performance`；财务族第二批 2026-11-06，计划
 * `docs/plans/2026-11-06-finance-batch2.md` §2）。旧页 = island 复刻页
 * `layui-island/pages/emp-emplist.ts`（对标 /Emp/emplist，20 列绩效表）。本页按同一功能
 * 重建为炭火 Vue 页：筛选区（发参照旧页 `toPerformanceSearch`：结账状态 / 日期口径 5 档
 * （录单时间默认）/ 起止日期（今天−3 自然月，运行期算）/ 选择游戏 / 收款状态（未收款 /
 * 已结款真发参，「结算中」渲染但点名提示不发参）/ 订单状态多选下拉 / 订单类型 /
 * 订单来源 / 订单分组 / 搜索1 / 销售客服多选 / 系统用户多选（内含「其它分组」两颗虚拟
 * 分组哨兵 option：排除禁止结算的发 `settle_forbidden_scope=excluded`、排除总绩效为负的
 * 发 `exclude_net_negative=1`；批量搜用户只覆盖 `username`，哨兵照发——2026-11-07）/
 * 批量搜用户弹窗）→ 工具条
 * ⭐（2026-11-07 R3，功能性有意偏离登记）「挂负账」红签：归属人在挂负账打手集 ⇒ 系统用户
 * 格用户名旁追加红边小签「挂负账」（红 = --tt2-money-red）。行级标记、零查询语义副作用；
 * 统计句注脚「含 N 名挂负账打手的全部未结账单」已随强制展示一并撤除（2026-11-08 业务方拍板）。
 * 6 颗独立钮 → 统计句（TOTAL_TEXT 骨架逐字复用，退款槽裸整数；本页 scoped 橙覆写
 * `--tt2-summary-orange`——证据件字面色 #ff6a00 特例口径，管理订单页同款）→ 两页签
 * 17 列主表 → 分页（默认 50，八档）。行内编辑格（预计/生效绩效 → `/adjust/`；Enter 提交、
 * blur/Esc/点格外部 = 还原退出不落库——有意不再 blur 即提交；非法输入「金额格式不正确，
 * 此次编辑无效」零请求）、亏损判据 `(price−temp−refund)<=0` 两列加粗、「这单貌似是亏损的」
 * 只进打手行 tooltip（SALES 行 tooltip 固定提成说明）、收款色块（收/未两态，判据 = 接单
 * 实收款 net_receipts）、签三类（结账状态/已申请/结账方式；账单属性 = 纯文字色字）、
 * 系统用户列「查看收款码」（无码提示、不开空白层）、关联订单蓝链
 * → 订单详情 iframe 弹窗（20 位形状闸、标题前缀展示单号）。「清算绩效」与「查重」
 * 两个子组件（`V2SettleDialog.vue` / `V2AlipayDupDialog.vue`，⛔ iframe）。
 *
 * ## 与旧页的口径差（有意，逐条登记）
 * 1. 旧页死筛选项（「排团团号」「批量搜单号」）**不渲染**（假控件不搬）；「申请结算的」照
 *    业务方 2026-11-06 指令展示但不发参；「「X」尚未开放，本次已忽略」合并提示通道保留（今天可达的只有「结算中」）。
 * 2. 日期控件 EP 化（input type=date，形状只有 `YYYY-MM-DD` / 空）⇒ 旧页「『录单时间』
 *    不是可识别的日期，本次已忽略该筛选」提示**不可达**，不实现。
 * 3. 收款色块「未」紫块（旧页 #E6E6FA）在炭火 token 里没有对应紫 ⇒ 照管理订单收款徽标
 *    先例落 `cv2-st--unsettled` 灰蓝（收 = settled 绿）；不造第三态。
 * 4. 旧页「退款」列是无处理器的死链（`href="javascript:void(0)"`）⇒ v2 渲染普通 span +
 *    title（不留点了没反应的假链）。
 * 5. 批量搜用户的逗号串：旧页**不 trim、保留空行**，本页**逐行 trim、丢空行**（照旧页
 *    已登记的「更严方向」同款）；空值判据含纯空白。
 * 6. ⭐（2026-11-09，业务方拍板「打手/下家下拉全改关键词搜，v2 也带上」）系统用户（打手/下家）
 *    多选的账号候选从**预载 ≤200 本地搜**改为**关键词远程搜索**（el-select remote + remote-method
 *    → `listAccounts({ key, page_size: 200 })`，400ms 自防抖；空词不发请求）；旧页同改
 *    （formselects 远程搜索）。预载那份现在只喂「销售客服」多选。已选标签由 el-select
 *    选中缓存保留；哨兵 option 静态在列不受搜索影响。对标偏离登记同旧页第 10 条。
 *
 * ⛔ 不 import island 页模块（纯函数为带出处注记的本地重写，改口径两处同改）。
 */
import { computed, watch, onMounted, reactive, ref, type Directive } from 'vue'
import { isOrderNoShape } from '@/api/orders'
import {
  adjustPerformance,
  deletePerformances,
  downloadPerformanceExport,
  EMP_TYPE,
  fetchPerformancesWithSummary,
  PLAYER_OPTION_EXCLUDE_NET_NEGATIVE,
  PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN,
  splitPlayerFilterSelection,
  type PerformanceListItem,
  type PerformanceSearchParams,
  type PerformanceSummary,
} from '@/api/performances'
import { fetchGames, type ConfigRow } from '@/api/config'
import {
  fetchOrderSourceGroups,
  fetchOrderSources,
  type OrderSourceGroupRow,
  type OrderSourceRow,
} from '@/api/orderSources'
import { filterSourcesByGroup } from './orderSourceFilter'
import { listAccounts, type AccountRow } from '@/api/accounts'
import {
  ORDER_STATUS_VALUES,
  v2OrderStatusLabel,
  V2_ORDER_STATUS_LABELS,
  type OrderStatus,
} from '@/config/orderStatus'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'
import V2SettleDialog from '@/v2/V2SettleDialog.vue'
import V2AlipayDupDialog from '@/v2/V2AlipayDupDialog.vue'
import V2OnlinePayrollDialog from '@/v2/V2OnlinePayrollDialog.vue'
import { PAYROLL_CLOSE_HINT, type PayrollBatch, type PayrollSelection } from '@/api/onlinePayroll'
import { useSessionStore } from '@/stores/session'

/* ── 逐字文案（出处 = island 页同名常量，本地重写不引模块）────────────────── */

const DELETE_CONFIRM_TEXT =
  '真的删除吗？警告：一旦删除，将无法找回，请谨慎处理，请勿随意删除绩效'
const NEED_SELECTION_TEXT = '请选中要操作的记录'
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'
const AMOUNT_INVALID_TEXT = '金额格式不正确，此次编辑无效'
const ADJUST_OK_TEXT = '已提交，按新接单价重算'
const ADJUST_REASON = '绩效管理页行内改金额'
const ORDER_STATUS_NOT_OPEN_TEXT = '「修改订单状态」尚未开放'
const NO_WECHAT_QR_TEXT = '该人员还没有上传微信收款码'
const ROW_STALE_TEXT = '该行数据已失效，请刷新列表后重试'
const NO_TAKER_TEXT = '该笔绩效还没有接单人，不能结账'
/** 收款色块的 title（我方自拟判据句，旧页同文；⛔ 不抄对标「与老板或上家结算」那句）。 */
const RECEIVED_TITLE =
  '本状态按「接单实收款」判定：订单已录入接单实收款（有值且不为 0）⇒ 收；要改请到订单管理里改价'
const UNRECEIVED_TITLE =
  '本状态按「接单实收款」判定：订单未录入接单实收款（或为 0）⇒ 未；要改请到订单管理里改价'
/** 「批量搜用户」弹窗标题（逐字照旧页，含「注意：那半句」——文案照抄、行为照 JS）。 */
const BATCH_USER_PROMPT_TITLE =
  '按系统用户昵称批量查询，每行一条。注意：该搜索会排除其它搜索条件'
const BATCH_USER_EMPTY_TEXT = '搜索项不能为空'

/** 收款状态两档真发参（旧页 NET_RECEIPTS_STATE_MAP：键 = 证据件 option value 原文）。 */
const NET_RECEIPTS_STATE_MAP: Record<string, 'received' | 'unreceived'> = {
  未结算: 'unreceived',
  已结算: 'received',
}

/** 日期口径五档（旧页 DATE_TYPE_MAP 的自描述值；录单时间 = 默认档）。 */
const DATE_TYPES: ReadonlyArray<{ value: string; label: string; api: string }> = [
  { value: '10', label: '发单时间', api: 'published' },
  { value: '20', label: '交单时间', api: 'submitted' },
  { value: '25', label: '录单时间', api: 'created' },
  { value: '40', label: '验收时间', api: 'completed' },
  { value: '30', label: '结账时间', api: 'settled' },
]
const DEFAULT_DATE_TYPE = '25'

/** 订单类型两档（旧页 ORDER_TYPE_MAP：0 代练 / 1 陪玩）。 */
const ORDER_TYPE_OPTIONS: ReadonlyArray<{ value: string; label: string; api: string }> = [
  { value: '0', label: '代练', api: 'BOOST' },
  { value: '1', label: '陪玩', api: 'COMPANION' },
]

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

/* ── 行模型（island toRow 同口径的本地重写；只留本页用到的字段）────────────── */

type PerfRow = {
  id: number
  user: number | null
  userName: string
  /** ⭐（2026-11-07 R3）「挂负账」红签判据（后端恒在布尔键）。 */
  negativeDebt: boolean
  tempAmount: string
  bonusAmount: string
  deductFee: string
  deductMode: string
  deductValue: string
  orderId: string
  sourceOrderNo: string
  empType: string
  orderStatusLabelText: string
  orderStatusEnum: string
  settleStatus: string
  createTime: string
  price: string
  netReceiptsDisplay: string
  netReceiptsRaw: string | null
  refundFee: string
  gameRole: string
  dlProject: string
  kefu: string
  wangwang: string
  logUserName: string
  logUserAccount: string
  settingTime: string
  settled: boolean
  settlementMethod: PerformanceListItem['settlement_method']
  payrollBatchId: string | null
  payrollOccupied: boolean
  settleRequestedAt: string | null
  wechatQrUrl: string | null
}

/** `null` ⇒ `—`（后端契约：扣点越界 / 未收款等可空金额不给假数）。 */
function amountOrDash(value: string | null): string {
  return value === null ? '—' : value
}

/** 后端 ISO-8601 → `YYYY-MM-DD HH:mm`（旧页 formatDateTime 同款正则）。 */
function formatDateTime(value: unknown): string {
  if (typeof value !== 'string') return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(value)
  return m ? `${m[1]} ${m[2]}` : ''
}

function toRow(item: PerformanceListItem & {
  wechat_qr_url?: string | null
  settle_requested_at?: string | null
}): PerfRow {
  return {
    id: item.id,
    user: item.user,
    userName: item.display_name ?? item.username ?? '',
    // ⭐（2026-11-07 R3）红签（后端恒在键；缺键 = 部署窗口 ⇒ fail-closed false）。
    negativeDebt: item.negative_debt === true,
    tempAmount: item.temp_amount,
    bonusAmount: item.bonus_amount,
    deductFee: amountOrDash(item.deduct_fee),
    deductMode: item.deduct_mode,
    deductValue: item.deduct_value,
    orderId: item.order_no,
    sourceOrderNo: item.source_order_no ?? '',
    empType: item.emp_type,
    orderStatusLabelText: v2OrderStatusLabel(item.order_status ?? ''),
    orderStatusEnum: item.order_status ?? '',
    settleStatus: item.settle_status,
    createTime: formatDateTime(item.created_at),
    price: amountOrDash(item.order_price),
    netReceiptsDisplay: amountOrDash(item.net_receipts),
    netReceiptsRaw: item.net_receipts,
    refundFee: amountOrDash(item.refund_fee),
    gameRole: item.game_role ?? '',
    dlProject: item.dl_project ?? '',
    kefu: item.kefu ?? '',
    wangwang: item.wangwang ?? '',
    logUserName: item.log_user_display ?? item.log_user_name ?? '',
    logUserAccount: item.log_user_name ?? '',
    settingTime: item.settled_at === null ? '—' : formatDateTime(item.settled_at),
    settled: item.settled_at !== null,
    settlementMethod: item.settlement_method,
    payrollBatchId: item.online_payroll_batch_id ?? null,
    payrollOccupied: item.online_payroll_occupied === true,
    settleRequestedAt: item.settle_requested_at ?? null,
    wechatQrUrl: item.wechat_qr_url ?? null,
  }
}

/* ── 单元格派生（island templet 的本地重写）──────────────────────────────── */

/** 亏损判据：`(price − tempAmount − refundFee) <= 0`（`Number()` 收口，null 参与 = 0）。 */
function isLossRow(row: PerfRow): boolean {
  return (
    Number(row.price) - Number(row.tempAmount) - Number(row.refundFee) <= 0 &&
    Number.isFinite(Number(row.price))
  )
}

/** 提成说明（旧页 commissionRemark：logUserName 子串判据，⛔ 不是相等）。 */
function commissionRemark(row: PerfRow): string {
  if (row.logUserAccount.includes('固定')) return '按固定金额提成'
  if (row.logUserAccount.includes('接单价')) return '按接单价百分比提成'
  return '按利润百分比提成(退款部分已计算在内)'
}

/** 预计/生效两格的 tooltip（SALES 行固定提成说明；打手行逐字 + 亏损动态句）。 */
function amountTips(row: PerfRow, field: 'tempAmount' | 'bonusAmount'): string {
  if (row.empType === EMP_TYPE.SALES) {
    return `销售客服的提成账单，${commissionRemark(row)}`
  }
  const loss = isLossRow(row) ? '，这单貌似是亏损的' : ''
  if (field === 'tempAmount') {
    return `发给${row.userName}时的预计发单价，结算时请以生效绩效为准，点击可以修改${loss}`
  }
  return `最终需要给${row.userName}结算的金额，如果${row.userName}没亲自交单，这里就是0，点击可以修改${loss}`
}

/** 收款色块：net_receipts 有值且 ≠0 ⇒ 收（负数按「≠0」算收）；null / 0 ⇒ 未。 */
function isReceived(row: PerfRow): boolean {
  const net = row.netReceiptsRaw
  return net !== null && Number(net) !== 0
}

/** 扣点格 title（扣点方式 + 参数值；null 行只说「越界」）。 */
function deductTitle(row: PerfRow): string {
  const mode = row.deductMode
  const value = row.deductValue
  const stateText =
    mode === '0' ? `按比例 ${value}` : mode === '1' ? `固定金额 ${value}` : '未设置扣点'
  return row.deductFee === '—' ? '扣点越界，这一格没有数' : `扣点：${stateText}`
}

/* ── 统计句（TOTAL_TEXT 骨架逐字复用；退款槽裸整数、⛔ 千分位）─────────────── */

const TOTAL_TEXT = [
  '本次筛选条件下共计',
  '0',
  '条记录，总计【预计绩效',
  '0.00',
  '元，生效绩效',
  '0.00',
  '元，',
  '订单金额',
  '0.00',
  '元，记录退款金额',
  '0',
  '，预计差额利润',
  '0.00',
  '元，已生效差额利润',
  '0.00',
  '】，默认显示最近3个月数据，若需扩大时间范围，可手动选择起始日期',
].join('')

/** 把骨架里整数位是 0 的金额槽按出现顺序换值（`String.replace` 第一个匹配；lib 无 replaceAll）。 */
function replaceTotalSlots(template: string, values: readonly string[]): string {
  let out = ''
  let rest = template
  for (const value of values) {
    const at = rest.indexOf('0.00')
    if (at < 0) break
    out += rest.slice(0, at) + value
    rest = rest.slice(at + '0.00'.length)
  }
  return out + rest
}

function totalTextOf(total: number, summary: PerformanceSummary | null): string {
  const n = Number.isFinite(total) ? total : 0
  const slot = (value: string | null | undefined, fallback: string): string =>
    value === undefined || value === null || value === '' ? fallback : value
  const base = replaceTotalSlots(TOTAL_TEXT, [
    slot(summary?.temp_amount, '0.00'),
    slot(summary?.bonus_amount, '0.00'),
    slot(summary?.order_price, '0.00'),
    slot(summary?.profit_expected, '0.00'),
    slot(summary?.profit_actual, '0.00'),
  ])
    .replace('共计0条记录', `共计${n}条记录`)
    .replace('元，记录退款金额0，', `元，记录退款金额${slot(summary?.refund_fee, '0')}，`)
  // （2026-11-08 撤：统计句注脚「含 N 名挂负账打手…」随强制展示功能一并取消，句尾回到骨架本体。）
  return base
}

/* ── 筛选状态与参数（toPerformanceSearch 的本地重写；唯一发参出口）──────────── */

const PAGE_LIMIT = 50
const PAGE_LIMITS = [10, 20, 30, 40, 50, 100, 200, 300]

const pad2 = (n: number): string => String(n).padStart(2, '0')
const ymd = (d: Date): string => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`

/** 默认窗 = 今天 − 3 个自然月 ~ 今天（旧页 defaultDateRange 的日期形状版）。 */
function defaultDateRange(now: Date = new Date()): { dateFrom: string; dateTo: string } {
  const from = new Date(now.getFullYear(), now.getMonth() - 3, now.getDate())
  return { dateFrom: ymd(from), dateTo: ymd(now) }
}

const initialRange = defaultDateRange()

type PerfFilters = {
  settleStatus: string
  dateType: string
  dateFrom: string
  dateTo: string
  game: string
  receiptsState: string
  orderStatuses: OrderStatus[]
  orderType: string
  orderSource: string
  sourceGroup: string
  key1: string
  /** 销售客服多选（逗号串发 `customer`）。 */
  kefuUsernames: string[]
  /** 系统用户多选（逗号串发 `username`；会被批量搜用户覆盖；含两颗虚拟分组哨兵）。 */
  systemUsernames: string[]
  /** 「代练/下家 (0) / 批量搜用户 (10)」档位（旧页 selectSeach1）。 */
  customerType: string
}

const filters = reactive<PerfFilters>({
  settleStatus: '',
  dateType: DEFAULT_DATE_TYPE,
  dateFrom: initialRange.dateFrom,
  dateTo: initialRange.dateTo,
  game: '',
  receiptsState: '',
  orderStatuses: [],
  orderType: '',
  orderSource: '',
  sourceGroup: '',
  key1: '',
  kefuUsernames: [],
  systemUsernames: [],
  customerType: '0',
})

/** 「批量搜用户」的用户名逗号串（null = 没开这一档；非 null 覆盖系统用户多选）。 */
let batchUsername: string | null = null

/** 「申请结算的」死筛选项：后端无对应参数，照原版只展示、不发参（业务方 2026-11-06 指令）。 */
const applySettleChecked = ref(false)

const games = ref<ConfigRow[]>([])
const orderSources = ref<OrderSourceRow[]>([])
const orderSourcesLoaded = ref(false)
const orderGroups = ref<OrderSourceGroupRow[]>([])
/** 选了订单分组后，来源下拉只显示组内来源；分组清空时恢复全部。 */
const availableSources = computed(() => {
  if (!orderSourcesLoaded.value) return orderSources.value
  const groupId = Number(filters.sourceGroup)
  return filterSourcesByGroup(orderSources.value, Number.isFinite(groupId) && groupId > 0 ? groupId : null)
})
watch([() => filters.sourceGroup, orderSourcesLoaded], () => {
  if (!orderSourcesLoaded.value) return
  if (filters.orderSource !== '' && !availableSources.value.some((source) => String(source.id) === filters.orderSource)) {
    filters.orderSource = ''
  }
})
/** 销售客服多选的候选（候选 = 本租户账号；值 = username）。⚠️ 只喂「销售客服」这一颗——
 * 系统用户（打手/下家）那颗已改关键词远程搜索（`playerOptions`），不再吃这份预载。 */
const accountOptions = ref<AccountRow[]>([])
/**
 * 系统用户（打手/下家）多选的候选：⭐ 2026-11-09 起改**关键词远程搜索**（业务方拍板
 * 「打手/下家下拉全改，v2 也带上」）——不再预载 200 个账号本地搜（最大在役租户 729 人底集截断 200，
 * 第 201 位起搜不到；租户名不进 src，见 docs/plans/2026-11-09-orderlist-taker-search.md），输入关键词时 `listAccounts({ key: <词>, page_size: 200 })` 远程取
 * （后端 `key` 筛 username/昵称/QQ/手机号 icase 包含，零后端改动）。空关键词不发请求
 * （候选清空，下拉里只剩「其它分组」两颗哨兵）。已选值由 el-select 自己保留
 * （multiple + remote 的已选标签来自组件内的选中缓存，不随 options 替换消失）。
 */
const playerOptions = ref<AccountRow[]>([])
const playerSearching = ref(false)
let playerSearchSeq = 0
/** 远程搜索每页条数（预载时代同款 200；与旧页各自声明——v2 ⛔ 不 import island 页模块）。 */
const PLAYER_SEARCH_PAGE_SIZE = 200
/** el-select 的 remote-method 没有内置防抖（每次输入都调）⇒ 这里自防 400ms（与旧页 formselects 同款拍板值）。 */
let playerSearchTimer: ReturnType<typeof setTimeout> | null = null

/** 系统用户多选的远程搜索：空词不发请求（候选清空）；有词 → `listAccounts({key, page_size:200})`，过期回包丢弃。 */
function searchPlayerOptions(query: string): void {
  const trimmed = query.trim()
  if (playerSearchTimer !== null) clearTimeout(playerSearchTimer)
  if (trimmed === '') {
    playerOptions.value = []
    playerSearching.value = false
    return
  }
  playerSearching.value = true
  const seq = ++playerSearchSeq
  playerSearchTimer = setTimeout(() => {
    playerSearchTimer = null
    void listAccounts({ key: trimmed, page_size: PLAYER_SEARCH_PAGE_SIZE })
      .then((page) => {
        if (seq !== playerSearchSeq) return // 过期回包：已有更新的搜索，丢弃
        playerOptions.value = page.items.filter((a) => a.username !== '')
      })
      .catch(() => {
        // 候选取不到：清空（这一格筛不了，下拉里只剩哨兵）；⛔ 不弹警告（辅助筛选，同预载时代口径）。
        if (seq === playerSearchSeq) playerOptions.value = []
      })
      .finally(() => {
        if (seq === playerSearchSeq) playerSearching.value = false
      })
  }, 400)
}

/** 主键参数：正整数才发（0 / 非数字一律不发——后端 min_value=1，送 0 会回 1000）。 */
function positiveInt(value: string): number | undefined {
  if (!/^\d+$/.test(value)) return undefined
  const n = Number(value)
  return n >= 1 ? n : undefined
}

/** 逗号串参数：逐项去空白、丢空项、拼回；⛔ 不排序不去重。 */
function commaList(value: string): string {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '')
    .join(',')
}

/** 筛选 → 后端参数（列表 / 搜索 / 刷新 / 导出共用唯一出口；照旧页 toPerformanceSearch）。 */
function buildParams(): PerformanceSearchParams {
  const out: PerformanceSearchParams = {
    emp_type: activeTab.value === 'sales' ? EMP_TYPE.SALES : EMP_TYPE.MAIN,
  }
  if (filters.settleStatus !== '') out.settle_status = filters.settleStatus
  const tier = DATE_TYPES.find((t) => t.value === filters.dateType)
  if (tier !== undefined) out.date_type = tier.api as PerformanceSearchParams['date_type']
  if (/^\d{4}-\d{2}-\d{2}$/.test(filters.dateFrom)) out.date_from = filters.dateFrom
  if (/^\d{4}-\d{2}-\d{2}$/.test(filters.dateTo)) out.date_to = filters.dateTo
  const game = positiveInt(filters.game)
  if (game !== undefined) out.game = game
  // 收款状态两档真发参；「结算中」/怪值映射不到 ⇒ 不发参（留给 warnings 点名提示）。
  const receipts = NET_RECEIPTS_STATE_MAP[filters.receiptsState]
  if (receipts !== undefined) out.net_receipts_state = receipts
  if (filters.orderStatuses.length > 0) out.order_status = filters.orderStatuses.join(',')
  const orderType = ORDER_TYPE_OPTIONS.find((t) => t.value === filters.orderType)
  if (orderType !== undefined) out.order_type = orderType.api
  const orderSource = positiveInt(filters.orderSource)
  if (orderSource !== undefined) out.order_source = orderSource
  const sourceGroup = positiveInt(filters.sourceGroup)
  if (sourceGroup !== undefined) out.source_group = sourceGroup
  // 销售客服逗号串；customer_type 只在 customer 有值时发（0 销售人 = 合法值，判「空」不判真值）。
  const customer = commaList(filters.kefuUsernames.join(','))
  if (customer !== '') {
    out.customer = customer
    out.customer_type = filters.customerType === '10' ? 10 : 0
  }
  // 系统用户多选 + 两颗虚拟分组哨兵（2026-11-07，与旧页 toPerformanceSearch 同一翻译函数）：
  // 哨兵剥出发参（excluded / '1'）；剩余用户名空 ⇒ 不发 username。批量搜用户模式只覆盖
  // username，两颗哨兵照发（照旧页 params 合并语义，登记口径）。
  const playerSelection = splitPlayerFilterSelection(filters.systemUsernames)
  const username =
    batchUsername ?? (playerSelection.usernames.length > 0 ? playerSelection.usernames.join(',') : '')
  if (username !== '') out.username = username
  if (playerSelection.excludeSettleForbidden) out.settle_forbidden_scope = 'excluded'
  if (playerSelection.excludeNetNegative) out.exclude_net_negative = '1'
  const keyword = filters.key1.trim()
  if (keyword !== '') out.keyword = keyword
  return out
}

/** 本次取数要弹的全部警告（旧页 filterWarnings 的可达子集：只剩「结算中」点名）。 */
function filterWarnings(): string[] {
  const out: string[] = []
  if (filters.receiptsState !== '' && NET_RECEIPTS_STATE_MAP[filters.receiptsState] === undefined) {
    out.push(`「${filters.receiptsState}」尚未开放，本次已忽略`)
  }
  return out
}

/* ── 取数（服务端分页 + summary 统计句）──────────────────────────────────── */

const rows = ref<PerfRow[]>([])
const total = ref(0)
const summary = ref<PerformanceSummary | null>(null)
const summaryText = ref(TOTAL_TEXT)
const page = ref(1)
const pageSize = ref(PAGE_LIMIT)
const loading = ref(false)
let loadSeq = 0

async function load(): Promise<void> {
  const seq = ++loadSeq
  loading.value = true
  try {
    const data = await fetchPerformancesWithSummary({ ...buildParams(), page: page.value, page_size: pageSize.value })
    if (seq !== loadSeq) return
    rows.value = data.items.map(toRow)
    total.value = data.total
    summary.value = data.summary ?? null
    summaryText.value = totalTextOf(data.total, data.summary ?? null)
    // 勾选态照旧页 layui checkStatus 的口径 = 只认当前页（每次重取后清空，防跨页误操作）。
    selectedIds.value = []
  } catch (e) {
    if (seq !== loadSeq) return
    // 清表时一起清掉勾选，避免批量按钮操作看不见的旧行。
    rows.value = []
    selectedIds.value = []
    total.value = 0
    summary.value = null
    summaryText.value = totalTextOf(0, null)
    showToast(errText(e), 'error')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function onSearch(): void {
  const warnings = filterWarnings()
  if (warnings.length > 0) showToast(warnings.join('；'))
  page.value = 1
  void load()
}

function onRefresh(): void {
  const warnings = filterWarnings()
  if (warnings.length > 0) showToast(warnings.join('；'))
  void load()
}

function onPageChange(next: number): void {
  page.value = next
  void load()
}

function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (!PAGE_LIMITS.includes(size)) return
  pageSize.value = size
  onPageChange(1)
}

/* ── 页签（代练员报酬账单 / 销售客服提成账单；切换重取，页码照旧页保留）───────── */

type PerfTab = 'main' | 'sales'
const activeTab = ref<PerfTab>('main')

function onTabChange(tab: PerfTab): void {
  if (activeTab.value === tab) return
  activeTab.value = tab
  // 旧页切页签 = reload()（不动表单、页码保留）。
  void load()
}

/* ── 勾选 ──────────────────────────────────────────────────────────────── */

const selectedIds = ref<number[]>([])

const allOnPageSelected = computed(
  () => rows.value.length > 0 && rows.value.every((row) => selectedIds.value.includes(row.id)),
)

function toggleAllOnPage(): void {
  if (allOnPageSelected.value) {
    selectedIds.value = selectedIds.value.filter((id) => !rows.value.some((row) => row.id === id))
  } else {
    const ids = rows.value.map((row) => row.id)
    selectedIds.value = [...new Set([...selectedIds.value, ...ids])]
  }
}

function toggleRow(row: PerfRow): void {
  if (selectedIds.value.includes(row.id)) {
    selectedIds.value = selectedIds.value.filter((id) => id !== row.id)
  } else {
    selectedIds.value = [...selectedIds.value, row.id]
  }
}

/** 勾 0 行 ⇒ 弹「请选中要操作的记录」并回空数组（旧页 checkedRows 同款）。 */
function checkedIdsOrToast(): number[] {
  if (selectedIds.value.length === 0) {
    showToast(NEED_SELECTION_TEXT)
    return []
  }
  return selectedIds.value
}

/* ── 候选下拉（取不到不拦页，旧页同口径）──────────────────────────────────── */

onMounted(() => {
  void fetchGames()
    .then((items) => {
      games.value = items
    })
    .catch(() => {
      games.value = []
    })
  void fetchOrderSources()
    .then((p) => {
      orderSources.value = p.items
      orderSourcesLoaded.value = true
    })
    .catch(() => {
      orderSources.value = []
      orderSourcesLoaded.value = false
    })
  void fetchOrderSourceGroups()
    .then((p) => {
      orderGroups.value = p.items ?? []
    })
    .catch(() => {
      orderGroups.value = []
    })
  // 候选 = 本租户账号（不取 PII）；page_size 200（后端上限现为 300，旧页同款边界；本页未顶格）。
  // ⭐ 这份预载只喂「销售客服」多选；系统用户（打手/下家）已改关键词远程搜索（searchPlayerOptions）。
  void listAccounts({ page_size: 200 })
    .then((p) => {
      accountOptions.value = p.items
    })
    .catch(() => {
      accountOptions.value = []
    })
  void load()
})

/* ── 工具条 6 颗 ───────────────────────────────────────────────────────── */

const payrollSelection = ref<PayrollSelection | null>(null)
const savedPayrollId = ref<string | null>(null)
const payrollSession = useSessionStore()
const canViewPayroll = computed(() => payrollSession.canNode(['finance.online_settlement_details']))
const canStartPayroll = computed(() => canViewPayroll.value && payrollSession.canNode(['finance.change_settlement_status']))

function onPayBtn(): void {
  if (payrollSelection.value || loading.value) return
  if (!canStartPayroll.value) {
    showToast('需同时拥有修改结算状态和查看在线结算明细的权限', 'error')
    return
  }
  const ids = checkedIdsOrToast()
  if (ids.length === 0) return
  const chosen = rows.value.filter((row) => ids.includes(row.id))
  const category = activeTab.value === 'sales' ? 'customer_service' : 'taker'
  const type = category === 'customer_service' ? EMP_TYPE.SALES : EMP_TYPE.MAIN
  if (chosen.length !== ids.length || chosen.some((row) =>
    row.empType !== type || row.orderStatusEnum !== 'COMPLETED' || row.settled || row.settleStatus !== '未结账'
  )) {
    showToast('只能对已完成订单的未结账报酬发起在线结算', 'error')
    return
  }
  savedPayrollId.value = null
  payrollSelection.value = { performance_ids: [...ids], category }
}

function onPayrollClose(batch: PayrollBatch | null): void {
  payrollSelection.value = null
  if (!batch) return
  if (batch.active) savedPayrollId.value = batch.id
  void load()
}

/** 查重弹窗。 */
const alipayDupOpen = ref(false)

/** 清算绩效弹窗（工具条批量 / 行内单行共用）。 */
const settleOpen = ref<{ ids: number[] } | null>(null)

function onSettleToolbar(): void {
  const ids = checkedIdsOrToast()
  if (ids.length === 0) return
  settleOpen.value = { ids: [...ids] }
}

function onSettleRow(row: PerfRow): void {
  // 行内结账签的归属人闸（工具条无此闸，照旧页）；回查不到行 ⇒ 提示刷新。
  const current = rows.value.find((item) => item.id === row.id)
  if (current === undefined) {
    showToast(ROW_STALE_TEXT)
    return
  }
  if (typeof current.user !== 'number') {
    showToast(NO_TAKER_TEXT)
    return
  }
  settleOpen.value = { ids: [current.id] }
}

function onSettled(): void {
  settleOpen.value = null
  void load()
}

/** 删除确认弹窗（自建 sm，⛔ ElMessageBox）。 */
const deleteOpen = ref(false)
const deleting = ref(false)

function onDeleteToolbar(): void {
  const ids = checkedIdsOrToast()
  if (ids.length === 0) return
  deleteOpen.value = true
}

function onDeleteConfirm(): void {
  if (deleting.value) return
  deleting.value = true
  void deletePerformances([...selectedIds.value])
    .then((result) => {
      deleteOpen.value = false
      // `deleted` 是 id 的列表（不是条数），要条数取 `.length`（E-0060 同族）。
      showToast(`已删除 ${result.deleted.length} 条绩效记录`)
      void load()
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      deleting.value = false
    })
}

/** 导出记录（Blob 下载当前筛选；不关页）。 */
const exporting = ref(false)

async function onExport(): Promise<void> {
  if (exporting.value) return
  const warnings = filterWarnings()
  if (warnings.length > 0) showToast(warnings.join('；'))
  exporting.value = true
  try {
    const { blob, filename } = await downloadPerformanceExport(buildParams())
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    exporting.value = false
  }
}

/* ── 批量搜用户弹窗（标题逐字；空值提示后关窗不发请求；成功清条件回第 1 页）───── */

const batchOpen = ref(false)
const batchText = ref('')

function onCustomerTypeChange(): void {
  if (filters.customerType === '10') {
    batchText.value = ''
    batchOpen.value = true
    return
  }
  // 选回「代练/下家」⇒ 清条件、就地重取（旧页同款；多一发请求、语义等价）。
  batchUsername = null
  page.value = 1
  void load()
}

function onBatchSubmit(): void {
  const usernames = batchText.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== '')
  if (usernames.length === 0) {
    // 空内容：提示 + 关窗 + 不发请求（旧页同款）。
    showToast(BATCH_USER_EMPTY_TEXT)
    batchOpen.value = false
    return
  }
  batchUsername = usernames.join(',')
  batchOpen.value = false
  page.value = 1
  void load()
}

/* ── 行内编辑（预计 / 生效绩效 → /adjust/；非法输入零请求）────────────────── */

const editing = ref<{ id: number; field: 'tempAmount' | 'bonusAmount' } | null>(null)
const editValue = ref('')

function startEdit(row: PerfRow, field: 'tempAmount' | 'bonusAmount'): void {
  editing.value = { id: row.id, field }
  editValue.value = field === 'tempAmount' ? row.tempAmount : row.bonusAmount
}

/** 动态插入的编辑框 autofocus 属性不可靠 ⇒ 挂载即聚焦。 */
const vFocus: Directive<HTMLInputElement> = { mounted: (el) => el.focus() }

/** 退出编辑并还原原值（blur / Esc / 点格外部共用；未提交不落库——不再 blur 即提交）。 */
function cancelEdit(): void {
  editing.value = null
}

function commitEdit(row: PerfRow): void {
  const cell = editing.value
  if (cell === null) return
  const value = editValue.value.trim()
  editing.value = null
  // 只挡明显不是数的输入；下界不判（金额可以为 0 / 负数）。不发请求，只提示。
  if (value === '' || !/^-?\d+(\.\d+)?$/.test(value)) {
    showToast(AMOUNT_INVALID_TEXT)
    return
  }
  void adjustPerformance(row.id, { taker_price: value, reason: ADJUST_REASON })
    .then(() => {
      showToast(ADJUST_OK_TEXT)
      // 后端按提成比例重算 ⇒ 必须重新取数（不刷新会以为「改了没反应」）。
      void load()
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
}

/* ── 收款码 / 订单详情 / 订单状态点击 ─────────────────────────────────────── */

const qrDlg = ref<{ owner: string; url: string } | null>(null)

function onViewQr(row: PerfRow): void {
  const current = rows.value.find((item) => item.id === row.id)
  if (current === undefined) {
    showToast(ROW_STALE_TEXT)
    return
  }
  const url = current.wechatQrUrl
  if (url === null || url === '') {
    showToast(NO_WECHAT_QR_TEXT)
    return
  }
  qrDlg.value = { owner: current.userName === '' ? '未分配' : current.userName, url }
}

const detailDlg = ref<{ orderNo: string; viewNo: string } | null>(null)

function openOrderDetail(row: PerfRow): void {
  // 形状闸：20 位系统单号（后端详情路由的约束）。不匹配 ⇒ 不开层 + 固定文案。
  if (!isOrderNoShape(row.orderId)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  // 标题前缀 = 展示单号（来源单号优先，data-orderIdView 口径；空回落系统单号）。
  detailDlg.value = { orderNo: row.orderId, viewNo: row.sourceOrderNo !== '' ? row.sourceOrderNo : row.orderId }
}

function onOrderStatusClick(): void {
  // 旧页那颗「更改订单状态」弹层入口已按业务方裁定删掉，这一列只负责显示 ⇒ 点击只提示。
  showToast(ORDER_STATUS_NOT_OPEN_TEXT)
}

/* ── 状态签的炭火映射（表内订单状态列；筛选下拉直接持枚举）────────────────── */

/** 8 态枚举 → cv2-st--* 类名（V2MyOrders/V2OrderManage 同一张表；⛔ 不抄旧页字面色）。 */
</script>

<template>
  <div class="cv2-page" data-v2-page="performance">
    <!-- 筛选区：原版两行（照 replica/emp-emplist.html 两段 form-pane 的字段配对与顺序；
         每对定宽、行左对齐、窗口拉宽不满铺）——发参逻辑不动，只重排 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="uf-row">
        <div class="uf-group" data-v2-filter-group="game">
          <span class="uf-label">选择游戏</span>
          <span class="uf-cell">
            <el-select v-model="filters.game" filterable placeholder="请选择" class="uf-w150" data-v2-filter="game" :teleported="false">
              <el-option value="" label="请选择" />
              <el-option v-for="g in games" :key="g.id" :value="String(g.id)" :label="g.name" />
            </el-select>
          </span>
        </div>
        <!-- 录单时间口径 + 起止日期（原版一节连体，无 label；select 自报当前档） -->
        <div class="uf-group" data-v2-filter-group="date">
          <span class="uf-cell">
            <el-select v-model="filters.dateType" class="uf-w106 uf-prefix" data-v2-filter="date-type" :teleported="false">
              <el-option v-for="t in DATE_TYPES" :key="t.value" :value="t.value" :label="t.label" />
            </el-select>
          </span>
          <span class="uf-cell uf-datecell">
            <input v-model="filters.dateFrom" class="cv2-input uf-date" type="date" data-v2-filter="date-from" />
          </span>
          <span class="uf-dash" aria-hidden="true">~</span>
          <span class="uf-cell uf-datecell">
            <input v-model="filters.dateTo" class="cv2-input uf-date" type="date" data-v2-filter="date-to" />
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="source">
          <span class="uf-label">订单来源</span>
          <span class="uf-cell">
            <el-select v-model="filters.orderSource" filterable placeholder="全部来源" class="uf-w150" data-v2-filter="source" :teleported="false">
              <el-option value="" label="全部来源" />
              <el-option v-for="s in availableSources" :key="s.id" :value="String(s.id)" :label="s.name" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="source-group">
          <span class="uf-label">订单分组</span>
          <span class="uf-cell">
            <el-select v-model="filters.sourceGroup" filterable placeholder="全部分组" class="uf-w150" data-v2-filter="source-group" :teleported="false">
              <el-option value="" label="全部分组" />
              <el-option v-for="g in orderGroups" :key="g.id" :value="String(g.id)" :label="g.name" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="order-type">
          <span class="uf-label">订单类型</span>
          <span class="uf-cell">
            <el-select v-model="filters.orderType" placeholder="全部" class="uf-w140" data-v2-filter="order-type" :teleported="false">
              <el-option value="" label="全部" />
              <el-option v-for="t in ORDER_TYPE_OPTIONS" :key="t.value" :value="t.value" :label="t.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="kefu">
          <span class="uf-label">销售客服</span>
          <span class="uf-cell">
            <el-select v-model="filters.kefuUsernames" multiple filterable collapse-tags collapse-tags-tooltip placeholder="请选择" class="uf-w170" data-v2-filter="kefu" :teleported="false">
              <el-option v-for="a in accountOptions" :key="a.username" :value="a.username" :label="a.display_name || a.username" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="receipts">
          <span class="uf-label">收款状态</span>
          <span class="uf-cell">
            <el-select v-model="filters.receiptsState" placeholder="全部" class="uf-w140" data-v2-filter="receipts" :teleported="false">
              <el-option value="" label="全部" />
              <el-option value="未结算" label="未收款" />
              <el-option value="已结算" label="已结款" />
              <el-option value="结算中" label="结算中" />
            </el-select>
          </span>
        </div>
      </div>
      <div class="uf-row uf-row--second">
        <!-- 代练/下家 + 系统用户（原版同节配对：两颗裸 select 连体，无 label；选「批量搜用户」弹窗） -->
        <div class="uf-group" data-v2-filter-group="system-user">
          <span class="uf-cell">
            <el-select v-model="filters.customerType" class="uf-w130 uf-prefix" data-v2-filter="customer-type" :teleported="false" @change="onCustomerTypeChange">
              <el-option value="0" label="代练/下家" />
              <el-option value="10" label="批量搜用户" />
            </el-select>
          </span>
          <span class="uf-cell">
            <!-- ⭐ 2026-11-09 候选改关键词远程搜索（业务方「全改，v2 也带上」）：remote + remote-method
                 走 listAccounts({key})；已选标签由 el-select 的选中缓存保留（不随候选替换消失）；
                 「其它分组」两颗哨兵是静态 option、始终在列。视觉与布局不动（同一颗下拉）。 -->
            <el-select v-model="filters.systemUsernames" multiple filterable remote :remote-method="searchPlayerOptions" :loading="playerSearching" reserve-keyword collapse-tags collapse-tags-tooltip placeholder="请选择" class="uf-w170" data-v2-filter="system-user" :teleported="false">
              <!-- 「其它分组」虚拟组头（2026-11-07；2026-11-08 置顶到候选之前）：两颗哨兵 option，经 buildParams 的同一翻译函数发参。 -->
              <el-option-group label="其它分组" data-v2-filter="player-virtual-group">
                <el-option :value="PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN" label="排除禁止结算的" />
                <el-option :value="PLAYER_OPTION_EXCLUDE_NET_NEGATIVE" label="排除总绩效为负的" />
              </el-option-group>
              <!-- 候选 = 关键词远程搜索的结果（playerOptions）；文本与值的回落口径与预载时代同款。 -->
              <el-option v-for="a in playerOptions" :key="a.username" :value="a.username" :label="a.display_name || a.username" />
            </el-select>
          </span>
        </div>
        <!-- 订单状态多选下拉（照管理订单页：multiple + collapse-tags；8 态选项与反查枚举不变） -->
        <div class="uf-group" data-v2-filter-group="order-status">
          <span class="uf-label">订单状态</span>
          <span class="uf-cell">
            <el-select v-model="filters.orderStatuses" multiple collapse-tags collapse-tags-limit="2" placeholder="全部状态" class="uf-w160" data-v2-filter="order-status" :teleported="false">
              <el-option v-for="s in ORDER_STATUS_VALUES" :key="s" :value="s" :label="V2_ORDER_STATUS_LABELS[s]" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="settle">
          <span class="uf-label">结账状态</span>
          <span class="uf-cell">
            <el-select v-model="filters.settleStatus" placeholder="全部" class="uf-w140" data-v2-filter="settle" :teleported="false">
              <el-option value="" label="全部" />
              <el-option value="未结账" label="未结账" />
              <el-option value="已结账" label="已结账" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="key1">
          <span class="uf-label">搜索1</span>
          <span class="uf-cell">
            <el-input v-model="filters.key1" placeholder="订单号/来源单号/角色名" class="uf-w165" data-v2-filter="key1" />
          </span>
        </div>
        <el-checkbox v-model="applySettleChecked" class="uf-check" data-v2="apply-settle">申请结算的</el-checkbox>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn-search cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onSearch">搜索</button>
        </div>
      </div>
    </section>

    <!-- 改结算状态是天天用的，深橙实底；打款先不做，青绿实底，两颗分开。 -->
    <div class="cv2-toolbar perf-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--tool perf-tbtn cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新列表</button>
      <button type="button" class="cv2-btn perf-tbtn cv2-btn--commit cv2-btn--fill" data-v2="settle" @click="onSettleToolbar">仅修改结算状态</button>
      <button type="button" class="cv2-btn cv2-btn--tool perf-tbtn cv2-btn--find" data-v2="alipay-dup" @click="alipayDupOpen = true">检测重复收款支付宝</button>
      <button type="button" class="cv2-btn cv2-btn--primary perf-tbtn cv2-btn--fill" data-v2="pay" @click="onPayBtn">结算并直接打款</button>
      <button type="button" class="cv2-btn cv2-btn--tool perf-tbtn cv2-btn--quiet" data-v2="export" :disabled="exporting" @click="onExport">导出记录</button>
      <button type="button" class="cv2-btn perf-tbtn cv2-btn--danger" data-v2="delete" @click="onDeleteToolbar">删除记录</button>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ selectedIds.length }} 行</span>
    </div>

    <!-- 统计句：TOTAL_TEXT 骨架逐字（本页 scoped 橙覆写——证据件字面色特例口径） -->
    <p class="cv2-total-line" data-v2="total">{{ summaryText }}</p>

    <!-- 页签：切换重取（表单与页码照旧页保留） -->
    <div class="cv2-ptabs" data-v2="tabs">
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'main' }"
        data-v2-tab="main"
        @click="onTabChange('main')"
      >
        代练员报酬账单
      </button>
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'sales' }"
        data-v2-tab="sales"
        @click="onTabChange('sales')"
      >
        销售客服提成账单
      </button>
    </div>

    <!-- 主表含勾选列；隐藏不再使用的角色、项目及旺旺/YY列。 -->
    <section class="cv2-panel cv2-table-panel perf-table-panel" aria-label="绩效列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input type="checkbox" data-v2="check-page-all" :checked="allOnPageSelected" aria-label="勾选当前页全部行" @change="toggleAllOnPage" />
            </th>
            <th class="cv2-th-center">系统用户</th>
            <th class="cv2-th-num cv2-th-center">预计绩效</th>
            <th class="cv2-th-num cv2-th-center">生效绩效</th>
            <th class="cv2-th-num cv2-th-center">扣点</th>
            <th class="cv2-th-num cv2-th-center">订单金额</th>
            <th class="cv2-th-num cv2-th-center">实际收款</th>
            <th class="cv2-th-num cv2-th-center">退款</th>
            <th class="cv2-th-center">账单属性</th>
            <th class="perf-th-order">关联订单</th>
            <th class="cv2-th-center">销售客服</th>
            <th class="cv2-th-center perf-th-status">订单状态</th>
            <th class="cv2-th-center">记录时间</th>
            <th class="cv2-th-center">记录者</th>
            <th class="cv2-th-center">结账状态</th>
            <th class="cv2-th-center">结账时间</th>
            <th class="cv2-th-center">结账方式</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td class="cv2-th-check">
              <input
                type="checkbox"
                :aria-label="`勾选 ${row.id}`"
                :data-v2-check="row.id"
                :checked="selectedIds.includes(row.id)"
                @change="toggleRow(row)"
              />
            </td>
            <td class="cv2-td-center">
              <a href="#" class="cv2-actlink perf-userlink" :data-v2-view-qr="row.id" title="查看收款码" @click.prevent="onViewQr(row)">{{ row.userName }}</a>
              <!-- ⭐（2026-11-07 R3）「挂负账」红签（功能性有意偏离登记，文件头） -->
              <span
                v-if="row.negativeDebt"
                class="perf-negdebt-tag"
                data-v2="neg-debt-tag"
                title="该打手名下还有未结账的负绩效行"
              >挂负账</span>
            </td>
            <td class="cv2-td-num cv2-td-center perf-cell">
              <button
                type="button"
                class="cv2-money cv2-num perf-amount"
                :class="{
                  'perf-amount--loss': isLossRow(row),
                  'perf-amount--ghost': editing !== null && editing.id === row.id && editing.field === 'tempAmount',
                }"
                :data-v2-cell="'tempAmount'"
                :title="amountTips(row, 'tempAmount')"
                @click="startEdit(row, 'tempAmount')"
              >{{ row.tempAmount }}</button>
              <input
                v-if="editing !== null && editing.id === row.id && editing.field === 'tempAmount'"
                v-focus
                v-model="editValue"
                class="perf-edit"
                data-v2="cell-edit"
                @keydown.enter.prevent="commitEdit(row)"
                @blur="cancelEdit"
                @keydown.esc.prevent="cancelEdit"
              />
            </td>
            <td class="cv2-td-num cv2-td-center perf-cell">
              <button
                type="button"
                class="cv2-money cv2-num perf-amount"
                :class="{
                  'perf-amount--loss': isLossRow(row),
                  'perf-amount--ghost': editing !== null && editing.id === row.id && editing.field === 'bonusAmount',
                }"
                :data-v2-cell="'bonusAmount'"
                :title="amountTips(row, 'bonusAmount')"
                @click="startEdit(row, 'bonusAmount')"
              >{{ row.bonusAmount }}</button>
              <input
                v-if="editing !== null && editing.id === row.id && editing.field === 'bonusAmount'"
                v-focus
                v-model="editValue"
                class="perf-edit"
                data-v2="cell-edit"
                @keydown.enter.prevent="commitEdit(row)"
                @blur="cancelEdit"
                @keydown.esc.prevent="cancelEdit"
              />
            </td>
            <td class="cv2-td-num cv2-td-center">
              <span class="cv2-money" :title="deductTitle(row)">{{ row.deductFee }}</span>
            </td>
            <td class="cv2-td-num cv2-td-center">
              <span class="cv2-st cv2-st--pushed" :class="isReceived(row) ? 'cv2-st--settled' : 'cv2-st--unsettled'" :title="isReceived(row) ? RECEIVED_TITLE : UNRECEIVED_TITLE" :data-v2-pay-badge="row.id">{{ isReceived(row) ? '收' : '未' }}</span>
              <span class="cv2-money">{{ row.price }}</span>
            </td>
            <td class="cv2-td-num cv2-td-center"><span class="cv2-money cv2-num">{{ row.netReceiptsDisplay }}</span></td>
            <td class="cv2-td-num cv2-td-center">
              <span class="cv2-money" :title="Number(row.refundFee) > 0 ? `退款金额：${row.refundFee}元` : '没有退款'">{{ row.refundFee }}</span>
            </td>
            <td class="cv2-td-center">
              <span v-if="row.empType === '打单工资'" class="perf-emp-doing" :data-v2-emp-type="row.id">打手的</span>
              <span v-else-if="row.empType === '销售提成'" class="perf-emp-doing" :data-v2-emp-type="row.id">客服的</span>
              <span v-else class="perf-emp-ink" :data-v2-emp-type="row.id">{{ row.empType }}</span>
            </td>
            <td>
              <a href="#" class="cv2-ono" :data-v2-detail="row.orderId" title="点击查看订单详情" @click.prevent="openOrderDetail(row)">{{ row.sourceOrderNo || row.orderId }}</a>
            </td>
            <td class="cv2-td-center">{{ row.kefu }}</td>
            <td class="cv2-td-center">
              <button type="button" class="cv2-st cv2-order-status cv2-st--mark" :class="orderStatusClass(row.orderStatusEnum)" :data-v2-order-status="row.id" :title="`当前订单状态：${row.orderStatusLabelText}`" @click="onOrderStatusClick"><i class="cv2-dot" aria-hidden="true"></i>{{ row.orderStatusLabelText }}</button>
            </td>
            <td class="cv2-td-center">{{ row.createTime }}</td>
            <td class="cv2-td-center">
              <span :title="`由${row.logUserName}记录此条账单(也是发单人)`">{{ row.logUserName }}</span>
            </td>
            <td class="cv2-td-center">
              <button
                type="button"
                class="cv2-st cv2-st--mark"
                :class="row.settled ? 'cv2-st--settled' : 'cv2-st--unsettled'"
                :data-v2-settle-tag="row.id"
                :title="row.settled ? '该笔订单已结账过' : '该笔订单还未进行结账'"
                @click="onSettleRow(row)"
              ><i class="cv2-dot" aria-hidden="true"></i>{{ row.settled ? '已结账' : '未结账' }}</button>
              <span
                v-if="row.settleRequestedAt !== null"
                class="cv2-st cv2-st--mark cv2-st--pending perf-requested"
                :title="`该笔绩效已申请提前结算（申请时间：${formatDateTime(row.settleRequestedAt) || row.settleRequestedAt}）`"
                :data-v2-requested="row.id"
              ><i class="cv2-dot" aria-hidden="true"></i>已申请</span>
            </td>
            <td class="cv2-td-center">{{ row.settingTime }}</td>
            <td class="cv2-td-center">
              <RouterLink
                v-if="canViewPayroll && row.payrollBatchId && (row.settlementMethod === 'online' || row.payrollOccupied)"
                class="perf-method"
                :data-v2-pay-method="row.id"
                :to="{ name: 'v2-online-pay-detail', query: { batch: row.payrollBatchId } }"
              >{{ row.settlementMethod === 'online' ? '在线结算' : '付款处理中' }}</RouterLink>
              <span v-else-if="row.settlementMethod === 'online' || row.payrollOccupied" class="perf-method" :data-v2-pay-method="row.id">{{ row.settlementMethod === 'online' ? '在线结算' : '付款处理中' }}</span>
              <span v-else-if="row.settled" class="perf-method" :data-v2-pay-method="row.id">人工结账</span>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="17" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
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
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="onPageChange"
        />
      </div>
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

    <!-- 批量搜用户弹窗（标题逐字；多行昵称，每行一条） -->
    <el-dialog
      v-model="batchOpen"
      :title="BATCH_USER_PROMPT_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-batch-user"
    >
      <el-input
        v-model="batchText"
        type="textarea"
        :rows="8"
        data-v2="batch-user-text"
      />
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="batch-cancel" @click="batchOpen = false">关闭</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="batch-submit" @click="onBatchSubmit">搜索</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除确认（自建 sm 弹窗，正文逐字；不可逆动作必过核对层） -->
    <el-dialog
      v-model="deleteOpen"
      title="删除记录"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-delete"
    >
      <p class="cv2-dlg-text" data-v2="delete-confirm">{{ DELETE_CONFIRM_TEXT }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="delete-cancel" @click="deleteOpen = false">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="delete-submit" :disabled="deleting" @click="onDeleteConfirm">删除</button>
        </div>
      </template>
    </el-dialog>

    <!-- 查看收款码（大图弹窗；标题 = X的微信收款码） -->
    <el-dialog
      :model-value="qrDlg !== null"
      :title="qrDlg === null ? '' : `${qrDlg.owner}的微信收款码`"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-qr"
      @update:model-value="qrDlg = null"
    >
      <div v-if="qrDlg !== null" class="perf-qr-big">
        <img :src="qrDlg.url" alt="微信收款码" data-v2="qr-img" />
      </div>
    </el-dialog>

    <OrderDetailDialog
      :model-value="detailDlg !== null"
      :title="detailDlg === null ? '' : `${detailDlg.viewNo}订单详情`"
      class="cv2-dialog cv2-dialog--detail"
      destroy-on-close
      data-v2="dlg-detail"
      @update:model-value="detailDlg = null"
    >
      <V2OrderDetailPane
        v-if="detailDlg !== null"
        :order-no="detailDlg.orderNo"
        :view-no="detailDlg.viewNo"
      />
    </OrderDetailDialog>

    <!-- 清算绩效弹窗（子组件 §2.1；成功后关窗刷新） -->
    <V2SettleDialog
      v-if="settleOpen !== null"
      :ids="settleOpen.ids"
      data-v2="dlg-settle-host"
      @close="settleOpen = null"
      @settled="onSettled"
    />

    <V2OnlinePayrollDialog
      v-if="payrollSelection"
      :selection="payrollSelection"
      @close="onPayrollClose"
    />
    <div v-if="savedPayrollId" class="cv2-toast" role="status" data-v2="payroll-resume">
      {{ PAYROLL_CLOSE_HINT }}
      <RouterLink :to="{ name: 'v2-online-pay-detail', query: { batch: savedPayrollId } }">查看付款单</RouterLink>
      <button type="button" class="cv2-btn" @click="savedPayrollId = null">关闭提示</button>
    </div>

    <!-- 查重弹窗（子组件 §2.2） -->
    <V2AlipayDupDialog v-if="alipayDupOpen" data-v2="dlg-alipay-host" @close="alipayDupOpen = false" />
  </div>
</template>



<style scoped>
/* 本页形状收在这里；色一律走 --tt2-* token（⛔ 裸色值）；图表无。 */

/* ── 筛选区 = 原版两行（replica/emp-emplist.html 两段 form-pane）。
 * 行 = flex-wrap 池左对齐；组 = [标签格(label-bg 底) + 值格…] 零间距 34px 带边框条，
 * 每格定宽 ⛔ 不吃剩余宽度；组间留缝、窄窗自动换行。
 * ⚠️ EP 内部件不携带本组件 scoped 属性 ⇒ 必须 :deep()；组框接管边界后控件自身去描边。 */
/* 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、行距 6、面板内边距 10/12），不压组高。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.uf-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
}

.uf-row--second {
  margin-top: 6px;
}

.uf-group {
  display: inline-flex;
  align-items: stretch;
  height: 32px;
  border: 1px solid var(--tt2-filter-line);
  border-radius: 6px;
  background: var(--tt2-panel);
  transition: border-color 0.15s;
}

/* ⛔ 不用 overflow:hidden 裁圆角——它会把下拉展开面板一起裁掉。首尾单元格自己带同族圆角。 */
.uf-group > :first-child {
  border-radius: 5px 0 0 5px;
}

.uf-group > :last-child {
  border-radius: 0 5px 5px 0;
}

.uf-group:focus-within {
  border-color: var(--tt2-primary);
}

.uf-label {
  display: inline-flex;
  align-items: center;
  padding: 0 0 0 10px;
  font-size: 13px;
  font-weight: 500;
  color: var(--tt2-dim);
  white-space: nowrap;
}

.uf-cell {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

/* 组内 EP 控件去自身描边/圆角（组框承担边界），高度钉 32px（34 − 上下边线）。 */
.uf-group :deep(.el-input__wrapper),
.uf-group :deep(.el-select__wrapper) {
  height: 32px;
  min-height: 32px;
  border-radius: 0;
  background: var(--tt2-panel);
  box-shadow: none;
}

.uf-group :deep(.el-select__wrapper.is-focused),
.uf-group :deep(.el-input__wrapper.is-focus) {
  box-shadow: none;
}
.uf-prefix :deep(.el-select__selected-item) { color: var(--tt2-dim); }

/* 宽度档（挂在 EP 组件根上）。 */
.uf-w106 { width: 106px; }
.uf-w130 { width: 130px; }
.uf-w150 { width: 150px; }
.uf-w160 { width: 160px; }
.uf-w165 { width: 165px; }
.uf-w170 { width: 170px; }

/* 打手多选下拉面板加高（业务方 2026-11-08）：EP 默认 wrap 限高 274px（el-select.css
   `.el-select-dropdown__wrap`）。popper 未 teleport ⇒ 落在组件根内；
   锚 data-v2-filter 只命中打手这一颗，kefu 等其他下拉不动。 */
[data-v2-filter='system-user'] :deep(.el-select-dropdown__wrap) {
  max-height: 420px;
}

/* 组内值格之间的分隔线（无标签格的组——日期节 / 代练下家+系统用户对——靠它分格）。 */
.uf-cell + .uf-cell,
.uf-cell + .uf-dash,
.uf-dash + .uf-cell {
  border-left: none;
}

.uf-datecell {
  width: 118px;
}

.uf-group .uf-date {
  box-sizing: border-box;
  width: 100%;
  height: 32px;
  padding: 0 4px;
  border: 0;
  border-radius: 0;
  background: var(--tt2-panel);
  font-size: 13px;
}

.uf-group .uf-date:focus {
  outline: none;
}

.uf-dash {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 「申请结算的」死筛选项：展示档与组条等高（EP 勾选框默认 margin-right:30px 清零）。 */
.uf-check {
  height: 34px;
  margin-right: 0;
}

/* 搜索钮：宽 80px 照旧页；高度对齐 34px 网格行。 */
.cv2-btn-search {
  width: 80px;
  height: 34px;
}

/* ⭐（2026-11-07 R3）「挂负账」红签：小号红边 tag（红 = --tt2-money-red token，⛔ 裸色值）。 */
.perf-negdebt-tag {
  display: inline-block;
  margin-left: 4px;
  padding: 0 4px;
  border: 1px solid var(--tt2-money-red);
  font-size: 11px;
  line-height: 16px;
  color: var(--tt2-money-red);
}

/* 工具条按钮跟全站横排同一高度，这里只负责垂直居中。 */
.perf-tbtn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* 统计句 = 原版橙（证据件字面色 #ff6a00 的特例口径，管理订单页同款覆写并留痕）。 */
.cv2-total-line {
  color: var(--tt2-summary-orange);
}

/* 主表横滚；数字列等宽。 */
.perf-table-panel {
  overflow-x: auto;
}
.perf-table-panel .cv2-table { min-width: 1800px; }
.perf-th-order { width: 220px; }
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* 订单状态列：签加了墨点+gap 后定宽兜底（table-layout:fixed，余列均分剩余宽）。 */
.perf-th-status {
  width: 96px;
}

/* 系统用户列蓝链：常态去浏览器默认下划线（可点性由蓝链+hover 表达）；hover 态保留。 */
.perf-userlink {
  text-decoration: none;
}
.perf-userlink:hover {
  text-decoration: underline;
}

/* 预计/生效绩效格：常态 = 红色 mono 数字（非链接、无下划线）；点击编辑。 */
.perf-cell {
  position: relative;
}
.perf-amount {
  padding: 0;
  border: 0;
  background: none;
  font-size: 13px;
  cursor: pointer;
}
.perf-amount--loss {
  font-weight: 700;
  font-size: 14px;
}
/* 编辑态：原数字 visibility:hidden 隐形占位防塌列，input 绝对定位充满整格（邻列零位移）。 */
.perf-amount--ghost {
  visibility: hidden;
}
.perf-edit {
  position: absolute;
  inset: 0;
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  padding: 0 8px;
  border: 1px solid var(--tt2-primary);
  border-radius: 4px;
  background: var(--tt2-panel);
  color: var(--tt2-ink);
  font-family: var(--tt2-mono);
  font-size: 13px;
  text-align: center;
}
.perf-edit:focus {
  outline: none;
}

/* 账单属性 = 纯文字（打手的/客服的 = doing 绿；其余墨色；⛔ 签皮）。 */
.perf-emp-doing {
  color: var(--tt2-st-doing);
}
.perf-emp-ink {
  color: var(--tt2-ink);
}

/* 「已申请」跟在结账色点后面，字号收一档。 */
.perf-requested {
  margin-left: 6px;
  font-size: 12px;
}

/* 结账方式：浅蓝字签，未结账不画空框。 */
.perf-method {
  display: inline-block;
  padding: 2px 8px;
  border: 1px solid var(--tt2-st-done-line);
  border-radius: 4px;
  background: var(--tt2-st-done-bg);
  color: var(--tt2-st-done);
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
}

/* 收款徽标压小（V2OrderManage 同款形状）。 */
.cv2-st--pushed {
  padding: 1px 6px;
  font-size: 11px;
  margin-right: 6px;
  vertical-align: 1px;
}

/* 订单状态签按钮化（保留签皮、去掉链接语义）。 */
button.cv2-st:not(.cv2-st--mark) {
  border: 0;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}

/* 收款码大图。 */
.perf-qr-big {
  text-align: center;
}
.perf-qr-big img {
  max-width: 260px;
  max-height: 260px;
}
</style>
