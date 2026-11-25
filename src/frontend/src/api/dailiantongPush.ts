// 代练通抛单（发单到第三方交易平台）的两个出口。契约来源（实现即契约）：
// `src/apps/business/api/dailiantong_push_views.py` + `urls.py`——`POST /api/business/orders/<order_no>/push-to-platform/`（真发单，返回**推送行**）、`GET /api/business/platform-integrations/zones/?q=`（区服字典）。
// 失败分层（⛔ 页面不要自己猜）：**前置闸**（没配置 / 状态不对 / 价格低于下限 / 重复发单 / 余额不足）⇒ HTTP 200 + `code=1002` + 中文 `msg`，`request()` 抛 `BizError`、页面**原样展示**；**上游业务失败 / 超时** ⇒ 接口仍是 `code=0`、**推送行本身**表达结果（`state=failed`/`unknown` + `last_error`）⇒ 页面必须按 `state` 分三态，⛔ 不能拿「请求没抛异常」当成功。
// 凭据红线（`AGENTS.md` §4）：响应里只有推送行，没有 `app_secret` / `pay_password_md5` / `pay_pass` ⇒ 本文件的读模型也**不定义**任何凭据字段（同 `api/platformIntegrations.ts` 的口径）。
import { HttpError, NetworkError } from './errors'
import { request } from './http'
import type { Page } from './unwrap'

/** 本版唯一支持真发单的平台（后端 `SUPPORTED_PLATFORM`；⛔ 不要照抄成 13 个平台枚举）。 */
export const DAILIANTONG_PLATFORM = 10

/** 区服搜索端点（后端 `DailiantongZonesView`）。 */
export const PLATFORM_ZONES_URL = '/api/business/platform-integrations/zones/'

/** 发单端点（**唯一**的 URL 拼装处；页面不自己拼）。 */
export function pushToPlatformUrl(orderNo: string): string {
  return `/api/business/orders/${encodeURIComponent(orderNo)}/push-to-platform/`
}

/** 推送行状态（后端 `DailiantongPush.State`；`closed` = 已在平台删除）。 */
export type DailiantongPushState = 'pending' | 'sent' | 'failed' | 'unknown' | 'closed'

/** ⭐ **「有效推送」= 这几种本地状态**。唯一权威在后端 `apps/business/models.py::DailiantongPush.ACTIVE_STATES`（与部分唯一索引条件逐字一致），前端这份是**镜像**：只用来回答「读面把撤销默认金额挂在哪一条行上」（后端只挂该平台**最新一条有效**推送行）。⛔ 不要把它当成业务状态机在页面里扩散。 */
export const PLATFORM_ACTIVE_PUSH_STATES: readonly DailiantongPushState[] = [
  'pending',
  'sent',
  'unknown',
]

/** 推送链路行（后端 `DailiantongPushSerializer` 的对外形状）。金额是 `DecimalField` ⇒ 字符串；`serial_no` / `remote_*` 在没拿到平台单号时是 `null`。 */
export type DailiantongPushRow = {
  id: number
  order: number
  platform: number
  serial_no: string | null
  state: DailiantongPushState
  state_display: string
  price: string
  ensure1: string
  ensure2: string
  time_limit_hours: number
  ex_order_no: string
  remote_status: number | null
  remote_cancel_status: number | null
 /** ⭐ **综合状态**（`Status` 与 `CancelStatus` 一起判，后端 `display_status_for_remote` 算出）：`CancelStatus 12/15/16` ⇒ `revoked` / `force_revoked`、`11` ⇒ `cancelling`、`14` ⇒ `arbitrating`、`13` ⇒ 按 `Status`。
  * 页面「交易状态」优先显示 `display_status_label`（⛔ 不许只看 `remote_status`）。可选是为了容忍旧回包（缺席 ⇒ 页面本地推导）。 */
  display_status?: string | null
  display_status_label?: string | null
 /** ⭐ 接单打手 UID（详情 `AcceptUID` 的落库值；空串 = 还不知道）。 */
  accept_uid: string
 /** ⭐ 现在能不能申请客服介入（仲裁）。真机定案（`docs/DAILIANTONG-API.md` §6.9）：只有「协商撤销中（`CancelStatus=11`）且已满 1 小时」能申请，别的时候上游回 `-1` / `-3`；后端算好下发（页面⛔ 不自己算时间）。
  * **字段缺席 = 后端未升级 ⇒ 保持可点**，点了让上游原文说话。 */
  arbitration_available?: boolean | null
 /** 不能申请时的中文原因（含剩余分钟）；与 `arbitration_available=false` 配套。 */
  arbitration_blocked_reason?: string | null
 /** ⭐ 这次协商撤销是**谁发起的**：`'taker'` ⇒ 打手主动申请（我们应当能「同意撤销」）；`'us'` ⇒ 我们自己发起的（只能等打手同意）；`''` / `null` / 键缺席 ⇒ 判不出（页面按「两颗都摆出来、点了由后端判」降级）。值域就这三档，⛔ 页面不要用别的值去猜发起方。 */
  cancel_initiator?: 'taker' | 'us' | '' | null
 /** ⭐ 现在能不能点「同意撤销」。语义与 `arbitration_available` 同族：后端算好下发，页面**只认 `=== false`**；键缺席（后端未升级）⇒ 按 `cancel_initiator` 降级判断，保持可点、点了让后端原文说话。 */
  agree_cancel_available?: boolean | null
 /** ⭐ 代练通拼好的**撤销要求原文**（含它自己算的支出金额与撤销原因，如 `接单者"…"申请撤销,需发单者支出:0.00, …`）。「同意撤销」的确认框与撤销区说明行都**原样**展示这段（保留换行 / 标点，⛔ 不改写、不截断）；缺席 ⇒ 退回 `cancel_reason`，再没有才用页面兜底文案。 */
  cancel_cmt?: string | null
 /** ⭐ 撤销原因（`cancel_cmt` 缺席时的退路；上游只有一句原因时用它）。 */
  cancel_reason?: string | null
 /** ⭐ **已结代练费 / 已赔保证金**（上游结算后的实际金额）。后端只在远端 `Status` 17（已结算）/ 16（撤销中）给值，其它时候是 `null`（键也可能整个缺席）。判据用 `is not None`（`0` 是合法金额边界）：有值 ⇒ 两位小数；`null` / 缺键 ⇒「—」，⛔ 不编 `0.00`。 */
  settled_pay_level_bal?: string | null
  settled_rep_ensure_bal?: string | null
 /** ⭐ 上游详情里的「撤销申请结算 / 撤销申请赔付」现值（`PayLevelBal` / `RepEnsureBal`，两位小数字符串）。发单列表「申请撤销」弹窗用它做**默认值**；⚠️ 后端序列化器目前**还没**下发 ⇒ 缺席 = 输入框留空、要求操作人手填（⛔ 不编 `0.00`）；判据 `is not None`（`0` 是合法金额边界）。 */
  pay_level_bal?: string | null
  rep_ensure_bal?: string | null
  last_synced_at: string | null
  last_error: string
  attempts: number
  created_at: string
  updated_at: string
}

/** 发单入参（后端 `PushToPlatformSerializer`）。⚠️ **恰好这些业务字段 + `platform`**（价格 / 两个保证金 / 时限 / 区服 / 两个接单门槛 / 代练要求 / 自动下架天数 / 代练内容 `memo`）；其余表单（账号 / 密码 / 角色 / 标题 / 频道 / 标签…）由后端按订单自动生成——⛔ 多送一个键等于偷偷改变契约。
 * ⭐ `memo` = 发单页「当前订单说明」框的**当前值**（打开时由 `preview.memo` 带出，客服可改）；**空串也是有效值** ⇒ 照发空串、由后端决定语义（⛔ 页面不自己吞掉这个键；后端没接之前多送的 `memo` 被 DRF 忽略、不会 500）。
 * 两个门槛的「传 / 不传 / 传 null」三态：**键缺席** ⇒ 后端用全局设置（`PlatformPushSettings`）；**显式 `null`** ⇒ 本单「不设置」；**数字** ⇒ 本单按这个值（`power_level: 0` = 上游语义「不限制」）。页面正常路径：设置读到真值 ⇒ 两个键**都传**；没读到 ⇒ **都不传**，让后端用它自己的全局默认（⛔ 不拿证据件里的 5 / 0 冒充）。 */
export type PushToPlatformPayload = {
  platform: number
  price: string
  ensure1: string
  ensure2: string
  time_limit_hours: number
  zone_server_id: string
 /** 本单「要求最低完单数」；`0` = 不限制（上游语义）。缺席 = 用全局默认。 */
  power_level?: number
 /** 本单「拒绝新手和介入率」；**`null` = 不设置**（键缺席则用全局默认）。 */
  limit_accept?: number | null
 /** ⭐ P9：代练内容（「当前订单说明」框的当前值；空串 = 客服清空了）。 */
  memo?: string
 /** ⭐ I3：代练/陪玩要求（页面填的值；缺席 = 后端用全局模板）。 */
  requirement?: string
 /** ⭐ I3：自动下架天数；`0` / 缺席 = 不上送（上游默认 7 天）。 */
  off_sell_days?: number
 /** ⭐ 发单频道（上游 `Groups`）。本版只发代练通 ⇒ 页面只送 `PUBLIC` / `GOODCHANNEL` / `CERTCHANNEL`。 */
  groups?: string
 /** ⭐ 标签 1（上游 `Label1`，自由文本）。`label2` 后端也支持，但发单页证据件里只有一格标签 ⇒ 本版不送 `label2`。 */
  label1?: string
  label2?: string
 /** ⭐ **同发内部抢单价**（⚠️ 不是代练通参数）——只在「抛代练通前补发内部大厅」那一步生效。页面只在 >0 时送；缺席 = 大厅价沿用发单价（⛔ 不送 0 把大厅价改成免费）。 */
  hall_price_same?: string
}

export function pushOrderToPlatform(
  orderNo: string,
  payload: PushToPlatformPayload,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: pushToPlatformUrl(orderNo),
    data: payload,
  })
}

/* ───────────── 平台推送读面 ───────────── */

/** 「第三方平台订单管理」列表端点（后端 `PlatformPushListView`）。 */
export const PLATFORM_PUSHES_URL = '/api/business/platform-pushes/'

/** 列表取数的**超时上限**（10 秒）。这一页只有这一发取数，没有超时就会一直停在「取数中」——用户看到空表、又没有任何失败提示（卡死）。超时后按 `NetworkError('请求超时')` 失败 ⇒ 页面显示「加载失败：请求超时」。 */
export const PLATFORM_PUSHES_TIMEOUT_MS = 10_000

/** 订单列表载荷里的 `platform_push` 摘要（后端 `OrderListSerializer.get_platform_push`）。
 * ⚠️ **恰好 7 个只读键**（后端 `tests/test_dailiantong_platform_data.py::PLATFORM_PUSH_KEYS`）：有推送行 ⇒ **最新一笔**（后端 `-created_at, -id` 排好取第一条；⛔ 不做「活跃优先」——列表摘要 / 详情读面 / 第三方页聚合必须来自**同一条**），没有 ⇒ 整个键是 `null`。
 * ⚠️ `serial_no` 在 `failed` / `pending` / `unknown` 档可能是**空串**（后端把 `NULL` 归一成 `''`）——⛔ 不要把它伪造成一个像单号的值。 */
export type PlatformPushSummary = {
  platform: number
  serial_no: string
  state: DailiantongPushState
  remote_status: number | null
  remote_cancel_status: number | null
  is_active: boolean
 /** ⭐「发平台价格」= 推送行自己的 `price`（与「发单价」列同源）。管理订单页「发单价」列在有推送时显示它（真机现场：发到代练通 3.00、本地 `hall_price` 为空，列表却显示 0.00）。两位小数字符串；**受限角色**（`order.restrict_view_offer_price`）⇒ `null`（与 `hall_price` 同一道脱敏）；`undefined` = 旧后端没下发 ⇒ 前端降级回本地。 */
  price?: string | null
}

/** 「第三方平台订单管理」页的一行（后端 `PlatformPushRowSerializer`）。本类型只声明**页面今天消费**的键（后端还回了 `qr_*` / `remote_taker_*` 等列，页面暂不消费 ⇒ 不声明，避免误以为有第二套渲染）。
 * 页面需要、但接口**没有**的数据源（利润 / 剩余时间 / 撤销时间 / 未读消息…）**不在这里造键**，页面按「—」渲染（缺数据 ≠ 给个假 0 / 假 null）。 */
export type PlatformPushListRow = {
  id: number
  platform: number
 /** 平台单号；`null` = 还没拿到（`pending` / `failed` / `unknown` 档）。
  * ⚠️ 与订单列表载荷的 `platform_push.serial_no` **不同**：那边后端归一成空串，这条读面（`PlatformPushRowSerializer`）直接回模型值 ⇒ **可能是 `null`**。⛔ 不要伪造成像单号的值；页面按「—」显示。 */
  serial_no: string | null
  order_no: string
 /** ⭐「来源单号」（`Order.source_order_no`）——「系统单号」列要显示它（空则回退 `order_no`，见 `order-coreordercenter.ts::sourceOrderNoOf`）。后端 `PlatformPushRowSerializer` **已下发**这个键；可选是因为旧回包 / 别的读面复用本类型时可能没有 ⇒ 页面按「有就用、空回退内部单号」。 */
  source_order_no?: string
  game: string
  server: string | null
  price: string
  state: DailiantongPushState
  remote_status: number | null
  remote_cancel_status: number | null
 /** ⭐ h1：「交易状态」的综合状态（后端 `display_status_for_remote`）。
  * 页面优先显示 `display_status_label`；缺席（旧回包）⇒ 本地按两个码推导。 */
  display_status?: string | null
  display_status_label?: string | null
  order_status: string
  created_at: string
  last_synced_at: string | null
  last_error: string
 /** ⭐「订单标签」（`Order.order_label`，本地列）。**可选是因为今天这条读面还不回它**（`PlatformPushRowSerializer.fields` 里没有它）⇒ 页面按「有就用、没有就回退本会话内刚写成功的值」，后端补下发后这一格自动显示。 */
  order_label?: string
 /** ⭐ 撤销申请的两个**现值**（上游 `LevelOrderCancel` 的 `PayLevelBal` / `RepEnsureBal`，两位小数字符串）。「协商撤销」弹窗拿它们做默认值，写入后靠「按需重取」刷新。⚠️ 后端今天可能**还不下发** ⇒ 缺席 = 弹窗留空、要求操作人手填（⛔ 不编 `0.00`）；判据 `is not None`（`0` 是合法金额边界）。 */
  pay_level_bal?: string | null
  rep_ensure_bal?: string | null
 /** ⭐ 这行**还值不值得轮询**（远端到终态后后端置 `False`）。⚠️ 第三方平台订单管理页**不拿它选条**（父行取**最新的一笔**，`created_at` 降序、`id` 降序，⛔ 不看这一列）；它仍是后端的事实列，别的读面（订单列表摘要）照旧可用。缺席 ⇒ 页面不受影响，⛔ 不报错。 */
  is_active?: boolean
 /** ⭐ 这一行的**本地接单价**（`Order` 上的接单价）。列表「接单价」列读它；「利润」= 它 − `price`（发单价），两边都有值才算（缺一边「—」）。⚠️ 写成可选；缺席时页面画「—」（⛔ 不拿 0 充数）。 */
  accept_price?: string | null
 /** ⭐「订单来源」的**显示值**。兼容两种下发形态：名字（非纯数字 ⇒ 直接显示）与**字典 id**（纯数字 ⇒ 显示「—」，⛔ 不拿 id 当名字）；缺席同样「—」。 */
  order_source?: string | number | null
 /** ⭐ h6x N7：验收倒计时的**绝对截止时间**（ISO；优先用它算剩余秒数）。 */
  verify_deadline_at?: string | null
 /** ⭐ h6x N7：后端抓取时刻的**剩余秒数**（没有截止时间时的兜底；`null` ⇒「—」）。 */
  verify_remaining_seconds?: number | null
 /** ⭐ h6x N7：「接手人」显示名（空 ⇒「—」）。 */
  accept_user_name?: string | null
 /** ⭐ h6x N7：接手人的平台 UID（有值时挂在「接手人」格的 `title` 上）。 */
  accept_uid?: string | null
 /** ⭐ h6x N7：该打手的**完成单数**（空 ⇒「—」）。 */
  accept_over_count?: number | null
 /** ⭐ h6x N7：该单的**投诉 / 取消次数**（> 0 标红）。 */
  cust_cancel_count?: number | null
 /** ⭐ h6x N7：平台侧的**接单时间**（ISO，页面格式化到分钟）。 */
  remote_accepted_at?: string | null
 /** ⭐ **剩余时间**（整数秒 = 接单时间 + 时限 − 现在，后端保证）：未接单 ⇒ `null`；已过期 ⇒ `0`。页面**直接用这个秒数**倒计时（本地每秒递减），⛔ **不拿时间戳自己再算一遍**。 */
  remaining_seconds?: number | null
}

/** ⭐ `?group_by=order` 时**一个订单一条**的聚合行。契约：`GET /api/business/platform-pushes/?platform=10&group_by=order&page=1&page_size=30` → `{ items: [{ order_no, source_order_no, order_status, game, server, pushes: [...] }], total: <订单数>, page, page_size }`。
 * ⚠️ **分页语义**：`total` / `page` / `page_size` 全部按**订单**算（页面聚合的前提；按推送行分页时前端聚合会把同一单拆到两页，⛔ 不做那种假聚合）。 */
export type PlatformPushGroupItem = {
  order_no: string
 /** 来源单号（`Order.source_order_no`，可空串）。 */
  source_order_no?: string
  order_status: string
  game: string
  server: string | null
  pushes: PlatformPushSubRow[]
}

/** 聚合行里的**平台子单**（一条推送一行）。键是后端 `group_by` 契约里点名的那几个 + 页面子行要用的补充键（`id` / 时间 / 错误原文）；值域与 `PlatformPushListRow` 的同名键一致（同一个上游状态码表 / 同一个金额字符串口径）。 */
export type PlatformPushSubRow = {
  platform: number
  serial_no: string | null
  state: DailiantongPushState
  remote_status: number | null
  remote_cancel_status: number | null
 /** ⭐ 子行的综合状态（缺键 ⇒ 页面本地按两个码推导）。 */
  display_status?: string | null
  display_status_label?: string | null
  price: string
 /** 撤销申请结算 / 撤销申请赔付现值（两位小数字符串；后端未给时缺席）。 */
  pay_level_bal?: string | null
  rep_ensure_bal?: string | null
  qr_sent_at?: string | null
 /** 子行 tooltip 用的补充键（后端契约里的 `...`；缺席时页面按「—」）。 */
  id?: number
  created_at?: string
  last_synced_at?: string | null
  last_error?: string
 /** ⭐ 这行**还值不值得轮询**（远端到终态后后端置 `False`）。⚠️ 第三方平台订单管理页**不拿它选条**（父行取**最新的一笔**，`created_at` 降序、`id` 降序，⛔ 不看这一列）；它仍是后端的事实列。缺席 ⇒ 页面不受影响，⛔ 不因此拒绝服务。 */
  is_active?: boolean
 /** ⭐ 这一行的**本地接单价**。列表「接单价」列读它；「利润」= 它 − `price`（发单价），两边都有值才算（缺一边「—」）。⚠️ 可选；缺席时页面画「—」（⛔ 不拿 0 充数）。 */
  accept_price?: string | null
 /** ⭐「订单来源」的**显示值**。兼容两种下发形态：名字（非纯数字 ⇒ 直接显示）与**字典 id**（纯数字 ⇒ 显示「—」，⛔ 不拿 id 当名字）；缺席同样「—」。 */
  order_source?: string | number | null
 /** ⭐ h6x N7：验收倒计时的**绝对截止时间**（ISO；优先用它算剩余秒数）。 */
  verify_deadline_at?: string | null
 /** ⭐ h6x N7：后端抓取时刻的**剩余秒数**（没有截止时间时的兜底；`null` ⇒「—」）。 */
  verify_remaining_seconds?: number | null
 /** ⭐ h6x N7：「接手人」显示名（空 ⇒「—」）。 */
  accept_user_name?: string | null
 /** ⭐ h6x N7：接手人的平台 UID（有值时挂在「接手人」格的 `title` 上）。 */
  accept_uid?: string | null
 /** ⭐ h6x N7：该打手的**完成单数**（空 ⇒「—」）。 */
  accept_over_count?: number | null
 /** ⭐ h6x N7：该单的**投诉 / 取消次数**（> 0 标红）。 */
  cust_cancel_count?: number | null
 /** ⭐ h6x N7：平台侧的**接单时间**（ISO，页面格式化到分钟）。 */
  remote_accepted_at?: string | null
 /** ⭐ **剩余时间**（整数秒 = 接单时间 + 时限 − 现在，后端保证）：未接单 ⇒ `null`；已过期 ⇒ `0`。页面**直接用这个秒数**倒计时（本地每秒递减），⛔ **不拿时间戳自己再算一遍**。 */
  remaining_seconds?: number | null
}

/** 列表一行的两种形态（后端 `group_by=order` 未上线时是扁平的推送行）。 */
export type PlatformPushListItem = PlatformPushListRow | PlatformPushGroupItem

/** `GET /api/business/platform-pushes/` 的查询参数（空值由 axios 丢弃）。 */
export type PlatformPushListParams = {
  platform?: number
 /** 上游 `Status` 码；`undefined` = 全部（⛔ 不传 `0` 冒充「全部」）。 */
  status?: number
  keyword?: string
  page?: number
  page_size?: number
 /** ⭐ `order` = 按本地订单聚合分页。⚠️ 后端未升级时会**忽略**这个键、照旧回扁平推送行 ⇒ 调用方必须判 `items[0].pushes` 是否存在再决定渲染方式（见 `isGroupedPushItem`）。 */
  group_by?: 'order'
}

/** 取平台推送行（分页）。`params` 里为 `undefined` 的键不会出现在查询串里（axios 默认行为），所以「全部 / 没搜索」就是**不带 `status` / `keyword`**，与后端「不带 = 不筛」的口径一致。 */
export function fetchPlatformPushes(
  params: PlatformPushListParams = {},
): Promise<Page<PlatformPushListRow>> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, PLATFORM_PUSHES_TIMEOUT_MS)
  return request<Page<PlatformPushListRow>>({
    method: 'get',
    url: PLATFORM_PUSHES_URL,
    params,
    signal: controller.signal,
  })
    .catch((error: unknown) => {
 // axios 被 abort 时抛的是取消错误（`errors.ts` 会归成 `网络异常`）⇒ 这里按**超时**说清楚。
      if (timedOut) throw new NetworkError('请求超时')
      throw error
    })
    .finally(() => {
      clearTimeout(timer)
    })
}

/** ⭐ 按**订单**聚合取数（`?group_by=order`）。与 `fetchPlatformPushes` 打同一个 URL，只多一个 `group_by=order` 键；超时 / 中止语义相同。
 * ⚠️ **后端未升级时**这个键会被忽略、回来仍是扁平推送行 ⇒ 调用方必须用 `isGroupedPushItem` 逐个判形状再决定渲染方式（⛔ 不要假定 grouped 一定生效）。 */
export function fetchPlatformPushGroups(
  params: PlatformPushListParams = {},
): Promise<Page<PlatformPushGroupItem>> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, PLATFORM_PUSHES_TIMEOUT_MS)
  return request<Page<PlatformPushGroupItem>>({
    method: 'get',
    url: PLATFORM_PUSHES_URL,
    params: { ...params, group_by: 'order' },
    signal: controller.signal,
  })
    .catch((error: unknown) => {
      if (timedOut) throw new NetworkError('请求超时')
      throw error
    })
    .finally(() => {
      clearTimeout(timer)
    })
}

/** ⭐ 一条列表项是不是**聚合行**（含 `pushes[]`）。判据是**数组**（`[]` 也是合法值），⛔ 不用真值（空数组是真值但语义是「这单没有子单」——那种行不该画三角，属于后端契约异常，页面按 0 条渲染并沿用同一个判据）。 */
export function isGroupedPushItem(item: unknown): item is PlatformPushGroupItem {
  return (
    item !== null &&
    typeof item === 'object' &&
    Array.isArray((item as { pushes?: unknown }).pushes)
  )
}

/* ───────────── 页签数字一次取回 ───────────── */

/** 14 个状态页签的括号数字（后端 `PlatformPushCountsView`，`GET /api/business/platform-pushes/counts/`）。
 * ⚠️ **只需一次请求**：页面打开时把 14 个页签一起填好，⛔ 不再「点一个查一个」。口径 = **推送行**（不是订单数）：`total` 与 `counts` 的分桶都与列表页 `?status=…` 的 `total` 同源；`counts` 按上游 `remote_status` 分桶（`"10"`–`"18"` 恒在，0 也回；`"18"` = 已下架，`"none"` = 还没回读过），`states` 按本地推送状态分桶。这是「同一订单两条推送 = 两个数」的口径——与列表分页同源。 */
export type PlatformPushCounts = {
 /** 范围内全部推送行（已软删的订单不算，与列表同口径）。 */
  total: number
 /** `remote_status` → 条数；键是**字符串**码（`"11"`…）。缺桶时页面画「—」，⛔ 不补 0。 */
  counts: Record<string, number>
 /** 本地推送状态 → 条数。 */
  states: Partial<Record<DailiantongPushState, number>>
}

/** 页签批量计数端点（后端 `PlatformPushCountsView`）。 */
export const PLATFORM_PUSH_COUNTS_URL = '/api/business/platform-pushes/counts/'

/** 一次取回 14 个页签的数字（只读、不调上游）。入参形状与列表同一套（`platform` / `keyword`）；⛔ 不收 `status`（本端点按状态分桶）。
 * 失败语义同列表：业务码 / 网络 / 超时都会让 `request()` 抛错，调用方按**中文原因**展示（页面口径：页签数字保持「—」+ 一句「页签数字加载失败：<原因>」）。 */
export function fetchPlatformPushCounts(
  params: { platform?: number; keyword?: string } = {},
): Promise<PlatformPushCounts> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, PLATFORM_PUSHES_TIMEOUT_MS)
  return request<PlatformPushCounts>({
    method: 'get',
    url: PLATFORM_PUSH_COUNTS_URL,
    params,
    signal: controller.signal,
  })
    .catch((error: unknown) => {
      if (timedOut) throw new NetworkError('请求超时')
      throw error
    })
    .finally(() => {
      clearTimeout(timer)
    })
}

/** 区服字典一行（后端 `list_zones()` 的对外形状）。 */
export type PlatformZone = {
  zone_server_id: string
  server_name: string
}

/** 按关键字搜区服（后端按 `ServerName` 或区服编码过滤，不区分大小写）。⚠️ 空关键字**不调用**：后端会回全部 4327 条，页面拿来做首屏下拉既慢又没法用 ⇒ 调用方（`order-pushjdp.ts`）在关键字为空时直接收起下拉。 */
export function fetchPlatformZones(keyword: string): Promise<PlatformZone[]> {
  const q = keyword.trim()
  return request<PlatformZone[]>({
    method: 'get',
    url: PLATFORM_ZONES_URL,
    params: q === '' ? {} : { q },
  })
}

/** 按**游戏**列该游戏在代练通的全部区服（发单页下拉数据源）。契约：`GET platform-integrations/zones/?game_id=<id>` → `[{zone_server_id, server_name}]`（给 `game_id` 时忽略 `q`、按游戏名筛全部区服）。⚠️ 若后端改成独立路径，只改本函数一处。 */
export function fetchGamePlatformZones(gameId: number): Promise<PlatformZone[]> {
  return request<PlatformZone[]>({
    method: 'get',
    url: PLATFORM_ZONES_URL,
    params: { game_id: gameId },
  })
}

/** 区服来源（发单预览的 `zone_source`）：`map` 映射优先 → `first` 字典首条 → `none` 匹配不到。 */
export type PlatformZoneSource = 'map' | 'first' | 'none'

/** 发单预览（`GET /api/business/orders/<order_no>/platform-push/preview/`）。这是页面默认值的**唯一来源**：所有值都是后端**真正将要发送**的那份，⛔ 前端不再自己派生随机账号 / 电话 / QQ（那样页面显示与实际发出去的不一致）。
 * ⚠️ 键名是前后端契约：后端必须返回这 19 个键（改键必须同一次改两边）。`can_push=false` ⇒ 页面显示 `reason` 并**禁用提交**；`zone_source=none` ⇒ 页面要求用户手选；`time_limit_hours` 是总小时数（720 = 30 天）；`requirement` 是实际写进 `Actors` 第五段的文本（模板为空时是占位文案）；`power_level` / `limit_accept` 是本单将要发送的门槛（`null` = 不设置）。 */
export type PlatformPushPreview = {
  can_push: boolean
  reason: string
 /** 订单的游戏 id（F5 拉「该游戏全部区服」用；后端 B8 追加）。 */
  game_id: number
 /** 可以在弹窗里改的提示（价格 <2 / 没价格 / 没区服）——只提示，⛔ 不据此禁用提交。 */
  warnings: string[]
 /** 两位小数字符串；**订单没定价时是 `null`** ⇒ 页面留空 + 要求手填（⛔ 不显示 0.00）。 */
  price: string | null
  ensure1: string
  ensure2: string
  time_limit_hours: number
  zone_server_id: string | null
  server_name: string | null
  zone_source: PlatformZoneSource
  title: string
  game_account: string
  game_password: string
  game_role: string
  current_info: string
  requirement: string
  contact_qq: string
  contact_phone: string
  power_level: number
  limit_accept: number | null
 /** 完整代练内容（客服发单前预览「打手会看到什么」）。⚠️ 后端未返回时按空处理（页面显示「订单没有填写内容」）。 */
  memo?: string
}

/** 发单预览端点（**唯一**的 URL 拼装处）。 */
export function platformPushPreviewUrl(orderNo: string): string {
  return `${orderPlatformPushUrl(orderNo)}preview/`
}

/** 取发单预览（打开页面 / 用户手选区服后各一次）。`zoneServerId` 省略 = 让后端用它自己的默认区服（映射优先 → 字典首条 → none）；传了 = 按用户选的区服重新算一遍（只更新区服与 `can_push`，页面不覆盖用户已改的其它字段）。 */
export function fetchPlatformPushPreview(
  orderNo: string,
  zoneServerId?: string,
): Promise<PlatformPushPreview> {
  const zone = (zoneServerId ?? '').trim()
  return request<PlatformPushPreview>({
    method: 'get',
    url: platformPushPreviewUrl(orderNo),
    params: zone === '' ? {} : { zone_server_id: zone },
  })
}

/* ───────── 单张订单的平台推送行 + 四个平台动作 ───────── */

/** `GET/POST /api/business/orders/<order_no>/platform-push/…` 的**唯一** URL 拼装处。后端：读面 `DailiantongPushListView`、四个动作 `dailiantong_action_views.py`（`urls.py` 的 `order-platform-push*` 四条）。
 * ⚠️ 四个动作都返回**推送行**（与读面同形状）＋ 中文成功 `msg`；**业务失败是 HTTP 200 + `code=1002` + 中文 `msg`** ⇒ `request()` 抛 `BizError`，页面原样展示 `message`（成功就是这一次 POST 过了业务码）。
 * 返回值尾部**带** `/`：四个动作都按 `<读面 URL>withdraw/` 这种形状拼，别手写第二遍单号段。 */
export function orderPlatformPushUrl(orderNo: string): string {
  return `/api/business/orders/${encodeURIComponent(orderNo)}/platform-push/`
}

/** 该单的**全部**推送链路行（后端 `DailiantongPushListView`；顺序 = 新在前）。没有推送行 ⇒ **空数组**（不是 `null`）：调用方据此画「本单尚未发到交易平台」。 */
export function fetchOrderPlatformPushes(orderNo: string): Promise<DailiantongPushRow[]> {
  return request<DailiantongPushRow[]>({
    method: 'get',
    url: orderPlatformPushUrl(orderNo),
  })
}

/** 撤回 / 申请撤销的入参（`POST …/platform-push/withdraw/`）。两个动作共用同一个端点但语义不同（⛔ 页面不许混用）：**未接手**（远端 `Status=11`）⇒ 直接从平台删除（即时退款），行内「从所有第三方交易平台撤回」发 `{ platform }`（逐平台各一发）；**已接手**（`12/13/14/15`）⇒ **申请协商撤销**，「申请撤销」弹窗三格发 `{ pay_level_bal, rep_ensure_bal, comment }`。
 * ⚠️ `pay_level_bal` / `rep_ensure_bal` 是上游 `LevelOrderCancel` 的 `PayLevelBal` / `RepEnsureBal`（**支付代练费 / 赔保证金**，两位小数字符串）；密码（`PayPass`）由**后端**从对接配置里自动带，页面⛔ 不碰、⛔ 不显示。`comment` = 撤销理由（后端 `WithdrawSerializer.comment`，可选、≤500 字）。
 * ⚠️ `platform` 只在逐平台撤回时带（后端当前只有代练通 = 10；服务层 `withdraw_push(order, platform=10)` 已支持按平台选，但**视图暂未透传**）。 */
export type WithdrawPushPayload = {
  comment?: string
  pay_level_bal?: string
  rep_ensure_bal?: string
  platform?: number
 /** ⭐ **协商 1 小时后自动申请客服介入**（弹窗勾选框，默认勾选；业务方口径「协商 1 小时后自动申请客服介入」，弹窗提示「默认 24 小时后自动进入仲裁」）。前端**勾/不勾都要显式发**（`true` / `false`）——⛔ 省略 `false` 会让后端无法区分「用户没勾」与「老客户端没这个字段」。 */
  auto_arbitrate?: boolean
}

/** 从平台撤回 / 申请协商撤销（`POST …/platform-push/withdraw/`）。后端两档语义：**未接手** ⇒ 直接删除并即时退款；**已接手** ⇒ 申请协商撤销（可能被驳回）。页面负责把这两档写进确认文案（见 `order-orderview.ts::WITHDRAW_CONFIRM_TEXT`）。
 * ⚠️ 载荷缺省是**空对象**（订单详情页仍这么调）：`comment` 可选；传 `payload` 时按 `WithdrawPushPayload` 逐键带（⛔ 不要多送别的键）。 */
export function withdrawOrderPlatformPush(
  orderNo: string,
  payload: WithdrawPushPayload = {},
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}withdraw/`,
    data: payload,
  })
}

/** 给平台单**补款**（`POST …/platform-push/amount/`）。后端 `DailiantongAmountView`（`dailiantong_extra_views.py`）：载荷**恰好一个键** `amount`（`AmountSerializer`：**整数元、≥ 1**，代练通不接受小数）；服务层 `add_push_amount` 允许**正在代练（12）/ 待验收（13）**，写前过终态闸 + 余额闸、写后回读 `Price == 原价 + 补款额`；成功 ⇒ `code=0` + **推送行**，业务失败 ⇒ `code=1002` + 中文原样。
 * ⚠️ 「加价」是另一个动作（`up-price/`，只有未接手、收两位小数）——⛔ 别把两者混用。 */
export function addOrderPlatformPushAmount(
  orderNo: string,
  amount: string,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}amount/`,
    data: { amount },
  })
}

/** **重发**一条失败 / 已关闭的推送（`POST …/platform-push/repush/`）。后端 `DailiantongRepushView`：入参**全部可选**（什么都不传 = 按上一条推送行的价格 / 保证金 / 时限原样再抛一次）；⛔ 不绕开五道闸，已有有效推送 ⇒ 中文拒绝。返回**新的**推送行 ⇒ 页面按它的 `state` 判「重发成功 / 失败 / 结果未知」。 */
export function repushOrderPlatformPush(orderNo: string): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}repush/`,
    data: {},
  })
}

/** 改代练通发单价（`POST …/platform-push/price/`，只有**未接手**可改）。 */
export function changeOrderPlatformPushPrice(
  orderNo: string,
  price: string,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}price/`,
    data: { price },
  })
}

/** 增量补时（`POST …/platform-push/time/`；`hours` = 增量，后端 1–720）。 */
export function addOrderPlatformPushTime(
  orderNo: string,
  hours: number,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}time/`,
    data: { hours },
  })
}

/** 发一条平台留言（`POST …/platform-push/message/`；后端 ≤512 字、非空）。 */
export function sendOrderPlatformPushMessage(
  orderNo: string,
  msg: string,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}message/`,
    data: { msg },
  })
}

/* ───────── 平台留言 / 进度图读面 ─────────
 * 后端：`DailiantongMessagesView` / `DailiantongProgressView`（`api/dailiantong_action_views.py` + `urls.py` 的 `order-platform-push-messages` / `order-platform-push-progress`），服务层 `read_push_messages` / `read_push_progress`。
 * ⚠️ **失败语义与写动作不同**：这两个读面**失败不 500**——HTTP 200 + `error` 是中文原因（`messages` / `progress` 为空数组）。⇒ 调用方必须判 `error`：非空 = **读失败**，⛔ 不许当成「没有留言 / 没有进度图」（那正是「静默空」）。 */

/** 一条平台留言（`messages[]` 的元素）。 */
export type PlatformPushMessage = {
 /** 上游原样时间串（⛔ 前端不按自己的格式重排；取不到时后端给空串）。 */
  time: string
 /** 留言正文（上游原值；可能含我方系统标记）。 */
  text: string
 /** 带我方系统标记（接手后发送 / 自动回复）⇒ 页面加「系统」标记。 */
  is_system: boolean
 /** 发布方（上游 `OperationID`；缺失时后端回退 `NickName` / `UID`，都没有 ⇒ 空串）。⚠️ 旧后端可能没有这个键 ⇒ 调用方按空串处理（⛔ 不编）。 */
  sender?: string
 /** 发布人用户 ID（上游原值；可能为空）。 */
  uid?: string
 /** 发布人昵称（上游原值；可能为空）。 */
  nickname?: string
}

/** 留言读面响应；`error` 非空 = 上游读失败（HTTP 仍 200）。⭐ 上游留言 API **没有图片字段**（只有 `MsgType/UID/NickName/CreateDate/Msg/OperationID`）⇒ 后端在同一响应里附 `images`（来自 `LevelOrderProgressList`）；`images_error` 非空 = **图片**读失败（留言本身仍然有效）。 */
export type PlatformPushMessagesResult = {
  messages: PlatformPushMessage[]
 /** 同一单的图片（与 `/progress/` 同源；旧后端没有这个键 ⇒ 按无图处理）。 */
  images?: PlatformPushProgressItem[]
 /** 图片读失败的中文原因（非空 ≠ `messages` 失败）。 */
  images_error?: string
  error: string
}

/** 取平台留言历史（只读；按上游原序）。 */
export function fetchOrderPlatformPushMessages(
  orderNo: string,
): Promise<PlatformPushMessagesResult> {
  return request<PlatformPushMessagesResult>({
    method: 'get',
    url: `${orderPlatformPushUrl(orderNo)}messages/`,
  })
}

/** 一条进度图（`progress[]` 的元素）。 */
export type PlatformPushProgressItem = {
  time: string
 /** 图片说明（上游原值）。 */
  caption: string
 /** 上游原值（对象名或 URL）——只在**排障**时用得到，⛔ 不要拿它当 `<img src>`：对象名没有可达地址。 */
  img: string
 /** 可显示的图片地址：只有上游本来就是 `http(s)` 时才非空。空串 = 我们没有该 OSS 桶的下载签名 ⇒ 页面显示「无预览」，⛔ 不画破图标。 */
  url: string
}

/** 进度图读面响应；`error` 语义同留言（非空 = 读失败）。 */
export type PlatformPushProgressResult = {
  progress: PlatformPushProgressItem[]
  error: string
}

/** 取平台进度图列表（只读；按上游原序）。 */
export function fetchOrderPlatformPushProgress(
  orderNo: string,
): Promise<PlatformPushProgressResult> {
  return request<PlatformPushProgressResult>({
    method: 'get',
    url: `${orderPlatformPushUrl(orderNo)}progress/`,
  })
}

/* ───────────── 第三方平台订单管理页的工具栏批量动作 ───────────── */

/** 工具栏批量动作端点（后端 `PlatformPushActionView`）。**一个端点装 11 个动作**（`action` 分派）：页面 11 颗按钮作用在同一批勾选行上，判据 / 失败语义同形（逐笔判、逐笔回原因）。⛔ 不要拆成 11 条 URL——那会把「批量」的公共部分（单号解析 / 数据权限 / 上限 / 逐笔回显）抄 11 遍。 */
export const PLATFORM_PUSH_ACTIONS_URL = '/api/business/platform-pushes/actions/'

/** 11 个动作名（与后端模块常量**逐字一致**；⛔ 改这里必须同一次改后端）。
 * `withdraw`=批量撤回（未接手删除 / 已接手协商撤销）· `accept`=批量验收（`LevelOrderOver` + 回读 `Status=17`）· `arbitrate`=批量仲裁（**本地**订单日志「待仲裁」，尚未提交平台）· `sync`=同步交易状态 · `change_status`=改系统状态（已抛单且远端未结束 ⇒ 拒绝）· `refund`=记退款（**累计总额**语义）· `set_label`=更改标签（`Order.order_label`）· `set_tag`=改标记 / `clear_tag`=删标记（`Order.tag`）· `urgent`=加急（**本地**标记）· `change_taker_price`=改接单价（既有 `change_price`，写 `Order.total_price`）。 */
export type PlatformPushBatchAction =
  | 'withdraw'
  | 'accept'
  | 'arbitrate'
  | 'sync'
  | 'change_status'
  | 'refund'
  | 'set_label'
  | 'set_tag'
  | 'clear_tag'
  | 'urgent'
  | 'change_taker_price'

/** 一个动作的载荷：`order_nos` 必带，其余键**按动作**带（后端序列化器逐动作校验必填）。`refund_total` 是**累计退款总额**（不是本次新增；后端 `RefundRequestSerializer` 的口径，D-0312）——页面提示语必须写清「填的是累计值」。`request_id` 是退款幂等键（后端 `uniq_order_refund_request_id`，列宽 64）：**每次打开退款弹窗生成一个**，同一批复用 ⇒ 重复点「确定」不会记两笔。 */
export type PlatformPushActionPayload = {
  comment?: string
  target?: string
  refund_total?: string
  reason?: string
  request_id?: string
  label?: string
  tag?: string
  urgent?: boolean
  taker_price?: string
}

/** 一笔的执行结果（⛔ 不含凭据 / 上游详情）。 */
export type PlatformPushActionItem = {
  order_no: string
  ok: boolean
  msg: string
}

/** 一个动作的整批结果：**逐笔回原因**（部分成功要如实显示）。 */
export type PlatformPushActionOutcome = {
  action: PlatformPushBatchAction
  items: PlatformPushActionItem[]
  ok_count: number
  fail_count: number
}

/** 执行一个批量动作。失败分层：**请求形状不对**（空 `order_nos` / 缺必填值 / 未知动作）⇒ 后端业务码 `1000`，`request()` 抛 `BizError`（页面原样显示中文 `msg`）；**单笔业务失败** ⇒ 正常返回、逐笔 `ok=false`（页面按 `fail_count` 决定提示样式）。 */
export function runPlatformPushAction(
  action: PlatformPushBatchAction,
  orderNos: string[],
  payload: PlatformPushActionPayload = {},
): Promise<PlatformPushActionOutcome> {
  return request<PlatformPushActionOutcome>({
    method: 'post',
    url: PLATFORM_PUSH_ACTIONS_URL,
    data: { action, order_nos: orderNos, ...payload },
  })
}

/** 退款幂等键（每次打开退款弹窗生成一个，列宽上限 64）。⚠️ 与 `pages/order-orderlist.ts::newRefundRequestId` **同一套生成策略**（安全上下文用 `crypto.randomUUID()`，否则回落「时间戳 + 随机」）。⛔ 不 import 那个页面模块（两个 island 页各自独立，跨页 import 会把对方的加载图拖进来）⇒ 这里是第二份实现，改动时两边一起改；本页只用它一个出口。 */
export function newPushActionRequestId(): string {
  const cryptoObj = globalThis.crypto as { randomUUID?: () => string } | undefined
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  const stamp = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 10)
  return `push-action-${stamp}-${rand}`
}

/* ── 代练通「有、我们没用上」的动作 ──
 * 载荷都是**恰好**服务端序列化器要的那几个键（⛔ 不多送）；后端会再做前置状态与回读，页面不替它猜状态（点了由后端回中文业务错误，原样展示）。 */

/** 未接手单加价（`POST …/platform-push/up-price/`；`up_price` = **增量**，两位小数）。 */
export function upPriceOrderPlatformPush(
  orderNo: string,
  upPrice: string,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}up-price/`,
    data: { up_price: upPrice },
  })
}

/** 申请客服介入（`POST …/platform-push/arbitration/`；没有入参）。 */
export function requestOrderPlatformPushArbitration(
  orderNo: string,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}arbitration/`,
    data: {},
  })
}

/** ⭐ 两个「后端在加」的写端点（`agree-cancel/` / `cancel-request/`）共用的出口。页面口径：以为端点已经上线来接线；**路由还没上线**时（HTTP 404 / 405）归入「结果未知」⇒ 页面提示 `PLATFORM_ACTION_UNKNOWN_MSG`（「已发出，请刷新核对」），⛔ 不谎报成功、也不把「接口不存在」说成后端的业务拒绝（那是两件不同的事）。业务失败仍是 HTTP 200 + `code=1002` 的中文 `msg` ⇒ 原样抛给页面展示。 */
async function postUnreleasedPlatformAction(url: string): Promise<DailiantongPushRow> {
  try {
    return await request<DailiantongPushRow>({ method: 'post', url, data: {} })
  } catch (e) {
    if (e instanceof HttpError && (e.status === 404 || e.status === 405)) {
      throw new NetworkError('接口尚未上线')
    }
    throw e
  }
}

/** 同意打手的撤销申请（`POST …/platform-push/agree-cancel/`）。上游动作 = `LevelOrderCancel(Flag=2)`（`docs/DAILIANTONG-API.md` §6.2）：**要支付密码**（由**后端**从对接配置里带，页面⛔ 不碰）；同意后按代练通详情里的现值支付代练费 / 赔偿保证金。载荷为空对象（端点不收任何入参）。
 * ⚠️ 后端路由 / 服务层由**另一条线**在加；页面先按本契约接线，缺键时降级（见 `DailiantongPushRow.cancel_initiator` / `agree_cancel_available`）。 */
export function agreeOrderPlatformPushCancel(
  orderNo: string,
): Promise<DailiantongPushRow> {
  return postUnreleasedPlatformAction(`${orderPlatformPushUrl(orderNo)}agree-cancel/`)
}

/** 取消**我们自己发起的**协商撤销申请（`POST …/platform-push/cancel-request/`）。上游动作 = `LevelOrderCancel(Flag=1)`（`docs/DAILIANTONG-API.md` §6.2）：实测**不需要支付密码、不需要金额**，成功 ⇒ `{"Result":1,"Err":"取消撤销成功."}` ⇒ 状态回到 `14/10`（订单回到原状态）。载荷为空对象（端点不收任何入参）。
 * ⚠️ 后端路由 / 服务层由**另一条线**在加；后端未上线时点击按「结果未知」降级（「已发出，请刷新核对」，⛔ 不谎报成功）。 */
export function cancelOrderPlatformPushRequest(
  orderNo: string,
): Promise<DailiantongPushRow> {
  return postUnreleasedPlatformAction(`${orderPlatformPushUrl(orderNo)}cancel-request/`)
}

/** 评价打手（`POST …/platform-push/value/`）。载荷 = `eval_type`（10 非常满意 / 11 满意 / 12 一般 / 13 很差）+ 可选 `comment`；空评价**不送 `comment` 键**（载荷恰好）。 */
export function valueOrderPlatformPush(
  orderNo: string,
  evalType: number,
  comment?: string,
): Promise<DailiantongPushRow> {
  const text = (comment ?? '').trim()
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}value/`,
    data: text === '' ? { eval_type: evalType } : { eval_type: evalType, comment: text },
  })
}

/** 上架 / 下架（`POST …/platform-push/off-sell/`；`flag` 1 上架 / 0 下架）。 */
export function offSellOrderPlatformPush(
  orderNo: string,
  flag: 0 | 1,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}off-sell/`,
    data: { flag },
  })
}

/** 锁定 / 解锁（`POST …/platform-push/lock/`；`locked` 1 锁定 / 0 解锁）。`reason` 是**可选**原因（后端 `LockSerializer.reason`，`required=False` / `allow_blank`，上限 128 字）；页面口径：**没填就不送这个键**（⛔ 不送空串——那等于替后端决定「这次没有原因」）。 */
export function lockOrderPlatformPush(
  orderNo: string,
  locked: 0 | 1,
  reason?: string,
): Promise<DailiantongPushRow> {
  const data: { locked: 0 | 1; reason?: string } = { locked }
  if (reason !== undefined) data.reason = reason
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}lock/`,
    data,
  })
}

/** 拉黑 / 解拉黑打手（`POST …/platform-push/blacklist/`；`flag` 0 拉黑 / 1 解除）。 */
export function blacklistOrderPlatformPush(
  orderNo: string,
  flag: 0 | 1,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}blacklist/`,
    data: { flag },
  })
}

/* ── 详情页补接的五个写动作 ──
 * 后端路由都在（`urls.py` 的 `order-platform-push-finish` / `order-platform-pushes-withdraw-all` / `order-platform-push-edit` / `order-platform-push-modify` / `order-platform-push-screenshot`），服务层分别是 `accept_push` / `withdraw_all_platforms` / `edit_push_order` / `add_push_screenshot`。本段只做**纯加法**：不改上面任何既有导出。
 * ⚠️ 成功 / 失败语义与既有动作一致：成功 ⇒ `code=0` + 数据对象；业务失败 ⇒ HTTP 200 + `code=1002` + 中文 `msg`（`request()` 抛 `BizError`，页面原样展示 `message`）。 */

/** 确认完单（`POST …/platform-push/finish/`）。服务层 = `accept_push`（与第三方平台订单管理页的「批量验收」同一实现）：写前回读 `Status=13`、写后回读 `Status=17`，平台结算后同步本地完成状态。载荷为空对象（端点不收任何入参）。 */
export function finishOrderPlatformPush(orderNo: string): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}finish/`,
    data: {},
  })
}

/** 「从所有平台撤回」里**单个平台**的结果（后端 `withdraw_all_platforms` 的 `results[]`）。 */
export type WithdrawAllPlatformResultItem = {
  platform: number
 /** 平台中文名（后端 `platform_label`）。 */
  platform_label: string
 /** 该平台的推送单号；还没拿到时后端给空串。 */
  serial_no: string
 /** 这个平台撤没撤成（**逐平台**判，⛔ 不是整单成败）。 */
  ok: boolean
 /** 撤回后的本地推送状态（`closed` = 已删除并退款；其余 = 协商撤销已登记）。 */
  state: string
 /** 中文结果 / 失败原因（成功与失败都有，页面逐条展示）。 */
  message: string
}

/** 「从所有平台撤回」的整单结果。⚠️ **部分成功也回 `code=0`**（`succeeded` / `failed` 如实计数）⇒ 页面必须逐平台展示，⛔ 不能只看「请求成功了」就说整单撤回成功（后端服务层注释明确要求）。路径是 `platform-pushes/`（**复数**），与列表页那条同族。 */
export type WithdrawAllPlatformsResult = {
  results: WithdrawAllPlatformResultItem[]
  succeeded: number
  failed: number
  total: number
}

/** 从**所有**平台撤回这张单（`POST …/platform-pushes/withdraw-all/`）。没有推送 ⇒ 后端回 `code=1002` +「这张单没有抛到任何平台…」（页面原样显示，⛔ 不假装成功）。本版只有代练通有客户端 ⇒ 其它平台的推送会逐条回中文原因（`ok=false`）。 */
export function withdrawAllOrderPlatformPushes(
  orderNo: string,
): Promise<WithdrawAllPlatformsResult> {
  return request<WithdrawAllPlatformsResult>({
    method: 'post',
    url: `/api/business/orders/${encodeURIComponent(orderNo)}/platform-pushes/withdraw-all/`,
    data: {},
  })
}

/** 编辑平台订单的入参（后端 `EditPushSerializer`；**只收非金额字段**，金额走 `price/`）。键**全部可选**，但至少要有一项（服务层会挡「什么都没改」并给中文原因）。页面口径 = **只提交用户这次填写的项**（载荷恰好）；留空 = 不提交（不覆盖上游现值）。 */
export type EditPlatformPushPayload = {
  title?: string
  memo?: string
  requirement?: string
  zone_server_id?: string
  groups?: string
  label1?: string
  label2?: string
}

/** 编辑**当前平台**订单（`POST …/platform-push/edit/`）。服务层 = `edit_push_order`（写前只允许「未接手 / 已下架」；写后回读 `Title` / `Require`；金额字段原样回传、⛔ 不在这个入口改钱）。 */
export function editOrderPlatformPush(
  orderNo: string,
  payload: EditPlatformPushPayload,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}edit/`,
    data: { ...payload },
  })
}

/** 全平台编辑订单（`POST …/platform-push/modify/`）。⚠️ `modify/` 与 `edit/` 是**同一个视图**（后端为两条前端线各留一个拼写）；行为逐字相同。后端目前只有代练通一个平台客户端 ⇒ 「全平台」实际只作用于代练通。 */
export function modifyOrderPlatformPush(
  orderNo: string,
  payload: EditPlatformPushPayload,
): Promise<DailiantongPushRow> {
  return request<DailiantongPushRow>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}modify/`,
    data: { ...payload },
  })
}

/** 追加进度截图的结果（后端 `add_push_screenshot`）：`url` 当前恒为空串（没有下载签名）。 */
export type PlatformPushScreenshotResult = {
 /** 对象名（排障用）。 */
  img: string
  caption: string
  url: string
}

/** 给平台单**追加一张进度截图**（`POST …/platform-push/screenshot/`，`multipart/form-data`）。两个字段（后端 `ScreenshotSerializer`）：`image`（必填，后端按**字节魔数**只收 jpg / png，上限 18 MB）；`caption`（可选，本页**只在非空时** append——空 = 不送这个键，让服务层用它自己的默认说明）。
 * ⚠️ `content-type` 不手写：交给浏览器 / axios 带 boundary（手写会把 boundary 写坏）。 */
export function uploadOrderPlatformPushScreenshot(
  orderNo: string,
  image: File,
  caption?: string,
): Promise<PlatformPushScreenshotResult> {
  const form = new FormData()
  form.append('image', image)
  const text = (caption ?? '').trim()
  if (text !== '') form.append('caption', text)
  return request<PlatformPushScreenshotResult>({
    method: 'post',
    url: `${orderPlatformPushUrl(orderNo)}screenshot/`,
    data: form,
  })
}
