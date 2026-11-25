/** 【自助注册】桌岛页（`layui-island/pages/self-registration.ts`）与 /m/ 公开注册页（`m/pages/SelfRegisterPage.vue`）的端点封装。spec：`docs/specs/2026-10-16-self-registration-links-design.md` §3（**API 契约**，逐字）。契约的另一半是**实现即契约**：`src/apps/accounts/selfreg_views.py` + `apps/accounts/urls.py`（挂在 `/api/accounts/` 前缀下）。| 方法 | 路径 | 门 | 用途 ||---|---|---|---|| GET/PUT | `/api/accounts/self-registration/config/` | 登录 + `system_user.add_edit` | 注册配置（游戏池 + 默认禁接单开关） |
 * | GET/POST | `/api/accounts/self-registration/invites/` | 登录 + `system_user.add_edit` | 链接与注册记录 / 生成一条一次性链接 || POST | `/api/accounts/self-registration/invites/<id>/revoke/` | 登录 + `system_user.add_edit` | 作废一条未使用的链接（D-0253：立即过期，零迁移） || GET/POST | `/api/accounts/self-registration/register/` | **匿名**（`AllowAny` + 无会话，PeerBindView 先例） | 注册页首屏 / 提交注册 |## 两条不能写错的纪律1. ⛔ **token 明文只在 `POST invites` 的响应里出现一次**（spec §3.2）：后端库里只存sha256（`token_hash`），前端拿到的明文只用于拼 `/m/register?tk=…` 这一件事。
 * ⛔ 不写日志、不写本地存储、不发往别的端点。2. ⚠️ **注册两跳都是匿名请求**：`request()` 对 POST 会先引导 CSRF cookie（无害的一次 GET，LoginView 同一条路），而 DRF `authentication_classes=[]` 的视图不校验CSRF（spec §8-5）——引导失败会如实报错，不存在"匿名发不出 POST"的分支。 */
import { request } from './http'

/** 配置端点（spec §3.1）。⚠️ 尾斜杠是契约的一部分。 */
export const SELF_REG_CONFIG_URL = '/api/accounts/self-registration/config/'
/** 链接端点（spec §3.2：生成 + 记录列表）。 */
export const SELF_REG_INVITES_URL = '/api/accounts/self-registration/invites/'
/** 公开注册端点（spec §3.3）。 */
export const SELF_REG_REGISTER_URL = '/api/accounts/self-registration/register/'

/** 注册配置（GET 的 `data`；PUT 的载荷与响应同形）。⚠️ 编号两列（spec §2.1）：`number_prefix` 只允许 1~5 个英文字母（**判在服务端**），`number_start` ≥ 1、没配是 `null`。两列**都非空**才算配置了编号——只填一列服务端照样存（按未配置处理），前端不做「缺一就不让存」的预检。 */
export type SelfRegConfig = {
  game_ids: number[]
  forbid_orders_default: boolean
  number_prefix: string
  number_start: number | null
}

/** 链接与注册记录的一行（spec §3.2 的逐字键名）。
 * `status` 是**服务端推导**的枚举（不落列：consumed_at ⇒ used、过期 ⇒ expired），
 * 中文文案由页面按状态映射（本模块不镜像词表）。 */
export type SelfRegInviteRow = {
  id: number
  created_at: string
  created_by_name: string
  status: SelfRegInviteStatus
  expires_at: string
  consumed_at: string | null
  consumed_by_name: string | null
  chosen_game_names: string[]
}

export type SelfRegInviteStatus = 'unused' | 'used' | 'expired'

/** `POST invites` 的响应（spec §3.2：`{token, expires_at}`——token 明文唯一出口）。 */
export type SelfRegInviteCreated = {
  token: string
  expires_at: string
}

/** 注册页首屏（spec §3.3）：有效 / 无效是**判别联合**，页面按 `valid` 分支渲染。 */
export type SelfRegisterScreen =
  | { valid: true; tenant_name: string; games: { id: number; name: string }[]; expires_at: string }
  | { valid: false }

/** `POST register` 的响应（spec §2.1 / §3.3 第 7 步）：初始密码唯一出口，⛔ 前端不硬编码。
 * `display_name`（账号名称）= 服务端算好的编号或支付宝姓名，/m/ 成功页第二行靠它；
 * ⛔ 前端**不**自己拼编号——那要复刻 `max(start, 同前缀最大号+1)`，两处实现必然漂移。 */
export type SelfRegisterDone = {
  username: string
  display_name: string
  initial_password: string
}

/** 读注册配置（行不存在时后端按默认值回：`game_ids: []` + 开关关）。 */
export function fetchSelfRegConfig(): Promise<SelfRegConfig> {
  return request<SelfRegConfig>({ url: SELF_REG_CONFIG_URL, method: 'GET' })
}

/** 保存注册配置（spec §3.1：`game_ids ≥ 1` 且都在本租户 `business.Game` 里——
 * **判在服务端**；本模块不重复校验，只如实把请求发出去）。 */
export function saveSelfRegConfig(payload: SelfRegConfig): Promise<SelfRegConfig> {
  return request<SelfRegConfig>({ url: SELF_REG_CONFIG_URL, method: 'PUT', data: payload })
}

/** 生成一条一次性注册链接（无请求体，spec §3.2 第 1 步明写）。 */
export function createSelfRegInvite(): Promise<SelfRegInviteCreated> {
  return request<SelfRegInviteCreated>({ url: SELF_REG_INVITES_URL, method: 'POST' })
}

/** 单条链接「作废」端点的 URL（D-0253）。⚠️ 由台账行的 `id` 拼装，**唯一出口在这里**——
 * 页面不自己拼（拼错一处的症状是 404，而两处拼装会漂到测试看不见的地方）。 */
export function selfRegInviteRevokeUrl(id: number): string {
  return `${SELF_REG_INVITES_URL}${id}/revoke/`
}

/** 作废一条未使用的链接（D-0253，2026-10-18：效果 = 立即过期，`data` 为空）。
 * ⚠️ 只对**未消费且未过期**的行可作废；已消费 / 已过期（含已作废）⇒ 后端回
 * **明确业务文案**（code 1000，`unwrap` 抛成 BizError 落到页面），页面如实透传。 */
export function revokeSelfRegInvite(id: number): Promise<void> {
  return request<void>({ url: selfRegInviteRevokeUrl(id), method: 'POST' })
}

/** 链接与注册记录（spec §3.2：最新 200 条**平铺**、有意不分页）。
 * ⚠️ "平铺" ⇒ `data` 就是**数组本身**，不是 `{items, total}` 那种分页信封——
 * 那是本 spec 有意偏离现行分页惯例的地方（spec §8-4）。 */
export function listSelfRegInvites(): Promise<SelfRegInviteRow[]> {
  return request<SelfRegInviteRow[]>({ url: SELF_REG_INVITES_URL, method: 'GET' })
}

/** 注册页首屏（匿名；`tk` 是链接里的明文令牌）。 */
export function fetchSelfRegisterScreen(tk: string): Promise<SelfRegisterScreen> {
  return request<SelfRegisterScreen>({
    url: SELF_REG_REGISTER_URL,
    method: 'GET',
    params: { tk },
  })
}

/** 提交注册（匿名；spec §2.1 的载荷 `{tk, mobile, alipay_account, alipay_name, game_ids}`）。⚠️ **没有 `display_name`**（姓名栏删了）：显示名由服务端算（编号或支付宝姓名），前端发了也会被忽略——两栏支付宝**都必填**（服务端判，前端只做体验层预检）。⚠️ 失败（手机号已注册 / 链接失效 / 限流……）一律以 **BizError** 落到页面（信封 code ≠ 0 ⇒ `unwrap` 抛出），文案是**后端那句**——⛔ 页面不新造一套判据。 */
export function submitSelfRegister(payload: {
  tk: string
  mobile: string
  alipay_account: string
  alipay_name: string
  game_ids: number[]
}): Promise<SelfRegisterDone> {
  return request<SelfRegisterDone>({ url: SELF_REG_REGISTER_URL, method: 'POST', data: payload })
}
