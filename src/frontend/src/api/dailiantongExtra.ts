/** 代练通「有、我们没用上」那批动作里**两个运营读面**的前端封装（dl-D，）。| 方法 | 路径 | 用途 ||---|---|---|| `GET` | `/api/business/platform-integrations/stats/` | 代练通按日统计（代理上游 `LevelOrderStat`） || `GET`/`POST` | `/api/business/platform-integrations/order-templates/` | 代练通侧订单要求模板列表 / 新增 || `PATCH`/`DELETE` | `/api/business/platform-integrations/order-templates/<id>/` | 改 / 删模板 || `POST` | `/api/business/platform-integrations/order-templates/<id>/default/` | 设默认模板 |契约的另一半是**实现即契约**：`src/apps/business/api/dailiantong_extra_views.py` 与
 * `src/apps/business/dailiantong_extra_actions.py`。## ⚠️ 「订单要求模板」是**代练通侧**的，不是我们自己的「代练要求模板」这里的模板是上游 `OrderRequireTemp*`（在代练通后台建、发单到代练通时用）；本仓自己的「代练要求模板」（发单页那份全局模板 / `PlatformPushSettings`）**是两回事**，⛔ 不要互相引用、也不要在页面文案里混着叫。## 权限：⛔ 不新增 RBAC 节点，两个端点都沿用 `order.list`后端 `DailiantongStatsView` / `DailiantongOrderTemplate*View` 的 `permission_classes`就是 `has_node("order.list")`（它们只服务订单页的运营视图）。这里的常量供两页做**iframe 深链自检**（路由 `meta.permissions` 是同一道门在 Vue 侧的那一半）。
 * 节点码写在本模块（而不是 `pages/*.ts`）是既有约定：`tests/test_rbac_menu_alignment.py`只扫 `pages/*.ts` 里的带点字面量，节点码放 api 模块不会被它当按钮门计数。 */
import { request } from './http'

/** 本批两个读面共用的 RBAC 节点（= 后端 `EXTRA_ACTION_NODE`）。 */
export const DAILIANTONG_EXTRA_NODE = 'order.list'

/* ── 按日统计 ─────────────────────────────────────────────────────────── */

/** 统计端点。⚠️ 尾斜杠是契约的一部分。 */
export const DAILIANTONG_STATS_URL = '/api/business/platform-integrations/stats/'

/** 统计方向（上游 `IsPub`）：1 发单 / 0 接单。 */
export const STATS_SIDE_PUBLISH = 1
export const STATS_SIDE_ACCEPT = 0
export type StatsSide = typeof STATS_SIDE_PUBLISH | typeof STATS_SIDE_ACCEPT

/** 上游 `LevelOrderStat` 的一行。⚠️ 形状**由上游决定**（`docs/references/dailiantong/open-api-v3.4.md`「订单统计」一节列出`QDate` / `PubCount` / `PubBal` / … / `SettleRate` 十三个键，但真实回包可能多、可能少）⇒ 前端**不在这里编一套字段类型**，按实际返回的键渲染（见页面的 `columnsFor`）。 */
export type DailiantongStatRow = Record<string, unknown>

/** `GET …/stats/` 的 `data`（后端 `_normalise_stat_payload` 的归一化结果）。 */
export type DailiantongStats = {
  items: DailiantongStatRow[]
  /** 上游 `LevelOrderStatTotal`；缺失时后端给 `{}`（⛔ 不是 `null`）。 */
  total: DailiantongStatRow
  /** 上游 `RecordCount`；缺失时是 `null`（⛔ 不拿 0 冒充「查过了」）。 */
  record_count: number | null
  /** 后端补齐后的实际窗口（`YYYY-MM-DD`）——页面用它确认「查的是哪一段」。 */
  start: string
  end: string
  is_pub: number
}

/** `GET …/stats/` 的查询参数（后端 `StatsQuerySerializer`）。 */
export type DailiantongStatsQuery = {
  start: string
  end: string
  is_pub: StatsSide
}

/** 取代练通按日统计。
 * ⚠️ 只发 `start` / `end` / `is_pub` 三个键：后端对缺省值有自己的口径（最近 7 天），
 * 页面把窗口**显式摆进日期框**再发，⛔ 不靠后端默认值（否则「框里显示的和实际查的对不上」）。 */
export function fetchDailiantongStats(query: DailiantongStatsQuery): Promise<DailiantongStats> {
  return request<DailiantongStats>({
    url: DAILIANTONG_STATS_URL,
    method: 'GET',
    params: {
      start: query.start,
      end: query.end,
      is_pub: query.is_pub,
    },
  })
}

/* ── 代练通侧订单要求模板（⚠️ 不是本仓的「代练要求模板」）────────────── */

/** 模板集合端点。 */
export const DAILIANTONG_ORDER_TEMPLATES_URL =
  '/api/business/platform-integrations/order-templates/'

/** 模板行（后端 `_template_rows` 的四个可用字段）。 */
export type OrderTemplateRow = {
  /** 上游 `ID`；解析不出时后端给 `null`（页面在这一档不画写按钮）。 */
  id: number | null
  title: string
  temp: string
  is_default: boolean
}

/** `GET …/order-templates/` 的 `data`。 */
export type OrderTemplateList = {
  items: OrderTemplateRow[]
  /** 上游 `RecordCount`；缺失时是 `null`（⛔ 不拿 0 冒充「查过了、没有模板」）。 */
  record_count: number | null
}

/** 新增 / 修改模板的载荷（三个键就是后端序列化器收的全部）。 */
export type OrderTemplateInput = {
  title: string
  temp: string
  is_default: boolean
}

/** 单条模板的路径（改 / 删共用；⛔ 页面不自己拼）。 */
export function orderTemplateDetailUrl(id: number): string {
  return `${DAILIANTONG_ORDER_TEMPLATES_URL}${id}/`
}

/** 设默认模板的路径。 */
export function orderTemplateDefaultUrl(id: number): string {
  return `${DAILIANTONG_ORDER_TEMPLATES_URL}${id}/default/`
}

/** 读模板列表。 */
export function listDailiantongOrderTemplates(): Promise<OrderTemplateList> {
  return request<OrderTemplateList>({
    url: DAILIANTONG_ORDER_TEMPLATES_URL,
    method: 'GET',
  })
}

/** 新增模板（后端回创建后的模板对象）。 */
export function createDailiantongOrderTemplate(
  payload: OrderTemplateInput,
): Promise<OrderTemplateRow> {
  return request<OrderTemplateRow>({
    url: DAILIANTONG_ORDER_TEMPLATES_URL,
    method: 'POST',
    data: { ...payload },
  })
}

/** 修改模板（`PATCH`；页面每次都发完整三项——后端三项都是可选，全给 = 明确覆盖）。 */
export function updateDailiantongOrderTemplate(
  id: number,
  payload: OrderTemplateInput,
): Promise<OrderTemplateRow> {
  return request<OrderTemplateRow>({
    url: orderTemplateDetailUrl(id),
    method: 'PATCH',
    data: { ...payload },
  })
}

/** 删除模板（后端回 `{id, deleted: true}`）。 */
export function deleteDailiantongOrderTemplate(
  id: number,
): Promise<{ id: number; deleted: boolean }> {
  return request<{ id: number; deleted: boolean }>({
    url: orderTemplateDetailUrl(id),
    method: 'DELETE',
  })
}

/** 设默认模板（后端回回读到的模板对象）。 */
export function setDefaultDailiantongOrderTemplate(id: number): Promise<OrderTemplateRow> {
  return request<OrderTemplateRow>({
    url: orderTemplateDefaultUrl(id),
    method: 'POST',
  })
}
