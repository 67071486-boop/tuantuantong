/**
 * 浏览器**直传**存储（图片存储线的「不经过我们服务器」那一跳）。
 *
 * 四处**同形**的必要性（照本线当初在 `pages/user-myinfo.ts` 里那份 `buildUploadInit` 的实测结论，
 * 那条线已过评审；本文件是它的共享化，⛔ 逻辑一字未改）：
 *
 *  - 同源（本机 `local` 后端 ⇒ `upload_url` 指向我们自己的 blob 端点）**必须带**
 *    `X-CSRFToken`：DRF 的 Session 认证对 PUT 也会跑 CSRF 校验；
 *  - 跨域（生产 COS 预签名）则 ⛔ **不能带**我们的头（`X-CSRFToken` 之类），否则会被 COS
 *    判成签名不符（签名只覆盖被签的那几个参数）；例外是 `Cache-Control`：它**被签进**
 *    预签名（业务方 2026-11-09，落桶即带缓存头），两条分支都必须按 `UPLOAD_CACHE_CONTROL` 原样携带；
 *  - `credentials` 同理：同源要 `include`（Session cookie），跨域要 `omit`；
 *  - `Content-Type` 取文件自己的类型，缺省 `application/octet-stream`。
 *
 * ⚠️ 为什么独立成文件而不是把这份逻辑留在 `pages/user-myinfo.ts` 里：跨页面模块互相 import 会把
 * 整页（连同它的 `replica` 证据件依赖）拖进来，且页面模块之间没有既定的依赖方向。
 * ✅ **已收口（2026-09-25 整体评审 M-1 订正）**：`pages/user-myinfo.ts` 已改走本模块
 * （`import { putFileToStorage } from '@/layui-island/directUpload'`），**全仓只此一份**
 * ——上面那段「四处同形」就是唯一出处，将来要改判据**只改这里**。
 */
import { readCsrfCookie } from '@/api/csrf-cookie'
import { CSRF_HEADER_NAME } from '@/api/http'

/**
 * 直传必须携带的缓存头（与后端 `apps/core/storage.py` 的 `MEDIA_CACHE_CONTROL` **逐字节一致**）：
 * 它被签进 COS 预签名，请求带的值不同 COS 就拒（403）。一致性由本目录 spec 的跨栈结构网钉住。
 * 本机 blob 端点不读它 ⇒ 同源带上无害；⚠️ 生产桶 CORS 的 AllowedHeaders 须含 `Cache-Control`。
 */
export const UPLOAD_CACHE_CONTROL = 'public, max-age=86400'

/** 这一跳的 `RequestInit`（同源 / 跨域两种形态，判据见文件头）。 */
export function buildUploadInit(file: File, uploadUrl: string): RequestInit {
  const headers: Record<string, string> = {
    'Content-Type': file.type || 'application/octet-stream',
    'Cache-Control': UPLOAD_CACHE_CONTROL,
  }
  const sameOrigin = uploadUrl.startsWith('/')
    || (typeof window !== 'undefined' && uploadUrl.startsWith(window.location.origin))
  if (sameOrigin) {
    const token = readCsrfCookie()
    if (token) headers[CSRF_HEADER_NAME] = token
  }
  return { method: 'PUT', body: file, headers, credentials: sameOrigin ? 'include' : 'omit' }
}

/**
 * 本机 blob 路由回 **HTTP 413** 时的固定文案。
 *
 * ⚠️ **为什么这里允许「前端自造」一句**（2026-09-25 评审 Minor-1 修复）：本机 blob 路由回的是
 * **裸 413、没有 body**（`apps/core/media_blob_views.py::MediaBlobView.put` 现在有**三**个 413
 * 分支：`declared <= 0`（声明值缺失 / 空串 / `0` ⇒ 体被读成空）、`declared > limit`（超限）、
 * 以及读体后 `len(body) > limit` 的兜底；三个都是 `HttpResponse(status=413)`）⇒ 服务端**没有**
 * `msg` 可原样显示；而把状态码当文案（`图片上传失败（HTTP 413）`）既看不懂、也违背本仓
 * 「失败文案不自造」的口径。这里给一句固定中文，是本仓**已知的、收口在这一处**的例外。
 *
 * ⚠️ **413 现在同时覆盖两种情形**（2026-09-25 复评 Minor-3）：一是**超过大小限制**，
 * 二是**声明值为空 / 0**（用户在浏览器里选了个 **0 字节文件**，或分块上传没有声明值）
 * ⇒ 文案必须两种都说得通，⛔ 不能只说「超过大小限制」（对 0 字节文件是说假话）。
 *
 * ⚠️ **不写具体数字**：这条路由**同时服务两种上限**——`_max_bytes_for_key` 按 key 种类取
 * 头像 / 收款码 **10 MB**、订单截图 **18 MiB**（`apps/core/storage.py` 的两个常量），
 * 而这一层（`putFileToStorage`）拿不到「是哪一种」以外的一致口径 ⇒ 写死任一个数字都会在
 * 另一半上说假话。真正的边界仍是后端（认领端点按各自上限重判）。
 *
 * ⚠️ **`413` 之外的 4xx/5xx 仍照旧带状态码**：那条分支的响应**可能**有服务端/第三方的 body
 * （如 COS 的错误 XML），现在没解析它，但文案里带上码至少便于排障。
 */
const OVERSIZE_TEXT = '图片无效或超过大小限制，请重新选择后重试'

/**
 * 把文件 PUT 到服务端签发的 `upload_url`。
 *
 * ⛔ **不判「响应码在不在」**：`fetch` 本身只在网络层失败时 reject，HTTP 4xx/5xx 是**正常返回**
 * ⇒ 必须自己看 `res.ok`（`AGENTS.md` §5-E「不拿解包结果判成败」的同族）。
 */
export async function putFileToStorage(uploadUrl: string, file: File, signal?: AbortSignal): Promise<void> {
  const init = buildUploadInit(file, uploadUrl)
  if (signal) init.signal = signal
  const res = await fetch(uploadUrl, init)
  if (res.status === 413) throw new Error(OVERSIZE_TEXT)
  if (!res.ok) throw new Error(`图片上传失败（HTTP ${res.status}）`)
}
