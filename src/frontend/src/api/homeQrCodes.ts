// 后台首页「二维码横幅」接口（自造功能，决策 D-0262）。契约来源（实现即契约）：
// `src/apps/business/api/qr_code_views.py` + `src/apps/business/urls.py` 的
// `router.register("home/qr-codes", …)`。
//
// | 方法 | 路径 | 门 |
// |---|---|---|
// | GET | `/api/business/home/qr-codes/` | **登录即可**；按「持不持 `home.qr_manage`」分档：持点含停用行、不持只含启用行（收口在服务端 queryset） |
// | POST | `/api/business/home/qr-codes/` | `home.qr_manage`（`{kind, game_id?, media_key, sort_order?}`） |
// | PATCH | `/api/business/home/qr-codes/<id>/` | `home.qr_manage`（换图 / 启停 / 排序 / 换游戏，部分更新） |
// | DELETE | `/api/business/home/qr-codes/<id>/` | `home.qr_manage`（**硬删**，照 SystemNotice 先例） |
// | POST | `/api/business/home/qr-codes/media/ticket/` | `home.qr_manage`（`{ext}` ⇒ `{key, upload_url, expires_in}`） |
//
// ⚠️ 字节直传那一跳不在这里：`putFileToStorage`（`@/layui-island/directUpload`）全仓只此一份。
// ⚠️ `media_url` 是服务端**现签**的短时效链接（⛔ 不落库、不回传）；`media_key` 才是持久事实。
import { request } from './http'

/** 与后端 `HomeQrCode.Kind` 的值逐字一致（`apps/business/models.py`）。 */
export type HomeQrKind = 'qq_bot' | 'qq_group' | 'wechat_mp'

/** 列表 / 写入回来的一行（键名照 `HomeQrCodeSerializer` 的逐字契约）。 */
export type HomeQrRow = {
  id: number
  kind: HomeQrKind
  game_id: number | null
  /** 服务端派生（前端不用再拿游戏字典来映射）。 */
  game_name: string | null
  media_key: string
  /** 服务端现签的读链接（会过期；⛔ 不落库、不回传）。 */
  media_url: string | null
  sort_order: number
  is_enabled: boolean
  created_by: string
  created_at: string
  updated_at: string
}

export const HOME_QR_CODES_URL = '/api/business/home/qr-codes/'

/** 签发上传地址的端点（照 `FEEDBACK_TICKET_URL` 的形状，尾斜杠是契约的一部分）。 */
export const HOME_QR_TICKET_URL = '/api/business/home/qr-codes/media/ticket/'

/** `GET` 的 `data`：列表不分页，`{items, total}`（同 feedback 列表）。 */
export type HomeQrListPayload = {
  items: HomeQrRow[]
  total: number
}

/** 上传地址的签发结果（照 `FeedbackTicket` 的形状）。 */
export type HomeQrTicket = {
  key: string
  upload_url: string
  expires_in: number
}

/** 列表（登录即可；持不持节点由服务端分流，前端不做任何筛选）。 */
export function fetchHomeQrCodes(): Promise<HomeQrListPayload> {
  return request<HomeQrListPayload>({ url: HOME_QR_CODES_URL, method: 'GET' })
}

/** 要一张一次性上传地址。
 * ⚠️ `ext` **去点、小写**由调用方保证（照 `issueTicket` 的口径），服务端再判一次白名单。 */
export function issueHomeQrTicket(ext: string): Promise<HomeQrTicket> {
  return request<HomeQrTicket>({ url: HOME_QR_TICKET_URL, method: 'POST', data: { ext } })
}

/** 新增一张码。`qq_group` 必须带 `game_id`（后端序列化器 + 库层 CheckConstraint 双闸）；
 * `qq_bot` / `wechat_mp` 已存在时后端回业务码 1001（`request()` 抛 `Error(msg)`）⇒
 * 页面把后端文案如实弹出来，⛔ 不在前端镜像一份「已存在」判据。 */
export function createHomeQrCode(payload: {
  kind: HomeQrKind
  game_id?: number | null
  media_key: string
  sort_order?: number
}): Promise<HomeQrRow> {
  return request<HomeQrRow>({ url: HOME_QR_CODES_URL, method: 'POST', data: payload })
}

/** 部分更新某一张码（换图 / 启停 / 排序 / 换游戏）。⚠️ **PATCH 而不是 PUT**：换图只带 `media_key`、启停只带 `is_enabled`——后端对 PUT 的 `media_key` 是必填（缺了回 1000），PATCH 才是「这一格不变」的语义。⚠️ 入参用**可选键**：「没改」与「改成空值」是两件事，键在不在表达意图（E-0021 同族）。 */
export function updateHomeQrCode(
  id: number,
  payload: {
    game_id?: number | null
    media_key?: string
    sort_order?: number
    is_enabled?: boolean
  },
): Promise<HomeQrRow> {
  return request<HomeQrRow>({ url: `${HOME_QR_CODES_URL}${id}/`, method: 'PATCH', data: payload })
}

/** 删除某一张码（**硬删**，照 SystemNotice 先例；存储里的对象不跟着删——孤儿对象，后端口径）。 */
export function deleteHomeQrCode(id: number): Promise<null> {
  return request<null>({ url: `${HOME_QR_CODES_URL}${id}/`, method: 'DELETE' })
}
