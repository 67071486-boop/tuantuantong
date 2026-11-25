<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「发布文章作品」v2（`/v2/articles`；APP与小程序组 B 波，计划
 * `docs/plans/2026-11-06-app-mp-feedback-batch.md` §4）。旧页 = island 页
 * `layui-island/pages/app-mp-articles.ts`（自有功能、无证据件）。列表态（频道简介区含保存 +
 * 失败警告行、五列表格、行操作编辑/发布/下架/删除、三态 + 重试、超量警告原文）与编辑态
 * （标题/简介/封面/正文 contenteditable + 工具栏四键 + 底部三键 + 返回列表）+ 右侧手机预览
 * （375×812 缩放 0.8、sticky、两视图、150ms 防抖）。功能与文案逐字照旧页（出处见常量区）。
 *
 * ⚠️ 正文的**两条闸**逐段照抄旧页（⛔ XSS 面，凭记忆重写 = 红线）：保存前
 * `cleanArticleHtml()` 白名单清洗（img 只留 `data-key`；script/style/iframe 连内容丢弃）；
 * 预览 `parseArticleBlocks()` 只认白名单、上屏一律 textContent（本页用 appendSpans 造文本
 * 节点 + 模板插值，等价旧页；落库仍以服务端清洗为准）。其余闸门：标题必填、图片前置闸
 * （类型→体积）、`cover_key` 三态（null=不改/''=移除/其它=新 key）、编辑已有文章必须先
 * `getArticle(id)`（否则正文覆盖成空）、防连点 `saving`、保存已发布文章不悄悄下架、
 * 图片直传三步 ticket→PUT→claim（端点无尾斜杠由 `api/articles` 管）。
 * 状态色钉死：已发布 `cv2-st--settled` / 草稿 `cv2-st--paused`（替旧页 #009688/#FF9800）。
 * 权限门 = 路由唯一（`system.article_manage`），页内不自检（照旧页注释口径）。
 * 有意差异（呈现层，算法零改动）：layui 标记换炭火 cv2-*；旧页行内 cssText 的预览占位/
 * 图片样式改 scoped token 类；删除确认 layer.confirm 换 el-dialog。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  claimArticleMedia,
  createArticle,
  deleteArticle,
  getArticle,
  getChannelProfile,
  issueArticleMediaTicket,
  listArticles,
  publishArticle,
  unpublishArticle,
  updateArticle,
  updateChannelProfile,
  type ArticleDetail,
  type ArticleRow,
  type ArticleStatus,
  type ArticleWritePayload,
  type ChannelProfile,
} from '@/api/articles'
import { putFileToStorage } from '@/layui-island/directUpload'

/* ── 常量与逐字文案（出处 = island 页 app-mp-articles.ts 同名常量，⛔ 自造）────────── */

const TITLE_MAX = 80 // 同上 L81
const INTRO_MAX = 200 // 同上 L82
const MAX_IMAGE_BYTES = 10 * 1024 * 1024 // 同上 L84
const ALLOWED_EXTS = ['jpg', 'jpeg', 'png'] as const // 同上 L86
const MIME_EXT: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpeg',
} // 同上 L87-90

const MESSAGES = {
  loading: '加载中…',
  empty: '还没有文章；点「新建文章」写一篇',
  failed: '操作失败',
  noTitle: '请填写标题',
  saved: '已保存草稿',
  savedPublished: '已保存',
  published: '已发布',
  unpublished: '已下架',
  deleted: '已删除',
  profileSaved: '频道简介已保存',
  imageBadType: '只支持 jpg / jpeg / png',
  imageTooLarge: '图片不能超过 10 MB',
  noPublished: '还没有发布文章',
  noIntro: '暂无简介',
} as const // 同上 L164-179

const PHONE_WIDTH = 375 // 同上 L182
const PHONE_HEIGHT = 812 // 同上 L183
const PHONE_SCALE = 0.8 // 同上 L184
const PREVIEW_DEBOUNCE_MS = 150 // 同上 L186

/** 预览里「文章详情 / 频道列表」两视图（island 页 PreviewView 同型）。 */
type PreviewView = 'detail' | 'channel'

/* ── 正文清洗与预览解析（⭐ 逐段照抄 island 页 L188-417，⛔ XSS 面）────────────────── */

/** 一篇文章里的行内片段（预览用；只保留粗体 / 斜体两个标记）。 */
type ArticleSpan = { text: string; bold: boolean; italic: boolean }

/** 预览里的一块正文；只有一个 `img` 变体带 `key` / `url`，文本块不带。 */
type ArticleBlock =
  | { type: 'p' | 'h2' | 'h3' | 'li'; spans: ArticleSpan[] }
  | { type: 'img'; key: string; url: string }

/** 白名单标签（保存 / 预览共用一份；`b` / `i` / `div` 先归一化再进集合）。 */
const ALLOWED_TAGS = new Set(['p', 'h2', 'h3', 'strong', 'em', 'br', 'img', 'ul', 'ol', 'li'])
/** 会被当成「块」的标签（预览里各自成块）。 */
const BLOCK_TAGS = new Set(['p', 'h2', 'h3'])
/** 连内容一起丢掉的标签（拆壳保留文字的一类特例；保存与预览两侧共用）。 */
const DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript'])

/** 归一化：编辑器 / 旧数据里可能出现的等价标签。 */
function normalizeTag(tag: string): string {
  if (tag === 'b') return 'strong'
  if (tag === 'i') return 'em'
  if (tag === 'div') return 'p'
  return tag
}

/** 是否块级容器（`div` 只有在子节点里没有块时才当段落；否则拆开递归）。 */
function hasBlockChild(element: Element): boolean {
  return Array.from(element.children).some((child) => {
    const tag = child.tagName.toLowerCase()
    return BLOCK_TAGS.has(tag) || tag === 'ul' || tag === 'ol' || tag === 'li' || tag === 'div'
  })
}

/** 正文 HTML → 只含白名单标签 / 只保留 `data-key` 的 HTML（保存前必过）。 */
function cleanArticleHtml(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  const out = document.createElement('div')
  copyClean(template.content, out)
  return out.innerHTML
}

function copyClean(source: ParentNode, target: Node): void {
  source.childNodes.forEach((node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      target.appendChild(document.createTextNode(node.textContent ?? ''))
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return
    const element = node as Element
    const rawTag = element.tagName.toLowerCase()
    if (DROP_TAGS.has(rawTag)) return
    const tag = normalizeTag(rawTag)
    if (tag === 'img') {
      const key = element.getAttribute('data-key') ?? ''
      if (key === '') return
      const img = document.createElement('img')
      img.setAttribute('data-key', key)
      target.appendChild(img)
      return
    }
    if (tag === 'br') {
      target.appendChild(document.createElement('br'))
      return
    }
    if (rawTag === 'div' && hasBlockChild(element)) {
      copyClean(element, target)
      return
    }
    if (ALLOWED_TAGS.has(tag)) {
      const copy = document.createElement(tag)
      copyClean(element, copy)
      target.appendChild(copy)
      return
    }
    copyClean(element, target)
  })
}

/** 把正文里的图片补上 `src`（只用于编辑器显示 / 预览，⛔ 不写回库）。 */
function hydrateArticleHtml(html: string, media: Readonly<Record<string, string>>): string {
  const template = document.createElement('template')
  template.innerHTML = cleanArticleHtml(html)
  template.content.querySelectorAll('img').forEach((img) => {
    const key = img.getAttribute('data-key') ?? ''
    const url = media[key]
    if (url === undefined || url === '') img.removeAttribute('src')
    else img.setAttribute('src', url)
  })
  return template.innerHTML
}

/** `content_html` → 预览用的块（只用于预览；所有属性都不认，读链接只从 `media` 取）。 */
function parseArticleBlocks(
  html: string,
  media: Readonly<Record<string, string>>,
): ArticleBlock[] {
  const template = document.createElement('template')
  template.innerHTML = html
  const blocks: ArticleBlock[] = []
  let pendingSpans: ArticleSpan[] = []
  let pendingType: 'p' | 'h2' | 'h3' | 'li' | null = null

  const flush = (): void => {
    if (pendingType !== null && pendingSpans.some((span) => span.text.trim() !== '')) {
      blocks.push({ type: pendingType, spans: pendingSpans })
    }
    pendingSpans = []
    pendingType = null
  }
  const addText = (text: string, bold: boolean, italic: boolean): void => {
    if (text === '') return
    if (pendingType === null) pendingType = 'p'
    const last = pendingSpans[pendingSpans.length - 1]
    if (last !== undefined && last.bold === bold && last.italic === italic) {
      last.text += text
      return
    }
    pendingSpans.push({ text, bold, italic })
  }
  const addImage = (key: string): void => {
    flush()
    if (key === '') return
    const url = media[key]
    blocks.push({ type: 'img', key, url: url ?? '' })
  }
  const walk = (parent: ParentNode, bold: boolean, italic: boolean): void => {
    parent.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        addText(node.textContent ?? '', bold, italic)
        return
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return
      const element = node as Element
      const rawTag = element.tagName.toLowerCase()
      if (DROP_TAGS.has(rawTag)) return
      if (rawTag === 'img') {
        addImage(element.getAttribute('data-key') ?? '')
        return
      }
      if (rawTag === 'br') {
        addText('\n', bold, italic)
        return
      }
      const tag = normalizeTag(rawTag)
      if (tag === 'strong') {
        walk(element, true, italic)
        return
      }
      if (tag === 'em') {
        walk(element, bold, true)
        return
      }
      if (tag === 'ul' || tag === 'ol') {
        flush()
        element.childNodes.forEach((child) => {
          if (child.nodeType !== Node.ELEMENT_NODE) return
          const childTag = (child as Element).tagName.toLowerCase()
          if (childTag !== 'li') {
            walk(child as Element, bold, italic)
            return
          }
          flush()
          pendingType = 'li'
          walk(child as Element, bold, italic)
          flush()
        })
        return
      }
      if (tag === 'li') {
        flush()
        pendingType = 'li'
        walk(element, bold, italic)
        flush()
        return
      }
      if (BLOCK_TAGS.has(tag)) {
        flush()
        pendingType = tag as 'p' | 'h2' | 'h3'
        walk(element, bold, italic)
        flush()
        return
      }
      if (rawTag === 'div') {
        if (hasBlockChild(element)) {
          flush()
          walk(element, bold, italic)
          return
        }
        flush()
        pendingType = 'p'
        walk(element, bold, italic)
        flush()
        return
      }
      // 白名单外的容器 / 行内标签：拆壳保留文字（属性一律不认）。
      walk(element, bold, italic)
    })
  }
  walk(template.content, false, false)
  flush()
  return blocks
}

/* ── 纯函数（出处 = island 页 L419-471 逐段照抄）──────────────────────────────────── */

/** ISO 8601 → `2026-10-23 10:11:00`（只截到秒）。`null` ⇒ `—`。 */
function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/** ISO / 日期 → `2026-10-23`（移动端卡片与详情只到日）。 */
function dateText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.slice(0, 10)
}

/** 状态的中文名（只用于网页端；移动端不显示状态）。 */
function statusText(status: ArticleStatus): string {
  return status === 'published' ? '已发布' : '草稿'
}

/** 取文件名的扩展名（小写、去点；没有就回空串）。 */
function extOf(name: string): string {
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return ''
  return name.slice(dot + 1).toLowerCase()
}

/** 这次要发给签发端点的扩展名（`null` = 判断不出；名字没有就按 MIME 兜底）。 */
function resolveExt(file: File): string | null {
  const byName = extOf(file.name)
  if ((ALLOWED_EXTS as readonly string[]).includes(byName)) return byName
  return MIME_EXT[(file.type ?? '').toLowerCase()] ?? null
}

/** 选文件时的前置闸（返回要说哪一句；`null` = 放行）。权威判据仍然后端。 */
function rejectReason(file: File): string | null {
  if (resolveExt(file) === null) return MESSAGES.imageBadType
  if (file.size > MAX_IMAGE_BYTES) return MESSAGES.imageTooLarge
  return null
}

/** 工作室名的首字（频道头部的头像）。空名回 `?`。 */
function initialOf(name: string): string {
  return name.trim().slice(0, 1) || '?'
}

/* ── 提示（页面内自绘 toast，同 V2Notices；info 1500ms / error 6000ms）────────────── */

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

function errText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

/* ── 列表态（列表 + 三态 + 频道简介区）─────────────────────────────────────────────── */

const view = ref<'list' | 'edit'>('list')
const articles = ref<ArticleRow[]>([])
const profile = ref<ChannelProfile>({ name: '工作室', intro: '', article_count: 0 })
const profileIntro = ref('')
const profileWarn = ref('')

/** 状态行四态（island setListState 同口径：none 隐藏行；error = 原文 + 重试）。 */
const listState = ref<'none' | 'loading' | 'empty' | 'error' | 'warn'>('loading')
const listStateText = ref<string>(MESSAGES.loading)
const listTableVisible = computed(
  () => listState.value === 'empty' || listState.value === 'warn' || listState.value === 'none',
)

const profileName = computed(() => (profile.value.name === '' ? '工作室' : profile.value.name))

async function loadList(): Promise<void> {
  listState.value = 'loading'
  listStateText.value = MESSAGES.loading
  try {
    const payload = await listArticles()
    articles.value = payload.items
    if (articles.value.length === 0) {
      listState.value = 'empty'
      listStateText.value = MESSAGES.empty
    } else if (payload.total > articles.value.length) {
      // 一次只取最近 200 篇；不静默截断（超量警告原文照旧页）。
      listState.value = 'warn'
      listStateText.value = `只显示最近 ${articles.value.length} 篇（共 ${payload.total} 篇），更早的请看后续分页`
    } else {
      listState.value = 'none'
    }
  } catch (error) {
    articles.value = []
    listState.value = 'error'
    listStateText.value = errText(error)
  }
}

/** 简介取不到不挡列表，但必须说出来（静默显示成「0 篇文章」会被当成真的）。 */
async function loadProfile(): Promise<void> {
  try {
    profile.value = await getChannelProfile()
    profileWarn.value = ''
  } catch (error) {
    profileWarn.value = `频道简介没取到：${errText(error)}`
  }
  profileIntro.value = profile.value.intro
}

async function saveChannelProfile(): Promise<void> {
  try {
    profile.value = await updateChannelProfile(profileIntro.value.trim())
    profileIntro.value = profile.value.intro
    showToast(MESSAGES.profileSaved)
  } catch (error) {
    showToast(errText(error), 'error')
  }
}

function onListRetry(): void {
  void Promise.all([loadList(), loadProfile()])
}

/* ── 行操作（编辑 / 发布 / 下架 / 删除）────────────────────────────────────────────── */

const delRow = ref<ArticleRow | null>(null)
const deleting = ref(false)

function onRowDelete(row: ArticleRow): void {
  delRow.value = row
}

async function confirmDelete(): Promise<void> {
  const row = delRow.value
  if (row === null || deleting.value) return
  deleting.value = true
  try {
    await deleteArticle(row.id)
    delRow.value = null
    await Promise.all([loadList(), loadProfile()])
    showToast(MESSAGES.deleted)
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    deleting.value = false
  }
}

async function onRowPublish(row: ArticleRow): Promise<void> {
  try {
    await publishArticle(row.id)
    await Promise.all([loadList(), loadProfile()])
    showToast(MESSAGES.published)
  } catch (error) {
    showToast(errText(error), 'error')
  }
}

async function onRowUnpublish(row: ArticleRow): Promise<void> {
  try {
    await unpublishArticle(row.id)
    await Promise.all([loadList(), loadProfile()])
    showToast(MESSAGES.unpublished)
  } catch (error) {
    showToast(errText(error), 'error')
  }
}

/* ── 编辑态状态（进出 + 保存 / 发布）───────────────────────────────────────────────── */

const editingId = ref<number | null>(null)
/** 进入编辑时的状态（决定「保存草稿」要不要保住已发布）。 */
const editingStatus = ref<ArticleStatus>('draft')
const editingPublishedAt = ref<string | null>(null)
const media = ref<Record<string, string>>({})
/** 封面三态：`null` = 没改过（PATCH 不带键）/ `''` = 明确移除 / 其它 = 新 key。 */
const coverKey = ref<string | null>(null)
const coverUrl = ref('')
const saving = ref(false)

const title = ref('')
const intro = ref('')

const titleCount = computed(() => `${title.value.length}/${TITLE_MAX}`)
const introCount = computed(() => `${intro.value.length}/${INTRO_MAX}`)

const editorEl = ref<HTMLElement | null>(null)
const titleEl = ref<HTMLInputElement | null>(null)
const coverFileEl = ref<HTMLInputElement | null>(null)
const bodyFileEl = ref<HTMLInputElement | null>(null)

/** 预览防抖定时器（卸载 / 退出编辑时清掉）。 */
let previewTimer: number | null = null

function schedulePreview(): void {
  if (previewTimer !== null) window.clearTimeout(previewTimer)
  previewTimer = window.setTimeout(() => {
    previewTimer = null
    renderPreview()
  }, PREVIEW_DEBOUNCE_MS)
}

function applyEdit(row: ArticleRow | null, detail: ArticleDetail | null): void {
  editingId.value = row?.id ?? null
  editingStatus.value = detail?.status ?? row?.status ?? 'draft'
  editingPublishedAt.value = detail?.published_at ?? row?.published_at ?? null
  media.value = detail?.media ?? {}
  coverKey.value = null
  coverUrl.value = detail?.cover_url ?? row?.cover_url ?? ''
  title.value = detail?.title ?? row?.title ?? ''
  intro.value = detail?.intro ?? row?.intro ?? ''
  if (editorEl.value !== null) {
    editorEl.value.innerHTML = detail === null ? '' : hydrateArticleHtml(detail.content_html, detail.media)
  }
  view.value = 'edit'
  renderPreview()
  titleEl.value?.focus()
}

/**
 * 打开编辑态（`null` = 新建）。列表行不带 `content_html` / `media`（后端契约）
 * ⇒ 编辑已有文章必须先 `getArticle(id)` 拿详情，否则正文会被保存覆盖成空。
 */
async function openEditor(row: ArticleRow | null): Promise<void> {
  if (row === null) {
    applyEdit(null, null)
    return
  }
  try {
    const detail = await getArticle(row.id)
    applyEdit(row, detail)
  } catch (error) {
    showToast(errText(error), 'error')
  }
}

function exitEdit(): void {
  if (previewTimer !== null) {
    window.clearTimeout(previewTimer)
    previewTimer = null
  }
  editingId.value = null
  editingStatus.value = 'draft'
  editingPublishedAt.value = null
  media.value = {}
  coverKey.value = null
  coverUrl.value = ''
  title.value = ''
  intro.value = ''
  if (editorEl.value !== null) editorEl.value.innerHTML = ''
  view.value = 'list'
  renderPreview()
}

/** 保存 / 发布共用的写入（校验标题 + 组载荷）。`null` = 被校验挡下。 */
async function persist(status: ArticleStatus): Promise<ArticleRow | null> {
  const trimmedTitle = title.value.trim()
  if (trimmedTitle === '') {
    showToast(MESSAGES.noTitle)
    return null
  }
  const payload: ArticleWritePayload = {
    title: trimmedTitle,
    intro: intro.value.trim(),
    content_html: cleanArticleHtml(editorEl.value?.innerHTML ?? ''),
    status,
  }
  // cover_key 三态：没改过就不带这个键（PATCH 才保得住原封面），明确移除才带空串。
  if (coverKey.value !== null) payload.cover_key = coverKey.value
  return editingId.value === null
    ? await createArticle(payload)
    : await updateArticle(editingId.value, payload)
}

async function onSaveDraft(): Promise<void> {
  if (saving.value) return
  saving.value = true
  try {
    // 编辑已发布文章时「保存」不该把它悄悄下架 ⇒ 保持 published。
    const status: ArticleStatus = editingStatus.value === 'published' ? 'published' : 'draft'
    const wasPublished = editingStatus.value === 'published'
    const saved = await persist(status)
    if (saved === null) return
    await Promise.all([loadList(), loadProfile()])
    exitEdit()
    // exitEdit 会把 editingStatus 复位 ⇒ 必须在退出前取快照（旧页 2026-10-23 评审指出）。
    showToast(wasPublished ? MESSAGES.savedPublished : MESSAGES.saved)
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    saving.value = false
  }
}

async function onPublish(): Promise<void> {
  if (saving.value) return
  saving.value = true
  try {
    const status: ArticleStatus = editingStatus.value === 'published' ? 'published' : 'draft'
    const saved = await persist(status)
    if (saved === null) return
    // 发布是独立端点（published_at 只写一次由服务端管），⛔ 不靠 PATCH 一个 status。
    await publishArticle(saved.id)
    await Promise.all([loadList(), loadProfile()])
    exitEdit()
    showToast(MESSAGES.published)
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    saving.value = false
  }
}

/* ── 媒体直传三步（取票 → PUT 原字节 → 认领；出处 = island 页 L1042-1128 逐段照抄）── */

/** 直传目标：相对路径时补后端前缀（`VITE_API_BASE`，本机为空 ⇒ 原样）。 */
function backendUploadUrl(uploadUrl: string): string {
  const base = import.meta.env.VITE_API_BASE ?? ''
  if (base === '' || !uploadUrl.startsWith('/')) return uploadUrl
  return `${base.replace(/\/$/, '')}${uploadUrl}`
}

/** 上传一张图并认领。返回 `null` = 被前置闸拦下 / 已提示过。 */
async function uploadMedia(file: File): Promise<{ key: string; url: string } | null> {
  const reason = rejectReason(file)
  if (reason !== null) {
    showToast(reason)
    return null
  }
  const ext = resolveExt(file)
  if (ext === null) {
    showToast(MESSAGES.imageBadType)
    return null
  }
  const ticket = await issueArticleMediaTicket(ext)
  await putFileToStorage(backendUploadUrl(ticket.upload_url), file)
  return await claimArticleMedia(ticket.key)
}

async function onCoverFile(): Promise<void> {
  const file = coverFileEl.value?.files?.[0]
  if (file === undefined) return
  try {
    const claimed = await uploadMedia(file)
    if (claimed === null) return
    coverKey.value = claimed.key
    coverUrl.value = claimed.url
    renderPreview()
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    if (coverFileEl.value !== null) coverFileEl.value.value = ''
  }
}

/** 把一张已经认领的图插到光标处（拿不到选区就追加到末尾）。 */
function insertImage(key: string, url: string): void {
  const editor = editorEl.value
  if (editor === null) return
  const img = document.createElement('img')
  img.setAttribute('data-key', key)
  img.setAttribute('src', url)
  img.alt = ''
  editor.focus()
  const selection = window.getSelection()
  const range = selection !== null && selection.rangeCount > 0 ? selection.getRangeAt(0) : null
  if (selection !== null && range !== null && editor.contains(range.commonAncestorContainer)) {
    range.deleteContents()
    range.insertNode(img)
    range.setStartAfter(img)
    range.collapse(true)
    selection.removeAllRanges()
    selection.addRange(range)
  } else {
    editor.appendChild(img)
  }
  schedulePreview()
}

async function onBodyFile(): Promise<void> {
  const file = bodyFileEl.value?.files?.[0]
  if (file === undefined) return
  try {
    const claimed = await uploadMedia(file)
    if (claimed === null) return
    media.value = { ...media.value, [claimed.key]: claimed.url }
    insertImage(claimed.key, claimed.url)
  } catch (error) {
    showToast(errText(error), 'error')
  } finally {
    if (bodyFileEl.value !== null) bodyFileEl.value.value = ''
  }
}

/** 工具栏：加粗走 execCommand('bold')，正文 / 小标题走 formatBlock（旧页同口径）。 */
function runEditorCommand(command: string): void {
  const editor = editorEl.value
  if (editor === null) return
  editor.focus()
  if (typeof document.execCommand !== 'function') return
  if (command === 'bold') {
    document.execCommand('bold', false)
  } else {
    document.execCommand('formatBlock', false, command)
  }
  schedulePreview()
}

/* ── 右侧手机预览（详情正文命令式渲染；其余模板渲染）──────────────────────────────── */

const previewView = ref<PreviewView>('detail')
const previewBodyEl = ref<HTMLElement | null>(null)

const previewTitle = computed(() => (title.value.trim() === '' ? '无标题' : title.value.trim()))
const previewDate = computed(() =>
  editingPublishedAt.value === null ? '未发布' : dateText(editingPublishedAt.value),
)
const previewFooter = computed(() => `本文由 ${profileName.value} 发布`)

const channelName = computed(() => profileName.value)
const channelInitial = computed(() => initialOf(channelName.value))
const channelIntro = computed(() =>
  profile.value.intro.trim() === '' ? MESSAGES.noIntro : profile.value.intro,
)

/** 频道列表只画已发布、按 `published_at` 倒序（与移动端公开接口同源）。 */
const channelArticles = computed<ArticleRow[]>(() => {
  const published = articles.value
    .filter((row) => row.status === 'published')
    .sort((a, b) => (b.published_at ?? '').localeCompare(a.published_at ?? ''))
  if (editingId.value === null || editingStatus.value !== 'published') return published
  // 正在编辑一篇已发布的文章 ⇒ 标题 / 封面用编辑器里的当前值（频道预览也跟着实时）。
  const titleNow = title.value.trim() === '' ? '无标题' : title.value.trim()
  return published.map((row) =>
    row.id === editingId.value ? { ...row, title: titleNow, cover_url: coverUrl.value } : row,
  )
})

/** 频道头部篇数与公开端同义（只数已发布）；Math.max 只兜 profile 未加载完的瞬间。 */
const channelCountText = computed(
  () => `${Math.max(profile.value.article_count, channelArticles.value.length)} 篇文章`,
)

/** 预览行内片段：一律造文本节点（⭐ 旧页 appendSpans 逐字照抄，textContent 口径）。 */
function appendSpans(target: HTMLElement, spans: readonly ArticleSpan[]): void {
  for (const span of spans) {
    const parts = span.text.split('\n')
    parts.forEach((part, index) => {
      if (index > 0) target.appendChild(document.createElement('br'))
      if (part === '') return
      let node: Node = document.createTextNode(part)
      if (span.italic) {
        const em = document.createElement('em')
        em.appendChild(node)
        node = em
      }
      if (span.bold) {
        const strong = document.createElement('strong')
        strong.appendChild(node)
        node = strong
      }
      target.appendChild(node)
    })
  }
}

/** 预览块渲染（⭐ 旧页 renderBlocks 照抄；仅旧页行内 cssText 的两处色改 token 类）。 */
function renderBlocks(target: HTMLElement, blocks: readonly ArticleBlock[]): void {
  target.textContent = ''
  for (const block of blocks) {
    if (block.type === 'img') {
      if (block.url === '') {
        const placeholder = document.createElement('div')
        placeholder.className = 'at-pv-imgph'
        placeholder.textContent = '图片'
        target.appendChild(placeholder)
        continue
      }
      const img = document.createElement('img')
      img.className = 'at-pv-img'
      img.src = block.url
      img.alt = ''
      target.appendChild(img)
      continue
    }
    const tag = block.type === 'li' ? 'div' : block.type
    const element = document.createElement(tag)
    element.className =
      block.type === 'li'
        ? 'at-pv-li'
        : block.type === 'h2'
          ? 'at-pv-h2'
          : block.type === 'h3'
            ? 'at-pv-h3'
            : 'at-pv-p'
    appendSpans(element, block.spans)
    target.appendChild(element)
  }
}

/** 预览正文的唯一渲染出口（清洗 → 解析 → 命令式上屏；标题/封面等走模板插值）。 */
function renderPreview(): void {
  const body = previewBodyEl.value
  if (body === null) return
  renderBlocks(body, parseArticleBlocks(cleanArticleHtml(editorEl.value?.innerHTML ?? ''), media.value))
}

/* ── 装载与卸载 ───────────────────────────────────────────────────────────────────── */

onMounted(() => {
  void Promise.all([loadList(), loadProfile()])
})

onUnmounted(() => {
  if (previewTimer !== null) {
    window.clearTimeout(previewTimer)
    previewTimer = null
  }
  if (toastTimer !== null) {
    clearTimeout(toastTimer)
    toastTimer = null
  }
})
</script>

<template>
  <div class="cv2-page" data-v2-page="articles">
    <!-- ══ 列表态 ══ -->
    <div v-show="view === 'list'" data-v2="list">
      <div class="cv2-toolbar" data-v2="list-toolbar">
        <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="new-article" @click="openEditor(null)">
          新建文章
        </button>
      </div>

      <!-- 频道简介区：工作室名 + 已发布篇数 + 简介 + 保存；取数失败必须说出来 -->
      <section class="cv2-panel" aria-label="频道简介" data-v2="profile">
        <div class="at-profile-row">
          <div class="at-profile-head">
            <b data-v2="profile-name">{{ profileName }}</b>
            <span class="at-profile-count" data-v2="profile-count">· {{ profile.article_count }} 篇文章</span>
          </div>
          <textarea
            v-model="profileIntro"
            class="cv2-input at-profile-intro"
            rows="2"
            :maxlength="INTRO_MAX"
            placeholder="频道简介（移动端频道头部显示，最多 200 字）"
            data-v2="profile-intro"
          ></textarea>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="profile-save" @click="saveChannelProfile">
            保存简介
          </button>
        </div>
        <p v-if="profileWarn !== ''" class="at-warn" data-v2="profile-warn">{{ profileWarn }}</p>
      </section>

      <!-- 状态行：加载中 / 空态 / 超量警告原文 / 失败原文 + 重试（旧页 listState 同口径） -->
      <p
        v-if="listState !== 'none'"
        class="cv2-status"
        :class="{ 'cv2-status--error': listState === 'error' }"
        data-v2="list-state"
      >
        <template v-if="listState === 'error'">
          <span data-v2="list-error">{{ listStateText }}</span>
          <button type="button" class="cv2-btn cv2-btn--mini cv2-btn--commit" data-v2="list-retry" @click="onListRetry">重试</button>
        </template>
        <template v-else>{{ listStateText }}</template>
      </p>

      <!-- 五列表格（加载中 / 失败时整块隐藏，旧页同口径） -->
      <section v-show="listTableVisible" class="cv2-panel cv2-table-panel" aria-label="文章列表">
        <table class="cv2-table" data-v2="list-table">
          <colgroup>
            <col />
            <col style="width: 90px" />
            <col style="width: 90px" />
            <col style="width: 170px" />
            <col style="width: 220px" />
          </colgroup>
          <thead>
            <tr>
              <th>标题</th>
              <th>状态</th>
              <th>阅读数</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in articles" :key="row.id" class="cv2-row" :data-v2-row="row.id">
              <td :data-v2-title="row.id">{{ row.title }}</td>
              <td>
                <span
                  class="cv2-st"
                  :class="row.status === 'published' ? 'cv2-st--settled' : 'cv2-st--paused'"
                  :data-v2-status="row.id"
                ><i class="cv2-dot" aria-hidden="true"></i>{{ statusText(row.status) }}</span>
              </td>
              <td class="cv2-td-num" :data-v2-read="row.id">{{ row.read_count }}</td>
              <td class="cv2-td-time" :data-v2-updated="row.id">{{ timeText(row.updated_at) }}</td>
              <td class="at-actions">
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-edit="row.id" @click="openEditor(row)">编辑</button>
                <button
                  v-if="row.status === 'draft'"
                  type="button"
                  class="cv2-btn cv2-btn--sm cv2-btn--primary cv2-btn--commit"
                  :data-v2-publish="row.id"
                  @click="onRowPublish(row)"
                >发布</button>
                <button
                  v-else
                  type="button"
                  class="cv2-btn cv2-btn--sm cv2-btn--danger"
                  :data-v2-unpublish="row.id"
                  @click="onRowUnpublish(row)"
                >下架</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger" :data-v2-del="row.id" @click="onRowDelete(row)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- ══ 编辑态（两块都留在 DOM 里靠 v-show 切换：编辑器节点与监听不重建，旧页同口径）══ -->
    <div v-show="view === 'edit'" data-v2="edit">
      <div class="cv2-toolbar" data-v2="edit-toolbar">
        <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="back" @click="exitEdit">返回列表</button>
      </div>

      <div class="at-edit-grid">
        <section class="cv2-panel" aria-label="文章编辑" data-v2="editor-panel">
          <div class="cv2-field">
            <span class="cv2-field-label">标题</span>
            <input
              ref="titleEl"
              v-model="title"
              class="cv2-input"
              type="text"
              :maxlength="TITLE_MAX"
              autocomplete="off"
              placeholder="文章标题（移动端列表显示）"
              data-v2-field="title"
              @input="schedulePreview"
            />
            <span class="cv2-field-hint at-count" data-v2="title-count">{{ titleCount }}</span>
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">简介</span>
            <textarea
              v-model="intro"
              class="cv2-input"
              rows="2"
              :maxlength="INTRO_MAX"
              placeholder="文章摘要（可选，最多 200 字）"
              data-v2-field="intro"
            ></textarea>
            <span class="cv2-field-hint at-count" data-v2="intro-count">{{ introCount }}</span>
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">封面</span>
            <div class="at-cover-row">
              <div class="at-cover-box" data-v2="cover-box">
                <img v-if="coverUrl !== ''" :src="coverUrl" alt="封面" class="at-cover-img" data-v2="cover-img" />
                <span v-else>暂无封面</span>
              </div>
              <div>
                <button type="button" class="cv2-btn cv2-btn--commit" data-v2="cover-pick" @click="coverFileEl?.click()">上传封面</button>
                <button
                  v-show="coverUrl !== ''"
                  type="button"
                  class="cv2-btn at-cover-remove cv2-btn--danger"
                  data-v2="cover-remove"
                  @click="coverKey = ''; coverUrl = ''; renderPreview()"
                >移除</button>
                <div class="cv2-field-hint" data-v2="cover-hint">支持 png / jpg / jpeg，单张不超过 10 MB</div>
              </div>
            </div>
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">正文</span>
            <!-- 工具栏 mousedown.prevent：点按钮不丢编辑器选区（旧页同口径） -->
            <div class="at-editor-toolbar" data-v2="editor-toolbar" @mousedown.prevent>
              <button type="button" class="cv2-btn cv2-btn--mini cv2-btn--commit" data-v2="cmd-p" @click="runEditorCommand('p')">正文</button>
              <button type="button" class="cv2-btn cv2-btn--mini cv2-btn--commit" data-v2="cmd-h2" @click="runEditorCommand('h2')">小标题</button>
              <button type="button" class="cv2-btn cv2-btn--mini cv2-btn--commit" data-v2="cmd-bold" @click="runEditorCommand('bold')">加粗</button>
              <button type="button" class="cv2-btn cv2-btn--mini cv2-btn--primary cv2-btn--commit" data-v2="cmd-image" @click="bodyFileEl?.click()">插图</button>
            </div>
            <div
              ref="editorEl"
              class="at-editor"
              contenteditable="true"
              data-v2="editor"
              @input="schedulePreview"
            ></div>
            <div class="cv2-field-hint" data-v2="body-hint">插图走与封面同一条直传链路；保存时只保留对象名。</div>
          </div>
          <div class="at-edit-actions" data-v2="edit-actions">
            <button type="button" class="cv2-btn cv2-btn--commit" data-v2="save-draft" :disabled="saving" @click="onSaveDraft">保存草稿</button>
            <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="publish" :disabled="saving" @click="onPublish">发布</button>
            <button type="button" class="cv2-btn cv2-btn--quiet" data-v2="cancel" :disabled="saving" @click="exitEdit">取消</button>
          </div>
        </section>

        <!-- 右侧手机预览：375×812 缩放 0.8、sticky、两视图、150ms 防抖 -->
        <aside class="at-preview" aria-label="手机预览" data-v2="preview">
          <div class="at-pv-switch" data-v2="pv-switch">
            <button
              type="button"
              class="cv2-btn cv2-btn--mini"
              :class="{ 'cv2-btn--primary': previewView === 'detail' }"
              data-v2="pv-view-detail"
              @click="previewView = 'detail'"
            >文章详情</button>
            <button
              type="button"
              class="cv2-btn cv2-btn--mini"
              :class="{ 'cv2-btn--primary': previewView === 'channel' }"
              data-v2="pv-view-channel"
              @click="previewView = 'channel'"
            >频道列表</button>
          </div>
          <div class="at-phone" :style="{ width: `${Math.round(PHONE_WIDTH * PHONE_SCALE)}px`, height: `${Math.round(PHONE_HEIGHT * PHONE_SCALE)}px` }" data-v2="pv-phone">
            <div
              class="at-phone-screen"
              :style="{ width: `${PHONE_WIDTH}px`, height: `${PHONE_HEIGHT}px`, transform: `scale(${PHONE_SCALE})` }"
            >
              <div v-show="previewView === 'detail'" class="at-pv-detail" data-v2="pv-detail">
                <div class="at-pv-coverwrap">
                  <img v-if="coverUrl !== ''" :src="coverUrl" alt="" class="at-pv-cover" data-v2="pv-cover" />
                </div>
                <div class="at-pv-titlerow">
                  <div class="at-pv-title" data-v2="pv-title">{{ previewTitle }}</div>
                  <div class="at-pv-date" data-v2="pv-date">{{ previewDate }}</div>
                </div>
                <!-- 正文：parseArticleBlocks → renderBlocks 命令式上屏（textContent 口径） -->
                <div ref="previewBodyEl" class="at-pv-body" data-v2="pv-body"></div>
                <div class="at-pv-footer" data-v2="pv-footer">{{ previewFooter }}</div>
              </div>
              <div v-show="previewView === 'channel'" class="at-pv-channel" data-v2="pv-channel">
                <div class="at-pv-chhead">
                  <div class="at-pv-avatar" data-v2="pv-avatar">{{ channelInitial }}</div>
                  <div class="at-pv-chmeta">
                    <div class="at-pv-chname" data-v2="pv-ch-name">{{ channelName }}</div>
                    <div class="at-pv-chintro" data-v2="pv-ch-intro">{{ channelIntro }}</div>
                    <div class="at-pv-chcount" data-v2="pv-ch-count">{{ channelCountText }}</div>
                  </div>
                </div>
                <div class="at-pv-cards" data-v2="pv-cards">
                  <div v-for="row in channelArticles" :key="row.id" class="at-card" :data-v2-card="row.id">
                    <!-- 移动端无封面时整块不占位（不是灰框），预览照做 -->
                    <img v-if="row.cover_url !== ''" class="at-card-cover" :src="row.cover_url" loading="lazy" alt="" />
                    <div class="at-card-title">{{ row.title }}</div>
                    <div class="at-card-date">{{ dateText(row.published_at ?? row.created_at) }}</div>
                  </div>
                </div>
                <div v-if="channelArticles.length === 0" class="at-pv-chempty" data-v2="pv-ch-empty">
                  {{ MESSAGES.noPublished }}
                </div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>

    <!-- 隐藏文件入口（封面 / 正文插图同一条直传链路） -->
    <input
      ref="coverFileEl"
      type="file"
      accept="image/png,image/jpeg"
      style="display: none"
      data-v2="cover-file"
      @change="onCoverFile"
    />
    <input
      ref="bodyFileEl"
      type="file"
      accept="image/png,image/jpeg"
      style="display: none"
      data-v2="body-file"
      @change="onBodyFile"
    />

    <!-- 删除确认（问句逐字照旧页 layer.confirm） -->
    <el-dialog
      :model-value="delRow !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete"
      @update:model-value="delRow = null"
    >
      <p class="cv2-dlg-text" data-v2="del-question">
        确定删除「{{ delRow === null ? '' : delRow.title }}」吗？删除后移动端就看不到了，且不能恢复。
      </p>
      <template #footer>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="delRow = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="del-confirm" :disabled="deleting" @click="confirmDelete">确定</button>
        </div>
      </template>
    </el-dialog>

    <!-- 炭火 toast（居中深条，同 V2Notices） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增形状收在这里；色一律 var(--tt2-*) token（⛔ 裸色值）。 */

/* 频道简介区：名 + 计数一行，textarea 占中段，保存钮右置（旧页 flex 布局的炭火版）。 */
.at-profile-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}

.at-profile-head {
  flex: 0 0 auto;
  line-height: 34px;
}

.at-profile-count {
  color: var(--tt2-dim);
}

.at-profile-intro {
  flex: 1 1 320px;
  min-width: 260px;
  resize: vertical;
}

/* 频道简介取数失败的橙色警告行（旧 #FF5722 → summary-orange token，V2GameManage 引文块同款）。 */
.at-warn {
  margin: 10px 0 0;
  color: var(--tt2-summary-orange);
  font-size: 13px;
  line-height: 20px;
}

/* 状态行沿用 V2DailiantongStats 的家族形态。 */
.cv2-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 2px 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

.cv2-status--error {
  color: var(--tt2-st-error);
}

/* 行操作：小号按钮排，间距 6px。 */
.at-actions {
  white-space: nowrap;
}

.at-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

/* 编辑态：左表单 + 右预览两列。 */
.at-edit-grid {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.at-edit-grid > section {
  flex: 1 1 auto;
  min-width: 0;
}

/* 字数计数：右对齐灰字（旧页 0/80 同位）。 */
.at-count {
  display: block;
  text-align: right;
}

/* 封面 150×94 预览框 + 上传/移除（旧页同尺寸）。 */
.at-cover-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
}

.at-cover-box {
  width: 150px;
  height: 94px;
  border: 1px dashed var(--tt2-line);
  border-radius: 3px;
  overflow: hidden;
  background: var(--tt2-tool-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--tt2-placeholder-dim);
  font-size: 12px;
}

.at-cover-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.at-cover-remove {
  margin-left: 6px;
}

/* 正文编辑器：工具栏 + contenteditable 本体（编辑器内动态节点走 :deep）。 */
.at-editor-toolbar {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  border: 1px solid var(--tt2-line);
  border-bottom: none;
  border-radius: 4px 4px 0 0;
  padding: 4px 6px;
  background: var(--tt2-tool-bg);
}

.at-editor {
  min-height: 320px;
  border: 1px solid var(--tt2-line);
  border-radius: 0 0 4px 4px;
  padding: 10px 12px;
  line-height: 26px;
  background: var(--tt2-panel);
  outline: none;
  overflow: auto;
  word-break: break-word;
}

.at-editor:empty:before {
  content: '在这里写正文…';
  color: var(--tt2-placeholder-dim);
}

.at-editor :deep(h2) {
  font-size: 19px;
  line-height: 30px;
  margin: 10px 0 4px;
  font-weight: 700;
}

.at-editor :deep(h3) {
  font-size: 16px;
  line-height: 28px;
  margin: 8px 0 4px;
  font-weight: 700;
}

.at-editor :deep(p) {
  margin: 0 0 8px;
}

.at-editor :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  margin: 6px 0;
}

.at-edit-actions {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}

/* 右侧预览列（旧页 flex:0 0 330px + sticky 同口径）。 */
.at-preview {
  flex: 0 0 330px;
  position: sticky;
  top: 8px;
}

.at-pv-switch {
  display: flex;
  justify-content: center;
  gap: 6px;
  padding-bottom: 6px;
}

/* 手机外框尺寸由模板按 PHONE_* 常量绑定；边框/投影走 token。 */
.at-phone {
  margin: 0 auto;
  border: 10px solid var(--tt2-ink);
  border-radius: 36px;
  overflow: hidden;
  background: var(--tt2-panel);
  box-shadow: 0 6px 18px var(--tt2-flash-shadow);
}

.at-phone-screen {
  transform-origin: top left;
  overflow: auto;
  background: var(--tt2-st-unassigned-bg);
}

/* 预览·文章详情（无封面整块不占位）。 */
.at-pv-detail {
  min-height: 100%;
  background: var(--tt2-panel);
}

.at-pv-coverwrap {
  padding: 12px 16px 0;
}

.at-pv-cover {
  display: block;
  width: 100%;
  height: 180px;
  object-fit: cover;
  border-radius: 6px;
}

.at-pv-titlerow {
  padding: 14px 16px 0;
}

.at-pv-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--tt2-ink);
  line-height: 30px;
  word-break: break-word;
}

.at-pv-date {
  color: var(--tt2-dim);
  font-size: 12px;
  line-height: 22px;
  padding-top: 6px;
}

/* 预览正文：容器与命令式节点全走 :deep（节点由 renderBlocks 造，不带 scoped 属性）。 */
.at-pv-body {
  padding: 10px 16px 20px;
  color: var(--tt2-ink);
  font-size: 15px;
  line-height: 26px;
  word-break: break-word;
}

.at-pv-body :deep(.at-pv-p) {
  margin: 0 0 10px;
}

.at-pv-body :deep(.at-pv-h2) {
  font-size: 19px;
  font-weight: 700;
  margin: 12px 0 6px;
  color: var(--tt2-ink);
}

.at-pv-body :deep(.at-pv-h3) {
  font-size: 16px;
  font-weight: 700;
  margin: 10px 0 4px;
  color: var(--tt2-ink);
}

.at-pv-body :deep(.at-pv-li) {
  position: relative;
  padding-left: 16px;
  margin: 0 0 6px;
}

.at-pv-body :deep(.at-pv-li:before) {
  content: '•';
  position: absolute;
  left: 2px;
  top: 0;
  color: var(--tt2-link-blue);
}

.at-pv-body :deep(.at-pv-imgph) {
  height: 120px;
  background: var(--tt2-st-unassigned-bg);
  color: var(--tt2-placeholder-dim);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  margin: 6px 0;
  font-size: 12px;
}

.at-pv-body :deep(.at-pv-img) {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  margin: 6px 0;
}

.at-pv-footer {
  padding: 14px 16px 26px;
  color: var(--tt2-dim);
  font-size: 12px;
  border-top: 1px solid var(--tt2-line);
}

/* 预览·频道列表（工作室头部 + 卡片；无阅读数——阅读数只在网页端）。 */
.at-pv-channel {
  background: var(--tt2-st-unassigned-bg);
  min-height: 100%;
}

.at-pv-chhead {
  background: var(--tt2-panel);
  padding: 18px 16px 14px;
  display: flex;
  gap: 12px;
  align-items: center;
}

.at-pv-avatar {
  flex: none;
  width: 48px;
  height: 48px;
  border-radius: 10px;
  background: var(--tt2-side);
  color: var(--tt2-on-primary);
  font-size: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.at-pv-chmeta {
  flex: 1 1 auto;
  min-width: 0;
}

.at-pv-chname {
  font-size: 16px;
  font-weight: 700;
  color: var(--tt2-ink);
}

.at-pv-chintro {
  color: var(--tt2-dim);
  font-size: 12px;
  line-height: 20px;
  word-break: break-word;
}

.at-pv-chcount {
  color: var(--tt2-dim);
  font-size: 12px;
  line-height: 20px;
}

.at-pv-cards {
  padding: 10px 12px 20px;
}

.at-card {
  background: var(--tt2-panel);
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 10px;
}

.at-card-cover {
  display: block;
  width: 100%;
  height: 130px;
  background: var(--tt2-st-unassigned-bg);
  object-fit: cover;
}

.at-card-title {
  font-size: 15px;
  color: var(--tt2-ink);
  line-height: 22px;
  padding: 8px 10px 0;
  font-weight: 700;
  word-break: break-word;
}

.at-card-date {
  font-size: 12px;
  color: var(--tt2-dim);
  line-height: 20px;
  padding: 4px 10px 10px;
}

.at-pv-chempty {
  color: var(--tt2-dim);
  text-align: center;
  padding: 24px 0;
}

/* 全局提示 = 居中深条（V2Notices 同款覆盖）。 */
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
</style>
