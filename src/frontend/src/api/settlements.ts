/** 结算接口：结算列表的**读**（P4-8）+ **结账**（P4-FE-2）。后端 = `apps/business/api/settlement_views.py`（注册在 `apps/business/urls.py`，本模块用到的各条路径**都没有**尾斜杠：`/api/business/settlements`、`/api/business/settlements/people`、`/api/business/settlements/settle`、`/api/business/settlements/revoke`）。| 方法 | 路径 | 权限（后端判，前端只原样展示 `msg`） ||---|---|---|| GET  | `/api/business/settlements` | `has_node("finance.payroll")`（`FinanceReadPermissionMixin`） || GET  | `/api/business/settlements/people` | 同上（`SettlementPeopleView`，D-0089） |
 * | GET  | `/api/business/settlements/daily-summary` | 同上（`SettlementDailySummaryView`，D-0319 按天聚合） || GET  | `/api/business/settlements/daily-summary/sources` | 同上（`SettlementDailySourcesView`，D-0323 按天按来源分解） || POST | `/api/business/settlements/settle` | `has_node("finance.change_settlement_status")`（`SettleView`） |本模块的**读**部分只做三件事：拼查询参数、取数、把「一次取全」的分页循环收成一处。金额的**显示**格式（两位小数）不在本模块决定——那是页面的事（对标的 templet 直接输出服务端原值，我们照做）。写方法有两个：`settlePerformances`（未结账 → 已结账）和 `revokeSettlements`（已结账 → 未结账）。
 * 结算记录页只接读接口，这里没有 `adjust` / `refund`。 */
import { request } from './http'
import type { Page } from './unwrap'

/** 列表端点。**没有尾斜杠**（后端 `settlement_views` 的路径逐字如此）。 */
export const SETTLEMENTS_URL = '/api/business/settlements'

/** 「结账对象人员」候选端点（`SettlementPeopleView`，D-0089）。**没有尾斜杠**（与 `/settlements` 同形）。 */
export const SETTLEMENT_PEOPLE_URL = '/api/business/settlements/people'

/** 结账端点（`SettleView`）。⚠️ 同样**没有尾斜杠**——`urls.py` 里是 `"settlements/settle"`，
 * 加尾斜杠会吃一次 301/404（`APPEND_SLASH` 对 POST 不重发请求体，是**静默丢参数**的形状）。 */
export const SETTLE_URL = '/api/business/settlements/settle'

/** 撤销结账端点（P4FU-B 的 `SettlementRevokeView`，D-0072 ②）。⚠️ **没有尾斜杠**。
 * 与 `/settle` 是**同一颗对标按钮**（`settBtn`「仅修改结算状态」）的两个方向，共用同一个
 * RBAC 节点 `finance.change_settlement_status`（见后端 `performance_write_views.py` 的模块 docstring）。 */
export const SETTLE_REVOKE_URL = '/api/business/settlements/revoke'

/** 单次取数的条数。本页取 **200** 条（后端 `EnvelopePagination.max_page_size` 现为 **300**，`apps/business/api/base.py`：`page_size = 20`）——传更大的值会被 DRF **静默**截到上限（`PageNumberPagination.get_page_size` 的`min(size, max)`），于是「一次取完」的假设会静默失效。 */
export const SETTLEMENT_PAGE_SIZE = 200

/** 「取全」的页数上限（`200 × 100 = 20000` 行）。它是**防御**后端忽略 `page` 参数那类失控用的，不是业务额度：真撞上时`fetchAllSettlements` **抛错**（而不是静默截断成「看起来只有这么多行」）——本页的金额是按人求和，**缺行的和是错的钱**，比没有数更危险。 */
export const MAX_SETTLEMENT_PAGES = 100

/** 结算列表的一行 = `SettlementListSerializer` 的字段全集。⚠️ 金额字段（`temp_amount` / `bonus_amount` / `deduct_value` / `taker_price` / `payable_net`）是 DRF `DecimalField` 序列化出的**字符串**（两位小数，如 `"-15.00"`），不是 number。`taker_price` 可以是 `null`（回落到订单默认价）。⚠️ `user` / `username` 可以为 `null`：`Performance.user` 是 `null=True` 的 FK（「还没人接」的行）。**不要**把 `null` 当 0 处理。⚠️ `payable_net` **只在结算列表里**（那正是它相对绩效列表多出来的那一列）；它读的是「未结账的调整记录」，值的口径见 `apps/business/settlement_amount.py`。
 * ⚠️ `settled_amount` **同样只在结算列表里**（与 `payable_net` 一起在 `SettlementListSerializer`上，绩效列表 `PerformanceListSerializer` 两个都没有）——两条注释说的是同一个字段集事实。 */
export type SettlementListItem = {
  /** 绩效行主键。**不是**订单标识——订单一律用 `order_no`。 */
  id: number
  /** 关联订单号（只读跨表字段）。 */
  order_no: string
  /** ⭐ **来源单号**（`Order.source_order_no`）—— `SettlementListSerializer`也回这个键（它与绩效列表共用 `_PERFORMANCE_LIST_CORE_FIELDS`）。⚠️ 与 `order_no` 的差别、以及「结算记录页的『关联订单』列显示它」这条口径，与 `PerformanceListItem.source_order_no` **同一个事实** ⇒ 详细依据不在本文件重复，见 `@/api/performances` 上那个字段的注释（AGENTS.md §4「不重复同一事实」）。 */
  source_order_no: string
  /** 归属人主键；`null` = 还没有接单人。 */
  user: number | null
  /** 归属人用户名（只读跨表字段）；`null` 的含义同 `user`。⚠️ 这是**登录账号**；要显示给人看的是 `display_name`。 */
  username: string | null
  /** 归属人的**显示名**：昵称优先、空串回落登录账号（Python 侧唯一出处`apps/accounts/display.py::user_display_name`）。后端 `SettlementListSerializer`回这个键。⚠️ 声明成可选是**部署窗口**的如实形状（前端已上、后端没上时这个键真的缺席）：页面按 `display_name ?? username` 取值，⛔ 不要把它写成一个恒在的 `string` 再说谎。 */
  display_name?: string
  /** 收入类型（后端 `EmpType` 的中文取值）。 */
  emp_type: string
  /** 预计绩效（`Performance.temp_amount`）。 */
  temp_amount: string
  /** 生效绩效（`Performance.bonus_amount`）——**不含扣点**（扣点由 `payable_net` 减）。 */
  bonus_amount: string
  /** `'0'` 按比例 / `'1'` 固定金额 / `''` 未设置。 */
  deduct_mode: string
  deduct_value: string
  /** 接单价覆盖值；`null` = 回落订单默认价。 */
  taker_price: string | null
  /** `待审核` / `已通过` / `已驳回` / `null`（还没有可审的东西）。 */
  review_status: string | null
  /** `未结账` / `已结账`。 */
  settle_status: string
  created_at: string
  /** **应付净额**（P4 的算钱口径；可为负，D-0015）。 */
  payable_net: string
  /** 结账那一刻该行的**实结净额快照**（P4-FE-4）；未结账 / 存量行是 `null`。两位小数字符串，可为负（D-0015）。⚠️ **与 `payable_net` 的语义差异（这是这一列存在的理由，别混用）**：- `payable_net` 是**活值**：`bonus_amount − 扣点 + Σ(**未结账**调整记录.amount)`，回答「**这一期还该付多少**」。结账动作会把那些调整记录翻成「已结账」⇒ 一条已结账的历史行，它显示的是「调整项归零后的当前值」，**不等于当初实结走的金额**。- `settled_amount` 是**快照**：结账那一刻把当时的 `payable_net` 抄下来，回答「**当初实结了多少**」；它**不随之后的调整变动**——历史行的凭证。⚠️ 语义边界（后端已登记为**已知代价**，不是缺陷）：快照只记「该行**最后一轮被结清**时的净额」，之后的「**只结调整记录**」轮次**不会**更新它 ⇒ **它不是「该行累计收到的钱」**，
   * 跨行求和 ≠ 累计打款。见 `.agents/notes/implemented/feature/2026-09-22-settled-amount-snapshot.md`。⛔ **不要把 `null` 当 `0`**：`null` 是「没有快照」（未结账 / 存量行），页面按可空渲染——回落 `payable_net`（取值回退，不是算钱），见`layui-island/pages/emp-billsummary.ts` 的 `settledOrPayable`。 */
  settled_amount: string | null
  /** 项目（`Order.project_text` 的跨表只读镜像，2026-09-21 为前端「结算记录」页新加）。
   * ⛔ 不是筛选项——`SettlementSearchSerializer` 没有它。 */
  project: string
  /** 结算时间（`Performance.settled_at` 的跨表镜像，）。⚠️ DRF `DateTimeField` 的 **ISO-8601 字符串**（`2026-09-21T21:58:50.123456+08:00`），**不是**对标那种服务端格式化好的 `yyyy-MM-dd HH:mm:ss`；显示格式由页面决定（`emp-billsummary.ts` 的 `formatSettledAt`）。⚠️ **未结账时是 `null`**（模型 `null=True`）。⚠️ 同名的 `PerformanceAdjustment.settled_at` 是**另一张表**上的「结账时间」，不是这个字段。 */
  settled_at: string | null
  /** 结算人（`Performance.settled_by`，存的是**用户名**，`settle_performances(actor=…)` 写入）。
   * ⚠️ **未结账时是空串 `''`**（模型 `blank=True` 默认值），不是 `null`。 */
  settled_by: string
}

export type SettlementPage = Page<SettlementListItem>

/** 后端**真的支持**的筛选项（`SettlementSearchSerializer` 的字段全集，一个不多）：绩效列表那六个（人 / 收入类型 / 审核状态 / 结账状态 / 时间窗）+ **订单号** + **游戏** +**订单来源**（D-0323 两态）。⚠️ `game`（对标的 `gameName`）**由 `emp-billsummary.ts` 真发该参数**（后端同口径 `order__game_id`，T1）——不再是「照渲染但不提交」。 */
export type SettlementSearchParams = {
  /** 归属人主键（后端 `min_value=1`，`?user=0` 会被入口拒成 `code 1000`）。 */
  user?: number | null
  // ⚠️ 这几个字段允许 `null`：`toSettlementParams` 把 `null` 当「没传」丢掉，
  // 类型若不收 `null` 就比实现窄（`pnpm build` 连 vitest 工程一起类型检查时才暴露）。
  emp_type?: string | null
  review_status?: string | null
  settle_status?: string | null
  /** **日期**（不是 datetime）：后端 `DateField` + **`settled_at__date`**（筛的是**结账时间**一列，
   * 两端都**含**）。⇒ `settled_at is None`（**未结账**）的行**被排除**。
   * ⚠️ 别与绩效导出端点混淆：`GET /api/business/performances/export` 的两个日期参数**仍是创建时间**。 */
  date_from?: string | null
  date_to?: string | null
  /** 订单号，**精确匹配**（后端 `order__order_no=`）。 */
  order_no?: string | null
  /** 游戏主键（`min_value=1`）。对标的 `gameName` 那一格；**本线（D-0089 同期）已接上**。 */
  game?: number | null
  /** ⭐ **订单来源（D-0323，两态）**：正整数 ⇒ 指定来源（后端 `order__order_source_id=`）；字面 `'none'` ⇒ **未指定来源**（后端 `order__order_source__isnull=True`——来源被删 /录单没选都落进这一档）。不传 / `null` ⇒ 不筛。消费方：结算记录页来源分解行的「查看明细」（当天 + 该来源）。⚠️ ⛔ 不要传 `0` / `''`：`0` 会被后端入口拒成 `code 1000`，空串在`toSettlementParams` 被「空值不发送」丢掉（= 没筛，不是「未指定」——要「未指定」必须显式传 `'none'`）。 */
  order_source?: number | 'none' | null
  page?: number
  page_size?: number
}

/** 成员类型写成 `unknown`：`Set<unknown>.has` 接受任意值，这里要的正是纯值比较（同 `api/orders.ts`）。 */
const EMPTY_KEYS: Set<unknown> = new Set(['', undefined, null])

/** 组装查询参数：**空值不发送**（空串 / `undefined` / `null` 一律去掉）。
 * ⚠️ 判据是「空」而不是「假值」：`0` 要**原样发出去**让后端明确拒绝（`?user=0` ⇒ `code 1000`），
 * 不能在参数层被 `if (v)` 静默丢掉变成「没筛」（E-0021 的同族）。 */
export function toSettlementParams(
  params: SettlementSearchParams,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(params)) {
    if (EMPTY_KEYS.has(value)) continue
    if (typeof value === 'string' && value.trim() === '') continue
    out[key] = value as string | number
  }
  return out
}

/** 结算列表（一页）。 */
export async function fetchSettlements(
  params: SettlementSearchParams = {},
): Promise<SettlementPage> {
  return request<SettlementPage>({
    method: 'get',
    url: SETTLEMENTS_URL,
    params: toSettlementParams(params),
  })
}

/** 一次取全（**同一批筛选下的全部行**，分页循环）。为什么要有它：结算记录的当天明细把整份行交给客户端分页，行数还要和按天汇总对得上。只取第一页会缺行，缺行就是错的钱。`params` 里的 `page` / `page_size` 会被本函数覆盖（它自己管分页）——传进来也无效，调用方不必传。撞上 `MAX_SETTLEMENT_PAGES` **抛错**（同 `api/myOrders.ts` 的口径：失控要响亮，不许静默截断成「看起来只有这么多」）。⚠️ **后端 `total` 与实返不一致时也抛错**（本条原先是 `if (next.items.length === 0) break`，那是**静默截断**：`total` 说还有 300 条、第 2 页却返空 ⇒ 我们把已取到的部分行当全部返回，而缺行会让明细与汇总对不上）。取空页只在「已取满 `total`」时才会正常发生，而那种情况循环条件不成立、根本不会进来 ⇒ 进到空页分支就是**不一致**，一律抛错并写清数字。边界：`total` 为 0（或缺失且首屏空）时循环不进入，正常返回空数组。 */
export async function fetchAllSettlements(
  params: SettlementSearchParams = {},
): Promise<SettlementListItem[]> {
  const base: SettlementSearchParams = { ...params }
  delete base.page
  delete base.page_size
  const first = await fetchSettlements({ ...base, page: 1, page_size: SETTLEMENT_PAGE_SIZE })
  const items = [...first.items]
  // `total` 缺失时退回「首屏行数」＝只取这一页（老行为；后端 `EnvelopePagination` 恒有 `total`）。
  const total = first.total ?? items.length
  let page = 1
  while (items.length < total) {
    page += 1
    if (page > MAX_SETTLEMENT_PAGES) {
      throw new Error(
        `匹配的结算行数超过单次可汇总上限（${MAX_SETTLEMENT_PAGES * SETTLEMENT_PAGE_SIZE} 条），` +
          '请缩小时间范围或加上筛选条件后重试',
      )
    }
    const next = await fetchSettlements({
      ...base,
      page,
      page_size: SETTLEMENT_PAGE_SIZE,
    })
    if (next.items.length === 0) {
      // ⭐ **不静默**：`total` 与实返不一致，缺行会让按人求和的金额变小（错的钱）。
      throw new Error(
        `结算分页取全中断：已取到 ${items.length} 条，后端 total 报 ${total} 条，` +
          `但第 ${page} 页返回 0 条（total 与实返不一致）——不静默返回部分行`,
      )
    }
    items.push(...next.items)
  }
  return items
}

/* ────────────────── 按天聚合（D-0319，）────────────────── */

/** 按天聚合端点（`SettlementDailySummaryView`）。⚠️ **没有尾斜杠**（与 `/settlements` 同形）。契约出处：`docs/plans/2026-10-28-settlement-daily-view.md` §2.1。一行 = 一个结账日（`settled_at` 按 **UTC+8** 归天），只聚合**已结账**行；权限与数据范围与 `/settlements`完全同闸（`finance.payroll` + 同一套 `scope_queryset`）。 */
export const SETTLEMENT_DAILY_URL = '/api/business/settlements/daily-summary'

/** 按天聚合的一行（后端 `list` 里手工装的载荷）。⚠️ `date` 是 `YYYY-MM-DD` 字符串（后端 `date.isoformat()`，**业务时区 UTC+8** 的日期）；`total_amount` 是 `Sum(settled_amount)` 的两位小数字符串（实结快照合计，与 `SettlementListItem.settled_amount` 同源；存量行快照为 `null` ⇒ 后端按 0 计入合计）。`order_count` 是 **distinct 订单数**（一单多绩效行计 1）。 */
export type SettlementDailyItem = {
  date: string
  order_count: number
  total_amount: string
}

/** 按收入类型及结账时间范围筛选，日期两端均含。 */
export type SettlementDailyParams = {
  emp_type?: string | null
  date_from?: string | null
  date_to?: string | null
  page?: number
  page_size?: number
}

/** 按天汇总（一页；`EnvelopePagination` 的 `{items,total,page,page_size}` 形状）。 */
export async function fetchSettlementDailySummary(
  params: SettlementDailyParams = {},
): Promise<Page<SettlementDailyItem>> {
  return request<Page<SettlementDailyItem>>({
    method: 'get',
    url: SETTLEMENT_DAILY_URL,
    // `date_from` / `date_to` 与列表同名同语义 ⇒ 复用同一个折参函数（空值不发送）。
    params: toSettlementParams(params),
  })
}

/* ────────────── 按天按来源分解（D-0323，）────────────────── */

/** 来源分解端点（`SettlementDailySourcesView`）。⚠️ **没有尾斜杠**（与 `/settlements/daily-summary` 同形）。契约出处：业务方拍板（D-0323）——「总结算金额」列点击展开后按订单来源展示各自结算了多少笔、多少钱。聚合基集与 `daily-summary` **逐字同一套**（已结账 +`settled_at` 非空 + 两条软删排除 + UTC+8 归天 + distinct 订单数 + `Sum` NULL 忽略），只是 GROUP BY 到 `order_source`；权限与数据范围也完全同闸。排序（后端自拟、D-0323 登记）：金额降序 →「未指定」垫底 → 同额名称码点升序。 */
export const SETTLEMENT_DAILY_SOURCES_URL = '/api/business/settlements/daily-summary/sources'

/** 来源分解的一行（后端 `get` 里手工装的载荷）。⚠️ **来源为空**的行 `source_id` / `source_name` 都是 `null`（订单没配来源，或来源被删——FK 是 `SET_NULL`）⇒ 页面渲染「未指定」（⛔ 不要把 `null` 当 0 或空串当某家店）。`total_amount` 是两位小数字符串（`Sum(settled_amount)`，NULL 快照按 0 计入）；`order_count` 是 **distinct 订单数**（与主表「总单数」同口径——一单多绩效行计 1）。 */
export type SettlementSourceItem = {
  source_id: number | null
  source_name: string | null
  order_count: number
  total_amount: string
}

/** 某一天的来源分解。⚠️ 出参是 `data.items`（本端点**不分页**——一天的来源行数有界），
 * 不是 `Page<T>`——⛔ 不要套 `unwrapPage`（与 `fetchSettlementPeople` 同款形状）。
 * `date` 必填（`YYYY-MM-DD`）；非法 / 缺失由后端回 `code 1000`（页面侧只传主表给的日期）。 */
export async function fetchSettlementDailySources(
  date: string,
  empType?: string,
): Promise<{ items: SettlementSourceItem[] }> {
  return request<{ items: SettlementSourceItem[] }>({
    method: 'get',
    url: SETTLEMENT_DAILY_SOURCES_URL,
    params: empType === undefined ? { date } : { date, emp_type: empType },
  })
}

/* ────────────────── 「结账对象人员」候选（D-0089 / T5）────────────────── */

/** `/settlements/people` 的一行（三把键，见后端出参契约）。⚠️ `username` 是**登录账号**（既有契约，保留），`display_name` 是**加法的**那个键——昵称优先、空串回落登录账号（口径唯一出处 `apps/accounts/display.py::user_display_name`；SQL 注解侧对应物在 `apps/business/selectors.py` 的 `display_name_expression()`）。下拉显示 `display_name`、**`value` 仍是 `id`**。⚠️ 后端 **恒有**这个键；声明成**可选**是因为运行期仍按**部署窗口**回落（前端已上、后端没上时这个键真的缺席）：消费侧按 `p.display_name ?? p.username` 取值，⛔ 不要把它写成一个恒在的 `string` 再说谎（同文件 `SettlementListItem.display_name?` 同款）。 */
export type SettlementPerson = { id: number; username: string; display_name?: string }

/** 「结账对象人员」候选（D-0089）：**在结算数据里出现过的人**，可按游戏收窄。⚠️ **出参是 `data.items`（本端点不分页）**，不是 `Page<T>`——⛔ 不要套 `unwrapPage`。⚠️ `game` 只在**正整数**时才送：`?game=0` 会被后端入口（`min_value=1`）拒成 `code 1000`，而「全部游戏」的语义就是**不送该参数**（与 `toSettlementSearch` 的口径一致）。 */
export async function fetchSettlementPeople(game?: number): Promise<SettlementPerson[]> {
  const data = await request<{ items: SettlementPerson[] }>({
    method: 'get',
    url: SETTLEMENT_PEOPLE_URL,
    params: game !== undefined && game > 0 ? { game } : {},
  })
  return data.items
}

/* ────────────────── 结算预览（清算绩效子页，）────────────────── */

/** 结算预览端点（`SettlementPreviewView`）。⚠️ 同样**没有尾斜杠**（与 `/settlements/settle` 同形）。契约出处：`docs/plans/2026-10-03-settlement-emp-popup.md` §3 T2.1（`GET /api/business/settlements/preview?performance_ids=1,2,3`，权限 `finance.payroll`，只读：不写库、不写审计。权限 `finance.payroll`。 */
export const SETTLEMENTS_PREVIEW_URL = '/api/business/settlements/preview'

/** 预览里「结算对象」一项：按**归属人**聚合的应付净额（`payable_net` 之和）。 */
export type SettlementPreviewOwner = {
  /** 归属人主键；**`null` = 无归属人**那一批（后端把「`user` 为 NULL」的行并成一项、
   * **排在最后**——跳掉它会让 `owners` 的金额和与 `bonus_total` 对不上）。 */
  user_id: number | null
  /** 归属人用户名；无归属人那一项是 `null`（⛔ 别把 `null` 当空串渲染成「某人」）。 */
  username: string | null
  /** **显示名**（昵称优先、空串回落登录账号；后端 追加的键，无归属人是 `null`）。
   * ⚠️ 写成**可选**是因为它是**加法**：前端先上、后端还没上时这个键真的缺席
   * ⇒ 消费侧按 `name ?? username` 取值（`emp-settlementemp.ts::ownersText` 就是这么写的）。 */
  name?: string | null
  /** 金额（DRF `DecimalField` 的两位小数字符串，如 `"20.00"`，可为负）。 */
  amount: string
}

/** 预览里的一行误差：`payable_net` 算不出来的那一行（后端按 `0.00` 计入并在此列出）。 */
export type SettlementPreviewError = {
  /** 出错的那一行（绩效主键）。 */
  performance_id: number
  /** 该行自己报的业务码（`AmountError.code`，整数）。 */
  code: number
  /** 中文理由（原样，不换文案）。 */
  message: string
}

/** 预览里的微信收款码一项（`StaffProfile.wechat_qr_key` 的**签名 URL**）。 */
export type SettlementPreviewQrCode = {
  /** 归属人主键（后端只取 `user` 非空的人 ⇒ 这里不会是 `null`）。 */
  user_id: number
  /** 登录账号。⚠️ 类型留 `null` 是**宽于实现**的防御（后端当前恒给字符串，见其 `_qr_codes`）。 */
  username: string | null
  /** 收款码上的姓名（昵称优先、空串回落登录账号）；没有就是空串。 */
  name: string
  /** 可直接放进 `<img src>` 的签名 URL。 */
  url: string
}

/** `GET /api/business/settlements/preview` 的响应载荷（`data`）。⚠️ **契约里的 `count` 是「命中行数」**（查不到的 id 不计）——它是**笔数**那一格的值，⛔ 不是 `performance_ids` 参数的长度（对标的 `0笔` 就是这个口径：`ids=999999999` 查不到 ⇒ `0`）。⚠️ `temp_total` / `bonus_total` 是**两位小数字符串**（DRF `DecimalField` 的序列化结果），与 `SettlementListItem` 的金额字段同形。⚠️ `bonus_total` 是 **`Σ payable_net`**（= `bonus_amount − 扣点 + Σ未结账调整记录`），**不是** `Σ bonus_amount`——这是**有意偏离对标读数一处**（计划 §3 T2.1 已登记）：对标那格显示的是`Σ bonusAmount`，而我们真正要结的钱是 `payable_net`（`settle_performances` 结的就是它）；
 * 有扣点 / 调整时两者不同，显示 `Σ bonus` 就是一个**与实际付款不一致的数**。无扣点无调整时两者恒等 ⇒ 已观测到的对标读数全部复现。⚠️ `owners` 里**有一项永远是 `username: null`** 的可能：后端把「无归属人（`user` 为 NULL）」的行并成一项、**排在最后**（判据见后端 `_owner_sort_key`）——它让 `owners` 的金额之和与`bonus_total` 对得上。本页照它渲染（名字位留空），⛔ 不跳过、也不伪造成某个人。⚠️ `errors` 声明成**可选**：计划 §3 T2.1 的口径段要求「`payable_net` 抛 `AmountError` 的行按 `0.00`计入并在 `errors` 里如实列出」，同节的 JSON 样例里却没有这个键 ⇒ 声明成可选，后端回了就带上。**本页不消费它**（五格是给业务看的读数；逐行误差要查就去看响应/后端日志）。形状按后端实现钉：`{performance_id, code, message}`（不是 `{id, msg}`）。 */
export type SettlementPreview = {
  /** 命中行数（笔数格的判据：`>0` ⇒ `X.XX元`，`===0` ⇒ `0元`）。 */
  count: number
  /** `Σ temp_amount`（预计要结总额）。 */
  temp_total: string
  /** `Σ payable_net`（实际需结总额；口径见上）。 */
  bonus_total: string
  /** 命中行里已是「已结账」的条数（预览用，五格不显示）。 */
  settled_count: number
  /** 命中行里还是「未结账」的条数（预览用，五格不显示）。 */
  unsettled_count: number
  /** 按归属人聚合的 `payable_net`，**用户名升序**、无归属人那一项排最后（结算对象格）。 */
  owners: SettlementPreviewOwner[]
  /** 每个归属人的微信收款码；没有码的人不出现（空数组 = 对标 `data: []` 那个形态）。 */
  qr_codes: SettlementPreviewQrCode[]
  /** 见上：`payable_net` 算不出来的那些行（可选，本页不渲染）。 */
  errors?: SettlementPreviewError[]
}

/** 结算预览（`GET /api/business/settlements/preview`）。入参是**绩效行主键**的列表（`Performance.id`），与 `settlePerformances` / `revokeSettlements`同一套标识。⚠️ 传输形态是**逗号串**（`performance_ids=1,2,3`；后端是`apps/business/api/serializers.py` 的 `CommaSeparatedIntegerListField(serializers.ListField)`——`to_internal_value` 里对**字符串**先 `split(",")` 再交给 `ListField`，所以查询串里接受逗号串。这与两个写端点 body 里的 **JSON 数组**是**不同形**的入参契约，别混用参数模板，E-0007 同族）。⚠️ 失败一律由 `request()` 抛（`1000` 入参非法 / `401` / `403`），本函数**不吞、不换文案**——
 * 页面按自己的方式显示（见 `pages/emp-settlementemp.ts` 的文件头）。 */
export async function fetchSettlementPreview(
  performanceIds: readonly number[],
): Promise<SettlementPreview> {
  return request<SettlementPreview>({
    method: 'get',
    url: SETTLEMENTS_PREVIEW_URL,
    params: { performance_ids: performanceIds.join(',') },
  })
}

/* ────────────────────────── 结账（P4-FE-2）────────────────────────── */

/** `POST /api/business/settlements/settle` 的响应载荷（`SettleView.post` 的 `data`）。⚠️⚠️ **两个字段都是「id 的列表」，不是条数**——实测（后端探针，用后即删的 `tests/test_zz_probe_p4fe2_orphan_settle.py` 的 probe 4）：`data == {"settled_performances": [4], "settled_adjustments": []}`。依据在后端两处：`apps/business/settlement.py` 的 `SettleResult` dataclass 字段类型就是`list`（它的 docstring 明写「两者都是列表而不是计数：调用方要能回答『这一批到底结了哪几笔』」），`apps/business/api/settlement_views.py` 的 `SettleView.post` 把这两个列表**原样**塞进 `data`（没有 `len()`）。
 * ⇒ 要显示条数必须自己取 `.length`。上一版这里把它声明成 `number`，页面的结果提示因此会把**绩效主键**当成条数念出来（单条结账时显示「已结账 4 条绩效、」——数字看着像条数，其实是 id，调整记录那一槽还会是空的）。这是**读码 + 实测**两级证据，不是推断。列表里是**绩效 id**（`settled_performances`）与**调整记录 id**（`settled_adjustments`）：已结账的绩效行不会被重复计入前者（它被追加的新调整记录单独进后者）。 */
export type SettleResult = {
  settled_performances: number[]
  settled_adjustments: number[]
  /** 已是「已结账」、被跳过的绩效 id 列表。⚠️ 与上面两个字段**同形**（id 列表、不是计数，E-0060 同族）；「本来就是已结账」与「这个 id 根本没查到」是**两回事**——后者不进这里（后端那条口径见计划 §3 T2.2）。「清算绩效」子页的成功文案取它的 `.length` 如实报数（业务方 Q3=A）。 */
  skipped_performances: number[]
}

/** 结账（`POST /api/business/settlements/settle`）。入参是**绩效行主键**（`Performance.id`）的列表——不是订单号，也**不是**对标意义上的人。单条结账就是长度为 1 的列表（后端没有第二个端点）。⚠️ 失败一律由 `request()` 抛 `BizError`（业务码 `1006` 状态不允许 / `1011` 净额为负 /`1000` 入参非法，`msg` 是后端的中文原话）或 `HttpError`（401/403）。**本函数不吞、不换文案**——调用方的 `notifyError` 原样显示，⛔ 不要在这里做任何兜底改写。```ts
 * const r = await settlePerformances([17, 18])// → { settled_performances: [17, 18], settled_adjustments: [] }   // 列表，不是条数//   条数 = r.settled_performances.length
 * ``` */
export async function settlePerformances(
  performanceIds: number[],
  reason = '',
): Promise<SettleResult> {
  return request<SettleResult>({
    method: 'post',
    url: SETTLE_URL,
    // ⚠️ 键名逐字对齐 `SettleRequestSerializer`（`performance_ids` / `reason`）；
    //    `reason` 上限 255，空串是合法值（`allow_blank=True` 的默认值就是 `""`）。
    data: { performance_ids: performanceIds, reason },
  })
}

/* ────────────────────────── 撤销结账（P4FU-B，D-0072 ②）────────────────────────── */

/** 撤销结账的出参（`SettlementRevokeView.post` 的 `data`）。⚠️ 与 `SettleResult` **同形**（两个字段都是 **id 的列表**，不是计数；E-0060）——但**不复用那个类型**：两个端点的字段名不同（`settled_*` vs `revoked_*`），合成一个类型会让「哪个端点回了哪一组」在类型上消失。 */
export type RevokeResult = {
  revoked_performances: number[]
  revoked_adjustments: number[]
  /** 见 `SettleResult.skipped_performances`：**本来就是「未结账」**、被跳过的绩效 id 列表。 */
  skipped_performances: number[]
}

/** 撤销结账（`POST /api/business/settlements/revoke`）：**已结账 → 未结账**，逐 id fail-closed。⚠️ **`reason` 必填且非空**：后端 `RevokeSettleRequestSerializer` 覆盖了父类的`required=False, allow_blank=True, default=""`（D-0072 明文「必须填原因，并写进订单日志」）⇒ 空串会被拒成 **`1000`**。前端据此在弹层里**先挡一道**（原因必填），但**不复制**后端的长度上限（255）等其余判据——那些留在后端一处。方向只有「已结账 → 未结账」。混着未结账的行由后端判并回业务码，本函数不预筛。已经是未结账的 id 进 `skipped_performances`，不算失败。 */
export async function revokeSettlements(
  performanceIds: number[],
  reason: string,
): Promise<RevokeResult> {
  return request<RevokeResult>({
    method: 'post',
    url: SETTLE_REVOKE_URL,
    // 键名逐字对齐 `RevokeSettleRequestSerializer`（继承 `SettleRequestSerializer` 的
    // `performance_ids` + 覆盖出来的必填 `reason`）。
    data: { performance_ids: performanceIds, reason },
  })
}
