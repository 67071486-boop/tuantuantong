/** 【开放平台 → 应用管理】页（`pages/open-apps.ts`）的端点封装。spec：`docs/specs/2026-10-08-open-platform-v2-parity-design.md` §7.1（回调配置与开关）/§7.4（推送与重试）/ §8（前端一节）/ §13.18-3（台账读 / 重投端点的**冻结契约**）。契约的另一半是**实现即契约**：`src/apps/openapi/urls.py` + `api/app_views.py` + `serializers.py`。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/open/apps/` | 列表（⭐ **含 `app_secret` 明文**与回调配置） || POST | `/api/open/apps/` | 创建（入参**只要** `name`；可选顺带给回调地址 / 开关）——响应含明文 `app_secret` || PATCH | `/api/open/apps/<pk>/` | 改名 / **停用启用** / 改 IP 白名单 / ⭐ 改 `callback_url`·`callback_state` || POST | `/api/open/apps/<pk>/rotate-secret/` | 轮换密钥（响应含**新**密钥，旧密钥**立即失效**） || GET | `/api/open/apps/<pk>/deliveries/` | ⭐ 推送台账（内部管理端点；`created_at` 倒序、分页） || POST | `/api/open/apps/<pk>/deliveries/<delivery_pk>/retry/` | ⭐ 重投一条 **`failed`** 的推送 || ⛔ | **没有 `DELETE`** | 业务方 选定「**只能停用不能删**」 |## ⛔ 权限 / 游戏范围那两块**已删**（2026-10-08 业务方明确要求，原文）业务方原话：「不需要配置权限了 游戏范围也不需要」⇒ 本模块**同一次**删掉：`appPermissionsUrl` / `appGamesUrl` / `ApiAppPermissionsSummary` / `ApiAppGamesSummary` /`replaceAppPermissions` / `replaceAppGames` / `replaceAppAllGames` / `OPEN_ENDPOINT_NODE_CODES`，以及 `ApiAppRow` 上的 `permissions` / `games` / `all_games` / `permission_codes` / `game_ids`。依据 = 设计文档 §4-V2 的裁定：**一个应用凭据 = 本租户全部业务数据**（不再有"这个应用只能看这几张单"）⇒ 页面上那两块配置**没有对应的后端语义**了。⛔ 别把它们"顺手加回来"：后端那两条 `PUT .../permissions/`、`PUT .../games/` 由收口任务整块删除（§13.1），在这里留一个函数就是留一个**点了会 404/405 的入口**。## 三条不能写错的纪律
 * 1. ⛔ **本模块没有删除函数**：`DELETE` 是 405（后端刻意不用 router，spec §7.3 的末行）。别为了「对称」补一个 `deleteApiApp`——那是**新的一次业务裁定**，不是补漏。2. ⭐ **密钥是明文、随时可取**（**2026-10-07 业务方改口径**，原句「只有两个出口」已作废）：列表 / PATCH / 创建 / 轮换回来的行载荷里**都**有 `app_secret`⇒ 页面**直接显示**它并给一个复制动作，⛔ 不再有"只显示这一次"那套提示。⛔ **别把它顺手塞进日志 / 埋点 / URL query**：后端那侧有网钉着"密钥不进留痕"，前端这侧靠这条注释与代码评审（页面只把它放进列表单元格的文本与剪贴板）。⚠️ 代价：`app_secret` 明文与「应用 = 全租户」**互相放大**（设计文档 §14-11），页面上必须说破（见岛页的 `FULL_TENANT_NOTICE`）。3. ⚠️ **PATCH 的判据是「键在不在」，不是真值**：`ip_allowlist` 与 `callback_url` 都允许传空串
 * （空 = 不限制 / 没配回调），用真值判断会把「清空白名单 / 清掉回调地址」当成"没传"而**静默不生效**（E-0021 同族）。这里由**调用方**（页面）用 `??` 与显式字段对象保证，本模块只透传。 */
import type { Page } from './unwrap'
import { request } from './http'

/** 应用管理端点的**基路径**。尾斜杠与 `apps/openapi/urls.py` 的字面路径一致
 * （`path("apps/", …)` 挂在 `api/open/` 下）。 */
export const OPEN_APPS_URL = '/api/open/apps/'

/** `POST .../rotate-secret/`（spec §7.3）：新密钥写入、**旧密钥当场作废**（没有宽限期）。 */
export function appRotateSecretUrl(pk: number): string {
  return `${OPEN_APPS_URL}${pk}/rotate-secret/`
}

/** `PATCH /api/open/apps/<pk>/`（改名 / 停用启用 / IP 白名单 / 回调配置）。 */
export function appDetailUrl(pk: number): string {
  return `${OPEN_APPS_URL}${pk}/`
}

/** `GET /api/open/apps/<pk>/deliveries/`（推送台账；§13.18-3）。 */
export function appDeliveriesUrl(pk: number): string {
  return `${OPEN_APPS_URL}${pk}/deliveries/`
}

/** `POST .../deliveries/<delivery_pk>/retry/`（只对 `failed` 的行有意义；§13.18-3）。 */
export function appDeliveryRetryUrl(pk: number, deliveryPk: number): string {
  return `${appDeliveriesUrl(pk)}${deliveryPk}/retry/`
}

/** 应用状态（spec §5.1 的 `state` 列）。⚠️ 取值逐字来自后端 `ApiAppState`（`apps/openapi/models.py`）；中文名**不由前端映射**——载荷里已经有 `state_text`（后端 `get_state_display()`），前端再抄一份词表就是第二处出处（E-0021 / spec §7.1 对 `status_text` 的同一条口径）。 */
export type ApiAppState = 'enabled' | 'disabled'

/** 回调开关（设计文档 §7.1 的 `callback_state` 列）。⚠️ 取值逐字来自后端 `CallbackState`（`apps/openapi/models.py:25-33`）：`enabled` / `disabled`。⛔ 它**不是布尔**（列是 `CharField` + `choices`）⇒ 页面发 PATCH 时给的是这两个字符串，别写成 `true` / `false`（后端会对非法取值回 `1000`）。⚠️ 与 `ApiAppState` 是**两个独立开关**：停用应用 ⇒ 整条对外通道都不通；停用回调 ⇒ 只停推送。⚠️ 页面**没有**词表映射：这一列在页面上就是那个开关本身（不需要中文名）。 */
export type ApiAppCallbackState = 'enabled' | 'disabled'

/** 一行应用（`app_row_payload`）。⭐ **含 `app_secret` 明文**（2026-10-07 业务方改口径）。⚠️ `callback_url` / `callback_state` 是**必填键**（⛔ 不是可选）：T8 已在`serializers.py::app_row_payload` 里回它们（与 `state` / `state_text` 同层），而"这个键不在"与"地址是空的"是**两件事**——把缺键读成空串，页面上就会把「后端没回」显示成「没配回调」，用户一保存就把真实配置覆盖掉（T7 在`permission_codes` 上踩过同形的坑）。 */
export type ApiAppRow = {
  id: number
  name: string
  /** **非机密**（spec §5.3）：明文入库 + 唯一索引，会出现在日志与排障会话里。 */
  app_key: string
  /** ⭐ **明文密钥**（2026-10-07 业务方改口径：可以随时获取，⛔ 没有"只显示一次"的限制）。⚠️ 它**会**出现在响应体里（业务方要的）⇒ 按页面的用法，它能落到**浏览器历史 / 截图 /反代访问日志 / 前端调试面板**这些地方。⛔ 但仍然**不许**写进任何**留痕 / 操作日志 /错误消息 / 埋点 / URL query**（后端那侧由 `test_openapi_app_api.py` 的`AppSecretNeverLoggedTests` 钉住）。 */
  app_secret: string
  state: ApiAppState
  /** 中文状态名（后端 `get_state_display()`）。 */
  state_text: string
  ip_allowlist: string
  /** ⭐ **主动回调的接收地址**（设计文档 §7.1；由租户自己填）。⚠️ **仅 https**，且后端在**保存时**与**每次投递前**各校验一次（DNS 可变，§4-V6）：非 https / 私有网段一律回业务码 `1000`。⛔ 页面**不替后端做判据**：界面上给即时提示（`pages/open-apps.ts::callbackUrlHint`），**真判据以后端为准**，后端拒绝的文案必须**如实显示**（⛔ 不许在页面上把它美化掉 / 静默丢掉）。⚠️ **空串是合法值**（= 没配回调 ⇒ 不产生投递）。 */
  callback_url: string
  /** 回调开关（见 `ApiAppCallbackState`）。地址为空或它停用 ⇒ **不推送**（不排队、不重试、不报错）。 */
  callback_state: ApiAppCallbackState
  last_used_at: string | null
  secret_rotated_at: string | null
  created_at: string
}

/** PATCH 的字段（**全部可选**；缺席 = 保持库里的值）。 */
export type ApiAppPatchPayload = {
  name?: string
  state?: ApiAppState
  ip_allowlist?: string
  /** ⚠️ **空串是合法值**（= 清掉回调地址，§7.1）⇒ 调用方判「要不要发这个键」时一律用
   * `'callback_url' in payload` / `!== undefined`，⛔ 不用真值（E-0021）。 */
  callback_url?: string
  callback_state?: ApiAppCallbackState
}

/** 应用管理端点自己的节点门（spec D9：**复用** `system.pinduoduo_settings`，⛔ 不新增 RBAC 节点）。用途只有一个：岛页被**深链**（`page.html?page=open-apps`）打开时**自己判一次**可见性——路由那两道门（组级 `accountAdminOnly` + 路由级 `requiresAccountAdmin`）在 iframe 里不生效（先例与理由：`pages/system-roleauthority-edit.ts` 的同名自检）。⚠️ 与 `config/routePermissions.ts` 里 `open-apps` 那条**必须同值**（两份独立声明是**有意**的，由 `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对纠错）。 */
export const APP_MANAGE_NODE = 'system.pinduoduo_settings'

/** 列表（按 `id` 升序；后端不分页，spec §10 的「>50 个应用再加分页」是**将来**的事）。 */
export function listApiApps(): Promise<ApiAppRow[]> {
  return request<ApiAppRow[]>({ method: 'get', url: OPEN_APPS_URL })
}

/** 创建（入参**只要** `name`）。⚠️ `app_key` / `app_secret` **不是入参**：由服务端按 spec §5.3 生成（⇒ 这个签名里就**没有**放它们的位置，别为了"灵活"加两个可选字段）。⚠️ 回调配置也可以在创建时顺带给（后端 `AppCreateSerializer` 支持），但**页面不走这条路**：地址与开关是要对着"已建好的这个应用"改的配置，创建弹层里多两个字段只会让"建完再配"变成两种入口。 */
export function createApiApp(name: string): Promise<ApiAppRow> {
  return request<ApiAppRow>({ method: 'post', url: OPEN_APPS_URL, data: { name } })
}

/** 改名 / 停用启用 / 改 IP 白名单 / ⭐ 改回调配置（PATCH；缺席的键保持库里的值）。⚠️ 回调地址的**真判据在后端**（仅 https + SSRF，`serializers.validate_callback_url_value`→ `outbound.validate_callback_url`）⇒ 非法地址会拿到业务码 `1000` 与一句中文⇒ 页面必须把 `e.message` 显示出来（`request()` 已经把失败信封变成 `BizError`）。 */
export function updateApiApp(pk: number, payload: ApiAppPatchPayload): Promise<ApiAppRow> {
  return request<ApiAppRow>({ method: 'patch', url: appDetailUrl(pk), data: payload })
}

/** 推送台账的三个状态（设计文档 §7.4；取值逐字来自后端 `OpenApiDelivery.Status`）。⚠️ 它与 `status_text`（中文）**不是一回事**：判据一律用机器值（服务端可以改中文文案），中文只上屏。⛔ 「重投」按钮的判据就打在 `status === 'failed'` 上——后端对 `pending` / `done` 的回 `1000`（把 `done` 重投 = 对方收到两次推送）。 */
export type ApiDeliveryStatus = 'pending' | 'done' | 'failed'

/** 台账里的一行（⭐ **契约已冻结**，设计文档 §13.18-3；两侧都不许自行改形状）。字段逐个来自契约：`id` / `event` / `event_text` / `order_no` / `source_order_no` /`status` / `status_text` / `attempts` / `reason` / `response_status` /`next_attempt_at` / `created_at` / `processed_at`。⛔ 后端**不回** `payload`（报文原文含业务数据）、⛔ 不回任何凭据类字段⇒ 本类型也不声明它们（页面拿不到的东西不要写进契约）。 */
export type ApiDeliveryRow = {
  id: number
  /** 事件机器名（`order.status_changed` / `order.deleted` / `order.image_added`）。⚠️ 类型是 `string` 而**不是**联合：这是**后端定的**取值，前端收窄成三个字面量只会在后端加第四类事件时变成一次静默的类型谎言（页面的判据打在 `status` 上，不打在 `event` 上）。 */
  event: string
  /** 事件中文名（后端 `get_event_display()`）——⛔ 前端不镜像一份词表（E-0021）。 */
  event_text: string
  order_no: string
  /** 对端的来源单号；我方单据可能没有 ⇒ 后端允许空串。 */
  source_order_no: string
  status: ApiDeliveryStatus
  /** 中文状态名（后端 `get_status_display()`）。 */
  status_text: string
  /** 已投递次数（首发 1 次 = `1`；退避六档打完是第 7 次失败才转 `failed`）。 */
  attempts: number
  /** 最近一次失败原因（人看的诊断文本；后端约定上限 2000 字符）。成功行是空串。 */
  reason: string
  /** 对方回执的 HTTP 状态码（3xx / 5xx 都是失败，但**码要看得到**）。 */
  response_status: number | null
  /** 下一次可投递时刻；`null` = 立刻可投（含"已终态"的行）。 */
  next_attempt_at: string | null
  created_at: string
  /** 处理时刻；`null` = 还没投过。 */
  processed_at: string | null
}

/** 台账列表的载荷形状。⚠️ 契约（§13.18-3）说的是**内部页面端分页口径**（`items` + `total`），载体就是仓内共享的 `EnvelopePagination` ⇒ 直接复用 `Page<T>`（`api/unwrap.ts:8`，四个键`items`/`total`/`page`/`page_size` 一起回来）。页面只读 `items` / `total`。 */
export type ApiDeliveryPage = Page<ApiDeliveryRow>

/** 台账列表的查询参数（`status` 筛选可选；`created_at` 倒序由后端定死）。 */
export type DeliveryListParams = {
  page?: number
  page_size?: number
  status?: ApiDeliveryStatus
}

/** 组装查询参数（**唯一的线上形状出口**）：**没给**的键不发（`0` 这类边界值要留下，E-0021）。 */
export function toDeliveryParams(
  params: DeliveryListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  if (params.status !== undefined) out.status = params.status
  return out
}

/** 推送台账（`GET .../deliveries/`；`created_at` 倒序，页面端分页）。 */
export function listDeliveries(
  pk: number,
  params: DeliveryListParams = {},
): Promise<ApiDeliveryPage> {
  return request<ApiDeliveryPage>({
    method: 'get',
    url: appDeliveriesUrl(pk),
    params: toDeliveryParams(params),
  })
}

/** 重投一条推送（`POST .../deliveries/<deliveryPk>/retry/`）。⚠️ **只对 `status === 'failed'` 可点**：后端对 `pending` / `done` 回 `1000`（`done` 重投 = 对方收到两次推送）。页面因此把按钮**只在失败行上渲染**，⛔ 不是"画出来再等后端拒"。⚠️ **返回值形状不在本模块的契约里**（§13.18-3 只定了 URL 与方法，没定响应体裁）⇒ 这里老实声明 `unknown`：调用方**只拿它判成败**（能 resolve 就刷新列表），⛔ 别在这里写一个猜出来的形状——那会变成"看着有类型、实际是编的"。 */
export function retryDelivery(pk: number, deliveryPk: number): Promise<unknown> {
  return request<unknown>({ method: 'post', url: appDeliveryRetryUrl(pk, deliveryPk) })
}

/** 轮换密钥：响应含**新**密钥。⚠️ **单密钥、立即失效**（spec §5.3）：旧密钥**当场作废**、**没有双密钥宽限期**⇒ 正在跑的第三方集成会**当场失败**（可见、不静默，但要人去改配置）。页面在调它之前必须有二次确认（代价登记在 spec §9-12）。⚠️ **二次确认里要保留的那句**：旧密钥**立刻作废**——改口径（§34）**没有**动这一条；改掉的是"新密钥只显示一次"那句（它今天**假的**：列表里随时能看到）。 */
export function rotateApiAppSecret(pk: number): Promise<ApiAppRow> {
  return request<ApiAppRow>({ method: 'post', url: appRotateSecretUrl(pk) })
}
