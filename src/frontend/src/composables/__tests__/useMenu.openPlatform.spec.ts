// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 【开放平台】组的**三条可见性判据** + **空组不渲染**（）。spec：`docs/specs/2026-10-08-taker-open-api-design.md` §7.3 与 §9-12（第 12 条判据）。## 为什么单独一个文件、而且要挂一个组件与 `useMenu.bottom.spec.ts` **同一个理由**：`useMenu()` 内部用 `useRouter()`（`router.hasRoute` 是**可点判定**的输入），而 `useRouter()` 在**组件实例上下文之外**读不到注入的 router ⇒ 必须挂一个最小组件、用 `global.plugins` 装上 pinia + router。既有 `useMenu.spec.ts` 全是纯函数、没装路由，所以这一组单开一处。## ⭐ 本文件钉的是一件**有意的可见性变更**（不是回归）【开放平台】组原先带组级门 `accountAdminOnly: true`；T4 为让**打手**看得见新加的
 * 「打手对接」而**去掉了它**，可见性下移到条目级。红队 2026-10-08 更正了 spec 早先那句「实际可见性不变」：`canSeeItem` 对 **`isTenantAdmin` 短路放行** ⇒**租户管理员（不是账号管理员的那种）会额外看到「性能分析」与「应用管理」**。而本仓此前**没有任何一条网**钉这件事（所以它本来是静默的）⇒ 这里把它**钉死**：1. **没有任何相关节点的普通账号** ⇒ `groups` 里**没有**「开放平台」（**空组不渲染**）；2. **持新节点的打手**（非管理员） ⇒ 组里有**且只有**「打手对接」；3. **租户管理员** ⇒ 三条都在（**有意变更**，spec §7.3 / §9-12）。⚠️ 本文件用**真** `config/menu.ts`（不 mock）：要钉的正是"真声明对每一种人各是什么结果"。合成夹具在这里会变成自证（`E-0024` 族）。 */
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, type User } from '@/api/auth'
import { MENU_GROUPS } from '@/config/menu'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import { useSessionStore } from '@/stores/session'
import { useMenu } from '../useMenu'

const me = vi.mocked(fetchMe)

const Blank = { template: '<div />' }

/** 真路由表的一小片：本组三条由 `router/index.ts` 提供（这里只关心 `hasRoute` 的结果）。⚠️ `system-perfanalysis` / `open-apps` 的**真实路由**还带 `requiresAccountAdmin: true`——那条轴是**路由守卫**的事（`guards.ts`），**不是**菜单可见性的事（`useMenu` 只读 `permissions` + 管理员短路）⇒ 这里不造那条 meta，否则会误以为菜单侧也在判它。它由 `router/__tests__/route-declarations.spec.ts` 的 (a)/(d) 钉住。 */
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/system/performance', name: 'system-perfanalysis', component: Blank },
      { path: '/open-platform/apps', name: 'open-apps', component: Blank },
      { path: '/open-platform/taker-apps', name: 'taker-apps', component: Blank },
    ],
  })
}

/** 把 `useMenu()` 的出口挂在一个最小组件上（`useRouter()` 需要有实例上下文）。 */
const Probe = defineComponent({
  setup() {
    return useMenu()
  },
  template: '<div />',
})

/** 一份 `/api/auth/me` 载荷（**合成值**，PII 守卫要求 `src/**` 里不留真值）。
 * ⚠️ 两个管理员判据默认都是 `false` 是**承重**的：判据 1 / 2 要证明"打手靠**节点**看到这一组"，
 * 而不是靠管理员短路。 */
function payload(over: Partial<User> = {}): User {
  return {
    id: 7,
    username: 'taker-a',
    display_name: '合成打手甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: [],
    ...over,
  }
}

/** 装 pinia + 路由、把会话置成给定载荷，返回挂好的探针。 */
async function mountMenu(over: Partial<User> = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  me.mockResolvedValue(payload(over))
  const store = useSessionStore()
  await store.load()
  const router = makeRouter()
  const wrapper = mount(Probe, { global: { plugins: [pinia, router] } })
  return { wrapper, router }
}

/** 该 viewer 看到的【开放平台】组（看不到 ⇒ `undefined`）。 */
function openPlatform(wrapper: { vm: { groups: Array<{ name: string; items: Array<{ label: string }> }> } }) {
  return wrapper.vm.groups.find((g) => g.name === '开放平台')
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
})

describe('【开放平台】组的可见性（T4：组级门已去掉，可见性下移到条目级）', () => {
  it('声明层：本组**没有**组级 `accountAdminOnly`（否则打手连这一组都看不见）', async () => {
    const group = MENU_GROUPS.find((g) => g.name === '开放平台')
    expect(group, '菜单里没有「开放平台」分组').toBeDefined()
    expect(group!.accountAdminOnly ?? false).toBe(false)
    expect(group!.items.map((i) => i.label)).toEqual(['性能分析', '应用管理', '打手对接'])
  })

  it('⭐ 判据 1（**空组不渲染**）：没有任何相关节点的普通账号 ⇒ `groups` 里**没有**「开放平台」', async () => {
    const { wrapper } = await mountMenu({ menu_permissions: ['order.list'] })
    // 阳性对照：别的组还在（否则"这一组不在"可能是整个菜单都没渲染而恒真）。
    expect(wrapper.vm.groups.map((g) => g.name)).toContain('订单管理')
    expect(openPlatform(wrapper), '本组一条都看不见时不该画出组头（空组）').toBeUndefined()
    // ⚠️ 反向自证：**整组为空**这件事是由「组内三条全被条目级门挡掉」造成的，
    //    而不是"这个组被 `hidden` 了"（那会让判据 2 / 3 变成同义反复）。
    expect(MENU_GROUPS.find((g) => g.name === '开放平台')!.hidden ?? false).toBe(false)
  })

  it('⭐ 判据 2：持新节点的**打手**（非管理员） ⇒ 组里**只有**「打手对接」', async () => {
    const { wrapper } = await mountMenu({
      menu_permissions: ['system.allow_taker_open_api'],
    })
    const group = openPlatform(wrapper)
    expect(group, '打手看不到【开放平台】组 ⇒ 新加的页面等于没加').toBeDefined()
    expect(group!.items.map((i) => i.label)).toEqual(['打手对接'])
    // ⚠️ 阳性对照：这一条**不是**靠管理员短路成立的（两个管理员判据都是 false，见 `payload`），
    //    也不是"整个菜单只剩这一组"（顶部直挂条目照旧可见）。
    expect(wrapper.vm.topLevel.map((i) => i.label)).toContain('后台首页')
  })

  it('⭐ 判据 2 的反例：只有别的节点 ⇒ 组里的「打手对接」**不出现**（不会"看得到点不开"）', async () => {
    const { wrapper } = await mountMenu({ menu_permissions: ['system.logs'] })
    const group = openPlatform(wrapper)
    expect(group, '持 `system.logs` 的人应当看得到「性能分析」⇒ 组还在').toBeDefined()
    expect(group!.items.map((i) => i.label)).toEqual(['性能分析'])
  })

  it('⭐ 判据 3（**有意变更**）：**租户管理员**（非账号管理员）⇒ 三条都在', async () => {
    // ⛔⭐ 这是 spec §7.3 那一次**有意的可见性变更**：`canSeeItem` 对 `isTenantAdmin` 短路放行，
    //    所以去掉组级门之后，租户管理员额外看到了「性能分析」与「应用管理」。
    //    本仓此前**没有任何一条网**钉它 ⇒ 本用例是那条裁定唯一的落点。
    const { wrapper } = await mountMenu({ is_tenant_admin: true, menu_permissions: [] })
    const group = openPlatform(wrapper)
    expect(group, '租户管理员必须看得到这一组（有意变更）').toBeDefined()
    expect(group!.items.map((i) => i.label)).toEqual(['性能分析', '应用管理', '打手对接'])
    // ⚠️ 与判据 1 的差别**只**来自那两条管理员短路 ⇒ 两条合起来才构成"有意变更"的判据。
    expect(wrapper.vm.groups.filter((g) => g.items.length === 0)).toEqual([])
  })

  it('⭐ 判据 3 的第二支：**账号管理员**（`is_account_admin`）同样三条都在', async () => {
    const { wrapper } = await mountMenu({ is_account_admin: true, menu_permissions: [] })
    const group = openPlatform(wrapper)
    expect(group!.items.map((i) => i.label)).toEqual(['性能分析', '应用管理', '打手对接'])
  })

  it('别名判据（防"数字对了但覆盖面没变"）：本组三条各自声明了不同的节点码', async () => {
    // 菜单侧手写、路由侧由 `routePermissions.ts` 供值，两份**独立来源**由
    // `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对；这里只钉"三个点各不相同"
    // （否则"只有打手对接"那条判据可能被一个共用点弄成恒真）。
    const group = MENU_GROUPS.find((g) => g.name === '开放平台')!
    expect(group.items.map((i) => (i.permissions ?? []).length)).toEqual([1, 1, 1])
    expect(new Set(group.items.flatMap((i) => i.permissions ?? [])).size).toBe(3)
    expect(ROUTE_NODE_CODES['taker-apps']).toBe('system.allow_taker_open_api')
  })

  it('空组不渲染的**通用性**：三个 viewer 下都不存在"组头在、里面什么都没有"的分组', async () => {
    for (const over of [
      { menu_permissions: ['order.list'] },
      { menu_permissions: ['system.allow_taker_open_api'] },
      { is_tenant_admin: true, menu_permissions: [] },
    ]) {
      const { wrapper } = await mountMenu(over)
      expect(
        wrapper.vm.groups.filter((g) => g.items.length === 0).map((g) => g.name),
        `viewer=${JSON.stringify(over)} 时出现了空组`,
      ).toEqual([])
    }
  })
})
