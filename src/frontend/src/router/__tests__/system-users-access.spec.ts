// @vitest-environment jsdom
// 上面这行**必需**：本文件 `import realRouter from '@/router'`，而 `router/index.ts` 用的是
// `createWebHistory(import.meta.env.BASE_URL)`（内部会解构 `window`）——node 环境下 import 即抛。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 通过生产路由和守卫验证系统人员入口。六个旧菜单地址转入新版，旧独立账号编辑仍由原路由承接。
 * 节点及落点都使用字面量，防止路由与预期一起漂移。页面组件只作替身。
 */
vi.mock('@/v2/CarbonShell.vue', () => ({ default: { __view: 'CarbonShell' } }))
vi.mock('@/v2/V2Dashboard.vue', () => ({ default: { __view: 'V2Dashboard' } }))
vi.mock('@/v2/V2UserList.vue', () => ({ default: { __view: 'V2UserList' } }))
vi.mock('@/v2/V2UserNew.vue', () => ({ default: { __view: 'V2UserNew' } }))
vi.mock('@/v2/V2LoginRestrict.vue', () => ({ default: { __view: 'V2LoginRestrict' } }))
vi.mock('@/v2/V2Blacklist.vue', () => ({ default: { __view: 'V2Blacklist' } }))
vi.mock('@/v2/V2RealName.vue', () => ({ default: { __view: 'V2RealName' } }))
vi.mock('@/v2/V2Contract.vue', () => ({ default: { __view: 'V2Contract' } }))
vi.mock('@/views/NotFoundView.vue', () => ({ default: { __view: 'NotFoundView' } }))
vi.mock('@/components/LayuiIslandHost.vue', () => ({ default: { __view: 'LayuiIslandHost' } }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/notify', () => ({ notifyError: vi.fn() }))

import { fetchMe, type User } from '@/api/auth'
import { notifyError } from '@/api/notify'
import realRouter from '@/router'

const me = vi.mocked(fetchMe)
const notify = vi.mocked(notifyError)

/** 一份 `/api/auth/me` 载荷（**合成值**；PII 守卫要求 `src/**` 里不留真值）。 */
function payload(over: Partial<User> = {}): User {
  return {
    id: 7,
    username: 'synth-user',
    display_name: '合成账号甲',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: [],
    ...over,
  }
}

/** 一条**不受本组门约束**的中性落点：未知路径 ⇒ `not-found`（只需登录，不声明节点码）。 */
const NEUTRAL = '/definitely-no-such-page-9f2c'

/** 从「中性落点」出发推一次目标（避免 duplicated navigation 让守卫不跑）。 */
async function goTo(target: string) {
  await realRouter.push(NEUTRAL)
  await realRouter.push(target)
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
  notify.mockReset()
})

const MATRIX: Array<[string, string, string[], string]> = [
  ['system-users', '/system-users', ['system_user.list'], 'v2-user-list'],
  ['system-user-new', '/system-users/new', ['system_user.add_edit'], 'v2-user-new'],
  ['system-user-edit', '/system-users/1/edit', ['system_user.add_edit'], 'system-user-edit'],
  ['system-login-restrict', '/system-users/login-restrict', ['system_user.login_restriction'], 'v2-login-restrict'],
  ['system-blacklist', '/system-users/blacklist', ['system_user.blacklist'], 'v2-blacklist'],
  ['system-real-name', '/system-users/real-name', ['system_user.realname_records'], 'v2-real-name'],
  ['system-contract', '/system-users/contract', ['system_user.contract_management'], 'v2-contract'],
]

/** 只持「人员管理 + 新开/编辑账号」那三个码 ⇒ 只能进**其中 3 条**（其余 4 条要别的码）。 */
const HR_MINIMAL = ['system_user.list', 'system_user.add_edit']

/** 上面那份最小节点集**进得去**的 3 条（`list` 一条 + `add_edit` 两条）。 */
const HR_MINIMAL_CAN_ENTER = MATRIX.filter(([name]) =>
  ['system-users', 'system-user-new', 'system-user-edit'].includes(name),
)

/** 上面那份最小节点集**进不去**的 4 条（各自要别的 `system_user.*` 码）。 */
const HR_MINIMAL_CANNOT_ENTER = MATRIX.filter(
  ([name]) => !['system-users', 'system-user-new', 'system-user-edit'].includes(name),
)

/** **内置「人事」职位的真实节点集**（`system_user` 栏目行 + 14 个非 flag 子节点）⇒ 7 条全进。 */
const HR_FULL = [
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
]

describe('【系统人员】7 条路由的真实准入', () => {
  it('声明层：各旧地址和当前落点都只按原节点码授权', () => {
    const byName = new Map(realRouter.getRoutes().map((r) => [String(r.name), r]))
    for (const [source, , codes, target] of MATRIX) {
      for (const name of new Set([source, target])) {
        const record = byName.get(name)
        expect(record, `路由表里没有 ${name}`).toBeDefined()
        expect(record!.meta.requiresAccountAdmin, `${name} 仍带 requiresAccountAdmin`).not.toBe(true)
        expect(record!.meta.permissions, `${name} 的声明节点码不符`).toEqual(codes)
      }
    }
  })

  it('⭐ 判据 1：持 `list` + `add_edit` 的非管理员 ⇒ 能进那 **3** 条', async () => {
    me.mockResolvedValue(payload({ menu_permissions: HR_MINIMAL }))
    for (const [, path, , target] of HR_MINIMAL_CAN_ENTER) {
      notify.mockReset()
      await goTo(path)
      expect(realRouter.currentRoute.value.name, `${path} 应当能进`).toBe(target)
      expect(notify, `${path} 不该弹权限提示`).not.toHaveBeenCalled()
    }
  })

  it('⭐ 判据 1c：持 `list` + `add_edit` 的非管理员 ⇒ 其余 **4** 条进不去（回新版首页 + 提示）', async () => {
    me.mockResolvedValue(payload({ menu_permissions: HR_MINIMAL }))
    for (const [, path] of HR_MINIMAL_CANNOT_ENTER) {
      notify.mockReset()
      await goTo(path)
      expect(realRouter.currentRoute.value.name, `${path} 应当被兜回新版首页`).toBe('v2-home')
      expect(notify, `${path} 应当弹 403 提示`).toHaveBeenCalled()
    }
  })

  it('⭐ 判据 1b：**内置「人事」的真实节点集** ⇒ **7 条全进**', async () => {
    me.mockResolvedValue(payload({ menu_permissions: HR_FULL }))
    for (const [, path, , target] of MATRIX) {
      notify.mockReset()
      await goTo(path)
      expect(realRouter.currentRoute.value.name, `${path} 应当能进`).toBe(target)
      expect(notify, `${path} 不该弹权限提示`).not.toHaveBeenCalled()
    }
  })

  it('⭐ 判据 2：**零节点**的非管理员 ⇒ 7 条**全部**回新版首页 + 就地提示（fail-closed）', async () => {
    me.mockResolvedValue(payload({ menu_permissions: [] }))
    for (const [, path] of MATRIX) {
      notify.mockReset()
      await goTo(path)
      expect(realRouter.currentRoute.value.name, `${path} 应当被兜回新版首页`).toBe('v2-home')
      // 承重半边：只断落在 home 会被"压根没发起导航"骗过 ⇒ 同时断那条 403 提示确实弹了。
      expect(notify, `${path} 应当弹 403 提示`).toHaveBeenCalled()
    }
  })

  it('⭐ 判据 3：管理员（account admin）⇒ 7 条照旧全通（正向对照，管理员短路）', async () => {
    me.mockResolvedValue(payload({ is_account_admin: true, menu_permissions: [] }))
    for (const [, path, , target] of MATRIX) {
      notify.mockReset()
      await goTo(path)
      expect(realRouter.currentRoute.value.name, `${path} 应当能进`).toBe(target)
      expect(notify, `${path} 不该弹权限提示`).not.toHaveBeenCalled()
    }
  })
})
