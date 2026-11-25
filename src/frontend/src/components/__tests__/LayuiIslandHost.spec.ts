// @vitest-environment jsdom
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive } from 'vue'
import { mount } from '@vue/test-utils'

const push = vi.fn()
/**
 * `useRoute()` 的桩：宿主从路由上下文取 island key、页签标题、params 与 fullPath
 * （D-0220 起宿主读的是 `ctx` = `frozenRoute ?? route`，所以桩要给出**整个路由形状**，
 * 不只是 `meta`）。形状与 `RouteLocationNormalized` 的相关子集一致。
 */
const routeMock = vi.hoisted(() => ({
  fullPath: '/system-users',
  meta: { islandPage: 'system-user-list', title: '人员管理' },
  params: {} as Record<string, unknown>,
  query: {} as Record<string, unknown>,
}))
// `routeMock` 用 `reactive` 包一层：否则改它不会触发宿主的 `computed`，
// 那条「island → island 切换要换 src」的回归测试就成了假通过（见下面那条用例）。
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  return {
    useRoute: () => reactive(routeMock),
    useRouter: () => ({ push }),
  }
})

/**
 * 会话 store 的取数出口（`refresh()` → `fetchMe()`）。
 * ⚠️ 桩打在 `@/api/auth`（**被查找的那个模块**，E-0029）：宿主只经 `useSessionStore()` 走这一跳，
 * 「壳收到消息后确实**重新取了一次**用户」这条判据就落在这个真实出口的调用次数上。
 */
vi.mock('@/api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchMe } from '@/api/auth'
import { AVATAR_UPDATED_MESSAGE_TYPE } from '@/layui-island/messageTypes'
import LayuiIslandHost from '../LayuiIslandHost.vue'

const me = vi.mocked(fetchMe)

beforeEach(() => {
  push.mockReset()
  me.mockReset()
  routeMock.fullPath = '/system-users'
  routeMock.meta.islandPage = 'system-user-list'
  routeMock.meta.title = '人员管理'
  for (const key of Object.keys(routeMock.params)) delete routeMock.params[key]
  for (const key of Object.keys(routeMock.query)) delete routeMock.query[key]
})

function emitNavigate(data: unknown, origin: string = window.location.origin): void {
  window.dispatchEvent(new MessageEvent('message', { data, origin }))
}

async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

/** 与 `LayuiIslandHost` 的 `frozenRoute` prop 同形（宿主侧的冻结路由上下文，D-0220）。 */
type FrozenRouteProp = {
  fullPath: string
  meta: { islandPage?: unknown; title?: unknown }
  params: Record<string, unknown>
  query: Record<string, unknown>
}

/**
 * 挂一个**带 pinia** 的宿主。本线（顶栏头像同步）起宿主在 setup 里
 * `useSessionStore()` ⇒ 没有 active pinia 会直接抛，所以每条用例都必须走这里。
 * D-0220 起可传 props（页签缓存层的冻结上下文）。
 */
function mountHost(props: { frozenRoute?: FrozenRouteProp } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(LayuiIslandHost, { props, global: { plugins: [pinia] } })
}

describe('LayuiIslandHost（通用 island 宿主）', () => {
  it('iframe 的 src 带 `?page=<key>`，title 取路由 meta.title', () => {
    const wrapper = mountHost()
    const iframe = wrapper.get('[data-testid="layui-island"]')
    expect(iframe.attributes('src')).toBe('/layui-island/page.html?page=system-user-list')
    expect(iframe.attributes('title')).toBe('人员管理')
    wrapper.unmount()
  })

  it('query 白名单：只有 `tab` 透进 iframe（白名单内进得去、白名单外进不去，spec §7.4）', () => {
    routeMock.query.tab = 'password'
    routeMock.query.evil = 'x'
    const wrapper = mountHost()
    const src = wrapper.get('[data-testid="layui-island"]').attributes('src')!
    // ① 白名单**内**的进去——去掉转发（或写成一律不发 query）⇒ 本行红。
    expect(src).toContain('tab=password')
    // ② 白名单**外**的进不去——把白名单删成「转发全部 query」⇒ 本行红。
    //    ⛔ 只测 ① 的话，② 那条变异也会绿，这条判据就废了（两个方向都要钉）。
    expect(src).not.toContain('evil')
    // ③ 整串逐字：页 key 仍在前，白名单 query 只追加、不顶替。
    expect(src).toBe('/layui-island/page.html?page=system-user-list&tab=password')
    wrapper.unmount()
  })

  it('收 island 的 `island:navigate` → 交给 vue-router（带 params）', () => {
    const wrapper = mountHost()
    emitNavigate({ type: 'island:navigate', name: 'system-user-edit', params: { id: '9' } })
    expect(push).toHaveBeenCalledWith({ name: 'system-user-edit', params: { id: '9' } })
    wrapper.unmount()
  })

  it('不带 params 的 navigate → push 只给 name', () => {
    const wrapper = mountHost()
    emitNavigate({ type: 'island:navigate', name: 'system-user-new' })
    expect(push).toHaveBeenCalledWith({ name: 'system-user-new' })
    wrapper.unmount()
  })

  it('跨源消息被忽略（只认同源）', () => {
    const wrapper = mountHost()
    emitNavigate({ type: 'island:navigate', name: 'x' }, 'https://evil.example')
    expect(push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('其它类型 / 缺 name 的消息被忽略', () => {
    const wrapper = mountHost()
    emitNavigate({ type: 'something-else', name: 'x' })
    emitNavigate({ type: 'island:navigate' })
    expect(push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('卸载后不再响应消息（避免路由已切走还 push）', () => {
    const wrapper = mountHost()
    wrapper.unmount()
    emitNavigate({ type: 'island:navigate', name: 'x' })
    expect(push).not.toHaveBeenCalled()
  })
  it('route.meta.islandPage 变化时 iframe 的 src 跟着变（island → island 复用的回归）', async () => {
    const wrapper = mountHost()
    expect(wrapper.get('[data-testid="layui-island"]').attributes('src')).toBe(
      '/layui-island/page.html?page=system-user-list',
    )
    // vue-router 在 island → island 时**复用组件实例**（同一个组件），不会重新挂载——若 src 不是 computed，这里就会一直停在上一个页面（「点菜单没反应、还停留在原页面」正是这个形状）。
    // ⚠️ 必须**穿过 `reactive` 代理**写：Vue 3 里直接改原始对象不触发依赖，那样这条用例会假通过/假失败（`reactive()` 对同一 target 返回同一个缓存代理）。
    const route = reactive(routeMock)
    route.meta.islandPage = 'system-roleauthority'
    route.meta.title = '角色职位权限'
    await nextTick()
    const iframe = wrapper.get('[data-testid="layui-island"]')
    expect(iframe.attributes('src')).toBe('/layui-island/page.html?page=system-roleauthority')
    expect(iframe.attributes('title')).toBe('角色职位权限')
    // ⚠️ **必须卸载**：本组件在 `window` 上挂 `message` 监听，不卸载的话它**活过本用例**——后面任何一条派发消息的用例都会被它多接一次（实测：头像刷新那条从 1 次变 2 次）。
    wrapper.unmount()
    // ⚠️ **必须卸载**：本组件在 `window` 上挂 `message` 监听，不卸载的话它**活过本用例**——后面任何一条派发消息的用例都会被它多接一次（实测：头像刷新那条从 1 次变 2 次）。
  })

  /**
   * 顶栏头像同步线③：岛页在头像**认领成功后**发 `AVATAR_UPDATED_MESSAGE_TYPE`，壳收到后必须**重新取一次会话用户**（`session.refresh()` → `fetchMe()`；⛔ 壳不自己拼 URL）。
   * 判据打在真实出口上：`fetchMe` 的调用次数。⛔ 不读 `session.user`——那样只证明"某个 store 状态变了"，证明不了"确实重新取了一次"。链接是**合成形状**（只仿本机后端 blob 端点），⛔ 不是真值 / 真 URL。
   */
  it('收 island 的头像更新消息 → 重新取一次会话用户（fetchMe）', async () => {
    me.mockResolvedValue({
      id: 9,
      username: 'kefu',
      avatar_url: '/api/media/blob/test/users/9/avatar.test.png',
    } as never)
    const wrapper = mountHost()
    emitNavigate({ type: AVATAR_UPDATED_MESSAGE_TYPE })
    await flush()
    expect(me).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  /**
   * ④ **未知类型与跨源消息都不触发取数**（防「任何消息都刷新」那个洞）。两个方向都必须钉：只测「同源 + 正确类型会刷新」的话，把刷新提到类型判断**之前**照样全绿——而那正是要点名的形态。
   * 同一条用例里带一个 `island:navigate` 的哨兵：新增分支**不许**吃掉既有跳转分支。
   */
  it('未知类型 / 跨源的头像消息都不触发取数，且既有 `island:navigate` 不受影响', async () => {
    const wrapper = mountHost()
    emitNavigate({ type: 'something-else' })
    emitNavigate({ type: AVATAR_UPDATED_MESSAGE_TYPE }, 'https://evil.example')
    await flush()
    expect(me).not.toHaveBeenCalled()

    // 哨兵：既有跳转分支照旧工作（本条若红，说明新分支把老分支吃掉了）。
    emitNavigate({ type: 'island:navigate', name: 'home' })
    expect(push).toHaveBeenCalledWith({ name: 'home' })
    wrapper.unmount()
  })
})

/**
 * D-0220（页签缓存）：宿主被缓存层**每签一份**地常驻挂载，
 * 于是多了「冻结上下文」与「激活门」两块行为。夹具全部用**合成形状**
 * （20 位单号是 `order_no` 路由的合法取值形状，不是真值）。
 */
describe('LayuiIslandHost 的冻结上下文（frozenRoute，D-0220）', () => {
  const FROZEN = {
    fullPath: '/orders/11111111111111111111',
    meta: { islandPage: 'order-orderview', title: '订单详情' },
    params: { order_no: '1'.repeat(20) },
    query: { tab: 'password' },
  }

  it('src / 标题按冻结上下文拼（page → params → 白名单 query 的顺序）', () => {
    routeMock.fullPath = FROZEN.fullPath // 当前路由 = 冻结层 ⇒ 激活态
    const wrapper = mountHost({ frozenRoute: FROZEN })
    const iframe = wrapper.get('[data-testid="layui-island"]')
    expect(iframe.attributes('src')).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=11111111111111111111&tab=password',
    )
    expect(iframe.attributes('title')).toBe('订单详情')
    wrapper.unmount()
  })

  it('当前路由再怎么变，冻结层的 src 不跟走（缓存的关键判据：跟走 = 所有缓存页一起换页）', async () => {
    routeMock.fullPath = FROZEN.fullPath
    const wrapper = mountHost({ frozenRoute: FROZEN })
    const route = reactive(routeMock)
    route.meta.islandPage = 'system-roleauthority'
    route.meta.title = '角色职位权限'
    await nextTick()
    const iframe = wrapper.get('[data-testid="layui-island"]')
    expect(iframe.attributes('src')).toContain('page=order-orderview')
    expect(iframe.attributes('title')).toBe('订单详情')
    wrapper.unmount()
  })

  it('非当前页签（fullPath 不同）：testid 换缓存后缀、island 消息一律不响应', async () => {
    // 桩里当前路由在 `/system-users`，与 FROZEN.fullPath 不同 ⇒ 本层是「被缓存的非激活页签」。
    const wrapper = mountHost({ frozenRoute: FROZEN })
    expect(wrapper.find('[data-testid="layui-island--cached"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="layui-island"]').exists()).toBe(false)

    emitNavigate({ type: 'island:navigate', name: 'somewhere' })
    emitNavigate({ type: AVATAR_UPDATED_MESSAGE_TYPE })
    await flush()
    expect(push).not.toHaveBeenCalled()
    expect(me).not.toHaveBeenCalled()

    // 变回当前页签后恢复响应（切换页签就是这条路）：testid 回到正式值、消息重新生效。
    const route = reactive(routeMock)
    route.fullPath = FROZEN.fullPath
    await nextTick()
    expect(wrapper.find('[data-testid="layui-island"]').exists()).toBe(true)
    emitNavigate({ type: 'island:navigate', name: 'home' })
    expect(push).toHaveBeenCalledWith({ name: 'home' })
    wrapper.unmount()
  })
})
