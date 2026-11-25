// 客户（会员）线（2026-11-10；第二/三批增强同日；第五/六批 2026-11-11）的**读出口**：
// 会员列表 + 两个下拉的候选字典 + 客户订单抽屉（系统单 + 来源单单一合并列表）。
//
// 分层与 `api/tbSendLogs.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里——
// 本文件不另解一层信封（`request<T>()` 回的就是 `data`）。
//
// ⚠️ 后端契约（`apps/business/api/customer_views.py`，2026-11-10 客户线；第六批 2026-11-11）：
// `total` = **客户表全量数**（不随筛选变，业务方拍板「直接全算」）；筛选打在**系统订单**上，
// 行 = **全部建档客户**（第五批扩容：没连过系统单的收割客户也在列），行的电话 / 店铺 /
// 录单人取**筛选范围内最近一单**的值（零单客户 = 空串/0.00）；分页翻页用的行集总数
// 单独放 `filtered_total`。订单数 / 累计消费 = 系统单 + 有效来源单**合并**统计
// （有效口径 = 排除已退款/已关闭 + 已在库系统单同号者——同单两面只算一张）。
// 第二批：排序 `order_by` 两档一律降序（默认订单数）；电话包含匹配（icase）；
// 录单人出 `recorder_display`（显示名，映射不到回落原账号）。
import { request } from './http'
import type { Page } from './unwrap'

/** 会员列表 / 筛选字典 / 订单抽屉的公共前缀（挂在 `/api/business/customers/` 下）。 */
export const CUSTOMER_MEMBERS_PATH = '/api/business/customers/members/'
export const CUSTOMER_MEMBER_FILTERS_PATH = '/api/business/customers/member-filters/'
export const CUSTOMER_MEMBER_ORDERS_PATH = '/api/business/customers/member-orders/'

/** 排序档位（与后端 `customers.MEMBER_ORDER_FIELDS` 同源的两档；一律降序）。 */
export type CustomerOrderBy = 'order_count' | 'total_amount'

/** 一行会员的**载荷形状**（前端侧契约副本；键名 = 后端 `_serialize_member` 的键）。 */
export type CustomerMemberRow = {
  /** 闲鱼买家唯一标识（闲管家 `buyer_eid`；身份键，⛔ 不按昵称去重）。 */
  eid: string
  /** 买家昵称最新快照（可为空串）。 */
  buyer_nick: string
  /** 电话 = 筛选范围内最近一单的 `customer_phone`（可能是空串）。 */
  phone: string
  /** 筛选范围内的订单数。 */
  order_count: number
  /** 筛选范围内最近一单的下单时刻（ISO 串）。 */
  last_order_at: string
  /** 累计消费 = 筛选范围内 `total_price` 之和，**两位小数字符串**（前端不算钱；负数合法）。 */
  total_amount: string
  /** 最近一单挂的来源字典 id（可空）。 */
  order_source: number | null
  /** 最近一单的来源显示名（字典当前名，回落快照）。 */
  source_name: string
  /** 最近一单的录单人（`Order.created_by` 登录账号快照；筛选入参仍收它）。 */
  recorder: string
  /** 录单人的**显示名**（`accounts/display.py` 同一出处；映射不到回落 `recorder`）。 */
  recorder_display: string
}

/** 会员列表载荷：`total` = 客户表全量；`filtered_total` = 筛选后行数（翻页按它算）。 */
export type CustomerMembersPage = Page<CustomerMemberRow> & {
  filtered_total: number
  recorder_counts: Array<{ recorder: string; name: string; count: number }>
}

/** 筛选下拉的候选字典。 */
export type CustomerMemberFilters = {
  sources: Array<{ id: number; name: string }>
  /** 录单人 = `[{value: 登录账号, label: 显示名}]`（下拉显示 label、提交 value）。 */
  recorders: Array<{ value: string; label: string }>
}

/** 取数参数：空串 / `undefined` = 不筛（后端缺键即不筛）。 */
export type CustomerMembersParams = {
  source?: number
  recorder?: string
  phone?: string
  order_by?: CustomerOrderBy
  page?: number
  page_size?: number
}

export async function fetchCustomerMembers(
  params: CustomerMembersParams,
): Promise<CustomerMembersPage> {
  return request<CustomerMembersPage>({
    method: 'get',
    url: CUSTOMER_MEMBERS_PATH,
    params,
  })
}

export async function fetchCustomerMemberFilters(): Promise<CustomerMemberFilters> {
  return request<CustomerMemberFilters>({
    method: 'get',
    url: CUSTOMER_MEMBER_FILTERS_PATH,
  })
}

/** 抽屉统一行 = 该客户一张订单（系统单或有效来源单）的只读快照
 * （键名 = 后端 `_serialize_drawer_system_order` / `_serialize_drawer_source_order`）。
 * 状态显示：系统单 = 前端按 `V2_ORDER_STATUS_LABELS` 映射 `status`（2026-11-03
 * 「待抢单→待接单」只落 v2；映射不到兜底后端 `status_text` = 共享 label）；
 * 来源单 = 后端 `status_text`（闲管家码表中文，未知兜底「未知」）。 */
export type CustomerOrderRow = {
  /** 系统单 = 系统单号（可点开详情）；来源单 = 来源单号（不可点）。 */
  no: string
  /** true = 系统单（有游戏/项目/录单人、可点详情）；false = 闲管家来源单（只展示）。 */
  is_system: boolean
  /** 状态枚举键：系统单 = `OrderStatus` 枚举值；来源单 = 闲管家状态码数值（不可比）。 */
  status: string | number
  /** 游戏名（来源单无 ⇒ 空串）。 */
  game: string
  /** 项目内容快照（来源单无 ⇒ 空串）。 */
  project: string
  /** 订单金额 = **两位小数字符串**原样（前端不算钱，D-0015 负数合法）。 */
  amount: string
  /** 时间（ISO 串）：系统单 = 下单时刻、来源单 = 来源侧更新时刻（可空串）。 */
  time: string
  /** 状态中文：系统单 = 后端共享 label（兜底）；来源单 = 闲管家码表中文。 */
  status_text: string
  /** 店铺（系统单 = 来源字典显示名/快照，来源单 = 来源店铺名；可空串）。 */
  shop: string
  /** 录单人**显示名**（映射不到回落登录账号；来源单恒空串）。 */
  recorder: string
}

/** 抽屉头部信息卡（第七批 §1.1；键名 = 后端 `customer_info` 出参）。
 * 统计 = 系统单 + 有效来源单**合并**口径（与会员行同源、不随外层筛选变）；
 * `eid_masked` = 打掩码的身份键（完整值不出参）；金额是后端两位小数字符串原样。 */
export type CustomerInfoCard = {
  eid_masked: string
  buyer_nick: string
  /** 该客户最近一张系统单的电话（可能空串）。 */
  phone: string
  /** 累计消费（合并口径），两位小数字符串（前端不算钱）。 */
  total_amount: string
  order_count: number
  /** 最近下单（合并口径，ISO 串；零单 = 空串）。 */
  last_order_at: string
  /** 建档时间（ISO 串）。 */
  created_at: string
}

/** 抽屉载荷：`total` = 该客户合并列表行数（系统单全量 + 有效来源单，两侧各截断 2,000 条）。 */
export type CustomerOrdersPage = Page<CustomerOrderRow> & { customer_info: CustomerInfoCard }

export async function fetchCustomerMemberOrders(params: {
  eid: string
  page?: number
  page_size?: number
}): Promise<CustomerOrdersPage> {
  return request<CustomerOrdersPage>({
    method: 'get',
    url: CUSTOMER_MEMBER_ORDERS_PATH,
    params,
  })
}

/* ── 客户分析报表（2026-11-10 第三批）────────────────────────────────────── */

/** 分析报表端点（后端 `CustomerAnalysisView`，一次性聚合；纯读）。 */
export const CUSTOMER_ANALYSIS_PATH = '/api/business/customers/analysis/'

/** 时间范围三档（与后端 `customers.ANALYSIS_RANGES` 同源；缺省 = 30d）。 */
export type CustomerAnalysisRange = '30d' | '90d' | '12m'

/** KPI 槽（键 = 后端 kpi 的键）。`period_orders` 只作复核用，页面不展示。 */
export type CustomerAnalysisKpi = {
  /** 客户总数 = 客户表**全量**（恒不随 range 变）。 */
  customer_total: number
  /** 复购客户数 = 范围内 ≥2 单的客户数。 */
  repeat_customers: number
  /** 本期消费合计 = 范围内 `total_price` 之和，两位小数字符串（负数 = 退款冲减，原样）。 */
  period_amount: string
  /** 本期订单数（avg_order 的分母，复核键）。 */
  period_orders: number
  /** 客单价 = 本期消费 ÷ 本期订单数，两位小数字符串。 */
  avg_order: string
}

/** 来源分布一行（扇形数据；`order_source` = 字典 id，未挂字典 = null）。 */
export type CustomerSourceDistRow = {
  order_source: number | null
  /** 字典当前名或快照（空归「未指定」，后端已归好）。 */
  name: string
  /** 该来源本期消费合计，两位小数字符串（负行 = 冲减，扇形侧自滤）。 */
  amount: string
}

/** 消费分层一行（全时段累计画像，不随 range 变；后端是四档唯一出处）。 */
export type CustomerConsumeTierRow = { label: string; count: number }

/** 客户数扇形一行（第七批 §1.3）：各店铺**有订单的客户数**（店內去重、系统单按来源
 * 显示名 / 来源单按 shop_name 同名合并；跟随 range）。 */
export type CustomerSourceCustomerDistRow = { name: string; count: number }

/** 客户增长一点 = 某月按**首单时间**新增的客户数（固定近 12 月，不随 range 变）。 */
export type CustomerGrowthPoint = { month: string; count: number }

/** 分析报表载荷（后端一次返回五块；第七批 +`source_customer_dist`）。 */
export type CustomerAnalysis = {
  range: CustomerAnalysisRange
  kpi: CustomerAnalysisKpi
  source_dist: CustomerSourceDistRow[]
  source_customer_dist: CustomerSourceCustomerDistRow[]
  consume_tiers: CustomerConsumeTierRow[]
  growth: CustomerGrowthPoint[]
}

export async function fetchCustomerAnalysis(params: {
  range: CustomerAnalysisRange
}): Promise<CustomerAnalysis> {
  return request<CustomerAnalysis>({
    method: 'get',
    url: CUSTOMER_ANALYSIS_PATH,
    params,
  })
}
