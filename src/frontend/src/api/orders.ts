import { AxiosError, type AxiosResponse } from 'axios'
import { http, request, requestEnvelope } from './http'
import { mapHttpStatus, NetworkError } from './errors'
import { unwrap, type Page } from './unwrap'
import type { OrderMoneyField } from '@/config/money'
// ⚠️ **跨 API 模块复用**（本仓已有先例：`api/*.ts` 之间不止一处互引）：形状分流那三件套
// （`isJsonBlob` / `envelopeFromBlob` / `exportFilename`）只有一份实现——
// ⛔ 不在本文件再抄一份（同一事实两处口径，`AGENTS.md` §5-C）。
import { envelopeFromBlob, exportFilename, isJsonBlob } from './performances'
import type { PlatformPushSummary } from './dailiantongPush'

/** 订单号形状：后端 `OrderViewSet.lookup_value_regex = r"\d{20}"`（恰好 20 位纯数字）。 */
const ORDER_NO_RE = /^\d{20}$/

/** 列表页要的字段（后端 `OrderListSerializer` 只回这些）。 */
export type OrderListItem = {
  chat_follow_up_available?: boolean
  order_no: string
 /** **来源单号**（后端 `Order.source_order_no`，非唯一、可为空串）。
  * 列表「订单号」列口径：来源单号非空 ⇒ 显示来源单号，为空 ⇒ 显示系统订单号；「系统内置单号」列恒显示 `order_no`。 */
  source_order_no: string
 /** **订单来源的名字快照**（后端 `Order.source`，字典化后由 `/adjust` 与录单写入）。
  * 历史快照：改名不重拍、字典被删也不重写；管理订单页「订单来源」列改读下面的派生键 `source_name`，旧前端 / 导出仍读本字段。 */
  source: string
 /** **「订单来源」列的显示值**（后端派生键 `source_name`）：挂字典 ⇒ 字典项当前名；没挂字典 ⇒ 回落 `source` 快照。
  * 回落由**后端**做（`apps/business/api/serializers.py::order_source_display_name`，与订单详情同源），⛔ 前端不做第二套兜底。
  * ⚠️ 只有订单列表载荷带它（`OrderSplitListSerializer`）；大厅 / 回收站不带（那两处不显示这一列）⇒ 写成可选。 */
  source_name?: string
 /** **接单人的显示名**（后端 `OrderListSerializer.taker_name`）：昵称优先、空串回落登录账号（`selectors.py::taker_name_expression`），只把空串当没有。
  * 三态：`''` ⇒ 没人接（页面渲染「指派打手」按钮）；名字 ⇒ 有人接；**整值常量 `无权查看`** ⇒ 查看者持 `order.restrict_view_taker`（页面拿它当哨兵渲染纯文本，不进指派界面）。
  * ⚠️ 那是把对标的显示值当 API 值用；前端⛔不自己判那个权限节点——镜像权限判据必然漂移。 */
  taker_name: string
 /** **该单接手人是不是代练通的占位账号**（后端 `OrderSplitListSerializer.taker_is_platform`；占位账号显示名「代练通」）。
  * 「接手人」列据此把文字转紫。后端判据 = `Performance.user.username == 固定占位用户名`，⛔ 不是拿 `taker_name === '代练通'` 判（显示名会随业务口径改）。
  * ⚠️ 可选是刻意的：只有订单列表载荷带它；后端未升级时读到 `undefined` ⇒ 按默认色渲染（⛔ 不炸、不猜）。 */
  taker_is_platform?: boolean
  status: string
 /** FK 的**主键**，不是名字——名字要在前端用 `fetchGames()` / `fetchServers()` 的字典映射。 */
  game: number
  server: number | null
  project_text: string
 /** 非空列（P4-1 起列默认 `0.00`）→ 后端恒回字符串，不会是 `null`。 */
  total_price: string
 /** **累计退款额**（模型 `Order.refund_fee`；列 `null=False default=0.00` ⇒ 后端恒回字符串）。
  * 列表载荷有它（`OrderListSerializer.Meta.fields`）⇒ 管理订单页「退款」列（`refundRow`）直接读；类型必填（后端恒回、非可空列，与 `taker_name` 同形）。 */
  refund_fee: string
  customer_phone: string
  is_urgent: boolean
  created_at: string

 /* ── 下面 10 个只读键 ──
 * 键名是**后端契约**，一个都不能改名。后端判据形状是「**缺键集合为空**」（`test_order_list_columns.py` 顶部注释与 `TEN_KEYS`）——不是「恰好这 10 个」：载荷本来就带既有键，这 10 个是**加法**。
 * `HallListSerializer` 继承同一份 `fields`（大厅载荷跟着多这些键）；财务「待归属」清单（`UnattributedView`）也用 `OrderListSerializer`，是第三个消费方。 */

 /** **角色名**（模型 `Order.game_role_name`，`blank=True` ⇒ 空串，不会是 `null`）。「角色名」列读它。 */
  game_role_name: string
 /** **旺旺/YY**（模型 `Order.customer_ww`，`blank=True`）。「旺旺/来源」列读它（不做对标那条外链）。 */
  customer_ww: string
  /** **新老客标签**（模型 `Order.customer_kind`，录单时快照）：`new` 新客 / `old` 老客 / 空串 = 判不出。
   * 「旺旺/来源」列最左侧的彩色「新/老」标签读它；可选是因为基类 `OrderListSerializer` 被大厅 / 回收站 / 待归属复用，
   * 那几处不带这两个键（只有订单列表的 `OrderSplitListSerializer` 带）。 */
  customer_kind?: 'new' | 'old' | ''
  /** 标签是否由录单时手动指定（`Order.customer_kind_is_manual`）。 */
  customer_kind_is_manual?: boolean
 /** **发内部抢单大厅价**（模型 `Order.hall_price`；列 `null=True` ⇒ **可能是 `null`**）。「发单价」列读它，`null` 走 `money()` ⇒ `0.00`。
  * ⚠️ 被 `order.restrict_view_offer_price` 限制时后端**脱敏成 `null`**（键仍在）⇒ 与「本来就没填价」在载荷上**区分不开**，两种都渲染 `0.00`。
  * ⚠️ 大厅载荷里这个键取的是**生效接单价**（`effective_taker_price`）——同一键名、不同来源，别混。 */
  hall_price: string | null
 /** **该单打手绩效（`Performance.emp_type=MAIN`）是否已结账**。「发单价」列的绿色「结」标记只读它；后端 = `Performance.is_settled` 三字段并集，一次相关子查询注解（`OrderListSerializer.taker_settled`，⛔ 不是逐行查）。
  * ⚠️ 恒为真 `boolean`、不会 `null`（`Exists` 在 SQL 层不会 NULL）：没有 MAIN 行（大厅行 / 未接单 / 历史遗留单）⇒ `false`，语义「没有可结账的打手绩效」。
  * 大厅 / 回收站 / 待归属的取数侧没有那条注解 ⇒ 键在、值恒 `false`（那三处不消费它）。 */
  taker_settled: boolean
 /** **接单销售客服**（模型 `Order.sales_agent`，文本快照，`blank=True`）。「销售客服」列读它。 */
  sales_agent: string
 /** **创建人**（模型 `Order.created_by`，用户名快照，`blank=True`）。 */
  created_by: string
 /** 创建人的**显示名**（后端 `created_by_name`：昵称优先、空串回落账号快照）。
  * 业务方：「录单人」列要显示账号名称（如「小九」），不是登录账号（手机号）⇒ 列读 `created_by_name || created_by`。 */
  created_by_name?: string
 /** **重要备注**（模型 `Order.remark`，`blank=True`）。「重要备注」列读它。 */
  remark: string
 /** **实收款**（派生键，两位小数字符串）：后端调 `settlement_amount.net_received(order)` 后格式化。
  * ⛔ **前端不要自己算这个数**（同一事实只有一处实现，两套算式必然分叉）。非空列 ⇒ 后端恒回字符串。 */
  net_received: string
 /** **发单人**（派生键）：`Order.hall_publisher.username`，没抛过单 ⇒ 空串（「空显无」）。 */
  hall_publisher_name: string
 /** **完单时间**（模型 `Order.completed_at`；列 `null=True` ⇒ ISO 字符串或 `null`）。「完单时间」列读它（默认隐藏）：`null` ⇒ 空串，否则按本页既有形状 `replace('T', ' ').slice(0, 16)` 渲染。 */
  completed_at: string | null
 /** **时间要求**（模型 `Order.time_requirement`，录单页的自由文本，如「2天3小时」）。「时间」列读它——对标的**刻意偏离**（对标读服务端算好的时长描述，见文件头「已知差异 22」）。
  * 按必填写（后端恒回它，与 `refund_fee` / `taker_name` 同形）；后端契约清单见 `tests/test_order_list_columns.py` 的 `TEN_KEYS`。 */
  time_requirement: string

 /* ── 收款状态 / 支付方式 ── */

 /** **收款状态码**（模型 `Order.payment_status`，三档 `UNSETTLED` / `SETTLING` / `SETTLED`，见 `apps/business/models.py` 的 `PaymentStatus`）。
  * 「接单价」列的收款状态标记读它；未知码（含空串）由页面落**默认那一档**（「未」）。 */
  payment_status: string
 /** **支付方式**（模型 `Order.payment_method`，**自由文本** `CharField(max_length=32, blank=True)` ⇒ 空串、不会是 `null`）。
  * 接单价的 `lay-tips` / `title` 里拼「，支付方式：X」那一段；自由文本 ⇒ 进属性前过 `escapeHtml`（本页既有纪律）。 */
  payment_method: string

 /* ── 拆单的两个末尾键（只加在订单列表这条读面上）──
 * 取值口是后端 `OrderSplitListSerializer`（`split_id` = 模型列、`split_members` = `selectors.split_members_expression()` 的 SQL 注解）。键**追加在末尾**是既有纪律（「键集合」网与既有前端都按顺序读载荷）。 */

 /** **拆单组 ID**（模型 `Order.split_id`，`blank=True, default=""` ⇒ 空串）。空串 = 不是拆单（语义开关）；非空 = 同一拆分组的组标识（形状同 20 位数字但**不是订单号**，对外定位一律用 `order_no`）。
  * ⚠️ **显式给 `?split_id=<它>`** 时后端**不折叠**（返回该组全部成员含主单）——本页「展开看子单」的取数口（见 `fetchSplitMembers`）。
  * ⚠️ 可选是刻意的（不是「后端可能不回」）：这两个键**只加在订单列表读面**，大厅 / 回收站 / 待归属**刻意不加**（各有恰好集合键表钉子且不消费）；本类型同时服务大厅行（`HallListItem` 继承）⇒ 只能表达「可能有」。订单列表侧「键恒在」由 `tests/test_order_list_columns.py::SplitKeysAreOrderListOnlyTests` 钉着。 */
  split_id?: string
  split_kind?: '' | 'LEGACY' | 'PEER'
 /** **组内成员总数**（int；**非拆单 = `0`**，后端按**未软删**成员数出）。本页只拿它判「要不要画『拆』徽标 / 展开开关」（`> 1` 才画）。
  * ⚠️ 不受本页当前筛选影响（后端按整组数）⇒「主单被筛掉、子单顶上来」展开后仍能看到全组。可选的同一条理由见 `split_id`。 */
  split_members?: number
 /** The earliest active row in a split group, exposed by the explicit group-members read. */
  split_is_main?: boolean
 /** Balance anchor for PEER and LEGACY groups; unlike split_is_main, applies to both rulesets. */
  split_is_balance_anchor?: boolean
  is_penalty_child?: boolean
  penalty_child_count?: number
  penalty_parent_order_no?: string | null

 /* ── 平台推送读面的只读键 ── */

 /** **这单已抛到代练通**的最近一条推送行摘要；`null` / 缺键 = 没有推送行。
  * 后端 `OrderListSerializer.get_platform_push`：`platform=10`、按 `created_at` 倒序第一条，恰好 6 个只读键（`platform/serial_no/state/remote_status/remote_cancel_status/is_active`）。管理订单列表「订单号」列据此画绿色「通」徽标（`order-orderlist.ts::orderIdRow`）。
  * ⚠️ 可选是刻意的：后端 `fields` 恒有该键（大厅 / 回收站 / 待归属继承处取数侧没预取 ⇒ 值 `null`），但测试夹具与不做徽标的页面不消费它 ⇒ 用「可能有」表达；⛔ 不要把 `null` 与「缺键」区分成两种业务含义（都渲染成什么都不画）。 */
  platform_push?: PlatformPushSummary | null

 /* ── 「陪」「标记」两个徽标要读的两个键 ──
 * ⚠️ 这两个键今天不在 `OrderListSerializer` / `OrderSplitListSerializer.Meta.fields` 里（后端能回它们的读面只有**详情** `GET /orders/<no>/` 与**回收站**）。
 * ⇒ 前端按「可能有」声明：**缺键时两个徽标都不画**（⛔ 不造假徽标、⛔ 不为它逐行拉详情造成 N+1 + 403 噪声）。后端做纯加法后同一个 templet 不用改一行就会开始画；缺口登记在页面文件头「已知差异 66」。 */
 /** **订单类型**（模型 `Order.order_type`，两枚举 `BOOST` 代练 / `COMPANION` 陪玩）。 */
  order_type?: string
 /** **订单标记**（模型 `Order.tag`，自由文本 `blank=True` ⇒ 空串）。「标记」徽标的前两个字符。 */
  tag?: string
}

/** 详情 / 录单 / 流转 / 改价的响应（后端 `OrderSerializer`：`exclude = []`，即模型字段全集 + `allowed_transitions`）。
 * ⚠️ 响应里还会多一个隐式主键 `id`（`Order.order_no` 不是主键，Django 另加了 `BigAutoField`）；本类型是**取子集**，前端一律用 `order_no` 定位（`lookup_field = "order_no"`），不要改用 `id`。 */
export type Order = {
  order_no: string
  status: string
  order_type: string
  game: number
  server: number | null
  category: number | null
  project: number | null
  project_text: string
 /** ⭐ **拆单组 ID**（模型 `Order.split_id`）——空串 = 不是拆单。
  * ⚠️ `OrderSerializer` 是全字段序列化器 ⇒ 详情 / 写端点响应**恒回**这个键（本类型取子集）；⛔ 不可由请求体写（`SERVER_WRITTEN_ORDER_FIELDS`：组标识只由服务端动作生成）。 */
  split_id: string
  split_kind?: '' | 'LEGACY' | 'PEER'
  penalty_parent_order_no?: string | null
  penalty_children?: { order_no: string; status: string }[]
  /** 仅订单详情返回，表示有已确认的代练通发单历史。 */
  has_dailiantong_history?: boolean
  /** 仅订单详情返回，表示已完成订单仍符合拆分的结账条件。 */
  can_continue_split?: boolean
  content: string
  demand: string
  time_requirement: string
  game_account: string
  game_sub_account: string
  game_role_name: string
  current_info_1: string
  current_info_2: string
  customer_phone: string
  customer_qq: string
  customer_ww: string
  /** 新老客标签（录单时快照）与是否手动指定；详情 / 写端点响应恒回这两个键。 */
  customer_kind?: 'new' | 'old' | ''
  customer_kind_is_manual?: boolean
 /** 非空列（P4-1 起列默认 `0.00`）→ 后端恒回字符串，不会是 `null`。 */
  total_price: string
  received_price: string | null
  platform_price: string | null
  hall_price: string | null
  refund_fee: string
  fee: string | null
  payment_method: string
 /** **收款状态**（模型 `Order.payment_status`，三档码 `UNSETTLED` / `SETTLING` / `SETTLED`，见 `apps/business/models.py` 的 `PaymentStatus`；与 `OrderListItem.payment_status` 同一模型列、同一套码，此处是详情 / 写端点响应）。
  * 改收款状态弹窗用该行的它**预勾选**。 */
  payment_status: string
  source: string
 /** 来源字典的主键（`OrderSource`，可空；字典项被删时后端 `SET_NULL`）。
  * ⚠️ 它是数字 id，与 `source`（录单当时的**名字快照**）不是同一件事：统计/去重一律按这个 id，名字只用于显示历史订单。前端**不传**名字——写 FK 时由后端同步写 `source`。 */
  order_source: number | null
  source_order_no: string
  sales_agent: string
  /** 当前客服提成归属的账号主键，历史订单可以为空。 */
  sales_user?: number | null
  is_urgent: boolean
  remark: string
  tag: string
 /** ⭐ **订单标签**（模型 `Order.order_label`，`blank=True` ⇒ 空串）。
  * `OrderSerializer` 是全字段序列化器 ⇒ 详情 / 写端点响应恒回它；写成可选是为了不动既有夹具（`Partial<Order>` 的基线对象不必补键）。
  * ⚠️ 平台推送**列表**读面（`PlatformPushRowSerializer`）今天不回它 ⇒ 列表页那一格要用本键只能点开时 `fetchOrder` 取（见 `order-coreordercenter.ts` 的行内标签）。 */
  order_label?: string
  created_by: string
  updated_by: string
  created_at: string
  updated_at: string
 /** 当前状态**可达**的目标状态（后端只读字段）。
  * ⚠️ 流转弹窗用**流转响应**里的它刷新即可——`OrderSerializer` 同时是流转响应的序列化器，**不要再发一次 GET 详情**。 */
  allowed_transitions: string[]
 /** `allowed_transitions` 里 `COMPLETED` 那一项**是否会被 `/transition/` 拒**（`true` ⇒ 前端选中它时只弹提示、不发请求）。
  * ⚠️ **有意的读/写不对称**：当前非 `COMPLETED` 且调用方持节点 17 时，后端照旧把 `COMPLETED` 列进 `allowed_transitions`（业务方要求下拉里有它），并用本键标出「会被拒」（`OrderSerializer.get_completed_requires_review`）。前端⛔不自己镜像状态机判这件事。 */
  completed_requires_review: boolean
}

export type OrderPage = Page<OrderListItem>

export type OrderSearchParams = {
  status?: string
 /** **收款状态**（三档**码**、单选）。参数名与取值都是我们的口径（对标是 `setSta` + 中文）。
  * ⚠️ 判据是「空值不发」：`''` / `undefined` 由 `toOrderParams` 丢掉（后端对空串回 `1000`）。 */
  payment_status?: string
  /** **新老客**（`new`/`old` 单选；`''` = 全部 ⇒ 不发）。后端按**存储的**标签筛。 */
  customer_kind?: 'new' | 'old'

 /* ── 本批实装的新筛选参数（spec §5.4）──
 * ⚠️ 参数名**逐字**取后端 `OrderSearchSerializer`（别照对标的驼峰 / 缩写写）；判据一律 `is not None`（E-0021）⇒ `0` / `false` 到这一层**不许**当空值丢掉（`toOrderParams` 只丢空串 / `undefined` / `null`）。 */

 /** 订单来源（`Order.order_source_id`；后端 `IntegerField(min_value=1)`）。 */
  order_source?: number | null
 /** ⭐ 公司店铺分组：只留「其来源属于该分组」的单（后端 `order_source__group_id`，`min_value=1`）。
  * 见 `docs/plans/2026-10-26-order-source-groups.md` §4。 */
  source_group?: number | null
 /** 订单类型（后端 `ChoiceField(OrderType.choices)`）。⚠️ 对标 `0`=全部 ⇒ **前端不发**。 */
  order_type?: 'BOOST' | 'COMPANION'
 /** 客服 / 录单人 / 退款记录者的**用户主键**（`IntegerField(min_value=1)`）。 */
  sales_user?: number | null
 /** `sales_user` 的**比法档位**：`0`⇒`sales_user_id`、`5`⇒`created_by`、`10`⇒`refunds__operator`
  * （后两档是**名字文本**匹配的降级，见后端 `selectors.search_orders`）。
  * ⚠️ **单独送它不产生筛选**（与 `sales_user` 合取）。 */
  kefu_type?: '0' | '5' | '10'
 /** 未安排（勾选才发 `true`；语义见对标 `lay-tips`）。 */
  is_leak?: boolean | null
 /** 价格口径五档：`0`⇒接单价 / `1`⇒发单价 / `2`⇒累计退款额 / `3`⇒接发组合 / `4`⇒搜亏本单。后端 `ChoiceField` 收 `0..4`（之外是 `1000`）。
  * `3` 档另用 `hall_price_min` / `hall_price_max`；`4` 档不使用任何金额区间。 */
  show_price_type?: '0' | '1' | '2' | '3' | '4'
 /** 金额下界（**字符串**；后端 `DecimalField` **无 `min_value`**——`0` 与负数都合法，D-0015）。 */
  price_min?: string
 /** 金额上界（同上；对标那个控件名拼错成 `maxPirce`，参数名用我们的）。 */
  price_max?: string
 /** ⭐ `3 接发组合` 档的**发单价**下界 / 上界。与 `price_min` / `price_max` 同款：**字符串**、后端 `DecimalField` **无 `min_value`**。
  * ⛔ **不要**复用主栏那两个参数名去装发单价：`3` 档里 `price_min` 装的仍是**接单价**。 */
  hall_price_min?: string
  hall_price_max?: string
 /** 批量搜单号（逗号分隔的 20 位单号；后端 `RegexField(r"^\d{20}(,\d{20})*$", max_length=4199)`）。
  * ⚠️ 形状不对后端回 `1000`（**不**静默忽略）。 */
  order_nos?: string
  batch_lookup_nos?: string
 /** ⭐ 代练打手（按 `Performance(emp_type=MAIN)` 的用户精确筛；`recType=0` 那一档）。 */
  taker_user?: number | null
 /** ⭐ 发单人（按 `Order.hall_publisher_id` FK 精确筛；`recType=10` 那一档）。 */
  hall_publisher_user?: number | null
 /** ⭐ 按**拆单组 ID** 取该组全部成员；它同时是后端列表读面「本次不做折叠」的开关：给了它 ⇒ 整组回来（含主单），不给 ⇒ 后端把每组折叠成一行（`id` 最小者）。
  * 本页只在**展开**那一发里给（`fetchSplitMembers`），主表取数**从不**带它。 */
  split_id?: string
  group_penalty_children?: boolean
  include_unmatched_split_members?: boolean
  penalty_parent_order_no?: string

  game?: number | null
  server?: number | null
  project_text?: string
  keyword?: string
 /** **布尔**：`false` 必须发出去（后端 `is_urgent is not None` 才按它过滤），所以这里不能当空值丢掉。 */
  is_urgent?: boolean | null
  tag?: string
 /** 带**时区**的 ISO 8601，见 `toLocalIso`。 */
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}

// 成员类型显式写成 `unknown`：`new Set(['', undefined, null])` 会被推断成
// `Set<string | null | undefined>`，于是 `.has(value)`（value 可能是 number/boolean）过不了类型检查，
// 只能在调用处把 value **谎报**成窄类型。`Set<unknown>.has` 接受任意值，这里要的正是纯值比较。
const EMPTY_KEYS: Set<unknown> = new Set(['', undefined, null])

/** 组装检索参数：**空值不发送**（空串 / `undefined` / `null` 一律去掉）。
 * ⚠️ 判据必须是「空」而不是「假值」（E-0021 族）：`is_urgent: false` 与 `0` 是真正有意义的值，真值过滤会把「不加急」静默丢掉；「把 0 当空值静默改写」不该发生在参数组装层，要拒绝就由后端明确拒绝（`page: 0` 由后端分页器回 `1002`）。
 * 抽成纯函数（而非直接交给 axios `params`）：①「空值不发」可单独测、不打桩 http 层；②不赌 axios 对 `undefined` 的内部处理（第三方行为）。 */
export function toOrderParams(params: OrderSearchParams): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {}
  for (const [key, value] of Object.entries(params)) {
    if (EMPTY_KEYS.has(value)) continue
    if (typeof value === 'string' && value.trim() === '') continue
    out[key] = value as string | number | boolean
  }
  return out
}

/** 订单导出端点（字面路径、**不带**尾斜杠；见后端 `apps/business/urls.py`）。 */
export const ORDER_EXPORT_URL = '/api/business/orders/export'

/** 后端取不到 `Content-Disposition` 时的兜底文件名（后缀必须与真实字节一致）。 */
export const ORDER_EXPORT_FALLBACK_FILENAME = 'orders.xlsx'

/** 导出的字节与浏览器要用的文件名。 */
export type OrderExport = { blob: Blob; filename: string }

/** 下载订单导出（`GET /api/business/orders/export`）。
 * ⭐ 不用 `window.open(url)`：同一 URL 成功是 xlsx 文件流、失败（参数畸形 / 超上限 10000 行）是**统一信封 + HTTP 200** ⇒ `window.open` 拿不到响应体，「超过上限请先收窄筛选」这条**可操作的**提示到不了用户眼前。
 * ⚠️ 代价（如实，与绩效导出同款）：这条路径**绕过** `request()` 的 401 处理器——会话过期时用户看到中文提示，但**不会**被自动踢回登录页。 */
export async function downloadOrderExport(params: OrderSearchParams): Promise<OrderExport> {
  let response: AxiosResponse<Blob>
  try {
    response = await http.get<Blob>(ORDER_EXPORT_URL, {
      params: toOrderParams(params),
      responseType: 'blob',
    })
  } catch (e) {
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
    const envelope = await envelopeFromBlob(blob)
    if (envelope) {
      unwrap(envelope)
      throw new NetworkError('导出端点返回了空的成功信封、没有文件内容')
    }
    throw new NetworkError('导出返回的形状无法识别（既不是 JSON 信封、也不是 xlsx）')
  }
  return {
    blob,
    filename: exportFilename(
      response.headers?.['content-disposition'],
      ORDER_EXPORT_FALLBACK_FILENAME,
    ),
  }
}

/** `Date` → **带时区偏移**的 ISO 8601，如 `2026-09-20T00:00:00+08:00`（spec §3 第 5 条）。
 * 为什么带偏移（实测）：裸日期由**服务器的** `TIME_ZONE` 解释日界——后端 `DateTimeField` 会走 DRF `enforce_timezone`、裸日期与带偏移是同一瞬间，但浏览器在别的时区时拿到的日界就不是用户选的那天；`toISOString()` 转 UTC（`Z` 结尾）会把本地午夜写成**前一天 16:00**，字符串日期段是前一天。
 * 所以取本地时间各段、拼上本机偏移，把日界交回用户所在时区。 */
export function toLocalIso(date: Date): string {
  const pad = (n: number): string => String(n).padStart(2, '0')
 // `getTimezoneOffset()` 返回「UTC − 本地」的分钟数，东八区是 -480。
  const offsetMin = -date.getTimezoneOffset()
  const sign = offsetMin < 0 ? '-' : '+'
  const abs = Math.abs(offsetMin)
  const offset = `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${offset}`
  )
}

/** 订单号**形状**校验：20 位纯数字。必须在**发请求之前**做：router 生成的 `orders/{pk}/…` 路由吃 `lookup_value_regex = r"\d{20}"`，形状不对回 **HTML 404** 而非统一信封（`http.ts` 的 `request()` 归成状态码兜底文案），调用方读不到「单号写错了」这个原因。
 * ⚠️ 不是所有订单端点都这样：退款（`…/refunds`）与大厅（`hall/<order_no>/…`）走**显式 `path()` + 宽松 `<order_no:…>` 转换器**（`apps/business/converters.py`），畸形单号照样匹配（实测：已授权 ⇒ 200+业务码；已登录无权限 ⇒ 403+JSON）——对这些端点本函数的作用是**省一次往返**，不是「不拦就没信封」。 */
export function isOrderNoShape(value: string): boolean {
  return ORDER_NO_RE.test(value)
}

/** 形状不对就抛前端自己的错误（**不发请求**）。`Error` 是刻意的：它不是后端错误，别伪装成 `BizError`。 */
function assertOrderNo(orderNo: string): void {
  if (!isOrderNoShape(orderNo)) {
    throw new Error(`订单号格式不正确（应为 20 位数字）：${orderNo}`)
  }
}

export async function fetchOrders(params: OrderSearchParams = {}): Promise<OrderPage> {
  return request<OrderPage>({
    method: 'get',
    url: '/api/business/orders/',
    params: toOrderParams(params),
  })
}

export async function fetchPenaltyChildren(
  parentNo: string,
  filters: OrderSearchParams,
): Promise<OrderListItem[]> {
  assertOrderNo(parentNo)
  const relatedFilters = { ...filters }
  for (const key of ['keyword', 'project_text', 'order_nos', 'batch_lookup_nos'] as const) {
    delete relatedFilters[key]
  }
  return fetchAllOrderPages({ ...relatedFilters, penalty_parent_order_no: parentNo })
}

async function fetchAllOrderPages(filters: OrderSearchParams): Promise<OrderListItem[]> {
  const items: OrderListItem[] = []
  for (let page = 1; ; page++) {
    const result = await fetchOrders({ ...filters, page, page_size: 300 })
    items.push(...result.items)
    if (result.items.length === 0 || result.page * result.page_size >= result.total) return items
  }
}

export async function fetchOrder(orderNo: string): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({ method: 'get', url: `/api/business/orders/${orderNo}/` })
}

/** 取同一拆单组成员；传入筛选时只返回匹配的成员，默认取完整组。 */
export async function fetchSplitMembers(splitId: string, filters: OrderSearchParams = {}): Promise<OrderListItem[]> {
  if (filters.group_penalty_children) return fetchAllOrderPages({ ...filters, split_id: splitId })
  const page = await fetchOrders({ ...filters, split_id: splitId, page_size: 200 })
  return page.items
}

/** ⭐ 订单详情页的「继续拆分订单」：`POST /orders/<单号>/split/`（**推断级设计**：对标入口零抓件，见后端 `OrderViewSet.split` 的 docstring）——当前这单成为组内第一笔、`projects[1:]` 各落一笔新单。
 * ⚠️ 入参是**字符串列表**（不是「含 `/` 的 project_text」）：切分口径只有一处，这一层⛔不再切一遍（两层切分必然漂移）。服务端校验「长度 ≥ 2、每项非空」。 */
export type SplitOrderResult = {
  order_no: string
  split_id: string
 /** 这一发**新建**的行（不含当前单自己）。D-0301 起是**行列表** `{order_no, source_order_no}`（前端展示回退需要两个原值）；当前唯一消费方只读 `created.length`。 */
  created: Array<{ order_no: string; source_order_no: string }>
}

export async function splitOrder(
  orderNo: string,
  projects: string[],
): Promise<SplitOrderResult> {
  assertOrderNo(orderNo)
  return request<SplitOrderResult>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/split/`,
    data: { projects },
  })
}

export type PeerSplitInput = {
  remaining_project_text: string
  source_total_price?: string
  new_orders: Array<{ project_text: string; total_price: string }>
  existing_orders?: Array<{ order_no: string; project_text?: string; total_price?: string }>
}
export type PeerSplitResult = SplitOrderResult & {
  split_kind: 'PEER'
  source_total_price: string
}

export async function splitPeerOrder(
  orderNo: string,
  data: PeerSplitInput,
): Promise<PeerSplitResult> {
  assertOrderNo(orderNo)
  return request<PeerSplitResult>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/split-peer/`,
    data,
  })
}

/** 候选名单的一项（后端每条**恰好** `{"id","name"}`；`name` 口径 = 昵称优先、空串回落账号）。 */
export type Option = { id: number; name: string }

/** `GET /api/business/orders/filter-options/` 的 `data`（spec §5.6）。
 * ⚠️ 键**恰好**这四个（后端 `filter_options` 视图的字面量）。`*_truncated` 为真时被截断的人**在控件里选不到**；订单列表页不弹提示（业务方拍板「不做提示，有搜索即可」），字段保留为线格式契约。 */
export type FilterOptions = {
  takers: Option[]
  publishers: Option[]
  takers_truncated: boolean
  publishers_truncated: boolean
}

/** 候选名单（spec §5.6 / D13）：给「代练打手 / 发单人」一格一份**与筛选同源**的名单。
 * - `game` 可选（不传 = 全租户口径）；`limit` 是**我方自定**参数（默认 200 / 上限 500，**对两份名单各自生效**）；任一份名单为空回**空数组**（不是 `null`）；
 * - ⭐ `search` 可选（2026-11-09：候选改关键词远程搜索；后端按显示名 icase 包含，空串视同未传）——
 *   它是**候选搜索**参数、不是订单列表的检索参数，故不进 `OrderSearchParams`；「空值不发」就地一条
 *   （空白剥掉、空串不带键），⛔ 不为此往 `toOrderParams` 的类型里塞第二语义的同名键；
 * - 权限节点是**本页自己的** `order.list`（⛔ 不复用受 `system_user.list` 门禁的 `/api/accounts/`）。`game` / `limit` 的组装仍复用 `toOrderParams`——「空值不发」这条口径只有一处实现（E-0021 族）。 */
export async function fetchFilterOptions(
  params: { game?: number | null; limit?: number; search?: string } = {},
): Promise<FilterOptions> {
  const { search: rawSearch, ...rest } = params
  const search = rawSearch?.trim() ?? ''
  return request<FilterOptions>({
    method: 'get',
    url: '/api/business/orders/filter-options/',
    params: {
      ...toOrderParams(rest),
      ...(search === '' ? {} : { search }),
    },
  })
}

/** `GET /api/business/orders/kefu-options/` 的 `data`。
 * ⚠️ 键**恰好**这两个（后端 `kefu_options` 视图的字面量）。`items` 每项与 `filter-options` 的 `takers` / `publishers` **同一行形状**（`{id, name}`，`name` = 昵称优先、空串回落登录账号），但**语义不同**：「客服相关人员（内置「客服」+「客服主管」）∪ 当前登录人」，不是「订单里出现过的人」⇒ 后端刻意用**不同的键**（不把两种语义并成一个名字）。
 * `truncated` 为真时被截断的人**在控件里选不到** ⇒ 调用方**必须**把这个事实告诉用户。 */
export type KefuOptions = {
  items: Option[]
  truncated: boolean
}

/** 「销售客服」候选（订单管理页 `#inputOrderKefu` 那一格）。
 * - 权限节点是**本页自己的** `order.list`（⛔ 不复用受 `system_user.list` 门禁的 `/api/accounts/`，也不复用 `assignee-options` 的门——那两个默认「客服」都不持）；`limit` 是**我方自定**参数（默认 200 / 上限 500），本页**不传**（截断文案里的数字就是后端默认值那一个口径）；空档回**空数组**（不是 `null`）。
 * ⚠️ `params` **原样交给 axios**（不过 `toOrderParams`）：入参集（只有 `limit`）与 `OrderSearchParams` 无交集，硬套会被 TS weak-type 检查拦下、只能靠断言绕（同族先例 `fetchAssigneeOptions` 就是直接传）；且这里没有「空值不发」判据要守（`limit` 只有 `undefined` 一种空值，axios 自己丢掉）。 */
export async function fetchKefuOptions(
  params: { limit?: number } = {},
): Promise<KefuOptions> {
  return request<KefuOptions>({
    method: 'get',
    url: '/api/business/orders/kefu-options/',
    params,
  })
}

/** 指派弹层候选名单的一项（后端 `assignee_options` 每条**恰好**这五个键）。
 * `name` = **昵称优先、空串回落登录账号**（与账号列表 `display_name || username` 同源）；`role` = 名称升序首个职位，没有为 `null`；`tag` / `remark` 在账号**没有档案行**时是**空串**（不是缺键）——弹层里那两处 `?? ''` 不是承重逻辑，保留不碍事。 */
export type AssigneeOption = {
  id: number
  name: string
  role: string | null
  tag: string
  remark: string
 /** **提成模式 / 提成值**——录单页快速指派「选中后自动算价」的输入（D-0179）。
  * ⚠️ 可选是因为后端今天不回这两个键（先例：`RecycleBinRow.customer_qq`）；**有意不加**：`assignee-options` 的读面比 `GET /api/accounts/` 宽，塞提成参数等于开绕过 `system_user.restrict_view_commission_params`（`apps/accounts/account_flags.py`，fail-open，失败表现是「提成参数被遮成 `None`」）的旁路。
  * ⇒ 只保留**算式与接线**（`assigneeThrowFee` + 单测 + 真 DOM 用例），线上恒落「没有提成数据」那一支（填 `0`）；裁决与代价的完整论证见实施报告 §③。 */
  commission_mode?: number | null
  commission_value?: number | string | null
}

/** `GET /api/business/orders/assignee-options/` 的 `data`（D-0146 方案②；D-0147 加了第三个键）。
 * 键**恰好** `{"items","truncated","roles"}`；`truncated` 为真时被截断的人**在弹层里选不到**（今天弹层送的上界是 200，超出即截断）。`roles` = 本租户**全部角色名**（内置职位 + 租户自建），与 `GET /api/accounts/roles/` **同源**（`apps.rbac.selectors.role_names`，不是两份实现）；取数口径与 `list_accounts` 一致 ⇒ 不受 `role=…` 筛选影响（筛的是 `items` 不是 `roles`）。
 * ⚠️ **别在这里写死内置职位的个数**：权威清单是 `apps/rbac/seed.py::BUILTIN_ROLE_MODULES`（比 `apps/business/roles.py::DEFAULT_ROLES` 多一个内置「人事」，两者不是同一个数）。⭐ D-0179 再有两个键 `defAssRecManUid` / `defAssRecMan`（默认接手人，见下）。 */
export type AssigneeOptions = {
  items: AssigneeOption[]
  truncated: boolean
  roles: string[]
 /** ⭐ **默认接手人**（D-0179，录单页快速指派）：形状照对标 `GetAllUserByGameName` 回的 `defAssRecManUid` / `defAssRecMan`。
  * ⚠️ 它是**当前操作人自己的**偏好（后端读认证态用户的 `StaffProfile.default_assignee`），换一个客服登录读到的是**他自己**那份；`0` / `''` = 没设（含「没有档案行」那一档），⛔ 不是缺键。 */
  defAssRecManUid: number
  defAssRecMan: string
 /** ⭐ **默认档位是不是「内部抢单」**（业务方裁定 R1/R2）。`true` ⇒ 默认档位是**内部抢单**（下次录单默认选中那一档、价格每次他自己填），此时 `defAssRecManUid` 为 `0`、`defAssRecMan` 为 `''`（后端两列**互斥**）；记着某个人 / 都没设 / 没有档案行 ⇒ `false`。
  * ⚠️ 与上面两键同属「当前操作人自己的偏好」，⛔ 读不到别人的。**有意不抄对标的魔法值 `999999`**（对标把「内部抢单」塞进 `defAssRecManUid` 本身）：用语义化布尔键说同一件事，`defAssRecManUid` 的口径**逐字不变**。 */
  defAssIsHall: boolean
}

/** 指派弹层的候选名单（D-0146 方案②）：与 `GET /api/accounts/` **同一数据范围**，但权限挂在**指派那侧**的节点上——⛔ 不再受 `system_user.list` 门禁（持 `order.assign_taker` / `order.clear_taker` 的「客服」也能拿到名单；旧端点对它回 403）。
 * 参数逐字对齐弹层原来的 `listAccounts({ role, business_type, key, page_size })`，只是分页键从 `page_size` 换成该端点的 `limit`（同值 200）。
 * ⚠️ 与 `listAccounts` 一样把参数对象**直接**交给 `request`（不做空值过滤）：调用方（`fetchAssignCandidates`）只在有值时放键，`0` / `false` 这类合法边界不会被静默丢掉（E-0021）。 */
export function fetchAssigneeOptions(
  params: { role?: string; business_type?: number; key?: string; limit?: number } = {},
): Promise<AssigneeOptions> {
  return request<AssigneeOptions>({
    method: 'get',
    url: '/api/business/orders/assignee-options/',
    params,
  })
}

export type CreateOrderPayload = {
 /** 必填（业务方只定了两项：游戏与 `project_text`）。 */
  game: number
  project_text: string
  server?: number | null
  category?: number | null
  project?: number | null
  content?: string
  demand?: string
  time_requirement?: string
  game_account?: string
  game_sub_account?: string
  game_role_name?: string
  customer_phone?: string
  customer_qq?: string
  customer_ww?: string
  /** ⭐ 新老客标签的手动覆盖（`new` / `old`；缺键或空串 = 不覆盖）。
   * 录单页三段控件只在选了新/老时发这个键。⚠️ 它不是订单列：后端收它的是
   * `CreateOrderAssignSerializer`，订单上的 `customer_kind` / `customer_kind_is_manual` 只读。 */
  customer_kind_override?: 'new' | 'old' | ''
  is_urgent?: boolean
 /** 订单类型：后端 `OrderType` 的两个枚举名（`BOOST` 代练 / `COMPANION` 陪玩）。
  * ⚠️ **两处序列化器都收它**：录单（`OrderSerializer`）与编辑（`OrderUpdateSerializer`）⇒ 管理订单页「改类型」用**同一个** `updateOrder()`（`PATCH …/<no>/`）发 `{ order_type }`，不另造端点（spec §5.2 / C4）。 */
  order_type?: 'BOOST' | 'COMPANION'
  tag?: string
  remark?: string
  source?: string
 /** 来源字典 id。⚠️ **只传 id，不传名字**：后端按 id 把该字典项的 `name` 覆盖写进 `source`，两者同时传时**以 id 为准**；`source` 既有字段不动。没选来源时**不发这个键**。
  * ⚠️ **`null` 有一个明确用途**（编辑态把来源清空时 `order-addorder.ts::onSave` 显式发 `order_source: null`）：不发键等于「不改」，用户看到「清空成功」而后端原样保留旧 FK。录单态不发 `null`。 */
  order_source?: number | null
  source_order_no?: string
 /** **接单销售客服**的显示名快照（模型 `Order.sales_agent`）。
  * ⚠️ 录单页不再发这个键：那一格选的人改走下面的 `sales_user`（**主键**），名字由**服务端**算（两列同源 ⇒ 不会「名字是甲、提成归乙」）。键仍可写（后端收它：该列是「可手填的展示快照」，上下游同步两条路本来就会写），⛔ 但**不要**在录单页加回载荷——会与 `sales_user` 打架，且服务端指定归属时以自己算的名字为准 ⇒ 送它是死载荷。 */
  sales_agent?: string
 /** ⭐ **录单时指定提成归属人**（业务方拍板「换销售客服 = 提成也换人」）。值 = 目标用户的 `auth.User` **主键**（⛔ 不是名字）；**缺键 ⇒ 归属 = 认证态**（`request.user`，即改动前的既有行为）。
  * ⚠️ 权限：只有持 `order.allow_change_sales_agent` 的人能带这个键——不持却带 ⇒ 后端回 **HTTP 403**（⛔ 不是静默忽略：静默会让界面显示甲、提成归录单人，而用户看到"保存成功"）。前端对应判据 `order-addorder.ts` 的 `canChangeSalesAgent`。
  * ⚠️ 它不是订单列：后端收它的是 `apps/business/api/serializers.py::CreateOrderAssignSerializer`，⛔ 不是 `OrderSerializer`（那个模型序列化器的 `sales_user` 仍是只读，⛔ 别放开）。 */
  sales_user?: number
  payment_method?: string
  actor?: string
 /** ⭐ **建单后立即指派给谁**（录单页快速指派，D-0179；`/assign/` 的 `user_id` 同义）。与 `hall_price` 合起来就是业务方裁定的三件套：**发单价 = 大厅价 = 指派价** ⇒ 值走 `hall_price` 列，⛔ 没有第二个金额键。
  * ⚠️ **缺键 = 不指派**（暂不指派）；传了但目标不存在 / 已停用 ⇒ 后端在**建单之前**回业务码 `1000`（一行不写）。 */
  assignee?: number
 /** 「默认指派此人」四态：`'Y'` 记下接手人（要同时给 `assignee`）· `'N'` 清除 · `'hall'` 记下「默认抛内部抢单」（必须同时 `throw_to_hall: true`，否则建单前回 `1000`；只在抛单成功后落库，`1023` 不落；不记价）· 缺键 = 不动。
  * `'Y'` 和「没勾」必须分开，否则每次录单都会发出一条无意义的清除。 */
  remember_assignee?: 'Y' | 'N' | 'hall'
 /** ⭐ **建单之后立刻按 `hall_price` 抛进内部抢单大厅**（录单页弹层「内部抢单」那一档）。与 `assignee` **互斥**（同一个单选组、二选一）：两个键同时给 ⇒ 后端在**建单之前**回业务码 `1000`（一行不写）。缺键 = 不抛。
  * ⚠️ **必须与 `hall_price > 0` 同时给**（前端拦在提交之前、后端再拦一道 `1000`，两层各自独立）：抛进大厅的单没有价就没人能抢，而 `hall_price` 在模型上允许 `0` ⇒ 这道闸不能靠列约束。
  * ⚠️ 抛单那一步在**建单事务之外**（与 `assignee` 指派同一形状）：它失败时订单**不回滚**（后端回 `1023`，`data.order_no` 非空），页面必须如实把「单已建好」说出来——⛔ 别把它并进普通的「保存失败」。 */
  throw_to_hall?: boolean
 /** ⭐ 把这一单按 `/` 拆成 N 笔：`true` 时后端把 `project_text` 按 `/` 切分（**去空白、丢空段**）——N ≤ 1（没写 `/`）⇒ **按普通录单落一笔**、`split_id` 空串、**不报错**；N ≥ 2 ⇒ 落 N 笔（`split_id` 同值，**第 1 笔保留全部价格原值、第 2..N 笔价格全 0**，口径「保证最终接单价不变即可」）。
  * ⚠️ **缺键 / `false` 同义**（布尔开关没有三态）；与 `assignee` / `throw_to_hall` **不互斥**（那些后续动作只作用在**第 1 笔**上）。⛔ 不复刻对标的 `chaifen=Y` 字符串形态：本仓入参走序列化器，这里送真 `bool`。 */
  split?: boolean
  /** 录单时填写的子单，由服务端从原单总价扣除这些接单价并一次建单。 */
  split_orders?: { project_text: string; total_price: string }[]
 /** ⭐ 2026-11-10 客户线（W1）：买家身份键（闲管家 `buyer_eid`，来自「读平台订单」回填）。
  * 非空 ⇒ 后端 upsert 客户档案并连单；缺键 / 空串 = 不建不连（手工录单没有买家标识）。
  * ⚠️ 隐藏透传：录单页**不显示**任何买家标识控件。⛔ 它不是订单列：后端收它的是
  * `CreateOrderAssignSerializer`（`OrderSerializer` 里 `customer` 列只读，⛔ 别放开）。 */
  buyer_eid?: string
 /** 买家昵称快照（随 `buyer_eid` 一起回传；后端「有值则刷新」客户档案的昵称列）。 */
  buyer_nick?: string
} & Partial<Record<OrderMoneyField, string>>

export async function createOrder(payload: CreateOrderPayload): Promise<Order> {
  return request<Order>({ method: 'post', url: '/api/business/orders/', data: payload })
}

/** 改价。**改价只能走这里**：后端 `OrderUpdateSerializer` 把金额字段（`MONEY_FIELDS`）全列为只读，PATCH 载荷里的金额会被**静默忽略**，于是「改了价但没留痕」会以「看起来成功了」的形式出现。
 * 「金额一律走 `changePrice`」这条口径由**类型约束**承载：`UpdateOrderPayload` 用 `Omit` 把金额字段**从载荷类型里摘掉**，谁想把金额塞进 PATCH 都会在编译期被挡住（通用 `updateOrder` 有生产调用方：岛页编辑态 `onSave`）。 */
export type PriceChangePayload = {
 /** 后端 required；**允许负数**（D-0015）。 */
  total_price: string
  reason?: string
  actor?: string
}

export async function changePrice(orderNo: string, payload: PriceChangePayload): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({ method: 'post', url: `/api/business/orders/${orderNo}/price/`, data: payload })
}

/** 改收款状态的载荷。⚠️ **只送这一个键**，且必须送**码**（`UNSETTLED` / `SETTLING` / `SETTLED`）——对标那个弹窗送的是中文 `sta`，中文 → 码的映射在页面侧**一处**常量里做（spec §9 的 V8）；后端是 `ChoiceField`，送中文以业务码 1000 被拒。
 * ⛔ 本载荷**不收** `reason`：对标那个弹窗没有备注输入（spec §5.1）。 */
export type PaymentStatusChangePayload = {
 /** 三档**码**：`UNSETTLED` / `SETTLING` / `SETTLED`（后端 `ChoiceField`）。 */
  payment_status: string
}

/** 改收款状态。与 `/price/` 同形：入参只送这一个键，留痕与写库由后端同事务完成。⛔ 本函数**不收** `reason`（对标那个弹窗没有备注输入，spec §5.1）。 */
export async function changePaymentStatus(
  orderNo: string,
  payload: PaymentStatusChangePayload,
): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/payment-status/`,
    data: payload,
  })
}

/** 换接单销售客服**归属**的载荷（业务方拍板「换销售客服 = 提成也换人」）。
 * ⚠️ **只有一个键，而且是目标用户的「主键」**：⛔ 不送名字——名字由服务端从这个人算出来（`order_writes.change_sales_agent` 与候选 / 录单兜底**同源**的表达式）。送名字会让「甲的名字 + 乙的主键」把 `Order.sales_agent`（展示列）与 `Order.sales_user`（提成归属）**改成两个人**，而两列被同一页读着。 */
export type SalesAgentChangePayload = {
 /** 目标用户的 `auth.User` 主键（后端 `SalesAgentChangeSerializer.user`，`min_value=1`）。 */
  user: number
}

/** 换接单销售客服**归属**（专用端点，⛔ 不是通用 PATCH）。
 * **为什么必须是专用端点**：`Order.sales_user` 决定客服提成的唯一归属，且**不在**通用 PATCH 的写入面里（`OrderUpdateSerializer` 只读）⇒ 想改归属只能走这条**能单独授权、单独留痕**的路（与 `/price/` 同一理由）；后端在**同一个事务**里搬 SALES 绩效行的归属人、同步名字快照、按新人重算金额。
 * ⚠️ 权限门只用 `order.allow_change_sales_agent` 一个节点（⇒ 不持它 **403**）；前端对应门 `order-addorder.ts::SALES_AGENT_PERMISSION_CODE`，两处**同源**（同一个 code 字面量各自写一份，⛔ 不要在前端判「后端会怎么做」）。⚠️ 失败形态与别的写端点不同一档（调用方**必须**分开处理）：被调用时**订单已经保存成功** ⇒ 失败是「订单存了、归属没换」——静默或报成「保存失败」都会让用户误判全成/全没成。 */
export async function changeSalesAgent(
  orderNo: string,
  payload: SalesAgentChangePayload,
): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/sales-agent/`,
    data: payload,
  })
}

/** 改发单价的载荷。⚠️ `price` 是**原样字符串**：直写金额（`"88.5"`、`"0"`，允许负 D-0015）或 `"50%"` 这种**按接单价（`Order.total_price`）比例**的串；后端解析与换算，前端**不做**换算、**不做**上限校验——镜像会漂移（spec §8 第 28 条 / §9 的 V7）。 */
export type HallPriceChangePayload = {
 /** 发单价：金额直写，或 `"50%"` 这种**按接单价（`Order.total_price`）比例**的串。后端解析与换算；前端**不做**换算、**不做**上限校验（镜像会漂移，spec §8 第 28 条）。 */
  price: string
}

/** 改发单价。只写 `Order.hall_price`，不动订单状态（业务方裁定）。 */
export async function changeHallPrice(
  orderNo: string,
  payload: HallPriceChangePayload,
): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/hall-price/`,
    data: payload,
  })
}

/** 编辑订单的 PATCH 载荷：`CreateOrderPayload` **减去金额字段**。三处刻意「不声明」（不是漏写）：
 * - **金额**：后端 `OrderUpdateSerializer` 全列为只读（改价必须走 `/price/` 单独留痕），送出去被静默忽略 ⇒ 类型上就不给写（见 `changePrice` 上方注释）；**`order_no` / `status`** 同样只读（「无论如何都有系统内置单号」、状态只能走 `/transition/`）——类型的 `Omit` 挡不住它们（`CreateOrderPayload` 本来就不含），**不要**把它们加进来；
 * - **审计列**（`created_by` / `updated_by`）：由视图按一条明确规则写入（后端只读字段）。 */
export type UpdateOrderPayload = Omit<CreateOrderPayload, OrderMoneyField | 'split_orders'>
/** Split members can save their price with ordinary fields in one PATCH; the server rejects anchor/non-member price writes. */
export type OrderUpdateInput = Partial<UpdateOrderPayload & { total_price: string }>

/** 编辑订单（编辑弹窗的「保存」必须真提交）。端点是后端**既有**的 `PATCH /api/business/orders/<order_no>/`（`OrderViewSet.partial_update` + `OrderUpdateSerializer`），本函数只是把它的前端调用补上——**不要**另造 `/update/` 一类的第二入口。
 * ⚠️ **载荷是 `Partial`**（PATCH 本来就是部分更新）：编辑弹窗那条路送整份表单，管理订单页「改类型」只送 `{ order_type }`（spec §5.2 / C4）；`Partial` 只放宽「哪些键必填」，**金额仍然进不来**（`UpdateOrderPayload` 已把它们 `Omit` 掉）。
 * 返回的是**改后**的完整订单，调用方不必再发一次详情。 */
export async function updateOrder(
  orderNo: string,
  payload: OrderUpdateInput,
): Promise<Order> {
  return request<Order>(updateOrderRequestConfig(orderNo, payload))
}

/** ⭐ 与 `updateOrder` 同一条请求，但把后端成功信封的 **`msg`** 一并带出。
 * 后端 `partial_update` 保存成功后会做「撤掉该单还挂着的代练通订单」这类副作用，**撤不掉时接口仍是成功码**、只把警告写进 `msg`（「代练通订单未能撤回（原话：…）…」）；只拿 `data` 的 `updateOrder` 会把这句话整个吞掉 ⇒ 客服看到的永远是父页那句固定的「保存成功」。编辑弹窗（`order-addorder.ts` 的 `onSave`）改用本出口，把 `msg` 原样带回去交给父页显示。 */
export async function updateOrderWithMessage(
  orderNo: string,
  payload: OrderUpdateInput,
): Promise<{ order: Order; message: string }> {
  const res = await requestEnvelope<Order>(updateOrderRequestConfig(orderNo, payload))
  return { order: res.data, message: res.msg }
}

/** PATCH 请求配置（`updateOrder` / `updateOrderWithMessage` 共用；⛔ 不两处拼 URL）。 */
function updateOrderRequestConfig(
  orderNo: string,
  payload: OrderUpdateInput,
) {
  assertOrderNo(orderNo)
  return {
    method: 'patch' as const,
    url: `/api/business/orders/${orderNo}/`,
    data: payload,
  }
}

/** 状态流转载荷。⭐ D-0337：「说明原因」可选——弹窗选中问题单 / 已暂停 / 已退单时前端要求填（后端仍宽松可选，≤200、trim），原因经后端落进订单日志的摘要（「…（原因：…）」）与 `reason` 列。
 * ⚠️ 非三状态时**不带这个键**（载荷与旧形状一致，回归钉在前端 spec 里）。 */
export type TransitionPayload = { target: string; actor?: string; reason?: string }

/** 状态流转（取消 = `target: 'REFUNDED'`，**不做物理删除**）。返回的是**流转后**的完整订单，其中的 `allowed_transitions` 已按新状态重算——流转弹窗用它刷新即可，不要为了刷新它再发一次 `fetchOrder`。 */
export async function transitionOrder(orderNo: string, payload: TransitionPayload): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/transition/`,
    data: payload,
  })
}

/** `/assign/` 的载荷（`apps/business/api/serializers.py::AssignSerializer`，spec §5.1 / §4.2）：`user_id` **必填**；⭐ `price` 写 `Order.hall_price`（订单发单价），后端写完价后按「有效基价 × 提成率」**重算** `Performance.temp_amount`（预计绩效不由前端算；门与 `fee` 同一条 `order.submit_payout_price` ⇒ 换键不改变谁能改钱）。
 * `fee` 是**另一件事**：直写 `Performance.temp_amount`（**覆盖**）。后端仍收但指派弹层不再用它（契约清理留待下一批）⇒ 别在新代码里用它；`hours` 非空 ⇒ `Performance.hours`（`min_value=1`）；`project` 非空且非全空白 ⇒ `Performance.project`（空串 / 纯空白回 `1000`）。
 * ⛔ 金额按后端口径是**字符串**（两位小数），不要在这里 `Number()` 化（`'88.50'` 变 `88.5`）。 */
export type AssignOrderPayload = {
  user_id: number
  chat_follow_up?: true
  price?: string
  fee?: string
  hours?: number
  project?: string
}

/** 清除接单人的载荷（spec §5.2）：`Y` ⇒ 额外把两条绩效金额归零；缺省 `N`。 */
export type ClearTakerPayload = { isDelEmp?: 'Y' | 'N' }

/** 指派接单人（`POST /api/business/orders/<no>/assign/`）。
 * ⚠️ 来源态由**状态机**回答（R8 放开后任意非自环来源态都能到「代练中」，`IN_PROGRESS` 那一档后端按 R-3 跳过迁移）；失败时后端回**业务码**，调用方原样显示 `msg`、**不**自己造提示语。
 * ⭐ 指派弹层「发单价」那一格送的是 **`price`**（写 `Order.hall_price`，D-0147），⛔ **不再送 `fee`**（那是直写 `Performance.temp_amount` 的另一件事）。 */
/** 指派请求配置（`assignOrder` / `assignOrderWithMessage` 共用；⛔ 不两处拼 URL / 载荷）。 */
function assignRequestConfig(orderNo: string, payload: AssignOrderPayload) {
  assertOrderNo(orderNo)
  return {
    method: 'post' as const,
    url: `/api/business/orders/${orderNo}/assign/`,
    data: payload,
  }
}

export async function assignOrder(
  orderNo: string,
  payload: AssignOrderPayload,
): Promise<Order> {
  return request<Order>(assignRequestConfig(orderNo, payload))
}

/** ⭐ 与 `assignOrder` 同一条请求，但把后端成功信封的 **`msg`** 一并带出。
 * 后端指派成功后若这单还挂着代练通订单会自动撤；**撤不掉时接口仍是成功码**、只在 `msg` 里带警告；只拿 `data` 的 `assignOrder` 会把这句话吞掉 ⇒ 页面改用本出口，成功提示优先显示 `msg` 原文。 */
export async function assignOrderWithMessage(
  orderNo: string,
  payload: AssignOrderPayload,
): Promise<{ order: Order; message: string }> {
  const res = await requestEnvelope<Order>(assignRequestConfig(orderNo, payload))
  return { order: res.data, message: res.msg }
}

/** 清除接单人（`POST /api/business/orders/<no>/clear-taker/`）。
 * 破坏性动作（订单迁回 `UNASSIGNED`、绩效回退三元组）⇒ 调用方先 `layer.confirm`。节点判据在后端（状态机来源边的节点集）——前端不预判，403 / 业务码都显示后端 `msg`。
 * ⚠️ `payload` 缺省时**整个 `data` 都不带**（不是带一个 `{}`）：后端两种形态都接受，但本仓的载荷判据是**恰好键**（见 `__tests__/orders.spec.ts`）。 */
export async function clearTaker(
  orderNo: string,
  payload?: ClearTakerPayload,
): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/clear-taker/`,
    ...(payload === undefined ? {} : { data: payload }),
  })
}

/** 复制成新订单（`POST /api/business/orders/<单号>/copy`，spec §8.1）。
 * - 路径**无尾斜杠**：它是 `apps/business/urls.py` 里显式 `path()` 注册的那条（与 `/refunds`、`/settlements/settle` 同形），**不是** router 生成的动作别名 ⇒ 别照 `/assign/` / `/clear-taker/` 那样补斜杠。载荷为空（原单号在路径里）；成功回的是**新建出来的那张单**（`OrderSerializer`）。
 * - 复制口径（状态回 `UNASSIGNED`、**不带发单价**、退款额归零、提成归原单销售）全在**后端**，前端**不要**把回来的新单再改成别的样子——那是算错钱的方向（spec §8.2）。权限节点 `order.add_edit`（与录单同一个节点）；原单不存在等失败由后端回业务码，调用方原样显示 `msg`。 */
export async function copyOrder(orderNo: string): Promise<Order> {
  assertOrderNo(orderNo)
  return request<Order>({ method: 'post', url: `/api/business/orders/${orderNo}/copy` })
}

/** 软删一笔订单（`POST /api/business/orders/<单号>/delete/`，spec §5.1）。⚠️ **一次一笔**：管理订单页（`/orders`）的删除走**批量**端点（`batch/delete/`，`order_nos` 数组）；本函数仍是**单号路径**，回收站等页面若仍用它，仍是一次一笔。
 * - 载荷 `{ isDelEmp: 'Y' | 'N' }`（与 `/clear-taker/` 同名同形；后端 `ChoiceField` 默认 `'N'`）：`'Y'` ⇒ 该单的全部绩效行一并写删除标记；`'N'` ⇒ 绩效行一行不动。权限节点 `order.delete`（后端判，没节点回 403）；失败由调用方原样显示后端 `msg`。
 * - 后端是**软删**：行保留、写 `deleted_at` / `deleted_by`，回收站可见（spec §2.1.3）；成功回 `{ order_no, deleted_at, isDelEmp }`（HTTP 200 + `code 0`，信封由 http 层解）。 */
export async function deleteOrder(
  orderNo: string,
  payload: { isDelEmp: 'Y' | 'N' },
): Promise<{ order_no: string; deleted_at: string; isDelEmp: string }> {
  assertOrderNo(orderNo)
  return request<{ order_no: string; deleted_at: string; isDelEmp: string }>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/delete/`,
    data: payload,
  })
}

/** 回收站列表的一行（后端 `RecycleBinListSerializer` = `OrderListSerializer` 的字段 + 两个软删旁证）。这里列的是**页面真正会读的那部分键**（不是 `Meta.fields` 的镜像）——本文件用不到的键不进类型，多声明会在改名时给出**假保证**。
 * ⚠️ 三个易错的点：`deleted_at` / `deleted_by` **只有回收站端点有**（拿 `OrderListSerializer` 去读这两个键是静默空）；该端点底集只含已删单 ⇒ `deleted_at` 恒有值（不是可空列），`deleted_by` 是**用户名快照**字符串；`customer_qq` **不在**列表载荷里 ⇒ 写成可选，页面读不到就留空（「旺旺/来源」列那条 qq 分支因此今天落空，见页面文件头）。 */
export type RecycleBinRow = {
  order_no: string
 /** ⭐ **来源单号**（D-0301）：`OrderListSerializer` 的既有键（载荷早就有、本类型此前漏登记）——回收站「订单号」列按「`source_order_no || order_no`」回退渲染。 */
  source_order_no: string
  status: string
  game: number
 /** FK 主键（名字要在前端用 `fetchServers()` 的字典映射）。 */
  server: number | null
  project_text: string
  game_role_name: string
  game_account: string
  customer_ww: string
 /** 来源的**名字快照**（与 `Order.source` 同一个语义）。 */
  source: string
 /** 可选：本端点今天**不回**它（见类型注释），留给「含微信/QQ 时显示 qq」那条对标分支。 */
  customer_qq?: string
  remark: string
  is_urgent: boolean
  tag: string
  deleted_at: string
  deleted_by: string
 /** 删除人的**账号名称**（昵称优先、空串回落登录账号）。
  * ⚠️ **可选**：部署窗口里「前端新、后端旧」时后端**不回这个键** ⇒ 页面必须回落 `deleted_by`（登录账号快照），⛔ 不是显示空白。`deleted_by` 本身**保留**（加法契约）。 */
  deleted_by_name?: string
}

/** 回收站列表的查询参数（spec §5.2 的 7 个 + 分页）。⚠️ 参数名与类型**逐字**取后端（`RecycleBinSearchSerializer`）：
 * `game` / `order_source` 是 `IntegerField(min_value=1)`；`status` 是 `ChoiceField(OrderStatus.choices)` ⇒ 送**枚举码**（`COMPLETED` 这类）不是中文；`deleted_from` / `deleted_to` 语义是**删除时间**（不是录单时间，spec D6-②——对标在这页复用订单列表的 `stime`/`etime` 拼写、语义却变了 ⇒ 我方用自描述名）；`keyword` / `game_account` 空值不发（本仓既有口径）。 */
export type RecycleBinSearchParams = {
  game?: number | null
  status?: string
  deleted_from?: string
  deleted_to?: string
  order_source?: number | null
  keyword?: string
  game_account?: string
  page?: number
 /** ⚠️ 只允许 10 / 20 / 30（逐字取对标 `orderrecycle.js:174-176` 的 `limits`；后端同值）。 */
  page_size?: number
}

/** 从回收站恢复一笔订单（`POST /api/business/orders/<20位单号>/restore/`，spec §5.3）。
 * ⚠️ **一次一笔**：对标的参数是逗号串（它那条路支持批量），我方页面约定一次一笔（spec §5.5-#1）——批量的拦截在**调用方**，本函数只送单号路径。权限节点 `order.recycle`（没节点 403）；成功回 `{order_no}`，失败由调用方原样显示后端 `msg`。 */
export async function restoreOrder(orderNo: string): Promise<{ order_no: string }> {
  assertOrderNo(orderNo)
  return request<{ order_no: string }>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/restore/`,
  })
}

/** 设置累计退款额（`POST /api/business/orders/<单号>/refunds`，spec §5.1 / §7）。⭐ D-0312 口径：`refund_total` 是**累计退款总额（绝对值）**、不是「本次要退的额」——后端把 `Order.refund_fee` **覆盖**成这个值（行锁内读改写），台账 `OrderRefund` 记**带符号差额**（`kind=SET`）⇒ 行内单笔入口预填当前累计（`data-fee`）、工具栏批量入口**一律开空**（每笔各设成这个累计额，见 `order-orderlist.ts`「已知差异 15/17」），⛔ 别按旧「本次增量」口径解释这个框。`refund_fee` 详情与列表两侧都有（见 `OrderListItem.refund_fee`）。
 * ⚠️ **差额为 0 时后端是全空操作**（不建行、不写审计、不改列）⇒ 响应 `data` 是 `{order_no, refund_fee, changed: false}`，调用方照「成功」处置。`request_id` 是幂等键（后端 `uniq_order_refund_request_id`）：**真要写**时同键第二次提交回业务码 **`1010`**（不产生第二笔；差额 0 不 INSERT ⇒ 「设成同值」重提**不会**回 1010）——调用方必须**每次打开弹窗生成一个、在弹窗内固定复用**（`order-orderlist.ts` 的 `newRefundRequestId()`），⛔ 每次提交现生成等于把幂等保护关掉；拿到 `1010` ⇒ 刷新列表并关掉弹窗（这一笔已在服务端落过账，见 `submitRefund`）。
 * 失败形状（全部由调用方原样显示后端 `msg`，见弹窗 `submitRefund`）：`1007` 状态不允许 / `1008` 累计额非法（负数或超过订单总价）/ `1009` 客服提成率未配 / `1010` 幂等键重复 / `1000` 订单不存在或不可见（含**缺 `refund_total`** 的旧前端）/ `401` 未认证 / `403` 无 `order.online_refund`。⚠️ 本端点是**显式 `path()` + 宽松的 `<order_no:...>` 转换器**（`apps/business/converters.py`）而非 router 那条 ⇒ 畸形单号实测回 **200 + 业务码**而非 HTML 404；`assertOrderNo` 在这里拦一道的作用是**省一次往返**、与本页其它写动作约定一致，**不是**「不拦就没有后端 `msg`」。 */
export type RefundRequestPayload = {
 /** **累计退款总额**（字符串，最多两位小数）；`>= 0` 与 `<= 订单总价` 由**后端**判（`1008`）。 */
  refund_total: string
 /** 退款原因（必填、≤255 由**后端**判；列宽 255）。 */
  reason: string
 /** 幂等键（≤64；同一个键在真要写时重复提交回 `1010`）。 */
  request_id: string
}

/** 一条退款记录（后端 `RefundSerializer`；`GET /orders/<单号>/refunds` 与 POST 的 `data` 同形）。 */
export type OrderRefundRecord = {
  id: number
  order_no: string
 /** `OrderRefund.Kind`：`SET`（设置累计额，金额是**带符号差额**）/ `PARTIAL` / `FULL` / `REVERSAL`（后三个是历史行：旧口径的本次退款额 / 全额退款 / 退单回池冲销）。 */
  kind: string
 /** 带符号金额：`SET` 行是差额（可能为负），原样上屏、⛔ 不取绝对值。 */
  amount: string
  reason: string
  operator: string
  request_id: string
  created_at: string
}

/** 差额为 0 时的成功响应（后端全空操作，没有退款记录行）。 */
export type RefundNoopResult = {
  order_no: string
  refund_fee: string
  changed: false
}

export async function partialRefund(
  orderNo: string,
  payload: RefundRequestPayload,
): Promise<OrderRefundRecord | RefundNoopResult> {
  assertOrderNo(orderNo)
  return request<OrderRefundRecord | RefundNoopResult>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/refunds`,
    data: payload,
  })
}

export type ParsedFields = {
  game_account?: string
  game_sub_account?: string
  game_role_name?: string
  customer_phone?: string
  customer_qq?: string
 /** 金额字段回的是**两位小数字符串**（后端 `/parse/` 出口转换过），如 `'-199.50'`。 */
  total_price?: string
  received_price?: string
 /** 名称类字段：需要前端在选项字典里找对应项，找不到就不填（见 Task 5）。 */
  game_name?: string
  server_name?: string
  project_text?: string
 // —— 以下 5 个是**配置驱动的识别项**产物（录单识别设置页，T8 起真正回填）——
 /** 当前信息1（表单里本来就有这一格，同名直写）。 */
  current_info_1?: string
 /** 当前信息2（同上）。 */
  current_info_2?: string
 /** 旺旺/YY（同名直写）。 */
  customer_ww?: string
 /** 来源单号：填进表单后点亮录单页「读平台订单」的触发点 ③（见 `order-addorder.ts`）。 */
  source_order_no?: string
 /** 订单来源的**名字字符串**（不是 id）。⚠️ 要按**精确同名**在来源字典（`dicts.orderSources`）里找 id，再写 `form.order_source`；找不到就进 `unresolved`（与游戏 / 区服同口径，**不静默**）——见 `views/orders/orderFormFields.ts::pasteFillTargets` / `applyParsedToForm`。 */
  source?: string
}

export type ParseResult = {
  fields: ParsedFields
 /** 解析出来的字段名（后端字段名，不是中文）。 */
  matched: string[]
 /** **没**解析出来的字段名——它也要给客服看，但不能当「错误」（spec §4.1）。 */
  unmatched: string[]
}

/** 粘贴解析。**不建单**：结果回给前端填进表单，允许人工修正。 */
export async function parsePastedText(content: string): Promise<ParseResult> {
  return request<ParseResult>({
    method: 'post',
    url: '/api/business/orders/parse/',
    data: { content },
  })
}

/** 「查看游戏账号密码及详情」的**审计动作码**（`apps/audit/actions.py`）。⚠️ 它**不是**权限节点；放在 `api/` 层是**有意的**：岛页目录里出现的「带点」字面量会被 `src/tests/test_rbac_menu_alignment.py` 的源 3 当成「声明的权限点」并当场变红（见 `docs/ERRORS.md` 的 E-0187）。 */
export const VIEW_CREDENTIALS_ACTION = 'order.view_credentials'

/** `/audit/` 返回的 `action` 取值。**刻意不是窄联合**：后端出口是 `OperationLog.objects.filter(order_no=…)`，这一单上挂过什么动作就返回什么（`order.transition` / `order.change_taker_price` / `performance.review` / `order.create` / `order.claim` / `order.throw_to_hall` / `order.withdraw` / `order.clear_taker` / `order.update` / `performance.assign` / `performance.reassign` / `performance.submit` / `order.view_credentials` …含迁移进来的历史行），写成两三个成员的联合就是在类型上撒谎。
 * 想给短中文标签的那几个 code 见 `views/orders/orderFilters.ts` 的 `AUDIT_ACTION_LABELS`；表外的动作名回退成 raw code（`auditActionLabel` 的既有行为）。⚠️ `main` 曾同期把本类型写成窄联合 `'STATUS_CHANGE' | 'PRICE_CHANGE'`（旧表取值），**被刻意丢弃**：窄联合会让 `'order.view_credentials'` 这类真实取值 typecheck 报错（`api/__tests__/orders.spec.ts:170` 正钉着它）。 */
export type AuditAction = string

/** 录单页「读平台订单」的出口（spec §6.2 + per-source spec §5）。
 * ⚠️ **金额的单位是元、已经是两位小数字符串**：后端把上游的分用 `Decimal(v) / 100` 换算过（D6），`data.payment` 就是我们直接写进 `#price` 的值——**前端一次换算都不要做**（再乘/除 100 都会把价格算错，而页面上看不出来）。 */
export type PlatformOrderFields = {
  payment: string
 /** 买家昵称（进 `customer_ww`，D5 的语义漂移已登记）。 */
  buyer_nick: string
 /** ⭐ 2026-11-10 客户线：买家身份键（闲管家 `buyer_eid`，录单时随创建载荷回传建客户档案）。
  * ⚠️ **可选**是刻意的：阿奇索那条腿**没有**这个键（那边没有买家唯一标识）⇒ 缺键 = 无身份，
  * 页面据此清掉暂存的买家（换单号重读时不能拿上一单的身份）。 */
  buyer_eid?: string
  customer_phone: string
  remark: string
  order_status: number
  pay_time: number
 /** 后端**判定出的**订单来源 id（per-source spec §5）。
  * ⚠️ 可能是 `null`（那时前端**不动**下拉）；手动覆盖时它就是用户选的那个（回填等于幂等）。 */
  order_source: number | null
 /** 上面那个来源的名字（自动判定刚建的来源**不在**首屏字典里，靠它补进下拉）。 */
  order_source_name: string
 /** 实际走的是哪条腿：`"agiso"`（阿奇索）/ `"xgj"`（闲管家）——排障与分流用。 */
  platform: string
}

/** 读平台订单（`POST /api/business/read-platform-order/`）：**不落库**，只回给前端填表（与 `POST /orders/parse/` 完全同形，D4）。
 * 两种用法（per-source spec §6）：传了 `orderSourceId` ⇒ 手动覆盖（必须传数字 id，不能传名字）；传 `null` ⇒ **自动判定**（后端按单号逐个试已配置的闲管家应用）——此时**不发 `order_source` 键**（"没给"就是自动判定的判据）。
 * 失败回 **HTTP 200 + 业务码**（`1015` 没配店铺 / `1016` Token 失效 / `1017` 上游其它失败），`request()` 抛成 `BizError`，调用方按对标**静默降级**处理（spec §2.5）；走既有 `request<T>()`——传输、认证与信封解包都在那一层，本文件不另造 HTTP 层（与 `api/orderSources.ts` 同形）。 */
export async function readPlatformOrder(
  orderSourceId: number | null,
  tid: string,
): Promise<PlatformOrderFields> {
  const data: { tid: string; order_source?: number } = { tid }
 // ⚠️ 判据用 `!== null` 而不是真值：来源 id `0` 是一个**给了的值**（只是查不到配置 ⇒ 1015），
 // 不该被当成"没给"而滑进自动判定（AGENTS.md §5 / E-0021）。
  if (orderSourceId !== null) data.order_source = orderSourceId
  return request<PlatformOrderFields>({
    method: 'post',
    url: '/api/business/read-platform-order/',
    data,
  })
}

/** 留痕一行（后端 `/audit/`，数组按时间**倒序**）。`from` / `to` 是后端原样的**键名**（别改成 TS 关键字）；值是**值**不是字段名：状态行是中文状态 label（`未分配`）、改价行是十进制字符串（`-199.50`，可为负，D-0015）。`id` / `summary` / `log_type` 三个键是必填；字面量夹具在 `layui-island/pages/__tests__/order-orderview*.spec.ts`（`api/__tests__/orders.spec.ts` 走 `mockedRequest`、泛型被擦掉，不吃这个类型）。 */
export type AuditEntry = {
  action: AuditAction
  actor: string
  reason: string
  from: string
  to: string
 /** ⚠️ **UTC 的 ISO-8601（尾部 `+00:00`）**，**不是**本地墙钟——与兄弟端点 `GET /api/audit/logs/` 的 `+08:00` **口径不同**（那边走 DRF `DateTimeField`，这边是 `row.created_at.isoformat()` 模型直读）。
  * ⛔ 展示前必须过 `layui-island/pages/order-orderview.ts::formatLogTime` 那支格式化函数，**别**照 `api/auditLogs.ts::formatCreatedAt` 直接切字符。 */
  created_at: string
 /** 行主键（`OperationLog.pk`）。`created_at` 会撞，前端用它做 key / tie-break。 */
  id: number
 /** **服务端已渲染好的整句「操作说明」**（`OperationLog.summary`）。⛔ 前端**不许自己拼句子**（spec §5.9）——它是「操作说明」的唯一渲染出口。 */
  summary: string
 /** 8 值分类 code（对标 `logType`；本页今天不渲染，便于将来分档）。 */
  log_type: number
 /** 操作人的**显示名**（昵称，空则回落登录账号）——审计日志线；`actor` 仍是**登录账号**（仅供 `?actor=` 过滤与身份追溯），展示面一律取本键。
  * ⚠️ **可选**（`?:`）不是偷懒：部署窗口里「前端新、后端旧」时后端**根本不回这个键**（前后端一起发版才有），页面必须能回落 `actor` 而不是显示 `undefined`。 */
  actor_display?: string
}

export async function fetchOrderAudit(orderNo: string): Promise<AuditEntry[]> {
  assertOrderNo(orderNo)
  return request<AuditEntry[]>({
    method: 'get', url: `/api/business/orders/${orderNo}/audit/`, params: { scope: 'business' },
  })
}

/** 「添加订单日志」（订单详情页「添加」框）：`POST /api/business/orders/<单号>/notes/`，body `{content}`。
 * 后端契约：写一行 `OperationLog`（动作 `order.note`，summary 模板「添加订单日志：<内容>」、内容进 `reason`），权限节点 **`order.add_log`（27）**；返回**新建的那一行**（与 `/audit/` 同形、同 9 键）⇒ 「添加成功就地 prepend」不必再取一次数。
 * ⚠️ **`content` 原样发（含首尾空白）**：后端 `OrderNoteSerializer` 显式 `trim_whitespace=False` + `validate_content`（纯空白才拒 ⇒ 业务码 `1000`）⇒ 前端**不要**在这里 trim；空 / 纯空白的拦截在**页面侧**（对标 `layer.msg('备注内容不能为空！')`、零请求）。失败（无 27 节点 **403** / 内容超 255 / 订单不存在 ⇒ `1000`）由 `request()` 抛成 `BizError` / `HttpError`，`message` 是后端 `msg` 原文——调用方原样显示，⛔ 不自造文案。 */
export async function createOrderNote(orderNo: string, content: string): Promise<AuditEntry> {
  assertOrderNo(orderNo)
  return request<AuditEntry>({
    method: 'post',
    url: `/api/business/orders/${orderNo}/notes/`,
    data: { content },
  })
}

/* ── 批量出口（spec §4.4 / §D2/D3/D9/D10/D13）：9 条端点都是 `POST /api/business/orders/batch/<动作>/`
 * （`OrderViewSet` 上的 `@action(detail=False, methods=["post"], url_path="batch/<动作>")`，见 `apps/business/api/order_views.py`）。本层只做两件事：① 把「动作名 → 路径段」拼对；② 把 `order_nos` 逐个过一遍与单笔**同一判据**的形状闸。
 * ⛔ **业务判据不在这里**：「哪些笔能改」由后端 `dry_run` 回答（全局约束第 7 条）。 */

/** 批量端点里的一个**不合格项**（后端逐笔给 `code` + `msg` 原文；文案一律原样显示）。 */
export type BatchIssue = {
  order_no: string
 /** ⭐ D-0301：结果弹窗按「`source_order_no || order_no`」回退显示；取不到行的 issue 是空串（后端占位）——写成可选是为了兼容老后端载荷。 */
  source_order_no?: string
  code: number
  msg: string
}

export type BatchEffect = {
  order_no: string
  source_order_no: string
  operation: string
}

/** 批量端点的统一响应 `data`（spec §D2；`order_batch.py::run_batch` / `transition_targets`）。
 * `dry_run: true` ⇒ `applied` 恒 `[]`、**一行都不写**；`eligible` / `ineligible` / `applied` 一律**按请求里 `order_nos` 的顺序**（§D9）⇒ 前端按位置对齐，不要自己排序。
 * `targets` / `per_order` **只有**改状态的**读取形态**（不传 `target`）才有（`order_batch.py:88`）⇒ 类型上可选。 */
export type BatchOutcome = {
  dry_run: boolean
  eligible: string[]
  ineligible: BatchIssue[]
  applied: string[]
  effects?: BatchEffect[]
 /** 读取形态专有：这批单可选目标的**并集**（按 `OrderStatus.choices` 声明顺序）。 */
  targets?: string[]
 /** 读取形态专有：每笔自己的可选集。 */
  per_order?: Array<{ order_no: string; allowed: string[] }>
}

/** 9 个批量动作的**路径段**（唯一出处；⛔ 调用方不许自己拼 URL）。 */
export type BatchAction =
  | 'assign'
  | 'transition'
  | 'types'
  | 'price'
  | 'hall-price'
  | 'payment-status'
  | 'refunds'
  | 'delete'
  | 'delete-v2'
  | 'recompute-commission'

/** 动作名 → 路径段。 */
const batchUrl = (action: BatchAction): string => `/api/business/orders/batch/${action}/`

/** 批量出口的**唯一实现**：先按两档把 `order_nos` 过一遍形状闸，再发一次 POST，body 原样透传。
 * ⚠️ **闸分两档**（口径与理由见 `docs/DECISIONS.md` 的 D-0160）：`delete` ⇒ 只要求「非空字符串」（后端 `parse_order_nos` 自己的契约，用于清理存量垃圾行）；其余 8 个动作 ⇒ 与单笔**同一判据**（20 位闸）。这是一处**有意的不对称**，⛔ 别读成漏改。形状不对时**一次请求都不发**（抛的是前端自己的 `Error`，不是后端错误）。
 * ⚠️ 第二参数类型收紧成 `{order_nos: string[]} & Record<string, unknown>`：原 `object` + `?? []` 会让「缺 `order_nos`」**静默跳过形状闸、请求照发** ⇒ 缺了就**响亮失败**（先判数组抛 `TypeError`，比 `for...of undefined` 的裸 TypeError 更好定位），⛔ 不把缺失当空数组。9 个薄包装的类型都保证了 `order_nos` 存在。 */
export async function batchOrderAction(
  action: BatchAction,
  payload: { order_nos: string[] } & Record<string, unknown>,
): Promise<BatchOutcome> {
  if (!Array.isArray(payload.order_nos)) {
    throw new TypeError(
      `batchOrderAction(${action}): payload.order_nos 必须是字符串数组，收到 ${String(payload.order_nos)}`,
    )
  }
 // 形状闸分两档：delete 只要求非空字符串，其余动作走 20 位单号闸（口径与理由见 docs/DECISIONS.md 的 D-0160）。
 // 批量路径其实不需要 `\d{20}`：单号在**请求体**里，后端自己的契约只是「非空字符串」（`order_batch.py::parse_order_nos`），取不到的行进 per-item issue（`ineligible`）——`\d{20}` 闸的存在理由是**详情路由**的 `lookup_value_regex`。只放开 `delete` 是**有意的不对称**：那条路的目的正是「把请求发出去」（清理存量垃圾行），前端拦一道省掉的往返恰好是唯一要做的事 ⇒ 取舍翻转；其余动作与单笔路径一字不动（详情真的会落 404 HTML，闸有正确性含义），⛔ 别读成漏改。
 // `delete` 档只把判据降成「非空字符串」，⛔ 不新造第二套提示语——空串 / 非字符串照样由 `assertOrderNo` 响亮失败（同一句文案在 `api/hall.ts` 的 `assertHallOrderNo` 与 `api/myOrders.ts` 的 `submitMyOrder` 内联另各有一份历史副本，⛔ 不声称这里「唯一」）。
  for (const orderNo of payload.order_nos) {
    if ((action === 'delete' || action === 'delete-v2') && typeof orderNo === 'string' && orderNo !== '') continue
    assertOrderNo(orderNo)
  }
  return request<BatchOutcome>({ method: 'post', url: batchUrl(action), data: payload })
}

/** `batch/assign/`（spec §4.1 第 1 行）：载荷 = 单笔 `AssignOrderPayload` + `order_nos` / `dry_run`。 */
export type BatchAssignOrdersPayload = AssignOrderPayload & {
  order_nos: string[]
  dry_run: boolean
}

export function batchAssignOrders(payload: BatchAssignOrdersPayload): Promise<BatchOutcome> {
  return batchOrderAction('assign', payload)
}

/** `batch/transition/` 的载荷：**只有** `order_nos` / `dry_run` / `target` 三个键（后端 `BatchTransitionSerializer` 是独立序列化器，⛔ 不收 `actor`——那条请求不读也不校验它）。
 * ⚠️ `target` **可缺省**（或缺省成空串）= spec §D10 的**读取形态**：只回可选目标、一行不写。写形态走 `batchTransitionOrders`，读取形态走 `fetchBatchTransitionTargets`。 */
export type BatchTransitionOrdersPayload = {
  order_nos: string[]
  dry_run: boolean
  target?: string
}

export function batchTransitionOrders(
  payload: BatchTransitionOrdersPayload,
): Promise<BatchOutcome> {
  return batchOrderAction('transition', payload)
}

/** `batch/types/` 的载荷（spec §9-R7）：后端 `BatchOrderTypeSerializer` **只放 `order_type`** ⇒ 批量**不得**成为「改订单别的字段」的第二条写通道。
 * ⚠️ `order_type` 取 `CreateOrderPayload` 的**同名枚举**（`NonNullable` 去掉可选）——与后端 `OrderType.choices` 同源；写死 `'BOOST' | 'COMPANION'` 会在枚举扩值时静默过期。 */
export type BatchChangeTypesPayload = {
  order_nos: string[]
  dry_run: boolean
  order_type: NonNullable<CreateOrderPayload['order_type']>
}

export function batchChangeTypes(payload: BatchChangeTypesPayload): Promise<BatchOutcome> {
  return batchOrderAction('types', payload)
}

/** `batch/price/` 的载荷：单笔 `PriceChangePayload`（`total_price` 必填、允许负数 D-0015）+ 两个批量键。 */
export type BatchChangePricesPayload = PriceChangePayload & {
  order_nos: string[]
  dry_run: boolean
}

export function batchChangePrices(payload: BatchChangePricesPayload): Promise<BatchOutcome> {
  return batchOrderAction('price', payload)
}

/** `batch/hall-price/` 的载荷：单笔 `HallPriceChangePayload` + 两个批量键。
 * ⚠️ `price` 的 `%` 档按**每笔自己的**接单价折算（分母逐笔不同）——那在后端做，前端⛔不换算。 */
export type BatchChangeHallPricesPayload = HallPriceChangePayload & {
  order_nos: string[]
  dry_run: boolean
}

export function batchChangeHallPrices(
  payload: BatchChangeHallPricesPayload,
): Promise<BatchOutcome> {
  return batchOrderAction('hall-price', payload)
}

/** `batch/payment-status/` 的载荷：单笔 `PaymentStatusChangePayload`（只送 `payment_status` 码）+ 两个批量键。 */
export type BatchChangePaymentStatusPayload = PaymentStatusChangePayload & {
  order_nos: string[]
  dry_run: boolean
}

export function batchChangePaymentStatus(
  payload: BatchChangePaymentStatusPayload,
): Promise<BatchOutcome> {
  return batchOrderAction('payment-status', payload)
}

/** `batch/refunds/` 的载荷（spec §D13）：单笔的 `request_id` **换成 `request_ids`**——每笔一个幂等键，**长度必须与 `order_nos` 相等、按下标对齐**（后端 `BatchRefundSerializer` 已用 `get_fields()` 把继承来的单笔 `request_id` 摘掉）。
 * `refund_total` / `reason` 仍是**整批一份**（口径 = 每一笔各**设置**成这个累计额，spec §5.4）；⚠️ 执行时两个列表都要按上一次 `dry_run` 的**合格子集**一起过滤。 */
export type BatchRefundOrdersPayload = Omit<RefundRequestPayload, 'request_id'> & {
  order_nos: string[]
  dry_run: boolean
  request_ids: string[]
}

export function batchRefundOrders(payload: BatchRefundOrdersPayload): Promise<BatchOutcome> {
  return batchOrderAction('refunds', payload)
}

/** `batch/delete/` 的载荷：`isDelEmp` 与单笔同名同形（`'Y'` ⇒ 连绩效行一起写删除标记）。
 * ⚠️ `delete` 是唯一对**已软删单**幂等合格的动作（spec §D9 的例外，后端 `include_deleted=True`）。 */
export type BatchDeleteOrdersPayload = {
  order_nos: string[]
  dry_run: boolean
  isDelEmp: 'Y' | 'N'
}

export function batchDeleteOrders(payload: BatchDeleteOrdersPayload): Promise<BatchOutcome> {
  return batchOrderAction('delete', payload)
}

export function batchDeleteV2Orders(
  payload: BatchDeleteOrdersPayload & { reason?: string },
): Promise<BatchOutcome> {
  return batchOrderAction('delete-v2', payload)
}

/** `batch/recompute-commission/` 的载荷（spec §D15）：**只有** `order_nos` / `dry_run`
 * ——这颗按钮不带任何业务值（后端 `BatchRecomputeSerializer` 只有这两个键）。 */
export type BatchRecomputeCommissionPayload = { order_nos: string[]; dry_run: boolean }

export function batchRecomputeCommission(
  payload: BatchRecomputeCommissionPayload,
): Promise<BatchOutcome> {
  return batchOrderAction('recompute-commission', payload)
}

/** `batch/transition/` 的**读取形态**：不传 `target` ⇒ 只回这批单可选目标的**并集** + 每笔自己的可选集（`order_batch.py::transition_targets`）。改状态弹层的可选项**只能**来自这里——前端⛔不镜像状态机（全局约束第 7 条）。
 * ⚠️ **`dry_run` 仍必须带 `true`**：后端那个字段是**必填布尔**，读取形态虽然不参与判断，缺了会被字段层以 `1000` 挡掉（实测契约，spec §D10 / 全局约束第 6 条）。 */
export function fetchBatchTransitionTargets(orderNos: string[]): Promise<BatchOutcome> {
  return batchTransitionOrders({ order_nos: orderNos, dry_run: true })
}

/** `POST /orders/xgj-receipt-sync/`（订单管理页「同步收款」线）。载荷只有两个日期（`YYYY-MM-DD`，**录单时间**，含首尾两天）；后端**立刻**返回 `{accepted: true}`，真正的逐笔查询跑在后台 daemon 线程里（PROD-02：请求路径不出网）。
 * ⚠️ 「范围 ≤ 31 天」与「单次 ≤ 500 单」的判据都在后端（前端只是先拦一道、省一次往返）。 */
export type XgjReceiptSyncPayload = { date_from: string; date_to: string }

export function startXgjReceiptSync(
  payload: XgjReceiptSyncPayload,
): Promise<{ accepted: boolean }> {
  return request<{ accepted: boolean }>({
    method: 'post',
    url: '/api/business/orders/xgj-receipt-sync/',
    data: payload,
  })
}
