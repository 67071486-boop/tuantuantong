// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import type { User } from '@/api/auth'

const chatwoot = vi.hoisted(() => ({
  mountChatwoot: vi.fn(() => true),
  syncChatwootUser: vi.fn(),
  unmountChatwoot: vi.fn(),
}))

vi.mock('@/chatwootWidget', () => chatwoot)

import App from '@/App.vue'
import { useSessionStore } from '@/stores/session'

const LOGGED_IN: User = {
  id: 42,
  username: 'web-chat-user',
  display_name: '网页端测试账号',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
  chat_identifier: 'web-tenant_a-42',
  chat_identifier_hash: 'a'.repeat(64),
  chat_base_url: 'https://chatwoot-web.example.test',
  chat_website_token: 'web-website-token-synthetic-0001',
}

enableAutoUnmount(afterEach)

const Blank = defineComponent({ render: () => h('div') })
function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/',
        name: 'home',
        component: Blank,
        meta: { title: '后台首页' },
      },
      { path: '/v2', name: 'v2', component: Blank, meta: { layout: 'blank' } },
      { path: '/kefu', name: 'kefu-console', component: Blank, meta: { layout: 'blank' } },
    ],
  })
}

async function mountDesktop(user: User | null, path = '/') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useSessionStore()
  store.user = user
  store.status = user ? 'ready' : 'guest'

  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  mount(App, { global: { plugins: [pinia, router], stubs: { AppShell: true } } })
  await flushPromises()
  return { router, store }
}

beforeEach(() => {
  chatwoot.mountChatwoot.mockClear()
  chatwoot.mountChatwoot.mockReturnValue(true)
  chatwoot.syncChatwootUser.mockClear()
  chatwoot.unmountChatwoot.mockClear()
  document.body.classList.remove('shell-flush-content')
})

describe('桌面入口的浏览器标签和 Chatwoot 接线', () => {
  it('有工作室名时旧、新后台都显示它，缺少工作室名回落默认标题', async () => {
    const tenant = { ...LOGGED_IN, tenant_name: '示例工作室A' }
    const { router, store } = await mountDesktop(tenant)
    expect(document.title).toBe('团团通 - 示例工作室A')
    await router.push('/v2')
    await flushPromises()
    expect(document.title).toBe('团团通 - 示例工作室A')
    store.user = LOGGED_IN
    await flushPromises()
    expect(document.title).toBe('团团通 · 租户后台')
  })

  it('默认新版挂载身份，切到旧壳不断线，进入独立客服工作台时隐藏', async () => {
    const { router } = await mountDesktop(LOGGED_IN, '/v2')
    expect(chatwoot.mountChatwoot).toHaveBeenCalledExactlyOnceWith(LOGGED_IN)
    expect(chatwoot.syncChatwootUser).toHaveBeenCalledExactlyOnceWith(LOGGED_IN)
    await router.push('/')
    await flushPromises()
    expect(chatwoot.mountChatwoot).toHaveBeenCalledTimes(1)
    expect(chatwoot.unmountChatwoot).not.toHaveBeenCalled()
    await router.push('/kefu')
    await flushPromises()
    expect(chatwoot.unmountChatwoot).toHaveBeenCalledTimes(1)
  })

  it('新版后台启用内部沟通时停用 Chatwoot，并在返回旧后台时恢复', async () => {
    const enabled = { ...LOGGED_IN, internal_chat_enabled: true }
    const { router } = await mountDesktop(enabled, '/v2')
    expect(chatwoot.mountChatwoot).not.toHaveBeenCalled()
    expect(chatwoot.syncChatwootUser).not.toHaveBeenCalled()

    await router.push('/')
    await flushPromises()
    expect(chatwoot.mountChatwoot).toHaveBeenCalledExactlyOnceWith(enabled)

    await router.push('/v2')
    await flushPromises()
    expect(chatwoot.mountChatwoot).toHaveBeenCalledTimes(1)
    expect(chatwoot.unmountChatwoot).toHaveBeenCalledTimes(2)
  })

  it('直接进入独立客服工作台不会挂载气泡', async () => {
    await mountDesktop(LOGGED_IN, '/kefu')
    expect(chatwoot.mountChatwoot).not.toHaveBeenCalled()
  })
  it('后端未配置（没有 chat_identifier）⇒ 不挂载气泡，只隐藏清零', async () => {
    // 后端未配置时 `/api/auth/{me,login}` **两个字段都不下发**（计划 §2.3）——用 delete 造
    // 那个形状，而不是留一个只有一半字段的假体。
    const withoutChat: User = { ...LOGGED_IN }
    delete withoutChat.chat_identifier
    delete withoutChat.chat_identifier_hash
    delete withoutChat.chat_base_url
    delete withoutChat.chat_website_token
    await mountDesktop(withoutChat)

    expect(chatwoot.mountChatwoot).not.toHaveBeenCalled()
    expect(chatwoot.syncChatwootUser).not.toHaveBeenCalled()
    expect(chatwoot.unmountChatwoot).toHaveBeenCalled()
  })

  it('已登录且有两个字段 ⇒ mountChatwoot() 后立刻 syncChatwootUser(user)', async () => {
    await mountDesktop(LOGGED_IN)

    expect(chatwoot.mountChatwoot).toHaveBeenCalledTimes(1)
    // 四个公开配置都在 user 上 ⇒ 整个 user 交给模块，模块自己 fail-closed。
    expect(chatwoot.mountChatwoot).toHaveBeenCalledWith(LOGGED_IN)
    expect(chatwoot.syncChatwootUser).toHaveBeenCalledWith(LOGGED_IN)
    const mountOrder = chatwoot.mountChatwoot.mock.invocationCallOrder[0]
    const syncOrder = chatwoot.syncChatwootUser.mock.invocationCallOrder[0]
    if (mountOrder === undefined || syncOrder === undefined) {
      throw new Error('预期 mountChatwoot 与 syncChatwootUser 都被调用过')
    }
    expect(syncOrder).toBeGreaterThan(mountOrder)
  })

  it('后端没下发公开配置（mountChatwoot 返回 false）⇒ 不 syncChatwootUser（不退化匿名）', async () => {
    chatwoot.mountChatwoot.mockReturnValue(false)
    await mountDesktop(LOGGED_IN)

    expect(chatwoot.mountChatwoot).toHaveBeenCalledTimes(1)
    expect(chatwoot.syncChatwootUser).not.toHaveBeenCalled()
  })

  it('用户变空（登出 / 401）⇒ unmountChatwoot()', async () => {
    const { store } = await mountDesktop(LOGGED_IN)
    chatwoot.unmountChatwoot.mockClear()

    store.user = null
    await flushPromises()

    expect(chatwoot.unmountChatwoot).toHaveBeenCalled()
  })
})
