<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 「新开系统账号」v2（`/v2/user-new`；菜单映射 `system-user-new`，v2/v2Menu.ts）。合并旧侧栏
 * 【系统人员】的「新开系统账号」与「自助注册」两个入口：页内两页签，Tab1 = 账号表单
 * （字段/校验/载荷逐字段对齐 island 页 `layui-island/pages/user-adduser.ts`），Tab2 = 自助注册
 * 三卡 + 记录表（照 `pages/self-registration.ts`，MESSAGES 逐字）。剪贴板抄 `user-adduser.ts`
 * 的完整版（clipboard + execCommand 兜底、返回布尔），⛔ 不 import layui-island 模块；
 * 提示一律页面自绘 toast / 自建 sm 弹窗（ElMessage/ElMessageBox 拿不到 .ttv2 炭火皮）。
 * 编辑态两个入口：整页路由 `?id=`（fillForm 同口径）；人员管理页把本组件以 `userId` prop
 * 装进页内弹窗——内嵌态隐藏页签条、只渲染 Tab1、跳过 Tab2 取数，保存成功 emit('saved') 留在弹窗。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import {
  createAccount,
  fetchAccountRoles,
  getAccount,
  updateAccount,
  type AccountRole,
  type AccountRow,
  type AccountWritePayload,
} from '@/api/accounts'
import {
  createSelfRegInvite,
  fetchSelfRegConfig,
  listSelfRegInvites,
  revokeSelfRegInvite,
  saveSelfRegConfig,
  type SelfRegConfig,
  type SelfRegInviteRow,
  type SelfRegInviteStatus,
} from '@/api/selfRegistration'

/* ── 页签 ──────────────────────────────────────────────────────────── */

const activeTab = ref<'form' | 'selfreg'>('form')

/* ── toast（页面自绘；错误停更久）──────────────────────────────────── */

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

onUnmounted(() => {
  if (toastTimer !== null) clearTimeout(toastTimer)
})

/* ── 剪贴板（抄 user-adduser.ts 的完整版：clipboard + execCommand 兜底）── */

async function copyToClipboard(text: string): Promise<boolean> {
  const legacy = (): boolean => {
    const el = document.createElement('textarea')
    el.value = text
    // 放到视口外但仍可选中：`display:none` 会让 execCommand 失败
    el.setAttribute('readonly', '')
    el.style.position = 'fixed'
    el.style.top = '-1000px'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(el)
    return ok
  }

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 非安全上下文/无权限 ⇒ 落到 execCommand
    }
  }
  return legacy()
}

/* ── 旧页同源常量（文案/正则逐字照抄，出处见各条注释）────────────── */

// 对标 adduser.js 的 `loginAcc` / `userName` / `VerifyMoblie` / `VerifyMoeny` 判据（逐字）。
const LOGIN_ACC_RE = /^[a-zA-Z0-9_\u4e00-\u9fa5\s·]+$/
const USER_NAME_RE = /^[a-zA-Z0-9_\u4e00-\u9fa5\s·]+$/
const MOBILE_RE = /^1[3-9]\d{9}$/
const MONEY_RE = /^\d{1,5}$/

/** 提成模式三档（对标 `autoBrokerageType` 的协议值，字面值同 island 页）。 */
const COMMISSION_MODE_FIXED = 10
const COMMISSION_MODE_ORDER_PERCENT = 20

const COPY_TOAST_OK = '已成功复制到剪贴板'
const COPY_TOAST_FAIL = '复制失败了'

/** 新增成功后弹窗正文（对标 adduser.js:715 实测原文模板；全角标点）。 */
function credentialDialogText(username: string, password: string): string {
  return `新开子账户成功，该用户登录账号：${username}，初始登录密码：${password}`
}

/** 职位内置提示（island 页 `ROLE_TIPS` 8 条逐字；按职位名 indexOf 命中）。 */
const ROLE_TIPS: readonly (readonly [string, string])[] = [
  ['系统管理员', '是系统内最高权限，拥有所有操作权限，正常情况下建议设置此职位的子账户数量不超过3个'],
  ['超级管理员', '是系统内最高权限，拥有所有操作权限，正常情况下建议设置此职位的子账户数量不超过3个'],
  ['总经理', '的权限仅次于系统管理员，拥有系统内绝大部分操作权限，默认情况下没有该职位设定数量不要超过2个'],
  ['客服', '一般用于接单录单销售的人用，拥有录单、查单、管理订单等通用权限，默认情况下没有删除订单权限'],
  ['代练员', '为最低权限位，适合给代练员、打手、团员或下家使用。这个职位的人只能看到指派给TA的单子，看不到别人的，也看不到接单价'],
  ['开发', '开发人员专用权限，仅用于开发人员协助本系统解决问题时使用，若担心安全，可以删除'],
  ['客服主管', '默认情况下，除了拥有客服的权限外，还拥有删除订单、更改接单价、新开系统子账号、管理子账号、管理打手与客服提成账单等高级功能，一般建议只给部门主管级人员使用'],
  ['外部录单', '适合给合作的销售用，可以录单、编辑,只能看到自己录的单子，看不到别人录的，也没有派单、删除权限'],
]

/** 提成金额格的 label（island `commissionLabel` 逐字；20 是后端默认模式）。 */
function commissionLabel(mode: number): string {
  return mode === COMMISSION_MODE_FIXED ? '固定提成金额' : '百分比数值'
}

/** 提成金额格的常驻 hint（island `commissionTip` 逐字，focus tips 降级为常驻）。 */
function commissionTip(mode: number): string {
  return mode === COMMISSION_MODE_FIXED
    ? '请输入固定的金额，例如：5、10，默认为0'
    : '请输入百分比纯数字（不要有百分号），例如：5，代表5%'
}

/* ── 编辑态（整页 `?id=` / 人员管理页弹窗内嵌，prop 优先、query 兜底，照 V2OrderNew 先例）── */

const props = defineProps<{ userId?: number }>()
const emit = defineEmits<{ saved: [] }>()

function parseEditId(raw: unknown): number | null {
  const value = Array.isArray(raw) ? raw[0] : raw
  const n = Number(value)
  return typeof value === 'string' && value !== '' && Number.isInteger(n) && n > 0 ? n : null
}

const route = useRoute()
/** 编辑目标 id：prop（弹窗内嵌态）优先，路由 query `?id=` 兜底（整页态）。 */
const editId = computed(() => props.userId ?? parseEditId(route.query.id))
const isEdit = computed(() => editId.value !== null)
/** 内嵌态 = userId prop 在场：隐藏页签条、只渲染 Tab1、跳过 Tab2 取数、保存后留在弹窗。 */
const embedded = computed(() => props.userId !== undefined)

/* ── Tab1 表单模型 ────────────────────────────────────────────────── */

const username = ref('')
const displayName = ref('')
const idCardNo = ref('')
const businessType = ref(1)
const memberType = ref(0)
const isStudio = ref(false)
const qq = ref('')
const mobile = ref('')
const hiredOn = ref('')
const tag = ref('')
const remark = ref('')
const liveUrl = ref('')
const commissionMode = ref(COMMISSION_MODE_ORDER_PERCENT)
const commissionValue = ref('0')
const alipayAccount = ref('')
const alipayName = ref('')
const bankName = ref('')
const bankCardNo = ref('')
const depositAmount = ref('0')
const grabLimitDl = ref('0')
const grabLimitPw = ref('0')
const grabPriceMin = ref('0')
const grabPriceMax = ref('99999')
const games = ref<number[]>([])
const role = ref('')

const gameOptions = ref<ConfigRow[]>([])
const roleOptions = ref<AccountRole[]>([])

/** 「代练员」移到首位（业务方 2026-11-06 定）；其余保持接口原序，找不到就不动。 */
function moveTakerFirst(roles: AccountRole[]): AccountRole[] {
  const i = roles.indexOf('代练员')
  if (i <= 0) return roles
  return [roles[i]!, ...roles.slice(0, i), ...roles.slice(i + 1)]
}

/** 今天（YYYY-MM-DD）；入职日期默认值（island 页同款）。 */
function todayStr(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function setHiredOn(v: string | null): void {
  hiredOn.value = v === null ? '' : v
}

/** 空串 → undefined（= 载荷整键不发；island `numOrUndefined` 同式）。 */
function numOrUndefined(raw: string): number | undefined {
  const text = raw.trim()
  if (text === '') return undefined
  const value = Number(text)
  return Number.isFinite(value) ? value : undefined
}

const commissionValueLabel = computed(() => commissionLabel(commissionMode.value))
const commissionValueHint = computed(() => commissionTip(commissionMode.value))

const roleTipText = computed(() => {
  if (role.value === '') return ''
  const hit = ROLE_TIPS.find(([key]) => role.value.indexOf(key) > -1)
  return hit === undefined ? '' : role.value + hit[1]
})

/* ── 校验（island `validateForm` 同序同文案；失败落在字段下方红字）────── */

type FieldError = { anchor: string; msg: string }

const fieldError = ref<FieldError | null>(null)

function validateForm(): FieldError | null {
  const loginAcc = username.value.trim()
  if (loginAcc === '') return { anchor: 'username', msg: '登录账号不能为空' }
  if (loginAcc.length < 2) return { anchor: 'username', msg: '登录账号长度最小是2位，请补齐' }
  if (!LOGIN_ACC_RE.test(loginAcc)) return { anchor: 'username', msg: '登录账号不能有特殊字符' }

  const userName = displayName.value.trim()
  if (userName === '') return { anchor: 'displayName', msg: '姓名/名称不能为空' }
  if (!USER_NAME_RE.test(userName)) return { anchor: 'displayName', msg: '姓名/名称不能有特殊字符' }
  if (/(^_)|(__)|(_+$)/.test(userName)) return { anchor: 'displayName', msg: '姓名/名称首尾不能出现下划线' }
  if (userName.indexOf(' ') > -1) return { anchor: 'displayName', msg: '姓名/名称不能有空格' }
  if (userName.length < 2) return { anchor: 'displayName', msg: '姓名/名称长度最小是2位，请补齐' }

  const mobileValue = mobile.value.trim()
  if (mobileValue !== '' && !MOBILE_RE.test(mobileValue)) {
    return { anchor: 'mobile', msg: '输入的联系电话格式不正确，要么请留空' }
  }

  if (!Number.isFinite(businessType.value)) return { anchor: 'businessType', msg: '请选择业务类型' }

  if (
    !MONEY_RE.test(grabPriceMin.value.trim()) ||
    !MONEY_RE.test(grabPriceMax.value.trim())
  ) {
    return { anchor: 'grabPrice', msg: '内部抢单价格范围格式不正确，只能填数字0到99999' }
  }
  if (games.value.length < 1) return { anchor: 'games', msg: '请为该用户至少选择一款游戏权限' }
  if (role.value === '') return { anchor: 'role', msg: '请为该用户选择一个职位权限' }

  const numberFields: readonly [string, string][] = [
    [commissionValue.value, '提成金额/百分比'],
    [grabLimitDl.value, '内部抢代练单上限'],
    [grabLimitPw.value, '内部抢陪玩单上限'],
    [depositAmount.value, '已交保证金'],
  ]
  for (const [raw, label] of numberFields) {
    const text = raw.trim()
    if (text !== '' && !/^\d+(\.\d+)?$/.test(text)) {
      return { anchor: anchorOfNumberField(label), msg: `${label}只能填数字` }
    }
  }

  if (!isEdit.value && hiredOn.value.trim() === '') return { anchor: 'hiredOn', msg: '请选择入职日期' }
  if (!isEdit.value && commissionValue.value.trim() === '') {
    return { anchor: 'commissionValue', msg: '请填写提成金额/百分比' }
  }
  return null
}

function anchorOfNumberField(label: string): string {
  if (label === '提成金额/百分比') return 'commissionValue'
  if (label === '内部抢代练单上限') return 'grabLimitDl'
  if (label === '内部抢陪玩单上限') return 'grabLimitPw'
  return 'depositAmount'
}

/* ── 载荷（island `buildPayload` 同式：数字发 number、空则整键不发）───── */

function buildPayload(): AccountWritePayload {
  const payload: AccountWritePayload = {
    username: username.value.trim(),
    display_name: displayName.value.trim(),
    role: role.value,
    games: [...games.value],
    // 兜底值与 island 同源：1 专做代练 / 0 普通成员 / 20 订单价百分比。
    business_type: Number.isFinite(businessType.value) ? businessType.value : 1,
    member_type: Number.isFinite(memberType.value) ? memberType.value : 0,
    // 布尔始终发送（island `:227-228` 同口径）。
    is_studio: isStudio.value === true,
    commission_mode: Number.isFinite(commissionMode.value) ? commissionMode.value : COMMISSION_MODE_ORDER_PERCENT,
    // 旧页隐藏格同款：base_salary 不渲染、固定发 0。
    base_salary: 0,
    id_card_no: idCardNo.value.trim(),
    alipay_account: alipayAccount.value.trim(),
    alipay_name: alipayName.value.trim(),
    bank_name: bankName.value.trim(),
    bank_card_no: bankCardNo.value.trim(),
    live_url: liveUrl.value.trim(),
    qq: qq.value.trim(),
    mobile: mobile.value.trim(),
    tag: tag.value.trim(),
    remark: remark.value.trim(),
  }
  const bag = payload as Record<string, unknown>
  const optionalNumbers: readonly [string, string][] = [
    ['commission_value', commissionValue.value],
    ['grab_limit_dl', grabLimitDl.value],
    ['grab_limit_pw', grabLimitPw.value],
    ['grab_price_min', grabPriceMin.value],
    ['grab_price_max', grabPriceMax.value],
    ['deposit_amount', depositAmount.value],
  ]
  for (const [key, raw] of optionalNumbers) {
    const value = numOrUndefined(raw)
    if (value !== undefined) bag[key] = value
  }
  const hired = hiredOn.value.trim()
  if (hired !== '') payload.hired_on = hired
  return payload
}

/* ── 编辑态回填（island `fillForm` 同口径）────────────────────────── */

function fillForm(account: AccountRow): void {
  const text = (v: string | null | undefined): string => (v === null || v === undefined ? '' : String(v))
  // 数字格：`String(Number(v))`；库里没值时保留默认值、不写空串（默认 0 / 抢单价上限 99999）。
  const money = (v: number | string | null | undefined): string =>
    v === null || v === undefined || v === '' ? '' : String(Number(v))
  const setNumber = (target: { value: string }, v: number | string | null | undefined): void => {
    const s = money(v)
    if (s === '') return
    target.value = s
  }
  username.value = text(account.username)
  displayName.value = text(account.display_name)
  idCardNo.value = text(account.id_card_no)
  setNumber(commissionValue, account.commission_value)
  alipayAccount.value = text(account.alipay_account)
  alipayName.value = text(account.alipay_name)
  bankName.value = text(account.bank_name)
  bankCardNo.value = text(account.bank_card_no)
  setNumber(grabLimitDl, account.grab_limit_dl)
  setNumber(grabLimitPw, account.grab_limit_pw)
  setNumber(grabPriceMin, account.grab_price_min)
  setNumber(grabPriceMax, account.grab_price_max)
  liveUrl.value = text(account.live_url)
  qq.value = text(account.qq)
  mobile.value = text(account.mobile)
  const hired = text(account.hired_on)
  hiredOn.value = hired === '' ? todayStr() : hired
  tag.value = text(account.tag)
  remark.value = text(account.remark)
  setNumber(depositAmount, account.deposit_amount)
  businessType.value = account.business_type ?? 1
  memberType.value = account.member_type ?? 0
  commissionMode.value = account.commission_mode ?? COMMISSION_MODE_ORDER_PERCENT
  isStudio.value = account.is_studio === true
  role.value = account.role ?? ''
  games.value = [...account.games]
}

/** 新开态复位（新增成功复制凭据后回到空白表单、留在本页）。 */
function resetForm(): void {
  username.value = ''
  displayName.value = ''
  idCardNo.value = ''
  businessType.value = 1
  memberType.value = 0
  isStudio.value = false
  qq.value = ''
  mobile.value = ''
  hiredOn.value = todayStr()
  tag.value = ''
  remark.value = ''
  liveUrl.value = ''
  commissionMode.value = COMMISSION_MODE_ORDER_PERCENT
  commissionValue.value = '0'
  alipayAccount.value = ''
  alipayName.value = ''
  bankName.value = ''
  bankCardNo.value = ''
  depositAmount.value = '0'
  grabLimitDl.value = '0'
  grabLimitPw.value = '0'
  grabPriceMin.value = '0'
  grabPriceMax.value = '99999'
  games.value = []
  role.value = ''
  fieldError.value = null
}

/* ── 保存 ─────────────────────────────────────────────────────────── */

const saving = ref(false)
const credOpen = ref(false)
const credential = ref<{ username: string; password: string } | null>(null)
const credOrigin = ref('')
const copying = ref(false)

const credentialBody = computed(() =>
  credential.value === null ? '' : credentialDialogText(credential.value.username, credential.value.password),
)

async function onSave(): Promise<void> {
  if (saving.value) return
  fieldError.value = validateForm()
  if (fieldError.value !== null) return
  saving.value = true
  try {
    const payload = buildPayload()
    const id = editId.value
    if (isEdit.value && id !== null) {
      await updateAccount(id, payload)
      showToast('已保存')
      // 内嵌态留在弹窗（列表收到 saved 刷新）；整页态回退路由。
      if (embedded.value) emit('saved')
      else history.back()
      return
    }
    const res = await createAccount(payload)
    showToast('账号已创建')
    credential.value = { username: res.account.username, password: res.initial_password }
    credOrigin.value = window.location.origin
    credOpen.value = true
  } catch (e) {
    showToast(e instanceof Error ? e.message : (isEdit.value ? '保存失败' : '创建失败'), 'error')
  } finally {
    saving.value = false
  }
}

/** 「确定并复制」：剪贴板文本末尾带登录网址（复制时刻的 origin）；成功才关弹窗并复位表单。 */
async function onCopyCredential(): Promise<void> {
  const cred = credential.value
  if (cred === null) return
  const text = `你的登录账号：${cred.username} 初始登录密码：${cred.password} 登录网址：${window.location.origin}`
  copying.value = true
  try {
    const ok = await copyToClipboard(text)
    if (ok) {
      showToast(COPY_TOAST_OK)
      credOpen.value = false
      credential.value = null
      resetForm()
    } else {
      showToast(COPY_TOAST_FAIL, 'error')
    }
  } finally {
    copying.value = false
  }
}

/* ── Tab2 自助注册（照 island `self-registration.ts`，MESSAGES 逐字）──── */

const SELFREG_MESSAGES = {
  noGames: '请先勾选至少一款注册游戏',
  saved: '注册配置已保存',
  linkCopied: '链接已复制，7 天内有效，注册一人后自动失效',
  copyFailed: '链接已生成，但自动复制失败，请在下方手动复制',
  empty: '还没有生成过注册链接',
  revokeConfirmFirst: '作废后这条链接立即失效，持有链接的人将无法注册。确定作废吗？',
  revokeConfirmSecond: '再次确认：作废无法恢复，需要时只能重新生成一条链接。确定作废吗？',
  revoked: '已作废，链接立即失效',
  failed: '操作失败',
} as const

/** 编号前缀的取值形状：只允许 1~5 个英文字母（服务端同一条正则）。 */
const NUMBER_PREFIX_RE = /^[A-Za-z]{1,5}$/

/** 注册链接的 m 端路径（`location.origin + '/m/register?tk=' + token`）。 */
const REGISTER_PAGE_PATH = '/m/register?tk='

const STATUS_LABEL: Record<SelfRegInviteStatus, string> = {
  unused: '未使用',
  used: '已使用',
  expired: '已过期',
}

const STATUS_CLASS: Record<SelfRegInviteStatus, string> = {
  unused: 'cv2-st--reg-unused',
  used: 'cv2-st--reg-used',
  expired: 'cv2-st--reg-expired',
}

const regGameIds = ref<number[]>([])
const forbidDefault = ref(false)
const numberPrefix = ref('')
const numberStart = ref('')
const inviteRows = ref<SelfRegInviteRow[]>([])
const regLink = ref<{ url: string; expiresAt: string } | null>(null)
const regSaving = ref(false)
const regGenerating = ref(false)
const revoking = ref(false)

/** 作废两跳：0 = 关闭、1 = 第一问、2 = 第二问（照旧页两连问都确认才发请求）。 */
const revokeStep = ref<0 | 1 | 2>(0)
const revokeTarget = ref<number | null>(null)

function applySelfRegConfig(config: SelfRegConfig): void {
  regGameIds.value = [...config.game_ids]
  forbidDefault.value = config.forbid_orders_default === true
  numberPrefix.value = config.number_prefix ?? ''
  numberStart.value =
    config.number_start === null || config.number_start === undefined ? '' : String(config.number_start)
}

/** ISO 8601 → `2026-10-16 12:00:00`（截到秒，时区偏移留原地）。 */
function formatTime(value: string): string {
  return value.replace('T', ' ').slice(0, 19)
}

function parseNumberStart(): number | null {
  const raw = numberStart.value.trim()
  if (raw === '') return null
  const n = Number(raw)
  return Number.isInteger(n) ? n : null
}

/** 编号两栏的预检（文案带字段名；服务端 §3.1 还会再判一次）。 */
function numberConfigPrecheckError(): string {
  const prefix = numberPrefix.value.trim()
  if (prefix !== '' && !NUMBER_PREFIX_RE.test(prefix)) return '编号前缀只能是 1~5 个英文字母'
  const raw = numberStart.value.trim()
  if (raw !== '') {
    const n = Number(raw)
    if (!Number.isInteger(n) || n < 1) return '起始编号必须是 ≥ 1 的整数'
  }
  return ''
}

async function reloadInvites(): Promise<void> {
  try {
    inviteRows.value = await listSelfRegInvites()
  } catch (e) {
    showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
  }
}

async function onSaveConfig(): Promise<void> {
  if (regSaving.value) return
  if (regGameIds.value.length === 0) {
    showToast(SELFREG_MESSAGES.noGames, 'error')
    return
  }
  const problem = numberConfigPrecheckError()
  if (problem !== '') {
    showToast(problem, 'error')
    return
  }
  regSaving.value = true
  try {
    await saveSelfRegConfig({
      game_ids: [...regGameIds.value],
      forbid_orders_default: forbidDefault.value === true,
      number_prefix: numberPrefix.value.trim(),
      number_start: parseNumberStart(),
    })
    showToast(SELFREG_MESSAGES.saved)
  } catch (e) {
    showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
  } finally {
    regSaving.value = false
  }
}

async function onGenerate(): Promise<void> {
  if (regGenerating.value) return
  regGenerating.value = true
  try {
    const created = await createSelfRegInvite()
    const url = `${window.location.origin}${REGISTER_PAGE_PATH}${created.token}`
    const copied = await copyToClipboard(url)
    // 复制失败不算生成失败：链接必须显示出来，让用户手动复制。
    regLink.value = { url, expiresAt: created.expires_at }
    showToast(copied ? SELFREG_MESSAGES.linkCopied : SELFREG_MESSAGES.copyFailed, copied ? 'info' : 'error')
  } catch (e) {
    showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
  } finally {
    regGenerating.value = false
  }
}

function askRevoke(id: number): void {
  if (revoking.value) return
  revokeTarget.value = id
  revokeStep.value = 1
}

function cancelRevoke(): void {
  revokeStep.value = 0
  revokeTarget.value = null
}

async function onRevokeConfirm(): Promise<void> {
  const id = revokeTarget.value
  if (id === null || revokeStep.value !== 2 || revoking.value) return
  cancelRevoke()
  revoking.value = true
  try {
    await revokeSelfRegInvite(id)
    showToast(SELFREG_MESSAGES.revoked)
  } catch (e) {
    showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
  } finally {
    revoking.value = false
  }
  await reloadInvites()
}

/* ── 首屏（选项并行取；失败兜底文案照旧页）────────────────────────── */

onMounted(async () => {
  const accountId = editId.value
  const [rolesRes, gamesRes, accountRes] = await Promise.all([
    fetchAccountRoles().catch((e: unknown) => {
      showToast(e instanceof Error ? e.message : '职位列表加载失败', 'error')
      return [] as AccountRole[]
    }),
    fetchAllGames().catch((e: unknown) => {
      showToast(e instanceof Error ? e.message : '游戏列表加载失败', 'error')
      return [] as ConfigRow[]
    }),
    accountId !== null
      ? getAccount(accountId).catch((e: unknown) => {
          showToast(e instanceof Error ? e.message : '账号详情加载失败', 'error')
          return null
        })
      : Promise.resolve(null),
  ])
  roleOptions.value = moveTakerFirst(rolesRes)
  gameOptions.value = gamesRes
  if (accountRes !== null) fillForm(accountRes)
  // 新开态：入职日期「值空才补」今天（编辑态 fillForm 已写库里的值）。
  if (hiredOn.value.trim() === '') hiredOn.value = todayStr()

  // 内嵌态（弹窗）不渲染 Tab2 ⇒ 自助注册的两条首屏请求一并跳过（照旧 iframe 弹窗只有表单）。
  if (embedded.value) return
  const [configRes, invitesRes] = await Promise.all([
    fetchSelfRegConfig().catch((e: unknown) => {
      showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
      return null
    }),
    listSelfRegInvites().catch((e: unknown) => {
      showToast(e instanceof Error ? e.message : SELFREG_MESSAGES.failed, 'error')
      return null
    }),
  ])
  if (configRes !== null) applySelfRegConfig(configRes)
  if (invitesRes !== null) inviteRows.value = invitesRes
})
</script>

<template>
  <div class="cv2-page tt-usernew" data-v2-page="user-new">
    <!-- 页头两页签（无大标题；页名由顶栏 meta.title 承担）；弹窗内嵌态整条隐藏。 -->
    <div v-if="!embedded" class="cv2-ptabs" data-v2="tabs">
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'form' }"
        data-v2-tab="form"
        @click="activeTab = 'form'"
      >
        新开账号
      </button>
      <button
        type="button"
        class="cv2-ptab"
        :class="{ 'cv2-ptab--on': activeTab === 'selfreg' }"
        data-v2-tab="selfreg"
        @click="activeTab = 'selfreg'"
      >
        自助注册
      </button>
    </div>

    <!-- ══ Tab 1「新开账号」表单（照证据件 replica/user-adduser.html 与业务方 2026-11-06 四图：
         无框两列（列间 30px）；每行自带边框——输入/radio 行 = 连体胶囊、游戏/职位行只有
         label 有框；行间 15px、成对行两对之间 10px）══ -->
    <template v-if="embedded || activeTab === 'form'">
      <div class="uf-form">
        <div class="uf-sheet" data-v2="user-sheet">
          <!-- 左列（证据件左栏字段序，自上而下） -->
          <div class="uf-col uf-col--left" data-v2="col-left">
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>自定义登录账号</span>
              <el-input v-model="username" :maxlength="16" data-v2-field="username" />
              <span v-if="fieldError?.anchor === 'username'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>账号名称</span>
              <el-input v-model="displayName" :maxlength="24" data-v2-field="display_name" />
              <span v-if="fieldError?.anchor === 'displayName'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">身份证号</span>
              <el-input v-model="idCardNo" data-v2-field="id_card_no" />
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>业务类型</span>
              <span class="uf-fvalue">
                <el-radio-group v-model="businessType" data-v2-field="business_type">
                  <el-radio :value="1">专做代练</el-radio>
                  <el-radio :value="2">专做陪玩</el-radio>
                  <el-radio :value="0">代练+陪玩</el-radio>
                </el-radio-group>
              </span>
              <span v-if="fieldError?.anchor === 'businessType'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">成员类型</span>
              <span class="uf-fvalue">
                <!-- 单选两档互斥（业务方 2026-11-06 四图）：radio 只驱动 is_studio；member_type 原值回发。 -->
                <el-radio-group v-model="isStudio" data-v2-field="is_studio">
                  <el-radio :value="false">普通成员</el-radio>
                  <el-radio :value="true">工作室</el-radio>
                </el-radio-group>
              </span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">销售/代练提成模式</span>
              <span class="uf-fvalue">
                <el-radio-group v-model="commissionMode" data-v2-field="commission_mode">
                  <el-radio :value="10">固定金额</el-radio>
                  <el-radio :value="20">订单价百分比</el-radio>
                  <el-radio :value="30">利润的百分比</el-radio>
                </el-radio-group>
              </span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>{{ commissionValueLabel }}</span>
              <span class="uf-fvalue uf-fvalue--input">
                <el-input v-model="commissionValue" :maxlength="6" data-v2-field="commission_value" />
                <span class="uf-hint">{{ commissionValueHint }}</span>
              </span>
              <span v-if="fieldError?.anchor === 'commissionValue'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--pairs">
              <div class="uf-pair">
                <span class="uf-flabel">结算支付宝账号</span>
                <el-input v-model="alipayAccount" :maxlength="32" data-v2-field="alipay_account" />
              </div>
              <div class="uf-pair">
                <span class="uf-flabel">结算支付宝姓名</span>
                <el-input v-model="alipayName" :maxlength="50" data-v2-field="alipay_name" />
              </div>
            </div>
            <div class="uf-fitem uf-fitem--pairs">
              <div class="uf-pair">
                <span class="uf-flabel">结算银行名称</span>
                <el-input v-model="bankName" :maxlength="32" data-v2-field="bank_name" />
              </div>
              <div class="uf-pair">
                <span class="uf-flabel">结算银行卡号</span>
                <el-input v-model="bankCardNo" :maxlength="100" data-v2-field="bank_card_no" />
              </div>
            </div>
            <div class="uf-fitem uf-fitem--pairs">
              <div class="uf-pair">
                <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>内部抢代练单上限</span>
                <el-input v-model="grabLimitDl" :maxlength="4" data-v2-field="grab_limit_dl" />
                <span v-if="fieldError?.anchor === 'grabLimitDl'" class="uf-ferr">{{ fieldError.msg }}</span>
              </div>
              <div class="uf-pair">
                <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>内部抢陪玩单上限</span>
                <el-input v-model="grabLimitPw" :maxlength="4" data-v2-field="grab_limit_pw" />
                <span v-if="fieldError?.anchor === 'grabLimitPw'" class="uf-ferr">{{ fieldError.msg }}</span>
              </div>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>内部抢单价范围</span>
              <el-input v-model="grabPriceMin" :maxlength="5" placeholder="￥" data-v2-field="grab_price_min" />
              <span class="uf-range-dash" aria-hidden="true">-</span>
              <el-input v-model="grabPriceMax" :maxlength="5" placeholder="￥" data-v2-field="grab_price_max" />
              <span v-if="fieldError?.anchor === 'grabPrice'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">打手直播地址</span>
              <el-input v-model="liveUrl" :maxlength="500" data-v2-field="live_url" />
            </div>
          </div>

          <!-- 右列（证据件右栏字段序，自上而下） -->
          <div class="uf-col uf-col--right" data-v2="col-right">
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">QQ/微信</span>
              <el-input v-model="qq" :maxlength="50" data-v2-field="qq" />
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">联系电话</span>
              <el-input v-model="mobile" :maxlength="11" data-v2-field="mobile" />
              <span v-if="fieldError?.anchor === 'mobile'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>入职日期</span>
              <span class="uf-date" data-v2-field="hired_on">
                <el-date-picker
                  :model-value="hiredOn"
                  type="date"
                  value-format="YYYY-MM-DD"
                  placeholder="请选择入职日期"
                  :teleported="false"
                  @update:model-value="setHiredOn"
                />
              </span>
              <span v-if="fieldError?.anchor === 'hiredOn'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">标记</span>
              <el-input v-model="tag" :maxlength="100" data-v2-field="tag" />
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel">备注说明</span>
              <el-input v-model="remark" :maxlength="50" data-v2-field="remark" />
            </div>
            <div class="uf-fitem uf-fitem--boxed">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>已交保证金</span>
              <el-input v-model="depositAmount" :maxlength="6" data-v2-field="deposit_amount" />
              <span v-if="fieldError?.anchor === 'depositAmount'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--bare">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>勾选游戏权限</span>
              <span class="uf-fvalue">
                <el-checkbox-group v-model="games" class="uf-chips" data-v2-field="games">
                  <el-checkbox v-for="g in gameOptions" :key="g.id" :value="g.id">{{ g.name }}</el-checkbox>
                </el-checkbox-group>
              </span>
              <span v-if="fieldError?.anchor === 'games'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
            <div class="uf-fitem uf-fitem--bare uf-fitem--role">
              <span class="uf-flabel uf-flabel--req"><span class="uf-req">*</span>他的角色职位</span>
              <span class="uf-fvalue">
                <el-radio-group v-model="role" class="uf-rolegrid" data-v2-field="role">
                  <el-radio v-for="r in roleOptions" :key="r" :value="r">{{ r }}</el-radio>
                </el-radio-group>
                <span v-if="roleTipText !== ''" class="uf-hint">{{ roleTipText }}</span>
              </span>
              <span v-if="fieldError?.anchor === 'role'" class="uf-ferr">{{ fieldError.msg }}</span>
            </div>
          </div>
        </div>

        <!-- 提交区：右对齐大钮 -->
        <div class="uf-actions">
          <button
            type="button"
            class="cv2-btn cv2-btn--submit uf-save-btn cv2-btn--commit cv2-btn--fill"
            data-v2="save-user"
            :disabled="saving"
            @click="onSave"
          >
            保存资料
          </button>
        </div>
      </div>
    </template>

    <!-- ══ Tab 2「自助注册」三卡 ══ -->
    <template v-else>
      <section class="cv2-panel" aria-label="注册配置" data-v2="panel-regconfig">
        <h2 class="cv2-panel-title">注册配置</h2>
        <div class="sr-row">
          <span class="cv2-field-label">注册游戏</span>
          <el-checkbox-group v-model="regGameIds" class="sr-chips" data-v2-field="reg-games">
            <el-checkbox v-for="g in gameOptions" :key="g.id" :value="g.id">{{ g.name }}</el-checkbox>
          </el-checkbox-group>
        </div>
        <div class="sr-row">
          <span class="cv2-field-label">默认开关</span>
          <label class="cv2-check">
            <input v-model="forbidDefault" type="checkbox" data-v2="forbid-switch" />
            <span>新注册账号默认开启『禁自助接单』</span>
          </label>
        </div>
        <div class="sr-row">
          <span class="cv2-field-label">编号前缀</span>
          <el-input v-model="numberPrefix" :maxlength="5" placeholder="如 sq" class="sr-w120" data-v2-field="number-prefix" />
          <span class="cv2-field-label">起始编号</span>
          <el-input v-model="numberStart" type="number" :min="1" placeholder="如 20001" class="sr-w120" data-v2-field="number-start" />
          <span class="cv2-field-hint">两栏都填了才启用编号（账号名称 = 前缀+编号，如 sq20001）；留空则用注册人填的支付宝姓名</span>
        </div>
        <div class="sr-row">
          <button type="button" class="cv2-btn cv2-btn--submit cv2-btn--commit cv2-btn--fill" data-v2="save-regconfig" :disabled="regSaving" @click="onSaveConfig">
            保存配置
          </button>
          <span class="cv2-field-hint">生成链接时按保存那一刻的配置固化（游戏池与开关都是快照）</span>
        </div>
      </section>

      <section class="cv2-panel" aria-label="生成注册链接" data-v2="panel-reginvite">
        <h2 class="cv2-panel-title">生成注册链接</h2>
        <p class="sr-note">链接一次性 · 7 天有效 · 按保存时的配置固化；注册一人后自动失效</p>
        <div class="sr-row">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="generate-invite" :disabled="regGenerating" @click="onGenerate">
            生成并复制链接
          </button>
        </div>
        <p v-if="regLink !== null" class="sr-linkline" data-v2="reg-link">
          注册链接（{{ formatTime(regLink.expiresAt) }} 前有效）：<span class="sr-linkurl">{{ regLink.url }}</span>
        </p>
      </section>

      <section class="cv2-panel" aria-label="链接与注册记录" data-v2="panel-reglist">
        <div class="cv2-panel-head">
          <h2 class="cv2-panel-title">链接与注册记录</h2>
          <div class="sr-head-actions">
            <button type="button" class="cv2-btn cv2-btn--find" data-v2="refresh-invites" @click="reloadInvites">刷新</button>
            <span class="cv2-toolbar-count" data-v2="invite-count">共 {{ inviteRows.length }} 条 · 最新 200 条</span>
          </div>
        </div>
        <div class="cv2-table-panel">
          <table class="cv2-table" data-v2="invite-table">
            <colgroup>
              <col style="width: 12%" />
              <col style="width: 11%" />
              <col style="width: 9%" />
              <col style="width: 12%" />
              <col style="width: 11%" />
              <col style="width: 12%" />
              <col style="width: 24%" />
              <col style="width: 9%" />
            </colgroup>
            <thead>
              <tr>
                <th>生成时间</th>
                <th>生成人（开户人）</th>
                <th>状态</th>
                <th>有效期至</th>
                <th>注册进来的人</th>
                <th>注册时间</th>
                <th>勾选游戏</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody v-if="inviteRows.length > 0">
              <tr v-for="row in inviteRows" :key="row.id" :data-v2-invite="row.id">
                <td class="sr-mono">{{ formatTime(row.created_at) }}</td>
                <td>{{ row.created_by_name }}</td>
                <td>
                  <span class="cv2-st" :class="STATUS_CLASS[row.status]" :data-v2-status="row.status">
                    <i class="cv2-dot" aria-hidden="true"></i>{{ STATUS_LABEL[row.status] }}
                  </span>
                </td>
                <td class="sr-mono">{{ formatTime(row.expires_at) }}</td>
                <td>{{ row.consumed_by_name === null ? '—' : row.consumed_by_name }}</td>
                <td class="sr-mono">{{ row.consumed_at === null ? '—' : formatTime(row.consumed_at) }}</td>
                <td>{{ row.chosen_game_names.length > 0 ? row.chosen_game_names.join('、') : '—' }}</td>
                <td>
                  <button
                    v-if="row.status === 'unused'"
                    type="button"
                    class="cv2-actlink cv2-actlink--danger"
                    :data-v2-revoke="row.id"
                    :disabled="revoking"
                    @click="askRevoke(row.id)"
                  >作废</button>
                  <template v-else>—</template>
                </td>
              </tr>
            </tbody>
            <tbody v-else>
              <tr>
                <td colspan="8" class="sr-empty-cell" data-v2="invites-empty">{{ SELFREG_MESSAGES.empty }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <!-- 新增成功的凭据弹窗（sm 档；正文与剪贴板文本都带登录网址，所见即所复） -->
    <el-dialog
      v-model="credOpen"
      title="账号已创建"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-credential"
      :close-on-click-modal="false"
    >
      <p class="cv2-dlg-text" data-v2="credential-text">{{ credentialBody }}</p>
      <p class="uf-dlg-origin" data-v2="credential-origin">登录网址：{{ credOrigin }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button
            type="button"
            class="cv2-btn cv2-btn--primary cv2-btn--commit"
            data-v2="copy-credential"
            :disabled="copying"
            @click="onCopyCredential"
          >
            确定并复制
          </button>
        </div>
      </template>
    </el-dialog>

    <!-- 同一个弹窗内推进确认步骤，避免首步收起时误清第二步。 -->
    <el-dialog
      :model-value="revokeStep !== 0"
      title="作废链接"
      class="cv2-dialog cv2-dialog--sm"
      data-v2="dlg-revoke"
      :close-on-click-modal="false"
      @update:model-value="(v: boolean) => { if (!v) cancelRevoke() }"
    >
      <p v-if="revokeStep === 1" class="cv2-dlg-text">{{ SELFREG_MESSAGES.revokeConfirmFirst }}</p>
      <p v-else class="cv2-dlg-text">{{ SELFREG_MESSAGES.revokeConfirmSecond }}</p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="cancelRevoke">取消</button>
          <button v-if="revokeStep === 1" type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="revoke-first" @click="revokeStep = 2">确定</button>
          <button v-else type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="revoke-second" :disabled="revoking" @click="onRevokeConfirm">确定</button>
        </div>
      </template>
    </el-dialog>

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
  </div>
</template>

<style scoped>
/* ═══ Tab1 表单（炭火皮；照证据件 replica/user-adduser.html + 业务方 2026-11-06 四图：
 *     无框两列、每行自带胶囊、行间 15px。颜色只引用 --tt2-* / EP token，零裸色值）══ */

/* 主表单区 38px 档（录单页同款：EP 尺寸走 CSS 变量，容器上改变量即可）。 */
.uf-form {
  --el-component-size: 38px;
  --el-checkbox-height: 38px;
}

.uf-form :deep(.el-select__wrapper) {
  min-height: 38px;
}

.uf-form :deep(.el-radio) {
  height: 38px;
}

/* 无框两列容器（证据件列容器无任何边框规则）：列间内容白隙 30px、行间 15px。 */
.uf-sheet {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 30px;
  margin-top: 18px;
}

.uf-col {
  display: flex;
  flex-direction: column;
  gap: 15px;
  min-width: 0;
}

/* 行基座（自身无框）；三种行形由修饰类区分（见下）。 */
.uf-fitem {
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  gap: 0;
  min-width: 0;
  max-width: 100%;
  min-height: var(--el-component-size);
}

/* ①② 输入行 / radio 行：label 格 + 值格拼成一个连体胶囊（圆角 2px；label|值的竖合缝
 * 由值侧控件自己的边线或 .uf-fvalue 左缘线提供）。 */
.uf-fitem--boxed {
  border: 1px solid var(--tt2-filter-line);
  border-radius: 2px;
  overflow: hidden;
}

/* 成对行：同排两对之间 10px（照证据件 .layui-inline 间距），替换旧 1px 竖线。 */
.uf-fitem--pairs {
  gap: 10px;
}

/* ③ 游戏/职位行：只有 label 胶囊有框，值区无框（⛔ 不给芯片/栅格套边框盒）。 */
.uf-fitem--bare > .uf-flabel {
  border: 1px solid var(--tt2-filter-line);
  border-radius: 2px;
}

.uf-fitem--bare > .uf-fvalue {
  border-left: none;
}

/* 成对字段（一行两对）：每对自成独立胶囊；basis 给 160px，
 * 极窄屏放不下两对时第二对自然换行（错误红字落在本对下方）。 */
.uf-pair {
  flex: 1 1 160px;
  min-width: 0;
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  min-height: var(--el-component-size);
  border: 1px solid var(--tt2-filter-line);
  border-radius: 2px;
  overflow: hidden;
}

/* 标签格：同列等宽、文字居中（证据件 pane 模式实测 center）。宽度按「最长适配」推算：
 * 左列含必填星号 136px / 右列 112px——⛔ 不照抄证据件两列同宽 110px（左列三行截断是对标缺陷）。 */
.uf-flabel {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 10px;
  background: var(--tt2-filter-label-bg);
  font-size: 13px;
  color: var(--tt2-dim);
  white-space: nowrap;
}

.uf-fitem--role > .uf-flabel {
  background: var(--tt2-label-bg);
}

.uf-col--left .uf-flabel {
  width: 136px;
}

.uf-col--right .uf-flabel {
  width: 112px;
}

/* 值格（radio 行）：basis:0 吃满余宽 ⇒ 输入框右缘垂直对齐；左缘自画 1px = 标签|值格的
 * 合缝线（这类控件没有自带边框）。 */
.uf-fvalue {
  flex: 1 1 0;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  padding: 0 12px;
  min-height: var(--el-component-size);
  border-left: 1px solid var(--tt2-filter-line);
}

/* 输入行值格（提成金额）：装自带边框的 el-input——去左缘线与内距（合缝 = 控件自己的
 * 边线），常驻 hint 挂格内下方（值格跟高）。 */
.uf-fvalue--input {
  padding: 0;
  border-left: none;
}

/* 自带边框的控件（el-input / 日期）贴标签同排：basis 收 0 + grow 吃余宽。
 * basis:auto 会被 EP .el-input 的 width:100% 撑成整格宽 ⇒ 控件换行叠到标签下方。 */
.uf-fitem > :deep(.el-input),
.uf-fitem > .uf-date,
.uf-pair > :deep(.el-input),
.uf-fvalue--input > :deep(.el-input) {
  flex: 1 1 0;
  min-width: 0;
}

.uf-fitem :deep(.el-input__wrapper) {
  border-radius: 0;
}

/* 日期格（el-date-picker 吞 attrs，锚点挂包裹层）；宽度吃满连体格余宽。 */
.uf-date {
  display: inline-flex;
  align-items: center;
}

.uf-date :deep(.el-date-editor.el-input) {
  width: 100%;
}

/* 游戏权限 = layui tag 芯片照图4（业务方 2026-11-06）：文字块左 + 勾选方块右 = 一枚 30px 芯片；
 * 间距横 15 / 竖 9。未选中灰底白字（对比度 ≈1.4:1 是照对标的已知豁免，照 D-0371 先例登记）；
 * 选中 = 文字块青绿实底白字、方块仍白底 + 对勾青绿（覆写 EP 的「主色底白勾」默认）。
 * ⚠️ 页面 .uf-form 的 38px checkbox 档在这里局部压回 30px。 */
.uf-chips {
  --el-checkbox-height: 30px;
  display: flex;
  flex-wrap: wrap;
  gap: 9px 15px;
}

.uf-chips :deep(.el-checkbox) {
  display: inline-flex;
  flex-direction: row-reverse; /* 文字块在左、勾选方块在右（照证据件 tag 皮） */
  align-items: stretch;
  height: 30px;
  margin-right: 0;
}

.uf-chips :deep(.el-checkbox__label) {
  display: inline-flex;
  align-items: center;
  padding: 0 11px;
  border-radius: 2px 0 0 2px;
  background: var(--tt2-btn-soft-line);
  color: var(--tt2-panel);
  font-size: 14px;
}

.uf-chips :deep(.el-checkbox:hover .el-checkbox__label) {
  background: var(--tt2-btn-soft-hover-line);
}

.uf-chips :deep(.el-checkbox__input.is-checked + .el-checkbox__label) {
  background: var(--tt2-primary);
  color: var(--tt2-panel);
}

/* 勾选方块 30×30 白底（选中也不变主色底）；对勾换青绿并按 30px 格重新居中。 */
.uf-chips :deep(.el-checkbox__inner) {
  width: 30px;
  height: 30px;
  border-color: var(--tt2-btn-soft-line);
  border-radius: 0 2px 2px 0;
  background-color: var(--tt2-panel);
}

.uf-chips :deep(.el-checkbox__inner:hover),
.uf-chips :deep(.el-checkbox__input.is-focus .el-checkbox__inner) {
  border-color: var(--tt2-btn-soft-hover-line);
}

.uf-chips :deep(.el-checkbox__input.is-checked .el-checkbox__inner) {
  background-color: var(--tt2-panel);
  border-color: var(--tt2-btn-soft-line);
}

/* 对勾几何：EP 的 ::after 自锚 top/left 50% + translate(-45%,-60%) rotate(45deg)，偏移按
 * 自身尺寸取百分比 ⇒ 方块加到 30px 仍居中；⛔ 只放大勾身（描边 1px 算入 = 8×16 ≈ 方块高
 * 53%，对齐 EP 原 8/14 比例）并换青绿——钉死 top/left 会与 EP translate 叠加把勾推出方块。 */
.uf-chips :deep(.el-checkbox__input.is-checked .el-checkbox__inner::after) {
  width: 7px;
  height: 15px;
  border-color: var(--tt2-primary);
}

/* 角色职位栅格：写死 5 列（证据件实为定宽格自然换行、一行 5 个只是窗口宽度产物；
 * v2 有意钉 5 列，STATUS 登记）。 */
.uf-rolegrid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px 8px;
  width: 100%;
}

.uf-rolegrid :deep(.el-radio) {
  height: 28px;
  margin-right: 0;
  font-size: 14px;
}

/* 抢单价范围两格之间的连字符（对标证据件同款分隔）。 */
.uf-range-dash {
  flex: none;
  display: inline-flex;
  align-items: center;
  padding: 0 4px;
  color: var(--tt2-dim);
}

/* 错误行 / 常驻 hint：换行落在连体格下缘。 */
.uf-ferr {
  flex-basis: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tt2-btn-danger);
}

.uf-hint {
  flex-basis: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--tt2-dim);
}

/* 必填标记（label 前 *）。 */
.uf-req {
  margin-right: 2px;
  color: var(--tt2-btn-danger);
}

/* 必填字段的 label 文字整条红（业务方 2026-11-06 四图；星+文字同色）。 */
.uf-flabel--req {
  color: var(--tt2-btn-danger);
}

/* 提交区：右对齐 + 本页大钮档（表单块外）。 */
.uf-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

.uf-save-btn {
  min-width: 200px;
  min-height: 44px;
  font-size: 14px;
}

/* 窄屏：左右两列退化为单列堆叠（行盒与 gap 结构不变）。 */
@media (max-width: 900px) {
  .uf-sheet {
    grid-template-columns: 1fr;
  }
}

/* ═══ Tab 2 自助注册 ═══ */
.sr-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-top: 12px;
}

.sr-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
}

.sr-note {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

.sr-w120 {
  width: 120px;
}

.sr-linkline {
  margin: 12px 0 0;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;
}

.sr-linkurl {
  color: var(--tt2-link-blue);
  font-family: var(--tt2-mono);
}

.sr-head-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 记录表时间列：等宽 + tabular-nums（一切数据走 mono 的炭火口径）。 */
.sr-mono {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

.sr-empty-cell {
  padding: 28px 12px;
  color: var(--tt2-dim);
  text-align: center;
}

/* 凭据弹窗里的网址行（小字 dim，正文下一行）。 */
.uf-dlg-origin {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--tt2-dim);
}

@media (max-width: 720px) {
  .sr-w120 {
    width: 100%;
  }

  .uf-save-btn {
    width: 100%;
  }
}
</style>
