// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 【APP与小程序】组的**逐 viewer 可见性判据**。计划：`docs/plans/2026-10-23-app-mp-stats.md` §7/§8。四条（2026-10-23 合并后，组内两条：数据概览 / 发布文章作品）：① 两个节点都没有的普通账号 ⇒ **整组不画**（空组不渲染）；② 只持 `finance.support_statistics` ⇒ 组里**只有**「数据概览」；③ 只持 `system.article_manage` ⇒ 组里**只有**「发布文章作品」；④ 租户管理员 ⇒ 两条都看得到（`canSeeItem` 的管理员短路，与另几组同源）。⚠️ 用**真** `config/menu.ts`（不 mock）：要钉的正是「真声明对每一种人各是什么结果」。合成夹具在这里会变成自证（E-0024 族）。 */
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe, type User } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import { useMenu } from '../useMenu'

const me = vi.mocked(fetchMe)
const Blank = { template: '<div />' }

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/business/app-stats', name: 'app-stats', component: Blank },
      { path: '/app-mp/articles', name: 'app-mp-articles', component: Blank },
    ],
  })
}

const Probe = defineComponent({
  setup() {
    return useMenu()
  },
  template: '<div />',
})

/** 一份 `/api/auth/me` 载荷（合成值；两个管理员判据默认 false 是**承重**的）。 */
function payload(over: Partial<User> = {}): User {
  return {
    id: 9,
    username: 'stats-viewer',
    display_name: '合成管理员',
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

type MenuGroupView = { name: string; items: Array<{ label: string }> }

function appStats(wrapper: { vm: { groups: MenuGroupView[] } }): MenuGroupView | undefined {
  return wrapper.vm.groups.find((group) => group.name === 'APP与小程序')
}

beforeEach(() => {
  setActivePinia(createPinia())
  me.mockReset()
})

describe('【APP与小程序】菜单可见性', () => {
  it('两个节点都没有的普通账号 ⇒ 整组不画', async () => {
    const wrapper = await mountMenu({ menu_permissions: ['order.list'] })
    expect(appStats(wrapper)).toBeUndefined()
  })

  it('持 finance.support_statistics 的非管理员 ⇒ 只有「数据概览」一条', async () => {
    const wrapper = await mountMenu({ menu_permissions: ['finance.support_statistics'] })
    expect(appStats(wrapper)?.items.map((item) => item.label)).toEqual(['数据概览'])
  })

  it('只持 system.article_manage ⇒ 只有「发布文章作品」一条', async () => {
    const wrapper = await mountMenu({ menu_permissions: ['system.article_manage'] })
    expect(appStats(wrapper)?.items.map((item) => item.label)).toEqual(['发布文章作品'])
  })

  it('租户管理员 ⇒ 两条都看得到（canSeeItem 的管理员短路）', async () => {
    const wrapper = await mountMenu({ is_tenant_admin: true })
    expect(appStats(wrapper)?.items.map((item) => item.label)).toEqual([
      '数据概览',
      '发布文章作品',
    ])
  })
})
