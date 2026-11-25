<script setup lang="ts">
import { computed, defineAsyncComponent, onScopeDispose, provide, ref, useTemplateRef, watch } from 'vue'
import { useRoute, useRouter, type RouteLocation } from 'vue-router'
import { f7App as F7App } from 'framework7-vue'
import { writeMobileRedirectSuppressed } from '@/mobileRedirect'
import { useSessionStore } from '@/stores/session'
import { openInternalChatPreview } from '@/v2/internalChat/launcherPreview'
import { internalChatOrderReturn } from './router/internalChatOrderReturn'
import { WXPUSHER_ENTRY } from './utils/lastWorkerPage'
import { mRoutePathOf, sanitizeMRedirect } from './utils/redirect'
import { isWxPusherAndroid, isWxPusherIos, wxpusherChatAppLink, wxpusherChatSitePath } from './utils/wxpusherOrderLink'
import { appChatCloseEnabled, appChatExpired, appChatHosted, appChatPreview, appChatRetainEnabled, notifyAppChat } from './components/appChatWebView'
import WxPusherBindingPrompt from './components/WxPusherBindingPrompt.vue'
import AndroidAppDownloadPrompt from './components/AndroidAppDownloadPrompt.vue'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const previewChatEnabled = import.meta.env.DEV && import.meta.env.VITE_INTERNAL_CHAT_PREVIEW === '1'
  && import.meta.env.VITE_INTERNAL_CHAT_LIVE === '1'
const MobileInternalChat = defineAsyncComponent(() => import('./components/MobileInternalChat.vue'))
const productionChat = computed(() => session.user?.internal_chat_enabled === true)
const chatEnabled = computed(() => previewChatEnabled || productionChat.value)
const embeddedChat = previewChatEnabled && appChatPreview
const isScreenshotBrowser = computed(() => route.name === 'm-order-screenshot-browser')
const isChatNotification = computed(() => route.name === 'm-wxpusher-chat')
const userAgent = navigator.userAgent
const isIos = /iPhone|iPod|iPad/.test(userAgent) || (/Macintosh/.test(userAgent) && navigator.maxTouchPoints > 1)
const isAndroid = /Android/i.test(userAgent)
const ordinaryAndroid = isAndroid && !appChatHosted && !appChatPreview && !isWxPusherAndroid()
const continuedNotificationLogin = ref(false)
if (ordinaryAndroid) {
  onScopeDispose(router.afterEach((to, from, failure) => {
    if (failure) return
    const redirect = from.name === 'm-login' && isDownloadPromptExcluded(from)
      ? sanitizeMRedirect(from.query.redirect)
      : null
    // LoginView replaces the marked login URL with the requested canonical path.
    continuedNotificationLogin.value = redirect !== null && mRoutePathOf(redirect) === (to.redirectedFrom ?? to).fullPath
  }))
}
const notificationEntry = computed(() => {
  if (!isChatNotification.value || !productionChat.value || session.status !== 'ready') return
  const id = Number(route.params.conversationId)
  const path = wxpusherChatSitePath(route.params.conversationId, route.query.message, undefined)
  return {
    conversationId: Number.isSafeInteger(id) && id > 0 ? id : undefined,
    staff: !session.isWorker,
    appLink: path && session.isWorker ? wxpusherChatAppLink(path, route.query.entry) : undefined,
  }
})
const showBindingPrompt = computed(() => isIos && !isWxPusherIos() && !appChatHosted && !appChatPreview
  && !isScreenshotBrowser.value && !notificationEntry.value)
const eligibleForDownloadPrompt = computed(() => {
  if (continuedNotificationLogin.value || isDownloadPromptExcluded(route)) return false
  if (route.name === 'm-login') {
    const redirect = sanitizeMRedirect(route.query.redirect)
    return redirect === null || !isDownloadPromptExcluded(router.resolve(mRoutePathOf(redirect)))
  }
  return route.meta.requiresAuth !== false && session.status === 'ready' && session.isWorker
})
const showRoute = computed(() => !appChatHosted && !notificationEntry.value
  && (!embeddedChat || session.status !== 'ready' || !session.isWorker
    || route.name === 'm-order-detail' || route.name === 'm-order-screenshots' || isScreenshotBrowser.value))
const showChat = computed(() => !appChatExpired.value && session.status === 'ready' &&
  (notificationEntry.value !== undefined || (!isChatNotification.value && chatEnabled.value && session.isWorker
    && route.meta.requiresAuth !== false)))
const chatIdentity = computed(() => `${session.identityPlane}:${session.user?.id}:${productionChat.value}`)
const chat = useTemplateRef<{
  returnFromOrder: () => boolean
  openConversation: (id?: number) => void
  isVisitingOrder: boolean
}>('chat')
const showHostedChatStatus = computed(() =>
  appChatHosted && (appChatExpired.value || session.status !== 'ready' || !productionChat.value || !session.isWorker)
)
let pendingChatOpen: { conversationId?: number } | undefined
provide(internalChatOrderReturn, () => chat.value?.returnFromOrder() ?? false)
provide(openInternalChatPreview, (conversationId) => {
  if (!showChat.value) return
  if (chat.value) chat.value.openConversation(conversationId)
  else pendingChatOpen = { conversationId }
})

// The order can resolve before the lazy launcher mounts. Only the current page and account may replay its click.
watch([() => route.fullPath, chatIdentity, showChat], () => { pendingChatOpen = undefined }, { flush: 'sync' })
watch(chat, (launcher) => {
  if (!launcher || !pendingChatOpen) return
  const { conversationId } = pendingChatOpen
  pendingChatOpen = undefined
  launcher.openConversation(conversationId)
}, { flush: 'post' })

watch(() => session.status, (status) => {
  if (appChatRetainEnabled && (status === 'guest' || status === 'ready' && (!productionChat.value || !session.isWorker))) {
    notifyAppChat('expired')
  }
}, { immediate: true })

function closeNotification(): void {
  if (session.isWorker) {
    void router.replace({ name: 'm-home' })
  } else {
    writeMobileRedirectSuppressed()
    window.location.replace('/v2/messages')
  }
}

function isSpecialEntry(target: Pick<RouteLocation, 'name' | 'path' | 'query'>): boolean {
  return target.query.entry === WXPUSHER_ENTRY
    || /^\/(?:wxpusher\/|me\/notifications(?:\/|$))/.test(target.path)
    || target.name === 'm-order-screenshots' || target.name === 'm-order-screenshot-browser'
}

function isDownloadPromptExcluded(target: Pick<RouteLocation, 'name' | 'path' | 'query' | 'redirectedFrom'>): boolean {
  return isSpecialEntry(target) || (!!target.redirectedFrom && isSpecialEntry(target.redirectedFrom))
}

watch(() => route.path, () => {
  const scrollContainer = document.getElementById('framework7-root')
  if (scrollContainer) scrollContainer.scrollTop = 0
  const tabScroll = scrollContainer?.querySelector<HTMLElement>('.m-shell__scroll')
  if (tabScroll) tabScroll.scrollTop = 0
}, { flush: 'post' })
</script>

<template>
  <F7App theme="ios" :colors="{ primary: '#bc00aa' }">
    <RouterView v-if="showRoute" v-slot="{ Component, route: current }">
      <component
        :is="Component"
        :key="chatEnabled && current.name === 'm-order-detail' ? String(current.params.orderNo) : undefined"
      />
    </RouterView>
    <WxPusherBindingPrompt v-if="showBindingPrompt" :defer-display="chat?.isVisitingOrder" />
    <AndroidAppDownloadPrompt v-if="ordinaryAndroid" :eligible="eligibleForDownloadPrompt" />
    <template v-if="showHostedChatStatus">
      <p role="status" class="m-native-chat-state">
        {{ session.status === 'loading' ? '正在打开内部沟通…' : '聊天登录已失效，请返回 App 后重新打开' }}
      </p>
      <button
        v-if="appChatCloseEnabled"
        type="button"
        class="m-native-chat-close"
        aria-label="关闭内部沟通"
        @click="notifyAppChat('close')"
      >×</button>
    </template>
    <MobileInternalChat
      v-if="showChat"
      :key="chatIdentity"
      ref="chat"
      :app-preview="embeddedChat"
      :embedded-app="appChatHosted"
      :production="productionChat"
      :notification-entry="notificationEntry"
      @close-notification="closeNotification"
    />
  </F7App>
</template>

<style scoped>
.m-native-chat-state { padding: 1.5rem; color: #17191b; text-align: center; }
.m-native-chat-close {
  position: fixed;
  right: 14px;
  bottom: calc(14px + env(safe-area-inset-bottom, 0px));
  width: 56px;
  height: 56px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: #3390ec;
  color: white;
  font-size: 32px;
  line-height: 1;
}
</style>
