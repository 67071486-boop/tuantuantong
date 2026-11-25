// @vitest-environment jsdom
// ↑ 本仓库测试默认 environment 是 `node`（vitest.config.ts），挂载组件需要 DOM，故只在本文件覆盖。只断言**结构 / 事件 / 那一次点击不导航**，不断言任何视觉结果（本机测试无渲染引擎）。
// 本文件与 `composables/__tests__/usePageTabs.spec.ts` 的分工：**增删切的规则全在那边**，这里只钉「把 props 渲染成什么、点哪里发什么事件」。
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import AppTabsBar from '../AppTabsBar.vue'
import { HOME_TAB_KEY, type PageTab } from '@/composables/usePageTabs'

const TABS: PageTab[] = [
  { key: HOME_TAB_KEY, title: '后台首页', closable: false },
  { key: '/orders', title: '管理订单', closable: true },
  { key: '/game-config/games', title: '游戏管理', closable: true },
]

/** 本组件自己**不用**路由；建一个只为「这些点击不得导航」那条断言提供可监听的实例。 */
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', name: 'home', component: { render: () => null } }],
  })
}

/**
 * `window.location` 的替身：**只记录写入与导航方法调用，不记录读取**（读一次 `href` 不是导航）。为什么整对象换掉、而不是 `vi.spyOn(window.location, 'reload')`：本机 jsdom 30.1.0 实测 spy 会 `TypeError: Cannot redefine property: reload`，而 `window` 的 `location` 描述符是 `{configurable: true}`，所以整个对象可换、也能按原描述符换回来。
 * 覆盖范围：`location.reload/assign/replace` 被调用，以及**任何**属性被写（含 `href = …`）。
 */
function installLocationSpy() {
  const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'location')
  const touched: string[] = []
  const target: Record<string, unknown> = { href: 'http://localhost/' }
  for (const method of ['reload', 'assign', 'replace'] as const) {
    target[method] = () => {
      touched.push(`${method}()`)
    }
  }
  Object.defineProperty(window, 'location', {
    value: new Proxy(target, {
      get: (t, prop) => t[prop as string],
      set: (t, prop, value) => {
        touched.push(`set:${String(prop)}`)
        t[prop as string] = value
        return true
      },
    }),
    configurable: true,
  })
  return {
    touched,
    restore: () => {
      if (originalDescriptor) Object.defineProperty(window, 'location', originalDescriptor)
    },
  }
}

let restoreLocation: (() => void) | null = null
afterEach(() => {
  restoreLocation?.()
  restoreLocation = null
})

function mountBar(activeKey = HOME_TAB_KEY) {
  const router = makeRouter()
  return mount(AppTabsBar, {
    props: { tabs: TABS, activeKey },
    global: { plugins: [router] },
  })
}

describe('AppTabsBar 的页签列表', () => {
  it('每条页签都渲染出来，只有激活那条带 --active', () => {
    const w = mountBar('/orders')
    const items = w.findAll('.shell-tabs__item')

    // 只断文字节点（`.shell-tabs__label`），不断 li 的 textContent——后者会把 × 的文本一起拼进来。
    expect(w.findAll('.shell-tabs__label').map((i) => i.text())).toEqual([
      '后台首页',
      '管理订单',
      '游戏管理',
    ])
    expect(items.map((i) => i.classes().includes('shell-tabs__item--active'))).toEqual([
      false,
      true,
      false,
    ])
  })

  it('不可关的页签（首页）没有 ×，可关的有', () => {
    const w = mountBar()
    const items = w.findAll('.shell-tabs__item')

    expect(items[0]?.find('.shell-tabs__close').exists()).toBe(false)
    expect(items[1]?.find('.shell-tabs__close').exists()).toBe(true)
    expect(w.findAll('.shell-tabs__close')).toHaveLength(2)
  })

  it('点页签发 select（参数是 key），不发 close', async () => {
    const w = mountBar()
    await w.findAll('.shell-tabs__item')[1]?.trigger('click')

    expect(w.emitted('select')).toEqual([['/orders']])
    expect(w.emitted('close')).toBeUndefined()
  })

  it('点 × 发 close（参数是 key）且**不**连带发 select（@click.stop）', async () => {
    const w = mountBar()
    await w.findAll('.shell-tabs__close')[0]?.trigger('click')

    expect(w.emitted('close')).toEqual([['/orders']])
    expect(w.emitted('select'), '× 的点击冒泡到了 li，会顺手切一次页签').toBeUndefined()
  })

  it('每个控件都是 type=button（默认的 submit 在表单里会变成一次导航）', () => {
    const w = mountBar()
    for (const b of w.findAll('button')) expect(b.attributes('type')).toBe('button')
  })
})

describe('AppTabsBar 的 ▾ 下拉（三个批量关闭）', () => {
  it('默认收起；点 ▾ 展开，文案逐字取对标 i18n', async () => {
    const w = mountBar()
    expect(w.find('.shell-tabs__menu').exists()).toBe(false)

    await w.get('.shell-tabs__more-toggle').trigger('click')

    expect(w.findAll('.shell-tabs__menu-item').map((b) => b.text())).toEqual([
      '关闭当前标签页',
      '关闭其它标签页',
      '关闭全部标签页',
    ])
    expect(w.get('.shell-tabs__more-toggle').attributes('aria-expanded')).toBe('true')
  })

  it('三个菜单项各自发一条事件、参数是当前激活键，并发完就收起菜单', async () => {
    const w = mountBar('/orders')
    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[0]?.trigger('click')

    expect(w.emitted('close')).toEqual([['/orders']])
    expect(w.find('.shell-tabs__menu').exists(), '点完菜单项没有收起').toBe(false)

    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[1]?.trigger('click')
    expect(w.emitted('closeOthers')).toEqual([['/orders']])

    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[2]?.trigger('click')
    expect(w.emitted('closeAll')).toHaveLength(1)
  })
})

describe('AppTabsBar 的刷新（语义未改）', () => {
  it('点刷新恰好发出一次 refresh', async () => {
    const w = mountBar()
    expect(w.emitted(), '挂载时不该有任何 emit').toEqual({})

    await w.get('.shell-tabs__refresh').trigger('click')

    expect(w.emitted('refresh')).toHaveLength(1)
    // ⚠️ 这里必须滤掉 `trigger()` 自己的事件名：实测 VTU 把**被 trigger 的原生事件**一并记进
    //   `emitted()`，所以原始键集里会有 `click`。滤掉后剩下的才是「组件自己发出的事件面」——
    //   将来多出第五个事件（哪怕只发一次）都会在这里红。
    const dispatchedByTrigger = ['click']
    // ⚠️ 已知窄缝（上一轮的评审 Minor，**未声称覆盖**）：这条过滤是按**事件名**做的，所以组件
    //   将来若真的 `emit('click')`，它会被一并滤掉、这条断言看不见。
    expect(
      Object.keys(w.emitted()).filter((name) => !dispatchedByTrigger.includes(name)),
      '除 refresh 外还发出了别的组件事件',
    ).toEqual(['refresh'])
  })

  it('刷新/切页签/关页签的点击都不导航：location 没被写、router 的 go/push/replace 没被调', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const go = vi.spyOn(router, 'go')
    const push = vi.spyOn(router, 'push')
    const replace = vi.spyOn(router, 'replace')

    const location = installLocationSpy()
    restoreLocation = location.restore

    const w = mount(AppTabsBar, {
      props: { tabs: TABS, activeKey: HOME_TAB_KEY },
      global: { plugins: [router] },
    })
    await w.get('.shell-tabs__refresh').trigger('click')
    await w.findAll('.shell-tabs__item')[1]?.trigger('click')
    await w.findAll('.shell-tabs__close')[0]?.trigger('click')
    await w.get('.shell-tabs__more-toggle').trigger('click')
    await w.findAll('.shell-tabs__menu-item')[2]?.trigger('click')

    expect(w.emitted('refresh')).toHaveLength(1)
    expect(w.emitted('select')).toEqual([['/orders']])
    expect(location.touched, '页签条自己碰了 window.location').toEqual([])
    expect([go.mock.calls.length, push.mock.calls.length, replace.mock.calls.length]).toEqual([0, 0, 0])
  })
})

describe('AppTabsBar 的「清空标签页」按钮（业务方 2026-09-23 新增，对标没有这一颗）', () => {
  it('渲染在最左侧（DOM 里排在刷新按钮与页签列表之前）', () => {
    const w = mountBar()
    const bar = w.get('.shell-tabs')
    const order = Array.from(bar.element.children).map((el) => el.className)
    const clearIdx = order.findIndex((c) => c.includes('shell-tabs__clear'))
    const refreshIdx = order.findIndex((c) => c.includes('shell-tabs__refresh'))
    const listIdx = order.findIndex((c) => c.includes('shell-tabs__list'))
    expect(clearIdx).toBe(0)
    expect(clearIdx).toBeLessThan(refreshIdx)
    expect(clearIdx).toBeLessThan(listIdx)
  })

  it('文案是「清空标签页」、带补充说明 title、type=button', () => {
    const w = mountBar()
    const btn = w.get('.shell-tabs__clear')
    expect(btn.text()).toBe('清空标签页')
    // ⚠️ 用 `title` 承载「所有」这个补充措辞，**不要**用 `aria-label`：两者并用且措辞不同时，
    // 无障碍名会被 `aria-label` 取代，而它必须**包含**可见文字（WCAG 2.5.3 Label in Name）——
    // 「清空所有标签页」不含「清空标签页」这个连续子串。只留可见文字当无障碍名最省事也最正确。
    expect(btn.attributes('title')).toBe('清空所有标签页')
    expect(btn.attributes('aria-label')).toBeUndefined()
    expect(btn.attributes('type')).toBe('button')
  })

  it('点它只发一次 closeAll（复用既有事件，不新增事件面）', async () => {
    const w = mountBar('/orders')
    expect(w.emitted(), '挂载时不该有任何 emit').toEqual({})
    await w.get('.shell-tabs__clear').trigger('click')
    expect(w.emitted('closeAll')).toHaveLength(1)
    expect(w.emitted('close')).toBeUndefined()
    expect(w.emitted('closeOthers')).toBeUndefined()
  })

  it('点它不导航：location 没被写、router 的 go/push/replace 没被调', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const go = vi.spyOn(router, 'go')
    const push = vi.spyOn(router, 'push')
    const replace = vi.spyOn(router, 'replace')
    const location = installLocationSpy()
    restoreLocation = location.restore

    const w = mount(AppTabsBar, {
      props: { tabs: TABS, activeKey: HOME_TAB_KEY },
      global: { plugins: [router] },
    })
    await w.get('.shell-tabs__clear').trigger('click')

    expect(w.emitted('closeAll')).toHaveLength(1)
    expect(location.touched, '页签条自己碰了 window.location').toEqual([])
    expect([go.mock.calls.length, push.mock.calls.length, replace.mock.calls.length]).toEqual([0, 0, 0])
  })
})
