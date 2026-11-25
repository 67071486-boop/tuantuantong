<script setup lang="ts">
/**
 * **layui island 的通用宿主**：所有「1:1 复刻页」的路由都用它，页面 key 从 `route.meta.islandPage` 取（见 `docs/FRONTEND-REPLICA-STANDARD.md` §2）。只做三件事：① 渲染 iframe（`/layui-island/page.html?page=<key>`）；② 收 island 的 `postMessage`：跳转请求交给 vue-router、头像更新请求重取会话用户（两条分支都在**同源校验**与**激活门**之后）；③ 接受页签缓存层（`AppTabbedContent`）传入的**冻结路由上下文**（D-0220）。
 * ⚠️ `shell-flush-content`（去掉壳内容区 16px 内边距的 body class）**不再由本组件管**：D-0220 页签缓存后宿主不再卸载，class 改由 `AppTabbedContent` 按「当前路由是不是 island 页」统一加删（单一 owner，避免多个缓存宿主互相覆盖）。
 */
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AVATAR_UPDATED_MESSAGE_TYPE } from '@/layui-island/messageTypes'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

/**
 * **冻结的路由上下文**（D-0220）：本宿主会被页签缓存层**每个页签各缓存一份**，每份的页面 key / params / query / 标题都必须钉死在自己那签上、不跟当前路由走——否则切一次菜单，所有缓存页的 iframe 会被一起导航到新页（缓存就废了）。
 * 不传（`RouterView` 直挂 / 组件测试）时回退到当前路由，行为与 D-0220 之前一致。
 */
const props = defineProps<{
  frozenRoute?: {
    fullPath: string
    meta: { islandPage?: unknown; title?: unknown }
    params: Record<string, unknown>
    query: Record<string, unknown>
  }
}>()

/** 本宿主读路由的唯一入口：有冻结上下文用冻结的，否则用当前路由。 */
const ctx = computed(() => props.frozenRoute ?? route)

/** 本层是否就是当前页签：D-0220 后同文档会有**多个缓存宿主并存**，消息与 testid 只认激活的那签。 */
const isActive = computed(() => !props.frozenRoute || route.fullPath === props.frozenRoute.fullPath)

/** 允许透进 iframe 的 query 白名单（只有一处可审；陌生参数不进页面，spec §7.4）。 */
const FORWARDED_QUERY = ['tab'] as const

/**
 * ⚠️ **必须是 `computed`，不能算一次**（实测踩过）：island → island 切换时，vue-router 认为**组件相同**（都是本组件）于是**复用实例、不重挂载**——若 `src` 在 setup 里只求值一次，`iframe` 会停在第一个页面，症状正是「点菜单没反应、还停留在原页面」（EP → island 因为换了组件会重挂载，那种切换正常）。
 * 依赖上下文变化后重建 src，iframe 才真的换页。D-0220 起上下文 = `ctx`（冻结优先），「直挂形态跟路由走、缓存形态钉在自己那签」两种行为都从这一个 computed 出。
 */
const islandSrc = computed(() => {
  const pageKey = String(ctx.value.meta.islandPage ?? '')
  const params = new URLSearchParams({ page: pageKey })
  // 把路由参数一并传给 island：需要「新增 vs 编辑」这类上下文的页面（如 `user-adduser` 对应 `/system-users/:id/edit`）就不必去猜——`document.referrer` 在同源 iframe 下能解析，但**跨源会被裁成 origin**、静默退化成「新增」；显式拼 query 才是可靠来源（页面模块仍可保留 referrer 作兜底）。
  for (const [key, value] of Object.entries(ctx.value.params ?? {})) {
    if (typeof value === 'string' && value !== '') params.set(key, value)
  }
  // query 走**白名单**（`?tab=password` 深链，spec §7.4）：只有 `tab` 会透进 iframe，上下文里其余参数一概不进页面（页面对陌生参数没有判据）。代价：将来别的页要深链别的 query，得往这个数组加一项。
  for (const key of FORWARDED_QUERY) {
    const value = ctx.value.query?.[key]
    if (typeof value === 'string' && value !== '') params.set(key, value)
  }
  const batch = ctx.value.query?.batch
  if (pageKey === 'emp-onlinepaytorecdetail' && typeof batch === 'string') params.set('batch', batch)
  return `${import.meta.env.BASE_URL}layui-island/page.html?${params.toString()}`
})

type IslandMessage = { type?: string; name?: string; params?: Record<string, string> }

function onIslandMessage(event: MessageEvent): void {
  // **激活门**（D-0220）：被缓存的非当前页签**不响应** island 消息——它的 iframe 还活着
  // （后台定时器仍在跑，见 D-0220 的已知代价），但跳转 / 头像刷新只认用户正在看的那一签；
  // 否则一个后台页的消息会把用户当前视图切走、或触发重复取数。
  if (!isActive.value) return
  // 只认同源消息（iframe 与父窗口同源）。⚠️ 这条校验**对两个分支都生效**，
  // 且必须排在类型分支之前——跨源窗口不得借任何一种消息类型触发副作用。
  if (event.origin !== window.location.origin) return
  const data = event.data as IslandMessage | undefined
  if (!data) return
  // 顶栏头像同步线：岛页在头像**认领成功后**发这条 ⇒ 重取会话用户（⛔ 壳不自己拼 URL，走会话 store 既有的 `refresh()` → `fetchMe()`），`AppSidebar` 的 `:avatar-url` 随即换成新头像。
  // ⚠️ **只认这一种类型**：未知类型一律落到下面那条 `island:navigate` 判断之后被忽略（⛔ 别把刷新提到类型判断之前——那会变成「任何消息都刷新」）。
  if (data.type === AVATAR_UPDATED_MESSAGE_TYPE) {
    void session.refresh()
    return
  }
  if (data.type !== 'island:navigate' || !data.name) return
  void router.push(data.params ? { name: data.name, params: data.params } : { name: data.name })
}

onMounted(() => {
  window.addEventListener('message', onIslandMessage)
})
onBeforeUnmount(() => {
  window.removeEventListener('message', onIslandMessage)
})
</script>

<template>
  <!-- 高度 = 100vh − 内容区 top（顶栏 50 + 页签条 40 = 90，`docs/FRONTEND-LAYOUT.md` §5.1 的冻结值）。 -->
  <!-- `data-testid` 只挂在**激活层**的 iframe 上（D-0220）：页签缓存后同文档会有多个 iframe 并存（隐藏的缓存层换 `layui-island--cached`），e2e / 组件测试的选择器 `iframe[data-testid="layui-island"]` 因此始终恰好命中「用户正在看的那一页」。 -->
  <iframe
    class="layui-island-frame"
    :src="islandSrc"
    :title="String(ctx.meta.title ?? '页面')"
    :data-testid="isActive ? 'layui-island' : 'layui-island--cached'"
  />
</template>

<style scoped>
.layui-island-frame {
  display: block;
  width: 100%;
  height: calc(100vh - 90px);
  border: 0;
  background: #fff;
}
</style>
