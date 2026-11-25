/** 订单截图的四个端点（spec §7 接口表；后端 `apps/business/api/screenshot_views.py`）。| 用途 | 方法 + 路径 | 节点 ||---|---|---|| 要一张上传凭证（**先落一条 `pending` 行**） | `POST /api/business/orders/<单号>/screenshots/ticket` | `order.upload_screenshot` || 认领（把刚直传上去的对象落成 `ready`） | `POST /api/business/orders/<单号>/screenshots` | 同上 || 列表 + 短时效看图链接 | `GET /api/business/orders/<单号>/screenshots` | `order.view_screenshot` || 删除单张 | `DELETE /api/business/orders/screenshots/<id>` | `order.screenshot_manage` |
 * | 打包下载 | `GET /api/business/orders/<单号>/screenshots/zip` | `order.download_screenshots_zip` |⚠️ **认领的键是 `screenshot_id`（行主键），不是 `key`**——这与同线的「我的资料」（`api/myProfile.ts::claimMedia(kind, key)`）**不同形**：那边的两列直接存在档案上、按 key 覆盖，这边是一行一张图、必须认领**签发时那条 `pending` 行**（后端 `_pending_row_or_error` 同时锁「本单 + 本租户 + 本签发人 + 仍 pending」）。⛔ 不要照抄 `myProfile.ts` 的形状改成本文件按 `key` 认领。 */
import { http, request } from './http'
import { NetworkError } from './errors'
import { exportFilename } from './performances'
import { isEnvelope, unwrap } from './unwrap'

/** `GET` 列表里的一项（字段与 `screenshot_views.list_items` 逐字对应）。 */
export type ScreenshotItem = {
  id: number
  /** `0` = 订单截图 / `1` = 群聊二维码（`OrderScreenshot.IMG_*`）。 */
  img_type: number
  /** **服务端签发的**短时效读链接（⛔ 前端不自己拼）。 */
  url: string
  thumbnail_url?: string
  original_name: string
  /** 后端 `HEAD` 实测的字节数（⛔ 不是前端上报的）。 */
  size_bytes: number
  remark: string
  /** 上传人的用户 id（载荷里**没有**显示名，见管理弹层的「已知差异」）。 */
  uploaded_by: number
  created_at: string
}

/** 凭证（`ScreenshotTicketView.issue_ticket` 的返回）。 */
export type ScreenshotTicket = {
  /** 签发时落下的那条 `pending` 行主键——认领时用它。 */
  screenshot_id: number
  /** 后端生成的对象名（前缀已焊死本租户）。 */
  key: string
  /** 浏览器直传目标：本机形态是我们自己的 blob 端点，生产是 COS 预签名 URL。 */
  upload_url: string
  expires_in: number
}

/** 要一张凭证。`imgType` 在**签发时**就要给（后端按它决定对象名前缀 `qr.` 与行的 `img_type`）。
 * ⚠️ 对标是把 `imgType` 放到「保存」那一步才发的（`uploadpicnew.js:455`）——我们的后端形态
 * 决定了它必须更早定下来，逐条登记在页面模块的「与对标不同」表里。 */
export function requestScreenshotTicket(
  orderNo: string, ext: string, imgType: number,
): Promise<ScreenshotTicket> {
  return request<ScreenshotTicket>({
    url: `/api/business/orders/${encodeURIComponent(orderNo)}/screenshots/ticket`,
    method: 'POST',
    data: { ext, img_type: imgType },
  })
}

/** 认领刚直传上去的那一个对象（`{screenshot_id, original_name, remark}`）。 */
export function claimScreenshot(
  orderNo: string,
  payload: { screenshot_id: number; original_name: string; remark: string },
): Promise<{ id: number }> {
  return request<{ id: number }>({
    url: `/api/business/orders/${encodeURIComponent(orderNo)}/screenshots`,
    method: 'POST',
    data: payload,
  })
}

/** 该订单的图（后端**不分页**：单订单的图被上传上限约束住）。 */
export function fetchScreenshots(
  orderNo: string,
): Promise<{ items: ScreenshotItem[]; total: number }> {
  return request<{ items: ScreenshotItem[]; total: number }>({
    url: `/api/business/orders/${encodeURIComponent(orderNo)}/screenshots`,
    method: 'GET',
  })
}

/** 硬删一张（行 + 对象，后端同时记一条操作日志）。 */
export function deleteScreenshot(id: number): Promise<null> {
  return request<null>({ url: `/api/business/orders/screenshots/${id}`, method: 'DELETE' })
}

/** 打包下载的 URL（`window.open` 用 ⇒ **不走 `request()`**）。⚠️ **已知代价（如实登记）**：这条路径拿不到响应体 —— 若调用者没有`order.download_screenshots_zip`(132) 这个节点，后端回的是统一信封（JSON），而它会被浏览器**摊在新标签页里**（不是一句可读的提示）。任务书点名要 `window.open`到 zip 端点（对标 `picmange.js:61` 也是 `window.open`），故照做；同仓 `emp-emplist`/`emp-billsummary` 的导出因为**同一个 URL 有两种形状**而改走「先请求再落 Blob」（见 `api/performances.ts:492` 的注释）——本端点成功时形状唯一，差别只在失败时。 */
export function screenshotZipUrl(orderNo: string): string {
  const base = import.meta.env.VITE_API_BASE ?? ''
  return `${base}/api/business/orders/${encodeURIComponent(orderNo)}/screenshots/zip`
}

export async function downloadScreenshotZip(orderNo: string): Promise<{ blob: Blob; filename: string }> {
  const response = await http.get<Blob>(screenshotZipUrl(orderNo), { responseType: 'blob', baseURL: '', timeout: 120_000 })
  const blob = response.data
  if (blob.type.includes('json')) {
    const body: unknown = JSON.parse(await blob.text())
    if (isEnvelope(body)) unwrap(body)
    throw new NetworkError('订单截图打包响应异常')
  }
  return { blob, filename: exportFilename(response.headers['content-disposition'], 'screenshots.zip') }
}
