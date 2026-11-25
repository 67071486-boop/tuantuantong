// 交易平台自动回复规则（）的出口。
//
// 分层与 `api/platformIntegrations.ts` 同形：传输、认证与信封解包全在 `request()`
// （`./http.ts`）里，本文件不 import 任何 UI 库。
//
// ## 读模型里为什么没有 `image_key`
//
// 后端把 `image_key` 当**只写**字段（对象名是内部命名空间的一部分，前端拿它没用），
// 读侧只回两件：`has_image`（布尔）与 `image_url`（**现签**的短时效链接，
// 300 秒过期、⛔ 不缓存）。⛔ 不要为了"方便"把 `image_key` 加回 `PlatformAutoReplyRuleRow`
// ——接口根本不回，加上去只会得到一个恒 `undefined` 的字段，下一个读者会以为"后端回了但没渲染"。
//
// ## 图片上传（B1，）
//
// `uploadPlatformAutoReplyImage()` 发 **multipart**（字段名 `file`）到集合下的
// `upload-image/`：对象名由**服务端**生成（`<租户>/platform-auto-replies/<随机>.<ext>`），
// 客户端拿回的 `image_key` 随规则保存（`PATCH`/`POST` 的 `image_key`）——⛔ 客户端不能自己拼 key。
//
// ## 时机码值（⛔ 别按直觉改）
//
// `trigger` 是**证据件的原值**：`0` = 接单后 / `10` = 申请验收后
// （`public/replica/system-platformautoreply.html` 弹层 `name="replyOcc"` 的 `value`）。
// 本版只做这两个；`15 提交异常后` / `20 申请撤销后` 不在这条接口的值域里。
import { request } from './http'

/** 集合路径（后端 `apps/business/urls.py` 的 `platform-auto-replies` router 注册）。 */
export const PLATFORM_AUTO_REPLIES_URL = '/api/business/platform-auto-replies/'

/** 规则图片上传端点（router 的 `@action(detail=False, url_path="upload-image")`）。 */
export const PLATFORM_AUTO_REPLY_IMAGE_UPLOAD_URL = `${PLATFORM_AUTO_REPLIES_URL}upload-image/`

/** 上传成功的回执：`image_key` 要随规则保存；`url` 是**现签**的短时效读链接（缩略图用）。 */
export type PlatformAutoReplyImageUpload = {
  image_key: string
  url: string
  expires_in: number
}

/** 一行规则（字段与 `PlatformAutoReplyRuleSerializer` 逐字对应）。 */
export type PlatformAutoReplyRuleRow = {
  id: number
  /** 平台 id（本版页面只落 `10` 代练通）。 */
  platform: number
  /** 平台中文名（后端 `get_platform_display()`，页面「启用平台」列直接显示它）。 */
  platform_label: string
  /** 限定游戏主键；`null` = **全部游戏**。 */
  game_id: number | null
  /** FK 主键（写回时用它；与 `game_id` 同值，保留是为了「读到的形状能直接回写」）。 */
  game: number | null
  /** 游戏名；`null`（全部游戏）时后端回**空串**（「全部游戏」那句是页面文案）。 */
  game_name: string
  /** 回复时机：`0` 接单后 / `10` 申请验收后（证据件原值）。 */
  trigger: number
  /** 时机中文（后端 `get_trigger_display()`）。 */
  trigger_name: string
  content: string
  /** 有没有图片（后端只回布尔，不回对象名）。 */
  has_image: boolean
  /** **现签**的短时效读链接；没有图片时是空串。⛔ 不落库、不缓存。 */
  image_url: string
  is_enabled: boolean
  created_at: string
  updated_at: string
}

/** 新增 / 编辑的载荷。- `game` **传 `null` = 全部游戏**；不传 = 不改（PATCH 语义）。- `image_key` 是只写字段：本版由 `uploadPlatformAutoReplyImage()` 产生，页面只在**本次上传了新图**时发它（没换图 = 不发，保持库里那张）。 */
export type PlatformAutoReplyRulePayload = {
  platform?: number
  game?: number | null
  trigger?: number
  content?: string
  /** 行内启用开关（页面那颗 `lay-skin="switch"` 发的是它）。 */
  is_enabled?: boolean
  image_key?: string
}

/** 列表：后端**直接回数组**（不是 `{items,total}`，见 `platform_auto_reply_views.py` 文件头）。 */
export async function fetchPlatformAutoReplyRules(): Promise<PlatformAutoReplyRuleRow[]> {
  return request<PlatformAutoReplyRuleRow[]>({ method: 'get', url: PLATFORM_AUTO_REPLIES_URL })
}

/** 新增一条（POST）。 */
export async function createPlatformAutoReplyRule(
  payload: PlatformAutoReplyRulePayload,
): Promise<PlatformAutoReplyRuleRow> {
  return request<PlatformAutoReplyRuleRow>({
    method: 'post',
    url: PLATFORM_AUTO_REPLIES_URL,
    data: payload,
  })
}

/** 编辑（页面「修改」/ 行内启用开关 / 批量改内容都用它；后端只开 PATCH）。 */
export async function updatePlatformAutoReplyRule(
  id: number,
  payload: PlatformAutoReplyRulePayload,
): Promise<PlatformAutoReplyRuleRow> {
  return request<PlatformAutoReplyRuleRow>({
    method: 'patch',
    url: `${PLATFORM_AUTO_REPLIES_URL}${id}/`,
    data: payload,
  })
}

/** 上传一张规则图片（multipart，字段名 `file`）。⚠️ 对象名由**服务端**生成并回给我们（⛔ 客户端不拼 key）；类型 / 大小（≤2 MiB）也由服务端判（按字节魔数，不认文件名）。失败抛 `HttpError` / `NetworkError`，`message` 是后端的中文原因（如「图片超过 2 MB，请压缩后再上传」）。⚠️ 这里手动构造 `FormData` 后交给 `request()`：axios 见到 FormData 会自己定`multipart/form-data; boundary=…`，⛔ 不要手写 `Content-Type`（写死 boundary 会让后端解不出）。 */
export async function uploadPlatformAutoReplyImage(
  file: File,
): Promise<PlatformAutoReplyImageUpload> {
  const form = new FormData()
  form.append('file', file)
  return request<PlatformAutoReplyImageUpload>({
    method: 'post',
    url: PLATFORM_AUTO_REPLY_IMAGE_UPLOAD_URL,
    data: form,
  })
}

/** 删除（硬删）。 */
export async function deletePlatformAutoReplyRule(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${PLATFORM_AUTO_REPLIES_URL}${id}/` })
}
