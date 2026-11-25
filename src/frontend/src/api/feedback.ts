/** 【问题反馈】页（`layui-island/pages/feedback.ts`）的端点封装。spec：`docs/specs/2026-10-08-feedback-design.md` §4（**API 契约**，逐字）。契约的另一半是**实现即契约**：`src/apps/business/api/feedback_views.py` + `apps/business/urls.py`。| 方法 | 路径 | 用途 ||---|---|---|| POST | `/api/business/feedback/media/ticket/` | 签发一张**一次性上传地址**（`{ext}` ⇒ `{key, upload_url, expires_in}`） || GET | `/api/business/feedback/` | 列表（**管理员看全部、非管理员只看自己的**——收口在服务端 queryset） || POST | `/api/business/feedback/` | 提交（`{content, image_keys}`，最多 3 张） |
 * | PATCH | `/api/business/feedback/<pk>/` | 管理员处置（`{status, admin_note}`） |## 三条不能写错的纪律1. ⛔ **本模块没有删除函数**（spec §10）：反馈是审计痕迹，误报由管理员标「已处理」收口——别为了「对称」补一个 `deleteFeedback`，那是**新的一次业务裁定**、不是补漏。2. ⛔ **不上传文件**：本模块只签发地址与落库，真正的字节 PUT 由 `@/layui-island/directUpload`的 `putFileToStorage` 做（spec §1.3 的图片存储线，全仓**只此一份**实现）。3. ⚠️ **非管理员看不到别人的行，是服务端 queryset 层收口**（spec §4.2 原话：「⛔ 不是前端过滤、⛔ 不是「先取全部再筛」」）⇒ 本模块**不做任何前端筛选**，拿到什么就返回什么。前端隐藏只是体验层。## ⚠️ 图片的读链接**不入库**（spec §3 / §4.2）
 * `images[].url` 是服务端**现签**的（`get_storage().presign_get(key).url`），库里只存对象名（`object_key`）——所以本模块的载荷里两个键都要声明：`key` 是持久事实、`url` 是会过期的东西。⛔ 别把 `url` 写回任何请求体。 */
import { request } from './http'

/** 反馈列表 / 写入端点的**基路径**（尾斜杠与 `apps/business/urls.py` 的字面路径一致）。 */
export const FEEDBACK_URL = '/api/business/feedback/'

/** 签发上传地址的端点（spec §4.1）。⚠️ 尾斜杠是契约的一部分。 */
export const FEEDBACK_TICKET_URL = '/api/business/feedback/media/ticket/'

/** 处置某一条（spec §4.4）。 */
export function feedbackDetailUrl(pk: number): string {
  return `${FEEDBACK_URL}${pk}/`
}

/** 反馈状态（spec §3 的 `FeedbackStatus`）：与后端 `TextChoices` 的**值**逐字一致。 */
export type FeedbackStatus = 'pending' | 'handled'

/** 一条反馈上的**一张截图**（spec §4.2 的 `images[]` 一行）。 */
export type FeedbackImage = {
  /** 对象名（持久事实；服务端拿它重签读链接）。 */
  key: string
  /** 服务端**现签**的读链接（会过期；⛔ 不落库、不回传）。 */
  url: string
}

/** 列表 / 写入回来的**一行反馈**（键名照 spec §4.2 的逐字契约）。 */
export type FeedbackRow = {
  id: number
  content: string
  status: FeedbackStatus
  /** 后端给的中文名（「未处理」/「已处理」）。⛔ 前端**不镜像**一份词表（E-0021）。 */
  status_label: string
  /** 管理员备注；空串 = 只标了状态没写备注（spec §3：允许空）。 */
  admin_note: string
  /** 提交人显示名。**非管理员看到的也照常返回**，只是页面上不给他看这一列。 */
  created_by_name: string
  created_at: string
  handled_by_name: string | null
  handled_at: string | null
  images: FeedbackImage[]
}

/** `GET /api/business/feedback/` 的 `data`（spec §4.2：`items` + `total`）。 */
export type FeedbackListPayload = {
  items: FeedbackRow[]
  total: number
}

/** 上传地址的签发结果（spec §4.1 的 `data`：`{key, upload_url, expires_in}`）。 */
export type FeedbackTicket = {
  key: string
  upload_url: string
  expires_in: number
}

/** 要一张一次性上传地址（spec §4.1）。⚠️ `ext` **去点、小写**由调用方保证（页面那边做），服务端再判一次白名单（`jpg` / `jpeg` / `png`）；这里**不替它猜**扩展名。⚠️ 这个端点**只签发、不写库**（spec §4.1 的 ⛔ 那一条）。 */
export function issueTicket(ext: string): Promise<FeedbackTicket> {
  return request<FeedbackTicket>({
    url: FEEDBACK_TICKET_URL,
    method: 'POST',
    data: { ext },
  })
}

/** 列表（spec §4.2）。
 * ⚠️ **管理员与非管理员打的是同一个端点**（`is_tenant_admin` 在服务端分流）——
 * 所以本函数**没有**「管理员用」的第二个版本：多一个函数就是多一处会漂移的判据。 */
export function listFeedback(): Promise<FeedbackListPayload> {
  return request<FeedbackListPayload>({ url: FEEDBACK_URL, method: 'GET' })
}

/** 提交一条反馈（spec §4.3）。⚠️ `image_keys` 可空（`[]` = 纯文字反馈，spec §4.3 原话「可空、最多 3 张」）——但这**不是**本模块替调用方做默认值：页面永远显式给（哪怕是空数组），免得「忘了传」与「刻意不传」在后端看起来一样。 */
export function createFeedback(payload: {
  content: string
  image_keys: string[]
}): Promise<{ id: number }> {
  return request<{ id: number }>({
    url: FEEDBACK_URL,
    method: 'POST',
    data: payload,
  })
}

/** 管理员处置某一条（spec §4.4）：`{status, admin_note}`，两者**都可选**、但服务端要求**至少给一个**（都给也行 → 一次改状态 + 备注）。⛔ 非管理员打这个端点回 **403**（spec §4.4：不是 404、不是静默成功）⇒页面把后端文案如实弹出来，⛔ 不在前端新造一套「我是不是管理员」的写入门。⚠️ 入参用**可选键**（`?:`）而不是 `| undefined` 的必填键：「没改备注」与「备注改成空串」在后端是两件事（前者不清空、后者清空），所以调用方必须能用**键在不在**表达意图（E-0021 同族：不看真值）。 */
export function handleFeedback(
  id: number,
  payload: { status?: FeedbackStatus; admin_note?: string },
): Promise<{ id: number; status: FeedbackStatus }> {
  return request<{ id: number; status: FeedbackStatus }>({
    url: feedbackDetailUrl(id),
    method: 'PATCH',
    data: payload,
  })
}
