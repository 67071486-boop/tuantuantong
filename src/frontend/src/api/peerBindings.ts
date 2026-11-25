// 绑定上家系统（上下游订单互通）的**三个页面端点**（spec §8.4 / §11）。
//
// | 方法 | 路径 | 用途 |
// |---|---|---|
// | GET    | `/api/business/peer-bindings/`     | 列表（`{items,total}`，**服务端不分页**） |
// | POST   | `/api/business/peer-bindings/`     | 绑定一个新上家（先与上家握手，成功才落库） |
// | GET    | `/api/business/peer-bindings/<pk>/` | 明细（编辑弹窗的回填出口；含大厅设置） |
// | PUT    | `/api/business/peer-bindings/<pk>/` | 更新大厅及游戏规则 |
// | DELETE | `/api/business/peer-bindings/<pk>/` | 解绑（**软删** + 通知上家） |
//
// ⚠️ **新建本文件**，不要把这三个封装塞进 `api/orders.ts`——那是并行线也在改的文件，
// 分文件是刻意的隔离（与 `api/orderSources.ts` / `api/auditLogs.ts` 同一处置）。
//
// 分层与 `api/orderSources.ts` 同形：传输、认证（CSRF）与信封解包全在 `request()`
// （`./http.ts`）；本文件**不 import 任何 UI 库**。
//
// ## 消费方与分工（与 `api/auditLogs.ts` 同形）
//
// - `pages/system-homesystemsetting.ts` 用 **`PEER_BINDINGS_URL` 常量**把 URL 交给
//   `layui.table` 的 `url`（spec §11.1 指定 `table.render({url, …})`），信封由页面的
//   `parseData` 一处解开；
// - `listPeerBindings` 是**同一端点的类型化出口**（统一信封 + 错误分类），与
//   `api/auditLogs.ts::fetchAuditLogs`、`api/performances.ts::fetchPerformances` 完全同型：
//   供测试与后续程序化调用方使用，**今天没有页面消费方**。
// - 两个写请求（`createPeerBinding` / `deletePeerBinding`）**只走本文件**：写请求要 CSRF
//   与统一错误分类，交给 layui 的通道会绕开它们。
import { request } from './http'
import type { UpstreamGameRules } from './upstreamBindings'

/** 列表 / 明细一行：**逐字**对齐后端 `PeerUpstreamBindingSerializer` 的 14 个字段（snake_case）。⚠️ `password` / `token_in_hash` / `token_out` **永不回显**（spec §9-S4）——载荷里只有布尔`password_set`（「要不要重填密码」）。本类型因此**不含**任何令牌或密码键。⚠️ `bound_by` 是**操作人的主键**（模型上是 `ForeignKey`，DRF 按 PK 序列化）；页面那列「绑定操作人」显示的是 `bound_by_name`（显示名，Task 8 复评修 1 之后才有）。两个键都保留：`bound_by` 是「销售提成归属人」的来源，别的消费方按 id 取。 */
export type PeerBindingRow = {
  id: number
  peer_name: string
  peer_base_url: string
  peer_account: string
  password_set: boolean
  bound_by: number
  bound_by_name: string
  bound_at: string
  last_ok_at: string | null
  last_error: string
  last_error_at: string | null
  consecutive_failures: number
  /** ⭐ 大厅镜像 P1：接收侧「同步上家抢单大厅」开关（模型默认 `false`）。
   * ⚠️ 老行读出来就是 `false`（两列非空带默认值）——⛔ 不是 `undefined` / `null`。 */
  hall_sync_enabled: boolean
  /** ⭐ 大厅价格百分比（1–100，模型默认 `100`）。展示价 = 上家大厅价 × 本值 / 100。 */
  hall_price_percent: number
  direct_taker_assignment_enabled?: boolean
  game_rules?: UpstreamGameRules
}

/** 编辑绑定的大厅设置和游戏规则，省略规则时沿用原配置。 */
export type PeerHallSettings = {
  hall_sync_enabled: boolean
  hall_price_percent: number
  direct_taker_assignment_enabled?: boolean
  game_rules?: UpstreamGameRules
}

/** 新增绑定的载荷（四个字段都是**必填**，与后端 `PeerBindCreateSerializer` 同集）。
 * ⚠️ `password` 是**必须发给后端**的握手用密码（`write_only`）；它只在这一条请求的 body 里
 * 出现，**不会**被任何列表 / 详情端点读回来。 */
export type PeerBindingPayload = {
  peer_name: string
  peer_base_url: string
  peer_account: string
  password: string
}

/** 集合端点（尾斜杠是本仓业务集合路径的统一口径）。 */
export const PEER_BINDINGS_URL = '/api/business/peer-bindings/'

/** 列表（`GET`）。后端回的是 `{items,total}`——**没有分页体**（`PeerBindingListView.get`
 * 直接 `api_response(data={"items": …, "total": …})`，不走 `EnvelopePagination`）⇒
 * 这里刻意**不**声明 `page`/`page_size`，也不带分页参数。 */
export function listPeerBindings(): Promise<{ items: PeerBindingRow[]; total: number }> {
  return request<{ items: PeerBindingRow[]; total: number }>({
    method: 'get',
    url: PEER_BINDINGS_URL,
  })
}

/** 新增绑定（`POST`）。后端会先拿这四个字段去与上家**握手**，成功才落库
 * （spec §8.4）⇒ 失败时抛的是**后端原样的中文**（`1000` 校验 / `1018` 拒绝 / `1021` 不可达），
 * 由 `api/errors.ts` 分类后经 `request()` 抛出，调用方**原样显示**即可。 */
export function createPeerBinding(payload: PeerBindingPayload): Promise<PeerBindingRow> {
  return request<PeerBindingRow>({
    method: 'post',
    url: PEER_BINDINGS_URL,
    data: payload,
  })
}

/** 明细（`GET <pk>/`）——编辑弹窗的回填出口（额度 / 大厅两格 / 四个只读字段）。
 * ⚠️ 回的是与列表行**同一个序列化器**（同一个 `PeerBindingRow` 形状），凭据仍是
 * `password_set` 布尔、密码本体连键都不出现。 */
export function getPeerBinding(id: number): Promise<PeerBindingRow> {
  return request<PeerBindingRow>({
    method: 'get',
    url: `${PEER_BINDINGS_URL}${id}/`,
  })
}

/** 保存已有绑定的大厅设置及游戏规则。 */
export function updatePeerBindingHallSettings(
  id: number,
  settings: Partial<PeerHallSettings>,
): Promise<PeerBindingRow> {
  return request<PeerBindingRow>({
    method: 'put',
    url: `${PEER_BINDINGS_URL}${id}/`,
    data: settings,
  })
}

/** 解绑（`DELETE`）。后端是**软删**（`deleted_at`）+ 尽力通知上家（通知失败**不影响**本地解绑，
 * 见 `PeerBindingDetailView.delete`）⇒ 页面只看这一发成功与否；失败（如 `1002` 不存在）
 * 会抛业务错误，调用方原样显示。 */
export function deletePeerBinding(id: number): Promise<null> {
  return request<null>({
    method: 'delete',
    url: `${PEER_BINDINGS_URL}${id}/`,
  })
}
