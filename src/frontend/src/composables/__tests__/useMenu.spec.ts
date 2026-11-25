import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MenuGroup, MenuItem } from '@/config/menu'

// ⚠️ 只有最后那组「三份实现等价性」用例需要登录态：`session.canNode` 读的是
// `/api/auth/me` 落进 store 的载荷。桩打在被查找的 `@/api/auth` 上（E-0029）。
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, type User } from '@/api/auth'
import { viewerCanNode } from '@/layui-island/pages/system-roleauthority'
import { useSessionStore } from '@/stores/session'
import { canSeeGroup, canSeeItem, isClickable, visibleGroups, visibleItems } from '../useMenu'

const me = vi.mocked(fetchMe)

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
})

const hasRoute = (name: string) => name === 'home'

describe('菜单渲染规则', () => {
  it('隐藏的分组不渲染', () => {
    const groups: MenuGroup[] = [
      { name: 'A', items: [{ label: 'a' }] },
      { name: 'B', hidden: true, items: [{ label: 'b' }] },
    ]
    expect(visibleGroups(groups, hasRoute).map((g) => g.name)).toEqual(['A'])
  })

  it('隐藏的单条不渲染', () => {
    const items: MenuItem[] = [{ label: 'x' }, { label: 'y', hidden: true }]
    expect(visibleItems(items, hasRoute).map((i) => i.label)).toEqual(['x'])
  })

  it('有 routeName 且路由存在 → 可点', () => {
    expect(isClickable({ label: '首页', routeName: 'home' }, hasRoute)).toBe(true)
  })

  it('没有 routeName → 不可点（置灰 + 即将开放）', () => {
    expect(isClickable({ label: '未做', soon: true }, hasRoute)).toBe(false)
  })

  it('routeName 指向不存在的路由 → 不可点，避免死链', () => {
    expect(isClickable({ label: '幽灵', routeName: 'nope' }, hasRoute)).toBe(false)
  })
})

const admin = {
  isTenantAdmin: true,
  isAccountAdmin: true,
  nodePermissions: new Set<string>(),
}
const accountAdmin = {
  isTenantAdmin: false,
  isAccountAdmin: true,
  nodePermissions: new Set<string>(),
}
const kefu = {
  isTenantAdmin: false,
  isAccountAdmin: false,
  nodePermissions: new Set(['order.list']),
}
const outsider = {
  isTenantAdmin: false,
  isAccountAdmin: false,
  nodePermissions: new Set<string>(),
}
const entry = (extra: Partial<MenuItem> = {}): MenuItem => ({ label: '录入订单', ...extra })

describe('canSeeItem（节点 code 判据；spec §5.4）', () => {
  it('is_tenant_admin：条目声明了别的权限也全可见', () => {
    expect(canSeeItem(entry({ permissions: ['system.settings'] }), admin)).toBe(true)
  })

  it('账号管理员同样全可见（部署空窗期 menu_permissions 为 [] 时的兜底）', () => {
    expect(canSeeItem(entry({ permissions: ['system.settings'] }), accountAdmin)).toBe(true)
  })

  it('未声明 permissions → 可见（登录即可见）', () => {
    expect(canSeeItem(entry(), outsider)).toBe(true)
  })

  it('[] ≡ 未声明 → 可见（不是「已声明但为空」）', () => {
    expect(canSeeItem(entry({ permissions: [] }), outsider)).toBe(true)
  })

  it('有交集 → 可见；无交集 → 不可见', () => {
    expect(canSeeItem(entry({ permissions: ['order.list'] }), kefu)).toBe(true)
    expect(canSeeItem(entry({ permissions: ['order.list', 'x'] }), kefu)).toBe(true)
    expect(canSeeItem(entry({ permissions: ['system.settings'] }), kefu)).toBe(false)
  })
})

describe('canSeeGroup（组级判据）', () => {
  it('hidden 组：对谁都不渲染（连账号管理员也不）', () => {
    const g: MenuGroup = { name: '游戏排团', hidden: true, items: [] }
    expect(canSeeGroup(g, admin)).toBe(false)
    expect(canSeeGroup(g, outsider)).toBe(false)
  })

  it('accountAdminOnly 组：非账号管理员不渲染、账号管理员渲染', () => {
    // 【系统人员】的判据。**两个方向都断言**：只测"非管理员看不到"会放过"实现把组全隐藏了"，
    // 只测"管理员看得到"会放过"实现其实没接线"。
    const g: MenuGroup = { name: '系统人员', accountAdminOnly: true, items: [] }
    expect(canSeeGroup(g, kefu)).toBe(false)
    expect(canSeeGroup(g, outsider)).toBe(false)
    expect(canSeeGroup(g, admin)).toBe(true)
  })

  it('普通组：登录即可见（不看 isAccountAdmin）', () => {
    const g: MenuGroup = { name: '订单管理', items: [] }
    expect(canSeeGroup(g, outsider)).toBe(true)
  })
})

/** **同一条节点判据有三份实现**（控制器裁决：不抽公共模块，加一条等价性判据）：`stores/session.ts::canNode` / `composables/useMenu.ts::canSeeItem` /`layui-island/pages/system-roleauthority.ts::viewerCanNode`。三者今天是同口径的，但任一处将来只改一半，就会变成「菜单可见但页面拒绝」的**静默漂移**。这组用例用**同一批 `(viewer, code)` 夹具同时驱动三者**，逐组断言同真同假——把漂移变成会红的判据。（变异验证：把 `canSeeItem` 的短路改成只认 `isTenantAdmin`，这组立刻红。） */
describe('节点判据三份实现逐组一致（canSeeItem / canNode / viewerCanNode）', () => {
  /** iframe 页判据要的是 `/api/auth/me` 的**原始载荷**（snake_case）。 */
  type RawViewer = {
    is_tenant_admin?: boolean
    is_account_admin?: boolean
    menu_permissions?: string[]
  }

  const CODE = 'system.role_authority'

  const 夹具: Array<[string, RawViewer]> = [
    ['is_tenant_admin 且 menu_permissions 空集', { is_tenant_admin: true, is_account_admin: false, menu_permissions: [] }],
    ['is_account_admin 且 menu_permissions 空集', { is_tenant_admin: false, is_account_admin: true, menu_permissions: [] }],
    ['持有该 code', { is_tenant_admin: false, is_account_admin: false, menu_permissions: [CODE] }],
    ['不持有该 code', { is_tenant_admin: false, is_account_admin: false, menu_permissions: ['order.list'] }],
    ['menu_permissions 缺席', { is_tenant_admin: false, is_account_admin: false }],
  ]

  it.each(夹具)('%s：三份实现同真同假', async (_label, raw) => {
    const store = useSessionStore()
    const payload: User = {
      id: 1,
      username: 'u',
      display_name: 'u',
      identity_plane: 'tenant_a',
      is_tenant_admin: raw.is_tenant_admin === true,
      is_account_admin: raw.is_account_admin === true,
      roles: [],
      permissions: [],
      menu_permissions: raw.menu_permissions,
    }
    me.mockResolvedValue(payload)
    await store.load()

    // ① composable 入口（Vue 侧的 viewer 形状）
    const fromMenu = canSeeItem(
      { label: 'x', permissions: [CODE] },
      {
        isTenantAdmin: store.isTenantAdmin,
        isAccountAdmin: store.isAccountAdmin,
        nodePermissions: store.menuPermissions,
      },
    )
    // ② store 入口（路由守卫用）
    const fromStore = store.canNode([CODE])
    // ③ iframe 页入口（原始载荷）
    const fromPage = viewerCanNode(raw, CODE)

    expect(fromStore, 'store 与 composable 必须同口径').toBe(fromMenu)
    expect(fromPage, 'iframe 页与 composable 必须同口径').toBe(fromMenu)
  })
})
