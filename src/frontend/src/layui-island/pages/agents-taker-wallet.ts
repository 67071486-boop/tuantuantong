/**
 * 【财务相关 → 打手钱包】合并页（island key = `agents-taker-wallet`，
 * 路由 `/agents/taker-wallet`，菜单 label「打手钱包」）。
 *
 * 计划：spec `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.2
 * （业务方 2026-10-22 R2：「把『打手提现审核』和『打手钱包』合并到一个页面」）。
 *
 * ## 最小改法（评审 M-7 修订，⛔ 不发明机制）
 *
 * - 两份既有页模块（`agents-withdraw-review.ts` 867 行 / `agents-wallet-adjust.ts` 809 行）
 *   **文件保留不动**、旧路由（`/agents/withdraw-review`、`/agents/wallet-adjust`）**照旧活着**
 *   ⇒ 旧深链直接可用；本页只是**新增一个入口**，不引入 redirect（仓库没有先例）。
 * - 本页两个页签（提现审核 / 钱包管理）的内容**复用两份既有页模块的内核**：
 *   把页模块的 `mount()` 挂进各自的页签面板（两份既有交互测试直接 import 页模块，保内核即全绿）。
 *
 * ## 页签按权限渲染（§3 默认拍板；两个既有权限节点都保留不动）
 *
 * 持 `finance.taker_withdraw_review` ⇒ 见「提现审核」页签；持 `finance.taker_wallet_adjust`
 * ⇒ 见「钱包管理」页签；**没权限的页签整个不画**（fail-closed 先例 D-0154 /
 * `index-console.visibleShortcuts`）⇒ 只持一个节点的人只见对应页签，
 * 人事已配的角色授权零影响。路由级门 = 两个节点**任一**（some 语义，D-0219 先例）。
 *
 * ## 权限门：fail-closed
 *
 * 页内 `fetchMe()` 一次判两个节点（页签**画不画**的判据）；两个都没有 ⇒ 提示一句、
 * 不注入标记、不 mount 任何子页（`fetchMe()` 那一次身份请求照发——它就是这道门）。
 * 子页 mount 里**各自还有一道同款门**（它们本来就是独立页面）⇒ 深链直接打开子页照旧受保护。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G；本页**没有定时器**）
 *
 * ① 类名与选择器分成两个常量；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)` → `root.innerHTML = buildHtml()` →
 *    `initIslandElements(layui)` → （之后才）mount 子页进面板。
 * ⚠️ 三条都有**运行期**判据（`__tests__/agents-taker-wallet.interaction.spec.ts`）。
 */
import { fetchMe } from '@/api/auth'
import { TAKER_WALLET_NODE } from '@/api/takerWallets'
import { TAKER_WITHDRAW_NODE } from '@/api/withdrawals'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import walletAdjustPage from './agents-wallet-adjust'
import withdrawReviewPage from './agents-withdraw-review'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'agents-taker-wallet'
export const TITLE = '打手钱包'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'takerWalletPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** tab 那一条 `<ul>` 的 `id`，以及每个 `<li>` 上承载 tab 值的属性名。 */
export const TABS_ID = 'takerWalletTabs'
export const TAB_ATTR = 'data-taker-wallet-tab'

/** 两个页签面板容器的 id（子页 mount 的落点）。 */
export const WITHDRAW_PANEL_ID = 'takerWalletWithdrawPanel'
export const WALLET_PANEL_ID = 'takerWalletWalletPanel'

/** 页签的取值与文案（顺序固定；**label 与旧菜单条目逐字一致**，用户找得到）。 */
export type TakerWalletTab = 'withdraw' | 'wallet'
export const TABS: ReadonlyArray<{ key: TakerWalletTab; label: string }> = [
  { key: 'withdraw', label: '提现审核' },
  { key: 'wallet', label: '钱包管理' },
]

/** 无任何节点时的自检提示（两个子页的提示语各管各的，这里是合并页自己那一档）。 */
export const NO_ACCESS_MESSAGE = '无权限查看打手钱包'

/**
 * 页面标记（只画**有权限的**页签；面板数与页签数一致 ⇒ `initIslandElements` 的
 * 索引切换天然对得上）。⚠️ 面板容器（`.layui-tab-content > .layui-tab-item`）必须留：
 * `initIslandElements` 只接管「有面板可切」的页签（`islandTabTarget` 的判据）。
 */
export function buildHtml(visible: ReadonlyArray<{ key: TakerWalletTab; label: string; panelId: string }>): string {
  const tabs = visible
    .map(
      (tab, index) =>
        `<li${index === 0 ? ' class="layui-this"' : ''} ${TAB_ATTR}="${tab.key}">${tab.label}</li>`,
    )
    .join('')
  const panels = visible
    .map(
      (tab, index) =>
        `<div class="layui-tab-item${index === 0 ? ' layui-show' : ''}">` +
        `<div id="${tab.panelId}"></div></div>`,
    )
    .join('')
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-tab layui-tab-brief" style="margin:0">
    <ul class="layui-tab-title" id="${TABS_ID}">${tabs}</ul>
    <div class="layui-tab-content" style="padding:0">${panels}</div>
  </div>
</div>`
}

export default {
  mount: async (root: HTMLElement, ctx): Promise<() => void> => {
    // ⚠️ 顺序是硬要求（陷阱 ③）：先挂样式，再塞标记，最后 `initIslandElements`。
    const layui = await bootstrapIsland([])
    const layer = layui.layer as unknown as Record<string, unknown>
    const msg = (text: string): void => islandMsg(layer, text)

    // ⚠️ 门禁必须排在注入标记**之前**：页签画不画的判据在这里定（fail-closed）。
    const me = await fetchMe()
    const canWithdraw = canNode(me, TAKER_WITHDRAW_NODE)
    const canWallet = canNode(me, TAKER_WALLET_NODE)
    if (!canWithdraw && !canWallet) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    const visible = TABS.map((tab) => ({
      ...tab,
      panelId: tab.key === 'withdraw' ? WITHDRAW_PANEL_ID : WALLET_PANEL_ID,
    })).filter((tab) => (tab.key === 'withdraw' ? canWithdraw : canWallet))

    root.innerHTML = buildHtml(visible)
    initIslandElements(layui)

    // 子页 mount 进各自的面板（内核复用；子页自己还有一道同款权限门）。
    const unmounts: Array<() => void> = []
    for (const tab of visible) {
      const panel = document.getElementById(tab.panelId)
      if (panel === null) continue
      const mountPage = tab.key === 'withdraw' ? withdrawReviewPage : walletAdjustPage
      // 顺序：两个子页串行 mount（各自的 bootstrapIsland 幂等；先 withdraw 与旧默认一致）。
      // ⚪ 子页 mount 会各自写 `document.title` ⇒ 循环结束后由本页**改回**自己的标题。
      unmounts.push(await mountPage.mount(panel, ctx))
    }
    document.title = TITLE

    return () => {
      for (const unmount of unmounts) unmount()
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
