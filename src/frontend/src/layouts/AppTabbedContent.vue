<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, watch, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { PageTab } from '@/composables/usePageTabs'

/**
 * **页签缓存的内容区**（D-0220）：切菜单再回来，编辑中的内容不丢。机制**照对标物**（layuiadmin iframe 版的 `openTabsPage`）：每个打开的页签对应一个常驻挂载层，切换只切 `v-show`——**iframe 的 DOM 从不移动、从不移除**，岛内未保存的表单状态原样保留；关页签才把层整个卸载。
 * 为什么不用 `<KeepAlive>`：KeepAlive 脱挂时会把 DOM **搬进**离屏容器，而 iframe 脱离文档再插回会被浏览器当作重新挂载（丢内容）——复刻对标「常驻层 + display 切换」正是绕开这一点；且 KeepAlive 的 `include` 按**组件名**匹配、全部复刻路由共用 `LayuiIslandHost` 一个组件，做不了「按页签逐个丢缓存」。
 * ⚠️ 已知边界（D-0220 登记）：被缓存页面的 JS 定时器**仍在跑**（隐藏 iframe 的 window 不销毁），有轮询的页会在后台继续发请求（回来时数据反而是新的）；内存随打开页签数增长，由「关页签即销毁」兜底。层里的组件不经 `RouterView` 挂载 ⇒ **组件内路由钩子**（`beforeRouteLeave` / `beforeRouteUpdate`）在缓存层里不会触发——今天壳内全部内容路由都是 `LayuiIslandHost`（无组件级钩子），新加壳内路由时要知道这一点。
 */
const props = defineProps<{
  /** 打开着的页签（key = fullPath）；缓存层与它一一对应，关页签即销毁。 */
  tabs: readonly PageTab[]
  /** AppShell「刷新」按钮的自增计数：每加一 = 只丢弃**当前**页签的缓存并重挂（别的页签不动）。 */
  refreshTick: number
}>()

const route = useRoute()
const router = useRouter()

/**
 * 冻结给缓存组件的路由上下文（`LayuiIslandHost::frozenRoute` 收）：每签一份，
 * 不跟当前路由走——否则切一次菜单，所有缓存页的 iframe 会被一起导航到新页。
 */
type FrozenRoute = {
  fullPath: string
  meta: { islandPage?: unknown; title?: unknown }
  params: Record<string, unknown>
  query: Record<string, unknown>
}

type Layer = {
  /** 页签键 = 路由 fullPath。 */
  key: string
  /** 该路由要渲染的组件；`null` 只剩理论可能（见 `layers` 里的注释）。 */
  component: Component | null
  /** island 路由给宿主冻结上下文；非 island 路由（如 `not-found`）不传。 */
  frozenRoute: FrozenRoute | undefined
}

const layers = computed<Layer[]>(() =>
  props.tabs.map((tab) => {
    // `resolve` 不跑守卫（真实导航时已跑过），只把 fullPath 解析回「组件 + meta + params + query」。
    const resolved = router.resolve(tab.key)
    const leaf = resolved.matched[resolved.matched.length - 1]
    const raw = leaf?.components?.default
    // 页签只会登记「真实导航过」的路由，而 vue-router 首次导航就会把懒加载组件解析结果**回写**进路由记录（`extractComponentsGuards` 里 `record.components[name] = resolvedComponent`）⇒ 这里拿到的必然是已解析的对象组件；
    // 函数只剩「未访问过」的理论可能（今天全部路由组件都是 SFC 对象），兜底为不渲染。
    const component = raw != null && typeof raw === 'object' ? (raw as Component) : null
    const frozenRoute: FrozenRoute | undefined =
      resolved.meta.islandPage != null
        ? {
            fullPath: resolved.fullPath,
            meta: { islandPage: resolved.meta.islandPage, title: resolved.meta.title },
            params: { ...resolved.params },
            query: { ...resolved.query },
          }
        : undefined
    return { key: tab.key, component, frozenRoute }
  }),
)

/**
 * 当前路由没有对应页签时的兜底：照旧由 `RouterView` 直挂、**不缓存**。
 * 今天只会发生在「标题与路由名都为空的匿名路由」（`AppShell` 的登记规则不登记它们，
 * 全仓目前没有这种路由），留着它是为了不让这类路由从「能渲染」变成「白屏」。
 */
const hasLayerForCurrent = computed(() => props.tabs.some((t) => t.key === route.fullPath))

/**
 * 「刷新」= 只丢弃当前页签的缓存：给当前 fullPath 累加一代号，层的 `v-for` key 随之
 * 变化 ⇒ 旧层子树卸载（iframe 销毁）、新层重挂。别的页签的代号不动 ⇒ 缓存不受牵连
 * （旧实现是整个 `<main>` 换 `:key`，一刷新所有页签的缓存全没了）。
 */
const generations = reactive(new Map<string, number>())
watch(
  () => props.refreshTick,
  () => {
    generations.set(route.fullPath, (generations.get(route.fullPath) ?? 0) + 1)
  },
)
function layerKey(tabKey: string): string {
  return `${tabKey}::${generations.get(tabKey) ?? 0}`
}

/**
 * `shell-flush-content`（去掉 `.shell-content` 那 16px 内边距）原先由 `LayuiIslandHost` 挂载/卸载时加删；页签缓存后宿主**不再卸载**，改由本容器按「当前路由是不是 island 页」统一管理。必须**单一 owner**：多个缓存宿主各自加删同一个 body class 会有先后覆盖问题（A 恢复、B 移除的顺序不定）。登录页不在壳里，不经过本容器。
 */
const activeIsIsland = computed(() => route.meta.islandPage != null)
watch(
  activeIsIsland,
  (on) => {
    document.body.classList.toggle('shell-flush-content', on)
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  document.body.classList.remove('shell-flush-content')
})
</script>

<template>
  <!-- `data-tab-layer` 给测试与排查用：层与页签键（fullPath）一一对应。 -->
  <div
    v-for="layer in layers"
    :key="layerKey(layer.key)"
    v-show="layer.key === route.fullPath"
    class="shell-tabsbody-item"
    :data-tab-layer="layer.key"
  >
    <component :is="layer.component" v-if="layer.component" :frozen-route="layer.frozenRoute" />
  </div>
  <RouterView v-if="!hasLayerForCurrent" />
</template>
