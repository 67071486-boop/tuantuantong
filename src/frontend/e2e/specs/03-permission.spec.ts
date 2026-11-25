import { expect, test, type Page } from '@playwright/test'

import { gameIdByName } from '../fixtures/api'
import { login, switchAccount } from '../fixtures/auth'
import { expectNoPageErrors, observe } from '../fixtures/observers'

/** 从浏览器上下文的 cookie 里取 CSRF token，并组装成写请求要带的头。 */
async function csrfHeaders(page: Page): Promise<Record<string, string>> {
  const token = (await page.context().cookies()).find((c) => c.name === 'csrftoken')?.value
  if (!token) throw new Error('上下文里没有 csrftoken —— 会话没建立起来，先查登录夹具')
  return { 'X-CSRFToken': token }
}

/**
 * 侧边栏菜单的**渲染文本**（Element Plus：`aside.shell-sidebar` 的**根** `.el-menu`）。
 *
 * ⛔ 本函数与任务书给的原样有两处**实测**差别（都不是读码推断，两次跑都复现）：
 *  1. **选择器必须钉在根菜单上**：`aside.shell-sidebar .el-menu` 会命中 **7** 个 `<ul>`
 *     ——根菜单 + 每个 `el-sub-menu` 自己的 `ul.el-menu--inline`——Playwright 严格模式直接报
 *     `strict mode violation: locator('aside.shell-sidebar .el-menu') resolved to 7 elements`，
 *     连第一行的 `toBeVisible()` 都过不去。
 *  2. **读 `innerText` 不可靠**：`el-sub-menu` 收起时它那个 `<ul>` 挂着 `v-show`
 *     （读码，非推断：`element-plus/es/components/menu/src/sub-menu.mjs` 的
 *     `withDirectives(h('ul', …, [slots.default?.()]), [[vShow, opened.value]])`）
 *     ⇒ **收起的分组条目不进 `innerText`**。而「哪一组恰好是展开的」**每跑一次都不一样**
 *     （同一份代码：一次跑出 `我的相关` 展开且带条目，下一次跑出**一组都没展开**）
 *     ⇒ 拿它当判据，客服那一侧的对照会**随机红**（实测收到串逐字是
 *     `"后台首页指派给我的订单内部抢单大厅我的相关订单管理财务相关游戏维护系统维护第三方平台"`，
 *     一个条目都没有）。
 *     改读 `textContent`：条目本来就在 DOM 里（`v-show` 只改样式、不删节点），
 *     于是判据与「碰巧哪一组展开」解耦。
 * ⚠️ 代价（如实写）：`textContent` **不区分**「渲染了但被 CSS 藏」。而那种回归本身也是 bug
 *    ——不该有这个条目的角色被渲染出了这一条——它会让代练员那一侧红，方向是对的。
 */
async function menuText(page: Page): Promise<string> {
  const menu = page.locator('aside.shell-sidebar > .el-menu')
  await expect(menu).toBeVisible()
  return (await menu.textContent())?.replace(/\s+/g, '') ?? ''
}

const NEW_ORDER_LABEL = '录入订单'
/** 录单接口要带的游戏名（与 `seed_e2e` 建的那一款一致，见 `fixtures/api.ts` 的 `gameIdByName`）。 */
const GAME_NAME = 'E2E 测试游戏'

test.describe('越权：不该能干的事，服务端必须拦住', () => {
  test('① 代练员的菜单里没有「录入订单」——且客服与管理员有（对照，证明这个标签真的存在）', async ({ page }) => {
    await login(page, 'taker')
    expect(await menuText(page)).not.toContain(NEW_ORDER_LABEL)

    // ⚠️ 换账号必须走 `switchAccount`（见 `fixtures/auth.ts` 的说明）：只 `clearCookies()`
    //    再 `login()` 会被路由守卫弹回首页，登录表单一辈子不出现。
    await switchAccount(page, 'cs')
    expect(await menuText(page)).toContain(NEW_ORDER_LABEL) // 对照：标签本身是存在的
    // ⛔ **管理员那一半也要真的断言**（终审 Minor）：用例标题写的是「客服/管理员」，
    //    而这里原先只验了客服 ⇒ 标题比断言强。补上管理员（它的节点判据短路成全集）。
    await switchAccount(page, 'admin')
    expect(await menuText(page)).toContain(NEW_ORDER_LABEL)
  })

  test('② 代练员 URL 直达 /orders/new 会被挡走', async ({ page }) => {
    await login(page, 'taker')
    const obs = observe(page)
    await page.goto('/orders/new')
    // 停在录入页 = 漏了；被挡走（去首页/无权限页）即通过。
    await expect(page).not.toHaveURL(/\/orders\/new/)
    // 被挡这件事本身不该伴随 JS 崩溃。
    // ⚠️ 用共享夹具而不是手搓 `expect(obs.pageErrors, …)`（**强化**，不是放宽）：夹具同时判
    //    `pageErrors` **与** `serverErrors`（本任务的判定口径：`pageerror` / HTTP ≥ 500 都算红），
    //    而任务书那行只读 `pageErrors` ⇒ 这段路上真出 5xx 会**一声不响**（观测器采了却没人判）。
    expectNoPageErrors(obs, '代练员直达 /orders/new 被挡走')
  })

  test('③ 绕过界面直接打 POST /api/business/orders/ 必须 403（且不是 CSRF 造成的）', async ({ page }) => {
    // —— 被测对象：代练员（默认没有 order.add_edit）——
    await login(page, 'taker')
    // ⚠️ 三发 POST 用**同一个真游戏主键**（终审 Minor）：原先把 `game` 写死成 `1`，那是 E2E 租户里
    //    唯一一款游戏**碰巧**取到的值——换库 / 重新 seed 后主键漂移，客服那一发就会从「真的建出订单」
    //    退化成「业务码 1000」，而下面原先只断 `not.toBe(403)` ⇒ **静默不再建单**、对照失去意义。
    //    按**名字**取 id 的方式与 `02-golden-path` 共用同一份实现（`fixtures/api.ts::gameIdByName`）。
    //    `games` 的 list 走默认权限类（只要登录）⇒ 代练员这一发读得到。
    const gameId = await gameIdByName(page, GAME_NAME)
    const payload = { game: gameId, project_text: 'E2E 越权尝试', total_price: '1.00' }
    const takerRes = await page.request.post('/api/business/orders/', {
      headers: { ...(await csrfHeaders(page)), 'Content-Type': 'application/json' },
      data: payload,
    })
    expect(takerRes.status(), '代练员打录单接口应当 403').toBe(403)
    const takerBody = await takerRes.json()
    expect(takerBody.code, '业务码也应当是 403').toBe(403)
    expect(
      takerBody.msg,
      '⛔ 这个 403 带 CSRF 字样 ⇒ 它是 CSRF 拦的，不是权限拦的，本用例是假绿',
    ).not.toContain('CSRF')

    // —— 对照一：完全没有权限的账号，同样 403 ——
    await switchAccount(page, 'nobody')
    const nobodyRes = await page.request.post('/api/business/orders/', {
      headers: { ...(await csrfHeaders(page)), 'Content-Type': 'application/json' },
      data: { ...payload, project_text: 'E2E 越权尝试（零权限）' },
    })
    expect(nobodyRes.status(), '零权限账号打录单接口应当 403').toBe(403)
    expect((await nobodyRes.json()).msg).not.toContain('CSRF')

    // —— 对照二：客服（有 order.add_edit）**不得**是 403 ⇒ 证明上面两个 403 是「按节点拦的」——
    await switchAccount(page, 'cs')
    const csRes = await page.request.post('/api/business/orders/', {
      headers: { ...(await csrfHeaders(page)), 'Content-Type': 'application/json' },
      data: { ...payload, project_text: 'E2E 越权对照（客服）' },
    })
    // ⛔ **收紧到 200**（终审 Minor）：原先是 `not.toBe(403)`，那对「5xx / CSRF 400 / 任何别的失败」
    //    都会绿——而这条对照的全部意义就是「客服这一发**真的走通了**」。注意：**单看 200 不够**，
    //    坏游戏会回 HTTP 200 + 业务码 1000（游戏主键现在按名字取，正是为了让这一发真的建单）。
    expect(csRes.status(), '客服有录单节点，这一发应当真的成功（200）').toBe(200)
    const csBody = await csRes.json()
    expect(
      csBody.code,
      `客服这一发应当是成功信封（业务码 0），实际 msg=${csBody.msg}`,
    ).toBe(0)
    expect(
      csBody.msg,
      `客服这一发若失败，会掩盖上面两个 403 的含义（业务码=${csBody.code}，msg=${csBody.msg}）`,
    ).not.toContain('CSRF')
  })
})
