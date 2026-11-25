/** 【第三方平台 → 微信公众号设置】页（`pages/system-wechatmp-settings.ts`）的端点封装。契约的另一半是**实现即契约**：`src/apps/wechatmp/urls.py` + `views.py`。决策 **D-0268**（自开发）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/wechatmp/config/` | 读配置（**appsecret 只回掩码**；token / aes_key 原文——它们要复制进公众平台）+ 服务器配置信息卡 || PUT | `/api/wechatmp/config/` | 保存；`appsecret` / `token` / `aes_key` **留空 = 保持不变**（S4） || POST | `/api/wechatmp/menu/sync/` | 同步预设菜单（含「绑定账号」view 子菜单；errcode≠0 ⇒ 业务码 1000） |
 * | GET | `/api/wechatmp/menu/` | 看当前菜单（cgi-bin/menu/get 的原文） || DELETE | `/api/wechatmp/menu/` | 删除菜单 || ⛔ | 回调 / 绑定那几条**不在本模块** | `/api/wechatmp/callback`（微信服务器调）与 `/api/wechatmp/bind-*`（微信内浏览器走）是**另一侧**的入口，控制台不用它们 |## ⚠️ 留空 = 保持不变（S4）的请求形状三个凭据字段**总是发**（空串 = 后端保持旧值）——这是后端定义的语义（掩码回写防呆），⛔ 不要"优化"成「没改就不发这个键」：后端对**缺席**与**空串**是同一件事（都 = 保持），但"缺席"形状会让人在页面上找不到"我到底改了什么"。 */
import { request } from './http'

/** 配置 / 菜单端点的基路径（尾斜杠与 `apps/wechatmp/urls.py` 的字面路径一致）。 */
export const WECHATMP_CONFIG_URL = '/api/wechatmp/config/'
export const WECHATMP_MENU_URL = '/api/wechatmp/menu/'
export const WECHATMP_MENU_SYNC_URL = '/api/wechatmp/menu/sync/'

/** 本端点的节点门（D-0268 / H7：**复用** `system.pinduoduo_settings`，⛔ 不新增 RBAC 节点；第三方平台组现行口径，企微群码先例）。用途：岛页被**深链**（`page.html?page=system-wechatmp-settings`）打开时**自己判一次**可见性——路由那两道门在 iframe 里不生效（先例：`pages/open-apps.ts` / `pages/wecom-group-qr.ts`的同名自检）。⚠️ 与 `config/routePermissions.ts` 里本页那条**必须同值**（两份独立声明是**有意**的，由 `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对纠错）。 */
export const WECHATMP_SETTINGS_NODE = 'system.pinduoduo_settings'

/** 消息加密模式（逐字来自后端 `EncryptionMode`；⛔ 前端不镜像中文名）。 */
export type WechatMpMode = 'plain' | 'compatible' | 'safe'

/** 服务器配置信息卡（一键复制的三样 + 服务器 IP）。 */
export type WechatMpServerInfo = {
  callback_url: string
  token: string
  aes_key: string
  /** 部署侧没配 `MP_SERVER_PUBLIC_IP` ⇒ **空串**（页面显示"向部署方查询"占位）。 */
  server_ip: string
}

/** GET config 的载荷（appsecret **只有掩码**；`ready` = 启用 + 三件凭据齐）。 */
export type WechatMpConfigRow = {
  appid: string
  appsecret_masked: string
  token: string
  aes_key: string
  mode: WechatMpMode
  enabled: boolean
  template_id: string
  ready: boolean
  server: WechatMpServerInfo
}

/** PUT config 的载荷。三个凭据**空串 = 保持不变**（S4，后端语义）。 */
export type WechatMpConfigPayload = {
  appid: string
  appsecret: string
  token: string
  aes_key: string
  mode: WechatMpMode
  enabled: boolean
  template_id: string
}

/** 读配置（`ready=false` 是**常态**：新租户第一行默认行，不是错误）。 */
export function getWechatMpConfig(): Promise<WechatMpConfigRow> {
  return request<WechatMpConfigRow>({ method: 'get', url: WECHATMP_CONFIG_URL })
}

/** 保存配置（PUT）。
 * ⚠️ 失败信封（序列化器 `1000`：首次保存缺凭据 / safe 模式缺 AESKey / 43 位不对）
 * 由 `request()` 翻成 `BizError` ⇒ 页面把 `e.message` **如实显示**（⛔ 不美化）。 */
export function saveWechatMpConfig(payload: WechatMpConfigPayload): Promise<{ ready: boolean }> {
  return request<{ ready: boolean }>({ method: 'put', url: WECHATMP_CONFIG_URL, data: payload })
}

/** 同步预设菜单（写端点；微信回 errcode≠0 ⇒ 后端回业务码 `1000` + 那句人话）。 */
export function syncWechatMpMenu(): Promise<{ menu: unknown; wechat: unknown }> {
  return request<{ menu: unknown; wechat: unknown }>({ method: 'post', url: WECHATMP_MENU_SYNC_URL })
}

/** 看当前菜单（cgi-bin/menu/get 原文放进 `wechat`；没有 errcode 键 = 读端点成功）。 */
export function getWechatMpMenu(): Promise<{ wechat: unknown }> {
  return request<{ wechat: unknown }>({ method: 'get', url: WECHATMP_MENU_URL })
}

/** 删除菜单（写端点；同 S1 判据——失败信封由后端翻译成 `1000`）。 */
export function deleteWechatMpMenu(): Promise<{ wechat: unknown }> {
  return request<{ wechat: unknown }>({ method: 'delete', url: WECHATMP_MENU_URL })
}
