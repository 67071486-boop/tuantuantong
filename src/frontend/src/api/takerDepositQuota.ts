/** 【人员管理 → 押金额度】子页（`pages/taker-deposit-quota.ts`）的端点封装。后端契约的权威出处 = `docs/plans/2026-10-23-taker-deposit-quota-plan.md` §5与 `src/apps/business/api/wallet_admin_views.py`（实现在客户端之后落地；两边漂移时先改后端再改这里）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/takers/<user_id>/deposit-quota/` | 某个打手的个人押金额度（开关 + 每款游戏一行） || PUT | 同上 | 保存开关 + 每款游戏的个人额度（`amounts` 是 game_id 字符串键 → 金额字符串） |## 三条不能写错的纪律1. ⛔ **金额一律字符串、原样转发**（后端同口径：`Decimal` 不交给 JSONEncoder 变 `float`）。页面只在**本地校验**里看形状（`>= 0`、最多两位小数），不做浮点运算。
 * 2. **`amount = null` 表示「没有个人行」**，不是 0：GET 里 `null` ⇒ 输入框显示空，保存时空输入框按 `"0"` 提交（0 = 该游戏免押，是**合法值**，E-0021 的边界）。3. ⭐ **节点 code 住在 API 模块里**（⛔ 不在岛页里）：`tests/test_rbac_menu_alignment.py`的源 3 会把 `layui-island/pages/*.ts` 里的**带点**字符串字面量当按钮级权限门收走并冻结条数 ⇒ 把字面量写进页面会让那条网无谓地 +1。手法照 `api/takerWallets.ts`的 `TAKER_WALLET_NODE`（同一节点、同一处置）。 */
import { request } from './http'

/** 本页自己的节点门（与后端 `has_node("finance.taker_wallet_adjust")` 同值）。
 * 复用的是打手钱包 / 全局押金规则那个**已有**节点（计划 §5.1：不新增 RBAC 节点）。
 * 用途：人员管理页按它决定「押金额度」按钮**画不画**（fail-closed，D-0154 / E-0098）。 */
export const TAKER_DEPOSIT_QUOTA_NODE = 'finance.taker_wallet_adjust'

/** `GET` / `PUT /api/business/takers/<user_id>/deposit-quota/`（尾斜杠与后端 `path(...)` 同形）。 */
export function depositQuotaUrl(userId: number): string {
  return `/api/business/takers/${userId}/deposit-quota/`
}

/** 一款游戏的一行（后端 GET 出参逐键）。金额一律字符串。 */
export type DepositQuotaGame = {
  game_id: number
  game_name: string
  /** 该游戏的**全局**押金额度（只读展示；开启个人额度后用不到它，但要给用户参照）。 */
  global_amount: string
  /** 个人行金额；**没有个人行 ⇒ `null`**（⛔ 不是 `'0.00'`）。 */
  amount: string | null
}

/** 某个打手的个人押金额度（后端 GET / PUT 出参逐键）。 */
export type DepositQuota = {
  user_id: number
  username: string
  display_name: string
  /** 个人押金额度开关。关 ⇒ 按全局；开 ⇒ 按 `games[].amount`（缺行 = 0 元）。 */
  enabled: boolean
  /** 只列这个打手**有权限且未软删**的游戏（与 App 钱包同一支取面）。 */
  games: DepositQuotaGame[]
}

/** 保存载荷（后端 PUT 入参逐键，⛔ 不多不少）。 */
export type DepositQuotaPayload = {
  enabled: boolean
  /** `game_id` 的**字符串**键 → 金额字符串（空输入框由页面归一成 `"0"`）。 */
  amounts: Record<string, string>
}

/** 「只改开关」的载荷（人员管理页那一列的行内开关）。⛔ **刻意没有 `amounts` 键**：后端把「`amounts` 缺席」定义为**不动任何额度行**（见 `wallet_admin_views._DepositQuotaSerializer`）。⛔ 别为了「对称」补一个`amounts: {}`——`{}` 与缺席在今天等价，但把语义压到「空对象」上，下一个人很容易顺手改成「传当前值」，那时一次开关就会连带把额度写一遍（E-0027 同型）。 */
export type DepositQuotaSwitchPayload = {
  enabled: boolean
}

/** 读某个打手的个人押金额度。 */
export function fetchDepositQuota(userId: number): Promise<DepositQuota> {
  return request<DepositQuota>({ method: 'get', url: depositQuotaUrl(userId) })
}

/** 保存某个打手的个人押金额度（回包与 GET 同形，方便页面刷新）。 */
export function saveDepositQuota(
  userId: number,
  payload: DepositQuotaPayload,
): Promise<DepositQuota> {
  return request<DepositQuota>({ method: 'put', url: depositQuotaUrl(userId), data: payload })
}

/** 只切换「启用个人押金额度」总闸（人员管理页的行内开关）。
 * 走**同一个** PUT 端点、只发 `{enabled}`：后端 `amounts` 缺席 ⇒ 一个
 * `TakerDepositQuota` 行都不动（钱相关，缺省 = 不改）。回包与 GET 同形。 */
export function setDepositQuotaEnabled(
  userId: number,
  enabled: boolean,
): Promise<DepositQuota> {
  const payload: DepositQuotaSwitchPayload = { enabled }
  return request<DepositQuota>({ method: 'put', url: depositQuotaUrl(userId), data: payload })
}
