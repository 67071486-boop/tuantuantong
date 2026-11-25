/** 【财务相关 → 打手提现审核】页（`pages/agents-withdraw-review.ts`）的端点封装。契约出处：计划 `docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md`**§4.3（端点表）** 与 **§4.3.1（响应契约——前后端并行施工用，改它要两边一起改）**。⚠️ 后端实现是另一条线同一次施工（`src/apps/`），本模块**只按 §4.3.1 的字面形状**声明；实现即契约的那一半以 `apps/accounts/…` 的序列化器为准，两边漂移时先改 §4.3.1 再改这里。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/withdrawals/` | 列表（`?state=&page=`；打开时后端对 `paying` 单兜底查一遍） || POST | `…/<id>/approve/` | 通过（`pending → approved`） || POST | `…/<id>/reject/` | 驳回（`remark` **必填**，后端 400 业务码） || POST | `…/<id>/transfer/` | 发起支付宝代付（`confirm_amount` **必填**，= 单子金额的**原样字符串**） || POST | `…/<id>/refresh/` | 查单刷新（`paying → paid / failed`） || POST | `…/<id>/mark-paid/` | 登记人工打款（`transfer_no` **必填** + 可选 `remark`） |## 两条不能写错的纪律1. ⛔ **金额一律字符串、原样转发**（§4.3.1 原话：「金额一律**字符串**，与钱包端点同口径，⛔ 客户端别 `parseFloat` 再算」）。`transfer/` 的 `confirm_amount` 就是行里的 `amount`**一个字符都不动**——后端的硬闸是「调用方回传的金额必须等于单子金额」（计划 §4.2 第 2 条），前端先做一次数值换算就等于把这道防错闸架空了。
 * ⚠️ 页面上唯一需要「比较大小」的地方（金额 ≤ `transfer_max_amount` 才显示代付按钮）走**整数分**比较（见页面的 `amountCents`），仍然不经浮点。2. ⭐ **本页的节点 code 刻意住在本模块里**（⛔ 不在岛页里），与 `api/takerApps.ts` /`api/openApps.ts` 同一手法、同一理由：`src/tests/test_rbac_menu_alignment.py` 的源 3会把 `layui-island/pages/*.ts` 里出现的**带点字符串字面量**当成按钮级权限门收走、并**冻结条数** ⇒ 门常量放这里，那条冻结数就不必因为「多了一页」而变化。⚠️ 取值另有两条网钉着：`config/__tests__/routePermissions.spec.ts`（路由表 ↔ 后端目录）与 `router/__tests__/route-declarations.spec.ts` 的 (c)（菜单 ↔ 路由 `meta.permissions`）。
 * ⛔ **别把这个常量搬进岛页**：搬进去会让那张网的冻结数当场变（有意绊线，但搬一次就要同一次改 `src/tests/`，而那不在本线范围）。 */
import type { Page } from './unwrap'
import { request } from './http'

/** 本页自己的节点门（计划 §4.4）——与 `config/routePermissions.ts` 的`ROUTE_NODE_CODES['agents-withdraw-review']` **必须同值**。用途：岛页被**深链**（`page.html?page=agents-withdraw-review`）打开时自己判一次可见性（那一刻路由那道门不生效——先例与理由见 `pages/self-registration.ts` 的同名自检）。fail-closed：判不过就**不注入标记、一个请求都不发**（`AGENTS.md` §5-D / D-0154 / E-0098）。 */
export const TAKER_WITHDRAW_NODE = 'finance.taker_withdraw_review'

/** 端点基路径（尾斜杠与后端 `path("withdrawals/", …)` 的字面值一致）。 */
export const WITHDRAWALS_URL = '/api/business/withdrawals/'

/** `POST /api/business/withdrawals/<id>/<action>/` 的 URL（五个动作共用这一处拼装）。 */
export function withdrawalActionUrl(id: number, action: WithdrawAction): string {
  return `${WITHDRAWALS_URL}${id}/${action}/`
}

/** 六个写动作（端点表 §4.3 的第二列；`GET` 不在内；`interrupt` 为 ）。 */
export type WithdrawAction =
  | 'approve'
  | 'reject'
  | 'transfer'
  | 'refresh'
  | 'mark-paid'
  | 'interrupt'

/** 单据状态的**机器值**（§4.3.1 的 `state` 字段）。⚠️ 取值按计划的**状态机**（§3）：`pending` / `approved` / `paying` / `paid` / `rejected`＋ 本批新增的 `failed`（「转账失败（已退余额）」——与 `rejected` 分开，§3 的 ⭐ 两条）。⛔ 中文名**不在前端镜像**：载荷里已经有 `state_text`（后端 `get_state_display()`），前端再抄一份词表就是第二处出处（E-0021）。类型保留 `string` 兜底见 `WithdrawRow.state`。 */
export type WithdrawState =
  | 'pending'
  | 'approved'
  | 'paying'
  | 'paid'
  | 'rejected'
  | 'failed'
  | 'interrupted'

/** 列表里的一行（§4.3.1 的**行对象逐键**，⛔ 不多不少）。⚠️ 金额是**字符串**（`"88.00"`）——见文件头纪律 1。⚠️ `display_name` 是打手的显示名；空串时页面回落 `username`（与钱包 / 绩效页同一口径）。⚠️ `transfer_id` / `transfer_error` / `remark` 都是**必填键、可为空串**（后端序列化器逐键给）——「这个键不在」与「值是空的」是两件事，把缺键读成空串会让页面把「后端没回」显示成「没有」。 */
export type WithdrawRow = {
  id: number
  user_id: number
  username: string
  display_name: string
  /** 金额（**字符串**，两位小数；⛔ 不在前端换算成数再算钱）。 */
  amount: string
  alipay_account: string
  alipay_name: string
  state: WithdrawState
  /** 中文状态名（后端 `get_state_display()`；⛔ 前端不镜像词表）。 */
  state_text: string
  /** `manual`（人工打款）/ `jeepay_alipay`（支付宝代付）。 */
  channel: string
  /** 平台转账单号（对账用；`manual` / 未发起时是空串）。 */
  transfer_id: string
  /** 上游失败时的错误码 / 描述（成功 / 未发起时是空串）。 */
  transfer_error: string
  /** ⭐ **我方代付请求是否正在途中**（评审阻断项 1，2026-10-20 同批契约新增的布尔键）。`true` = 我们正在打上游的代付请求、钱可能已经在路上 ⇒ **不许人工介入**（页面只给「刷新状态」）；`false` = 没有在途请求（代付发起失败 / 从未发起）⇒「转账中」的行可以**登记人工打款**收场。⚠️ 没有它，代付发起失败的单子会永远卡在「转账中」（这是评审点名的那条阻断级缺陷）。⚠️ 契约里它是**必填布尔**；运行期缺键（部署窗口里前端新、后端旧）时页面按「不能确认不在途」**fail-closed**（不画「登记打款」并如实提示），见页面 `blocksManualPay`。 */
  transfer_inflight: boolean
  /** 驳回原因 / 人工打款备注（空串 = 没有）。 */
  remark: string
  /** ⭐ 前三单快路：`true` = 自动通过、跳过冷静期。 */
  fast_lane: boolean
  /** ⭐ 预计打款时间（快路 = 申请时刻；正常路 = 审核通过 + 3 天）。 */
  payout_after: string | null
  /** ⭐ 财务中断的时间与原因（只对 `interrupted` 有值）。 */
  interrupted_at: string | null
  interrupt_reason: string
  created_at: string
  reviewed_at: string | null
  paid_at: string | null
}

/** 列表载荷：仓内共享的 `EnvelopePagination` ＋ **两个开关位**（§4.3.1 明写「并多带一个开关位」）。⚠️ 两个开关位是**整页一份**（不是逐行）：`auto_transfer_enabled` 决定「发起代付」出不出现，`transfer_max_amount` 是单笔上限（计划 §9 第 1 条：默认 1000）。⚠️ `transfer_max_amount` 同样是**字符串**（`"1000.00"`）——比较时走整数分，见页面 `amountCents`。 */
export type WithdrawListPage = Page<WithdrawRow> & {
  auto_transfer_enabled: boolean
  transfer_max_amount: string
}

/** 列表的查询参数（§4.3：`?state=&page=`；`state` 省略 = 不筛）。 */
export type WithdrawListParams = {
  state?: WithdrawState
  page?: number
}

/** 组装查询参数（**唯一的线上形状出口**）：**没给**的键不发。
 * ⚠️ 判「要不要发这个键」一律用 `!== undefined`（`page` 的 `0` 不合法，但 `state` 是枚举、
 * 空串没有语义 ⇒ 这里两种都不靠真值判断，E-0021）。 */
export function toWithdrawListParams(
  params: WithdrawListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.state !== undefined) out.state = params.state
  if (params.page !== undefined) out.page = params.page
  return out
}

/** 列表（`GET /api/business/withdrawals/`；`state` 筛 + 分页，两个开关位随每页一起回）。 */
export function listWithdrawals(params: WithdrawListParams = {}): Promise<WithdrawListPage> {
  return request<WithdrawListPage>({
    method: 'get',
    url: WITHDRAWALS_URL,
    params: toWithdrawListParams(params),
  })
}

/** 动作端点回的三个键（§4.3.1 最后一行：`{"id": .., "state": .., "state_text": ..}`）。
 * ⚠️ 页面**不拿它上屏**：它只判成败，然后**重新拉一次列表**（一次动作之后同一状态桶里
 * 可能不止那一行变——例如后端打开列表时会对 `paying` 单兜底查一遍）。 */
export type WithdrawActionResult = {
  id: number
  state: WithdrawState
  state_text: string
}

/** `transfer/` 额外带两个键（§4.3.1 最后一行）；两者都可能是空串（`state=1` 时 `transfer_id` 有值、`transfer_error` 空）。 */
export type WithdrawTransferResult = WithdrawActionResult & {
  transfer_id: string
  transfer_error: string
}

/** 通过（`pending → approved`）。 */
export function approveWithdrawal(id: number): Promise<WithdrawActionResult> {
  return request<WithdrawActionResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'approve'),
  })
}

/** 驳回（`pending → rejected`，**退余额**）。
 * ⚠️ `remark` 是**必填**（后端 §4.2 / §7 第 4 条：缺原因回 400 业务码）；页面在弹窗里
 * 先做一次「空串不发请求」的体验层预检，文案带字段名（E-0074 口诀），⛔ 不替后端判。 */
export function rejectWithdrawal(id: number, remark: string): Promise<WithdrawActionResult> {
  return request<WithdrawActionResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'reject'),
    data: { remark },
  })
}

/** 财务中断（`approved → interrupted`，**退余额**；）。
 * ⚠️ 只对 `approved` 有效：`pending` 请走驳回（后端 `CONFLICT`）。
 * ⚠️ `reason` 必填（后端缺原因回 400 业务码）；页面弹窗先做一次空串预检。 */
export function interruptWithdrawal(
  id: number,
  reason: string,
): Promise<WithdrawActionResult> {
  return request<WithdrawActionResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'interrupt'),
    data: { reason },
  })
}

/** 发起支付宝代付（`approved → paying / paid / failed`）。
 * ⚠️ `confirmAmount` **必须是行里 `amount` 的原样字符串**（计划 §4.2 第 2 条：它等于
 * 「调用方回传的金额必须等于单子金额」那道防错闸的输入；前端换算一次就把闸架空了）。 */
export function transferWithdrawal(
  id: number,
  confirmAmount: string,
): Promise<WithdrawTransferResult> {
  return request<WithdrawTransferResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'transfer'),
    data: { confirm_amount: confirmAmount },
  })
}

/** 查单刷新（`paying → paid / failed`；**不排除**同单重复点，后端按状态机挡）。 */
export function refreshWithdrawal(id: number): Promise<WithdrawTransferResult> {
  return request<WithdrawTransferResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'refresh'),
  })
}

/** 登记人工打款（`approved / paying → paid`，**单号必填**）。
 * ⚠️ `remark` **可选**：`undefined` 时**不发这个键**（空串与「没填」在契约里不是一回事，
 * 后端 `remark` 允许空 ⇒ 但"没填"就让库里的默认值生效，别替用户写一个空串覆盖）。 */
export function markWithdrawalPaid(
  id: number,
  transferNo: string,
  remark?: string,
): Promise<WithdrawActionResult> {
  const data: Record<string, unknown> = { transfer_no: transferNo }
  if (remark !== undefined) data.remark = remark
  return request<WithdrawActionResult>({
    method: 'post',
    url: withdrawalActionUrl(id, 'mark-paid'),
    data,
  })
}
