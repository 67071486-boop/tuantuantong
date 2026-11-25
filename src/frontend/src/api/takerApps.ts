/** 【开放平台 → 打手对接】页（`pages/taker-apps.ts`）的端点封装。spec：`docs/specs/2026-10-08-taker-open-api-design.md` §3.1（`TakerApp` 的列）/ §6（身份限权）/ §7.3（菜单与页面）＋ 第二期（主动回调推送）`docs/specs/2026-10-09-taker-callback-design.md` §7。契约的另一半是**实现即契约**：`src/apps/takerapi/urls.py` + `api/app_views.py` + `serializers.py`（T3 / T5 已落地）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/taker/apps/` | 列表（⭐ **含 `app_secret` 明文**，与上家侧 `D-0201` 同口径） || POST | `/api/taker/apps/` | 创建（入参 `name` / 打手账号 / 可选 `ip_allowlist`）——响应含明文密钥 || PATCH | `/api/taker/apps/<pk>/` | 改名 / **停用启用** / 改 `ip_allowlist`（⛔ 改不了绑定的打手）<br>⭐ 第二期追加：改 `callback_url` / `callback_state` || POST | `/api/taker/apps/<pk>/rotate-secret/` | 轮换密钥（新密钥写回**同一列**，旧密钥**立即失效**） || GET | `/api/taker/apps/<pk>/deliveries/` | ⭐ 推送台账（第二期 §7 第二行；`created_at` 倒序、分页 20、可按 `status` 筛） || POST | `/api/taker/apps/<pk>/deliveries/<delivery_pk>/retry/` | ⭐ 手工重投一条 **`failed`** 的推送（第二期 §7 第三行） || ⛔ | **没有 `DELETE`** | 「只能停用不能删」（spec §3.1 / §6）⇒ 停用是唯一的关闭动作 |## 两条不能写错的纪律
 * 1. ⛔ **本模块没有删除函数**：后端连 `DELETE` 路由都没有（未定义方法在已存在的路径上回 405）。别为了"对称"补一个 `deleteTakerApp`——那是**新的一次业务裁定**，不是补漏。2. ⭐ **密钥是明文、随时可取**（与上家侧 `D-0201` 一致）：列表 / 创建 / `PATCH` / 轮换回来的行载荷里**都**有 `app_secret` ⇒ 页面直接显示并给复制动作。⛔ 但它**不许**进任何留痕 / 操作日志 / 错误消息 / 埋点 / URL query（后端那侧有网钉着，前端这侧靠代码评审）。## ⚠️ 两个节点 code **刻意住在本模块里**（不在岛页里）与上家侧 `api/openApps.ts::APP_MANAGE_NODE` **同一手法**、同一理由：- 岛页（`layui-island/pages/*.ts`）里出现的**带点**字符串字面量会被后端那张网（`src/tests/test_rbac_menu_alignment.py` 的源 3）当成**按钮级权限门**收走、并**冻结条数**
 * ⇒ 门常量放这里，那条冻结数就不必因为"多了一页"而变化（本线的选择，已在`test_rbac_menu_alignment.py` 那两条断言旁逐字登记）；- 它们的**取值**另有两条网钉住：`config/__tests__/routePermissions.spec.ts`（路由表 ↔ 目录，含"表里的每个 code 都真实存在于后端目录"）与 `router/__tests__/route-declarations.spec.ts`的 (c)（菜单声明 ↔ 路由 `meta.permissions` 逐条相等，两个来源都是手写的）。⛔ **别把这两个常量搬进岛页**：搬进去会让那张网的冻结数当场变（那是有意的绊线，但搬一次就要同一次改它，且没有额外收益）。 */
import type { AccountRow } from './accounts'
import { listAccounts } from './accounts'
import type { Page } from './unwrap'
import { request } from './http'

/** 打手对接端点的**基路径**。尾斜杠与 `apps/takerapi/urls.py` 的字面路径一致
 * （`path("apps/", …)` 挂在 `api/taker/` 下）。 */
export const TAKER_APPS_URL = '/api/taker/apps/'

/** `PATCH /api/taker/apps/<pk>/`（改名 / 停用启用 / IP 白名单）。 */
export function takerAppDetailUrl(pk: number): string {
  return `${TAKER_APPS_URL}${pk}/`
}

/** `POST /api/taker/apps/<pk>/rotate-secret/`：新密钥写入、**旧密钥当场作废**（没有宽限期）。 */
export function takerAppRotateSecretUrl(pk: number): string {
  return `${TAKER_APPS_URL}${pk}/rotate-secret/`
}

/** 本页自己的节点门（spec §7.1）——**本仓第一条自有节点**，与`config/routePermissions.ts` 的 `ROUTE_NODE_CODES['taker-apps']` **必须同值**。用途有两个：① 岛页被**深链**（`page.html?page=taker-apps`）打开时**自己判一次**可见性（那一刻路由那道门不生效——先例与理由见 `pages/system-roleauthority-edit.ts` 的同名自检）；② 与下面的 `SYSTEM_ADMIN_NODE` 一起决定"看到全部 + 必须选绑谁"还是"只看自己 + 锁死为自己"。 */
export const TAKER_APP_NODE = 'system.allow_taker_open_api'

/** 服务端"谁是系统管理员"那一条分支的判据（**与后端同值**）。后端是 `apps/takerapi/api/app_views.py::TakerAppAdminView._is_system_admin`= `has_node_now(user, SYSTEM_ADMIN_NODE)`，`SYSTEM_ADMIN_NODE = "system.pinduoduo_settings"`。⚠️ **必须与它同值、同语义**（前端这侧用 `canNode`，它的两条管理员短路与后端`has_node_now` 的 `is_tenant_admin` 短路同源）——两边分叉就会出现「**进得来、但被当成打手**」那种门与身份不一致的形状（判据：打手建应用时载荷里的 `taker` **不被采纳**）。⛔ 不要改用职位名 / `is_tenant_admin` 单判（那会把"被显式授了本节点的非管理员"错判成打手）。 */
export const SYSTEM_ADMIN_NODE = 'system.pinduoduo_settings'

/** 应用状态（spec §3.1 的 `state` 列）。⚠️ 取值逐字来自后端 `TakerApp.state`（`apps/takerapi/models.py` 的 `AppState`）；中文名**不由前端映射**——载荷里已经有 `state_text`（后端 `get_state_display()`），前端再抄一份词表就是第二处出处（E-0021）。 */
export type TakerAppState = 'enabled' | 'disabled'

/** 回调开关（第二期 spec §3.1 给 `TakerApp` 加的第二列）。⚠️ 取值逐字来自后端 `apps/takerapi/models.py::CallbackState`：`enabled` / `disabled`。⛔ 它**不是布尔**（列是 `CharField` + `choices`）⇒ 页面发 `PATCH` 时给的是这两个字符串，别写成 `true` / `false`（后端会对非法取值回 `1000`）。⚠️ 与 `TakerAppState` 是**两个独立开关**：停用应用 ⇒ 整条对外通道都不通；停用回调 ⇒ 只停推送。⚠️ 载荷里**没有** `callback_state_text`（第二期 spec §7 第四行明说「⛔ 不加」）⇒ 这一列在页面上就是那个开关本身，不需要中文名（⛔ 别在前端镜像一份词表，`E-0021`）。 */
export type TakerAppCallbackState = 'enabled' | 'disabled'

/** 一行打手侧应用（`serializers.app_row_payload` 的 **14** 个键，⛔ 不多不少）。⭐ 第二期追加的两键是 `callback_url` / `callback_state`（`app_row_payload` 的 docstring逐键登记）。它们与 `state` / `state_text` 同层，**都是必填键**（⛔ 不是可选）：「这个键不在」与「地址是空的」是**两件事**——把缺键读成空串，页面上就会把「后端没回」显示成「没配回调」，用户一保存就把真实配置覆盖掉（上家侧 `api/openApps.ts` 的 `ApiAppRow`为同一理由把这两键定成必填，打手侧**逐字同款**）。⭐ **含 `app_secret` 明文**（与上家侧 `D-0201` 同口径）。⚠️ `taker` / `taker_name` 两个都给：页面按**主键**决定行为、按**显示名**显示，⛔ 不许拿名字当判据（改个昵称就静默失效）。 */
export type TakerAppRow = {
  id: number
  name: string
  /** **非机密**（明文入库 + 唯一索引，会出现在排障面）。 */
  app_key: string
  /** ⭐ 明文密钥（可随时获取）。⛔ 仍不许进留痕 / 日志 / 错误消息 / URL query。 */
  app_secret: string
  /** 绑定的打手账号主键（**必填**：范围判据的唯一来源）。 */
  taker: number
  /** 那个账号的显示名（后端 `apps.accounts.display.user_display_name` 的唯一出处）。 */
  taker_name: string
  state: TakerAppState
  /** 中文状态名（后端 `get_state_display()`）。 */
  state_text: string
  ip_allowlist: string
  /** ⭐ **主动回调的接收地址**（第二期 §7 第一行；由打手自己填）。⚠️ **仅 https + 必须公网**，且后端在**保存时**与**每次投递前**各校验一次（DNS 可变，第二期 §8-4）：非 https / 私有网段 / 回环 / 链路本地 / URL 内带凭据一律回业务码 `1000`。⛔ 页面**不替后端做判据**：界面上给一句即时提示（`pages/taker-apps.ts::callbackUrlHint`），**真判据以后端为准**，后端拒绝的那句中文必须**如实显示**。⚠️ **空串是合法值**（= 没配回调 ⇒ 不产生投递）。 */
  callback_url: string
  /** 回调开关（见 `TakerAppCallbackState`）。地址为空或它停用 ⇒ **不推送**（不排队、不重试、不报错）。 */
  callback_state: TakerAppCallbackState
  /** 最近一次**验签成功**（`null` = 还没被调用过）。 */
  last_used_at: string | null
  secret_rotated_at: string | null
  created_at: string
}

/** `PATCH` 的字段（**全部可选**；缺席 = 保持库里的值）。⛔ `taker` 不在里面（后端会拒）。 */
export type TakerAppPatchPayload = {
  name?: string
  state?: TakerAppState
  /** ⚠️ **空串是合法值**（= 不限制来源 IP）⇒ 调用方判「要不要发这个键」时一律用
   * `!== undefined`，⛔ 不用真值（E-0021）。 */
  ip_allowlist?: string
  /** ⚠️ **空串是合法值**（= 清掉回调地址，第二期 §7 第一行）⇒ 判断「要不要发这个键」一律用
   * `!== undefined`，⛔ 不用真值（`E-0021`）。 */
  callback_url?: string
  /** 回调开关（`enabled` / `disabled`，⛔ 不是布尔）。 */
  callback_state?: TakerAppCallbackState
}

/** 创建端点的入参。⚠️ `taker` 是**可选**的：**打手**那一支发不发都一样（后端不看载荷、锁死为他自己），**管理员**那一支**必须**发（后端缺它回 `1000` + 点名，见 `TAKER_REQUIRED_MSG`）。⛔ `app_key` / `app_secret` **不是入参**：由服务端生成（签名里就没有放它们的位置）。 */
export type TakerAppCreatePayload = {
  name: string
  taker?: number
  ip_allowlist?: string
}

/** 列表（按 `id` 升序；后端不分页，与上家侧列表同形）。 */
export function listTakerApps(): Promise<TakerAppRow[]> {
  return request<TakerAppRow[]>({ method: 'get', url: TAKER_APPS_URL })
}

/** 创建应用。⚠️ **键在不在是判据，不是真值**：`ip_allowlist` 允许传空串（空 = 不限制）⇒ 这里用`!== undefined` 决定要不要带上那个键（`E-0021`）。`taker` 同理（`0` 不是合法账号，但"没传"与"传了非法值"必须由后端区分着答，前端不替它判）。 */
export function createTakerApp(payload: TakerAppCreatePayload): Promise<TakerAppRow> {
  const data: Record<string, unknown> = { name: payload.name }
  if (payload.taker !== undefined) data.taker = payload.taker
  if (payload.ip_allowlist !== undefined) data.ip_allowlist = payload.ip_allowlist
  return request<TakerAppRow>({ method: 'post', url: TAKER_APPS_URL, data })
}

/** 改名 / 停用启用 / 改 IP 白名单（PATCH；缺席的键保持库里的值）。 */
export function updateTakerApp(pk: number, payload: TakerAppPatchPayload): Promise<TakerAppRow> {
  return request<TakerAppRow>({ method: 'patch', url: takerAppDetailUrl(pk), data: payload })
}

/** 轮换密钥：响应含**新**密钥。⚠️ **单密钥、立即失效**：旧密钥**当场作废**、**没有双密钥宽限期**（spec §6）⇒ 正在跑的第三方集成会**当场失败**（可见、不静默，但要人去改配置）⇒ 页面在调它之前必须有二次确认，且确认里要写出"旧密钥立刻作废"。 */
export function rotateTakerAppSecret(pk: number): Promise<TakerAppRow> {
  return request<TakerAppRow>({ method: 'post', url: takerAppRotateSecretUrl(pk) })
}

/** 「绑定打手账号」选择器的**候选来源**：本租户的账号列表（既有内部端点`GET /api/accounts/`，走 `api/accounts.ts` 的 `listAccounts`）。## ⚠️ 为什么是它（而不是自造一条端点）- 后端 `AppCreateSerializer` 收的是一个**账号主键**，而本仓"账号列表"的唯一既有出处就是`/api/accounts/`（先例：指派弹层的候选名单 `selectors.assignee_options` 也直接调那条取数层）。⛔ 本线**不自造**第二条候选端点（那会多一份"谁算打手"的判据，spec §3.1 明确⛔ 不建第二套授权存储）。- 准入对得上：那条端点的最低门槛是 `IsAccountAdmin`，而后端"管理员分支"的判据`has_node_now(user, "system.pinduoduo_settings")` 在**租户平面上**与 `is_tenant_admin`同真同假（`apps/accounts/permissions.py::is_account_admin` 委托同一个谓词）
 * ⇒ 会走管理员分支的人，正是能列出账号的人。## ⚠️ 如实登记的一条边界（不是缺陷，但要知道）若某个**非租户管理员**的角色被显式授了 `system.pinduoduo_settings`（或本页那个节点），他会走管理员的**创建**分支（必须选一个打手），但 `/api/accounts/` 会对他回 403⇒ 页面的降级是**如实显示后端那句话**（`pickerHint` 里写明"候选列表取不到"），⛔ 不静默变成一个空下拉、也不在前端另造一条候选来源。⚠️ 分页取第一页（200 条；后端上限现为 300：`EnvelopePagination.max_page_size`）；`total` 大于拿到条数时页面**如实说**"只显示前 N 个"（⛔ 不假装这就是全部）。 */
export const TAKER_CANDIDATE_PAGE_SIZE = 200

/** 取候选账号（**第一页，上限 200**）。
 * ⚠️ 走的是 `api/accounts.ts` 的 `listAccounts`（**唯一**一处账号列表出口），
 * ⛔ 不在这里另拼一条 URL：那是「同一事实两套判据」，而漂移只表现为"某天候选列表悄悄空了"。 */
export function listTakerCandidates(): Promise<Page<AccountRow>> {
  return listAccounts({ page: 1, page_size: TAKER_CANDIDATE_PAGE_SIZE })
}

// ── 回调配置 / 推送台账 / 手工重投（第二期 spec §7；T5 已落地端点）──────────────

/** 回调地址的**长度上限**（与后端同值，⛔ 不许在前端另拍一个数）。⚠️ 唯一出处是 `apps/openapi/serializers.py::CALLBACK_URL_MAX_LENGTH = 200`——打手侧`apps/takerapi/serializers.py` 的两个序列化器字段**直接 import 那一个常量**（那个文件里逐字写着「`max_length` 与**模型列宽**（`TakerApp.callback_url` = 200）同源」）⇒ 前端这个数、后端那个数、以及**列宽**今天是同一个 200。⚠️ 它只用在标记的 `maxlength` 上（**输入框的软上限**）；真判据在后端序列化器（超长 ⇒ `1000` + 点名），⛔ 页面不拿它当校验门。 */
export const CALLBACK_URL_MAX_LENGTH = 200

/** `GET /api/taker/apps/<pk>/deliveries/`（推送台账，第二期 §7 第二行）。 */
export function takerAppDeliveriesUrl(pk: number): string {
  return `${takerAppDetailUrl(pk)}deliveries/`
}

/** `POST .../deliveries/<delivery_pk>/retry/`（只有 `failed` 的行能重投，第二期 §7 第三行）。 */
export function takerAppDeliveryRetryUrl(pk: number, deliveryPk: number): string {
  return `${takerAppDeliveriesUrl(pk)}${deliveryPk}/retry/`
}

/** 推送台账的三个状态（第二期 §7 第四行；取值逐字来自后端 `TakerApiDelivery.Status`）。⚠️ 它与 `status_text`（中文名）**不是一回事**：判据一律用机器值（服务端可以改中文文案），中文只上屏。⛔ 「重投」按钮的判据就打在 `status === 'failed'` 上——后端对 `pending` / `done` 回 `1000`（把 `done` 重投 = **对方会收到两次**同一个事件）。 */
export type TakerDeliveryStatus = 'pending' | 'done' | 'failed'

/** 台账里的一行（⭐ **契约已冻结**：第二期 §7 第四行「台账行 13 键**照上家侧那张**」）。字段逐个来自 `apps/takerapi/serializers.py::delivery_row_payload`（13 键，与上家侧`apps/openapi/serializers.py` 的同名函数**逐字同集**）：`id` / `event` / `event_text` /`order_no` / `source_order_no` / `status` / `status_text` / `attempts` / `reason` /`response_status` / `next_attempt_at` / `created_at` / `processed_at`。⛔ 后端**不回** `payload`（报文原文含订单数据）、⛔ 不回任何凭据类字段⇒ 本类型也不声明它们（页面拿不到的东西不要写进契约）。⚠️ **本类型与上家侧的 `ApiDeliveryRow`（`api/openApps.ts`）逐键同形**（那是同一条冻结契约），
 * 但**两族各声明一份**（与后端「两个 `delivery_row_payload` 各有一份」同一条形状纪律：两条通道的台账是两张表，合并会让"一族改了、另一族没改"只在其中一族的页面上看得见）。 */
export type TakerDeliveryRow = {
  id: number
  /** 事件机器名（`order.status_changed` / `order.deleted` / `order.image_added` / `order.reassigned`）。
   * ⚠️ 类型是 `string` 而**不是**联合：这是**后端定的**取值，前端收窄成四个字面量
   * 只会在后端加第五类事件时变成一次静默的类型谎言（页面的判据打在 `status` 上，不打在 `event` 上）。 */
  event: string
  /** 事件中文名（后端 `get_event_display()`）——⛔ 前端不镜像一份词表（`E-0021`）。 */
  event_text: string
  order_no: string
  /** 对端的来源单号；我方单据可能没有 ⇒ 后端允许空串。 */
  source_order_no: string
  status: TakerDeliveryStatus
  /** 中文状态名（后端 `get_status_display()`）。 */
  status_text: string
  /** 已投递次数（首发 1 次 = `1`；退避六档打完是第 7 次失败才转 `failed`）。 */
  attempts: number
  /** 最近一次失败原因（后端已按页面口径截断到 200 字符 + `…`）。成功行是空串。 */
  reason: string
  /** 对方回执的 HTTP 状态码（3xx / 5xx 都是失败，但**码要看得到**）。 */
  response_status: number | null
  /** 下一次可投递时刻；`null` = 立刻可投（含"已终态"的行）。 */
  next_attempt_at: string | null
  created_at: string
  /** 处理时刻；`null` = 还没投过。 */
  processed_at: string | null
}

/** 台账列表的载荷形状：仓内共享的 `EnvelopePagination`（`items` + `total` + `page` + `page_size`）。
 * ⚠️ 它是**内部页面端**的分页口径，⛔ 不是对外 `v1/…` 那套（`count` / `results`）——
 * 后者是对第三方的契约，两件事。 */
export type TakerDeliveryPage = Page<TakerDeliveryRow>

/** 台账列表的查询参数（`status` 筛选可选；`created_at` 倒序由后端定死）。 */
export type TakerDeliveryListParams = {
  page?: number
  page_size?: number
  status?: TakerDeliveryStatus
}

/** 组装查询参数（**唯一的线上形状出口**）：**没给**的键不发
 * （`0` 这类边界值要留下，⛔ 不用真值判断，`E-0021`）。 */
export function toTakerDeliveryParams(
  params: TakerDeliveryListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  if (params.status !== undefined) out.status = params.status
  return out
}

/** 台账第一页取多少条（= 后端 `EnvelopePagination.page_size` 的默认值，第二期 §7 第二行
 * 明说「分页 20」；页面只展示最近这些）。 */
export const TAKER_DELIVERIES_PAGE_SIZE = 20

/** 推送台账（`GET .../deliveries/`；`created_at` 倒序，页面端分页）。 */
export function listTakerDeliveries(
  pk: number,
  params: TakerDeliveryListParams = {},
): Promise<TakerDeliveryPage> {
  return request<TakerDeliveryPage>({
    method: 'get',
    url: takerAppDeliveriesUrl(pk),
    params: toTakerDeliveryParams(params),
  })
}

/** 重投一条**失败**的推送（`POST .../deliveries/<deliveryPk>/retry/`）。⚠️ **只对 `status === 'failed'` 可点**：后端对 `pending` / `done` 回 `1000`（`done` 重投 = 对方收到两次）。页面因此把按钮**只在失败行上渲染**，⛔ 不是"画出来再等后端拒"。⚠️ **被后端拒时那句中文必须原样显示**（例如「只有投递失败的推送记录可以重投（…）」）——那是**后端**的判据在说话，⛔ 前端不许自己编一句（`BizError.message` 里就是它）。⭐ 端点回的是**重排后的那一行**（`delivery_row_payload`，与列表 item 同键集，T5 的端点docstring 逐字写着）⇒ 本模块照实声明成 `TakerDeliveryRow`（⚠️ 与上家侧 `api/openApps.ts`的 `retryDelivery` 声明成 `unknown` **不同**：那边 §13.18-3 只冻结了 URL 与方法、没冻结响应体，
 * 这边后端把响应体也定死了 ⇒ 两边各自按**自己那一份契约**声明，不是谁抄谁）。⚠️ 但页面**不拿它上屏**：它只判成败，然后**重新拉一次台账**（一次重投之后列表里可能不止那一行变）。 */
export function retryTakerDelivery(pk: number, deliveryPk: number): Promise<TakerDeliveryRow> {
  return request<TakerDeliveryRow>({
    method: 'post',
    url: takerAppDeliveryRetryUrl(pk, deliveryPk),
  })
}
