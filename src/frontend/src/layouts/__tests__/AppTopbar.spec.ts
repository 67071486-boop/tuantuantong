// @vitest-environment jsdom
import { createPinia, setActivePinia } from 'pinia'
import { DOMWrapper, enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))

// ⚠️ AppTopbar 里有 `useRouter()`：不 mock 它，登出时 `router.replace` 会是 undefined → TypeError。
// （先例：`src/views/orders/__tests__/OrderNewView.spec.ts:32` 同款手法。）
const replace = vi.fn()
// 壳接真身份后（2026-09-24 我的资料线）顶栏还要 `router.push` 到我的资料页：mock 必须一并给出 `push`，
// 否则点「我的资料」会是 `push is not a function`（TypeError），而不是一条可读的断言失败。
const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ replace, push }) }))

import { logout } from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import AppTopbar from '../AppTopbar.vue'

const apiLogout = vi.mocked(logout)

/**
 * ⚠️ 菜单必须从 **`document.body`** 取，不能从 `wrapper` 取——这是实测结论：ElDropdown 的菜单由 ElTooltip 的 popper 渲染、`teleported` 默认 `true` → 落进 `document.body`。实测：默认 teleport 时 `wrapper.text()` 里没有身份串、`wrapper.find('[data-test="logout"]')` 实得 0 个；`document.body` 一侧全命中。所以 mount 用 `attachTo: document.body`，断言与点击都打在 body 上。
 * **不用 `stubs: { teleport: true }`**（试过，已否决）：桩掉 teleport 后 popper 会在测试收尾时自我更新失控，vitest 报 `Maximum recursive updates exceeded in component <ElDropdown>` 的 Unhandled Rejection——断言即使全绿也以 **exit 1** 结束（门禁假红）。故不用桩。
 */
function mountTopbar() {
  return mount(AppTopbar, { props: { title: 'T' }, attachTo: document.body })
}

/** 菜单在 body 里（teleport），用 `DOMWrapper` 包一层保留 VTU 的 `trigger` 语义（而不是裸 dispatchEvent）。 */
function logoutItem() {
  return new DOMWrapper(document.body.querySelector('[data-test="logout"]') as HTMLElement)
}

/** 菜单的 popper（菜单本体 `.el-dropdown-menu` 在它里面）。 */
function popperEl(): HTMLElement {
  const el = document.body.querySelector<HTMLElement>('.el-dropdown__popper')
  if (!el) throw new Error('下拉 popper 未渲染')
  return el
}

/** 菜单里的项（`divided` 渲染出的是 `li[role="separator"]`，不吃 `.el-dropdown-menu__item` 这个类）。 */
function menuItems(): HTMLElement[] {
  return Array.from(document.body.querySelectorAll<HTMLElement>('.el-dropdown-menu__item'))
}

/**
 * **走用户路径打开下拉**，并等到它**真的展开**——不依赖 popper 的预渲染。两个实测约束：① EP 的 hover 触发带 `showTimeout: 150`（`element-plus/es/components/dropdown/src/dropdown.mjs`），`hover` 之后立刻读仍是未展开（250ms 后才展开），`flushPromises()` **推不动定时器** ⇒ 用 `vi.waitFor` 轮询条件而不是 sleep 魔数；
 * ② 展开与否的判据是 **popper 的 `aria-hidden`**，不是 `.el-dropdown-menu` 的 display：菜单本体是 `persistent` 预渲染的，未展开时它自己也是 `display: block`。判别手段：删掉下面的 `trigger('mouseenter')`，`vi.waitFor` 会超时 → 用例红。
 */
async function openMenu(wrapper: ReturnType<typeof mountTopbar>) {
  await wrapper.find('.topbar-user').trigger('mouseenter')
  await vi.waitFor(() => {
    expect(popperEl().getAttribute('aria-hidden'), '下拉必须真的展开（F2）').toBe('false')
  })
}

/** 每次用例后卸载：组件实例不该跨用例存活（body 由下一个 beforeEach 清空）。 */
enableAutoUnmount(afterEach)

beforeEach(() => {
  setActivePinia(createPinia())
  apiLogout.mockReset()
  apiLogout.mockResolvedValue(null)
  replace.mockReset()
  push.mockReset()
  document.body.innerHTML = ''
})

describe('顶栏身份区', () => {
  it('触发元素渲染「昵称(职位)」，下拉恰好对标那三项（顺序固定）', async () => {
    const store = useSessionStore()
    store.user = {
      id: 1, username: 'kefu', display_name: '小客服', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
      // Task 6 起右区读**会话里的真数据**：昵称(职位) 与 下拉三项都由它装配。
      role: '客服', tenant_name: '示例工作室',
    }
    store.status = 'ready'
    const wrapper = mountTopbar()
    // 触发元素上是**半角圆括号**的「昵称(职位)」（对标顶栏那一处；侧栏是方括号，见 AppAccountBlock）
    expect(wrapper.find('.topbar-user').text()).toBe('小客服(客服)')
    await openMenu(wrapper)
    // ⚠️ 这一条**必须在 `openMenu()` 外面**：`openMenu` 内部的 `aria-hidden` 断言会随 helper 一起被删掉，"把 `await openMenu(wrapper)` 整行删掉"原先**仍然是绿的**（内容断言不要求菜单真展开——EP 的 popper 是 persistent 预渲染，关闭时也读得到、点得动）。把判据留在外面，删掉调用就必红。
    // 判据取**父级** popper 的 computed display：`.el-dropdown__popper` 关闭时是 `none`、展开后是 `block`（实测值）。**不要**改判子级 `.el-dropdown-menu`——它同样是 persistent 预渲染的，关闭时自己也是 `block`，那样断言恒真。
    expect(getComputedStyle(popperEl()).display).toBe('block')
    expect(document.body.textContent).toContain('小客服(客服)')
    // 本线的顶栏下拉：恰好对标那三项（术语「数据空间」与 `identity_plane` 不再上屏，spec §7.2/D4）
    expect(menuItems().map((el) => el.textContent?.trim())).toEqual(['我的资料', '修改密码', '退出系统'])
    // A6：`identity_plane`（平台/租户平面的内部标识）**不许**上屏——spec D4 / §13-5。
    // ⛔ 这条**否定断言必须留着**：原来那句 `toContain('tenant_a')` 被删掉后不补，
    // 给顶栏再加一行平面信息时不会有任何用例红。夹具的平面值就是 `tenant_a`。
    expect(document.body.textContent).not.toContain('tenant_a')
  })

  it('「我的资料 / 修改密码」按对标跳同一页、后者带密码页签参数', async () => {
    const store = useSessionStore()
    store.user = {
      id: 1, username: 'kefu', display_name: '小客服', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
      role: '客服', tenant_name: '示例工作室',
    }
    store.status = 'ready'
    const wrapper = mountTopbar()

    await openMenu(wrapper)
    await menuItems()[0]!.click()
    expect(push).toHaveBeenCalledWith({ name: 'accounts-profile' })

    // 两项各自落一个**不同的**目标：只钉一个的话，「两处都跳同一地址」也能全绿。
    await openMenu(wrapper)
    await menuItems()[1]!.click()
    expect(push).toHaveBeenCalledWith({ name: 'accounts-profile', query: { tab: 'password' } })
  })

  it('登出：**请求还没回来**本地就已终结，并跳 /login', async () => {
    // ⚠️ 这条必须**对顺序敏感**：让 apiLogout 返回 pending promise，点了之后**不 flush** 就断言。
    // 若实现写成"先 await 请求、回来了再 clear()"，本用例会红（"await 之后才断言终态"的写法
    // 对顺序零判别力——Task 2 评审实测抓到这个坑）。
    let release!: () => void
    // `logout()` 的返回类型是 `Promise<null>`（`api/auth.ts:31`），所以桩的 promise 也必须解析成 `null`：
    // brief 里写的 `new Promise<void>` 过不了 `pnpm typecheck`（TS2322，实测）。
    apiLogout.mockImplementation(
      () => new Promise<null>((resolve) => { release = () => resolve(null) }),
    )
    const store = useSessionStore()
    store.user = {
      id: 1, username: 'kefu', display_name: '', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
    }
    store.status = 'ready'
    const wrapper = mountTopbar()
    // 「空 display_name 回退 username」的**正面**断言（brief 的两条用例都没断言它）：
    // 只把 `who` 改成只取 display_name 时，brief 版会因"菜单整个消失"而在下面的 find 上红——
    // 红的原因是菜单没了，不是回退失效，判别力打在别处。所以补这一条。
    expect(document.body.textContent).toContain('kefu')
    await openMenu(wrapper) // F2：登出也走「菜单已打开」的用户路径，而不是靠预渲染的隐藏节点
    await logoutItem().trigger('click')
    expect(store.status).toBe('guest') // ← 请求仍在途，本地必须已经终结（spec §4.1-4）
    expect(store.user).toBeNull()
    release()
    await flushPromises()
    expect(apiLogout).toHaveBeenCalled()
    expect(replace).toHaveBeenCalledWith({ name: 'login' })
  })

  // ── 下面这组来自**壳复刻分支**（合并 `main` 时并入）：顶栏标题与「账号名(职位)」同处顶栏。
  // 合并适配（如实记）：原版 mount **不带 pinia**，而 AppTopbar 要读会话 ⇒ 补 `setActivePinia(createPinia())`；`accountLabel` prop 已随占位常量一并删除，**无会话时右区留空**（不回落假名字）——原来靠 prop 断言的那条用例换成「装配真会话」的正面断言。
  it('标题渲染在顶栏内；会话未就绪时右区渲染空（不回落假名）', () => {
    setActivePinia(createPinia())
    const wrapper = mount(AppTopbar, { props: { title: '团团通 · 租户后台' } })

    expect(wrapper.find('.shell-topbar__title').text()).toBe('团团通 · 租户后台')
    // 账号元素必须和标题同处顶栏，不能落到 logo 区或壳外。
    expect(wrapper.find('.shell-topbar .shell-topbar__account').text()).toBe('')
    // 没有会话就不渲染触发元素（`v-if="who"`）：假名字比空更糟（spec §7.5 的代价段）。
    expect(wrapper.find('.topbar-user').exists()).toBe(false)
  })

  it('有会话时右区渲染「名(职位)」——半角圆括号（侧栏那处是方括号，不统一）', () => {
    const store = useSessionStore()
    store.user = {
      id: 1, username: 'kefu', display_name: '示例账号', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
      role: '系统管理员', tenant_name: '示例工作室',
    }
    store.status = 'ready'
    const wrapper = mount(AppTopbar, { props: { title: '团团通 · 租户后台' } })

    expect(wrapper.find('.shell-topbar__title').text()).toBe('团团通 · 租户后台')
    // ⚠️ D-0346 起账号容器里还住着「客服工具」链接 ⇒ 身份断言收窄到下拉触发元素本身。
    expect(wrapper.find('.shell-topbar__account .topbar-user').text()).toBe('示例账号(系统管理员)')
  })
})

/**
 * 客服工具下载入口（D-0345；⭐ D-0346 挪进右区容器——线上错位修复）。定位（读码结论，登记在 D-0345）：本仓顶栏是**自建壳**、不是对标顶栏的转录（对标那一排在本仓没有对应渲染面）⇒ 「客服工具」是自建面上的新增入口，不走「对标转录 + 有意偏离登记」那条路，也没有 menu/replicaRoutes 义务。可见性（业务方拍板）：**所有登录用户**；未登录不渲染。目标 `/kefu-tools/` 静态下载页**不在 SPA 路由表里** ⇒ 新标签 `<a target="_blank">`（站内 router.push 会把整个壳连页签状态一起丢掉）。
 * **位置（D-0346，业务方报障「被侧栏遮住只剩一个具字」）**：链接必须住在**右侧账号容器** `.shell-topbar__account` 里面、账号下拉旁边。⚠️ 错位根因：顶栏的 toggle/title 用 `position: relative; left: var(--shell-sidebar-w)` 只是**画位偏移**、flex 布局盒仍留在最左 ⇒ 排在后面的兄弟节点落进布局洞（被 fixed logo 块盖住，只剩末字）。所以位置钉断言「在账号容器内」而**不是**「顶栏直接子元素」——谁再把它挪回顶栏层（回到布局洞）就当场红。
 */
describe('客服工具下载入口', () => {
  /** 与本文件既有夹具同形的登录态。 */
  function login(): void {
    const store = useSessionStore()
    store.user = {
      id: 1, username: 'kefu', display_name: '小客服', identity_plane: 'tenant_a',
      is_tenant_admin: false, roles: [], permissions: [],
      role: '客服', tenant_name: '示例工作室',
    }
    store.status = 'ready'
  }

  it('登录态：顶栏渲染「客服工具」链接——指向 /kefu-tools/、新标签，且住在右侧账号容器内', () => {
    login()
    const wrapper = mountTopbar()
    const entry = wrapper.find('[data-test="kefu-tool-entry"]')
    expect(entry.exists(), '登录态必须有客服工具入口').toBe(true)
    expect(entry.text()).toBe('客服工具')
    // 站内绝对路径：落在哪个租户域名就下哪个域名的 /kefu-tools/（静态页随前端发版，每台租户机同一份）
    expect(entry.attributes('href')).toBe('/kefu-tools/')
    // 静态页在 SPA 之外 ⇒ 新标签；rel=noopener 是 target=_blank 的安全标配
    expect(entry.attributes('target')).toBe('_blank')
    expect(entry.attributes('rel')).toBe('noopener')
    // ⭐ 位置钉（D-0346）：必须在账号容器 `.shell-topbar__account` **里面**（margin-left:auto
    //    把整块顶到右上角），且排在账号下拉触发元素之前（「账号名(职位)」的左边）。
    const account = wrapper.find('.shell-topbar__account')
    expect(account.exists()).toBe(true)
    expect(
      entry.element.closest('.shell-topbar__account'),
      '客服工具必须住在右侧账号容器内——挪回顶栏直接子元素就会落进 toggle/title 的 relative 布局洞（被 logo 盖住）',
    ).not.toBeNull()
    const trigger = wrapper.find('.topbar-user')
    expect(trigger.exists()).toBe(true)
    const pos = entry.element.compareDocumentPosition(trigger.element)
    expect(pos & Node.DOCUMENT_POSITION_FOLLOWING, '入口必须排在账号名(职位)左侧').toBeTruthy()
    // 同处顶栏（不能掉到壳外）
    expect(entry.element.closest('.shell-topbar')).not.toBeNull()
  })

  it('未登录：不渲染入口（顶栏右区照现状留空）', () => {
    const wrapper = mountTopbar()
    expect(wrapper.find('[data-test="kefu-tool-entry"]').exists()).toBe(false)
    expect(wrapper.find('.topbar-user').exists()).toBe(false)
  })
})
