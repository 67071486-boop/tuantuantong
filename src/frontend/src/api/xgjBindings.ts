// 闲管家店铺对接的**七个**出口（per-source spec §5）。
//
// 分层与 `api/shopBindings.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里，
// 本文件不 import 任何 UI 库。
//
// ⚠️ **`app_secret` 只写不读**（spec §9-S1）：下面的 `XgjShopBindingRow` **没有**这个字段，
// 只有一个 `app_secret_set`（布尔）。这不是漏写——**不要**为了"方便核对"把它加回读模型：
// 后端根本不回它（序列化器 `write_only`），加了只会得到一个恒 `undefined` 的字段，
// 而下一个读者会以为"后端回了但没渲染"。
import { request } from './http'
import type { OrderSourceGroupChoice } from './orderSources'
import type { Page } from './unwrap'

/** 一行对接（字段照 spec §5 的模型 + 序列化器；接口一律 snake_case）。 */
export type XgjShopBindingRow = {
  id: number
  /** 租户自己起的名字（闲管家的店铺名要调 `user/authorize/list` 才知道）。 */
  shop_name: string
  /** 租户自己创建的闲管家应用的 AppKey（标识，不是密钥）。 */
  app_key: string
  /** **只回答「配没配」，不回答「配的是什么」**（后端 `app_secret` 是 `write_only`）。 */
  app_secret_set: boolean
  /** 订单消息推送URL：**服务端生成、只读**，且**接收端尚未实现**（spec §3-D5 / §12-1）。 */
  order_push_url: string
  /** 商品消息推送URL：同上。 */
  product_push_url: string
  remark: string
}

/** 一行**店铺**（一个应用名下的一家店）——对接页下方那张表的数据源。⚠️ 「一个订单来源 = 一家店」（per-source spec）：来源挂在**店**上，不再挂在应用上（`XgjShopBinding.order_source` 已拆掉），所以本行才有 `order_source`。⚠️ `subscription_warning` 是**订购状态**的提醒（`is_valid=false` 或订购已过期），**不是版本判定**——上游不返回版本名，标黄 ≠ 不是铂金版（per-source spec §3-D11）。 */
export type XgjShopRow = {
  id: number
  shop_name: string
  user_name: string
  /** 对应订单来源的 id（`null` = 还没建 / 被删了；下次同步或录单会重建）。 */
  order_source: number | null
  order_source_name: string
  is_valid: boolean | null
  /** 订购有效结束时间（**秒级时间戳**；`0` = 没有有效订购）。`null` = 还没同步过。 */
  valid_end_time: number | null
  subscription_warning: boolean
  binding: number
  binding_name: string
}

/** 一次店铺同步的计数（后端 `sync_shops` 的返回，per-source spec §5）。 */
export type XgjSyncResult = {
  total?: number
  created_shops?: number
  updated_shops?: number
  created_sources?: number
  error?: string
  code?: number
  skipped?: string
}

/** 保存响应上挂的 `sync` 键（R5：绑定 / 重存后**自动同步一次店铺**）。
 * ⚠️ 同步失败**不影响保存成功**（后端有意为之：凭据写错了要能改）：所以 `code === 0`
 * 也可能带 `sync.error`，页面必须把这件事**说出来**，否则租户会以为"店铺已经同步好了"。 */
export type XgjBindingWithSync = XgjShopBindingRow & { sync?: XgjSyncResult }

/** 新增 / 编辑的载荷。⚠️ **`app_secret` 省略或空串 = 不改**（后端 `update()`，spec §9-S4）：页面上的密码框留空表示"不动它"。**不要**在提交前把空串过滤掉又指望后端报错——两条路都错：后端把空串当"不改"（对），前端过滤掉只是等价写法，但**绝不能**让它变成"清空密钥"。⚠️ 表上**没有** `order_source` 了（per-source spec D3）：来源归店铺，不归应用。 */
export type XgjShopBindingPayload = {
  shop_name?: string
  app_key?: string
  app_secret?: string
  remark?: string
}

/** 「测试连接」回来的一行店铺（**上游原样**，只取我们要显示的字段，其余透传）。 */
export type XgjShop = {
  shop_name?: string
  user_name?: string
  user_nick?: string
  authorize_id?: number
  is_valid?: boolean
  is_pro?: boolean
  is_trial?: boolean
  [key: string]: unknown
}

/** 订单详情：**上游原样**（四十来个字段，中文标签在前端 `thirdparty-xgjshop.ts` 里）。 */
export type XgjOrderDetail = Record<string, unknown>

/** 列表的 `page_size` 取 **200** 条（后端上限现为 **300**：`EnvelopePagination.max_page_size`；本页未顶格）。
 * ⚠️ 不带就是默认 **20** ⇒ 列表**静默截断**（第 21 家店在表格里看不到，页面不报错）。
 * 与 `api/shopBindings.ts` 的 `PAGE_SIZE_ALL` 同一条残差（本页取 200、未顶格到 300；超过 200 条仍会截断）。 */
const PAGE_SIZE_ALL = 200

const XGJ_BINDINGS_URL = '/api/business/xgj-bindings/'
const XGJ_ORDER_DETAIL_URL = '/api/business/xgj/order-detail/'
const XGJ_SHOPS_URL = '/api/business/xgj-shops/'

/** 列表 = 本页表格的数据源。显式带 `page=1&page_size=200`，返回**整页**。 */
export async function fetchXgjBindings(): Promise<Page<XgjShopBindingRow>> {
  return request<Page<XgjShopBindingRow>>({
    method: 'get',
    url: XGJ_BINDINGS_URL,
    params: { page: 1, page_size: PAGE_SIZE_ALL },
  })
}

/** 新增。后端成功回 HTTP 200（`EnvelopeModelViewSet.finalize_response` 把 201 归一成 200）。
 * ⚠️ 返回体上多一个 `sync` 键（R5：**保存后自动同步一次店铺**）——调用方要把它说出来，
 * 详见 `XgjBindingWithSync`。 */
export async function createXgjBinding(
  payload: XgjShopBindingPayload,
): Promise<XgjBindingWithSync> {
  return request<XgjBindingWithSync>({ method: 'post', url: XGJ_BINDINGS_URL, data: payload })
}

/** 编辑（页面「修改」）。走 **PATCH**：`urls.py` 关掉了 PUT。同样带 `sync`。 */
export async function updateXgjBinding(
  id: number,
  payload: XgjShopBindingPayload,
): Promise<XgjBindingWithSync> {
  return request<XgjBindingWithSync>({
    method: 'patch',
    url: `${XGJ_BINDINGS_URL}${id}/`,
    data: payload,
  })
}

/** 删除（硬删；无反向引用）。 */
export async function deleteXgjBinding(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${XGJ_BINDINGS_URL}${id}/` })
}

/** 「测试连接」：拿**这一行**的凭据去查这个应用能操作哪些店。⚠️ 不传任何凭据：后端只从这一行取（spec §9-S3）——否则这条端点就是一条拿我们当跳板去打闲管家接口的通道。失败回业务码 `1015`（凭据不全）/ `1016`（AppID/签名/时间戳错）。 */
export async function testXgjConnection(id: number): Promise<{ shops: XgjShop[] }> {
  return request<{ shops: XgjShop[] }>({
    method: 'post',
    url: `${XGJ_BINDINGS_URL}${id}/test-connection/`,
  })
}

/** 查订单详情（页面「查询订单详情」）。失败回业务码 `1015`（这家店没配好）/ `1016`（凭据无效）/ `1017`（上游其它失败）。⚠️ 入参 `order_no` 必须是**纯数字**（后端序列化器挡非数字 ⇒ `1000`）；页面在提交前也要挡一次，免得白跑一趟拿 `1000`。 */
export async function queryXgjOrderDetail(
  binding: number,
  orderNo: string,
): Promise<{ detail: XgjOrderDetail }> {
  return request<{ detail: XgjOrderDetail }>({
    method: 'post',
    url: XGJ_ORDER_DETAIL_URL,
    data: { binding, order_no: orderNo },
  })
}

/** 「同步店铺」：拉一次这个应用下的店铺列表，幂等地落成本系统的店铺 + 订单来源（R5/R7）。失败回业务码 `1015`（凭据不全，`request()` 抛 `BizError`）/ `1016`（凭据无效）/ `1017`（其它）。⚠️ **本函数就是升级时要手动点的那一下**（per-source spec §7）：旧字段被拆掉之后，「店 ↔ 来源」要靠它（或重存那一行）重建。 */
export async function syncXgjShops(id: number): Promise<XgjSyncResult> {
  return request<XgjSyncResult>({
    method: 'post',
    url: `${XGJ_BINDINGS_URL}${id}/sync-shops/`,
  })
}

/** 这个应用下的**店铺**（含对应来源 id/名字 + 订购状态）——对接页下方那张表。
 * ⚠️ 两个表格是**两个请求**（应用表不带店铺：一个应用最多 30 家店，塞进一行渲染不出来）：
 * 页面按应用逐个取、拼成一张总表（`loadShops`）。 */
export async function fetchXgjShops(id: number): Promise<{ items: XgjShopRow[] }> {
  return request<{ items: XgjShopRow[] }>({
    method: 'get',
    url: `${XGJ_BINDINGS_URL}${id}/shops/`,
  })
}

/* ── ⭐ 跨应用店铺列表（「订单来源管理」页「店铺归属」区块）────────────

见计划 `docs/plans/2026-10-26-order-source-groups.md` §3.2 / §3.4。与上面按应用的
`fetchXgjShops(id)` **不是同一条路**（那条要 N 次请求才能拼出整张表）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/business/xgj-shops/?order_source=&source_group=&page=&page_size=200` | 全部店铺（含 `binding` 为空的孤儿店铺），可按来源 / 分组筛 |

⚠️ **没有节点门**：后端这个读端点只要求登录（与同模块写动作等价）⇒ 前端也不另判。
⚠️ `binding` / `order_source` 都**可空**（两者都是 `SET_NULL`）：孤儿店铺照样出现在列表里，
消费方要显示空、不伪装。

/** 「店铺归属」一行（后端 `XgjShopRowSerializer` 逐键；比按应用那份多 `group_id`/`group_name`）。 */
export type XgjShopsListRow = XgjShopRow & {
  /** 店铺**当前**所属来源的分组；`null` = 未分组或没有来源。 */
  group_id: number | null
  /** 分组名；空串 = 未分组 / 没有来源。 */
  group_name: string
  groups?: OrderSourceGroupChoice[]
}

/** 「店铺归属」子表的取数出口（唯一）。⚠️ **两个筛选都不传 = 不筛**（判据 `!== undefined`，E-0021）：页面「没选」送 `undefined`，⛔ 不送 `''`（空串会被后端判成非法整数 ⇒ `1000`）。⚠️ 显式 `page_size=200`：后端分页默认 20 ⇒ 第 21 家店静默消失（与 `PAGE_SIZE_ALL` 同一条残差）。 */
export async function fetchXgjShopsList(
  params: { order_source?: number; source_group?: number } = {},
): Promise<Page<XgjShopsListRow>> {
  const query: Record<string, number> = { page: 1, page_size: PAGE_SIZE_ALL }
  if (params.order_source !== undefined) query.order_source = params.order_source
  if (params.source_group !== undefined) query.source_group = params.source_group
  return request<Page<XgjShopsListRow>>({ method: 'get', url: XGJ_SHOPS_URL, params: query })
}
