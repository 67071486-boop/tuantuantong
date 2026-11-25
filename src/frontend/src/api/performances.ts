/** 绩效接口（P3b B1）：列表 / 改金额 / 审核。后端 = `apps/business/api/performance_views.py`（`PerformanceViewSet`，注册在`apps/business/urls.py` 的 `router.register("performances", …)`），所以三个路径是：| 方法 | 路径 | 权限（全部在**后端**判，前端只负责原样展示 msg） ||---|---|---|| GET  | `/api/business/performances/` | 节点 `my_performance.export`（类级门）；**看得到多少行**另由数据范围定：持 `order.list` 看全部，不持只看自己 || POST | `/api/business/performances/{id}/adjust/` | 节点 `order.submit_payout_price`（类级门）；**载荷带 `taker_price` 时**再判「能不能改接单价」，不满足时后端**只忽略这一个键**、其余字段照常应用 |
 * | POST | `/api/business/performances/{id}/review/` | 节点 `order.change_status_completed`（类级门） |⚠️ **`/review` 不再是订单完成的唯一路径**（D-0071，业务方裁定「对标没有审核、订单状态变成完成后绩效就生效」）：通用 `/transition/` 现在**接受** `target=COMPLETED`，按权限节点 `order.change_status_completed`（17）授权（其余状态仍走 16，两处判据同源）。本接口仍是「**走过审核动作**」的那条路：通过 ⇒ 绩效 `已通过` + 订单 `COMPLETED`；驳回 ⇒ `已驳回` + 订单回 `IN_PROGRESS`；但审核**不再是**钱或结账的前置条件。⚠️ **金额可以为负 / 为 0**（D-0015），所以本模块**不做任何前端金额下界**；格式错误由后端的
 * `PerformanceAdjustSerializer` 判（`ValidationError` ⇒ HTTP 200 + `code 1000` + 可读 msg）。⚠️ 列表分页参数名是 `page` / `page_size`（`EnvelopePagination`），响应里没有 `next`——layui 用 `data.total` 自己算总页数。 */
import { AxiosError, type AxiosResponse } from 'axios'
import { http, request } from './http'
import { mapHttpStatus, NetworkError } from './errors'
import { isEnvelope, unwrap, type ApiResult, type Page } from './unwrap'

/** 列表端点（`apps/business/urls.py` 的 `router.register("performances", …)`）。 */
export const PERFORMANCES_URL = '/api/business/performances/'

/** 删除记录端点（P4FU-B / D-0072 ①：`PerformanceDeleteView`）。⚠️ **没有尾斜杠**（后端 `urls.py` 的字面 `path("performances/delete", …)`）。多打一个尾斜杠会命中 router 的 detail 路由（`pk="delete"`）而 404/405——`/settlements/settle` 记过同形的坑（`urls.py` 的注释里也写了）。 */
export const PERFORMANCE_DELETE_URL = '/api/business/performances/delete'

/** 导出记录端点。⚠️ **没有尾斜杠**。
 * 成功时不是统一信封：成功是 xlsx 字节，失败仍是 `{code,msg,data}` 信封，
 * 同一个 URL 两种形状。所以不能走 `request()`（它要求成功响应也是信封）。 */
export const PERFORMANCE_EXPORT_URL = '/api/business/performances/export'

/** 检测重复收款支付宝（P4FU-C：`AlipayDuplicatesView`，只读、无参）。⚠️ **没有尾斜杠**。 */
export const PERFORMANCE_ALIPAY_DUPLICATES_URL = '/api/business/performances/alipay-duplicates'

/** 收入类型（后端 `EmpType`，**中文取值**，就是库里的值）。 */
export const EMP_TYPE = {
  /** 代练员的报酬（对标页签 `lay-id=10`「代练员报酬账单」）。 */
  MAIN: '打单工资',
  /** 客服的销售提成（对标页签 `lay-id=20`）。P3 不产出这种行，列表会是空的（**不是**前端造数据）。 */
  SALES: '销售提成',
} as const

/** 绩效的后端字段（`PerformanceListSerializer` 的全部字段；金额一律两位小数字符串或 `null`）。 */
export type PerformanceListItem = {
  /** 绩效行主键——`/adjust`、`/review` 的路径参数用的是**它**，不是 `order_no`。 */
  id: number
  /** 关联订单的订单号（只读跨表字段）。 */
  order_no: string
  /** ⭐ **来源单号**（`Order.source_order_no` = 来源 / 交易平台那一侧的单号）——后端 `PerformanceListSerializer` 回这个键。⚠️ **与 `order_no` 是两个字段、别混**（业务方 报的正是把这两个搞反了）：「关联订单」列**显示的是它**，而点的动作（开层取数）用 `order_no`。实测值域见 `.tools/recon/order-view/_SOURCE.txt:14-19`（对标 `jionOrder` 19 位来源单号 /`orderId` 20 位系统单号）。⚠️ `blank=True` 的**非 NULL** 文本列 ⇒ 没填是**空串**（不是 `null`）；页面遇到空串**渲染空**，⛔ 不回落成 `order_no`（那会造成「看起来有来源单号」的假象）。 */
  source_order_no: string
  /** 归属人主键；`null` = 还没有接单人（这种行 `/adjust` 与 `/review` 都会被后端拒）。 */
  user: number | null
  username: string | null
  emp_type: string
  temp_amount: string
  bonus_amount: string
  /** `'0'` 按比例 / `'1'` 固定金额 / `''` 未设置（`apps/business/performance_amount.py`）。 */
  deduct_mode: string
  deduct_value: string
  /** 接单价覆盖值；`null` = 回落到订单默认价（spec §6.3）。 */
  taker_price: string | null
  /** `待审核` / `已通过` / `已驳回` / `null`（后者 = 还没有可审的东西）。 */
  review_status: string | null
  settle_status: string
  created_at: string
  /** ⭐ 关联**订单**的状态（P4FU-C 起，`source="order.status"`；D-0071 ③）。⚠️ 它存的是 **`OrderStatus` 的枚举名**（`UNASSIGNED` / `WAITING_GRAB` / `IN_PROGRESS` /`PENDING_REVIEW` / `COMPLETED` / `ISSUE` / `PAUSED` / `REFUNDED`），**不是中文**——C 线实测推翻了 brief 初稿里「中文存储值」的说法（brief §8.1）。⇒ 显示要过 `@/config/orderStatus` 的 `ORDER_STATUS_LABELS`；筛选要**送枚举名**。⚠️ **不要**把它与 `review_status` 混用：后者是「审核动作」的产物（D-0071 ④ 保留`/review` 那条路），而这一列显示的是订单状态。 */
  order_status: string
  // ── ⭐（绩效管理页 `/Emp/emplist` 补完，后端已落地）新增的 12 个只读字段 ──
  //    契约出处：`apps/business/api/serializers.py` 的 `PerformanceListSerializer`
  //    （`_EMPLIST_FIELDS` + `_SETTLED_SNAPSHOT_FIELDS`）。
  //    ⚠️ **12 个是「后端出参」的个数，不是「本页用上」的个数**：绩效管理页
  //    （`layui-island/pages/emp-emplist.ts`）只消费其中 **10 个**；`settled_by` /
  //    `settled_amount` **在本页没有对应列、也没有任何消费者**（`toRow` 不取它们）。
  //    两个 key **保留**——这个类型如实描述**后端响应**，不是描述某一张页面的用量；
  //    删掉它们会让类型对不上真实响应。⛔ 不要据此以为它们被用上了。
  //: 订单金额（`order.total_price`，`null=False default=0.00`）。⭐ 对**不持**
  //: `order.list` 的请求者读成 `null`（字段脱敏，见 `PerformanceSummary` 的同一段注释）。
  order_price: string | null
  //: 实际收款（`order.received_price`）——**可空**：订单没填实收时是 `null`，**不是** `"0.00"`；
  //: ⭐ 同样按 `order.list` 脱敏（不持 ⇒ `null`）。
  net_receipts: string | null
  //: 退款（`order.refund_fee`，累计额；**可以是负数**，D-0015）。⭐ 按节点脱敏。
  refund_fee: string | null
  //: 角色名（`order.game_role_name`，`blank=True` 非 NULL ⇒ 没填回**空串**，不是 `null`）。
  game_role: string
  game_name?: string
  //: 订单项目（`order.project_text` 文本快照，不是 FK 的 `project.name`）。
  dl_project: string
  //: 旺旺 / YY（`order.customer_ww`）。
  wangwang: string
  //: 销售客服（`order.sales_agent` 文本快照）。
  kefu: string
  //: 记录者（`order.created_by` 用户名快照）。
  log_user_name: string
  //: 记录者显示名，无法映射时回落账号快照。
  log_user_display?: string
  /** ⭐ 扣点**额**（后端用 `performance_amount.deduct_amount` 算出来，不是扣点参数值）。⚠️ **可空**：扣点越界（比例 > 100，或固定额 > `temp_amount`）时那一行回 `null`——这是后端刻意的**读容忍**（`DeductFeeField` 的 docstring：让坏行留在列表里给财务看见，⛔ 不给 `0.00` 之类假数）。⇒ 前端那一格显示 `—`，**不要**显示 `0.00`。 */
  deduct_fee: string | null
  //: 结账时间（未结账 ⇒ `null`）。
  settlement_method?: 'manual' | 'online' | null
  online_payroll_batch_id?: string | null
  online_payroll_occupied?: boolean
  settled_at: string | null
  //: 结账操作人（未结账 ⇒ 空串）。⚠️ **绩效管理页暂无消费者**（无对应列；类型如实描述响应）。
  settled_by: string
  //: 结账那一刻的实结金额快照（未结账 ⇒ `null`，**不是** `0`）。⚠️ **绩效管理页暂无消费者**。
  settled_amount: string | null
  /** ⭐ 本批（D-0142）：归属人显示名（**昵称优先、空串回落登录账号**；`user` 为空 ⇒ `""`）。
   * 与 `OrderListItem.taker_name` 是**同一个口径**（`user.first_name or user.username`），
   * 只是这里逐行读 FK、不做相关子查询。 */
  display_name?: string
  /** ⭐ 本批：`Performance.hours`（可空列原值；未填 / 已清除 ⇒ `null`）。 */
  hours?: number | null
  /** ⭐ 本批：**接手项目**（`Performance.project`）。⛔ 与 `dl_project`（**订单**项目）不是一回事。 */
  taker_project?: string
  /** ⭐（2026-11-07 R3）恒在布尔「挂负账」行标记：该行归属人 ∈ 挂负账打手集
   * （2026-11-08 撤强制展示后只剩红签用途）。`/adjust`、`/review` 等单行响应不传 context ⇒ 恒 `false`。 */
  negative_debt: boolean
}

export type PerformancePage = Page<PerformanceListItem>

/** 列表响应 `data.summary` 的六项合计（后端 `performance_viewset.performance_summary()`）。⚠️ 三项形状要点：1. **每一项都是字符串**（后端 `_money()` 显式 `str(Decimal(...).quantize(0.01))`），如 `"70.00"`——**不是数字**、也**不是** `"70.0"`（`COERCE_DECIMAL_TO_STRING` 的同款形状）；2. **空集 ⇒ `"0.00"`**，不是 `null`（六项都有值）；3. 口径是**筛选后的全集**（与 `items` 同一份 QuerySet），**不是当前页**。⭐ **2026-10-22 例外（业务方报的越权展示）**：不持 `order.list` 的请求者（打手 / 下家）拿到的 `order_price` / `refund_fee` / `profit_expected` / `profit_actual` 是 **`null`**
 * ——字段脱敏、不是 403（`apps/business/api/performance_views.py::can_view_order_money`）。本类型如实标 `string | null`；消费方 ⛔ 不要把 `null` 当 `"0.00"` 渲染（手机绩效页按节点**整格不画**，见 `m/pages/PerformancePage.vue`）。`temp_amount` / `bonus_amount`（他自己挣的钱）恒为字符串。⚠️ `profit_expected` / `profit_actual` 是**两个不同的真值**（前者减 `temp_amount`、后者减 `bonus_amount`）。对标那页把两处都渲染成 `profit1`（它自己的笔误）——骨架照抄、值不照抄（计划 §2.2）。 */
export type PerformanceSummary = {
  temp_amount: string
  bonus_amount: string
  unsettled_bonus_amount?: string
  settled_bonus_amount?: string
  order_price: string | null
  refund_fee: string | null
  profit_expected: string | null
  profit_actual: string | null
}

/** 后端**真的支持**的筛选项。前六个是 `PerformanceSearchSerializer`（基类，P3b 起）；P4FU-C 又加了一组（`PerformanceListSearchSerializer`，**只服务绩效列表与导出**，不服务 `/settlements`）。⚠️ **这一组用 `null` 表示「没传」**（`toPerformanceParams` 把 `''`/`null`/`undefined` 都丢掉）。⚠️ **`customer_type: 0` 是合法值**（销售人），判据一律 `is not None`——不要写 `if (v)`（E-0021：`0` 会被当成「没传」而静默丢掉筛选条件）。⚠️ 「全部」= **不送该参数**，⛔ 不要把证据件里那几个「全部」option 的值（`21714` / `-1` / `""`）送出去：`game` / `order_source` 是 `min_value=1`、`order_type` 的 `-1` 是非法值 ⇒ 都会回业务码 `1000`（brief §8.1）。
 * ⚠️ 五类**无数据**的对照控件（淘宝状态 / 是否实名 / 申请结算 / 排团团号 / 机位）后端**一个参数都没加** ⇒ 本类型里**没有**它们，页面也**不许**发（发了是静默忽略，等于骗自己）。收款状态有 `net_receipts_state`，已映射的两档要真的发参，不在「无数据控件」里。 */
export type PerformanceSearchParams = {
  /** 归属人主键（后端 `min_value=1`，`?user=0` 会被入口拒成 `code 1000`）。 */
  user?: number | null
  // ⚠️ 这几个字段**允许 `null`**：`toPerformanceParams` 的 `EMPTY_KEYS` 明确把 `null` 当"没传"丢掉，
  // 类型若不收 `null` 就会比实现窄（`pnpm build` 连 vitest 工程一起类型检查时才暴露）。
  emp_type?: string | null
  review_status?: string | null
  settle_status?: string | null
  /** **日期**（不是 datetime）：后端用 `DateField` + `date_type` 指定的那一列，两端都含。 */
  date_from?: string | null
  date_to?: string | null
  /** ⭐ **线 C**：`date_from` / `date_to` 判**哪一列**——自描述字符串。后端是 `ChoiceField`（`PerformanceListSearchSerializer.date_type`），**默认 `created`**⇒ 不传它 = 录单时间（改动前的行为，逐字相同）。⚠️ **取值不是**对标证据件里那五个数字档（`10` 发单 / `20` 交单 / `25` 录单 / `30` 结账 /`40` 验收）——那是**证据件里 `<option>` 的值**，后端只认下面这五个字符串；数字档送出去会回业务码 `1000`。映射见 `layui-island/pages/emp-emplist.ts` 的 `DATE_TYPE_MAP`。⭐⭐ **批次 5 · 线 E：三档 → 五档**，且原 `reviewed` **改名换列**成`completed`（打 `Order.completed_at`，不再打生产上零写入的 `Performance.reviewed_at`）。
   * ⛔ `reviewed` **不再被后端接受**（不给它留第二个契约）——本类型、后端 `choices`与页面 `DATE_TYPE_MAP` 三处是**同一件事**，改一处就要三处一起改。 */
  date_type?: 'created' | 'published' | 'submitted' | 'completed' | 'settled' | null
  /** 游戏主键（`min_value=1`）。对标的 `gameName` 那一格。 */
  game?: number | null
  /** ⭐ **D-0365**：收款状态两档（`received` / `unreceived`）——后端`PerformanceListSearchSerializer.net_receipts_state`。判据 = 订单**接单实收款**`order.received_price`（与 `emp-emplist.ts::priceTemplet` 的色块**同一句话**）：有值且 ≠ 0 ⇒ `received`；`null` / `0` ⇒ `unreceived`（负数按「≠ 0」归 received）。⚠️ 对标的「结算中」一档**没有**我方对应值 ⇒ 页面对它不发参、走「尚未开放」点名提示。 */
  net_receipts_state?: 'received' | 'unreceived' | null
  /** 订单状态：**枚举名**（`COMPLETED`…），不是中文（见 `PerformanceListItem.order_status`）。 */
  order_status?: string | null
  /** 订单类型：`OrderType` 的枚举名（`BOOST` / `COMPANION`）；对标的 `-1` 「全部」由**不传**表达。 */
  order_type?: string | null
  /** 订单来源主键（`min_value=1`）。对标的 `orderSoures`。 */
  order_source?: number | null
  /** ⭐ 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §4 #5）：
   * 只留「其来源属于该分组」的绩效行。两账单（打单工资 / 销售提成）共用同一个筛选表单。 */
  source_group?: number | null
  /** ⭐（2026-11-07）「排除禁止结算的」哨兵（打手多选下拉虚拟分组；原三态控件的
   * `all`/`only` 档已撤）：只认 `excluded`，默认 = **不发参**。 */
  settle_forbidden_scope?: 'excluded' | null
  /** ⭐（2026-11-07）「排除总绩效为负的」哨兵（同上、同一颗下拉）：只认 `'1'`，默认 = 不发参
   *（不排除；挂负账打手的强制展示与此无关、恒开）。 */
  exclude_net_negative?: '1' | null
  /** `0` = 销售人 / `10` = 发单人（**`0` 是合法值**）；只在 `customer` 有值时有意义。 */
  customer_type?: number | null
  /** 逗号串：对标的「销售人 / 发单人」多选名单（后端按 `customer_type` 走不同列）。 */
  customer?: string | null
  /** 逗号串：按**归属人用户名**批量筛（对标的「批量搜用户」那一格）。 */
  username?: string | null
  /** 订单号**精确**匹配。 */
  order_no?: string | null
  /** 模糊关键词：命中订单号**片段**或**角色名**片段（对标 `key1` 的 placeholder 原文）。 */
  keyword?: string | null
  /** 订单项目（后端 `order__project_text__icontains`）。 */
  project?: string | null
  /** 客户旺旺/YY（后端 `order__customer_ww__icontains`）。 */
  customer_ww?: string | null
  /** 同一次汇总中带上已完成订单的未结账和已结账绩效。 */
  include_settlement_summary?: 1
  page?: number
  page_size?: number
}

const EMPTY_KEYS: Set<unknown> = new Set(['', undefined, null])

/** 组装查询参数：空值不发送（空串 / `undefined` / `null`）。⚠️ 与 `api/orders.ts::toOrderParams` 同形但**刻意各写一份**：两个模块的字段集不同，共用一个泛型函数会把两条线的契约绑在一起（简报 §0.1 要求一线一个模块文件）。判据同样必须是「空」而不是「假值」——`0` 与 `false` 都要留下（E-0021：真值判断会静默丢条件）。 */
export function toPerformanceParams(
  params: PerformanceSearchParams,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(params)) {
    if (EMPTY_KEYS.has(value)) continue
    if (typeof value === 'string' && value.trim() === '') continue
    out[key] = value as string | number
  }
  return out
}

/* ── 打手多选下拉的「虚拟分组」哨兵（2026-11-07；两版前端共用、唯一出处 = 本文件）───────── */

/** 「其它分组」组头下的两颗哨兵 option：排除禁止结算的（发 `settle_forbidden_scope=excluded`）。 */
export const PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN = ':exclude_settle_forbidden'
/** 「排除总绩效为负的」（发 `exclude_net_negative=1`）。 */
export const PLAYER_OPTION_EXCLUDE_NET_NEGATIVE = ':exclude_net_negative'

export type PlayerFilterSelection = {
  /** 剩余的真用户名（哨兵与空串已剥掉；保持原顺序，不排序不去重）。 */
  usernames: string[]
  excludeSettleForbidden: boolean
  excludeNetNegative: boolean
}

/** 把打手多选的取值折成「剩余用户名 + 两颗哨兵命中」。⚠️ 不 trim：调用方先各自归一
 * （旧页过 `commaList`，v2 的取值本来就是 option 的 `username` 原值）。 */
export function splitPlayerFilterSelection(values: readonly string[]): PlayerFilterSelection {
  const usernames: string[] = []
  let excludeSettleForbidden = false
  let excludeNetNegative = false
  for (const value of values) {
    if (value === PLAYER_OPTION_EXCLUDE_SETTLE_FORBIDDEN) {
      excludeSettleForbidden = true
    } else if (value === PLAYER_OPTION_EXCLUDE_NET_NEGATIVE) {
      excludeNetNegative = true
    } else if (value !== '') {
      usernames.push(value)
    }
  }
  return { usernames, excludeSettleForbidden, excludeNetNegative }
}

/** 绩效列表（`GET /api/business/performances/`）。 */
export async function fetchPerformances(
  params: PerformanceSearchParams = {},
): Promise<PerformancePage> {
  return request<PerformancePage>({
    method: 'get',
    url: '/api/business/performances/',
    params: toPerformanceParams(params),
  })
}

/** `/adjust` 的载荷。**字段与后端序列化器逐条对应**：`taker_price` / `deduct_mode` / `deduct_value` / `reason`（全 `required=False`）。- 只改扣点、只改接单价、两者一起改都是合法请求；**空载荷 ⇒ `code 0` 且无副作用**。- `taker_price: null` = **清空覆盖**、回落订单默认价（后端 `allow_null=True`）。- `reason` 上限 200（超长由后端回 `code 1000`，前端不截断用户的理由）。 */
export type PerformanceAdjustPayload = {
  taker_price?: string | null
  /** `'0'` 按比例 / `'1'` 固定金额（空串 = 未设置，也是后端 `ChoiceField` 认的值）。 */
  deduct_mode?: string
  deduct_value?: string
  /** ⭐ 本批（D-0142）：**直写**预计绩效 / 生效绩效（不经过提成算式）。⚠️ D-0147 批 E 起，绩效弹窗/账单表那两个金额格与垃圾桶**不再走本键**——它们走 `adjustPerformanceAmount` / `deletePerformance`（见下）；本类型保留给 `/adjust/` 的既有消费者。⛔ 后端 `allow_null=False`（`serializers.py::PerformanceAdjustSerializer`）⇒ `null` 会回业务码 `1000`，**不要送**；省略才是「不改这一列」。`toPerformanceParams` 那套「空值丢掉」只作用于**查询参数**，POST body 由调用方自己保证。 */
  temp_amount?: string
  bonus_amount?: string
  reason?: string
}

/** 改绩效金额 / 扣点（`POST …/adjust/`）。返回**改后**的那一行，调用方可用它刷新那一格。 */
export async function adjustPerformance(
  id: number,
  payload: PerformanceAdjustPayload,
): Promise<PerformanceListItem> {
  return request<PerformanceListItem>({
    method: 'post',
    url: `/api/business/performances/${id}/adjust/`,
    data: payload,
  })
}

/** `/adjust-amount/` 的金额格（spec §4.6）：`temp` = 预计绩效 / `bonus` = 生效绩效。 */
export type PerformanceAmountKind = 'temp' | 'bonus'

/** `/adjust-amount/` 的载荷（spec §4.1 / §4.6；D-0147 批 E）。⚠️ 与 `/adjust/` 的 `temp_amount` / `bonus_amount` 是**两个动作**：本端点一次只写一个金额格，由服务端反推「有效基价」（发单价 / 接单价覆盖值）并重算预计绩效。`amount` 是**十进制串**（spec §4.6 明写不用 float，避免 JSON float 进 `Decimal` 的精度漂移）。 */
export type PerformanceAdjustAmountPayload = {
  amount_kind: PerformanceAmountKind
  amount: string
  /** 审计理由，可选（后端 `required=False, allow_blank=True, default=""`）。 */
  reason?: string
}

/** 改**一个**金额格（`POST /api/business/performances/<id>/adjust-amount/`；spec §4.1 / §4.6）。成功 `data` = 该绩效行的**最新形状**（发单价与预计绩效的 1 分差如实呈现 ⇒ 调用方据此重渲染，⛔ 不在本地假装等于用户填的值）。失败由 `request()` 抛 `BizError`：`1014`（已结账 / 无接单人）、`1000`（`amount_kind` 非法 / `amount` 非十进制 / **提成算不出**），flag 命中且该行已有 `taker_price` ⇒ HTTP `403`。⚠️ **「提成算不出」是 `1000` 不是 `1014`**（spec §4.6 的 ⛔ R-4 更正块）：后端那一支直接透传`CommissionError.code`，而 `commission.py` 各 raise 点用的是 `ErrorCode.VALIDATION`(1000)——与既有 `/assign/`、`/adjust/` **同码**，调用方不必按端点记两套表。`1014`
 * （`PERFORMANCE_STATE_INVALID`）只留给「状态 / 条件不允许」（已结账、无接单人）。 */
export async function adjustPerformanceAmount(
  id: number,
  payload: PerformanceAdjustAmountPayload,
): Promise<PerformanceListItem> {
  return request<PerformanceListItem>({
    method: 'post',
    url: `/api/business/performances/${id}/adjust-amount/`,
    data: payload,
  })
}

/** `/performances/<id>/delete/` 的出参（spec §4.6）：`deleted` = 绩效主键；`taker_cleared` = 是否连带清了接手人。 */
export type DeletePerformanceResult = {
  deleted: number
  taker_cleared: boolean
}

/** 软删**一条**绩效记录（`POST /api/business/performances/<id>/delete/`；spec §4.5 / §4.6）。载荷空体；成功 `data` = `{deleted, taker_cleared}`。失败面（spec §4.5 / §4.6）：`1014`（`PERFORMANCE_STATE_INVALID`：已结账 / 该行不是打单工资行——后者文案「只能删除接单人的绩效记录」，与「已结账不能删」**同码**、靠 `msg` 区分）；`1002`（`BUSINESS`：订单已软删 ⇒`get_object()` 走 `_base_queryset` 的口径，与 `/adjust`、`/review` 同形）；HTTP `403`（缺节点 `order.submit_payout_price`（1162，软删任何绩效行都要）或 `order.clear_taker`（19，删当前接单人那条 MAIN 行时会连带清接手人 ⇒ 额外要求））。
 * ⚠️ 与批量删除 `deletePerformances`（`/performances/delete`，**没有尾斜杠**的另一个视图）不是同一个端点，别互相替代。 */
export async function deletePerformance(id: number): Promise<DeletePerformanceResult> {
  return request<DeletePerformanceResult>({
    method: 'post',
    url: `/api/business/performances/${id}/delete/`,
  })
}

export type PerformanceReviewPayload = {
  /** **必填**：后端刻意不给默认值——漏传意味着「静默驳回」，那是发钱方向的事故。 */
  approved: boolean
  /** 审核意见，上限 255（后端截断兜底在留痕侧）。 */
  comment?: string
}

/** 审核绩效（`POST …/review/`；通过 ⇒ **订单被完成**）。返回审核后的那一行。⚠️ 路径写死 `/review/`：本函数**只**表达「审核」这个动作（写 `review_status` + 审核留痕）。订单完成**不再**只能由它触发（D-0071）：通用 `/orders/{no}/transition/` 也能完成订单，但要持节点 `order.change_status_completed`（17）。」 */
export async function reviewPerformance(
  id: number,
  payload: PerformanceReviewPayload,
): Promise<PerformanceListItem> {
  return request<PerformanceListItem>({
    method: 'post',
    url: `/api/business/performances/${id}/review/`,
    data: payload,
  })
}

/* ────────────────────── P4FU-B / C：删除、导出、支付宝查重 ────────────────────── */

/** 删除记录的出参：`{deleted: [绩效 id, …]}`（B 线实测形状——**id 列表，不是计数**，E-0060）。
 * ⚠️ 后端的三类拒绝（**已结账不许删** / **订单的 MAIN 行一律不许删** / **有调整记录**）
 * 都由后端判并回业务码 + 中文 `msg` ⇒ 前端**原样弹 msg、不自己预判、不给假成功**。 */
export type DeletePerformancesResult = { deleted: number[] }

/** 删除绩效记录（`POST /api/business/performances/delete`，D-0072 ①）。
 * ⚠️ **空列表不要调本函数**：后端 `PerformanceIdsSerializer` 的 `allow_empty=False`
 * ⇒ `[]` 会回业务码 `1000`。页面在「勾 0 行」时就该提示（对标原话「请选中要操作的记录」）。 */
export async function deletePerformances(
  performanceIds: number[],
): Promise<DeletePerformancesResult> {
  return request<DeletePerformancesResult>({
    method: 'post',
    url: PERFORMANCE_DELETE_URL,
    data: { performance_ids: performanceIds },
  })
}

/** 支付宝查重的一行子账号（后端 `AlipayDuplicatesView` 的 `users[i]`）。 */
export type AlipayDuplicateUser = {
  username: string
  /** **显示名**（昵称优先、空串回落登录账号；后端 追加的键）。
   * ⚠️ 写成**可选**是因为它是**加法**：前端先上、后端还没上时这个键真的缺席
   * ⇒ 消费侧按 `display_name || username` 取值（`emp-emplist.ts::alipayRows` 就是这么写的）。 */
  display_name?: string
  /** 该用户的职位（Group 名，多个用「、」连接）；**没有职位时是空串**。 */
  job: string
  is_active: boolean
}

/** 一组重复收款支付宝：`count` = 用这个账户的人数（≥ 2），与 `users.length` 同源。 */
export type AlipayDuplicateGroup = {
  /** ⚠️ **完整账号**（本端点有意不做脱敏：脱敏后两条看起来一样、端点即失效；见后端 docstring）。 */
  alipay_account: string
  count: number
  users: AlipayDuplicateUser[]
}

/** 支付宝查重的出参（无参 GET、**不看列表筛选**：后端是租户内全量）。 */
export type AlipayDuplicatesResult = { duplicates: AlipayDuplicateGroup[] }

/** 检测重复收款支付宝（`GET /api/business/performances/alipay-duplicates`，只读）。 */
export async function fetchAlipayDuplicates(): Promise<AlipayDuplicatesResult> {
  return request<AlipayDuplicatesResult>({
    method: 'get',
    url: PERFORMANCE_ALIPAY_DUPLICATES_URL,
  })
}

/** 导出的字节与浏览器要用的文件名。 */
export type PerformanceExport = { blob: Blob; filename: string }

/** 后端取不到 `Content-Disposition` 时的兜底文件名（后端写的是 ASCII 名，见导出视图）。
 * 后缀必须是 xlsx，和真实字节一致。csv 后缀的 xlsx 会让 Excel 先弹格式警告。 */
export const EXPORT_FALLBACK_FILENAME = 'performances.xlsx'

/** 从 `Content-Disposition` 里取文件名；取不到回兜底名。后端同时给 ASCII 的 `filename=` 和 RFC 5987 的 `filename*=UTF-8''…`（中文名）。先认 `filename*`，取不到或解不开再回落 `filename=`，都没有才用兜底名。⚠️ **顺序是承重的、不是风格**：后端把 ASCII 的 `filename=` 写在**前面**，先跑 `filename=` 的正则就永远拿不到中文名（那样浏览器落盘的是`performances-YYYYMMDD.xlsx`，不是业务方要的那个名字）。⚠️ **解码失败不抛**（`decodeURIComponent` 对畸形 `%` 转义会抛 `URIError`）：回落 `filename=`——把异常抛给页面会让一次导出在「解析文件名」这一步炸掉。`fallback` 给订单导出复用：两边兜底文件名不同，解析只留这一份。默认值是绩效那份，既有调用点不用传。 */
export function exportFilename(
  disposition: unknown,
  fallback: string = EXPORT_FALLBACK_FILENAME,
): string {
  if (typeof disposition !== 'string') return fallback
  // ① RFC 5987：`filename*=UTF-8''<percent-encoded>`
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  if (extended?.[1]) {
    try {
      const decoded = decodeURIComponent(extended[1].trim())
      if (decoded) return decoded
    } catch {
      // 畸形转义 ⇒ 落回下面的 `filename=`（⛔ 不把 URIError 抛给调用方）
    }
  }
  // ② ASCII 兜底：`filename="..."` / `filename=...`（⚠️ 不要让它抢在 `filename*` 前面）
  const matched = /filename="?([^";]+)"?/.exec(disposition)
  return matched?.[1]?.trim() || fallback
}

/** 下载导出记录（`GET /api/business/performances/export`）。⭐ **为什么不用 `window.open(url)`**（对标就是那么干的）：同一个 URL 有**两种形状**——成功是 xlsx 文件流、失败（参数非法 / 超上限 10000 行）是**统一信封 + HTTP 200**（后端 `PerformanceExportView` 的 docstring 明写了这条有意偏离）。`window.open` 拿不到响应体，失败时只会把信封 JSON 原样摊在一个新标签页里 ⇒「超上限请先收窄筛选」这条**可操作的**提示到不了用户眼前，也违反本页「失败原样弹后端 `msg`」的硬口径。所以这里**先请求再落 Blob**。⚠️ 本函数**不走 `request()`**：那个函数要求成功响应也是信封，xlsx 会被它判成`NetworkError('后端返回了非信封响应')`。两种形状在这里分开处置：- **信封**（`Content-Type` 含 `json`）⇒ 交给 **`unwrap()`**（与别处**同一个**判据）
 * ⇒ `code≠0` 抛 `BizError`（含后端 `msg`）；`code=0` 时 `data` 是 `null`（导出不会这样，留给防御分支）。- **HTTP 非 2xx**（401/403/5xx）⇒ `mapHttpStatus(status, 信封 msg)`，与 `request()` 同源。- 其余 ⇒ xlsx 字节。**代价（如实登记）**：① 文件先进内存——后端自己封顶 10000 行（≈1–2 MB），可接受；② 这条路径**绕过了 `request()` 的 401 处理器**（`onUnauthorized`：清会话 + 跳登录页）。原因是**形状**而非排他：它的成功响应本来就不是统一信封，所以不走 `request()`——这里不按清点下结论，也不主张「除此之外还有没有别的出口」（那种句子会被新出口证伪，见 E-0011）。`api/http.ts` 的模块注释把它列作**非穷举**的例子之一；该处也确实还有别的出口
 * （例如 `layui-island/config-replica.ts` 的 `apiRequest`、layui 表格自发的 XHR）。历史先例是 `/healthz` 存活探针，但它的消费者 `views/HomeView.vue` 已于 2026-09-23 删除。会话过期时用户会看到 `AuthExpiredError` 的中文提示，但**不会**被自动踢回登录页。换取的是「超上限 / 参数非法」这两类失败照旧走页面的统一失败出口。⚠️ 参数用 `toPerformanceParams`（**与列表同一份折参函数**）：导出与列表**同源**，不另拼一套（否则「导出的是另一批数据」这种 bug 无法被测试发现）。分流只看 Content-Type 里有没有 json。非信封那一支是 xlsx 字节，失败仍是信封。 */
/** Blob 是不是 JSON 信封（成功是文件字节，失败是 `application/json`）。
 * 只有 Content-Type 含 json 才算信封。 */
export function isJsonBlob(blob: Blob): boolean {
  return String(blob.type || '')
    .toLowerCase()
    .includes('json')
}

/** 从 Blob 里读统一信封；不是信封 / 读不出来都回 `null`（调用方按「形状异常」处置）。 */
export async function envelopeFromBlob(blob: Blob): Promise<ApiResult<unknown> | null> {
  try {
    const parsed: unknown = JSON.parse(await blob.text())
    return isEnvelope(parsed) ? parsed : null
  } catch {
    return null
  }
}

export async function downloadPerformanceExport(
  params: PerformanceSearchParams,
): Promise<PerformanceExport> {
  return downloadExport(PERFORMANCE_EXPORT_URL, toPerformanceParams(params))
}

export async function downloadExport(url: string, params: Record<string, string | number>): Promise<PerformanceExport> {
  let response: AxiosResponse<Blob>
  try {
    response = await http.get<Blob>(url, {
      params,
      responseType: 'blob',
    })
  } catch (e) {
    // HTTP 非 2xx：DRF 的 401/403 也是信封（`{code,msg}`），5xx 是 HTML ⇒ msg 取不到就交给
    // `mapHttpStatus` 的兜底文案（与 `request()` 同一条判据，不另写一套）。
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
    // ⭐ HTTP 200 + 信封 = 后端**预期**的业务失败（参数非法 `1000` / 超上限 `1000`）：
    //    走与别处同一个 `unwrap()` 判据 ⇒ `code≠0` 抛带后端 `msg` 的 `BizError`。
    const envelope = await envelopeFromBlob(blob)
    if (envelope) {
      unwrap(envelope)
      // `code=0` 却不是文件流：形状对不上，**不给假成功**（响亮地报，别写一个空 xlsx 落盘）。
      throw new NetworkError('导出端点返回了空的成功信封、没有文件内容')
    }
    throw new NetworkError('导出返回的形状无法识别（既不是 JSON 信封、也不是 xlsx）')
  }
  return { blob, filename: exportFilename(response.headers?.['content-disposition']) }
}

/* ────────────────────── M2：手机端「我的绩效」取数（只新增，不改既有导出） ────────────────────── */

/** 绩效列表响应（`data` 里**含 `summary`** 的形状）。⚠️ 与 `PerformancePage` 的唯一差异是多了 `summary`：后端 `PerformanceViewSet.list`经 `core.envelope.paginated(summary=…)` 把六项合计放进 `data["summary"]`，而 `fetchPerformances` 的返回类型把它**丢了**（桌面 island 页不用它）。手机端「我的绩效」顶部合计（预计 / 实际两个数）要的就是它——统计口径 = **后端同一份筛选后 QuerySet 的全集**（`performance_summary(queryset)`），不是当前页。 */
export type PerformancePageWithSummary = PerformancePage & { summary: PerformanceSummary }

/** 手机端绩效列表：与 `fetchPerformances` **同一个端点、同一份折参函数**（⛔ 不另起一套取数）。实现 = 委托 `fetchPerformances` 后把 `summary` 补进返回类型——`request<T>()` 的 `T` 只是类型视图，运行期信封的 `data` 本来就带 `summary`（`core.envelope.paginated` 的形状），所以这个收窄是**诚实的**：没有第二个调用点，端点 URL / 参数组装 / 错误分类全部单源。 */
export async function fetchPerformancesWithSummary(
  params: PerformanceSearchParams = {},
): Promise<PerformancePageWithSummary> {
  const page = await fetchPerformances(params)
  return page as PerformancePageWithSummary
}
