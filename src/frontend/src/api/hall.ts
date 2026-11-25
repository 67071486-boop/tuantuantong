/** 【内部接单大厅】的 API 出口（P3a 后端自 main 起就在，前端此前一条都没接）。为什么**新建**一个文件而不是塞进 `orders.ts`：1. 大厅的读端点不在订单路由上（`/api/business/hall/`，`business/urls.py:39` 显式注册，不是 `OrderViewSet` 的 `@action`——同一件事两个入口是被 spec §5 否决的写法）；2. `orders.ts` 另有前端线在改，同文件并行编辑必冲突。⚠️ **不做任何 403 特判**：已认证但无权限在后端是 **HTTP 403 + 统一信封**（`core/exceptions.py:66-67`），`./http.ts` 的 `request()` 会把它归成`HttpError(403, 后端msg)`——这是项目**现有**的统一分支，页面直接显示 `e.message` 即可。在这里 catch 403 再换成自己的文案，正好会破坏那条分支（测试会红，见`__tests__/hall-transport.spec.ts`）。 */
import { request, requestEnvelope } from './http'
import { isOrderNoShape, type Order, type OrderListItem } from './orders'
import type { Page } from './unwrap'

/** 行的来源：`native` = 我方原生订单；`remote` = 鸡蛋铺大厅单的**镜像行**（P2 租户级线）；`upstream` = **上家鸡蛋铺**（客服个人绑定）大厅单的镜像行；`simulated` = **模拟单**（D-0296，租户开关 `hall_simulated_orders` 生成的演示卡，不建订单行）；⭐ `peer` = **上家团团通**的大厅镜像行（D-0303，2026-10-25；后端投影在`apps/business/peer_hall.py::serialize_peer_row`，行键与另几路**恰好同键**）。⚠️ 五路共用**同一个**行类型（后端同键，由 `tests/test_jidanpu_hall_merge.py` /`tests/test_peer_hall_*` 等合并网钉住）。 */
export type HallOrigin = 'native' | 'remote' | 'upstream' | 'simulated' | 'peer'

/** 大厅列表项 = 订单列表字段 + **生效接单价** + **三源合并的 8 个加法键**（spec §4.6）。⚠️ `hall_price` 取的是**生效接单价**（绩效覆盖价优先，空才用订单默认价，spec §6.3 / Ruling 44），不是 `Order.hall_price` 的字面值；对外 key 仍是 `hall_price`（后端 `HallListSerializer` 的契约）。它**可以为 null**（抛单时没带价 ⇒ 大厅里就是一张「没有接单价」的单，抢单会被 `1013` 拒）。⚠️ 镜像行上 `order_no` 是**空串**（它不是我方订单）、`server` 是 `null`（远端区服是**文本**）⇒ 渲染两源行时一律按 `origin` 分支，⛔ 不要假设 `order_no` 一定有值。⛔ 新增键一律是**加法**：原生行的既有键与取值一个都没改（老前端不读新键 = 行为不变）。 */
export type HallListItem = OrderListItem & {
  hall_price: string | null
  origin: HallOrigin
  source_system: string
  remote_order_id: string
  remote_order_view: string
  remote_game_area: string
  remote_game_server: string
  remote_hours_desc: string
  /** ⭐ **镜像行的主键**（两条镜像线各用自己的那张表）。五路**都有这个键**（「恰好同键」契约），取值：原生行 / P2 镜像行恒 `null`；本线（鸡蛋铺 `JidanpuUpstreamHallOrder`）镜像行 = 它自己的主键；⭐ 团团通上家（`PeerHallMirror`，`origin === 'peer'`）镜像行 = **它自己的主键**。⚠️ **为什么必须有**：两条镜像线的接单端点都是按**本表主键**取行——本线是 `POST /api/business/hall/upstream/<int:pk>/claim/`、⭐ 团团通上家是 `POST /api/business/hall/peer/<int:pk>/claim/`（`mirror_pk` 就是那个 `<pk>`）。没有它那两颗「接手此单」**结构上点不动**。⛔ 别拿 `remote_order_id` 去顶：那是**上家单号**，不是本表主键，端点会打不中行。
   * ⚠️ **fail-closed**：`origin` 是 `upstream` / `peer` 而它是 `null` / 非正整数时，页面**不画**那颗按钮（⛔ 不拿空值拼 URL 发请求）。 */
  mirror_pk: number | null
  /** ⭐ D-0296四键：优选 / 灰卡 / 离厅原因 / 模拟单主键。- `is_preferred`：优选单（模拟单恒 `true`）⇒ 卡片左上角「优选」角标；- `is_exited`：灰卡（已被抢 / 已撤下的历史单）⇒ 整卡置灰 + 「已被抢」水印，⛔ 灰卡**不渲染**抢单按钮（fail-closed），整卡仍可点开详情浮层；- `exit_kind`：`claimed` / `withdrawn` / `null`（在厅行）——只作留痕，不进卡片 UI；- `sim_pk`：模拟单主键（其余来源恒 `null`）；接单走 `claimSimulatedHallOrder(sim_pk)`。⚠️ 模拟单是**假单**：接单端点**永远**回业务码 `1012`「此单已被人接手，请刷新」。⛔ 全部是**加法键**：老前端不读它们 = 行为一字不变。 */
  is_preferred: boolean
  is_exited: boolean
  exit_kind: 'claimed' | 'withdrawn' | null
  sim_pk: number | null
}

export type HallPage = Page<HallListItem>

export type HallClaimSource = 'local' | 'upstream' | 'peer'

export function fetchHallClaimTarget(source: HallClaimSource, ref: string): Promise<HallListItem & { game_name?: string }> {
  return request({ method: 'get', url: '/api/business/hall-claim-target/', params: { source, ref } })
}

/** 大厅**只认这 8 个**筛选参数（后端 `HallSearchSerializer` 声明的字段集 + D-0296 加的两个查询参数；传其它参数会被 DRF **静默忽略**）。`page` / `page_size` 是分页参数（`EnvelopePagination`：默认 20、上限 200）。⚠️ `include_exited` / `preferred_only` 后端**只认 `1`**（D-0296：不传 = 旧行为），所以类型收成 `number`、由调用方传 `1`——⛔ 不要传 `true`（axios 会序列化成`include_exited=true`，后端契约没承诺认它）。 */
export type HallSearchParams = {
  game?: number | null
  server?: number | null
  project_text?: string
  keyword?: string
  price_min?: string | number | null
  price_max?: string | number | null
  page?: number
  page_size?: number
  /** 返回里含灰区（D-0296：灰区已按离厅时间倒序排在可接区后面，前端按返回顺序渲染）。 */
  include_exited?: number
  /** 只返回优选单（D-0296）。 */
  preferred_only?: number
}

const EMPTY_KEYS: Set<unknown> = new Set(['', undefined, null])

/** 组装大厅检索参数：**空值不发送**（空串 / `undefined` / `null` 去掉）。⚠️ **判据必须是「空」而不是「假值」**（E-0021）：`price_min: 0`、`price_max: 0` 在 D-0015 下是**有意义的价格边界**（钱可以为 0 与负数）。用 `if (value)` 过滤会把「传了 0」当成「没传」、静默丢掉筛选条件——本项目已经踩过一次同型的坑，别改回真值判断。抽成纯函数（而不是把对象直接交给 axios 的 `params`）的理由与 `orders.ts` 的 `toOrderParams` 相同：「空值不发」这条规则可以单独测，且不赌第三方对 `undefined` 的处理方式。 */
export function toHallParams(params: HallSearchParams): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(params)) {
    if (EMPTY_KEYS.has(value)) continue
    if (typeof value === 'string' && value.trim() === '') continue
    out[key] = value as string | number
  }
  return out
}

/** 订单号**形状**校验：20 位纯数字（后端 `lookup_value_regex = r"\d{20}"`）。 */
function assertHallOrderNo(orderNo: string): void {
  if (!isOrderNoShape(orderNo)) {
    throw new Error(`订单号格式不正确（应为 20 位数字）：${orderNo}`)
  }
}

/** 大厅列表。分页载荷与订单列表同形（`{items,total,page,page_size}`）。 */
export async function fetchHall(params: HallSearchParams = {}): Promise<HallPage> {
  return request<HallPage>({
    method: 'get',
    url: '/api/business/hall/',
    params: toHallParams(params),
  })
}

/** 抢单。**只给代练员**：非代练员后端回 HTTP 403（msg 由后端给，前端原样显示）。 */
export async function claimOrder(orderNo: string): Promise<Order> {
  return request<Order>(claimOrderRequestConfig(orderNo))
}

/** 抢单请求配置（两个出口共用；⛔ 不两处拼 URL）。 */
function claimOrderRequestConfig(orderNo: string) {
  assertHallOrderNo(orderNo)
  return { method: 'post' as const, url: `/api/business/hall/${orderNo}/claim/` }
}

/** ⭐ 与 `claimOrder` 同一条请求，但把后端成功信封的 **`msg`** 一并带出。
 * 与指派同因：抢单成功后后端可能自动撤回该单挂着的代练通订单；撤不掉时仍是成功码，
 * 只在 `msg` 里带警告 ⇒ 页面必须显示 `msg` 原文，⛔ 不能只弹一句固定的「抢单成功」。 */
export async function claimOrderWithMessage(
  orderNo: string,
): Promise<{ order: Order; message: string }> {
  const res = await requestEnvelope<Order>(claimOrderRequestConfig(orderNo))
  return { order: res.data, message: res.msg }
}

/** 接一张**模拟单**（D-0296 T4）：`POST /api/business/hall/sim/<sim_pk>/claim/`。⚠️ **产品语义：这个端点永远失败**——即使这张模拟卡还亮着、你是第一个点的，后端也回**业务码 1012**「此单已被人接手，请刷新」（模拟卡不建订单行，接不走；D-0296 #4）。调用方（`m/pages/HallPage.vue::confirmClaim`）按 1012 走「toast 后端 msg + 重取已加载列表」，⛔ 不要给这条路写成功分支——它不存在。⚠️ 主键**形状先行拒绝**（与 `claimUpstreamMirror` 同一条纪律，E-0021 同族）：`0` / 负数 / 非整数 / NaN 都不是合法主键，别让它们变成一条注定打不中的请求。 */
export async function claimSimulatedHallOrder(simPk: number): Promise<Order> {
  if (!Number.isInteger(simPk) || simPk <= 0) {
    throw new Error(`模拟单主键不合法（应为正整数）：${String(simPk)}`)
  }
  return request<Order>({
    method: 'post',
    url: `/api/business/hall/sim/${simPk}/claim/`,
  })
}

/** ⭐ 接一张**团团通上家**的镜像单（D-0303；spec `docs/specs/2026-10-25-peer-hall-mirror.md` §4.1）：`POST /api/business/hall/peer/<mirror_pk>/claim/`。⚠️ **路径参数是镜像行主键 `mirror_pk`**（后端 `PeerHallClaimView.post(request, pk)` →`peer_hall.claim_peer_mirror(mirror_id=pk)`，`business/urls.py` 的 `hall/peer/<int:pk>/claim/`）。⛔ **不是**上游单号：`remote_order_id` / `remote_order_view` 都是上家的单号，拿去打这条端点会按主键解析失败（404）——两条鸡蛋铺线与本线在这一点上口径一致。⚠️ **与 `claimUpstreamMirror` 同族但不同源**：那条是鸡蛋铺线（同步上游 HTTP、当场转正）；
 * 本条是**确认制**——本地预占（`listed → claiming`）+ 排 `hall.claim` 事件就返回，正式订单要等上家 `order.assign` 推回来。所以**成功也不等于接到单**（响应只是「受理」）。## 结局用**业务码**判（⛔ 不要只看 HTTP 状态）端点回的是统一信封，业务失败一律是 HTTP 200 + 非 0 `code`（`api/http.ts` 抛 `BizError`）：- `0`    = 受理成功（`data` 里带 `status=claiming` / `status_text`）⇒ 页面说「正在受理中」；- `1012` = `ORDER_ALREADY_TAKEN`：被别的打手接走了 / 本人已在受理中 ⇒ 页面**刷新列表**；- `1015` = `PLATFORM_NOT_CONFIGURED`：上家绑定停用 / 解绑（领域层的 `NOT_CONFIGURED_MESSAGE`）；- 其余（`GRAB_LIMIT_EXCEEDED` / 押金 / 实名 / 权限 403 …）⇒ 页面**原样显示后端 `msg`**。
 * ⚠️ 主键**形状先行拒绝**（与 `claimUpstreamMirror` / `claimSimulatedHallOrder` 同一条纪律，E-0021 同族）：`0` / 负数 / 非整数 / `NaN` 都不是合法主键，一次请求都不发——页面渲染期也有同一道数据门（`claimTargetOf`），两处判据指向同一个「正整数」事实。 */
export type PeerHallClaimResult = {
  /** 镜像行主键（= 请求路径里那个 `mirror_pk`，回包回显一次便于对账）。 */
  mirror_pk: number
  /** 上家单号（展示用；⛔ 不是接单参数）。 */
  peer_order_no: string
  /** 行状态（受理成功时是 `claiming`，人话在 `status_text`）。 */
  status: string
  /** 状态的人话（后端 `PeerHallMirror.Status` 的 label）。 */
  status_text: string
}

/** 团团通镜像单**受理成功**时的页面提示（业务方口径：「正在受理中」，⛔ 不说「抢单成功」）。
 * 为什么不是「抢单成功」：这一跳只是本地预占 + 事件已排队，正式订单还没落到我方
 * （确认制，见上面的 docstring）。桌面 / H5 两处提示**共用这一个出处**。 */
export const PEER_CLAIM_ACCEPTED_TEXT = '正在受理中'

/** 业务码 `1012` = 后端 `apps/core/error_codes.py::ORDER_ALREADY_TAKEN`（被接走 / 正在处理中）。⚠️ `config/errorCodes.ts` 的镜像**没有登记它**（那份镜像只收「前端真的有分支」的码，且已有先例在用处写数字 + 注明出处，见 `order-addorder.ts` 的 `1015`）⇒ 这里写字面量并带上后端常量名，⛔ 不要顺手往镜像里加（会与后端码表出现第二个出处）。 */
export const PEER_CLAIM_TAKEN_CODE = 1012

export async function claimPeerMirror(mirrorPk: number): Promise<PeerHallClaimResult> {
  if (!Number.isInteger(mirrorPk) || mirrorPk <= 0) {
    throw new Error(`镜像行主键不合法（应为正整数）：${String(mirrorPk)}`)
  }
  return request<PeerHallClaimResult>({
    method: 'post',
    url: `/api/business/hall/peer/${mirrorPk}/claim/`,
  })
}

/** 抛单到内部接单大厅（`UNASSIGNED → WAITING_GRAB`，客服）。`price` **可不传**——不传时后端沿用订单已有的默认接单价（`ThrowToHallSerializer.price`是 `required=False`）。带了价需要 `business.override_taker_price` 权限，否则后端回 HTTP 403。⚠️ **`price: '0'` 必须发出去**（0 是合法金额，D-0015）：只有 `undefined`/`null`/空串才不发。⚠️ **不送 `actor`**：后端 `throw_to_hall` 的 actor 取自 `request.user`（读码 `:372`），载荷里没有这个字段。 */
export async function throwToHall(orderNo: string, price?: string | null): Promise<Order> {
  assertHallOrderNo(orderNo)
  const data: Record<string, string> = {}
  if (price !== undefined && price !== null && String(price).trim() !== '') {
    data.price = String(price)
  }
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/throw-to-hall/`,
    data,
  })
}

/** 撤回抛单。**只接受 `WAITING_GRAB`**（`order_views.withdraw:391` 的入口判据）——
 * 其它状态回 **HTTP 200 + `code 1002`**，msg 里带当前状态；走 `BizError`，页面原样显示。 */
export async function withdrawThrow(orderNo: string): Promise<Order> {
  assertHallOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/withdraw/`,
  })
}
