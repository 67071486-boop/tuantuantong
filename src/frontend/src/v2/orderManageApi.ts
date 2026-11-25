/** 「管理订单」v2 的**本地薄封装**（`src/v2/**` 内，⛔ 不改 `api/orders.ts`）。
 *
 * 为什么要有这个文件：`GET /api/business/orders/` 的 `data` 在分页四键之外还带
 * `summary`（后端对**当前筛选全集**的聚合：笔数 / 总发单价 / 总接单价 / 差额 / 八状态计数），
 * 工具栏统计句吃它。共享出口 `fetchOrders()` 的返回类型是 `Page<OrderListItem>`，
 * 没有声明 `summary` ⇒ 读不到那个键。本封装用同一份参数组装（`toOrderParams`）与
 * 同一个信封出口（`request`）把 `summary` 带出来——参数口径只有 `toOrderParams`
 * 一处实现（E-0021 族），这里不重写第二份判据。
 */
import { request } from '@/api/http'
import { toOrderParams, type OrderListItem, type OrderSearchParams } from '@/api/orders'

/** 列表响应里的统计块（后端 `order_views.py` 的 summary 注解；金额 = 两位小数字符串，
 * 允许为负；笔数/状态按筛选集，金额含权限内关联罚款单；`status_counts` 键 = 状态码）。与 island 页 `OrderListSummary` 同形——
 * 那份定义在 island 模块里，⛔ 不 import（会把 layui 装载链拉进 v2 模块图）。 */
export type OrderListSummary = {
  count: number
  total_hall_price: string
  total_price: string
  diff: string
  /** 利润口径（2026-11-05 同款）：总退款额 / 已退单单自己的「接单价−发单价」合计。 */
  total_refund_fee: string
  refunded_diff: string
  status_counts?: Partial<Record<string, number>>
}

/** 管理订单列表的一页：分页四键（与 `Page<T>` 同形）+ `summary`。 */
export type OrderManagePage = {
  items: OrderListItem[]
  total: number
  page: number
  page_size: number
  summary: OrderListSummary | null
  page_relations?: {
    split_members_by_id: Record<string, OrderListItem[]>
    penalty_children_by_parent: Record<string, OrderListItem[]>
  }
}

/** 取订单列表一页（带统计句）。参数 = `OrderSearchParams` 原样，空值丢弃规则照旧。 */
export async function fetchOrdersWithSummary(
  params: OrderSearchParams & { include_page_relations?: boolean },
): Promise<OrderManagePage> {
  return request<OrderManagePage>({
    method: 'get',
    url: '/api/business/orders/',
    params: toOrderParams(params),
  })
}

/** 行内「一键发单到交易平台」的入口探针（`GET /platform-integrations/push-availability/`）。
 * ⛔ fail-closed：失败 / 非信封 / `available !== true` 一律按「不可用」——按钮不画（同 island 页口径）。 */
export async function fetchPushAvailability(): Promise<boolean> {
  try {
    const data = await request<{ available?: boolean }>({
      method: 'get',
      url: '/api/business/platform-integrations/push-availability/',
    })
    return data.available === true
  } catch {
    return false
  }
}
