/**
 * 【财务相关 → 打手钱包管理】island 页（island key = `agents-wallet-adjust`，
 * 路由 `/agents/wallet-adjust`，菜单 label「打手钱包管理」）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）没有「客服给打手加 / 减余额或押金」这一块，也没有「按游戏改押金金额」
 * 这一页 ⇒ 本页**没有证据件**、不走 `artifactPage` / `mountConfigLike` 那条复刻链路。
 * 标记由本文件自己写，风格照同类的**自建** island 页
 * （`agents-withdraw-review.ts` / `self-registration.ts` / `open-apps.ts`）。⛔ 不用 Element Plus。
 *
 * ## 页面形状（三个 tab，同一页）
 *
 * 1. **打手钱包**：搜索框（账号 / 昵称 / 电话）+ 表格（打手 / 电话 / 可用余额 / 押金冻结 /
 *    押金要求 / 能否接单 / 操作）。「调账」开一个自建弹层（`layer.open({type:1})`）：
 *    选调整项目（余额 / 押金冻结）、方向（加 / 减）、填金额与**必填原因**；弹层里实时显示
 *    **调整前 → 调整后**，点「保存」再走一次 `layer.confirm` **二次确认**。
 * 2. **押金金额**：每款游戏一行（游戏 / 当前押金 / 来源 / 更新时间 / 操作）。
 *    ⚠️ 页面上**必须**写清「改押金会直接影响谁能接单」（见 `DEPOSIT_WARNING`）——
 *    押金闸的判据是「打手冻结额 ≥ 该游戏押金」（`apps/business/deposit_gate.py` 与
 *    `wallet.py::ensure_deposit_for_claim`），一行改错会让一批打手接不了这款游戏。
 * 3. **流水明细**：搜索（打手）+ 类型下拉 + 起止日期；表列 = 时间 / 打手 /
 *    类型 / 金额 / 变动后余额 / 变动后冻结 / 说明 / 关联单号。类型下拉的选项**来自后端响应
 *    的 `kinds`**（`WalletTransaction.KIND_CHOICES` 是唯一出处，⛔ 前端不另抄 13 种中文）。
 *
 * ## 服务端分页 / 排序
 *
 * - 两张列表都是**服务端分页**（`page:false` + 上一页 / 下一页 + `第 x / y 页，共 N 人`），
 *   照 `xgj-receipts.ts` 的现成先例；`page_size` 一律 20。
 * - **搜索 / 排序 / 切 tab 一律回第 1 页**：否则「第 3 页再搜出 5 人」会稳定打越界。
 * - **调账成功后**按新的 `total / page_size` 把页码夹到末页；越界（HTTP 200 + 业务码 `1002`，
 *   ⛔ 不是 404）也接住、退一页重拉。
 * - 排序参数是**状态**（`walletSort` / `walletOrder`，缺省 `frozen` / `desc`）：两张表的
 *   「可用余额」「押金冻结」表头带 `sort:true`，`table.on('sort')` 把 `sort/order` 传给后端；
 *   搜索 / 调账重载都带上它们。
 * - 流水的类型 / 日期**改选即查**（照 `xgj-receipts.ts`，不设查询按钮）；搜索框按回车查。
 *
 * ## 幂等：客户端单号在**打开弹层那一刻**生成一次
 *
 * 后端硬要求 `client_request_no` 必填、同一单号重复提交**不重复记账**
 * （`wallet.py::adjust_wallet`）。本页把它存在 `currentClientRequestNo` 里，整段弹层交互
 * （含二次确认）复用同一个值；⛔ 不在点「确定」时重新生成——那等于没有幂等。
 * 弹层关闭后下一次打开才换新单号（新的一笔调账）。
 *
 * ## 金额：字符串原样转发，前端只在**预览**里比较 / 相加
 *
 * 后端金额一律字符串（`Decimal` 不交给 JSONEncoder 变 float）。本页需要相加减的地方只有
 * 「调整前 → 调整后」的预览 ⇒ 走 `amountCents()` 转**整数分**再算，⛔ 不 `parseFloat`。
 * 提交给后端的 `amount` 是输入框里的**原样字符串**（只 trim）。
 *
 * ## 权限门：fail-closed，且只有一道
 *
 * 路由 `meta.permissions` 已经把这一页挡住（节点 `finance.taker_wallet_adjust`）；
 * 页内 `fetchMe()` + `canNode()` 是**同一道门**在 iframe 深链那一侧的落实
 * （先例：`agents-withdraw-review.ts` / `self-registration.ts`）。拿不到节点 ⇒ 提示一句、
 * **不注入标记、不发任何钱包 / 押金请求**（`fetchMe()` 那一次身份请求照发——它就是这道门）。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（先挂样式）→ `root.innerHTML = buildHtml()`（再塞标记）
 *    → `initIslandElements(layui)`（最后按标记里的 `lay-*` 初始化）。
 * ⚠️ 三条都有**运行期**判据（`__tests__/agents-wallet-adjust.interaction.spec.ts`）。
 */
import { fetchMe } from '@/api/auth'
import { BizError } from '@/api/errors'
import {
  adjustWallet,
  listDepositRules,
  listWalletTransactions,
  listWallets,
  TAKER_WALLET_NODE,
  updateDepositRule,
  type AdjustDirection,
  type AdjustTarget,
  type DepositRuleRow,
  type WalletKindOption,
  type WalletOrder,
  type WalletRow,
  type WalletSort,
  type WalletTransactionRow,
} from '@/api/takerWallets'
import { ErrorCode } from '@/config/errorCodes'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'agents-wallet-adjust'
export const TITLE = '打手钱包管理'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'walletAdjustPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** tab 那一条 `<ul>` 的 `id`，以及每个 `<li>` 上承载 tab 值的属性名。 */
export const TABS_ID = 'walletAdjustTabs'
export const TAB_ATTR = 'data-wallet-tab'

/** 三张表的 id / elem（layui 的 `reloadData` / `on` 要的是 **id**，带 `#` 是静默 no-op）。 */
export const WALLET_TABLE_ID = 'walletAdjustTable'
export const WALLET_TABLE_ELEM = `#${WALLET_TABLE_ID}`
export const DEPOSIT_TABLE_ID = 'depositRuleTable'
export const DEPOSIT_TABLE_ELEM = `#${DEPOSIT_TABLE_ID}`
export const TRANSACTION_TABLE_ID = 'walletTransactionTable'
export const TRANSACTION_TABLE_ELEM = `#${TRANSACTION_TABLE_ID}`

/** 搜索框 / 搜索按钮 / 提示位的 id。 */
export const SEARCH_INPUT_ID = 'walletAdjustSearch'
export const SEARCH_BUTTON_ID = 'walletAdjustSearchBtn'
export const WALLET_HINT_ID = 'walletAdjustHint'
export const DEPOSIT_HINT_ID = 'depositRuleHint'

/** 钱包列表的翻页按钮 id（服务端分页；`page:false` + 手写上一页/下一页）。 */
export const WALLET_PREV_PAGE_ID = 'walletAdjustPrevPage'
export const WALLET_NEXT_PAGE_ID = 'walletAdjustNextPage'

/** 流水 tab 的筛选 / 表 / 提示 / 翻页节点 id。 */
export const TX_SEARCH_INPUT_ID = 'walletTxSearch'
export const TX_KIND_SELECT_ID = 'walletTxKind'
export const TX_DATE_FROM_ID = 'walletTxDateFrom'
export const TX_DATE_TO_ID = 'walletTxDateTo'
export const TX_HINT_ID = 'walletTxHint'
export const TX_PREV_PAGE_ID = 'walletTxPrevPage'
export const TX_NEXT_PAGE_ID = 'walletTxNextPage'

/** 调账弹层里的表单容器 id 与字段名。 */
export const ADJUST_FORM_ID = 'walletAdjustForm'
export const ADJUST_PREVIEW_ID = 'walletAdjustPreview'

/** 三个 tab 的值与文案（顺序固定）。 */
export type WalletTab = 'wallets' | 'deposit-rules' | 'transactions'
export const TABS: ReadonlyArray<{ key: WalletTab; label: string }> = [
  { key: 'wallets', label: '打手钱包' },
  { key: 'deposit-rules', label: '押金金额' },
  { key: 'transactions', label: '流水明细' },
]
export const DEFAULT_TAB: WalletTab = 'wallets'

/** 行内动作的 `lay-event` 名。 */
export const EVENTS = {
  adjust: 'adjust',
  editRule: 'editrule',
} as const
export type WalletEvent = (typeof EVENTS)[keyof typeof EVENTS]

/** 按钮文案（抽成常量只为让 spec 钉住字面值）。 */
export const BUTTON_TEXT = { adjust: '调账', editRule: '修改金额' } as const

/**
 * ⚠️ 押金页顶部的**必读**提示：改押金会直接改变「谁能接单」。
 * 业务方要求（本批任务书）：「改押金会影响『谁能接单』，文案要写明这一点」。
 */
export const DEPOSIT_WARNING =
  '注意：押金金额直接决定「谁能接这款游戏的单」——' +
  '打手的押金冻结额必须 ≥ 该游戏押金，否则他接不了这款游戏。改完会立即生效。'

/** 搜索框默认提示。 */
export const SEARCH_PLACEHOLDER = '输入打手账号 / 昵称 / 电话搜索'
/** 流水 tab 搜索框提示（改选即查；文本框按回车查）。 */
export const TX_SEARCH_PLACEHOLDER = '输入打手账号 / 昵称 / 电话，回车查询'

/** 钱包 / 流水列表一页取多少条（服务端分页；翻页条由页面自己画，⛔ 不再截断本页）。 */
export const WALLET_PAGE_SIZE = 20
export const TRANSACTION_PAGE_SIZE = 20

/** 无节点时的自检提示。 */
export const NO_ACCESS_MESSAGE = '无权限查看打手钱包管理'

/** 页内文案（抽成常量只为让 spec 钉住字面值）。 */
export const MESSAGES = {
  noAccess: NO_ACCESS_MESSAGE,
  failed: '操作失败',
  /** 原因空着就点保存（体验层预检；后端同样回 400）。 */
  reasonRequired: '请填写调账原因（会写进这笔流水与操作日志）',
  /** 金额格式不对（`amountCents` 的合法形态见那里）。 */
  amountInvalid: '请输入正确的金额（最多两位小数、大于 0）',
  /** 服务端上限（列表回 `adjust_max_amount`；空串 = 没配上限）。 */
  amountTooLarge: (limit: string): string => `单笔调账金额不能超过 ${limit} 元`,
  /** 扣成负数（体验层预检；后端同样回 400）。 */
  balanceNotEnough: '扣完会变成负数，这笔调不了',
  /** 没有选中项目 / 方向（正常不会发生；fail-closed 兜底）。 */
  targetInvalid: '请选择调整项目与方向',
  /** 调账成功（普通）。 */
  adjustDone: (text: string): string => `已调账：${text}`,
  /** 命中幂等：这次没有再次记账（页面要**说清**，否则客服会以为调了两次）。 */
  adjustDuplicate: '这笔调账已经记过账了，本次没有重复扣/加',
  /** 押金改成功。 */
  ruleDone: (name: string, amount: string): string => `已把「${name}」的押金改为 ${amount} 元`,
  /** 押金金额格式不对（允许 0：0 = 接单不需要押金）。 */
  ruleAmountInvalid: '请输入正确的押金金额（最多两位小数，不能为负数）',
} as const

/** 翻页条的文案（`第 x / y 页，共 N 人` / `… 笔`；⛔ 不再出现「本页只显示前 N 个」）。 */
export function pageHint(page: number, total: number, pageSize: number, unit: string): string {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  return `第 ${page} / ${totalPages} 页，共 ${total} ${unit}`
}

/** 十进制金额串 → **整数分**；非法（负数 / 超过两位小数 / 科学计数法 / 空）⇒ `null`。 */
export function amountCents(value: string): number | null {
  const matched = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value.trim())
  if (matched === null) return null
  const whole = Number(matched[1])
  const frac = (matched[2] ?? '').padEnd(2, '0')
  if (!Number.isSafeInteger(whole)) return null
  return whole * 100 + Number(frac)
}

/** 整数分 → 两位小数字符串（预览用；⛔ 不 `Number/100` 再 `toFixed`，那会引入浮点）。 */
export function centsText(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** 一次调账的预览：四个金额（字符串）与错误原因（`null` = 可以提交）。 */
export type AdjustPreview = {
  target: AdjustTarget
  direction: AdjustDirection
  amount: string
  balanceBefore: string
  balanceAfter: string
  frozenBefore: string
  frozenAfter: string
  error: string | null
}

/**
 * 「调整前 → 调整后」的**唯一**计算点（纯函数）。
 *
 * 判据：`amountCents` 必须解析成功且 > 0；扣减后不得为负（余额与冻结额各自判）。
 * ⚠️ 上限**不在这里**判：上限是整页一份的服务端配置（`adjust_max_amount`），由调用方传
 * `limitCents`；不传 = 不判（页面在没有列表数据时也能算预览）。
 */
export function previewAdjustment(
  row: Pick<WalletRow, 'balance' | 'frozen'>,
  target: AdjustTarget,
  direction: AdjustDirection,
  amount: string,
  limitCents: number | null = null,
): AdjustPreview {
  const amountC = amountCents(amount)
  const balanceC = amountCents(row.balance)
  const frozenC = amountCents(row.frozen)
  const base: AdjustPreview = {
    target,
    direction,
    amount: amount.trim(),
    balanceBefore: row.balance,
    balanceAfter: row.balance,
    frozenBefore: row.frozen,
    frozenAfter: row.frozen,
    error: null,
  }
  if (amountC === null || amountC <= 0 || balanceC === null || frozenC === null) {
    return { ...base, error: MESSAGES.amountInvalid }
  }
  if (limitCents !== null && amountC > limitCents) {
    return { ...base, error: MESSAGES.amountTooLarge(centsText(limitCents)) }
  }
  const delta = direction === 'increase' ? amountC : -amountC
  if (target === 'balance') {
    const after = balanceC + delta
    if (after < 0) return { ...base, error: MESSAGES.balanceNotEnough }
    return { ...base, balanceAfter: centsText(after) }
  }
  const after = frozenC + delta
  if (after < 0) return { ...base, error: MESSAGES.balanceNotEnough }
  return { ...base, frozenAfter: centsText(after) }
}

/** 预览的两行文字（弹层里显示；⛔ 不把 HTML 拼进业务文案——外层再 escape）。 */
export function previewText(preview: AdjustPreview): string {
  const direction = preview.direction === 'increase' ? '增加' : '减少'
  const target = preview.target === 'balance' ? '可用余额' : '押金冻结额'
  return (
    `${target}${direction} ${preview.amount} 元：` +
    `${preview.balanceBefore} → ${preview.balanceAfter}（余额）；` +
    `${preview.frozenBefore} → ${preview.frozenAfter}（冻结）`
  )
}

/** 二次确认正文（弹层里那句话；含打手与原因）。 */
export function confirmText(row: WalletRow, preview: AdjustPreview, reason: string): string {
  // ⚠️ `workerText`（display_name 无字符白名单）与 `reason`（客服自由输入）都会进
  // `layer.confirm`，而 vendored layui 把 content **当 HTML 渲染** ⇒ 必须过 `escapeHtml`：
  // 否则持「账号编辑」节点的人写一个昵称就能在管理员会话里执行脚本（G3）。
  return (
    `确定给「${escapeHtml(workerText(row))}」调账吗？调整前 → 调整后：${previewText(preview)}；` +
    `原因：${escapeHtml(reason)}。调账会立即改这个打手的钱包，且会写进流水与操作日志。`
  )
}

/** 打手那一格：显示名优先、空串回落登录账号（与钱包 / 提现页同一口径）。 */
export function workerText(row: Pick<WalletRow, 'display_name' | 'username'>): string {
  return row.display_name !== '' ? row.display_name : row.username
}

/**
 * 「押金要求」那一格：金额原样上屏；**未知**（这个打手一款游戏都没勾）写「未知」。
 *
 * ⚠️ 后端此时回 `null`（⛔ 不是 0）：0 是「接这单不需要冻结」，与「我们不知道他要多少」是
 * 两件事——写成 0 会让客服以为他不必补冻。
 */
export function depositRequiredText(row: Pick<WalletRow, 'deposit_required'>): string {
  return row.deposit_required === null ? '未知' : escapeHtml(row.deposit_required)
}

/** 「能否接单」那一格：够 / 不够 / 未知（要求未知时 ⛔ 不写「不够」，那会骗客服去补冻）。 */
export function depositEnoughText(row: Pick<WalletRow, 'deposit_enough'>): string {
  if (row.deposit_enough === null) return '未知'
  return row.deposit_enough ? '够' : '不够'
}

/** 押金来源的中文说明（机器值 → 人话；⛔ 不在弹窗之外另造第二套词表）。 */
export function depositSourceText(source: DepositRuleRow['source']): string {
  if (source === 'rule') return '后台规则'
  if (source === 'default_delta') return '三角洲默认 300'
  return '默认 50'
}

/** ISO 8601 → `2026-10-20 10:00:00`（只截到秒；⛔ 不按浏览器时区二次换算）。 */
export function formatTime(value: string | null): string {
  if (value === null || value === '') return ''
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/** 钱包列表行（金额字符串原样上屏；操作列一颗「调账」）。 */
export function walletTableData(rows: WalletRow[]): Array<Record<string, unknown>> {
  return rows.map((row) => ({ ...row }))
}

/** 押金规则行（金额字符串原样上屏）。 */
export function depositTableData(rows: DepositRuleRow[]): Array<Record<string, unknown>> {
  return rows.map((row) => ({ ...row }))
}

/** 流水行（原样上屏；时间 / 打手由列的 templet 处理）。 */
export function transactionTableData(
  rows: WalletTransactionRow[],
): Array<Record<string, unknown>> {
  return rows.map((row) => ({ ...row }))
}

/**
 * 类型下拉的 `<option>` 们（**唯一出处是后端响应的 `kinds`**；空值 = 全部类型）。
 * ⚠️ 中文标签⛔ 不在前端另抄一份：后端的 `kinds` 直接来自 `WalletTransaction.KIND_CHOICES`。
 */
export function kindOptionsHtml(kinds: WalletKindOption[], selected = ''): string {
  const head = '<option value="">全部类型</option>'
  const rest = kinds.map(
    (item) =>
      `<option value="${escapeHtml(item.value)}"` +
      `${item.value === selected ? ' selected' : ''}>${escapeHtml(item.label)}</option>`,
  )
  return [head, ...rest].join('')
}

/** 调账弹层的标记（唯一一处拼它；字段名与后端 `_AdjustSerializer` 同值）。 */
export function adjustDialogHtml(row: WalletRow): string {
  return `
<div id="${ADJUST_FORM_ID}" class="layui-form" style="padding:14px 16px 2px">
  <div style="margin-bottom:10px">
    打手：<b>${escapeHtml(workerText(row))}</b>（${escapeHtml(row.username)}）<br>
    当前：可用余额 ${escapeHtml(row.balance)} 元 / 押金冻结 ${escapeHtml(row.frozen)} 元
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">调整项目</label>
    <div class="layui-input-block">
      <select name="target" class="layui-input">
        <option value="balance">可用余额</option>
        <option value="frozen">押金冻结额</option>
      </select>
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">方向</label>
    <div class="layui-input-block">
      <select name="direction" class="layui-input">
        <option value="increase">增加</option>
        <option value="decrease">减少</option>
      </select>
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">金额</label>
    <div class="layui-input-block">
      <input name="amount" class="layui-input" placeholder="例如 100（元）" autocomplete="off">
    </div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">原因</label>
    <div class="layui-input-block">
      <textarea name="reason" class="layui-textarea" maxlength="200"
        placeholder="必填：例如「补发 10-20 活动奖励」"></textarea>
    </div>
  </div>
  <div id="${ADJUST_PREVIEW_ID}" style="color:#666;margin:6px 0 2px">调整前 → 调整后：请先填金额</div>
</div>`
}

/**
 * 页面标记（两个 tab + 两张表 + 搜索框）。
 *
 * ⚠️ tab 的每个 `<li>` 只有一个 `data-wallet-tab` 属性（属性名同样是**一个常量** `TAB_ATTR`）。
 * ⚠️ 面板容器（`.layui-tab-content > .layui-tab-item`）**必须留**：`initIslandElements` 只接管
 * 「有面板可切」的 tab（`islandTabTarget` 的判据），没有它点 tab 连高亮都不动。
 * ⚠️ `lay-filter` 与表 id 同值（`table.on('tool(<id>)')` 按它认领）。
 */
export function buildHtml(): string {
  const tabs = TABS.map(
    (tab, index) =>
      `<li${index === 0 ? ' class="layui-this"' : ''} ${TAB_ATTR}="${tab.key}">${tab.label}</li>`,
  ).join('')
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-tab layui-tab-brief" style="margin:0">
    <ul class="layui-tab-title" id="${TABS_ID}">${tabs}</ul>
    <div class="layui-tab-content" style="padding:0">
      <div class="layui-tab-item layui-show">
        <div style="margin:8px 0">
          <input id="${SEARCH_INPUT_ID}" class="layui-input" style="width:280px;display:inline-block"
            placeholder="${SEARCH_PLACEHOLDER}" autocomplete="off">
          <button id="${SEARCH_BUTTON_ID}" class="layui-btn layui-btn-sm">搜索</button>
          <span id="${WALLET_HINT_ID}" style="color:#999;margin-left:10px"></span>
        </div>
        <table id="${WALLET_TABLE_ID}" lay-filter="${WALLET_TABLE_ID}"></table>
        <div style="margin-top:6px">
          <button id="${WALLET_PREV_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">上一页</button>
          <button id="${WALLET_NEXT_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">下一页</button>
        </div>
      </div>
      <div class="layui-tab-item">
        <div style="color:#ff5722;margin:8px 0">${DEPOSIT_WARNING}</div>
        <table id="${DEPOSIT_TABLE_ID}" lay-filter="${DEPOSIT_TABLE_ID}"></table>
        <div id="${DEPOSIT_HINT_ID}" style="color:#999;margin-top:8px"></div>
      </div>
      <div class="layui-tab-item">
        <div style="margin:8px 0">
          <input id="${TX_SEARCH_INPUT_ID}" class="layui-input" style="width:220px;display:inline-block"
            placeholder="${TX_SEARCH_PLACEHOLDER}" autocomplete="off">
          <select id="${TX_KIND_SELECT_ID}" class="layui-input" style="width:140px;display:inline-block">
            ${kindOptionsHtml([])}
          </select>
          <input type="date" id="${TX_DATE_FROM_ID}" class="layui-input" style="width:150px;display:inline-block">
          <span style="margin:0 6px">至</span>
          <input type="date" id="${TX_DATE_TO_ID}" class="layui-input" style="width:150px;display:inline-block">
          <span id="${TX_HINT_ID}" style="color:#999;margin-left:10px"></span>
        </div>
        <table id="${TRANSACTION_TABLE_ID}" lay-filter="${TRANSACTION_TABLE_ID}"></table>
        <div style="margin-top:6px">
          <button id="${TX_PREV_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">上一页</button>
          <button id="${TX_NEXT_PAGE_ID}" class="layui-btn layui-btn-sm layui-btn-primary">下一页</button>
        </div>
      </div>
    </div>
  </div>
</div>`
}

/** 错误对象 → 用户可见文案（后端 `msg` 优先；兜底那句不带字段名，仅供意外分支）。 */
function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : MESSAGES.failed
}

/**
 * 客户端单号生成器（幂等键）：优先 `crypto.randomUUID()`，没有就退化成
 * `adj-<时间戳>-<随机>`。⚠️ 只在**打开调账弹层**时调用一次，见文件头「幂等」段。
 */
export function newClientRequestNo(): string {
  const cryptoObj = globalThis.crypto as Crypto | undefined
  if (cryptoObj !== undefined && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  return `adj-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`
}

/** 在弹层里读字段（`layer.open` 的 `yes` 回调里用；返回 trim 过的原值）。 */
export function readAdjustForm(host: HTMLElement): {
  target: AdjustTarget
  direction: AdjustDirection
  amount: string
  reason: string
} {
  const value = (name: string): string =>
    (host.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      `[name="${name}"]`,
    )?.value ?? '').trim()
  const targetRaw = value('target')
  const directionRaw = value('direction')
  return {
    target: targetRaw === 'frozen' ? 'frozen' : 'balance',
    direction: directionRaw === 'decrease' ? 'decrease' : 'increase',
    amount: value('amount'),
    reason: value('reason'),
  }
}

export const __testables = {
  adjustDialogHtml,
  amountCents,
  buildHtml,
  centsText,
  confirmText,
  depositEnoughText,
  depositRequiredText,
  depositSourceText,
  depositTableData,
  errorText,
  formatTime,
  kindOptionsHtml,
  newClientRequestNo,
  pageHint,
  previewAdjustment,
  previewText,
  readAdjustForm,
  transactionTableData,
  walletTableData,
  workerText,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（陷阱 ③）：先挂样式，再塞标记，最后 `initIslandElements`。
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer', 'form'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const form = layui.form as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    // ⚠️ 门禁必须排在注入标记**之前**（理由见文件头「权限门」段）。
    const me = await fetchMe()
    if (!canNode(me, TAKER_WALLET_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 当前 tab（只影响拉哪份数据，见 `switchTab`）。 */
    let currentTab: WalletTab = DEFAULT_TAB
    /** 钱包当前的关键词、页码、排序与整页上限（上限由列表响应给；`null` = 没配）。 */
    let keyword = ''
    let walletPage = 1
    let walletTotal = 0
    /** 排序是**状态**（缺省 frozen desc）：搜索 / 调账重载都必须带上它。 */
    let walletSort: WalletSort = 'frozen'
    let walletOrder: WalletOrder = 'desc'
    let adjustLimitCents: number | null = null
    /** 流水的独立筛选与页码（改选即查；与钱包的页码互不影响）。 */
    let txKeyword = ''
    let txKind = ''
    let txDateFrom = ''
    let txDateTo = ''
    let txPage = 1
    let txTotal = 0
    /** 当前打开的调账弹层的行 + 幂等键（幂等键在**打开时**生成一次，见文件头）。 */
    let adjusting: { row: WalletRow; clientRequestNo: string; dialogIndex: number | null } | null =
      null
    /** 防连点：写请求在途时不再发第二次（键 = 行 + 动作）。 */
    const inflight = new Set<string>()

    const walletSearch = (): HTMLElement | null =>
      root.querySelector<HTMLElement>(`#${SEARCH_INPUT_ID}`)
    const hint = (id: string, text: string): void => {
      const node = root.querySelector<HTMLElement>(`#${id}`)
      if (node !== null) node.textContent = text
    }

    // ── 两张表 ──────────────────────────────────────────────────
    ;(table.render as (o: unknown) => void)({
      elem: WALLET_TABLE_ELEM,
      id: WALLET_TABLE_ID,
      data: [] as WalletRow[],
      defaultToolbar: [],
      page: false,
      // 排序交给后端（`table.on('sort')`）：本表自己的本地排序会把「当前这一页」重排，
      // 与「翻到别的页」不是一回事（⛔ 不打开自动排序）。
      autoSort: false,
      // 首屏的排序箭头与真实请求一致（缺省 frozen desc）；否则表头看起来「没排序」。
      initSort: { field: 'frozen', type: 'desc' },
      cols: [
        [
          {
            field: 'display_name',
            title: '打手',
            width: 140,
            templet: (d: WalletRow) =>
              `${escapeHtml(workerText(d))}<div style="color:#999">${escapeHtml(d.username)}</div>`,
          },
          {
            field: 'mobile',
            title: '联系电话',
            width: 130,
            templet: (d: WalletRow) => escapeHtml(d.mobile),
          },
          // ⭐ 只有这两列可排序（业务方要按押金冻结 / 可用余额看谁充过值、谁冻结过押金）。
          { field: 'balance', title: '可用余额', width: 110, sort: true },
          { field: 'frozen', title: '押金冻结', width: 110, sort: true },
          {
            field: 'deposit_required',
            title: '押金要求',
            width: 110,
            templet: (d: WalletRow) => depositRequiredText(d),
          },
          {
            field: 'deposit_enough',
            title: '能否接单',
            width: 100,
            templet: (d: WalletRow) => {
              const text = depositEnoughText(d)
              // 未知（没勾游戏）走灰字，⛔ 不染成红色（红 = 不够，会让人去补冻）。
              const color =
                d.deposit_enough === null ? '#999' : d.deposit_enough ? '#16b777' : '#ff5722'
              return `<span style="color:${color}">${text}</span>`
            },
          },
          {
            title: '操作',
            width: 110,
            align: 'center',
            templet: () =>
              `<a class="layui-btn layui-btn-xs" lay-event="${EVENTS.adjust}">${BUTTON_TEXT.adjust}</a>`,
          },
        ],
      ],
      text: { none: '没有找到打手' },
    })
    ;(table.render as (o: unknown) => void)({
      elem: DEPOSIT_TABLE_ELEM,
      id: DEPOSIT_TABLE_ID,
      data: [] as DepositRuleRow[],
      defaultToolbar: [],
      page: false,
      cols: [
        [
          { field: 'game_name', title: '游戏', width: 220 },
          { field: 'amount', title: '当前押金（元）', width: 150 },
          {
            field: 'source',
            title: '金额来源',
            width: 160,
            templet: (d: DepositRuleRow) => escapeHtml(depositSourceText(d.source)),
          },
          {
            field: 'updated_at',
            title: '最后修改',
            width: 170,
            templet: (d: DepositRuleRow) => formatTime(d.updated_at),
          },
          {
            title: '操作',
            width: 130,
            align: 'center',
            templet: () =>
              `<a class="layui-btn layui-btn-xs" lay-event="${EVENTS.editRule}">${BUTTON_TEXT.editRule}</a>`,
          },
        ],
      ],
      text: { none: '没有游戏' },
    })
    ;(table.render as (o: unknown) => void)({
      elem: TRANSACTION_TABLE_ELEM,
      id: TRANSACTION_TABLE_ID,
      data: [] as WalletTransactionRow[],
      defaultToolbar: [],
      page: false,
      cols: [
        [
          {
            field: 'created_at',
            title: '时间',
            width: 165,
            templet: (d: WalletTransactionRow) => formatTime(d.created_at),
          },
          {
            field: 'display_name',
            title: '打手',
            width: 140,
            templet: (d: WalletTransactionRow) =>
              `${escapeHtml(workerText(d))}<div style="color:#999">${escapeHtml(d.username)}</div>`,
          },
          {
            field: 'kind_label',
            title: '类型',
            width: 120,
            templet: (d: WalletTransactionRow) => escapeHtml(d.kind_label),
          },
          { field: 'amount', title: '金额', width: 110 },
          { field: 'balance_after', title: '变动后余额', width: 120 },
          { field: 'frozen_after', title: '变动后冻结', width: 120 },
          { field: 'note', title: '说明', minWidth: 180 },
          { field: 'ref_no', title: '关联单号', width: 150 },
        ],
      ],
      text: { none: '没有流水' },
    })

    const reloadWallets = (rows: WalletRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(WALLET_TABLE_ID, { data: rows })
    const reloadDeposit = (rows: DepositRuleRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(DEPOSIT_TABLE_ID, { data: rows })
    const reloadTransactions = (rows: WalletTransactionRow[]): void =>
      (table.reloadData as (id: string, o: unknown) => void)(TRANSACTION_TABLE_ID, { data: rows })

    /** 翻页按钮的禁用态（首页不能上一页、末页不能下一页）。 */
    const setDisabled = (id: string, disabled: boolean): void => {
      const node = root.querySelector<HTMLButtonElement>(`#${id}`)
      if (node !== null) node.disabled = disabled
    }

    /** 翻页条的唯一渲染点：`第 x / y 页，共 N 人|笔` + 两端按钮禁用。 */
    const renderPager = (
      prevId: string,
      nextId: string,
      hintId: string,
      page: number,
      total: number,
      pageSize: number,
      unit: string,
    ): void => {
      hint(hintId, pageHint(page, total, pageSize, unit))
      const lastPage = Math.max(1, Math.ceil(total / pageSize))
      setDisabled(prevId, page <= 1)
      setDisabled(nextId, page >= lastPage)
    }

    /** 越界（HTTP 200 + 业务码 1002）——分页页码超出末页时的**唯一**可重试分支。 */
    const isPageOutOfRange = (error: unknown): boolean =>
      error instanceof BizError && error.code === ErrorCode.BUSINESS

    /**
     * 拉钱包（唯一出口；搜索 / 排序 / 翻页 / 写动作成功后都走它）。
     *
     * `retryOnOutOfRange`：页码越界时退一页重拉（调账后总人数可能变少；⛔ 不把 1002 当成
     * 「没有权限」或「空列表」）。
     */
    async function loadWallets(retryOnOutOfRange = true): Promise<void> {
      try {
        const page = await listWallets({
          q: keyword,
          page: walletPage,
          page_size: WALLET_PAGE_SIZE,
          sort: walletSort,
          order: walletOrder,
        })
        adjustLimitCents = amountCents(page.adjust_max_amount)
        walletTotal = page.total
        walletPage = page.page
        reloadWallets(page.items)
        renderPager(
          WALLET_PREV_PAGE_ID,
          WALLET_NEXT_PAGE_ID,
          WALLET_HINT_ID,
          page.page,
          page.total,
          WALLET_PAGE_SIZE,
          '人',
        )
      } catch (error) {
        if (retryOnOutOfRange && walletPage > 1 && isPageOutOfRange(error)) {
          walletPage -= 1
          return loadWallets(false)
        }
        throw error
      }
    }

    /** 调账成功后把页码夹到**末页**（人数变少时最多退到最后一页，⛔ 不留在空白页）。 */
    function clampWalletPage(): void {
      const lastPage = Math.max(1, Math.ceil(walletTotal / WALLET_PAGE_SIZE))
      walletPage = Math.min(walletPage, lastPage)
    }

    /** 把后端 `kinds` 灌进类型下拉（**中文标签的唯一出处**是响应，⛔ 前端不另抄）。 */
    function applyKindOptions(kinds: WalletKindOption[]): void {
      const node = root.querySelector<HTMLSelectElement>(`#${TX_KIND_SELECT_ID}`)
      if (node === null) return
      node.innerHTML = kindOptionsHtml(kinds, txKind)
      // 选项变了要重新渲染 layui 的 select 皮肤。
      ;(form.render as (type?: string) => void)('select')
    }

    /** 拉流水（当前筛选 + 当前页；越界同钱包那样退一页重拉）。 */
    async function loadTransactions(retryOnOutOfRange = true): Promise<void> {
      try {
        const page = await listWalletTransactions({
          q: txKeyword,
          kind: txKind,
          date_from: txDateFrom,
          date_to: txDateTo,
          page: txPage,
          page_size: TRANSACTION_PAGE_SIZE,
        })
        txTotal = page.total
        txPage = page.page
        reloadTransactions(page.items)
        applyKindOptions(page.kinds)
        renderPager(
          TX_PREV_PAGE_ID,
          TX_NEXT_PAGE_ID,
          TX_HINT_ID,
          page.page,
          page.total,
          TRANSACTION_PAGE_SIZE,
          '笔',
        )
      } catch (error) {
        if (retryOnOutOfRange && txPage > 1 && isPageOutOfRange(error)) {
          txPage -= 1
          return loadTransactions(false)
        }
        throw error
      }
    }

    /** 拉押金规则（懒加载；改完金额后也走它）。 */
    async function loadDepositRules(): Promise<void> {
      const page = await listDepositRules()
      reloadDeposit(page.items)
      hint(
        DEPOSIT_HINT_ID,
        `兜底金额：默认 ${page.default_amount} 元 / 三角洲 ${page.delta_amount} 元；` +
          '没有单独规则的游戏按兜底值生效。',
      )
    }

    // ── 写动作收口 ──────────────────────────────────────────────
    /**
     * 一次写请求的统一收口：防连点 → 成功提示 → 重新拉当前 tab 的数据。
     *
     * 返回 **是否真的成功**：调账弹层据此决定「关不关」——失败时**不关**，
     * 这样用户再点一次「保存」仍带**同一个 `client_request_no`**（幂等键），
     * 上一次若其实已经记账（只是回包丢了）就不会重复记第二笔。
     */
    async function runWrite(
      key: string,
      action: () => Promise<unknown>,
      done: (result: unknown) => string,
      reload: () => Promise<void>,
    ): Promise<boolean> {
      if (inflight.has(key)) return false
      inflight.add(key)
      try {
        const result = await action()
        msg(done(result))
        await reload()
        return true
      } catch (error) {
        msg(errorText(error))
        return false
      } finally {
        inflight.delete(key)
      }
    }

    /** 弹层里的「调整前 → 调整后」实时预览（值随输入变；判据只有 `previewAdjustment` 一处）。 */
    function renderPreview(host: HTMLElement): AdjustPreview | null {
      if (adjusting === null) return null
      const data = readAdjustForm(host)
      const preview = previewAdjustment(
        adjusting.row,
        data.target,
        data.direction,
        data.amount,
        adjustLimitCents,
      )
      const node = host.querySelector<HTMLElement>(`#${ADJUST_PREVIEW_ID}`)
      if (node !== null) {
        node.textContent =
          data.amount === '' ? '调整前 → 调整后：请先填金额' : `调整前 → 调整后：${previewText(preview)}`
        node.style.color = preview.error === null ? '#666' : '#ff5722'
      }
      return preview
    }

    /** 打开调账弹层（**幂等键在这里生成一次**）。 */
    function openAdjust(row: WalletRow): void {
      adjusting = { row, clientRequestNo: newClientRequestNo(), dialogIndex: null }
      const index = (layer.open as (o: unknown) => number)({
        type: 1,
        title: `调账：${escapeHtml(workerText(row))}`,
        area: ['560px', 'auto'],
        shade: [0.3, '#333'],
        shadeClose: false,
        skin: 'layui-layer-lan',
        content: adjustDialogHtml(row),
        btn: ['保存', '取消'],
        yes: (layerIndex: number) => {
          const host = document.getElementById(ADJUST_FORM_ID)
          if (host === null || adjusting === null) return
          const data = readAdjustForm(host)
          const preview = previewAdjustment(
            adjusting.row,
            data.target,
            data.direction,
            data.amount,
            adjustLimitCents,
          )
          if (data.reason === '') {
            msg(MESSAGES.reasonRequired)
            return
          }
          if (preview.error !== null) {
            msg(preview.error)
            return
          }
          // ⭐ 二次确认：把「调整前 → 调整后」原样摆出来（业务方要求）。
          ;(layer.confirm as (t: string, o: unknown, ok: () => void) => void)(
            confirmText(adjusting.row, preview, data.reason),
            { title: '确认调账' },
            () => {
              const current = adjusting
              if (current === null) return
              void runWrite(
                `${current.row.user_id}:${EVENTS.adjust}`,
                () =>
                  adjustWallet(current.row.user_id, {
                    target: data.target,
                    direction: data.direction,
                    amount: data.amount,
                    reason: data.reason,
                    client_request_no: current.clientRequestNo,
                  }),
                (result) => {
                  const payload = result as {
                    duplicate?: boolean
                    balance_before?: string
                    balance_after?: string
                    frozen_before?: string
                    frozen_after?: string
                  }
                  if (payload.duplicate === true) return MESSAGES.adjustDuplicate
                  return MESSAGES.adjustDone(
                    `${payload.balance_before ?? ''} → ${payload.balance_after ?? ''}（余额）；` +
                      `${payload.frozen_before ?? ''} → ${payload.frozen_after ?? ''}（冻结）`,
                  )
                },
                async () => {
                  // ⭐ 调账成功后按新的 total 把页码夹到末页，再重拉（⛔ 不留在空白页）。
                  clampWalletPage()
                  await loadWallets()
                },
              ).then((ok) => {
                // ⚠️ 只有**成功**才关层并丢弃幂等键：失败时留着弹层，
                //    重试仍用同一个 `client_request_no`（见 `runWrite` 的 docstring）。
                if (!ok) return
                ;(layer.close as (i: number) => void)(layerIndex)
                adjusting = null
              })
            },
          )
        },
        end: () => {
          adjusting = null
        },
      })
      if (adjusting !== null) adjusting.dialogIndex = index
      // 预览随输入实时更新（事件委托在弹层容器上）。
      const host = document.getElementById(ADJUST_FORM_ID)
      if (host !== null) {
        host.addEventListener('input', () => renderPreview(host))
        host.addEventListener('change', () => renderPreview(host))
      }
      ;(form.render as (type?: string) => void)('select')
    }

    /** 改押金金额（`layer.prompt` 收金额 → 二次确认（写明影响接单）→ PUT）。 */
    function editDepositRule(row: DepositRuleRow): void {
      ;(
        layer.prompt as unknown as (
          o: unknown,
          cb: (value: string, index: number) => void,
        ) => void
      )(
        {
          formType: 0,
          title:
            `修改「${escapeHtml(row.game_name)}」的押金金额（当前 ${row.amount} 元）。` +
            '⚠️ 改完会立即影响「谁能接这款游戏的单」：',
          value: row.amount,
          maxlength: 32,
          placeholder: '例如 50（元；0 = 接单不需要押金）',
        },
        (value: string, index: number) => {
          const amount = value.trim()
          const cents = amountCents(amount)
          if (cents === null) {
            msg(MESSAGES.ruleAmountInvalid)
            return
          }
          ;(layer.close as (i: number) => void)(index)
          ;(layer.confirm as (t: string, o: unknown, ok: () => void) => void)(
            `确定把「${escapeHtml(row.game_name)}」的押金从 ${row.amount} 元改为 ${centsText(cents)} 元吗？` +
              '改完会立即影响谁能接这款游戏的单：冻结额不足的打手接不了。',
            { title: '确认修改押金金额' },
            () => {
              void runWrite(
                `${row.game_id}:${EVENTS.editRule}`,
                () => updateDepositRule(row.game_id, amount),
                (result) => {
                  const payload = result as { game_name?: string; amount?: string }
                  return MESSAGES.ruleDone(
                    payload.game_name ?? row.game_name,
                    payload.amount ?? centsText(cents),
                  )
                },
                loadDepositRules,
              )
            },
          )
        },
      )
    }

    /** 切 tab（只改 `currentTab` 与高亮；数据按 tab 懒加载，**进入时回第 1 页**）。 */
    function switchTab(next: WalletTab): void {
      if (next === currentTab) return
      currentTab = next
      if (next === 'wallets') {
        walletPage = 1
        void loadWallets().catch((error: unknown) => {
          msg(errorText(error))
        })
        return
      }
      if (next === 'deposit-rules') {
        void loadDepositRules().catch((error: unknown) => {
          msg(errorText(error))
        })
        return
      }
      // ⭐ 第三档必须显式写：原先的 `next === 'wallets' ? … : …` 三元会把这个 tab
      //    静默拉成「押金金额」（计划 §7 的风险点）。
      txPage = 1
      void loadTransactions().catch((error: unknown) => {
        msg(errorText(error))
      })
    }

    // ── 事件接线 ────────────────────────────────────────────────
    const onTabClick = (event: Event): void => {
      const li = (event.target as HTMLElement | null)?.closest<HTMLElement>(`[${TAB_ATTR}]`)
      if (!li) return
      const raw = li.getAttribute(TAB_ATTR)
      const next = TABS.find((tab) => tab.key === raw)
      if (next !== undefined) switchTab(next.key)
    }
    root.querySelector<HTMLElement>(`#${TABS_ID}`)?.addEventListener('click', onTabClick)

    /** 搜索：**回第 1 页**再查（否则「第 3 页再搜出 5 人」稳定打越界）。 */
    const onSearch = (): void => {
      keyword = ((walletSearch() as HTMLInputElement | null)?.value ?? '').trim()
      walletPage = 1
      void loadWallets().catch((error: unknown) => {
        msg(errorText(error))
      })
    }
    root.querySelector<HTMLElement>(`#${SEARCH_BUTTON_ID}`)?.addEventListener('click', onSearch)
    walletSearch()?.addEventListener('keydown', (event: KeyboardEvent) => {
      if (event.key === 'Enter') onSearch()
    })

    /** 钱包翻页（边界在这里挡住一次，后端越界另有 1002 退页兜底）。 */
    const goWalletPage = (delta: number): void => {
      const lastPage = Math.max(1, Math.ceil(walletTotal / WALLET_PAGE_SIZE))
      const next = walletPage + delta
      if (next < 1 || next > lastPage) return
      walletPage = next
      void loadWallets().catch((error: unknown) => {
        msg(errorText(error))
      })
    }
    root
      .querySelector<HTMLElement>(`#${WALLET_PREV_PAGE_ID}`)
      ?.addEventListener('click', () => goWalletPage(-1))
    root
      .querySelector<HTMLElement>(`#${WALLET_NEXT_PAGE_ID}`)
      ?.addEventListener('click', () => goWalletPage(1))

    /** 表头排序 → 带 `sort/order` 重查，且**回第 1 页**（缺省 frozen desc）。 */
    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `sort(${WALLET_TABLE_ID})`,
      (obj) => {
        const { field, type } = obj as { field?: unknown; type?: unknown }
        if (field !== 'balance' && field !== 'frozen') return
        if (type === 'asc' || type === 'desc') {
          walletSort = field
          walletOrder = type
        } else {
          // ⚠️ 第三次点同一列表头时 layui 给 `type: null`（= 取消排序），⛔ 不能当成 asc：
          //    与 `system-systemlog.ts` / `order-tradingorderactionloglist.ts` 同口径
          //    （null 不是升序）。本页的「不排序」= 回缺省 frozen desc（与 `initSort` 一致）。
          walletSort = 'frozen'
          walletOrder = 'desc'
        }
        walletPage = 1
        void loadWallets().catch((error: unknown) => {
          msg(errorText(error))
        })
      },
    )

    // ── 流水 tab：筛选（改选即查 / 搜索框回车查）与翻页 ──────────────
    const readTxFilters = (): void => {
      txKeyword = (
        root.querySelector<HTMLInputElement>(`#${TX_SEARCH_INPUT_ID}`)?.value ?? ''
      ).trim()
      txKind = root.querySelector<HTMLSelectElement>(`#${TX_KIND_SELECT_ID}`)?.value ?? ''
      txDateFrom = root.querySelector<HTMLInputElement>(`#${TX_DATE_FROM_ID}`)?.value ?? ''
      txDateTo = root.querySelector<HTMLInputElement>(`#${TX_DATE_TO_ID}`)?.value ?? ''
    }
    /** 改选即查：任何筛选变化都**回第 1 页**（`xgj-receipts.ts` 的同款做法，不设查询按钮）。 */
    const onTxFilterChange = (): void => {
      readTxFilters()
      txPage = 1
      void loadTransactions().catch((error: unknown) => {
        msg(errorText(error))
      })
    }
    for (const id of [TX_KIND_SELECT_ID, TX_DATE_FROM_ID, TX_DATE_TO_ID]) {
      root.querySelector<HTMLElement>(`#${id}`)?.addEventListener('change', onTxFilterChange)
    }
    root
      .querySelector<HTMLElement>(`#${TX_SEARCH_INPUT_ID}`)
      ?.addEventListener('keydown', (event: KeyboardEvent) => {
        if ((event as KeyboardEvent).key === 'Enter') onTxFilterChange()
      })

    const goTxPage = (delta: number): void => {
      const lastPage = Math.max(1, Math.ceil(txTotal / TRANSACTION_PAGE_SIZE))
      const next = txPage + delta
      if (next < 1 || next > lastPage) return
      txPage = next
      void loadTransactions().catch((error: unknown) => {
        msg(errorText(error))
      })
    }
    root
      .querySelector<HTMLElement>(`#${TX_PREV_PAGE_ID}`)
      ?.addEventListener('click', () => goTxPage(-1))
    root
      .querySelector<HTMLElement>(`#${TX_NEXT_PAGE_ID}`)
      ?.addEventListener('click', () => goTxPage(1))

    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `tool(${WALLET_TABLE_ID})`,
      (obj) => {
        const row = (obj as { data: WalletRow }).data
        if ((obj as { event: string }).event === EVENTS.adjust) openAdjust(row)
      },
    )
    ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
      `tool(${DEPOSIT_TABLE_ID})`,
      (obj) => {
        const row = (obj as { data: DepositRuleRow }).data
        if ((obj as { event: string }).event === EVENTS.editRule) editDepositRule(row)
      },
    )

    // 首屏：钱包 tab（押金 / 流水 tab 懒加载）。
    try {
      await loadWallets()
    } catch (error) {
      msg(errorText(error))
    }

    return () => {
      root.removeEventListener('click', onTabClick)
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
