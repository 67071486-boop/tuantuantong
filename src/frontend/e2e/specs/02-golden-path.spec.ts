/**
 * T5：订单主干 —— 一个真浏览器把 8 个业务动作走完，每步都回读后端。
 *
 * ## 为什么是「一个 test + 多个 `test.step`」
 * 这几步操作的是**同一笔订单**，步骤之间有强依赖（最典型：⑥ 的「清除接单人」按钮
 * 只在④抢单之后才渲染）。拆成多个 test 就得靠跨用例共享状态（顺序耦合、单独跑必挂）；
 * `test.step()` 分段照样能在报告里看到**卡在第几步**。
 * 本文件共 **10 个 `test.step`**：①–⑨ 是 spec §6.1 那 8 个业务动作 + 一个「②回读后端」
 * 的判据步，第 10 个是收尾（`expectNoPageErrors`）。⛔ 步号与顺序**照任务书**，未重排。
 *
 * ## 四处交互事实（T5 Step 1 在真 DOM 上实测过，不是读码推断）
 * ① **layui 把原生 `select`/`radio` 设成 `display:none`** ⇒ `selectOption` / `check` 会因不可见而
 *    失败，只能点 layui 渲染出来的替身节点（`.layui-form-select dd[lay-value]` /
 *    `.layui-form-radio`）；⚠️ 下拉的 `dl` 收起时点不动，必须**先点 `.layui-select-title` 展开**。
 * ② **`layer.*` 建在 island iframe 自己的 document 里** ⇒ 所有 `.layui-layer-*` 一律用
 *    `frameLocator(ISLAND_IFRAME)` 取。
 * ③ 接单人的后端字段名未核实 ⇒ 「这笔单被谁接了」的判据一律打在**界面那一格**（`a.jsr` 的文本）。
 * ④ 订单列表的**操作列是 `fixed: 'right'`**（`order-orderlist.ts:1595`）：layui 会把这一列**克隆**
 *    一份渲染进 `.layui-table-fixed-r`，而**人真正点到的是克隆那一份**——终审探针在真 DOM 上量到：
 *    主表里那颗 `a.throwEdit` 的矩形左边界已经 **1535px**（iframe 视口只有约 1240px）⇒
 *    `elementFromPoint` 返回 `null`；而 `.layui-table-fixed-r a.throwEdit` 的中心命中的是它**自己的**
 *    `<i class="layui-icon layui-icon-home">`（`inside=true`）。绩效页与抢单大厅**没有** fixed 列
 *    （实测 `.layui-table-fixed-r` 容器数 = 0）⇒ 那两页的点直接打在主表节点上即可。
 *    ⛔ **不再用 `dispatchEvent('click')`**（终审 I1）：它绕过浏览器的命中测试，只能证明
 *    「这个节点上的委托监听器会响应」，证明不了**人点到的是哪一个节点**——而「点了没反应」
 *    （E-0079 那种静默缺陷）恰恰发生在「最上层那个节点不是带处理器的那个」的时候。
 *    现在 `clickRowAction()` 走**真实点击**，并在点之前用 `elementFromPoint` 断言命中测试落在
 *    目标自身或它的后代上；操作列那一个按钮则先经 `fixedRightRowOf()` 取**克隆行**。
 *
 * ## 偏离任务书的地方（逐条列出，每条都给了理由与实测证据）
 * - **⑥ 用客服**：指派名单来自 `GET /api/business/orders/assignee-options/`
 *   （判 `order.assign_taker`，客服持有）。不要改回管理员顶替。
 * - **游戏下拉的选择器收窄到 `#gameName + .layui-form-select`**：任务书写的裸
 *   `.layui-form-select dd` 在同一页还命中「时间要求」等下拉（实测那份 DOM 里近百个 `dd`）。
 * - **① 的粘贴文本多带一行「接单实收：199.50」**：实收支票不建出来的话 ⑨ 的退款**必然**被
 *   后端拒（`1008`，实测文案见 `RECEIVED_PRICE` 的注释）⇒ ⑨ 需要的前件在任务书里缺了。
 *   补的是**前件**，⑨ 的断言一个字没动。
 * - **④ 抢单的后端回读挪到 ⑤ 开头**：代练员的节点集里只有 `order.submit_work` 一个订单节点
 *   ⇒ 拿它的会话读**全量**订单端点 `GET /api/business/orders/<no>/` 是 403（那是权限模型的
 *   正确行为，⛔ 不给代练员补 `order.view_detail`——全量 `OrderSerializer` 含客户联系方式）。
 *   判据强度不变，只换了个有权限的读者；④ **自己**另有一条界面判据（抢单后这一笔从大厅消失）。
 *   ⚠️ **改准（2026-10-29，fix/taker-web-order-detail）**：「打手**点开订单详情**什么都看不见」
 *   已修——`order-orderview` 岛页挂载期自判：无详情节点 ⇒ 改打**受限端点**
 *   `GET /api/business/my-orders/<no>/detail/`（D-0222 桌面侧补全，与手机端 H5 同一条）⇒ ④ 现在
 *   额外驱动「打手点蓝链看到脱敏详情」（见 ④ 末尾）；但**本文件的后端回读**（`readOrder` 走
 *   全量端点）仍必须由有权限的读者做，那条安排不变。
 * - **⑧ 的绩效行按 `emp_type='打单工资'` 取、并钉 `未结账→已结账`**：`rows[0]` 是「销售提成」
 *   那条（结账结的是打单工资那条）⇒ 任务书那条「变了就行」会假红。
 * - **本用例 `test.setTimeout(180_000)`**：九个步骤、七次换账号整页加载，全局 60s 实测在 ⑥ 用完。
 *   只加在本用例体内，不动全局配置、也不影响同文件将来的其它用例。
 *
 * ## 夹具值全是**合成值**（`AGENTS.md` §5C 的 E-0077）
 */
import {
  expect, test, type FrameLocator, type Locator, type Page, type Request,
} from '@playwright/test'

import { gameIdByName } from '../fixtures/api'
import { login, switchAccount } from '../fixtures/auth'
import { expectNoPageErrors, observe } from '../fixtures/observers'
import {
  ADD_CONTENT, ADD_GAME_PROXY_PREFIX, ADD_GAME_SELECT, ADD_PRICE, ADD_PROJECT_TEXT, ADD_RECEIVED,
  ADD_SUBMIT,
  ASSIGN_DIALOG, DIALOG_ASSIGN_CLEAR, DIALOG_ASSIGN_USER_LIST, DIALOG_ASSIGN_SAVE,
  DIALOG_REFUND_AMOUNT, DIALOG_REFUND_REASON, DIALOG_REFUND_SAVE,
  DIALOG_STATUS_RADIO_PROXY, DIALOG_STATUS_SAVE, DIALOG_THROW_PANEL, DIALOG_THROW_PRICE,
  DIALOG_THROW_SAVE, HALL_CLAIM_BY_ORDER_NO, ISLAND_IFRAME, LAYER_CONFIRM_OK, LAYER_MSG,
  ORDER_SEARCH_BUTTON, ORDER_SEARCH_INPUT, PERF_ROW_BY_ORDER_NO, PERF_SEARCH_BUTTON,
  PERF_SEARCH_INPUT, PERF_SETTLE_TAG,
  PROMPT_INPUT, PROMPT_OK, ROW_ASSIGN, ROW_PRICE, ROW_REFUND, ROW_STATUS_BADGE,
  ROW_BY_ORDER_NO, ROW_THROW, STATUS_DIALOG,
} from '../fixtures/selectors'

const GAME_NAME = 'E2E 测试游戏'
const PROJECT_TEXT = 'E2E 主干项目甲'
const ORDER_PRICE = '199.50'
/**
 * 接单实收（`received_price`）。**退款那一步的前件**。
 *
 * ⛔ 为什么必须显式给（T5 实测踩到的第三个坑）：`refunds.py::_gross_received` 的回落规则是
 * 「`received_price` **为 `None`** 才回落订单总价」，而录单页的 `#priceReceipts` 控件**不填也会**
 * 送出一个 `0`（实测新单入库就是 `received_price = 0.00`，不是 NULL）⇒ 可退额算出来是
 * `0.00 − 0` ⇒ 任何正数退款都被 `1008` 拒掉，现场提示逐字是
 * 「退款金额超过剩余可退额（剩余可退 0.00，本次 50.00）」。
 * ⇒ ①「粘贴解析」那一行里带上「接单实收：199.50」（后端 `intake_rules.BUILTIN_RULES` 的
 *   `received_price` 规则就认这个词），把「客户确实付过钱」这件事建出来——退款本来就以它为前提。
 * ⚠️ 这是**补上前件**、不是放宽 ⑨ 的断言：⑨ 仍然要求 `refund_fee` 真的变成 50.00。
 */
const RECEIVED_PRICE = '199.50'
const REPRICED = '260.00'
const REFUND_AMOUNT = '50.00'
/**
 * 抛单时填的发单价（`Order.hall_price`）。**③ 的回读真的读它**（`readOrder().hall_price`），
 * 所以它与录单总价（199.50）、改价（260.00）取值都不同，回读里能分清「是哪一处写进去的」。
 * ⚠️ 客服没有 `order.restrict_view_offer_price`（实测 DB：三个内置角色都没有这个节点）
 * ⇒ 这一格不会被脱敏成 `null`。
 */
const THROW_PRICE = '88.00'
/** 代练员账号名。`seed_e2e` 把每个账号的 `display_name`（= `user.first_name`）也设成用户名。 */
const TAKER_NAME = 'taker001'
/**
 * 批 F / F3 那扇提示窗的**逐字**文案（后端 `order_status.COMPLETE_REQUIRES_REVIEW_MESSAGE`，
 * 前端 `order-orderlist.ts::COMPLETED_REQUIRES_REVIEW_MESSAGE` 与它同句）。
 *
 * ⚠️ 本常量**逐字**重抄一遍（不从前端模块 import）：e2e 的判据要能独立失败——
 * 前端哪天把文案改坏了，这条断言必须红，而不是跟着一起改。
 */
const COMPLETED_REQUIRES_REVIEW_MESSAGE = '必须先把订单状态改成待验收才能改成已完成'
/**
 * `layer.alert` 的层（`type:0` ⇒ `.layui-layer-dialog`）。
 * ⚠️ 状态弹层是 `layer.open({type:1})` ⇒ `.layui-layer-page`，两类不重叠，
 * 所以这个选择器挑到的提示窗**不是**状态弹层自己。
 */
const ALERT_DIALOG = '.layui-layer-dialog'
/** 提示窗的正文容器（F3 那句话在它里面；⚠️ 状态弹层的 `.layui-layer-content` 不在本作用域下）。 */
const ALERT_CONTENT = '.layui-layer-dialog .layui-layer-content'
/** 通用流转端点的路径后缀（`transitionOrder` → `POST /api/business/orders/<单号>/transition/`）。 */
const TRANSITION_PATH_SUFFIX = '/transition/'

/**
 * 挂一个只记录**流转请求**的监听，返回 `{ calls, stop }`。
 *
 * ⚠️ 判据是「点完『确定』之后 `calls` 仍为空」：F3 的实现在弹完提示后**同步返回**、不碰网络
 * ⇒ 若哪天它退回「先发请求再提示」，这一条会红。`page.on('request')` 收的是**浏览器自己**
 * 发出的请求，本文件用 `page.request` 做的后端回读**不经过它**（那是另一条通道）。
 */
function watchTransitionRequests(page: Page): { calls: string[]; stop: () => void } {
  const calls: string[] = []
  const listener = (r: Request): void => {
    if (new URL(r.url()).pathname.endsWith(TRANSITION_PATH_SUFFIX)) calls.push(r.url())
  }
  page.on('request', listener)
  return { calls, stop: () => page.off('request', listener) }
}

type Envelope<T> = { code: number; msg: string; data: T }

/**
 * 带**单条**超时的 `expect`（`expect.poll` 的宽松/严格档只能挂在这一层上，
 * 写成 `expect.poll(fn, { timeout })` 不起作用——那是 `toPass` 的签名）。
 *
 * ⚠️ 为什么不用 Playwright 里那个默认值：`playwright.config.ts` 的 `expect.timeout` 是 10s，
 * 而「等后端状态真的落库」这一步在 CI 的冷启动下可能更慢 ⇒ 逐条显式给。
 */
const withTimeout = (timeout: number) => expect.configure({ timeout })

/** 回读后端（**订单详情端点**，字段名已在 `src/frontend/src/api/orders.ts:310-312` 核实）。 */
async function readOrder(page: Page, orderNo: string): Promise<Record<string, unknown>> {
  const res = await page.request.get(`/api/business/orders/${orderNo}/`)
  expect(res.status(), `读订单 ${orderNo} 失败`).toBe(200)
  const body = (await res.json()) as Envelope<Record<string, unknown>>
  expect(body.code, `读订单 ${orderNo} 的业务码非 0：${body.msg}`).toBe(0)
  return body.data
}

/**
 * 「按名字取这款游戏的当前主键」的实现已上移到 `fixtures/api.ts` 共享（终审 Minor）：
 * `03-permission` 的客服对照也要按名字取主键（原先它写死 `game: 1`）⇒ 两处必须**同一份**实现，
 * 不能各写一份（同 `switchAccount` 的教训）。
 */

/** 岛内的 frame locator（每个 helper 都从这里开始，避免把 `page.frameLocator` 抄得到处都是）。 */
function island(page: Page): FrameLocator {
  return page.frameLocator(ISLAND_IFRAME)
}

/** 按**订单号单元格的 id**（`order_<单号>`）定位那一行——比文本匹配精确，也不受重复文本影响。 */
function rowOf(page: Page, orderNo: string): Locator {
  return island(page).locator(ROW_BY_ORDER_NO(orderNo)).first()
}

/**
 * 订单列表「操作」列（`fixed: 'right'`）里**人真正点得到的那一份**所在的行。
 *
 * ⛔ 为什么需要它（终审 I1，真 DOM 实测）：layui 把 fixed 列**克隆**一份渲染进
 * `.layui-table-fixed-r`（终审探针：主表 `a.throwEdit` 的矩形左边界 1535px、在 iframe 视口之外，
 * 而克隆里那颗的中心命中的是它自己的图标）。`rowOf()` 命中的是**主表**那一行
 * （`div.orderLine` 的 id 只在主表里，克隆行只含 fixed 那一列，故不重复）⇒ 直接点它会被克隆盖住。
 *
 * ⚠️ **用 `data-index` 把两行钉在一起**：主行与克隆行由 layui 从同一份数据渲染，
 * 都会带 `data-index`（实测主行 `data-index="0"`、克隆行 `tr[data-index="0"]` 同步存在）。
 */
async function fixedRightRowOf(page: Page, orderNo: string): Promise<Locator> {
  const index = await rowOf(page, orderNo).getAttribute('data-index')
  expect(index, `${orderNo}：主表那一行应当带 data-index（fixed 克隆行要靠它对齐）`).not.toBeNull()
  return island(page)
    .locator(`.layui-table-fixed-r tr[data-index="${index}"]`)
    .first()
}

/**
 * 点**数据行**里的操作按钮——**真实点击**，不再 `dispatchEvent('click')`。
 *
 * ⛔ 为什么要真实点击（终审 I1）：`dispatchEvent` 把事件**直接派发**到目标节点，**绕过命中测试**
 * ⇒ 只能证明「这个节点上的委托监听器会响应」，证明不了**人点到的是哪一个节点**。
 * 订单列表的「操作」列是 `fixed: 'right'`，layui 会克隆一份到 `.layui-table-fixed-r`
 * （克隆与原节点**同类名**、也走同一个 `root` 上的委托监听器
 * `root.addEventListener('click', onCellClick)`）⇒ 克隆丢了处理这类缺陷在旧写法下**全绿**。
 *
 * 判据（两条，都能红）：
 *  ① `document.elementFromPoint(目标矩形的中心)` 必须落在**目标自身或它的后代**上——
 *     这就是浏览器的命中测试；被别的节点盖住时它返回盖住的那个节点，断言当场红并打印那个节点。
 *  ② 点击用 Playwright 的 `.click()`（自带可操作性检查与命中测试），不是 `dispatchEvent`。
 * ⚠️ 目标必须传**人真正点得到的那一份**：fixed 列里的按钮传 `fixedRightRowOf(...)` 里的克隆。
 * ⚠️ 先 `scrollIntoViewIfNeeded()` 再量矩形：主表横向溢出时目标可能在视口外，
 *    那时 `elementFromPoint` 返回 `null`（实测），先滚进视口量的才是人点得到的点。
 * ⚠️ **命中测试是「轮询到成立」而不是「只量一次」**（本轮实测踩到）：layui 关层有动画，
 *    上一步的 `#tt-order-assign` 已经 `toHaveCount(0)`、但它那一层的
 *    `div.layui-layer-shade` 还会盖住页面**一小段时间**——只量一次会把一个**正在消失的遮罩**
 *    判成「点了没反应」（首次跑实测报 `目标矩形中心的最上层节点是「div#layui-layer-shade2…」`）。
 *    真人也不会在那一下点击，所以这里给 10s 等它散掉；⛔ 若它**一直**不散，照样红（判据仍能失败）。
 */
async function clickRowAction(target: Locator, what: string): Promise<void> {
  await expect(target, `${what}：按钮应当存在`).toBeVisible()
  await target.scrollIntoViewIfNeeded()
  await expect
    .poll(
      async () => {
        const hit = await target.evaluate((el) => {
          const rect = el.getBoundingClientRect()
          const top = el.ownerDocument.elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2,
          )
          const inside = !!top && (top === el || el.contains(top))
          if (inside) return 'ok'
          return top
            ? `${top.tagName.toLowerCase()}${top.id ? '#' + top.id : ''}.` +
              (top.getAttribute('class') ?? '').trim().split(/\s+/).join('.')
            : 'null（点在视口之外）'
        })
        return hit
      },
      {
        timeout: 10_000,
        message:
          `${what}：目标矩形中心的最上层节点一直不是目标本身或它的后代 —— ` +
          `真实鼠标点击到不了它（这正是「点了没反应」的形状）。` +
          `若它在 fixed 列里，请改点 .layui-table-fixed-r 那一份（见 fixedRightRowOf）`,
      },
    )
    .toBe('ok')
  await target.click()
}

/**
 * 进订单管理页，并**用订单号把那一行筛出来**（每步之后都回这里看界面）。
 *
 * ⛔ **为什么必须筛、不能「取第一行」**：`Order` **有** `Meta.ordering = ["-created_at", "-id"]`
 * （`apps/business/models.py:540` ⇒ 最新在前；⚠️ 旧注说"没有 ordering"是**错的**，评审修复波
 * 第 4 条就地订正），但本线会对同一个租户**反复跑**、订单越积越多 ⇒
 * 按文本取第一行会取到**上一轮的旧单**：
 * 界面动作打在旧单上、而回读断言查的是新单，于是要么假红、要么（更糟）在「旧单碰巧也是同状态」时**假绿**。
 */
async function gotoOrderList(page: Page, orderNo: string): Promise<void> {
  await page.goto('/orders')
  const frame = island(page)
  await frame.locator(ORDER_SEARCH_INPUT).waitFor({ timeout: 20_000 })
  await frame.locator(ORDER_SEARCH_INPUT).fill(orderNo)
  await frame.locator(ORDER_SEARCH_BUTTON).click()
  await expect(rowOf(page, orderNo), `列表里应当能筛出 ${orderNo}`).toBeVisible({ timeout: 20_000 })
}

/**
 * 在某个 `<select>` 的 layui 替身里按**可见文本**选中一项（先展开、再点 `dd`）。
 *
 * `scope` 是**已经限定好的**作用域（页面本体，或「装了这个控件的那个 `.layui-layer`」）。
 * `proxyPrefix` 必须是「原生 select 的选择器 + ` + .layui-form-select`」这种**钉在具体控件上**
 * 的前缀：同一页有多个 layui 下拉，裸 `.layui-form-select dd` 会命中无关选项（实测近百个）。
 */
async function pickLayuiOption(
  scope: Locator | FrameLocator,
  proxyPrefix: string,
  text: string,
  what: string,
): Promise<void> {
  await scope.locator(`${proxyPrefix} .layui-select-title`).click()
  const option = scope.locator(`${proxyPrefix} dd`, { hasText: text })
  await expect(option.first(), `${what}：下拉里应当有「${text}」`).toBeVisible({ timeout: 15_000 })
  await option.first().click()
}

/**
 * **最新**那条 `layer.msg` 的 toast。
 *
 * ⛔ 不能直接断言 `.layui-layer-msg`（本轮修复实测踩到）：实测**关闭动画那一小段窗口里**
 * 新旧两条 toast 同时挂在 DOM 里（报 strict mode violation，且 Playwright 取到的是**旧**那条，
 * 于是「指派成功」被读成「已清除接单人」）。⚠️ 本条**只断言实测到的这一点**：
 * 「关闭动画期间两条并存」——⛔ 不要据此扩写成「layui 关 msg 永不删节点」那种绝对句
 * （没量过动画结束之后，也没量过别的 `layer` 类型；`AGENTS.md` §5C 的 E-0011）。
 * 文案语义本来就是「**后来的覆盖先前的**」（实现里多处注释写了这条顺序要求，如
 * `order-orderlist.ts` 的「先 reload 再 msg」）⇒ 判据取**最后一条**。
 */
function toast(frame: FrameLocator): Locator {
  return frame.locator(LAYER_MSG).last()
}

/**
 * 开指派弹层，并**等候选名单那一发请求回来**。
 *
 * ⛔ **为什么要等网络、而不是只等节点**（T5 实测踩到）：弹层一开就发
 * `GET /api/business/orders/assignee-options/?role=代练员`（`openAssignDialog`），名单回来才 `fillOptions`；
 * 而清除接单人之后**列表自身还在 reload**，重渲染会把**刚开的弹层从 DOM 里摘掉**
 * （实测点 `.layui-select-title` 时反复报 `element was detached from the DOM, retrying`）。
 * ⇒ 这里显式等那一发名单请求（与录单页抓创建响应同一个手法），再交给 `pickAssignTaker`。
 *   **不是** `waitForTimeout`：等的是一个具体的网络事件。
 *
 * ⚠️ **失败路径也要收口 `pending`**（评审 Minor 7）：`clickRowAction` 抛错时 `pending` 已经注册，
 * 不 await 会留下未处理拒绝噪声。这里 `catch` 里静默收口它、再把**原错**抛出去（不吞原错）。
 */
async function openAssignDialog(page: Page, target: Locator, what: string): Promise<void> {
  const pending = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === '/api/business/orders/assignee-options/' &&
      r.request().method() === 'GET',
    { timeout: 20_000 },
  )
  try {
    await clickRowAction(target, what)
    await pending
  } catch (err) {
    await pending.catch(() => undefined)
    throw err
  }
}

/**
 * 在**指派弹层**里选中某位代练员（先等名单出现，再展开、再点）。
 *
 * ⛔ **为什么不预先按名字筛层**（评审 Minor 7，实测坐实）：layui 关层的**动画窗口内**旧层还挂在
 * DOM 里（⚠️ **是暂时**的——下面第二步与 step ⑥ 的 `toHaveCount(0)` 都建立在「它会消失」上；
 * ⛔ 不要把它读成「关了就不删节点、旧层会一直留着」，那会让人以为 step ⑥ 那道闸门删得掉）。
 * 清除接单人之后**那一小段时间**文档里会同时存在上一轮的 `#tt-order-assign`
 * （实测 `opts=2 || opts=1`，旧的排在前面）。若一上来就写 `.filter({ hasText: TAKER_NAME })`，
 * 那么「名单一直没回来」时**根本选不中任何一层**，失败会表现为「无界地等标题可点」——
 * 而那条「名单里应当出现『taker001』」的诊断**永远不会出现**。
 * ⇒ 分两步：先 `toBeAttached` 等**选项本身**出现（诊断可达），再按「含这个选项的那一层」收窄去点。
 *    `dd` 在 `dl` 收起时不可见，所以第一步只能等 **attached**（不是 visible）。
 */
async function pickAssignTaker(page: Page, text: string): Promise<void> {
  const frame = island(page)
  // 第一步（**诊断可达的那一步**）：等**名单里真的出现这一项**。
  // ⚠️ 只能等 `attached`：原生 `input` 与它的替身都可能在列表还没回来时根本不存在。
  // 名单一直不回来 ⇒ 在这里红，而不是超时在一个点不到的节点上（评审 Minor 7）。
  const native = frame.locator(`${DIALOG_ASSIGN_USER_LIST}[title="${text}"]`)
  await expect(native.first(), `指派弹层名单里应当出现「${text}」`).toBeAttached({ timeout: 15_000 })
  // 第二步：在**当前这一轮**的那一层里点它的**替身**。
  // ⚠️ 调用方必须先保证上一次那个层已经**彻底关闭**（见 step ⑥ 的 `toHaveCount(0)`）：
  //    实测 layui 的关闭动画期间旧层还挂在 DOM 里，而它的名单里**也有 taker001**
  //    ⇒ 不先等它消失，任何「按内容挑层」的写法都会挑中那个正在消失的旧层，
  //    表现为 `element was detached from the DOM, retrying`。
  const layer = frame.locator('.layui-layer').filter({ has: native.first() }).last()
  // ⚠️ 原生 `input[type=checkbox]` 被 layui 设成 `display:none` ⇒ 必须点替身 `.layui-form-checkbox`
  //    （替身的 click 处理器会对原生 input 调 `trigger('click')`，所以原生 `checked` 会被置位）。
  await layer.locator('.layui-form-checkbox', { hasText: text }).first().click()
  await expect(native.first(), `点了替身之后原生 checkbox 应当被选中（${text}）`).toBeChecked()
}

/**
 * 回读**绩效列表**里这一笔的绩效行。
 *
 * ⚠️ 端点用 `/api/business/performances/`（绩效管理页表格的同一个 url），**不是** `/settlements`——
 * 后者是「结算记录」页的取数端点（2026-10-28 D-0319 起该页主视图 = 按天聚合的
 * `/settlements/daily-summary`、点开某天才是 `/settlements` 明细；`api/settlements.ts`）。
 * ⚠️ **按 `order_no` 精确筛**（`PerformanceListSearchSerializer.order_no`，`serializers.py:1005`；
 * 落点 `performance_views.py:149-150` 的 `order__order_no=`）——⛔ **不要**退回
 * 「取一页再本地筛」：本线每跑一次就多一笔单（±2 条绩效行），`page_size=200` 的第一页迟早装不下，
 * 于是这条回读会在**正是它该稳的「反复重跑」场景**里假红（评审 Important 2）。
 * 返回形状是 `{ items, total, page, page_size }`（`api/unwrap.ts:8` 的 `Page<T>`）。
 */
async function readPerformances(page: Page, orderNo: string) {
  const res = await page.request.get(
    `/api/business/performances/?order_no=${orderNo}&page=1&page_size=200`,
  )
  expect(res.status(), '读绩效列表失败').toBe(200)
  const body = (await res.json()) as Envelope<{
    items: Array<Record<string, unknown>>
    total: number
  }>
  expect(body.code, `读绩效列表的业务码非 0：${body.msg}`).toBe(0)
  // ⛔ **先钉「非空」**（终审 Minor）：`[].every(...)` 恒为 `true`、`0 === 0` 也恒真
  // ⇒ 少了这一条，上面那条「服务端真的筛过」在**一条都没回来**时是**空真**（判据看着绿、其实没判）。
  expect(
    body.data.items.length,
    `绩效回读一条都没回来（order_no=${orderNo}）——空数组会让下面两条判据变成空真`,
  ).toBeGreaterThan(0)
  // 判据：**服务端真的按单号筛过**（`total` 是 `paginator.count`，即**筛选后**的总数，
  // `base.py:37`）。若哪天有人把 `order_no` 参数删掉（退回「取一页再本地筛」），
  // 这里返回的就是全库第一页 ⇒ 下面这条会红，而不是等到单子多了才假红。
  expect(
    body.data.items.every((row) => row.order_no === orderNo),
    `绩效回读里混进了别的订单（说明服务端没有按 order_no 筛）`,
  ).toBe(true)
  expect(
    body.data.total,
    `这一笔的绩效行数超出了单页容量（${body.data.total} > 200），回读会漏行`,
  ).toBe(body.data.items.length)
  return body.data.items
}

/** 绩效行的「收入类型」取值（后端 `EmpType.MAIN`，`models.py:441`）。 */
const EMP_TYPE_MAIN = '打单工资'

/**
 * 从绩效行里取出**代练员那条打单工资行**（`EmpType.MAIN`）。
 *
 * ⛔ **不能取 `rows[0]`、也不能兜底回落它**（T5 实测的第二个假红 + 评审 Important 2）：
 * 一笔订单有**两条**绩效行——「销售提成」（客服 `cs001` 的提成）与「打单工资」（代练员那一行），
 * 而列表按 `id` 升序返回 ⇒ **`[0]` 是销售提成那条**（实测：结账成功后它仍是「未结账」，
 * 于是一条本该绿的断言在 `settle_status` 上一直等到 15s 超时）。「结账状态」列那颗 `.setSett`
 * 结的是**被点的那一行**。判据用 `emp_type` 这个**值本身**（不是行序）；
 * **找不到就响亮失败**——`?? rows[0]` 那种兜底会把「取错行」重新放回来。
 */
function takerPerformance(rows: Array<Record<string, unknown>>): Record<string, unknown> {
  const found = rows.find((row) => row.emp_type === EMP_TYPE_MAIN)
  if (!found) {
    throw new Error(
      `绩效列表里找不到「${EMP_TYPE_MAIN}」那一行（实测 emp_type：` +
        `${rows.map((row) => String(row.emp_type)).join('、') || '一条都没有'}）`,
    )
  }
  return found
}

/**
 * 进绩效管理页，并**用页面自带的筛选栏把这一笔筛出来**。
 *
 * ⛔ 为什么必须筛（评审 Important 2）：绩效列表是分页的（页面自己发 `page_size=50`），
 * 而本线每跑一次就多一笔单 ⇒ 「新行恰好在第一页」是个**会过期的隐含前提**。
 * 页面「搜索1」的 `keyword` 后端落成
 * `Q(order__order_no__icontains) | Q(order__game_role_name__icontains)`
 * （`performance_views.py:150-155`），所以按订单号筛得动。
 * 行再按 `TAKER_NAME` 收窄：一笔订单有「销售提成」与「打单工资」两行，两行的订单号文本相同，
 * 而 `.setSett` 只结**被点的那一行**——要点的必须是代练员那一行。
 */
async function gotoPerformance(page: Page, orderNo: string): Promise<void> {
  await page.goto('/agents/performance')
  const frame = island(page)
  await frame.locator(PERF_SEARCH_INPUT).waitFor({ timeout: 20_000 })
  await frame.locator(PERF_SEARCH_INPUT).fill(orderNo)
  await frame.locator(PERF_SEARCH_BUTTON).click()
  await expect(perfRowOf(page, orderNo), `绩效页里应当能筛出 ${orderNo} 的打单工资行`).toBeVisible({
    timeout: 20_000,
  })
}

/** 绩效页里那**一行**绩效：先按订单号筛出来的表中，再取代练员（`打单工资`）那一行。 */
function perfRowOf(page: Page, orderNo: string): Locator {
  return island(page)
    .locator(PERF_ROW_BY_ORDER_NO(orderNo))
    .filter({ hasText: TAKER_NAME })
    .first()
}

test('订单主干：录单 → 抛单 → 抢单 → 改价 → 指派/清除 → 完成 → 结账 → 退款', async ({ page }) => {
  /**
   * 本用例的**单步超时**（`test.step` 也吃 `test.setTimeout` 的预算）。
   *
   * ⚠️ 为什么这一个是例外（配置文件里全局是 60s）：这条链**九个步骤**、每个步骤都要
   * 换账号 + 整页加载 + 后台回读，光是 ①③④⑤⑥⑦⑨ 七次登录跳转在冷启动的 Vite 上就要
   * 40s 以上；60s 的全局预算会让它**卡在中间某一步**（实测：第一次跑就在 ⑥ 的候选下拉上用完）。
   * ⛔ 这不是「放宽断言」——每一条判据都原样保留，只是给它足够的时间跑完。
   * ⛔ **必须写在用例体内**（评审 Minor 8）：写成模块级的 `test.setTimeout(...)` 会连带
   *    抬高同文件**其它**用例的预算（配置里没有 `actionTimeout` ⇒ 每个裸 click/fill 的最坏挂起
   *    也跟着 ×3），那是给未来的用例埋的坑。
   */
  test.setTimeout(180_000)

  const obs = observe(page)
  let orderNo = ''

  await test.step('① 录单（客服）：粘贴解析 + 提交', async () => {
    await login(page, 'cs')
    await page.goto('/orders/new')
    const frame = island(page)
    await frame.locator(ADD_CONTENT).waitFor({ timeout: 20_000 })
    // 粘贴解析：`#dlcontent` **失焦即触发**（`order-addorder.ts:1547-1549`），没有独立按钮。
    await frame.locator(ADD_CONTENT).fill(`价格：${ORDER_PRICE}\n接单实收：${RECEIVED_PRICE}`)
    await frame.locator(ADD_CONTENT).blur()
    // ⚠️ 断言值来自 Step 1 的实测：`#price` 的证据件默认值是 `value="0"`，解析把它改写成
    //    `199.50`（`applyPasteToDom` → `writeFormState` → `[name="price"]`）。这一条是**真判据**
    //    ——解析没命中时它仍是 `0`，断言当场红。
    await expect(frame.locator(ADD_PRICE)).toHaveValue(ORDER_PRICE)
    await expect(frame.locator(ADD_RECEIVED)).toHaveValue(RECEIVED_PRICE)
    // 游戏是**必填**（`order-addorder.ts:530-534`），且原生 select 不可见 ⇒ 点 layui 替身。
    await pickLayuiOption(island(page), ADD_GAME_PROXY_PREFIX, GAME_NAME, '录单页游戏下拉')
    // 判据是**选中项的可读文本**（后端返回的游戏名），⛔ 不是原生 select 的 `value`：
    // 那是数据库主键——随夹具插入顺序 / 历史漂移，⛔ 不是接口契约。`seed_e2e` 自本线
    // （game-scope-audit）起建**两款**游戏（`E2E 测试游戏` 今天 id=1、`E2E 测试游戏乙` id=2，
    // 实测），钉 `toHaveValue('1')` 只会把用例绑死在主键这个实现细节上；
    // 真正判「点中的是不是这款游戏」的是下面这条**精确文本**断言（评审 Minor 4）。
    await expect(
      frame.locator(`${ADD_GAME_SELECT} option:checked`),
      '游戏下拉里被选中的那一项应当就是 E2E 这款游戏',
    ).toHaveText(GAME_NAME)
    await frame.locator(ADD_PROJECT_TEXT).fill(PROJECT_TEXT)
    // 录单成功时抓创建响应——订单号只有这里能给（界面不显示：业务方裁定不弹单号）。
    const [createRes] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname === '/api/business/orders/',
      ),
      frame.locator(ADD_SUBMIT).click(),
    ])
    const created = (await createRes.json()) as Envelope<{ order_no: string }>
    expect(created.code, `录单失败：${created.msg}`).toBe(0)
    orderNo = created.data.order_no
    expect(orderNo, '订单号应当是 20 位').toMatch(/^\d{20}$/)
  })

  await test.step('② 回读后端确认订单真的落库（防「页面说成功了其实没有」）', async () => {
    const order = await readOrder(page, orderNo)
    expect(order.status).toBe('UNASSIGNED')
    expect(order.project_text).toBe(PROJECT_TEXT)
    // 顺带把「录单总价 / 接单实收就是粘贴解析出来那两笔」也钉住：这是 ① 的载荷真的带上了
    // 那两个值的证据（`received_price` 同时是 ⑨ 退款的前件）。
    expect(String(order.total_price)).toBe(ORDER_PRICE)
    expect(String(order.received_price)).toBe(RECEIVED_PRICE)
    // 游戏那一项也在这里钉住：订单上落的 `game` FK 必须是界面上选的那一款
    // （判据 = **名字**→字典 id，不是写死的 PK；见 `gameIdByName` 的注释）。
    expect(order.game, '订单应当挂在界面上选的那款游戏上').toBe(await gameIdByName(page, GAME_NAME))
  })

  await test.step('③ 抛单到内部大厅（客服）', async () => {
    // ⚠️ 这里也走 `switchAccount`（虽然角色没变）：本用例此时仍是①那次登录的文档，
    //    直接 `goto('/login')` 会被守卫判成「已登录」并跳回 home（与换账号同一个坑）。
    await switchAccount(page, 'cs')
    await gotoOrderList(page, orderNo)
    // ⛔ 「发到内部接单大厅」在**操作列**（`fixed: 'right'`）里 ⇒ 必须点 fixed 克隆那一份
    //    （人真正点到的那一份）。主表那一颗在 iframe 视口之外，真实点击到不了它（终审 I1 实测）。
    await clickRowAction(
      (await fixedRightRowOf(page, orderNo)).locator(ROW_THROW),
      '行内「发到内部接单大厅」',
    )
    const frame = island(page)
    await frame.locator(DIALOG_THROW_PANEL).waitFor({ timeout: 15_000 })
    // `#setbprice`（保证金）的证据件默认值是 `0`，`submitThrow` 只在「非 0」时才拦
    // ⇒ 这里只填发单价就够。
    await frame.locator(DIALOG_THROW_PRICE).fill(THROW_PRICE)
    await frame.locator(DIALOG_THROW_SAVE).click()
    await expect(toast(frame)).toContainText('已发到内部接单大厅')
    // 回读：状态必须已经离开 `UNASSIGNED`（只看 toast 不算数）。
    await withTimeout(10_000).poll(async () => (await readOrder(page, orderNo)).status)
      .not.toBe('UNASSIGNED')
    // 抛单后必须是「等人抢」那一档，而不是别的状态——`.not.toBe('UNASSIGNED')` 单独一条
    // 分不出「抛单成功」与「别的什么把它改走了」；这一条把目标值钉死。
    // ⚠️ 同时**真读一次 `hall_price`**：抛单那一步填的发单价必须落库（评审 Minor 5——
    //    原先 `THROW_PRICE` 的注释声称「在回读里可区分」，却没有任何回读读它，是句假话）。
    const thrown = await readOrder(page, orderNo)
    expect(thrown.status).toBe('WAITING_GRAB')
    expect(String(thrown.hall_price), '抛单价应当真的写成界面填的那个值').toBe(THROW_PRICE)
  })

  await test.step('④ 抢单（代练员）：在内部抢单大厅抢到那一笔', async () => {
    await switchAccount(page, 'taker')
    await page.goto('/orders/inside-hall')
    const frame = island(page)
    // 按**订单号**点名那一颗按钮（按钮自带 `data-id="<单号>"`），不猜行序。
    // ⛔ 必须 `.first()`：这一行有**两颗**同形的抢单按钮（`actioncol` 与 `actioncol2` 两列），
    //    而 Playwright 的严格模式在多命中时会直接报错。
    const claim = frame.locator(HALL_CLAIM_BY_ORDER_NO(orderNo)).first()
    await expect(claim, '大厅里应当有这一笔').toBeVisible({ timeout: 20_000 })
    await clickRowAction(claim, '大厅「接手此单」')
    // `layer.confirm`（确认语逐字取对标 i18n）⇒ 点第一个按钮「确定」。
    await frame.locator(LAYER_CONFIRM_OK).click()
    await expect(toast(frame)).toContainText('抢单成功')
    // ⛔ **这里不回读订单详情**（T5 实测）：代练员的节点集里**只有** `order.submit_work`
    //    一个订单节点（实测：`代练员` 角色的 `order.*` 节点只有这一条）⇒ 拿代练员的会话读
    //    `GET /api/business/orders/<no>/`（**全量**端点）是 **403**（详情要
    //    `order.view_detail`/`order.view_content`），那是权限模型的正确行为，不是抢单失败。
    //    ⭐ **改准（2026-10-29，fix/taker-web-order-detail）**：「打手**在网页端点开订单详情**
    //    什么也看不见」已修——`order-orderview` 岛页（蓝链开的那个 iframe 层）挂载期自判：
    //    无 `order.view_detail`/`order.view_content` ⇒ 改打**受限端点**
    //    `GET /api/business/my-orders/<no>/detail/`（16 键脱敏载荷，无客户联系方式；与
    //    手机端 H5 详情页同一条，D-0222 桌面侧补全）⇒ ④ 末尾新增「点蓝链看到脱敏详情」的驱动。
    //    全量端点对打手**仍** 403（不给代练员补详情节点——全量载荷含客户联系方式）⇒
    //    ⇒ 这一笔的**后端回读**仍放在 ⑤ 的开头（那时会话已经换回有读权限的客服），
    //      判据是「抢单真的把它推进到 `IN_PROGRESS`」。
    //
    // ⛔ 但 ④ **自己**也必须有一条**会随行为改变真假**的界面判据（评审 Important 1）：
    //    `layer.msg` 那种 toast 不算判据。抢单成功后实现会 `reload(currentPage)`，而大厅的
    //    查询是「`status=WAITING_GRAB` **且** 打单工资绩效行 `user IS NULL`」
    //    （`selectors.py::search_hall`）⇒ 这一笔**必须从大厅消失**：
    //    被接走的单如果还挂在大厅里（或按钮仍在），这条断言会红。
    await expect(
      claim,
      '抢单后这一笔应当从大厅消失（大厅只列「已抛单且还没人接」的单）',
    ).toHaveCount(0, { timeout: 15_000 })

    // ⭐ 2026-10-29（fix/taker-web-order-detail）：打手在「指派给我的订单」页点单号蓝链
    //    ⇒ 层里是**脱敏详情**（修复前：岛页只打全量端点 ⇒ 403 ⇒ 层里是对标那 16 行
    //    「标签在、值全空」的静态骨架——业务方 2026-10-29 报障的现场）。
    await page.goto('/orders/mine')
    const mineFrame = island(page)
    // 行内点击按 `data-order`（= 20 位系统单号）点名这一笔，不猜行序。
    const detailLink = mineFrame.locator(`a.orderdetails[data-order="${orderNo}"]`).first()
    await expect(detailLink, '我的订单里应当有打手刚接的这一笔').toBeVisible({ timeout: 15_000 })
    await detailLink.click()
    // 层是 island iframe **文档里**的 `type:2` 层 ⇒ 详情页在**嵌套** iframe 里。
    const detailFrame = mineFrame.frameLocator('.layui-layer-iframe iframe')
    // 数据行（`.ttRestrictedRow`）只在**真渲染了脱敏详情**时存在——修复前的空骨架一行都没有。
    await expect(
      detailFrame.locator('.ttRestrictedRow').first(),
      '打手点开订单详情应当看到脱敏详情（不是空骨架）',
    ).toBeVisible({ timeout: 15_000 })
    const detailText = await detailFrame.locator('.ttRestrictedDetail').innerText()
    // 字段集 = 手机端 H5 详情同款：基本信息在、游戏真取到数、报酬行在。
    expect(detailText).toContain('基本信息')
    expect(detailText).toContain('E2E 测试游戏')
    expect(detailText).toContain('接单金额')
    // 客户联系方式类行**不渲染**（受限载荷本来就没有那些键）。
    expect(detailText, '打手的详情里不该有客户联系方式行').not.toContain('联系电话')
    // 收口：关掉这一层，别让遮罩跟到后面的步骤。
    await mineFrame.locator('.layui-layer-close').first().click()
  })

  await test.step('⑤ 改价（客服）：点行内「改接单价」，走 layer.prompt', async () => {
    await switchAccount(page, 'cs')
    // ⛔ ④ 的**后端回读在 ④ 里做不了**（代练员读不到订单详情，见 ④ 的注释），所以补在这里：
    //    换回客服后先钉住「抢单真的落库了」——状态被推进到 `IN_PROGRESS`。
    //    ⚠️ 它同时是 ⑥「清除接单人」按钮会出现的前件（`currentTaker` 非空）。
    await expect
      .poll(async () => (await readOrder(page, orderNo)).status, { timeout: 10_000 })
      .toBe('IN_PROGRESS')
    await gotoOrderList(page, orderNo)
    await clickRowAction(rowOf(page, orderNo).locator(ROW_PRICE), '行内接单价链接')
    const frame = island(page)
    await frame.locator(PROMPT_INPUT).waitFor({ timeout: 10_000 })
    await frame.locator(PROMPT_INPUT).fill(REPRICED) // 输入框在 `.layui-layer-prompt` 层里
    await frame.locator(PROMPT_OK).click() // 第一个按钮「保存」
    // ⚠️ 该实现**先 `layer.close()` 再发请求**（`order-orderlist.ts:2223-2225`）⇒ 失败时层也已经关了，
    //    所以「层没了」不能当成功判据；真正的判据在下面这条回读上。
    // 回读：`/price/` 改的就是 `total_price`（`api/orders.ts` 的 `changePrice`）。
    await withTimeout(10_000).poll(async () => String((await readOrder(page, orderNo)).total_price))
      .toBe(REPRICED)
  })

  await test.step('⑥ 清除接单人 → 再指派回代练员（客服）', async () => {
    // ⚠️ **历史（已解决）**：本步原先改用 `admin` 顶替客服，理由见本文件头 A 条——`D-0145`
    //    （客服持有 `order.assign_taker`，但弹层名单走 `GET /api/accounts/`、判 `system_user.list`
    //    ⇒ 客服拉名单 403、下拉只剩占位项）。
    //    ⭐ **`D-0146`（`c635450f`）已把那个缺陷修掉**：弹层名单改走专用端点
    //    `GET /api/business/orders/assignee-options/`（判 `order.assign_taker`，客服持有）
    //    ⇒ 顶替的理由消失，本步**改回真实职位「客服」**；这一跳因此同时覆盖
    //    「客服能不能真的走完指派 / 清除」——比拿 admin 走有价值。
    await switchAccount(page, 'cs')
    await gotoOrderList(page, orderNo)
    const frame = island(page)
    const row = rowOf(page, orderNo)

    // —— 先清除（④ 抢来的那一份）：清除是一次 `layer.confirm`，要点它的「确定」——
    // ⚠️ `[data-act="assign-clear"]` **只在「该单已有接单人」时才渲染**（`order-orderlist.ts:2953-2956`）
    //    ⇒ 这一步之前必须先有 ④ 的抢单，否则这颗按钮根本不存在（这是有意的依赖，不是偶然）。
    await openAssignDialog(page, row.locator(ROW_ASSIGN), '行内「接手人」那一格')
    await expect(frame.locator(DIALOG_ASSIGN_CLEAR), '该单应已有接单人，清除按钮才在').toBeVisible()
    await frame.locator(DIALOG_ASSIGN_CLEAR).click()
    await frame.locator(LAYER_CONFIRM_OK).click() // 第 1 跳：`CLEAR_TAKER_CONFIRM_TEXT`
    // 第 2 句问要不要删绩效。本链选「不需要删除」：后面结账要结的就是这条绩效行。
    await frame
      .locator('.layui-layer-btn a', { hasText: '不需要删除接手人绩效' })
      .last()
      .click()
    await expect(toast(frame)).toContainText('已清除接单人')
    await withTimeout(10_000).poll(async () => (await row.textContent()) ?? '')
      .not.toContain(TAKER_NAME)

    // —— 再指派回去 ——
    // ⛔ **先等上一次那个指派弹层彻底关闭**（`confirmClearTaker` 的成功路径会 `layer.close(index)`）。
    //    为什么这是一条硬闸门（本轮实测）：layui 关层有动画，关闭期间旧层还挂在 DOM 里，
    //    而**它的名单里同样有 `taker001`** ⇒ 不先等它消失，`pickAssignTaker` 会挑中那个
    //    正在消失的旧层，反复报 `element was detached from the DOM, retrying`。
    //    用 `toHaveCount(0)` 而不是 `waitForResponse(...).catch(() => undefined)`：
    //    后者把「等失败」静默吞掉（评审 Minor 6），而且它等的是**列表** reload、不是这条真正的前件。
    await expect(
      frame.locator(ASSIGN_DIALOG),
      '清除成功后上一次的指派弹层应当已从 DOM 里消失',
    ).toHaveCount(0, { timeout: 10_000 })
    await openAssignDialog(page, row.locator(ROW_ASSIGN), '行内「接手人」那一格（指派回去）')
    // 选项是**异步**拉的（`D-0146` 后是 `fetchAssigneeOptions({role:'代练员'})`）⇒ `pickAssignTaker`
    // 会先等名单里真的出现这一项（那一步的诊断是可达的，见它的注释），再展开、再点。
    await pickAssignTaker(page, TAKER_NAME)
    await frame.locator(DIALOG_ASSIGN_SAVE).click()
    await expect(toast(frame)).toContainText('指派成功')
    // 判据打在界面那一格上：接单人的**后端字段名本线未核实**，不猜（§5E）。
    await withTimeout(10_000).poll(async () => (await row.textContent()) ?? '')
      .toContain(TAKER_NAME)
    // 界面那一格是**软证据**（它只是重新渲染的数据）；真正的落库判据是接线表里的
    // `created_at`/`updated_at` 之外那一处——用后端详情端点把「这笔单确实不在 UNASSIGNED」钉住，
    // 与 ③④ 的回读同口径（指派把状态推进到 `IN_PROGRESS`）。
    expect((await readOrder(page, orderNo)).status).toBe('IN_PROGRESS')
  })

  await test.step('⑦ 完成（客服）：先验「不能直接改已完成」，再经「待验收」改已完成', async () => {
    await switchAccount(page, 'cs')
    await gotoOrderList(page, orderNo)
    const frame = island(page)

    // 前件：⑥ 结束时这一笔是「代练中」（⑥ 末尾已回读钉住，这里把 ⑦ 自己的前态再钉一次——
    // 下面 F3 那条负例只在这一档才成立）。
    expect((await readOrder(page, orderNo)).status, '⑦ 的前态应当是代练中').toBe('IN_PROGRESS')

    await clickRowAction(rowOf(page, orderNo).locator(ROW_STATUS_BADGE), '行内状态徽标')
    await frame.locator(STATUS_DIALOG).waitFor({ timeout: 15_000 })
    // radio 的原生控件被 layui 隐藏 ⇒ 点替身节点；替身里的文本取自原生 input 的 `title` 属性
    // （实测 `<div class="layui-form-radio"><i …></i><div>已完成</div></div>`）。
    // ⚠️ 候选来自后端 `order.allowed_transitions`（按调用方持有的节点过滤过）：
    //    没有 `order.change_status_completed`(17) 的人**根本渲染不出「已完成」这一项**；
    //    而 `allowed_transitions` 为空时**弹层都不开**、只弹一句提示（`order-orderlist.ts:2112-2118`）。
    //    ⇒ 这两条都是**真判据**：客服若没有节点 17，这里会红，而那正是我们要知道的事。
    const completedRadio = frame.locator(DIALOG_STATUS_RADIO_PROXY, { hasText: '已完成' })
    await expect(completedRadio.first(), '客服应当有「已完成」这个流转目标').toBeVisible()

    // ⭐ 批 F / F3 的浏览器侧回归网（本批新增）：代练中选「已完成」⇒ 只弹**逐字**提示、零请求。
    // ⚠️ 它此前**不在这条链上**（`pnpm e2e` 与前端四门禁是两条独立的网）⇒ 少了这一段，
    //    F3 的行为在真浏览器里就没有判据。
    const watched = watchTransitionRequests(page)
    try {
      await completedRadio.first().click()
      await frame.locator(DIALOG_STATUS_SAVE).click()
      await expect(
        frame.locator(ALERT_CONTENT),
        'F3：代练中选「已完成」应当弹出逐字提示',
      ).toContainText(COMPLETED_REQUIRES_REVIEW_MESSAGE, { timeout: 10_000 })
      expect(
        watched.calls,
        'F3：弹提示这一支不发流转请求（后端那道闸仍在，这里是双保险）',
      ).toEqual([])
      // 后端判据：这一下没有把状态改走（提示之外还要钉住「订单真的没动」）。
      expect((await readOrder(page, orderNo)).status).toBe('IN_PROGRESS')
    } finally {
      watched.stop()
    }
    // 关掉提示窗：F3 的实现**不关**状态弹层（用户还能改选别的目标）⇒ 关掉提示后接着用同一个层。
    await expect(frame.locator(ALERT_DIALOG), 'F3 的提示窗应当还开着').toBeVisible()
    await frame.locator(LAYER_CONFIRM_OK).click()
    await expect(frame.locator(ALERT_DIALOG)).toHaveCount(0, { timeout: 10_000 })

    // —— 正路第一步：在**同一个**状态弹层里改成「待验收」（客服持节点 16）——
    const reviewRadio = frame.locator(DIALOG_STATUS_RADIO_PROXY, { hasText: '待验收' })
    await expect(reviewRadio.first(), '客服应当有「待验收」这个流转目标').toBeVisible()
    await reviewRadio.first().click()
    await frame.locator(DIALOG_STATUS_SAVE).click()
    await expect(toast(frame)).toContainText('状态已更新')
    await withTimeout(15_000).poll(async () => (await readOrder(page, orderNo)).status)
      .toBe('PENDING_REVIEW')

    // —— 正路第二步：**重开**弹层选「已完成」⇒ 照旧成功（F3 拦的是「直接改」，不是「不许改」）——
    await clickRowAction(
      rowOf(page, orderNo).locator(ROW_STATUS_BADGE),
      '行内状态徽标（待验收那一档）',
    )
    await frame.locator(STATUS_DIALOG).waitFor({ timeout: 15_000 })
    const completedAgain = frame.locator(DIALOG_STATUS_RADIO_PROXY, { hasText: '已完成' })
    await expect(completedAgain.first(), '待验收时「已完成」应当照旧可选').toBeVisible()
    await completedAgain.first().click()
    await frame.locator(DIALOG_STATUS_SAVE).click()
    await expect(toast(frame)).toContainText('状态已更新')
    // 回读：状态**真的**变成 `COMPLETED`。
    // ⚠️ 这一步同时在验 T3 那条硬约束——客服的提成模式必须是 30，否则后端会回业务码 1000 并整笔回滚。
    await withTimeout(15_000).poll(async () => (await readOrder(page, orderNo)).status)
      .toBe('COMPLETED')
  })

  await test.step('⑧ 结账（管理员）：绩效管理页点这一笔的「结账状态」', async () => {
    // 结账节点是 `finance.change_settlement_status`，客服没有 ⇒ 用管理员（节点短路成全集）。
    await switchAccount(page, 'admin')
    // 回读的是**代练员那条打单工资行**（见 `takerPerformance`：`[0]` 是客服的销售提成行；
    // 找不到那一行会**响亮失败**，不回落）。端点按 `order_no` 精确筛，不靠分页第一页。
    const before = takerPerformance(await readPerformances(page, orderNo))
    // 前置：这一行必须**还没结账**。钉死这个**前态**而不是「变了就行」——后者在
    // 「本来就是已结账」时也会绿（重复跑同一笔的真实形态），那是恒真判据，本项目明令禁止
    // （`AGENTS.md` §5G 的 E-0064 同族）；本条把「本轮确实发生了 未结账→已结账」变成前提 + 结论。
    expect(before.settle_status, '本轮的绩效行应当还没结账（否则下面那条断言会假绿）').toBe('未结账')

    // 界面侧同样**先筛**（页面「搜索1」→ 后端 `order__order_no__icontains`，见
    // `gotoPerformance` 的注释）：不筛就等于假设「新行恰好在第一页」，而本线每跑一次就多一笔单。
    // `perfRowOf` 再按 `TAKER_NAME` 收窄到代练员那一行（两行的订单号文本相同）。
    await gotoPerformance(page, orderNo)
    const frame = island(page)
    const row = perfRowOf(page, orderNo)
    await clickRowAction(row.locator(PERF_SETTLE_TAG), '绩效页「结账状态」标签')
    // `layer.confirm`（标题「清算绩效」，按钮「确认结账」/「取消」）⇒ 点第一个按钮。
    await frame.locator(LAYER_CONFIRM_OK).click()
    await expect(toast(frame)).toContainText('已结账')

    // 回读：打单工资那行从「未结账」变成「已结账」。
    // 值 `已结账` 与后端 `SettleStatus` 同词。钉字面值：变成别的词也会红。
    await withTimeout(15_000)
      .poll(async () => takerPerformance(await readPerformances(page, orderNo)).settle_status)
      .toBe('已结账')
  })

  await test.step('⑨ 部分退款（客服）：填金额 + 必填原因', async () => {
    await switchAccount(page, 'cs')
    await gotoOrderList(page, orderNo)
    const frame = island(page)
    await clickRowAction(rowOf(page, orderNo).locator(ROW_REFUND), '行内退款金额链接')
    await frame.locator(DIALOG_REFUND_AMOUNT).waitFor({ timeout: 10_000 })
    await frame.locator(DIALOG_REFUND_AMOUNT).fill(REFUND_AMOUNT)
    await frame.locator(DIALOG_REFUND_REASON).fill('E2E 部分退款（合成原因）')
    await frame.locator(DIALOG_REFUND_SAVE).click()
    // 成功文案逐字取实现（`order-orderlist.ts:607` 的 `REFUND_SUCCESS_MESSAGE`）——
    // 比「有一个 toast」更强：失败时弹的是后端 msg 原文，不会是这一句。
    await expect(toast(frame)).toContainText('已记录退款')
    // 回读：`refund_fee` 必须真的涨到这一笔的金额。
    await withTimeout(10_000).poll(async () => String((await readOrder(page, orderNo)).refund_fee))
      .toBe(REFUND_AMOUNT)
  })

  await test.step('收尾：整条链跑完后不得有 JS 崩溃或 5xx', async () => {
    expectNoPageErrors(obs, '订单主干')
  })
})
