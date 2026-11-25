// 上家鸡蛋铺绑定的出口（写 / 删 / 测试连接 / 立即同步）+ 大厅第三路的接单出口。
// 页面在「系统维护 → 绑定上家系统设置」，表单收在弹窗里（D-0185 / D-0189）。
//
// `getUpstreamBinding` / `testUpstreamConnection` / `syncUpstreamNow` 今天没有页面按钮，
// 后端端点和 `api/__tests__/jidanpuUpstream.spec.ts` 仍覆盖它们。D-0189 是页面上不给按钮，
// 不是把端点删掉。
// 传输、认证与信封解包全在 `request()`（`./http.ts`）里，本文件不 import 任何 UI 库。
//
// 今天和 `jidanpuMember.ts` 各管一张表，端点路径刻意不同名，不要合成一个文件：
//   - `jidanpuMember.ts` → 代练员个人绑的下家（`JidanpuMemberBinding`）
//   - 本文件 → 客服绑的上家（`JidanpuUpstreamBinding`，D-0256 起是多行）
// 租户级那条线（`jidanpuSync.ts` / `JidanpuBinding`）已经删掉，别再找那个文件。
//
// `password` 只写不读：读模型只有 `password_set`。后端把它声明成 `write_only`，
// 响应里连这个键都没有；加回读字段只会得到恒 `undefined`。
import { request, requestEnvelope } from './http'
import { type Order } from './orders'
import type { UpstreamGameRules } from './upstreamBindings'

/** 一行上家绑定（`JidanpuUpstreamBinding`，多行资源；本类型服务任意行的 GET/POST/PUT）。⚠️ 两个轮询间隔（`hall_poll_seconds` / `my_orders_poll_seconds`）是**只读展示**：页面**没有**这两个输入框（业务方要的字段清单里没有它们，与 `jidanpuMember` 同口径），所以 `JidanpuUpstreamBindingPayload` 里也不声明——让「页面上没有的东西」在类型上就发不出去。 */
export type JidanpuUpstreamBindingRow = {
  id: number
  /** 上家鸡蛋铺站点地址（尾斜杠已在后端归一化掉）。 */
  jidanpu_base_url: string
  /** 上家那边的登录名（= 打手账号，不是姓名）。 */
  account: string
  /** 上家账号显示名缓存（引擎还没跑过时为空串）。 */
  account_name: string
  /** ⭐ 人填的**上家系统名称**（选填；没起名是空串）——同一租户绑多家上家
   * 鸡蛋铺时用来区分。⚠️ 与 `account_name`（同步引擎缓存的只读显示名）是两回事。 */
  name: string
  /** **只回答「配没配」，不回答「配的是什么」**（后端 `password` 是 `write_only`）。
   * ⛔ **不要拿它当密码回填输入框**——那是把一个布尔当成密码串，页面上会显示 `true`/`false`。 */
  password_set: boolean
  /** 总开关；**检测通过才允许为真**（后端 `enabled=True` 时会先同步跑一次检测）。 */
  enabled: boolean
  /** 发单价的勾兑百分比（1–100）：我方**发单价** = 上家 `recFee` × 本值 / 100；100 = 原价接手。 */
  hall_price_percent: number
  game_rules?: UpstreamGameRules
  /** 上家**大厅**拉取间隔秒数（展示用，页面不可改）。 */
  hall_poll_seconds: number
  /** 上家**「我的订单」**拉取间隔秒数（展示用，页面不可改）。 */
  my_orders_poll_seconds: number
  /** 上一轮同步**开跑**的时刻；还没跑过是 `null`。 */
  last_run_at: string | null
  /** 上一轮**成功**的时刻；从没成功过是 `null`。 */
  last_ok_at: string | null
  /** 最近的失败原因（**人话**，后端给的就是可显示文案）；没有失败时是空串。 */
  last_error: string
  last_error_at: string | null
  /** 上一次「启用」时那次检测通过的时刻（换过凭据会被清成 `null`）。 */
  checked_at: string | null
  /** ⭐ D-0261：这行绑定配的**订单来源**字典项（`{id, 名字}`）；未配置时是 `null`。
   * 上家来的单落池 / 转正时自动带上它与名字快照（后端两条建单写点的口径）。 */
  order_source: { id: number; name: string } | null
  created_at: string
  updated_at: string
}

/** 保存的载荷。⚠️ **`password` 省略或空串 = 不改**：页面上的密码框**永远留空**（后端不回密码），⛔ 不要让它变成「清空密码」。⚠️ `hall_price_percent`（1–100）在**本页**是必填（那一格永远有值：留空按 100 提交）；后端那一边是 `required=False`（缺键 = 保持库里原值，模型默认 100）——⛔ 两边的「必填」不是一回事。范围由序列化器与模型校验器**两头卡**（⛔ 传 0 等于把一张**免费单**发进我方大厅）。 */
export type JidanpuUpstreamBindingPayload = {
  jidanpu_base_url: string
  account: string
  /** ⭐ 人填的「上家系统名称」（选填）。⚠️ 它是**可回显**的可见格 ⇒
   * 空串 = **清除**（后端照落空串），与 `password` 的「空串 = 不改」**不是**一套语义；
   * 页面（弹窗）总是显式发这一键。 */
  name?: string
  password?: string
  hall_price_percent: number
  game_rules?: UpstreamGameRules
  enabled: boolean
  /** ⭐ D-0261：订单来源字典项的主键；`null` = **不指定**（清除）。⚠️ 后端的口径是「省略 = 不改、`null` = 清除」：弹窗的下拉恒有值（「不指定」也是一种选择）⇒ 页面**总是**显式发（`number | null`），让「用户选了不指定」与「没这个字段」不混成一件事（E-0021 同族：空值语义要显式）。 */
  order_source_id?: number | null
}

/** 「测试连接」的结论（后端 `JidanpuUpstreamTestConnectionView` 的出参）。⚠️ **`ok === false` 是一条正常的结论**（信封 `code` 恒为 0），不是请求失败：那时 `messages` 是后端逐条点名的人话（登录 / 大厅 / 我的订单 / 店铺标识四项预检），页面要把它**显示出来**。⛔ 别把 `ok === false` 当成异常吞掉。 */
export type JidanpuUpstreamTestResult = {
  ok: boolean
  /** 检测没过时逐条点名的人话；全过时是空数组。 */
  messages: string[]
}

/** 「立即同步」的结果：这一轮**真的改了几笔**我方订单（`applied`）。 */
export type JidanpuUpstreamSyncNowResult = {
  applied: number
}

// 租户级只读总览（overview 类型 / URL / `getUpstreamOverview`）已经删掉，别加回来。
// 它和本人绑定、检测、立即同步、绑定列表、删除、接单没有关系。

const BINDING_URL = '/api/business/member/upstream-jidanpu-binding/'
const TEST_CONNECTION_URL = '/api/business/member/upstream-jidanpu-binding/test-connection/'
const SYNC_NOW_URL = '/api/business/member/upstream-jidanpu-sync-now/'
/** **行级**四条（`POST` 集合 + `GET` / `PUT` / `DELETE` 明细；E-0255 / D-0256，）：绑定改**多行**资源后，页面的新增 / 编辑 / 回填 / 删除走这里——旧写法（弹窗打按用户的member PUT）会把同账号的第二条绑定**静默覆盖**掉（生产实锤 E-0255）。⚠️ 它**不在**上面那条单行资源路径下，而是「绑定上家系统设置」页那张**统一列表**这一侧：`<pk>` 是那一行在列表里的 `id`；后端节点门与列表 / 删除同一个（`system.bind_upstream_system`）。其中 `DELETE` 是 2026-10-04 计划 §2.2 的既有真删端点（D-0256 把它并进同一组行级出口）。 */
const ROW_BASE_URL = '/api/business/upstream-bindings/jidanpu/'
/** 大厅第三路的接单端点（`urls.py` 的 `hall/upstream/<int:pk>/claim/`，`<pk>` = 镜像行主键）。 */
const CLAIM_URL = '/api/business/hall/upstream/'

/** 读**本人**那一行；**还没绑定时后端回 `null`**（不编一个空行）。 */
export async function getUpstreamBinding(): Promise<JidanpuUpstreamBindingRow | null> {
  return request<JidanpuUpstreamBindingRow | null>({ method: 'get', url: BINDING_URL })
}

/** 存（**PUT**）。单行资源没有主键、没有 PATCH：后端「没有行就建、有就改」。⚠️ `enabled: true` 时后端**先同步跑一次检测**：不过就**一行都不落**，并用业务失败码把缺项**逐条点名**回给你（`request()` 会把它抛成 `BizError`，`message` 就是那句人话）。⇒ 调用方必须把这句话**原样显示**，⛔ 不要自己改写成「保存失败」。 */
export async function putUpstreamBinding(
  payload: JidanpuUpstreamBindingPayload,
): Promise<JidanpuUpstreamBindingRow> {
  return request<JidanpuUpstreamBindingRow>({ method: 'put', url: BINDING_URL, data: payload })
}

/** 「测试连接」：拿**本人那一行的凭据**登录上家鸡蛋铺跑一遍绑定检测（spec §9.2）。
 * ⚠️ **不传任何参数**（凭据只从后端那一行取，spec §8.3）：这条端点连请求体都不读。
 * 失败（还没绑定 / 缺项 / 上游异常）回业务失败码 ⇒ `request()` 抛 `BizError`，`message` 是人话。 */
export async function testUpstreamConnection(): Promise<JidanpuUpstreamTestResult> {
  return request<JidanpuUpstreamTestResult>({ method: 'post', url: TEST_CONNECTION_URL })
}

/** 「立即同步」：对**本人那一行**跑一轮状态回拉（spec §9.3）。⚠️ **空体**：行主只来自登录态（后端一个字节的请求体都不读）。⚠️ 有频控（同会话最小间隔 / 在途互斥 / 失败退避）——被挡时后端回**人话文案（含剩余秒数）**，`request()` 抛 `BizError` ⇒ 页面**原样显示**它，⛔ 页面不许自己再写一套节流判据。⚠️ 这一跳**会真的改我方订单状态**（后端 `apply_transition` + 审计留痕）。 */
export async function syncUpstreamNow(): Promise<JidanpuUpstreamSyncNowResult> {
  return request<JidanpuUpstreamSyncNowResult>({ method: 'post', url: SYNC_NOW_URL })
}

/** 在我方大厅接一张**本线**的上家镜像单（spec §6.1）。⚠️ 端点按**本线镜像行的主键**取行（`hall/upstream/<int:pk>/claim/`）⇒ 参数就是大厅行里的`mirror_pk`（后端 `serialize_upstream_row` 填的、**只有本线路非空**）。⛔ **拿上家单号 / 展示单号去打这个端点会打不中那一行**（那是 `remote_order_id`，不是主键）。「形状先行拒绝」与其它写端点同一条纪律：主键不是**正整数**时一次请求都不发（E-0021 同族：`0` / `NaN` / 负数都不是合法主键，别让它们变成一条注定 404 的请求）。⚠️ 权限节点与原生接单**同一个**（后端 `grab_hall.take_order`）⇒ 页面画按钮之前必须先过那道门。⚠️ 失败**一律原样显示后端 `msg`**（「该笔订单已被其他打手接取」/「暂时无法接单，请稍后再试」）。 */
function claimUpstreamMirrorConfig(mirrorPk: number) {
  if (!Number.isInteger(mirrorPk) || mirrorPk <= 0) {
    throw new Error(`镜像行主键不合法（应为正整数）：${String(mirrorPk)}`)
  }
  return { method: 'post' as const, url: `${CLAIM_URL}${mirrorPk}/claim/` }
}

export async function claimUpstreamMirror(mirrorPk: number): Promise<Order> {
  return request<Order>(claimUpstreamMirrorConfig(mirrorPk))
}

/** ⭐ 与 `claimUpstreamMirror` 同一条请求，额外带出后端信封的 `msg`
 * （抢单成功后可能自动撤回代练通订单；撤不掉的警告只在 `msg` 里）。 */
export async function claimUpstreamMirrorWithMessage(
  mirrorPk: number,
): Promise<{ order: Order; message: string }> {
  const res = await requestEnvelope<Order>(claimUpstreamMirrorConfig(mirrorPk))
  return { order: res.data, message: res.msg }
}

/** **删掉一条**鸡蛋铺上家绑定（`DELETE /api/business/upstream-bindings/jidanpu/<id>/`；计划 §2.2）。⚠️ **真删**（不是置 `enabled=False`）：后端 `row.delete()`，级联口径由模型既有的 `on_delete`决定 —— 它名下还在跟踪的同步链路行（订单 ↔ 上家单的对应关系）**跟着删、不可恢复**、大厅镜像行的归属置空、同步台账保留（计划 §0 裁定 5 / §2.2）。⛔ 前端**不自己**去删链路 / 台账（那是模型的事），也⛔ 不通知上游（鸡蛋铺没有解绑接口）。⚠️ 它与上面的 `putUpstreamBinding` 作用的是**同一条记录**（登录人自己那一行）：写仍只作用于本人那一行，删可以删**列表里任何人的**那一行（计划 §7 残差 2）。⚠️ 失败（如 `1002` 「这个绑定不存在」）由 `request()` 抛业务错误，调用方**原样显示** `msg`。 */
export function deleteUpstreamBinding(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${ROW_BASE_URL}${id}/` })
}

/** 行主键的**形状先行拒绝**（与 `claimUpstreamMirror` 同一条纪律，E-0021 同族）：
 * `0` / `NaN` / 负数 / 非整数都不是合法主键，别让它们变成一条注定打不中的请求。 */
function assertRowPk(id: number): void {
  if (!Number.isInteger(id) || id <= 0) {
    throw new Error(`绑定行主键不合法（应为正整数）：${String(id)}`)
  }
}

/** **行级读**一条（`GET /api/business/upstream-bindings/jidanpu/<id>/`；D-0256）。编辑弹窗的回填出口：统一列表的 11 键里没有 `enabled` / `hall_price_percent`，要按库里现值预填就只能按主键读一次。响应形状与 `getUpstreamBinding`（member GET）**同一份**序列化器：`password` 依旧只写不读，只有 `password_set` 布尔。⚠️ 行已被删 ⇒ 后端回业务失败码「这个绑定不存在」⇒ `request()` 抛 `BizError`，调用方原样显示。 */
export async function getUpstreamBindingRow(id: number): Promise<JidanpuUpstreamBindingRow> {
  assertRowPk(id)
  return request<JidanpuUpstreamBindingRow>({ method: 'get', url: `${ROW_BASE_URL}${id}/` })
}

/** **行级创建**（`POST /api/business/upstream-bindings/jidanpu/`；D-0256）。多行资源：同一账号可以建多条不同上家的绑定，⛔ 不再有「覆盖本人那一行」这回事。载荷与 `putUpstreamBinding` 同形（⭐ 多一格可选的 `name`）；`enabled: true`时后端先跑同一份检测闸，不过就一行都不落并把缺项逐条点名（`BizError.message`），页面原样显示。 */
export async function createUpstreamBindingRow(
  payload: JidanpuUpstreamBindingPayload,
): Promise<JidanpuUpstreamBindingRow> {
  return request<JidanpuUpstreamBindingRow>({ method: 'post', url: ROW_BASE_URL, data: payload })
}

/** **行级编辑**（`PUT /api/business/upstream-bindings/jidanpu/<id>/`；D-0256）。
 * ⚠️ 只动**这一行**；`password` 省略或空串 = 不改（同既有口径）；
 * 换了地址 / 账号 ⇒ 后端清 `checked_at` 并把这一行在飞的同步事件收敛成 `skipped`。 */
export async function updateUpstreamBindingRow(
  id: number,
  payload: JidanpuUpstreamBindingPayload,
): Promise<JidanpuUpstreamBindingRow> {
  assertRowPk(id)
  return request<JidanpuUpstreamBindingRow>({
    method: 'put',
    url: `${ROW_BASE_URL}${id}/`,
    data: payload,
  })
}
