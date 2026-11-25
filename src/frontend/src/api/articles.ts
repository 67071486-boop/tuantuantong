/** 【APP与小程序 →「发布文章作品」】页（`layui-island/pages/app-mp-articles.ts`）的端点封装。契约：`docs/plans/2026-10-23-channel-articles.md` §1「管理端 API」那一张表 +业务方 的口径（移动端只读、编辑 / 发布只在网页端）。契约的另一半是**实现即契约**：`src/apps/business/api/article_views.py` + `apps/business/urls.py`。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/articles/` | 列表（全部状态，带 `read_count`） || POST | `/api/business/articles/` | 新建（默认草稿） || GET/PATCH/DELETE | `/api/business/articles/<id>/` | 读 / 改 / 删 |
 * | POST | `/api/business/articles/<id>/publish/` | 发布 || POST | `/api/business/articles/<id>/unpublish/` | 下架回草稿 || POST | `/api/business/articles/media/ticket` | 签发直传凭证（`{ext}` ⇒ `{key,upload_url,expires_in}`） || POST | `/api/business/articles/media/claim` | 认领（`{key}` ⇒ `{key,url}`） || GET/PUT | `/api/business/channel-profile/` | 频道简介与文章数 |## 三条纪律1. ⛔ **图片的读链接不入库**：库里只存对象名（`content_html` 里是 `<img data-key="…">`），读链接是服务端现签的（`media` 那张 `{key: url}`）。⇒ `ArticleWritePayload` 里没有
 * 「每张图一个 url」的位置；`cover_key` 是持久事实、`cover_url` 是回读时的现签链接。2. ⛔ **本模块不上传字节**：真正的 `PUT` 由 `@/layui-island/directUpload` 的`putFileToStorage` 做（全仓只此一份实现，同源补 CSRF、跨源不带我们的头）。3. ⛔ **不做前端筛选**：列表端点回什么就返回什么（草稿 / 已发布都在里面），「频道列表只显示已发布」那一半在页面渲染时判（公开频道本来就只该看到已发布）。⚠️ **`media/ticket` 与 `media/claim` 的路径没有尾斜杠**（与 `channel-profile/` 不同）——这是本批后端契约的字面值（计划 §1 的表），⛔ 别按「Django 一般带尾斜杠」顺手补一个：补了会打到另一条路由（或 301），而 POST 被 301 之后请求体就丢了。 */
import { request } from './http'

/** 文章列表 / 写端的基路径（尾斜杠与后端的字面路径一致）。 */
export const ARTICLES_URL = '/api/business/articles/'
/** 签发文章图片直传地址（⚠️ **没有**尾斜杠，见文件头）。 */
export const ARTICLE_MEDIA_TICKET_URL = '/api/business/articles/media/ticket'
/** 认领一张文章图片（⚠️ **没有**尾斜杠，见文件头）。 */
export const ARTICLE_MEDIA_CLAIM_URL = '/api/business/articles/media/claim'
/** 频道简介（单行，读 / 写同一个端点）。 */
export const CHANNEL_PROFILE_URL = '/api/business/channel-profile/'

/** 本页的 RBAC 节点 code（**本批新增的自有节点**，`benchmark_id = None`）。取值必须与 `config/routePermissions.ts` 的 `ROUTE_NODE_CODES['app-mp-articles']` 一致；页面不需要自己再判（路由门已经判过），这里留着是为了「节点码只写一处」——⛔ 别在页面里再抄一个字符串。 */
export const ARTICLE_MANAGE_NODE = 'system.article_manage'

/** 文章状态（与后端 `TextChoices` 的**值**逐字一致）。 */
export type ArticleStatus = 'draft' | 'published'

/** 列表里的一行文章（键名照契约逐字）。⚠️ **列表行不带 `content_html` / `media`**（后端 现有契约：列表只回元信息，正文与图片链接要到详情端点才给）。这两个键声明成**可选**，是为了让「拿列表行直接渲染正文」在类型上就写不出来——页面编辑某篇时必须先 `getArticle(id)`，⛔ 不能拿列表行凑合。⚠️ `cover_url` 是**现签的读链接**（会过期），`cover_key` 只在写入时给。 */
export type ArticleRow = {
  id: number
  title: string
  intro: string
  status: ArticleStatus
  cover_url: string
  /** 只有详情响应才有（列表行没有这个键）。 */
  content_html?: string
  /** 对象名 → 读链接（服务端现签）；只有详情响应才有。 */
  media?: Record<string, string>
  read_count: number
  published_at: string | null
  created_at: string
  updated_at: string
  created_by: string
}

/** 详情响应（`GET /api/business/articles/<id>/`）：列表行那套元信息 + **必有**的正文与 media。
 * ⚠️ 这两个键在详情里**必须有**（哪怕是空串 / 空对象）：编辑器要靠它们回填。
 * 页面用这个类型把「必须取详情」这件事写进签名里。 */
export type ArticleDetail = ArticleRow & {
  content_html: string
  media: Record<string, string>
}

/** `GET /api/business/articles/` 的 `data`。 */
export type ArticleListPayload = {
  items: ArticleRow[]
  total: number
  page: number
  page_size: number
}

/** 频道简介（单行）。`article_count` 是**已发布**篇数。 */
export type ChannelProfile = {
  name: string
  intro: string
  article_count: number
}

/** 写入一篇文章的载荷。⚠️ `cover_key` **可选**：不传 = 不动现有封面（PATCH 语义），`''` = 明确移除。⇒ 页面用 `coverKey: string | null` 表达这三态（`null` = 没改过）。⚠️ `status` 可选：新建时页面显式给 `draft`（业务方要的是「保存草稿」这条链），编辑已发布文章时给 `published`（保存不该把它悄悄下架）。 */
export type ArticleWritePayload = {
  title: string
  intro: string
  cover_key?: string
  content_html: string
  status?: ArticleStatus
}

/** 上传地址的签发结果。 */
export type ArticleMediaTicket = {
  key: string
  upload_url: string
  expires_in: number
}

/** 认领结果：对象名 + 现签读链接。 */
export type ArticleMediaClaim = {
  key: string
  url: string
}

/** 某篇文章的路径（`…/articles/<id>/`）。 */
export function articleDetailUrl(id: number): string {
  return `${ARTICLES_URL}${id}/`
}

/** 发布 / 下架的路径。 */
export function articlePublishUrl(id: number): string {
  return `${articleDetailUrl(id)}publish/`
}

export function articleUnpublishUrl(id: number): string {
  return `${articleDetailUrl(id)}unpublish/`
}

/** 列表（全部状态，带阅读数）。
 * ⚠️ 页面一次要拿全量来画列表 + 频道预览 ⇒ 显式要 `page_size=200`（后端 `EnvelopePagination`
 * 的上限）；⛔ 不要依赖后端默认值（默认可能是 20，文章一多就会**静默少几篇**）。 */
export function listArticles(): Promise<ArticleListPayload> {
  return request<ArticleListPayload>({
    url: ARTICLES_URL,
    method: 'GET',
    params: { page: 1, page_size: 200 },
  })
}

/** 新建（默认草稿；页面显式传 `status: 'draft'`）。 */
export function createArticle(payload: ArticleWritePayload): Promise<ArticleRow> {
  return request<ArticleRow>({ url: ARTICLES_URL, method: 'POST', data: payload })
}

/** 读一篇（**编辑前必须调它**：列表行不带 `content_html` / `media`）。 */
export function getArticle(id: number): Promise<ArticleDetail> {
  return request<ArticleDetail>({ url: articleDetailUrl(id), method: 'GET' })
}

/** 改一篇（PATCH：缺席的键保持库里的值）。
 * ⚠️ 返回按**列表行**那套形状用（页面只取 `id` / `status`）；正文要不要以响应为准，
 * 页面不依赖——保存完会重新取列表。 */
export function updateArticle(id: number, payload: ArticleWritePayload): Promise<ArticleRow> {
  return request<ArticleRow>({ url: articleDetailUrl(id), method: 'PATCH', data: payload })
}

/** 删一篇（不可恢复；调用方必须先二次确认）。 */
export function deleteArticle(id: number): Promise<{ id: number }> {
  return request<{ id: number }>({ url: articleDetailUrl(id), method: 'DELETE' })
}

/** 发布（`published_at` 由服务端只写一次）。 */
export function publishArticle(id: number): Promise<ArticleRow> {
  return request<ArticleRow>({ url: articlePublishUrl(id), method: 'POST' })
}

/** 下架回草稿（`published_at` 保留历史值）。 */
export function unpublishArticle(id: number): Promise<ArticleRow> {
  return request<ArticleRow>({ url: articleUnpublishUrl(id), method: 'POST' })
}

/** 要一张文章图片的一次性上传地址。
 * ⚠️ `ext` 去点、小写由调用方保证（页面那边做），服务端再判一次白名单
 * （`jpg` / `jpeg` / `png`）；这里**不替它猜**扩展名。 */
export function issueArticleMediaTicket(ext: string): Promise<ArticleMediaTicket> {
  return request<ArticleMediaTicket>({
    url: ARTICLE_MEDIA_TICKET_URL,
    method: 'POST',
    data: { ext },
  })
}

/** 认领一张已经 PUT 上去的图片，拿回读链接。 */
export function claimArticleMedia(key: string): Promise<ArticleMediaClaim> {
  return request<ArticleMediaClaim>({
    url: ARTICLE_MEDIA_CLAIM_URL,
    method: 'POST',
    data: { key },
  })
}

/** 频道简介（读）。 */
export function getChannelProfile(): Promise<ChannelProfile> {
  return request<ChannelProfile>({ url: CHANNEL_PROFILE_URL, method: 'GET' })
}

/** 频道简介（写）。
 * ⚠️ 只发 `intro`：`name` / `article_count` 是服务端的事实（工作室名来自租户、篇数来自文章表），
 * 前端回传它们只会多一处「把服务端的值写回去」的漂移面。 */
export function updateChannelProfile(intro: string): Promise<ChannelProfile> {
  return request<ChannelProfile>({
    url: CHANNEL_PROFILE_URL,
    method: 'PUT',
    data: { intro },
  })
}
