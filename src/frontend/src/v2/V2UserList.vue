<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「人员管理」v2（`/v2/user-list`；系统人员批次 2026-11-05，计划 §3.1）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/system-user-list.ts`（对标 `/User/userlist`）。
 * 按同一套功能重建为炭火页（骨架照管理订单先例）：筛选 10 项 + 身份切换 chip（单向同步）
 * → 工具条 14+1 颗（连体）→ 橙色统计行（两形态）→ 主表（勾选列 + 登录账号 sticky 左冻、
 * 操作列右冻；可见 16 列、持节点者 +1；隐藏 6 列走列显隐 popover）→ 服务端分页。
 * 行内：保证金原位编辑 / 标记 / 排序值 / 四个开关（有效状态成功不刷新；禁止结算与
 * 禁自助接单成功必须刷新；个人押金额度乐观更新失败回滚）；操作列 恢复/编辑/重置；
 * 删除两跳确认；设定游戏权限 / 押金额度两弹层与接单统计 / 支付宝查重两卡片各自组件化。
 * ⛔ 不 import island 页模块；逐字文案照 island 源码复制；押金额度按钮与列只给持
 * `finance.taker_wallet_adjust` 的人（fail-closed）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  deleteAccount,
  fetchAccountRoles,
  fetchAccountOpeners,
  listAccounts,
  resetAccountPassword,
  restoreAccount,
  setAccountStatus,
  setAccountsStatus,
  updateAccount,
  type AccountListParams,
  type AccountRow,
  type AccountWritePayload,
} from '@/api/accounts'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { setDepositQuotaEnabled, TAKER_DEPOSIT_QUOTA_NODE } from '@/api/takerDepositQuota'
import {
  BUSINESS_TYPE_FILTER_OPTIONS,
  BUSINESS_TYPE_LABELS,
  CERT_FILTER_OPTIONS,
  SETTLE_FILTER_OPTIONS,
  USER_STATUS_FILTER_OPTIONS,
} from '@/config/systemUsers'
import { useSessionStore } from '@/stores/session'
import { useColumnWidths } from '@/v2/tableResize'
import V2SetGamesDialog from '@/v2/V2SetGamesDialog.vue'
import V2DepositQuotaDialog from '@/v2/V2DepositQuotaDialog.vue'
import V2OrderStatsCard from '@/v2/V2OrderStatsCard.vue'
import V2AlipayCheckCard from '@/v2/V2AlipayCheckCard.vue'
import V2UserNew from '@/v2/V2UserNew.vue'

const session = useSessionStore()

/* ── 逐字文案（出处 = island 页同语义常量；本地重写不引模块）────────────────── */

const REQUIRE_CHECKED_TEXT = '请勾选要操作的数据行'
const TAG_UNCHANGED_TEXT = '编辑的内容与原先一样，无需提交'
const ORDER_BY_INVALID_TEXT = '请输入正整数'
const SELF_COUNT_INVALID_TEXT = '请输入正整数'
/** 删除两跳确认（island DELETE_CONFIRM_TEXTS 逐字；文案照抄对标，实际是软删可恢复）。 */
const DEL_FIRST_1 = '确认要删除吗？'
const DEL_FIRST_2 =
  '警告：一旦删除，将彻底无法恢复，强烈建议不要删除，只需要将此用户的有效状态改为禁用即可，请三思再操作！'
const DEL_SECOND_1 = '第二次确认:'
const DEL_SECOND_2 = '是不是真的要删除？删除就永远无法恢复了'
/** 三个 prompt 的 title / maxlength（island TAG/ORDER_BY/SELF_COUNT_PROMPT_BASE 逐字）。 */
const PROMPT_META = {
  tag: { title: '设置用户标记，不需要请填写：无', maxlength: 100 },
  orderby: { title: '设置指派接手人时的排序值，值越大越在前，默认0', maxlength: 4 },
  selfcount: {
    title: '设置用户在内部接单页面接单上限（以用户当前未完成订单数为准），0代表不限制',
    maxlength: 4,
  },
} as const
/** 排序值格的 title（island orderBy templet 原文）。 */
const ORDER_BY_CELL_TITLE = '在指派界面按排序值排列时，数值越大越排前面，点击可以编辑'
/** 「修改业务类型」弹层的三颗 radio（D-0086 顺序 1→2→0，island BUSINESS_TYPE_OPTIONS 同序）。 */
const BUSINESS_TYPE_OPTIONS = [
  { value: 1, label: '专做代练' },
  { value: 2, label: '专做陪玩' },
  { value: 0, label: '代练+陪玩' },
] as const
/** 成员类型筛选三档（v2 本地定义、⛔ 不引共享配置：共享 `config/systemUsers.ts` 的同名常量
 * 同时喂旧壳 island 页——那页的显示名是「内部成员」，v2 改「普通成员」只能落本页，D-0344。） */
const MEMBER_TYPE_FILTER_OPTIONS = [
  { label: '全部', value: -1 },
  { label: '普通成员', value: 0 },
  { label: '合作用户', value: 1 },
] as const
/** 身份切换四档（island IDENTITY_TABS 同口径：chip 只驱动 workerFlag / studioFlag，
 * member_type 一律回 -1；同步是单向的——手改成员类型下拉不回移 chip 高亮）。 */
const IDENTITY_TABS = [
  { key: 'all', label: '全部', memberType: -1, workerFlag: null, studioFlag: null },
  { key: 'internal', label: '内部员工', memberType: -1, workerFlag: false, studioFlag: null },
  { key: 'taker', label: '打手', memberType: -1, workerFlag: true, studioFlag: false },
  { key: 'studio', label: '工作室', memberType: -1, workerFlag: null, studioFlag: true },
] as const

/** 尚未开放按钮的统一句式（island notOpen 同式）。 */
function notOpen(label: string): void {
  showToast(`「${label}」尚未开放`)
}
/** 排序值 / 接单上限共用的非负整数判据（island isPositiveInt 同式）。 */
function isPositiveInt(value: string): boolean {
  return /^\d+$/.test(value)
}
/** 抢单价范围的金额形状判据（island isMoneyLike 逐字照抄：允许 ± 号与 1-2 位小数）。 */
function isMoneyLike(value: string): boolean {
  return /^(((\-|\+)?[1-9]\d*)|0)(\.\d{1,2})?$/.test(value)
}
/** 「设抢单价范围」的 6 条校验，文案与顺序逐字（island recPriceValidationMessage 同式）。 */
function recPriceValidationMessage(priceMin: string, priceMax: string): string | null {
  if (priceMin === '') return '请输入最小接单价，不能小于0'
  if (priceMax === '') return '请输入最大接单价，不能大于99999'
  if (!isMoneyLike(priceMin)) return '最小接单价格式不正确，必须是0到99999之间的整数'
  if (!isMoneyLike(priceMax)) return '最大接单价格式不正确，必须是0到99999之间的整数'
  if (Number(priceMin) < 0) return '最小接单价必须大于或等于0'
  if (Number(priceMax) > 99999) return '最大接单价必须小于或等于99999'
  return null
}
/** 「设自助接单数」预填：勾选行上限全部相同 ⇒ 该值，否则 0（island selfCountPromptValue 同式）。 */
function selfCountPromptValue(picked: readonly AccountRow[]): number {
  const limits = picked.map((r) => r.grab_limit_dl ?? 0)
  const first = limits[0] ?? 0
  return limits.every((v) => v === first) ? first : 0
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，居中深条照管理订单；info 1500ms / error 6000ms）────── */

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

/* ── 权限门：押金额度按钮与列（本批唯一按钮级权限门，fail-closed）────────────── */

const canDepositQuota = computed(() => session.canNode([TAKER_DEPOSIT_QUOTA_NODE]))

/* ── 字典：游戏 / 职位 ──────────────────────────────────────────────────────── */

const games = ref<ConfigRow[]>([])
const roleNames = ref<string[]>([])
const openers = ref<Array<{ id: number; name: string; username: string }>>([])
const openersTruncated = ref(false)
const openersError = ref(false)
const openersLoading = ref(false)
const openerQuery = ref('')
const openerPage = ref(1)
let retryPage = 1
let openerRequest = 0
async function loadOpeners(query = '', page = 1): Promise<void> {
  const serial = ++openerRequest
  if (page === 1 && query !== openerQuery.value) {
    openers.value = []
    openersTruncated.value = false
  }
  openerQuery.value = query
  openersLoading.value = true
  retryPage = page
  try {
    const result = await fetchAccountOpeners(query, page)
    if (serial !== openerRequest) return
    openers.value = page === 1 ? result.items : [...openers.value, ...result.items]
    openerPage.value = result.page
    openersTruncated.value = result.total > openers.value.length
    openersError.value = false
  } catch {
    if (serial === openerRequest) {
      if (page === 1) openers.value = []
      openersError.value = true
      showToast('开户人列表加载失败，请重试', 'error')
    }
  } finally {
    if (serial === openerRequest) openersLoading.value = false
  }
}

const gameNameOf = computed(() => {
  const map = new Map<number, string>()
  for (const g of games.value) map.set(g.id, g.name)
  return (id: number): string => map.get(id) ?? ''
})

/* ── 列定义（渲染顺序 = island buildCols；隐藏 6 列走列显隐 popover）────────── */

const USER_COLUMNS = [
  'check', 'username', 'display_name', 'rname', 'bussType', 'sign', 'role', 'deposit_amount',
  'quota', 'grab_limit_dl', 'rate', 'priceLimit', 'qq', 'mobile', 'remark', 'is_active',
  'settle_forbidden', 'grab_forbid_self_service', 'tag', 'games', 'lastLoginTime', 'orderBy',
  'creator', 'actions',
] as const

const HIDABLE_COL_TITLES: Record<string, string> = {
  rname: '姓名',
  sign: '签约',
  deposit_amount: '保证金',
  rate: '完单率',
  priceLimit: '接单价限制',
  lastLoginTime: '最后登录',
  creator: '开户人',
}
/* ⭐ key 升 v3：默认隐藏集再变（开户人列默认展示，仍可手动隐藏）；老用户存值已固化，
 * 不升版则新默认集对其静默失效。代价 = 既有列偏好重置一次（有意为之，STATUS 登记）。 */
const HIDDEN_COLS_KEY = 'tt.v2.user-list.hidden-cols.v3'
const HIDDEN_COLS_DEFAULT = ['rname', 'sign', 'rate', 'priceLimit', 'lastLoginTime', 'deposit_amount']

function loadHiddenCols(): string[] {
  try {
    const raw = localStorage.getItem(HIDDEN_COLS_KEY)
    if (raw === null) return [...HIDDEN_COLS_DEFAULT]
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return [...HIDDEN_COLS_DEFAULT]
    return parsed.filter((k): k is string => typeof k === 'string' && k in HIDABLE_COL_TITLES)
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

/** 列显隐 = el-checkbox-group：v-model = 可见列键，set 时换算回 hiddenCols。 */
const visibleHiddenKeys = computed<string[]>({
  get: () => Object.keys(HIDABLE_COL_TITLES).filter((k) => colVisible(k)),
  set: (keys) => {
    hiddenCols.value = Object.keys(HIDABLE_COL_TITLES).filter((k) => !keys.includes(k))
    saveHiddenCols()
  },
})

/** 渲染列序列（colgroup / 空行 colspan 共用；quota 列随权限出现）。 */
const visibleColumns = computed(() =>
  USER_COLUMNS.filter((k) => (k === 'quota' ? canDepositQuota.value : true)).filter((k) =>
    colVisible(k),
  ),
)

const { colStyle, startResize, resetColumn } = useColumnWidths('user-list', {
  check: 36,
  username: 110,
  display_name: 110,
  rname: 90,
  bussType: 100,
  sign: 64,
  role: 100,
  deposit_amount: 96,
  /* 四个开关列（开关用 EP 默认档 40×20）：列宽随之 +10；th 横向 padding 由本页 scoped
   * 减到 6px 配套（见样式区）。弹性列且无 resizer ⇒ 只钉宽、不给拖拽；quota 表头 6 字给 100。 */
  quota: 100,
  is_active: 90,
  settle_forbidden: 90,
  grab_forbid_self_service: 90,
  grab_limit_dl: 84,
  rate: 76,
  priceLimit: 104,
  qq: 100,
  mobile: 110,
  tag: 90,
  games: 160,
  lastLoginTime: 150,
  orderBy: 76,
  creator: 90,
  actions: 136,
})

/* ── 筛选（发参映射 = island buildUserListWhere 逐条重写）────────────────────── */

/** 后端已接受但共享类型未声明的筛选键（island 同一批发送；不新增端点）。 */
type UserListParams = AccountListParams & {
  game?: number
  member_type?: number
  is_cert?: string
  settle_forbidden?: boolean
  key2?: string
  key3?: string
  is_studio?: boolean
  is_worker?: boolean
}

const filters = reactive({
  gameId: null as number | null,
  openerId: null as number | null,
  roles: [] as string[],
  status: '',
  bussType: -1,
  memberType: -1,
  isCert: '',
  isDisSett: '',
  key: '',
  key2: '',
  key3: '',
  workerFlag: null as boolean | null,
  studioFlag: null as boolean | null,
})

// el-option 的 value prop 类型不含 null；运行时仍以 null 表示「全部」（与 filters.gameId 同值比较）。
const GAME_ALL_OPTION_VALUE = null as unknown as number

const activeIdentity = ref<string>('all')

function buildWhere(): UserListParams {
  const where: UserListParams = {}
  // 「选择职位」多选：原样发逗号串（⛔ 不 split / 不取第一个）。
  if (filters.roles.length > 0) where.role = filters.roles.join(',')
  // 「已删除」是字符串档：走 deleted=true；'true'/'false' 仍走 is_active（旧语义没被抢）。
  if (filters.status === 'deleted') where.deleted = true
  else if (filters.status === 'true' || filters.status === 'false') {
    where.is_active = filters.status === 'true'
  }
  if (filters.key !== '') where.key = filters.key
  if (filters.gameId !== null) where.game = filters.gameId
  if (filters.openerId !== null) where.opened_by = filters.openerId
  // 业务类型 / 成员类型：「全部」= -1 不发；0（代练+陪玩）是合法值必须发（E-0021）。
  if (filters.bussType !== -1) where.business_type = filters.bussType
  if (filters.memberType !== -1) where.member_type = filters.memberType
  // 身份分档两隐藏域：false 是合法显式值（⛔ 不能真值判断）。
  if (filters.studioFlag !== null) where.is_studio = filters.studioFlag
  if (filters.workerFlag !== null) where.is_worker = filters.workerFlag
  if (filters.isCert !== '') where.is_cert = filters.isCert
  if (filters.isDisSett === 'Y' || filters.isDisSett === 'N') {
    where.settle_forbidden = filters.isDisSett === 'Y'
  }
  if (filters.key2 !== '') where.key2 = filters.key2
  if (filters.key3 !== '') where.key3 = filters.key3
  return where
}

/** 点身份 chip = 同写成员类型下拉 + 两隐藏域，回第 1 页重取；高亮只由 chip 点击驱动。 */
function onIdentityTab(tab: (typeof IDENTITY_TABS)[number]): void {
  filters.memberType = tab.memberType
  filters.workerFlag = tab.workerFlag
  filters.studioFlag = tab.studioFlag
  activeIdentity.value = tab.key
  void reload(1)
}

function onIdentityChange(key: string): void {
  const tab = IDENTITY_TABS.find((item) => item.key === key)
  if (tab) onIdentityTab(tab)
}

function onSearch(): void {
  void reload(1)
}

/* ── 取数（服务端分页；统计行两形态照 island done 回调）──────────────────────── */

const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]
const rows = ref<AccountRow[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(20)
const totalLine = ref('')

async function reload(targetPage = page.value): Promise<void> {
  loading.value = true
  try {
    const result = await listAccounts({
      ...buildWhere(),
      page: targetPage,
      page_size: pageSize.value,
    })
    rows.value = result.items
    total.value = result.total
    page.value = result.page
    loadError.value = null
    void computeTotalLine(result.total)
  } catch (e) {
    rows.value = []
    total.value = 0
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
  }
}

/** 统计行：总数 > 200 只报总数；否则拉一页 200 行算 活跃/禁用/保证金合计（island 同口径）。 */
async function computeTotalLine(count: number): Promise<void> {
  if (count > 200) {
    totalLine.value = `本系统内共有${count}个系统子账户`
    return
  }
  try {
    const all = await listAccounts({ page: 1, page_size: 200 })
    const active = all.items.filter((r) => r.is_active).length
    const deposit = all.items.reduce((s, r) => s + Number(r.deposit_amount ?? 0), 0)
    const depositText = Number.isInteger(deposit) ? String(deposit) : deposit.toFixed(2)
    totalLine.value =
      `本系统内共有${count}个系统子账户，其中有${active}个系统子账户状态正常，` +
      `${all.items.length - active}个系统子账户被禁用，总计已登记保证金${depositText}元`
  } catch {
    totalLine.value = `本系统内共有${count}个系统子账户`
  }
}

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

/* ── 勾选（工具栏批量动作只吃勾选的行）──────────────────────────────────────── */

const checkedIds = ref<number[]>([])

function isChecked(id: number): boolean {
  return checkedIds.value.includes(id)
}

function toggleRow(id: number): void {
  const i = checkedIds.value.indexOf(id)
  if (i === -1) checkedIds.value.push(id)
  else checkedIds.value.splice(i, 1)
}

const pageAllChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => isChecked(r.id)),
)

function togglePageAll(): void {
  if (pageAllChecked.value) {
    checkedIds.value = checkedIds.value.filter((id) => !rows.value.some((r) => r.id === id))
  } else {
    for (const r of rows.value) {
      if (!isChecked(r.id)) checkedIds.value.push(r.id)
    }
  }
}

function checkedRows(): AccountRow[] {
  return rows.value.filter((r) => isChecked(r.id))
}

const checkedCount = computed(() => checkedRows().length)

function requireChecked(): AccountRow[] | null {
  const picked = checkedRows()
  if (picked.length === 0) {
    showToast(REQUIRE_CHECKED_TEXT)
    return null
  }
  return picked
}

/* ── 批量 PATCH 共路（island patchEachRow / patchSelected 同口径）────────────── */

async function patchEachRow(
  build: (row: AccountRow) => Partial<AccountWritePayload>,
  okMsg: string,
): Promise<boolean> {
  const picked = requireChecked()
  if (picked === null) return false
  try {
    await Promise.all(picked.map((r) => updateAccount(r.id, build(r))))
  } catch (e) {
    showToast(errText(e), 'error')
    return false
  }
  showToast(okMsg)
  void reload()
  return true
}

function patchSelected(patch: Partial<AccountWritePayload>, okMsg: string): Promise<boolean> {
  return patchEachRow(() => patch, okMsg)
}

/* ── 工具条动作 ─────────────────────────────────────────────────────────────── */

async function onToggleStatus(): Promise<void> {
  const picked = requireChecked()
  if (picked === null) return
  const allActive = picked.every((r) => r.is_active)
  try {
    await setAccountsStatus(
      picked.map((r) => r.id),
      !allActive,
    )
    showToast(allActive ? '已停用' : '已启用')
    void reload()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

function onToggleSettle(): void {
  // 逐行各自取反（⛔ 不是统一置值）：混合勾选时统一置值会把一半行写成反的。
  void patchEachRow(
    (row) => ({ settle_forbidden: row.settle_forbidden !== true }),
    '已切换禁止结算状态',
  )
}

/* ── 设定游戏权限弹层（工具栏批量 + 单元格单人共用）──────────────────────────── */

const setGamesUids = ref<number[] | null>(null)

function onToolbarGames(): void {
  const picked = requireChecked()
  if (picked === null) return
  setGamesUids.value = picked.map((r) => r.id)
}

function onRowGames(row: AccountRow): void {
  setGamesUids.value = [row.id]
}

/** 关层即刷新（照 island `end` 语义：无论保存与否，关掉就刷一遍列表）。 */
function onGamesDlgClose(): void {
  setGamesUids.value = null
  void reload()
}

/* ── 押金额度弹层（单人；0 行 / 多行各给一句提示，都不开层）──────────────────── */

const depositQuotaUid = ref<number | null>(null)

function onToolbarDepositQuota(): void {
  const picked = requireChecked()
  if (picked === null) return
  if (picked.length > 1) {
    showToast('押金额度只能给一个人配置')
    return
  }
  depositQuotaUid.value = picked[0]!.id
}

function onQuotaDlgClose(): void {
  depositQuotaUid.value = null
  void reload()
}

/* ── 设自助接单数 / 排序值 / 标记 三个 prompt 弹层 ───────────────────────────── */

type PromptKind = keyof typeof PROMPT_META
const promptDlg = ref<{ kind: PromptKind; ids: number[]; value: string; original: string } | null>(
  null,
)
const promptMaxlength = computed(() =>
  promptDlg.value === null ? 100 : PROMPT_META[promptDlg.value.kind].maxlength,
)
const promptTitle = computed(() =>
  promptDlg.value === null ? '' : PROMPT_META[promptDlg.value.kind].title,
)

function onToolbarSelfCount(): void {
  const picked = requireChecked()
  if (picked === null) return
  promptDlg.value = {
    kind: 'selfcount',
    ids: picked.map((r) => r.id),
    value: String(selfCountPromptValue(picked)),
    original: '',
  }
}

function onRowTag(row: AccountRow): void {
  promptDlg.value = { kind: 'tag', ids: [row.id], value: row.tag || '', original: row.tag || '' }
}

function onRowOrderby(row: AccountRow): void {
  const value = String(row.order_by ?? 0)
  promptDlg.value = { kind: 'orderby', ids: [row.id], value, original: value }
}

async function confirmPrompt(): Promise<void> {
  const dlg = promptDlg.value
  if (dlg === null) return
  if (dlg.kind === 'tag') {
    // 值没变 ⇒ 只提示「无需提交」，不提交（弹层不关，照旧页）。
    if (dlg.value === dlg.original) {
      showToast(TAG_UNCHANGED_TEXT)
      return
    }
    try {
      await updateAccount(dlg.ids[0]!, { tag: dlg.value })
    } catch (e) {
      showToast(errText(e), 'error')
      return
    }
    promptDlg.value = null
    showToast('标记已保存')
    void reload()
    return
  }
  if (dlg.kind === 'orderby') {
    // 值没变 ⇒ 直接关层不提交；非正整数 ⇒ 提示且不关层。
    if (dlg.value === dlg.original) {
      promptDlg.value = null
      return
    }
    if (!isPositiveInt(dlg.value)) {
      showToast(ORDER_BY_INVALID_TEXT)
      return
    }
    try {
      await updateAccount(dlg.ids[0]!, { order_by: Number(dlg.value) })
    } catch (e) {
      showToast(errText(e), 'error')
      return
    }
    promptDlg.value = null
    showToast('排序值已保存')
    void reload()
    return
  }
  // selfcount：一个值同写 dl + pw 两个上限；0 是合法值（= 不限制）。
  if (!isPositiveInt(dlg.value)) {
    showToast(SELF_COUNT_INVALID_TEXT)
    return
  }
  const limit = Number(dlg.value)
  const ok = await patchSelected(
    { grab_limit_dl: limit, grab_limit_pw: limit },
    '接单上限已保存',
  )
  if (ok) promptDlg.value = null
}

/* ── 设置业务类型 / 设抢单价范围 两个弹层 ───────────────────────────────────── */

const bizTypeDlg = ref<{ value: number | null } | null>(null)

function onToolbarBizType(): void {
  const picked = requireChecked()
  if (picked === null) return
  // 三颗 radio 不预选：勾选多行且各行业务类型不同时预选会把存量行静默改值（E-0027 同型）。
  bizTypeDlg.value = { value: null }
}

async function onBizTypeSave(): Promise<void> {
  const dlg = bizTypeDlg.value
  if (dlg === null) return
  if (dlg.value === null) {
    showToast('请选择业务类型')
    return
  }
  const ok = await patchSelected({ business_type: dlg.value }, '业务类型已保存')
  if (ok) bizTypeDlg.value = null
}

const priceRangeDlg = ref<{ min: string; max: string } | null>(null)

function onToolbarPriceRange(): void {
  const picked = requireChecked()
  if (picked === null) return
  // 容器默认值 0 / 99999 照旧页（页面已挡住前两条「空值」校验，照抄不优化掉）。
  priceRangeDlg.value = { min: '0', max: '99999' }
}

async function onPriceRangeSave(): Promise<void> {
  const dlg = priceRangeDlg.value
  if (dlg === null) return
  const problem = recPriceValidationMessage(dlg.min, dlg.max)
  if (problem !== null) {
    showToast(problem)
    return
  }
  const ok = await patchSelected(
    { grab_price_min: dlg.min, grab_price_max: dlg.max },
    '抢单价范围已保存',
  )
  if (ok) priceRangeDlg.value = null
}

/* ── 删除用户（两跳确认） / 恢复 / 重置密码 ─────────────────────────────────── */

const delDlg = ref<{ step: 1 | 2; rows: AccountRow[] } | null>(null)

function onDeleteSelected(): void {
  const picked = requireChecked()
  if (picked === null) return
  delDlg.value = { step: 1, rows: picked }
}

function onDelNext(): void {
  const dlg = delDlg.value
  if (dlg !== null) delDlg.value = { step: 2, rows: dlg.rows }
}

async function onDelConfirm(): Promise<void> {
  const dlg = delDlg.value
  if (dlg === null) return
  delDlg.value = null
  let done = 0
  let failed = 0
  let firstError = ''
  for (const row of dlg.rows) {
    try {
      await deleteAccount(row.id)
      done += 1
    } catch (e) {
      failed += 1
      if (firstError === '') firstError = e instanceof Error ? e.message : '删除失败'
    }
  }
  showToast(
    failed === 0 ? `已删除 ${done} 个账号` : `已删除 ${done} 个，${failed} 个失败：${firstError}`,
  )
  void reload()
}

async function onRestore(row: AccountRow): Promise<void> {
  try {
    await restoreAccount(row.id)
    showToast('已恢复')
  } catch (e) {
    showToast(errText(e), 'error')
  }
  // 恢复必须刷新：默认（非「已删除」）口径下这一行不该再出现。
  void reload()
}

const resetDlg = ref<{ id: number } | null>(null)

async function onResetConfirm(): Promise<void> {
  const dlg = resetDlg.value
  if (dlg === null) return
  resetDlg.value = null
  try {
    await resetAccountPassword(dlg.id)
    showToast('密码已重置为 123456') // 照旧页不刷新
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 行内编辑：保证金原位 input（Enter/blur 提交、Esc 还原；成功不刷新）──────── */

const depositEditingId = ref<number | null>(null)
const depositDraft = ref('')

function startDepositEdit(row: AccountRow): void {
  if (depositEditingId.value === row.id) return
  depositEditingId.value = row.id
  depositDraft.value = row.deposit_amount ?? ''
}

function cancelDepositEdit(): void {
  depositEditingId.value = null
}

async function commitDepositEdit(row: AccountRow): Promise<void> {
  if (depositEditingId.value !== row.id) return
  depositEditingId.value = null
  try {
    // 空值发 0（照旧页 edit 回调）；成功只 toast 不刷新；失败弹 msg 保值。
    await updateAccount(row.id, {
      deposit_amount: depositDraft.value === '' ? 0 : depositDraft.value,
    })
    showToast('保证金已保存')
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

/* ── 行内开关：有效状态 / 禁止结算 / 禁自助接单 / 个人押金额度 ───────────────── */

async function onStatusSwitch(row: AccountRow): Promise<void> {
  try {
    await setAccountStatus(row.id, row.is_active)
    showToast('已保存') // 成功不刷新（照旧页 staChecked 分支）
  } catch (e) {
    showToast(errText(e), 'error')
    void reload() // 失败刷新回真值
  }
}

async function onRealSwitch(
  row: AccountRow,
  field: 'settle_forbidden' | 'grab_forbid_self_service',
): Promise<void> {
  const next = row[field] === true
  try {
    await updateAccount(
      row.id,
      field === 'settle_forbidden' ? { settle_forbidden: next } : { grab_forbid_self_service: next },
    )
    showToast('已保存')
  } catch (e) {
    showToast(errText(e), 'error')
  }
  // 成功也必须刷新：行缓存是「禁/解结算」取反的输入，不刷新会把旧值再发一遍。
  void reload()
}

async function onQuotaSwitch(row: AccountRow): Promise<void> {
  const next = row.deposit_quota_enabled === true
  try {
    await setDepositQuotaEnabled(row.id, next)
    showToast('已保存') // 乐观更新：成功不刷新
  } catch (e) {
    row.deposit_quota_enabled = !next // 失败回滚到切换前的值
    showToast(errText(e), 'error')
  }
}

/* ── 编辑/新开弹窗（装 v2 新开账号页组件；⛔ 不再 router.push——CarbonShell 页签按 fullPath
 * 去重，跳整页会「每点一行各开一签」。id = null 表示新增。照旧页 iframe 大弹窗形态。） */

const userDlg = ref<{ id: number | null } | null>(null)

function onNewAccount(): void {
  userDlg.value = { id: null }
}

function openEdit(row: AccountRow): void {
  userDlg.value = { id: row.id }
}

/** saved（弹窗内保存成功）或弹窗关闭都刷新列表。 */
function onUserSaved(): void {
  void reload()
}

function onUserDlgClose(): void {
  userDlg.value = null
  void reload()
}

/* ── 两张数据卡片 ──────────────────────────────────────────────────────────── */

const statsOpen = ref(false)
const alipayOpen = ref(false)

/* ── 业类徽标（代陪蓝 / 陪玩橙 / 代练绿，照 island bussBadge 三色映射）────────── */

function bussBadgeClass(value: number | undefined): string {
  if (value === 0) return 'ul-kind--both'
  if (value === 2) return 'ul-kind--play'
  if (value === 1) return 'ul-kind--doing'
  return ''
}

function bussBadgeText(value: number | undefined): string {
  return value === undefined ? '' : (BUSINESS_TYPE_LABELS[value] ?? '')
}

/** 翻页后勾选残留死 id 会污染「已选 N 行」：只保留当前页仍存在的。 */
watch(rows, () => {
  const alive = new Set(rows.value.map((r) => r.id))
  checkedIds.value = checkedIds.value.filter((id) => alive.has(id))
})

onMounted(() => {
  void loadOpeners()
  void (async () => {
    try {
      const [gameRows, roleList] = await Promise.all([fetchAllGames(), fetchAccountRoles()])
      games.value = gameRows
      roleNames.value = roleList
    } catch {
      // 字典取不到：筛选下拉与游戏名降级为空 / 主键，列表照常（管理订单同口径）。
    }
    await reload(1)
  })()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="user-list">
    <!-- 筛选区：连体组（照管理订单 flt 形态，本页前缀 uf-）；无重置钮（照旧页） -->
    <section class="cv2-panel cv2-filter" aria-label="筛选" data-v2="filters">
      <div class="uf-wrap">
        <div class="uf-group" data-v2-filter-group="identity">
          <span class="uf-label">用户类型</span>
          <span class="uf-cell">
            <el-select :model-value="activeIdentity" class="uf-w140" data-v2-filter="identity" :teleported="false" @change="onIdentityChange">
              <el-option v-for="item in IDENTITY_TABS" :key="item.key" :value="item.key" :label="item.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="opener">
          <span class="uf-label">开户人</span>
          <span class="uf-cell">
            <el-select v-model="filters.openerId" clearable filterable remote :remote-method="loadOpeners" placeholder="全部（输入姓名或账号）" class="uf-w170" data-v2-filter="opener" :teleported="false">
              <el-option v-for="item in openers" :key="item.id" :value="item.id" :label="item.name === item.username ? item.name : `${item.name}（${item.username}）`" />
            </el-select>
          </span>
        </div>
        <button v-if="openersTruncated && !openersError" type="button" class="cv2-btn cv2-btn--tool" :disabled="openersLoading" @click="loadOpeners(openerQuery, openerPage + 1)">加载更多开户人</button>
        <button v-if="openersError" type="button" class="cv2-btn cv2-btn--tool" :disabled="openersLoading" @click="loadOpeners(openerQuery, retryPage)">开户人加载失败，重试</button>
        <div class="uf-group" data-v2-filter-group="game">
          <span class="uf-label">游戏权限</span>
          <span class="uf-cell">
            <el-select v-model="filters.gameId" filterable placeholder="全部" class="uf-w150" data-v2-filter="game" :teleported="false">
              <el-option :value="GAME_ALL_OPTION_VALUE" label="全部" />
              <el-option v-for="g in games" :key="g.id" :value="g.id" :label="g.name" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="role">
          <span class="uf-label">选择职位</span>
          <span class="uf-cell">
            <el-select v-model="filters.roles" multiple collapse-tags collapse-tags-limit="2" placeholder="请选择" class="uf-w170" data-v2-filter="role" :teleported="false">
              <el-option v-for="r in roleNames" :key="r" :value="r" :label="r" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="status">
          <span class="uf-label">有效状态</span>
          <span class="uf-cell">
            <el-select v-model="filters.status" class="uf-w140" data-v2-filter="status" :teleported="false">
              <el-option v-for="o in USER_STATUS_FILTER_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="buss-type">
          <span class="uf-label">业务类型</span>
          <span class="uf-cell">
            <el-select v-model="filters.bussType" class="uf-w140" data-v2-filter="buss-type" :teleported="false">
              <el-option v-for="o in BUSINESS_TYPE_FILTER_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="member-type">
          <span class="uf-label">成员类型</span>
          <span class="uf-cell">
            <el-select v-model="filters.memberType" class="uf-w140" data-v2-filter="member-type" :teleported="false">
              <el-option v-for="o in MEMBER_TYPE_FILTER_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="is-cert">
          <span class="uf-label">是否实名</span>
          <span class="uf-cell">
            <el-select v-model="filters.isCert" class="uf-w140" data-v2-filter="is-cert" :teleported="false">
              <el-option v-for="o in CERT_FILTER_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
          </span>
        </div>
        <div class="uf-group" data-v2-filter-group="settle">
          <span class="uf-label">禁止结算</span>
          <span class="uf-cell">
            <el-select v-model="filters.isDisSett" class="uf-w140" data-v2-filter="settle" :teleported="false">
              <el-option v-for="o in SETTLE_FILTER_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
          </span>
        </div>
      </div>
      <!-- 搜索排：三个搜索框 + 搜索钮（无重置钮，照旧页） -->
      <div class="uf-searchrow">
        <div class="uf-group uf-grow" data-v2-filter-group="key1">
          <span class="uf-label">搜索1</span>
          <span class="uf-cell uf-cell--grow">
            <el-input v-model="filters.key" placeholder="账号/姓名/电话/QQ微信" data-v2-filter="key1" @keydown.enter="onSearch" />
          </span>
        </div>
        <div class="uf-group uf-grow" data-v2-filter-group="key2">
          <span class="uf-label">搜索2</span>
          <span class="uf-cell uf-cell--grow">
            <el-input v-model="filters.key2" placeholder="支付宝账号/姓名" data-v2-filter="key2" @keydown.enter="onSearch" />
          </span>
        </div>
        <div class="uf-group uf-grow" data-v2-filter-group="key3">
          <span class="uf-label">搜索3</span>
          <span class="uf-cell uf-cell--grow">
            <el-input v-model="filters.key3" placeholder="备注/标记" data-v2-filter="key3" @keydown.enter="onSearch" />
          </span>
        </div>
        <div class="cv2-filter-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--lg cv2-btn--find cv2-btn--fill" data-v2="search" :disabled="loading" @click="onSearch">搜索</button>
        </div>
      </div>
    </section>

    <!-- 工具条 14+1 颗（押金额度仅持 finance.taker_wallet_adjust 渲染） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <div class="ttb-strip" data-v2="toolbar-strip">
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-new" @click="onNewAccount">新增系统子账户</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-refresh" :disabled="loading" @click="reload()">刷新列表</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-toggle" @click="onToggleStatus">禁用/启用</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-sign" @click="notOpen('发起/更改签约')">发起/更改签约</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-games" @click="onToolbarGames">设置游戏权限</button>
        <button v-if="canDepositQuota" type="button" class="cv2-btn cv2-btn--submit" data-v2="tb-deposit-quota" @click="onToolbarDepositQuota">押金额度</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-busstype" @click="onToolbarBizType">设置业务类型</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-selfcount" @click="onToolbarSelfCount">设自助接单数</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-price" @click="onToolbarPriceRange">设抢单价范围</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-settle" @click="onToggleSettle">禁/解结算</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-rate" @click="notOpen('统计完单率')">统计完单率</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-stats" @click="statsOpen = true">接单统计</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-alipay" @click="alipayOpen = true">检测功能</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="tb-blacklist" @click="notOpen('加入黑名单')">加入黑名单</button>
        <button type="button" class="cv2-btn cv2-btn--solid-yellow" data-v2="tb-delete" @click="onDeleteSelected">删除用户</button>
      </div>
      <span class="cv2-toolbar-count" data-v2="checked-count">已选 {{ checkedCount }} 行</span>
      <div class="ul-colpick" data-v2="colpick">
        <el-popover trigger="click" :teleported="false" :show-arrow="false" :width="188" popper-class="cv2-colpick-pop">
          <template #reference>
            <button
              type="button"
              class="cv2-btn cv2-btn--primary ul-colpick-btn"
              title="选择展现的字段"
              aria-label="选择展现的字段"
            >
              <svg viewBox="0 0 16 16" aria-hidden="true" class="ul-colpick-ico">
                <rect x="2" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="9" y="2.5" width="5" height="4.5" rx="1" />
                <rect x="2" y="9" width="5" height="4.5" rx="1" />
                <rect x="9" y="9" width="5" height="4.5" rx="1" />
              </svg>
            </button>
          </template>
          <p class="ul-colpick-title">选择展现的字段</p>
          <el-checkbox-group v-model="visibleHiddenKeys" class="ul-colpick-group" data-v2="colpick-group">
            <el-checkbox
              v-for="(title, key) in HIDABLE_COL_TITLES"
              :key="key"
              :value="key"
              :data-v2-colcheck="key"
            >{{ title }}</el-checkbox>
          </el-checkbox-group>
        </el-popover>
      </div>
    </div>

    <!-- 统计行：两形态照旧页（>200 只报总数；否则拉 200 行算活跃/禁用/保证金合计） -->
    <p v-if="totalLine !== ''" class="cv2-total-line" data-v2="total">{{ totalLine }}</p>

    <!-- 主表：勾选列 + 登录账号 sticky 左冻、操作列右冻；<colgroup> 承接列宽拖拽 -->
    <section class="cv2-panel cv2-table-panel" aria-label="人员列表">
      <table class="cv2-table">
        <colgroup>
          <col v-for="key in visibleColumns" :key="key" :style="colStyle(key)" />
        </colgroup>
        <thead>
          <tr>
            <th class="cv2-th-check ul-fix-l ul-fix-l-check">
              <input type="checkbox" data-v2="check-page-all" :checked="pageAllChecked" aria-label="勾选当前页全部行" @change="togglePageAll" />
            </th>
            <th class="ul-fix-l ul-fix-l-user">
              登录账号
              <span class="cv2-th-resizer" data-v2-col="username" @mousedown="startResize($event, 'username')" @dblclick="resetColumn('username')" />
            </th>
            <th v-if="colVisible('display_name')">
              账号昵称
              <span class="cv2-th-resizer" data-v2-col="display_name" @mousedown="startResize($event, 'display_name')" @dblclick="resetColumn('display_name')" />
            </th>
            <th v-if="colVisible('rname')">姓名</th>
            <th v-if="colVisible('bussType')" class="cv2-th-center">业类</th>
            <th v-if="colVisible('sign')" class="cv2-th-center">签约</th>
            <th v-if="colVisible('role')" class="cv2-th-center">角色职位</th>
            <th v-if="colVisible('deposit_amount')" class="cv2-th-center">保证金</th>
            <th v-if="canDepositQuota && colVisible('quota')" class="cv2-th-center ul-th-narrow">个人押金额度</th>
            <th v-if="colVisible('grab_limit_dl')" class="cv2-th-center">
              接单上限
              <span class="cv2-th-resizer" data-v2-col="grab_limit_dl" @mousedown="startResize($event, 'grab_limit_dl')" @dblclick="resetColumn('grab_limit_dl')" />
            </th>
            <th v-if="colVisible('rate')" class="cv2-th-center">完单率</th>
            <th v-if="colVisible('priceLimit')" class="cv2-th-center">接单价限制</th>
            <th v-if="colVisible('qq')">QQ/微信</th>
            <th v-if="colVisible('mobile')">电话</th>
            <th v-if="colVisible('remark')">备注说明</th>
            <th v-if="colVisible('is_active')" class="cv2-th-center ul-th-narrow">有效状态</th>
            <th v-if="colVisible('settle_forbidden')" class="cv2-th-center ul-th-narrow">禁止结算</th>
            <th v-if="colVisible('grab_forbid_self_service')" class="cv2-th-center ul-th-narrow">禁自助接单</th>
            <th v-if="colVisible('tag')">标记</th>
            <th v-if="colVisible('games')">游戏权限</th>
            <th v-if="colVisible('lastLoginTime')" class="cv2-th-center">最后登录</th>
            <th v-if="colVisible('orderBy')" class="cv2-th-center">排序值</th>
            <th v-if="colVisible('creator')" class="cv2-th-center">开户人</th>
            <th class="cv2-th-center ul-fix-r">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.id"
            class="cv2-row"
            :class="{ 'cv2-row--checked': isChecked(row.id) }"
            :data-v2-row="row.id"
          >
            <td class="cv2-th-check ul-fix-l ul-fix-l-check">
              <input
                type="checkbox"
                :data-v2-row-check="row.id"
                :checked="isChecked(row.id)"
                :aria-label="`勾选 ${row.username}`"
                @change="toggleRow(row.id)"
              />
            </td>
            <td class="ul-fix-l ul-fix-l-user">
              <a
                href="#"
                :class="row.is_active ? 'cv2-order-link' : 'cv2-order-link ul-link-disabled'"
                :data-v2-edit="row.id"
                @click.prevent="openEdit(row)"
              >{{ row.username }}</a>
            </td>
            <td v-if="colVisible('display_name')">
              <a
                href="#"
                :class="row.is_active ? 'cv2-order-link' : 'cv2-order-link ul-link-disabled'"
                :data-v2-edit-name="row.id"
                @click.prevent="openEdit(row)"
              >{{ row.display_name }}</a>
            </td>
            <td v-if="colVisible('rname')" />
            <td v-if="colVisible('bussType')" class="cv2-td-center ul-td-st">
              <span v-if="bussBadgeText(row.business_type) !== ''" class="ul-kind" :class="bussBadgeClass(row.business_type)" :data-v2-buss="row.id">
                <i class="ul-kind-dot" aria-hidden="true"></i>{{ bussBadgeText(row.business_type) }}
              </span>
            </td>
            <td v-if="colVisible('sign')" class="cv2-td-center" />
            <td v-if="colVisible('role')" class="cv2-td-center ul-td-st">
              <span v-if="row.role" class="ul-role" :title="row.role" :data-v2-role="row.id">{{ row.role }}</span>
            </td>
            <td v-if="colVisible('deposit_amount')" class="cv2-td-center ul-deposit" @click="startDepositEdit(row)">
              <el-input
                v-if="depositEditingId === row.id"
                v-model="depositDraft"
                class="ul-deposit-input"
                size="small"
                :data-v2-deposit-input="row.id"
                @keydown.enter.prevent="void commitDepositEdit(row)"
                @keydown.esc.prevent="cancelDepositEdit()"
                @blur="void commitDepositEdit(row)"
              />
              <span v-else class="cv2-num ul-deposit-cell" :data-v2-deposit="row.id">{{ row.deposit_amount ?? '' }}</span>
            </td>
            <td v-if="canDepositQuota && colVisible('quota')" class="cv2-td-center">
              <el-switch
                v-model="row.deposit_quota_enabled"
                :data-v2-quota-switch="row.id"
                @change="onQuotaSwitch(row)"
              />
            </td>
            <td v-if="colVisible('grab_limit_dl')" class="cv2-td-center cv2-td-num">{{ row.grab_limit_dl ?? '' }}</td>
            <td v-if="colVisible('rate')" class="cv2-td-center" />
            <td v-if="colVisible('priceLimit')" class="cv2-td-center cv2-td-num">{{ row.grab_price_min ?? '' }}-{{ row.grab_price_max ?? '' }}</td>
            <td v-if="colVisible('qq')">{{ row.qq || '' }}</td>
            <td v-if="colVisible('mobile')">{{ row.mobile || '' }}</td>
            <td v-if="colVisible('remark')" class="ul-remark" :title="row.remark || ''">{{ row.remark || '' }}</td>
            <td v-if="colVisible('is_active')" class="cv2-td-center">
              <el-switch v-model="row.is_active" :data-v2-status-switch="row.id" @change="onStatusSwitch(row)" />
            </td>
            <td v-if="colVisible('settle_forbidden')" class="cv2-td-center">
              <el-switch v-model="row.settle_forbidden" :data-v2-settle-switch="row.id" @change="onRealSwitch(row, 'settle_forbidden')" />
            </td>
            <td v-if="colVisible('grab_forbid_self_service')" class="cv2-td-center">
              <el-switch v-model="row.grab_forbid_self_service" :data-v2-forbid-switch="row.id" @change="onRealSwitch(row, 'grab_forbid_self_service')" />
            </td>
            <td v-if="colVisible('tag')">
              <a href="#" class="cv2-order-link" :data-v2-tag="row.id" @click.prevent="onRowTag(row)">{{ row.tag || '无' }}</a>
            </td>
            <td v-if="colVisible('games')">
              <a
                href="#"
                class="ul-games"
                :class="{ 'ul-games--empty': row.games.length === 0 }"
                :data-v2-games="row.id"
                @click.prevent="onRowGames(row)"
              >{{ row.games.length > 0 ? row.games.map((id) => gameNameOf(id)).join('、') : '无' }}</a>
            </td>
            <td v-if="colVisible('lastLoginTime')" class="cv2-td-center cv2-td-time">{{ row.last_login ?? '' }}</td>
            <td v-if="colVisible('orderBy')" class="cv2-td-center">
              <a
                href="#"
                class="cv2-order-link cv2-num"
                :data-v2-orderby="row.id"
                :title="ORDER_BY_CELL_TITLE"
                @click.prevent="onRowOrderby(row)"
              >{{ row.order_by ?? 0 }}</a>
            </td>
            <td v-if="colVisible('creator')" class="cv2-td-center">{{ row.createUserName || '' }}</td>
            <td class="cv2-td-center ul-fix-r ul-actions">
              <template v-if="row.deleted_at">
                <button type="button" class="cv2-rowact ul-act ul-act--restore" :data-v2-restore="row.id" title="恢复该用户" @click="onRestore(row)">恢复</button>
              </template>
              <template v-else>
                <button type="button" class="cv2-rowact" :data-v2-edit-btn="row.id" title="编辑用户资料" @click="openEdit(row)">编辑</button>
                <button type="button" class="cv2-rowact cv2-rowact--danger" :data-v2-reset="row.id" title="重置该用户的登录密码为123456" @click="resetDlg = { id: row.id }">重置</button>
              </template>
            </td>
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

    <!-- 设定游戏权限弹层（工具栏批量 / 单元格单人共用；关层即刷新照 island end 语义） -->
    <V2SetGamesDialog
      v-if="setGamesUids !== null"
      :uids="setGamesUids"
      :notify="showToast"
      @close="onGamesDlgClose"
      @saved="onGamesDlgClose"
    />

    <!-- 押金额度弹层（单人） -->
    <V2DepositQuotaDialog
      v-if="depositQuotaUid !== null"
      :user-id="depositQuotaUid"
      :notify="showToast"
      @close="onQuotaDlgClose"
      @saved="onQuotaDlgClose"
    />

    <!-- 编辑/新开系统子账户：页内弹窗装 v2 新开账号页（照旧页 iframe 大弹窗形态，标题照对标；
         内嵌态隐藏页签条、只渲染表单）。:key + destroy-on-close = 每开必重挂、换目标不复位；
         保存成功（saved）或弹窗关闭都刷新列表 -->
    <el-dialog
      :model-value="userDlg !== null"
      :title="userDlg === null || userDlg.id === null ? '新开系统子账户' : '编辑系统子账户'"
      class="cv2-dialog cv2-dialog--lg cv2-user-edit-dlg"
      destroy-on-close
      data-v2="dlg-user-edit"
      @update:model-value="onUserDlgClose"
    >
      <V2UserNew
        v-if="userDlg !== null"
        :key="userDlg.id ?? 'new'"
        :user-id="userDlg.id ?? undefined"
        @saved="onUserSaved"
      />
    </el-dialog>

    <!-- 接单统计卡片（lg） -->
    <V2OrderStatsCard v-if="statsOpen" :games="games" :notify="showToast" @close="statsOpen = false" />

    <!-- 重复收款支付宝子账号检测卡片（lg） -->
    <V2AlipayCheckCard v-if="alipayOpen" :notify="showToast" @close="alipayOpen = false" />

    <!-- 设自助接单数 / 排序值 / 标记 prompt 弹层（标题与上限随 kind 变，照 island 三处 prompt） -->
    <el-dialog
      :model-value="promptDlg !== null"
      :title="promptTitle"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-prompt"
      @update:model-value="promptDlg = null"
    >
      <el-input
        v-if="promptDlg !== null"
        v-model="promptDlg.value"
        :maxlength="promptMaxlength"
        data-v2="prompt-input"
        @keydown.enter.prevent="void confirmPrompt()"
      />
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="promptDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="prompt-ok" @click="void confirmPrompt()">确定</button>
      </div>
    </el-dialog>

    <!-- 修改业务类型（radio 不预选；「请选择业务类型」拦「没选就保存」） -->
    <el-dialog
      :model-value="bizTypeDlg !== null"
      title="修改业务类型"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-busstype"
      @update:model-value="bizTypeDlg = null"
    >
      <div class="cv2-dlg-radios" data-v2="busstype-radios">
        <label v-for="o in BUSINESS_TYPE_OPTIONS" :key="o.value" class="cv2-radio">
          <input v-if="bizTypeDlg !== null" v-model="bizTypeDlg.value" type="radio" name="v2-user-busstype" :value="o.value" />
          {{ o.label }}
        </label>
      </div>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="bizTypeDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="busstype-ok" @click="void onBizTypeSave()">确定</button>
      </div>
    </el-dialog>

    <!-- 批量设置内部抢单价格范围限制（6 条校验文案与顺序照旧页） -->
    <el-dialog
      :model-value="priceRangeDlg !== null"
      title="批量设置内部抢单价格范围限制"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-price-range"
      @update:model-value="priceRangeDlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">接单价范围</span>
        <span class="ul-pricerange">
          <el-input v-if="priceRangeDlg !== null" v-model="priceRangeDlg.min" class="ul-priceinput" placeholder="￥" data-v2="price-min" />
          <span class="ul-pricedash">-</span>
          <el-input v-if="priceRangeDlg !== null" v-model="priceRangeDlg.max" class="ul-priceinput" placeholder="￥" data-v2="price-max" />
        </span>
      </div>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="priceRangeDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="price-range-ok" @click="void onPriceRangeSave()">保存</button>
      </div>
    </el-dialog>

    <!-- 重置密码确认（成功不刷新照旧页） -->
    <el-dialog
      :model-value="resetDlg !== null"
      title="重置密码"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-reset"
      @update:model-value="resetDlg = null"
    >
      <p class="cv2-dlg-text">确定将该用户的登录密码重置为 123456 吗？</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="resetDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="reset-ok" @click="void onResetConfirm()">确定</button>
      </div>
    </el-dialog>

    <!-- 删除用户两跳确认（文案逐字；实际为软删、可恢复） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="删除用户"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-delete"
      @update:model-value="delDlg = null"
    >
      <template v-if="delDlg !== null && delDlg.step === 1">
        <p class="cv2-dlg-text">{{ DEL_FIRST_1 }}</p>
        <p class="cv2-dlg-text ul-delwarn">{{ DEL_FIRST_2 }}</p>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="del-next" @click="onDelNext">确定</button>
        </div>
      </template>
      <template v-else-if="delDlg !== null && delDlg.step === 2">
        <p class="cv2-dlg-text">{{ DEL_SECOND_1 }}</p>
        <p class="cv2-dlg-text ul-delwarn">{{ DEL_SECOND_2 }}</p>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger" data-v2="del-confirm" @click="void onDelConfirm()">确定删除</button>
        </div>
      </template>
    </el-dialog>

    <!-- 炭火 toast（居中深条） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（类或 var 引用，⛔ 不写裸色值）。 */

/* 数字列等宽（等宽是一切数据的身份；尺寸/字体非色值）。 */
.cv2-num {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 筛选区 = 连体网格组（整段拷自管理订单 flt- 形态，前缀改 uf-）；
 * 组 = [标签格 + 值格…] 零间距 34px 带边框条，组间留缝自动换行（⚠️ EP 内部件须 :deep()）。
 * 紧凑 = 间距收紧（管理订单 2026-11 同档：组间 6/8、行距 6、面板内边距 10/12），不压组高。 */
.cv2-page .cv2-filter {
  padding: 10px 12px;
}

.uf-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
}

.uf-group {
  display: inline-flex;
  align-items: stretch;
  height: 32px;
  border: 1px solid var(--tt2-line);
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
  color: color-mix(in srgb, var(--tt2-ink) 35%, var(--tt2-dim));
  white-space: nowrap;
}

.uf-cell {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.uf-cell--grow {
  flex: 1;
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

/* 宽度档（挂在 EP 组件根上）。 */
.uf-w150 { width: 150px; }
.uf-w170 { width: 170px; }
.uf-w106 { width: 106px; }

/* 搜索排：独立于换行池之外的一行。 */
.uf-searchrow {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
  margin-top: 6px;
}

.uf-searchrow .uf-cell--grow {
  min-width: 200px;
}

.uf-grow {
  flex: 1;
}

/* 统计行 = 原版橙（对标证据件字面色，走 --tt2-summary-orange token）。 */
.cv2-total-line {
  color: var(--tt2-summary-orange);
}

/* ── sticky 冻结列（管理订单先例的左向版 + 右向操作列）：
 *    勾选列钉 left:0、登录账号列钉 left:36px（勾选列固定 36 不给拖拽）；
 *    底色随行状态补齐（面板白 / hover 晕 / 勾选青绿底）。 */
.ul-fix-l,
.ul-fix-r {
  position: sticky;
  background: var(--tt2-panel);
}

th.ul-fix-l,
th.ul-fix-r {
  z-index: 3;
}

td.ul-fix-l,
td.ul-fix-r {
  z-index: 2;
}

.ul-fix-l-check {
  left: 0;
  border-right: 1px solid var(--tt2-line);
}

.ul-fix-l-user {
  left: 36px;
  border-right: 1px solid var(--tt2-line);
}

.ul-fix-r {
  right: 0;
  border-left: 1px solid var(--tt2-line);
}

tbody tr:hover .ul-fix-l,
tbody tr:hover .ul-fix-r {
  /* 晕是 rgba：垫白底再叠同色晕 = 与其它单元格的复合结果一致 */
  background: var(--tt2-panel);
  background-image: linear-gradient(var(--tt2-hover-wash), var(--tt2-hover-wash));
}

tbody tr.cv2-row--checked .ul-fix-l,
tbody tr.cv2-row--checked .ul-fix-r {
  background: var(--tt2-row-checked);
}

td.ul-actions {
  white-space: nowrap;
}

td.ul-actions .cv2-rowact + .cv2-rowact {
  margin-left: 4px;
}

/* 表内开关收小，关态走暖灰，开态走主题青。 */
.cv2-table :deep(.el-switch) {
  height: 18px;
  --el-switch-on-color: var(--tt2-primary);
  --el-switch-off-color: var(--tt2-btn-soft-line);
}
.cv2-table :deep(.el-switch__core) {
  min-width: 32px;
  height: 18px;
  border-radius: 9px;
}
.cv2-table :deep(.el-switch__core .el-switch__action) {
  width: 14px;
  height: 14px;
}
.cv2-table :deep(.el-switch.is-checked .el-switch__core .el-switch__action) {
  left: calc(100% - 15px);
}

/* 游戏权限：暖米小签。没有权限时字变浅，仍可点开设置。 */
.ul-games {
  display: inline-block;
  max-width: 100%;
  padding: 2px 8px;
  border: 1px solid var(--tt2-filter-line);
  border-radius: 4px;
  background: var(--tt2-filter-label-bg);
  color: var(--tt2-ink);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.ul-games--empty {
  color: var(--tt2-dim);
  background: transparent;
}
.ul-games:hover {
  border-color: var(--tt2-primary);
  color: var(--tt2-primary-deep);
}

/* 禁用行的账号链接 = 红（island accountLink 的禁用红分支；色取 error token）。 */
.ul-link-disabled {
  color: var(--tt2-st-error);
}

/* 业类 = 色点 + 字。职位是普通文字。编辑走草绿，重置走红。 */
.ul-kind {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--tt2-ink);
  white-space: nowrap;
}
.ul-kind-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--tt2-dim);
  box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 25%, transparent);
}
.ul-kind--doing .ul-kind-dot { color: var(--tt2-st-doing-dot); background: currentColor; }
.ul-kind--play .ul-kind-dot { color: var(--tt2-st-paused-dot); background: currentColor; }
.ul-kind--both .ul-kind-dot { color: var(--tt2-st-done-dot); background: currentColor; }
.ul-role {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: middle;
  color: var(--tt2-ink);
  white-space: nowrap;
}
.ul-actions .ul-act--restore {
  background: var(--tt2-st-doing-bg);
  border-color: var(--tt2-st-doing-line);
  color: var(--tt2-st-doing);
}
.ul-actions .ul-act--restore:hover {
  border-color: var(--tt2-st-doing);
}

/* 业类、职位两列侧距收一点，色点和字才不贴格线。 */
.cv2-table td.ul-td-st {
  padding: 0 6px;
}

/* 开关四列表头：钉宽 90/100px 的配套——减 th 横向 padding（全局 10px 12px 会把
 * 6 字表头「个人押金额度」顶到 ~102px 才不折行）。只作用于本页这四列。 */
.cv2-table th.ul-th-narrow {
  padding: 10px 6px;
}

/* ── 保证金原位编辑格：点格展开 input（Enter/blur 提交、Esc 还原）。── */
.ul-deposit {
  cursor: pointer;
}

.ul-deposit-input {
  width: 80px;
}

.ul-deposit-cell:hover {
  color: var(--tt2-primary-deep);
}

/* 备注列长文本单行截断（44px 行高的家族口径）。 */
.ul-remark {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 删除确认的警告段略放大行距（长句可读性；尺寸非色值）。 */
.ul-delwarn {
  line-height: 1.7;
}

/* 抢单价范围弹层的 min-max 连排。 */
.ul-pricerange {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.ul-priceinput {
  width: 120px;
}

.ul-pricedash {
  color: var(--tt2-dim);
}

/* ── 列显隐选择器（整段拷自管理订单 colpick 形态，前缀改 ul-）。── */
.ul-colpick {
  position: relative;
  margin-left: 4px;
}

.ul-colpick-btn {
  width: 34px;
  height: 34px; /* 与加高后的工具条同排同高（矮一截会很扎眼） */
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ul-colpick-ico {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
}

.ul-colpick-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--tt2-dim);
}

.ul-colpick-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ul-colpick-group :deep(.el-checkbox) {
  margin-right: 0;
  height: 28px;
}

/* ── 表格面板：限高内部滚动（冻结列依赖），分页条常驻面板底缘。── */
.cv2-table-panel {
  max-height: calc(100vh - 430px);
  min-height: 320px;
  overflow: auto;
}

/* 编辑/新开弹层（装 v2 新开账号页）：1400px 下角色 5 列栅格每列 95.4px ≥ 5 字职位 92px
 * 不溢出（业务方 2026-11-06 四图）；页面级覆写压过 lg 档 860px，body padding 不受影响。 */
.cv2-page :deep(.el-dialog.cv2-user-edit-dlg) {
  width: min(1400px, 96vw);
}

/* 弹层表单行距（尺寸非色值）。 */
.cv2-dialog .cv2-field {
  margin-bottom: 12px;
}

/* 业务类型等小弹层的 radio 放不下换行、标签不竖切。 */
.cv2-dlg-radios {
  flex-wrap: wrap;
  row-gap: 10px;
}

.cv2-dlg-radios .cv2-radio {
  white-space: nowrap;
}

/* 全局提示 = 对标 layer.msg 居中深条（管理订单同款；覆盖 carbon.css 顶部白卡片形态）。 */
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

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}
</style>
