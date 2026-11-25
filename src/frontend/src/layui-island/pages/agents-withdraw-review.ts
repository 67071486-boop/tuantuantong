/**
 * 【财务相关 → 打手提现审核】island 页（island key = `agents-withdraw-review`，
 * 路由 `/agents/withdraw-review`，菜单 label「打手提现审核」）。
 *
 * 计划：`docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §5（前端范围）
 * ＋ **§4.3.1（响应契约——前后端并行施工用，改它要两边一起改）**。
 * 端点封装与行类型在 `@/api/withdrawals`（那里逐键登记了契约出处）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）没有「打手提现审核」这一页 ⇒ 本页**没有证据件**、也不走 `artifactPage`
 * / `mountConfigLike` 那条复刻专用链路（那两条是「取 `public/replica/<key>.html` → 注入 →
 * 灌数据」）。标记由本文件自己写，风格照同类的**自建** island 页
 * （`self-registration.ts` / `open-apps.ts` / `game-projectsetting.ts`：`layui-card` 的
 * 类名、`layui-btn` 的尺寸与配色、`layer.confirm` / `layer.prompt` 的用法）。
 * ⛔ 不要用 Element Plus 去仿（`AGENTS.md` §5-D）。
 *
 * ## 页面形状（计划 §5）
 *
 * 1. 顶部 **6 个状态 tab**：待审核 / 已通过 / 转账中 / 已打款 / **转账失败** / 已驳回。
 *    切换**只**改列表请求的 `state` 查询参数并重拉（计划 §5 原话；⛔ 不重载 iframe、
 *    ⛔ 不在前端按状态过滤——筛选是服务端的事，否则分页与 `total` 都会说谎）。
 *    ⭐ 「转账失败」是**只读** tab（业务方 2026-10-20 口头补充）：`failed` 是「系统打过但上游
 *    失败、钱**已经退回**打手余额」的终态，客服必须看得见「这笔试过没成功、为什么」，否则
 *    单子会凭空消失；但**不给任何操作按钮**（⛔ 不重试、⛔ 不登记打款——钱已退，要打只能走
 *    人工那条路，不在这页）。所以它只有状态列（`state_text` + 行里的 `transfer_error`）。
 * 2. 表格 7 列：申请时间 / 打手 / 金额 / 支付宝账号 / 姓名 / 状态 / 操作。
 * 3. 操作列**按状态与开关出现**（`actionCell`，纯函数、判据只有这一处）：
 *
 *    | 行状态 | 出现的动作 |
 *    |---|---|
 *    | `pending`（待审核） | 通过 / 驳回 |
 *    | `approved`（已通过） | **发起代付**（仅当 `auto_transfer_enabled === true` **且** 金额 ≤ `transfer_max_amount`）＋ 登记打款 |
 *    | `paying`（转账中）且 **`transfer_inflight === false`**（没有在途代付） | **登记打款** ＋ 刷新状态 |
 *    | `paying` 且 **`transfer_inflight === true`**（我方代付请求在途） | **只**刷新状态（钱可能已经在路上，⛔ 不许人工介入）＋ 状态列一句「正在发起代付，请稍后刷新」 |
 *    | `paid` / `rejected` | 无 |
 *    | `failed`（转账失败，**有 tab、只读**） | 无——钱已退回打手余额，⛔ 不在这里重试 / 登记打款 |
 *    | 其它未知值 | 无（**fail-closed**：后端将来新增状态时先什么都不给，⛔ 不猜） |
 *
 *    代付那颗按钮**不是「画出来再等后端拒」**：开关关着 / 金额超上限 / 金额不是合法十进制
 *    一律**不画**（D-0154 / E-0098 的同款 fail-closed）。
 *
 *    ⭐ **「转账中」必须能收场**（评审阻断项 1）：代付**发起失败**时单子会停在 `paying`，
 *    若这一行只给「刷新」，客服就永远没有出口。所以 `paying` 行按契约新字段
 *    `transfer_inflight` 分两种情况：**不在途**（`false`）⇒ 可登记人工打款收场；
 *    **在途**（`true`）⇒ 只给刷新（钱可能已经在路上，人工再打一笔就是双付）；
 *    **缺键**（前端新 / 后端旧）⇒ 按「不能确认不在途」fail-closed（不画登记打款 + 如实提示）。
 * 4. 四个写动作（通过 / 驳回 / 发起代付 / 登记打款）都要**二次确认**，且弹窗里把
 *    **金额 + 支付宝账号 + 姓名**摆出来（`subjectText`，纯函数）。驳回原因与人工打款单号
 *    **必填**——`layer.prompt` 收值，空串当场提示、**不发请求**（后端还会再判一次）。
 *    「刷新状态」是**读**语义的查单（计划 §4.3 的 `refresh/`），计划 §5 没要求它二次确认。
 * 5. 所有写动作**成功后重新拉列表**（一次动作之后同一状态桶里可能不止那一行变——后端打开
 *    列表时还会对 `paying` 单兜底查一遍）；**失败如实显示后端 `msg`**（`BizError.message`），
 *    ⛔ 前端不编第二句。
 *    ⭐ **代付的结果提示按返回的 `state` 说人话**（评审 A7）：`transfer/` 可能**立刻**回
 *    `paid` / `paying` / `failed`（失败时后端已退余额）⇒ 固定一句「请求已接受」会让客服以为
 *    钱在路上（`failed` 时其实是「没打出去、钱退回打手」）。三档文案见 `transferResultText`。
 *
 * ## 金额：字符串原样转发，比较只走整数分
 *
 * §4.3.1 原话「金额一律**字符串**……⛔ 客户端别 `parseFloat` 再算」；计划 §4.2 的硬闸是
 * 「调用方回传的 `confirm_amount` 必须等于单子金额」⇒ 代付请求里的 `confirm_amount`
 * 就是行里的 `amount` **一个字符都不动**（`transferWithdrawal(id, row.amount)`）。
 * 唯一需要「比大小」的地方是代付按钮的可见性 ⇒ `amountCents()` 把十进制串拆成**整数分**
 * （整数部分 × 100 ＋ 两位小数，⛔ 不经过浮点小数），再与上限的整数分比较。
 *
 * ## 权限门：fail-closed，且只有一道
 *
 * 路由 `meta.permissions` 已经把这一页挡住了（节点 `finance.taker_withdraw_review`）；
 * 页内这次 `fetchMe()` + `canNode()` 是**同一道门**在 iframe 深链那一侧的落实
 * （先例：`self-registration.ts` / `open-apps.ts` / `feedback.ts`——iframe 读不到 Vue 路由的
 * `meta`）。拿不到节点 ⇒ 提示一句、**不注入标记**、**不发任何提现审核请求**（列表与 5 个写动作
 * 一次都不发；`fetchMe()` 那一次身份请求照发——它就是这道门本身），于是也**一个操作按钮都不画**
 * （满足「拿不到权限就不画操作按钮」那一条）。⛔ 不要在这里另造第二套可见性判据。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(table.render as …)(…)`）；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ `root.innerHTML = buildHtml()`（再塞标记）
 *    → `initIslandElements(layui)`（最后按标记里的 `lay-*` 初始化）。
 * ⚠️ 三条都有**运行期**判据（`__tests__/agents-withdraw-review.interaction.spec.ts`），
 * 不是靠这段注释。
 */
import { fetchMe } from '@/api/auth'
import {
  approveWithdrawal,
  interruptWithdrawal,
  listWithdrawals,
  markWithdrawalPaid,
  refreshWithdrawal,
  rejectWithdrawal,
  transferWithdrawal,
  TAKER_WITHDRAW_NODE,
  type WithdrawListPage,
  type WithdrawRow,
  type WithdrawState,
} from '@/api/withdrawals'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'agents-withdraw-review'
export const TITLE = '打手提现审核'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'withdrawReviewPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 状态 tab 那一条 `<ul>` 的 `id`，以及每个 `<li>` 上承载状态值的属性名。 */
export const TABS_ID = 'withdrawReviewTabs'
export const TAB_ATTR = 'data-withdraw-state'

/**
 * 列表表的 id / elem（layui 的 `reloadData` / `on` 要的是 **id**，带 `#` 是静默 no-op）。
 * 表上的 `lay-filter` 与 id **同值**（`table.on('tool(<id>)')` 按它认领）。
 */
export const TABLE_ID = 'withdrawReviewTable'
export const TABLE_ELEM = `#${TABLE_ID}`

/** 七个 tab 的状态值与文案（顺序：计划 §5 的五个 + 业务方 2026-10-20 补的「转账失败」+ 2026-10-21 的「已中断」；⛔ 别调）。 */
export const STATUS_TABS: ReadonlyArray<{ state: WithdrawTabState; label: string }> = [
  { state: 'pending', label: '待审核' },
  { state: 'approved', label: '已通过' },
  { state: 'paying', label: '转账中' },
  { state: 'paid', label: '已打款' },
  // ⭐ 「转账失败」= `failed`（系统打过但上游失败、钱已退回打手余额）：**只读** tab，
  // 状态列显示中文名 + 失败原因，⛔ 没有任何操作按钮（业务方 2026-10-20 定）。
  { state: 'failed', label: '转账失败' },
  { state: 'rejected', label: '已驳回' },
  // ⭐ 「已中断」= `interrupted`：财务在打款前拦下、钱已退回余额；
  // 只读 tab，状态列显示中文名 + 中断原因。
  { state: 'interrupted', label: '已中断' },
]

/** tab 能选的状态（= 六个 tab 的值 = `WithdrawState` 的全部取值）。 */
export type WithdrawTabState = WithdrawState

/** 默认打开哪个 tab（待审核——客服的待办就在这一桶）。 */
export const DEFAULT_TAB_STATE: WithdrawTabState = 'pending'

/** 六个行内动作的 `lay-event` 名（一个不多一个不少，见文件头的表格）。 */
export const EVENTS = {
  approve: 'approve',
  reject: 'reject',
  transfer: 'transfer',
  refresh: 'refresh',
  markPaid: 'markpaid',
  interrupt: 'interrupt',
} as const
export type WithdrawEvent = (typeof EVENTS)[keyof typeof EVENTS]

/** 六颗按钮的文案（抽成常量只为让 spec 钉住字面值）。 */
export const BUTTON_TEXT = {
  approve: '通过',
  reject: '驳回',
  transfer: '发起代付',
  refresh: '刷新状态',
  markPaid: '登记打款',
  interrupt: '中断',
} as const

/** 状态 tab 上那句说明（说清「代付要不要出现」由服务端开关决定，⛔ 不是前端策略）。 */
export const TABS_HINT =
  '「发起代付」只在代付开关打开、且金额不超过单笔上限时出现；其余情况请人工打款后登记单号。' +
  '「转账失败」那一桶是只读的（钱已退回打手余额，要打只能走人工）。'

/**
 * 「转账中」那一行在途时的提示（`transfer_inflight === true`）——
 * 说清**为什么没有「登记打款」**：钱可能已经在路上，人工再打一笔就是双付。
 */
export const TRANSFER_INFLIGHT_HINT = '正在发起代付，请稍后刷新'

/**
 * 契约里 `transfer_inflight` 缺键时的提示（部署窗口：前端新、后端旧）。
 *
 * ⚠️ 缺键**不等于** `false`（`E-0021` 同族）：不能确认「没有在途代付」就不许人工介入
 * ⇒ 这一行同样**不画「登记打款」**，但要把原因说出来（⛔ 不静默按 `false` 处理——那会
 * 在代付请求真在路上的那几秒里把「登记打款」放出来，正是双付的形状）。
 */
export const TRANSFER_FLAG_MISSING_HINT = '后端未返回代付在途标记，暂不能登记打款；请刷新后再看'

/** 无节点时的自检提示（与路由那道门同源，⛔ 不是第二套判据；页面被深链打开时用）。 */
export const NO_ACCESS_MESSAGE = '无权限查看打手提现审核'

/** 页内用户可见文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  /** 无节点时的自检提示（= `NO_ACCESS_MESSAGE`，收在这里只为让文案集中）。 */
  noAccess: NO_ACCESS_MESSAGE,
  /** 后端文案没带过来时的兜底（后端拒的时候都会带一句中文，见 §4.3.1 的信封）。 */
  failed: '操作失败',
  /** 驳回原因空着就点确定（体验层预检；后端 §7 第 4 条同样回 400）。 */
  rejectReasonRequired: '请填写驳回原因（会退余额，也会写进这笔提现的记录）',
  /** 中断原因空着就点确定（体验层预检；后端同样回 400）。 */
  interruptReasonRequired: '请填写中断原因（会退余额，也会写进这笔提现的记录）',
  /** 人工打款单号空着就点确定（同上）。 */
  transferNoRequired: '请填写人工打款单号（银行 / 支付宝的转账流水号）',
  /** 通过成功。⚠️ 说清「还没打款」——「通过」不等于「钱到了」。 */
  approved: '已通过；接下来请发起代付或人工打款后登记单号',
  /** 驳回成功（后端已退余额，§3 的状态机）。 */
  rejected: '已驳回；这笔提现的余额已退回打手钱包',
  /** 中断成功（后端已退余额，2026-10-21）。 */
  interrupted: '已中断；这笔提现的余额已退回打手钱包',
  /**
   * 代付结果三档（评审 A7：按**返回的 `state`** 说人话，⛔ 不再固定一句「请求已接受」）。
   * ⚠️ 取值出处 = 计划 §2 的 `data.state` 语义与 §3 的状态机（`paid` / `paying` / `failed`）。
   */
  transferredPaid: '已打款',
  transferredPaying: '转账中，可稍后刷新状态',
  transferredFailed: '代付失败，金额已退回打手余额',
  /** 返回了一个本页还不认识的状态（后端将来新增）⇒ 不猜结论，只说「已提交」并让人刷新。 */
  transferredSubmitted: '代付请求已提交，请稍后刷新状态确认',
  /** 查单刷新成功（状态可能会变，列表会重拉）。 */
  refreshed: '已刷新转账状态',
  /** 登记人工打款成功。 */
  markPaidDone: '已登记为已打款',
  /** 单据太多、一页取不全时的**大声**提示（⛔ 不静默截断：少一条就是错的钱）。 */
  tooManyRows: '这个状态下的单据太多，页面一次取不全；请改用别的状态筛选后再看',
} as const

/**
 * 列表一页取多少条 / 最多翻多少页（翻页取全，然后交给 layui 在**客户端**分页）。
 *
 * ⚠️ 为什么取全：§4.3.1 的 `page_size` 默认 20，而页面要显示「共 N 条」的真实分页；
 * 只取第一页再把数组交给 `page: true` 会让第 2 页起**永远空**（少行 = 少钱，`AGENTS.md` §5-E）。
 * 上限 100 页 = 2000 行（提现单量级远小于它）；撞上上限**抛错**并如实提示，⛔ 不静默截断。
 */
export const WITHDRAW_PAGE_SIZE = 20
export const MAX_WITHDRAW_PAGES = 100

/** 「代付」按钮的可见性开关（页面从列表响应的两个开关位算出这一份）。 */
export type ActionCellContext = {
  /** 服务端开关（`auto_transfer_enabled`）；**按真布尔**判，只有 `=== true` 才算开。 */
  autoTransferEnabled: boolean
  /** 单笔上限（`transfer_max_amount` 的**原样字符串**）。 */
  transferMaxAmount: string
}

/**
 * 十进制金额串 → **整数分**（⛔ 不经过浮点小数，见文件头的「金额」段）。
 *
 * 合法形态：`^\d+(\.\d{1,2})?$`（后端给两位小数；这里也接受整数形，⛔ 不接受 `1e3` /
 * 负数 / 千分位 / 超过两位小数）。非法 ⇒ `null`（调用方 fail-closed：不画代付按钮）。
 *
 * ⚠️ `Number(整数部分) * 100 + Number(两位小数)` 对提现这种量级的金额是**精确**的
 * （整数部分远小于 2^53 / 100；乘法与加法都是整数运算），⛔ 不要改成 `parseFloat * 100`
 * ——那会在 `0.29` 一类值上引入 28.999999999999996 的坑，也是 §4.3.1 明令禁止的算法。
 */
export function amountCents(value: string): number | null {
  const matched = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value.trim())
  if (matched === null) return null
  const whole = Number(matched[1])
  const frac = (matched[2] ?? '').padEnd(2, '0')
  if (!Number.isSafeInteger(whole)) return null
  return whole * 100 + Number(frac)
}

/**
 * 「发起代付」该不该出现（纯函数，唯一判据）。
 *
 * 三个条件**缺一不可**：① 开关 `=== true`；② 金额与上限都能解析成整数分；
 * ③ 金额 ≤ 上限（计划 §9 第 1 条：超上限 ⇒ 页面只显示「登记打款」）。
 */
export function canAutoTransfer(
  row: Pick<WithdrawRow, 'amount'>,
  ctx: ActionCellContext,
): boolean {
  if (ctx.autoTransferEnabled !== true) return false
  const amount = amountCents(row.amount)
  const max = amountCents(ctx.transferMaxAmount)
  if (amount === null || max === null) return false
  return amount <= max
}

/** 一颗行内按钮的标记（`lay-event` 由 `table.on('tool(…)')` 认领）。 */
function actionButton(event: WithdrawEvent, text: string, extraClass = ''): string {
  const cls = `layui-btn layui-btn-xs${extraClass === '' ? '' : ` ${extraClass}`}`
  return `<a class="${cls}" lay-event="${event}">${text}</a>`
}

/**
 * 「转账中」这一行**能不能人工打款**（纯函数，判据只有这一处）。
 *
 * 只有**明确读到 `transfer_inflight === false`** 才允许（`E-0021`：边界判据用 `===`，
 * ⛔ 不用真值）；`true`（我方代付在途）与**缺键**（部署窗口 / 后端没回）都返回 `true`
 * = 挡住人工登记打款。理由：在途时人工再打一笔就是**双付**，而这个洞的代价比「多刷新一次」大得多。
 */
export function blocksManualPay(row: Pick<WithdrawRow, 'transfer_inflight'>): boolean {
  return row.transfer_inflight !== false
}

/**
 * 「操作」那一格（函数 `templet`，纯函数；**判据只有这一处**）。
 *
 * ⚠️ 每颗按钮**都没有** `id`：行是 layui 重铺的，逐行挂监听会随重铺丢掉 ⇒ 走
 * `table.on('tool(<id>)')` 的**事件委托**（与 `open-apps.ts` 同一做法）。
 * ⚠️ 未知状态（含后端新增的 `failed`）⇒ 空串：fail-closed，⛔ 不猜一个动作。
 * ⭐ `paying` 分两种（评审阻断项 1）：**不在途**才能登记人工打款收场；**在途**只给刷新。
 */
export function actionCell(row: WithdrawRow, ctx: ActionCellContext): string {
  if (row.state === 'pending') {
    return actionButton(EVENTS.approve, BUTTON_TEXT.approve) +
      actionButton(EVENTS.reject, BUTTON_TEXT.reject, 'layui-btn-danger')
  }
  if (row.state === 'approved') {
    const transfer = canAutoTransfer(row, ctx)
      ? actionButton(EVENTS.transfer, BUTTON_TEXT.transfer, 'layui-btn-normal')
      : ''
    return (
      transfer +
      actionButton(EVENTS.markPaid, BUTTON_TEXT.markPaid, 'layui-btn-primary') +
      // ⭐ 财务中断——打款前拦下，钱退回余额。
      actionButton(EVENTS.interrupt, BUTTON_TEXT.interrupt, 'layui-btn-danger')
    )
  }
  if (row.state === 'paying') {
    const refresh = actionButton(EVENTS.refresh, BUTTON_TEXT.refresh, 'layui-btn-primary')
    // 在途 / 缺键 ⇒ ⛔ 不画「登记打款」（见 `blocksManualPay`）。
    if (blocksManualPay(row)) return refresh
    return actionButton(EVENTS.markPaid, BUTTON_TEXT.markPaid, 'layui-btn-primary') + refresh
  }
  return ''
}

/** ISO 8601 → `2026-10-20 10:00:00`（只截到秒；⛔ 不按浏览器时区二次换算）。 */
export function formatTime(value: string): string {
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/** 打手那一格：显示名优先、空串回落登录账号（与钱包 / 绩效页同一口径）。 */
export function workerText(row: Pick<WithdrawRow, 'display_name' | 'username'>): string {
  return escapeHtml(row.display_name !== '' ? row.display_name : row.username)
}

/**
 * 「状态」那一格：中文状态名（**后端给的 `state_text`**，⛔ 前端不镜像词表）
 * ＋ 上游失败原因（有才显示，红字；它是排障时最要紧的一格）
 * ＋ 「转账中」在途时的灰色提示（`TRANSFER_INFLIGHT_HINT` / `TRANSFER_FLAG_MISSING_HINT`）。
 *
 * ⚠️ 在途提示放在**状态列**（不是操作列）：操作列保持「只有按钮」的既有形状，
 * 而「为什么这一行没有登记打款」正是状态的一部分。
 */
export function statusCell(row: WithdrawRow): string {
  const error =
    row.transfer_error === ''
      ? ''
      : `<div style="color:#ff5722;word-break:break-all">${escapeHtml(row.transfer_error)}</div>`
  const inflight = row.state === 'paying' ? payingHint(row) : ''
  const hint = inflight === '' ? '' : `<div style="color:#999">${escapeHtml(inflight)}</div>`
  // ⭐ 财务中断：原因显示在状态列（只对 interrupted 有值）。
  const interrupt =
    row.state === 'interrupted' && row.interrupt_reason !== ''
      ? `<div style="color:#ff5722;word-break:break-all">原因：${escapeHtml(row.interrupt_reason)}</div>`
      : ''
  return `<span>${escapeHtml(row.state_text)}</span>${error}${hint}${interrupt}`
}

/** 「转账中」那一行的在途提示（`true` / 缺键各一句，`false` 无）。纯函数。 */
export function payingHint(row: Pick<WithdrawRow, 'state' | 'transfer_inflight'>): string {
  if (row.state !== 'paying') return ''
  if (row.transfer_inflight === false) return ''
  if (row.transfer_inflight === true) return TRANSFER_INFLIGHT_HINT
  // 缺键 / 非布尔值：不能确认不在途 ⇒ 按「不许人工介入」处理，并把原因说出来。
  return TRANSFER_FLAG_MISSING_HINT
}

/**
 * 代付结果提示（评审 A7：按 `transfer/` 返回的 `state` 分三档说人话）。
 *
 * | 返回 `state` | 文案 | 为什么 |
 * |---|---|---|
 * | `paid` | 已打款 | 上游当场成功，钱已经到打手支付宝 |
 * | `paying` | 转账中，可稍后刷新状态 | 上游受理、结果未知（§2 判据 2），⛔ 不要说成成功 |
 * | `failed` | 代付失败，金额已退回打手余额 | 上游失败且后端已退余额（§3 状态机），客服要据此改走人工 |
 * | 其它 | 代付请求已提交，请稍后刷新状态确认 | 本页不认识的取值 ⇒ ⛔ 不猜结论 |
 */
export function transferResultText(result: unknown): string {
  const state = (result as { state?: unknown } | null | undefined)?.state
  if (state === 'paid') return MESSAGES.transferredPaid
  if (state === 'paying') return MESSAGES.transferredPaying
  if (state === 'failed') return MESSAGES.transferredFailed
  return MESSAGES.transferredSubmitted
}

/**
 * 二次确认弹窗里那句「这笔单子是谁的」——**金额 + 支付宝账号 + 姓名**（计划 §5 原话）。
 *
 * ⚠️ 服务端的文本/账号一律 `escapeHtml`：这段会进 `layer` 的 HTML 通道。
 * ⚠️ 金额**原样**贴（不换算、不补零），与请求里的 `confirm_amount` 是同一个字符串
 * ⇒ 用户核对的就是将要提交的那个值。
 */
export function subjectText(row: WithdrawRow): string {
  return (
    `金额 ${escapeHtml(row.amount)} 元、` +
    `支付宝账号 ${escapeHtml(row.alipay_account)}、` +
    `姓名 ${escapeHtml(row.alipay_name)}`
  )
}

/** 二次确认弹窗的标题 / 正文（纯函数；⛔ 文案别散落在 mount 里，spec 要逐句钉）。 */
export const CONFIRM_TEXT = {
  approveTitle: '通过提现申请',
  transferTitle: '发起支付宝代付',
  /** 通过：说清「通过 ≠ 打款」——下一步还要代付或登记单号。 */
  approve: (row: WithdrawRow): string =>
    `确定通过这笔提现吗？${subjectText(row)}。` +
    `通过只是审核通过，钱还没出去——接下来要发起代付，或人工打款后登记单号。`,
  /** 代付：说清「真出账 / 失败会退余额」（§3 的状态机）。 */
  transfer: (row: WithdrawRow): string =>
    `确定对这笔提现发起支付宝代付吗？${subjectText(row)}。` +
    `代付会真出账；上游受理后可能先落「转账中」，要刷新状态才能确认是否成功（失败会退余额）。`,
}

/**
 * 驳回的 `layer.prompt` 选项：**多行文本域**收原因（`formType: 2`）。
 *
 * ⚠️ 原因**必填**：空串/纯空白时页面提示「请填写驳回原因」并**不关层、不发请求**
 * （体验层预检；真判据在后端，§7 第 4 条）。弹窗里同样把三样摆出来（计划 §5）。
 */
export function rejectPromptOptions(row: WithdrawRow): Record<string, unknown> {
  return {
    formType: 2,
    title: `驳回这笔提现（${subjectText(row)}）。请填写驳回原因（必填，驳回后余额会退回打手钱包）：`,
    value: '',
    placeholder: '例如：收款账号与实名不一致，请核对后重新申请',
    area: ['460px', '200px'],
  }
}

/**
 * 财务中断的 `layer.prompt` 选项：原因必填、会退余额。
 *
 * ⚠️ 只对 `approved` 行出现（`actionCell` 的判据）；`pending` 请走驳回。
 */
export function interruptPromptOptions(row: WithdrawRow): Record<string, unknown> {
  return {
    formType: 2,
    title: `中断这笔提现（${subjectText(row)}）。请填写中断原因（必填，中断后余额会退回打手钱包）：`,
    value: '',
    placeholder: '例如：风控复核：该打手近期有退款争议，暂缓打款',
    area: ['460px', '200px'],
  }
}

/**
 * 登记人工打款的 `layer.prompt` 选项：**单行**收单号（`formType: 0`）。
 *
 * ⚠️ 单号**必填**（后端 `mark-paid` 缺它回 400，§7 第 4 条）；`remark` 本页不收
 * （计划 §5 只点名「人工打款单号必填」）⇒ 调用时**不发这个键**，⛔ 不替用户写一个空串。
 */
export function markPaidPromptOptions(row: WithdrawRow): Record<string, unknown> {
  return {
    formType: 0,
    title: `登记人工打款（${subjectText(row)}）。请填写打款单号（必填）：`,
    value: '',
    maxlength: 64,
    placeholder: '例如：支付宝 / 银行的转账流水号',
  }
}

/**
 * 页面标记（一个状态 tab + 一张表）。
 *
 * ⚠️ tab 的**每个 `<li>` 只有一个 `data-withdraw-state` 属性**（状态值进标记）：
 * 类名/选择器纪律（陷阱 ①）管的是「类名」，这里的属性名同样是**一个常量**
 * （`TAB_ATTR`），⛔ 不在标记里另写一个字面属性名。
 * ⚠️ 面板容器（`.layui-tab-content > .layui-tab-item`）**必须留**：`initIslandElements` 只接管
 * 「有面板可切」的 tab（`islandTabTarget` 的判据），没有它点 tab 连高亮都不动。
 * 六个 tab 共用**同一个**面板（数据是同一张表，按 `state` 换的只是请求参数）。
 */
export function buildHtml(): string {
  const tabs = STATUS_TABS.map(
    (tab, index) =>
      `<li${index === 0 ? ' class="layui-this"' : ''} ${TAB_ATTR}="${tab.state}">${tab.label}</li>`,
  ).join('')
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-tab layui-tab-brief" style="margin:0">
    <ul class="layui-tab-title" id="${TABS_ID}">${tabs}</ul>
    <div class="layui-tab-content" style="padding:0">
      <div class="layui-tab-item layui-show">
        <table id="${TABLE_ID}" lay-filter="${TABLE_ID}"></table>
        <div style="color:#999;margin-top:8px">${TABS_HINT}</div>
      </div>
    </div>
  </div>
</div>`
}

/**
 * 一次「翻页取全」的结果：行 + 两个开关位（开关位取**第一页**那份，整页一份）。
 */
export type WithdrawPageData = {
  rows: WithdrawRow[]
  context: ActionCellContext
}

/** 把一页响应收成 `ActionCellContext`（开关位**按真布尔**读，见计划 §4.3.1）。 */
export function toContext(page: WithdrawListPage): ActionCellContext {
  return {
    autoTransferEnabled: page.auto_transfer_enabled === true,
    transferMaxAmount: page.transfer_max_amount,
  }
}

/**
 * **翻页取全**（唯一的取数出口；`reload()` 调它）。
 *
 * ⚠️ `state` 一定带上（tab 切换 = 只改这个参数，见文件头）；`page` 从 1 开始。
 * ⚠️ 撞上 `MAX_WITHDRAW_PAGES` 还取不全 ⇒ **抛错**（`MESSAGES.tooManyRows`），
 * ⛔ 不静默截断（少一条就是少一笔钱，`AGENTS.md` §5-E 的「空结果不等于没有数据」同族）。
 */
export async function fetchAllWithdrawRows(
  state: WithdrawTabState,
  fetchPage: (params: {
    state: WithdrawTabState
    page: number
  }) => Promise<WithdrawListPage> = listWithdrawals,
): Promise<WithdrawPageData> {
  const first = await fetchPage({ state, page: 1 })
  const rows = [...first.items]
  const context = toContext(first)
  const total = first.total
  let page = 1
  while (rows.length < total && page < MAX_WITHDRAW_PAGES) {
    page += 1
    const next = await fetchPage({ state, page })
    if (next.items.length === 0) break
    rows.push(...next.items)
  }
  if (rows.length < total) throw new Error(MESSAGES.tooManyRows)
  return { rows, context }
}

/** 错误对象 → 用户可见文案（后端 `msg` 优先；兜底那句不带字段名，仅供意外分支）。 */
function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

export const __testables = {
  actionCell,
  amountCents,
  blocksManualPay,
  buildHtml,
  canAutoTransfer,
  errorText,
  fetchAllWithdrawRows,
  formatTime,
  markPaidPromptOptions,
  payingHint,
  rejectPromptOptions,
  statusCell,
  subjectText,
  toContext,
  transferResultText,
  workerText,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /**
     * ⚠️ **本页自己的门禁**（与路由那道门同源，⛔ 不是第二套判据）：iframe 可被深链
     * （`page.html?page=agents-withdraw-review`）打开，那一刻路由的 `meta.permissions`
     * 不生效（先例与理由：`pages/self-registration.ts` 的同名自检）。
     * ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
     * 拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳
     * （也会画出操作按钮——「拿不到节点就不画按钮」就落在这里）。
     */
    const me = await fetchMe()
    if (!canNode(me, TAKER_WITHDRAW_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 当前 tab 的状态（列表请求的 `state` 参数；切换只改它，见 `onTabClick`）。 */
    let currentState: WithdrawTabState = DEFAULT_TAB_STATE
    /** 列表响应的两个开关位（初始值 = 最保守：开关关、上限 0 ⇒ 不画代付按钮）。 */
    let context: ActionCellContext = { autoTransferEnabled: false, transferMaxAmount: '' }
    /**
     * 在途闸门（**按 行 + 动作** 一格一个）：防连点把同一笔写两次。
     * ⚠️ 它**不包住二次确认**——`layer.confirm` 只有点「确定」才走回调，把闸门压在它上面会让
     * 「点了取消」之后下一次点击静默无效（= 本仓反复堵的「点了没反应」，见 `open-apps.ts`）。
     */
    const inflight = new Set<string>()

    const reloadData = (data: WithdrawRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, { data })

    ;(table.render as (o: unknown) => void)({
      elem: TABLE_ELEM,
      id: TABLE_ID,
      // ⚠️ 先 render 一次（哪怕空数据）：实例是在 render 时注册的；数据用 `reloadData` 灌。
      data: [] as WithdrawRow[],
      defaultToolbar: [],
      // ⚠️ **客户端分页**：数据由 `fetchAllWithdrawRows()` 翻页取全后灌进来，
      //    layui 自己按 `limit` 切片（与 `emp-billsummary.ts` 右表同一做法）。
      page: true,
      limit: WITHDRAW_PAGE_SIZE,
      limits: [WITHDRAW_PAGE_SIZE, 50, 100],
      cols: [
        [
          {
            field: 'created_at',
            title: '申请时间',
            width: 170,
            templet: (d: WithdrawRow) => formatTime(d.created_at),
          },
          {
            field: 'display_name',
            title: '打手',
            width: 130,
            templet: (d: WithdrawRow) => workerText(d),
          },
          // ⚠️ 金额**原样**上屏（§4.3.1：字符串，⛔ 前端不换算）。
          { field: 'amount', title: '金额', width: 110 },
          {
            field: 'alipay_account',
            title: '支付宝账号',
            width: 180,
            templet: (d: WithdrawRow) => escapeHtml(d.alipay_account),
          },
          {
            field: 'alipay_name',
            title: '姓名',
            width: 110,
            templet: (d: WithdrawRow) => escapeHtml(d.alipay_name),
          },
          {
            field: 'state_text',
            title: '状态',
            width: 170,
            templet: (d: WithdrawRow) => statusCell(d),
          },
          // ⭐ 预计打款时间：快路 = 申请时刻，正常路 = 审核通过 + 3 天。
          {
            field: 'payout_after',
            title: '预计打款',
            width: 170,
            templet: (d: WithdrawRow) =>
              d.payout_after == null ? '' : formatTime(d.payout_after),
          },
          // 函数 `templet`（不是 `toolbar: '#id'`）：按钮组合**逐行不同**，见 `actionCell`。
          {
            title: '操作',
            width: 240,
            align: 'center',
            templet: (d: WithdrawRow) => actionCell(d, context),
          },
        ],
      ],
      text: { none: '这个状态下没有单据' },
    })

    /** 取当前 tab 的全部行并上屏（唯一的刷新出口；写动作成功后也走它）。 */
    async function reload(): Promise<void> {
      const data = await fetchAllWithdrawRows(currentState)
      context = data.context
      reloadData(data.rows)
    }

    /** 写动作前先 `trim` 一次（`layer.prompt` 的回调给的是输入框原值）。 */
    function promptValue(value: string): string {
      return value.trim()
    }

    /**
     * 一个写动作的**统一收口**：发请求 → 成功提示 → **重新拉列表**；失败如实显示后端 `msg`。
     *
     * ⚠️ 成功提示排在重新拉列表**之前**：刷新失败不该把「其实写成功了」这件事吞掉
     * （两件事都会各提示一句，第二句说明列表没刷新出来）。
     * ⚠️ `inflight` 的键是 `行 id + 动作`：同一行连点同一动作只发一次；别的行 / 别的动作照常。
     */
    async function runAction(
      key: string,
      action: () => Promise<unknown>,
      done: (result: unknown) => string,
    ): Promise<void> {
      if (inflight.has(key)) return
      inflight.add(key)
      let result: unknown
      try {
        result = await action()
      } catch (error) {
        msg(errorText(error))
        inflight.delete(key)
        return
      }
      try {
        msg(done(result))
        await reload()
      } catch (error) {
        msg(errorText(error))
      } finally {
        inflight.delete(key)
      }
    }

    /** 一次二次确认（`layer.confirm`；只有点「确定」才走 `action`）。 */
    function confirmThen(
      text: string,
      title: string,
      key: string,
      action: () => Promise<unknown>,
      done: (result: unknown) => string,
    ): void {
      ;(layer.confirm as (t: string, o: unknown, ok: () => void) => void)(
        text,
        { title },
        () => {
          void runAction(key, action, done)
        },
      )
    }

    /**
     * 一次「必填文本」弹窗（`layer.prompt`；读码确认它的 `yes` **不自动关层**）。
     *
     * ⚠️ 空串 / 纯空白 ⇒ 提示一句、**不关层、不发请求**（体验层预检；后端还会再判一次）。
     * ⚠️ 合法 ⇒ 先关层再发请求（与 `emp-settlementemp.ts` 的顺序一致）。
     */
    function promptThen(
      options: Record<string, unknown>,
      emptyText: string,
      key: string,
      build: (value: string) => Promise<unknown>,
      done: (result: unknown) => string,
    ): void {
      ;(
        layer.prompt as unknown as (
          o: unknown,
          cb: (value: string, index: number) => void,
        ) => void
      )(options, (value: string, index: number) => {
        const text = promptValue(value)
        if (text === '') {
          msg(emptyText)
          return
        }
        ;(layer.close as (index: number) => void)(index)
        void runAction(key, () => build(text), done)
      })
    }

    /** 通过（`pending → approved`）。 */
    function confirmApprove(row: WithdrawRow): void {
      confirmThen(
        CONFIRM_TEXT.approve(row),
        CONFIRM_TEXT.approveTitle,
        `${row.id}:${EVENTS.approve}`,
        () => approveWithdrawal(row.id),
        () => MESSAGES.approved,
      )
    }

    /** 驳回（`pending → rejected`，原因必填 + 退余额）。 */
    function promptReject(row: WithdrawRow): void {
      promptThen(
        rejectPromptOptions(row),
        MESSAGES.rejectReasonRequired,
        `${row.id}:${EVENTS.reject}`,
        (reason) => rejectWithdrawal(row.id, reason),
        () => MESSAGES.rejected,
      )
    }

    /** 财务中断（`approved → interrupted`，原因必填 + 退余额；2026-10-21）。 */
    function promptInterrupt(row: WithdrawRow): void {
      promptThen(
        interruptPromptOptions(row),
        MESSAGES.interruptReasonRequired,
        `${row.id}:${EVENTS.interrupt}`,
        (reason) => interruptWithdrawal(row.id, reason),
        () => MESSAGES.interrupted,
      )
    }

    /** 发起代付（`confirm_amount` = 行里 `amount` 的**原样字符串**，见文件头）。 */
    function confirmTransfer(row: WithdrawRow): void {
      confirmThen(
        CONFIRM_TEXT.transfer(row),
        CONFIRM_TEXT.transferTitle,
        `${row.id}:${EVENTS.transfer}`,
        () => transferWithdrawal(row.id, row.amount),
        // ⭐ 按返回的 `state` 说人话（评审 A7），⛔ 不再固定一句「请求已接受」。
        transferResultText,
      )
    }

    /** 查单刷新（**读**语义的查单，计划 §5 没要求二次确认）。 */
    function refreshStatus(row: WithdrawRow): void {
      void runAction(
        `${row.id}:${EVENTS.refresh}`,
        () => refreshWithdrawal(row.id),
        () => MESSAGES.refreshed,
      )
    }

    /**
     * 登记人工打款（单号必填；`remark` 不发，见 `markPaidPromptOptions`）。
     *
     * ⚠️ 「转账中」且**代付在途 / 无法确认不在途**时**直接挡掉**（连弹窗都不开）：
     * 按钮本来就不画（`actionCell`），这里挡的是**陈旧事件**（行数据被上一次渲染留在
     * `table.on` 的入参里）——在途时人工再打一笔就是双付。
     */
    function promptMarkPaid(row: WithdrawRow): void {
      if (row.state === 'paying' && blocksManualPay(row)) return
      promptThen(
        markPaidPromptOptions(row),
        MESSAGES.transferNoRequired,
        `${row.id}:${EVENTS.markPaid}`,
        (transferNo) => markWithdrawalPaid(row.id, transferNo),
        () => MESSAGES.markPaidDone,
      )
    }

    // ── 事件接线（tab 切换 / 行内动作）────────────────────────────────────────
    // tab 切换：**只**改列表请求的 `state` 并重拉（高亮由 `initIslandElements` 负责切类名）。
    root.querySelector<HTMLElement>(`#${TABS_ID}`)?.addEventListener('click', (event: MouseEvent) => {
      const li = (event.target as HTMLElement | null)?.closest<HTMLElement>(`[${TAB_ATTR}]`)
      if (!li) return
      const raw = li.getAttribute(TAB_ATTR)
      const next = STATUS_TABS.find((tab) => tab.state === raw)
      if (next === undefined || next.state === currentState) return
      currentState = next.state
      void reload().catch((error: unknown) => {
        msg(errorText(error))
      })
    })

    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `tool(${TABLE_ID})`,
      (obj) => {
        const row = (obj as { data: WithdrawRow }).data
        const event = (obj as { event: string }).event
        if (event === EVENTS.approve) return confirmApprove(row)
        if (event === EVENTS.reject) return promptReject(row)
        if (event === EVENTS.interrupt) return promptInterrupt(row)
        if (event === EVENTS.transfer) return confirmTransfer(row)
        if (event === EVENTS.refresh) return refreshStatus(row)
        if (event === EVENTS.markPaid) return promptMarkPaid(row)
      },
    )

    try {
      await reload()
    } catch (error) {
      msg(errorText(error))
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
