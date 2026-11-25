import { expect, test } from '@playwright/test'

import { LOGIN_PASSWORD, LOGIN_SUBMIT, LOGIN_TITLE_PATTERN, LOGIN_USERNAME } from '../fixtures/selectors'

test.describe('连通性：真浏览器能打开我们的应用', () => {
  test('登录页渲染正常（跳转 + 标题 + 表单）', async ({ page }) => {
    await page.goto('/')
    // 未登录时 `/` 必须跳登录页——这一条同时证明 SPA 路由与鉴权守卫是活的。
    await expect(page).toHaveURL(/\/login(\?|$)/)
    // 标题冒烟判据，⛔ **不许为让它变绿而删掉**。
    // ⚠️ **它只证明「顶层标题以产品名开头」，不证明「我在登录页」**（评审 J3 订正）：
    //    island 页的 `document.title` 写在 iframe 文档里，顶层标题在除登录页外的所有壳路由上
    //    都是 `index.html` 那句 ⇒ 这条在任何一页都会绿。「我在登录页」由上面那行
    //    `toHaveURL(/\/login/)` 承担（详见 `selectors.ts` 的 `LOGIN_TITLE_PATTERN` 注释）。
    //    Task 7（2026-09-24）起登录页会把标题改写成 `团团通 - <工作室名>`
    //    （`LoginView.vue` 的 `onMounted` 里那次 `document.title` 赋值）
    //    ⇒ 判**前缀模式**，不再判 `APP_TITLE` 那个字面量。
    await expect(page).toHaveTitle(LOGIN_TITLE_PATTERN)
    // 两个输入框各有 id（`LoginView.vue` 的 `id="login-username"` / `id="login-password"`，
    // 即 `LOGIN_USERNAME` / `LOGIN_PASSWORD`）：用 id 常量而不是 `input.login-input`，
    // 选择器只有一个出处，类名改了不会静默漂移（`AGENTS.md` §5G 的 E-0061）。
    await expect(page.locator(LOGIN_USERNAME)).toHaveCount(1)
    await expect(page.locator(LOGIN_PASSWORD)).toHaveCount(1)
    await expect(page.locator(LOGIN_SUBMIT)).toBeVisible()
  })

  test('经 Vite 代理打后端能拿到业务信封（证明租户 Host 注入生效）', async ({ request }) => {
    const res = await request.get('/api/auth/csrf')
    expect(res.status()).toBe(200)
    const body = await res.json()
    // 判据是**业务码**，不是「响应体非空」：api_response() 永远返回信封 dict（E-0063）。
    expect(body.code).toBe(0)
  })
})
