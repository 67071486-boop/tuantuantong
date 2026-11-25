/** 【功能维护 → 企微自建应用】v2 页（`V2WecomSettings.vue`）的端点封装（D-0379）。
 * 契约的另一半是**实现即契约**：`src/apps/wecom/urls.py` + `views.py`（类型按那边的
 * 视图装配**逐字**写，⛔ 本模块不发明字段）。
 *
 * | 方法 | 路径 | 用途 |
 * |---|---|---|
 * | GET/PUT | `/api/wecom/config/` | 配置单例。**secret 族只写不读**（响应只有 `*_masked`）；PUT **留空/不带 = 不改** |
 * | POST | `/api/wecom/config/selfcheck/` | 两类 token 各真拉一次，逐项 `{ok,skipped,message}` |
 * | POST | `/api/wecom/config/test_send/` | 应用消息测试发送（企微 errcode≠0 ⇒ 业务码 1040，`msg` 是人话） |
 * | GET | `/api/wecom/contacts/departments/` | 部门列表（proxy） |
 * | GET | `/api/wecom/contacts/users/?department_id=` | 部门成员列表（proxy，simplelist） |
 * | GET | `/api/wecom/messages/?page=` | 回调消息台账（倒序 50/页） |
 * | GET | `/api/wecom/external/follow_users/` | 配置了客户联系的成员（proxy） |
 * | GET | `/api/wecom/external/customers/?userid=` | 成员的客户列表 + 详情（proxy） |
 * | GET | `/api/wecom/external/groupchats/?cursor=&chat_id=` | 客户群列表 / 详情（proxy） |
 *
 * ## 两条业务码（`apps/core/error_codes.py`；页面按码分支，⛔ 不按 msg 猜）
 * - **未配置** = `WECOM_NOT_CONFIGURED`(1039)：页面显示「先填凭据」引导；
 * - **远端失败** = `WECOM_REMOTE_FAILED`(1040)：`msg` 已是企微错误的人话，原样显示。
 * 业务码 ≠ 0 时 `request()` 抛 `BizError`（`api/http.ts`），`e.code` / `e.message` 分支。
 */
import { request } from './http'
import type { Page } from './unwrap'

/** 本端点自己的节点门（后端 `apps/rbac/catalog.py` 的自有节点）。用途：v2 页深链时
 * 页内自检（fail-closed）。⚠️ 与 `config/routePermissions.ts` 的 `'v2-wecom-settings'`
 * 同值（两份独立声明是有意的）。**钉它的网**（与 qqbot 那颗常量的钉法不同——本页
 * v2-only、旧路由表无 `wecom-settings` 名 ⇒ route-declarations.spec (c) 对它 skip）：
 * `tests/test_rbac_menu_alignment.py`（后端树 ↔ 菜单/路由声明）、
 * `v2/__tests__/v2Routes.spec.ts`（v2 路由 meta 逐字）、本页 spec 的 fail-closed 用例。 */
export const WECOM_SETTINGS_NODE = 'system.wecom_settings'

/** 「未配置」业务码（后端 `WECOM_NOT_CONFIGURED`）。 */
export const WECOM_NOT_CONFIGURED_CODE = 1039
/** 「企微侧失败」业务码（后端 `WECOM_REMOTE_FAILED`）。 */
export const WECOM_REMOTE_FAILED_CODE = 1040

const CONFIG_URL = '/api/wecom/config/'
const SELFCHECK_URL = '/api/wecom/config/selfcheck/'
const TEST_SEND_URL = '/api/wecom/config/test_send/'
const DEPARTMENTS_URL = '/api/wecom/contacts/departments/'
const USERS_URL = '/api/wecom/contacts/users/'
const MESSAGES_URL = '/api/wecom/messages/'
const FOLLOW_USERS_URL = '/api/wecom/external/follow_users/'
const CUSTOMERS_URL = '/api/wecom/external/customers/'
const GROUPCHATS_URL = '/api/wecom/external/groupchats/'

/** 配置读面（键集 = `views.WecomConfigView.get`；⛔ 没有 `secret` 这个键——只回掩码）。 */
export type WecomConfig = {
  corp_id: string
  agent_id: number
  secret_masked: string
  contacts_secret_masked: string
  /** 原样回：它们是要复制进企微后台「接收消息」配置的值。 */
  callback_token: string
  encoding_aes_key: string
  enabled: boolean
  ready: boolean
  contacts_ready: boolean
  /** 最近一次回调 GET 验证成功时刻；null = 还没通过。 */
  verified_at: string | null
  callback_url: string
}

/** PUT 配置的载荷。⚠️ secret 族**留空/不带 = 不改**（⛔ 不发空串去当「清掉」）。 */
export type WecomConfigPut = {
  corp_id: string
  agent_id: number
  secret?: string
  callback_token?: string
  encoding_aes_key?: string
  contacts_secret?: string
  enabled: boolean
}

export type WecomSelfCheckItem = { ok: boolean; skipped: boolean; message: string }
export type WecomSelfCheckReport = { main: WecomSelfCheckItem; contacts: WecomSelfCheckItem }

export type WecomDepartment = { id: number; name: string; parentid?: number }
export type WecomUserRow = { userid: string; name?: string; department?: number[] }

/** 回调消息台账行（键集 = `views.WecomMessagesView`）。 */
export type WecomMessageRow = {
  id: number
  msg_id: string | null
  from_user_name: string
  msg_type: string
  event: string | null
  agent_id: number | null
  content: string
  received_at: string
}

export type WecomCustomerContact = { external_contact: { name?: string }; follow_info?: { userid: string } }
export type WecomGroupChatRow = { chat_id: string }
export type WecomGroupChatInfo = { chat_id: string; name?: string; member_list?: Array<{ member: { userid: string; name?: string } }> }

/** 读配置（未配置的租户也有全默认行）。 */
export async function getWecomConfig(): Promise<WecomConfig> {
  return request<WecomConfig>({ method: 'get', url: CONFIG_URL })
}

/** 保存配置；`ready` 回「这次保存后凭据齐不齐」。 */
export async function putWecomConfig(payload: WecomConfigPut): Promise<{ ready: boolean }> {
  return request<{ ready: boolean }>({ method: 'put', url: CONFIG_URL, data: payload })
}

/** 连通性自检（两类 token 各真拉一次）。 */
export async function runWecomSelfCheck(): Promise<WecomSelfCheckReport> {
  return request<WecomSelfCheckReport>({ method: 'post', url: SELFCHECK_URL, data: {} })
}

/** 测试发消息（企微 errcode≠0 ⇒ 抛 `BizError`，code 1040、message 是人话）。 */
export async function sendWecomTestMessage(toUser: string, content: string): Promise<{ sent: boolean }> {
  return request<{ sent: boolean }>({
    method: 'post',
    url: TEST_SEND_URL,
    data: { to_user: toUser, content },
  })
}

export async function listWecomDepartments(departmentId?: number): Promise<WecomDepartment[]> {
  const data = await request<{ departments: WecomDepartment[] }>({
    method: 'get',
    url: DEPARTMENTS_URL,
    params: departmentId === undefined ? undefined : { department_id: departmentId },
  })
  return data.departments
}

export async function listWecomUsers(departmentId: number): Promise<WecomUserRow[]> {
  const data = await request<{ users: WecomUserRow[] }>({
    method: 'get',
    url: USERS_URL,
    params: { department_id: departmentId },
  })
  return data.users
}

export async function listWecomMessages(page: number): Promise<Page<WecomMessageRow>> {
  return request<Page<WecomMessageRow>>({ method: 'get', url: MESSAGES_URL, params: { page } })
}

export async function listWecomFollowUsers(): Promise<string[]> {
  const data = await request<{ follow_users: string[] }>({ method: 'get', url: FOLLOW_USERS_URL })
  return data.follow_users
}

export async function listWecomCustomers(
  userid: string,
): Promise<{ external_userids: string[]; contacts: WecomCustomerContact[] }> {
  return request({ method: 'get', url: CUSTOMERS_URL, params: { userid } })
}

export async function listWecomGroupChats(
  cursor = '',
): Promise<{ group_chat_list: WecomGroupChatRow[]; next_cursor: string }> {
  return request({
    method: 'get',
    url: GROUPCHATS_URL,
    params: cursor === '' ? undefined : { cursor },
  })
}

export async function getWecomGroupChatInfo(
  chatId: string,
): Promise<WecomGroupChatInfo | null> {
  const data = await request<{ group_chat: WecomGroupChatInfo | null }>({
    method: 'get',
    url: GROUPCHATS_URL,
    params: { chat_id: chatId },
  })
  return data.group_chat
}
