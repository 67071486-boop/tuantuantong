<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
/**
 * 「我的订单收入」v2（`/v2/income`；「我的相关」组 4 页 v2 化，D-0353）。
 *
 * ## 旧页是什么、这里照搬什么
 *
 * 旧页 = island 复刻页 `layui-island/pages/emp-myemp.ts`（路由 `agents-my-income`，对标
 * `/Emp/myemp`）：单表列表页，数据 = `fetchAllMyOrders()` 一次取完（**客户端筛选 + 客户端分页**，
 * 与「指派给我的订单」同一数据源、同一后端闸门）。本页按同一套功能重建为炭火 Vue 页：
 *
 * | 旧页功能 | 本页落点 |
 * |---|---|
 * | 5 个客户端筛选（选择游戏 / 结算状态 / 订单状态多选 / 搜索1=单号 / 搜索2=内容） | 同字段同判据（`applyIncomeFilters`，5 条 AND；判据 `!== ''` / `length > 0`，不用真值）；**全部即时生效**（旧页只有游戏下拉即时、其余要点「搜索」——触发粒度是 UX 细节，同 V2MyOrders 先例）。⚠️ D-0354：订单状态多选从筛选面板上移为页头下的**状态卡片**（与 V2MyOrders 同款：状态名 + 单量、点击切换；筛选面板里的状态签行移除） |
 * | 游戏下拉按 **option 文案**对 `game_name`，「全部游戏」按文案判哨兵（证据件里同文案两档，D-0025 §12 登记） | 同一条规则：第一档 + 与首档同文案的档都当「全部」 |
 * | 结算状态下拉 `N/Y` → 后端 `未结账`/`已结账` | 同一映射（下拉直接给中文值） |
 * | 订单状态多选：option 中文文案反查 `ORDER_STATUS_LABELS` 得枚举 | 卡片直接持枚举（同 V2MyOrders 形态；D-0354 起卡片取代筛选签） |
 * | 表格 19 列（6 列后端不回值的复刻空列 + 2 列 `hide:true`） | 只搬**真有值**的列（用户名称 / 预计收入 / 实际收入 / 收入类型 / 订单号〔⭐ 业务方「不需要展示系统内置单号 展示订单号即可 并且可点击」：单列订单号=来源单号优先、空回退系统内置单号，点开详情+一键复制〕/ 游戏 / 代练项目 / 订单状态 / 记录时间 / 结算状态 / 是否已申请结算）；空列与隐藏列**不搬**（DRAFT §8 登记） |
 * | 单号蓝链 `.orderdetails` → 原地开层装 `order-orderview`（60%×92%、20 位形状闸 + 固定文案） | 同 island 页 iframe（`el-dialog` 宽 60%；`isOrderNoShape` 闸照搬） |
 * | 状态徽标按本页对标色表分色 | 炭火状态签 token 族（D-0351 定稿；色值不随旧页——v2 不做 1:1 复刻） |
 * | 分页 `page:true, limit:20, limits:[10,20,30,40,50,100]`（本页对标自己的值） | 同值（`el-pagination` 客户端分页） |
 * | `#total` 统计句（7 个数字恒 0 的服务端聚合骨架 + 「默认显示最近6个月」标称值） | **不搬**：只报真数据（筛出行数）；后端没有 `res.total.*` 出口，凑数 = 编数（同 V2MyOrders 先例，DRAFT §8） |
 * | 日期：`GET /my-orders/` 收 `date_type` 四档 + `date_from`/`date_to`。档下拉 + 起止 `input type="date"`，改了即重取；解析不出就不发参 |
 * | 工具栏「刷新列表」（真） | 同（重新取数，筛选不清空；⭐ 合并轮定稿：刷新住筛选行动作区〔D-0354 追记 3〕，孤行工具栏只剩 申请结算 / 导出记录 两颗真功能钮） |
 * | 「申请结算」= 勾选行批量 `POST /my-orders/settle-requests/`（无勾选提示「请先勾选要申请的行」；成功 ≥1 条才清空选中并重取；选中集跨页保留）。「导出记录」= 当前筛选 `GET /my-orders/export`。「同步结算状态到我的系统」不搬：没有对应端点 |
 * | 收入类型分色（`打单工资` 红 / 其余绿） | 不做分色（纯文本）：旧页那对色值不在炭火 token 表里，语义上也不是状态——如实显示后端取值 |
 * | 取数失败：表清空 + 后端 `msg` 原样弹 6000ms | 同（错误 toast 6000ms） |
 *
 * ## 视觉补充一：状态卡片与筛选栏（D-0354）
 *
 * - 订单状态筛选上移为**状态卡片**（与 V2MyOrders 完全同款）：页头下、筛选面板上一排 8 张
 *   （状态名 + 该状态单量，客户端统计自 `fetchAllMyOrders()` 全量行、不受筛选影响）；
 *   点击 = 切换该状态筛选（单选语义：点已选中的 = 取消回全部），选中卡 = 样式 C（浅底不动 +
 *   墨点色描边加粗 + 右上角墨点色三角 ✓，与 V2MyOrders 同款）；筛选面板里的状态签行移除。
 * - 筛选栏保持**横排一行、标签左置**（业务方重申不回退）。
 * - 筛选控件 **EP 化**（业务方看本地环境追记）：`el-select`（游戏 filterable）/ `el-input`；
 *   v-model 接原 filters 字段（'' 哨兵不变）、`data-v2-*` 锚点保留；面板
 *   `:teleported="false"` 留在 `.ttv2` 内吃青绿皮。
 *
 * ## 视觉补充二：列表网格 + 列宽拖拽
 *
 * - 表格改**全网格线**（th/td 四边 1px `--tt2-line`）+ 列宽拖拽（共享 `src/v2/tableResize.ts`；
 *   手柄在每列 th 右缘 6px、双击复原，存 localStorage `tt.v2.table.income`；44px 截断不变）。
 * - 结算状态跟订单状态同一套色点：字和点同色，不套胶囊。
 *
 * ## 数据：零新增端点
 *
 * `api/myOrders.ts` 的 `fetchAllMyOrders()`（后端闸门 = 节点 `my_orders.send_to_my_system`，
 * 403/401 的 msg 原样显示——与 island 页同一口径）。
 *
 * ⛔ **不 `import` island 页模块**（同 `V2Dashboard` / `V2MyOrders` 的纪律）：那会把 layui
 * 装载链拉进 v2 的模块图。下面的纯函数 / 常量（筛选判据、状态签映射）是**带出处注记的本地重写**，
 * 与 island 页逐字同语义；改口径时要两处同改（两处都有测试钉住）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { isOrderNoShape } from '@/api/orders'
import {
  applyForSettlement,
  downloadMyOrderExport,
  fetchAllMyOrders,
  type FetchAllMyOrdersParams,
  type MyOrderItem,
} from '@/api/myOrders'
import { ORDER_STATUS_VALUES, V2_ORDER_STATUS_LABELS, v2OrderStatusLabel, type OrderStatus } from '@/config/orderStatus'
import { ElDatePicker } from 'element-plus'
import { useColumnWidths } from '@/v2/tableResize'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'

/* ── 文案（出处见 island 页同名常量；本地重写不引模块）──────────────────── */

/** 层的单号形状闸固定文案（island 页同句；20 位闸与后端路由约束同源）。 */
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'
/** 后端 `SettleStatus` 的两个值（island 页 `SETTLE_STATUS_*` 的镜像）。 */
const SETTLE_STATUS_UNSETTLED = '未结账'
const SETTLE_STATUS_SETTLED = '已结账'

/** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（炭火 toast，页面内自绘；info 1500ms / error 6000ms——错误要看清）──────── */

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

/* ── 取数 ─────────────────────────────────────────────────────────────── */

const allRows = ref<MyOrderItem[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 日期三参数（⭐ D-0365）：**没有口径 ⇒ 日期参数整个不发**（与后端「缺档不筛、不猜
 * 默认列」的约定成对，也和旧页 `dateFetchParams` 同一条成对约定）；`input type="date"`
 * 的值要么空、要么 `YYYY-MM-DD`，这里仍做一次形状闸——解析不出就不发参（从简登记）。 */
function dateFetchParams(): FetchAllMyOrdersParams {
  if (filters.dateType === '') return {}
  const params: FetchAllMyOrdersParams = { date_type: filters.dateType }
  const from = filters.dateFrom.trim()
  const to = filters.dateTo.trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(from)) params.date_from = from
  if (/^\d{4}-\d{2}-\d{2}$/.test(to)) params.date_to = to
  return params
}

async function reload(): Promise<void> {
  loading.value = true
  try {
    allRows.value = await fetchAllMyOrders(dateFetchParams())
    loadError.value = null
  } catch (e) {
    // 失败也要把表清空并**如实报错**（island 页同口径：403/401 的 msg 原样显示）。
    allRows.value = []
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void reload()
})

/* ── 筛选（本地筛已取到的全量行；判据 = island 页 `applyFilters` 的逐条重写）──────── */

/** 页面筛选条件（与 island 页 `MyEmpFilters` 同形；游戏下拉直接持文案）。 */
type IncomeFilters = {
  game: string
  settleStatus: string
  orderStatuses: OrderStatus[]
  key1: string
  key2: string
  /** ⭐ D-0365：日期口径（`''` = 不按日期筛；四档值 = 后端 `date_type` 契约）。 */
  dateType: '' | 'claimed' | 'submitted' | 'created' | 'settled'
  /** `input type="date"` 的值（`''` 或 `YYYY-MM-DD`）。 */
  dateFrom: string
  dateTo: string
}

/** 默认起始日 = 今天往前推 6 个月（YYYY-MM-DD；本地时区，与日期输入框同源）。 */
const SIX_MONTHS_AGO = (() => {
  const d = new Date()
  d.setMonth(d.getMonth() - 6)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})()

const filters = reactive<IncomeFilters>({
  game: '',
  settleStatus: '',
  orderStatuses: [],
  key1: '',
  key2: '',
  /* ⭐ D-0371 第六轮：默认窗口 = 最近 6 个月（业务方「默认显示最近6个月数据」），
   * 口径 = 录单时间 + 起始日 = 今天往前推 6 个月、结束不封顶。 */
  dateType: 'created',
  dateFrom: SIX_MONTHS_AGO,
  dateTo: '',
})

/** 日期口径下拉的四档（文案出处 = 旧页证据件 i18n 字典 `dateType1..4` 的中文）。 */
const DATE_TIERS = [
  { value: 'claimed', label: '接手时间' },
  { value: 'submitted', label: '交单时间' },
  { value: 'created', label: '录单时间' },
  { value: 'settled', label: '结算时间' },
] as const

/** 日期三控件（服务端筛选）任一变化 ⇒ 重取 + 回第 1 页（客户端刷不到服务端筛选）。 */
function onDateChange(): void {
  page.value = 1
  void reload()
}

/** 游戏下拉的选项 = 数据里出现过的游戏名（去重排序）。 */
const gameNames = computed(() =>
  Array.from(new Set(allRows.value.map((r) => r.game_name).filter((n) => n !== ''))).sort((a, b) =>
    a.localeCompare(b, 'zh-Hans-CN'),
  ),
)

/** 客户端筛选（island 页 `applyFilters` 的逐条重写；5 个条件 AND）。
 * 判据是 `!== ''` / `length > 0`，**不是真值**（金额 0 与空串语义不同，E-0021 同族）。
 * 单号 / 内容子串**大小写不敏感**（island 页同一行为）。 */
function applyIncomeFilters(rows: MyOrderItem[], f: IncomeFilters): MyOrderItem[] {
  const k1 = f.key1.trim().toLowerCase()
  const k2 = f.key2.trim().toLowerCase()
  return rows.filter((row) => {
    if (f.game !== '' && row.game_name !== f.game) return false
    if (f.settleStatus !== '' && row.settle_status !== f.settleStatus) return false
    if (f.orderStatuses.length > 0 && !f.orderStatuses.some((s) => s === row.status)) return false
    if (k1 !== '' && !row.order_no.toLowerCase().includes(k1)) return false
    if (k2 !== '' && !row.project_text.toLowerCase().includes(k2)) return false
    return true
  })
}

const filteredRows = computed(() => applyIncomeFilters(allRows.value, filters))

/** 重置（清全部筛选；拨回第 1 页）。日期是服务端筛选 ⇒ 重置后要**重取**（撤销日期窗口）。 */
function resetFilters(): void {
  filters.game = ''
  filters.settleStatus = ''
  filters.orderStatuses = []
  filters.key1 = ''
  filters.key2 = ''
  filters.dateType = 'created'
  filters.dateFrom = SIX_MONTHS_AGO
  filters.dateTo = ''
  page.value = 1
  void reload()
}

/* ── 勾选与批量申请结算（⭐ D-0365）────────────────────────── */

/** 勾选的绩效行主键集。**跨页保留**（登记口径）：翻页 / 客户端筛选**不清空**——「勾几行 → 翻页再勾几行 → 一次申请」是批量动作的自然用法；代价是重取后可能留下指向已不可见行的 id，申请时由后端逐条判（归属不合 ⇒ 1035 如实呈现）。**申请成功（≥1 条）后清空并重取**——让「已申请」标记立即反映落库结果。
 * 勾选的绩效行主键集。**跨页保留**（登记口径）：翻页 / 客户端筛选**不清空**——「勾几行 → 翻页再勾几行 → 一次申请」是批量动作的自然用法；代价是重取后可能留下指向已不可见行的 id，申请时由后端逐条判（归属不合 ⇒ 1035 如实呈现）。**申请成功（≥1 条）后清空并重取**——让「已申请」标记立即反映落库结果。 */
const selectedIds = ref<number[]>([])
const applying = ref(false)

function toggleRow(row: MyOrderItem): void {
  const i = selectedIds.value.indexOf(row.id)
  if (i === -1) selectedIds.value.push(row.id)
  else selectedIds.value.splice(i, 1)
}

/** 当前页是否全选（表头勾选框的选中态）。 */
const allOnPageSelected = computed(
  () =>
    pagedRows.value.length > 0 &&
    pagedRows.value.every((row) => selectedIds.value.includes(row.id)),
)

/** 表头全选：已全选 ⇒ 取消本页全部；否则把本页缺的补上（其它页的保留）。 */
function toggleAllOnPage(): void {
  if (allOnPageSelected.value) {
    const pageIds = new Set(pagedRows.value.map((row) => row.id))
    selectedIds.value = selectedIds.value.filter((id) => !pageIds.has(id))
    return
  }
  for (const row of pagedRows.value) {
    if (!selectedIds.value.includes(row.id)) selectedIds.value.push(row.id)
  }
}

/** 批量申请提前结算。语义与旧页 `applySettlement` 同一套（同一后端端点）：逐条结果汇总（成功 N 条 + 失败 `msg` 去重合并），**后端是唯一判据**（已申请 /额度用完等逐条拒绝、如实呈现）；成功 ≥1 条才清空选中并重取。⭐ 空选提示是 v2 自拟（计划 B5 指定这句；旧页用对标的 `toolbar` 句——两页各自登记）。
 * 批量申请提前结算。语义与旧页 `applySettlement` 同一套（同一后端端点）：逐条结果汇总（成功 N 条 + 失败 `msg` 去重合并），**后端是唯一判据**（已申请 /额度用完等逐条拒绝、如实呈现）；成功 ≥1 条才清空选中并重取。⭐ 空选提示是 v2 自拟（计划 B5 指定这句；旧页用对标的 `toolbar` 句——两页各自登记）。 */
async function applySettlement(): Promise<void> {
  if (selectedIds.value.length === 0) {
    showToast('请先勾选要申请的行', 'error')
    return
  }
  applying.value = true
  try {
    const result = await applyForSettlement(selectedIds.value)
    const okCount = result.items.filter((item) => item.ok).length
    const failureMsgs = [
      ...new Set(result.items.filter((item) => !item.ok).map((item) => item.msg)),
    ]
    const parts: string[] = []
    if (okCount > 0) parts.push(`已提交 ${okCount} 条提前结算申请`)
    parts.push(...failureMsgs)
    showToast(parts.join('；'), failureMsgs.length > 0 ? 'error' : 'info')
    if (okCount > 0) {
      selectedIds.value = []
      await reload()
    }
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    applying.value = false
  }
}

/* ── 导出记录（⭐ D-0365）：当前筛选打包 → 服务端 xlsx → Blob 下载 ─────────── */

const exporting = ref(false)

/** 导出查询串：页面 5 个客户端筛选的**服务端镜像** + 日期三参数（与旧页 `exportQuery` 同判据）。 */
function exportQuery(): Record<string, string> {
  const query: Record<string, string> = {}
  // 判据是「空」而不是「真值」（E-0021 同族）；键名 = 后端 `MyOrdersExportSerializer`。
  if (filters.game !== '') query.game = filters.game
  if (filters.settleStatus !== '') query.settle_status = filters.settleStatus
  if (filters.orderStatuses.length > 0) query.order_status = filters.orderStatuses.join(',')
  const k1 = filters.key1.trim()
  const k2 = filters.key2.trim()
  if (k1 !== '') query.key1 = k1
  if (k2 !== '') query.key2 = k2
  const params = dateFetchParams()
  if (params.date_type !== undefined) query.date_type = params.date_type
  if (params.date_from !== undefined) query.date_from = params.date_from
  if (params.date_to !== undefined) query.date_to = params.date_to
  return query
}

async function exportRecords(): Promise<void> {
  exporting.value = true
  try {
    const { blob, filename } = await downloadMyOrderExport(exportQuery())
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

/* ── 分页（客户端；limit 20 / limits [10,20,30,40,50,100] = 本页对标自己的值）──────── */

const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]
const page = ref(1)
const pageSize = ref(20)

const pageCount = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize.value)))
// 行数变少（筛选/刷新）后当前页可能越界 ⇒ 收回来（el-pagination 不会自己夹）。
watch(pageCount, (c) => {
  if (page.value > c) page.value = c
})

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredRows.value.slice(start, start + pageSize.value)
})

/** 每页条数切换（切完拨回第 1 页）。 */
function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    page.value = 1
  }
}

/* ── 单元格展示（与 island 页 templet 同口径的本地重写）──────────────────────────── */

/* ── 账单描述句（⭐ D-0371 第六轮）：数字 = 当前筛选行的真值聚合，⛔ 不编数。
 * 总计口径排除已退单（业务方样句「总计(不含已退单)」）；已结账金额 = 其实收合计、
 * 未结账金额 = 其预计合计（与表格两列展示口径一致）。
 * 「已结账/还未结账」两段可点 = 切结算状态筛选（管理订单统计句同款，见 onBillSettleClick）。 */
const billStat = computed(() => {
  const money = (v: string): number => {
    const n = Number.parseFloat(v)
    return Number.isFinite(n) ? n : 0
  }
  const active = filteredRows.value.filter((r) => r.status !== 'REFUNDED')
  const settled = filteredRows.value.filter((r) => r.settle_status === SETTLE_STATUS_SETTLED)
  const unsettled = filteredRows.value.filter((r) => r.settle_status === SETTLE_STATUS_UNSETTLED)
  const sum = (list: MyOrderItem[], pick: (r: MyOrderItem) => string): string =>
    fmtYuan(list.reduce((acc, r) => acc + money(pick(r)), 0))
  return {
    count: filteredRows.value.length,
    expected: sum(active, (r) => r.temp_amount),
    actual: sum(active, (r) => r.bonus_amount),
    settledCount: settled.length,
    settledAmount: sum(settled, (r) => r.bonus_amount),
    unsettledCount: unsettled.length,
    unsettledAmount: sum(unsettled, (r) => r.temp_amount),
  }
})
function fmtYuan(n: number): string {
  return n.toFixed(2)
}

/** 账单句结算段点击三态（管理订单统计句同款）：点 = 只筛该结算状态；再点同一个 = 清回全部；
 * 点另一个 = 切到那个。filters.settleStatus 同时是「结算状态」下拉的 v-model ⇒ 下拉自动同步。 */
function onBillSettleClick(code: string): void {
  filters.settleStatus = filters.settleStatus === code ? '' : code
  page.value = 1
}

/** 记录时间：`YYYY-MM-DD HH:mm`（island 页 `formatTime` 同式）。 */
function formatTime(value: string): string {
  return value.replace('T', ' ').slice(0, 16)
}

/** 状态 → 炭火签的 token 类（与 V2MyOrders 同一张映射；⭐ D-0354 八状态各占一个色相，
 * 未知取值落中性灰，文案原样显示）。 */
function statusText(status: string): string {
  return v2OrderStatusLabel(status)
}

/** 结算状态色点：已结账走绿，未结账走灰蓝；未知值落中性灰。 */
function settleClass(settleStatus: string): string {
  if (settleStatus === SETTLE_STATUS_SETTLED) return 'cv2-st--settled'
  if (settleStatus === SETTLE_STATUS_UNSETTLED) return 'cv2-st--unsettled'
  return 'cv2-st--unassigned'
}

/* ── 列宽拖拽（⭐ 对标式网格 + 手动拉宽；共享 src/v2/tableResize.ts）────
 * 列键与模板 th 一一对应；「代练项目」不给默认宽（弹性列）；存 localStorage
 * `tt.v2.table.income`。 */
const INCOME_COLUMNS = ['user', 'temp', 'bonus', 'type', 'order', 'game', 'project', 'status', 'time', 'settle', 'applied'] as const

const { colStyle, startResize, resetColumn } = useColumnWidths('income', {
  /* ⭐ user/temp/bonus/type/status/time/settle/applied = 内容短且基本固定 ⇒ 窄固定宽 + 居中 +
   * 无拖拽手柄（业务方「用户昵称过宽」→ user 转 110 固定）。 */
  user: 110,
  temp: 96,
  bonus: 96,
  type: 104,
  status: 96,
  /* ⭐ D-0371 第六轮：time 150→168（16 字形 13px mono 实测 150 仍截断）；game 从弹性改
   * 150 定宽起步（业务方「游戏名称太宽了」，仍可拖拽）。 */
  game: 150,
  time: 168,
  settle: 100,
  applied: 120,
  /* ⭐ order 从弹性改定宽（业务方「订单号列过宽」——19 位 mono + 复制钮 ~198 即够，
   * 超出省略；仍可拖拽）。 */
  order: 206,
})

const detailDlg = ref<{ orderNo: string } | null>(null)

/** 一键复制单号（剪贴板 + toast 回执；不可用时静默，不谎报成功）。 */
async function copyOrderNo(no: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(no)
    showToast('已复制单号')
  } catch {
    /* 剪贴板不可用 */
  }
}

function openDetail(d: MyOrderItem): void {
  const orderNo = d.order_no
  // ⛔ 形状闸同 island 页：`/^\d{20}$/`（后端详情路由的约束）。不匹配 ⇒ 不开层 + 固定文案。
  if (!isOrderNoShape(orderNo)) {
    showToast(BAD_ORDER_NO_TEXT, 'error')
    return
  }
  detailDlg.value = { orderNo }
}
</script>

<template>
  <div class="cv2-page" data-v2-page="income">
    <!-- 筛选行：顶部状态卡片已撤。表里的状态字还在。其余字段同旧页，全部即时生效；
         ⚠️ 业务方重申：本栏保持横排一行、小字标签在控件左边（cv2-field 左置布局不回退） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="cv2-filter-row">
        <!-- ⭐ D-0354：筛选控件 EP 化（原生 select/input → EP）；v-model 接原 filters 字段、
             data-v2-* 锚点原样保留；下拉面板 :teleported="false" 留在 .ttv2 内吃青绿皮 -->
        <label class="cv2-field">
          <span class="cv2-field-label">选择游戏</span>
          <el-select v-model="filters.game" filterable placeholder="全部游戏" data-v2-filter="game" @change="page = 1">
            <el-option value="" label="全部游戏" />
            <el-option v-for="n in gameNames" :key="n" :value="n" :label="n" />
          </el-select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">订单状态</span>
          <el-select v-model="filters.orderStatuses" multiple collapse-tags placeholder="全部" data-v2-filter="order-status" :teleported="false" @change="page = 1">
            <el-option v-for="s in ORDER_STATUS_VALUES" :key="s" :value="s" :label="V2_ORDER_STATUS_LABELS[s]" />
          </el-select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">结算状态</span>
          <el-select v-model="filters.settleStatus" placeholder="全部" data-v2-filter="settle" @change="page = 1">
            <el-option value="" label="全部" />
            <el-option :value="SETTLE_STATUS_UNSETTLED" :label="SETTLE_STATUS_UNSETTLED" />
            <el-option :value="SETTLE_STATUS_SETTLED" :label="SETTLE_STATUS_SETTLED" />
          </el-select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">搜索1</span>
          <el-input v-model="filters.key1" placeholder="系统内置单号" data-v2-filter="key1" />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">搜索2</span>
          <el-input v-model="filters.key2" placeholder="代练项目" data-v2-filter="key2" />
        </label>
        <div class="income-date" data-v2-filter-group="date">
          <el-select v-model="filters.dateType" data-v2-filter="date-type" :teleported="false" @change="onDateChange">
            <el-option value="" label="不按日期筛" />
            <el-option v-for="t in DATE_TIERS" :key="t.value" :value="t.value" :label="t.label" />
          </el-select>
          <el-date-picker :model-value="filters.dateFrom" data-v2-filter="date-from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" :teleported="false" @update:model-value="(v: string | null) => { filters.dateFrom = v ?? ''; onDateChange() }" />
          <span>–</span>
          <el-date-picker :model-value="filters.dateTo" data-v2-filter="date-to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" :teleported="false" @update:model-value="(v: string | null) => { filters.dateTo = v ?? ''; onDateChange() }" />
        </div>
        <!-- ⭐ D-0354：「刷新列表」住下方筛选动作区；统计句随行。 -->
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="refresh" :disabled="loading" @click="reload">
            刷新列表
          </button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
          <button
            type="button"
            class="cv2-btn cv2-btn--tool cv2-btn--commit"
            data-v2="apply-settlement"
            :disabled="applying"
            @click="applySettlement"
          >
            申请结算
          </button>
          <button
            type="button"
            class="cv2-btn cv2-btn--tool cv2-btn--quiet"
            data-v2="export"
            :disabled="exporting"
            @click="exportRecords"
          >
            导出记录
          </button>
        </div>
        <!-- 「已结账/还未结账」两段可点（点 = 只筛该结算状态 / 再点 = 清回全部），句子其余文字逐字不变 -->
        <p class="cv2-total-line" data-v2="total">
          您接手过的订单，都会在此产生账单，本次筛选条件下共计{{ billStat.count }}条账单，总计(不含已退单)：预计收入{{ billStat.expected }}元，实际收入{{ billStat.actual }}元，<button
            type="button"
            class="cv2-bill-st"
            :class="{ 'cv2-bill-st--on': filters.settleStatus === SETTLE_STATUS_SETTLED }"
            data-v2-bill-settle="settled"
            @click="onBillSettleClick(SETTLE_STATUS_SETTLED)"
          >已结账{{ billStat.settledCount }}笔（{{ billStat.settledAmount }}元）</button>，<button
            type="button"
            class="cv2-bill-st"
            :class="{ 'cv2-bill-st--on': filters.settleStatus === SETTLE_STATUS_UNSETTLED }"
            data-v2-bill-settle="unsettled"
            @click="onBillSettleClick(SETTLE_STATUS_UNSETTLED)"
          >还未结账{{ billStat.unsettledCount }}笔（预计有{{ billStat.unsettledAmount }}元）</button>，默认显示最近6个月数据，若需扩大时间范围，可手动选择起始日期。
        </p>
      </div>
    </section>

    <!-- 表格：语义 table（DRAFT §7）；列 = 旧页真有值的那些 + ⭐ D-0365 勾选列。
         ⭐ <colgroup> 承接列宽拖拽 + 每 th 右缘 6px 手柄（双击复原）。
         ⭐ 合并轮定稿：孤行工具栏不渲染——刷新住筛选动作区；申请结算 / 导出记录
         （D-0365 真功能）同样住筛选动作区，紧挨刷新右侧。 -->
    <section class="cv2-panel cv2-table-panel" aria-label="收入账单列表">
      <table class="cv2-table">
        <colgroup>
          <col style="width: 36px" />
          <col v-for="key in INCOME_COLUMNS" :key="key" :style="colStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th data-v2="check-col">
              <input
                type="checkbox"
                data-v2="check-all"
                aria-label="全选本页"
                :checked="allOnPageSelected"
                @change="toggleAllOnPage"
              />
            </th>

            <th class="cv2-th-center">
              用户名称
            </th>
            <th class="cv2-th-num cv2-th-center">
              预计收入
            </th>
            <th class="cv2-th-num cv2-th-center">
              实际收入
            </th>
            <th class="cv2-th-center">
              收入类型
            </th>
            <th>
              订单号
              <span class="cv2-th-resizer" data-v2-col="order" @mousedown="startResize($event, 'order')" @dblclick="resetColumn('order')" />
            </th>
            <th>
              游戏名称
              <span class="cv2-th-resizer" data-v2-col="game" @mousedown="startResize($event, 'game')" @dblclick="resetColumn('game')" />
            </th>
            <th>
              代练项目
              <span class="cv2-th-resizer" data-v2-col="project" @mousedown="startResize($event, 'project')" @dblclick="resetColumn('project')" />
            </th>
            <th class="cv2-th-center">
              订单状态
            </th>
            <th class="cv2-th-center">
              记录时间
            </th>
            <th class="cv2-th-center">
              结算状态
            </th>
            <th class="cv2-th-center">
              是否已申请结算
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in pagedRows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td>
              <input
                type="checkbox"
                :aria-label="`勾选 ${row.order_no}`"
                :data-v2-check="row.id"
                :checked="selectedIds.includes(row.id)"
                @change="toggleRow(row)"
              />
            </td>
            <td class="cv2-td-center">{{ row.username ?? '' }}</td>
            <td class="cv2-td-num cv2-td-center"><span class="cv2-money" :data-v2-amount="row.id">{{ row.temp_amount }}</span></td>
            <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ row.bonus_amount }}</span></td>
            <td class="cv2-td-center"><span class="cv2-money">{{ row.emp_type }}</span></td>
            <td class="cv2-td-order">
              <!-- ⭐ 业务方「不需要展示系统内置单号 展示字段订单号即可 并且可点击」：
                   单列订单号 = 来源单号优先、空回退系统内置单号；点击开详情（详情按 order_no 取）。 -->
              <a
                href="#"
                class="cv2-order-link cv2-ono"
                :data-v2-detail="row.order_no"
                title="点击查看订单详情"
                @click.prevent="openDetail(row)"
              >{{ row.source_order_no || row.order_no }}</a>
              <button
                type="button"
                class="cv2-copybtn"
                :data-v2-copy="row.order_no"
                :aria-label="`复制单号 ${row.source_order_no || row.order_no}`"
                title="复制单号"
                @click.stop="copyOrderNo(row.source_order_no || row.order_no)"
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="5.5" y="5.5" width="8" height="9" rx="1.5" />
                  <path d="M10.5 3.5h-6A1.5 1.5 0 0 0 3 5v7" />
                </svg>
              </button>
            </td>
            <td>{{ row.game_name }}</td>
            <td class="cv2-td-project">{{ row.project_text }}</td>
            <td class="cv2-td-center">
              <span class="cv2-st cv2-order-status cv2-st--mark" :class="orderStatusClass(row.status)" :data-v2-status="row.order_no" :data-status="row.status">
                <i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}
              </span>
            </td>
            <td class="cv2-td-time cv2-td-center">{{ formatTime(row.created_at) }}</td>
            <td class="cv2-td-center">
              <span class="cv2-st cv2-st--mark" :class="settleClass(row.settle_status)" :data-v2-settle="row.id">
                <i class="cv2-dot" aria-hidden="true"></i>{{ row.settle_status }}
              </span>
              <!-- ⭐ D-0365：该行自己的提前结算申请存在（`cancelled` 后端回 null ⇒ 如实消失） -->
              <!-- ⭐ 整列「是否已申请结算」已承接该事实，格内徽标（D-0365）
                   与它同义重复 ⇒ 删除（列值 = settle_requested_at 非空 ⇔ 已申请）。 -->
            </td>
            <td class="cv2-td-center" :data-v2-applied-col="row.id">{{ row.settle_requested_at !== null ? '已申请' : '未申请' }}
            </td>
          </tr>
          <tr v-if="pagedRows.length === 0">
            <td colspan="12" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ filteredRows.length }} 条</span>
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
          :total="filteredRows.length"
          layout="prev, pager, next"
          background
        />
      </div>
    </section>

    <!-- 炭火 toast -->
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
        allow-own-order-detail
        :order-no="detailDlg.orderNo"
        :view-no="detailDlg.orderNo"
      />
    </OrderDetailDialog>
  </div>
</template>

<style scoped>
.cv2-st--mark:not(.cv2-order-status) .cv2-dot { box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 14%, transparent); }
.income-date { display: inline-flex; align-items: center; height: 32px; border: 1px solid var(--tt2-line); border-radius: 6px; background: var(--tt2-panel); }
.income-date:focus-within { border-color: var(--tt2-primary); }
.income-date > .el-select { width: 106px; }
.income-date :deep(.el-date-editor.el-input) { width: 118px; }
.income-date :deep(.el-input__inner) { font-size: 13px; }
.income-date > span { padding: 0 6px; color: var(--tt2-dim); }
.income-date :deep(.el-input__wrapper), .income-date :deep(.el-select__wrapper) { box-shadow: none; background: transparent; }
.income-date :deep(.el-select__wrapper) { background: var(--tt2-filter-label-bg); }
.income-date :deep(.el-select__selected-item:not(.is-transparent)) {
  color: var(--tt2-ink);
  font-weight: 400;
}

/* 账单句结算段可点（管理订单统计句同款）：句底色不变（整句仍红、色继承），可点态 =
 * pointer + 悬停下划线；选中段常驻下划线，标记当前筛选落在哪个结算状态。 */
.cv2-bill-st {
  padding: 0;
  border: none;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.cv2-bill-st:hover {
  text-decoration: underline;
}

.cv2-bill-st--on {
  text-decoration: underline;
  font-weight: 600;
}
</style>
