// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 【系统人员】组的可见性（线 D P0-2，2026-10-12：组级门 `accountAdminOnly` → 条目级节点码）。## 为什么单独一个文件、而且要挂一个组件与 `useMenu.openPlatform.spec.ts` / `useMenu.bottom.spec.ts` **同一个理由**：`useMenu()`内部用 `useRouter()`（`router.hasRoute` 是**可点判定**的输入），而 `useRouter()` 在**组件实例上下文之外**读不到注入的 router ⇒ 必须挂一个最小组件、用 `global.plugins` 装上pinia + router。既有 `useMenu.spec.ts` 全是纯函数、没装路由。## 为什么必须是**真** `config/menu.ts`要钉的正是「**真声明**对每一种人各是什么结果」。合成夹具在这里会变成自证（E-0024 族）：拿一个自己造出来的组去断言「组级门去掉了」，改坏真声明也不会红。## 钉的是三件事（业务方口径：人事要能进人员管理，收益与门槛同时成立）
 * 1. **持 `system_user.list` + `add_edit` 的非管理员（人事）** ⇒ 组**在**、且只有他持节点的那两条（`人员管理` / `新开系统账号`）——「能看到」与「看不到别的」两个方向都钉；2. **一条 `system_user.*` 节点都没有的会话** ⇒ `groups` 里**没有**这一组（**空组不渲染**，fail-closed：门没开没）；3. **管理员（tenant / account 两种）** ⇒ 6 条全在（管理员短路，不是靠节点）。⚠️ 真实的内置「人事」职位持 **6 条** `system_user.*` 非 flag 点（`rbac.seed` 的`BUILTIN_ROLE_MODULES["人事"]`）⇒ 本文件另有一条**照它**的用例：6 条全在。那条与第 1 条**不是重复**：第 1 条钉「按节点逐条过滤」，那条钉「真职位的真实结果」。 */
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, type User } from '@/api/auth'
import { MENU_GROUPS } from '@/config/menu'
import { useSessionStore } from '@/stores/session'
import { useMenu } from '../useMenu'

const me = vi.mocked(fetchMe)

const Blank = { template: '<div />' }

/** 真路由表的一小片（本组 6 条 + 首页；这里只关心 `hasRoute` 的结果）。 */
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/system-users', name: 'system-users', component: Blank },
      { path: '/system-users/new', name: 'system-user-new', component: Blank },
      { path: '/system-users/login-restrict', name: 'system-login-restrict', component: Blank },
      { path: '/system-users/blacklist', name: 'system-blacklist', component: Blank },
      { path: '/system-users/real-name', name: 'system-real-name', component: Blank },
      { path: '/system-users/contract', name: 'system-contract', component: Blank },
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

/** 一份 `/api/auth/me` 载荷（**合成值**；PII 守卫要求 `src/**` 里不留真值）。 */
function payload(over: Partial<User> = {}): User {
  return {
    id: 7,
    username: 'synth-hr',
    display_name: '合成人事甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: [],
    ...over,
  }
}

async function mountMenu(over: Partial<User> = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  me.mockResolvedValue(payload(over))
  const store = useSessionStore()
  await store.load()
  const router = makeRouter()
  const wrapper = mount(Probe, { global: { plugins: [pinia, router] } })
  return wrapper
}

/** 该 viewer 看到的【系统人员】组（看不到 ⇒ `undefined`）。 */
function systemUsers(wrapper: { vm: { groups: Array<{ name: string; items: Array<{ label: string }> }> } }) {
  return wrapper.vm.groups.find((g) => g.name === '系统人员')
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
})

describe('【系统人员】组的可见性（线 D P0-2：组级门去掉，下移到条目级节点码）', () => {
  it('声明层：本组**没有**组级 `accountAdminOnly`（否则人事连这一组都看不见）', () => {
    const group = MENU_GROUPS.find((g) => g.name === '系统人员')
    expect(group, '菜单里没有「系统人员」分组').toBeDefined()
    expect(group!.accountAdminOnly ?? false).toBe(false)
    expect(group!.items.map((i) => i.label)).toEqual([
      '新开系统账号',
 // 组内含「自助注册」（spec §6；与「新开系统账号」同组同节点）。
      '自助注册',
      '人员管理',
      '登录限制设置',
      '人员黑名单',
      '人员实名记录与配置',
      '员工电子合同管理',
    ])
  })

  it('⭐ 判据 1：持 `system_user.list` + `add_edit` 的**非管理员** ⇒ 组在，且**只有**他持节点的那三条', async () => {
    const wrapper = await mountMenu({ menu_permissions: ['system_user.list', 'system_user.add_edit'] })
    const group = systemUsers(wrapper)
    expect(group, '人事看不到【系统人员】组 —— 业务目标（能开子账户）等于没交付').toBeDefined()
    expect(group!.items.map((i) => i.label)).toEqual(['新开系统账号', '自助注册', '人员管理'])
 // 反向：他没有节点的条目**不许**出现（否则就是「组级门去掉」顺手把整组都放开了）。
    expect(group!.items.map((i) => i.label)).not.toContain('人员黑名单')
    expect(group!.items.map((i) => i.label)).not.toContain('人员实名记录与配置')
  })

  it('⭐ 判据 1b：**真实**的内置「人事」节点集（`system_user` 栏目行 + 14 个非 flag 子节点）⇒ 7 条全在', async () => {
 // 这 15 个 code 就是 `rbac.seed.BUILTIN_ROLE_MODULES["人事"]` 展开 `system_user` 栏目时的
 // 全部非 flag 点（14 个子节点 + 栏目行自己）——⛔ 不是本用例编的，是目录实测的那一批。
    const wrapper = await mountMenu({
      menu_permissions: [
        'system_user',
        'system_user.list',
        'system_user.add_edit',
        'system_user.change_status',
        'system_user.reset_password',
        'system_user.batch_set_game_permissions',
        'system_user.login_restriction',
        'system_user.add_edit_login_restriction',
        'system_user.blacklist',
        'system_user.add_edit_blacklist',
        'system_user.delete_blacklist',
        'system_user.view_order_stats',
        'system_user.realname_records',
        'system_user.reset_realname',
        'system_user.contract_management',
      ],
    })
    expect(systemUsers(wrapper)!.items.map((i) => i.label)).toEqual([
      '新开系统账号',
 // +1（同节点 `system_user.add_edit` ⇒ 人事天然看得见）。
      '自助注册',
      '人员管理',
      '登录限制设置',
      '人员黑名单',
      '人员实名记录与配置',
      '员工电子合同管理',
    ])
  })

  it('⭐ 判据 2（**空组不渲染**）：没有任何 `system_user.*` 节点的会话 ⇒ `groups` 里**没有**这一组', async () => {
    const wrapper = await mountMenu({ menu_permissions: ['order.list'] })
 // 阳性对照：别的组还在（否则"这一组不在"可能是整个菜单都没渲染而恒真）。
    expect(wrapper.vm.groups.map((g) => g.name)).toContain('订单管理')
    expect(systemUsers(wrapper), '一条都看不见时不该画出组头（空组）').toBeUndefined()
 // 反向自证：「整组为空」是**条目级节点门**造成的，而不是这个组被 `hidden` 了
 //（后者会让判据 1 / 3 变成同义反复）。
    expect(MENU_GROUPS.find((g) => g.name === '系统人员')!.hidden ?? false).toBe(false)
  })

  it('⭐ 判据 2b：**零节点**会话同样看不到（fail-closed 的最紧一档）', async () => {
    const wrapper = await mountMenu({ menu_permissions: [] })
    expect(systemUsers(wrapper)).toBeUndefined()
  })

  it('⭐ 判据 3：管理员（tenant / account 两种）⇒ 7 条全在（管理员短路，不靠节点）', async () => {
    const asTenantAdmin = await mountMenu({ is_tenant_admin: true, menu_permissions: [] })
    expect(systemUsers(asTenantAdmin)!.items.map((i) => i.label)).toHaveLength(7)
    const asAccountAdmin = await mountMenu({ is_account_admin: true, menu_permissions: [] })
    expect(systemUsers(asAccountAdmin)!.items.map((i) => i.label)).toHaveLength(7)
  })
})
