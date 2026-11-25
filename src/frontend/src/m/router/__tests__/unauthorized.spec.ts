// @vitest-environment jsdom
// `flushPromises` 来自 @vue/test-utils（在 jsdom 环境下稳定排空导航的微任务链）。
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/http', () => ({ setUnauthorizedHandler: vi.fn() }))

import { setUnauthorizedHandler } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import { createMUnauthorizedHandler, installMUnauthorizedHandler } from '../unauthorized'

const setHandler = vi.mocked(setUnauthorizedHandler)

/**
 * m 端 401 处理器（形状照桌面 `router/__tests__/unauthorized.spec.ts` 的可测口径）。
 * 与桌面的两处差异都钉住：目标是 `m-login`；`redirect` 存**站内 /m/ 路径**。
 */

const Blank = { template: '<div />' }

function makeMRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'm-login', component: Blank },
      { path: '/hall', name: 'm-hall', component: Blank },
      { path: '/mine', name: 'm-mine', component: Blank },
    ],
  })
}

const booster = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
}

beforeEach(() => {
  setActivePinia(createPinia())
  setHandler.mockReset()
})

describe('m 端 401 处理器', () => {
  it('业务路由上 401 → 清会话 + 落 m-login，redirect 存站内 /m/ 路径', async () => {
    const router = makeMRouter()
    await router.push('/hall?game=3')
    const session = useSessionStore()
    session.user = booster
    session.status = 'ready'

    const handler = createMUnauthorizedHandler(router)
    handler()
    await flushPromises()

    expect(session.status).toBe('guest')
    expect(session.user).toBeNull()
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query.redirect).toBe('/m/hall?game=3')
  })

  it('已在 m-login 上 → 清会话但不再导航（防死循环）', async () => {
    const router = makeMRouter()
    await router.push('/login')
    const session = useSessionStore()
    session.status = 'ready'
    const replaceSpy = vi.spyOn(router, 'replace')

    const handler = createMUnauthorizedHandler(router)
    handler()
    await flushPromises()

    expect(session.status).toBe('guest')
    expect(replaceSpy).not.toHaveBeenCalled()
    expect(router.currentRoute.value.query.redirect).toBeUndefined()
  })

  it('并发 401 只导航一次', async () => {
    const router = makeMRouter()
    await router.push('/mine')
    const replaceSpy = vi.spyOn(router, 'replace')

    const handler = createMUnauthorizedHandler(router)
    handler()
    handler()
    handler()
    await flushPromises()

    expect(replaceSpy).toHaveBeenCalledTimes(1)
  })

  it('复位：第二次 401（跨一次已完成的导航）仍能导航', async () => {
    const router = makeMRouter()
    await router.push('/hall')
    const replaceSpy = vi.spyOn(router, 'replace')

    const handler = createMUnauthorizedHandler(router)
    handler()
    await flushPromises() // 让 replace 落地 → .finally 复位
    await router.push('/mine')
    handler()
    await flushPromises()

    expect(replaceSpy).toHaveBeenCalledTimes(2)
  })

  it('装配：installMUnauthorizedHandler 把真处理器接到 api/http 的单例上（恰一次）', async () => {
    const router = makeMRouter()
    await router.push('/mine')
    const session = useSessionStore()
    session.status = 'ready'

    installMUnauthorizedHandler(router)

    expect(setHandler).toHaveBeenCalledTimes(1)
    const wired = setHandler.mock.calls[0]?.[0]
    expect(typeof wired).toBe('function')

    wired?.()
    await flushPromises()

    expect(session.status).toBe('guest')
    expect(router.currentRoute.value.name).toBe('m-login')
    expect(router.currentRoute.value.query.redirect).toBe('/m/mine')
  })
})
