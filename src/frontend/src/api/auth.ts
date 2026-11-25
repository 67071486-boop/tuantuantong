import { request } from './http'
import { appChatGrant, appChatHosted } from '@/m/components/appChatWebView'
import { AuthExpiredError } from './errors'
import { bindBrowserChatSession, browserChatCsrf, confirmBrowserChatLogin, endBrowserChatSession } from './chatSession'
import { bindOrderFinancePage, confirmOrderFinanceLogin, invalidateOrderFinanceSession, orderFinancePageCookie } from './orderFinanceDirect'

/** `data.user`（后端 `apps/core/auth_views.py:96-117` 的 `_user_payload`；login 与 me 同形）。⚠️ **行号是执行期实测值**：任务书写的 `:82-103` 与工作区实测不符；常量本身原先写的 `:48-56` 在**基线时**就已经不对（那是函数头附近的注释区，不是返回的载荷字典）。载荷字典今天的范围 = `_user_payload`（定义在 `:54`）里 `return {` 到它的收尾 `}`。 */
export type User = {
  id: number
  internal_chat_enabled?: boolean
  username: string
  display_name: string
  identity_plane: string
  is_tenant_admin: boolean
  /** 账号线新增的第二个管理员判据：`is_superuser` **或**持「系统管理员」Group。声明为**可选**：服务端自账号线起恒回该字段，但消费侧（`stores/session.ts` 的`isAccountAdmin`）本来就 `?? false` **fail-closed**，而它缺失只会让系统人员菜单更不可见、不会放开任何东西。可选让跨版本后端与测试夹具不必编造这个布尔。 */
  is_account_admin?: boolean
  roles: string[]
  dashboard_type?: 'admin' | 'worker' | 'work' | ''
  /** RBAC 节点 code 的**排序后并集**（①线 `/api/auth/me` 新增，spec §5.3）。含 `available=False` 的预留点（预留点也可能被勾选，前端要能显示出来）。⚠️ **对 `is_superuser` 没有短路**（与后端的 `has_node_now` 不同）——所以消费侧**必须**与 `is_tenant_admin` / `is_account_admin` **组合**，不能只读它：部署顺序里 `seed_rbac_catalog`（步骤 3）与 `migrate_rbac_roles --apply`（步骤 4）之间，`is_tenant_admin` 账号的这个数组**确实是 `[]`**。声明为**可选**，消费侧 `?? []`（fail-closed：缺失只会让菜单更不可见，不会放开任何东西）。 */
  menu_permissions?: string[]
  /** 当前账号有权访问的游戏主键（**升序**；「按游戏权限收窄」线 Task 1 给 `/api/auth/me`与 `/api/auth/login` 的 `user` 载荷新增的字段）。四种取值口径（后端 `apps/business/game_scope.py::games_of`）：- 租户平面、有档案且有游戏 ⇒ 那几个 id；- 租户平面、**没有档案**或**一款没勾** ⇒ `[]`（严格口径：什么都看不到）；- `public` 平面 ⇒ `[]`（游戏是租户里的东西）；- **租户管理员** ⇒ 全部游戏（`games_of` 自己带管理员豁免，判据与 `scope_queryset` /`assert_game_allowed` **同源**：都是 `apps.core.permissions.is_tenant_admin`）。⚠️ 收口评审 A-1 更正：这里原写「`scope_queryset` 对管理员短路」——`scope_queryset`**不是**本字段的取数路径（`_user_payload` 直接调 `games_of`），当初是**假话**；
   * 豁免已补进 `games_of`。供给出来的租户 `admin` **按构造没有档案** ⇒ 没有它`game_ids` 会是 `[]`，数据页游戏下拉变空 ⇒ 管理员从前端录不了单。⚠️ 声明为**可选**：服务端自本线起恒回它，而消费侧（`api/config.ts` 的 `fetchGames`）按 `?? []` **fail-closed**——缺失只会让**数据页**的下拉变空，不会放开任何东西（配置面走的是不看这个字段的 `fetchAllGames`）。 */
  game_ids?: number[]
  permissions: string[]
  /** 当前租户（工作室）名；服务端恒回，缺失按空串（`?? ''`）。 */
  tenant_name?: string
  /** **单值**职位（多个 Group 时取名称升序首个）。⚠️ 无角色时服务端给 `''`、**不是** `null`
   * （与 `/api/accounts/` 的 `role` 字段不同：那个在无角色时是 `null`，因为 Django 那边是
   * `null=True` 的列，而本键是从用户名册算出来的字符串）——消费侧一律 `?? ''`。 */
  role?: string
  /** **本人**头像的短时效读链接（顶栏头像同步线，2026-09-25；后端 `_user_payload` 的`avatar_url`）。没有上传过 / 平台平面 ⇒ `null`。⚠️ 是**派生的读链接**、不是可写列：⛔ 前端不拼它、不落库；过期由后端重签。⚠️ 声明为**可选**（与 `is_account_admin` / `menu_permissions` / `game_ids` 同一取舍）：消费侧（`AppSidebar.vue`）按 `?? null` 处理——缺失只会让顶栏回落生成的头像，不会放开任何东西，也让跨版本后端与测试夹具不必编造这个字段。 */
  avatar_url?: string | null
  /** 网页端 Chatwoot 小部件的身份（2026-10-22；后端 `apps/core/auth_views.py::_user_payload`的 `include_web_chat`，取值口径在 `apps/accounts/web_chat.py`）。形状 = `web-<identity_plane>-<id>`，⛔ **不是**裸 `id`：Chatwoot 按 identifier 合并联系人，裸主键会把两个租户的同 id 用户并成一个人（计划 §2.3-1）。声明的语义：**四个字段同时在场才有意义**（服务端「三个 env 缺任一 ⇒ 四个都不下发」）。消费侧（`chatwootWidget.ts`）缺任一个都走 fail-closed：不 `setUser`、不挂气泡，⛔ 不退化成匿名会话。 */
  chat_identifier?: string
  /** `HMAC-SHA256(chat_identifier, 网页收件箱 hmac 密钥)` 的小写 hex（密钥只住服务端）。 */
  chat_identifier_hash?: string
  /** Chatwoot 实例地址（`chatwootSDK.run` 的 `baseUrl`）。**公开值**，由后端下发
   * （`CHATWOOT_WEB_BASE_URL`）——⛔ 前端不另建 `VITE_*` 配置：两份真值源换收件箱必漏一处。 */
  chat_base_url?: string
  /** 网页收件箱的 website token（`chatwootSDK.run` 的 `websiteToken`）。**公开值**，
   * 同样由后端下发（`CHATWOOT_WEB_WEBSITE_TOKEN`）；hmac 密钥仍只住服务端。 */
  chat_website_token?: string
}

export type LoginPayload = { username: string; password: string }

/** GET /api/auth/csrf：让 Django 下发 `csrftoken`（公开端点，但也可能被守卫短路成 401）。 */
export function fetchCsrf(): Promise<null> {
  return request<null>({ url: '/api/auth/csrf', method: 'GET', skipUnauthorizedHandler: true })
}

export function login(payload: LoginPayload): Promise<User> {
  return request<{ user: User }>({
    url: '/api/auth/login',
    method: 'POST',
    data: payload,
    skipUnauthorizedHandler: true,
  }).then((d) => {
    confirmBrowserChatLogin(d.user.id)
    confirmOrderFinanceLogin(d.user.id)
    return d.user
  })
}

/** POST /api/auth/logout：服务端幂等（未登录也 200）。 */
export function logout(): Promise<null> {
  endBrowserChatSession(true)
  invalidateOrderFinanceSession(true)
  return request<null>({ url: '/api/auth/logout', method: 'POST' })
}

export async function fetchMe({ fresh = false, signal }: { fresh?: boolean; signal?: AbortSignal } = {}): Promise<User> {
  if (appChatHosted && !appChatGrant) throw new AuthExpiredError('聊天登录已过期')
  let chatCsrf = appChatHosted ? undefined : browserChatCsrf()
  let orderFinanceCsrf = orderFinancePageCookie()
  if (chatCsrf === null || orderFinanceCsrf === null) {
    await fetchCsrf().catch(() => undefined)
    chatCsrf = browserChatCsrf()
    orderFinanceCsrf = orderFinancePageCookie()
  }
  const { user } = await request<{ user: User }>({
    url: appChatGrant ? '/api/internal-chat/app/me/' : '/api/auth/me',
    method: 'GET',
    ...(signal ? { signal } : {}),
    ...(appChatGrant ? { baseURL: '' } : {}),
    ...(appChatGrant ? { headers: { Authorization: `Chat ${appChatGrant}`, 'Cache-Control': 'no-cache' } }
      : fresh ? { headers: { 'Cache-Control': 'no-cache' } } : {}),
    skipUnauthorizedHandler: true,
  })
  if (signal?.aborted) throw new AuthExpiredError('登录状态已变更，请重新打开页面')
  bindBrowserChatSession(user.id, chatCsrf)
  bindOrderFinancePage(user.id, orderFinanceCsrf)
  return user
}
