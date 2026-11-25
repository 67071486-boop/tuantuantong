import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/notify', () => ({ notifyError: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { notifyError } from '@/api/notify'
import { AuthExpiredError } from '@/api/errors'
import { installGuards } from '../guards'

const me = vi.mocked(fetchMe)
const notify = vi.mocked(notifyError)

const Blank = { template: '<div/>' }
function makeRouter() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: Blank, meta: { requiresAuth: false, layout: 'blank' } },
      { path: '/', name: 'home', component: Blank }, // 刻意**不**声明 requiresAuth（钉 fail-closed 默认）
      {
        path: '/secret',
        name: 'secret',
        component: Blank,
        meta: { permissions: ['order.list'] },
      },
      {
        // ⚠️ **合成路由**（本文件里唯一还带这条轴的地方）：【系统人员】那 7 条**已不再**用 `requiresAccountAdmin`（准入改为纯节点码，判据见 `router/__tests__/system-users-access.spec.ts`）。
        // 这条轴今天服务于 3 条**自有功能**路由——它们的准入是 `IsAccountAdmin` 权限类（不是一个 codename），所以声明 `requiresAccountAdmin` 而**不**声明 `meta.permissions` 的那一类。
        path: '/admin-only',
        name: 'admin-only',
        component: Blank,
        meta: { requiresAccountAdmin: true },
      },
      { path: '/:pathMatch(.*)*', name: 'not-found', component: Blank },
    ],
  })
  installGuards(router)
  return router
}

const kefu = {
  id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
  is_tenant_admin: false, roles: [], permissions: [],
  menu_permissions: [] as string[],
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
  notify.mockReset()
})

describe('守卫', () => {
  it('未认证访问业务路由 → /login?redirect=<原 fullPath>', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    const router = makeRouter()
    await router.push('/secret?game=3')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/secret?game=3')
  })

  it('未 ready 时先 load（守卫是 load 的所有者）', async () => {
    me.mockResolvedValue(kefu)
    const router = makeRouter()
    await router.push('/')
    expect(me).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('home') // 未声明 requiresAuth 也要登录，登录后放行
  })

  // brief 之外新增（依据 spec §6.1 第 16 条）：brief 里 `home` 那条用例用的是**已认证**账号，
  // 于是"未声明 requiresAuth 的 home，在 guest 下仍被拦"这条 fail-closed 默认只能靠 `/secret`
  // 间接钉住——把 `?? true` 改成 `?? false` 时它并不直接变红。这条把默认值钉在 `home` 本身。
  it('未认证访问未声明 requiresAuth 的 home → 仍被拦（fail-closed 默认）', async () => {
    me.mockRejectedValue(new AuthExpiredError('未认证'))
    const router = makeRouter()
    await router.push('/')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/')
  })

  it('已认证但权限不足 → 回 home + 就地提示', async () => {
    me.mockResolvedValue(kefu)
    const router = makeRouter()
    await router.push('/')
    await router.push('/secret')
    expect(router.currentRoute.value.name).toBe('home')
    expect(notify).toHaveBeenCalled()
  })

  it('已认证且有权限 → 放行', async () => {
    me.mockResolvedValue({ ...kefu, menu_permissions: ['order.list'] })
    const router = makeRouter()
    await router.push('/secret')
    expect(router.currentRoute.value.name).toBe('secret')
  })

  it('is_tenant_admin 短路：menu_permissions 为空也放行（部署空窗期，spec §5.3）', async () => {
    me.mockResolvedValue({ ...kefu, is_tenant_admin: true, menu_permissions: [] })
    const router = makeRouter()
    await router.push('/')
    await router.push('/secret')
    expect(router.currentRoute.value.name).toBe('secret')
  })

  it('账号管理员同样短路（「两者都不是」那一半由上面「已认证但权限不足」那条覆盖）', async () => {
    me.mockResolvedValue({ ...kefu, is_account_admin: true, menu_permissions: [] })
    const router = makeRouter()
    await router.push('/secret')
    expect(router.currentRoute.value.name).toBe('secret')
  })

  it('requiresAccountAdmin：非账号管理员 → 回 home + 就地提示', async () => {
    // **两个方向都测**（只测拒绝会放过"实现把所有路由都拒了"，只测放行会放过"没接线"）。
    me.mockResolvedValue(kefu) // is_account_admin 缺席 → fail-closed 当作 false
    const router = makeRouter()
    await router.push('/')
    await router.push('/admin-only')
    expect(router.currentRoute.value.name).toBe('home')
    expect(notify).toHaveBeenCalled()
  })

  it('requiresAccountAdmin：账号管理员 → 放行（且不要求任何 codename）', async () => {
    me.mockResolvedValue({ ...kefu, is_account_admin: true, permissions: [] })
    const router = makeRouter()
    await router.push('/admin-only')
    expect(router.currentRoute.value.name).toBe('admin-only')
  })

  it('/login 免认证；已 ready 时访问 /login → 跳 home', async () => {
    me.mockResolvedValue(kefu)
    const router = makeRouter()
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('home')
  })

  it('引导失败的错误只提示一次', async () => {
    me.mockRejectedValue(new Error('网络异常'))
    const router = makeRouter()
    await router.push('/')
    await router.push('/login')
    expect(notify).toHaveBeenCalledTimes(1)
  })
})
