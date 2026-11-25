// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * ⭐ **R9 的「全页零遗漏」钉子**（spec §7.1 #6）：本页每一处「开层」调用点都必须被盘点到。
 *
 * ⚠️ **这条钉子抓什么 / 不抓什么（如实登记，E-0011）**：
 *  - **抓**：新增（或删掉）一处 `layer.open` / `layer.prompt` / `layer.confirm` **调用点** ⇒ 计数变 ⇒ 本用例红
 *    ⇒ 逼实现者**回头读 spec §3.1 那张对照表、并同一次补上判据**；
 *  - **⛔ 不抓**：① 把下面的计数常量**同步改大**（那是「故意绕过」，任何计数钉子都挡不住）；
 *    ② 既有判据被删掉（删一条 `it()` 不会让本用例红）；
 *    ③ 新增 `layer.alert` / 自造 DOM 弹层这一类**不经 layui 层**的入口。
 *  ⇒ 它是**提醒**、不是保证。这一句照 AGENTS §5-C「不写会被反例证伪的绝对句」写。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const ORDER_ORDERLIST_TS = path.resolve(HERE, '../order-orderlist.ts')
const SRC = readFileSync(ORDER_ORDERLIST_TS, 'utf8')

/**
 * 三种开层 family 的**调用点个数**（⛔ 只数真调用：注释里的 `layer.open({…})` 不含 ` as`）。
 *
 * ⭐ **当场重取命令**（T1 Step 1；本文件里的数字是 2026-09-28 开工时实测的）：
 *
 * ```sh
 * for k in open prompt confirm; do
 *   printf "layer.%s as = %s\n" "$k" "$(grep -c "layer\.$k as" src/layui-island/pages/order-orderlist.ts)"
 * done
 * ```
 *
 * ⚠️ `layer.confirm` 的调用点里有**二跳的嵌套强转**（`confirmClearTaker` / `confirmDeleteOrder`）——
 * 调用点个数 ≠ 逻辑弹层数；本钉子钉的是**调用点**，不是弹层。
 *
 * ⭐ **2026-10-09 当场重取（价格口径线加了「接单价+发单价组合查询」那一层）**：按上面那条命令实测
 * `open = 11` / `prompt = 5` / `confirm = 7`（⛔ 不是按「10 + 1」推算）⇒ `open` 由 **10 改 11**。
 * 这正是本网存在的意义：**新增一层开层调用点 ⇒ 红 ⇒ 逼实现者回头补对照表并同一次加判据**
 * （见下面 `SUBMIT_DIALOGS` 的 #19）。
 *
 * ⭐ **2026-10-26 当场重取（T6「一键发单到交易平台」复刻）**：按上面那条命令实测
 * `open = 12` / `prompt = 5` / `confirm = 7` ⇒ `open` 由 **11 改 12**。新增的是行内地球按钮
 * `.pushJdpEdit` 的 `openPushJdpLayer`：**本层自身没有「点确定 ⇒ 发请求」路径**（弹层里装的是
 * `order-pushjdp` 复刻页；页内提交在 iframe 里，T9d 起已接真，判据在
 * `__tests__/order-pushjdp.spec.ts`），所以下面 `SUBMIT_DIALOGS` 的 #20 只登记「开层锚点」，
 * ⛔ 不把页内提交算成这个 `layer.open` 的请求。
 *
 * ⭐ **2026-10-27 当场重取（P3/P7 行内平台动作）**：按上面那条命令实测
 * `open = 14` / `prompt = 5` / `confirm = 8` ⇒ `open` 由 **13 改 14**、`confirm` 由 **7 改 8**。
 * 新增两处：#22 `confirmWithdrawAll`（「从所有平台撤回」的 `layer.confirm`，确认后逐平台
 * `POST …/platform-push/withdraw/`，判据在 `order-orderlist.spec.ts` 的 P3 一组）、
 * #23 `openApplyCancelDialog`（「申请撤销」三格弹窗，提交后同样打 withdraw，判据在同一 spec 的 P7 一组）。
 *
 * ⭐ **2026-10-28 当场重取（业务方最终定案：只搬走加款 / 重发 / 协商撤销）**：按上面那条命令实测
 * `open = 13` / `prompt = 5` / `confirm = 8`。同时摘掉四处开层：
 * `openApplyCancelDialog`（`layer.open`）、`openPushAmountDialog`（`layer.prompt` + `layer.confirm`）、
 * `confirmPushRepush`（`layer.confirm`）；它们原先就是下面 #23–#26 那四行，已随按钮一起删
 * （⛔ 不留无主弹层）。
 * ✅ **撤回箭头保留** ⇒ `confirmWithdrawAll`（#22）连同它的 `layer.confirm` 仍在。
 *
 * ⭐ **2026-10-26 当场重取（订单管理页「同步收款」线）**：按上面那条命令实测
 * `open = 13` / `prompt = 5` / `confirm = 7` ⇒ `open` 由 **12 改 13**。新增的是工具栏「同步收款」的
 * `openReceiptSyncDialog`（下面 `SUBMIT_DIALOGS` 的 #21）：它**有**「点确定 ⇒ 发请求」路径，
 * 但那一发是 `POST …/xgj-receipt-sync/`、**立刻返回** `{accepted:true}`（逐笔查询在后台线程里跑）
 * ⇒ 判据在 `__tests__/order-orderlist-receipt-sync.spec.ts`（不是「弹层里跑完才关」那类）。
 */
const EXPECTED_LAYER_CALL_SITES = { open: 13, prompt: 5, confirm: 8 } as const

/**
 * 「点确定 ⇒ 发请求」的弹层清单（spec §3.1 对照表 15 行 + 本线盘点追加的 3 行 + 价格口径线追加的 1 行
 * + T6 追加的 1 行［零请求，只登记开层点］）。
 *
 * ⚠️ 第 4/5 行刻意共用同一个函数（`openEditPrompt`）⇒ 锚点相同是有意的。
 */
const SUBMIT_DIALOGS = [
  { n: 1, name: '改类型', form: 'layer.open', anchor: 'typeLayerIndex = (layer.open as' },
  { n: 2, name: '改发单价', form: 'layer.prompt', anchor: 'function openHallPricePrompt' },
  { n: 3, name: '收款状态', form: 'layer.open', anchor: 'settLayerIndex = (layer.open as' },
  { n: 4, name: '备注', form: 'layer.prompt', anchor: 'function openEditPrompt' },
  { n: 5, name: '角色名（与 #4 同一函数）', form: 'layer.prompt', anchor: 'function openEditPrompt' },
  { n: 6, name: '改状态', form: 'layer.open', anchor: 'const save = (index: number, dialog: HTMLElement)' },
  { n: 7, name: '发价弹层', form: 'layer.open', anchor: 'throwLayerIndex = (layer.open as' },
  { n: 8, name: '记退款', form: 'layer.open', anchor: 'refundLayerIndex = (layer.open as' },
  { n: 9, name: '删除两跳', form: 'layer.confirm', anchor: 'const confirmDeleteOrder' },
  { n: 10, name: '编辑弹窗', form: 'layer.open', anchor: 'const index = (layer.open as (o: unknown) => unknown)(' },
  { n: 11, name: '绩效编辑（病灶）', form: 'layer.prompt', anchor: 'function promptPerformanceAmount' },
  { n: 12, name: '重算客服提成（病灶）', form: 'layer.confirm', anchor: 'const onRecomputeCommission' },
  { n: 13, name: '改接单价', form: 'layer.prompt', anchor: 'function openPricePrompt' },
  { n: 14, name: '指派保存', form: 'layer.open', anchor: 'const submitAssign' },
  { n: 15, name: '清除接单人两跳', form: 'layer.confirm', anchor: 'const confirmClearTaker' },
  // ⭐ 本线盘点追加（规划时实测：spec §3.1 的表漏了它们，而它们都满足 spec 自己的判据）
  { n: 16, name: '批量订单号搜索（工具栏）', form: 'layer.prompt', anchor: 'BATCH_ORDER_PROMPT_TITLE' },
  { n: 17, name: '撤回抛单', form: 'layer.confirm', anchor: 'function withdrawFromHall' },
  { n: 18, name: '垃圾桶软删绩效', form: 'layer.confirm', anchor: 'function confirmDeletePerformance' },
  // ⭐ 价格口径线追加：`3 接发组合` 那一层。
  // ⚠️ **它的「确定 ⇒ 发请求」路径与其余 18 行不同**：**不**走 layer 自己的 `btn`（那一层没给
  //    `btn`），而是**弹层正文里那颗 `.seachMutPriceBtn`「搜索」** —— 点它 ⇒ 逐个校验 4 个金额
  //    ⇒ 合法则关层 + `reload()`（触发列表刷新，走 `collectSearch` 的真出口）。
  // ⚠️ 那颗按钮的监听是**懒绑定**（开层绑在按钮元素自身、关层摘），⛔ 不是挂载期的文档级委托
  //    ——这一点与 #3 / #7 / #8 那几层**有意不同**，理由见 `order-orderlist.ts` 的
  //    `onComboPriceSearchClick` / `bindComboPriceSearch` 注释；判据在
  //    `order-orderlist-filters.spec.ts` 的 T7 那一组（含「关层后再开仍有效」）。
  { n: 19, name: '接单价+发单价组合查询（价格口径 3 档；确定 = 弹层里那颗「搜索」）', form: 'layer.open', anchor: 'comboPriceLayerIndex = (layer.open as' },
  // ⭐ T6 追加：行内地球按钮的**发单页 iframe**（`order-pushjdp`）。
  // ⚠️ 与其余 20 行**有意不同**：这个 `layer.open` **本身没有**「点确定 ⇒ 发请求」路径——
  //    弹层里是复刻页，页内提交发生在 iframe 里（T9d 起已接真，判据在
  //    `__tests__/order-pushjdp.spec.ts`），不归本文件的调用点盘点。
  { n: 20, name: '发单到交易平台（iframe 复刻页；开层点）', form: 'layer.open', anchor: 'function openPushJdpLayer' },
  // ⭐（订单管理页「同步收款」，计划 `2026-10-26-receipt-sync-button.md`）：
  //    「开始同步」⇒ `POST /api/business/orders/xgj-receipt-sync/`（两个日期），端点**立刻返回**、
  //    逐笔查询在后台 daemon 线程里跑 ⇒ 判据是「请求不阻塞」（见 receipt-sync 那个 spec 的挂载级用例）。
  { n: 21, name: '同步收款（按录单时间范围；端点立刻返回、后台跑）', form: 'layer.open', anchor: 'function openReceiptSyncDialog' },
  // ⭐ P3：行内「从所有第三方交易平台撤回」的确认层（对标 `orderlist.js:4702`）。
  // 确定 ⇒ `GET …/platform-push/` → **逐平台** `POST …/platform-push/withdraw/`（逐平台回结果）。
  // ✅ 2026-10-28 定案：撤回箭头保留 ⇒ 这一层也保留。
  { n: 22, name: '从所有平台撤回（确认后逐平台撤回）', form: 'layer.confirm', anchor: 'function confirmWithdrawAll' },
  // ⭐ P3：行内「从所有第三方交易平台撤回」的确认层（对标 `orderlist.js:4702`）。
  // 确定 ⇒ `GET …/platform-push/` → **逐平台** `POST …/platform-push/withdraw/`（逐平台回结果）。
] as const

describe('order-orderlist 的「点确定 ⇒ 发请求」弹层：R9 全页零遗漏', () => {
  it('三种开层 family 的调用点个数与对照表一致（新增弹层 ⇒ 红）', () => {
    const actual = {
      open: SRC.split('layer.open as').length - 1,
      prompt: SRC.split('layer.prompt as').length - 1,
      confirm: SRC.split('layer.confirm as').length - 1,
    }
    expect(actual).toEqual(EXPECTED_LAYER_CALL_SITES)
  })

  it('对照表逐行的实现锚点在源码里命中（表腐化 ⇒ 红）', () => {
    for (const row of SUBMIT_DIALOGS) {
      // 第 4/5 行刻意共用同一个函数 ⇒ 这里只断「锚点串在」。
      expect(SRC, `#${row.n} ${row.name} 的锚点找不到：${row.anchor}`).toContain(row.anchor)
    }
  })
})
