<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「设置第三方交易平台接」v2（`/v2/platform-integration`；路由 name `v2-platform-integration`）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/system-tripartiteplatformsetting.ts`（真 API 页）：
 * 13 列对接账号表 + 新增/修改弹层（字段随平台变形）+ 支付密码弹层 + 「对所有平台生效」9 组单选。
 * v2 等价重建，文案逐字照证据件 `public/replica/system-tripartiteplatformsetting.html`（行号见常量表）。
 * 有意差异（设计稿 `docs/plans/2026-11-05-v2-game-and-platform-pages.md` §4.1）：
 * ① 5 颗行内开关 EP `el-switch`：PATCH 单字段、失败拨回 + 后端原文、**成功也整表重取**
 *   （默认账号开关会降级同平台其他行，照旧页 reloadList 行为）；② 9 组设置的保存钮在
 *   「读到后端真值」前禁用 + 常驻提示（旧页是点了才拦）；③ 字段随平台变形照 island
 *   `platformFieldConfig`（L171-212）逐字实现；④ 凭据红线：AppSecret/支付密码框永远留空、
 *   只在用户填了值时随载荷发（⛔ 空串不发——pay_password 传空串会被后端拒）；
 * ⑤ 9 组逐组标「已生效/仅保存」（只有 SK145 / SK158 已生效）。
 * ⛔ 不 import island 页模块；权限门 `session.canNode('system.platform_integration_settings')`。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  createPlatformIntegration,
  deletePlatformIntegration,
  fetchPlatformIntegrations,
  fetchPlatformPushSettings,
  savePlatformPushSettings,
  updatePlatformIntegration,
  type PlatformIntegrationPayload,
  type PlatformIntegrationRow,
  type PlatformPushSettings,
} from '@/api/platformIntegrations'
import { useSessionStore } from '@/stores/session'

/* ── 逐字文案常量（出处 = 证据件 HTML / island 页，行号逐条标注）──────────────── */

/** 页头引文块（证据件 system-tripartiteplatformsetting.html L12-13）。 */
const PAGE_QUOTE_LINES = [
  '设置了以下各项，您就可以很方便的在系统里一键发单到各大交易平台，系统会自动同步代练平台的交易状态和交易信息。若您不会设置，我们的技术人员很乐意协助您设置。',
  '允许相同平台添加多个发单账号',
] as const
/** 工具条（证据件 L52-53；「刷新」成功 toast 是 v2 口径，见设计稿 §4.1）。 */
const TOOLBAR_REFRESH_TEXT = '刷新列表'
const TOOLBAR_ADD_TEXT = '新增平台对接'
/** 9 组设置面板标题（证据件 L151「这里的设置项会针对所有平台生效」）与生效范围声明（island L81-82）。 */
const SETTINGS_PANEL_TITLE = '这里的设置项会针对所有平台生效'
const SETTINGS_NOTICE_TEXT =
  '本版有两条已生效：「撞单自动 0 撤」与「平台接手后自动发群二维码」；其余开关仍只保存、不影响业务'
const SETTINGS_EFFECT_LEGEND =
  '绿色「已生效」= 发单 / 同步链路会读它；橙色「仅保存」= 只写进系统，业务行为不变。逐组见下方标注。'
const SETTINGS_EFFECT_ON_TEXT = '已生效'
const SETTINGS_EFFECT_OFF_TEXT = '仅保存'
const SETTINGS_NOT_LOADED_TEXT = '设置还没读取完成，请稍后再试（或刷新页面）'
/** 「暂未开通」徽标（island platformNameCell L300-301）。 */
const NOT_AVAILABLE_BADGE_TEXT = '暂未开通'
const NOT_AVAILABLE_BADGE_TIP = '该平台本版暂未开通：参数可以保存，但不会用于发单'
/** 支付密码格两态（island payPasswordCell L316-323，逐字）。 */
const PAY_PWD_SET_TIP = '已设置平台支付密码，点击可以更改'
const PAY_PWD_EMPTY_TEXT = '点击设置'
const PAY_PWD_EMPTY_TIP = '还没有设置平台支付密码，点击快速设置'
/** 新增/修改弹层（证据件 L64-66 引文、L89-137 各格；标题 = island L717）。 */
const FORM_QUOTE_TEXT = '请联系我们售后人员帮您对接'
const FORM_TITLE_ADD = '新增一个代练平台对接'
const FORM_TITLE_EDIT = '编辑对接'
const ACCOUNT_PLACEHOLDER = '请填写平台的账号'
const LOGIN_PWD_PLACEHOLDER = '请填写平台的登录密码'
const LOGIN_PWD_NOT_OPEN_TEXT = '「平台登录密码」尚未开放：本版不会保存这一格的内容。'
const PAY_PWD_PLACEHOLDER_ADD = '请输入交易平台的支付密码'
const PAY_PWD_PLACEHOLDER_EDIT = '若没有改过，请不要动此处'
const APP_ID_PLACEHOLDER = '请填写AppID'
const APP_SECRET_PLACEHOLDER = '请填写密钥'
const FORM_SAVE_TEXT = '保存设置'
/** 弹层校验与保存提示（island submitForm L736-804，逐字）。 */
const VALIDATE_PLATFORM_TEXT = '请选择一个平台'
const VALIDATE_APP_ID_TEXT = 'AppId不能为空'
const SAVE_OK_TEXT = '已保存'
const SAVE_OK_NOT_AVAILABLE_SUFFIX = '；该平台暂未开通，本版只保存参数'
const SAVE_OK_LOGIN_PWD_SUFFIX = '；「平台登录密码」尚未开放，未随本次保存提交'
const SAVE_FAIL_PREFIX = '保存失败：'
/** 支付密码弹层（island L849-869，逐字）。 */
const PAY_PWD_DIALOG_TITLE = '请填充平台的支付密码'
const PAY_PWD_EMPTY_TEXT_VALIDATE = '支付密码不能为空'
const PAY_PWD_SET_OK_TEXT = '支付密码已设置'
/** 删除确认（island L816-817，逐字）。 */
const DELETE_CONFIRM_TEXT =
  '是否确认删除这个对接账号？提示：删除此对接账号不会影响之前用这个账号发过单的交易操作'
const DELETE_CONFIRM_OK_TEXT = '确认删除'
const DELETE_CONFIRM_CANCEL_TEXT = '点错了'
const DELETE_OK_TEXT = '已删除'
/** 表格三态（同 v2 页通用口径；失败带「重试」）。 */
const LOADING_TEXT = '正在加载…'
const EMPTY_TEXT = '暂无数据'
const LOAD_FAIL_PREFIX = '加载失败：'
const RETRY_TEXT = '重试'
const REFRESH_OK_TEXT = '已刷新'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 平台 radio 7 颗（island PLATFORM_OPTIONS L99-107，顺序逐字）────────────── */

const PLATFORM_OPTIONS: ReadonlyArray<{ value: number; title: string }> = [
  { value: 10, title: '代练通' },
  { value: 30, title: '代练妈妈' },
  { value: 80, title: '代练丸子' },
  { value: 90, title: '趣代练' },
  { value: 93, title: '天玩代练' },
  { value: 50, title: '咕噜代练' },
  { value: 60, title: '易代练' },
]

/** 对标在弹层里不要求 AppId 的平台（island L117：50 / 93）。 */
const APPID_OPTIONAL_PLATFORMS = new Set([50, 93])

/** 弹层里「选平台后改 label / 显隐」的结果（island PlatformFieldConfig L152-160 同形）。 */
type PlatformFields = {
  accountLabel: string
  appIdLabel: string
  appSecretLabel: string
  showLoginPassword: boolean
  showAppId: boolean
  showPayPassword: boolean
  showAppSecret: boolean
}

/** 照 island platformFieldConfig（L171-212）逐字实现：80 与 30/90 同形（对标 L286 那支是死代码，不复刻）。 */
function platformFieldsOf(platform: number): PlatformFields {
  const base: PlatformFields = {
    accountLabel: '平台用户ID',
    appIdLabel: 'AppId',
    appSecretLabel: 'AppSecret',
    showLoginPassword: false,
    showAppId: true,
    showPayPassword: true,
    showAppSecret: platform !== 0,
  }
  if (platform === 0) return base
  if (platform === 10) return { ...base, accountLabel: '代练通用户ID' }
  if (platform === 30 || platform === 80 || platform === 90) {
    return { ...base, accountLabel: '平台用户ID', appIdLabel: '商户标识', appSecretLabel: '商户密钥' }
  }
  if (platform === 50) {
    return { ...base, accountLabel: '平台账号', appSecretLabel: '对接秘钥', showLoginPassword: true, showAppId: false }
  }
  if (platform === 93) return { ...base, accountLabel: 'User_Code', appSecretLabel: '对接密钥' }
  return base
}

/* ── 9 组全局设置（字段映射与 effective 唯一出处 = island SETTINGS_GROUPS L462-472；⭐ Y/N 布尔 4 组、0/1 整数 5 组——api/platformIntegrations.ts L70 的注释写反了，以 island 为准）────────────────────────────────────────────── */

type SettingsField =
  | 'quick_push_enabled'
  | 'legacy_simple_mode'
  | 'check_game_region'
  | 'forbid_push_when_double_gold_zero'
  | 'duplicate_push_check'
  | 'select_jidanpu_center_by_default'
  | 'auto_zero_refund_on_conflict'
  | 'block_push_completed_orders'
  | 'auto_send_group_qr_after_accept'

type SettingsGroup = {
  sk: string
  field: SettingsField
  kind: 'boolean' | 'zeroOne'
  effective: boolean
  desc: string
  options: ReadonlyArray<{ value: string; title: string }>
}

/** 组描述与选项文案逐字照证据件 L156-238（1 → 6 的顺序也照它）。 */
const SETTINGS_GROUPS: readonly SettingsGroup[] = [
  {
    sk: 'SK132',
    field: 'quick_push_enabled',
    kind: 'boolean',
    effective: false,
    desc: '1、录单页面是否需要支持快速发单到交易平台(默认不支持，若需要支持，需要在此页面对接好平台，再到系统维护/平台快速发单设置进行配置快速发单参数)',
    options: [
      { value: 'N', title: '关' },
      { value: 'Y', title: '开' },
    ],
  },
  {
    sk: 'SK156',
    field: 'legacy_simple_mode',
    kind: 'zeroOne',
    effective: false,
    desc: '1.1、发单到交易平台界面是否需要启用传统简易模式？',
    options: [
      { value: '0', title: '否' },
      { value: '1', title: '是' },
    ],
  },
  {
    sk: 'SK157',
    field: 'check_game_region',
    kind: 'zeroOne',
    effective: false,
    desc: '1.2、发单到交易平台时是否需要检测游戏大区准确性？',
    options: [
      { value: '0', title: '否' },
      { value: '1', title: '是' },
    ],
  },
  {
    sk: 'SK107',
    field: 'forbid_push_when_double_gold_zero',
    kind: 'boolean',
    effective: false,
    desc: '2、发单时如果检测到双金任意一个为0是否禁止发出去(默认不限制)',
    options: [
      { value: 'Y', title: '禁止发单' },
      { value: 'N', title: '不限制' },
    ],
  },
  {
    sk: 'SK159',
    field: 'duplicate_push_check',
    kind: 'zeroOne',
    effective: false,
    desc: '2.1、发相同平台的订单是否需要检测重复发单(即同一平台相同账号已有单子在平台上进行中，继续发会出现冲突)',
    options: [
      { value: '0', title: '检测' },
      { value: '1', title: '不做检测' },
    ],
  },
  {
    sk: 'SK106',
    field: 'select_jidanpu_center_by_default',
    kind: 'boolean',
    effective: false,
    desc: '3、发单时是否默认勾选发鸡蛋铺交易中心(如果本系统绑定了鸡蛋铺官方账号，默认将勾选)',
    options: [
      { value: 'Y', title: '勾选' },
      { value: 'N', title: '不勾选' },
    ],
  },
  {
    sk: 'SK145',
    field: 'auto_zero_refund_on_conflict',
    kind: 'boolean',
    effective: true,
    desc: '4、若发生撞单(即多个平台同时接手)时系统会保留最早的接手外，其它所有平台全申请0撤',
    options: [
      { value: 'Y', title: '0撤' },
      { value: 'N', title: '不自动处理撤销' },
    ],
  },
  {
    sk: 'SK155',
    field: 'block_push_completed_orders',
    kind: 'zeroOne',
    effective: false,
    desc: '5、已完成的订单是否不允许再次发到交易平台(可适当防止客服自盗作假)',
    options: [
      { value: '0', title: '不限制' },
      { value: '1', title: '限制' },
    ],
  },
  {
    sk: 'SK158',
    field: 'auto_send_group_qr_after_accept',
    kind: 'zeroOne',
    effective: true,
    desc: '6、平台接手后是否自动将订单群二维码发送到平台（需要提前上传好）',
    options: [
      { value: '0', title: '不发送' },
      { value: '1', title: '自动发' },
    ],
  },
]

function emptySettingsValues(): Record<SettingsField, string> {
  // 空串 = 没选中（选项值域是 'Y'/'N'/'0'/'1'，空串永不在其中）；用 '' 而非 null 是为了
  // 过 el-radio-group v-model 的 prop 类型（null 不在其值域），运行时表现相同。
  return {
    quick_push_enabled: '',
    legacy_simple_mode: '',
    check_game_region: '',
    forbid_push_when_double_gold_zero: '',
    duplicate_push_check: '',
    select_jidanpu_center_by_default: '',
    auto_zero_refund_on_conflict: '',
    block_push_completed_orders: '',
    auto_send_group_qr_after_accept: '',
  }
}

/* ── 权限门（fail-closed：没节点不画、不发请求）────────────────────────────── */

const session = useSessionStore()
const canView = computed(() => session.canNode(['system.platform_integration_settings']))

/* ── toast（同 V2OrderRecycle：info 1500ms / error 6000ms）───────────────────── */

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

/* ── 列表（数组；无服务端分页 ⇒ 不渲染分页器）────────────────────────────────── */

const rows = ref<PlatformIntegrationRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

async function reloadList(): Promise<void> {
  loading.value = true
  try {
    rows.value = await fetchPlatformIntegrations()
    loadError.value = null
  } catch (e) {
    rows.value = []
    loadError.value = errText(e)
  } finally {
    loading.value = false
  }
}

async function onRefresh(): Promise<void> {
  await reloadList()
  if (loadError.value !== null) showToast(loadError.value, 'error')
  else showToast(REFRESH_OK_TEXT)
}

/* ── 行内开关：PATCH 单字段；失败拨回 + 后端原文；成功也整表重取（默认开关降级同平台其他行）── */

type SwitchField = 'is_enabled' | 'is_default' | 'sync_accept' | 'sms_notify' | 'include_push'

async function onRowSwitch(row: PlatformIntegrationRow, field: SwitchField, checked: boolean): Promise<void> {
  const payload: Partial<PlatformIntegrationPayload> = {}
  payload[field] = checked
  try {
    await updatePlatformIntegration(row.id, payload)
  } catch (e) {
    showToast(errText(e), 'error')
  }
  await reloadList()
  if (loadError.value !== null) showToast(loadError.value, 'error')
}

/* ── 新增 / 修改弹层 ─────────────────────────────────────────────────────────── */

const editDlg = ref(false)
const editingId = ref<number | null>(null)
const submitting = ref(false)

const form = reactive({
  platform: 0,
  account: '',
  loginPassword: '',
  payPassword: '',
  appId: '',
  appSecret: '',
  remark: '',
  contactQq: '',
  contactPhone: '',
})

const fields = computed(() => platformFieldsOf(form.platform))
const payPwdPlaceholder = computed(() =>
  editingId.value === null ? PAY_PWD_PLACEHOLDER_ADD : PAY_PWD_PLACEHOLDER_EDIT,
)
const formTitle = computed(() => (editingId.value === null ? FORM_TITLE_ADD : FORM_TITLE_EDIT))

/** 弹层密码框永远留空（凭据红线）：读模型没有这两个字段，回显不可能也不允许。 */
function openForm(row: PlatformIntegrationRow | null): void {
  editingId.value = row?.id ?? null
  form.platform = row?.platform ?? 0
  form.account = row?.account ?? ''
  form.loginPassword = ''
  form.payPassword = ''
  form.appId = row?.app_id ?? ''
  form.appSecret = ''
  form.remark = row?.remark ?? ''
  form.contactQq = row?.contact_qq ?? ''
  form.contactPhone = row?.contact_phone ?? ''
  editDlg.value = true
}

async function doSave(): Promise<void> {
  if (submitting.value) return
  const platform = form.platform
  if (!platform) {
    showToast(VALIDATE_PLATFORM_TEXT)
    return
  }
  const appId = form.appId.trim()
  if (!APPID_OPTIONAL_PLATFORMS.has(platform) && appId === '') {
    showToast(VALIDATE_APP_ID_TEXT)
    return
  }
  const payload: PlatformIntegrationPayload = {
    platform,
    account: form.account.trim(),
    app_id: appId,
    remark: form.remark,
    contact_qq: form.contactQq,
    contact_phone: form.contactPhone,
  }
  // 空串 = 不改（后端约定）⇒ 连键都不带；pay_password 空串会被后端拒。
  if (form.appSecret !== '') payload.app_secret = form.appSecret
  const payPassword = form.payPassword.trim()
  if (payPassword !== '') payload.pay_password = payPassword
  const loginPassword = form.loginPassword
  submitting.value = true
  try {
    const saved =
      editingId.value === null
        ? await createPlatformIntegration(payload)
        : await updatePlatformIntegration(editingId.value, payload)
    editDlg.value = false
    let text = SAVE_OK_TEXT
    if (!saved.is_available) text += SAVE_OK_NOT_AVAILABLE_SUFFIX
    if (loginPassword !== '' && platformFieldsOf(platform).showLoginPassword) text += SAVE_OK_LOGIN_PWD_SUFFIX
    await reloadList()
    if (loadError.value !== null) showToast(`${text}；但列表刷新失败：${loadError.value}`, 'error')
    else showToast(text)
  } catch (e) {
    showToast(`${SAVE_FAIL_PREFIX}${errText(e)}`, 'error')
  } finally {
    submitting.value = false
  }
}

/* ── 支付密码弹层（列上对勾 / 点击设置进这里；只 PATCH {pay_password}）───────── */

const payPwdDlg = ref<{ id: number } | null>(null)
const payPwdValue = ref('')
const payPwdSaving = ref(false)

function openPayPwd(row: PlatformIntegrationRow): void {
  payPwdValue.value = ''
  payPwdDlg.value = { id: row.id }
}

async function doSavePayPwd(): Promise<void> {
  const dlg = payPwdDlg.value
  if (dlg === null || payPwdSaving.value) return
  // 先 trim（照 island P2-8）：纯空格被 MD5 后就是一个「真密码」。空白 = 没填。
  const password = payPwdValue.value.trim()
  if (password === '') {
    showToast(PAY_PWD_EMPTY_TEXT_VALIDATE)
    return
  }
  payPwdSaving.value = true
  try {
    await updatePlatformIntegration(dlg.id, { pay_password: password })
    payPwdDlg.value = null
    await reloadList()
    if (loadError.value !== null) showToast(loadError.value, 'error')
    else showToast(PAY_PWD_SET_OK_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    payPwdSaving.value = false
  }
}

/* ── 删除确认 ───────────────────────────────────────────────────────────────── */

const confirmDel = ref<PlatformIntegrationRow | null>(null)
const deleting = ref(false)

async function doDelete(): Promise<void> {
  const row = confirmDel.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    await deletePlatformIntegration(row.id)
    confirmDel.value = null
    await reloadList()
    if (loadError.value !== null) showToast(loadError.value, 'error')
    else showToast(DELETE_OK_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    deleting.value = false
  }
}

/* ── 9 组全局设置：读真值前保存钮禁用；读失败全清 + 报错；成功回填后端返回值 ── */

const settingsValues = ref<Record<SettingsField, string>>(emptySettingsValues())
const settingsLoaded = ref(false)
const settingsSubmitting = ref(false)

function fillSettings(s: PlatformPushSettings): void {
  const values = emptySettingsValues()
  for (const g of SETTINGS_GROUPS) {
    if (g.kind === 'boolean') values[g.field] = s[g.field] ? 'Y' : 'N'
    else values[g.field] = String(s[g.field])
  }
  settingsValues.value = values
}

async function loadSettings(): Promise<void> {
  try {
    const s = await fetchPlatformPushSettings()
    fillSettings(s)
    settingsLoaded.value = true
  } catch (e) {
    // 读不到真值 ⇒ 全清（空串 = 没选中），保存钮保持禁用；不拿默认值冒充真值。
    settingsValues.value = emptySettingsValues()
    settingsLoaded.value = false
    showToast(errText(e), 'error')
  }
}

function settingsPayload(): Partial<PlatformPushSettings> {
  const payload: Record<string, boolean | number> = {}
  for (const g of SETTINGS_GROUPS) {
    const v = settingsValues.value[g.field]
    if (v === '') throw new Error(`第 ${g.sk} 组设置没有选中值，请重新选择`)
    if (g.kind === 'boolean') payload[g.field] = v === 'Y'
    else payload[g.field] = Number(v)
  }
  return payload as Partial<PlatformPushSettings>
}

async function onSaveSettings(): Promise<void> {
  if (!settingsLoaded.value) {
    showToast(SETTINGS_NOT_LOADED_TEXT)
    return
  }
  if (settingsSubmitting.value) return
  let payload: Partial<PlatformPushSettings>
  try {
    payload = settingsPayload()
  } catch (e) {
    showToast(errText(e))
    return
  }
  settingsSubmitting.value = true
  try {
    const saved = await savePlatformPushSettings(payload)
    fillSettings(saved)
    showToast(SAVE_OK_TEXT)
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    settingsSubmitting.value = false
  }
}

/* ── 其它出口 ───────────────────────────────────────────────────────────────── */

onMounted(() => {
  if (!canView.value) return
  // 两路取数互不拖累：列表挂了也要能看到设置（反之亦然），各自把错误说出来。
  void reloadList()
  void loadSettings()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="platform-integration">
    <template v-if="canView">
      <!-- 页头引文块（证据件 L11-14 逐字） -->
      <div class="cv2-note cv2-page-quote" data-v2="page-quote">
        <p v-for="line in PAGE_QUOTE_LINES" :key="line">{{ line }}</p>
      </div>

      <!-- 工具条（证据件 L52-53 文案逐字；档位照设计稿 §0.5） -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--tool" data-v2="refresh" :disabled="loading" @click="onRefresh">{{ TOOLBAR_REFRESH_TEXT }}</button>
        <button type="button" class="cv2-btn" data-v2="add" @click="openForm(null)">{{ TOOLBAR_ADD_TEXT }}</button>
        <span class="cv2-toolbar-count" data-v2="count">共 {{ rows.length }} 个对接账号</span>
      </div>

      <!-- 13 列对接账号表（列名照 island buildIntegrationCols L334-392；宽度也照它） -->
      <section class="cv2-panel cv2-table-panel" aria-label="交易平台对接列表">
        <div class="cv2-table-scroll">
          <table class="cv2-table">
            <colgroup>
              <col style="width: 105px" />
              <col style="width: 170px" />
              <col style="width: 150px" />
              <col style="width: 100px" />
              <col style="width: 96px" />
              <col style="width: 96px" />
              <col style="width: 96px" />
              <col style="width: 110px" />
              <col style="width: 96px" />
              <col style="width: 120px" />
              <col style="width: 120px" />
              <col style="width: 120px" />
              <col style="width: 110px" />
            </colgroup>
            <thead>
              <tr>
                <th>平台名称</th>
                <th>平台账号/ID</th>
                <th>AppId</th>
                <th class="cv2-th-center">支付密码</th>
                <th class="cv2-th-center">是否启用</th>
                <th class="cv2-th-center">默认账号</th>
                <th class="cv2-th-center">同步验收</th>
                <th class="cv2-th-center">接单短信通知</th>
                <th class="cv2-th-center">发单包含</th>
                <th>发单联系QQ</th>
                <th>发单联系电话</th>
                <th>备注说明</th>
                <th class="cv2-th-center">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
                <td>
                  <span :data-v2-cell="`platform-${row.id}`">{{ row.platform_label || String(row.platform) }}</span>
                  <span
                    v-if="!row.is_available"
                    class="cv2-st cv2-st--paused cv2-static-badge"
                    :title="NOT_AVAILABLE_BADGE_TIP"
                    :data-v2-cell="`avail-badge-${row.id}`"
                  >{{ NOT_AVAILABLE_BADGE_TEXT }}</span>
                </td>
                <td :title="row.account">{{ row.account }}</td>
                <td :title="row.app_id">{{ row.app_id }}</td>
                <td class="cv2-td-center">
                  <button
                    v-if="row.has_password"
                    type="button"
                    class="cv2-paypwd-set"
                    :title="PAY_PWD_SET_TIP"
                    :data-v2-paypwd="row.id"
                    @click="openPayPwd(row)"
                  >
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 5" /></svg>
                  </button>
                  <button
                    v-else
                    type="button"
                    class="cv2-actlink"
                    :title="PAY_PWD_EMPTY_TIP"
                    :data-v2-paypwd="row.id"
                    @click="openPayPwd(row)"
                  >{{ PAY_PWD_EMPTY_TEXT }}</button>
                </td>
                <td class="cv2-td-center">
                  <el-switch
                    :model-value="row.is_enabled"
                    :title="'绿色代表启用，灰色代表禁用'"
                    :data-v2-switch="`is_enabled-${row.id}`"
                    @change="(v: string | number | boolean) => onRowSwitch(row, 'is_enabled', Boolean(v))"
                  />
                </td>
                <td class="cv2-td-center">
                  <el-switch
                    :model-value="row.is_default"
                    :title="'绿色代表发单到交易平台时，默认以这个账号发单。相同平台只能有1个默认账号'"
                    :data-v2-switch="`is_default-${row.id}`"
                    @change="(v: string | number | boolean) => onRowSwitch(row, 'is_default', Boolean(v))"
                  />
                </td>
                <td class="cv2-td-center">
                  <el-switch
                    :model-value="row.sync_accept"
                    :title="'绿色代表当本系统订单状态改成已完成时，自动把平台上的单子也验收。灰色代表不会自动验收平台订单，需要你手动验收平台订单'"
                    :data-v2-switch="`sync_accept-${row.id}`"
                    @change="(v: string | number | boolean) => onRowSwitch(row, 'sync_accept', Boolean(v))"
                  />
                </td>
                <td class="cv2-td-center">
                  <el-switch
                    :model-value="row.sms_notify"
                    :title="'绿色代表当平台有人接单时，本系统自动发送一条通知短信到发单客服手机上'"
                    :data-v2-switch="`sms_notify-${row.id}`"
                    @change="(v: string | number | boolean) => onRowSwitch(row, 'sms_notify', Boolean(v))"
                  />
                </td>
                <td class="cv2-td-center">
                  <el-switch
                    :model-value="row.include_push"
                    :title="'绿色代表在发单时，会自动包含发单到这个平台'"
                    :data-v2-switch="`include_push-${row.id}`"
                    @change="(v: string | number | boolean) => onRowSwitch(row, 'include_push', Boolean(v))"
                  />
                </td>
                <td :title="row.contact_qq">{{ row.contact_qq }}</td>
                <td :title="row.contact_phone">{{ row.contact_phone }}</td>
                <td :title="row.remark">{{ row.remark }}</td>
                <td class="cv2-td-center">
                  <button type="button" class="cv2-actlink" :data-v2-edit="row.id" @click="openForm(row)">编辑</button>
                  <button type="button" class="cv2-actlink cv2-actlink--danger cv2-actlink-gap" :data-v2-delete="row.id" @click="confirmDel = row">删除</button>
                </td>
              </tr>
              <tr v-if="rows.length === 0">
                <td :colspan="13" class="cv2-empty" data-v2="empty">
                  <template v-if="loading">{{ LOADING_TEXT }}</template>
                  <template v-else-if="loadError !== null">
                    {{ LOAD_FAIL_PREFIX }}{{ loadError }}
                    <button type="button" class="cv2-actlink cv2-actlink-gap" data-v2="retry" @click="onRefresh">{{ RETRY_TEXT }}</button>
                  </template>
                  <template v-else>{{ EMPTY_TEXT }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 「对所有平台生效」9 组设置（证据件 L149-247；标题 / 声明 / 图例逐字） -->
      <section class="cv2-panel" data-v2="settings-panel">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">{{ SETTINGS_PANEL_TITLE }}</h3>
        </div>
        <p class="cv2-note cv2-note--alert cv2-settings-notice" data-v2="settings-notice">{{ SETTINGS_NOTICE_TEXT }}</p>
        <p class="cv2-note cv2-settings-legend" data-v2="settings-legend">{{ SETTINGS_EFFECT_LEGEND }}</p>
        <div
          v-for="g in SETTINGS_GROUPS"
          :key="g.sk"
          class="cv2-settings-group"
          :data-v2-settings-group="g.sk"
        >
          <p class="cv2-settings-desc">
            <span
              class="cv2-st cv2-static-badge"
              :class="g.effective ? 'cv2-st--settled' : 'cv2-st--paused'"
              :data-v2-settings-badge="g.sk"
            >{{ g.effective ? SETTINGS_EFFECT_ON_TEXT : SETTINGS_EFFECT_OFF_TEXT }}</span>
            {{ g.desc }}
          </p>
          <el-radio-group v-model="settingsValues[g.field]" class="cv2-settings-radios" :data-v2-settings-group="g.sk">
            <el-radio
              v-for="opt in g.options"
              :key="opt.value"
              :value="opt.value"
              :data-v2-settings-opt="`${g.sk}:${opt.value}`"
            >{{ opt.title }}</el-radio>
          </el-radio-group>
        </div>
        <div class="cv2-settings-actions">
          <button
            type="button"
            class="cv2-btn cv2-btn--submit"
            data-v2="settings-save"
            :disabled="!settingsLoaded || settingsSubmitting"
            @click="onSaveSettings"
          >{{ FORM_SAVE_TEXT }}</button>
          <span v-if="!settingsLoaded" class="cv2-settings-hint" data-v2="settings-hint">{{ SETTINGS_NOT_LOADED_TEXT }}</span>
        </div>
      </section>

      <!-- 新增 / 修改弹层（lg；字段随平台变形照 platformFieldsOf） -->
      <el-dialog
        :model-value="editDlg"
        :title="formTitle"
        class="cv2-dialog cv2-dialog--lg"
        destroy-on-close
        data-v2="dlg-form"
        @update:model-value="editDlg = false"
      >
        <p class="cv2-note cv2-note--dlg">{{ FORM_QUOTE_TEXT }}</p>
        <div class="cv2-form plat-grid">
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">要对接的平台</span>
            <el-radio-group v-model="form.platform" :data-v2-field="`platform`">
              <el-radio
                v-for="opt in PLATFORM_OPTIONS"
                :key="opt.value"
                :value="opt.value"
                :data-v2-platform-opt="opt.value"
              >{{ opt.title }}</el-radio>
            </el-radio-group>
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">{{ fields.accountLabel }}</span>
            <el-input
              v-model="form.account"
              :placeholder="ACCOUNT_PLACEHOLDER"
              :maxlength="30"
              :data-v2-field="`account`"
            />
          </div>
          <div v-if="fields.showLoginPassword" class="cv2-field cv2-form-field">
            <span class="cv2-field-label">平台登录密码</span>
            <el-input
              v-model="form.loginPassword"
              type="password"
              autocomplete="new-password"
              :placeholder="LOGIN_PWD_PLACEHOLDER"
              :maxlength="30"
              :data-v2-field="`login-password`"
            />
            <p class="cv2-field-hint cv2-field-hint--warn" data-v2="login-pwd-notice">{{ LOGIN_PWD_NOT_OPEN_TEXT }}</p>
          </div>
          <div v-if="fields.showPayPassword" class="cv2-field cv2-form-field">
            <span class="cv2-field-label">平台支付密码</span>
            <el-input
              v-model="form.payPassword"
              type="password"
              autocomplete="new-password"
              :placeholder="payPwdPlaceholder"
              :maxlength="30"
              :data-v2-field="`pay-password`"
            />
          </div>
          <div v-if="fields.showAppId" class="cv2-field cv2-form-field">
            <span class="cv2-field-label">{{ fields.appIdLabel }}</span>
            <el-input
              v-model="form.appId"
              :placeholder="APP_ID_PLACEHOLDER"
              :maxlength="20"
              :data-v2-field="`app-id`"
            />
          </div>
          <div v-if="fields.showAppSecret" class="cv2-field cv2-form-field">
            <span class="cv2-field-label">{{ fields.appSecretLabel }}</span>
            <el-input
              v-model="form.appSecret"
              :placeholder="APP_SECRET_PLACEHOLDER"
              :maxlength="1000"
              :data-v2-field="`app-secret`"
            />
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">备注说明</span>
            <el-input v-model="form.remark" :maxlength="100" :data-v2-field="`remark`" />
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">发单联系QQ</span>
            <el-input v-model="form.contactQq" :maxlength="50" :data-v2-field="`contact-qq`" />
          </div>
          <div class="cv2-field cv2-form-field">
            <span class="cv2-field-label">发单联系电话</span>
            <el-input v-model="form.contactPhone" :maxlength="50" :data-v2-field="`contact-phone`" />
          </div>
        </div>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="form-cancel" @click="editDlg = false">取消</button>
            <button type="button" class="cv2-btn cv2-btn--submit" data-v2="form-save" :disabled="submitting" @click="void doSave()">{{ FORM_SAVE_TEXT }}</button>
          </div>
        </template>
      </el-dialog>

      <!-- 支付密码弹层（sm；标题 = island L849 逐字） -->
      <el-dialog
        :model-value="payPwdDlg !== null"
        :title="PAY_PWD_DIALOG_TITLE"
        width="480px"
        class="cv2-dialog cv2-dialog--sm"
        destroy-on-close
        data-v2="dlg-paypwd"
        @update:model-value="payPwdDlg = null"
      >
        <div class="cv2-field cv2-form-field">
          <span class="cv2-field-label">平台支付密码</span>
          <el-input
            v-model="payPwdValue"
            type="password"
            autocomplete="new-password"
            :maxlength="30"
            :data-v2-field="`new-pay-password`"
          />
        </div>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="paypwd-cancel" @click="payPwdDlg = null">取消</button>
            <button type="button" class="cv2-btn cv2-btn--submit" data-v2="paypwd-save" :disabled="payPwdSaving" @click="void doSavePayPwd()">{{ FORM_SAVE_TEXT }}</button>
          </div>
        </template>
      </el-dialog>

      <!-- 删除确认（sm；确认语与按钮逐字照 island L816-817） -->
      <el-dialog
        :model-value="confirmDel !== null"
        title="删除对接账号"
        width="480px"
        class="cv2-dialog cv2-dialog--sm"
        destroy-on-close
        data-v2="dlg-confirm"
        @update:model-value="confirmDel = null"
      >
        <p class="cv2-dlg-text" data-v2="confirm-text">{{ DELETE_CONFIRM_TEXT }}</p>
        <template #footer>
          <div class="cv2-dlg-actions">
            <button type="button" class="cv2-btn" data-v2="confirm-cancel" @click="confirmDel = null">{{ DELETE_CONFIRM_CANCEL_TEXT }}</button>
            <button type="button" class="cv2-btn cv2-btn--danger" data-v2="confirm-delete" :disabled="deleting" @click="void doDelete()">{{ DELETE_CONFIRM_OK_TEXT }}</button>
          </div>
        </template>
      </el-dialog>

      <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
        {{ toast.text }}
      </div>
    </template>
    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="no-permission">无权限查看本页</p>
    </section>
  </div>
</template>

<style scoped>
/* 13 列宽表横向滚动（面板内滚，页面不被撑破）。 */
.cv2-table-scroll {
  overflow-x: auto;
}

/* 页头引文块：贴住页头、压紧行距。 */
.cv2-page-quote {
  margin-top: 12px;
}

.cv2-page-quote p {
  margin: 0;
  line-height: 1.7;
}

.cv2-page-quote p + p {
  margin-top: 4px;
}

/* 徽标（暂未开通 / 已生效 / 仅保存）不是可点签：去掉 .cv2-st 的 pointer 假象。 */
.cv2-static-badge {
  cursor: default;
}

td .cv2-static-badge {
  margin-left: 6px;
}

/* 支付密码已配的绿对勾（内联 SVG，色走 --tt2-st-settled）。 */
.cv2-paypwd-set {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
}

.cv2-paypwd-set svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: var(--tt2-st-settled);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.cv2-paypwd-set:hover svg {
  stroke: var(--tt2-primary-deeper);
}

/* 行内两颗动作链接的间隙（actlink 无边框自带不了间距）。 */
.cv2-actlink-gap {
  margin-left: 10px;
}

/* 弹层里的引文 / 表单排版。 */
.cv2-note--dlg {
  margin: 0 0 12px;
}

.cv2-form {
  display: grid;
  gap: 12px;
}

.cv2-form-field .el-radio-group {
  flex-wrap: wrap;
}

/* 9 组设置：组块 + 选项行。 */
.cv2-settings-notice {
  margin-top: 12px;
}

.cv2-settings-legend {
  margin-top: 8px;
}

.cv2-settings-group {
  margin-top: 14px;
}

.cv2-settings-desc {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--tt2-ink);
}

.cv2-settings-desc .cv2-static-badge {
  margin-right: 8px;
  vertical-align: 1px;
}

.cv2-settings-radios {
  margin-top: 4px;
  padding-left: 10px;
}

.cv2-settings-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
}

.cv2-settings-hint {
  font-size: 12px;
  color: var(--tt2-st-paused);
}

.plat-grid {
  border: 1px solid var(--tt2-filter-line);
  border-right: 0;
  border-bottom: 0;
}
.plat-grid :deep(.cv2-form-field) {
  display: flex;
  align-items: center;
  margin: 0;
  gap: 0;
}
.plat-grid :deep(.cv2-field-label) {
  flex: 0 0 112px;
  justify-content: center;
  min-height: 46px;
  background: var(--tt2-filter-label-bg);
  border-right: 1px solid var(--tt2-filter-line);
  border-bottom: 1px solid var(--tt2-filter-line);
}
.plat-grid :deep(.cv2-form-field > :not(.cv2-field-label)) {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  border-right: 1px solid var(--tt2-filter-line);
  border-bottom: 1px solid var(--tt2-filter-line);
}
.plat-grid :deep(.el-input),
.plat-grid :deep(.el-input__wrapper) {
  display: flex;
  width: 100%;
}
.plat-grid :deep(.el-input__inner) {
  flex: 1;
  width: 100%;
  min-width: 0;
}
</style>
