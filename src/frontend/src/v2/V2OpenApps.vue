<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「应用管理」v2（`/v2/open-apps`；2026-11-06 开放平台批，计划 §3）。
 * 旧页 = island 自建页 `layui-island/pages/open-apps.ts`（自有功能，无对标物）。
 * 六块全保留：①列表四钮（回调与推送/复制密钥/停用|启用/轮换密钥；⛔ 无删除无权限钮）+
 * 列表上方告知 quote 逐字；②新建弹层（唯一字段应用名 → 建完弹密钥层）；③回调与推送弹层
 * （两键一起 PATCH、台账 20/200、failed 重投二次确认、缺键降级中止、保存回报三分支）；
 * ④停用/启用确认；⑤轮换密钥确认（含「旧密钥立刻作废」）；⑥「API 文档」弹层
 * （`src/docs/open-api.md` ?raw + marked + 目录 + 复制/下载）。页内门照旧页：
 * `is_account_admin !== true || !canNode(APP_MANAGE_NODE)` → toast。
 * ⛔ 不 import island 页模块：密钥/回调/剪贴板等 helper **逐段照抄**旧页（⛔ 凭记忆重写）；
 * 文档渲染链照旧页口径（仓内受控 md、v-html 只用于这份产物）。
 */
import { computed, onMounted, ref } from 'vue'
import { Marked } from 'marked'
// ⭐ 原文 = `docs/OPEN-API.md` 的逐字节副本 `src/docs/open-api.md`（改了出处要重跑 cp，同旧页）。
import openApiDocSource from '../docs/open-api.md?raw'
import {
  APP_MANAGE_NODE,
  createApiApp,
  listApiApps,
  listDeliveries,
  retryDelivery,
  rotateApiAppSecret,
  updateApiApp,
  type ApiAppCallbackState,
  type ApiAppRow,
  type ApiDeliveryRow,
} from '@/api/openApps'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案与常量（出处 = island 页同语义常量；本地重写不引模块）────────────── */

/** §4-V2 的代价必须在页上说破（island FULL_TENANT_NOTICE / COST_NOTICE / PII_NOTICE 逐字）。 */
const FULL_TENANT_NOTICE =
  '本应用可访问本租户全部业务数据（一个应用 = 整个租户，没有「只给几张单」这种收窄）：' +
  '拿到密钥的人能查单、改单、改状态、改发单价，也能读到成本与提成。'
const COST_NOTICE =
  '第三方查单时能看到这张单的成本与提成（打手 / 客服的预计、生效、发单价与提成率）。'
const PII_NOTICE =
  '同时，第三方也会看到客户联系方式（电话 / QQ微信 / 旺旺）、玩家账号 / 子账号 / 角色名与订单原文' +
  '（客户填写的内容不由我方清洗）。'

/** 密钥弹层顶部提示（island SECRET_PANEL_NOTICE 逐字；⛔ 不许出现「只显示一次」——列表里随时看得到）。 */
const SECRET_PANEL_NOTICE =
  '密钥随时可以在列表里查看与复制（关闭本窗口不会丢失）。⛔ 这是凭据：不要贴到聊天群 / 工单 / 截图里。'

const NO_ACCESS_MESSAGE = '无权限管理开放平台应用'

const MESSAGES = {
  noName: '请填写应用名',
  failed: '操作失败',
  /** 行载荷缺回调两键（部署面降级分支）：必须说出来并中止，⛔ 不当「没配回调」显示成空。 */
  callbackFieldsMissing:
    '后端这一次没有回这个应用的回调配置（只回了别的字段），为避免把现有配置覆盖成空，' +
    '这一次不打开设置面板。请刷新页面重试；若一直如此，说明前端与后端版本不一致。',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中列表里的密钥复制',
  copyDocDone: '已复制整份 API 文档的 Markdown 原文（可以直接粘进编辑器或喂给 AI）。',
  copyDocFailed: '复制失败（浏览器不允许自动复制）——请改用上面的「下载 .md」把文件存下来。',
  docDownloadFailed:
    '这个浏览器不支持直接下载——原文在仓库的 docs/OPEN-API.md 里，直接打开那个文件即可。',
} as const

/** 回调地址的即时提示（island CALLBACK_URL_HINT 逐字；它是反馈不是判据，真判据在后端）。 */
const CALLBACK_URL_HINT =
  '回调地址只接受 https 开头的地址（http 与其它协议一律不收；内网地址后端也会拒）。'

/** 「最近推送记录」折叠链接两句（textContent 语义 → 纯文本）。 */
const DELIVERIES_TOGGLE_CLOSED = '最近推送记录（点这里展开）'
const DELIVERIES_TOGGLE_OPEN = '最近推送记录（点这里收起）'

/** API 文档弹层的文案（island DOC_* 逐字）。 */
const DOC_COPY_BUTTON_TEXT = '复制 Markdown 原文'
const DOC_DOWNLOAD_BUTTON_TEXT = '下载 .md'
const DOC_DOWNLOAD_FILENAME = 'OPEN-API.md'

const EMPTY_TABLE_TEXT = '还没有应用；点上面的「新增应用」建一个'

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

/* ── 页内门（照旧页 mount 自检：路由两道门在深链/刷新场景之外的兜底）────────────── */

const denied = ref(false)

/* ── 列表（island decorateRows / stateActionLabel / timeText / lastUsedText 同式）─ */

/** 那颗状态按钮的文案（判据打在机器值 state 上，⛔ 不用 state_text）。 */
function stateActionLabel(row: ApiAppRow): string {
  return row.state === 'enabled' ? '停用' : '启用'
}

type OpenAppTableRow = ApiAppRow & { state_action_label: string }

const rows = ref<OpenAppTableRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

/** ISO 时刻显示形态：`YYYY-MM-DD HH:mm:ss`；null/空 ⇒ `—`（⛔ 不是空白格）。 */
function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/** 「最近使用」：null = 一次都没验签成功过，照实说（island lastUsedText 逐字）。 */
function lastUsedText(value: string | null): string {
  if (value === null || value === '') return '—（还没被调用过）'
  return timeText(value)
}

async function reload(): Promise<boolean> {
  loading.value = true
  try {
    const list = await listApiApps()
    // 装配唯一出口：状态按钮文案在组装行这一刻算好（island decorateRows 同式）。
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

/* ── 剪贴板与文件（island copyCredentialText / copyTextToClipboard / downloadTextFile
 *    逐段照抄：缺 clipboard / createObjectURL 不是静默失败）────────────────────── */

function copyCredentialText(row: ApiAppRow): string {
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

/** 把一段文本存成文件；`false` = 这个环境没有 `URL.createObjectURL`（jsdom/旧浏览器）。 */
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

function onCopySecret(row: ApiAppRow): void {
  // 复制的内容从行数据取（⛔ 不从 DOM 读：密钥不在操作格里固化为属性）。
  copyTextToClipboard(copyCredentialText(row), (ok) =>
    showToast(ok ? `已复制「${row.name}」的 AppKey 与 AppSecret` : MESSAGES.copyFailed),
  )
}

/* ── 新建弹层与密钥弹层（island openCreate / openSecret 同式）─────────────────── */

const createDlg = ref<{ name: string } | null>(null)
const savingCreate = ref(false)
const secretDlg = ref<{ title: string; name: string; appKey: string; appSecret: string } | null>(null)

function openCreate(): void {
  createDlg.value = { name: '' }
}

/** 建成 / 轮换后把 AppKey 与 AppSecret 一起摆出来（不是「唯一能看到的地方」——列表里就有）。 */
function openSecret(row: ApiAppRow, title: string): void {
  secretDlg.value = { title, name: row.name, appKey: row.app_key, appSecret: row.app_secret }
}

function submitCreate(): Promise<void> {
  const dlg = createDlg.value
  if (dlg === null || savingCreate.value) return Promise.resolve()
  const name = dlg.name.trim()
  if (name === '') {
    showToast(MESSAGES.noName)
    return Promise.resolve()
  }
  savingCreate.value = true
  return createApiApp(name)
    .then(async (saved) => {
      createDlg.value = null
      await reload()
      openSecret(saved, '应用已创建：这是它的密钥')
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      savingCreate.value = false
    })
}

/* ── 回调与推送弹层（island openCallback / saveCallbackSettings / callbackUrlHint /
 *    callbackSaveReport 同式；两键一起 PATCH；缺键降级中止）─────────────────────── */

const callbackDlg = ref<{
  row: ApiAppRow
  url: string
  enabled: boolean
} | null>(null)
const savingCallback = ref(false)
/** 回调面板此刻对着哪个应用（也用来丢弃迟到的台账响应——island callbackApp 对账同式）。 */
const callbackAppId = ref<number | null>(null)

/** 回调地址的即时提示（纯函数照抄 island callbackUrlHint：空串合法、https 开头合法）。 */
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

/** 保存回报三分支（island callbackSaveReport 逐字：对着真实后果说，⛔ 不许「保存成功」了事）。 */
function callbackSaveReport(url: string, state: ApiAppCallbackState): string {
  if (url.trim() === '') {
    return '已保存：回调地址为空 ⇒ 不会推送任何事件（不排队、不重试、也不报错）。'
  }
  if (state === 'disabled') {
    return '已保存：回调开关是「停用」⇒ 不会推送任何事件（地址留着，随时可以再打开）。'
  }
  return '已保存：推送已打开；下面「最近推送记录」里失败的行可以手工重投。'
}

function openCallback(row: ApiAppRow): void {
  // 部署面降级分支：行载荷缺回调两键就中止打开（显示成空会让保存覆盖真实配置）。
  if (!('callback_url' in row) || !('callback_state' in row)) {
    showToast(MESSAGES.callbackFieldsMissing)
    return
  }
  callbackDlg.value = { row, url: row.callback_url, enabled: row.callback_state === 'enabled' }
  // 台账回到收起态、清掉上一个应用的痕迹。
  deliveriesOpen.value = false
  deliveries.value = []
  deliveriesTotal.value = 0
  deliveriesError.value = null
  callbackAppId.value = row.id
  void loadDeliveries(row.id)
}

function saveCallbackSettings(): void {
  const dlg = callbackDlg.value
  if (dlg === null || savingCallback.value) return
  const app = dlg.row
  const state: ApiAppCallbackState = dlg.enabled ? 'enabled' : 'disabled'
  const url = dlg.url.trim()
  savingCallback.value = true
  // 两个键一起发（地址与开关是同一份配置，分开发会造出没人选过的中间态）。
  void updateApiApp(app.id, { callback_url: url, callback_state: state })
    .then(async () => {
      await reload()
      showToast(callbackSaveReport(url, state))
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      savingCallback.value = false
    })
}

/* ── 推送台账（island loadDeliveries / deliveriesHtml / deliveryRowHtml 同式；
 *    打开面板即拉，20/200，total 大于条数如实说）────────────────────────────── */

const DELIVERIES_PAGE_SIZE = 20

const deliveriesOpen = ref(false)
const deliveries = ref<ApiDeliveryRow[]>([])
const deliveriesTotal = ref(0)
const deliveriesError = ref<string | null>(null)
const deliveriesLoading = ref(false)

const deliveriesToggleLabel = computed(() =>
  deliveriesOpen.value ? DELIVERIES_TOGGLE_OPEN : DELIVERIES_TOGGLE_CLOSED,
)

/** 「失败原因」格：空串 → `—`；否则原文 +（HTTP 码，看得到才算数）。 */
function reasonText(d: ApiDeliveryRow): string {
  if (d.reason === '') return '—'
  return d.reason + (d.response_status === null ? '' : `（HTTP ${d.response_status}）`)
}

/** 主行 = 展示单号（来源号非空取它、空回退内置号）；副行只在两号不同时标注「内置 X」。 */
function orderMain(d: ApiDeliveryRow): string {
  return d.source_order_no || d.order_no
}

function orderSub(d: ApiDeliveryRow): string {
  return d.source_order_no !== '' && d.source_order_no !== d.order_no ? d.order_no : ''
}

/** 台账行状态签的档位（色走 token：done=doing 绿、failed=error 红、pending=paused 琥珀）。 */
function deliveryStatusClass(d: ApiDeliveryRow): string {
  return d.status === 'done' ? 'cv2-st--open' : d.status === 'failed' ? 'cv2-st--error' : 'cv2-st--paused'
}

async function loadDeliveries(pk: number): Promise<void> {
  deliveriesError.value = null
  deliveriesLoading.value = true
  try {
    const page = await listDeliveries(pk, { page: 1, page_size: DELIVERIES_PAGE_SIZE })
    // 迟到响应丢弃：面板可以关掉再开另一个应用（island callbackApp?.id 对账同式）。
    if (callbackAppId.value !== pk) return
    deliveries.value = page.items
    deliveriesTotal.value = page.total
  } catch (e) {
    if (callbackAppId.value !== pk) return
    deliveries.value = []
    deliveriesTotal.value = 0
    // 失败如实显示，⛔ 不渲染成「还没有推送记录」（「一条都没有」≠「没取到」）。
    deliveriesError.value = errText(e)
  } finally {
    deliveriesLoading.value = false
  }
}

function toggleDeliveries(): void {
  deliveriesOpen.value = !deliveriesOpen.value
}

/* ── 重投（island confirmRetry 同式：只 failed 行可点；二次确认；成功回报逐字）───── */

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
  return retryDelivery(app.id, dlg.deliveryPk)
    .then(async () => {
      retryDlg.value = null
      deliveriesOpen.value = true
      await loadDeliveries(app.id)
      showToast('已重投：这一条回到了待投递队列，下一轮投递就会发出去。')
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
    .finally(() => {
      retrying.value = false
    })
}

/* ── 停用 / 启用（island confirmToggle 同式；⛔ 无删除——后端 405）──────────────── */

const toggleDlg = ref<{ row: OpenAppTableRow; to: 'enabled' | 'disabled' } | null>(null)

const TOGGLE_TAILS: Record<'enabled' | 'disabled', (name: string) => string> = {
  disabled: (name) =>
    `确定停用「${name}」吗？停用后第三方当场调不通（所有调用回「该应用已停用」），回调推送也一起停。`,
  enabled: (name) =>
    `确定启用「${name}」吗？启用后第三方就能调它的接口了（推送是否发得出去还要看回调地址与开关）。`,
}

function confirmToggle(row: OpenAppTableRow): void {
  toggleDlg.value = { row, to: row.state === 'enabled' ? 'disabled' : 'enabled' }
}

function doToggle(): Promise<void> {
  const dlg = toggleDlg.value
  if (dlg === null) return Promise.resolve()
  return updateApiApp(dlg.row.id, { state: dlg.to })
    .then(async () => {
      toggleDlg.value = null
      await reload()
      showToast(dlg.to === 'disabled' ? '已停用' : '已启用')
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
}

/* ── 轮换密钥（island confirmRotate 同式：确认必须含「旧密钥立刻作废」、
 *    ⛔ 不含「只显示一次」）──────────────────────────────────────────────────── */

const rotateDlg = ref<{ row: OpenAppTableRow } | null>(null)

function confirmRotate(row: OpenAppTableRow): void {
  rotateDlg.value = { row }
}

function doRotate(): Promise<void> {
  const dlg = rotateDlg.value
  if (dlg === null) return Promise.resolve()
  return rotateApiAppSecret(dlg.row.id)
    .then(async (saved) => {
      rotateDlg.value = null
      await reload()
      openSecret(saved, '新密钥（已生效）')
    })
    .catch((e: unknown) => showToast(errText(e), 'error'))
}

/* ── 「API 文档」弹层（island renderApiDoc / docTocHtml / htmlToText / slugifyHeading
 *    逐段照抄：目录与锚点从同一次 marked 渲染的产物派生；⛔ 不走 eval）────────────── */

/** 构建期常量走函数取（压缩器会把整份文档内联进调用点 ⇒ 产物存两份，island openApiDocText 同款）。 */
function openApiDocText(): string {
  return openApiDocSource
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

/** 一小段 HTML → 纯文本（先去标签再解实体，一趟正则；island htmlToText 逐段照抄）。 */
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

/** 标题文字 → 锚点 slug（唯一性由序号保证；island slugifyHeading 逐段照抄）。 */
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

/** 全份只跑一次 marked，并给每个标题补唯一锚点 id（island renderApiDoc 逐段照抄）。 */
function renderApiDoc(source: string): RenderedDoc {
  const marked = new Marked({ gfm: true })
  const headings: DocHeading[] = []
  const html = marked
    .parse(source, { async: false })
    .replace(DOC_HEADING_RE, (_whole, depthText: string, inner: string) => {
      const depth = Number(depthText)
      const text = htmlToText(inner).trim()
      const id = `oaDocSection-${headings.length + 1}-${slugifyHeading(text)}`
      headings.push({ depth, text, id })
      return `<h${depth} id="${id}">${inner}</h${depth}>`
    })
  return { html, headings }
}

let docCache: RenderedDoc | null = null

function apiDoc(): RenderedDoc {
  if (docCache === null) docCache = renderApiDoc(openApiDocText())
  return docCache
}

const docDlgOpen = ref(false)
const docBodyRef = ref<HTMLElement | null>(null)
const docActiveAnchor = ref('')

const doc = computed(() => (docDlgOpen.value ? apiDoc() : null))

/** 目录条目：只用 `##` 与 `###` 两级（island docTocHtml 同口径；`###` 缩进一级）。 */
const tocItems = computed(() =>
  (doc.value?.headings ?? []).filter((h) => h.depth === 2 || h.depth === 3),
)

/** 点目录跳小节：判据是标题上那个 id（与目录项同串）；jsdom 没有 scrollIntoView 要让过。 */
function jumpToDocSection(anchor: string): void {
  docActiveAnchor.value = anchor
  const body = docBodyRef.value
  if (body === null) return
  const target = body.querySelector(`[id="${anchor}"]`)
  if (target === null) return
  for (const el of body.querySelectorAll('.oa-doc-active')) el.classList.remove('oa-doc-active')
  target.classList.add('oa-doc-active')
  if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'start' })
}

function onCopyDoc(): void {
  copyTextToClipboard(openApiDocText(), (ok) =>
    showToast(ok ? MESSAGES.copyDocDone : MESSAGES.copyDocFailed),
  )
}

function onDownloadDoc(): void {
  // `false` = 这个环境存不了文件 ⇒ 说人话并给出路（⛔ 不是点了没反应）。
  if (!downloadTextFile(DOC_DOWNLOAD_FILENAME, openApiDocText())) {
    showToast(MESSAGES.docDownloadFailed)
  }
}

function onRefresh(): void {
  void reload().then((ok) => {
    if (ok) showToast('已刷新')
  })
}

onMounted(() => {
  // 页内门（与路由两道门同源；island mount 自检同式）。
  if (session.isAccountAdmin !== true || !session.canNode([APP_MANAGE_NODE])) {
    denied.value = true
    showToast(NO_ACCESS_MESSAGE)
    return
  }
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="open-apps">
    <template v-if="!denied">
      <!-- 列表上方告知 quote（§4-V2 代价的显式告知；island buildHtml 逐字，⛔ 不删不挪弹层） -->
      <blockquote class="cv2-note oa-quote" data-v2="quote">
        开放平台应用：给第三方一套 <b>AppKey / AppSecret</b>，让它调用本系统的对外接口
        （查单 / 改单 / 改状态 / 上传进度图）。
        <br />
        <span class="oa-warn oa-strong" data-v2="notice-full-tenant">{{ FULL_TENANT_NOTICE }}</span>
        <br />
        <span class="oa-warn" data-v2="notice-cost">{{ COST_NOTICE }}</span>
        <br />
        <span class="oa-warn" data-v2="notice-pii">{{ PII_NOTICE }}</span>
        <br />
        <span class="oa-dim">应用<b>只能停用、不能删除</b>（停用是唯一的关闭动作，秒级生效）；密钥<b>没有宽限期</b>，轮换后旧密钥立刻作废。</span>
        <br />
        <span class="oa-dim">推送按应用各配一份：在「回调与推送」里填<b>只收 https</b> 的地址；地址为空或开关停用就<b>不会推送</b>（下面「最近推送记录」里能看到发出去没有）。</span>
      </blockquote>

      <!-- 工具栏：新增应用 / 刷新 / API 文档（恰好三颗，照旧页 toolbar） -->
      <div class="cv2-toolbar" data-v2="toolbar">
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="add" @click="openCreate">新增应用</button>
        <button type="button" class="cv2-btn cv2-btn--find" data-v2="refresh" :disabled="loading" @click="onRefresh">刷新</button>
        <button type="button" class="cv2-btn cv2-btn--tint-blue cv2-btn--find" data-v2="apidoc" @click="docDlgOpen = true">API 文档</button>
      </div>

      <!-- 主表 6 列：应用名 / AppKey / AppSecret 明文 / 状态(停用标红) / 最近使用 / 操作恰好四颗 -->
      <section class="cv2-panel cv2-table-panel" aria-label="开放平台应用列表">
        <table class="cv2-table">
          <colgroup>
            <col style="width: 150px" />
            <col style="width: 190px" />
            <col />
            <col style="width: 100px" />
            <col style="width: 170px" />
            <col style="width: 300px" />
          </colgroup>
          <thead>
            <tr>
              <th>应用名</th>
              <th>AppKey</th>
              <th>AppSecret</th>
              <th class="cv2-th-center">状态</th>
              <th>最近使用</th>
              <th class="cv2-th-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td :title="row.name">{{ row.name }}</td>
              <td class="oa-mono">{{ row.app_key }}</td>
              <td><span class="oa-mono oa-secret">{{ row.app_secret }}</span></td>
              <td class="cv2-td-center">
                <span class="cv2-st" :class="row.state === 'enabled' ? 'cv2-st--open' : 'cv2-st--error'" :data-v2-state="row.state">{{ row.state_text }}</span>
              </td>
              <td class="cv2-td-time">{{ lastUsedText(row.last_used_at) }}</td>
              <td class="cv2-td-center oa-actions">
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-callback="row.id" @click="openCallback(row)">回调与推送</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--tint-blue cv2-btn--quiet" :data-v2-copy="row.id" @click="onCopySecret(row)">复制密钥</button>
                <button type="button" class="cv2-btn cv2-btn--sm" :data-v2-toggle="row.id" @click="confirmToggle(row)">{{ row.state_action_label }}</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger cv2-btn--commit" :data-v2-rotate="row.id" @click="confirmRotate(row)">轮换密钥</button>
              </td>
            </tr>
            <tr v-if="rows.length === 0">
              <td colspan="6" class="cv2-empty" data-v2="empty">
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

    <!-- ② 新建弹层：唯一字段应用名（maxlength 50 + placeholder 逐字） -->
    <el-dialog
      :model-value="createDlg !== null"
      title="新增开放平台应用"
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
            placeholder="给这个第三方起的名字，例如：对接方甲"
            data-v2="create-name"
            @keydown.enter="void submitCreate()"
          />
        </div>
        <p class="cv2-field-hint" data-v2="create-hint">
          创建成功后系统会生成 AppKey 与 AppSecret（<b>密钥在列表里随时可以查看与复制</b>）；回调地址与开关建完再到「回调与推送」里配。
        </p>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="create-save" :disabled="savingCreate" @click="void submitCreate()">创建应用</button>
        </div>
      </template>
    </el-dialog>

    <!-- ② 密钥层（创建/轮换共用；标题逐字传入；⛔ 无「只显示一次」措辞） -->
    <el-dialog
      :model-value="secretDlg !== null"
      :title="secretDlg === null ? '' : secretDlg.title"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-secret"
      @update:model-value="secretDlg = null"
    >
      <p class="oa-warn oa-strong" data-v2="secret-notice">{{ SECRET_PANEL_NOTICE }}</p>
      <template v-if="secretDlg !== null">
        <div class="cv2-field">
          <span class="cv2-field-label">应用</span>
          <span data-v2="secret-name">{{ secretDlg.name }}</span>
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">AppKey</span>
          <el-input :model-value="secretDlg.appKey" readonly data-v2="secret-key" />
        </div>
        <div class="cv2-field">
          <span class="cv2-field-label">AppSecret</span>
          <el-input :model-value="secretDlg.appSecret" type="textarea" :rows="3" readonly data-v2="secret-text" />
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--quiet" data-v2="secret-close" @click="secretDlg = null">关闭</button>
        </div>
      </template>
    </el-dialog>

    <!-- ③ 回调与推送弹层（旧页 90%×90% → 炭火 xl 档；两键一起 PATCH；台账即开即拉） -->
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
        <blockquote class="cv2-note oa-cb-notice" data-v2="cb-notice">
          推送怎么走：订单状态变化 / 订单删除 / 订单新增进度图这三类事件，我们会按产生顺序 POST 到上面的地址，
          每条都带 <b>X-TT-Timestamp</b> / <b>X-TT-Delivery</b> / <b>X-TT-Signature</b> 三个头，
          对方要用 AppSecret 按我们的算法<b>验真</b>再处理（⛔ 不要只看来源 IP 就算数）。
          <br />地址为空、或者开关是「停用」⇒ <b>不会推送</b>（不排队、不重试、也不报错）；
          推送失败会自动重试 6 次（10 秒 / 1 分 / 5 分 / 15 分 / 1 小时 / 6 小时），仍失败就落在这里，可以手工重投；
          因为是「至少一次」，对方要按 <b>X-TT-Delivery</b> 去重。
          <br />地址<b>只收 https</b>：保存时与每次投递前各校验一次（DNS 会变），内网 / 回环地址会被拒。
        </blockquote>
        <div class="cv2-field">
          <span class="cv2-field-label">回调地址</span>
          <el-input v-model="callbackDlg.url" :maxlength="200" placeholder="https://接收推送的地址（留空 = 不推送）" data-v2="cb-url" />
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
        <div class="oa-deliveries">
          <a href="#" class="cv2-panel-more" data-v2="deliveries-toggle" @click.prevent="toggleDeliveries">{{ deliveriesToggleLabel }}</a>
          <div v-if="deliveriesOpen" class="oa-deliveries-body" data-v2="deliveries-body">
            <p v-if="deliveriesError !== null" class="oa-warn" data-v2="deliveries-warn">{{ deliveriesError }}</p>
            <p v-else-if="deliveriesLoading" class="oa-dim">正在加载…</p>
            <p v-else-if="deliveries.length === 0" class="oa-dim" data-v2="deliveries-empty">还没有推送记录（这个应用没有产生过事件）。</p>
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
                      <span v-if="orderSub(d) !== ''" class="oa-dim oa-order-sub">内置 {{ orderSub(d) }}</span>
                    </td>
                    <td>
                      <span class="cv2-st" :class="deliveryStatusClass(d)" :data-v2-delivery-status="d.status">{{ d.status_text }}</span>
                      <div v-if="d.status === 'pending' && d.next_attempt_at !== null" class="oa-dim">下次 {{ timeText(d.next_attempt_at) }}</div>
                    </td>
                    <td class="cv2-td-center">{{ d.attempts }}</td>
                    <td>{{ reasonText(d) }}</td>
                    <td class="cv2-td-center">
                      <button v-if="d.status === 'failed'" type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger cv2-btn--commit" :data-v2-retry="d.id" @click="confirmRetry(d.id)">重投</button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-if="deliveriesTotal > deliveries.length" class="oa-dim" data-v2="deliveries-more">共 {{ deliveriesTotal }} 条，这里只显示最近 {{ deliveries.length }} 条。</p>
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

    <!-- ④ 停用/启用确认（island confirmToggle 文案逐字；按钮按方向配文字） -->
    <el-dialog
      :model-value="toggleDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-toggle"
      @update:model-value="toggleDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="toggle-question">
        {{ toggleDlg === null ? '' : TOGGLE_TAILS[toggleDlg.to](toggleDlg.row.name) }}
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="toggleDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="toggle-confirm" @click="void doToggle()">确定</button>
      </div>
    </el-dialog>

    <!-- ⑤ 轮换密钥确认（含「旧密钥立刻作废」逐字；⛔ 不含「只显示一次」） -->
    <el-dialog
      :model-value="rotateDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      :close-on-click-modal="false"
      data-v2="dlg-rotate"
      @update:model-value="rotateDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="rotate-question">
        确定轮换「{{ rotateDlg === null ? '' : rotateDlg.row.name }}」的密钥吗？「旧密钥立刻作废」——正在跑的第三方集成会当场调不通，要有人去改它的配置。（新密钥会写进这个应用，列表里随时看得到、也能一键复制。）
      </p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="rotateDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="rotate-confirm" @click="void doRotate()">确定</button>
      </div>
    </el-dialog>

    <!-- ⑥ API 文档弹层（目录 + marked 正文 + 复制/下载两钮；文案逐字） -->
    <el-dialog
      v-model="docDlgOpen"
      title="对外 API 文档（docs/OPEN-API.md）"
      class="cv2-dialog cv2-dialog--xl"
      destroy-on-close
      data-v2="dlg-doc"
    >
      <div class="oa-doc">
        <div class="oa-doc-bar">
          <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--primary" data-v2="doc-copy" @click="onCopyDoc">{{ DOC_COPY_BUTTON_TEXT }}</button>
          <button type="button" class="cv2-btn cv2-btn--sm" data-v2="doc-download" @click="onDownloadDoc">{{ DOC_DOWNLOAD_BUTTON_TEXT }}</button>
          <p class="cv2-field-hint" data-v2="doc-notice">
            内容就是仓库里的 <b>docs/OPEN-API.md</b>（渲染成网页版看更方便）：左边点目录跳小节，右边是正文。要原文就点上面两颗按钮（复制 / 下载）。⚠️ 文档改动要重新构建前端才会带上。
          </p>
        </div>
        <div class="oa-doc-cols">
          <nav v-if="doc !== null" class="oa-doc-toc" data-v2="doc-toc">
            <a
              v-for="h in tocItems"
              :key="h.id"
              href="#"
              class="oa-doc-toc-item"
              :class="{ 'oa-doc-toc-item--sub': h.depth === 3, 'oa-doc-active': docActiveAnchor === h.id }"
              :data-doc-anchor="h.id"
              @click.prevent="jumpToDocSection(h.id)"
            >{{ h.text }}</a>
          </nav>
          <!-- 仓内受控 md 的 marked 产物（XSS 面照旧页口径，渲染链不改造；⛔ 不走 eval）。 -->
          <div v-if="doc !== null" ref="docBodyRef" class="oa-doc-body" data-v2="doc-body" v-html="doc.html" />
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

/* 告知 quote：三行警示取警示 token，两行灰色说明取 dim。 */
.oa-quote {
  line-height: 26px;
}

.oa-warn {
  color: var(--tt2-money-red);
}

.oa-strong {
  font-weight: 700;
}

.oa-dim {
  color: var(--tt2-dim);
}

.oa-mono {
  font-family: var(--tt2-mono, monospace);
  font-variant-numeric: tabular-nums;
}

/* AppSecret 明文格：长串折行显示（⛔ 不截断——「看得见」不能是半句）。 */
.oa-secret {
  word-break: break-all;
}

td.oa-actions {
  white-space: nowrap;
}

td.oa-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 回调弹层内的人话说明块。 */
.oa-cb-notice {
  line-height: 26px;
}

/* 台账折叠区。 */
.oa-deliveries {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--tt2-line);
}

.oa-deliveries-body {
  margin-top: 6px;
}

.oa-order-sub {
  display: block;
  font-size: 12px;
}

/* 文档弹层：顶栏动作 + 左目录右正文（island DOC 面板同构）。 */
.oa-doc-bar {
  padding-bottom: 8px;
  border-bottom: 1px solid var(--tt2-line);
}

.oa-doc-bar .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

.oa-doc-cols {
  display: flex;
  align-items: flex-start;
  gap: 0;
  margin-top: 8px;
}

.oa-doc-toc {
  flex: 0 0 250px;
  max-width: 250px;
  max-height: 60vh;
  overflow: auto;
  padding-right: 10px;
  border-right: 1px solid var(--tt2-line);
}

.oa-doc-toc-item {
  display: block;
  padding: 1px 4px;
  border-radius: 2px;
  color: var(--tt2-ink);
  text-decoration: none;
  line-height: 22px;
}

.oa-doc-toc-item--sub {
  padding-left: 18px;
}

.oa-doc-toc-item:hover {
  color: var(--tt2-primary-deep);
  background: var(--tt2-accent-box-bg);
}

.oa-doc-toc-item.oa-doc-active {
  background: var(--tt2-primary);
  color: var(--tt2-panel);
}

.oa-doc-body {
  flex: 1 1 auto;
  max-height: 60vh;
  overflow: auto;
  padding: 8px 16px;
}

/* marked 产物（v-html 内容无 scoped 属性 ⇒ 必须 :deep()）。 */
.oa-doc-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}

.oa-doc-body :deep(th),
.oa-doc-body :deep(td) {
  border: 1px solid var(--tt2-line);
  padding: 4px 8px;
  font-size: 12px;
  vertical-align: top;
}

.oa-doc-body :deep(th) {
  background: var(--tt2-label-bg);
  text-align: left;
}

.oa-doc-body :deep(pre) {
  background: var(--tt2-label-bg);
  border: 1px solid var(--tt2-line);
  padding: 8px;
  overflow: auto;
  font-size: 12px;
}

.oa-doc-body :deep(code) {
  font-family: var(--tt2-mono, monospace);
}

.oa-doc-body :deep(h1) {
  font-size: 22px;
}

.oa-doc-body :deep(h2) {
  font-size: 17px;
  margin: 20px 0 8px;
  border-bottom: 1px solid var(--tt2-line);
  padding-bottom: 4px;
}

.oa-doc-body :deep(h3) {
  font-size: 14px;
  margin: 14px 0 6px;
}

.oa-doc-body :deep(blockquote) {
  border-left: 4px solid var(--tt2-primary);
  background: var(--tt2-accent-box-bg);
  margin: 8px 0;
  padding: 6px 10px;
}

.oa-doc-body :deep(.oa-doc-active) {
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
