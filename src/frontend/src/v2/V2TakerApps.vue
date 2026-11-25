<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「打手对接」v2（`/v2/taker-apps`；2026-11-06 开放平台批，计划 §4）。
 * 旧页 = island 自建页 `layui-island/pages/taker-apps.ts`（自有功能，无对标物）。
 * 五块 + 回调线三块全保留：列表四钮；新建**按身份两副面孔**（持 SYSTEM_ADMIN_NODE 的
 * 看全部且创建必须选绑定打手，候选 = `listTakerCandidates` 第一页 200 条 + 三条降级提示；
 * 持 TAKER_APP_NODE 的打手只见自己的、创建锁死为自己不渲染选择器）；停用/启用（PATCH
 * `{state}`，⛔ 载荷无 `taker` 键）；轮换密钥（立即失效确认）；复制密钥（两行进剪贴板）；
 * 回调面板（两键一起 PATCH，地址上限 200）；推送台账**懒加载**（打开面板不取，点开折叠区
 * 才拉 20/200）；「接入文档」弹层（`src/docs/taker-api.md` + marked 渲染链照旧页逐段照抄）。
 * 顶部告知四句、校验与 toast 全集逐字（retryDone 含「至少一次」「再收到一次」「X-TT-Delivery」）。
 * ⛔ 不 import island 页模块（helper 照抄不引用）；模板插值自带转义，v-html 只用于仓内受控 md。
 */
import { computed, onMounted, ref } from 'vue'
import { Marked } from 'marked'
// ⭐ 原文 = `docs/TAKER-API.md` 的逐字节副本 `src/docs/taker-api.md`（改了出处要重跑 cp，同旧页）。
import takerApiDocSource from '../docs/taker-api.md?raw'
import {
  CALLBACK_URL_MAX_LENGTH,
  SYSTEM_ADMIN_NODE,
  TAKER_APP_NODE,
  TAKER_DELIVERIES_PAGE_SIZE,
  createTakerApp,
  listTakerApps,
  listTakerCandidates,
  listTakerDeliveries,
  retryTakerDelivery,
  rotateTakerAppSecret,
  updateTakerApp,
  type TakerAppCallbackState,
  type TakerAppRow,
  type TakerDeliveryRow,
} from '@/api/takerApps'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

const NO_ACCESS_MESSAGE = '无权限管理打手对接凭据'

/** 顶部告知（island AUTHORITY_NOTICE / ROLE_NOTICE / 两个 SCOPE 逐字；<b> 在模板里还原）。 */

/** 回调地址的即时提示（island CALLBACK_URL_HINT 逐字；反馈不是判据，真判据在后端）。 */
const CALLBACK_URL_HINT =
  '回调地址必须是 https 开头的公网地址（http 与其它协议一律不收；内网 / 回环地址后端也会拒）。'

/** 折叠区两句（island 逐字；与上家侧「最近推送记录」是两份文案，各指各的折叠区）。 */
const DELIVERIES_TOGGLE_CLOSED = '查看推送台账'
const DELIVERIES_TOGGLE_OPEN = '收起推送台账'

/** 接入文档弹层文案（island DOC_* 逐字）。 */
const DOC_COPY_BUTTON_TEXT = '复制 Markdown 原文'
const DOC_DOWNLOAD_BUTTON_TEXT = '下载 .md'
const DOC_DOWNLOAD_FILENAME = 'TAKER-API.md'
const DOC_COPY_DONE = '已复制接入文档的 Markdown 原文'
const DOC_COPY_FAILED = '复制失败（浏览器不允许自动复制），请用旁边的「下载 .md」'
const DOC_DOWNLOAD_FAILED =
  '这个浏览器不支持直接下载——原文在仓库的 docs/TAKER-API.md 里，直接打开那个文件即可。'

const MESSAGES = {
  noName: '请填写应用名',
  noTaker: '请选择要绑定的打手账号',
  failed: '操作失败',
  /** 候选取不到（降级分支）：如实说，⛔ 不许静默变成空下拉。 */
  pickerFailed: '候选账号列表取不到（后端没放行这条请求），无法新建：',
  pickerEmpty: '本租户没有可绑定的账号。',
  pickerTruncated: '账号较多，这里只显示前 {shown} 个（共 {total} 个）。',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中列表里的密钥复制',
  /** 行载荷缺回调两键（部署面降级分支）：说出来并中止打开。 */
  callbackFieldsMissing:
    '后端这一次没有回这个应用的回调配置（只回了别的字段），为避免把现有配置覆盖成空，' +
    '这一次不打开设置面板。请刷新页面重试；若一直如此，说明前端与后端版本不一致。',
  /** 重投成功回报（spec 钉死「至少一次」「再收到一次」「X-TT-Delivery」三个片段）。 */
  retryDone:
    '已重投：这一条回到了待投递队列，下一轮投递（约 10 秒内）就会发出去。' +
    '⚠️ 投递是「至少一次」：如果对方上次其实收到了，它会「再收到一次」' +
    '（对方要按 X-TT-Delivery 去重）。',
} as const

const EMPTY_TABLE_TEXT = '还没有凭据；点上面的「新增打手凭据」建一套'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast；info 1500ms / error 6000ms）────────────────────── */

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

/* ── 页内门与身份分支（与后端 `_is_system_admin` 同一个谓词：canNode 管理员短路同源）─ */

const denied = ref(false)
/** 管理员面孔：看全部 + 创建必须选绑定打手；打手面孔：只看自己 + 锁死为自己。 */
const isSystemAdmin = ref(false)

/* ── 列表（island decorateRows / timeText / lastUsedText 同式）───────────────── */

function stateActionLabel(row: TakerAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

type TakerAppTableRow = TakerAppRow & { state_action_label: string }

const rows = ref<TakerAppTableRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

function lastUsedText(value: string | null): string {
  if (value === null || value === '') return '—（还没被调用过）'
  return timeText(value)
}

async function reload(): Promise<boolean> {
  loading.value = true
  try {
    const list = await listTakerApps()
    rows.value = list.map((row) => ({ ...row, state_action_label: stateActionLabel(row) }))
    loadError.value = null
    return true
  } catch (e) {
    rows.value = []
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
    return false
  } finally {
    loading.value = false
  }
}

/* ── 剪贴板（island copyCredentialText / copyTextToClipboard 逐段照抄）────────── */

function copyCredentialText(row: TakerAppRow): string {
  return `AppKey: ${row.app_key}\nAppSecret: ${row.app_secret}`
}

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

function onCopySecret(row: TakerAppRow): void {
  copyTextToClipboard(copyCredentialText(row), (ok) =>
    showToast(ok ? `已复制「${row.name}」的 AppKey 与 AppSecret` : MESSAGES.copyFailed),
  )
}

/* ── 新建（按身份两副面孔；候选 = listTakerCandidates 第一页 200 条 + 三条降级提示）─ */

const createDlg = ref<{ name: string } | null>(null)
const savingCreate = ref(false)
const candidates = ref<{ id: number; username: string; display_name: string }[]>([])
const pickerHint = ref('')
const candidatesLoading = ref(false)
/** 管理员面孔里选中的绑定打手（undefined = 没选；⛔ 真值判断会把 0 号边界混掉）。 */
const takerId = ref<number | undefined>(undefined)

/** 候选显示形态：`显示名（账号名）`（island candidateLabel 同式；只用于上屏）。 */
function candidateLabel(row: { username: string; display_name: string }): string {
  const name = row.display_name === '' ? row.username : row.display_name
  return `${name}（${row.username}）`
}

/** 候选列表提示文（island pickerHintText 逐字三分支）。 */
function pickerHintText(shown: number, total: number): string {
  if (shown === 0) return MESSAGES.pickerEmpty
  if (total > shown) {
    return MESSAGES.pickerTruncated.replace('{shown}', String(shown)).replace('{total}', String(total))
  }
  return ''
}

async function loadCandidates(): Promise<void> {
  pickerHint.value = ''
  candidatesLoading.value = true
  try {
    const page = await listTakerCandidates()
    candidates.value = page.items
    pickerHint.value = pickerHintText(page.items.length, page.total)
  } catch (e) {
    // 失败如实说（⛔ 不渲染成「本租户没有账号」——两个方向都是假信号）。
    candidates.value = []
    pickerHint.value = MESSAGES.pickerFailed + errText(e)
  } finally {
    candidatesLoading.value = false
  }
}

function openCreate(): void {
  createDlg.value = { name: '' }
  takerId.value = undefined
  if (isSystemAdmin.value) {
    pickerHint.value = ''
    void loadCandidates()
  }
}

function submitCreate(): Promise<void> {
  const dlg = createDlg.value
  if (dlg === null || savingCreate.value) return Promise.resolve()
  const name = dlg.name.trim()
  if (name === '') {
    showToast(MESSAGES.noName)
    return Promise.resolve()
  }
  let taker: number | undefined
  if (isSystemAdmin.value) {
    if (takerId.value === undefined) {
      showToast(MESSAGES.noTaker)
      return Promise.resolve()
    }
    taker = takerId.value
  }
  savingCreate.value = true
  // 打手那一支不发 taker（后端锁死为他自己），管理员那支必须发。
  return createTakerApp(taker === undefined ? { name } : { name, taker })
    .then(async () => {
      createDlg.value = null
      await reload()
      showToast(
        isSystemAdmin.value
          ? '已创建：密钥就在列表里，随时可以查看与复制'
          : '已创建，并绑定在你名下：密钥就在列表里，随时可以查看与复制',
      )
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      savingCreate.value = false
    })
}

/* ── 回调与推送面板（两键一起 PATCH；缺键降级中止；台账懒加载）─────────────────── */

const callbackDlg = ref<{ row: TakerAppRow; url: string; enabled: boolean } | null>(null)
const savingCallback = ref(false)
const callbackAppId = ref<number | null>(null)

function callbackUrlHint(value: string): string {
  const trimmed = value.trim()
  if (trimmed === '') return ''
  const prefix = 'https://'
  if (trimmed.toLowerCase().startsWith(prefix) && trimmed.length > prefix.length) return ''
  return CALLBACK_URL_HINT
}

const callbackHint = computed(() =>
  callbackDlg.value === null ? '' : callbackUrlHint(callbackDlg.value.url),
)

/** 保存回报三分支（island taker callbackSaveReport 逐字；第三支指向本页「查看推送台账」）。 */
function callbackSaveReport(url: string, state: TakerAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；下面「查看推送台账」里失败的行可以手工重投。'
}

function openCallback(row: TakerAppRow): void {
  if (!('callback_url' in row) || !('callback_state' in row)) {
    showToast(MESSAGES.callbackFieldsMissing)
    return
  }
  callbackDlg.value = { row, url: row.callback_url, enabled: row.callback_state === 'enabled' }
  // ⭐ 懒加载：清痕迹、回收起态，但**不**取台账——点开折叠区才拉（旧页 spec 判据 ③）。
  deliveriesOpen.value = false
  deliveries.value = []
  deliveriesTotal.value = 0
  deliveriesError.value = null
  callbackAppId.value = row.id
}

function saveCallbackSettings(): void {
  const dlg = callbackDlg.value
  if (dlg === null || savingCallback.value) return
  const app = dlg.row
  const state: TakerAppCallbackState = dlg.enabled ? 'enabled' : 'disabled'
  const url = dlg.url.trim()
  savingCallback.value = true
  void updateTakerApp(app.id, { callback_url: url, callback_state: state })
    .then(async () => {
      await reload()
      showToast(callbackSaveReport(url, state))
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      savingCallback.value = false
    })
}

/* ── 推送台账（懒加载：打开面板不取、点开折叠区才拉；20/200；渲染照 island 同式）── */

const deliveriesOpen = ref(false)
const deliveries = ref<TakerDeliveryRow[]>([])
const deliveriesTotal = ref(0)
const deliveriesError = ref<string | null>(null)
const deliveriesLoading = ref(false)

const deliveriesToggleLabel = computed(() =>
  deliveriesOpen.value ? DELIVERIES_TOGGLE_OPEN : DELIVERIES_TOGGLE_CLOSED,
)

function reasonText(d: TakerDeliveryRow): string {
  if (d.reason === '') return '—'
  return d.reason + (d.response_status === null ? '' : `（HTTP ${d.response_status}）`)
}

function orderMain(d: TakerDeliveryRow): string {
  return d.source_order_no || d.order_no
}

function orderSub(d: TakerDeliveryRow): string {
  return d.source_order_no !== '' && d.source_order_no !== d.order_no ? d.order_no : ''
}

function deliveryStatusClass(d: TakerDeliveryRow): string {
  return d.status === 'done' ? 'cv2-st--open' : d.status === 'failed' ? 'cv2-st--error' : 'cv2-st--paused'
}

async function loadDeliveries(pk: number): Promise<void> {
  deliveriesError.value = null
  deliveriesLoading.value = true
  try {
    const page = await listTakerDeliveries(pk, { page: 1, page_size: TAKER_DELIVERIES_PAGE_SIZE })
    // 迟到响应丢弃：面板可以关掉再开另一个应用。
    if (callbackAppId.value !== pk) return
    deliveries.value = page.items
    deliveriesTotal.value = page.total
  } catch (e) {
    if (callbackAppId.value !== pk) return
    deliveries.value = []
    deliveriesTotal.value = 0
    deliveriesError.value = errText(e)
  } finally {
    deliveriesLoading.value = false
  }
}

/** 折叠区的唯一取数入口：只在**展开**的那一下拉（收起时不打任何请求）。 */
function toggleDeliveries(): void {
  deliveriesOpen.value = !deliveriesOpen.value
  if (deliveriesOpen.value && callbackAppId.value !== null) {
    void loadDeliveries(callbackAppId.value)
  }
}

/* ── 重投（只有 failed 行可点；二次确认；成功回报 = MESSAGES.retryDone 逐字）────── */

const retryDlg = ref<{ deliveryPk: number } | null>(null)
const retrying = ref(false)

function confirmRetry(deliveryPk: number): void {
  retryDlg.value = { deliveryPk }
}

function doRetry(): Promise<void> {
  const dlg = retryDlg.value
  const app = callbackDlg.value?.row
  if (dlg === null || app === undefined || retrying.value) return Promise.resolve()
  retrying.value = true
  return retryTakerDelivery(app.id, dlg.deliveryPk)
    .then(async () => {
      retryDlg.value = null
      deliveriesOpen.value = true
      await loadDeliveries(app.id)
      // 失败那一支不在这里：那必须显示后端那句中文（上面的 catch）。
      showToast(MESSAGES.retryDone)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      retrying.value = false
    })
}

/* ── 停用 / 启用（PATCH `{state}`；⛔ 载荷无 `taker` 键——改不了绑定）──────────── */

const toggleDlg = ref<{ row: TakerAppTableRow; to: 'enabled' | 'disabled' } | null>(null)
const toggling = ref(false)

function confirmToggle(row: TakerAppTableRow): void {
  toggleDlg.value = { row, to: row.state === 'enabled' ? 'disabled' : 'enabled' }
}

function doToggle(): Promise<void> {
  const dlg = toggleDlg.value
  if (dlg === null || toggling.value) return Promise.resolve()
  toggling.value = true
  return updateTakerApp(dlg.row.id, { state: dlg.to })
    .then(async () => {
      toggleDlg.value = null
      await reload()
      showToast(`已${dlg.to === 'disabled' ? '停用' : '启用'}「${dlg.row.name}」`)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      toggling.value = false
    })
}

/* ── 轮换密钥（立即失效确认；确认里必须写出「旧密钥立刻作废」）──────────────────── */

const rotateDlg = ref<{ row: TakerAppTableRow } | null>(null)
const rotating = ref(false)

function confirmRotate(row: TakerAppTableRow): void {
  rotateDlg.value = { row }
}

function doRotate(): Promise<void> {
  const dlg = rotateDlg.value
  if (dlg === null || rotating.value) return Promise.resolve()
  rotating.value = true
  return rotateTakerAppSecret(dlg.row.id)
    .then(async () => {
      rotateDlg.value = null
      await reload()
      showToast(`已轮换「${dlg.row.name}」的密钥（新密钥在列表里）`)
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      rotating.value = false
    })
}

/* ── 「接入文档」弹层（island taker 渲染链照抄：renderApiDoc / htmlToText / slugify）─ */

/** 构建期常量走函数取（压缩器会把整份文档内联进调用点 ⇒ 产物存两份）。 */
function takerApiDocText(): string {
  return takerApiDocSource
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

function htmlToText(html: string): string {
  return html
    .replace(/<[^>]*>/g, '')
    .replace(/&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);/g, (whole, body: string) => {
      if (body.startsWith('#')) {
        const hex = body[1] === 'x' || body[1] === 'X'
        const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10)
        return Number.isFinite(code) ? String.fromCodePoint(code) : whole
      }
      const named = NAMED_ENTITIES[body]
      return named === undefined ? whole : named
    })
}

function slugifyHeading(text: string): string {
  const slug = text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/-+$/g, '')
  return slug === '' ? 'section' : slug
}

type DocHeading = { depth: number; text: string; id: string }
type RenderedDoc = { html: string; headings: DocHeading[] }

const DOC_HEADING_RE = /<h([1-6])>([\s\S]*?)<\/h\1>/g

function renderApiDoc(source: string): RenderedDoc {
  const marked = new Marked({ gfm: true })
  const headings: DocHeading[] = []
  const html = marked
    .parse(source, { async: false })
    .replace(DOC_HEADING_RE, (_whole, depthText: string, inner: string) => {
      const depth = Number(depthText)
      const text = htmlToText(inner).trim()
      const id = `taDocSection-${headings.length + 1}-${slugifyHeading(text)}`
      headings.push({ depth, text, id })
      return `<h${depth} id="${id}">${inner}</h${depth}>`
    })
  return { html, headings }
}

let docCache: RenderedDoc | null = null

function takerApiDoc(): RenderedDoc {
  if (docCache === null) docCache = renderApiDoc(takerApiDocText())
  return docCache
}

const docDlgOpen = ref(false)
const docBodyRef = ref<HTMLElement | null>(null)
const docActiveAnchor = ref('')

const doc = computed(() => (docDlgOpen.value ? takerApiDoc() : null))

const tocItems = computed(() =>
  (doc.value?.headings ?? []).filter((h) => h.depth === 2 || h.depth === 3),
)

function jumpToDocSection(anchor: string): void {
  docActiveAnchor.value = anchor
  const body = docBodyRef.value
  if (body === null) return
  const target = body.querySelector(`[id="${anchor}"]`)
  if (target === null) return
  for (const el of body.querySelectorAll('.ta-doc-active')) el.classList.remove('ta-doc-active')
  target.classList.add('ta-doc-active')
  if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start' })
}

function onCopyDoc(): void {
  copyTextToClipboard(takerApiDocText(), (ok) => showToast(ok ? DOC_COPY_DONE : DOC_COPY_FAILED))
}

function onDownloadDoc(): void {
  if (!downloadTextFile(DOC_DOWNLOAD_FILENAME, takerApiDocText())) {
    showToast(DOC_DOWNLOAD_FAILED)
  }
}

/** 把一段文本存成文件（island downloadTextFile 逐段照抄；`false` = 环境存不了文件）。 */
function downloadTextFile(filename: string, text: string): boolean {
  const create = URL.createObjectURL
  if (typeof create !== 'function') return false
  const url = create.call(URL, new Blob([text], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url)
  return true
}

function onRefresh(): void {
  void reload().then((ok) => {
    if (ok) showToast('已刷新')
  })
}

onMounted(() => {
  // 页内门（与路由那道门同源；iframe 深链场景的兜底，照旧页 mount 自检）。
  if (!session.canNode([TAKER_APP_NODE])) {
    denied.value = true
    showToast(NO_ACCESS_MESSAGE)
    return
  }
  // 身份那一半：与后端 `_is_system_admin` 同一个谓词（canNode 的管理员短路同源）。
  isSystemAdmin.value = session.canNode([SYSTEM_ADMIN_NODE])
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="taker-apps">
    <template v-if="!denied">
      <!-- 顶部告知 quote（能力边界 / 静默变化 / 只能停用不能删 / 身份范围，逐字） -->
      <blockquote class="cv2-note ta-quote" data-v2="quote">
        <b>打手对接</b>：给<b>打手</b>（用别家系统的外部工作室）一套 <b>AppKey / AppSecret</b>，
        让他调用本系统的对外接口（拉抢单大厅 / 抢单 / 查他的单 / 改他单的状态 / 传进度图）。
        <br />
        <span class="ta-warn ta-strong" data-v2="notice-authority">
          这套凭据的能力 = <b>绑定的那个打手能看什么</b>：拿到密钥的一方可以以他的身份拉大厅、抢单、查他的单、改他单的状态、传进度图。
        </span>
        <br />
        <span class="ta-warn" data-v2="notice-role">
          ⚠️ 绑定账号的职位 / 权限节点一改，这套外部凭据的能力就<b>静默跟着变</b>（范围判据与抢单判据都取自那一行）——改完记得回来看一眼。
        </span>
        <br />
        <span class="ta-dim">凭据<b>只能停用、不能删除</b>（停用是唯一的关闭动作，秒级生效）；密钥<b>没有宽限期</b>，轮换后旧密钥立刻作废。</span>
        <br />
        <span class="ta-dim" data-v2="notice-scope">
          <template v-if="isSystemAdmin">你是系统管理员：这里能看到<b>全部</b>打手侧凭据；新建时必须<b>指定它绑定哪个打手账号</b>（范围判据与抢单判据都从那一个账号算，spec §6）。</template>
          <template v-else>这里只会出现<b>你自己的</b>接入凭据：新建出来的应用自动绑在你名下（不需要、也不允许选别人）。</template>
        </span>
      </blockquote>

      <!-- 工具栏：新增打手凭据 / 刷新 / 接入文档 -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="add" @click="openCreate">新增打手凭据</button>
        <button type="button" class="cv2-btn cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新</button>
        <button type="button" class="cv2-btn cv2-btn--tint-blue cv2-btn--find" data-v2="apidoc" @click="docDlgOpen = true">接入文档</button>
      </div>

      <!-- 主表 7 列：应用名 / AppKey / AppSecret 明文 / 绑定打手 / 状态 / 最近使用 / 操作四颗 -->
      <section class="cv2-panel cv2-table-panel" aria-label="打手对接凭据列表">
        <table class="cv2-table">
          <colgroup>
            <col style="width: 150px" />
            <col style="width: 190px" />
            <col />
            <col style="width: 140px" />
            <col style="width: 100px" />
            <col style="width: 170px" />
            <col style="width: 300px" />
          </colgroup>
          <thead>
            <tr>
              <th>应用名</th>
              <th>AppKey</th>
              <th>AppSecret</th>
              <th>绑定打手</th>
              <th class="cv2-th-center">状态</th>
              <th>最近使用</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td :title="row.name">{{ row.name }}</td>
              <td class="ta-mono">{{ row.app_key }}</td>
              <td><span class="ta-mono ta-secret">{{ row.app_secret }}</span></td>
              <td>{{ row.taker_name }}</td>
              <td class="cv2-td-center">
                <span class="cv2-st" :class="row.state === 'enabled' ? 'cv2-st--open' : 'cv2-st--error'" :data-v2-state="row.state">{{ row.state_text }}</span>
              </td>
              <td class="cv2-td-time">{{ lastUsedText(row.last_used_at) }}</td>
              <td class="cv2-td-center ta-actions">
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-callback="row.id" @click="openCallback(row)">回调与推送</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--tint-blue cv2-btn--quiet" :data-v2-copy="row.id" @click="onCopySecret(row)">复制密钥</button>
                <button type="button" class="cv2-btn cv2-btn--sm" :data-v2-toggle="row.id" @click="confirmToggle(row)">{{ row.state_action_label }}</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger cv2-btn--commit" :data-v2-rotate="row.id" @click="confirmRotate(row)">轮换密钥</button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td colspan="7" class="cv2-empty" data-v2="empty">
                <template v-if="loading">正在加载…</template>
                <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
                <template v-else>{{ EMPTY_TABLE_TEXT }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <div v-else class="cv2-empty" data-v2="denied">{{ NO_ACCESS_MESSAGE }}</div>

    <!-- 新建弹层（管理员面孔带「绑定打手」选择器；打手面孔不渲染选择器——锁死为自己） -->
    <el-dialog
      :model-value="createDlg !== null"
      :title="isSystemAdmin ? '新增打手凭据（需指定绑定的打手账号）' : '新增打手凭据'"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-create"
      @update:model-value="createDlg = null"
    >
      <template v-if="createDlg !== null">
        <div class="cv2-field">
          <span class="cv2-field-label">应用名</span>
          <el-input
            v-model="createDlg.name"
            :maxlength="50"
            placeholder="给这套凭据起的名字，例如：对接方甲"
            data-v2="create-name"
            @keydown.enter="void submitCreate()"
          />
        </div>
        <div v-if="isSystemAdmin" class="cv2-field" data-v2="taker-picker">
          <span class="cv2-field-label">绑定打手</span>
          <el-select
            v-model="takerId"
            placeholder="请选择要绑定的打手账号"
            clearable
            filterable
            :loading="candidatesLoading"
            data-v2="taker-select"
          >
            <el-option v-for="c in candidates" :key="c.id" :value="c.id" :label="candidateLabel(c)" />
          </el-select>
          <span class="cv2-field-hint cv2-field-hint--warn" data-v2="picker-hint">{{ pickerHint }}</span>
        </div>
        <p class="cv2-field-hint" data-v2="create-hint">
          创建成功后系统会生成 AppKey 与 AppSecret（<b>密钥在列表里随时可以查看与复制</b>）。
        </p>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="create-save" :disabled="savingCreate" @click="void submitCreate()">创建</button>
        </div>
      </template>
    </el-dialog>

    <!-- 回调与推送面板（两键一起 PATCH；台账懒加载——点开折叠区才拉） -->
    <el-dialog
      :model-value="callbackDlg !== null"
      :title="callbackDlg === null ? '' : `回调与推送：${callbackDlg.row.name}`"
      class="cv2-dialog cv2-dialog--xl"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-callback"
      @update:model-value="callbackDlg = null; callbackAppId = null"
    >
      <template v-if="callbackDlg !== null">
        <p class="cv2-field-hint" data-v2="cb-app">应用：<b>{{ callbackDlg.row.name }}</b></p>
        <blockquote class="cv2-note ta-cb-notice" data-v2="cb-notice">
          推送怎么走：<b>你当前接手的单</b>发生状态变化 / 进度图新增 / 被删除 / 被改派人时，
          我们会按产生顺序 POST 到上面的地址，每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> /
          <b>X-TT-Signature</b> 三个头，对方要用 AppSecret 按我们的算法<b>验真</b>再处理（⛔ 不要只看来源 IP 就算数）。
          <br />⚠️ <b>大厅里来了新单不推</b>：想第一时间知道"有新单可抢"仍然要轮询大厅接口。
          <br />地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；
          推送失败会自动重试 6 次（10 秒 / 1 分 / 5 分 / 15 分 / 1 小时 / 6 小时），仍失败就落在这里，可以手工重投；
          因为是「至少一次」，对方要按 <b>X-TT-Delivery</b> 去重。
          <br />地址<b>只收 https 且必须是公网</b>：保存时与每次投递前各校验一次（DNS 会变），内网 / 回环 / 带用户名密码的地址会被拒。
        </blockquote>
        <div class="cv2-field">
          <span class="cv2-field-label">回调地址</span>
          <el-input v-model="callbackDlg.url" :maxlength="CALLBACK_URL_MAX_LENGTH" placeholder="https://接收推送的公网地址（留空 = 不推送）" data-v2="cb-url" />
          <span class="cv2-field-hint cv2-field-hint--warn" data-v2="cb-hint">{{ callbackHint }}</span>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">回调开关</span>
          <el-switch
            v-model="callbackDlg.enabled"
            inline-prompt
            active-text="启用"
            inactive-text="停用"
            data-v2="cb-switch"
          />
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="cb-save" :disabled="savingCallback" @click="saveCallbackSettings">保存回调设置</button>
        </div>
        <div class="ta-deliveries">
          <a href="#" class="cv2-panel-more" data-v2="deliveries-toggle" @click.prevent="toggleDeliveries">{{ deliveriesToggleLabel }}</a>
          <div v-if="deliveriesOpen" class="ta-deliveries-body" data-v2="deliveries-body">
            <p v-if="deliveriesError !== null" class="ta-warn" data-v2="deliveries-warn">{{ deliveriesError }}</p>
            <p v-else-if="deliveriesLoading" class="ta-dim">正在加载…</p>
            <p v-else-if="deliveries.length === 0" class="ta-dim" data-v2="deliveries-empty">还没有推送记录（这个应用没有产生过事件）。</p>
            <template v-else>
              <table class="cv2-table" data-v2="deliveries-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>事件</th>
                    <th>订单号</th>
                    <th>状态</th>
                    <th class="cv2-th-center">重试次数</th>
                    <th>失败原因</th>
                    <th class="cv2-th-center">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="d in deliveries" :key="d.id" class="cv2-row" :data-v2-delivery="d.id">
                    <td class="cv2-td-time">{{ timeText(d.created_at) }}</td>
                    <td>{{ d.event_text }}</td>
                    <td>
                      {{ orderMain(d) }}
                      <span v-if="orderSub(d) !== ''" class="ta-dim ta-order-sub">内置 {{ orderSub(d) }}</span>
                    </td>
                    <td>
                      <span class="cv2-st" :class="deliveryStatusClass(d)" :data-v2-delivery-status="d.status">{{ d.status_text }}</span>
                      <div v-if="d.status === 'pending' && d.next_attempt_at !== null" class="ta-dim">下次 {{ timeText(d.next_attempt_at) }}</div>
                    </td>
                    <td class="cv2-td-center">{{ d.attempts }}</td>
                    <td>{{ reasonText(d) }}</td>
                    <td class="cv2-td-center">
                      <button v-if="d.status === 'failed'" type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger cv2-btn--commit" :data-v2-retry="d.id" @click="confirmRetry(d.id)">重投</button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-if="deliveriesTotal > deliveries.length" class="ta-dim" data-v2="deliveries-more">共 {{ deliveriesTotal }} 条，这里只显示最近 {{ deliveries.length }} 条。</p>
            </template>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 重投二次确认（island confirmRetry 文案逐字） -->
    <el-dialog
      :model-value="retryDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-retry"
      @update:model-value="retryDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="retry-question">
        确定重投这一条推送吗（台账第 {{ retryDlg === null ? '' : retryDlg.deliveryPk }} 条）？我们会把它排回待投递队列，
        投递进程下一轮（约 10 秒内）重发。⚠️ 投递是「至少一次」：如果对方上次其实收到了，它会「再收到一次」（对方要按 X-TT-Delivery 去重）。
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="retryDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="retry-confirm" :disabled="retrying" @click="void doRetry()">确定</button>
      </div>
    </el-dialog>

    <!-- 停用/启用确认（island confirmToggle 文案逐字；标题「停用凭据/启用凭据」） -->
    <el-dialog
      :model-value="toggleDlg !== null"
      :title="toggleDlg === null ? '' : `${toggleDlg.to === 'disabled' ? '停用' : '启用'}凭据`"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-toggle"
      @update:model-value="toggleDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="toggle-question">
        确定要{{ toggleDlg === null ? '' : toggleDlg.to === 'disabled' ? '停用' : '启用' }}「{{ toggleDlg === null ? '' : toggleDlg.row.name }}」吗？
        <template v-if="toggleDlg !== null && toggleDlg.to === 'disabled'">停用后第三方<b>当场</b>调不通（秒级生效），但这套凭据与它的密钥都还在。</template>
        <template v-else-if="toggleDlg !== null">启用后第三方可以立刻继续用它调用接口。</template>
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="toggleDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="toggle-confirm" :disabled="toggling" @click="void doToggle()">确定</button>
      </div>
    </el-dialog>

    <!-- 轮换密钥确认（island confirmRotate 文案逐字；标题「轮换密钥」） -->
    <el-dialog
      :model-value="rotateDlg !== null"
      title="轮换密钥"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-rotate"
      @update:model-value="rotateDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="rotate-question">
        确定要轮换「{{ rotateDlg === null ? '' : rotateDlg.row.name }}」的密钥吗？旧密钥<b>立刻作废</b>（没有宽限期），正在用它的第三方<b>当场调不通</b>，必须马上换成新密钥。
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="rotateDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="rotate-confirm" :disabled="rotating" @click="void doRotate()">确定</button>
      </div>
    </el-dialog>

    <!-- 接入文档弹层（目录 + marked 正文 + 复制/下载两钮；文案逐字） -->
    <el-dialog
      v-model="docDlgOpen"
      title="接入文档（docs/TAKER-API.md）"
      class="cv2-dialog cv2-dialog--xl"
      destroy-on-close
      data-v2="dlg-doc"
    >
      <div class="ta-doc">
        <div class="ta-doc-bar">
          <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--primary" data-v2="doc-copy" @click="onCopyDoc">{{ DOC_COPY_BUTTON_TEXT }}</button>
          <button type="button" class="cv2-btn cv2-btn--sm" data-v2="doc-download" @click="onDownloadDoc">{{ DOC_DOWNLOAD_BUTTON_TEXT }}</button>
          <p class="cv2-field-hint" data-v2="doc-notice">
            内容就是仓库里的 <b>docs/TAKER-API.md</b>（渲染成网页版看更方便）：左边点目录跳小节，右边是正文。要原文就点上面两颗按钮（复制 / 下载）。
            ⚠️ 这份是给<b>用别家系统的外部工作室（打手身份）</b>的接入契约；文档改动要重新构建前端才会带上。
          </p>
        </div>
        <div class="ta-doc-cols">
          <nav v-if="doc !== null" class="ta-doc-toc" data-v2="doc-toc">
            <a
              v-for="h in tocItems"
              :key="h.id"
              href="#"
              class="ta-doc-toc-item"
              :class="{ 'ta-doc-toc-item--sub': h.depth === 3, 'ta-doc-active': docActiveAnchor === h.id }"
              :data-doc-anchor="h.id"
              @click.prevent="jumpToDocSection(h.id)"
            >{{ h.text }}</a>
          </nav>
          <!-- 仓内受控 md 的 marked 产物（XSS 面照旧页口径，渲染链不改造；⛔ 不走 eval）。 -->
          <div v-if="doc !== null" ref="docBodyRef" class="ta-doc-body" data-v2="doc-body" v-html="doc.html" />
        </div>
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

.ta-quote {
  line-height: 26px;
}

.ta-warn {
  color: var(--tt2-money-red);
}

.ta-strong {
  font-weight: 700;
}

.ta-dim {
  color: var(--tt2-dim);
}

.ta-mono {
  font-family: var(--tt2-mono);
  font-variant-numeric: tabular-nums;
}

.ta-secret {
  word-break: break-all;
}

td.ta-actions {
  white-space: nowrap;
}

td.ta-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

.ta-cb-notice {
  line-height: 26px;
}

.ta-deliveries {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--tt2-line);
}

.ta-deliveries-body {
  margin-top: 6px;
}

.ta-order-sub {
  display: block;
  font-size: 12px;
}

.ta-doc-bar {
  padding-bottom: 8px;
  border-bottom: 1px solid var(--tt2-line);
}

.ta-doc-bar .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

.ta-doc-cols {
  display: flex;
  align-items: flex-start;
  margin-top: 8px;
}

.ta-doc-toc {
  flex: 0 0 250px;
  max-width: 250px;
  max-height: 60vh;
  overflow: auto;
  padding-right: 10px;
  border-right: 1px solid var(--tt2-line);
}

.ta-doc-toc-item {
  display: block;
  padding: 1px 4px;
  border-radius: 2px;
  color: var(--tt2-ink);
  text-decoration: none;
  line-height: 22px;
}

.ta-doc-toc-item--sub {
  padding-left: 18px;
}

.ta-doc-toc-item:hover {
  color: var(--tt2-primary-deep);
  background: var(--tt2-accent-box-bg);
}

.ta-doc-toc-item.ta-doc-active {
  background: var(--tt2-primary);
  color: var(--tt2-panel);
}

.ta-doc-body {
  flex: 1 1 auto;
  max-height: 60vh;
  overflow: auto;
  padding: 8px 16px;
}

.ta-doc-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}

.ta-doc-body :deep(th),
.ta-doc-body :deep(td) {
  border: 1px solid var(--tt2-line);
  padding: 4px 8px;
  font-size: 12px;
  vertical-align: top;
}

.ta-doc-body :deep(th) {
  background: var(--tt2-label-bg);
  text-align: left;
}

.ta-doc-body :deep(pre) {
  background: var(--tt2-label-bg);
  border: 1px solid var(--tt2-line);
  padding: 8px;
  overflow: auto;
  font-size: 12px;
}

.ta-doc-body :deep(code) {
  font-family: var(--tt2-mono);
}

.ta-doc-body :deep(h1) {
  font-size: 22px;
}

.ta-doc-body :deep(h2) {
  font-size: 17px;
  margin: 20px 0 8px;
  border-bottom: 1px solid var(--tt2-line);
  padding-bottom: 4px;
}

.ta-doc-body :deep(h3) {
  font-size: 14px;
  margin: 14px 0 6px;
}

.ta-doc-body :deep(blockquote) {
  border-left: 4px solid var(--tt2-primary);
  background: var(--tt2-accent-box-bg);
  margin: 8px 0;
  padding: 6px 10px;
}

.ta-doc-body :deep(.ta-doc-active) {
  background: var(--tt2-st-unsettled-bg);
}

/* 全局提示 = 对标 layer.msg 居中深条（V2Notices 同款）。 */
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
