import { expect, type Page } from '@playwright/test'

import {
  LOGIN_ERROR, LOGIN_PASSWORD, LOGIN_SUBMIT, LOGIN_TITLE_PATTERN, LOGIN_USERNAME,
} from './selectors'

/**
 * E2E 账号。⛔ 与 `src/apps/platform/management/commands/seed_e2e.py` 的 `PASSWORDS`
 * **必须逐字一致**——改一边就要改另一边。这是本夹具与那条命令之间**已有机器判据**的那处耦合
 * （`01-auth-guard.spec.ts` 的「口令对拍」），特意放在文件顶部显眼处。
 * 口令是**合成值**，只在本机 E2E 租户里有意义。
 */
export const ACCOUNTS = {
  /** 系统管理员：节点判据短路成全集 ⇒ 全站可开（冒烟用这个角色）。 */
  admin: { username: 'admin', password: 'e2e-pw-admin' },
  /** 客服：有 `order.add_edit` ⇒ 主干里负责录单。 */
  cs: { username: 'cs001', password: 'e2e-pw-cs001' },
  /** 客服主管：客服的模块上多一个 `system_user`（账号管理），本线四职位对拍之一。 */
  supervisor: { username: 'supervisor001', password: 'e2e-pw-supervisor001' },
  /** 代练员：没有 `order.add_edit` ⇒ 越权用例的被测对象。 */
  taker: { username: 'taker001', password: 'e2e-pw-taker001' },
  /** 系统开发员：只有 `my_*` / `home`，本线四职位对拍之一。 */
  dev: { username: 'dev001', password: 'e2e-pw-dev001' },
  /**
   * 最小权限：**自定义 RBAC 角色**（不是内置组预设），节点集显式枚举、只给 `order.list`
   * ——本线（`minimal-role-audit`）新增的第 5 个被测职位。
   */
  minrole: { username: 'minrole001', password: 'e2e-pw-minrole001' },
  /**
   * 客服（单游戏）：节点集与内置「客服」**同源**（`seed_e2e._seed_gamescope` 调
   * `apps/rbac/seed.py::builtin_role_nodes("客服")`），但 `StaffProfile.games` **只挂第 1 款**
   * 游戏 ——本线（`game-scope-audit`）新增的「**节点够、游戏不够**」那一格。
   * ⛔ 它补的正是审计此前缺的那条权限轴：租户只有 1 款游戏时，「只给一款」与「给全部」在库里
   * 是同一种状态，`apps/business/game_scope.py::games_of` 的收窄**一次都没被测过**。
   */
  gamescope: { username: 'gamescope001', password: 'e2e-pw-gamescope001' },
  /** 不进任何组、无档案 ⇒ 「完全没有权限」那一支。 */
  nobody: { username: 'nobody001', password: 'e2e-pw-nobody001' },
} as const

export type RoleName = keyof typeof ACCOUNTS

/** 走**真实登录表单**登录（设计取舍见本任务开头那段说明）。 */
export async function login(page: Page, role: RoleName): Promise<void> {
  const { username, password } = ACCOUNTS[role]
  await page.goto('/login')
  // 标题只证明「以产品名开头」，不证明「我在登录页」：岛页改的是 iframe 标题，
  // 顶层标题和登录页标题都命中这个前缀。人在登录页靠两个输入框真能填上。
  // 后半截是运行期工作室名，所以判前缀模式，不判写死的产品名。
  await expect(page).toHaveTitle(LOGIN_TITLE_PATTERN)
  await page.locator(LOGIN_USERNAME).fill(username)
  await page.locator(LOGIN_PASSWORD).fill(password)
  await page.locator(LOGIN_SUBMIT).click()
  // 成功判据①（**承重**）：离开登录页（默认去 `name:'home'` = `/`，
  // 见 `LoginView.vue` 的 `router.replace(target ?? { name: 'home' })`）。
  await expect(page).not.toHaveURL(/\/login(\?|$)/, { timeout: 15_000 })
  // 成功判据②（**补充，非承重**）：错误容器不得残留
  // （`LoginView.vue` 的 `class="login-error"`，即 `LOGIN_ERROR`；文本 = 服务端 msg 原样）。
  // ⚠️ 它**管不了**「因为别的原因离开登录页」——`.login-error` 是 `v-if`，登录页一卸载节点就随之消失，
  //    于是在任何「已离开 /login」的终态上它**恒为 0**；别把它当成比判据①更强的证据。
  await expect(page.locator(LOGIN_ERROR)).toHaveCount(0)
  // 身份判据：登进来的必须是指定角色。忽略 role、永远登 admin 的 login()
  // 会让「都能登录」和「口令对拍」一起变绿（对拍比的是常量）。
  // 看 MeView.get 的 username（_user_payload），信封是 {code,msg,data}。
  const me = await page.request.get('/api/auth/me')
  const meBody = await me.json()
  expect(meBody.code, `登录 ${role} 后 /api/auth/me 未返回成功信封`).toBe(0)
  expect(
    meBody.data.user.username,
    `登录 ${role} 后 /api/auth/me 报的账号不是 ${username}（登录没登到指定角色）`,
  ).toBe(username)
}

/**
 * 在同一张 page 上换账号。先清 Cookie，再整页打开 `/`，然后 login()。
 * 不清 Cookie 就 goto('/login') 会被守卫弹回首页（凭据还在）。
 * 不要用 reload() 代替：停在受保护路径上时，reload 会先被重定向到
 * `/login?redirect=…`，login() 的「已离开登录页」会在登录之前就判红。
 */
export async function switchAccount(page: Page, role: RoleName): Promise<void> {
  await page.context().clearCookies()
  await page.goto('/', { waitUntil: 'load' })
  await login(page, role)
}
