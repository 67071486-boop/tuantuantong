// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** `useMenu().bottom`（**侧栏最底部**那一条，2026-10-08 问题反馈线）的出口判据。spec：`docs/specs/2026-10-08-feedback-design.md` §6.1（`useMenu` 出口加 `bottom`、**与 `topLevel` 同一套判据、同一个 `keepItem` 收口**）与 §9 验收判据第 8 条：「`useMenu()` 的 `bottom` **恰好 1 条**（label `问题反馈`、routeName `feedback`）；持 `home.feedback` 的**普通角色**能看到它（判据走 `canSeeItem`，**不是 `isTenantAdmin` 短路**）」。## 为什么单独一个文件、而且要挂一个组件`useMenu()` 内部用 `useRouter()`（`router.hasRoute` 是**可点判定**的输入），而`useRouter()` 在**组件实例上下文之外**读不到注入的 router（实测：直接调会抛
 * `Cannot read properties of undefined (reading 'hasRoute')`）⇒ 必须挂一个最小组件、用 `global.plugins` 装上 pinia + router。既有 `useMenu.spec.ts` 全是纯函数、没装路由，所以这一组单开一处（也便于后来者一眼看到「底部条目是这样接线的」）。⚠️ 本文件用**真** `config/menu.ts`（不 mock）：要钉的正是「真声明里恰好 1 条、且它真的能被普通角色看到」。合成夹具在这里会变成自证（`E-0024` 族）。 */
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, type User } from '@/api/auth'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import { useSessionStore } from '@/stores/session'
import { useMenu } from '../useMenu'

const me = vi.mocked(fetchMe)

const Blank = { template: '<div />' }

/** 真路由表的一小片：`feedback` 那条由 `router/index.ts` 提供（这里只关心 `hasRoute` 的结果）。 */
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/feedback', name: 'feedback', component: Blank },
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
 * ⚠️ `is_tenant_admin: false` 是**承重**的：本文件的判据要证明「普通角色靠**节点**看到底部条目」，
 * 而不是靠管理员短路（spec §9 第 8 条明确要求判据走 `canSeeItem`）。 */
function payload(over: Partial<User> = {}): User {
  return {
    id: 7,
    username: 'kefu-fb-a',
    display_name: '客服甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: [],
    ...over,
  }
}

/** 装 pinia + 路由、把会话置成给定载荷，返回挂好的探针（`vm.bottom` / `vm.topLevel`）。 */
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

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
})

describe('useMenu().bottom（侧栏最底部那一条）', () => {
  it('声明恰好 1 条：label `问题反馈`、routeName `feedback`（spec §9 第 8 条）', async () => {
    const { wrapper } = await mountMenu({ menu_permissions: ['home.feedback'] })
    const bottom = wrapper.vm.bottom
    expect(bottom.length).toBe(1)
    expect(bottom[0]!.label).toBe('问题反馈')
    expect(bottom[0]!.routeName).toBe('feedback')
  })

  it('持 `home.feedback` 的**普通角色**（非管理员）看得到它 —— 判据走 canSeeItem，不是管理员短路', async () => {
    const { wrapper } = await mountMenu({ menu_permissions: ['home.feedback'] })
    expect(wrapper.vm.bottom.map((i) => i.label)).toEqual(['问题反馈'])
 // ⚠️ 上面那条只有在**确认过它不是靠管理员短路**之后才有意义（spec §9 第 8 条的原话）：
 // 这个夹具的两个管理员判据都是 false（见 `payload`），所以它只能靠节点码可见。
    expect(wrapper.vm.bottom[0]!.disabled).toBe(false)
  })

  it('反向：不持该节点的普通角色**看不到**它（整条消失，⛔ 不是置灰冒充）', async () => {
    const { wrapper } = await mountMenu({ menu_permissions: ['order.list'] })
 // 阳性对照：别的条目还在（否则"底部为空"可能是整个菜单都没渲染而恒真）。
    expect(wrapper.vm.topLevel.map((i) => i.label)).toContain('后台首页')
    expect(wrapper.vm.bottom).toEqual([])
  })

  it('管理员（`is_tenant_admin`）看得到它（`canSeeItem` 的短路那一支）', async () => {
    const { wrapper } = await mountMenu({ is_tenant_admin: true, menu_permissions: [] })
    expect(wrapper.vm.bottom.map((i) => i.label)).toEqual(['问题反馈'])
  })

  it('可点判定与 `topLevel` 同一条：路由表里有 `feedback` ⇒ 不置灰', async () => {
    const { wrapper, router } = await mountMenu({ menu_permissions: ['home.feedback'] })
    expect(router.hasRoute('feedback')).toBe(true)
    expect(wrapper.vm.bottom[0]!.disabled).toBe(false)
  })

  it('声明里的节点码与 `routePermissions` 的登记**同值**（同一事实两处，靠这条钉住漂移）', async () => {
 // 菜单侧的 `permissions` 是**手写**的（`router/__tests__/route-declarations.spec.ts` 的 (c)
 // 拿它与路由 `meta` 比对，两份独立来源才让那条承重）；这里再钉一条**取值**：
 // 「问题反馈」这条用的必须是 `home.feedback`（对标 112 号点的精确命中），
 // ⛔ 不是某个复用的点——改错值会让页面打后端时 403，而**菜单侧看不出来**。
    const { wrapper } = await mountMenu({ menu_permissions: ['home.feedback'] })
    expect(wrapper.vm.bottom[0]!.permissions).toEqual(['home.feedback'])
    expect(ROUTE_NODE_CODES['feedback']).toBe('home.feedback')
  })
})
