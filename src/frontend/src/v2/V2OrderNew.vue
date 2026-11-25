<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 「录入订单」v2（`/v2/order-new`；菜单「订单管理」组第一条 `order-new`，D-0355）。布局与功能 = 现役 island 复刻页 `layui-island/pages/order-addorder.ts`（对标 1:1）的**全量照搬** + 炭火换皮（`docs/FRONTEND-V2-DRAFT.md` §5）：右列按客服动线「填来源单号 → 自动识别游戏价格 → 写代练内容 → 提交」做主流程/折叠两层信息架构（连体网格、限宽 880、resize 柄已撤；「尚未开放」死下拉在连体格内改 `popper-options: fixed` 防 overflow 裁剪），左列粘贴识别区不动。
 * 编辑态复用录单表单。普通字段走 PATCH，销售客服改归属走专用端点。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  fetchCategories,
  fetchGames,
  fetchProjects,
  fetchServers,
  type CategoryRow,
  type ConfigRow,
  type ProjectRow,
  type ServerRow,
} from '@/api/config'
import { fetchOrderSources } from '@/api/orderSources'
import {
  changeSalesAgent,
  createOrder,
  fetchAssigneeOptions,
  fetchKefuOptions,
  fetchOrder,
  parsePastedText,
  readPlatformOrder,
  updateOrderWithMessage,
  type AssigneeOption,
  type CreateOrderPayload,
  type Order,
  type ParsedFields,
} from '@/api/orders'
import { BizError, HttpError, NetworkError } from '@/api/errors'
import {
  MONEY_INPUT_PLACEHOLDER,
  ORDER_MONEY_FIELDS,
  formatMoney,
  isBlankLikePython,
  trimLikePython,
  validateMoneyInput,
} from '@/config/money'
import { ErrorCode } from '@/config/errorCodes'
import {
  applyParsedToForm,
  currentActor,
  emptyOrderForm,
  formToPayload,
  type OptionDicts,
  type OrderForm,
} from '@/views/orders/orderFormFields'
import { projectTextSyncLimitError, toggleProjectName } from '@/views/orders/projectText'
import { detectGameFromText } from '@/views/orders/gameDetect'
import { rememberAssigneeValue } from '@/views/orders/assigneePreference'
import { useSessionStore } from '@/stores/session'
import { useRouter } from 'vue-router'
import { V2_GAME_PROJECTS } from '@/v2/v2Menu'

const session = useSessionStore()
const router = useRouter()

/* ── 编辑态（⭐ 2026-11-04 业务方「编辑弹窗应该复用录单页面那种布局」）：管理订单页把本组件
 *    带着订单号装进弹窗；同一份表单，回填 = island `orderToAddOrderForm` 同口径，保存 = PATCH
 *    （载荷 = formToPayload 删金额字段，island `formToUpdatePayload` 同式）。 */
const props = defineProps<{ orderNo?: string; allowSplitPrice?: boolean }>()
const emit = defineEmits<{ saved: [message: string] }>()
const isEdit = computed(() => (props.orderNo ?? '') !== '')

/* ── 文案常量（⛔ 逐字对齐 island 页 `order-addorder.ts` 同名导出；出处见各条）──────── */

/** island `SAVE_SUCCESS_MESSAGE`（业务方口径：小提示、不弹单号、不跳转）。 */
const SAVE_SUCCESS_MESSAGE = '录单成功'

/** ⭐ 业务方「录单成功的反馈更明显一点 反馈太弱了」：小 toast 换成屏中大浮层
 * （青绿圆勾 + 「录单成功」），~1.8s 自散；「不弹单号不跳转」保留、提示形态升级。 */
const saveFlash = ref(false)
let saveFlashTimer = 0
function flashSaveSuccess(): void {
  saveFlash.value = true
  if (saveFlashTimer !== 0) window.clearTimeout(saveFlashTimer)
  saveFlashTimer = window.setTimeout(() => {
    saveFlash.value = false
    saveFlashTimer = 0
  }, 1800)
}
// 卸载清理：saveFlash 计时器统一收口（toast/fieldTip 自清型不在此列）。
onUnmounted(() => {
  if (saveFlashTimer !== 0) window.clearTimeout(saveFlashTimer)
})
/** island `PASTE_HINT_LABEL_SUFFIX`：缺解析权限时从 label 摘掉的那句承诺。 */
const PASTE_HINT_LABEL_SUFFIX = '[会自动识别订单各项内容]'
/** 证据件 `#dlcontent` 的 placeholder（对标原文，缺解析权限时摘掉）。 */
const PASTE_PLACEHOLDER =
  '可以把已经格式化好的订单文本内容直接粘贴到这里，然后用鼠标点一下框框外面任何地方，即可自动识别出订单各个内容'
/** 证据件 `#project` 的 placeholder（对标原文）。 */
const PROJECT_PLACEHOLDER =
  '可选择上面的项目，也可以直接在此输入项目，若有多个项目，请用/号分隔，最终代练/陪玩项目以这里填写的为准...'
/** 证据件 `#souresOrderID` 的 placeholder（对标原文）。 */
const SOURCE_ORDER_PLACEHOLDER = '若有多个请用逗号分隔，可空'
/** 常用项目卡头的设置链接（对标 `lay-tips`，逐字）。 */
const PROJECT_SETTING_TIP = '进入常用代练项目设置'
/** 粘贴区设置链接（对标 `lay-tips`，逐字）。 */
const AUTODISCERN_TIP = '订单内容里有识别不了的内容，可以进入这里进行关键词设置'
/** 项目分类 label 的对标 `lay-tips`（逐字）。 */
const CATEGORY_TIP = '可进入游戏维护-->订单项目分类管理中维护这里的'
/** 余额扣款的对标 `lay-tips`（逐字）。 */
const VIP_DED_TIP = '勾选此项后将从会员余额扣钱，同时累计消费并产生消费明细'
/** 内部派单按钮的对标 `title`（逐字）。 */
const ASSIGN_BUTTON_TIP = '将订单快速指派给指定接手人或抛单至内部接单大厅里让大家抢单'
/** island `THROW_FEE_TIP_HALL`（内部抢单档专属）。 */
const THROW_FEE_TIP_HALL =
  '发内部抢单的单价，必须大于 0。填 0 或留空点「录单」会被拦下，订单也不会建立。'
/** island `KSZP_LAYER_TITLE`（对标 addorder.js L1558 逐字）。 */
const KSZP_DIALOG_TITLE = '快速指派接手人'
/** island `HALL_ASSIGNEE_LABEL`（对标 L1490 逐字）。 */
const HALL_ASSIGNEE_LABEL = '内部抢单'
/** island `HALL_PRICE_REQUIRED_MESSAGE`（业务方定稿文案）。 */
const HALL_PRICE_REQUIRED_MESSAGE = '内部抢单请先填发单价（必须大于 0）'
/** island `HALL_RADIO_VALUE`：内部抢单 radio 的纯前端档位值（⛔ 不抄对标的 999999）。 */
const HALL_RADIO_VALUE = 'hall'
/** island `ASSIGNEE_OPTIONS_LIMIT` / `SALES_AGENT_OPTIONS_LIMIT`（两个端点的默认 limit）。 */
const ASSIGNEE_OPTIONS_LIMIT = 200
const SALES_AGENT_OPTIONS_LIMIT = 200
/** island `SUBMIT_OUTCOME_UNKNOWN_MESSAGE` / `RESUBMIT_CONFIRM_MESSAGE`（评审 Important-1）。 */
const SUBMIT_OUTCOME_UNKNOWN_MESSAGE =
  '提交结果未知（服务器错误）：可能已经建单，请先到订单列表确认是否已建，再决定要不要重填或重新提交'
const RESUBMIT_CONFIRM_MESSAGE =
  '上一次提交的结果未知（服务器错误），可能已经建单。请先到订单列表确认是否已建——确认要再提交一次吗？'
const RESUBMIT_DIALOG_TITLE = '上一次提交的结果未知'
/** island `SPEAKING_READ_ORDER_CODES`：读平台订单失败时要把后端 msg 弹出来的两个码。 */
const SPEAKING_READ_ORDER_CODES: ReadonlySet<number> = new Set([1015, 1028])
/** island `CREATE_PERMISSION_CODE` / `PARSE_PERMISSION_CODE`（create 模式的单点门）。 */
const CREATE_PERMISSION_CODE = 'order.add_edit'
const PARSE_PERMISSION_CODE = 'order.add_edit'
/** island `SALES_AGENT_PERMISSION_CODE`（销售客服格的编辑门）。 */
const SALES_AGENT_PERMISSION_CODE = 'order.allow_change_sales_agent'
/** island `INTAKE_SALES_AGENT_SELF_HINT`（新建态无权限档的下半句）。 */
const INTAKE_SALES_AGENT_SELF_HINT = '保存后归你自己'
/** island `ORDER_CREATED_ASSIGN_FAILED` / `ORDER_CREATED_THROW_FAILED`（码表唯一出处）。 */
const ORDER_CREATED_ASSIGN_FAILED: number = ErrorCode.ORDER_CREATED_ASSIGN_FAILED
const ORDER_CREATED_THROW_FAILED: number = ErrorCode.ORDER_CREATED_THROW_FAILED
/** 证据件各输入框的 placeholder（对标原文，逐字）。 */
const GAME_ACC_PLACEHOLDER = '请输入登录账号，可空'
const GAME_PWD_PLACEHOLDER = '请输入登录密码，可空'
const GAME_ZACC_PLACEHOLDER = '若无请留空'
const GAME_ROLE_PLACEHOLDER = '请输入角色名，没有请留空'
const MARK_PLACEHOLDER = '若无请留空'
const LEAVE_EMPTY_PLACEHOLDER = '可留空'
const CUSTOMER_NAME_PLACEHOLDER = '会员号或老板名'
const REMARK_PLACEHOLDER = '可写重要的备注内容'
const TAG_PLACEHOLDER = '可自定义标识，可空'
const THROW_FEE_PLACEHOLDER = '抛单价'
const TOTAL_PRICE_PLACEHOLDER = '请输入接单价'
const RECEIVED_PLACEHOLDER = '实际收款额'
const PLATFORM_PRICE_PLACEHOLDER = '发平台的价格'
const SERVICE_FEE_PLACEHOLDER = '请输入产生的手续费'
const KSZP_SEARCH_PLACEHOLDER = '用户名/名称/标记'
/** 新老客三段控件（「旺旺/YY」输入框左侧）：空串 = 不选（自动单走自动判定）。 */
const CUSTOMER_KIND_OPTIONS = [
  { value: '', label: '不选' },
  { value: 'new', label: '新' },
  { value: 'old', label: '老' },
] as const
/** 项目分类 / 常用项目为空时的引导句（对标该区运行期注入前是空的；本页给一句引导）。 */
const EMPTY_CATEGORY_HINT = '请先选游戏与项目分类'
/** 选齐了游戏与分类、取数落定后确实没有 ⇒ 如实说没有（文案同管理页逐字，旧页 game-projectsetting L816）。 */
const PROJECT_EMPTY_TEXT = '这个分类下还没有常用项目'

/* ── 表单模型（island `AddOrderForm` 同型；钱面默认 '0' 来自证据件 value="0"）──────── */

/** 本页表单 = 共享 `OrderForm` + 录单页自己的三段（`order_type` / 天 / 小时）。⚠️ **金额默认 '0' 不是空串**：island 页的模型从 DOM 起家（证据件 6 个金额框`value="0"`，`readFormState` 起手读 DOM），载荷里是 `'0'` 不是缺键。本页是受控表单，在模型里给同样的初值（`emptyAddForm`），保证最小保存路径的载荷与 island 逐键相同。⚠️ `hall_price` 由「给打手发单价」（#throwFee）承担——island `FIELD_MAP` 里`throwInterPrice`（隐藏格）与 `throwFee` 同写 `hall_price`、遍历顺序让后者赢；受控表单里等价实现 = 隐藏格只落本地 ref（`interPrice`），**不进载荷**（赢家的值就是`form.hall_price`，语义与 island 逐字一致）。
 * 本页表单 = 共享 `OrderForm` + 录单页自己的三段（`order_type` / 天 / 小时）。⚠️ **金额默认 '0' 不是空串**：island 页的模型从 DOM 起家（证据件 6 个金额框`value="0"`，`readFormState` 起手读 DOM），载荷里是 `'0'` 不是缺键。本页是受控表单，在模型里给同样的初值（`emptyAddForm`），保证最小保存路径的载荷与 island 逐键相同。⚠️ `hall_price` 由「给打手发单价」（#throwFee）承担——island `FIELD_MAP` 里`throwInterPrice`（隐藏格）与 `throwFee` 同写 `hall_price`、遍历顺序让后者赢；受控表单里等价实现 = 隐藏格只落本地 ref（`interPrice`），**不进载荷**（赢家的值就是`form.hall_price`，语义与 island 逐字一致）。 */
type AddForm = OrderForm & { order_type: 'BOOST' | 'COMPANION' }

function emptyAddForm(): AddForm {
  return {
    ...emptyOrderForm(),
    order_type: 'BOOST',
    total_price: '0',
    received_price: '0',
    platform_price: '0',
    hall_price: '0',
    fee: '0',
  }
}

const form = reactive<AddForm>(emptyAddForm())
/** 新建默认 30 天、小时 0。保存成功后回到这对。 */
const day = ref('30')
const hours = ref('0')
/** 编辑态：时间要求初值 + 「用户动过天/小时」标志（没动过就按后端原值回，认不出的形状不静默改写）。 */
const editTimeOriginal = ref('')
let editTimeDirty = false
watch([day, hours], () => { editTimeDirty = true })
/** 是否加急（证据件 `jiaji` radio：N 默认勾中；Y → is_urgent=true）。 */
const jiaji = ref('N')
/* island 同款同步（order-addorder.ts:1073）：radio 只驱动 jiaji，读表单时才落 form——
 * v2 直接双向落（漏了它 ⇒ formToPayload 恒发 is_urgent:false）。 */
watch(jiaji, (v) => { form.is_urgent = v === 'Y' })
/** 按代练项目拆分订单（勾上 ⇒ 载荷 `split: true`）。 */
const splitChecked = ref(false)
/** 新老客标签的手动覆盖（`''` = 不选）：只在选了新/老时随建单载荷发 `customer_kind_override`。 */
const customerKindOverride = ref<'' | 'new' | 'old'>('')
type IntakeSplitOrder = NonNullable<CreateOrderPayload['split_orders']>[number]
const splitOrders = ref<(IntakeSplitOrder & { key: number })[]>([])
let nextSplitOrderKey = 0

function addSplitOrder(): void {
  splitOrders.value.push({ key: nextSplitOrderKey++, project_text: '', total_price: '' })
}

function centsForPreview(value: string): number | null {
  if (isBlankLikePython(value) || !validateMoneyInput(value).ok) return null
  return Math.round(Number(trimLikePython(value)) * 100)
}

const splitRemainingPrice = computed(() => {
  const total = centsForPreview(form.total_price)
  if (total === null) return null
  let remaining = total
  for (const order of splitOrders.value) {
    const price = centsForPreview(order.total_price)
    if (price === null) return null
    remaining -= price
  }
  return formatMoney(remaining / 100)
})
/** 「默认指派此人」（island `defAss`；四态判据读它）。 */
const defAssChecked = ref(false)
/** 隐藏格「发内部抢单大厅价」（`#throwInterPrice`，value="0"；不进载荷，见 `AddForm` 注）。 */
const interPrice = ref('0')
/** 「尚未开放」文本框的死值（登录密码/暗号口令/会员号/两保证金——blur 非空提示用）。 */
const deadForm = reactive<Record<string, string>>({
  gamePwd: '',
  loginKey: '',
  customerName: '',
  safeAmount: '',
  effAmount: '',
  delaySec: '',
})
/** 隐藏容器 / 尚未开放壳里的 inert 控件值（密保/延迟/支付方式/快速发单配置/三颗 xm-select）。 */
const deadSelect = ref('')
const safeKeyType = ref('20')
const isDelay = ref('N')
const payMethod = ref('0')
const pushTemplate = ref('0')
const vipDedCheck = ref(false)

/* ── toast（页面内自绘；ElMessage 拿不到 .ttv2 皮）与字段旁瞬时提示 ──────────────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

/** info 停 1500ms、error 停 6000ms（V2MyOrders 同一口径：错误要看清）。 */
function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/** 字段旁瞬时提示（island `layer.tips` 的炭火载体：金额焦点提示 4s / 发单价 tip 9s）。 */
const fieldTip = ref<{ anchor: string; text: string } | null>(null)
let fieldTipTimer: ReturnType<typeof setTimeout> | null = null

function showFieldTip(anchor: string, text: string, ms: number): void {
  if (fieldTipTimer !== null) clearTimeout(fieldTipTimer)
  fieldTip.value = { anchor, text }
  fieldTipTimer = setTimeout(() => {
    fieldTip.value = null
    fieldTipTimer = null
  }, ms)
}

function clearFieldTip(): void {
  if (fieldTipTimer !== null) clearTimeout(fieldTipTimer)
  fieldTip.value = null
  fieldTipTimer = null
}

/* ── 权限门（fail-closed；`session.canNode` 与 island `canNode` 同口径）────────── */

const canSaveOrder = computed(() => session.canNode(
  isEdit.value ? [CREATE_PERMISSION_CODE, 'order.submit_edit'] : [CREATE_PERMISSION_CODE],
))
const canParsePaste = computed(() => session.canNode([PARSE_PERMISSION_CODE]))
const canPublishToHall = computed(() => session.canNode(['order.send_to_grab_hall']))
const canChangeSalesAgent = computed(() => session.canNode([SALES_AGENT_PERMISSION_CODE]))

/* ── 字典（游戏/区服/分类/常用项目/订单来源）与级联 ───────────────────────────── */

const games = ref<ConfigRow[]>([])
const servers = ref<ServerRow[]>([])
const categories = ref<CategoryRow[]>([])
const projects = ref<ProjectRow[]>([])
/** 来源字典（运行期可变：自动判定可能当场建一个新来源，`ensureOrderSourceOption` 补进来）。 */
const sourceOptions = ref<{ id: number; name: string }[]>([])
/** 「当前来源不是客服指定的」（island `orderSourceIsAuto`：默认第一条/自动判定回填 ⇒ true）。 */
const orderSourceIsAuto = ref(true)
/** 「客服自己点过游戏下拉」（island `userPickedGame`：唯一置位点 = 用户改游戏下拉）。 */
const userPickedGame = ref(false)
/** ⭐ 2026-11-10 客户线：最近一次「读平台订单」暂存的买家身份（闲管家 `buyer_eid`，隐藏透传，页面无控件）。
 * **每次读单先清**（换单号不拿上一单的身份），读成功且响应带非空 `buyer_eid` 才落（阿奇索腿无此键）；
 * `resetAfterSave()` 再清一次。新建保存随载荷回传（后端建户并连单）；本页没有编辑态。判据 = V2OrderNew.spec。 */
const pendingBuyer = ref<{ eid: string; nick: string } | null>(null)

/** 勾选中的配置项目主键（恰好一项 ⇒ 载荷带 `project` FK；多项/零项 ⇒ null，文本是权威值）。 */
const checkedProjectIds = ref<number[]>([])

/** 勾选差分 → 文本拼/删（共享 `toggleProjectName`，对标 addorder.js L1390-1403 的算法）。⚠️ 与 island `toggleProjectText` 同口径：**拼/删本身没有 700 字节闸**——island 只在`onSave` 里拦（`projectSyncBad`）。上一版的「超闸弹回」是本页自创，已删（保真）。用 watcher 而不是每颗 checkbox 的 @change：EP 的 group v-model 与子项事件先后不定，差分只认「数组前后变了哪一个」，天然免竞态。
 * 勾选差分 → 文本拼/删（共享 `toggleProjectName`，对标 addorder.js L1390-1403 的算法）。⚠️ 与 island `toggleProjectText` 同口径：**拼/删本身没有 700 字节闸**——island 只在`onSave` 里拦（`projectSyncBad`）。上一版的「超闸弹回」是本页自创，已删（保真）。用 watcher 而不是每颗 checkbox 的 @change：EP 的 group v-model 与子项事件先后不定，差分只认「数组前后变了哪一个」，天然免竞态。 */
watch(checkedProjectIds, (now, before) => {
  const added = now.find((id) => !before.includes(id))
  const removed = before.find((id) => !now.includes(id))
  if (added === undefined && removed === undefined) return
  const changedId = added ?? removed
  const name = projects.value.find((p) => p.id === changedId)?.name ?? ''
  // 级联清空（切游戏/切分类）也走这里：那时勾选被程序清空、项目已不在当前列表里
  // （name === ''）⇒ 与 island 同口径**不动文本**（island 是把 checkbox 区 innerHTML 清掉，
  // 权威文本框不参与）；只有真人勾/取消才拼/删。
  if (name === '') return
  form.project_text = toggleProjectName(form.project_text, name, added !== undefined)
})

/** 切游戏：清空区服/分类/常用项目并重拉前两者（island `onGameChange` 同一道清空语义）。 */
async function onGameChange(gameId: number, selectCategory = true): Promise<void> {
  form.server = null
  form.category = null
  form.project = null
  checkedProjectIds.value = []
  projects.value = []
  categories.value = []
  servers.value = []
  let nextServers: ServerRow[] = []
  let nextCategories: CategoryRow[] = []
  try {
    ;[nextServers, nextCategories] = await Promise.all([
      fetchServers(gameId),
      fetchCategories(gameId),
    ])
  } catch (e) {
    showToast(errText(e) || '配置加载失败', 'error')
    return
  }
  // 连点两次游戏时，先发的慢响应不能覆盖后发的（island `onGameChange` 同一道守卫）。
  if (form.game !== gameId) return
  servers.value = nextServers
  categories.value = nextCategories
  preselectFirstServer()
  if (selectCategory && nextCategories.length > 0) {
    form.category = nextCategories[0]!.id
    await onCategoryChange()
  }
}

/** ⭐ 业务方指令（island 页无此行为，是有意偏离）：游戏选定（手动或识别回填）后「把对应游戏的区服平台抄一份过来，然后默认自动选第一个」。后端游戏字典（island 同款）只有**区服一级真数据**——平台/服/职业/阵营在 v2 是「尚未开放」壳、无字典可预选 ⇒ 预选落在区服一级。优先级 = **识别回填值 > 自动预选**：粘贴解析/读单路径随后按名字落位时会覆盖预选值（`onPasteBlur` 的显式落位在 `onGameChange` 之后跑），本函数只在当前**没有选中值**时挑第一项；用户手改之后不被覆盖（预选只挂在游戏切换这一件事上）。
 * ⭐ 业务方指令（island 页无此行为，是有意偏离）：游戏选定（手动或识别回填）后「把对应游戏的区服平台抄一份过来，然后默认自动选第一个」。后端游戏字典（island 同款）只有**区服一级真数据**——平台/服/职业/阵营在 v2 是「尚未开放」壳、无字典可预选 ⇒ 预选落在区服一级。优先级 = **识别回填值 > 自动预选**：粘贴解析/读单路径随后按名字落位时会覆盖预选值（`onPasteBlur` 的显式落位在 `onGameChange` 之后跑），本函数只在当前**没有选中值**时挑第一项；用户手改之后不被覆盖（预选只挂在游戏切换这一件事上）。 */
function preselectFirstServer(): void {
  if (form.server !== null) return
  const first = servers.value[0]
  if (first !== undefined) form.server = first.id
}

/** 常用项目取数的在途标记（值 = 发起时的分类 id；旧一轮回来发现已被新一轮覆盖就不再动它）与失败标记。 */
const projectsInflight = ref<number | null>(null)
const projectsFailed = ref(false)

/** 常用项目区为空时的引导句：未选齐游戏/分类给引导；选齐且取数落定仍空 ⇒ 如实说没有；
 * 加载中不给句、失败不给句（错误已走 toast，失败不能冒充「还没有」）。 */
const projectHint = computed(() => {
  if (projects.value.length > 0) return ''
  if (form.game === null || form.category === null) return EMPTY_CATEGORY_HINT
  if (projectsInflight.value !== null || projectsFailed.value) return ''
  return PROJECT_EMPTY_TEXT
})

/** 切分类：重建常用项目、清空勾选（island `onCategoryChange`；文本是权威值，不被动）。 */
async function onCategoryChange(): Promise<void> {
  const categoryId = form.category
  if (categoryId === null) return
  form.project = null
  checkedProjectIds.value = []
  projects.value = []
  projectsInflight.value = categoryId
  projectsFailed.value = false
  let nextProjects: ProjectRow[] = []
  try {
    nextProjects = await fetchProjects(categoryId)
  } catch (e) {
    projectsFailed.value = true
    showToast(errText(e) || '项目列表加载失败', 'error')
    return
  } finally {
    if (projectsInflight.value === categoryId) projectsInflight.value = null
  }
  // 与 `onGameChange` 逐字同一谓词：响应回来时选中的**还是**这个分类才落盘。
  if (form.category !== categoryId) return
  projects.value = nextProjects
}

/** 项目分类 radio（用户点选的唯一入口；v-model 不直绑，清空值不能混进 FK）。 */
function onCategoryPick(val: unknown): void {
  form.category = pickId(val)
  void onCategoryChange()
}

/** EP 下拉/单选的值 → 数字 id（'' 与其它非数一律归 null，⛔ 空串不能混进 FK）。 */
function pickId(val: unknown): number | null {
  if (typeof val === 'number' && Number.isFinite(val)) return val
  if (typeof val === 'string' && val !== '') {
    const n = Number(val)
    if (Number.isFinite(n)) return n
  }
  return null
}

/** 「点击常用项目设置」→ 常用项目/项目分类管理 v2 页（routeName 集中登记在 v2Menu）。 */
function openProjectSettings(): void {
  void router.push({ name: V2_GAME_PROJECTS })
}

/** 用户点游戏下拉（island `userPickedGame` 的唯一置位点 + 级联重建）。 */
let gameServerPickVersion = 0

function onUserPickGame(val: unknown): void {
  gameServerPickVersion++
  userPickedGame.value = true
  const id = pickId(val)
  if (id === null) return
  form.game = id
  void onGameChange(id)
}

/** 区服下拉（清空 ⇒ null）。 */
function onServerPick(val: unknown): void {
  gameServerPickVersion++
  form.server = pickId(val)
}

/** 游戏名含「魔兽世界」才露阵营门派格（island 照 addorder.js L693-698 的隐藏）。 */
const showGameGroup = computed(() => {
  const game = games.value.find((g) => g.id === form.game)
  return game !== undefined && game.name.includes('魔兽世界')
})

/* ── 订单类型的 4 个 label（对标 `showpw()` L793-829 那组，逐字）────────────────── */

const typeLabels = computed(() => {
  const companion = form.order_type === 'COMPANION'
  return {
    project: companion ? '陪玩内容/标题' : '代练项目/标题',
    throwFee: companion ? '发陪玩价格' : '给打手发单价',
    time: companion ? '陪玩时长' : '时间要求',
    role: companion ? '老板名称' : '角色名',
  }
})

/* ── 时间要求双下拉的选项（证据件 `#day` 0–365 / `#hours` 0–23 步 0.5，逐值）────── */

const DAY_OPTIONS = Array.from({ length: 366 }, (_, i) => String(i))
const HOUR_OPTIONS = Array.from({ length: 47 }, (_, i) => String(i * 0.5))

/** island `dayHoursToTimeRequirement`：合成 `N天M小时`（0 段省略；两段都 0 = 空串 = 不发）。 */
function dayHoursToTimeRequirement(dayValue: string, hoursValue: string): string {
  const parts: string[] = []
  if (Number(dayValue) > 0) parts.push(`${Number(dayValue)}天`)
  if (Number(hoursValue) > 0) parts.push(`${Number(hoursValue)}小时`)
  return parts.join('')
}

/* ── 校验链（island `onSave` 同序同文案；各段都是带出处的本地重写）──────────────── */

/** 共用两态的必填（island `requiredError` 同源）：游戏 + 项目文本（Python strip 判空）。 */
function requiredError(): string {
  if (form.game === null) return '请选择游戏'
  if (isBlankLikePython(form.project_text)) return '代练/陪玩项目不可为空'
  return ''
}

/** 录单（新建）态第三条：订单总价必填（island `totalPriceRequiredError`，D-0082）。 */
function totalPriceRequiredError(): string {
  if (isBlankLikePython(form.total_price)) return '订单总价不可为空'
  return ''
}

/** 第一个金额错误（顺序 = `ORDER_MONEY_FIELDS`；island `firstMoneyError` 同源）。 */
function firstMoneyError(): string {
  for (const f of ORDER_MONEY_FIELDS) {
    const value = (form as unknown as Record<string, string>)[f] ?? ''
    const result = validateMoneyInput(value)
    if (!result.ok) return result.msg
  }
  return ''
}

function splitOrdersError(): string {
  if (!splitChecked.value || splitOrders.value.length === 0) return ''
  for (const [index, order] of splitOrders.value.entries()) {
    const label = `第 ${index + 1} 笔子单`
    if (isBlankLikePython(order.project_text)) return `${label}的项目内容不可为空`
    const contentError = projectTextSyncLimitError(order.project_text)
    if (contentError !== '') return `${label}：${contentError}`
    if (isBlankLikePython(order.total_price)) return `${label}的接单价不可为空`
    const price = validateMoneyInput(order.total_price)
    if (!price.ok) return `${label}的接单价：${price.msg}`
  }
  const remainder = splitRemainingPrice.value
  if (remainder !== null && !validateMoneyInput(remainder).ok) return '原单剩余接单价超出范围'
  return ''
}

/** island `isUnknownSubmitOutcome`：只有「结果未知」才需要防重复提交（⛔ 不算业务失败/4xx）。 */
function isUnknownSubmitOutcome(error: unknown): boolean {
  if (error instanceof NetworkError) return true
  return error instanceof HttpError && error.status >= 500
}

/* ── 粘贴解析（island `onPasteBlur` 同构；落模型 = 共享 `applyParsedToForm`）────── */

/** 粘贴框失焦的守卫（缺 `order.add_edit` 时 island **根本不注册**那个监听器、并摘掉「会自动识别」的承诺——这里等价实现 = 容器上的 capture 监听器先判权限再动）。⚠️ 绑在容器（`@blur.capture`）而不是 ElInput 上：EP 的 @blur 是「fallthrough 原生监听+ 组件 emit」双通道，直绑会一blur跑两次解析（两发 `/orders/parse/`）。
 * 粘贴框失焦的守卫（缺 `order.add_edit` 时 island **根本不注册**那个监听器、并摘掉「会自动识别」的承诺——这里等价实现 = 容器上的 capture 监听器先判权限再动）。⚠️ 绑在容器（`@blur.capture`）而不是 ElInput 上：EP 的 @blur 是「fallthrough 原生监听+ 组件 emit」双通道，直绑会一blur跑两次解析（两发 `/orders/parse/`）。 */
function onPasteBlurGuarded(): void {
  if (canParsePaste.value) void onPasteBlur()
}

async function onPasteBlur(): Promise<void> {
  const text = form.content
  if (text.trim() === '') return
  let fields: ParsedFields
  let unmatched: string[]
  try {
    const result = await parsePastedText(text)
    fields = result.fields
    unmatched = result.unmatched
  } catch (e) {
    // 「解析不出来」不是错误，是常态；只有传输层 / 信封层失败才提示。
    showToast(errText(e) || '识别失败', 'error')
    return
  }
  const gameBefore = form.game
  // 接入点 B（D-0178）：解析结果没有显式「游戏：X」时按整段粘贴原文认一次游戏；
  // 客服接管了（userPickedGame）⇒ 连比对都不做；多款 ⇒ 拼进既有的 parts（⛔ 不能单独弹）。
  let ambiguousGameMessage = ''
  if (fields.game_name === undefined && !userPickedGame.value) {
    const r = detectGameFromText(text, games.value)
    if (r.kind === 'hit') {
      fields.game_name = r.game.name
    } else if (r.kind === 'ambiguous') {
      ambiguousGameMessage = `粘贴内容里出现了多款游戏（${r.names.join('、')}），请手动选择游戏`
    }
  }
  const dicts: OptionDicts = {
    games: games.value.map((g) => ({ id: g.id, name: g.name })),
    servers: servers.value.map((s) => ({ id: s.id, name: s.name })),
    categories: [],
    projects: [],
    orderSources: sourceOptions.value,
  }
  const { filled, unresolved } = applyParsedToForm(form, fields, dicts)
  // 触发点 ③（对标 L1367-1370）：粘贴回填出来源单号之后读一次平台订单。
  if (filled.includes('source_order_no')) void readPlatformOrderIntoForm()
  const pastedServer = fields.server_name
  if (form.game !== gameBefore && form.game !== null) {
    // 粘贴同时改了游戏：上面那次区服解析用的是上一个游戏的列表，必须作废并重解析
    // （island I1 裁决：不重来区服会静默丢失）。
    const gameId = form.game
    const pickVersion = gameServerPickVersion
    await onGameChange(gameId)
    const filteredUnresolved = unresolved.filter((u) => u !== `区服「${pastedServer}」`)
    const filteredFilled = filled.filter((f) => f !== 'server')
    unresolved.length = 0
    unresolved.push(...filteredUnresolved)
    filled.length = 0
    filled.push(...filteredFilled)
    if (pastedServer !== undefined && form.game === gameId && gameServerPickVersion === pickVersion) {
      const hit = servers.value.find((s) => s.name === pastedServer)
      if (hit) {
        form.server = hit.id
        filled.push('server')
      } else {
        // 名字确实不在新游戏的列表里 → 提示留着，让客服手动选（⛔ 不静默）。
        unresolved.push(`区服「${pastedServer}」`)
      }
    }
  }
  const parts: string[] = []
  // 多款游戏那句排在最前（它是要客服动手的一句，island 同序）。
  if (ambiguousGameMessage !== '') parts.push(ambiguousGameMessage)
  if (filled.length > 0) parts.push(`已识别：${filled.join('、')}`)
  if (unmatched.length > 0) parts.push(`没认出来：${unmatched.join('、')}`)
  if (unresolved.length > 0) parts.push(`没能对应上：${unresolved.join('、')}`)
  if (parts.length > 0) showToast(parts.join('；'))
}

/* ── 读平台订单（island `readPlatformOrderIntoForm` 同构，对标 getTaobaoOrderInfo）── */

async function readPlatformOrderIntoForm(sourceOverride?: number | null): Promise<void> {
  const tid = form.source_order_no.trim()
  const sourceId =
    sourceOverride === undefined
      ? orderSourceIsAuto.value
        ? null
        : form.order_source
      : sourceOverride
  // 两条独立的返回条件（island 同构）：单号太短一次请求都不发（对标 L1666）；
  // 刚清空来源不发（自动判定会把刚清掉的来源又填回来）。
  if (tid.length < 15) return
  if (sourceOverride === null) return
  // ⭐ 客户线：买家身份随**这次读单**暂存（成功才落、失败/无身份即清——换单号不能拿上一单
  //    的身份；阿奇索腿无 `buyer_eid` 键）。⚠️ 残差：清掉后上一单回填的昵称/电话仍显示在
  //    表单（仅空才填）⇒ 界面像"还连着"，实际这一单不连任何客户（STATUS 残差有记）。
  pendingBuyer.value = null
  try {
    const fields = await readPlatformOrder(sourceId, tid)
    const buyerEid = (fields.buyer_eid ?? '').trim()
    if (buyerEid !== '') pendingBuyer.value = { eid: buyerEid, nick: fields.buyer_nick }
    // 价格仅当平台侧非空时写（空串是后端「上游没有价格」的合法契约；无条件写会清掉已填价）。
    if (fields.payment !== '') form.total_price = fields.payment
    // 昵称仅当「旺旺/YY」为空时填（请求飞行期间手填的不被覆盖）。
    if (form.customer_ww === '') form.customer_ww = fields.buyer_nick
    // 电话仅当平台侧非空。
    if (fields.customer_phone !== '') form.customer_phone = fields.customer_phone
    // 商品标题（`fields.remark`）不再回填需求：随单保存、打手可见（2026-11-05 业务方拍板）；
    // 标题仅用于下方按内容自动切游戏。
    // 回填来源下拉：自动判定可能刚建了一个来源 ⇒ 先补字典再选中（顺序不能反）。
    if (typeof fields.order_source === 'number') {
      ensureOrderSourceOption(fields.order_source, fields.order_source_name)
      form.order_source = fields.order_source
      // 没发来源的那一次，这个值确实是后端判出来的 ⇒ 保持"自动"；
      // 发过来源（客服选的）⇒ 不许被这次回填降级成"自动"（island `sentExplicitSource`）。
      orderSourceIsAuto.value = sourceId === null
    }
    // 接入点 A：全部回填之后按商品标题自动切游戏（⛔ 扫 remark，不是粘贴框的值）。
    if (sourceOverride === undefined) userPickedGame.value = false
    await autoSelectGameByContent(fields.remark, '商品内容')
  } catch (e) {
    // 静默降级只覆盖后端明确的业务失败（1015/1028 开口面 = `SPEAKING_READ_ORDER_CODES`）；
    // 其余错误不得消失（编程错误伪装成上游故障 = 监控失明，E-0010）。
    if (e instanceof BizError) {
      if (SPEAKING_READ_ORDER_CODES.has(e.code)) showToast(e.message, 'error')
    } else {
      console.error('[V2OrderNew] 读平台订单回填失败（非业务错误）', e)
    }
  }
}

/** 来源字典里补一项（自动判定当场建来源的那一档；已有的不动，按 id 判重）。 */
function ensureOrderSourceOption(id: number, name: string): void {
  if (sourceOptions.value.some((s) => s.id === id)) return
  sourceOptions.value = [...sourceOptions.value, { id, name: name || `#${id}` }]
}

/** island `autoSelectGameByContent`：按内容认游戏；平手不猜；客服接管了不插手。 */
async function autoSelectGameByContent(text: string, label: string): Promise<void> {
  if (userPickedGame.value) return
  const r = detectGameFromText(text, games.value)
  if (r.kind === 'none') return
  if (r.kind === 'ambiguous') {
    showToast(`${label}里出现了多款游戏（${r.names.join('、')}），请手动选择游戏`, 'error')
    return
  }
  if (form.game === r.game.id) return
  const reset = form.server !== null || form.category !== null
  form.game = r.game.id
  showToast(
    `已按${label}自动选择游戏「${r.game.name}」` +
      // island 原文是「（区服/分类已重置，请重新选择）」；级联自动预选上线后
      // 区服不再空着（预选第一项）⇒ 后半句按实际行为改写，⛔ 不对客服说错话。
      (reset ? '（已切换游戏，请核对区服和项目分类）' : ''),
  )
  await onGameChange(r.game.id)
}

/** 触发点 ②：来源下拉一变就用刚选的新来源值读一次（对标 L1977-1979 的 svalue 语义）。 */
function onUserPickSource(val: unknown): void {
  const picked = pickId(val)
  orderSourceIsAuto.value = picked === null
  form.order_source = picked
  void readPlatformOrderIntoForm(picked)
}

/** 销售客服 radio（选人即改 `salesAgentPicked`；清不出值就保持原选，不给"没人"档）。 */
function onSalesAgentPick(val: unknown): void {
  const id = pickId(val)
  if (id !== null) salesAgentPicked.value = id
}

/** 触发点 ①：来源单号改完（失焦/回车）读一次。 */
function onSourceOrderNoChange(): void {
  void readPlatformOrderIntoForm()
}

/* ── 快速指派（D-0179：选人 / 内部抢单 / 暂不指派 + 发单价 + 默认指派此人）────────── */

const assignDialogOpen = ref(false)
const assignButton = ref<HTMLButtonElement | null>(null)
const assignSearch = ref('')
const assignSearched = ref(false)
const candidates = ref<AssigneeOption[]>([])
const selectedAssignee = ref<AssigneeOption | null>(null)
const toGrabHall = ref(false)
/** 服务端记着的默认接手人（`0` = 没设）与档位（hall 优先，两列互斥）。 */
const initialDefaultAssigneeId = ref(0)
const initialDefAssIsHall = ref(false)
/** 复原只在第一次成功拉到名单的那一拉（island 修复轮裁决 1；之后的重拉只重画名单）。 */
let defAssPreferenceSynced = false
/** 发单价格的可见态与按钮文案（island `applyAssigneeSelection` 的三态产物）。 */
const throwFeeVisible = ref(false)
const saveAssVisible = ref(false)
const kspdText = ref('暂不指派')
const kspdActive = ref(false)
const throwFeeInputRef = ref<{ focus: () => void } | null>(null)

/** 三态：内部抢单露出价框且不改已填的价；选中某人藏起价框并把发单价收回 0，
 * 避免刚才填的大厅价留在载荷里；暂不指派同样写 0。 */
function applyAssigneeSelection(focusIt = false, clearDefAss = false): void {
  if (toGrabHall.value) {
    throwFeeVisible.value = true
    kspdText.value = HALL_ASSIGNEE_LABEL
    kspdActive.value = true
    saveAssVisible.value = false
    if (focusIt) {
      throwFeeInputRef.value?.focus()
      showFieldTip('hall_price', THROW_FEE_TIP_HALL, 9000)
    }
  } else if (selectedAssignee.value !== null) {
    form.hall_price = '0'
    throwFeeVisible.value = false
    saveAssVisible.value = false
    kspdText.value = selectedAssignee.value.name
    kspdActive.value = true
    clearFieldTip()
    void focusIt
  } else {
    throwFeeVisible.value = false
    form.hall_price = '0'
    saveAssVisible.value = false
    kspdText.value = '暂不指派'
    kspdActive.value = false
    if (clearDefAss) defAssChecked.value = false
    clearFieldTip()
  }
}

/** island `loadCandidates`：失败如实提示不静默；默认档位复原只在首屏那一次。 */
async function loadCandidates(key?: string): Promise<void> {
  try {
    const page = await fetchAssigneeOptions({
      limit: ASSIGNEE_OPTIONS_LIMIT,
      ...(key === undefined || key === '' ? {} : { key }),
    })
    candidates.value = page.items
    // 被截断 ⇒ 名单外的选不到，必须把这件事说出来（与 island 同口径）。
    if (page.truncated) {
      showToast(`可指派接手人超过 ${ASSIGNEE_OPTIONS_LIMIT} 位，已截断：可用搜索缩小范围`, 'error')
    }
    initialDefaultAssigneeId.value = Number(page.defAssRecManUid ?? 0)
    // `=== true` 刻意（旧后端可能整个缺键 ⇒ 落 false；不许把 undefined 当 true）。
    initialDefAssIsHall.value = canPublishToHall.value && page.defAssIsHall === true
    if (!defAssPreferenceSynced) {
      defAssPreferenceSynced = true
      if (initialDefAssIsHall.value) {
        toGrabHall.value = true
        defAssChecked.value = true
        selectedAssignee.value = null
      } else if (initialDefaultAssigneeId.value > 0) {
        defAssChecked.value = true
        // 默认接手人不在名单里 ⇒ 不假装选中（按钮仍是「暂不指派」，勾选框保持服务端事实）。
        selectedAssignee.value =
          candidates.value.find((row) => row.id === initialDefaultAssigneeId.value) ?? null
      }
    }
    applyAssigneeSelection()
  } catch (e) {
    showToast(errText(e) || '可指派接手人加载失败', 'error')
  }
}

/** 点「内部派单」按钮 ⇒ 开层；开着搜索词时先清掉并重拉一次名单（对标 L1547-1550）。 */
function openAssignDialog(): void {
  assignSearched.value = false
  if (assignSearch.value !== '') {
    assignSearch.value = ''
    void loadCandidates()
  }
  assignDialogOpen.value = true
}

/** 点搜索结果里的人 ⇒ 立即关层。 */
function chooseAssignee(value: string): void {
  assignDialogOpen.value = false
  if (value === HALL_RADIO_VALUE) {
    if (!canPublishToHall.value) return
    toGrabHall.value = true
    // 档位互斥：内部抢单时没有人被选中（提交时也就不会带 `assignee`）。
    selectedAssignee.value = null
    applyAssigneeSelection(true)
    return
  }
  toGrabHall.value = false
  const id = Number(value)
  if (!Number.isFinite(id) || id === 0) {
    selectedAssignee.value = null
    // 用户选「暂不指派」⇒ 照对标 L1150 把「默认指派此人」也取消勾选。
    applyAssigneeSelection(false, true)
    return
  }
  selectedAssignee.value =
    candidates.value.find((row) => row.id === id) ??
    { id, name: '', role: null, tag: '', remark: '' }
  applyAssigneeSelection(true)
}

async function clearPickedAssignee(): Promise<void> {
  chooseAssignee('0')
  await nextTick()
  assignButton.value?.focus()
}

function searchCandidates(): void {
  assignSearched.value = true
  void loadCandidates(assignSearch.value)
}

function resetCandidateSearch(): void {
  assignSearch.value = ''
  assignSearched.value = false
  void loadCandidates()
}

/** island `syncLocalDefaultPreference`：把本地默认档位对齐到服务端事实（三档）。 */
function syncLocalDefaultPreference(
  remember: 'Y' | 'N' | 'hall' | undefined,
  takerId: number | null,
): void {
  if (remember === 'hall') {
    initialDefAssIsHall.value = true
    initialDefaultAssigneeId.value = 0
    return
  }
  if (remember === 'Y') {
    initialDefAssIsHall.value = false
    if (takerId !== null) initialDefaultAssigneeId.value = takerId
    return
  }
  if (remember === 'N') {
    initialDefAssIsHall.value = false
    initialDefaultAssigneeId.value = 0
  }
}

/* ── 接单销售客服（island `feat/sales-agent` 那一格：候选 radio 网格 + 两档口径）────── */

const salesAgentItems = ref<{ id: number; name: string }[]>([])
const salesAgentPicked = ref<number | null>(null)
const salesAgentDialogOpen = ref(false)
const salesAgentOptionsLoading = ref(false)
const editOrderLoaded = ref(false)
let editSalesAgentId: number | null = null
const viewerId = session.user?.id ?? null
const viewerName = session.user ? session.user.display_name || session.user.username || '' : ''

/** 新建时默认选当前用户，编辑时保留详情回传的账号。 */
function renderSalesAgentOptions(): void {
  if (!canChangeSalesAgent.value) {
    salesAgentItems.value = []
    salesAgentPicked.value = null
    return
  }
  const items = [...salesAgentItems.value]
  const selfIn = viewerId !== null && items.some((o) => o.id === viewerId)
  if (viewerId !== null && viewerName !== '' && !selfIn) {
    items.unshift({ id: viewerId, name: viewerName })
  }
  salesAgentItems.value = items
  salesAgentPicked.value = items.find((o) => o.id === viewerId)?.id ?? null
}

/** 无权限档的只读文案（拿不到登录名时只有下半句；⛔ 不编名字）。 */
const salesAgentReadonlyText = computed(() =>
  isEdit.value ? form.sales_agent || '未设置' : viewerName === ''
    ? INTAKE_SALES_AGENT_SELF_HINT
    : `${viewerName}（${INTAKE_SALES_AGENT_SELF_HINT}）`,
)

/** 读那一格当前选中的主键 + 名字（一次读，别分两处；无权限 ⇒ 双空 = 两条提交路都不发）。 */
function readSalesAgentChoice(): { id: number | null; name: string } {
  if (!canChangeSalesAgent.value) return { id: null, name: '' }
  const id = salesAgentPicked.value
  const name = salesAgentItems.value.find((o) => o.id === id)?.name ?? ''
  return { id: id !== null && id > 0 ? id : null, name }
}

const selectedSalesAgentName = computed(() => {
  const { id, name } = readSalesAgentChoice()
  if (!isEdit.value) return name || viewerName || '未选择'
  if (id === editSalesAgentId) return form.sales_agent || name || '未设置'
  return name || '未设置'
})

const salesAgentOptionNames = computed(() => {
  const counts = new Map<string, number>()
  for (const item of salesAgentItems.value) counts.set(item.name, (counts.get(item.name) ?? 0) + 1)
  return new Map(salesAgentItems.value.map(({ id, name }) => [
    id, isEdit.value && (counts.get(name) ?? 0) > 1 ? `${name}（账号 ID ${id}）` : name,
  ]))
})

async function loadSalesAgentOptions(): Promise<boolean> {
  salesAgentOptionsLoading.value = true
  try {
    const kefu = await fetchKefuOptions({ limit: SALES_AGENT_OPTIONS_LIMIT })
    salesAgentItems.value = (kefu.items ?? []).map((r) => ({ id: r.id, name: r.name }))
    if (kefu.truncated === true) {
      showToast(`可作为销售客服的人超过 ${SALES_AGENT_OPTIONS_LIMIT} 位，已截断`, 'error')
    }
    return true
  } catch (error) {
    if (isEdit.value) showToast(`销售客服名单加载失败，${errText(error)}`, 'error')
    return false
  } finally {
    salesAgentOptionsLoading.value = false
  }
}

async function openSalesAgentDialog(): Promise<void> {
  if (isEdit.value && salesAgentItems.value.length === 0 && !await loadSalesAgentOptions()) return
  salesAgentDialogOpen.value = true
}

function chooseSalesAgent(id: number): void {
  onSalesAgentPick(id)
  salesAgentDialogOpen.value = false
}

/* ── 保存（island `onSave` 的 create 分支逐字同构）────────────────────────────── */

const saving = ref(false)
const initialOrderPrice = ref('')
/** 「上一次建单提交的结果未知」（5xx/网络异常）⇒ 下一次保存先二次确认。 */
let submitOutcomeUncertain = false
const resubmitOpen = ref(false)

function askToResubmit(): void {
  resubmitOpen.value = false
  submitOutcomeUncertain = false
  void onSave()
}

async function onSave(): Promise<void> {
  if (saving.value) return
  // 编辑态走 PATCH 分岔（载荷删金额字段；没有指派/抛大厅/拆分这些新建专有面）。
  if (isEdit.value) {
    await onSaveEdit()
    return
  }
  // 结果未知 ⇒ 不许盲目重试：先让用户明确确认一次（读表单之前、不发请求、不构造载荷）。
  if (submitOutcomeUncertain) {
    resubmitOpen.value = true
    return
  }
  // 读时合成（island `readFormState` 的对应三段）：恰好一项勾选 ⇒ project FK；天/小时 ⇒ 文本。
  form.project = checkedProjectIds.value.length === 1 ? checkedProjectIds.value[0]! : null
  form.time_requirement = dayHoursToTimeRequirement(day.value, hours.value)
  const pickedSalesAgent = readSalesAgentChoice()
  form.sales_agent = pickedSalesAgent.name
  /** 「这次建单要不要指定提成归属」（两条建单路共用的同一处判据）：
   * 选自己 / 读不出主键 / 不持节点 ⇒ 不发那个键（后端维持「归属 = 认证态」）；
   * 选了别人且持节点 ⇒ 发主键。 */
  const intakeSalesUserId =
    canChangeSalesAgent.value &&
    pickedSalesAgent.id !== null &&
    pickedSalesAgent.id !== viewerId
      ? pickedSalesAgent.id
      : null
  const bad =
    requiredError() ||
    totalPriceRequiredError() ||
    firstMoneyError() ||
    projectTextSyncLimitError(form.project_text) ||
    splitOrdersError()
  if (bad !== '') {
    showToast(bad, 'error')
    return
  }
  // 内部抢单必须有一个大于 0 的价：拦在提交之前，一个请求都不发（`saving` 未置位）。
  if (toGrabHall.value) {
    const fee = Number(form.hall_price)
    if (!(fee > 0)) {
      showToast(HALL_PRICE_REQUIRED_MESSAGE, 'error')
      return
    }
  }
  /** 这一次保存发出去的 `remember_assignee`（`undefined` = 没发这个键）。 */
  let remembered: 'Y' | 'N' | 'hall' | undefined
  saving.value = true
  try {
    const payload: CreateOrderPayload = formToPayload({ ...form }, currentActor())
    // 新老客手动覆盖：不选就不发这个键（自动单用自动结果、无 ID 手工单不显示标签）。
    if (customerKindOverride.value !== '') {
      payload.customer_kind_override = customerKindOverride.value
    }
    // ⭐ 客户线（W1）：暂存的买家身份随创建载荷回传（后端 upsert 客户档案并连单）。
    //    ⛔ 没有就不发这个键（缺键 = 不建不连，与 `throw_to_hall` 同一条纪律）。
    if (pendingBuyer.value !== null) {
      payload.buyer_eid = pendingBuyer.value.eid
      payload.buyer_nick = pendingBuyer.value.nick
    }
    // 拆单勾选：没勾就不发这个键（缺键 = 不拆；布尔开关没有三态）。
    if (splitChecked.value) {
      payload.split = true
      if (splitOrders.value.length > 0) {
        payload.split_orders = splitOrders.value.map(({ project_text, total_price }) => ({ project_text, total_price }))
      }
    }
    if (toGrabHall.value) {
      // 内部抢单（R1/R4）：同一次请求抛大厅；⛔ 不带 `assignee`（二选一，同时给 ⇒ 1000）。
      payload.throw_to_hall = true
      remembered = rememberAssigneeValue(
        null,
        defAssChecked.value,
        initialDefaultAssigneeId.value,
        true,
        initialDefAssIsHall.value,
      )
      if (remembered !== undefined) payload.remember_assignee = remembered
      if (intakeSalesUserId !== null) payload.sales_user = intakeSalesUserId
      await createOrder(payload)
      submitOutcomeUncertain = false
      flashSaveSuccess()
      syncLocalDefaultPreference(remembered, null)
      resetAfterSave()
      return
    }
    if (selectedAssignee.value !== null) payload.assignee = selectedAssignee.value.id
    const remember = rememberAssigneeValue(
      selectedAssignee.value?.id ?? null,
      defAssChecked.value,
      initialDefaultAssigneeId.value,
      false,
      initialDefAssIsHall.value,
    )
    remembered = remember
    if (remember !== undefined) payload.remember_assignee = remember
    // 显式指定归属时名字那一列不进载荷（服务端以自己算的显示名为准，送它是死载荷）。
    if (intakeSalesUserId !== null) {
      payload.sales_user = intakeSalesUserId
      delete (payload as Record<string, unknown>).sales_agent
    }
    // 单号仍由后端返回，但有意不展示（业务方口径「只出小提示 + 刷新」）。
    await createOrder(payload)
    submitOutcomeUncertain = false
    flashSaveSuccess()
    syncLocalDefaultPreference(remember, selectedAssignee.value?.id ?? null)
    resetAfterSave()
  } catch (e) {
    // 「单已建好、指派没成功」（1022）：原样显示后端那句并复位表单（再点一次=又建一张单）。
    if (e instanceof BizError && e.code === ORDER_CREATED_ASSIGN_FAILED) {
      showToast(e.message, 'error')
      resetAfterSave()
      return
    }
    // 「单已建好、抛大厅没成功」（1023）：同处置；本地只按 'N' 对齐（'hall' 没落库）。
    if (e instanceof BizError && e.code === ORDER_CREATED_THROW_FAILED) {
      showToast(e.message, 'error')
      if (remembered === 'N') syncLocalDefaultPreference('N', null)
      resetAfterSave()
      return
    }
    // 「结果未知」档（只对建单）：不复位（会把用户填的一整单丢掉）、不伪装成功、置标志。
    if (isUnknownSubmitOutcome(e)) {
      submitOutcomeUncertain = true
      showToast(SUBMIT_OUTCOME_UNKNOWN_MESSAGE, 'error')
      return
    }
    // 后端业务失败原样展示，不改写。
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

/** 保存成功后把表单恢复成刚打开：金额回 0，天回 30、小时回 0。 */
function resetAfterSave(): void {
  submitOutcomeUncertain = false
  Object.assign(form, emptyAddForm())
  day.value = '30'
  hours.value = '0'
  jiaji.value = 'N'
  splitChecked.value = false
  splitOrders.value = []
  customerKindOverride.value = ''
  defAssChecked.value = false
  interPrice.value = '0'
  deadSelect.value = ''
  safeKeyType.value = '20'
  isDelay.value = 'N'
  payMethod.value = '0'
  pushTemplate.value = '0'
  vipDedCheck.value = false
  for (const key of Object.keys(deadForm)) deadForm[key] = ''
  checkedProjectIds.value = []
  projects.value = []
  categories.value = []
  servers.value = []
  userPickedGame.value = false
  // ⭐ 客户线：暂存的买家身份跟着「新的一单」一起清（§5-E 回查复位路径——不清会把上一单的
  //    买家身份粘到下一单）。
  pendingBuyer.value = null
  orderSourceIsAuto.value = true
  // 快速指派的表单级状态照「下次录单」的默认档位再带出来（island R2 改准）。
  toGrabHall.value = canPublishToHall.value && initialDefAssIsHall.value
  defAssChecked.value = toGrabHall.value || initialDefaultAssigneeId.value > 0
  selectedAssignee.value =
    initialDefAssIsHall.value || initialDefaultAssigneeId.value === 0
      ? null
      : (candidates.value.find((row) => row.id === initialDefaultAssigneeId.value) ?? null)
  applyAssigneeSelection()
  // 来源：先清再选中第一条（auto = 本页给的默认值，读单时不发出去）。
  form.order_source = sourceOptions.value.length > 0 ? sourceOptions.value[0]!.id : null
  const firstGame = games.value[0]
  form.game = firstGame ? firstGame.id : null
  if (firstGame) void onGameChange(firstGame.id)
  // 销售客服格复位：候选不变，默认勾回当前登录人（基线 = 当前登录人，提交判据直接比 id）。
  renderSalesAgentOptions()
}

/* ── 「尚未开放」（D-0025：照渲染 + 点了提示，不置灰；文本框失焦非空也提示）────────── */

function notOpenClick(label: string): void {
  showToast(`「${label}」尚未开放`)
}

function notOpenBlur(label: string, value: string): void {
  if (value.trim() !== '') showToast(`「${label}」尚未开放`)
}

/** 捕获阶段拦截（island 同款手法：layui/EP 的委托在冒泡阶段会吞事件，先拿先拦）。 */
function swallowAndNotOpen(event: Event, label: string): void {
  event.preventDefault()
  event.stopPropagation()
  notOpenClick(label)
}

/* ── 「更多设置（偶尔用）」折叠区（⭐ 默认收起，展开态 localStorage 记忆）── */

/** localStorage 键（带页面前缀，照 `views/orders/orderFilters.ts` 的约定）。 */
const MORE_OPEN_KEY = 'tt.orders.new.moreOpen'

/** 收起是默认：读不到 / 值不是 '1' 一律回退收起（存储可能是别人写的、也可能被手改）。 */
function readMoreOpen(): boolean {
  try {
    return localStorage.getItem(MORE_OPEN_KEY) === '1'
  } catch {
    return false
  }
}

const moreOpen = ref(readMoreOpen())

/** 点折叠头：翻转展开态并落 localStorage（Safari 隐私模式写失败不影响折叠本身）。 */
function toggleMoreOpen(): void {
  moreOpen.value = !moreOpen.value
  try {
    localStorage.setItem(MORE_OPEN_KEY, moreOpen.value ? '1' : '0')
  } catch {
    // 存储被禁用：记忆失败不影响折叠本身。
  }
}

/* ── 首屏（island mount 的字典四发 + 级联起点 + 销售客服首渲）────────────────────── */

/** `timeRequirementToDayHours` 的本地重实现（island 同式逐字；认不出回空串 ⇒ 保留后端原值）。 */
function timeRequirementToDayHours(text: string): { day: string; hours: string } {
  const d = /(\d+(?:\.\d+)?)\s*天/.exec(text)
  const h = /(\d+(?:\.\d+)?)\s*小时/.exec(text)
  return { day: d ? d[1]! : '', hours: h ? h[1]! : '' }
}

/** 编辑态回填（island `orderToAddOrderForm` + `prefillEditOrder` 同口径的受控表单版）：
 * 文本键 null→''、FK 显式落、级联按序重建（游戏 → 区服覆盖预选 → 分类 → 项目）、
 * 时间要求逆解析后只落「下拉里确实存在」的值；金额字段进模型但禁用（PATCH 不收钱面）。 */
async function prefillEditOrder(): Promise<void> {
  let order: Order
  try {
    order = await fetchOrder(props.orderNo ?? '')
  } catch (e) {
    showToast(errText(e) || '订单详情加载失败', 'error')
    return
  }
  const text: Array<keyof OrderForm> = [
    'game_account', 'game_sub_account', 'game_role_name', 'current_info_1', 'current_info_2',
    'project_text', 'content', 'demand', 'source_order_no', 'customer_phone', 'customer_qq',
    'customer_ww', 'remark', 'tag', 'payment_method', 'sales_agent',
  ]
  for (const key of text) {
    const value = order[key]
    ;(form as Record<string, unknown>)[key] = value === null || value === undefined ? '' : String(value)
  }
  for (const key of ORDER_MONEY_FIELDS) {
    const value = order[key]
    ;(form as Record<string, unknown>)[key] = value === null || value === undefined ? '' : String(value)
  }
  initialOrderPrice.value = order.total_price
  form.order_type = order.order_type === 'COMPANION' ? 'COMPANION' : 'BOOST'
  form.is_urgent = order.is_urgent
  jiaji.value = order.is_urgent ? 'Y' : 'N'
  form.game = order.game
  form.order_source = order.order_source ?? null
  if (form.game !== null) await onGameChange(form.game, false)
  form.server = order.server
  if (order.category !== null) {
    form.category = order.category
    await onCategoryChange()
    form.project = order.project
  }
  const parsed = timeRequirementToDayHours(order.time_requirement)
  if (parsed.day !== '' && DAY_OPTIONS.includes(parsed.day)) day.value = parsed.day
  if (parsed.hours !== '' && HOUR_OPTIONS.includes(parsed.hours)) hours.value = parsed.hours
  // 初值与脏标志必须**最后**复位：上面落 day/hours 会先把脏标志置真。
  editTimeOriginal.value = order.time_requirement
  editTimeDirty = false
  editSalesAgentId = order.sales_user ?? null
  salesAgentPicked.value = editSalesAgentId
  editOrderLoaded.value = true
}

/** 编辑态保存：PATCH（载荷 = formToPayload 删金额字段；时间要求没动过按原值回）。 */
async function onSaveEdit(): Promise<void> {
  if (!canSaveOrder.value || !editOrderLoaded.value) return
  const orderNo = props.orderNo ?? ''
  const { id: pickedId } = readSalesAgentChoice()
  const bad = requiredError() || projectTextSyncLimitError(form.project_text)
  if (bad !== '') {
    showToast(bad, 'error')
    return
  }
  saving.value = true
  try {
    const payload = formToPayload({ ...form }, currentActor()) as Record<string, unknown>
    // 普通编辑不送金额；拆单组非锚点成员只在单次 PATCH 中带改过的接单价。
    for (const field of ORDER_MONEY_FIELDS) delete payload[field]
    delete payload.sales_agent
    if (props.allowSplitPrice === true && form.total_price !== initialOrderPrice.value) {
      payload.total_price = form.total_price
    }
    if (!editTimeDirty) payload.time_requirement = editTimeOriginal.value
    const { order, message } = await updateOrderWithMessage(orderNo, payload)
    const salesAgentChanged = pickedId !== null && pickedId !== editSalesAgentId
    if (order.sales_user !== undefined) editSalesAgentId = order.sales_user
    if (typeof order.sales_agent === 'string') form.sales_agent = order.sales_agent
    if (salesAgentChanged && pickedId !== editSalesAgentId && canChangeSalesAgent.value) {
      try {
        await changeSalesAgent(orderNo, { user: pickedId })
      } catch (error) {
        const reason = errText(error)
        let result = '销售客服未更换'
        if (error instanceof NetworkError || (error instanceof HttpError && error.status >= 500)) {
          try {
            const confirmed = await fetchOrder(orderNo)
            editSalesAgentId = confirmed.sales_user ?? null
            form.sales_agent = confirmed.sales_agent
            if (editSalesAgentId === pickedId) {
              emit('saved', message)
              return
            }
          } catch {
            result = '暂时无法确认销售客服是否已更换，请重新打开订单核对'
          }
        }
        salesAgentPicked.value = editSalesAgentId
        showToast([message || '订单已保存', result, reason].filter(Boolean).join('，'), 'error')
        return
      }
    }
    emit('saved', message)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    games.value = await fetchGames()
  } catch (e) {
    showToast(errText(e) || '游戏列表加载失败', 'error')
  }
  // 编辑态：来源字典只装载、不默认选中（来源 = 这张订单自己带的值，回填在 prefillEditOrder）。
  try {
    const page = await fetchOrderSources()
    sourceOptions.value = page.items.map((r) => ({ id: r.id, name: r.name }))
    if (!isEdit.value && sourceOptions.value.length > 0) {
      form.order_source = sourceOptions.value[0]!.id
      orderSourceIsAuto.value = true
    }
  } catch (e) {
    showToast(errText(e) || '订单来源加载失败', 'error')
  }
  // 编辑态按需加载销售客服候选，首次打开只回填订单。
  if (isEdit.value) {
    await prefillEditOrder()
    return
  }
  // 候选名单首屏（默认指派/内部抢单的复原靠它）；失败由 loadCandidates 自己提示。
  await loadCandidates()
  await loadSalesAgentOptions()
  renderSalesAgentOptions()
  const firstGame = games.value[0]
  if (firstGame && !userPickedGame.value && form.game === null) {
    form.game = firstGame.id
    await onGameChange(firstGame.id)
  }
})
</script>

<template>
  <div class="cv2-page tt-ord" data-v2-page="order-new">

    <!-- 页内页签（对标 layui-tab；第二页签在 island 里整块「尚未开放」⇒ 点页签弹同一句） -->
    <div class="cv2-ptabs" data-v2="order-tabs">
      <button type="button" class="cv2-ptab cv2-ptab--on" data-v2-tab="manual">手工录入订单</button>
      <button
        type="button"
        class="cv2-ptab"
        data-v2-tab="import"
        @click.capture="swallowAndNotOpen($event, '导入文件订单')"
      >导入文件订单</button>
    </div>

    <div class="tt-ord-grid" :class="{ 'tt-ord-grid--single': isEdit }">
      <!-- 左列：订单内容粘贴识别区（浅底面板 + 两条提示逐字） -->
      <aside v-if="!isEdit" class="tt-paste-panel cv2-panel" aria-label="订单内容">
        <p class="tt-paste-label">
          订单内容<template v-if="canParsePaste">{{ PASTE_HINT_LABEL_SUFFIX }}</template>
        </p>
        <!-- ⚠️ 失焦识别绑在**容器**上（blur 不冒泡、capture 只收一次）：
             EP ElInput 的 @blur 是「fallthrough 原生监听 + 组件 emit」双通道，直绑会一blur发两次解析 -->
        <div @blur.capture="onPasteBlurGuarded">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="20"
            :placeholder="canParsePaste ? PASTE_PLACEHOLDER : undefined"
            data-v2-field="content"
          />
        </div>
        <blockquote class="tt-paste-quote" :title="AUTODISCERN_TIP">
          <span
            class="cv2-actlink"
            title="订单识别关键词设置"
            data-v2-notopen="订单识别关键词设置"
            @click.capture="swallowAndNotOpen($event, '订单识别关键词设置')"
          >识别不出来或有误？点击这里进行录单识别设置</span>
        </blockquote>
      </aside>

      <!-- ⭐ 右列信息架构：主流程区（客服 90% 动线，控件 38px）+
           「更多设置（偶尔用）」折叠区（其余全部字段，34px）。折叠 ≠ 省略：
           字段锚点一项不落、载荷与校验链和 island 逐字段零变化。 -->
      <div class="tt-ord-form">
        <!-- ── 主流程区（data-v2="main-flow"；控件 38px）────────────────────────── -->
        <section class="tt-main" data-v2="main-flow" aria-label="录单主流程">
          <!-- ① 订单类型（模式开关，紧凑一行；label 切换功能照旧） -->
          <div class="tt-frow">
            <div class="tt-fitem">
              <span class="tt-flabel tt-flabel--strong">订单类型</span>
              <span class="tt-fvalue">
                <el-radio-group v-model="form.order_type" data-v2-field="order-type">
                  <el-radio value="BOOST">代练单</el-radio>
                  <el-radio value="COMPANION">陪玩单</el-radio>
                </el-radio-group>
              </span>
            </div>
          </div>

          <!-- ③ 游戏/区/平台/服（游戏 + 区服接真级联，选定后区服自动预选第一项；服/职业/阵营 = 尚未开放壳） -->
          <div class="tt-frow">
            <div class="tt-fitem">
              <span class="tt-flabel">游戏/区/平台/服</span>
              <el-select
                :model-value="form.game ?? ''"
                filterable
                placeholder="请选择游戏"
                data-v2-field="game"
                :teleported="false"
                :popper-options="{ strategy: 'fixed' }"
                class="tt-w-lg"
                @update:model-value="onUserPickGame"
              >
                <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
              </el-select>
              <el-select
                :model-value="form.server ?? ''"
                filterable
                clearable
                placeholder="请选择或输入"
                data-v2-field="server"
                :teleported="false"
                :popper-options="{ strategy: 'fixed' }"
                class="tt-w-lg"
                @update:model-value="onServerPick"
              >
                <el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" />
              </el-select>
              <span
                class="tt-notopen-shell"
                data-v2-notopen="服务器（服）"
                @click.capture="swallowAndNotOpen($event, '服务器（服）')"
              ><el-select v-model="deadSelect" placeholder="请选择或输入" class="tt-w-md" :teleported="false" /></span>
              <span
                class="tt-notopen-shell"
                data-v2-notopen="职业/门派"
                @click.capture="swallowAndNotOpen($event, '职业/门派')"
              ><el-select v-model="deadSelect" placeholder="请选择或输入" class="tt-w-md" :teleported="false" /></span>
              <span
                v-show="showGameGroup"
                class="tt-notopen-shell"
                data-v2-notopen="阵营门派"
                @click.capture="swallowAndNotOpen($event, '阵营门派')"
              ><el-select v-model="deadSelect" placeholder="请选择或输入" class="tt-w-md" :teleported="false" /></span>
            </div>
          </div>

          <!-- ③b 是否拆分订单 -->
          <div v-if="!isEdit" class="tt-frow">
            <div class="tt-fitem">
              <span class="tt-flabel">是否拆分订单</span>
              <span class="tt-fvalue">
                <el-checkbox v-model="splitChecked" data-v2-field="split">是</el-checkbox>
                <span class="cv2-field-hint">勾选后可在项目下新增子单。未新增时按 / 分隔项目。</span>
              </span>
            </div>
          </div>

          <!-- ④ 项目分类和常用项目与代练内容放在一起。 -->
          <div class="tt-panel-card">
            <p class="tt-card-head">
              常用代练/陪玩项目（<span class="tt-card-hint">没有常用的?</span>
              <!-- 锚点名沿用保真网钉的选择器（spec 网1 钉 data-v2-notopen="常用项目设置"，改名须同次改网）；行为已实装为跳转管理页。 -->
              <button
                type="button"
                class="cv2-actlink tt-card-link"
                :title="PROJECT_SETTING_TIP"
                data-v2-notopen="常用项目设置"
                @click="openProjectSettings"
              >点击常用项目设置</button>）
            </p>
            <div v-show="categories.length > 0" class="tt-project-categories" data-v2-row="category">
              <span class="tt-flabel" :title="CATEGORY_TIP">项目分类</span>
              <span class="tt-fvalue">
                <el-radio-group :model-value="form.category ?? ''" data-v2-field="category" @update:model-value="onCategoryPick">
                  <el-radio v-for="c in categories" :key="c.id" :value="c.id">{{ c.name }}</el-radio>
                </el-radio-group>
              </span>
            </div>
            <div class="tt-check-list" data-v2-field="projects">
              <el-checkbox-group v-model="checkedProjectIds" class="tt-check-list">
                <el-checkbox
                  v-for="p in projects"
                  :key="p.id"
                  :value="p.id"
                  :data-v2-project="p.id"
                >{{ p.name }}</el-checkbox>
              </el-checkbox-group>
              <span v-if="projectHint !== ''" class="cv2-field-hint">{{ projectHint }}</span>
            </div>

            <div class="tt-fitem tt-mt8">
              <span class="tt-flabel tt-flabel--accent">{{ typeLabels.project }}</span>
              <!-- ⭐ 业务方「内容超过长度后 自动换行」：单行 input 永不换行，换
                   autosize textarea——2 行起步、随内容自动拉长、8 行封顶转内滚（同旧版
                   「代练项目/标题」框最终口径）；resize 手柄关掉，高度只由内容驱动。 -->
              <el-input
                v-model="form.project_text"
                type="textarea"
                :rows="2"
                :autosize="{ minRows: 2, maxRows: 8 }"
                :maxlength="900"
                :placeholder="PROJECT_PLACEHOLDER"
                data-v2-field="project_text"
                class="tt-grow tt-accent-box"
              />
            </div>
            <div v-if="!isEdit && splitChecked" class="tt-intake-split" data-v2="create-split-orders">
              <div v-for="(order, index) in splitOrders" :key="order.key" class="tt-intake-split-row" data-v2="create-split-order">
                <el-input
                  v-model="order.project_text"
                  type="textarea"
                  :autosize="{ minRows: 1, maxRows: 6 }"
                  :maxlength="900"
                  resize="none"
                  placeholder="子单项目内容"
                  :aria-label="`第 ${index + 1} 笔子单项目内容`"
                  data-v2="create-split-project"
                />
                <el-input
                  v-model="order.total_price"
                  inputmode="decimal"
                  placeholder="接单价"
                  :aria-label="`第 ${index + 1} 笔子单接单价`"
                  data-v2="create-split-price"
                />
                <button type="button" class="cv2-btn cv2-btn--quiet" :aria-label="`删除第 ${index + 1} 笔子单`" @click="splitOrders.splice(index, 1)">删除</button>
              </div>
              <div class="tt-intake-split-summary">
                <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="create-split-add" @click="addSplitOrder">＋ 新增子单</button>
                <span data-v2="create-split-remaining" aria-live="polite">
                  原单剩余接单价 <strong>{{ splitRemainingPrice ?? '—' }}</strong>
                </span>
              </div>
            </div>
          </div>


          <!-- ②+②b 来源行（⭐ 业务方「来源单号和订单来源合并为一排 订单来源在左
               来源单号在右 整体宽度与其他排对齐」）：一格一半等分铺满整行；
               失焦/回车读平台订单回填（@change 行级委托）与选来源触发点②照旧。 -->
          <div class="tt-frow" @change="onSourceOrderNoChange">
            <div class="tt-fitem">
              <span class="tt-flabel">订单来源</span>
              <el-select
                :model-value="form.order_source ?? ''"
                filterable
                clearable
                placeholder="请选择"
                data-v2-field="order_source"
                :teleported="false"
                :popper-options="{ strategy: 'fixed' }"
                class="tt-grow"
                @update:model-value="onUserPickSource"
              >
                <el-option v-for="s in sourceOptions" :key="s.id" :value="s.id" :label="s.name" />
              </el-select>
            </div>
            <div class="tt-fitem">
              <!-- accent 红随主流程三 label（来源单号/代练项目·标题/订单总价）。 -->
              <span class="tt-flabel tt-flabel--accent">来源单号</span>
              <el-input
                v-model="form.source_order_no"
                :maxlength="100"
                :placeholder="SOURCE_ORDER_PLACEHOLDER"
                data-v2-field="source_order_no"
                class="tt-grow"
              />
            </div>
          </div>

          <!-- ⑤+⑩+⑤b 订单总价 + 时间要求 + 是否加急（⭐ 业务方「订单总价和时间加急
               合并到一排 订单总价在左侧」；总价accent红字+强调框照旧，密保隐藏件照藏照渲染） -->
          <div class="tt-frow">
            <div class="tt-fitem tt-price-item">
              <span class="tt-flabel tt-flabel--accent">订单总价</span>
              <el-input
                v-model="form.total_price"
                :maxlength="7"
                :placeholder="TOTAL_PRICE_PLACEHOLDER"
                :disabled="isEdit && props.allowSplitPrice !== true"
                data-v2-field="total_price"
                class="tt-grow tt-strong tt-accent-box tt-price"
              />
            </div>
            <div class="tt-fitem">
              <span class="tt-flabel">{{ typeLabels.time }}</span>
              <el-select v-model="day" data-v2-field="day" :teleported="false" :popper-options="{ strategy: 'fixed' }" class="tt-w-sm">
                <el-option v-for="d in DAY_OPTIONS" :key="d" :value="d" :label="`${d}天`" />
              </el-select>
              <el-select v-model="hours" data-v2-field="hours" :teleported="false" :popper-options="{ strategy: 'fixed' }" class="tt-w-sm">
                <el-option v-for="h in HOUR_OPTIONS" :key="h" :value="h" :label="`${h}小时`" />
              </el-select>
            </div>
            <div v-show="false" class="tt-fitem" data-v2-hidden="safe-key-type">
              <span class="tt-flabel">密保类型</span>
              <span class="tt-fvalue" data-v2-notopen="密保类型" @click.capture="swallowAndNotOpen($event, '密保类型')">
                <label class="cv2-radio"><input v-model="safeKeyType" type="radio" name="safeKeyType" value="10" />无</label>
                <label class="cv2-radio"><input v-model="safeKeyType" type="radio" name="safeKeyType" value="20" />安全令牌</label>
              </span>
            </div>
            <div class="tt-fitem">
              <span class="tt-flabel">是否加急</span>
              <span class="tt-fvalue">
                <el-radio-group v-model="jiaji" data-v2-field="is_urgent">
                  <el-radio value="N">否</el-radio>
                  <el-radio value="Y">是</el-radio>
                </el-radio-group>
              </span>
            </div>
          </div>

          <!-- ⑭ 联系三连（⭐ 业务方「这一排也拿出来 放到重要备注上一排」）；
               ⭐ 三格等分钉死（tt-frow--thirds）——auto basis 下三格 max-content 合计恰好
               超行宽 ~2px，格内 label+控件被 wrap 成上下叠（业务方截图圈红），钉死 basis 根治 -->
          <div class="tt-frow tt-frow--thirds">
            <div class="tt-fitem">
              <span class="tt-flabel">联系电话</span>
              <el-input
                v-model="form.customer_phone"
                :maxlength="40"
                :placeholder="LEAVE_EMPTY_PLACEHOLDER"
                data-v2-field="customer_phone"
                class="tt-grow"
              />
            </div>
            <div class="tt-fitem">
              <span class="tt-flabel">QQ/微信</span>
              <el-input
                v-model="form.customer_qq"
                :maxlength="50"
                :placeholder="LEAVE_EMPTY_PLACEHOLDER"
                data-v2-field="customer_qq"
                class="tt-grow"
              />
            </div>
            <div class="tt-fitem">
              <span class="tt-flabel">旺旺/YY</span>
              <el-input
                v-model="form.customer_ww"
                :maxlength="50"
                :placeholder="LEAVE_EMPTY_PLACEHOLDER"
                data-v2-field="customer_ww"
                class="tt-grow tt-ww-input"
              >
                <template v-if="!isEdit" #prefix>
                  <div class="tt-kind-seg" role="group" aria-label="新老客">
                    <button
                      v-for="opt in CUSTOMER_KIND_OPTIONS"
                      :key="opt.value || 'none'"
                      type="button"
                      class="tt-kind-btn"
                      :class="[
                        `tt-kind-btn--${opt.value || 'none'}`,
                        { 'tt-kind-btn--on': customerKindOverride === opt.value },
                      ]"
                      :aria-pressed="customerKindOverride === opt.value"
                      :data-v2-customer-kind="opt.value || 'none'"
                      @click="customerKindOverride = opt.value"
                    >{{ opt.label }}</button>
                  </div>
                </template>
              </el-input>
            </div>
          </div>

          <!-- 内部派单和重要备注并排，编辑时保留备注。 -->
          <div class="tt-frow tt-dispatch-row">
            <div v-if="!isEdit" class="tt-fitem tt-dispatch-item">
              <span class="tt-flabel">内部派单</span>
              <span class="tt-fvalue">
                <span class="tt-assignee-control">
                  <button
                    ref="assignButton"
                    type="button"
                    class="cv2-btn"
                    :class="{
                      'cv2-btn--tool': kspdActive,
                      'tt-assignee-picked': selectedAssignee !== null && !toGrabHall,
                    }"
                    data-v2="assign-button"
                    :title="ASSIGN_BUTTON_TIP"
                    @click="openAssignDialog"
                  >{{ kspdText }}</button>
                  <button
                    v-if="selectedAssignee !== null && !toGrabHall"
                    type="button"
                    class="cv2-btn tt-assignee-picked tt-assignee-clear"
                    data-v2="assign-clear"
                    aria-label="取消指派"
                    title="取消指派"
                    @click="void clearPickedAssignee()"
                  >×</button>
                </span>
              </span>
            </div>
            <div v-if="!isEdit && toGrabHall" class="tt-fitem" data-v2-row="throw-fee">
              <span class="tt-flabel">{{ typeLabels.throwFee }}</span>
              <el-input
                ref="throwFeeInputRef"
                v-model="form.hall_price"
                :maxlength="7"
                :placeholder="THROW_FEE_PLACEHOLDER"
                :disabled="isEdit"
                data-v2-field="hall_price"
                class="tt-w-xs tt-strong tt-price"
                @focus="showFieldTip('hall_price', MONEY_INPUT_PLACEHOLDER, 4000)"
                @blur="clearFieldTip"
              />
              <span v-if="fieldTip?.anchor === 'hall_price' && fieldTip.text !== MONEY_INPUT_PLACEHOLDER" class="tt-tip">
                {{ fieldTip.text }}
              </span>
            </div>
            <div class="tt-fitem tt-grow-item">
              <span class="tt-flabel">重要备注</span>
              <el-input
                v-model="form.remark"
                :maxlength="500"
                :placeholder="REMARK_PLACEHOLDER"
                data-v2-field="remark"
                class="tt-grow"
              />
            </div>
          </div>

          <!-- ⑤e 接单销售客服（⭐ 业务方第二轮挪出折叠区；radio 网格 / 无权限只读） -->
          <div class="tt-frow">
            <div class="tt-fitem" data-v2-field="sales_agent">
              <span class="tt-flabel">接单销售客服</span>
              <span class="tt-fvalue">
                <template v-if="canChangeSalesAgent">
                  <span data-v2="sales-agent-current">{{ selectedSalesAgentName }}</span>
                  <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="sales-agent-change" :disabled="saving || salesAgentOptionsLoading || (isEdit && !editOrderLoaded)" @click="void openSalesAgentDialog()">更换</button>
                </template>
                <span v-else class="cv2-field-hint" data-v2="sales-agent-readonly">{{ salesAgentReadonlyText }}</span>
              </span>
            </div>
          </div>

          <!-- ⑥ 提交保存：深橙实底，避免纯字不够显眼。 -->
          <div class="tt-frow tt-actions">
            <button
              v-if="canSaveOrder"
              type="button"
              class="cv2-btn cv2-btn--submit tt-save-btn cv2-btn--commit cv2-btn--fill"
              data-v2="save-order"
              :disabled="saving || (isEdit && !editOrderLoaded)"
              @click="onSave"
            >
              提交保存
            </button>
            <span v-if="saving" class="cv2-filter-loading">正在保存…</span>
          </div>
        </section>

        <!-- ── 「更多设置（偶尔用）」折叠区（默认收起；展开态 localStorage 记忆；200ms 滑动）──
             其余全部字段一项不落：字段值照常进载荷（hidden ≠ omitted，载荷与 island 逐字段零变化）；
             展开时布局沿用全量对齐版：标签左置、EP 控件、34px。 -->
        <section class="tt-more" :class="{ 'tt-more--open': moreOpen }" data-v2="more-settings">
          <button
            type="button"
            class="tt-more-head"
            data-v2="more-toggle"
            :aria-expanded="moreOpen ? 'true' : 'false'"
            @click="toggleMoreOpen"
          >
            <span>更多设置（偶尔用）</span>
            <span class="tt-more-arrow" aria-hidden="true">▶</span>
          </button>
          <div class="tt-more-body" data-v2="more-body">
            <div class="tt-more-inner">
              <!-- ⑦ 游戏账号行（4 连；密码/暗号 = 尚未开放，失焦非空也提示） -->
              <div class="tt-frow">
                <div class="tt-fitem">
                  <span class="tt-flabel">游戏账号</span>
                  <el-input
                    v-model="form.game_account"
                    :maxlength="50"
                    :placeholder="GAME_ACC_PLACEHOLDER"
                    data-v2-field="game_account"
                    class="tt-w-md"
                  />
                </div>
                <div
                  class="tt-fitem"
                  data-v2-notopen="登录密码"
                  @click.capture="swallowAndNotOpen($event, '登录密码')"
                  @blur.capture="notOpenBlur('登录密码', deadForm.gamePwd ?? '')"
                >
                  <span class="tt-flabel">登录密码</span>
                  <el-input
                    v-model="deadForm.gamePwd"
                    :maxlength="50"
                    :placeholder="GAME_PWD_PLACEHOLDER"
                    data-v2-field="game_pwd"
                    class="tt-w-md"
                  />
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">子账号</span>
                  <el-input
                    v-model="form.game_sub_account"
                    :maxlength="50"
                    :placeholder="GAME_ZACC_PLACEHOLDER"
                    data-v2-field="game_sub_account"
                    class="tt-w-md"
                  />
                </div>
                <div
                  class="tt-fitem"
                  data-v2-notopen="暗号/口令"
                  @click.capture="swallowAndNotOpen($event, '暗号/口令')"
                  @blur.capture="notOpenBlur('暗号/口令', deadForm.loginKey ?? '')"
                >
                  <span class="tt-flabel">暗号/口令</span>
                  <el-input
                    v-model="deadForm.loginKey"
                    :maxlength="50"
                    :placeholder="GAME_ZACC_PLACEHOLDER"
                    data-v2-field="login_key"
                    class="tt-w-md"
                  />
                </div>
              </div>

              <!-- ⑧ 角色名行（3 连；mark1 的 label 逐字取证据件服务端渲染值「开拓等级」） -->
              <div class="tt-frow">
                <div class="tt-fitem">
                  <span class="tt-flabel">{{ typeLabels.role }}</span>
                  <el-input
                    v-model="form.game_role_name"
                    :maxlength="50"
                    :placeholder="GAME_ROLE_PLACEHOLDER"
                    data-v2-field="game_role_name"
                    class="tt-w-md"
                  />
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">开拓等级</span>
                  <el-input
                    v-model="form.current_info_1"
                    :maxlength="499"
                    :placeholder="MARK_PLACEHOLDER"
                    data-v2-field="current_info_1"
                    class="tt-w-md"
                  />
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">当前信息2</span>
                  <el-input
                    v-model="form.current_info_2"
                    :maxlength="499"
                    :placeholder="MARK_PLACEHOLDER"
                    data-v2-field="current_info_2"
                    class="tt-w-md"
                  />
                </div>
              </div>

              <!-- ⑪ 隐藏金额件（对标 received/pushplat 照藏照渲染；⭐ 重排后整组收进折叠区——
                   订单总价留在主流程⑤；发平台价格/发内部抢单大厅价按业务方追加修正同日移入；
                   值照常进载荷：platform_price 在载荷、hall_price_inter 走「给打手发单价」承担） -->
              <div v-show="false" class="tt-frow" data-v2-hidden="money-extra">
                <div v-show="false" class="tt-fitem" data-v2-hidden="received-price">
                  <span class="tt-flabel">接单实收款</span>
                  <el-input
                    v-model="form.received_price"
                    :maxlength="7"
                    :placeholder="RECEIVED_PLACEHOLDER"
                    :disabled="isEdit"
                    data-v2-field="received_price"
                    class="tt-w-xs tt-price"
                  />
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">发平台价格</span>
                  <el-input
                    v-model="form.platform_price"
                    :maxlength="6"
                    :placeholder="PLATFORM_PRICE_PLACEHOLDER"
                    :disabled="isEdit"
                    data-v2-field="platform_price"
                    class="tt-w-xs tt-price"
                  />
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">发内部抢单大厅价</span>
                  <el-input
                    v-model="interPrice"
                    :maxlength="6"
                    :placeholder="PLATFORM_PRICE_PLACEHOLDER"
                    data-v2-field="hall_price_inter"
                    class="tt-w-xs tt-price"
                  />
                </div>
                <div
                  class="tt-fitem"
                  data-v2-notopen="安全保证金"
                  @click.capture="swallowAndNotOpen($event, '安全保证金')"
                  @blur.capture="notOpenBlur('安全保证金', deadForm.safeAmount ?? '')"
                >
                  <span class="tt-flabel">安全保证金</span>
                  <el-input
                    v-model="deadForm.safeAmount"
                    :maxlength="6"
                    placeholder="安全保证金"
                    data-v2-field="safe_amount"
                    class="tt-w-xs tt-price"
                  />
                </div>
                <div
                  class="tt-fitem"
                  data-v2-notopen="效率保证金"
                  @click.capture="swallowAndNotOpen($event, '效率保证金')"
                  @blur.capture="notOpenBlur('效率保证金', deadForm.effAmount ?? '')"
                >
                  <span class="tt-flabel">效率保证金</span>
                  <el-input
                    v-model="deadForm.effAmount"
                    :maxlength="6"
                    placeholder="效率保证金"
                    data-v2-field="eff_amount"
                    class="tt-w-xs tt-price"
                  />
                </div>
                <div class="tt-fitem" data-v2-hidden="push-template">
                  <span class="tt-flabel">快速发单配置的要求</span>
                  <select v-model="pushTemplate" data-v2-field="push_template" class="cv2-input tt-w-md">
                    <option value="0">快速发单配置的要求</option>
                  </select>
                </div>
              </div>

              <!-- ⑫ 延迟发平台（隐藏行，对标 delayDiv 照藏照渲染） -->
              <div v-show="false" class="tt-frow" data-v2-hidden="delay">
                <div class="tt-fitem">
                  <span class="tt-flabel">延迟发平台</span>
                  <span class="tt-fvalue">
                    <label class="cv2-radio"><input v-model="isDelay" type="radio" name="isDelay" value="N" />否</label>
                    <label class="cv2-radio"><input v-model="isDelay" type="radio" name="isDelay" value="Y" />是</label>
                  </span>
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">延迟</span>
                  <el-input v-model="deadForm.delaySec" data-v2-field="delay_sec" class="tt-w-xs" />
                  <span class="cv2-field-hint">秒后再发到交易平台</span>
                </div>
              </div>

              <!-- ⑬ 支付方式行（对标整行 layui-hide；手续费 fee 照常进载荷） -->
              <div v-show="false" class="tt-frow" data-v2-hidden="pay-method">
                <div class="tt-fitem">
                  <span class="tt-flabel">支付方式</span>
                  <select v-model="payMethod" data-v2-field="pay_method" class="cv2-input tt-w-sm">
                    <option value="0">默认</option>
                    <option value="10">花呗</option>
                    <option value="20">信用卡</option>
                  </select>
                </div>
                <div class="tt-fitem">
                  <span class="tt-flabel">产生的手续费</span>
                  <el-input
                    v-model="form.fee"
                    :maxlength="7"
                    :placeholder="SERVICE_FEE_PLACEHOLDER"
                    :disabled="isEdit"
                    data-v2-field="fee"
                    class="tt-w-xs tt-price"
                  />
                </div>
              </div>

              <!-- ⑮ 会员号行（两格都是对标有、后端没有 ⇒ 尚未开放，失焦非空也提示） -->
              <div class="tt-frow">
                <div
                  class="tt-fitem"
                  data-v2-notopen="会员号/会员名"
                  @click.capture="swallowAndNotOpen($event, '会员号/会员名')"
                  @blur.capture="notOpenBlur('会员号/会员名', deadForm.customerName ?? '')"
                >
                  <span class="tt-flabel">会员号/会员名</span>
                  <el-input
                    v-model="deadForm.customerName"
                    :maxlength="50"
                    :placeholder="CUSTOMER_NAME_PLACEHOLDER"
                    data-v2-field="customer_name"
                    class="tt-w-md"
                  />
                </div>
                <div
                  class="tt-fitem"
                  data-v2-notopen="从余额扣款"
                  @click.capture="swallowAndNotOpen($event, '从余额扣款')"
                >
                  <span class="tt-flabel">余额扣款</span>
                  <span class="tt-fvalue">
                    <span class="cv2-field-hint" :title="VIP_DED_TIP">
                      <el-checkbox v-model="vipDedCheck" data-v2-field="vip_ded_check">需要从余额扣款</el-checkbox>
                    </span>
                  </span>
                </div>
              </div>

              <!-- ⑯ 自定义标记（重要备注/是否加急已挪进主流程⑤c⑤b） -->
              <div class="tt-frow">
                <div class="tt-fitem">
                  <span class="tt-flabel">自定义标记</span>
                  <el-input
                    v-model="form.tag"
                    :maxlength="20"
                    :placeholder="TAG_PLACEHOLDER"
                    data-v2-field="tag"
                    class="tt-w-md"
                  />
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <el-dialog v-model="salesAgentDialogOpen" title="选择销售客服" class="cv2-dialog cv2-dialog--sm" destroy-on-close data-v2="dlg-sales-agent">
      <p v-if="salesAgentItems.length === 0" class="cv2-field-hint">暂无可选销售客服</p>
      <div class="tt-radio-list" data-v2="sales-agent-options">
        <button v-for="o in salesAgentItems" :key="o.id" type="button" class="cv2-btn cv2-btn--quiet"
          :class="{ 'cv2-btn--primary': o.id === salesAgentPicked }" :data-v2-sales-agent="o.id" @click="chooseSalesAgent(o.id)">{{ salesAgentOptionNames.get(o.id) }}</button>
      </div>
    </el-dialog>

    <!-- 快速指派层 -->
    <el-dialog
      :model-value="assignDialogOpen"
      :title="KSZP_DIALOG_TITLE"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-assign"
      @update:model-value="assignDialogOpen = false"
    >
      <div class="tt-frow">
        <div class="tt-fitem tt-grow-item">
          <span class="tt-flabel">搜索</span>
          <el-input
            v-model="assignSearch"
            :placeholder="KSZP_SEARCH_PLACEHOLDER"
            data-v2-field="assign-search"
            class="tt-grow"
          />
        </div>
        <div class="tt-fitem">
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find cv2-btn--fill" data-v2="assign-search-btn" @click="searchCandidates">搜索</button>
          <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--quiet" data-v2="assign-reset-btn" @click="resetCandidateSearch">重置</button>
        </div>
      </div>
      <div v-if="assignSearched" class="tt-frow">
        <div class="tt-fitem">
          <span class="tt-flabel">搜索结果</span>
          <div class="tt-radio-list" data-v2="assign-candidates">
            <button
              v-for="row in candidates"
              :key="row.id"
              type="button"
              class="cv2-btn cv2-btn--quiet"
              :data-v2-assignee="row.id"
              @click="chooseAssignee(String(row.id))"
            >{{ row.name }}</button>
            <p v-if="candidates.length === 0">没有匹配的接手人</p>
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 「结果未知」二次确认（island layer.confirm 的炭火版；文案逐字）；⭐ 弹窗档 = sm 480 -->
    <el-dialog
      :model-value="resubmitOpen"
      :title="RESUBMIT_DIALOG_TITLE"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-resubmit"
      @update:model-value="resubmitOpen = false"
    >
      <p class="cv2-dlg-text">{{ RESUBMIT_CONFIRM_MESSAGE }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="resubmitOpen = false">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="resubmit-confirm" @click="askToResubmit">
            确定
          </button>
        </div>
      </template>
    </el-dialog>

    <!-- 炭火 toast（页面内自绘） -->
    <div
      v-if="toast"
      class="cv2-toast"
      :class="{ 'cv2-toast--error': toast.kind === 'error' }"
      role="status"
      data-v2="toast"
    >
      {{ toast.text }}
    </div>

    <!-- 录单成功大浮层（⭐ 业务方「反馈太弱」）：屏中居中、青绿圆勾、1.8s 自散 -->
    <Transition name="cv2-flash">
      <div v-if="saveFlash" class="cv2-save-flash" data-v2="save-flash">
        <span class="cv2-save-flash-badge"><i class="layui-icon layui-icon-ok" aria-hidden="true"></i></span>
        <span class="cv2-save-flash-text">{{ SAVE_SUCCESS_MESSAGE }}</span>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
/* ═══ 录单页版式（炭火皮；只引用 --tt2-* token，不写裸色值）══════════════════════ */

/* 两列栅格：左粘贴区 3 / 右表单 9（对标 layui-col-md3/md9）。 */
.tt-ord-grid {
  display: grid;
  grid-template-columns: minmax(250px, 3fr) 9fr;
  gap: 18px;
  margin-top: 18px;
  align-items: start;
}

/* 编辑态：左列粘贴识别区不渲染 ⇒ 单列（弹窗里就是一张右栏表单）。 */
.tt-ord-grid--single {
  grid-template-columns: 1fr;
}

.tt-paste-panel {
  margin-top: 0;
  /* #fbfbfb = 对标证据件字面色，业务方 2026-11-06 指定（页面级特例，压过全局 .cv2-panel 的底色）。 */
  background: #fbfbfb;
  border-color: var(--tt2-tool-line);
  /* 业务方「不需要左右内边距」：压过全局 .cv2-panel 的 18px 左右 padding，上下 16px 保留。 */
  padding: 16px 0;
}

.tt-paste-label {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--tt2-ink);
}

.tt-paste-panel :deep(.el-textarea__inner) {
  min-height: 420px;
  font-size: 13px;
}

.tt-paste-quote {
  margin: 10px 0 0;
  padding: 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  font-size: 13px;
}

/* ⭐ 表单字段网格化 + 自适应（「可拖宽」已撤，见下）：右列整体限宽 880。 */
.tt-ord-form {
  min-width: 0;
  max-width: 880px;
}

/* ⭐ 主流程区：控件抬 38px「大而舒展」。EP 尺寸走 CSS 变量：
 * el-input 的 `--el-input-height` = var(--el-component-size)，容器上改变量即可；
 * el-select wrapper / el-radio 是写死的 32px，得就地覆盖。折叠区不在此作用域 ⇒ 照全局 34px。 */
.tt-main {
  --el-component-size: 38px;
  --el-checkbox-height: 38px;
  padding-bottom: 4px;
}

.tt-main :deep(.el-select__wrapper) {
  min-height: 38px;
}

.tt-main :deep(.el-radio) {
  height: 38px;
}

.tt-main .tt-frow {
  gap: 10px 12px;
  margin-bottom: 18px;
}

/* 「更多设置（偶尔用）」折叠区：面板式折叠，滑动照侧栏子菜单的
 * CSS grid 0fr↔1fr 手法（高度自适应内容、无需 max-height 魔数）；内容常驻 DOM
 * （收起 = 行高压到 0fr + visibility:hidden，后者让它退出 a11y 树与焦点序）。 */
.tt-more {
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
}

.tt-more-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  color: var(--tt2-ink);
  text-align: left;
}

.tt-more-head:hover {
  color: var(--tt2-primary-deep);
}

.tt-more-arrow {
  flex: none;
  font-size: 12px;
  color: var(--tt2-dim);
  transition: transform var(--tt2-motion) ease;
}

.tt-more--open .tt-more-arrow {
  transform: rotate(90deg);
}

.tt-more-body {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--tt2-motion) ease;
}

.tt-more--open .tt-more-body {
  grid-template-rows: 1fr;
}

.tt-more-inner {
  min-height: 0;
  overflow: hidden;
  visibility: hidden;
  padding: 2px 14px 8px;
  transition: visibility var(--tt2-motion);
}

.tt-more--open .tt-more-inner {
  visibility: visible;
}

/* reduced-motion：滑动直切（业务方口径「无载入动画」的同族要求）。 */
@media (prefers-reduced-motion: reduce) {
  .tt-more-body,
  .tt-more-arrow,
  .tt-more-inner {
    transition: none;
  }
}

.tt-frow {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 8px 12px;
  margin-bottom: 14px;
}

/* 三格等分行（联系三连）：右列总宽 880，三等分每格 ~285px——.tt-grow 的 260px basis
 * 加标签格必超（flex 换行按 basis 判；业务方圈红的「标签叠上去」即此），
 * 三等分行内把弹性 basis 收小让「标签+控件」稳放一行，余宽照吃。 */
.tt-frow--thirds .tt-fitem {
  flex: 1 1 0;
  min-width: 240px;
}

.tt-frow--thirds .tt-fitem > .tt-grow {
  flex: 1 1 160px;
  min-width: 120px;
}

/* ═══ 连体字段格（⭐ 业务方「每个字段的长度自适应…字段之间可以手动
 * 拉长拉宽」+「像对标一样加上网格」）═══════════════════════════════════════════════
 * 标签 = 带边框浅灰底的格子，与输入框**零间距连体**（标签格右边框 = 输入框左缘边线，
 * 合成一个整格；内缝全部直角、外角 4px 由包裹层的 overflow clip 给出——上一轮的
 * 「纯文字标签散排」版式整体作废，上一轮的 110/120px 标签列一并清干净）。
 * ⭐ 业务方「每个输入框右下角都有那些斜杆 这个删掉不需要」：原「可拖宽」
 * 用的包裹层 resize: horizontal（原生柄就是那两道斜杆）整项撤掉，格宽由行布局决定。 */
.tt-fitem {
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  flex: 1 1 auto; /* ⭐ 字段容器撑满行宽——否则输入框 100% 只等于内容宽（190px 挤压的根因） */
  gap: 0;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  resize: none;
  border-radius: 4px;
}

/* 名字格用筛选栏的暖米。右边框去掉，和输入框共用一条线。 */
.tt-flabel {
  flex: none;
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  border: 1px solid var(--tt2-filter-line);
  border-right: none;
  background: var(--tt2-filter-label-bg);
  font-size: 13px;
  color: var(--tt2-dim);
  white-space: nowrap;
}

/* 值区（radio 组 / checkbox / 按钮这类**没有自带边框**的控件套它）：与标签格同族连体的
 * 白底格子；高度随控件档（主流程 38 / 折叠区 34，走 --el-component-size）。 */
.tt-fvalue {
  flex: 1 1 auto;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  padding: 0 12px;
  min-height: var(--el-component-size);
  border: 1px solid var(--tt2-filter-line);
  background: var(--tt2-panel);
}

/* 自带边框的控件（EP input/select / 原生 .cv2-input）：内缝直角（外角由包裹层 clip）、
 * 吃满连体格的剩余宽（行布局决定）、与前一格共享 1px 边线（-1px 重叠）。 */
.tt-fitem > :deep(.el-input),
.tt-fitem > :deep(.el-select),
.tt-fitem > .tt-notopen-shell {
  flex: 1 1 auto;
  min-width: 0;
  margin-left: -1px;
}

/* 紧跟标签格的第一个控件不重叠（标签右边框已去掉，它的左缘就是合缝线）。 */
.tt-fitem > .tt-flabel + :deep(.el-input),
.tt-fitem > .tt-flabel + :deep(.el-select),
.tt-fitem > .tt-flabel + .tt-notopen-shell,
.tt-fitem > .tt-flabel + .tt-fvalue {
  margin-left: 0;
}

.tt-fitem :deep(.el-input__wrapper),
.tt-fitem :deep(.el-select__wrapper) {
  border-radius: 0;
}

.tt-fitem > .cv2-input {
  flex: 1 1 auto;
  min-width: 0;
  border-radius: 0;
  margin-left: -1px;
}

.tt-fitem > .tt-flabel + .cv2-input {
  margin-left: 0;
}

.tt-fitem > :deep(.el-checkbox) {
  margin-left: 0;
}

/* 弹性大字段（代练项目/标题、重要备注、KSZP 搜索）：保证最小可用宽，吃满连体格余宽。 */
.tt-fitem > .tt-grow {
  flex: 1 1 260px;
  min-width: 200px;
}

/* 裸按钮对（KSZP 搜索行的 搜索/重置）在 gap 0 的连体格里补回按钮间距。 */
.tt-fitem .cv2-btn + .cv2-btn {
  margin-left: 8px;
}

/* 字段旁瞬时提示：换行落在连体格下缘（gap 0 的连体格里补回与控件的呼吸空隙）。 */
.tt-tip {
  flex-basis: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tt2-primary-deep);
}

.tt-flabel--strong {
  color: var(--tt2-ink);
  font-weight: 600;
}

/* 代练项目、来源单号、订单总价的名字用金额红。 */
.tt-flabel--accent {
  color: var(--tt2-money-red);
  font-weight: 600;
}

.tt-strong :deep(.el-input__inner) {
  font-weight: 600;
}

/* 代练项目、订单总价的边和旁边格子一样：平时细灰线，悬停和聚焦才转青绿。 */
.tt-accent-box :deep(.el-input__wrapper),
.tt-accent-box :deep(.el-textarea__inner) {
  box-shadow: 0 0 0 1px var(--tt2-filter-line) inset;
  border-radius: 0;
}
.tt-accent-box :deep(.el-textarea__inner) {
  resize: none;
}
.tt-accent-box :deep(.el-input__wrapper:hover),
.tt-accent-box :deep(.el-textarea__inner:hover) {
  box-shadow: 0 0 0 1px var(--tt2-primary) inset;
}
.tt-accent-box :deep(.el-input__wrapper.is-focus),
.tt-accent-box :deep(.el-textarea__inner:focus) {
  box-shadow:
    0 0 0 1px var(--tt2-primary) inset,
    0 0 0 3px var(--tt2-focus-ring);
}

/* 价格格并入「总价+时间+加急」多格行：.tt-grow 的 260px 弹性 basis
 * 会把「标签格+输入格」顶破价格格分到的列宽 → 标签被挤到输入框上方（与联系三连同因），
 * 价格输入的弹性 basis 收到 220px（min 160），行内分配后稳放一行。 */
/* 价格格自身份额钉 340：格内「标签 72 + 输入 basis 220」稳放一行；380 会把加急格
 * 挤下换行（三格同排是业务方要求），340 恰好三格合计 856 ≤ 880 列宽。 */
.tt-fitem.tt-price-item {
  flex: 1 1 340px;
}

.tt-fitem > .tt-price {
  flex: 1 1 220px;
  min-width: 160px;
}

/* 价格数字用同一红。占位符保持灰，不跟着变红。 */
.tt-price :deep(.el-input__inner) {
  color: var(--tt2-money-red);
  font-weight: 600;
}

.tt-price :deep(.el-input__inner::placeholder) {
  color: var(--tt2-filter-ph);
  font-weight: 400;
}

/* 控件定宽（尺寸非色值）：对齐证据件里各格的窄宽（90/70/132/160…收成四档）。 */
.tt-w-xs { width: 96px; }
.tt-w-sm { width: 110px; }
.tt-w-md { width: 160px; }
.tt-w-lg { width: 190px; }

/* ⭐ 第三轮（业务方）：来源单号输入框收短（原 tt-grow 铺满）。 */

/* 空框里的灰字跟筛选栏同一浅灰。挂在本页格子上，才能盖过皮里的深灰。 */
.tt-ord-grid :deep(.el-input__inner::placeholder),
.tt-ord-grid :deep(.el-textarea__inner::placeholder),
.tt-ord-grid :deep(.el-select__placeholder.is-transparent) {
  color: var(--tt2-filter-ph);
}

/* ⭐ 第三轮（业务方）：销售客服网格字体与圆圈加大一档。 */
.tt-radio-list.el-radio-group {
  gap: 10px 22px;
}
.tt-radio-list .el-radio {
  height: 30px;
  margin-right: 0;
}
.tt-radio-list .el-radio__label {
  font-size: 14px;
  color: var(--tt2-ink);
}
.tt-radio-list .el-radio__inner {
  width: 17px;
  height: 17px;
}
.tt-radio-list .el-radio.is-checked .el-radio__label {
  color: var(--tt2-primary-deep);
  font-weight: 600;
}

.tt-grow {
  flex: 1 1 260px;
  min-width: 0;
}

/* 「旺旺/YY」输入框内左侧的新老客三段控件（不选 / 新 / 老）：与文字同一行。 */
.tt-ww-input :deep(.el-input__prefix) {
  margin-right: 0;
}
.tt-kind-seg {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  align-self: stretch;
  border-right: 1px solid var(--tt2-filter-line);
}
.tt-kind-btn {
  height: 24px;
  padding: 0 5px;
  border: 0;
  background: transparent;
  color: var(--tt2-dim);
  font-size: 12px;
  line-height: 24px;
  cursor: pointer;
}
.tt-kind-btn:last-child {
  border-right: 0;
}
.tt-kind-btn--none.tt-kind-btn--on {
  background: var(--tt2-filter-label-bg);
  color: var(--tt2-primary-deep);
}
.tt-kind-btn--new.tt-kind-btn--on {
  background: var(--tt2-st-doing-bg);
  color: var(--tt2-st-doing);
}
.tt-kind-btn--old.tt-kind-btn--on {
  background: var(--tt2-tint-amber-bg);
  color: var(--tt2-tint-amber);
}

.tt-grow-item {
  flex: 1 1 320px;
}

.tt-panel-card {
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
}

.tt-card-head {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.tt-card-hint {
  color: var(--tt2-primary);
  font-size: 12px;
  font-weight: 400;
}

.tt-card-link {
  font-size: 12px;
}

.tt-intake-split {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.tt-intake-split-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(70px, 120px) auto;
  align-items: start;
  gap: 8px;
}

.tt-intake-split-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px 12px;
}

.tt-check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  min-width: 0;
  max-width: 100%;
  padding: 4px 0;
}

.tt-check-list :deep(.el-checkbox) {
  height: auto;
  max-width: 100%;
  margin-right: 0;
  white-space: normal;
}

.tt-check-list :deep(.el-checkbox__label) {
  overflow-wrap: anywhere;
}

.tt-assignee-control {
  display: inline-flex;
  min-width: 0;
}

.tt-assignee-control > .tt-assignee-picked:first-child {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
}

.tt-assignee-control > .tt-assignee-clear {
  flex: 0 0 30px;
  margin-left: -1px;
  padding: 0;
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  font-size: 18px;
}

.tt-assignee-clear:focus-visible { z-index: 1; }

.tt-dispatch-row .tt-dispatch-item {
  flex: 0 1 35%;
  min-width: 220px;
}

.tt-dispatch-row .tt-grow-item {
  flex: 1 1 300px;
}

.tt-project-categories {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 0;
  padding: 6px 0;
  border-bottom: 1px solid var(--tt2-line);
}

.tt-project-categories .tt-fvalue {
  min-width: 0;
}

.tt-project-categories :deep(.el-radio-group) {
  gap: 4px 12px;
}

.tt-project-categories :deep(.el-radio) {
  margin-right: 0;
}

.tt-mt8 {
  margin-top: 8px;
}

.tt-radio-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
}

.tt-actions {
  margin-top: 6px;
}

/* ⭐ 业务方「提交保存按钮加大一些」：190→260 宽、44 高、15px 字。 */
.tt-save-btn {
  width: 260px;
  min-height: 44px;
  font-size: 15px;
}

/* 录单成功大浮层（⭐ 反馈升级）：屏中居中、无遮罩不拦操作、进出淡缩。 */
.cv2-save-flash {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: var(--tt2-z-chrome);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 28px;
  border-radius: 10px;
  background: var(--tt2-panel);
  box-shadow: 0 10px 40px var(--tt2-flash-shadow);
}

.cv2-save-flash-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--tt2-primary);
  color: var(--tt2-on-primary);
  font-size: 22px;
}

.cv2-save-flash-text {
  font-size: 20px;
  font-weight: 700;
  color: var(--tt2-primary-deep);
}

.cv2-flash-enter-active,
.cv2-flash-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.cv2-flash-enter-from,
.cv2-flash-leave-to {
  opacity: 0;
  transform: translate(-50%, -50%) scale(0.92);
}

/* 「尚未开放」的壳：包着对标有、后端没有的控件；点击被捕获拦截（控件不置灰、点不进）。
 * 连体网格里壳是 flex 项：吃满余宽、内部的死下拉随壳铺满（不露白边）。 */
.tt-notopen-shell {
  display: inline-flex;
  align-items: center;
  cursor: not-allowed;
}

.tt-notopen-shell .el-select {
  width: 100%;
}

/* ═══ 响应式（断点口径见 carbon.css 文件头注释）═══════════
 * 纯 CSS 重排：堆叠/竖排不隐藏任何字段锚点（「堆叠 ≠ 隐藏」，字段恰好集合网照旧钉 DOM）。
 * ⭐ 业务方一直在 ~620px 宽面板里看本页——两列栅格与标签左置的连体格在那个宽度下
 * 会塌成一字一行竖排，这两档就是给它用的。 */
@media (max-width: 900px) {
  /* 左粘贴区 + 右表单两列 → 上下堆叠（粘贴区在上、表单在下，各自全宽）。 */
  .tt-ord-grid {
    grid-template-columns: 1fr;
  }

  .tt-ord-form {
    max-width: none;
  }

  /* 堆叠后粘贴框别占半屏（420px 是两列档给左列的定值）。 */
  .tt-paste-panel :deep(.el-textarea__inner) {
    min-height: 180px;
  }
}

@media (max-width: 720px) {
  /* ⭐ 响应式追加：固定宽度控件（xs/sm/md/lg/src）在窄档全部放开为全宽。
   * !important：压过组件内更后定义的 .tt-w-* 固定宽（同特异性下源码顺序会反过来）。 */
  .ttv2 .tt-w-xs, .ttv2 .tt-w-sm, .ttv2 .tt-w-md, .ttv2 .tt-w-lg, .ttv2 .tt-w-src {
    width: 100% !important;
  }
  /* 字段改「标签在上、控件在下、控件全宽」：连体格竖排（缝用 -1px 纵向合边线）；
   * 窄屏横向拖宽无意义 ⇒ resize 收回（overflow:hidden 保留，外角 4px 的 clip 还靠它）。 */
  .tt-fitem {
    flex-direction: column;
    align-items: stretch;
    resize: none;
  }

  /* 标签格横过来铺满：右边框补回（自己的下缘才是合缝线）。 */
  .tt-flabel {
    width: 100%;
    padding: 7px 10px;
    border-right: 1px solid var(--tt2-filter-line);
  }

  /* 控件全宽：定宽档（tt-w-*，含 tt-w-src 360）与连体 -1px 横缝全部压掉，改纵向 -1px 合缝；
   * 同格多控件（时间要求天/小时、游戏级联）逐个纵向接排、边线合并，不断裂。 */
  .tt-fitem > :deep(.el-input),
  .tt-fitem > :deep(.el-select),
  .tt-fitem > .tt-notopen-shell,
  .tt-fitem > .tt-fvalue,
  .tt-fitem > .cv2-input {
    width: 100%;
    margin-left: 0;
    margin-top: -1px;
  }

  .tt-fitem > .tt-grow {
    min-width: 0;
  }

  /* 提交按钮窄屏铺满（190px 定宽在竖排档没有意义）。 */
  .tt-save-btn {
    width: 100%;
  }
}
</style>
