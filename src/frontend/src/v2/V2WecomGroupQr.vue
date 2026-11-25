<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/** 「自动上传群码」v2（`/v2/wecom-group-qr`；2026-11-06 第三方平台批 B2，计划 §3）：
 *  企微群码应用 + 「马上来查码对接」两块凭据面合一页；三裁定照搬（api_key 只回掩码 /
 *  无「立即拉取」/ 无台账在操作日志）。⛔ 不 import island 页；文案以旧页常量逐字。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  WECOM_QR_APP_NODE,
  createWecomQrApp,
  getWecomQrApp,
  listWecomQrDeliveries,
  rotateWecomQrAppSecret,
  updateWecomQrApp,
  type WecomQrAppCallbackState,
  type WecomQrAppRow,
  type WecomQrDeliveryRow,
  type WecomQrAppState,
} from '@/api/wecomGroupQr'
import { getOrderQrConfig, saveOrderQrConfig, type OrderQrConfigRow } from '@/api/orderQrFetch'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 上半逐字文案（出处 = island pages/wecom-group-qr.ts 同名常量）────────────── */

/** 「每租户只有一把」必须说破（island ONLY_ONE_NOTICE 逐字）。 */
const ONLY_ONE_NOTICE =
  '创建后每个租户只有这一把，不能建第二个（同一套能力要给两家对接方的话，本功能不支持）。'

const NO_ACCESS_MESSAGE = '无权限管理企微群码上传应用'

/** 回调地址的即时提示（island CALLBACK_URL_HINT 逐字；反馈不是判据，真判据在后端）。 */
const CALLBACK_URL_HINT =
  '回调地址只接受 https 开头的地址（http 与其它协议一律不收；内网地址后端也会拒）。'

const MESSAGES = {
  failed: '操作失败',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中上面的密钥复制',
  created: '已创建：这把密钥就是本租户唯一的一把，把它交给对接方（下面可以复制）。',
  rotated: '已轮换：新密钥已生效，旧密钥当场作废（对接方要马上换配置）。',
} as const

const COPY_OK_TEXT = '已复制 AppKey 与 AppSecret（两行）'

/** 停用/启用与轮换的二次确认（island layer.confirm 文案逐字）。 */
const TOGGLE_CONFIRM: Record<WecomQrAppState, string> = {
  disabled: '确定停用这把应用吗？停用后第三方当场调不通（所有调用回「该应用已停用」），回调推送也一起停。',
  enabled: '确定启用这把应用吗？启用后第三方就能调它的接口了（推送是否发得出去还要看回调地址与开关）。',
}
const ROTATE_CONFIRM =
  '确定轮换密钥吗？「旧密钥立刻作废」——正在跑的第三方集成会当场调不通，要有人去改它的配置。' +
  '（新密钥会写进来，本页随时看得到、也能一键复制。）'

/** 台账折叠链接两句与空态（island DELIVERIES_* 逐字；只取第一页 20 条、无重投）。 */
const DELIVERIES_TOGGLE_CLOSED = '最近推送记录（点这里展开）'
const DELIVERIES_TOGGLE_OPEN = '最近推送记录（点这里收起）'
const DELIVERIES_EMPTY = '还没有推送记录（这把应用没有产生过事件）。'
const DELIVERIES_PAGE_SIZE = 20

/* ── 下半逐字文案（出处 = island pages/system-order-qr-fetch.ts；全角括号照旧页）── */

const QR_CAPABILITY_LINES: ReadonlyArray<string> = [
  '作用：订单还没有群二维码时，打手 / 客服点开订单的群码区块、或打手接单成功的那一刻，自动去对端查一次群码并落到订单上（扫码进本单群不用再等人工上传）。',
  '不会狂刷：同一单失败 / 未就绪后 10 分钟内不再自动查；对端返回「群没建 / 建码中」时静默等下一个自然触发点，不做后台轮询。',
  '没配置或开关关闭时一切照旧（完全向后兼容）。拉取的结果（成功 / 失败）在「操作日志」里，动作名是「马上来群码自动拉取」。',
]

const QR_MESSAGES = {
  saved: '已保存，配置立即生效',
  failed: '读取配置失败',
  /** 401 闩横幅（仅 auth_failed_at 非空显示；保存成功后按服务端返回自动摘除）。 */
  latchBanner:
    '查码密钥被对端拒绝过（401），自动拉取已暂停；填入新密钥（或保存任意配置）后自动恢复',
  apiKeyPlaceholder: '留空 = 保持不变（已保存的值只显示掩码）',
  baseUrlPlaceholder: '形如 https://example.example（从对端运维页复制）',
} as const

/** 保存按钮旁那句常驻提示（island SAVE_HINT_TEXT 逐字）。 */
const QR_SAVE_HINT_TEXT = 'api_key 留空 = 保持不变；保存任意配置会同时清除「密钥被拒」的暂停状态'
/** 启用开关旁注（island 逐字）。 */
const QR_SWITCH_NOTE = '关闭 = 全链路零动作（完全照旧）'
const QR_LATCH_PREFIX = '已暂停：'
const QR_MASKED_PREFIX = '当前密钥：'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : MESSAGES.failed
}

/** 查码块读配置失败的兜底（island 口径：非 Error 才用「读取配置失败」）。 */
function qrErrText(e: unknown): string {
  return e instanceof Error ? e.message : QR_MESSAGES.failed
}

/* ── toast（家族口径：info 1500ms / error 6000ms）──────────────────────────── */

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

/* ── 页内门（照旧页 mount 自检：is_account_admin + 节点双判，fail-closed）────── */

const denied = ref(false)

/* ── 剪贴板（island copyTextToClipboard 逐段照抄：缺 clipboard 不是静默失败）── */

function copyTextToClipboard(text: string, onDone: (ok: boolean) => void): void {
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    onDone(false)
    return
  }
  void Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
    () => onDone(true),
    () => onDone(false),
  )
}

function credentialText(row: WecomQrAppRow): string {
  return `AppKey: ${row.app_key}\nAppSecret: ${row.app_secret}`
}

/** ISO 时刻显示形态：`YYYY-MM-DD HH:mm:ss`；null/空 ⇒ `—`（⛔ 不是空白格）。 */
function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/* ═══════════════════ 上半：企微群码应用（原 wecom-group-qr 页）═══════════════════ */

const appRow = ref<WecomQrAppRow | null>(null)
const appLoaded = ref(false)
const appLoading = ref(false)
const appLoadError = ref<string | null>(null)
const creating = ref(false)
const saving = ref(false)
const rotating = ref(false)
const domain = ref('')

const callbackUrl = ref('')
const callbackEnabled = ref(false)
const ipAllowlist = ref('')

/** 那颗状态按钮的文案（判据打在机器值 state 上，⛔ 不用 state_text）。 */
function stateActionLabel(row: WecomQrAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

/** 回调地址即时提示（island callbackUrlHint 同式：空串合法、https 开头合法）。 */
function callbackUrlHint(value: string): string {
  const trimmed = value.trim()
  if (trimmed === '') return ''
  const prefix = 'https://'
  if (trimmed.toLowerCase().startsWith(prefix) && trimmed.length > prefix.length) return ''
  return CALLBACK_URL_HINT
}

const callbackHint = computed(() => callbackUrlHint(callbackUrl.value))

/** 保存回报三分支（island callbackSaveReport 逐字，wecom 版第三支与 openApps 不同）。 */
function callbackSaveReport(url: string, state: WecomQrAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；发失败的记录会留在「最近推送记录」里（服务端会先自动重试几次）。'
}

/** IP 白名单保存回报（空 = 不限制是合法值；island ipSaveReport 逐字）。 */
function ipSaveReport(value: string): string {
  return value.trim() === ''
    ? '已保存：IP 白名单为空 ⇒ 不限制来源 IP（任何 IP 拿着这把密钥都能调）。'
    : '已保存：只有白名单里的 IP 能调（其余来源一律 401）。'
}

/** 装配唯一出口：null = 还没建；台账同时清空并收起（旧台账是指向别一把的证据）。 */
function applyApp(row: WecomQrAppRow | null): void {
  appRow.value = row
  deliveriesOpen.value = false
  deliveries.value = []
  deliveriesTotal.value = 0
  deliveriesError.value = null
  if (row === null) return
  callbackUrl.value = row.callback_url
  callbackEnabled.value = row.callback_state === 'enabled'
  ipAllowlist.value = row.ip_allowlist
}

async function reloadApp(): Promise<void> {
  appLoading.value = true
  try {
    applyApp(await getWecomQrApp())
    appLoaded.value = true
    appLoadError.value = null
  } catch (e) {
    appLoadError.value = errText(e)
    showToast(appLoadError.value, 'error')
  } finally {
    appLoading.value = false
  }
}

function doCreate(): void {
  if (creating.value) return
  creating.value = true
  // 不传名字：默认名由后端给（缺名请求体形状由 api 模块负责）。
  void createWecomQrApp()
    .then(async (saved) => {
      await reloadApp()
      if (appLoadError.value === null) showToast(`${MESSAGES.created}（应用名：${saved.name}）`)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      creating.value = false
    })
}

function onCopyCredential(): void {
  const row = appRow.value
  if (row === null) return
  // 复制内容从行数据取（⛔ 不从 DOM 读）。
  copyTextToClipboard(credentialText(row), (ok) =>
    showToast(ok ? COPY_OK_TEXT : MESSAGES.copyFailed),
  )
}

/* ── 停用 / 启用（两键同一次 PATCH {state}；确认文案按方向二选一）────────────── */

const toggleDlg = ref<{ to: WecomQrAppState } | null>(null)

const toggleConfirmText = computed(() =>
  toggleDlg.value === null ? '' : TOGGLE_CONFIRM[toggleDlg.value.to],
)

function confirmToggle(): void {
  const row = appRow.value
  if (row === null) return
  toggleDlg.value = { to: row.state === 'enabled' ? 'disabled' : 'enabled' }
}

function doToggle(): Promise<void> {
  const dlg = toggleDlg.value
  if (dlg === null || saving.value) return Promise.resolve()
  saving.value = true
  return updateWecomQrApp({ state: dlg.to })
    .then(async () => {
      toggleDlg.value = null
      await reloadApp()
      if (appLoadError.value === null) showToast(dlg.to === 'disabled' ? '已停用' : '已启用')
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      saving.value = false
    })
}

/* ── 轮换密钥（旧密钥当场作废；闸门只包请求不包确认）────────────────────────── */

const rotateDlg = ref(false)

function doRotate(): Promise<void> {
  if (!rotateDlg.value || rotating.value) return Promise.resolve()
  rotating.value = true
  return rotateWecomQrAppSecret()
    .then(async () => {
      rotateDlg.value = false
      await reloadApp()
      if (appLoadError.value === null) showToast(MESSAGES.rotated)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      rotating.value = false
    })
}

/* ── 保存回调设置（两键一起发，无前端校验门；后端 1000 原句如实显示）────────── */

function saveCallbackSettings(): void {
  const row = appRow.value
  if (row === null || saving.value) return
  const state: WecomQrAppCallbackState = callbackEnabled.value ? 'enabled' : 'disabled'
  const url = callbackUrl.value.trim()
  saving.value = true
  void updateWecomQrApp({ callback_url: url, callback_state: state })
    .then(async () => {
      await reloadApp()
      if (appLoadError.value === null) showToast(callbackSaveReport(url, state))
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      saving.value = false
    })
}

/* ── 保存 IP 白名单（单独一颗、单独一个键；空串 = 不限制是合法值）───────────── */

function saveIpAllowlist(): void {
  const row = appRow.value
  if (row === null || saving.value) return
  const value = ipAllowlist.value.trim()
  saving.value = true
  void updateWecomQrApp({ ip_allowlist: value })
    .then(async () => {
      await reloadApp()
      if (appLoadError.value === null) showToast(ipSaveReport(value))
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      saving.value = false
    })
}

/* ── 推送台账（点开即拉一次、只取第一页 20 条；⛔ 无重投端点，六列全只读）───── */

const deliveriesOpen = ref(false)
const deliveries = ref<WecomQrDeliveryRow[]>([])
const deliveriesTotal = ref(0)
const deliveriesError = ref<string | null>(null)
const deliveriesLoading = ref(false)

const deliveriesToggleLabel = computed(() =>
  deliveriesOpen.value ? DELIVERIES_TOGGLE_OPEN : DELIVERIES_TOGGLE_CLOSED,
)

function toggleDeliveries(): void {
  const next = !deliveriesOpen.value
  deliveriesOpen.value = next
  if (next) void loadDeliveries()
}

async function loadDeliveries(): Promise<void> {
  deliveriesError.value = null
  deliveriesLoading.value = true
  try {
    const page = await listWecomQrDeliveries({ page: 1, page_size: DELIVERIES_PAGE_SIZE })
    deliveries.value = page.items
    deliveriesTotal.value = page.total
  } catch (e) {
    // 失败如实显示，⛔ 不渲染成「还没有推送记录」（「一条都没有」≠「没取到」）。
    deliveries.value = []
    deliveriesTotal.value = 0
    deliveriesError.value = errText(e)
  } finally {
    deliveriesLoading.value = false
  }
}

/** 主行 = 展示单号（来源号非空取它、空回退内置号，D-0301）。 */
function orderMain(d: WecomQrDeliveryRow): string {
  return d.source_order_no || d.order_no
}

/** 副行只在两号不同时标注「内置 X」。 */
function orderSub(d: WecomQrDeliveryRow): string {
  return d.source_order_no !== '' && d.source_order_no !== d.order_no ? d.order_no : ''
}

/** 「失败原因」格：空串 → `—`；否则原文 +（HTTP 码）。 */
function reasonText(d: WecomQrDeliveryRow): string {
  if (d.reason === '') return '—'
  return d.reason + (d.response_status === null ? '' : `（HTTP ${d.response_status}）`)
}

/** 台账行状态签档位（色走 token：done 绿、failed 红、其余琥珀）。 */
function deliveryStatusClass(d: WecomQrDeliveryRow): string {
  return d.status === 'done' ? 'cv2-st--open' : d.status === 'failed' ? 'cv2-st--error' : 'cv2-st--paused'
}

/* ═══════════════════ 下半：马上来查码对接（原 system-order-qr-fetch 页）═══════════════════ */

const qrRow = ref<OrderQrConfigRow | null>(null)
const qrLoaded = ref(false)
const qrLoading = ref(false)
const qrLoadError = ref<string | null>(null)
const qrSaving = ref(false)
const latched = ref(false)

const qrForm = reactive({ baseUrl: '', apiKey: '', enabled: false })

const qrMaskedText = computed(() => {
  const masked = qrRow.value?.api_key_masked ?? ''
  return masked === '' ? '' : `${QR_MASKED_PREFIX}${masked}`
})

/** 装配唯一出口：api_key 输入框刻意留空（只回掩码，回写掩码会把掩码存成新值）。 */
function applyQr(row: OrderQrConfigRow): void {
  qrRow.value = row
  qrForm.baseUrl = row.base_url
  qrForm.apiKey = ''
  qrForm.enabled = row.enabled
  latched.value = row.auth_failed_at != null
}

async function loadQr(): Promise<void> {
  qrLoading.value = true
  try {
    applyQr(await getOrderQrConfig())
    qrLoaded.value = true
    qrLoadError.value = null
  } catch (e) {
    qrLoadError.value = qrErrText(e)
    showToast(qrLoadError.value, 'error')
  } finally {
    qrLoading.value = false
  }
}

/** 保存配置：api_key 总是发、空串 = 后端保持旧值；成功响应即已清 401 闩（按返回行装配）。 */
function saveQrConfig(): void {
  if (qrSaving.value) return
  qrSaving.value = true
  const baseUrl = qrForm.baseUrl.trim()
  const apiKey = qrForm.apiKey
  const enabled = qrForm.enabled
  void saveOrderQrConfig({ enabled, base_url: baseUrl, api_key: apiKey })
    .then((row) => {
      showToast(QR_MESSAGES.saved)
      applyQr(row)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      qrSaving.value = false
    })
}

/* ── 入口与出口 ──────────────────────────────────────────────────────────────── */

onMounted(() => {
  domain.value = window.location.origin
  // 页内门：查码块节点与群码块同值（都是 system.pinduoduo_settings），一门两块。
  if (session.isAccountAdmin !== true || !session.canNode([WECOM_QR_APP_NODE])) {
    denied.value = true
    showToast(NO_ACCESS_MESSAGE)
    return
  }
  // 两路取数互不拖累：查码挂了群码照常，反之亦然。
  void reloadApp()
  void loadQr()
})
</script>

<template>
  <div class="cv2-page integration-form" data-v2-page="wecom-group-qr">
    <template v-if="!denied">
      <!-- 页头提示 -->
      <blockquote class="cv2-note wq-quote integration-note" data-v2="quote">
        <span>创建应用并配置回调地址后，第三方程序可将<b>企微群二维码</b>上传为订单截图。</span>
        <br />
        <span>密钥<b>只能停用不能删除</b>；轮换密钥<b>没有宽限期</b>——旧密钥当场作废。密钥随时可以在下面查看与复制。</span>
      </blockquote>

      <!-- ══ 上半：企微群码应用 ══ -->
      <section v-if="appLoadError !== null && !appLoaded" class="cv2-panel" data-v2="app-load-error">
        <p class="wq-warn">加载失败：{{ appLoadError }}</p>
        <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" data-v2="app-retry" @click="void reloadApp()">重试</button>
      </section>
      <p v-else-if="!appLoaded" class="cv2-empty" data-v2="app-loading">正在加载…</p>

      <template v-else-if="appRow === null">
        <!-- 还没创建（island EMPTY 块逐字；创建直接发、无弹层） -->
        <section class="cv2-panel" data-v2="empty-panel">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">还没创建</h3>
          </div>
          <p class="cv2-note" data-v2="only-one">{{ ONLY_ONE_NOTICE }}</p>
          <div class="wq-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="create" :disabled="creating" @click="doCreate">创建应用</button>
          </div>
          <p class="cv2-field-hint" data-v2="create-aux">
            创建成功后系统会生成 AppKey 与 AppSecret（默认应用名由系统给），再把<b>回调通知地址</b>与<b>IP 白名单</b>配好。
          </p>
        </section>
      </template>

      <template v-else>
        <!-- 接入信息（AppSecret 明文 readonly；三颗钮 + 状态签） -->
        <section class="cv2-panel" data-v2="app-panel">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">接入信息</h3>
          </div>
          <div class="wq-form integration-form">
            <div class="cv2-field">
              <span class="cv2-field-label">本租户域名</span>
              <span class="wq-mono" data-v2="domain">{{ domain }}</span>
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">AppKey</span>
              <el-input :model-value="appRow.app_key" readonly data-v2="app-key" />
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">AppSecret</span>
              <el-input class="wq-secret" :model-value="appRow.app_secret" type="textarea" :rows="3" readonly data-v2="app-secret" />
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">状态</span>
              <div class="wq-actions">
                <span class="cv2-st" :class="appRow.state === 'enabled' ? 'cv2-st--open' : 'cv2-st--error'" :data-v2-state="appRow.state">{{ appRow.state_text }}</span>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--primary cv2-btn--quiet" data-v2="copy" @click="onCopyCredential">复制 AppKey 与 AppSecret</button>
                <button type="button" class="cv2-btn cv2-btn--sm" data-v2="toggle" @click="confirmToggle">{{ stateActionLabel(appRow) }}</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger cv2-btn--commit" data-v2="rotate" @click="rotateDlg = true">轮换密钥</button>
              </div>
            </div>
          </div>
          <p class="cv2-field-hint" data-v2="credential-warning">
            ⛔ 这是凭据：不要贴到聊天群 / 工单 / 截图里。轮换之后<b>旧密钥立刻作废</b>，对接方要马上改配置。
          </p>
        </section>

        <!-- 回调通知地址（与 IP 白名单分开两颗保存，有意） -->
        <section class="cv2-panel" data-v2="callback-panel">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">回调通知地址</h3>
          </div>
          <blockquote class="cv2-note wq-cb-notice integration-note" data-v2="callback-notice">
            <span>推送怎么走：订单状态变化 / 订单删除 / 订单新增图片这几类事件，我们会按产生顺序 POST 到上面的地址，每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> / <b>X-TT-Signature</b> 三个头，对方要用 AppSecret 按我们的算法<b>验真</b>再处理（⛔ 不要只看来源 IP 就算数）。</span>
            <br />
            <span>地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；推送失败服务端会自动重试几次，仍失败的那条会留在下面的台账里（状态是「投递失败」）。</span>
          </blockquote>
          <div class="wq-form integration-form">
            <div class="cv2-field">
              <span class="cv2-field-label">回调地址</span>
              <el-input v-model="callbackUrl" :maxlength="200" autocomplete="off" placeholder="https://接收推送的地址（留空 = 不推送）" data-v2="callback-url" />
              <span class="cv2-field-hint cv2-field-hint--warn" data-v2="callback-hint">{{ callbackHint }}</span>
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">回调开关</span>
              <el-switch v-model="callbackEnabled" inline-prompt active-text="启用" inactive-text="停用" data-v2="callback-switch" />
            </div>
          </div>
          <div class="wq-panel-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="callback-save" :disabled="saving" @click="saveCallbackSettings">保存回调设置</button>
          </div>
        </section>

        <!-- IP 白名单 -->
        <section class="cv2-panel" data-v2="ip-panel">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">IP 白名单</h3>
          </div>
          <div class="wq-form integration-form">
            <div class="cv2-field">
              <span class="cv2-field-label">允许的 IP</span>
              <el-input v-model="ipAllowlist" type="textarea" :rows="2" :maxlength="500" placeholder="留空 = 不限制。多个 IP / 网段怎么写，按对接方给的出口 IP 填，一行一个" data-v2="ip-allowlist" />
            </div>
          </div>
          <div class="wq-panel-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="ip-save" :disabled="saving" @click="saveIpAllowlist">保存 IP 白名单</button>
          </div>
        </section>

        <!-- 推送台账（可折叠；只读六列、无重投列） -->
        <section class="cv2-panel" data-v2="deliveries-panel">
          <div class="cv2-panel-head">
            <h3 class="cv2-panel-title">推送台账</h3>
          </div>
          <div class="wq-deliveries">
            <a href="#" class="cv2-panel-more" data-v2="deliveries-toggle" @click.prevent="toggleDeliveries">{{ deliveriesToggleLabel }}</a>
            <div v-if="deliveriesOpen" class="wq-deliveries-body" data-v2="deliveries-body">
              <p v-if="deliveriesError !== null" class="wq-warn" data-v2="deliveries-warn">{{ deliveriesError }}</p>
              <p v-else-if="deliveriesLoading" class="wq-dim">正在加载…</p>
              <p v-else-if="deliveries.length === 0" class="wq-dim" data-v2="deliveries-empty">{{ DELIVERIES_EMPTY }}</p>
              <template v-else>
                <table class="cv2-table" data-v2="deliveries-table">
                  <colgroup>
                    <col style="width: 150px" />
                    <col style="width: 130px" />
                    <col />
                    <col style="width: 130px" />
                    <col style="width: 70px" />
                    <col />
                  </colgroup>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>事件</th>
                      <th>订单号</th>
                      <th>状态</th>
                      <th class="cv2-th-center">尝试次数</th>
                      <th>失败原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="d in deliveries" :key="d.id" class="cv2-row" :data-v2-delivery="d.id">
                      <td class="cv2-td-time">{{ timeText(d.created_at) }}</td>
                      <td>{{ d.event_text }}</td>
                      <td>
                        {{ orderMain(d) }}
                        <span v-if="orderSub(d) !== ''" class="wq-order-sub">内置 {{ orderSub(d) }}</span>
                      </td>
                      <td>
                        <span class="cv2-st" :class="deliveryStatusClass(d)" :data-v2-delivery-status="d.status">{{ d.status_text }}</span>
                        <div v-if="d.status === 'pending' && d.next_attempt_at !== null" class="wq-next">下次 {{ timeText(d.next_attempt_at) }}</div>
                      </td>
                      <td class="cv2-td-center">{{ d.attempts }}</td>
                      <td>{{ reasonText(d) }}</td>
                    </tr>
                  </tbody>
                </table>
                <p v-if="deliveriesTotal > deliveries.length" class="wq-dim" data-v2="deliveries-more">共 {{ deliveriesTotal }} 条，这里只显示最近 {{ deliveries.length }} 条。</p>
              </template>
            </div>
          </div>
        </section>

      </template>

      <!-- ══ 下半：马上来查码对接（独立 panel，两块保存互不干扰）══ -->
      <section class="cv2-panel" data-v2="qr-panel">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">马上来查码对接</h3>
        </div>
        <template v-if="qrLoadError !== null && !qrLoaded">
          <p class="wq-warn" data-v2="qr-load-error">加载失败：{{ qrLoadError }}</p>
          <div class="wq-actions">
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" data-v2="qr-retry" @click="void loadQr()">重试</button>
          </div>
        </template>
        <p v-else-if="!qrLoaded" class="cv2-empty" data-v2="qr-loading">正在加载…</p>
        <template v-else>
          <div class="wq-qr-lines" data-v2="qr-capability">
            <p v-for="line in QR_CAPABILITY_LINES" :key="line">{{ line }}</p>
          </div>
          <div v-if="latched" class="wq-latch" data-v2="qr-latch">
            <b>{{ QR_LATCH_PREFIX }}</b><span data-v2="qr-latch-text">{{ QR_MESSAGES.latchBanner }}</span>
          </div>
          <div class="wq-form integration-form">
            <div class="cv2-field">
              <span class="cv2-field-label">对端基址</span>
              <el-input v-model="qrForm.baseUrl" autocomplete="off" :placeholder="QR_MESSAGES.baseUrlPlaceholder" data-v2="qr-base-url" />
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">api_key</span>
              <el-input v-model="qrForm.apiKey" type="password" autocomplete="new-password" :placeholder="QR_MESSAGES.apiKeyPlaceholder" data-v2="qr-api-key" />
              <span v-if="qrMaskedText !== ''" class="cv2-field-hint" data-v2="qr-masked">{{ qrMaskedText }}</span>
            </div>
            <div class="cv2-field">
              <span class="cv2-field-label">启用</span>
              <div class="wq-inline">
                <el-switch v-model="qrForm.enabled" data-v2="qr-enabled" />
                <span class="cv2-field-hint" data-v2="qr-switch-note">{{ QR_SWITCH_NOTE }}</span>
              </div>
            </div>
          </div>
          <div class="wq-qr-actions">
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="qr-save" :disabled="qrSaving" @click="saveQrConfig">保存配置</button>
            <span class="cv2-field-hint" data-v2="qr-save-hint">{{ QR_SAVE_HINT_TEXT }}</span>
          </div>
        </template>
      </section>
    </template>

    <div v-else class="cv2-empty" data-v2="denied">{{ NO_ACCESS_MESSAGE }}</div>

    <!-- 停用/启用确认（island layer.confirm 文案逐字） -->
    <el-dialog
      :model-value="toggleDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-toggle"
      @update:model-value="toggleDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="toggle-question">{{ toggleConfirmText }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="toggleDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="toggle-confirm" :disabled="saving" @click="void doToggle()">确定</button>
      </div>
    </el-dialog>

    <!-- 轮换密钥确认（含「旧密钥立刻作废」逐字） -->
    <el-dialog
      :model-value="rotateDlg"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-rotate"
      @update:model-value="rotateDlg = false"
    >
      <p class="cv2-dlg-text" data-v2="rotate-question">{{ ROTATE_CONFIRM }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="rotateDlg = false">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="rotate-confirm" :disabled="rotating" @click="void doRotate()">确定</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */
.wq-quote {
  line-height: 26px;
}

.wq-warn {
  color: var(--tt2-st-error);
}

.wq-strong {
  font-weight: 700;
}

.wq-dim {
  color: var(--tt2-dim);
}

.wq-mono {
  font-family: var(--tt2-mono);
  word-break: break-all;
}

.wq-form {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

.wq-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.wq-panel-actions {
  display: flex;
  justify-content: center;
  margin-top: 14px;
}

.wq-cb-notice {
  color: var(--tt2-ink);
  line-height: 26px;
}

.wq-deliveries {
  margin-top: 12px;
}

.wq-deliveries-body {
  margin-top: 8px;
  max-height: 360px;
  overflow: auto;
}

.wq-order-sub {
  display: block;
  font-size: 12px;
  color: var(--tt2-dim);
}

.wq-next {
  font-size: 12px;
  color: var(--tt2-dim);
}

.wq-latch {
  margin-top: 12px;
  padding: 10px 14px;
  border: 1px solid var(--tt2-st-error-line);
  border-radius: 4px;
  background: var(--tt2-st-error-bg);
  color: var(--tt2-st-error);
  line-height: 22px;
}

.wq-qr-lines {
  margin-top: 12px;
  color: var(--tt2-ink);
  font-size: 13px;
  line-height: 1.7;
}

.wq-qr-lines p {
  margin: 0 0 6px;
}

.wq-inline {
  display: flex;
  align-items: center;
  gap: 10px;
}

.wq-qr-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}

/* AppSecret 明文长串折行（⛔ 不截断）。 */
.wq-form :deep(.wq-secret .el-textarea__inner) {
  word-break: break-all;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2OpenApps 同款）。 */
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
</style>

<style scoped src="./integration-form.css"></style>
