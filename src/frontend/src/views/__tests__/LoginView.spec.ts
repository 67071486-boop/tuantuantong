// @vitest-environment jsdom
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/notify', () => ({ notifyError: vi.fn() }))
// 公开的工作室名端点（`GET /api/auth/site`，spec §5.5）只桩**传输层**：`api/site.ts` 自己的
// 单飞 / 成功缓存 / 失败回落都真的跑一遍（与 `api/__tests__/auth.spec.ts` 同一层，不桩 axios）。
vi.mock('@/api/http', () => ({ request: vi.fn() }))

const replace = vi.fn()
/** `router.resolve()` 的桩：显式标出 `name?`，否则 `mockReturnValueOnce({ name: 'not-found', … })` 过不了 `vue-tsc`。 */
type ResolveResult = { matched: { path: string }[]; name?: string }
const resolve = vi.fn((): ResolveResult => ({ matched: [{ path: '/x' }] }))
/** 可变：每个用例自己设 `redirect`。**不要**用 `vi.doMock` 去换 mock（它与模块缓存打架）。 */
const routeQuery: { redirect?: unknown } = { redirect: '/orders/new?game=3' }
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace, resolve }),
  useRoute: () => ({ query: routeQuery }),
}))

import { login } from '@/api/auth'
import { request } from '@/api/http'
import LoginView from '../LoginView.vue'

const apiLogin = vi.mocked(login)
const siteRequest = vi.mocked(request)

beforeEach(() => {
  setActivePinia(createPinia())
  apiLogin.mockReset()
  // 站点名端点的**默认**行为是「取不到」：登录页的核心任务是登录，名字拿不到必须不拦任何交互，下面每条既有用例都在这个前提下跑。⚠️ 默认必须是一个**被拒的 promise**，不能是别的：
  //  · `mockReset()` 后不管（返回 `undefined`）⇒ `fetchSiteName()` 里 `undefined.then` **同步抛**，变成 mounted 钩子的未处理错误（实测：exit 1 + 5 条 Unhandled Rejection）；
  //  · 默认 resolve 空名 ⇒ `''` 会被**成功缓存**住（spec §7.3），串味到后面真要取名字的用例。要测「拿得到名字」的用例自己 `mockResolvedValueOnce`。
  siteRequest.mockReset()
  siteRequest.mockRejectedValue(new Error('本用例未桩工作室名'))
  replace.mockReset()
  resolve.mockClear()
  routeQuery.redirect = '/orders/new?game=3' // 每个用例回到站内默认值
})

async function fill(wrapper: ReturnType<typeof mount>, user = 'kefu', pw = 'pw') {
  await wrapper.find('input[name="username"]').setValue(user)
  await wrapper.find('input[name="password"]').setValue(pw)
}

describe('登录页', () => {
  it('提交成功后按 redirect 回跳', async () => {
    apiLogin.mockResolvedValue({
      id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
    })
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await fill(wrapper)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(apiLogin).toHaveBeenCalledWith({ username: 'kefu', password: 'pw' })
    expect(replace).toHaveBeenCalledWith('/orders/new?game=3')
  })

  it('失败时只显示服务端 msg，且不跳转', async () => {
    apiLogin.mockRejectedValue(new Error('用户名或密码错误'))
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await fill(wrapper)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    // 登录页改版后错误节点从 `el-alert` 换成自有节点（`.login-error`）：断言**语义不变**
    // ——仍然只断言"显示的就是服务端那句 msg"，且它必须是 `role=alert`（读屏可感知）。
    expect(wrapper.find('.login-error').text()).toBe('用户名或密码错误')
    expect(wrapper.find('.login-error').attributes('role')).toBe('alert')
    expect(replace).not.toHaveBeenCalled()
  })

  it('用户名框回车也提交（原生 form + type=submit，另有 keyup.enter 兜底）', async () => {
    apiLogin.mockResolvedValue({
      id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
    })
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await fill(wrapper)
    await wrapper.find('input[name="username"]').trigger('keyup.enter')
    await flushPromises()
    expect(apiLogin).toHaveBeenCalledWith({ username: 'kefu', password: 'pw' })
  })

  it('redirect 解析到 not-found 兜底页时也回 home（catch-all 让 matched 恒非空）', async () => {
    routeQuery.redirect = '/nope'
    resolve.mockReturnValueOnce({ name: 'not-found', matched: [{ path: '/:pathMatch(.*)*' }] })
    apiLogin.mockResolvedValue({
      id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
    })
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await fill(wrapper)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'home' })
  })

  it('站外 redirect 被忽略，回 home', async () => {
    routeQuery.redirect = 'https://evil.example'
    apiLogin.mockResolvedValue({
      id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
    })
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await fill(wrapper)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'home' })
  })

  /**
   * ⚠️ 下面两条的顺序**有意义**，别调换：`fetchSiteName()` 是**模块级单飞 + 成功缓存**（spec §7.3），一旦成功过，后续调用只会拿到缓存值 ⇒「失败回落」这条必须排在「成功」那条**之前**，否则它测到的是缓存而不是回落（已实测：调换后它断言到缓存值）。两条各自单独跑（`vitest run -t '<名字>'`）都成立。
   */
  it('拿不到工作室名时回落「团团通」（不弹错、不拦登录），且**不动**浏览器标题', async () => {
    siteRequest.mockRejectedValueOnce(new Error('boom'))
    // 哨兵：先写一个已知值，再挂载。判据是「**没被动过**」（spec §7.3：失败 ⇒ 保留
    // `h1 = '团团通'`、`document.title` 不动），**不是**「被写成了某个特定值」。
    document.title = '哨兵标题'
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    expect(wrapper.get('.login-title').text()).toBe('团团通')
    expect(document.title).toBe('哨兵标题')
  })

  it('拿到工作室名时显示在主标题上，并写进浏览器标题', async () => {
    siteRequest.mockResolvedValueOnce({ tenant_name: '示例工作室甲' } as never)
    const wrapper = mount(LoginView, { global: { plugins: [createPinia()] } })
    await flushPromises()
    expect(wrapper.get('.login-title').text()).toBe('示例工作室甲')
    expect(document.title).toBe('团团通 - 示例工作室甲')
  })
})
