// 纯逻辑测试：默认 environment 是 `node`（vitest.config.ts），本文件不碰 DOM、不建路由。
// 页签的增 / 删 / 切三条规则都在这里钉住——组件测试只断言「渲染与发事件」，
// 把规则放在一处才不会出现「组件里一套、composable 里另一套」。
import { describe, expect, it } from 'vitest'
import { HOME_TAB_KEY, usePageTabs } from '../usePageTabs'

/** 造一条页签输入。`title` 用 key 派生，方便断言「没串行」。 */
const t = (key: string) => ({ key, title: `标题${key}` })

describe('usePageTabs 的打开（自动新增 + 去重）', () => {
  it('首次打开：加一条并激活它', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t('/orders'))

    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/orders'])
    expect(tabs.activeKey.value).toBe('/orders')
    expect(tabs.activeTab.value?.title).toBe('标题/orders')
  })

  it('重复打开同一条只激活、不追加（对标 openTabsPage 的 matchTo 分支）', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t('/'))
    tabs.openPageTab(t('/orders'))
    tabs.openPageTab(t('/orders'))

    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/orders'])
    expect(tabs.activeKey.value).toBe('/orders')
  })

  it('键是 fullPath 语义：同一路由不同参数是两条页签', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t('/orders/1'))
    tabs.openPageTab(t('/orders/2'))

    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/orders/1', '/orders/2'])
  })

  it('首页那条标成不可关，其余可关', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t(HOME_TAB_KEY))
    tabs.openPageTab(t('/orders'))

    expect(tabs.tabs.value.map((x) => [x.key, x.closable])).toEqual([
      ['/', false],
      ['/orders', true],
    ])
  })
})

describe('usePageTabs 的关闭（先右后左）', () => {
 /** 三条页签：首页 + /a + /b。 */
  function three() {
    const tabs = usePageTabs()
    tabs.openPageTab(t(HOME_TAB_KEY))
    tabs.openPageTab(t('/a'))
    tabs.openPageTab(t('/b'))
    return tabs
  }

  it('关掉当前激活页签：切到右边那个', () => {
    const tabs = three()
    tabs.openPageTab(t('/a')) // 激活中间那条

    expect(tabs.closePageTab('/a')).toBe('/b')
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/b'])
    expect(tabs.activeKey.value).toBe('/b')
  })

  it('关掉当前激活页签且它是最右：切到左边那个', () => {
    const tabs = three() // /b 是激活的（最后打开的）

    expect(tabs.closePageTab('/b')).toBe('/a')
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/a'])
  })

  it('关掉非激活页签：当前页签不动', () => {
    const tabs = three() // 激活 /b

    expect(tabs.closePageTab('/a')).toBe('/b')
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/b'])
    expect(tabs.activeKey.value).toBe('/b')
  })

  it('左边那个可以是不可关的首页（对标 tabDelete 的邻居就是 DOM 兄弟，不过滤可关性）', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t(HOME_TAB_KEY))
    tabs.openPageTab(t('/a')) // 激活 /a，它右边没有别的页签

    expect(tabs.closePageTab('/a')).toBe(HOME_TAB_KEY)
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/'])
    expect(tabs.activeKey.value).toBe(HOME_TAB_KEY)
  })

  it('首页关不掉：键不删、激活键不变（三处对标依据见 usePageTabs.ts 的 HOME_TAB_KEY）', () => {
    const tabs = three()
    tabs.openPageTab(t(HOME_TAB_KEY))

    expect(tabs.closePageTab(HOME_TAB_KEY)).toBe(HOME_TAB_KEY)
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/a', '/b'])
  })

  it('关掉的键不在列表里：原样返回当前激活键，不抛', () => {
    const tabs = three()
    expect(tabs.closePageTab('/nope')).toBe('/b')
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/a', '/b'])
  })

  it('只剩一条可关页签时关掉它：返回空串（调用方据此回首页）', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t('/orders'))

    expect(tabs.closePageTab('/orders')).toBe('')
    expect(tabs.tabs.value).toEqual([])
    expect(tabs.activeTab.value).toBeNull()
  })
})

describe('usePageTabs 的批量关闭（对标 admin.js 的 closeOtherTabs / closeAllTabs）', () => {
  function three() {
    const tabs = usePageTabs()
    tabs.openPageTab(t(HOME_TAB_KEY))
    tabs.openPageTab(t('/a'))
    tabs.openPageTab(t('/b'))
    return tabs
  }

  it('关闭其它：保留首页与当前，当前不变', () => {
    const tabs = three()
    tabs.openPageTab(t('/a'))

    expect(tabs.closeOtherPageTabs('/a')).toBe('/a')
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/', '/a'])
  })

  it('关闭全部：只剩首页，并停在首页', () => {
    const tabs = three()

    expect(tabs.closeAllPageTabs()).toBe(HOME_TAB_KEY)
    expect(tabs.tabs.value.map((x) => x.key)).toEqual(['/'])
    expect(tabs.activeKey.value).toBe(HOME_TAB_KEY)
  })

  it('关闭全部且首页没被打开过（深链场景）：列表空、返回空串', () => {
    const tabs = usePageTabs()
    tabs.openPageTab(t('/a'))

    expect(tabs.closeAllPageTabs()).toBe('')
    expect(tabs.tabs.value).toEqual([])
  })
})
