/**
 * 【APP与小程序】→「发布文章作品」页（island key = `app-mp-articles`，路由 `/app-mp/articles`）。
 *
 * 计划：`docs/plans/2026-10-23-channel-articles.md` §1「网页端页面」/ §2「判据」第 7 条。
 * 契约的另一半是**实现即契约**：`src/apps/business/api/article_views.py` + `apps/business/urls.py`
 * （端点封装见 `@/api/articles`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有【APP与小程序】这一组 ⇒ 本页**没有证据件**、也不走 `mountConfigLike`
 * （那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。标记由本文件自己写，
 * 风格照 `pages/feedback.ts` / `pages/open-apps.ts`（同属自有功能页）。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 这一页干什么
 *
 * 移动端「频道」是**只读**的（业务方 2026-10-23 口径第 2 条）；编辑 / 发布 / 草稿列表都在这里：
 *  - **列表态**：标题 / 状态 / 阅读数 / 更新时间 / 操作（编辑、发布、下架、删除）+ 频道简介；
 *  - **编辑态**：标题（≤80）、简介（≤200）、封面、正文 `contenteditable`（正文 / 小标题 / 加粗 / 插图）；
 *  - **右侧手机预览**（375×812 外框、缩放 0.8、`sticky`）：实时（150ms 防抖）画移动端文章详情，
 *    并可切换「频道列表」视图（工作室头部 + 卡片；**阅读数只在网页端**）。
 *
 * ## 正文的**两条闸**（都不许省）
 *
 * 1. **保存前**：`cleanArticleHtml()` 把 `contenteditable` 的产物过一遍白名单，并把每个 `<img>`
 *    整理成**只有 `data-key`**（删掉 `src` 等属性）。⛔ 不这么做的话，编辑器为了显示图片补上的
 *    `src`（本机是 `/api/media/blob/...` 这种**会过期的相对 URL**）会被写进库——
 *    库里只该有对象名，读链接由服务端现签。
 * 2. **预览**：`parseArticleBlocks()` 是**只用于预览**的轻量解析器，把 `content_html` 解析成块
 *    （`p` / `h2` / `h3` / `li` / `img` + 行内的粗体 / 斜体）。它**只认白名单标签与 `data-key`**，
 *    其余标签一律**拆掉外壳保留文字**、所有属性一律不认；上屏时文本一律走 `textContent`
 *    （⛔ 不拿 `innerHTML` 拼用户内容）。
 *    ⚠️ 落库仍以**服务端清洗**为准（同一份 HTML 会被后端 `article_content.py` 再洗一次）。
 *    ⚠️ 兼容 `<b>` / `<i>` / `<div>`：后端会把它们归一化成 `<strong>` / `<em>` / 段落，
 *    编辑器（`document.execCommand`）在部分浏览器上产出的就是 `<b>`。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(layer.confirm as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）；
 * ④ 页里有定时器（预览防抖 150ms）：假定时器要**先** `vi.useFakeTimers()` **再**包装
 *    `window.setTimeout`，且每个用例自己收口（`mount` 返回的 cleanup 会清掉这个定时器）。
 * ⚠️ 四条都有**运行期**判据（`__tests__/app-mp-articles.spec.ts`），不是靠这段注释。
 *
 * ## ⚠️ 页面上**没有**的东西（都是有意的）
 *
 * - ⛔ **没有权限树 / 游戏范围**：这一页不涉及它们；
 * - ⛔ **没有「阅读数」在移动端预览的频道卡片里**（业务方口径第 5 条：阅读数只在网页端）；
 * - ⛔ **没有定时发布 / 多作者 / 评论**（计划 §3「不做」）。
 */
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
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'app-mp-articles'
export const TITLE = '发布文章作品'

/** 标题 / 简介的字数上限（与后端模型一致）。 */
export const TITLE_MAX = 80
export const INTRO_MAX = 200
/** 单张图片上限：与 `article-media` 的 10 MB 口径一致（权威判据在后端）。 */
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024
/** 允许的图片扩展名（后端同一集合；MIME 只用于「名字里没有扩展名」时兜底）。 */
export const ALLOWED_EXTS = ['jpg', 'jpeg', 'png'] as const
const MIME_EXT: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpeg',
}

// ── 类名与选择器**分成两个常量**（陷阱 ①：`class="${SELECTOR}"` 会造出 `class=".x"`）──────

/** 列表态 / 编辑态的容器（拼标记用 CLASS、查节点用 SELECTOR）。 */
export const LIST_CLASS = 'ampList'
export const LIST_SELECTOR = `.${LIST_CLASS}`
export const EDIT_CLASS = 'ampEdit'
export const EDIT_SELECTOR = `.${EDIT_CLASS}`
/** 右侧预览容器。 */
export const PREVIEW_CLASS = 'ampPreview'
export const PREVIEW_SELECTOR = `.${PREVIEW_CLASS}`

// ── 各节点的 `id` ────────────────────────────────────────────────────────────

/** 列表态。 */
export const NEW_BUTTON_ID = 'ampNewBtn'
export const LIST_STATE_ID = 'ampListState'
export const LIST_TABLE_ID = 'ampListTable'
export const LIST_BODY_ID = 'ampListBody'
export const PROFILE_NAME_ID = 'ampProfileName'
export const PROFILE_COUNT_ID = 'ampProfileCount'
export const PROFILE_INTRO_ID = 'ampProfileIntro'
export const PROFILE_SAVE_ID = 'ampProfileSave'
export const PROFILE_WARN_ID = 'ampProfileWarn'

/** 编辑态。 */
export const BACK_BUTTON_ID = 'ampBackBtn'
export const TITLE_INPUT_ID = 'ampArticleTitle'
export const TITLE_COUNT_ID = 'ampTitleCount'
export const INTRO_INPUT_ID = 'ampArticleIntro'
export const INTRO_COUNT_ID = 'ampIntroCount'
export const COVER_PICK_ID = 'ampCoverPick'
export const COVER_FILE_ID = 'ampCoverFile'
export const COVER_PREVIEW_ID = 'ampCoverPreview'
export const COVER_REMOVE_ID = 'ampCoverRemove'
export const EDITOR_ID = 'ampEditor'
export const EDITOR_TOOLBAR_ID = 'ampEditorToolbar'
export const BODY_FILE_ID = 'ampBodyFile'
export const SAVE_DRAFT_BUTTON_ID = 'ampSaveDraftBtn'
export const PUBLISH_BUTTON_ID = 'ampPublishBtn'
export const CANCEL_BUTTON_ID = 'ampCancelBtn'

/** 工具栏按钮上的**属性名**（陷阱 ① 的同族：属性名进标记、选择器 `[${EDITOR_CMD_ATTR}]` 找节点）。 */
export const EDITOR_CMD_ATTR = 'data-cmd'
/** 插图那颗按钮的取值。 */
export const CMD_IMAGE = 'image'

/** 行操作按钮上的属性（值 = 动作名 / 文章 id）。 */
export const ARTICLE_ACTION_ATTR = 'data-article-action'
export const ARTICLE_ID_ATTR = 'data-article-id'
/** 列表失败态里「重试」那颗按钮的属性。 */
export const LIST_RETRY_ATTR = 'data-article-retry'

/** 预览里「文章详情 / 频道列表」两颗切换按钮的属性。 */
export const PREVIEW_VIEW_ATTR = 'data-preview-view'
export type PreviewView = 'detail' | 'channel'

/** 右侧预览。 */
export const PREVIEW_DETAIL_ID = 'ampPreviewDetail'
export const PREVIEW_CHANNEL_ID = 'ampPreviewChannel'
export const PREVIEW_TITLE_ID = 'ampPreviewTitle'
export const PREVIEW_DATE_ID = 'ampPreviewDate'
export const PREVIEW_COVER_ID = 'ampPreviewCover'
export const PREVIEW_BODY_ID = 'ampPreviewBody'
export const PREVIEW_FOOTER_ID = 'ampPreviewFooter'
export const CHANNEL_AVATAR_ID = 'ampChannelAvatar'
export const CHANNEL_NAME_ID = 'ampChannelName'
export const CHANNEL_INTRO_ID = 'ampChannelIntro'
export const CHANNEL_COUNT_ID = 'ampChannelCount'
export const CHANNEL_CARDS_ID = 'ampChannelCards'
export const CHANNEL_EMPTY_ID = 'ampChannelEmpty'

/** 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
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
} as const

/** 预览里手机外框的尺寸（业务方口径：375×812，缩放 ~0.8）。 */
export const PHONE_WIDTH = 375
export const PHONE_HEIGHT = 812
export const PHONE_SCALE = 0.8
/** 预览防抖（毫秒）。 */
export const PREVIEW_DEBOUNCE_MS = 150

/** 一篇文章里的**行内片段**（预览用；只保留粗体 / 斜体两个标记）。 */
export type ArticleSpan = { text: string; bold: boolean; italic: boolean }

/**
 * 预览里的一块正文。
 *
 * ⚠️ 只有一个 `img` 变体带 `key` / `url`；文本块不带。`li` 块在预览里画成一个带圆点的块
 * （⛔ 不拼 `<ul>`：单块渲染更简单，也不会因为跨块漏掉 `</ul>`）。
 */
export type ArticleBlock =
  | { type: 'p' | 'h2' | 'h3' | 'li'; spans: ArticleSpan[] }
  | { type: 'img'; key: string; url: string }

/** 白名单标签（保存 / 预览共用一份；`b` / `i` / `div` 先归一化再进集合）。 */
const ALLOWED_TAGS = new Set(['p', 'h2', 'h3', 'strong', 'em', 'br', 'img', 'ul', 'ol', 'li'])
/** 会被当成「块」的标签（预览里各自成块）。 */
const BLOCK_TAGS = new Set(['p', 'h2', 'h3'])
/**
 * 连**内容一起丢掉**的标签（白名单之外的一类特例）。
 *
 * ⚠️ 普通未知标签是「拆壳保留文字」，但这几个不行：把 `<script>` 的源码当正文显示出来
 * 既难看、也不是用户写的内容；`<style>` / `<iframe>` 等同理。⛔ 它们**永远不该**出现在
 * 白名单里，所以这里直接整块丢弃（保存与预览两侧共用这一份名单）。
 */
const DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript'])
/** 归一化：编辑器 / 旧数据里可能出现的等价标签。 */
function normalizeTag(tag: string): string {
  if (tag === 'b') return 'strong'
  if (tag === 'i') return 'em'
  if (tag === 'div') return 'p'
  return tag
}

/** 是否块级容器（`div` 只有在**子节点里没有块**时才当段落；否则拆开递归）。 */
function hasBlockChild(element: Element): boolean {
  return Array.from(element.children).some((child) => {
    const tag = child.tagName.toLowerCase()
    return BLOCK_TAGS.has(tag) || tag === 'ul' || tag === 'ol' || tag === 'li' || tag === 'div'
  })
}

/**
 * 正文 HTML → **只含白名单标签 / 只保留 `data-key`** 的 HTML（保存前必过）。
 *
 * - `<b>` / `<i>` 归一化成 `<strong>` / `<em>`（后端也这么归一化，前端先对齐省一次惊讶）；
 * - `<div>`（无块子节点时）当段落；
 * - 非白名单标签**拆壳保留文字**（⛔ 不整段丢掉——用户写的内容不该因为一个 `<span>` 消失）；
 * - `<img>`：**只留 `data-key`**，`src` / `alt` / `class` / 事件属性全丢。没有 `data-key` 的图
 *   直接丢掉（它不可能被后端认领，留着只会在移动端变成一个破图）。
 */
export function cleanArticleHtml(html: string): string {
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

/**
 * 把正文里的图片补上 `src`（**只用于编辑器显示 / 预览，⛔ 不写回库**）。
 *
 * 顺序：先过 `cleanArticleHtml`（把 `src` 与白名单外的东西清掉），再用 `media[key]` 补 `src`。
 * ⇒ 编辑器里看得到图，而保存时重新走 `cleanArticleHtml` 又会把 `src` 去掉。
 */
export function hydrateArticleHtml(html: string, media: Readonly<Record<string, string>>): string {
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

/**
 * `content_html` → 预览用的块（**只用于预览**；落库仍以服务端清洗为准）。
 *
 * 只认白名单：`p` / `h2` / `h3` / `li` / `img` + 行内 `strong` / `em` / `br`；
 * `b` / `i` / `div` 兼容进来（见文件头）。**所有属性都不认**，图片只认 `data-key`，
 * 读链接只从 `media` 那张表取（⛔ 不读 HTML 里的 `src`——那是会过期的链接，也可能是脏的）。
 */
export function parseArticleBlocks(
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

/** ISO 8601 → `2026-10-23 10:11:00`（只截到秒；时区偏移留在原地）。`null` ⇒ `—`。 */
export function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/** ISO / 日期 → `2026-10-23`（移动端卡片与详情只到日）。 */
export function dateText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.slice(0, 10)
}

/** 今天（本地时区）的 `YYYY-MM-DD`——文章详情的日期就是「今天」。 */
export function todayText(now: Date = new Date()): string {
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** 状态的中文名（**只用于网页端**；移动端不显示状态）。 */
export function statusText(status: ArticleStatus): string {
  return status === 'published' ? '已发布' : '草稿'
}

/** 取文件名的扩展名（小写、去点；没有就回空串）。 */
export function extOf(name: string): string {
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return ''
  return name.slice(dot + 1).toLowerCase()
}

/**
 * 这次要发给签发端点的扩展名（`null` = 判断不出可用的那一种）。
 * 先看文件名，名字里没有就按 MIME 兜底（粘贴 / 远程桌面里 `File.name` 可能是空的）。
 */
export function resolveExt(file: File): string | null {
  const byName = extOf(file.name)
  if ((ALLOWED_EXTS as readonly string[]).includes(byName)) return byName
  return MIME_EXT[(file.type ?? '').toLowerCase()] ?? null
}

/** 选文件时的前置闸（返回要说哪一句；`null` = 放行）。权威判据仍然后端。 */
export function rejectReason(file: File): string | null {
  if (resolveExt(file) === null) return MESSAGES.imageBadType
  if (file.size > MAX_IMAGE_BYTES) return MESSAGES.imageTooLarge
  return null
}

/** 工作室名的首字（频道头部的头像）。空名回 `?`。 */
export function initialOf(name: string): string {
  return name.trim().slice(0, 1) || '?'
}

/**
 * 页面标记（列表态 + 编辑态 + 右侧手机预览）。
 *
 * ⚠️ 两块都留在 DOM 里、靠 `display` 切换：`exitEdit()` 不重建标记（编辑器里的监听与选区
 * 都挂在节点自己身上，重建会把它们丢掉）。
 */
export function buildHtml(): string {
  const phoneScreenWidth = PHONE_WIDTH
  const phoneScreenHeight = PHONE_HEIGHT
  return `
<div class="${LIST_CLASS}" id="ampListSection">
  <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0 10px">
    <h2 style="font-size:16px;color:#333">${TITLE}</h2>
    <button type="button" class="layui-btn layui-btn-sm" id="${NEW_BUTTON_ID}">新建文章</button>
  </div>
  <blockquote class="layui-elem-quote layui-quote-nm" style="padding:10px 14px">
    <div style="display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap">
      <div style="flex:0 0 auto;line-height:24px">
        <b id="${PROFILE_NAME_ID}"></b>
        <span style="color:#999" id="${PROFILE_COUNT_ID}"></span>
      </div>
      <div style="flex:1 1 320px;min-width:260px">
        <textarea id="${PROFILE_INTRO_ID}" class="layui-textarea" rows="2" maxlength="200"
                  placeholder="频道简介（移动端频道头部显示，最多 200 字）"></textarea>
      </div>
      <div style="flex:0 0 auto">
        <button type="button" class="layui-btn layui-btn-sm layui-btn-normal" id="${PROFILE_SAVE_ID}">
          保存简介
        </button>
      </div>
    </div>
    <div id="${PROFILE_WARN_ID}" style="color:#FF5722;line-height:20px;padding-top:4px"></div>
  </blockquote>
  <div id="${LIST_STATE_ID}" style="color:#666;line-height:26px;padding:6px 0"></div>
  <table class="layui-table" id="${LIST_TABLE_ID}" style="margin-top:0">
    <colgroup>
      <col>
      <col style="width:90px">
      <col style="width:90px">
      <col style="width:170px">
      <col style="width:220px">
    </colgroup>
    <thead>
      <tr>
        <th>标题</th><th>状态</th><th>阅读数</th><th>更新时间</th><th>操作</th>
      </tr>
    </thead>
    <tbody id="${LIST_BODY_ID}"></tbody>
  </table>
</div>

<div class="${EDIT_CLASS}" id="ampEditSection" style="display:none">
  <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0 10px">
    <h2 style="font-size:16px;color:#333">${TITLE}</h2>
    <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${BACK_BUTTON_ID}">
      返回列表
    </button>
  </div>
  <div style="display:flex;gap:16px;align-items:flex-start">
    <div style="flex:1 1 auto;min-width:0">
      <div class="layui-form-item" style="margin-bottom:10px">
        <label class="layui-form-label" style="width:64px">标题</label>
        <div class="layui-input-block" style="margin-left:94px">
          <input type="text" id="${TITLE_INPUT_ID}" class="layui-input" maxlength="${TITLE_MAX}"
                 autocomplete="off" placeholder="文章标题（移动端列表显示）">
          <div style="color:#999;line-height:20px;text-align:right" id="${TITLE_COUNT_ID}">0/${TITLE_MAX}</div>
        </div>
      </div>
      <div class="layui-form-item" style="margin-bottom:10px">
        <label class="layui-form-label" style="width:64px">简介</label>
        <div class="layui-input-block" style="margin-left:94px">
          <textarea id="${INTRO_INPUT_ID}" class="layui-textarea" rows="2" maxlength="${INTRO_MAX}"
                    placeholder="文章摘要（可选，最多 ${INTRO_MAX} 字）"></textarea>
          <div style="color:#999;line-height:20px;text-align:right" id="${INTRO_COUNT_ID}">0/${INTRO_MAX}</div>
        </div>
      </div>
      <div class="layui-form-item" style="margin-bottom:10px">
        <label class="layui-form-label" style="width:64px">封面</label>
        <div class="layui-input-block" style="margin-left:94px">
          <div style="display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap">
            <div id="${COVER_PREVIEW_ID}" style="width:150px;height:94px;border:1px dashed #ccc;
                 border-radius:3px;overflow:hidden;background:#fafafa;display:flex;
                 align-items:center;justify-content:center;color:#bbb"></div>
            <div>
              <button type="button" class="layui-btn layui-btn-sm" id="${COVER_PICK_ID}">上传封面</button>
              <button type="button" class="layui-btn layui-btn-sm layui-btn-primary"
                      id="${COVER_REMOVE_ID}">移除</button>
              <div style="color:#999;line-height:20px">支持 png / jpg / jpeg，单张不超过 10 MB</div>
            </div>
          </div>
          <input type="file" id="${COVER_FILE_ID}" accept="image/png,image/jpeg" style="display:none">
        </div>
      </div>
      <div class="layui-form-item" style="margin-bottom:10px">
        <label class="layui-form-label" style="width:64px">正文</label>
        <div class="layui-input-block" style="margin-left:94px">
          <div id="${EDITOR_TOOLBAR_ID}" style="border:1px solid #e6e6e6;border-bottom:none;
               padding:4px 6px;background:#fafafa">
            <button type="button" class="layui-btn layui-btn-xs" ${EDITOR_CMD_ATTR}="p">正文</button>
            <button type="button" class="layui-btn layui-btn-xs" ${EDITOR_CMD_ATTR}="h2">小标题</button>
            <button type="button" class="layui-btn layui-btn-xs" ${EDITOR_CMD_ATTR}="bold">加粗</button>
            <button type="button" class="layui-btn layui-btn-xs layui-btn-normal"
                    ${EDITOR_CMD_ATTR}="${CMD_IMAGE}">插图</button>
          </div>
          <div id="${EDITOR_ID}" contenteditable="true"
               style="min-height:320px;border:1px solid #e6e6e6;padding:10px 12px;line-height:26px;
                      background:#fff;outline:none;overflow:auto"></div>
          <div style="color:#999;line-height:20px">插图走与封面同一条直传链路；保存时只保留对象名。</div>
          <input type="file" id="${BODY_FILE_ID}" accept="image/png,image/jpeg" style="display:none">
        </div>
      </div>
      <div style="padding:6px 0 24px 94px">
        <button type="button" class="layui-btn" id="${SAVE_DRAFT_BUTTON_ID}">保存草稿</button>
        <button type="button" class="layui-btn layui-btn-normal" id="${PUBLISH_BUTTON_ID}">发布</button>
        <button type="button" class="layui-btn layui-btn-primary" id="${CANCEL_BUTTON_ID}">取消</button>
      </div>
    </div>

    <div class="${PREVIEW_CLASS}" style="flex:0 0 330px;position:sticky;top:8px">
      <div style="text-align:center;padding-bottom:6px">
        <button type="button" class="layui-btn layui-btn-xs" ${PREVIEW_VIEW_ATTR}="detail">
          文章详情
        </button>
        <button type="button" class="layui-btn layui-btn-xs layui-btn-primary"
                ${PREVIEW_VIEW_ATTR}="channel">频道列表</button>
      </div>
      <div style="width:${Math.round(PHONE_WIDTH * PHONE_SCALE)}px;
                  height:${Math.round(PHONE_HEIGHT * PHONE_SCALE)}px;margin:0 auto;
                  border:10px solid #222;border-radius:36px;overflow:hidden;background:#fff;
                  box-shadow:0 6px 18px rgba(0,0,0,.18)">
        <div style="width:${phoneScreenWidth}px;height:${phoneScreenHeight}px;transform:scale(${PHONE_SCALE});
                    transform-origin:top left;overflow:auto;background:#f5f5f5">
          <div id="${PREVIEW_DETAIL_ID}" style="min-height:100%;background:#fff">
            <div style="padding:12px 16px 0">
              <img id="${PREVIEW_COVER_ID}" alt="" style="display:none;width:100%;height:180px;
                   object-fit:cover;border-radius:6px">
            </div>
            <div style="padding:14px 16px 0">
              <div id="${PREVIEW_TITLE_ID}" style="font-size:22px;font-weight:700;color:#222;
                   line-height:30px;word-break:break-word">无标题</div>
              <div id="${PREVIEW_DATE_ID}" style="color:#999;font-size:12px;line-height:22px;
                   padding-top:6px"></div>
            </div>
            <div id="${PREVIEW_BODY_ID}" style="padding:10px 16px 20px;color:#333;font-size:15px;
                 line-height:26px;word-break:break-word"></div>
            <div id="${PREVIEW_FOOTER_ID}" style="padding:14px 16px 26px;color:#999;font-size:12px;
                 border-top:1px solid #f0f0f0"></div>
          </div>
          <div id="${PREVIEW_CHANNEL_ID}" style="display:none;background:#f5f5f5;min-height:100%">
            <div style="background:#fff;padding:18px 16px 14px;display:flex;gap:12px;align-items:center">
              <div id="${CHANNEL_AVATAR_ID}" style="width:48px;height:48px;border-radius:10px;
                   background:#15171A;color:#fff;font-size:22px;display:flex;align-items:center;
                   justify-content:center"></div>
              <div style="flex:1 1 auto;min-width:0">
                <div id="${CHANNEL_NAME_ID}" style="font-size:16px;font-weight:700;color:#222"></div>
                <div id="${CHANNEL_INTRO_ID}" style="color:#999;font-size:12px;line-height:20px;
                     word-break:break-word"></div>
                <div id="${CHANNEL_COUNT_ID}" style="color:#9AA1AB;font-size:12px;line-height:20px"></div>
              </div>
            </div>
            <div id="${CHANNEL_CARDS_ID}" style="padding:10px 12px 20px"></div>
            <div id="${CHANNEL_EMPTY_ID}" style="display:none;color:#999;text-align:center;
                 padding:24px 0">${MESSAGES.noPublished}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<style>
  #${EDITOR_ID}:empty:before{content:'在这里写正文…';color:#ccc}
  #${EDITOR_ID} h2{font-size:19px;line-height:30px;margin:10px 0 4px;font-weight:700}
  #${EDITOR_ID} h3{font-size:16px;line-height:28px;margin:8px 0 4px;font-weight:700}
  #${EDITOR_ID} p{margin:0 0 8px}
  #${EDITOR_ID} img{max-width:100%;height:auto;border-radius:4px;margin:6px 0}
  .ampPreviewP{margin:0 0 10px}
  .ampPreviewH2{font-size:19px;font-weight:700;margin:12px 0 6px;color:#222}
  .ampPreviewH3{font-size:16px;font-weight:700;margin:10px 0 4px;color:#222}
  .ampPreviewLi{position:relative;padding-left:16px;margin:0 0 6px}
  .ampPreviewLi:before{content:'•';position:absolute;left:2px;top:0;color:#1E9FFF}
  .ampCard{background:#fff;border-radius:6px;overflow:hidden;margin-bottom:10px;
    box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .ampCardCover{width:100%;height:130px;background:#f2f2f2;color:#bbb;display:flex;
    align-items:center;justify-content:center;font-size:12px;object-fit:cover}
  .ampCardTitle{font-size:15px;color:#222;line-height:22px;padding:8px 10px 0;
    font-weight:700;word-break:break-word}
  .ampCardDate{font-size:12px;color:#999;line-height:20px;padding:4px 10px 10px}
</style>`
}

export const __testables = {
  buildHtml,
  cleanArticleHtml,
  dateText,
  extOf,
  hydrateArticleHtml,
  initialOf,
  parseArticleBlocks,
  rejectReason,
  resolveExt,
  statusText,
  timeText,
  todayText,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），
    //    再塞页面标记，最后 `initIslandElements`（要按标记里的 `lay-*` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['layer'])
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    const byId = <T extends HTMLElement>(id: string): T => {
      const node = root.querySelector<T>(`#${id}`)
      if (node === null) throw new Error(`页面里没有 #${id}`)
      return node
    }
    const listSection = byId<HTMLElement>('ampListSection')
    const editSection = byId<HTMLElement>('ampEditSection')
    const listState = byId<HTMLElement>(LIST_STATE_ID)
    const listTable = byId<HTMLElement>(LIST_TABLE_ID)
    const listBody = byId<HTMLElement>(LIST_BODY_ID)
    const profileName = byId<HTMLElement>(PROFILE_NAME_ID)
    const profileCount = byId<HTMLElement>(PROFILE_COUNT_ID)
    const profileIntro = byId<HTMLTextAreaElement>(PROFILE_INTRO_ID)
    const profileWarn = byId<HTMLElement>(PROFILE_WARN_ID)
    const titleInput = byId<HTMLInputElement>(TITLE_INPUT_ID)
    const titleCount = byId<HTMLElement>(TITLE_COUNT_ID)
    const introInput = byId<HTMLTextAreaElement>(INTRO_INPUT_ID)
    const introCount = byId<HTMLElement>(INTRO_COUNT_ID)
    const coverPreview = byId<HTMLElement>(COVER_PREVIEW_ID)
    const coverRemove = byId<HTMLButtonElement>(COVER_REMOVE_ID)
    const coverFile = byId<HTMLInputElement>(COVER_FILE_ID)
    const bodyFile = byId<HTMLInputElement>(BODY_FILE_ID)
    const toolbar = byId<HTMLElement>(EDITOR_TOOLBAR_ID)
    const editor = byId<HTMLElement>(EDITOR_ID)
    const previewTitle = byId<HTMLElement>(PREVIEW_TITLE_ID)
    const previewDate = byId<HTMLElement>(PREVIEW_DATE_ID)
    const previewCover = byId<HTMLImageElement>(PREVIEW_COVER_ID)
    const previewBody = byId<HTMLElement>(PREVIEW_BODY_ID)
    const previewFooter = byId<HTMLElement>(PREVIEW_FOOTER_ID)
    const previewDetail = byId<HTMLElement>(PREVIEW_DETAIL_ID)
    const previewChannel = byId<HTMLElement>(PREVIEW_CHANNEL_ID)
    const channelAvatar = byId<HTMLElement>(CHANNEL_AVATAR_ID)
    const channelName = byId<HTMLElement>(CHANNEL_NAME_ID)
    const channelIntro = byId<HTMLElement>(CHANNEL_INTRO_ID)
    const channelCount = byId<HTMLElement>(CHANNEL_COUNT_ID)
    const channelCards = byId<HTMLElement>(CHANNEL_CARDS_ID)
    const channelEmpty = byId<HTMLElement>(CHANNEL_EMPTY_ID)

    /** 列表当前数据（列表态与频道预览共用这一份）。 */
    let articles: ArticleRow[] = []
    /** 频道简介（工作室名 / 简介 / 已发布篇数）。 */
    let profile: ChannelProfile = { name: '工作室', intro: '', article_count: 0 }
    /** 预览正在看哪个视图。 */
    let previewView: PreviewView = 'detail'
    /** 编辑中的文章 id（`null` = 新建）。 */
    let editingId: number | null = null
    /** 编辑中的文章**进入编辑时**的状态（决定「保存草稿」要不要保住已发布）。 */
    let editingStatus: ArticleStatus = 'draft'
    /** 正在编辑的文章的首次发布时间（ISO）；`null` = 草稿 / 新建 ⇒ 预览显示「未发布」。 */
    let editingPublishedAt: string | null = null
    /** 这一篇的 `media` 表（对象名 → 读链接）。 */
    let media: Record<string, string> = {}
    /** 封面的三态：`null` = 没改过 / `''` = 明确移除 / 其它 = 新 key。 */
    let coverKey: string | null = null
    /** 封面当前显示用的读链接（新上传的取认领结果，已有的取 `cover_url`）。 */
    let coverUrl = ''
    /** 在途闸门（防连点写两次）。 */
    let saving = false
    /** 预览防抖定时器（cleanup 里要清掉）。 */
    let previewTimer: number | null = null

    const errorText = (error: unknown): string =>
      error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed

    // ── 列表三态（加载中 / 失败重试 / 空）────────────────────────────────────

    function setListState(kind: 'none' | 'loading' | 'empty' | 'error' | 'warn', text = ''): void {
      listState.textContent = ''
      if (kind === 'none') {
        listState.style.display = 'none'
        return
      }
      listState.style.display = ''
      if (kind === 'error') {
        const span = document.createElement('span')
        span.textContent = text
        const retry = document.createElement('button')
        retry.type = 'button'
        retry.className = 'layui-btn layui-btn-xs layui-btn-normal'
        retry.setAttribute(LIST_RETRY_ATTR, '1')
        retry.style.marginLeft = '8px'
        retry.textContent = '重试'
        listState.appendChild(span)
        listState.appendChild(retry)
        return
      }
      listState.textContent = text
    }

    function actionButton(
      label: string,
      action: string,
      id: number,
      extraClass = '',
    ): HTMLButtonElement {
      const button = document.createElement('button')
      button.type = 'button'
      button.className = `layui-btn layui-btn-xs ${extraClass}`.trim()
      button.setAttribute(ARTICLE_ACTION_ATTR, action)
      button.setAttribute(ARTICLE_ID_ATTR, String(id))
      button.textContent = label
      button.style.marginRight = '4px'
      return button
    }

    function renderListRows(): void {
      listBody.textContent = ''
      for (const row of articles) {
        const tr = document.createElement('tr')
        const titleCell = document.createElement('td')
        titleCell.textContent = row.title
        const statusCell = document.createElement('td')
        const status = document.createElement('span')
        status.textContent = statusText(row.status)
        status.style.color = row.status === 'published' ? '#009688' : '#FF9800'
        statusCell.appendChild(status)
        const readCell = document.createElement('td')
        // ⭐ 阅读数**只在网页端**显示（业务方口径第 5 条）。
        readCell.textContent = String(row.read_count)
        const updatedCell = document.createElement('td')
        updatedCell.textContent = timeText(row.updated_at)
        const actionCell = document.createElement('td')
        actionCell.appendChild(actionButton('编辑', 'edit', row.id))
        if (row.status === 'draft') {
          actionCell.appendChild(actionButton('发布', 'publish', row.id, 'layui-btn-normal'))
        } else {
          actionCell.appendChild(actionButton('下架', 'unpublish', row.id, 'layui-btn-primary'))
        }
        actionCell.appendChild(actionButton('删除', 'delete', row.id, 'layui-btn-danger'))
        tr.appendChild(titleCell)
        tr.appendChild(statusCell)
        tr.appendChild(readCell)
        tr.appendChild(updatedCell)
        tr.appendChild(actionCell)
        listBody.appendChild(tr)
      }
    }

    /** 唯一的取数出口：列表 + 三态。 */
    async function loadList(): Promise<void> {
      setListState('loading', MESSAGES.loading)
      listTable.style.display = 'none'
      try {
        const payload = await listArticles()
        articles = payload.items
        renderListRows()
        listTable.style.display = ''
        if (articles.length === 0) {
          setListState('empty', MESSAGES.empty)
        } else if (payload.total > articles.length) {
          // ⚠️ 列表一次只取最近 200 篇；**不静默截断**（老文章改不到 = 隐性数据损失）。
          setListState('warn', `只显示最近 ${articles.length} 篇（共 ${payload.total} 篇），更早的请看后续分页`)
        } else {
          setListState('none')
        }
      } catch (error) {
        articles = []
        renderListRows()
        listTable.style.display = 'none'
        setListState('error', errorText(error))
      }
      renderPreview()
    }

    // ── 频道简介（列表态顶部）────────────────────────────────────────────────

    function renderProfile(): void {
      profileName.textContent = profile.name === '' ? '工作室' : profile.name
      profileCount.textContent = ` · ${profile.article_count} 篇文章`
      profileIntro.value = profile.intro
      renderPreview()
    }

    async function loadProfile(): Promise<void> {
      try {
        profile = await getChannelProfile()
        profileWarn.textContent = ''
      } catch (error) {
        // ⚠️ 简介取不到**不挡住列表**（它是频道头部的一块），但必须说出来：
        //    静默显示成「0 篇文章 / 空简介」会被当成真的。
        profileWarn.textContent = `频道简介没取到：${errorText(error)}`
      }
      renderProfile()
    }

    async function saveProfile(): Promise<void> {
      try {
        profile = await updateChannelProfile(profileIntro.value.trim())
        renderProfile()
        msg(MESSAGES.profileSaved)
      } catch (error) {
        msg(errorText(error))
      }
    }

    // ── 预览：详情 + 频道列表 ───────────────────────────────────────────────

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

    function renderBlocks(target: HTMLElement, blocks: readonly ArticleBlock[]): void {
      target.textContent = ''
      for (const block of blocks) {
        if (block.type === 'img') {
          if (block.url === '') {
            const placeholder = document.createElement('div')
            placeholder.style.cssText =
              'height:120px;background:#f2f2f2;color:#bbb;display:flex;align-items:center;' +
              'justify-content:center;border-radius:4px;margin:6px 0;font-size:12px'
            placeholder.textContent = '图片'
            target.appendChild(placeholder)
            continue
          }
          const img = document.createElement('img')
          img.src = block.url
          img.alt = ''
          img.style.cssText = 'max-width:100%;height:auto;border-radius:4px;margin:6px 0'
          target.appendChild(img)
          continue
        }
        const tag = block.type === 'li' ? 'div' : block.type
        const element = document.createElement(tag)
        element.className =
          block.type === 'li'
            ? 'ampPreviewLi'
            : block.type === 'h2'
              ? 'ampPreviewH2'
              : block.type === 'h3'
                ? 'ampPreviewH3'
                : 'ampPreviewP'
        appendSpans(element, block.spans)
        target.appendChild(element)
      }
    }

    /**
     * 频道列表要画的文章：**只画已发布**（草稿在公开频道里不存在），
     * 顺序与移动端公开接口同源：`published_at` 倒序（⛔ 不是管理端列表的 `updated_at`）。
     */
    function channelArticles(): ArticleRow[] {
      const published = articles
        .filter((row) => row.status === 'published')
        .sort((a, b) => (b.published_at ?? '').localeCompare(a.published_at ?? ''))
      if (editingId === null || editingStatus !== 'published') return published
      // 正在编辑一篇**已发布**的文章 ⇒ 标题 / 封面用编辑器里的当前值（频道预览也跟着实时）。
      const title = titleInput.value.trim() === '' ? '无标题' : titleInput.value.trim()
      return published.map((row) =>
        row.id === editingId ? { ...row, title, cover_url: coverUrl } : row,
      )
    }

    function renderChannelView(): void {
      const name = profile.name === '' ? '工作室' : profile.name
      channelAvatar.textContent = initialOf(name)
      channelName.textContent = name
      channelIntro.textContent = profile.intro.trim() === '' ? MESSAGES.noIntro : profile.intro
      channelCards.textContent = ''
      const rows = channelArticles()
      // ⚠️ 管理端 `article_count` 与公开端**同义**（只数已发布，后端 2026-10-23 已改准）；
      //    移动端头部用的就是它 ⇒ 预览必须同源。列表最多取 200 篇，超出的部分靠这个数体现。
      //    `Math.max` 只兜「profile 还没加载完」的瞬间，⛔ 不是拿行数冒充总篇数。
      channelCount.textContent = `${Math.max(profile.article_count, rows.length)} 篇文章`
      channelEmpty.style.display = rows.length === 0 ? '' : 'none'
      for (const row of rows) {
        const card = document.createElement('div')
        card.className = 'ampCard'
        // ⚠️ 移动端无封面时**整块不占位**（不是灰框 / 「暂无封面」）⇒ 预览照做。
        if (row.cover_url !== '') {
          const cover = document.createElement('img')
          cover.className = 'ampCardCover'
          cover.loading = 'lazy'
          cover.src = row.cover_url
          cover.alt = ''
          card.appendChild(cover)
        }
        const title = document.createElement('div')
        title.className = 'ampCardTitle'
        title.textContent = row.title
        const date = document.createElement('div')
        date.className = 'ampCardDate'
        date.textContent = dateText(row.published_at ?? row.created_at)
        card.appendChild(title)
        card.appendChild(date)
        channelCards.appendChild(card)
      }
    }

    /** 预览的**唯一渲染出口**（详情 + 频道两块一起刷，切视图不会看到旧内容）。 */
    function renderPreview(): void {
      previewTitle.textContent = titleInput.value.trim() === '' ? '无标题' : titleInput.value.trim()
      // 与移动端同源：已发布用 `published_at`；草稿 / 新建在真机上不会出现 ⇒ 明写「未发布」。
      previewDate.textContent =
        editingPublishedAt === null ? '未发布' : dateText(editingPublishedAt)
      if (coverUrl === '') {
        // 移动端无封面时**整块不占位**（`message.uvue` / `article.uvue` 同口径）⇒ 预览也不画灰框。
        previewCover.style.display = 'none'
        previewCover.removeAttribute('src')
      } else {
        previewCover.src = coverUrl
        previewCover.style.display = ''
      }
      previewFooter.textContent = `本文由 ${profile.name === '' ? '工作室' : profile.name} 发布`
      renderBlocks(previewBody, parseArticleBlocks(cleanArticleHtml(editor.innerHTML), media))
      renderChannelView()
      previewDetail.style.display = previewView === 'detail' ? '' : 'none'
      previewChannel.style.display = previewView === 'channel' ? '' : 'none'
    }

    function schedulePreview(): void {
      if (previewTimer !== null) window.clearTimeout(previewTimer)
      previewTimer = window.setTimeout(() => {
        previewTimer = null
        renderPreview()
      }, PREVIEW_DEBOUNCE_MS)
    }

    // ── 封面的显示 ───────────────────────────────────────────────────────────

    function renderCover(): void {
      coverPreview.textContent = ''
      if (coverUrl === '') {
        coverPreview.textContent = '暂无封面'
        coverRemove.style.display = 'none'
        return
      }
      const img = document.createElement('img')
      img.src = coverUrl
      img.alt = '封面'
      img.style.cssText = 'width:100%;height:100%;object-fit:cover'
      coverPreview.appendChild(img)
      coverRemove.style.display = ''
    }

    // ── 媒体上传（取票 → PUT 原字节 → 认领）──────────────────────────────────

    /**
     * 直传目标：`upload_url` 是**相对路径**时补上后端前缀。
     *
     * ⚠️ 本机形态 `upload_url` 是 `/api/media/blob/<key>`（同源），生产是 COS 预签名**绝对地址**。
     * 两者都直接交给 `putFileToStorage`；只有部署把接口挂在 `VITE_API_BASE`（例如 `/sub`）下时，
     * 相对地址才需要补前缀——⛔ 不要在这里另写一套 PUT（同源补 CSRF / 跨源不带我们的头那条判据
     * 只在 `directUpload.ts` 里有一份）。
     */
    function backendUploadUrl(uploadUrl: string): string {
      const base = import.meta.env.VITE_API_BASE ?? ''
      if (base === '' || !uploadUrl.startsWith('/')) return uploadUrl
      return `${base.replace(/\/$/, '')}${uploadUrl}`
    }

    /** 上传一张图并认领。返回 `null` = 被前置闸拦下 / 已提示过。 */
    async function uploadMedia(file: File): Promise<{ key: string; url: string } | null> {
      const reason = rejectReason(file)
      if (reason !== null) {
        msg(reason)
        return null
      }
      const ext = resolveExt(file)
      if (ext === null) {
        msg(MESSAGES.imageBadType)
        return null
      }
      const ticket = await issueArticleMediaTicket(ext)
      // ⚠️ 直传这一跳照旧走**全仓唯一**的实现（`directUpload.ts`）：同源补 CSRF、跨源不带我们的头。
      await putFileToStorage(backendUploadUrl(ticket.upload_url), file)
      return await claimArticleMedia(ticket.key)
    }

    async function onCoverFile(): Promise<void> {
      const file = coverFile.files?.[0]
      if (file === undefined) return
      try {
        const claimed = await uploadMedia(file)
        if (claimed === null) return
        coverKey = claimed.key
        coverUrl = claimed.url
        renderCover()
        renderPreview()
      } catch (error) {
        msg(errorText(error))
      } finally {
        coverFile.value = ''
      }
    }

    /** 把一张已经认领的图插到光标处（拿不到选区就追加到末尾）。 */
    function insertImage(key: string, url: string): void {
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
      const file = bodyFile.files?.[0]
      if (file === undefined) return
      try {
        const claimed = await uploadMedia(file)
        if (claimed === null) return
        media = { ...media, [claimed.key]: claimed.url }
        insertImage(claimed.key, claimed.url)
      } catch (error) {
        msg(errorText(error))
      } finally {
        bodyFile.value = ''
      }
    }

    // ── 编辑器工具栏 ─────────────────────────────────────────────────────────

    function runEditorCommand(command: string): void {
      editor.focus()
      if (typeof document.execCommand !== 'function') return
      if (command === 'bold') {
        document.execCommand('bold', false)
      } else {
        // ⚠️ 正文 / 小标题走 `formatBlock`（后端把 `<div>` 当段落，写入的是 `p` / `h2` / `h3`）。
        document.execCommand('formatBlock', false, command)
      }
      schedulePreview()
    }

    // ── 编辑态进出 ───────────────────────────────────────────────────────────

    function updateCounters(): void {
      titleCount.textContent = `${titleInput.value.length}/${TITLE_MAX}`
      introCount.textContent = `${introInput.value.length}/${INTRO_MAX}`
    }

    function applyEdit(row: ArticleRow | null, detail: ArticleDetail | null): void {
      editingId = row?.id ?? null
      editingStatus = detail?.status ?? row?.status ?? 'draft'
      editingPublishedAt = detail?.published_at ?? row?.published_at ?? null
      media = detail?.media ?? {}
      coverKey = null
      coverUrl = detail?.cover_url ?? row?.cover_url ?? ''
      titleInput.value = detail?.title ?? row?.title ?? ''
      introInput.value = detail?.intro ?? row?.intro ?? ''
      editor.innerHTML =
        detail === null ? '' : hydrateArticleHtml(detail.content_html, detail.media)
      updateCounters()
      renderCover()
      listSection.style.display = 'none'
      editSection.style.display = ''
      renderPreview()
      titleInput.focus()
    }

    /**
     * 打开编辑态（`null` = 新建）。
     *
     * ⚠️ **列表行不带 `content_html` / `media`**（后端契约）⇒ 编辑已有文章必须先
     * `GET /api/business/articles/<id>/` 拿详情；直接拿列表行进编辑器会把它渲染成空白，
     * 而用户一保存就会**把正文覆盖成空**（本仓反复堵的那类静默数据损失）。
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
        msg(errorText(error))
      }
    }

    function exitEdit(): void {
      if (previewTimer !== null) {
        window.clearTimeout(previewTimer)
        previewTimer = null
      }
      editingId = null
      editingStatus = 'draft'
      editingPublishedAt = null
      media = {}
      coverKey = null
      coverUrl = ''
      titleInput.value = ''
      introInput.value = ''
      editor.innerHTML = ''
      updateCounters()
      renderCover()
      editSection.style.display = 'none'
      listSection.style.display = ''
      renderPreview()
    }

    /** 保存 / 发布共用的写入（校验标题 + 组载荷）。`null` = 被校验挡下。 */
    async function persist(status: ArticleStatus): Promise<ArticleRow | null> {
      const title = titleInput.value.trim()
      if (title === '') {
        msg(MESSAGES.noTitle)
        return null
      }
      const payload: ArticleWritePayload = {
        title,
        intro: introInput.value.trim(),
        content_html: cleanArticleHtml(editor.innerHTML),
        status,
      }
      // ⚠️ `cover_key` 是**三态**：没改过就**不带这个键**（PATCH 才会保住原封面），
      //    明确移除才带空串。
      if (coverKey !== null) payload.cover_key = coverKey
      return editingId === null
        ? await createArticle(payload)
        : await updateArticle(editingId, payload)
    }

    async function onSaveDraft(): Promise<void> {
      if (saving) return
      saving = true
      try {
        // 编辑一篇已发布的文章时，「保存」不该把它悄悄下架 ⇒ 保持 `published`。
        const status: ArticleStatus = editingStatus === 'published' ? 'published' : 'draft'
        const wasPublished = editingStatus === 'published'
        const saved = await persist(status)
        if (saved === null) return
        await Promise.all([loadList(), loadProfile()])
        exitEdit()
        // ⚠️ `exitEdit()` 会把 `editingStatus` 复位成 draft ⇒ 必须在**退出前**取快照，
        //    否则已发布文章保存后永远提示「已保存草稿」（2026-10-23 评审指出）。
        msg(wasPublished ? MESSAGES.savedPublished : MESSAGES.saved)
      } catch (error) {
        msg(errorText(error))
      } finally {
        saving = false
      }
    }

    async function onPublish(): Promise<void> {
      if (saving) return
      saving = true
      try {
        const status: ArticleStatus = editingStatus === 'published' ? 'published' : 'draft'
        const saved = await persist(status)
        if (saved === null) return
        // ⭐ 发布是**独立端点**（`published_at` 只写一次由服务端管），⛔ 不靠 PATCH 一个 status。
        await publishArticle(saved.id)
        await Promise.all([loadList(), loadProfile()])
        exitEdit()
        msg(MESSAGES.published)
      } catch (error) {
        msg(errorText(error))
      } finally {
        saving = false
      }
    }

    // ── 行操作 ───────────────────────────────────────────────────────────────

    async function onRowAction(action: string, id: number): Promise<void> {
      const row = articles.find((item) => item.id === id)
      if (row === undefined) return
      if (action === 'edit') {
        void openEditor(row)
        return
      }
      if (action === 'delete') {
        ;(layer.confirm as (text: string, options: unknown, ok: () => void) => void)(
          `确定删除「${escapeHtml(row.title)}」吗？删除后移动端就看不到了，且不能恢复。`,
          {},
          () => {
            void deleteArticle(id)
              .then(() => Promise.all([loadList(), loadProfile()]))
              .then(() => msg(MESSAGES.deleted))
              .catch((error: unknown) => msg(errorText(error)))
          },
        )
        return
      }
      try {
        if (action === 'publish') await publishArticle(id)
        else if (action === 'unpublish') await unpublishArticle(id)
        else return
        await Promise.all([loadList(), loadProfile()])
        msg(action === 'publish' ? MESSAGES.published : MESSAGES.unpublished)
      } catch (error) {
        msg(errorText(error))
      }
    }

    // ── 事件接线（节点自己的监听；节点随页面一起销毁，不往 document 上叠闭包）────────

    byId<HTMLButtonElement>(NEW_BUTTON_ID).addEventListener('click', () => {
      void openEditor(null)
    })
    byId<HTMLButtonElement>(BACK_BUTTON_ID).addEventListener('click', () => exitEdit())
    byId<HTMLButtonElement>(CANCEL_BUTTON_ID).addEventListener('click', () => exitEdit())
    byId<HTMLButtonElement>(SAVE_DRAFT_BUTTON_ID).addEventListener('click', () => {
      void onSaveDraft()
    })
    byId<HTMLButtonElement>(PUBLISH_BUTTON_ID).addEventListener('click', () => {
      void onPublish()
    })
    byId<HTMLButtonElement>(PROFILE_SAVE_ID).addEventListener('click', () => {
      void saveProfile()
    })

    titleInput.addEventListener('input', () => {
      updateCounters()
      schedulePreview()
    })
    introInput.addEventListener('input', () => updateCounters())
    editor.addEventListener('input', () => schedulePreview())

    // ⚠️ `mousedown` 阻止默认：点工具栏按钮不该把编辑器里的选区弄丢（丢了下一条命令就没处落）。
    toolbar.addEventListener('mousedown', (event) => event.preventDefault())
    toolbar.addEventListener('click', (event) => {
      const target = (event.target as HTMLElement | null)?.closest<HTMLElement>(
        `[${EDITOR_CMD_ATTR}]`,
      )
      const command = target?.getAttribute(EDITOR_CMD_ATTR)
      if (command === undefined || command === null || command === '') return
      if (command === CMD_IMAGE) {
        bodyFile.click()
        return
      }
      runEditorCommand(command)
    })

    byId<HTMLButtonElement>(COVER_PICK_ID).addEventListener('click', () => coverFile.click())
    coverFile.addEventListener('change', () => {
      void onCoverFile()
    })
    bodyFile.addEventListener('change', () => {
      void onBodyFile()
    })
    coverRemove.addEventListener('click', () => {
      coverKey = ''
      coverUrl = ''
      renderCover()
      renderPreview()
    })

    root.querySelectorAll<HTMLElement>(`[${PREVIEW_VIEW_ATTR}]`).forEach((button) => {
      button.addEventListener('click', () => {
        const view = button.getAttribute(PREVIEW_VIEW_ATTR)
        if (view !== 'detail' && view !== 'channel') return
        previewView = view
        renderPreview()
      })
    })

    listBody.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement | null)?.closest<HTMLElement>(
        `[${ARTICLE_ACTION_ATTR}]`,
      )
      if (button === null || button === undefined) return
      const action = button.getAttribute(ARTICLE_ACTION_ATTR) ?? ''
      const id = Number(button.getAttribute(ARTICLE_ID_ATTR) ?? '')
      if (!Number.isInteger(id) || id <= 0) return
      void onRowAction(action, id)
    })

    listState.addEventListener('click', (event) => {
      const retry = (event.target as HTMLElement | null)?.closest<HTMLElement>(
        `[${LIST_RETRY_ATTR}]`,
      )
      if (retry === null || retry === undefined) return
      void Promise.all([loadList(), loadProfile()])
    })

    // ── 首次取数 ─────────────────────────────────────────────────────────────

    renderProfile()
    renderPreview()
    await Promise.all([loadList(), loadProfile()])

    return () => {
      if (previewTimer !== null) {
        window.clearTimeout(previewTimer)
        previewTimer = null
      }
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
