<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { computed, defineAsyncComponent, onMounted, onUnmounted, provide, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppShell from '@/layouts/AppShell.vue'
import { mountChatwoot, syncChatwootUser, unmountChatwoot } from '@/chatwootWidget'
import { useSessionStore } from '@/stores/session'
import { desktopChatOwner } from '@/v2/internalChat/desktopChatNotifications'
import { internalChatProduction, openInternalChatPreview } from '@/v2/internalChat/launcherPreview'

// 全局中文：本项目是**纯中文控制台**，不设 locale 时 Element Plus 的表格空态、分页、
// 日期范围选择器等会渲染英文（P2 第一个订单列表就会暴露）。这里用官方机制
// `<el-config-provider :locale>` 包住整个应用，覆盖所有后代组件。
// `zhCn` 从 `element-plus/es/locale/lang/zh-cn` 具名路径引入（该文件 default 导出，见 `element-plus/es/locale/lang/zh-cn.d.ts` 的 `export { _default as default }`），
// 不引整个 element-plus，按需引入的前提不变。

const route = useRoute()
const router = useRouter()
// 布局归属（spec §5.1）：`/login` 的 meta 声明 `layout: 'blank'`，所以登录页**不渲染业务壳**
// （`AppShell.vue` 渲染顶栏 + 侧栏 + main）——否则未认证用户在登录页上会看到侧栏 IA，
// 且顶栏身份区在 `user === null` 下也要渲染。默认值是 `'shell'`：业务路由**不必**声明 layout。
const useShell = computed(() => (route.meta.layout ?? 'shell') === 'shell')
const session = useSessionStore()
const isV2 = computed(() => route.matched.some((record) => record.name === 'v2'))
const isDesktop = computed(() => useShell.value || isV2.value)
const desktopUser = computed(() => isDesktop.value ? session.user : null)
const launcherPreviewEnabled = import.meta.env.DEV && import.meta.env.VITE_INTERNAL_CHAT_PREVIEW === '1'
const launcherPreview = defineAsyncComponent(() => import('@/v2/internalChat/InternalChatLauncherPreview.vue'))
const liveChatEnabled = computed(() => isV2.value && desktopUser.value?.internal_chat_enabled === true)
const chatwootUser = computed(() => liveChatEnabled.value ? null : desktopUser.value)
provide(internalChatProduction, liveChatEnabled)
const chatOpenRequest = ref(0)
const chatConversationId = ref<number>()

provide(openInternalChatPreview, (conversationId) => {
  if (!launcherPreviewEnabled && !liveChatEnabled.value) {
    void router.push({ name: 'v2-internal-chat' })
    return
  }
  chatConversationId.value = conversationId
  chatOpenRequest.value += 1
})
const chatOwner = computed(() => desktopUser.value
  ? desktopChatOwner(desktopUser.value)
  : null)
watch(chatOwner, () => {
  chatConversationId.value = undefined
  chatOpenRequest.value = 0
}, { flush: 'sync' })

async function openOrderFromChat(orderNo: string) {
  await router.push({ name: 'v2-order-detail-preview', params: { order_no: orderNo }, query: { orderChat: '1' } })
  if (route.name === 'v2-order-detail-preview' && route.params.order_no === orderNo) {
    window.dispatchEvent(new CustomEvent('tt:order-chat-focus', { detail: { orderNo } }))
  }
}

watchEffect(() => {
  if (!isDesktop.value) return
  const name = desktopUser.value?.tenant_name
  document.title = name ? `团团通 - ${name}` : '团团通 · 租户后台'
})

// 两套桌面壳共用客服会话，切换壳或进入旧详情时不重置。
function syncDesktopChat(user: typeof chatwootUser.value): void {
  if (launcherPreviewEnabled) return
  if (!user?.chat_identifier) {
    unmountChatwoot()
    return
  }
  if (mountChatwoot(user)) syncChatwootUser(user)
}

onMounted(() => {
  if (isDesktop.value) syncDesktopChat(chatwootUser.value)
})
watch(chatwootUser, syncDesktopChat)
onUnmounted(unmountChatwoot)
</script>

<template>
  <el-config-provider :locale="zhCn">
    <!-- 业务壳的内容区由壳自己渲染（D-0220 的页签缓存容器 `AppTabbedContent`），
         App.vue 不再往壳里塞 RouterView——缓存层需要读页签状态，多一层转手没有意义。 -->
    <AppShell v-if="useShell" />
    <RouterView v-else />
    <component
      :is="launcherPreview"
      v-if="(launcherPreviewEnabled || liveChatEnabled) && chatOwner"
      :key="`${chatOwner}:${liveChatEnabled}`"
      :production="liveChatEnabled"
      :identity="desktopUser ?? undefined"
      :notification-owner="isV2 ? chatOwner : undefined"
      :open-request="chatOpenRequest"
      :conversation-id="chatConversationId"
      @open-order="openOrderFromChat"
    />
  </el-config-provider>
</template>
