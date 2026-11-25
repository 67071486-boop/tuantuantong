// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/notify', () => ({ notifyError: vi.fn() }))

import { fetchMe, login, logout } from '@/api/auth'
import { AuthExpiredError } from '@/api/errors'
import { installGuards } from '@/router/guards'
import { useSessionStore } from '@/stores/session'
import App from '../App.vue'

/**
 * spec §6.1 第 27 条：**薄集成**——把「守卫 + 会话状态机 + App 的布局分支」串起来跑一遍真实闭环。
 *
 * 它**证明不了**什么（必须在报告里如实写上，spec §6.2-5）：
 *  - CSRF 注入与 401 分流：那两条在 `api/http.ts` 里，本文件把 `@/api/auth` 整个桩掉了；
 *  - `main.ts` 的装配：本文件自己 `installGuards`，不加载 `main.ts`；
 *  - 登录页的 UI：`/login` 用的是占位组件（`LoginView` 有自己的 5 条用例）。
 *
 * 它**要证明的**是那条最容易断的链：未认证 → 挡回登录页且**不渲染业务壳**；
 * 登录后 → 进业务壳；登出 → 本地立刻终结并被挡回去。
 * `App.vue` 的 `meta.layout ?? 'shell'` 分支只有这里覆盖（计划里的变异 ⑦）。
 */
const Blank = { template: '<div />' }

const USER = {
  id: 1,
  username: 'kefu',
  display_name: '小客服',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
}

function makeRouter() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/login',
        name: 'login',
        component: Blank,
        meta: { requiresAuth: false, layout: 'blank' },
      },
      // `requiresAuth` **故意不声明**：守卫默认 true（fail-closed），本文件顺带把它钉住。
      { path: '/', name: 'home', component: Blank },
      { path: '/orders', name: 'orders', component: Blank },
    ],
  })
  installGuards(router)
  return router
}

beforeEach(() => {
  vi.mocked(fetchMe).mockReset()
  vi.mocked(login).mockReset()
  vi.mocked(logout).mockReset()
})

describe('登录闭环（薄集成）', () => {
  it('未认证落登录页 → 登录后进业务壳 → 登出后被挡回登录页', async () => {
    // ⚠️ pinia **只建一个**：`mount` 的 `app.use(pinia)` 会把它设为 active，
    // 守卫（在组件外调用）与用例里 `useSessionStore()` 必须拿到**同一个实例**——
    // 建两个就会变成"断言看似通过、其实测的不是一条链"的假绿。
    const pinia = createPinia()
    setActivePinia(pinia)
    const router = makeRouter()

    vi.mocked(fetchMe).mockRejectedValue(new AuthExpiredError('未认证'))
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    await router.isReady()
    await flushPromises()

    // 1) 初始导航 `/`：未认证 → 落登录页，且**不渲染业务壳**
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/')
    expect(wrapper.find('.shell').exists()).toBe(false)

    // 2) 访问受保护的 `/orders`：挡回登录页并把原 fullPath 放进 `redirect`
    await router.push('/orders')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/orders')
    expect(wrapper.find('.shell').exists()).toBe(false)

    // 3) 登录（同一个 store 实例）→ 受保护路由放行，业务壳出现
    const store = useSessionStore()
    vi.mocked(login).mockResolvedValue({ ...USER })
    await store.login('kefu', 'pw')
    await router.push('/orders')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('orders')
    expect(wrapper.find('.shell').exists()).toBe(true)

    // 4) 登出：本地**先**终结为 guest，再导航 → 重新被挡回登录页、壳消失
    vi.mocked(logout).mockResolvedValue(null)
    await store.logout()
    expect(store.status).toBe('guest')
    await router.push('/')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
    expect(wrapper.find('.shell').exists()).toBe(false)

    wrapper.unmount()
  })
})
