import { computed, ref, type ComputedRef, type Ref } from 'vue'

/** 页签条（`AppTabsBar`）的**纯逻辑**：打开列表 + 增 / 删 / 切换。组件只渲染与发事件、`AppShell` 只负责把结果接到 `router.push` 上，状态全在本文件里（业务方 的要求：状态不塞进 `AppShell` 的模板）。这样增删切三种规则的单测不依赖 DOM，也不需要造路由。⚠️ 本文件里**没有** `router`、没有 `window.location`：导航一律由调用方执行。 */
export type PageTab = {
  /** 去重键，同时也**就是切换时的 push 目标**（都是路由的 `fullPath`，含 params 与 query）。去重键取 `fullPath`、不取 `route.name`，依据是对标物的机制（读码）：`layuiadmin_index.js:22-36`的 `openTabsPage(url, text)` 把页签的去重键定为 **完整 url**——它遍历 `#LAY_app_tabsheader>li`逐个比对 `li.attr('lay-id') === url`，而 `lay-id` 就是打开时传进来的那个 url（`element.tabAdd(..., {id: url})`，同文件 49-53 行）。所以 `/orders/1` 与 `/orders/2` 是**两条**页签，而用 `route.name` 做键会把它们并成一条（订单详情这类带 params 的路由是本项目的真实场景，任务要求「保留必要的 params」）。⚠️ **一处已登记的偏离**：对标物切换时用的是**去掉 query 的路径**——它给每个 `<li>` 存了两个属性，
   * `lay-id` = 完整 url（去重）、`lay-attr` = `url.replace(/(^http(s*):)|(\?[\s\S]*$)/g, '')`（`layuiadmin_index.js:26`），`admin.js` 的 `setThisRouter` 读 `lay-attr` 去同步 iframe。我们**不照抄这一半**：它的 iframe 页体已经在那个 tab 里，按 `lay-attr` 同步不会新建页签；而我们的 SPA 若「键含 query、push 不含 query」，push 完路由的 `fullPath` 既不等于当前页签的键、又会被下面的 `AppShell` 观察器当成一次新导航 ⇒ **每切一次就多一个幽灵页签**。所以切换也用 `fullPath`（params 与 query 都原样回去）。代价：对标物「切回去丢掉 query」的行为我们没有复刻——这一条要改只需改 `AppShell` 的 push 目标，键不用动。 */
  key: string
  /** 页签文字。对标 `lay-text`；`AppShell` 从 `route.meta.title`（取不到时回退 `route.name`）解析。 */
  title: string
  /** 是否有关闭按钮。对标物给每个 `<li>` 加 `lay-closable="false"` 才不可关，
   * 而**首签（后台首页）三条独立依据都指向不可关**，见 `HOME_TAB_KEY`。 */
  closable: boolean
}

/** 首页（`/`，页签文案「后台首页」）的键。**不可关**，依据（三处独立、都是对标物自己的实现）：1. `admin.js` 的 `closeThisTabs` 第一行就是 `if(!admin.tabsPage.index) return;`—— 当前是第 0 个页签时，「关闭当前标签页」**直接返回**；2. 同一文件的 `closeOtherTabs` 只移除 `index && index != admin.tabsPage.index` 的项、`closeAllTabs` 只移除 `$('#LAY_app_tabsheader>li:gt(0)')` 并 `$(...).eq(0).trigger('click')`—— **两个批量关闭都保留第 0 个页签**，且「关闭全部」之后停在它上面；3. 样式层 `.layadmin-pagetabs .layui-tab-title li:first-child .layui-tab-close{display:none}`（`.tools/recon/css/admin.css`）—— 首签那个 `×` **根本不显示**。
 * 我们此前只知道「首页通常不可关」，这三条是去对标产物里核出来的实际依据，不是惯例。⚠️ 注意它是**按键值**判的（首页 = `/`），不是按「列表里第一个」判的：对标物的首签是服务端渲染死在壳里的，我们的首页可能不是用户打开的第一个页签（直接深链到别的路由时）。 */
export const HOME_TAB_KEY = '/'

export type PageTabs = {
  /** 已打开的页签，按打开顺序。首页永远排在最前（第一次进入 `/` 时建立）。 */
  tabs: Ref<PageTab[]>
  /** 当前激活页签的键；空串表示「当前路由不在列表里」（深链到未登记的页面）。 */
  activeKey: Ref<string>
  activeTab: ComputedRef<PageTab | null>
  /** 登记一条页签并激活它。**已存在则只激活、不追加**（对标 `openTabsPage` 的 `matchTo` 分支）。 */
  openPageTab: (input: { key: string; title: string }) => void
  /** 关闭一条页签，返回关闭后应当激活的键。**返回空串表示列表已空**，由调用方回首页。 */
  closePageTab: (key: string) => string
  /** 关闭「其它」页签（保留不可关的那些 + `key`），返回新的激活键。 */
  closeOtherPageTabs: (key: string) => string
  /** 关闭全部可关页签（只剩不可关的那些），返回新的激活键。 */
  closeAllPageTabs: () => string
}

export function usePageTabs(): PageTabs {
  const tabs = ref<PageTab[]>([])
  const activeKey = ref('')

  const activeTab = computed<PageTab | null>(
    () => tabs.value.find((t) => t.key === activeKey.value) ?? null,
  )

  function openPageTab(input: { key: string; title: string }): void {
    if (!tabs.value.some((t) => t.key === input.key)) {
      // 标题为空串的页签不画（调用方会先过滤）——但这里不替调用方做判据，避免两处规则。
      tabs.value.push({
        key: input.key,
        title: input.title,
        closable: input.key !== HOME_TAB_KEY,
      })
    }
    activeKey.value = input.key
  }

  /** 关闭页签时的**切换规则**取自对标物 layui 的 `element` 模块（读码，`layui.js` 的 `tabDelete`）：`t.hasClass(CLASS_THIS) && (t.next()[0] && t.next().is('li')? o.tabClick.call(t.next()[0], {index: a + 1})
   * : t.prev()[0] && t.prev().is('li') && o.tabClick.call(t.prev()[0], null, a - 1))`
   * 即：**只有被关的是当前激活页签时才换页**，换到**右边那个**；没有右边就换**左边那个**；两个都没有（列表空了）就不换。关掉一个**非激活**页签时当前页签不动。邻居取的是 DOM 兄弟，所以可以是不可关的首页——照抄，不做过滤。 */
  function closePageTab(key: string): string {
    const index = tabs.value.findIndex((t) => t.key === key)
    if (index === -1) return activeKey.value

    const tab = tabs.value[index]
    // 首页不可关（依据见 `HOME_TAB_KEY`）：直接返回，不删、不换页。
    if (tab?.closable === false) return activeKey.value

    const wasActive = activeKey.value === key
    // ⚠️ 邻居必须在**移除之前**取：移除后 `index + 1` 已经指向别人了。
    const neighbour = tabs.value[index + 1] ?? tabs.value[index - 1] ?? null
    tabs.value.splice(index, 1)

    if (wasActive) activeKey.value = neighbour?.key ?? ''
    return activeKey.value
  }

  /** 「关闭其它标签页」：对标 `admin.js` 的 `closeOtherTabs`——
   * `if(index && index != admin.tabsPage.index){ …remove… }`
   * 即**保留第 0 个（首页）与当前**，其余移除；它**不调用 change**，所以当前页签不变。 */
  function closeOtherPageTabs(key: string): string {
    tabs.value = tabs.value.filter((t) => !t.closable || t.key === key)
    activeKey.value = tabs.value.some((t) => t.key === activeKey.value)
      ? activeKey.value
      : (tabs.value.find((t) => t.key === key)?.key ?? tabs.value[0]?.key ?? '')
    return activeKey.value
  }

  /** 「关闭全部标签页」：对标 `admin.js` 的 `closeAllTabs` → `closeOtherTabs('all')`——
   * `$(TABS_HEADER + ':gt(0)').remove(); …; $(TABS_HEADER).eq(0).trigger('click');`
   * 即移除首页之后的全部、并**停在首页**。所以新的激活键是列表里的第一个（也就是首页）。 */
  function closeAllPageTabs(): string {
    tabs.value = tabs.value.filter((t) => !t.closable)
    activeKey.value = tabs.value[0]?.key ?? ''
    return activeKey.value
  }

  return {
    tabs,
    activeKey,
    activeTab,
    openPageTab,
    closePageTab,
    closeOtherPageTabs,
    closeAllPageTabs,
  }
}
