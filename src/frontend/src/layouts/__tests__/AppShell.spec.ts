// @vitest-environment jsdom
// ↑ 本仓库的测试默认 environment 是 `node`（vitest.config.ts），挂载组件需要 DOM，所以**只在本文件**显式覆盖（写法与 layouts/__tests__/AppTopbar.spec.ts 一致）；本文件只断言**结构与导航行为**，不断言任何视觉结果。为什么这几条值得单独写：AppShell 是「页签状态（composable）↔ 路由」唯一接线的地方——没有它们，「导航不登记页签」或「关页签不切路由」都不会让任何门禁响。
// ⚠️ **本文件没有闸的两件事**：`AppTabsBar` 里那些点击**自己不导航**——那条在 `components/__tests__/AppTabsBar.spec.ts` 里断言；`LayuiIslandHost` 的冻结上下文行为——那条在 `components/__tests__/LayuiIslandHost.spec.ts` 里断言。这里断言的是**壳这一层**把页签 ↔ 缓存层 ↔ 路由接了起来（D-0220）。
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, onBeforeUnmount } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import AppShell from '../AppShell.vue'

/**
 * 带计数探针的路由表：每条路由一个**不同的**具名组件（setup / unmount 各自计数），「内容组件有没有被重挂 / 有没有被销毁」就打在这两个计数上（D-0220 的判据形态）。
 * `/`、`/orders`、`/games` 带 `meta.islandPage`（island 页），`/orders/:order_no` 与 `/bare` 不带（非 island 页），`/anon` 无 name 无 title（匿名路由，不登记页签）。
 */
type Probes = { setups: Record<string, number>; unmounts: Record<string, number> }

function makeProbedRouter(): { router: Router } & Probes {
  const setups: Record<string, number> = {}
  const unmounts: Record<string, number> = {}
  const probe = (name: string) =>
    defineComponent({
      name: `probe-${name}`,
      setup() {
        setups[name] = (setups[name] ?? 0) + 1
        onBeforeUnmount(() => {
          unmounts[name] = (unmounts[name] ?? 0) + 1
        })
        return () => h('div', { class: `probe-${name}` })
      },
    })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: probe('home'), meta: { title: '后台首页', islandPage: 'index-console' } },
      { path: '/orders', name: 'orders', component: probe('orders'), meta: { title: '管理订单', islandPage: 'order-orderlist' } },
      { path: '/orders/:order_no', name: 'order-detail', component: probe('detail'), meta: { title: '订单详情' } },
      { path: '/games', name: 'games', component: probe('games'), meta: { title: '游戏管理', islandPage: 'game-gameserver' } },
      { path: '/bare', name: 'bare', component: probe('bare') },
      { path: '/anon', component: probe('anon') },
    ],
  })
  return { router, setups, unmounts }
}

async function mountShellAt(path: string) {
  // ⚠️ 必须装 pinia（P5 前端合并后）：AppShell 里的 AppTopbar 会 `useSessionStore()` 读登录态，
  // 没有 active pinia 时 pinia 直接抛。这里不设 `user`：本文件的断言只看页签条与内容区。
  const pinia = createPinia()
  setActivePinia(pinia)
  const probed = makeProbedRouter()
  await probed.router.push(path)
  await probed.router.isReady()
  const w = mount(AppShell, { global: { plugins: [pinia, probed.router] } })
  await flushPromises()
  return { w, router: probed.router, setups: probed.setups, unmounts: probed.unmounts }
}

// 内容区容器（AppTabbedContent）会给 body 加删 `shell-flush-content`；用例不逐个卸载壳，
// 统一在每条用例前清一次，避免上一条的挂载态漏进下一条的断言。
beforeEach(() => {
  document.body.classList.remove('shell-flush-content')
})

type W = VueWrapper
const tabLabels = (w: W) => w.findAll('.shell-tabs__label').map((i) => i.text())
const activeLabel = (w: W) => w.get('.shell-tabs__item--active .shell-tabs__label').text()

describe('AppShell 的页签条接线', () => {
  it('进页面时自动登记一条页签，标题取路由的 meta.title', async () => {
    const { w } = await mountShellAt('/')
    expect(tabLabels(w)).toEqual(['后台首页'])
    expect(activeLabel(w)).toBe('后台首页')
  })

  it('meta.title 缺失时回退到路由名（口径同设计 spec §5.1 第 1 条）', async () => {
    const { w } = await mountShellAt('/bare')
    expect(tabLabels(w)).toEqual(['bare'])
  })

  it('每次成功导航各登记一条；重复导航同一条不新增、只切换激活', async () => {
    const { w, router } = await mountShellAt('/')

    await router.push('/orders')
    await flushPromises()
    await router.push('/games')
    await flushPromises()
    expect(tabLabels(w)).toEqual(['后台首页', '管理订单', '游戏管理'])
    expect(activeLabel(w)).toBe('游戏管理')

    await router.push('/orders')
    await flushPromises()
    expect(tabLabels(w), '回到已开过的页面时新增了重复页签').toEqual([
      '后台首页',
      '管理订单',
      '游戏管理',
    ])
    expect(activeLabel(w)).toBe('管理订单')
  })

  it('首页那条没有 ×（对标：首签的 close 被 admin.css 隐藏 + 三个批量关闭都保留 index 0）', async () => {
    const { w } = await mountShellAt('/')
    expect(w.find('.shell-tabs__close').exists()).toBe(false)
  })

  it('点页签走 router.push 切回去（不是改 iframe、不是 router.go）', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/games')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/games')

    await w.findAll('.shell-tabs__item')[0]?.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(activeLabel(w)).toBe('后台首页')
  })

  it('带 params 的路由：不同参数是两条页签，切回去保留参数', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/orders/1')
    await flushPromises()
    await router.push('/orders/2')
    await flushPromises()

    expect(tabLabels(w)).toEqual(['后台首页', '订单详情', '订单详情'])

    await w.findAll('.shell-tabs__item')[1]?.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath, '切回带 params 的页签丢了参数').toBe('/orders/1')
  })

  it('关掉当前激活的中间页签：切到右边那条，页签数减一', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/orders')
    await flushPromises()
    await router.push('/games')
    await flushPromises()
    await router.push('/orders') // 激活中间那条
    await flushPromises()

    await w.findAll('.shell-tabs__close')[0]?.trigger('click')
    await flushPromises()

    expect(tabLabels(w)).toEqual(['后台首页', '游戏管理'])
    expect(router.currentRoute.value.fullPath, '关掉当前页签后没有切到相邻页签').toBe('/games')
    expect(activeLabel(w)).toBe('游戏管理')
  })

  it('关掉当前激活的最后一条：切到左边那条', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/games')
    await flushPromises()

    await w.findAll('.shell-tabs__close')[0]?.trigger('click')
    await flushPromises()

    expect(tabLabels(w)).toEqual(['后台首页'])
    expect(router.currentRoute.value.fullPath).toBe('/')
  })

  it('「关闭全部标签页」回到首页；「关闭其它」保留首页与当前', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/orders')
    await flushPromises()
    await router.push('/games')
    await flushPromises()

    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[2]?.trigger('click') // 关闭全部标签页
    await flushPromises()
    expect(tabLabels(w)).toEqual(['后台首页'])
    expect(router.currentRoute.value.fullPath).toBe('/')

    await router.push('/orders')
    await flushPromises()
    await router.push('/games')
    await flushPromises()
    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[1]?.trigger('click') // 关闭其它标签页
    await flushPromises()
    expect(tabLabels(w)).toEqual(['后台首页', '游戏管理'])
    expect(router.currentRoute.value.fullPath).toBe('/games')
  })

  it('点刷新只重挂**当前页签**的内容，其它页签的缓存保留（D-0220 起的刷新语义）', async () => {
    const { w, router, setups } = await mountShellAt('/')
    await router.push('/orders')
    await flushPromises()
    // 前置条件也要断言：两层若压根没挂上，下面的计数断言会以「0 → 1」的假象通过。
    expect(setups.home, '首页层没有挂上，下面的缓存断言会失去意义').toBe(1)
    expect(setups.orders).toBe(1)

    await w.get('.shell-tabs__refresh').trigger('click')
    await flushPromises()
    expect(setups.orders, '刷新后当前页没有重挂').toBe(2)
    expect(setups.home, '刷新把别的页签缓存也丢了（旧实现整 main 换 :key 的行为）').toBe(1)
  })
})

describe('AppShell 的页签缓存（D-0220）', () => {
  it('切到别的路由再回来：内容组件不重挂、缓存层隐藏但不销毁（主判据）', async () => {
    const { w, router, setups, unmounts } = await mountShellAt('/')
    await router.push('/orders')
    await flushPromises()
    await router.push('/')
    await flushPromises()

    expect(setups.home, '切走再回来重挂了 ⇒ 缓存没生效（业务方报的「内容丢了」）').toBe(1)
    expect(unmounts.home ?? 0).toBe(0)
    expect(setups.orders).toBe(1)

    // 只有当前层可见；缓存层在 DOM 里、display:none（iframe 不移动不销毁）。
    expect(w.get('[data-tab-layer="/"]').attributes('style') ?? '').not.toContain('display: none')
    expect(w.get('[data-tab-layer="/orders"]').attributes('style') ?? '').toContain('display: none')
  })

  it('关页签销毁对应缓存层（卸载钩子触发），别的层不动', async () => {
    const { w, router, unmounts } = await mountShellAt('/')
    await router.push('/orders')
    await flushPromises()
    await router.push('/games')
    await flushPromises()

    await w.findAll('.shell-tabs__close')[0]?.trigger('click') // 关掉中间的「管理订单」
    await flushPromises()

    expect(unmounts.orders, '关页签没有销毁那一层的缓存').toBe(1)
    expect(unmounts.home ?? 0).toBe(0)
    expect(unmounts.games ?? 0).toBe(0)
  })

  it('island 页加 shell-flush-content、非 island 页移除（class 由缓存容器统一管）', async () => {
    const { router } = await mountShellAt('/') // 首页 = island 页
    expect(document.body.classList.contains('shell-flush-content')).toBe(true)

    await router.push('/orders/1') // 无 islandPage 的路由（非 island 页形态）
    await flushPromises()
    expect(document.body.classList.contains('shell-flush-content')).toBe(false)

    await router.push('/games') // 切到另一个 island 页
    await flushPromises()
    expect(document.body.classList.contains('shell-flush-content')).toBe(true)
  })

  it('匿名路由（标题与路由名都为空）不登记页签，但内容仍照常渲染（RouterView 兜底）', async () => {
    const { w, router } = await mountShellAt('/')
    await router.push('/anon')
    await flushPromises()

    expect(tabLabels(w), '匿名路由不该画成页签').toEqual(['后台首页'])
    expect(w.find('.probe-anon').exists(), '匿名路由没有内容渲染（白屏回归）').toBe(true)
  })
})
