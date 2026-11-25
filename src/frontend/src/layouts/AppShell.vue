<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppTabsBar from '@/components/AppTabsBar.vue'
import AppTabbedContent from '@/layouts/AppTabbedContent.vue'
import AppSidebar from './AppSidebar.vue'
import AppTopbar from './AppTopbar.vue'
import { BREAKPOINTS } from '@/config/breakpoints'
import { HOME_TAB_KEY, usePageTabs } from '@/composables/usePageTabs'

const SITE_TITLE = '团团通 · 租户后台'
// ⚠️ 顶栏右区的身份**不由本组件拼**：AppTopbar 自己读会话（`tenant_name` / `display_name` / `role`），无会话时右区留空；`accountLabel` prop 已随占位常量一并删除（spec §7.5）。

const sidebarOpen = ref(false)

// 窗口宽度必须**持续跟踪**：只在初始化时读一次的话，用户拖动窗口跨过断点后侧栏不会切换到收起态（真会发生的 bug）。
// 收起态只有 AppShell 一个 owner：`.shell--collapsed` 需要三块区域的共同祖先才能命中，所以侧栏不再自己读窗口宽度、改用 prop 收下。
const windowWidth = ref(window.innerWidth)
const collapsed = computed(
  () => windowWidth.value <= BREAKPOINTS.compactDesktopMax && windowWidth.value > BREAKPOINTS.tabletMax,
)

function toggle() {
  sidebarOpen.value = !sidebarOpen.value
}

function onResize() {
  windowWidth.value = window.innerWidth
  // 抽屉只在平板及以下有意义；放大回桌面时关掉，避免残留状态
  if (windowWidth.value > BREAKPOINTS.tabletMax) sidebarOpen.value = false
}
onMounted(() => window.addEventListener('resize', onResize))
onUnmounted(() => window.removeEventListener('resize', onResize))

// 页签条的标题：路由 meta.title（见 src/router/index.ts）；取不到就回退到路由名（设计 spec §5.1 第 1 条）。
// ⚠️ 这个回退**不是**「结果一定非空」：`route.name` 也是 undefined 的匿名路由会解析出空标题。今天所有路由都有 name、取不到那种路由，所以这是**刻意留着**的边界；不擅自改用 `route.path` 之类兜底——那是规范里没有的行为，要改先改 spec。本函数是登记页签时的唯一消费者（组件不再自己解析标题）。
const route = useRoute()
function resolveTitle(): string {
  const t = route.meta.title
  return typeof t === 'string' && t !== '' ? t : String(route.name ?? '')
}

// ── 页签条（业务方 2026-09-20 更新口径：对标物的机制，不再只画当前页签）──────────────
// 状态全在 composable 里，本组件只做两件事：**登记**与**导航**。
const router = useRouter()
const { tabs, activeKey, openPageTab, closePageTab, closeOtherPageTabs, closeAllPageTabs } =
  usePageTabs()

// **自动新增**：每次路由落到新地址就登记一条页签。用 `watch(fullPath)` 而不是往 `router/index.ts` 挂 `afterEach`——`fullPath` 变化与「一次成功导航」在本壳里等价（导航失败时 vue-router 不更新当前路由，watcher 不触发）。
// 去重键为什么是 fullPath 而不是 route.name，见 `composables/usePageTabs.ts` 的 `PageTab.key`。
watch(
  () => route.fullPath,
  () => {
    const title = resolveTitle()
    // 标题为空的（匿名路由）不登记：画出来是一条没有文字的空白页签。
    if (title !== '') openPageTab({ key: route.fullPath, title })
  },
  { immediate: true },
)

/**
 * 页签动作 -> 导航。键为空串表示「列表已空」（例如关掉了最后一条可关页签），回首页。**一律走 `router.push` 的正常导航**：`LayuiIslandHost` 的 `islandSrc` 已改成 `computed`，island→island 切换靠路由变化驱动那个计算属性；绕开路由直接改 iframe src 会重新引入「点了没反应还停在原页面」那类问题。目标与当前 `fullPath` 相同时不重复 push（对标物点自己那一页也不做什么）。
 */
function goToTab(keyOrEmpty: string): void {
  const key = keyOrEmpty === '' ? HOME_TAB_KEY : keyOrEmpty
  if (key === route.fullPath) return
  void router.push(key)
}

// 刷新＝丢弃**当前页签**的缓存并重挂（D-0220 起语义收窄）：本组件只把计数 +1，由 `AppTabbedContent` 按「给当前 fullPath 累加代号」只重挂当前层——旧实现是给整个 `<main>` 换 `:key`，一刷新**所有**页签的缓存全没了。
// 不碰 `window.location`（那会整页重载、丢掉 SPA 的状态）。AppTabsBar 只发事件、执行在这里（设计 spec §5.1 第 2 条：刷新方式将来可换而不必动那个组件）。
const contentKey = ref(0)
function refreshContent() {
  contentKey.value += 1
}
</script>

<template>
  <div class="shell" :class="{ 'shell--collapsed': collapsed }">
    <AppTopbar :title="SITE_TITLE" @toggle="toggle" />
    <AppSidebar :open="sidebarOpen" :collapsed="collapsed" />
    <AppTabsBar
      :tabs="tabs"
      :active-key="activeKey"
      @refresh="refreshContent"
      @select="goToTab"
      @close="(key) => goToTab(closePageTab(key))"
      @close-others="(key) => goToTab(closeOtherPageTabs(key))"
      @close-all="goToTab(closeAllPageTabs())"
    />
    <!-- 内容区 = 页签缓存容器（D-0220）：每个打开的页签一个常驻挂载层，切页签只切
         v-show、关页签才销毁——「切菜单再回来，编辑中的内容不丢」。 -->
    <main class="shell-content">
      <AppTabbedContent :tabs="tabs" :refresh-tick="contentKey" />
    </main>
  </div>
</template>
