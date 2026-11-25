/** 【第三方平台 → 马上来查码对接】页（`pages/system-order-qr-fetch.ts`）的端点封装。契约的另一半是**实现即契约**：`src/apps/business/api/order_qr_config_views.py` +`src/apps/business/urls.py`（D-0348，）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/order-qr/config/` | 读配置（**api_key 只回掩码**，⛔ 原文不回显） || PATCH | `/api/business/order-qr/config/` | 保存；`api_key` **留空 = 不改**；**启用态基址必填**（缺失回业务码 1000，页面 toast 原句）；任何成功保存**清 401 闩** |## ⚠️ 留空 = 不改 的请求形状`api_key` **总是发**（空串 = 后端保持旧值）——后端定义的语义（与公众号设置 S4 同一口径），⛔ 不要"优化"成「没改就不发这个键」。 */
import { request } from './http'

/** 配置端点基路径（尾斜杠与 `business/urls.py` 的字面路径一致）。 */
export const ORDER_QR_CONFIG_URL = '/api/business/order-qr/config/'

/** 本端点的节点门（D-0348：**复用** `system.pinduoduo_settings`，⛔ 不新增 RBAC 节点；微信公众号设置先例）。用途：岛页被**深链**打开时自己判一次可见性（路由门在 iframe 里不生效——`pages/open-apps.ts` 同款自检）。⚠️ 与 `config/routePermissions.ts` 里本页那条**必须同值**（两份独立声明是**有意**的）。 */
export const ORDER_QR_FETCH_NODE = 'system.pinduoduo_settings'

/** GET config 的载荷（api_key **只有掩码**）。 */
export type OrderQrConfigRow = {
  configured: boolean
  enabled: boolean
  base_url: string
  api_key_masked: string
  /** 非 null = 密钥被对端拒绝过（401 闩），改密钥或保存任意配置后恢复。 */
  auth_failed_at: string | null
  updated_at: string | null
}

/** PATCH config 的载荷。`api_key` **空串 = 保持不变**（后端语义）。 */
export type OrderQrConfigPayload = {
  enabled?: boolean
  base_url?: string
  api_key?: string
}

/** 读配置（`configured=false` 是**常态**：没配过 = 功能零动作，不是错误）。 */
export function getOrderQrConfig(): Promise<OrderQrConfigRow> {
  return request<OrderQrConfigRow>({ method: 'get', url: ORDER_QR_CONFIG_URL })
}

/** 保存配置（成功响应即已生效；后端对无变更回业务码 1000）。 */
export function saveOrderQrConfig(payload: OrderQrConfigPayload): Promise<OrderQrConfigRow> {
  return request<OrderQrConfigRow>({
    method: 'patch',
    url: ORDER_QR_CONFIG_URL,
    data: payload,
  })
}
