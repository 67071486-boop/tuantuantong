/** 【第三方平台 → 自动上传企微群码到订单截图】页（`pages/wecom-group-qr.ts`）的端点封装。spec：`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §7.3（**后端端点契约**，五行表）/ §8.3（本模块）/ D7（单例资源形状）/ D9（复用现成节点，⛔ 不新增 RBAC 节点）。契约的另一半是**实现即契约**：`src/apps/openapi/urls.py` + `api/wecom_qr_app_views.py`。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/open/wecom-qr-app/` | 读本租户**那一把**锁定应用；⛔ **不存在 ⇒ `data=null`**（不是 404） || POST | `/api/open/wecom-qr-app/` | 创建（已有一把 ⇒ 业务码 `1000`；并发 ⇒ 同上，⛔ 不 500） || PATCH | `/api/open/wecom-qr-app/` | 改 `state` / `callback_url` / `callback_state` / `ip_allowlist` || POST | `/api/open/wecom-qr-app/rotate-secret/` | 轮换密钥（旧密钥**当场失效**） || GET | `/api/open/wecom-qr-app/deliveries/` | 推送台账（`page` / `page_size` / `status`） || ⛔ | **没有 `DELETE`** | 沿用「只能停用不能删」（与 `/api/open/apps/` 同一条口径） |## ⛔ 本模块**不写第二份字段表**（E-0021 同族）行载荷与台账行载荷在后端都是**逐字复用**既有那两个装配函数（`serializers.app_row_payload` /`delivery_row_payload`——`api/wecom_qr_app_views.py` 的模块 docstring 亲笔写明了这一点）
 * ⇒ 前端也**逐字复用** `api/openApps.ts` 那边已声明的类型（`ApiAppRow` / `ApiDeliveryRow` /`ApiDeliveryPage` / `DeliveryListParams`），本模块只**另起名字**。抄一份出来 = 两份字段表，后端加一个键时只有一处会漂，而漂的那一处**不会报错**。⚠️ 与 `openApps.ts` 的**两处有意不同**（都是本端点自己的契约，⛔ 不是漏写）：1. **没有 `name`**：`api/wecom_qr_app_views.py` 的模块 docstring 写明「`name` **不在本端点的可改字段里**（§7.3 的四项）」⇒ 本模块的 PATCH 载荷里**没有** `name` 这个键；创建时它也只是可选（缺席 ⇒ 后端给默认名「自动上传企微群码」）。2. **读是单例**：`getWecomQrApp()` 回 `ApiAppRow | null`（`null` = 这个租户还没建），⛔ 不是「一个列表」——「唯一、不能多个」这条约束写成 URL 形状（D7）。
 * ## ⚠️ PATCH 的判据是「键在不在」，⛔ 不是真值`ip_allowlist` 与 `callback_url` 都**允许空串**（空 = 不限制 / 没配回调），用真值判断会把「清空白名单 / 清掉回调地址」当成"没传"而**静默不生效**（`AGENTS.md` §5-E / E-0021）。本模块只透传，判据由**调用方**（页面）用显式字段对象保证。 */
import type {
  ApiAppCallbackState,
  ApiAppRow,
  ApiAppState,
  ApiDeliveryPage,
  ApiDeliveryRow,
  DeliveryListParams,
} from './openApps'
import { request } from './http'

/** 单例资源的**基路径**。尾斜杠与 `apps/openapi/urls.py` 的字面路径一致
 * （`path("wecom-qr-app/", …)` 挂在 `api/open/` 下）。
 * ⚠️ 它**不在** `v1/` 之下：这五条是**内部**端点（会话认证 + 节点门），第三方不调它们。 */
export const WECOM_QR_APP_URL = '/api/open/wecom-qr-app/'

/** 轮换密钥：新密钥写入、**旧密钥当场作废**（没有宽限期，与既有口径同）。 */
export function wecomQrAppRotateSecretUrl(): string {
  return `${WECOM_QR_APP_URL}rotate-secret/`
}

/** 推送台账（`GET`；`created_at` 倒序、页面端分页）。 */
export function wecomQrAppDeliveriesUrl(): string {
  return `${WECOM_QR_APP_URL}deliveries/`
}

/** 本端点自己的节点门（spec D9：**复用** `system.pinduoduo_settings`，⛔ 不新增 RBAC 节点）。用途只有一个：岛页被**深链**（`page.html?page=wecom-group-qr`）打开时**自己判一次**可见性——路由那两道门（`permissions` + `requiresAccountAdmin`）在 iframe 里不生效（先例与理由：`pages/open-apps.ts` 的同名自检，spec D10 直接指定照它做）。⚠️ 与 `config/routePermissions.ts` 里 `wecom-group-qr` 那条**必须同值**（两份独立声明是**有意**的，由 `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对纠错）。 */
export const WECOM_QR_APP_NODE = 'system.pinduoduo_settings'

/** 状态机取值：逐字来自后端 `ApiAppState`（⛔ 前端不镜像一份词表，中文名用 `state_text`）。 */
export type WecomQrAppState = ApiAppState

/** 回调开关：逐字来自后端 `CallbackState`（`enabled` / `disabled`，⛔ **不是布尔**）。 */
export type WecomQrAppCallbackState = ApiAppCallbackState

/** 本租户那一把锁定应用的一行（键集与 `app_row_payload` 逐字相同，含明文 `app_secret`）。 */
export type WecomQrAppRow = ApiAppRow

/** 台账里的一行（键集与 `delivery_row_payload` 逐字相同）。 */
export type WecomQrDeliveryRow = ApiDeliveryRow

/** 台账列表的载荷形状（`Page<T>`：`items` / `total` / `page` / `page_size`）。 */
export type WecomQrDeliveryPage = ApiDeliveryPage

/** 台账列表的查询参数（`status` 筛选可选；`created_at` 倒序由后端定死）。 */
export type WecomQrDeliveryListParams = DeliveryListParams

/** PATCH 的字段（**全部可选**；缺席 = 保持库里的值）。⚠️ 恰好这**四项**（§7.3 逐字）：⛔ 没有 `name`（见文件头）。⚠️ `callback_url` / `ip_allowlist` 的**空串是合法值** ⇒ 调用方判「要不要发这个键」时一律用`'callback_url' in payload` / `!== undefined`，⛔ 不用真值（E-0021）。 */
export type WecomQrAppPatchPayload = {
  state?: WecomQrAppState
  ip_allowlist?: string
  callback_url?: string
  callback_state?: WecomQrAppCallbackState
}

/** 读本租户那把应用（⛔ **不存在时 `data` 是 `null`**，不是 404、也不是空行）。
 * ⚠️ 调用方判「建了没有」必须用 `=== null`（⛔ 不用真值：业务值判断不能拿空对象兜）。 */
export function getWecomQrApp(): Promise<WecomQrAppRow | null> {
  return request<WecomQrAppRow | null>({ method: 'get', url: WECOM_QR_APP_URL })
}

/** 创建那把应用（`name` **可选** ⇒ 省略时后端给默认名「自动上传企微群码」）。⚠️ **每租户只有一把**（spec A6 业务方原话「该应用每个租户唯一 **不能设置多个**」）：已经有一把时后端回业务码 `1000`「本租户已创建过该应用，不能重复创建」——页面必须把 `e.message` **如实显示**出来，⛔ 不许把它美化掉 / 静默吞掉。⚠️ `app_key` / `app_secret` **不是入参**（服务端生成）⇒ 这个签名里就没有放它们的位置。 */
export function createWecomQrApp(name?: string): Promise<WecomQrAppRow> {
  // ⚠️ 缺名的请求体形状是 **`{}`**（⛔ 不是 `{name: undefined}`：JSON 里那会变成**没有这个键**，
  //    但显式写出来会让人以为"传了个空名"；后端把缺键与空串当**两件事**，后者回 `1000`）。
  return request<WecomQrAppRow>({
    method: 'post',
    url: WECOM_QR_APP_URL,
    data: name === undefined ? {} : { name },
  })
}

/** 改状态 / 回调配置 / IP 白名单（PATCH；**缺席的键保持库里的值**）。
 * ⚠️ 回调地址的**真判据在后端**（仅 https + SSRF 校验）⇒ 非法地址会拿到业务码 `1000` 与一句中文
 * ⇒ 页面必须把 `e.message` 显示出来（`request()` 已经把失败信封变成 `BizError`）。 */
export function updateWecomQrApp(payload: WecomQrAppPatchPayload): Promise<WecomQrAppRow> {
  return request<WecomQrAppRow>({ method: 'patch', url: WECOM_QR_APP_URL, data: payload })
}

/** 组装台账查询参数（**唯一的线上形状出口**）：**没给**的键不发（`page=0` 这类边界值要留下，E-0021）。 */
export function toWecomQrDeliveryParams(
  params: WecomQrDeliveryListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  if (params.status !== undefined) out.status = params.status
  return out
}

/** 推送台账（`GET .../deliveries/`；`created_at` 倒序，页面端分页）。 */
export function listWecomQrDeliveries(
  params: WecomQrDeliveryListParams = {},
): Promise<WecomQrDeliveryPage> {
  return request<WecomQrDeliveryPage>({
    method: 'get',
    url: wecomQrAppDeliveriesUrl(),
    params: toWecomQrDeliveryParams(params),
  })
}

/** 轮换密钥：响应含**新**密钥。⚠️ **单密钥、立即失效**（与 `docs/OPEN-API.md` §7.4 的既有口径一致）：旧密钥**当场作废**、**没有双密钥宽限期** ⇒ 正在跑的第三方集成会**当场失败**（可见、不静默，但要人去改配置）⇒ 页面在调它之前必须有二次确认、且确认文案里要写出这条后果。 */
export function rotateWecomQrAppSecret(): Promise<WecomQrAppRow> {
  return request<WecomQrAppRow>({ method: 'post', url: wecomQrAppRotateSecretUrl() })
}
