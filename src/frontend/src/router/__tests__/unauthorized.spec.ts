import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
// http.ts 的真实实现会建 axios 实例并持有模块级单例；这里只关心"接线"本身。
vi.mock('@/api/http', () => ({ setUnauthorizedHandler: vi.fn() }))

import { setUnauthorizedHandler } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import { createUnauthorizedHandler, installUnauthorizedHandler } from '../unauthorized'

const setHandler = vi.mocked(setUnauthorizedHandler)

const Blank = { template: '<div/>' }

function makeRouter() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: Blank, meta: { requiresAuth: false, layout: 'blank' } },
      { path: '/', name: 'home', component: Blank },
      { path: '/orders/new', name: 'order-new', component: Blank },
      // 前缀判据的反例路由（见「`/login2` 不是登录页」一条）
      { path: '/login2', name: 'login2', component: Blank },
    ],
  })
  return { router, onUnauthorized: createUnauthorizedHandler(router) }
}

const kefu = {
  id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
  is_tenant_admin: false, roles: [], permissions: [],
}

beforeEach(() => {
  setActivePinia(createPinia())
  setHandler.mockReset()
})

describe('401 处理器（装配层）', () => {
  it('业务路由上 401 → 清会话 + 落 /login?redirect=<原 fullPath>', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/orders/new')
    const session = useSessionStore()
    session.user = kefu
    session.status = 'ready'

    onUnauthorized()
    await flushPromises()

    expect(session.status).toBe('guest')
    expect(session.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/orders/new')
  })

  it('已在 /login 上 → 不再导航（防死循环）', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/login')
    const replaceSpy = vi.spyOn(router, 'replace')

    onUnauthorized()
    await flushPromises()

    expect(replaceSpy).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('login')
  })

  it('并发 401 只导航一次', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/orders/new')
    const replaceSpy = vi.spyOn(router, 'replace')

    onUnauthorized()
    onUnauthorized()
    await flushPromises()

    expect(replaceSpy).toHaveBeenCalledTimes(1)
  })

  it('redirect 不会变成 /login 自身（在 /login 的前缀路径上也一样）', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/login')
    const session = useSessionStore()
    session.status = 'ready'

    onUnauthorized()
    await flushPromises()

    expect(router.currentRoute.value.fullPath.startsWith('/login')).toBe(true)
    expect(router.currentRoute.value.query.redirect).toBeUndefined()
  })

  // `redirecting` 的**复位**必须有判据（真实场景见下）。
  // 真实场景：首次 401 落地 → 重新登录 → 又来一次 401；不复位则永不导航（会话已清、人却停在业务页）。
  it('复位：第二次 401（跨一次已完成的导航）仍能导航', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/orders/new')
    const replaceSpy = vi.spyOn(router, 'replace')

    onUnauthorized()
    await flushPromises() // 让 replace 落地 → .finally 复位
    await router.push('/')
    onUnauthorized()
    await flushPromises()

    expect(replaceSpy).toHaveBeenCalledTimes(2)
  })

  // `installUnauthorizedHandler` 与 `main.ts` 的注入点要有**装配层**证据（spec §6.3 变异①）——
  // 删掉 `setUnauthorizedHandler(...)` 那行测试全绿，而 spec §6.3 变异①要的是**装配层**证据。
  // 这条不只断 spy 次数，还用捕获到的函数真的走一遍导航（证明接的是真处理器、不是空壳）。
  it('装配：installUnauthorizedHandler 把真处理器接到 api/http 的单例上（恰一次）', async () => {
    const { router } = makeRouter()
    await router.push('/orders/new')
    const session = useSessionStore()
    session.status = 'ready'

    installUnauthorizedHandler(router)

    expect(setHandler).toHaveBeenCalledTimes(1)
    const wired = setHandler.mock.calls[0]?.[0]
    expect(typeof wired).toBe('function')

    wired?.()
    await flushPromises()

    expect(session.status).toBe('guest')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/orders/new')
  })

  // 判据必须恰为 `/login` 系列：`startsWith('/login')` 偏宽，`/login2` 也会被剥掉 `redirect`。
  it('`/login2` 不是登录页 → redirect 保留（判据恰为 /login、/login?、/login/）', async () => {
    const { router, onUnauthorized } = makeRouter()
    await router.push('/login2')
    const session = useSessionStore()
    session.status = 'ready'

    onUnauthorized()
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/login2')
  })

  // brief 之外新增（依据 spec §5.4「`redirect` 不得是 `/login` 的 fullPath」）：上面那条在 `/login` 本身上跑，会被 `current.name === 'login'` 的提前返回短路——`target.startsWith('/login')` 这个分支删掉它照样绿。
  // 真实可达路径是 `/login/xxx`（命中 `not-found`，name 不是 login），所以这里用一条 `/login/help` 路由把它单独钉住。
  it('/login 前缀路径（非 login 路由）上 401 → 落 /login 且不带 redirect', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/login', name: 'login', component: Blank, meta: { requiresAuth: false, layout: 'blank' } },
        { path: '/login/help', name: 'login-help', component: Blank, meta: { requiresAuth: false } },
      ],
    })
    const onUnauthorized = createUnauthorizedHandler(router)
    await router.push('/login/help')
    const session = useSessionStore()
    session.status = 'ready'

    onUnauthorized()
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBeUndefined()
  })
})
