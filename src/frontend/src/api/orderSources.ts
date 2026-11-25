// 订单来源字典的四个出口（spec §4 / §6.3）。
//
// ⚠️ **新建本文件**，不要把来源封装塞进 `api/orders.ts`——那是并行线（P4 / 录单页）也要改的文件
// （spec §5.2 的冲突面实测），分文件是刻意的隔离。
//
// 分层与 `api/config.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里，
// 本文件不 import 任何 UI 库。
import { request } from './http'
import type { Page } from './unwrap'

/** 一行的字段**照 spec §4 的字段清单**（接口一律 snake_case）。⚠️ **刻意不写「与后端 `OrderSourceSerializer` 逐字一致」**（T4 独立评审 Minor 1）：那是**当时无法对拍的绝对句**（E-0011 族）——T4 落地时后端序列化器还不存在，前端测试又把传输层整个 mock 掉，字段名一旦在 T2 里写歪，本文件的测试**仍会全绿**。真正的对拍由 **T2 的契约测试**负责（序列化器字段集合 == 本类型的七个键）。 */
export type OrderSourceGroupChoice = { id: number; name: string }

export type OrderSourceRow = {
  id: number
  name: string
  remark: string
  order_by: number
  shop_type: number
  /** 「公司店铺分组」的主键；`null` = 未分组（计划 `2026-10-26-order-source-groups.md`）。 */
  group: number | null
  /** 分组名；**空串 = 未分组**（后端 `SerializerMethodField`，不是 `null`）。 */
  group_name: string
  groups?: OrderSourceGroupChoice[]
  created_at: string
  updated_at: string
}

/** 新增 / 编辑的载荷（键名同 `OrderSourceRow` 的 snake_case）。`name` 必填（模型 `max_length=10`，R3）；其余三列后端都有默认值（`remark=""` / `order_by=0` / `shop_type=0`），所以可省。显式允许 `null`：调用方用 `null` 表示「这项没填」或「没选」，由 `toOrderSourcePayload` 统一丢掉——这样「没填」与「填了 0」不会混成同一件事（E-0021）。 */
export type OrderSourcePayload = {
  name: string
  remark?: string | null
  order_by?: number | null
  shop_type?: number | null
  /** 公司店铺分组：`null` = **改成未分组**（这是唯一一个允许透传 `null` 的键）。
   * ⛔ 不要用 `0` 表示未分组——后端的 `PrimaryKeyRelatedField` 会把 `0` 判成非法主键（`1000`）。 */
  group?: number | null
  group_ids?: number[]
}

/** 一行的字段照后端的 `OrderSourceGroupSerializer`（计划 §3.2）。⚠️ `source_count` 是**列表注解**：`GET` 的行里一定有它，而 `POST/PATCH` 的写响应**不带**这个键（单行实例上没有注解 ⇒ DRF 的 read_only 字段走 `SkipField`）。所以这里标成可选，消费方（删除确认文案）按 `typeof === 'number'` 判断「拿不拿得到」。 */
export type OrderSourceGroupRow = {
  id: number
  name: string
  order_by: number
  source_count?: number
  created_at: string
  updated_at: string
}

/** 新增 / 改名的载荷（`order_by` 与来源字典同口径：0 是合法值，E-0021）。 */
export type OrderSourceGroupPayload = {
  name: string
  order_by?: number | null
  source_ids?: number[]
}

/** 列表的 `page_size` 取 **200** 条（后端上限现为 **300**：`EnvelopePagination.max_page_size`，`api/base.py`；本页未顶格）。与 `config.ts:39` 的 `CONFIG_PAGE_SIZE` 是**同一处口径**，但各自声明、不跨文件 import：那个常量绑在「四类配置」上，来源字典不属于那四类。⚠️ 不带就是默认 **20**（`api/base.py:25`）——列表与下拉都会**静默截断**：第 21 条来源在页面上选不到，而页面不会报任何错。这是本线的评审 Critical 1，而列表是**管理页表格与录单页下拉共用的同一个端点**（spec §4 的 D2）。⚠️ **同义残差（I-2）**：来源超过 **200** 条时仍会被**本页截断**（本页取 200、未顶格到后端上限 300），第 201 条起在下拉与管理页都取不到。即：拿这条 `200` 换掉默认 `20` 只把窗口从 20 推到 200，**没有**消除「超出即静默消失」这一类。 */
const PAGE_SIZE_ALL = 200

const ORDER_SOURCES_URL = '/api/business/order-sources/'
const ORDER_SOURCE_GROUPS_URL = '/api/business/order-source-groups/'

/** 组装写载荷：只丢 `undefined` / `null`，**`0` 与 `''` 一律保留**（口径同 `config.ts:108` 的 `toListParams`）。两个方向都不能按「假值」判（E-0021）：- `order_by: 0` 是 R5 的默认值、也是合法排序值；`if (v)` 会把「传了 0」当成「没传」而静默丢掉排序；- `remark: ''` 是**编辑时清空来源描述**的唯一表达；丢掉它后端就收不到空串 ⇒「保存成功」但备注还在（静默失败，用户看得见结果不对）。抽成**导出的纯函数**是为了让「空值不发」这条规则能单独测，不必打桩传输层（与 `orders.ts` 的 `toOrderParams` 同一手法）。 */
export function toOrderSourcePayload(payload: OrderSourcePayload): OrderSourcePayload {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(payload)) {
    // ⚠️ **`null` 只对 `group` 透传**（计划 §5 的 B1）：
    //    「改成未分组」= `group: null`，丢掉它会让这次保存**静默失败**（页面显示保存成功、
    //    分组没变）。而其余三列（`remark` / `order_by` / `shop_type`）在后端
    //    `allow_null=False` ⇒ 送 `null` 会被判 `1000`，必须继续丢。
    //    ⛔ 不能笼统改成「只丢 `undefined`」——那会把 `remark: null` 一起送出去。
    if (value === undefined) continue
    if (value === null && key !== 'group') continue
    out[key] = value
  }
  return out as OrderSourcePayload
}

/** 列表 = **管理页的表格数据源** 与 **录单页的下拉字典**（同一个端点，spec §4 的 D2）。显式带 `page=1` + `page_size=200`：一次拿全（对标管理页是 `page:false`，没有分页控件）。返回的是**整页**（`{items,total,page,page_size}`）：只要下拉选项的调用方取 `.items`，需要 `total` 的调用方也能用——这与 `config.ts:83-89` 记的「下拉语义 vs 列表页语义」分工一致。 */
export async function fetchOrderSources(page = 1): Promise<Page<OrderSourceRow>> {
  return request<Page<OrderSourceRow>>({
    method: 'get',
    url: ORDER_SOURCES_URL,
    params: { page, page_size: PAGE_SIZE_ALL },
  })
}

/** 新增。后端成功回 HTTP 200（`EnvelopeModelViewSet.finalize_response` 把 201 归一成 200）。 */
export async function createOrderSource(payload: OrderSourcePayload): Promise<OrderSourceRow> {
  return request<OrderSourceRow>({
    method: 'post',
    url: ORDER_SOURCES_URL,
    data: toOrderSourcePayload(payload),
  })
}

/** 编辑（页面「修改」）。后端是 `ModelSerializer` 的部分更新 ⇒ PATCH 只发改动字段。 */
export async function updateOrderSource(
  id: number,
  payload: OrderSourcePayload,
): Promise<OrderSourceRow> {
  return request<OrderSourceRow>({
    method: 'patch',
    url: `${ORDER_SOURCES_URL}${id}/`,
    data: toOrderSourcePayload(payload),
  })
}

/** 删除。**允许**：后端是 `on_delete=SET_NULL`，历史订单保留来源名快照（R7 / D5）
 * ——所以这里不需要前端二次确认之外的额外保护，也不需要「软删除」。 */
export async function removeOrderSource(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${ORDER_SOURCES_URL}${id}/` })
}

/* ── 公司店铺分组（计划 §3.2 / §5）────────────────────────────── */

/** 分组字典：管理页弹层下拉 + 「店铺归属」区块的「订单分组」下拉 + 三个筛选页的候选
 * （同一个端点）。显式带 `page=1&page_size=200`——理由与 `fetchOrderSources` 逐字相同
 * （`EnvelopePagination` 默认 20 ⇒ 第 21 个分组静默消失）。 */
export async function fetchOrderSourceGroups(page = 1): Promise<Page<OrderSourceGroupRow>> {
  return request<Page<OrderSourceGroupRow>>({
    method: 'get',
    url: ORDER_SOURCE_GROUPS_URL,
    params: { page, page_size: PAGE_SIZE_ALL },
  })
}

async function fetchAllRows<T>(fetchPage: (page: number) => Promise<Page<T>>): Promise<T[]> {
  const first = await fetchPage(1)
  const pageCount = Math.ceil(first.total / first.page_size)
  const items = [...first.items]
  for (let page = 2; page <= pageCount; page++) {
    const next = await fetchPage(page)
    items.push(...next.items)
  }
  return items
}

export function fetchAllOrderSources(): Promise<OrderSourceRow[]> {
  return fetchAllRows(fetchOrderSources)
}

export function fetchAllOrderSourceGroups(): Promise<OrderSourceGroupRow[]> {
  return fetchAllRows(fetchOrderSourceGroups)
}

/** 组装分组写载荷：只丢 `undefined` / `null`（`order_by: 0` 必须留下，E-0021 同族）。 */
export function toOrderSourceGroupPayload(
  payload: OrderSourceGroupPayload,
): OrderSourceGroupPayload {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || value === null) continue
    out[key] = value
  }
  return out as OrderSourceGroupPayload
}

/** 新增分组。写响应**不带** `source_count`（见 `OrderSourceGroupRow` 的注释）。 */
export async function createOrderSourceGroup(
  payload: OrderSourceGroupPayload,
): Promise<OrderSourceGroupRow> {
  return request<OrderSourceGroupRow>({
    method: 'post',
    url: ORDER_SOURCE_GROUPS_URL,
    data: toOrderSourceGroupPayload(payload),
  })
}

/** 改名 / 改排序（PATCH，只发改动字段）。 */
export async function updateOrderSourceGroup(
  id: number,
  payload: OrderSourceGroupPayload,
): Promise<OrderSourceGroupRow> {
  return request<OrderSourceGroupRow>({
    method: 'patch',
    url: `${ORDER_SOURCE_GROUPS_URL}${id}/`,
    data: toOrderSourceGroupPayload(payload),
  })
}

/** 删除分组：**来源不删**，只变「未分组」（后端 `SET_NULL`，业务方拍板②）。 */
export async function removeOrderSourceGroup(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${ORDER_SOURCE_GROUPS_URL}${id}/` })
}
