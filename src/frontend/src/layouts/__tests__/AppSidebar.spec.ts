// @vitest-environment jsdom
// ↑ 本仓库的测试默认 environment 是 `node`（vitest.config.ts），挂载组件需要 DOM ⇒ **只在本文件**显式覆盖（写法与 layouts/__tests__/AppTopbar.spec.ts 一致）。
// 本文件承载**两组**断言（合并 `main` 时并入）：壳复刻分支的**结构闸**（只断言结构与文本，不断言任何视觉结果——图标字号/条目高/子菜单底色是 CSS 值，要核就核构建产物 `dist/assets/*.css`，源规则在 `src/styles/shell.css`）；P5 前端分支的**权限可见性 DOM 闸**（spec §6.1 第 19–21 条）。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * spec §6.1 第 19–21 条的 **DOM 半条**（第 19 条明文要求"`canSeeItem` 纯函数 + 一条 AppSidebar DOM 断言"）。为什么必须 `vi.mock('@/config/menu')` 用**合成夹具**：实测真实菜单里 `permissions` 声明 **0** 次 ⇒ 没有任何真实条目会因权限被隐藏，第 20 条在真配置下**无法被承载**。这份 mock 同时是 `useMenu()` **接线**的唯一守卫：把 `keepItem` 里的 `canSeeItem` 调用去掉后，纯函数测试依然全绿、只有这里会红。
 * 三条判据**必须互不冒充**：#19 admin 全渲染；#20 无交集 → **DOM 里没有该 label 节点**（**不许**用 `disabled === true` 冒充）；#21 未实现（路由表里没有该 name）→ **有节点且置灰**、且被测条目**必须未声明 `permissions`**（否则它会被隐藏、断言恒真）。
 * ⚠️ 夹具生效范围是**整个文件**（`vi.mock` 是模块级的），下面「壳复刻结构闸」那组也在夹具菜单上跑——那组只看 `.shell-logo` / 账号区 / `ElMenu` 的 prop，与菜单条目内容无关，不受影响。判据从 codename 换成节点 code 时本文件同步改（它是 `canSeeItem` **接线**的唯一 DOM 守卫，不跟着改整个套件是红的）。
 */
vi.mock('@/config/menu', () => ({
  // 未声明 permissions（→ 登录即可见）+ 路由表里**有** name → 可见且可点
  TOP_LEVEL_ITEMS: [{ label: '概览', routeName: 'home' }],
  MENU_GROUPS: [
    {
      name: '业务',
      items: [
        // 未声明 permissions + 路由表里**没有** name → 承载 #21：有节点且置灰
        { label: '未开放入口', routeName: 'not-yet' },
        // 已声明 permissions → 持有则可见、不持有则 DOM 里没有节点（承载 #19/#20）
        { label: '机密报表', routeName: 'orders', permissions: ['report.view'] },
      ],
    },
  ],
  // ⚠️ **侧栏最底部那一条必须一起 mock**：`vi.mock` 是**整体替换**模块，少了这个导出，`useMenu()` 里那句 `BOTTOM_ITEMS.filter(...)` 会在**每次挂载**时抛 `Cannot read properties of undefined` ⇒ 本文件**全部**用例红（看起来像壳坏了，其实只是夹具没跟上）。
  // ⚠️ 用一个**路由表里查不到**的 name：渲染出来是**置灰**的，不会混进下面那些「可见 / 可点 / 节点缺席」的判据。
  BOTTOM_ITEMS: [{ label: '底部入口', routeName: 'not-yet-bottom' }],
}))

import { useSessionStore } from '@/stores/session'
import AppSidebar from '../AppSidebar.vue'

/** 测试路由表：**故意**不注册 `not-yet`（那是 #21 的判据来源）。 */
function makeRouter() {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Blank },
      { path: '/orders', name: 'orders', component: Blank },
    ],
  })
}

const USER = {
  id: 1,
  username: 'kefu',
  display_name: '小客服',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
}

/**
 * 工作室名（**合成值**，PII 守卫要求 `src/**` 里不留真值）：侧栏 logo 槽位渲染的就是会话里的
 * `tenant_name`（Task 6 起不再读账号区那个占位常量）。
 */
const STUDIO = '示例工作室甲'

/**
 * 会话里那个头像链接的**合成值**（只仿本机后端 `/api/media/blob/<key>` 的**形状**，
 * ⛔ 不是真 URL、不是真值）。顶栏头像同步线起，侧栏账号区渲染的就是它。
 */
const AVATAR_URL = '/api/media/blob/test/users/9/avatar.test.png'

/**
 * 装 pinia 与路由。⚠️ 合并后的 `useMenu()` 会读会话 store（`useSessionStore()`），所以**任何**挂载都要有 pinia（壳复刻那组测试原版没装，并入时已补）。
 * 只建**一个** pinia 实例：`setActivePinia` 与 `mount` 必须用同一个，否则组合式函数与你拿到的 store 不是同一条链（经典的"两个 pinia"假绿）。
 */
function mountSidebar(
  opts: {
    isTenantAdmin?: boolean
    menuPermissions?: string[]
    tenantName?: string
    avatarUrl?: string | null
  } = {},
) {
  const pinia = createPinia()
  setActivePinia(pinia)
  // `tenantName` 也要能单独给：壳复刻那组只看 logo 槽位与账号区的结构，但**同样**需要一条已就绪的
  // 会话（否则 `tenant_name` 读不到，logo 是空串——那是产品行为，不是这组要测的东西）。
  if (
    opts.isTenantAdmin !== undefined ||
    opts.menuPermissions !== undefined ||
    opts.tenantName !== undefined ||
    opts.avatarUrl !== undefined
  ) {
    const store = useSessionStore()
    store.user = {
      ...USER,
      tenant_name: opts.tenantName ?? '',
      is_tenant_admin: opts.isTenantAdmin ?? false,
      menu_permissions: opts.menuPermissions ?? [],
      avatar_url: opts.avatarUrl ?? null,
    }
    store.status = 'ready'
  }
  const router = makeRouter()
  const wrapper = mount(AppSidebar, {
    props: { open: false, collapsed: false },
    global: { plugins: [pinia, router] },
  })
  return { wrapper, router }
}

/**
 * 菜单项的 label。实测（探针，见 ledger 的 Task 7 修复轮）：EP 渲染成
 * `<li class="el-menu-item"><span>{{ label }}</span>…（置灰时还有 `el-tag`）</li>`，
 * 所以取每个 `li.el-menu-item` 的**第一个** `span` 才是 label，不能取整条文本。
 */
function labels(wrapper: ReturnType<typeof mountSidebar>['wrapper']): string[] {
  return wrapper.findAll('li.el-menu-item').map((li) => li.find('span').text())
}

/** 某个 label 对应的 `li`（找不到就是 undefined——#20 要断言的正是"找不到"）。 */
function itemLi(wrapper: ReturnType<typeof mountSidebar>['wrapper'], label: string) {
  return wrapper.findAll('li.el-menu-item').find((li) => li.find('span').text() === label)
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('AppSidebar 的菜单可见性（spec §6.1 第 19–21 条的 DOM 半条）', () => {
  it('#19 is_tenant_admin：声明了权限的条目照样渲染，且**可点**（不是靠置灰冒充可见）', () => {
    const { wrapper } = mountSidebar({ isTenantAdmin: true })
    expect(labels(wrapper)).toContain('机密报表')
    const li = itemLi(wrapper, '机密报表')
    expect(li, '管理员必须能看到声明了 permissions 的条目').toBeDefined()
    expect(li!.classes()).not.toContain('is-disabled')
  })

  it('#20 普通账号不持有声明的节点 code：DOM 里**没有该 label 节点**', () => {
    const { wrapper } = mountSidebar({ menuPermissions: [] })
    // 先钉住"夹具确实渲染出来了"——否则"没有节点"可能是整块都没渲染而恒真
    expect(labels(wrapper)).toContain('概览')
    expect(labels(wrapper)).toContain('未开放入口')
    expect(labels(wrapper)).not.toContain('机密报表')
    // #20 的判据是**节点缺席**，不是置灰：这条把它与 #21 的语义分开
    expect(itemLi(wrapper, '机密报表')).toBeUndefined()
    expect(wrapper.text()).not.toContain('机密报表')
  })

  it('#20 反向：持有该节点 code 的普通账号就能看到（证明判据真的读的是登录态的 menu_permissions）', () => {
    const { wrapper } = mountSidebar({ menuPermissions: ['report.view'] })
    expect(labels(wrapper)).toContain('机密报表')
    expect(itemLi(wrapper, '机密报表')!.classes()).not.toContain('is-disabled')
  })

  it('#21 路由表里没有该 name：**有节点**且置灰（未声明 permissions 的条目不会被权限隐藏）', () => {
    const { wrapper } = mountSidebar({ menuPermissions: [] })
    const li = itemLi(wrapper, '未开放入口')
    expect(li, '未声明 permissions 的条目必须仍然渲染').toBeDefined()
    expect(li!.classes()).toContain('is-disabled')
    // 对照：#20 的隐藏项是"没有节点"，这里必须"有节点"——两者不能互相冒充
    expect(itemLi(wrapper, '机密报表')).toBeUndefined()
  })

  it('可见且可点的条目**不**置灰（否则上面的 `is-disabled` 断言可能是恒真）', () => {
    const { wrapper } = mountSidebar({ menuPermissions: [] })
    expect(itemLi(wrapper, '概览')!.classes()).not.toContain('is-disabled')
  })

  /**
   * **侧栏最底部**那一条（问题反馈线，spec §6.1）。判据分两层：它**真的渲染出来了**（`useMenu().bottom` 接到了模板上——只测 `useMenu` 的话"接线漏了"不会红）；它在 `</el-sub-menu>` **之后**、`</el-menu>` **之前**（业务方原话「直接排到最下面的菜单」）。
   * ⚠️ **不**断言视觉位置（`top` / `margin-top` 不是本文件的判据），只断言 DOM 顺序：底条目的 `li` 是 `li.el-menu-item` 里的**最后一个**、且排在所有 `li.el-sub-menu` 之后。
   */
  it('侧栏最底部那一条真的渲染出来了，且排在所有分组之后（不是"接到别处去了"）', () => {
    const { wrapper } = mountSidebar({ menuPermissions: [] })
    expect(labels(wrapper)).toContain('底部入口')
    const menu = wrapper.get('ul.el-menu')
    const children = Array.from(menu.element.children)
    const last = children[children.length - 1] as HTMLElement
    expect(last.textContent).toContain('底部入口')
    // 它之前确实有分组（否则"排在最后"这条可能是"菜单里只有它"而恒真）。
    expect(wrapper.findAll('li.el-sub-menu').length).toBeGreaterThan(0)
    const subMenuIndex = children.findIndex((el) => el.classList.contains('el-sub-menu'))
    expect(subMenuIndex).toBeGreaterThan(-1)
    expect(children.indexOf(last)).toBeGreaterThan(subMenuIndex)
  })
})

describe('AppSidebar（壳复刻分支并入的结构闸）', () => {
  // 这条闸来自实现者提议：工作室名渲染在 `.shell-logo` 槽位里，而**没有任何机器闸**守着它——改回产品名、或又搬回账号区，门禁都不会响。取值来源 = 会话里的 `tenant_name`（占位常量已删）。
  it('logo 槽位渲染的是会话里的工作室名，不是产品名', () => {
    const { wrapper } = mountSidebar({ tenantName: STUDIO })
    const logo = wrapper.get('.shell-logo')
    expect(logo.text()).toContain(STUDIO)
    expect(logo.text()).not.toContain('团团通')
  })

  // 上一条闸只覆盖「工作室名不见了 / 被搬走」，**不覆盖重复渲染**——账号区又把工作室名渲染一遍时它照样通过（那正是裁定要防的回归）。所以另加一条计数的闸。
  it('工作室名在整个侧栏里只出现一次（账号区不得重复渲染它）', () => {
    const { wrapper } = mountSidebar({ tenantName: STUDIO })
    const sidebarText = wrapper.get('.shell-sidebar').text()
    const occurrences = sidebarText.split(STUDIO).length - 1
    expect(occurrences).toBe(1)
  })

  // 会话未就绪（引导 `me` 还没回来 / 已登出）时 logo 是空串：spec §7.1 明确「不写占位假名」。
  // 没有这条，「回落到一个示例名」也能全绿（上面两条都给了会话）。
  it('会话未就绪时 logo 为空，不回落假名', () => {
    const { wrapper } = mountSidebar()
    expect(wrapper.get('.shell-logo').text()).toBe('')
  })

  it('el-menu 打开 unique-opened（同时只展开一个，对标物 lay-shrink="all"）', async () => {
    const { wrapper, router } = mountSidebar()
    await router.isReady()
    const menu = wrapper.findComponent({ name: 'ElMenu' })
    expect(menu.exists()).toBe(true)
    expect(menu.props('uniqueOpened')).toBe(true)
  })

  /**
   * 顶栏头像同步线：会话里的 `avatar_url` 必须**真的**传进账号区。红线：此前写死 `:avatar-url="null"` ⇒ 业务方传了头像、右上角仍是空。判据取两态（给链接 / 不给链接），否则「写死 null」与「正确透传」只靠一半断言分不开。⚠️ 链接是**合成形状**，⛔ 不是真值 / 真 URL。
   */
  it('会话里的 `avatar_url` 非空 ⇒ 账号区渲染它；为空 ⇒ 回落生成的色板头像', () => {
    const withAvatar = mountSidebar({ avatarUrl: AVATAR_URL })
    expect(withAvatar.wrapper.get('.shell-account__avatar').attributes('src')).toBe(AVATAR_URL)

    // 对照：同一夹具不给 `avatar_url` 时是生成器给的 `data:` URI ⇒ 上面的断言不是恒真。
    const without = mountSidebar({ avatarUrl: null })
    expect(without.wrapper.get('.shell-account__avatar').attributes('src'))
      .toMatch(/^data:image\/svg\+xml/)
  })
})
