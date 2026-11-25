<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「指派给我的订单」v2（`/v2/my-orders`；业务方指令 ④「把这个页面也做了吧
 * 功能和布局基本一样 只用换成我们自己的前端语言组件」，D-0346）。
 *
 * ## 旧页是什么、这里照搬什么
 *
 * 旧页 = island 复刻页 `layui-island/pages/order-myorderlist.ts`（路由 `orders-mine`）：
 * 单表列表页，数据 = `fetchAllMyOrders()` 一次取完（**客户端筛选 + 客户端分页**），
 * 业务判据（谁能提交 / 能不能改状态 / 金额怎么算）**全部在后端**，前端只把 `msg` 原样显示。
 * 本页按同一套功能重建为炭火 Vue 页（EP 内核 + `.ttv2` 作用域皮）：
 *
 * | 旧页功能 | 本页落点 |
 * |---|---|
 * | 7 个本地筛选（游戏区服 / 状态多选 / 单号 / 内容 / 日期起止 / 加急） | 筛选行同字段；**全部即时生效**（旧页文本框要点「搜索」才套用，这里一律即时——能力集一致，触发粒度是 UX 细节）。⚠️ D-0354：状态多选从筛选面板上移为页头下的**状态卡片**（状态名 + 单量，点击切换；筛选面板里的状态签行移除） |
 * | 单号搜索两个号都认（D-0301：`source_order_no ‖ order_no`） | 同一条规则（`applyMyOrderFilters`） |
 * | 日期默认「今天往前 6 个月 ~ 今天」 | 同一条规则（`defaultDateRange`） |
 * | 列表列 | 只搬**真有值**的列（订单号+急徽标 / 游戏 / 内容 / 状态 / 接手人 / 接手价 / 截图 / 接手时间(可排序) / 操作）；旧页那些后端不回值的复刻空列（角色名 / 阵营 / 角色信息 / 时间 / 机位 / 日志 / 空表头列）**不搬** |
 * | 分页 limit 20 / limits [10,20,30,40,50] | 同值（`el-pagination` 客户端分页） |
 * | 行内「提交完成」→ confirm → `POST /orders/<no>/submit/` | 同端点同文案（炭火确认弹层） |
 * | 状态徽标 / 工具栏「更改状态」→ 受限两档弹层 | 同语义（只对代练中、只许 待验收/问题单；逐单串行、先刷新再提示） |
 * | 工具栏「发到我的管理系统」/「同步订单状态」（双档） | 同端点同语义（勾选⇒推 `record`/`status`；无勾选⇒拉一轮） |
 * | 「导出订单」→ 前端 CSV（BOM + 公式注入转义） | 同一套列与转义规则（本地重写，见下） |
 * | 截图两颗按钮 → island 弹层（权限门 fail-closed） | 同 island 页 iframe（`el-dialog` 装同一 URL） |
 * | 单号蓝链 → 订单详情 | v2 暖米连体格（左右一样宽；20 位形状闸照搬） |
 * | `#total` 统计行 | 只保留**真数据**：筛出行数 + 声明句（租户名取会话，仅展示）；旧页那排恒 0 的服务端聚合骨架**不搬**（后端没有那个出口，搬过来 = 假数） |
 * | 「检查发送失败订单」（点了提示尚未开放） | **不搬**：后端没有端点，v2 不摆只会弹「尚未开放」的假控件（DRAFT §5 口径） |
 *
 * ## 视觉补充（2026-10-31 业务方看线上反馈 D-0351「内容区要更突出、更直观」）
 *
 * - 工具条跟管理订单一条软底；查询仍是青绿。重置是白底细框。
 * - 行内操作：传 / 看 = 图标幽灵钮（`cv2-iconact`）；「提交完成」= 小号红橙实底（`cv2-btn--submit`）；
 *   其余行内动作为文字链（`cv2-actlink`）。
 * - 行 hover = 极浅青绿晕（`--tt2-hover-wash`）；勾选行 = 浅青绿底（tr 挂 `cv2-row--checked`）。
 * - 分页选中页码 = 青绿 deep 实底白字（EP 内核的选中色经 carbon.css 覆盖）。
 *
 * ## 状态卡片与八色签（2026-10-31 业务方看线上真机六项精修 D-0354 建，D-0371 重设计）
 *
 * - 状态筛选 = **状态卡片**（页头下、筛选面板上）：8 张卡 = 状态名 + 该状态单量
 *   （客户端统计自 `fetchAllMyOrders()` 全量行，不受筛选影响）；点击 = 切换该状态筛选
 *   （单选语义：点已选中的 = 取消回全部），选中卡 = **样式 C**（浅底不动 + 墨点色描边加粗 +
 *   右上角墨点色三角 ✓，见 carbon.css）；筛选面板里的状态签行移除。
 * - 行内状态签 = 九状态**三件套** token（墨点 + 同族浅底 + 同族深字，色相分配见 STATUS_CLASS
 *   注释与 DRAFT §1），逐对实测 ≥4.5:1、墨点对底 ≥3:1。
 * - 筛选格跟管理订单连体：标签在框内，提示字仍浅。
 * - 工具条不再按功能涂色，和刷新同一条软底。
 * - 筛选控件 **EP 化**（业务方看本地环境追记）：原生 select/input/checkbox → `el-select`
 *   （filterable）/ `el-date-picker`（value-format YYYY-MM-DD，filters 字符串类型不变）/
 *   `el-input` / `el-checkbox`；v-model 接原 filters 字段、`data-v2-*` 锚点原样保留；
 *   面板 `:teleported="false"` 留在 `.ttv2` 内吃青绿皮（carbon.css EP 皮块）。
 *   ⚠️ attrs 落点实测（jsdom 探针）：el-input 的 data-* 落**内层原生 input**、el-select/
 *   el-checkbox 落组件根、el-date-picker **整包吞掉** ⇒ 日期的锚点挂在包裹层 `.cv2-date` 上。
 *
 * ## 视觉补充（2026-11-01 业务方第二轮：列表网格 + 列宽拖拽）
 *
 * - 表格改**全网格线**（th/td 四边 1px `--tt2-line`，对标/layui 的网格表；行内 44px 单行截断不变）。
 * - **列宽拖拽**：表头每列右缘 6px 热区（`.cv2-th-resizer`），mousedown 拖动 / 双击复原，
 *   共享 `src/v2/tableResize.ts`；列宽按「页面名 + 列键」存 localStorage `tt.v2.table.my-orders`。
 *
 * ## 数据：零新增端点
 *
 * `api/myOrders.ts` + `api/jidanpuMember.ts` 全部既有端点；权限门走会话 store 的
 * `canNode`（与 island 页 `canNode` 同口径的短路规则，免二次 `/auth/me`）。
 *
 * ⛔ **不 `import` island 页模块**（同 `V2Dashboard` 的纪律）：那会把 layui 装载链拉进 v2 的
 * 模块图。下面的纯函数 / 常量（筛选判据、CSV、推送结果文案）是**带出处注记的本地重写**，
 * 与 island 页逐字同语义；改口径时要两处同改（两处都有测试钉住）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { isOrderNoShape } from '@/api/orders'
import {
  fetchAllMyOrders,
  reportIssueOrder,
  submitMyOrder,
  type MyOrderItem,
} from '@/api/myOrders'
import {
  pushMemberJidanpuNow,
  syncMemberJidanpuNow,
  type JidanpuMemberPushAction,
} from '@/api/jidanpuMember'
import { v2OrderStatusLabel, V2_ORDER_STATUS_LABELS, type OrderStatus } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import { useColumnWidths } from '@/v2/tableResize'
import OrderDetailDialog from '@/v2/OrderDetailDialog.vue'
import V2OrderDetailPane from '@/v2/V2OrderDetailPane.vue'
import V2ScreenshotUpload from '@/v2/V2ScreenshotUpload.vue'

const session = useSessionStore()

/* ── 文案（出处见 island 页同名常量；本地重写不引模块）──────────────────── */

/** 未勾选任何行时的提示（island 页 `SELECT_RECORDS_FIRST_TEXT`，对标 zh-CN `setRecMan`）。 */
const SELECT_RECORDS_FIRST_TEXT = '请选中要操作的记录'
/** 提交确认（island 页 `SUBMIT_CONFIRM_TEXT`，对标 zh-CN `js.submtEnd` 逐字）。 */
const SUBMIT_CONFIRM_TEXT = '确定打完了吗？提交前请再三确认哦'
/** 提交成功提示（自拟；后端成功信封 msg 为空串——island 页 `SUBMIT_SUCCESS_TEXT` 同句）。 */
const SUBMIT_SUCCESS_TEXT = '提交成功，已送交验收'
/** 「更改状态」弹层标题（island 页 `CHANGE_STATUS_DIALOG_TITLE`，对标 zh-CN `changeOrderSta`）。 */
const CHANGE_STATUS_DIALOG_TITLE = '更改订单状态'
/** 可选目标 = 业务方裁定的全部：「只能从代练中更改为待验收或者问题单」。 */
const CHANGE_STATUS_TARGETS: readonly OrderStatus[] = ['PENDING_REVIEW', 'ISSUE']
/** 报问题单成功提示（自拟；同 island 页 `REPORT_ISSUE_SUCCESS_TEXT`）。 */
const REPORT_ISSUE_SUCCESS_TEXT = '已标记为问题单'
/** 「不是代练中的单」提示前缀（同 island 页 `CHANGE_STATUS_REQUIRES_IN_PROGRESS`）。 */
const CHANGE_STATUS_REQUIRES_IN_PROGRESS = '只有代练中的单可以更改状态，当前状态：'
/** 「导出」时一行都没有（同 island 页 `EXPORT_EMPTY_TEXT`）。 */
const EXPORT_EMPTY_TEXT = '没有可导出的订单'
/** 推送在途提示（同 island 页 `PUSH_IN_PROGRESS_TEXT`；真互斥在后端串行闸）。 */
const PUSH_IN_PROGRESS_TEXT = '正在推送，请稍候'
/** 层的单号形状闸固定文案（同 island 页；20 位闸与后端路由约束同源）。 */
const BAD_ORDER_NO_TEXT = '订单号格式不正确，无法打开订单详情'

/** 错误对象的可显示文本。**不自造文案**：`request()` 抛出的 message 就是后端 `msg` 原文。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（炭火 toast，页面内自绘——ElMessage 会 teleport 到 body、拿不到 .ttv2 的炭火皮；
 *    本地实现顺带让测试可以钉 DOM，而不用 spy 全局函数）──────────────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

/** info 停 1500ms、error 停 6000ms（island 页 `islandMsg` / `errMsg` 的同值口径：错误要看清）。 */
function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 权限门（fail-closed：会话取不到 = 不画；与 island 页 setMyOrdersPermissions 同口径）── */

const canViewScreenshots = computed(() => session.canNode(['order.view_screenshot']))
const canUploadScreenshots = computed(() => session.canNode(['order.upload_screenshot']))
const canChangeStatus = computed(() => session.canNode(['order.submit_work']))

/* ── 取数 ─────────────────────────────────────────────────────────────── */

const allRows = ref<MyOrderItem[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

async function reload(): Promise<void> {
  loading.value = true
  try {
    allRows.value = await fetchAllMyOrders()
    loadError.value = null
  } catch (e) {
    // 失败也要把表清空并**如实报错**：403（无节点 `my_orders.send_to_my_system`）/ 401 的 msg 由 `request()` 给出。
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

/* ── 筛选（本地筛已取到的全量行；判据 = island 页 `applyFilters` 的逐条重写）──────────── */

/** 页面筛选条件（与 island 页 `MyOrderFilters` 同形）。 */
type MyOrderFilters = {
  gameName: string
  statuses: OrderStatus[]
  key1: string
  key2: string
  sDate: string
  eDate: string
  urgent: boolean
}

/** `YYYY-MM-DD`（本地时区；island 页 `toDateInputValue` 同式）。 */
function toDateInputValue(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 日期默认值 = 「今天往前 6 个月 ~ 今天」（对标规则；island 页 `defaultDateRange` 同式）。 */
function defaultDateRange(now: Date = new Date()): [string, string] {
  return [
    toDateInputValue(new Date(now.getFullYear(), now.getMonth() - 6, now.getDate())),
    toDateInputValue(now),
  ]
}

const [defaultFrom, defaultTo] = defaultDateRange()

const filters = reactive<MyOrderFilters>({
  gameName: '',
  statuses: [],
  key1: '',
  key2: '',
  sDate: defaultFrom,
  eDate: defaultTo,
  urgent: false,
})

/** el-date-picker 清空时发 `null`（EP 约定）——filters 保持**字符串类型**不变（空串 = 不筛），
 * 在这里归一（⛔ 不让 null 混进 `MyOrderFilters`，日期比较 `day < f.sDate` 的字典序判据不动）。 */
function setSDate(v: unknown): void {
  filters.sDate = typeof v === 'string' ? v : ''
}

function setEDate(v: unknown): void {
  filters.eDate = typeof v === 'string' ? v : ''
}

/** 区服下拉的选项 = 数据里出现过的游戏名（去重排序；island 页 `rebuildGameOptions` 同口径）。 */
const gameNames = computed(() =>
  Array.from(new Set(allRows.value.map((r) => r.game_name).filter((n) => n !== ''))).sort((a, b) =>
    a.localeCompare(b, 'zh-Hans-CN'),
  ),
)

/** 本地筛选（island 页 `applyFilters` 的逐条重写；判据「空值丢弃」天然成立）：
 * 单号搜索**两个号都认**（来源号能搜到 + 系统号仍能搜到，D-0301）；
 * 日期比 `created_at` 的本地日期部分（`YYYY-MM-DD` 定长字典序；⛔ 不经 `new Date()`，时区会漂一天）。 */
function applyMyOrderFilters(rows: MyOrderItem[], f: MyOrderFilters): MyOrderItem[] {
  return rows.filter((row) => {
    if (f.gameName !== '' && row.game_name !== f.gameName) return false
    if (f.statuses.length > 0 && !f.statuses.includes(row.status as OrderStatus)) return false
    if (f.urgent && row.is_urgent !== true) return false
    if (
      f.key1 !== '' &&
      !row.order_no.includes(f.key1) &&
      !(row.source_order_no || '').includes(f.key1)
    ) {
      return false
    }
    if (f.key2 !== '' && !row.project_text.includes(f.key2)) return false
    const day = row.created_at.slice(0, 10)
    if (f.sDate !== '' && day < f.sDate) return false
    if (f.eDate !== '' && day > f.eDate) return false
    return true
  })
}

const filteredRows = computed(() => applyMyOrderFilters(allRows.value, filters))

function resetFilters(): void {
  filters.gameName = ''
  filters.statuses = []
  filters.key1 = ''
  filters.key2 = ''
  filters.sDate = defaultFrom
  filters.eDate = defaultTo
  filters.urgent = false
  page.value = 1
}

/** 查询/重置/游戏切换/加急切换都会把页码拨回 1（重筛从第一页看起）。 */
function refilter(): void {
  page.value = 1
}

/* ── 排序（旧页只有「接手时间」一列可排；layui 同款三态：升 → 降 → 不排）────────────── */

const sortDir = ref<'asc' | 'desc' | null>(null)

function toggleSort(): void {
  sortDir.value = sortDir.value === 'asc' ? 'desc' : sortDir.value === 'desc' ? null : 'asc'
}

const sortedRows = computed(() => {
  if (sortDir.value === null) return filteredRows.value
  const dir = sortDir.value === 'asc' ? 1 : -1
  return [...filteredRows.value].sort((a, b) => (a.created_at < b.created_at ? -dir : dir))
})

/* ── 分页（客户端；与旧页同值：limit 20 / limits [10,20,30,40,50]）────────────────── */

const PAGE_LIMITS = [10, 20, 30, 40, 50]
const page = ref(1)
const pageSize = ref(20)

const pageCount = computed(() => Math.max(1, Math.ceil(sortedRows.value.length / pageSize.value)))
// 行数变少（筛选/刷新）后当前页可能越界 ⇒ 收回来（el-pagination 不会自己夹）。
watch(pageCount, (c) => {
  if (page.value > c) page.value = c
})

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return sortedRows.value.slice(start, start + pageSize.value)
})

/** 每页条数切换（旧页 limits 同值；切完拨回第 1 页）。 */
function onPageSizeChange(event: Event): void {
  const size = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(size) && size > 0) {
    pageSize.value = size
    page.value = 1
  }
}

/* ── 勾选（旧页 layui 的 checkbox 列；表头勾选 = 勾当前页全部行）────────────────────── */

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
  () => pagedRows.value.length > 0 && pagedRows.value.every((r) => isChecked(r.order_no)),
)

function togglePageAll(): void {
  if (pageAllChecked.value) {
    checkedNos.value = checkedNos.value.filter((no) => !pagedRows.value.some((r) => r.order_no === no))
  } else {
    for (const r of pagedRows.value) {
      if (!isChecked(r.order_no)) checkedNos.value.push(r.order_no)
    }
  }
}

/** 勾选行的完整数据（工具栏动作只吃勾选的行）。 */
const checkedRows = computed(() => allRows.value.filter((r) => isChecked(r.order_no)))

/* ── 列宽拖拽（⭐ 2026-11-01 第二轮：对标式网格 + 手动拉宽；共享 src/v2/tableResize.ts）────
 * 列键与模板 th 一一对应；「代练/陪玩内容」不给默认宽（弹性列，吃掉剩余空间），
 * 其余列按内容给默认档；拖过 / 双击复原后的值存 localStorage `tt.v2.table.my-orders`。 */
const ORDER_COLUMNS = ['check', 'order', 'game', 'project', 'status', 'taker', 'price', 'shots', 'time', 'actions'] as const

const { widths, colStyle, startResize, resetColumn } = useColumnWidths('my-orders', {
  check: 36,
  /* ⭐ 2026-11-03：order/game/project = 长内容列；
   * status/price/shots = 内容短且基本固定 ⇒ 窄固定宽 + 居中 + 无拖拽手柄。
   * ⭐ order 定宽（业务方「订单号列过宽」——19 位 mono + 复制钮 ~198 即够，
   * 超出省略；仍可拖拽）。 */
  order: 206,
  game: 120,
  status: 96,
  taker: 100,
  price: 96,
  shots: 104,
  time: 150,
  actions: 110,
})
const tableMinWidth = computed(() => ORDER_COLUMNS.reduce(
  (total, key) => total + (widths[key] ?? (key === 'project' ? 220 : 0)), 0,
))

/* ── 单元格展示（与 island 页 templet 同口径的本地重写）────────────────────────────── */

/** 展示单号：来源号非空取它、空回退内置号（D-0301）。 */
function viewNoOf(d: MyOrderItem): string {
  return d.source_order_no || d.order_no
}

/** 接手时间：`YYYY-MM-DD HH:mm`（island 页 templet 15 同式；不渲染后端原样 ISO 串）。 */
function claimedAtText(d: MyOrderItem): string {
  return d.created_at.replace('T', ' ').slice(0, 16)
}

/** 状态 → 炭火签的 token 类（D-0371 三件套：八状态各占一个色相；未知取值落中性灰，文案原样显示）：
 * 未分配=中性灰 / 待接单=紫 / 代练中=绿 / 待验收=橙红 / 已完成=蓝 /
 * 问题单=深砖红 / 已暂停=橙 / 已退单=浅红。 */
function statusText(status: string): string {
  return v2OrderStatusLabel(status)
}

/* ── 提交完单（行内「提交完成」；业务规则全在后端，前端只转述 msg）──────────────────── */

const submitting = ref(false)
const confirmState = ref<{ orderNo: string } | null>(null)

/** 确认弹层（旧页 `layer.confirm` 的炭火版；文案逐字同 `SUBMIT_CONFIRM_TEXT`）。 */
function askSubmit(orderNo: string): void {
  if (orderNo === '') return
  confirmState.value = { orderNo }
}

async function doSubmit(orderNo: string): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  confirmState.value = null
  try {
    await submitMyOrder(orderNo)
    // 顺序同 island 页：先刷新、再提示成功（刷新里的失败提示若排在后面会盖掉成功提示）。
    await reload()
    showToast(SUBMIT_SUCCESS_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    submitting.value = false
  }
}

/* ── 更改状态（受限两档；工具栏那颗有节点门，行内状态签谁都能点——判据在后端）────────── */

const changing = ref(false)
const statusDlg = ref<{ orderNos: string[]; target: OrderStatus; reason: string } | null>(null)

/** 点「更改状态」：先**逐行**校验来源态，再开弹层（island 页 `requestChangeStatus` 同语义：
 * 只有代练中的单能改、且只能改到「待验收 / 问题单」；前端这道只是省一趟明知会被拒的请求）。 */
function requestChangeStatus(rows: readonly { order_no: string; status: string }[]): void {
  if (rows.length === 0) {
    showToast(SELECT_RECORDS_FIRST_TEXT)
    return
  }
  const notInProgress = rows.find((r) => r.status !== 'IN_PROGRESS')
  if (notInProgress !== undefined) {
    showToast(`${CHANGE_STATUS_REQUIRES_IN_PROGRESS}${statusText(notInProgress.status)}`, 'error')
    return
  }
  statusDlg.value = { orderNos: rows.map((r) => r.order_no), target: 'PENDING_REVIEW', reason: '' }
}

/** 真发请求：**逐单串行**；失败也照旧刷新（先刷新、再提示，同 island 页 `runChangeStatus`）。 */
async function runChangeStatus(target: OrderStatus, orderNos: readonly string[], reason = ''): Promise<void> {
  if (changing.value) return
  if (target === 'ISSUE' && reason.trim() === '') {
    showToast('请填写变更原因（将记入订单日志）', 'error')
    return
  }
  changing.value = true
  statusDlg.value = null
  let done = 0
  let firstError = ''
  try {
    for (const orderNo of orderNos) {
      try {
        if (target === 'ISSUE') await reportIssueOrder(orderNo, reason.trim())
        else await submitMyOrder(orderNo)
        done += 1
      } catch (e) {
        if (firstError === '') firstError = errText(e)
      }
    }
  } finally {
    changing.value = false
  }
  await reload()
  if (firstError !== '') {
    showToast(done > 0 ? `已更改 ${done} 笔；${firstError}` : firstError, 'error')
    return
  }
  showToast(target === 'ISSUE' ? REPORT_ISSUE_SUCCESS_TEXT : SUBMIT_SUCCESS_TEXT)
}

/* ── 推送到下家 / 同步（island 页 `doPushToMySystem` / `doSyncOrderStatus` 同语义）──────── */

/** 手动推送的结果提示（island 页 `pushResultText` 同式重写：主体「已推送 N 笔，跳过 M 笔」，
 * 失败追加一句；有跳过 / 失败时把**后端第一条不同原因**缀在后面，多条只在末尾加「等」）。 */
function pushResultText(
  pushed: number,
  skipped: number,
  failed: number,
  reasons: readonly string[] = [],
): string {
  const base = `已推送 ${pushed} 笔，跳过 ${skipped} 笔`
  const head = failed > 0 ? `${base}，失败 ${failed} 笔` : base
  const hasReason = skipped + failed > 0 && reasons.some((reason) => reason !== '')
  if (!hasReason) return head
  const distinct = Array.from(new Set(reasons.filter((reason) => reason !== '')))
  return `${head}：${distinct[0]}${distinct.length > 1 ? '等' : ''}`
}

const pushing = ref(false)

async function doPushToMySystem(
  action: JidanpuMemberPushAction,
  rows: readonly MyOrderItem[],
): Promise<void> {
  if (rows.length === 0) {
    showToast(SELECT_RECORDS_FIRST_TEXT)
    return
  }
  if (pushing.value) {
    showToast(PUSH_IN_PROGRESS_TEXT)
    return
  }
  pushing.value = true
  const orderNos = rows.map((row) => row.order_no)
  try {
    const result = await pushMemberJidanpuNow(orderNos, action)
    await reload()
    const reasons = result.items.map((item) => item.reason)
    const text = pushResultText(result.pushed, result.skipped, result.failed, reasons)
    // 有原因要说清 → 6000ms；一眼能看完 → 1500ms（island 页同判据）。
    showToast(
      text,
      result.skipped + result.failed > 0 && reasons.some((r) => r !== '') ? 'error' : 'info',
    )
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    pushing.value = false
  }
}

/** 「同步订单状态」双档：有勾选 ⇒ 推状态；无勾选 ⇒ 往下拉一轮（island 页同语义）。 */
async function doSyncOrderStatus(): Promise<void> {
  const checked = checkedRows.value
  if (checked.length > 0) {
    await doPushToMySystem('status', checked)
    return
  }
  try {
    const result = await syncMemberJidanpuNow()
    await reload()
    showToast(`同步完成，本次更新 ${result.applied} 笔订单`)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 导出 CSV（island 页 12-(d) 的同式重写：BOM + 公式注入前缀转义 + CRLF）────────────── */

type CsvColumn = { title: string; value: (d: MyOrderItem) => string }

/** 只导**本页真有值**的列（列名逐字同 island 页 `CSV_COLUMNS`；「加急」是徽标承载的事实）。 */
const CSV_COLUMNS: readonly CsvColumn[] = [
  { title: '订单号', value: (d) => viewNoOf(d) },
  { title: '游戏名称', value: (d) => d.game_name },
  { title: '代练/陪玩内容', value: (d) => d.project_text },
  { title: '订单状态', value: (d) => statusText(d.status) },
  { title: '接手时间', value: (d) => claimedAtText(d) },
  { title: '加急', value: (d) => (d.is_urgent === true ? '是' : '否') },
]

/** 会被 Excel / WPS 当**公式**解释的开头字符（CSV 注入）。 */
const CSV_FORMULA_PREFIX = /^[=+\-@\t\r]/

function csvCell(value: string): string {
  const guarded = CSV_FORMULA_PREFIX.test(value) ? `'${value}` : value
  return `"${guarded.replace(/"/g, '""')}"`
}

/** 整份 CSV 正文（CRLF；不含 BOM——BOM 由下载层加）。 */
function buildCsv(rows: MyOrderItem[], columns: readonly CsvColumn[] = CSV_COLUMNS): string {
  const lines = [columns.map((c) => csvCell(c.title)).join(',')]
  for (const row of rows) lines.push(columns.map((c) => csvCell(c.value(row))).join(','))
  return lines.join('\r\n')
}

/** `我的订单_YYYYMMDDHHMMSS.csv`（本地时间；island 页 `csvFilename` 同式）。 */
function csvFilename(now: Date = new Date()): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return (
    `我的订单_${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}` +
    `${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}.csv`
  )
}

function exportOrders(rows: MyOrderItem[]): void {
  if (rows.length === 0) {
    showToast(EXPORT_EMPTY_TEXT)
    return
  }
  // 正文前置 UTF-8 BOM（Excel 打开中文不乱码）；⛔ 不用 window.open（island 页同一坑）。
  const blob = new Blob([`\uFEFF${buildCsv(rows)}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = csvFilename()
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  showToast(`已导出 ${rows.length} 笔订单`)
}

/** 导出优先级同旧页：勾了行导勾选的、没勾导当前筛出来的全部。 */
function onExport(): void {
  const checked = checkedRows.value
  exportOrders(checked.length > 0 ? checked : filteredRows.value)
}

/* ── 弹层：订单详情 / 截图（装既有 island 页的 iframe；零新端点）────────────────────── */

const ISLAND_PAGE_BASE = `${import.meta.env.BASE_URL}layui-island/page.html`

function islandUrl(page: string, orderNo: string): string {
  return `${ISLAND_PAGE_BASE}?page=${page}&order_no=${encodeURIComponent(orderNo)}`
}

const detailDlg = ref<{
  orderNo: string
  viewNo: string
  gameName: string
  projectText: string
  status: string
  takerName: string
} | null>(null)

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
  detailDlg.value = {
    orderNo,
    viewNo: viewNoOf(d),
    gameName: d.game_name,
    projectText: d.project_text,
    status: d.status,
    takerName: d.taker_name ?? '',
  }
}

/** 截图弹层（上传 / 查看共用一个壳，装各自的 island 页；权限门在渲染层）。 */
const shotDlg = ref<{ page: string; title: string; orderNo: string } | null>(null)
const shotBusy = ref(false)
const shotUrl = computed(() =>
  shotDlg.value === null ? '' : islandUrl(shotDlg.value.page, shotDlg.value.orderNo),
)

function openScreenshot(page: string, title: string, orderNo: string): void {
  if (!isOrderNoShape(orderNo)) {
    showToast('订单号格式不正确，无法打开订单截图', 'error')
    return
  }
  shotDlg.value = { page, title, orderNo }
}

/* ── 其它出口 ─────────────────────────────────────────────────────────── */

const tenantName = computed(() => session.user?.tenant_name || '')
</script>

<template>
  <div class="cv2-page" data-v2-page="my-orders">
    <!-- 筛选行：顶部状态卡片已撤。状态列仍在表里。 -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="cv2-filter-row">
        <!-- ⭐ D-0354 追记：筛选控件 EP 化（原生 select/input → EP）；v-model 接原 filters 字段、
             data-v2-* 锚点原样保留；下拉/日期面板 :teleported="false" 留在 .ttv2 内吃青绿皮 -->
        <label class="cv2-field">
          <span class="cv2-field-label">游戏</span>
          <el-select v-model="filters.gameName" filterable placeholder="全部游戏" :class="{ 'flt-ph': filters.gameName === '' }" data-v2-filter="game" @change="refilter">
            <el-option value="" label="全部游戏" />
            <el-option v-for="n in gameNames" :key="n" :value="n" :label="n" />
          </el-select>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">订单号</span>
          <el-input
            v-model="filters.key1"
            placeholder="订单号"
            data-v2-filter="key1"
            @keydown.enter="refilter"
          />
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">内容</span>
          <el-input
            v-model="filters.key2"
            placeholder="代练/陪玩内容"
            data-v2-filter="key2"
            @keydown.enter="refilter"
          />
        </label>
        <!-- ⚠️ el-date-picker 不透传 attrs（实测：data-v2-filter 整个丢失）⇒ 锚点挂在包裹层。 -->
        <label class="cv2-field">
          <span class="cv2-field-label">接单从</span>
          <span class="cv2-date" data-v2-filter="sdate">
            <el-date-picker
              :model-value="filters.sDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="开始日期"
              :teleported="false"
              @update:model-value="setSDate"
            />
          </span>
        </label>
        <label class="cv2-field">
          <span class="cv2-field-label">到</span>
          <span class="cv2-date" data-v2-filter="edate">
            <el-date-picker
              :model-value="filters.eDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="结束日期"
              :teleported="false"
              @update:model-value="setEDate"
            />
          </span>
        </label>
        <el-checkbox v-model="filters.urgent" data-v2-filter="urgent" @change="refilter">只看加急</el-checkbox>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find cv2-btn--fill" data-v2="search" @click="refilter">查询</button>
          <!-- 高频工具档（D-0351 第 3 条）：重置这类高频件升琥珀浅底；查询保持主档。 -->
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="reset" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <!-- 工具栏：刷新 / 发到我的管理系统 / 同步订单状态（双档）/ 更改状态（节点门）/ 导出。
         高频件升工具档（D-0351 第 3 条：刷新 / 导出订单）；低频推送件保持白底灰框。 -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip">
      <button type="button" class="cv2-btn" data-v2="refresh" :disabled="loading" @click="reload">刷新</button>
      <button type="button" class="cv2-btn" data-v2="push-record" @click="doPushToMySystem('record', checkedRows)">
        发到我的管理系统
      </button>
      <button type="button" class="cv2-btn" data-v2="sync-status" @click="doSyncOrderStatus">
        同步订单状态
      </button>
      <!-- 按节点渲染（order.submit_work；没有就不画——点了会 403 的入口不出现，fail-closed） -->
      <button
        v-if="canChangeStatus"
        type="button"
        class="cv2-btn"
        data-v2="change-status"
        @click="requestChangeStatus(checkedRows)"
      >
        更改状态
      </button>
      <button type="button" class="cv2-btn" data-v2="export" @click="onExport">导出订单</button>
      </div>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedRows.length }} 行</span>
    </div>

    <!-- 统计行：只报真数据（筛出行数）+ 声明句；旧页恒 0 的服务端聚合骨架不搬（后端没有出口） -->
    <p class="cv2-total" data-v2="total">
      【统计】共 {{ filteredRows.length }} 笔订单（当前筛选出）
      <span class="cv2-total-notice">
        【声明】该系统并非交易平台，无担保服务，系统内所有订单业务及结算均由{{ tenantName }}负责运营，
        本系统一切业务和结算与软件开发商团团通无关
      </span>
    </p>

    <!-- 表格：语义 table（DRAFT §7）；列 = 旧页真有值的那些。
         ⭐ 2026-11-01 第二轮：<colgroup> 承接列宽拖拽（table-layout:fixed 下 col width 定宽）；
         每个 th 右缘一枚 6px 拖拽手柄（双击复原）。 -->
    <section class="cv2-panel cv2-table-panel" aria-label="订单列表">
      <table class="cv2-table" :style="{ minWidth: `${tableMinWidth}px` }">
        <colgroup>
          <col v-for="key in ORDER_COLUMNS" :key="key" :style="colStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th class="cv2-th-check">
              <input
                type="checkbox"
                data-v2="check-page-all"
                :checked="pageAllChecked"
                aria-label="勾选当前页全部行"
                @change="togglePageAll"
              />
              <span class="cv2-th-resizer" data-v2-col="check" @mousedown="startResize($event, 'check')" @dblclick="resetColumn('check')" />
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
              代练/陪玩内容
              <span class="cv2-th-resizer" data-v2-col="project" @mousedown="startResize($event, 'project')" @dblclick="resetColumn('project')" />
            </th>
            <th class="cv2-th-center">
              订单状态
            </th>
            <th>
              接手人
              <span class="cv2-th-resizer" data-v2-col="taker" @mousedown="startResize($event, 'taker')" @dblclick="resetColumn('taker')" />
            </th>
            <th class="cv2-th-num cv2-th-center">
              接手价
            </th>
            <th v-if="canViewScreenshots || canUploadScreenshots" class="cv2-th-center">
              订单截图
            </th>
            <th>
              <button type="button" class="cv2-sort" data-v2="sort-claimed" @click="toggleSort">
                接手时间<span v-if="sortDir === 'asc'">↑</span><span v-else-if="sortDir === 'desc'">↓</span>
              </button>
              <span class="cv2-th-resizer" data-v2-col="time" @mousedown="startResize($event, 'time')" @dblclick="resetColumn('time')" />
            </th>
            <th>
              操作
              <span class="cv2-th-resizer" data-v2-col="actions" @mousedown="startResize($event, 'actions')" @dblclick="resetColumn('actions')" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in pagedRows"
            :key="row.id"
            class="cv2-row"
            :class="{ 'cv2-row--checked': isChecked(row.order_no) }"
            :data-v2-row="row.order_no"
          >
            <td class="cv2-th-check">
              <input
                type="checkbox"
                :data-v2-row-check="row.order_no"
                :checked="isChecked(row.order_no)"
                :aria-label="`勾选 ${viewNoOf(row)}`"
                @change="toggleRow(row.order_no)"
              />
            </td>
            <td class="cv2-td-order">
              <div class="my-order-line">
              <span v-if="row.is_urgent === true" class="cv2-urgent" title="加急订单，优先安排">急</span>
              <a
                href="#"
                class="cv2-order-link cv2-ono"
                :data-v2-detail="row.order_no"
                @click.prevent="openDetail(row)"
              >{{ viewNoOf(row) }}</a>
              <button
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
              </div>
            </td>
            <td>{{ row.game_name }}</td>
            <td class="cv2-td-project">{{ row.project_text }}</td>
            <td class="cv2-td-center">
              <button
                type="button"
                class="cv2-st cv2-order-status cv2-st--mark"
                :class="orderStatusClass(row.status)"
                :data-v2-status="row.order_no"
                :data-status="row.status"
                :title="`${viewNoOf(row)}点击可以更改订单状态`"
                @click="requestChangeStatus([{ order_no: row.order_no, status: row.status }])"
              >
                <i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}
              </button>
            </td>
            <td>{{ row.taker_name === null ? '' : row.taker_name }}</td>
            <td class="cv2-td-num cv2-td-center"><span class="cv2-money">{{ row.taker_price === null ? '' : row.taker_price }}</span></td>
            <td v-if="canViewScreenshots || canUploadScreenshots" class="cv2-td-shots cv2-td-center">
              <!-- ⭐ 业务方「上传与查看需要做成图标 类似对标那样 别单纯弄个文字」：
                   传=layui upload 字形、看=picture 字形（码点见 styles/layui-icons.css 尾部追加）。 -->
              <button
                v-if="canUploadScreenshots"
                type="button"
                class="cv2-iconact"
                :data-v2-shot-upload="row.order_no"
                title="上传订单截图"
                aria-label="上传订单截图"
                @click="openScreenshot('order-screenshots-upload', '上传订单截图', row.order_no)"
              >
                <i class="layui-icon layui-icon-upload" aria-hidden="true"></i>
              </button>
              <button
                v-if="canViewScreenshots"
                type="button"
                class="cv2-iconact"
                :data-v2-shot-view="row.order_no"
                title="查看订单截图"
                aria-label="查看订单截图"
                @click="openScreenshot('order-screenshots-manage', '查看订单截图', row.order_no)"
              >
                <i class="layui-icon layui-icon-picture" aria-hidden="true"></i>
              </button>
            </td>
            <td class="cv2-td-time">{{ claimedAtText(row) }}</td>
            <td>
              <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--sm cv2-btn--commit" :data-v2-submit="row.order_no" @click="askSubmit(row.order_no)">
                提交完成
              </button>
            </td>
          </tr>
          <tr v-if="pagedRows.length === 0">
            <td :colspan="canViewScreenshots || canUploadScreenshots ? 10 : 9" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="cv2-pager" data-v2="pager">
        <!-- 计数与每页条数是本页自己的（EP 的 total/sizes 文案走它内置 locale，炭火页不吃那套皮）；
             翻页按钮用 EP 内核（locale 无关）。 -->
        <span class="cv2-pager-total" data-v2="pager-total">共 {{ sortedRows.length }} 条</span>
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
          :total="sortedRows.length"
          layout="prev, pager, next"
          background
        />
      </div>
    </section>

    <!-- 炭火 toast（页面内，吃 .ttv2 的皮） -->
    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>

    <!-- 提交确认（旧页 layer.confirm 的炭火版；文案逐字同对标 zh-CN）。
         ⭐ 弹窗三档统一（2026-11-01 第二轮）：确认类 = sm 480。 -->
    <el-dialog
      :model-value="confirmState !== null"
      title="提交完成"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-confirm"
      @update:model-value="confirmState = null"
    >
      <p class="cv2-dlg-text">{{ SUBMIT_CONFIRM_TEXT }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="confirmState = null">取消</button>
          <button
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="confirm-submit"
            @click="confirmState !== null && void doSubmit(confirmState.orderNo)"
          >
            确定
          </button>
        </div>
      </template>
    </el-dialog>

    <!-- 更改订单状态（受限两档；业务方裁定「只能从代练中更改为待验收或者问题单」）；弹窗档 = sm 480 -->
    <el-dialog
      :model-value="statusDlg !== null"
      :title="CHANGE_STATUS_DIALOG_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-status"
      @update:model-value="statusDlg = null"
    >
      <div class="cv2-dlg-radios" data-v2="status-targets">
        <label v-for="code in CHANGE_STATUS_TARGETS" :key="code" class="cv2-radio">
          <input v-if="statusDlg" v-model="statusDlg.target" type="radio" name="v2-change-target" :value="code" />
          {{ V2_ORDER_STATUS_LABELS[code] }}
        </label>
      </div>
      <div v-if="statusDlg !== null && statusDlg.target === 'ISSUE'" class="cv2-field cv2-reason" data-v2="status-reason">
        <span class="cv2-field-label">说明原因</span>
        <el-input
          v-model="statusDlg.reason"
          maxlength="200"
          placeholder="原因将记入订单日志（必填）"
          data-v2="status-reason-input"
        />
      </div>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="statusDlg = null">取消</button>
          <button
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="confirm-status"
            @click="statusDlg !== null && void runChangeStatus(statusDlg.target, statusDlg.orderNos, statusDlg.reason)"
          >
            确定
          </button>
        </div>
      </template>
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
        allow-own-order-detail
        :order-no="detailDlg.orderNo"
        :view-no="detailDlg.viewNo"
        :game-name="detailDlg.gameName"
        :project-text="detailDlg.projectText"
        :status="detailDlg.status"
        :taker-name="detailDlg.takerName"
      />
    </OrderDetailDialog>

    <!-- 订单截图（上传 / 查看共用壳，装各自 island 页；⭐ 弹窗档 = lg 860（看图/截图类）） -->
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
      <V2ScreenshotUpload
        v-if="shotDlg !== null && shotDlg.page === 'order-screenshots-upload'"
        :order-no="shotDlg.orderNo"
        @busy-change="shotBusy = $event"
      />
      <iframe
        v-else-if="shotDlg !== null"
        :src="shotUrl"
        class="cv2-island-frame cv2-island-frame--short"
        :title="shotDlg.title"
      ></iframe>
    </el-dialog>
  </div>
</template>

<style scoped>
.cv2-table-panel { overflow-x: auto; }
.my-order-line { display: flex; align-items: center; min-width: 0; width: 100%; }
.my-order-line .cv2-order-link { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.my-order-line .cv2-copybtn, .my-order-line .cv2-urgent { flex: none; }
.my-order-line .cv2-copybtn { opacity: 1; }
.cv2-page .cv2-toast { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: var(--tt2-side); color: var(--tt2-side-active); z-index: 3000; }
</style>
