/**
 * 「我的资料」的真浏览器验收（Task 9）。
 *
 * ## 这一条测的是**端到端的路**，不是页内单元
 *
 * 页内逻辑（回填 / 回显 / 页签 / 白名单）在
 * `src/layui-island/pages/__tests__/user-myinfo.spec.ts`
 * 与 `src/config/__tests__/avatar.spec.ts` 里已经逐条测过（Task 5–8）。本文件的职责是
 * **只有真浏览器能证明的那条链**：右上角下拉 → 真路由 → island iframe 挂载 → 改字段 → 保存 →
 * **整页重载后仍在**（判据是持久化，不是 toast）→ 从下拉再进「修改密码」→ 错口令回**后端那句**
 * msg → 对口令真的改成。
 *
 * ## 三条实测出来的交互事实（写在这里，免得下一个人再踩）
 *
 * 1. **右上角下拉是 Element Plus 的 `ElDropdown`**（`layouts/AppTopbar.vue:61-70`），
 *    菜单**teleport 到顶层 `document.body`**、且**在悬停后延迟打开**（EP 的 `showTimeout`，约 150ms）。
 *    ⇒ 先 `hover()`，再**等目标项可见**（`toBeVisible()` 自等，不是 `waitForTimeout`），最后才点。
 *    ⛔ 菜单**不在 island iframe 里** ⇒ 这两个选择器相对 `page` 取，不是 `frameLocator`。
 * 2. **`layer.*` 建在 island iframe 自己的 document 里**（`02-golden-path.spec.ts:15-16` 已实测过）
 *    ⇒ toast 的 `.layui-layer-*` 一律 `frameLocator(ISLAND_IFRAME)` 之后再取。
 *    ⚠️ 任务书 Step 4 原文写的是 `page.locator('.layui-layer-content')`（顶层 page）——
 *    执行期实测：**顶层永远找不到它**，必须落在 iframe 作用域里，这里按实现与既有先例订正。
 * 3. 页签深链 `?tab=password` 由**宿主**转发（`LayuiIslandHost.vue:19,41-44` 的 query 白名单）
 *    ⇒ 从已在 `/accounts/profile` 的页面点「修改密码」会让 iframe 的 `src` 变化并**重新加载**，
 *    页面模块重新 `mount()`、深链那一段才会跑（详见 Task 9 报告 §「页签深链」）。
 *
 * ## ⚠️ 本用例会**改掉 admin 的口令**（`e2e-pw-admin` → `e2e-new-pw-1`）
 *
 * **这是有意为之，不要「修」成改完再改回去**：
 *  - `e2e/run.mjs` 每次整体运行都会先跑 `seed_e2e`，而
 *    `seed_e2e.py:85-86` 对既有账号**每次都 `set_password(PASSWORDS[...])`**
 *    （注释原文：「每次都设 ⇒ 重跑结果确定」）⇒ **下一次 `pnpm e2e` 会把它恢复**，
 *    所以不需要、也不该加一段「改回去」的补偿步骤（那只会多一条会自己失败的路）。
 *  - ⛔ **不得让新口令漏到别的 spec**：本文件是 05、按文件名字典序在最后跑
 *    （00/01/02/03/04 都已跑完）；里面也**不许**把 `e2e-new-pw-1` 写进
 *    `fixtures/auth.ts::ACCOUNTS`（那份常量与 `seed_e2e.PASSWORDS` 有机器判据钉着）。
 */
import { expect, test, type Page } from '@playwright/test'

import { login } from '../fixtures/auth'
import {
  ISLAND_IFRAME, LAYER_MSG, PASSWORD_LINK, PROFILE_LINK, PROFILE_MOBILE_INPUT,
  PROFILE_PWD_CONFIRM, PROFILE_PWD_NEW, PROFILE_PWD_OLD, PROFILE_PWD_SUBMIT,
  PROFILE_SAVE_BUTTON,
} from '../fixtures/selectors'

/** 改成什么手机号：**合成值**（形状像真的，但不是任何人的号码）。 */
const NEW_MOBILE = '13800000000'
/** 新口令（12 位，落在后端 6–18 的区间内）。⛔ 只在本文件内使用，不回写 `ACCOUNTS`。 */
const NEW_PASSWORD = 'e2e-new-pw-1'

/**
 * 打开顶栏账号下拉里的某一项（见文件头实测事实 1）。
 *
 * ## 为什么是「先把指针挪开 + 重试 hover」，而不是 `waitForTimeout`
 *
 * 全量跑了两次，本步**偶发**失败过一次：`[data-test="my-profile"]` 报 **element(s) not found**
 * （菜单**根本没挂上**，不是「挂上了但不可见」）——即那次 `hover()` 没被 EP 当成一次打开。
 * 两个候选机制（**都未确证**，`AGENTS.md` §5G 的 E-0081：只记症状、不记根因）：
 *  1. 浏览器只在指针**真的移动**时派发 `mouseenter`；指针恰好已停在触发器上、或触发器在
 *     `who` 就绪前后被替换过一次时，这一次 `hover()` 可能一次事件都不产生；
 *  2. EP 的延迟打开（约 150ms）在等待期间被一次 `mouseleave`（布局还在动）取消。
 * ⇒ 对策是把**人做的那一步**重试，而不是加 sleep：每次先 `mouse.move` 到别处强制产生位移，
 *    再 hover、再等菜单项；`expect(...).toPass()` 自带超时与失败信息。
 * ⛔ **不用** `waitForTimeout`（那是猜时间，且在本仓 `02` 的注释里已明确禁过）。
 */
async function openTopbarItem(page: Page, item: string): Promise<void> {
  const trigger = page.locator('.topbar-user')
  await expect(trigger, '顶栏账号区没渲染出来（登录态没进壳？）').toBeVisible()
  const target = page.locator(item)
  await expect(async () => {
    await page.mouse.move(0, 0) // 强制位移 ⇒ 下一次 hover 一定产生 mouseenter
    await trigger.hover()
    await expect(target).toBeVisible({ timeout: 2_000 })
  }, `顶栏下拉里的 ${item} 一直没打开`).toPass({ timeout: 20_000 })
  await target.click()
}

test('我的资料：改资料能存住、改密码先报错后成功', async ({ page }) => {
  await login(page, 'admin')

  // 1) 从右上角下拉进「我的资料」（⛔ 不用直接 goto：那只证明 URL 能开，
  //    证明不了那颗菜单项真的能点）
  await openTopbarItem(page, PROFILE_LINK)
  await expect(page).toHaveURL(/\/accounts\/profile/)

  const island = page.frameLocator(ISLAND_IFRAME)
  const mobile = island.locator(PROFILE_MOBILE_INPUT)
  await expect(mobile).toBeVisible()
  // ⛔ **必须等「回填完成」再改字段**，而这里的闸门就是**产品自己保证的那个时刻**：
  //    取数回来之前那 7 个可编辑框 + 保存按钮是 `disabled` 的（页面模块的「加载完成前不许编辑」，
  //    见 `user-myinfo.ts` 的 `LOADING_LOCKED_CONTROLS` / `setLoadingLock()`）。
  //    ⇒ `toBeEnabled()` 一头是「回填已落地」，另一头顺带证明那道锁**真的解开了**。
  //    ⚠️ 这条不是可选的稳健性道具：产品没那道锁的话，先打的字会被 `fillProfile()` 用服务端旧值
  //    覆盖，PATCH 写回空串而页面照旧报「保存成功」（真浏览器实测到过：PATCH 响应 278 字节，
  //    正常是 289）。产品侧修在页面模块；E2E 只是**等产品自己的可用时刻**。
  await expect(
    mobile,
    '资料回填没完成（保存控件还锁着）就改字段，会被回填用旧值覆盖',
  ).toBeEnabled()

  // 2) 改一个可改字段 → 保存 → **重新加载**后仍在（判据是持久化，不是 toast）
  await mobile.fill(NEW_MOBILE)
  await island.locator(PROFILE_SAVE_BUTTON).click()
  // ⚠️ 这条 toast 是**前端字面量**（`user-myinfo.ts:324` 的 `msg('保存成功')`），
  //    **不是**后端回的文案——后端只回成功信封（没有 msg）。它的作用是「这一发有回执」，
  //    ⛔ 别把它当成「存住了」的判据（真正的判据在下面两条）。
  // ⚠️ toast 在 iframe 自己的 document 里（文件头实测事实 2）。
  await expect(island.locator(LAYER_MSG).last()).toContainText('保存成功')
  await page.reload()
  // 判据①（DOM）：刷新后输入框里还是那个值。
  await expect(page.frameLocator(ISLAND_IFRAME).locator(PROFILE_MOBILE_INPUT)).toHaveValue(NEW_MOBILE)
  // 判据②（**不依赖 DOM / 浏览器表单恢复**）：重新用 API 读一次后端。
  // ⛔ **为什么不能只靠判据①**：Chromium 在 reload 时**可能自己恢复表单值**（bfcache / 表单状态恢复）
  //    ⇒ 即使 PATCH 根本没落库，输入框里也可能还是那个值，① 会**假绿**。
  //    这条走 `page.request`，读的是服务端真值，与页面 DOM 无关。
  const afterReload = await page.request.get('/api/auth/profile')
  const afterBody = await afterReload.json()
  expect(afterBody.code, '重载后回读 /api/auth/profile 没拿到成功信封').toBe(0)
  expect(
    afterBody.data.profile.mobile,
    '重载后后端里的 mobile 不是刚存进去的那个值（PATCH 没落库，或落库后被覆盖）',
  ).toBe(NEW_MOBILE)

  // 3) 右上角「修改密码」⇒ 直接落到第三个页签（宿主转发 `?tab=password` ⇒ iframe 重载）
  await openTopbarItem(page, PASSWORD_LINK)
  await expect(page).toHaveURL(/tab=password/)
  const island2 = page.frameLocator(ISLAND_IFRAME)
  await expect(island2.locator(PROFILE_PWD_OLD)).toBeVisible()

  // 4) 原密码错 ⇒ 显示**后端那句** msg（这一条才是真·后端文案：前端只是 `errorText` 原样回显，
  //    见 `user-myinfo.ts:326` 的 `msg(errorText(e))`；后端原文在 `apps/core/auth_views.py:303`）。
  await island2.locator(PROFILE_PWD_OLD).fill('definitely-wrong-pw')
  await island2.locator(PROFILE_PWD_NEW).fill(NEW_PASSWORD)
  await island2.locator(PROFILE_PWD_CONFIRM).fill(NEW_PASSWORD)
  await island2.locator(PROFILE_PWD_SUBMIT).click()
  // 后端文案原文见 `apps/core/auth_views.py:303`（`原密码不正确`，code=1000、HTTP 200）。
  await expect(island2.locator(LAYER_MSG).last()).toContainText('原密码')

  // 5) 填对 ⇒ 成功
  await island2.locator(PROFILE_PWD_OLD).fill('e2e-pw-admin')
  await island2.locator(PROFILE_PWD_NEW).fill(NEW_PASSWORD)
  await island2.locator(PROFILE_PWD_CONFIRM).fill(NEW_PASSWORD)
  await island2.locator(PROFILE_PWD_SUBMIT).click()
  // 判据①（**这一发真的成功**）：成功 toast。⚠️ 与保存那条同源，是**前端字面量**
  // （`user-myinfo.ts:345` 的 `msg('修改成功')`），不是后端回的文案——后端成功时只回空信封。
  // ⛔ 没有它，下面的 URL 判据在**点击之前**就已经成立（人本来就在 `/accounts/profile`）
  // ⇒ 那是一条恒真的「判据」。
  await expect(island2.locator(LAYER_MSG).last()).toContainText('修改成功')
  // 判据②（任务书要求的那条）：改完**仍登录着** ⇒ 不跳登录页（`update_session_auth_hash`）。
  await expect(page).not.toHaveURL(/\/login/)
})
