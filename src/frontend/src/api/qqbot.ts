/** 【第三方平台 → QQ机器人设置】页（`pages/qqbot-settings.ts`）的端点封装（QQ 机器人线 批次C）。契约的另一半是**实现即契约**：`src/apps/qqbot/urls.py` + `api/views.py` + `api/serializers.py`（批次B）。类型按那边的序列化器 / 视图装配**逐字**写，⛔ 本模块不发明字段。| 方法 | 路径 | 用途 ||---|---|---|| GET/PATCH | `/api/qqbot/binding/` | 凭据单例。**`app_secret` 只写不读**（响应里只有 `app_secret_set` 布尔）；PATCH **留空/不带 = 不改**；带凭据格时服务端试取 token，响应多一个 `verified` 布尔 || GET | `/api/qqbot/groups/` | 群列表（`?refresh_info=<pk>` 顺手刷一个群的官方资料；白名单未批**降级**进 `info_error`，⛔ 不 500）。⭐ 行内带 `join_policy_whitelist`——「已存名单」的读出口不只 PATCH join-policy 的响应 || PATCH | `/api/qqbot/groups/<pk>/` | 只改 `remark` || PUT | `/api/qqbot/groups/<pk>/push-config/` | 推送配置**整份替换** `{is_active,msg_type,game_ids}`（PUT 语义：没带的格子=清掉） || POST | `/api/qqbot/groups/<pk>/test-push/` | 建一条测试消息入发件箱（不碰网络；**不要求机器人已配置**） || GET | `/api/qqbot/groups/<pk>/mutes/` | 禁言列表代理；失败降级进 `remote_error`（`items: null`） || GET | `/api/qqbot/groups/<pk>/join-requests/` | 入群申请：本地留痕 `items` + 远端 `remote` 两块并列；远端失败只带 `remote_error`、本地照出。⭐ 每行带 `acted_by` / `acted_by_name`（`null` = 自动处置） || POST | `/api/qqbot/groups/<pk>/join-requests/approve/` | 人工审批 `{op,join_request_id,member_openid,reject_reason?}`；**无论成败都写留痕行** || PATCH | `/api/qqbot/groups/<pk>/join-policy/` | `{mode,whitelist?}`；非 whitelist 模式服务端把名单清成 `[]` |
 * | GET/PUT | `/api/qqbot/menu/` | 群菜单（`?group=<pk>` / body `group` 键）；PUT 服务端先校验（一级≤10、二级≤5 且只许 send_message/link、link 必须 https），失败=业务码 `1000` 不调 QQ || POST | `/api/qqbot/share-link/` | `{group}` → `{url}`；未配置回业务码 `1030`（HTTP 200，前端是 `BizError`） || GET | `/api/qqbot/messages/` | 发件箱分页列表（`?group=&status=&page=`；`-created_at`；页大小固定 20） |## 两条降级形态（页面必须按字段分支，⛔ 不许当异常）- **代理类端点**（mutes / join-requests / menu / share-link）在**机器人未配置**时回
 * HTTP 200 + 业务码 `QQBOT_NOT_CONFIGURED`(1030) ⇒ `request()` 解包成 **`BizError`**（`e.code === QQBOT_NOT_CONFIGURED_CODE`、`e.message` 是后端那句人话）；远端失败则是HTTP 200 + code 0，错误在 `data.remote_error` / `data.info_error` 里——**两种都要显示原文**。- `get_menu` 的「未配置」同样走 1030（视图直接 `api_response(code=…)`，不是 `remote_error`）。## 「已存名单」的读出口`GET /api/qqbot/groups/` 的行**带** `join_policy_whitelist`（`QQBotGroupSerializer` 的fields 里有它）⇒ 页面刷新后能用群行把 `savedWhitelists`回填起来，编辑区不再被清成空。PATCH join-policy 的响应仍是名单的**写后**读出口（两者同源）。 */
import { request } from './http'
import type { Page } from './unwrap'

/** 本端点自己的节点门（批次B 登记的本仓第三条自有节点）。用途：岛页被深链（`page.html?page=qqbot-settings`）打开时**自己判一次**可见性（路由的 `permissions` + `requiresAccountAdmin` 在 iframe 里不生效）。⚠️ 与 `config/routePermissions.ts` 里 `qqbot-settings` 那条**必须同值**（两份独立声明是**有意**的，由 `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对纠错）。⚠️ 常量住在 **api 模块**（不在岛页里写字面量）：`tests/test_rbac_menu_alignment.py` 只扫`layui-island/pages/*.ts` 的带点字面量，先例是 `taker-apps` / `open-apps` 的同一做法。 */
export const QQBOT_SETTINGS_NODE = 'system.qqbot_settings'

/** 「机器人未配置」的业务码（后端 `apps/core/error_codes.py` 的 `QQBOT_NOT_CONFIGURED`）。 */
export const QQBOT_NOT_CONFIGURED_CODE = 1030

/** 凭据单例的基路径（尾斜杠与 `apps/qqbot/urls.py` 的字面路径一致）。 */
export const QQBOT_BINDING_URL = '/api/qqbot/binding/'
/** 群集合的基路径。 */
export const QQBOT_GROUPS_URL = '/api/qqbot/groups/'
/** 群菜单（`?group=<pk>` 读 / body `group` 键写）。 */
export const QQBOT_MENU_URL = '/api/qqbot/menu/'
/** 分享链接（body `{group}`）。 */
export const QQBOT_SHARE_LINK_URL = '/api/qqbot/share-link/'
/** 发件箱分页列表。 */
export const QQBOT_MESSAGES_URL = '/api/qqbot/messages/'

/** 某个群的资源路径（`PATCH groups/<pk>/` 用；其余子资源在它下面拼）。 */
export function qqbotGroupUrl(groupId: number): string {
  return `${QQBOT_GROUPS_URL}${groupId}/`
}

/** 消息类型（`QQBotGroupPushConfig.MsgType`，逐字）。 */
export type QQBotMsgType = 'markdown' | 'text'

/** 群状态（`QQBotGroup.Status`，逐字）。 */
export type QQBotGroupStatus = 'active' | 'gone'

/** 入群审批策略（`QQBotGroup.JoinPolicyMode`，逐字）。 */
export type QQBotJoinPolicyMode = 'off' | 'approve_all' | 'decline_all' | 'whitelist'

/** 发件箱消息状态（`QQBotOutboundMessage.Status`，逐字）。 */
export type QQBotMessageStatus = 'pending' | 'sent' | 'failed'

/** 远端 / 白名单失败的降级形状（`views._remote_error` / `info_error`，code 是**字符串**）。 */
export type QQBotRemoteError = { code: string; message: string }

/** 凭据单例的读面（键集 = `QQBotBindingSerializer.fields`；⛔ 没有 `app_secret` 这个键）。 */
export type QQBotBinding = {
  app_id: string
  enabled: boolean
  auth_verified: boolean
  bot_name: string
  markdown_ok: boolean
  connection_state: string
  last_alive_at: string | null
  last_event_at: string | null
  last_error: string
  menu_version: number | null
  /** 只回答「配没配」，不是「配的是什么」（`get_app_secret_set`）。 */
  app_secret_set: boolean
}

/** PATCH binding 的响应：`verified` 只在**这次请求带了凭据格**时出现（`views.QQBotBindingView.patch`）。 */
export type QQBotBindingSaveResult = QQBotBinding & { verified?: boolean }

/** PATCH binding 的字段（**全部可选**）。
 * ⚠️ `app_secret` **留空/不带 = 不改**（序列化器把空串 pop 掉）⇒ 调用方判「发不发这个键」，
 * ⛔ 不许发 `app_secret: ''` 去当「不改」——虽然后端也接得住，但「键在不在」才是可读的判据。 */
export type QQBotBindingPatch = {
  app_id?: string
  app_secret?: string
  enabled?: boolean
  auth_verified?: boolean
}

/** 群行里的推送配置摘要（`get_push_config`；`null` = 这个群还没配过推送）。 */
export type QQBotPushConfigSummary = {
  is_active: boolean
  msg_type: QQBotMsgType
  game_ids: number[]
}

/** 群列表行（键集 = `QQBotGroupSerializer.fields`；⭐ P1-2 起**含** `join_policy_whitelist`，见文件头）。 */
export type QQBotGroupRow = {
  id: number
  group_openid: string
  remark: string
  group_name: string
  /** `null` = 还没拿到官方资料（多半是白名单接口未批）⇒ 页面显示占位，⛔ 不是 0。 */
  member_num: number | null
  member_role: string
  allow_proactive_msg: boolean | null
  recv_msg_setting: string
  status: QQBotGroupStatus
  first_seen_at: string
  last_seen_at: string
  join_policy_mode: QQBotJoinPolicyMode
  /** 已存的审批白名单（名单不是机密：member_openid 本来就显示在留痕表里）。 */
  join_policy_whitelist: string[]
  push_config: QQBotPushConfigSummary | null
}

/** `GET /api/qqbot/groups/` 的 data：群行 + 可选的降级说明（`?refresh_info` 失败时）。 */
export type QQBotGroupsPayload = { items: QQBotGroupRow[]; info_error?: QQBotRemoteError }

/** 推送配置 PUT 的**整份替换**载荷（`QQBotPushConfigSerializer`：三格都必填）。 */
export type QQBotPushConfigPayload = {
  is_active: boolean
  msg_type: QQBotMsgType
  game_ids: number[]
}

/** 推送配置 PUT 的响应（`views.QQBotGroupPushConfigView.put`）。 */
export type QQBotPushConfigResult = {
  group_id: number
  is_active: boolean
  msg_type: QQBotMsgType
  game_ids: number[]
}

/** 测试推送的响应：行落库即成功（`views.QQBotGroupTestPushView.post`）。 */
export type QQBotTestPushResult = { id: number; status: string }

/** 禁言列表：`items: null` = 远端失败（看 `remote_error`），`[]` = 真的没有。 */
export type QQBotMutesPayload = { items: unknown[] | null; remote_error?: QQBotRemoteError }

/** 本地留痕行（键集 = `views.QQBotGroupJoinRequestsView.get` 的行装配，逐字）。 */
export type QQBotJoinRequestRow = {
  id: number
  join_request_id: string
  member_openid: string
  username: string
  apply_at: string | null
  apply_source: string
  risk_tips: string
  verify_info: unknown
  decision: string
  reject_reason: string
  error_code: string
  acted_at: string | null
  /** ⭐ P2-3：操作人（用户主键）＋显示名；两个都 `null` = 自动处置（机器路径）。 */
  acted_by: number | null
  acted_by_name: string | null
  created_at: string
}

/** 入群申请的 data：本地 `items` 恒在；`remote` / `remote_error` 二选一（也可能都没有）。 */
export type QQBotJoinRequestsPayload = {
  items: QQBotJoinRequestRow[]
  remote?: unknown
  remote_error?: QQBotRemoteError
}

/** 人工审批的载荷（`QQBotJoinRequestActionSerializer`；`reject_reason` 可选）。 */
export type QQBotJoinActionPayload = {
  op: 'approve' | 'decline'
  join_request_id: string
  member_openid: string
  reject_reason?: string
}

/** 人工审批的响应：`ok=false` 时 `error` 带远端码与文案（留痕行**仍会写**）。 */
export type QQBotJoinActionResult = {
  ok: boolean
  decision: string
  error: QQBotRemoteError | null
}

/** 审批策略 PATCH 的载荷（`whitelist` 仅 whitelist 模式有意义；缺省 = 后端按 `[]` 处理）。 */
export type QQBotJoinPolicyPayload = { mode: QQBotJoinPolicyMode; whitelist?: string[] }

/** 审批策略 PATCH 的响应（这里**有** `join_policy_whitelist`——它是名单的唯一读出口）。 */
export type QQBotJoinPolicyResult = {
  group_id: number
  join_policy_mode: QQBotJoinPolicyMode
  join_policy_whitelist: string[]
}

/** PUT menu 的载荷：`group` 指定群，`menu` 原样透传给官方 `put_menu`（服务端已校验）。 */
export type QQBotMenuPutPayload = { group: number; menu: { buttons: unknown[] } }

/** GET menu 的 data：`menu` 是官方回包原样；失败降级时 `menu`/`version` 都是 `null` + `remote_error`。 */
export type QQBotMenuGetResult = {
  menu: unknown
  version: number | null
  remote_error?: QQBotRemoteError
}

/** PUT menu 的 data：校验/远端失败时 `ok:false` + `remote_error`；业务校验失败是 code 1000（BizError）。 */
export type QQBotMenuPutResult = {
  ok: boolean
  version?: number | null
  remote_error?: QQBotRemoteError
}

/** 分享链接的 data：远端失败时 `url: null` + `remote_error`；未配置是 code 1030（BizError）。 */
export type QQBotShareLinkResult = { url: string | null; remote_error?: QQBotRemoteError }

/** 发件箱一行（键集 = `views.QQBotMessageListView.get` 的行装配，逐字；`payload_text` 服务端已截断）。 */
export type QQBotMessageRow = {
  id: number
  group_id: number
  group_openid: string
  kind: string
  order_id: number | null
  msg_type_eff: string
  payload_text: string
  status: QQBotMessageStatus
  attempts: number
  error_code: string
  error_text: string
  scheduled_at: string
  sent_at: string | null
  created_at: string
}

/** 发件箱列表的查询参数（`?group=&status=&page=`；没给的键不发，`toQQBotMessageParams`）。 */
export type QQBotMessageListParams = { group?: number; status?: QQBotMessageStatus; page?: number }

/** 发件箱列表的响应形状（`apps/core/envelope.py::paginated`）。 */
export type QQBotMessagePage = Page<QQBotMessageRow>

/** 读凭据单例。 */
export function getQQBotBinding(): Promise<QQBotBinding> {
  return request<QQBotBinding>({ method: 'get', url: QQBOT_BINDING_URL })
}

/** 改凭据与开关（PATCH，部分更新）。
 * ⚠️ 响应里的 `verified`：这次请求带了 `app_id` / `app_secret` 且凭据齐 ⇒ 服务端**同步试取
 * 一次 token**；`false` 时配置**仍已保存**、错误文案在 `last_error`（页面要把它显示出来）。 */
export function updateQQBotBinding(payload: QQBotBindingPatch): Promise<QQBotBindingSaveResult> {
  return request<QQBotBindingSaveResult>({ method: 'patch', url: QQBOT_BINDING_URL, data: payload })
}

/** 群列表（可选 `refreshInfo`：顺手刷一个群的官方资料）。
 * ⚠️ 白名单接口未批时**不是错误**：HTTP 200 + `info_error` 字段，群行照出
 * （`group_name` / `member_num` 是空的）⇒ 页面按 `info_error` 提示、按空值显示占位。 */
export function listQQBotGroups(refreshInfo?: number): Promise<QQBotGroupsPayload> {
  return request<QQBotGroupsPayload>({
    method: 'get',
    url: QQBOT_GROUPS_URL,
    params: refreshInfo === undefined ? {} : { refresh_info: refreshInfo },
  })
}

/** 只改群备注（`QQBotGroupRemarkSerializer` 只收 `remark`），响应是**整行**（刷新用）。 */
export function updateQQBotGroupRemark(groupId: number, remark: string): Promise<QQBotGroupRow> {
  return request<QQBotGroupRow>({
    method: 'patch',
    url: qqbotGroupUrl(groupId),
    data: { remark },
  })
}

/** 推送配置整份替换（PUT 语义：三格都发，没发的格子就是「清掉」）。 */
export function putQQBotGroupPushConfig(
  groupId: number,
  payload: QQBotPushConfigPayload,
): Promise<QQBotPushConfigResult> {
  return request<QQBotPushConfigResult>({
    method: 'put',
    url: `${qqbotGroupUrl(groupId)}push-config/`,
    data: payload,
  })
}

/** 建一条测试消息（行落库即成功；发不发得出去是 worker 与凭据的事）。 */
export function testQQBotGroupPush(groupId: number): Promise<QQBotTestPushResult> {
  return request<QQBotTestPushResult>({ method: 'post', url: `${qqbotGroupUrl(groupId)}test-push/` })
}

/** 禁言列表（代理；`items: null` = 远端失败，看 `remote_error`）。 */
export function listQQBotGroupMutes(groupId: number): Promise<QQBotMutesPayload> {
  return request<QQBotMutesPayload>({ method: 'get', url: `${qqbotGroupUrl(groupId)}mutes/` })
}

/** 入群申请（本地留痕 + 远端两块并列；远端失败只带 `remote_error`、本地照出）。 */
export function listQQBotGroupJoinRequests(groupId: number): Promise<QQBotJoinRequestsPayload> {
  return request<QQBotJoinRequestsPayload>({
    method: 'get',
    url: `${qqbotGroupUrl(groupId)}join-requests/`,
  })
}

/** 人工审批一条入群申请（`ok:false` 时留痕行**仍会写**，页面把 `error.message` 显示出来）。 */
export function approveQQBotJoinRequest(
  groupId: number,
  payload: QQBotJoinActionPayload,
): Promise<QQBotJoinActionResult> {
  return request<QQBotJoinActionResult>({
    method: 'post',
    url: `${qqbotGroupUrl(groupId)}join-requests/approve/`,
    data: payload,
  })
}

/** 审批策略（非 whitelist 模式服务端把名单清成 `[]`；响应带回**存下来的**名单）。 */
export function updateQQBotGroupJoinPolicy(
  groupId: number,
  payload: QQBotJoinPolicyPayload,
): Promise<QQBotJoinPolicyResult> {
  return request<QQBotJoinPolicyResult>({
    method: 'patch',
    url: `${qqbotGroupUrl(groupId)}join-policy/`,
    data: payload,
  })
}

/** 读群菜单（`menu` 是官方回包原样；未配置是 code 1030 ⇒ `BizError`）。 */
export function getQQBotMenu(groupId: number): Promise<QQBotMenuGetResult> {
  return request<QQBotMenuGetResult>({
    method: 'get',
    url: QQBOT_MENU_URL,
    params: { group: groupId },
  })
}

/** 保存群菜单（服务端先校验；业务校验失败 code 1000 ⇒ `BizError`，`e.message` 是中文清单）。 */
export function putQQBotMenu(payload: QQBotMenuPutPayload): Promise<QQBotMenuPutResult> {
  return request<QQBotMenuPutResult>({ method: 'put', url: QQBOT_MENU_URL, data: payload })
}

/** 生成群的分享链接（未配置是 code 1030；远端失败 `url: null` + `remote_error`）。 */
export function createQQBotShareLink(groupId: number): Promise<QQBotShareLinkResult> {
  return request<QQBotShareLinkResult>({
    method: 'post',
    url: QQBOT_SHARE_LINK_URL,
    data: { group: groupId },
  })
}

/** 发件箱查询参数（**唯一的线上形状出口**）：没给的键不发。 */
export function toQQBotMessageParams(
  params: QQBotMessageListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.group !== undefined) out.group = params.group
  if (params.status !== undefined) out.status = params.status
  if (params.page !== undefined) out.page = params.page
  return out
}

/** 发件箱分页列表（`-created_at`；页大小后端定死 20）。 */
export function listQQBotMessages(
  params: QQBotMessageListParams = {},
): Promise<QQBotMessagePage> {
  return request<QQBotMessagePage>({
    method: 'get',
    url: QQBOT_MESSAGES_URL,
    params: toQQBotMessageParams(params),
  })
}
