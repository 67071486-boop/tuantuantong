/** 【财务相关 → 打手钱包管理】页（`pages/agents-wallet-adjust.ts`，两个 tab：钱包调账 / 押金规则）的端点封装。后端契约的**权威出处** = `src/apps/business/api/wallet_admin_views.py` 的模块 docstring与两个 ViewSet / APIView；本模块按那一份逐键声明（两边漂移时先改后端 docstring 再改这里）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/wallets/` | 打手钱包列表（`?q=` 搜账号 / 昵称 / 电话；`?sort=` / `?order=` 排序；分页） || POST | `/api/business/wallets/<user_id>/adjust/` | 调账（`target` / `direction` / `amount` / `reason` / `client_request_no`） |
 * | GET | `/api/business/deposit-rules/` | 每款游戏的押金金额（当前生效值 + 来源） || PUT | `/api/business/deposit-rules/<game_id>/` | 改某款游戏的押金金额（`amount`） || GET | `/api/business/wallet-transactions/` | 跨打手钱包流水（`q` / `kind` / `date_from` / `date_to`；分页，带 `kinds` 下拉项） |## 三条不能写错的纪律1. ⛔ **金额一律字符串、原样转发**（后端同口径：`Decimal` 不交给 JSONEncoder 变 `float`）。页面只在「调整前 → 调整后」的**预览**里比较 / 相加，算法走**整数分**（见页面的 `amountCents` / `previewAdjustment`），⛔ 不 `parseFloat` 后再算钱。2. ⭐ **`client_request_no` 是幂等键**（后端硬要求必填）：同一笔调账**重复提交只记一次账**。
 * 页面在**打开调账弹窗那一刻**生成一次、整段交互复用；⛔ 不在每次点「确定」时重新生成——那等于没有幂等。生成器见页面的 `newClientRequestNo()`。3. ⭐ **本页的节点 code 刻意住在本模块里**（⛔ 不在岛页里），与 `api/takerApps.ts` /`api/openApps.ts` / `api/withdrawals.ts` 同一手法：`tests/test_rbac_menu_alignment.py`的源 3 会把 `layui-island/pages/*.ts` 里的**带点**字符串字面量当按钮级权限门收走并冻结条数 ⇒ 门常量放这里，那条冻结数不必因为「多了一页」而变化。 */
import type { Page } from './unwrap'
import { request } from './http'

/** 本页自己的节点门（与 `config/routePermissions.ts` 的`ROUTE_NODE_CODES['agents-wallet-adjust']` **必须同值**）。用途：岛页被**深链**（`page.html?page=agents-wallet-adjust`）打开时自己判一次可见性（那一刻路由那道门不生效——先例见 `pages/self-registration.ts`）。fail-closed：判不过就**不注入标记、一个请求都不发**（`AGENTS.md` §5-D / D-0154 / E-0098）。 */
export const TAKER_WALLET_NODE = 'finance.taker_wallet_adjust'

/** 端点基路径（尾斜杠与后端 `router.register("wallets", …)` / `path("deposit-rules/")` 同形）。 */
export const WALLETS_URL = '/api/business/wallets/'
export const DEPOSIT_RULES_URL = '/api/business/deposit-rules/'
export const WALLET_TRANSACTIONS_URL = '/api/business/wallet-transactions/'

/** `POST /api/business/wallets/<user_id>/adjust/`。⚠️ `<user_id>` 就是打手的 `auth.User` 主键。 */
export function walletAdjustUrl(userId: number): string {
  return `${WALLETS_URL}${userId}/adjust/`
}

/** `PUT /api/business/deposit-rules/<game_id>/`。 */
export function depositRuleUrl(gameId: number): string {
  return `${DEPOSIT_RULES_URL}${gameId}/`
}

/** 调账的两个项目（机器值与后端 `ADJUST_TARGETS` 同值）。 */
export type AdjustTarget = 'balance' | 'frozen'
/** 调账的两个方向（机器值与后端 `ADJUST_DIRECTIONS` 同值）。 */
export type AdjustDirection = 'increase' | 'decrease'

/** 钱包列表的一行（后端 `_wallet_row` 逐键）。金额一律字符串。 */
export type WalletRow = {
  user_id: number
  username: string
  display_name: string
  mobile: string
  /** 可用余额（字符串，两位小数）。 */
  balance: string
  /** 押金冻结额（字符串，两位小数）。 */
  frozen: string
  /** 这个人**自己勾的游戏里要求最大**的那一档押金（字符串，两位小数；`'0.00'` = 接单不需要冻结）。
   * `null` = 他一款游戏都没勾 ⇒ 押金要求**未知**（⛔ 不是 0、也不是「不够」）。 */
  deposit_required: string | null
  /** `frozen >= deposit_required`——押金够不够接单；`null` = 未知（跟着 `deposit_required` 走）。 */
  deposit_enough: boolean | null
  updated_at: string | null
}

/** 钱包列表载荷：仓内共享分页信封 + 单笔调账上限（整页一份）。 */
export type WalletListPage = Page<WalletRow> & {
  /** 单笔调账上限（字符串）；空串 = 未配置上限。 */
  adjust_max_amount: string
}

/** 排序键 / 方向（与后端 `WALLET_SORT_CHOICES` / `WALLET_ORDER_CHOICES` 同值）。 */
export type WalletSort = 'frozen' | 'balance' | 'username'
export type WalletOrder = 'desc' | 'asc'

/** 列表查询参数（`q` 省略 = 不筛）。 */
export type WalletListParams = {
  q?: string
  page?: number
  page_size?: number
  sort?: WalletSort
  order?: WalletOrder
}

/** 组装查询参数（唯一出口）：**没给的键不发**；判据用 `!== undefined`（E-0021：
 * `page` 的 `0` 不合法、`q` 可能是空串，都不靠真值判断）。 */
export function toWalletListParams(params: WalletListParams = {}): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.q !== undefined) out.q = params.q
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  if (params.sort !== undefined) out.sort = params.sort
  if (params.order !== undefined) out.order = params.order
  return out
}

/** 列表（`GET /api/business/wallets/`）。 */
export function listWallets(params: WalletListParams = {}): Promise<WalletListPage> {
  return request<WalletListPage>({
    method: 'get',
    url: WALLETS_URL,
    params: toWalletListParams(params),
  })
}

/** 调账请求体（后端 `_AdjustSerializer` 逐键，⛔ 不多不少）。 */
export type WalletAdjustPayload = {
  target: AdjustTarget
  direction: AdjustDirection
  /** 变动量（字符串、> 0、最多两位小数）。 */
  amount: string
  /** 必填：进流水 `note` 与操作日志。 */
  reason: string
  /** 幂等键（必填）：同一次调账的重复提交必须带同一个值。 */
  client_request_no: string
}

/** 调账结果（后端 `_adjust_payload` 逐键；`duplicate=true` 说明命中幂等、这次没再记账）。 */
export type WalletAdjustResult = {
  user_id: number
  username: string
  display_name: string
  transaction_id: number
  kind: string
  amount: string
  reason: string
  balance_before: string
  balance_after: string
  frozen_before: string
  frozen_after: string
  duplicate: boolean
  created_at: string
}

/** 调账（`POST …/adjust/`）。 */
export function adjustWallet(
  userId: number,
  payload: WalletAdjustPayload,
): Promise<WalletAdjustResult> {
  return request<WalletAdjustResult>({
    method: 'post',
    url: walletAdjustUrl(userId),
    data: payload,
  })
}

/** 押金金额的**来源**（后端 `SOURCE_*`）：规则行 / 三角洲名字兜底 / 默认 50。 */
export type DepositSource = 'rule' | 'default_delta' | 'default'

/** 押金规则列表的一行（后端 `DepositRuleListView.get` 逐键）。 */
export type DepositRuleRow = {
  game_id: number
  game_name: string
  /** 当前**生效**金额（字符串）。 */
  amount: string
  source: DepositSource
  rule_id: number | null
  updated_at: string | null
}

/** 押金规则载荷：每款游戏一行 + 两档兜底值（页面的说明文案据此解释「为什么是 50 / 300」）。 */
export type DepositRulePage = {
  items: DepositRuleRow[]
  default_amount: string
  delta_amount: string
}

/** 押金规则列表（`GET /api/business/deposit-rules/`）。 */
export function listDepositRules(): Promise<DepositRulePage> {
  return request<DepositRulePage>({ method: 'get', url: DEPOSIT_RULES_URL })
}

/** 改押金后的回包（后端 `DepositRuleUpdateView.put` 逐键）。 */
export type DepositRuleUpdateResult = {
  game_id: number
  game_name: string
  amount: string
  source: DepositSource
  rule_id: number
  updated_at: string
  created: boolean
}

/** 改某款游戏的押金金额（`PUT …/deposit-rules/<game_id>/`）。
 * ⚠️ `amount` 是**字符串**（同纪律 1）；后端接受 0（= 接单不需要押金），拒绝负数。 */
export function updateDepositRule(
  gameId: number,
  amount: string,
): Promise<DepositRuleUpdateResult> {
  return request<DepositRuleUpdateResult>({
    method: 'put',
    url: depositRuleUrl(gameId),
    data: { amount },
  })
}

/** 流水明细的一行（后端 `_transaction_row` 逐键）。金额一律字符串。 */
export type WalletTransactionRow = {
  id: number
  user_id: number
  username: string
  display_name: string
  mobile: string
  /** 机器值（`WalletTransaction.KIND_CHOICES` 的 value）。 */
  kind: string
  /** 中文（后端 `get_kind_display()`，⛔ 前端不另抄词表）。 */
  kind_label: string
  amount: string
  balance_after: string
  frozen_after: string
  note: string
  ref_no: string
  created_at: string
}

/** 类型下拉的一项（后端把全部 `KIND_CHOICES` 随每页响应带回来）。 */
export type WalletKindOption = { value: string; label: string }

/** 流水明细载荷：分页信封 + `kinds`（下拉的**唯一出处**，⛔ 前端不另抄 13 种中文）。 */
export type WalletTransactionPage = Page<WalletTransactionRow> & {
  kinds: WalletKindOption[]
}

/** 流水查询参数（空串/`undefined` = 不筛）。 */
export type WalletTransactionListParams = {
  q?: string
  kind?: string
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}

/** 组装流水查询参数（唯一出口；判据一律 `!== undefined`）。 */
export function toWalletTransactionListParams(
  params: WalletTransactionListParams = {},
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (params.q !== undefined) out.q = params.q
  if (params.kind !== undefined) out.kind = params.kind
  if (params.date_from !== undefined) out.date_from = params.date_from
  if (params.date_to !== undefined) out.date_to = params.date_to
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  return out
}

/** 流水明细（`GET /api/business/wallet-transactions/`）。 */
export function listWalletTransactions(
  params: WalletTransactionListParams = {},
): Promise<WalletTransactionPage> {
  return request<WalletTransactionPage>({
    method: 'get',
    url: WALLET_TRANSACTIONS_URL,
    params: toWalletTransactionListParams(params),
  })
}
