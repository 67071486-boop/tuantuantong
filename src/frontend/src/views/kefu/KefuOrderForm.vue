<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, useId, useTemplateRef, watchPostEffect } from 'vue'
import {
  f7AccordionContent as F7AccordionContent,
  f7AccordionItem as F7AccordionItem,
  f7Button as F7Button,
  f7Checkbox as F7Checkbox,
  f7List as F7List,
  f7ListInput as F7ListInput,
  f7Segmented as F7Segmented,
} from 'framework7-vue'
import KefuPicker from './KefuPicker.vue'
import KefuOrderScreenshot from './KefuOrderScreenshot.vue'
import KefuPeerDispatch from './KefuPeerDispatch.vue'
import { attachKefuOrderScreenshot, newScreenshotUpload, type ScreenshotUpload } from './orderScreenshotUpload'
import {
  createOrder,
  fetchAssigneeOptions,
  fetchKefuOptions,
  fetchOrder,
  readPlatformOrder,
  updateOrderWithMessage,
  type AssigneeOption,
  type CreateOrderPayload,
  type Option,
  type Order,
  type UpdateOrderPayload,
} from '@/api/orders'
import { fetchOrderSources } from '@/api/orderSources'
import { assignPeerTaker, fetchPeerDirectAssignment, type KefuPeerSelection } from '@/api/peerDirectAssignment'
import { fetchGames, fetchServers, listConfig, type ConfigRow } from '@/api/config'
import { BizError } from '@/api/errors'
import { ErrorCode } from '@/config/errorCodes'
import {
  MONEY_FIELD_LABELS,
  ORDER_MONEY_FIELDS,
  isBlankLikePython,
  validateMoneyInput,
  type OrderMoneyField,
} from '@/config/money'
import { useSessionStore } from '@/stores/session'
import { rememberAssigneeValue } from '@/views/orders/assigneePreference'
// 拼/删与 700 字节同步闸：与录单岛页**共用同一份**实现（岛页再导出，源头在共享模块）。
import { projectTextSyncLimitError, toggleProjectName } from '@/views/orders/projectText'
import { detectGameFromText } from '@/views/orders/gameDetect'
import {
  currentActor,
  emptyOrderForm,
  formToPayload,
  type ConfigOption,
  type OrderForm,
} from '@/views/orders/orderFormFields'

type NoticeKind = 'error' | 'success' | 'info'

/** 复用管理后台的录单载荷和字段默认值。 */
type KefuForm = OrderForm & { order_type: 'BOOST' | 'COMPANION' }

/**
 * 派单方式（业务方定稿：三档）：`none` 只建单（没有个人偏好时的默认档）；`assign` 建单 + `assignee`（指派给某个打手）；`hall` 建单 + `throw_to_hall` + `hall_price`（建完直接进抢单大厅）。
 */
type AssignMode = 'none' | 'assign' | 'hall'

/**
 * 「常用项目」一次取多少条：**200**（后端分页上限现为 **300**：`EnvelopePagination.max_page_size`；本页未顶格）。
 * ⚠️ 超过 200 个项目的游戏只能看到前 200 个（`projectsTruncated` 会如实提示）——本页取数 200，
 * 本页不另造「取全量」的第二条取数链（与 `api/config.ts` 的其它下拉同一条口径）。
 */
const PROJECT_PAGE_SIZE = 200

const props = defineProps<{ orderNo: string }>()
const emit = defineEmits<{
  back: []
  saved: [payload: { orderNo: string; message: string }]
  createdFailed: [message: string]
}>()

const session = useSessionStore()
const isEdit = computed(() => props.orderNo !== '')

const form = ref<KefuForm>(emptyForm())
/**
 * 表单自己的提示（读单回填 / 校验失败）。⚠️ **位置放在底部操作条里**（见模板），不是页面顶端：客服点的是底部的「保存」，把「项目名称不可为空」显示在页面最上面等于让他自己去滚回去找原因。
 */
const formNotice = ref('')
const formNoticeKind = ref<NoticeKind>('info')
const submitting = ref(false)
const reading = ref(false)
const loadingOrder = ref(false)
/** 最近一次「读平台订单」带回来的买家身份（闲管家 buyer_eid）；建单时随载荷回传，和阿奇索/旧录单页同一条口径。 */
const pendingBuyer = ref<{ eid: string; nick: string } | null>(null)
/** 录单时的「新客/老客」手动覆盖；空串 = 不覆盖（有 buyer_eid 时由后端自动判定）。 */
const customerKindOverride = ref<'' | 'new' | 'old'>('')

const games = ref<ConfigOption[]>([])
const serverOptions = ref<ConfigOption[]>([])
const projectOptions = ref<ConfigOption[]>([])
/** 项目超过一页（200）时置真 ⇒ 界面上如实说「只显示前 200 个」。 */
const projectsTruncated = ref(false)
const projectsLoading = ref(false)
const sourceOptions = ref<ConfigOption[]>([])

/** 「常用项目」选中的项目 id（勾选后按 `/` 拼进「项目名称」，见 `onProjectsChange`）。 */
const pickedProjects = ref<number[]>([])
/** 上一次的选中集合：用来算出「这次加了哪一个 / 去掉了哪一个」，好做增量拼/删。 */
let prevPicked: number[] = []
/** 「项目名称」那一格的容器：保存时为空就滚过去 + 聚焦（见 `focusProjectField`）。 */
const projectFieldEl = ref<HTMLElement | null>(null)

const assigneeId = ref<number | null>(null)
const assigneeOptions = ref<AssigneeOption[]>([])
const assigneeTruncated = ref(false)
const rememberChecked = ref(false)
const rememberTouched = ref(false)
/** 服务端记着的默认接手人 / 默认档位（决定「取消勾选」该不该发 `'N'`，同录单页）。 */
const initialDefaultAssigneeId = ref(0)
const initialIsHall = ref(false)

const kefuOptions = ref<Option[]>([])
const kefuTruncated = ref(false)
const salesUserId = ref<number | null>(null)
const initialSalesAgentName = ref('')

/** 进来时订单已有的来源 id（编辑态「用户清空来源」要显式发 `null` 才清得掉，同录单页 T6）。 */
const initialOrderSource = ref<number | null>(null)
/**
 * 进来时的游戏 id（编辑态「换了游戏」的判据）。
 * 换游戏后旧区服 / 项目 FK 属于上一款游戏 ⇒ PATCH 必须**显式发 null**（见 `saveEdit`），
 * 否则「空值不发」会让库里留下「游戏=B、区服/项目还是 A 的」。
 */
const initialGame = ref<number | null>(null)

const canSaveOrder = computed(() =>
  isEdit.value
    ? session.canNode(['order.add_edit', 'order.submit_edit'])
    : session.canNode(['order.add_edit']),
)
const canAssign = computed(() => session.canNode(['order.assign_taker']))
const canAssignPeer = computed(() => canAssign.value
  && (session.user?.is_tenant_admin || !session.canNode(['order.restrict_view_offer_price'])))
const canPublishToHall = computed(() => session.canNode(['order.send_to_grab_hall']))
const defaultAssignMode = computed<AssignMode>(() => !isEdit.value && canPublishToHall.value ? 'hall' : 'none')
const assignMode = ref<AssignMode>(defaultAssignMode.value)
const assignTarget = ref<'local' | 'peer'>('local')
const moreOpen = ref(false)
const peerSelection = ref<KefuPeerSelection | null>(null)
const canChangeSales = computed(() => session.canNode(['order.allow_change_sales_agent']))
const canUploadScreenshot = computed(() => session.canNode(['order.upload_screenshot']))
const screenshotFiles = shallowRef<File[]>([])
const screenshotBusy = ref(false)
const screenshotKey = ref(0)

type SavedOrder = { orderNo: string; message: string; dispatchFailed?: true }
type SaveFollowUp = {
  order: SavedOrder
  uploads: ScreenshotUpload[]
  screenshotMessage: string
  peer: KefuPeerSelection | null
  reselectPeer?: true
}
const pendingFollowUp = ref<SaveFollowUp | null>(null)
const needsPeerReselection = computed(() => pendingFollowUp.value?.reselectPeer === true)
const pendingScreenshot = computed(() => pendingFollowUp.value?.uploads[0])
const replacingScreenshot = computed(() => Boolean(pendingScreenshot.value?.canReplace))
const blocksNavigation = computed(() => submitting.value || pendingFollowUp.value !== null)
const wantsPeerDispatch = computed(() =>
  canAssignPeer.value && (isEdit.value || assignMode.value === 'assign' && assignTarget.value === 'peer'),
)
const recoveringPeer = computed(() => isEdit.value && peerSelection.value !== null && !peerSelection.value.match_token)
const orderFieldsReadOnly = computed(() => replacingScreenshot.value || recoveringPeer.value)


const myUserId = computed(() => session.user?.id ?? null)

const kefuOptionsFailed = ref(false)
const kefuOptionsLoading = ref(false)

const myName = computed(() => session.user?.display_name || session.user?.username || '')
const mySalesLabel = computed(() => myName.value ? `我自己（${myName.value}）` : '我自己')
const salesOptionsNotice = computed(() => {
  if (kefuOptionsFailed.value) return '候选加载失败，只能选自己'
  if (kefuTruncated.value) return '候选名单过长，只显示前 200 人'
  return undefined
})

function chooseSalesUser(id: number | null): void {
  salesUserId.value = id === myUserId.value ? null : id
}

const formId = useId()
const fieldId = (name: string) => `${formId}-${name}`
const additionalFields: readonly {
  name: keyof Pick<KefuForm, 'time_requirement' | 'customer_phone' | 'customer_ww' | 'customer_qq'
    | 'game_account' | 'game_sub_account' | 'game_role_name' | 'current_info_1' | 'current_info_2'>
  label: string
  placeholder?: string
}[] = [
  { name: 'time_requirement', label: '时间要求', placeholder: '如 2天3小时' },
  { name: 'customer_phone', label: '联系电话' },
  { name: 'customer_ww', label: '旺旺/YY' },
  { name: 'customer_qq', label: 'QQ' },
  { name: 'game_account', label: '游戏账号' },
  { name: 'game_sub_account', label: '子账号' },
  { name: 'game_role_name', label: '角色名' },
  { name: 'current_info_1', label: '当前信息1' },
  { name: 'current_info_2', label: '当前信息2' },
]
const additionalPrices = [
  { name: 'received_price', label: '接单实收' },
  { name: 'platform_price', label: '发平台价' },
  { name: 'fee', label: '手续费' },
] as const

const dispatchOptions = computed(() => [
  { mode: 'hall', label: '内部抢单', allowed: canPublishToHall.value },
  { mode: 'assign', label: '内部指派', allowed: canAssign.value },
  { mode: 'none', label: '不派单', allowed: true },
] as const satisfies readonly { mode: AssignMode; label: string; allowed: boolean }[])
const allowedDispatchOptions = computed(() => dispatchOptions.value.filter((option) => option.allowed))

function emptyForm(): KefuForm {
  // 订单总价默认 0（D-0082：录单把总价改成必填）。
  return { ...emptyOrderForm(), total_price: '0', order_type: 'BOOST' }
}

function messageOf(e: unknown, fallback = '操作失败'): string {
  return e instanceof Error && e.message !== '' ? e.message : fallback
}

function notify(kind: NoticeKind, message: string): void {
  formNoticeKind.value = kind
  formNotice.value = message
}

/* ── 字典 / 游戏相关的下拉 ── */

async function loadDicts(): Promise<void> {
  try {
    games.value = await fetchGames()
  } catch (e) {
    notify('error', messageOf(e, '游戏字典加载失败'))
  }
  try {
    const page = await fetchOrderSources()
    sourceOptions.value = page.items.map((row) => ({ id: row.id, name: row.name }))
  } catch (e) {
    notify('error', messageOf(e, '订单来源加载失败'))
  }
  if (!isEdit.value) {
    // 「接单销售客服」候选（`limit` 与录单页同档 200）：候选拉不到**不拦录单**，
    // 但界面上要说清「只能选自己」（M10 / §2.7），⛔ 不静默降级成一个空名单。
    kefuOptionsFailed.value = false
    kefuOptionsLoading.value = true
    try {
      const res = await fetchKefuOptions({ limit: 200 })
      kefuOptions.value = res.items
      kefuTruncated.value = res.truncated
    } catch {
      kefuOptions.value = []
      kefuTruncated.value = false
      kefuOptionsFailed.value = true
    } finally {
      kefuOptionsLoading.value = false
    }
  }
}

/** 来源下拉里补一条字典外的项（后端自动判定时**刚建**的来源不在首屏字典里，同录单页）。 */
function ensureSourceOption(id: number, name: string): void {
  if (sourceOptions.value.some((s) => s.id === id)) return
  sourceOptions.value = [...sourceOptions.value, { id, name: name || `来源 ${id}` }]
}

let serverToken = 0
async function loadServers(gameId: number): Promise<void> {
  const token = ++serverToken
  try {
    const servers = await fetchServers(gameId)
    // 连点两次游戏时，先发的慢响应不能覆盖后发的（与录单页同一道守卫）。
    if (token !== serverToken) return
    serverOptions.value = servers.map((s) => ({ id: s.id, name: s.name }))
  } catch (e) {
    // 旧游戏的慢失败不能写到新游戏的界面上（与响应同样的 token 守卫）。
    if (token !== serverToken) return
    notify('error', messageOf(e, '区服加载失败'))
  }
}

let projectToken = 0
async function loadProjects(gameId: number): Promise<void> {
  const token = ++projectToken
  projectsLoading.value = true
  try {
    // 业务方 2026-10-20：**不要分类下拉**，直接按游戏拉全部常用项目。
    // `GET /api/business/projects/?game=<id>` 是既有能力（后端 `ProjectViewSet` 的 docstring 写明
    // `?game=` 会被翻译成 `category__game`）⇒ 走 `listConfig`，⛔ 不新造 API、不改 `api/config.ts` 签名。
    const page = await listConfig<ConfigRow>('projects', {
      game: gameId,
      page_size: PROJECT_PAGE_SIZE,
    })
    if (token !== projectToken) return
    projectOptions.value = page.items.map((p) => ({ id: p.id, name: p.name }))
    projectsTruncated.value = page.total > page.items.length
  } catch (e) {
    // 同上：旧游戏的慢失败一律丢弃，⛔ 不把提示写到新游戏上。
    if (token !== projectToken) return
    notify('error', messageOf(e, '常用项目加载失败'))
  } finally {
    if (token === projectToken) projectsLoading.value = false
  }
}

function onGameChange(gameId: number | null): void {
  // 换游戏 ⇒ 常用项目列表换了一批，折叠状态也回默认（否则新游戏的列表一进来就是展开的）。
  projectsExpanded.value = false
  // 切游戏 ⇒ 区服 / 常用项目 / 项目 FK 全部作废（它们都属于上一款游戏）。
  // `project_text` **不清**：那是客服手敲过、也可能改过的自由文本（录单页同样只清 FK 类字段），
  // 清掉会把人工输入一起抹掉。
  form.value.server = null
  form.value.project = null
  pickedProjects.value = []
  prevPicked = []
  serverOptions.value = []
  projectOptions.value = []
  projectsTruncated.value = false
  if (typeof gameId === 'number') {
    void loadServers(gameId)
    void loadProjects(gameId)
  }
}

/**
 * 勾选常用项目 ⇒ 按 `/` 拼进「项目名称」（算法逐条照录单页 `toggleProjectText`）：
 * 勾上加 `名称`、取消时删掉它（连同可能的前导 `/`），客服随后可以继续手改这段文本。
 */
function onProjectsChange(value: unknown): void {
  // 常用项目只接受数字 id。
  // ⛔ 不用 `as number[]` 骗过类型。
  const ids = Array.isArray(value)
    ? value.filter((item): item is number => typeof item === 'number')
    : []
  let text = form.value.project_text
  const nameOf = (id: number) => projectOptions.value.find((p) => p.id === id)?.name ?? ''
  // 拼/删都走**共享实现**（`views/orders/projectText.ts`，录单岛页同一份）：
  // 取消勾选按 `/` 分词做**整词精确匹配**，⛔ 不能用子串 replace
  //（「星耀」是「星耀上王者」的前缀，子串删会把后者改成「上王者」）。
  for (const id of prevPicked.filter((old) => !ids.includes(old))) {
    text = toggleProjectName(text, nameOf(id), false)
  }
  for (const id of ids.filter((now) => !prevPicked.includes(now))) {
    text = toggleProjectName(text, nameOf(id), true)
  }
  form.value.project_text = text
  // FK 只在**恰好一项**时落（与录单页同一判据：多项时以自由文本为准）。
  form.value.project = ids.length === 1 ? (ids[0] as number) : null
  pickedProjects.value = [...ids]
  prevPicked = [...ids]
}

const projectsExpanded = ref(false)
const projectChips = useTemplateRef<HTMLElement>('projectChips')
const hiddenProjectIds = ref(new Set<number>())

// 收起时优先保留选中项，方便直接取消。
const orderedProjects = computed(() => {
  if (projectsExpanded.value) return projectOptions.value
  const selected = new Set(pickedProjects.value)
  return [
    ...projectOptions.value.filter((project) => selected.has(project.id)),
    ...projectOptions.value.filter((project) => !selected.has(project.id)),
  ]
})

function measureProjectRows(list: HTMLElement): void {
  // 收起后只露出首行，不能沿用展开时的滚动位置。
  if (!projectsExpanded.value && list.scrollTop !== 0) list.scrollTop = 0
  const chips = Array.from(list.querySelectorAll<HTMLElement>('.kefu-project-chip'))
  const available = list.parentElement?.clientWidth ?? 0
  const gap = Number.parseFloat(getComputedStyle(list).columnGap) || 0
  const fullWidth = chips.reduce((width, chip) => width + chip.offsetWidth, 0) + Math.max(0, chips.length - 1) * gap
  if (available > 0 && fullWidth <= available) {
    hiddenProjectIds.value = new Set()
    return
  }
  const firstRowTop = chips[0]?.offsetTop
  hiddenProjectIds.value = new Set(
    chips.filter((chip) => chip.offsetTop !== firstRowTop).map((chip) => Number(chip.dataset.projectId)),
  )
}

watchPostEffect((onCleanup) => {
  const list = projectChips.value
  const projects = orderedProjects.value
  if (!list || projects.length === 0) return
  measureProjectRows(list)
  if (typeof ResizeObserver === 'undefined') return
  const observer = new ResizeObserver(() => measureProjectRows(list))
  observer.observe(list)
  onCleanup(() => observer.disconnect())
})

/**
 * 点一下项目块 ⇒ 拼进 / 删出「代练内容」。
 * ⚠️ 复用 `onProjectsChange`（同一份整词删实现），⛔ 不在这里重写一遍拼接。
 */
function toggleProject(id: number): void {
  const next = pickedProjects.value.includes(id)
    ? pickedProjects.value.filter((x) => x !== id)
    : [...pickedProjects.value, id]
  onProjectsChange(next)
}

function setAssignMode(next: AssignMode): void {
  if (next === 'hall' && !canPublishToHall.value) return
  assignMode.value = next
  if (next !== 'assign') {
    assignTarget.value = 'local'
    peerSelection.value = null
  }
  syncRememberedAssignee()
}

function syncRememberedAssignee(): void {
  if (!rememberTouched.value) {
    rememberChecked.value = assignMode.value === 'hall'
      ? initialIsHall.value
      : assignMode.value === 'assign' && initialDefaultAssigneeId.value > 0
  }
  if (assignMode.value === 'assign' && assigneeId.value === null) {
    assigneeId.value = assigneeOptions.value.find((row) => row.id === initialDefaultAssigneeId.value)?.id ?? null
  }
}

function onRememberAssignee(checked: boolean): void {
  rememberChecked.value = checked
  rememberTouched.value = true
}

function setAssignTarget(target: 'local' | 'peer'): void {
  assignTarget.value = target
  if (target === 'local') peerSelection.value = null
}

/**
 * 把「项目名称」滚到可视区并聚焦（保存时为空的那条路）。
 * ⚠️ 纯防御：`scrollIntoView` 不是所有运行环境都有（jsdom 没有）⇒ 先判存在再调；聚焦失败也不影响底部那句提示。
 */
async function focusProjectField(): Promise<void> {
  await nextTick()
  const el = projectFieldEl.value
  if (!el) return
  // 「代练内容」现在是 textarea ⇒ 两种都找（改回 input 也还能用）。
  const input = el.querySelector<HTMLTextAreaElement | HTMLInputElement>('textarea, input')
  if (input && typeof input.focus === 'function') input.focus()
  if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' })
}

/* ── 自动识别平台订单（改版：去掉「读取」按钮） ── */

/**
 * 改单号时立即作废在途响应。请求发起时记录单号与来源，合并输入停顿、回车和失焦触发。
 */
let readToken = 0
let lastReadKey = ''
let autoSource: number | null = null
let sourceReadTimer: ReturnType<typeof setTimeout> | undefined
const sourceReadQueued = ref(false)

function stopSourceTimer(): void {
  clearTimeout(sourceReadTimer)
  sourceReadTimer = undefined
  sourceReadQueued.value = false
}

function invalidateSourceRead(): void {
  stopSourceTimer()
  readToken += 1
  lastReadKey = ''
  reading.value = false
  // 换单号/换来源后不拿上一单的买家身份（同旧录单页）。
  pendingBuyer.value = null
}

/**
 * 用商品标题（`fields.remark`）自动认游戏；`detectGameFromText` 与电脑录单同一份纯函数。返回**要追加到成功提示后面的那句话**（空串 = 没有可说的）：
 * `none` 内容没提游戏是常态 ⇒ 静默；`ambiguous` 平手**不猜**（切错会连带清掉区服/常用项目，代价比多问一句大）；`hit` 当前不是它才切，切了走 `onGameChange`（清区服 / 常用项目 / 项目 FK）。自动切游戏**不覆盖**识别回填的 `project_text`（`onGameChange` 本来就不清它）。
 */
function autoSelectGameByContent(text: string, label: string): string {
  const r = detectGameFromText(text, games.value)
  if (r.kind === 'none') return ''
  if (r.kind === 'ambiguous') {
    return `${label}里出现了多款游戏（${r.names.join('、')}），请手动选择游戏`
  }
  if (form.value.game === r.game.id) return ''
  const hadServer = form.value.server !== null
  form.value.game = r.game.id
  onGameChange(r.game.id)
  return (
    `已按${label}自动选择游戏「${r.game.name}」` + (hadServer ? '（区服/常用项目已重置）' : '')
  )
}

function onSourceOrderNoInput(value: string | number): void {
  const previous = form.value.source_order_no.trim()
  form.value.source_order_no = String(value)
  const current = form.value.source_order_no.trim()
  if (previous === current) return
  invalidateSourceRead()
  if (autoSource !== null && form.value.order_source === autoSource) {
    form.value.order_source = null
    autoSource = null
  }
  if (current.length < 15) return
  sourceReadQueued.value = true
  sourceReadTimer = setTimeout(onSourceOrderNoChange, 350)
}

function onSourceOrderNoChange(): void {
  stopSourceTimer()
  void autoReadPlatformOrder()
}

function onSourceOrderNoEnter(event: KeyboardEvent): void {
  if (!event.isComposing) onSourceOrderNoChange()
}

/**
 * 订单来源变化 ⇒ 自动读。
 * ⚠️ **清空来源不发请求**（照电脑端：不能把刚清掉的来源又自动判定填回来）。
 */
function onSourceChange(value: unknown): void {
  const id = typeof value === 'number' ? value : null
  if (form.value.order_source === id) return
  invalidateSourceRead()
  autoSource = null
  form.value.order_source = id
  if (id === null) return
  void autoReadPlatformOrder({ sourceOverride: id })
}

/** 手动重试：作废去重键，同一 `(单号, 来源)` 也能再读一次（I9：识别失败/想重读不能被卡住）。 */
function retryRead(): void {
  if (reading.value) return
  stopSourceTimer()
  lastReadKey = ''
  void autoReadPlatformOrder()
}

async function autoReadPlatformOrder(
  opts: { sourceOverride?: number | null } = {},
): Promise<void> {
  const tid = form.value.source_order_no.trim()
  // 与录单页同一道形状闸（`orderid.length >= 15`）。⚠️ 那里是**静默** return；本页把原因说出来，
  // 否则客服填完单号什么都没发生，只能猜。
  if (tid.length < 15) {
    if (tid !== '') notify('info', '来源单号不足 15 位，请检查')
    return
  }
  let sourceId: number | null
  if (opts.sourceOverride === undefined) {
    const raw: unknown = form.value.order_source
    sourceId = typeof raw === 'number' ? raw : null
  } else {
    if (opts.sourceOverride === null) return
    sourceId = opts.sourceOverride
  }
  const key = `${tid}|${sourceId === null ? 'auto' : sourceId}`
  if (key === lastReadKey) return
  lastReadKey = key
  const my = ++readToken
  reading.value = true
  try {
    const fields = await readPlatformOrder(sourceId, tid)
    if (my !== readToken) return
    // 买家身份：闲管家有 buyer_eid 才落；没有就保持空（不按昵称猜）。
    const buyerEid = (fields.buyer_eid ?? '').trim()
    pendingBuyer.value = buyerEid !== '' ? { eid: buyerEid, nick: fields.buyer_nick ?? '' } : null
    // 价格**仅当平台侧非空**才写：上游没有价格时后端回空串（合法契约），无条件写会把客服
    // 已经填好的价格清空——空价在 `formToPayload` 里会被整键丢掉，最后落成 0 元单（算错钱）。
    if (fields.payment !== '') form.value.total_price = fields.payment
    // 昵称仅当「旺旺/YY」为空（同录单页，且在 await 之后读，免得请求飞行期间的手填被覆盖）。
    if (form.value.customer_ww.trim() === '' && fields.buyer_nick) {
      form.value.customer_ww = fields.buyer_nick
    }
    if (fields.customer_phone) form.value.customer_phone = fields.customer_phone
    // 商品标题（`fields.remark`）不再回填订单内容：随单保存、打手可见
    //（2026-11-05 业务方拍板；电脑录单页/旧版录单页同批）。标题仅用于下方自动切游戏。
    if (typeof fields.order_source === 'number') {
      ensureSourceOption(fields.order_source, fields.order_source_name)
      form.value.order_source = fields.order_source
      if (sourceId === null || sourceId === autoSource) autoSource = fields.order_source
      lastReadKey = `${tid}|${fields.order_source}`
    }
    // 自动选游戏的提示**追加**在后面，⛔ 不覆盖读单结果（客服要能同时看到两件事）。
    const gameMsg = autoSelectGameByContent(fields.remark, '商品内容')
    const parts = ['已读取平台订单并回填', gameMsg].filter(Boolean)
    notify('success', parts.join('；'))
  } catch (e) {
    if (my !== readToken) return
    // 业务码照实分档（同电脑端口径）：1015 店铺没配对 / 1028 待付款 ⇒ 后端 `msg` 本身就是给客服的操作指引，**原样显示**；
    // 1016 凭据失效 / 1017 上游其它故障 ⇒ 只说「没读到，可手动填」，不把上游原文抛给客服；其它业务错误 ⇒ 后端 `msg`；非业务错误 ⇒ 兜底文案。
    if (e instanceof BizError) {
      if (e.code === 1015 || e.code === 1028) notify('error', e.message)
      else if (e.code === 1016 || e.code === 1017) {
        notify('info', '没读到平台订单，可手动填写')
      } else notify('error', e.message)
    } else {
      notify('error', messageOf(e, '读取平台订单失败'))
    }
  } finally {
    if (my === readToken) reading.value = false
  }
}

/* ── 回填 / 初始化 ── */

/** 编辑回填：只搬本页**真的渲染**的那些字段（完整映射在录单页，本页字段集更小）。 */
function orderToForm(order: Order): KefuForm {
  const next = emptyForm()
  next.game = order.game
  next.server = order.server
  next.project = order.project
  next.project_text = order.project_text
  next.content = order.content
  next.demand = order.demand
  next.time_requirement = order.time_requirement
  next.game_account = order.game_account
  next.game_sub_account = order.game_sub_account
  next.game_role_name = order.game_role_name
  next.current_info_1 = order.current_info_1
  next.current_info_2 = order.current_info_2
  next.customer_phone = order.customer_phone
  next.customer_qq = order.customer_qq
  next.customer_ww = order.customer_ww
  next.is_urgent = order.is_urgent
  next.remark = order.remark
  next.order_source = order.order_source
  next.source_order_no = order.source_order_no
  // 编辑态只用于**展示**「接单销售客服」是谁；PATCH 载荷里会删掉它（后端把它列为只读，
  // 换归属走专用端点，而本页不提供那一条 ⇒ 本页不改归属）。
  next.sales_agent = order.sales_agent
  next.order_type = order.order_type === 'COMPANION' ? 'COMPANION' : 'BOOST'
  next.total_price = order.total_price ?? '0'
  next.received_price = order.received_price ?? ''
  next.platform_price = order.platform_price ?? ''
  next.hall_price = order.hall_price ?? ''
  next.fee = order.fee ?? ''
  // 分类在本页**没有控件**（业务方 2026-10-20 去掉分类下拉）⇒ 不回填，`saveEdit` 也不会发它。
  return next
}

async function loadEdit(): Promise<void> {
  loadingOrder.value = true
  try {
    const order = await fetchOrder(props.orderNo)
    form.value = orderToForm(order)
    initialOrderSource.value = order.order_source
    initialGame.value = order.game
    initialSalesAgentName.value = order.sales_agent
    // 来源字典被截断 / 那条来源已从字典删掉时，下拉里没有它的选项 ⇒ 补一条，
    // 否则编辑页会显示成一串数字 id（`order_source` 是 FK 主键）。
    if (order.order_source !== null) ensureSourceOption(order.order_source, order.source)
    await Promise.all([loadServers(order.game), loadProjects(order.game)])
    // 项目 FK 只在候选里真有它时才预勾（被删 / 改名的项目不假装选中）。
    const selected =
      order.project !== null && projectOptions.value.some((p) => p.id === order.project)
        ? [order.project]
        : []
    pickedProjects.value = selected
    prevPicked = [...selected]
  } catch (e) {
    notify('error', messageOf(e, '读取订单失败'))
  } finally {
    loadingOrder.value = false
  }
}

/**
 * 首屏复原「默认指派此人 / 默认档位」（同录单页：**只复原一次**）。`defAssIsHall` 与 `defAssRecManUid` 是**当前操作人自己的**偏好（不是租户设置），两列互斥。
 * 复原只在成功拉到名单那一次发生；之后用户怎么改就是什么（重拉不许覆盖）。
 */
async function loadAssignDefaults(): Promise<void> {
  try {
    const res = await fetchAssigneeOptions({ limit: 200 })
    assigneeOptions.value = res.items
    assigneeTruncated.value = res.truncated
    initialDefaultAssigneeId.value = Number(res.defAssRecManUid ?? 0)
    initialIsHall.value = canPublishToHall.value && res.defAssIsHall === true
    if (!canPublishToHall.value && canAssign.value && initialDefaultAssigneeId.value > 0) {
      assignMode.value = 'assign'
    }
    syncRememberedAssignee()
  } catch {
    // 名单拉不到不拦保存（同录单页），但**不能静默**：否则客服选了「内部派单」才发现没人可选。
    notify('error', '打手名单加载失败，可先不派单保存')
  }
}

onMounted(async () => {
  if (isEdit.value) {
    await loadDicts()
    await loadEdit()
    return
  }
  await Promise.all([loadDicts(), loadAssignDefaults()])
})

onBeforeUnmount(invalidateSourceRead)

/* ── 校验 / 提交 ── */

function totalPriceRequiredError(): string {
  if (isBlankLikePython(form.value.total_price)) return '订单总价不可为空'
  return ''
}

/**
 * 本次保存**真的会提交**、且当前**真的渲染**的金额字段。编辑态：金额只读、也不进 PATCH ⇒ 一律不校验——否则一个永远不会保存的框会把「改价走管理订单页」变成「保存失败」。
 * 录单态：发单价只在内部派单 / 内部抢单档渲染 ⇒ 「不派单」时**不许它拦住保存**（切到抢单填了非法值、再切回不派单，页面上根本没有这一格）。
 */
function renderedMoneyFields(): OrderMoneyField[] {
  if (isEdit.value) return []
  const fields: OrderMoneyField[] = ['total_price', 'received_price', 'platform_price', 'fee']
  if (assignMode.value !== 'none') fields.push('hall_price')
  return fields
}

/** 逐个过 `@/config/money` 的同一份校验；提示**带字段名**（E-0074：本仓自有文案一律带字段名）。 */
function firstMoneyError(): string {
  for (const field of renderedMoneyFields()) {
    const result = validateMoneyInput(form.value[field])
    if (!result.ok) return `${MONEY_FIELD_LABELS[field]}：${result.msg}`
  }
  return ''
}

/**
 * 本次建单的 `remember_assignee`（四态，见 `views/orders/assigneePreference.ts`）。⚠️ 只在**真的派单**（内部派单 / 内部抢单）时取：`none` 档不发这个键——「不派单」不动操作人的默认偏好（发 `'N'` / `'hall'` 会在一次与偏好无关的保存里把它改掉）。
 */
function rememberedValue(): 'Y' | 'N' | 'hall' | undefined {
  if (!rememberTouched.value) return undefined
  if (assignMode.value === 'hall') {
    return rememberAssigneeValue(
      null,
      rememberChecked.value,
      initialDefaultAssigneeId.value,
      true,
      initialIsHall.value,
    )
  }
  return rememberAssigneeValue(
    assigneeId.value,
    rememberChecked.value,
    initialDefaultAssigneeId.value,
    false,
    initialIsHall.value,
  )
}

/** 本次建单要不要指定提成归属：选的是**别人**、且持 `order.allow_change_sales_agent`。 */
function intakeSalesUserId(): number | null {
  if (!canChangeSales.value) return null
  const picked = salesUserId.value
  if (picked === null || picked === myUserId.value) return null
  return picked
}

function finishSave(order: SavedOrder, ...details: string[]): void {
  const message = [order.message, ...details].filter(Boolean).join('；')
  pendingFollowUp.value = null
  if (order.dispatchFailed) emit('createdFailed', message)
  else emit('saved', { orderNo: order.orderNo, message })
}

async function finishFollowUp(followUp: SaveFollowUp): Promise<void> {
  const { order } = followUp
  const savedMessage = `${order.message}（单号 ${order.orderNo}）`
  while (followUp.uploads[0]) {
    const upload = followUp.uploads[0]
    try {
      await attachKefuOrderScreenshot(order.orderNo, upload, session.canNode(['order.view_screenshot']))
      followUp.screenshotMessage = '截图已上传'
      followUp.uploads.shift()
      screenshotFiles.value = screenshotFiles.value.filter((file) => file !== upload.file)
    } catch (error) {
      const nextStep = upload.canReplace ? '。请移除该截图或换一张图，再重试' : ''
      notify('error', `${savedMessage}。截图「${upload.file.name}」未确认上传：${messageOf(error, '请重试')}${nextStep}`)
      return
    }
  }

  const selected = followUp.peer
  if (selected) {
    try {
      await assignPeerTaker(order.orderNo, selected)
      followUp.peer = null
    } catch (error) {
      const current = await fetchPeerDirectAssignment(order.orderNo).catch(() => null)
      const pending = current?.pending
      if (pending?.studio_id === selected.studio_id && pending.account_name === selected.account_name) {
        followUp.peer = {
          studio_id: pending.studio_id,
          account_name: pending.account_name,
          expected_price: pending.price,
        }
      } else if (current && !pending && current.studios.length > 0) {
        peerSelection.value = null
        followUp.reselectPeer = true
        notify('error', `${savedMessage}。下家指派失败：${messageOf(error, '请重新选择打手')}`)
        return
      }
      notify('error', `${savedMessage}。下家指派未确认：${messageOf(error, '请重试')}`)
      return
    }
  }
  finishSave(order, selected ? '下家指派成功' : '', followUp.screenshotMessage)
}

async function completeSave(order: SavedOrder, peer: KefuPeerSelection | null = null): Promise<void> {
  const uploads = canUploadScreenshot.value ? screenshotFiles.value.map(newScreenshotUpload) : []
  pendingFollowUp.value = { order, uploads, screenshotMessage: '', peer }
  await finishFollowUp(pendingFollowUp.value)
}

async function onCancel(): Promise<void> {
  if (submitting.value || screenshotBusy.value) return
  const followUp = pendingFollowUp.value
  if (followUp?.uploads.length) {
    followUp.uploads = []
    screenshotFiles.value = []
    followUp.screenshotMessage = followUp.screenshotMessage ? '已上传部分截图，已跳过其余截图' : '已跳过截图'
    submitting.value = true
    try {
      await finishFollowUp(followUp)
    } finally {
      submitting.value = false
    }
    return
  }
  if (followUp?.peer) {
    finishSave(followUp.order, followUp.screenshotMessage, '下家指派未确认，请到管理订单查看或重试')
    return
  }
  emit('back')
}

function partialOrderNo(data: unknown): string {
  if (typeof data !== 'object' || data === null || !('order_no' in data)) return ''
  return typeof data.order_no === 'string' ? data.order_no : ''
}

/** 建单：按当前派单档位只追加**这一档**需要的键。 */
async function saveCreate(): Promise<void> {
  const payload: CreateOrderPayload = formToPayload(form.value, currentActor())
  // 读单带回来的买家身份随建单回传（后端据此建客户档案并自动判新老客）。
  if (pendingBuyer.value !== null) {
    payload.buyer_eid = pendingBuyer.value.eid
    payload.buyer_nick = pendingBuyer.value.nick
  }
  if (customerKindOverride.value !== '') {
    payload.customer_kind_override = customerKindOverride.value
  }
  const peer = wantsPeerDispatch.value ? peerSelection.value : null
  // `sales_agent` 是名字快照：归属一律走 `sales_user`（主键），名字由服务端从那个人算，
  // 两列同源 ⇒ 送名字是**死载荷**，还会与主键打架（同录单页的口径）。
  delete (payload as Record<string, unknown>).sales_agent
  const owner = intakeSalesUserId()
  if (owner !== null) payload.sales_user = owner
  if (assignMode.value === 'none') {
    // 「不派单」不动任何派单字段：发单价即使模型里还留着（先切到抢单再切回来）也不发，
    // ⛔ 不让已经隐藏的控件偷偷写库。
    delete (payload as Record<string, unknown>).hall_price
  } else if (peer) {
    payload.hall_price ??= '0'
  } else {
    if (assignMode.value === 'hall') payload.throw_to_hall = true
    else if (assigneeId.value !== null) payload.assignee = assigneeId.value
    const remember = rememberedValue()
    if (remember !== undefined) payload.remember_assignee = remember
  }
  try {
    const order = await createOrder(payload)
    await completeSave({
      orderNo: order.order_no,
      message: assignMode.value === 'hall' ? '录单成功，已发内部大厅' : '录单成功',
    }, peer ? { ...peer, expected_price: order.hall_price } : null)
  } catch (e) {
    // 指派或发大厅失败时，先前已经建好的订单仍能保存截图。
    if (
      e instanceof BizError &&
      (e.code === ErrorCode.ORDER_CREATED_THROW_FAILED ||
        e.code === ErrorCode.ORDER_CREATED_ASSIGN_FAILED)
    ) {
      const message = `${e.message}；请到团团通后台重试`
      const orderNo = partialOrderNo(e.data)
      if (orderNo) await completeSave({ orderNo, message, dispatchFailed: true })
      else emit('createdFailed', screenshotFiles.value.length ? `${message}并补传截图` : message)
      return
    }
    notify('error', messageOf(e, '保存失败'))
  }
}

async function saveEdit(): Promise<void> {
  const peer = wantsPeerDispatch.value ? peerSelection.value : null
  // 待确认的指派先恢复原操作，编辑入口此时无法更新订单。
  if (peer && !peer.match_token) {
    await completeSave({ orderNo: props.orderNo, message: '订单已保存' }, peer)
    return
  }
  const raw = formToPayload(form.value, currentActor()) as Record<string, unknown>
  // 此页编辑代练内容（project_text），旧单单独存的订单内容继续由后台管理。
  delete raw.content
  // 编辑态**不发任何金额**：后端 `OrderUpdateSerializer` 把它们列只读，送出去会被静默忽略，
  // 于是「改了价、保存成功、价没变」——改价只能走 `/price/`（同录单页 `formToUpdatePayload`）。
  for (const field of ORDER_MONEY_FIELDS) delete raw[field]
  // 归属走专用端点（本页不提供），所以这个键也不进 PATCH：送它只会误触后端那道节点闸。
  delete raw.sales_agent
  // 分类在本页**没有控件**（业务方 2026-10-20 去掉分类下拉）⇒ 一个键都不发：
  // 「不发键 = 不改」，⛔ 不能补空值把订单已有的分类清掉。
  delete raw.category
  // 换过游戏 ⇒ 旧区服 / 项目 FK 属于上一款游戏，必须**显式置空**（照 `order_source` 的先例）。
  // ⛔ 只靠「空值不发」会让库里留下「游戏=B、区服/项目还是 A 的」这种跨游戏脏数据。
  if (initialGame.value !== null && form.value.game !== initialGame.value) {
    raw.server = null
    raw.project = null
  }
  // 来源被**显式清空**时才补 `null`（「不发键 = 不改」，同录单页 T6 评审 I1）。
  const rawSource: unknown = form.value.order_source
  const sourceId = typeof rawSource === 'number' ? rawSource : null
  if (initialOrderSource.value !== null && sourceId === null) raw.order_source = null
  try {
    // ⭐ 用**带回信封 `msg`** 的出口（与主控台 `order-addorder.ts::onSave` 同族）：后端保存成功后可能顺手撤这张单还挂着的代练通订单；**撤不掉时接口仍是成功码**、警告只在 `msg` 里——只取 `data` 会把这句话吞掉，父页永远只显示固定的「保存成功」。空 `msg` 回退「保存成功」（老桩 / 无警告的成功信封）。
    const { message } = await updateOrderWithMessage(
      props.orderNo,
      raw as unknown as Partial<UpdateOrderPayload>,
    )
    await completeSave({ orderNo: props.orderNo, message: message || '保存成功' }, peer)
  } catch (e) {
    notify('error', messageOf(e, '保存失败'))
  }
}

/**
 * 唯一的保存入口（业务方定稿：底部只留一个「保存」，按当前档位执行）：`none` 只建单；`assign` 建单 + `assignee`（必须先选打手，否则这一档名不副实）；`hall` 建单 + `throw_to_hall` + `hall_price`（必须 > 0）。
 */
async function submit(): Promise<void> {
  if (submitting.value || loadingOrder.value || screenshotBusy.value || sourceReadQueued.value || reading.value) return
  const retry = pendingFollowUp.value
  if (retry) {
    if (retry.reselectPeer) {
      const selected = peerSelection.value
      if (!selected) { notify('error', '请重新选择下家打手'); return }
      if (selected.price !== undefined) {
        const price = validateMoneyInput(selected.price)
        if (!price.ok) { notify('error', `发单价：${price.msg}`); return }
      }
      retry.peer = selected
      delete retry.reselectPeer
    }
    if (pendingScreenshot.value?.canReplace) {
      const failedFile = pendingScreenshot.value.file
      if (screenshotFiles.value.includes(failedFile)) {
        notify('error', '请移除失败的截图或换一张图再重试，订单已保存')
        return
      }
      const remaining = new Map(retry.uploads.map((upload) => [upload.file, upload]))
      retry.uploads = screenshotFiles.value.map((file) => remaining.get(file) ?? newScreenshotUpload(file))
    }
    submitting.value = true
    try {
      await finishFollowUp(retry)
    } finally {
      submitting.value = false
    }
    return
  }
  if (!canSaveOrder.value) {
    notify('error', '没有保存订单的权限')
    return
  }
  if (recoveringPeer.value) {
    submitting.value = true
    try {
      await saveEdit()
    } finally {
      submitting.value = false
    }
    return
  }
  if (form.value.game === null) {
    notify('error', '请选择游戏')
    return
  }
  if (isBlankLikePython(form.value.project_text)) {
    // 项目名称为空：滚过去 + 聚焦 + 底部提示补填，⛔ 不能静默不发请求也不说原因。
    await focusProjectField()
    notify('error', '请填写代练内容')
    return
  }
  if (!isEdit.value) {
    const totalPriceBad = totalPriceRequiredError()
    if (totalPriceBad !== '') {
      notify('error', totalPriceBad)
      return
    }
  }
  const moneyBad = firstMoneyError()
  if (moneyBad !== '') {
    notify('error', moneyBad)
    return
  }
  if (wantsPeerDispatch.value && peerSelection.value?.price !== undefined) {
    const price = validateMoneyInput(peerSelection.value.price)
    if (!price.ok) { notify('error', `发单价：${price.msg}`); return }
  }
  // ⭐ 「代练项目 700 字节同步上限闸」（业务方裁定 E-0242）：鸡蛋铺对 `project` 文本的实际接受上限 ≈732/733 **字节**（UTF-8），超限会被对方拒收 ⇒ 拦在提交之前（一个请求都不发），文案与录单页逐字同源（`projectTextSyncLimitError`）。
  // 建单与编辑**两态都拦**（两条路都会把这段文本同步出去）。串在校验链**最后**：上面几条必填的提示优先级不变（与岛页 `onSave` 的链序一致）。
  const projectSyncBad = projectTextSyncLimitError(form.value.project_text)
  if (projectSyncBad !== '') {
    await focusProjectField()
    notify('error', projectSyncBad)
    return
  }
  if (!isEdit.value) {
    if (assignMode.value === 'hall' && !(Number(form.value.hall_price) > 0)) {
      // 内部抢单必须有一个**大于 0** 的价（业务方裁定 R3，同录单页）：拦在提交之前，
      // 一个请求都不发。判据用解析后的数值——`'0'` / `''` 都是非法（E-0021 的 JS 侧同型）。
      notify('error', '内部抢单请先填发单价（必须大于 0）')
      return
    }
    if (assignMode.value === 'assign') {
      if (!canAssign.value) {
        notify('error', '没有指派打手的权限')
        return
      }
      if (assignTarget.value === 'peer' && !peerSelection.value) {
        notify('error', '请查找并选择下家打手')
        return
      }
      if (assignTarget.value === 'local' && assigneeId.value === null) {
        notify('error', '请选择要指派的打手')
        return
      }
    }
  }
  submitting.value = true
  try {
    if (isEdit.value) await saveEdit()
    else await saveCreate()
  } finally {
    submitting.value = false
  }
}

/* ── 复位 / 草稿判据（父页面在「保存成功 / 1022·1023 / 取消」时调用） ── */

/**
 * 把表单复位成**新单**（父页面 `KefuConsoleView` 在保存成功、1022/1023、取消后调用）。⚠️ 给本组件新增状态时，把对应的重置一并补进这里——新 ref 不复位 = 上一单的残留带进下一单。
 */
function resetForm(): void {
  screenshotKey.value += 1
  form.value = emptyForm()
  pickedProjects.value = []
  prevPicked = []
  projectOptions.value = []
  projectsTruncated.value = false
  serverOptions.value = []
  assignMode.value = defaultAssignMode.value
  assignTarget.value = 'local'
  peerSelection.value = null
  assigneeId.value = null
  rememberChecked.value = false
  rememberTouched.value = false
  salesUserId.value = null
  screenshotFiles.value = []
  pendingFollowUp.value = null
  kefuOptionsFailed.value = false
  formNotice.value = ''
  projectsExpanded.value = false
  moreOpen.value = false
  autoSource = null
  customerKindOverride.value = ''
  pendingBuyer.value = null
  invalidateSourceRead()
  // 从服务端取回个人指派偏好，内部抢单仍是有权限时的新单默认值。
  void loadAssignDefaults()
}

function hasDraft(): boolean {
  const base = emptyForm()
  const changed = (Object.keys(base) as (keyof KefuForm)[]).some(
    (k) => form.value[k] !== base[k],
  )
  const assignDraft =
    assignMode.value !== defaultAssignMode.value || assigneeId.value !== null
      || rememberTouched.value
  return changed || pickedProjects.value.length > 0 || salesUserId.value !== null
    || screenshotFiles.value.length > 0 || peerSelection.value !== null || assignDraft
}

const footer = useTemplateRef<HTMLElement>('footer')
defineExpose({ resetForm, hasDraft, blocksNavigation, footer })
</script>

<template>
  <div class="kefu-form">
    <div class="kefu-form-scroll">
      <p v-if="loadingOrder" class="kefu-hint" role="status">订单加载中…</p>

      <fieldset class="kefu-fields" :disabled="submitting || loadingOrder || (blocksNavigation && !replacingScreenshot)">
        <F7List strong inset class="kefu-fields-list">
          <template #list>
            <li class="kefu-list-row kefu-pair" :inert="orderFieldsReadOnly || undefined">
              <div class="kefu-field">
                <label :for="fieldId('source')">订单来源</label>
                <KefuPicker
                  :model-value="form.order_source"
                  :trigger-id="fieldId('source')"
                  trigger-class="kefu-order-source"
                  label="订单来源"
                  :options="sourceOptions"
                  empty-label="自动判定"
                  placeholder="自动判定"
                  @update:model-value="onSourceChange"
                />
              </div>
              <div class="kefu-field">
                <label :for="fieldId('game')">游戏</label>
                <KefuPicker
                  v-model="form.game"
                  :trigger-id="fieldId('game')"
                  trigger-class="kefu-game"
                  label="游戏"
                  :options="games"
                  placeholder="请选择"
                  @update:model-value="onGameChange"
                />
              </div>
            </li>

            <li class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
              <label :for="fieldId('source_order_no')">来源单号</label>
              <div class="kefu-number-control">
                <F7ListInput
                  :wrap="false"
                  :input-id="fieldId('source_order_no')"
                  :value="form.source_order_no"
                  class="kefu-input"
                  name="source_order_no"
                  placeholder="平台订单号"
                  autocomplete="off"
                  @update:value="onSourceOrderNoInput"
                  @change="onSourceOrderNoChange"
                  @keyup.enter="onSourceOrderNoEnter"
                />
                <F7Button
                  v-if="form.source_order_no.trim() !== ''"
                  type="button"
                  class="kefu-read-retry"
                  :disabled="reading"
                  :aria-disabled="reading"
                  @click="retryRead"
                >
                  {{ reading ? '识别中…' : '重新识别' }}
                </F7Button>
              </div>
            </li>

            <li class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
              <span class="kefu-label">常用项目</span>
              <div>
                <p v-if="form.game === null" class="kefu-hint">先选游戏</p>
                <p v-else-if="projectsLoading" class="kefu-hint" role="status">常用项目加载中…</p>
                <p v-else-if="projectOptions.length === 0" class="kefu-hint">无常用项目</p>
                <div v-else class="kefu-project-selector">
                  <div
                    ref="projectChips"
                    class="kefu-project-chips"
                    :class="{ 'is-expanded': projectsExpanded }"
                  >
                    <F7Button
                      v-for="project in orderedProjects"
                      :key="project.id"
                      :data-project-id="project.id"
                      type="button"
                      small tonal round class="kefu-project-chip"
                      :class="{ 'is-overflowing': hiddenProjectIds.has(project.id) }"
                      :active="pickedProjects.includes(project.id)"
                      :aria-pressed="pickedProjects.includes(project.id)"
                      :aria-hidden="!projectsExpanded && hiddenProjectIds.has(project.id) || undefined"
                      :title="project.name"
                      @click="toggleProject(project.id)"
                    >
                      {{ project.name }}
                    </F7Button>
                  </div>
                  <F7Button
                    v-if="projectsExpanded || hiddenProjectIds.size > 0"
                    type="button"
                    small class="kefu-projects-toggle"
                    :aria-expanded="projectsExpanded"
                    @click="projectsExpanded = !projectsExpanded"
                  >
                    {{ projectsExpanded ? '收起' : `展开(${hiddenProjectIds.size})` }}
                  </F7Button>
                </div>
                <p v-if="projectsTruncated" class="kefu-hint">
                  项目超过 {{ PROJECT_PAGE_SIZE }} 个，只显示前 {{ PROJECT_PAGE_SIZE }} 个。
                </p>
              </div>
            </li>

            <li ref="projectFieldEl" class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
              <label class="kefu-project-label" :for="fieldId('project_text')">代练内容<span aria-hidden="true" class="kefu-required"> *</span></label>
              <F7ListInput
                type="textarea"
                :wrap="false"
                :input-id="fieldId('project_text')"
                v-model:value="form.project_text"
                class="kefu-input"
                name="project_text"
                maxlength="900"
                required
                resizable
                placeholder="可选常用项目或手填"
              />
            </li>
            <li v-if="canUploadScreenshot" class="kefu-list-row">
              <KefuOrderScreenshot
                :key="screenshotKey"
                v-model="screenshotFiles"
                v-model:busy="screenshotBusy"
                :order-no="orderNo"
                :can-view-existing="session.canNode(['order.view_screenshot'])"
                :disabled="submitting || loadingOrder || (blocksNavigation && !replacingScreenshot)"
              />
            </li>

            <li class="kefu-list-row kefu-pair kefu-price-pair" :inert="orderFieldsReadOnly || undefined">
              <div class="kefu-field">
                <label :for="fieldId('total_price')">订单总价</label>
                <F7ListInput
                  :wrap="false"
                  :input-id="fieldId('total_price')"
                  v-model:value="form.total_price"
                  class="kefu-input"
                  name="total_price"
                  inputmode="decimal"
                  :disabled="isEdit"
                />
              </div>
              <div class="kefu-field">
                <label :for="!isEdit && canChangeSales ? fieldId('sales') : undefined">销售客服</label>
                <KefuPicker
                  v-if="!isEdit && canChangeSales"
                  :model-value="salesUserId"
                  :trigger-id="fieldId('sales')"
                  trigger-class="kefu-sales-btn"
                  label="销售客服"
                  :options="kefuOptions"
                  placeholder="我自己"
                  :empty-label="mySalesLabel"
                  :loading="kefuOptionsLoading"
                  :notice="salesOptionsNotice"
                  @update:model-value="chooseSalesUser"
                />
                <span v-else class="kefu-readonly">
                  {{ isEdit ? initialSalesAgentName || '未记录' : '我自己' }}
                </span>
              </div>
            </li>
            <li v-if="isEdit" class="kefu-list-row kefu-hint kefu-field-hint" :inert="orderFieldsReadOnly || undefined">改价请到「管理订单」。</li>

            <template v-if="!isEdit">
              <li class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
                <span class="kefu-label">派单方式</span>
                <F7Segmented strong class="kefu-seg" role="group" aria-label="派单方式">
                  <F7Button
                    v-for="item in allowedDispatchOptions"
                    :key="item.mode"
                    type="button"
                    class="kefu-seg-btn"
                    :active="assignMode === item.mode"
                    :aria-pressed="assignMode === item.mode"
                    @click="setAssignMode(item.mode)"
                  >
                    {{ item.label }}
                  </F7Button>
                </F7Segmented>
              </li>
              <li v-if="assignMode === 'assign' && canAssignPeer" class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
                <span class="kefu-label">指派范围</span>
                <F7Segmented strong class="kefu-seg" role="group" aria-label="指派范围">
                  <F7Button type="button" :active="assignTarget === 'local'" :aria-pressed="assignTarget === 'local'" @click="setAssignTarget('local')">
                    本工作室
                  </F7Button>
                  <F7Button type="button" :active="assignTarget === 'peer'" :aria-pressed="assignTarget === 'peer'" @click="setAssignTarget('peer')">
                    下家工作室
                  </F7Button>
                </F7Segmented>
              </li>
              <li v-if="assignMode === 'assign' && assignTarget === 'local'" class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
                <label :for="fieldId('assignee')">指派给</label>
                <KefuPicker
                  v-model="assigneeId"
                  :trigger-id="fieldId('assignee')"
                  trigger-class="kefu-assignee"
                  label="打手"
                  :options="assigneeOptions"
                  placeholder="请选择打手"
                  empty-label="清空"
                  :notice="assigneeTruncated ? '候选名单过长，只显示前 200 人' : undefined"
                />
              </li>
              <li v-if="assignMode !== 'none'" class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
                <label :for="fieldId('hall_unit_price')">发单价</label>
                <F7ListInput
                  :wrap="false"
                  :input-id="fieldId('hall_unit_price')"
                  v-model:value="form.hall_price"
                  class="kefu-input"
                  name="hall_unit_price"
                  inputmode="decimal"
                  :placeholder="assignMode === 'hall' ? '必须大于 0' : '可不填，默认 0'"
                />
              </li>
              <li v-if="assignMode !== 'none' && !wantsPeerDispatch" class="kefu-list-row kefu-field" :inert="orderFieldsReadOnly || undefined">
                <span class="kefu-label">记住默认</span>
                <F7Checkbox :checked="rememberChecked" name="remember_assignee" @update:checked="onRememberAssignee">
                  {{ assignMode === 'hall' ? '默认内部抢单' : '默认指派此人' }}
                </F7Checkbox>
              </li>
            </template>

            <li v-if="wantsPeerDispatch && !loadingOrder && !needsPeerReselection" class="kefu-list-row" :inert="replacingScreenshot || undefined">
              <KefuPeerDispatch
                v-model="peerSelection"
                :game-id="form.game"
                :order-no="orderNo || undefined"
                :original-game-id="initialGame"
                :disabled="blocksNavigation"
              />
            </li>

            <li class="kefu-more-row" :inert="orderFieldsReadOnly || undefined">
              <F7AccordionItem :opened="moreOpen">
                <F7Button
                  type="button"
                  class="kefu-more-toggle"
                  :aria-expanded="moreOpen"
                  :aria-controls="fieldId('more')"
                  @click="moreOpen = !moreOpen"
                >
                  更多字段 <span aria-hidden="true">{{ moreOpen ? '⌃' : '⌄' }}</span>
                </F7Button>
                <F7AccordionContent :id="fieldId('more')" :inert="!moreOpen || undefined" :aria-hidden="!moreOpen">
                  <div class="kefu-field">
                    <label :for="fieldId('demand')">需求描述</label>
                    <F7ListInput
                      type="textarea"
                      :wrap="false"
                      :input-id="fieldId('demand')"
                      v-model:value="form.demand"
                      class="kefu-input"
                      name="demand"
                      placeholder="打手可见"
                    />
                  </div>
                  <div class="kefu-field">
                    <label :for="fieldId('server')">区服</label>
                    <KefuPicker
                      v-model="form.server"
                      :trigger-id="fieldId('server')"
                      trigger-class="kefu-server"
                      label="区服"
                      :options="serverOptions"
                      placeholder="请选择"
                      empty-label="清空"
                      :disabled="form.game === null"
                    />
                  </div>
                  <div v-for="field in additionalFields" :key="field.name" class="kefu-field">
                    <label :for="fieldId(field.name)">{{ field.label }}</label>
                    <div v-if="field.name === 'customer_ww'" class="kefu-kind-line">
                      <F7Segmented strong class="kefu-kind-seg" role="group" aria-label="新老客">
                        <F7Button
                          v-for="opt in ([{ value: '', label: '不选' }, { value: 'new', label: '新' }, { value: 'old', label: '老' }] as const)"
                          :key="opt.value || 'none'"
                          type="button"
                          :active="customerKindOverride === opt.value"
                          :aria-pressed="customerKindOverride === opt.value"
                          :data-kefu-customer-kind="opt.value || 'none'"
                          @click="customerKindOverride = opt.value"
                        >{{ opt.label }}</F7Button>
                      </F7Segmented>
                      <F7ListInput
                        :wrap="false"
                        :input-id="fieldId(field.name)"
                        v-model:value="form[field.name]"
                        class="kefu-input"
                        :name="field.name"
                        :placeholder="field.placeholder"
                      />
                    </div>
                    <F7ListInput
                      v-else
                      :wrap="false"
                      :input-id="fieldId(field.name)"
                      v-model:value="form[field.name]"
                      class="kefu-input"
                      :name="field.name"
                      :placeholder="field.placeholder"
                    />
                  </div>
                  <div v-for="field in additionalPrices" :key="field.name" class="kefu-field">
                    <label :for="fieldId(field.name)">{{ field.label }}</label>
                    <F7ListInput
                      :wrap="false"
                      :input-id="fieldId(field.name)"
                      v-model:value="form[field.name]"
                      class="kefu-input"
                      :name="field.name"
                      inputmode="decimal"
                      :disabled="isEdit"
                    />
                  </div>
                  <div class="kefu-field">
                    <label :for="fieldId('remark')">重要备注</label>
                    <F7ListInput type="textarea" :wrap="false" :input-id="fieldId('remark')" v-model:value="form.remark" class="kefu-input" name="remark" />
                  </div>
                  <div class="kefu-field">
                    <label :for="fieldId('order_type')">订单类型</label>
                    <F7ListInput type="select" :wrap="false" :input-id="fieldId('order_type')" v-model:value="form.order_type" class="kefu-input" name="order_type">
                      <option value="BOOST">代练</option>
                      <option value="COMPANION">陪玩</option>
                    </F7ListInput>
                  </div>
                  <div class="kefu-field">
                    <span class="kefu-label">加急</span>
                    <F7Checkbox v-model:checked="form.is_urgent" name="is_urgent">加急处理</F7Checkbox>
                  </div>
                </F7AccordionContent>
              </F7AccordionItem>
            </li>
          </template>
        </F7List>
      </fieldset>
      <F7List v-if="needsPeerReselection && pendingFollowUp" strong inset class="kefu-fields-list">
        <template #list>
          <li class="kefu-list-row">
            <KefuPeerDispatch
              v-model="peerSelection"
              :game-id="form.game"
              :order-no="pendingFollowUp.order.orderNo"
              :disabled="submitting"
            />
          </li>
        </template>
      </F7List>
    </div>

    <footer ref="footer" class="kefu-footer">
      <p
        v-if="formNotice"
        class="kefu-footer-hint"
        :class="`is-${formNoticeKind}`"
        role="alert"
      >
        {{ formNotice }}
      </p>
      <F7Button
        type="button"
        tonal round class="kefu-cancel"
        :disabled="submitting || screenshotBusy"
        :aria-disabled="submitting || screenshotBusy"
        @click="onCancel"
      >
        {{ pendingScreenshot ? '跳过截图' : pendingFollowUp?.peer ? '暂时返回' : '取消' }}
      </F7Button>
      <p v-if="!canSaveOrder" class="kefu-footer-hint is-error">没有保存订单的权限</p>
      <F7Button
        v-else
        type="button"
        fill round
        class="kefu-submit"
        :disabled="submitting || loadingOrder || screenshotBusy || sourceReadQueued || reading"
        :aria-disabled="submitting || loadingOrder || screenshotBusy || sourceReadQueued || reading"
        @click="submit"
      >
        {{ submitting ? '保存中…' : pendingScreenshot ? '重试上传截图' : pendingFollowUp?.peer || recoveringPeer ? '重试下家指派' : '保存' }}
      </F7Button>
    </footer>
  </div>
</template>

<style scoped>
.kefu-form {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.kefu-form-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
.kefu-fields { min-width: 0; padding: 0; margin: 0; border: 0; }
.kefu-kind-line { position: relative; display: block; min-width: 0; }
.kefu-kind-seg {
  position: absolute;
  left: 4px;
  top: 50%;
  z-index: 2;
  width: 76px;
  transform: translateY(-50%);
}
.kefu-kind-seg :deep(.segmented) { margin: 0; }
.kefu-kind-seg :deep(.button) { padding-inline: 2px; font-size: 11px; }
.kefu-kind-line :deep(input) { padding-left: 84px !important; }
.kefu-fields-list :deep(ul) { overflow: hidden; }
.kefu-list-row { padding: 1px 10px; }
.kefu-list-row:not(:last-child) { border-bottom: 1px solid var(--kefu-divider); }
.kefu-more-row :deep(.accordion-item-content) > .kefu-field { padding: 1px 10px; border-top: 1px solid var(--kefu-divider); }
.kefu-more-toggle.button { justify-content: space-between; padding-inline: 10px; height: 41px; text-align: left; }
.kefu-list-row.kefu-field-hint { padding-inline-start: 81px; }
.kefu-pair {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 8px;
}
.kefu-pair .kefu-field { grid-template-columns: auto minmax(0, 1fr); }
.kefu-pair .kefu-field > label:first-child { white-space: nowrap; }
.kefu-price-pair { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.kefu-input.item-content,
.kefu-input :deep(.item-inner) { min-height: 0; padding: 0; }
.kefu-input :deep(.item-input-wrap) { margin: 0; }
.kefu-input :deep(.item-inner::after),
.kefu-input :deep(.item-input-wrap::after) { display: none; }
.kefu-input :deep(textarea) { min-height: 41px; height: 41px; padding-block: 6px; resize: vertical; }
.kefu-input :deep(textarea.resizable) { resize: none; }
.kefu-project-label { white-space: nowrap; }
.kefu-required { color: var(--kefu-error); }
.kefu-readonly { overflow-wrap: anywhere; }
.kefu-number-control { display: flex; align-items: center; gap: 4px; }
.kefu-number-control .kefu-input { flex: 1 1 0; }
.kefu-read-retry { flex: none; width: auto; padding-inline: 4px; }
.kefu-seg :deep(.kefu-seg-btn) { min-width: 0; padding-inline: 1px; white-space: nowrap; font-size: 12px; }
.kefu-project-selector { display: flex; align-items: flex-start; gap: 4px; }
.kefu-project-chips {
  position: relative;
  display: flex;
  flex: 1 1 0;
  min-width: 0;
  flex-wrap: wrap;
  align-content: flex-start;
  gap: 4px;
  overflow: hidden;
  max-height: 28px;
}
.kefu-project-chips.is-expanded { max-height: 118px; overflow-y: auto; }
.kefu-project-chips:not(.is-expanded) .is-overflowing { visibility: hidden; }
.kefu-project-chips .kefu-project-chip {
  display: inline-block;
  width: auto;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kefu-projects-toggle { flex: none; width: auto; padding-inline: 2px; }
.kefu-footer {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 10px 10px;
  border-top: 1px solid var(--kefu-divider);
  background: var(--f7-bars-bg-color);
}
.kefu-footer .kefu-submit { flex: 1 1 0; }
.kefu-footer .kefu-cancel { width: auto; padding-inline: 14px; }
.kefu-footer-hint { flex: 1 0 100%; margin: 0; overflow-wrap: anywhere; color: var(--kefu-muted); font-size: 12px; }
.kefu-footer-hint.is-error { color: var(--kefu-error); }
.kefu-footer-hint.is-success { color: var(--f7-theme-color); }
</style>
