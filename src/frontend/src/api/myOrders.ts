/** 「我的订单」（代练员侧）的 API 封装。**本线独占模块**（`impl-brief-wire.md` §0.1：一个线一个 API 模块文件），不要把这些调用塞进 `api/orders.ts`。两个端点（都是后端**既有**的，本模块只补前端调用）：| 端点 | 权限 | 后端出处 ||---|---|---|| `GET /api/business/my-orders/` | 认证 + 节点 `my_orders.send_to_my_system`（类级门，由 `MyOrdersViewSet.MY_ORDERS_PERMISSIONS` 声明） | `apps/business/api/my_orders_views.py` 的 `MyOrdersViewSet.get_permissions` || `POST /api/business/orders/<order_no>/submit/` | 认证 + 节点 `order.submit_completion`（`OrderViewSet.ORDER_PERMISSIONS["submit"]`）**且必须是该单接单人**（同方法的接单人判据） | `apps/business/api/order_views.py` 的 `OrderViewSet.submit` |⭐ D-0365追加的两个端点（同一颗节点门，⛔ 不新增 RBAC 节点）：`POST /api/business/my-orders/settle-requests/`（批量申请提前结算，`applyForSettlement`）与 `GET /api/business/my-orders/export`（收入记录 xlsx，`downloadMyOrderExport`）；后端出处同文件（`settle_requests` 动作 / `MyOrdersExportView`）。## `submit/` 的真实业务规则（**读码得到，不是推断**）
 * `order_views.py` 的 `OrderViewSet.submit` 依次判两件（顺序即优先级）：1. **不是这张单的接单人**（`main_performance_of(order).user_id != request.user.id`）⇒ **HTTP 403**+ msg「该单不是你接的，不能提交」。⚠️ 这一条**在来源态判据之前**：先确认「这是你的单」。⚠️ 2026-09-23 单轴化之前这里**还有一道 `Role.TAKER` 职位门**（msg「只有代练员可以提交完单」），它已随旧角色轴**删除**——今天「谁能到这个端点」只由节点 `order.submit_completion` 回答（无该节点 ⇒ 权限层的 403），职位不再是判据。2. **来源态不是 `IN_PROGRESS`** ⇒ **HTTP 200 + `code=1002`**、msg「只有代练中的单可以提交，当前状态：X」。这条是**业务码**（不是 403），前端必须按业务失败提示，不能当成功。
 * 另外金额**不可计算**时（提成模式不是订单价百分比 / 接单价两边都没有）在**任何写入之前**返回`code=1000/1002` 的业务失败（`commission.py` / `performance_amount.py`），msg 直接可显示。⇒ 前端**不自己复刻这些判据**（复刻必然漂移，且 `order_views.py` 是唯一判据）：页面把请求发出去、把后端的 `msg` 原样显示（见 `order-myorderlist.ts` 的 `errMsg`）。 */
import { AxiosError, type AxiosResponse } from 'axios'
import { http, request } from './http'
import { mapHttpStatus, NetworkError } from './errors'
import { isOrderNoShape, type Order } from './orders'
import type { Page } from './unwrap'
// 导出「同一个 URL 两种形状」的分流判据与文件名解析**复用绩效导出的同一份实现**
// （`api/orders.ts::downloadOrderExport` 复用 `exportFilename` 是同一先例；
// ⛔ 不在本模块各写一份——两份判据必然漂移）。
import { envelopeFromBlob, exportFilename, isJsonBlob } from './performances'
import { unwrap } from './unwrap'

/** 单次取数的条数。本页取 **200** 条（后端 `EnvelopePagination.max_page_size` 现为 **300**，`api/base.py`）——
 * 传更大的值会被 DRF 静默截到上限（`PageNumberPagination.get_page_size` 的 `min(size, max)`），
 * 于是「一次取完」的假设会静默失效、分页条数算错。 */
export const MY_ORDERS_PAGE_SIZE = 200

/** `MyOrderSerializer`（`my_orders_views.py` 的 `Meta.fields`）的对外字段。⚠️ 三个「不是本表字段」的来源：`order_no` / `status` / `game_name` / `project_text` 都是`source="order.xxx"` 的**跨表读**——`status` 是**订单**状态，不是绩效的 `review_status`（后者是审核口径，客服侧从 `/performances/` 看）。⚠️ 金额字段（`temp_amount` / `bonus_amount`）DRF 的 `DecimalField` 序列化成**字符串**（如 `"0.00"`），不要当 number 用。 */
export type MyOrderItem = {
  /** 绩效行主键。**不要**拿它当订单标识——订单一律用 `order_no`（详情/动作的 URL 里都是它）。 */
  id: number
  order_no: string
  /** ⭐ **来源单号**（D-0301）：展示口径 =「来源号非空取它、空回退 `order_no`」
   * （回退渲染在消费方做；载荷给两个原值）。`Order.source_order_no` 是 `blank=True`
   * 的非 NULL 文本列 ⇒ 没填就是空串。 */
  source_order_no: string
  /** 订单状态（`OrderStatus` 的 8 个取值之一），不是审核状态。 */
  status: string
  game_name: string
  game_role_name?: string
  project_text: string
  /** 订单是否加急（跨表读 `order.is_urgent`，后端 `Order.is_urgent` 是 `default=False` 的布尔
   * ⇒ 恒有值，不是 `null`）。「指派给我的订单」第 1 列的加急徽标按它渲染。 */
  is_urgent: boolean
  temp_amount: string
  bonus_amount: string
  /** 审核状态；`null` = 还没有可审的东西（后端 `Performance.review_status` 可为空）。 */
  review_status: string | null
  created_at: string
  /** 接单人用户名（跨表读 `user.username`）。`null` = 该绩效行还没有归属人（`Performance.user` 是 `null=True` 的 FK；`MyOrderSerializer` 显式写了`allow_null=True`，所以空 user 的行回的是 `null` 而不是**丢键**）。⚠️ 三个字段（本字段 + `emp_type` + `settle_status`）是「我的收入」页（`layui-island/pages/emp-myemp.ts`）要的，由后端 `MyOrderSerializer` 提供。**恒在载荷里**（`Meta.fields` 显式列出），所以这里是**必填**：`username` 可以为 `null`（空 user），`emp_type` / `settle_status` 是两个 choices 的字符串。 */
  username: string | null
  /** 「接手人」的**显示名**（计划 §1）：后端 `MyOrderSerializer.taker_name`，取值口径是 `apps/accounts/display.py::user_display_name`（**昵称优先、空串回落登录账号**）。⚠️ `null` = 这一行还没有归属人（`Performance.user` 是 `null=True` 的 FK）⇒ 「指派给我的订单」第 12 列渲染成**空串**（⛔ 不写「—」这类自造占位，也⛔ 不拿登录账号冒充）。 */
  taker_name: string | null
  /** 收入类型：后端 `EmpType` 的 `打单工资` / `销售提成`（`Meta.fields` 显式列出，恒有值）。 */
  emp_type: string
  /** 结算状态：后端 `SettleStatus` 的 `未结账` / `已结账`（`Meta.fields` 显式列出，恒有值）。 */
  settle_status: string
  /** ⭐ 该行**自己的**提前结算申请时刻（ `MyOrderSerializer` 就回这个键；D-0365 起「是否已申请结算」列与「已申请」小标记按它渲染）。`null` = 没有申请、或**申请已作废（`cancelled`）**（作废之后可重申，显示「已申请」会是假话——与后端 `get_settle_requested_at` 的口径一致）。ISO 串（项目时区 `+08:00` 偏移），展示用 `formatTime` 同式截取。 */
  settle_requested_at: string | null
  /** 「接手价」（计划 §1）：后端 `MyOrderSerializer.taker_price`，**覆盖价优先**（`Performance.taker_price`），否则回落**大厅接单价**（`Order.hall_price`）。⚠️ DRF 的金额出口把它序列化成**两位小数字符串**（如 `"88.00"`），⛔ 不要当 number 用。⚠️ `null` = 覆盖价与大厅接单价**都没有**（不是 `0`：`0.00` 是合法金额，会原样回来）。 */
  taker_price: string | null
}

/** `/my-orders/` 认的查询参数：分页两条 + ⭐ D-0365 起的日期三参数。 */
export type MyOrderPageParams = {
  page?: number
  page_size?: number
  /** ⭐ D-0365：日期筛选三参数（与后端 `MyOrdersIncomeSerializer` 同名同契约）。`date_type` 四档 = `claimed` 接手 / `submitted` 交单 / `created` 录单 / `settled` 结算（列映射在后端 `MY_ORDER_DATE_FIELDS`，前端只发档名）。⚠️ **`date_type` 缺省时日期两端不生效**（后端不猜默认列）⇒ 只给日期不给档时前端就不发日期参数（成对约定）。 */
  date_type?: 'claimed' | 'submitted' | 'created' | 'settled'
  /** **日期**（`YYYY-MM-DD`，不是 datetime）：后端 `DateField`，两端都含。 */
  date_from?: string
  date_to?: string
}

/** 组装查询参数：分页判据是「传没传」而不是「真值」（`AGENTS.md` §5 的 E-0021 同族）——`page: 0` 这种越界值要**原样发出去**让后端明确拒绝（回业务码 1002），不能在参数层被 `if (v)` 静默丢掉、变成「翻到第 1 页」。⭐ D-0365：日期三参数按「空 = 没传」折（空串 / `undefined` 不发，其余原样发）。 */
export function toMyOrderParams(
  params: MyOrderPageParams,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  // `date_type` 的类型是四档联合（没有空串形态）⇒ 只判「传没传」；
  // 「空档」在调用方（页面）就被折成「不发」（与后端「缺档不筛」成对）。
  if (params.date_type !== undefined) out.date_type = params.date_type
  if (params.date_from !== undefined && params.date_from !== '') {
    out.date_from = params.date_from
  }
  if (params.date_to !== undefined && params.date_to !== '') {
    out.date_to = params.date_to
  }
  return out
}

/** 我接的单（一页）。 */
export async function fetchMyOrders(
  params: MyOrderPageParams = {},
): Promise<Page<MyOrderItem>> {
  return request<Page<MyOrderItem>>({
    method: 'get',
    url: '/api/business/my-orders/',
    params: toMyOrderParams(params),
  })
}

/** 一次取完我接的单。**为什么要有它**：页面把整份列表交给 `layui.table` 做**客户端分页**（`TABLE.page` 的`limit:20` + `limits`）——这样「取数」与「错误提示」都走**同一个** `request()` 出口（403 的 msg 能被原样显示），不必让 layui 自己发 XHR（那条路绕开 `api/` 的错误分类，403 会变成「表格空了、什么都不说」）。代价是分页在客户端做，见页面模块的说明。`while (items.length < total)` 每轮都换 `page`，因此在正常后端上必然收敛；`MAX_PAGES` 是防御**后端忽略 `page` 参数**的那类失控（真的撞上时**响亮报错**，而不是静默截断成「看起来只有这么多单」）。 */
const MAX_PAGES = 100

/** ⭐ D-0365：`fetchAllMyOrders` 的可选参数（日期三参数；既有调用点零影响——不传 = 不筛）。 */
export type FetchAllMyOrdersParams = Pick<
  MyOrderPageParams,
  'date_type' | 'date_from' | 'date_to'
>

export async function fetchAllMyOrders(
  params: FetchAllMyOrdersParams = {},
): Promise<MyOrderItem[]> {
  const first = await fetchMyOrders({ page: 1, page_size: MY_ORDERS_PAGE_SIZE, ...params })
  const items = [...first.items]
  let page = 1
  while (items.length < (first.total ?? items.length)) {
    page += 1
    if (page > MAX_PAGES) {
      throw new Error(
        `订单数量超过单次可加载上限（${MAX_PAGES * MY_ORDERS_PAGE_SIZE} 条），请稍后重试`,
      )
    }
    const next = await fetchMyOrders({ page, page_size: MY_ORDERS_PAGE_SIZE, ...params })
    // 防御「后端忽略 page」：拿到空页就停，不空转到 MAX_PAGES。
    if (next.items.length === 0) break
    items.push(...next.items)
  }
  return items
}

/** 提交完单。返回**流转后**的完整订单（后端 `OrderSerializer`），其中 `status` 已是`PENDING_REVIEW`、`allowed_transitions` 已按新状态重算——调用方**不要**再发一次详情。⚠️ **单号形状必须在发请求之前判**：本端点是 router 生成的 `orders/{order_no}/submit/`，吃 `OrderViewSet.lookup_value_regex = r"\d{20}"`（`order_views.py:173`）⇒ 形状不对时 Django路由匹配不上，回的是 **404 + HTML** 而不是统一信封（末轮实测：`POST /api/business/orders/abc/submit/` ⇒ `404` + `text/html`）——`request()` 只能给一句状态码兜底文案，调用方读不到「单号写错了」。⚠️ **这套判据的完整口径**（哪些端点走宽松转换器、哪些走这条 404 路线）见
 * `api/orders.ts` 的 `isOrderNoShape` docstring——**别**在这里重述「逐字相同」那种引用。 */
export async function submitMyOrder(orderNo: string): Promise<Order> {
  if (!isOrderNoShape(orderNo)) {
    throw new Error(`订单号格式不正确（应为 20 位数字）：${orderNo}`)
  }
  // 后端 `submit` 动作**不读请求体**（`request.data` 全程未被使用）：订单号在 URL 里，
  // 所以这里**刻意不带 `data`**——多送一个空对象只会让「参数是什么」这件事变得含糊。
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/submit/`,
  })
}

export const ISSUE_REASON_MAX_LENGTH = 200
export const ISSUE_REASON_REQUIRED_TEXT = '请输入问题原因'

/** 打手报问题单时必填原因，返回流转后的订单。 */
export async function reportIssueOrder(orderNo: string, reason: string): Promise<Order> {
  if (!isOrderNoShape(orderNo)) {
    throw new Error(`订单号格式不正确（应为 20 位数字）：${orderNo}`)
  }
  const trimmed = reason.trim()
  if (trimmed === '') throw new Error(ISSUE_REASON_REQUIRED_TEXT)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/report-issue/`,
    data: { reason: trimmed },
  })
}

/** 打手**受限**订单详情（booster-h5）：`GET /api/business/my-orders/<order_no>/detail/`（后端 `MyOrdersViewSet.order_detail`，`my_orders_views.py`；门 = 类级节点`my_orders.send_to_my_system` + 「存在 `Performance(user=me, emp_type=打单工资)`」）。## 载荷 = `MyOrderDetailSerializer` 的**恰好** 16 键（⛔ 不多给）键集的权威判例是 `src/tests/test_my_order_detail_api.py::DETAIL_KEYS`；**刻意不含**：客户联系方式（R1 裁定不给）、其它金额（`total_price` / `hall_price` 等）、审核 / 结算字段、审计日志。⚠️ 各键的序列化口径（与列表 `MyOrderItem` 同源的部分不再重复）：
 * - `status` 是**订单**状态（`OrderStatus` 的 8 取值），不是绩效的 `review_status`；- `claimed_at` = **绩效行的 `created_at`**（「接手时间」与列表同轴），DRF 按项目时区序列化 ⇒ **`+08:00` 偏移的 ISO 串**（spec §6.2 已更正注记；显示换算用`m/utils/datetime.ts::formatDateTime`，按串内偏移、⛔ 不切字符）；- `temp_amount` / `server_name`：`DecimalField` ⇒ **字符串**；区服可空 ⇒ `server_name` 可 `null`；- `group_qr_url`：该单 `img_type=1`（群聊二维码）最新一张已认领行的**短时签名 URL**；无 ⇒ `null`（⛔ 绝不回 `img_type=0` 的进度截图——那是 M2 截图线的事）。⚠️ **单号形状不在本层判**（与上面 `submitMyOrder` 刻意相反）：detail 走的是**宽松**
 * 共享转换器 `<order_no:order_no>`（只挡控制字符），畸形但干净的单号会进视图、落**统一信封**的业务失败（HTTP 200 + `1002`「记录不存在或已被删除」），`request()` 能把 msg 原样交给调用方——没有 `submitMyOrder` 那种「路由正则不匹配 → HTML 404」的不可读出口。20 位形状闸在手机端路由 + `m/utils/orderNoShape.ts`（进入页面即验、零请求）。失败语义（都由后端判，前端不复刻）：非接单人 / 无节点 ⇒ **403**（`HttpError`）；单不存在 / 软删 ⇒ **200 + `BizError(1002)`**。 */
export type MyOrderDetail = {
  order_no: string
  /** ⭐ 来源单号（D-0301，）：展示回退取「`source_order_no || order_no`」。
   * `blank=True` 的非 NULL 文本列 ⇒ 没填就是空串。 */
  source_order_no: string
  /** 订单状态（`OrderStatus` 的 8 个取值之一），不是绩效审核态。 */
  status: string
  game_name: string
  /** `Order.server` 可空 ⇒ 无区服的单是 `null`（页面显示「—」）。 */
  server_name: string | null
  project_text: string
  content: string
  demand: string
  time_requirement: string
  game_account: string
  game_sub_account: string
  game_role_name: string
  is_urgent: boolean
  remark: string
  /** 绩效行 `created_at`（接手时间），`+08:00` 偏移的 ISO 串（见上）。 */
  claimed_at: string
  /** DecimalField ⇒ 字符串（如 `"88.00"`），不要当 number 用。 */
  temp_amount: string
  /** 群聊二维码的短时签名 URL；该单没有已认领群码 ⇒ `null`（页面显示「暂无群码」）。 */
  group_qr_url: string | null
}

export async function fetchMyOrderDetail(orderNo: string): Promise<MyOrderDetail> {
  return request<MyOrderDetail>({
    method: 'get',
    url: `/api/business/my-orders/${orderNo}/detail/`,
  })
}

/* ── ⭐ D-0365：收入页的批量申请结算与导出 ────────────────────── */

/** 批量申请结算端点（后端 `MyOrdersViewSet.settle_requests`；**带尾斜杠**，与同族一致）。 */
export const MY_ORDERS_SETTLE_REQUESTS_URL = '/api/business/my-orders/settle-requests/'

/** 导出端点（后端 `MyOrdersExportView`；**没有尾斜杠**，照 `performances/export` 先例）。 */
export const MY_ORDERS_EXPORT_URL = '/api/business/my-orders/export'

/** 批量申请的**逐条**结果（成功条再附 `settle_request` 载荷，形状与 App 端同源）。 */
export type SettleRequestItem =
  | {
      performance_id: number
      ok: true
      code: number
      msg: string
      settle_request: {
        id: number
        performance_id: number
        order_no: string
        state: string
        bonus_amount: string
        created_at: string
        settled_at: string | null
      }
    }
  | {
      performance_id: number
      ok: false
      code: number
      msg: string
      /** 失败条**没有** `settle_request` 键（申请没建成，⛔ 不给空壳假装成功）。 */
      settle_request?: undefined
    }

/** 批量申请的出参：逐条结果 + **当场重算**的额度（`used` 已含本批成功条）。 */
export type SettleRequestsResult = {
  items: SettleRequestItem[]
  early_settlement: { used: number; limit: number }
}

/** 批量申请提前结算（`POST /api/business/my-orders/settle-requests/`，D-0365）。语义 = **已有的「提前结算申请」**（App 端 2026-10-23 落地的那条判据链）：逐条过归属 → 已完成 → 未结账 → 无生效申请 → 额度，**单条失败不中断整批**——逐条结果在`items[]` 里（`ok` / `code`（1035/1036 原样）/ `msg`（后端中文原文）），调用方照实汇总展示、⛔ 不自己预判（后端是唯一判据）。空数组**不要调**：后端`allow_empty=False` ⇒ `[]` 回业务码 `1000`（页面在勾 0 行时自己提示）。 */
export async function applyForSettlement(
  performanceIds: number[],
): Promise<SettleRequestsResult> {
  return request<SettleRequestsResult>({
    method: 'post',
    url: MY_ORDERS_SETTLE_REQUESTS_URL,
    data: { performance_ids: performanceIds },
  })
}

/** 导出成功时的字节与浏览器要用的文件名（形状与 `PerformanceExport` 同源）。 */
export type MyOrdersExport = { blob: Blob; filename: string }

/** 下载收入记录导出（`GET /api/business/my-orders/export`，D-0365）。**为什么照 `downloadPerformanceExport` 的形状**（⛔ 不用 `window.open`）：同一个 URL**两种形状**——成功是 `.xlsx` 文件字节、失败（参数非法 / 超上限）是**统一信封 +HTTP 200**；`window.open` 拿不到响应体 ⇒「请先收窄筛选条件」这条可操作的提示到不了用户眼前。这里先请求再落 Blob：信封 ⇒ `unwrap` 抛带后端 `msg` 的 `BizError`；xlsx ⇒ 落 Blob 交给调用方触发下载。**代价（与绩效导出同两条，如实登记）**：① 文件先进内存（后端自封顶 10000 行）；② 这条路径**绕过了 `request()` 的 401 处理器**——会话过期时用户看到中文提示但不会被自动踢回登录页（`downloadPerformanceExport` 的注释里有同一段登记）。
 * `query` 与列表**同源**：键集 = `MyOrderPageParams` 的日期三条 + 页面 5 个客户端筛选的服务端镜像（`game` / `settle_status` / `order_status` / `key1` / `key2`——后端`MyOrdersExportSerializer` 的同名列，页面打包时自己保证「筛了什么就发什么」）。 */
export async function downloadMyOrderExport(
  query: Record<string, string>,
): Promise<MyOrdersExport> {
  let response: AxiosResponse<Blob>
  try {
    response = await http.get<Blob>(MY_ORDERS_EXPORT_URL, {
      params: query,
      responseType: 'blob',
    })
  } catch (e) {
    // HTTP 非 2xx：DRF 的 401/403 也是信封，5xx 是 HTML ⇒ msg 取不到就交给兜底文案
    // （与 `downloadPerformanceExport` 同一条判据，不另写一套）。
    if (e instanceof AxiosError && e.response) {
      const envelope = await envelopeFromBlob(e.response.data as Blob)
      throw mapHttpStatus(e.response.status, envelope?.msg ?? '')
    }
    if (e instanceof AxiosError) {
      throw new NetworkError(e.code === 'ECONNABORTED' ? '请求超时' : '网络异常，请检查连接')
    }
    throw e
  }
  const blob = response.data
  if (isJsonBlob(blob)) {
    // HTTP 200 + 信封 = 后端**预期**的业务失败（参数非法 1000 / 超上限 1000）⇒ 抛 BizError。
    const envelope = await envelopeFromBlob(blob)
    if (envelope) {
      unwrap(envelope)
      throw new NetworkError('导出端点返回了空的成功信封、没有文件内容')
    }
    throw new NetworkError('导出返回的形状无法识别（既不是 JSON 信封、也不是 xlsx）')
  }
  return { blob, filename: exportFilename(response.headers?.['content-disposition'], 'my-orders.xlsx') }
}
