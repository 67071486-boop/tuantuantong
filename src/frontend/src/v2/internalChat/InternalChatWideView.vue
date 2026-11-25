<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { fetchMe } from '@/api/auth'
import { AuthExpiredError } from '@/api/errors'
import { chatRequest } from '@/api/chatTransport'
import { bindChatWideWindow, chatConversationId, openChatWindow } from './chatWideWindow'
import { desktopChatOwner, useDesktopChatNotifications } from './desktopChatNotifications'
import { attachLiveChatPreview, chatWebKBase, isSameChatIdentity } from './liveChatPreview'
import VisitorReceptionDialog from './VisitorReceptionDialog.vue'
import type { VisitorReceptionPanel } from './visitorReception'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const frame = ref<HTMLIFrameElement>()
const ready = ref(false)
const allowed = computed(() => session.user?.internal_chat_enabled === true)
const expired = ref(false)
const timedOut = ref(false)
const popupBlocked = ref(false)
const notificationPromptDismissed = ref(false)
const receptionPanel = shallowRef<VisitorReceptionPanel>()
const receptionFrameKey = ref(0)
const src = chatWebKBase(true) + '/?local-chat=1&noServiceWorker=1&live=1&wide=1'
let connection: ReturnType<typeof attachLiveChatPreview> | undefined
let notificationOpened = false
let readyTimer: number | undefined
let unbindWideWindow: (() => void) | undefined
const requests = new AbortController()

function expireChat() {
  expired.value = true
  connection?.dispose()
}

async function verifyNotificationIdentity() {
  const expected = session.user
  if (!expected || expired.value) return false
  try {
    const current = await fetchMe({ fresh: true })
    if (session.user !== expected || expired.value) return false
    if (isSameChatIdentity(expected, current)) return true
    expireChat()
  } catch (error) {
    if (error instanceof AuthExpiredError && session.user === expected) expireChat()
  }
  return false
}

const {
  supported: notificationsAvailable, status: notificationStatus,
  pending: permissionPending, enable: enableDesktopNotifications, incomingMessage,
} = useDesktopChatNotifications(
  () => allowed.value && !expired.value && session.user ? desktopChatOwner(session.user) : undefined,
  (id, scope) => {
    notificationOpened = true
    if (scope === 'visitor') connection?.openVisitorConversation(id)
    else connection?.openConversation(id)
  },
  verifyNotificationIdentity,
)

function openWideWindow(conversationId?: number) {
  if (!allowed.value || expired.value) return false
  const source = connection?.sourceConversation(conversationId)
  const { href } = router.resolve({
    name: 'v2-internal-chat-window',
    query: source ? {
      conversation: String(source.id),
      ...(source.scope === 'visitor' ? { scope: 'visitor' } : {}),
    } : {},
  })
  const opened = openChatWindow(href)
  popupBlocked.value = !opened
  return opened
}

onMounted(async () => {
  document.title = '内部沟通 · 团团通'
  const user = session.user
  if (!user || !allowed.value) return
  unbindWideWindow = bindChatWideWindow(() => frame.value, openWideWindow)
  connection = attachLiveChatPreview(frame, allowed, (orderNo) => {
    void router.push({ name: 'v2-order-detail-preview', params: { order_no: orderNo }, query: { orderChat: '1' } })
  }, {
    identity: user,
    visitorReception: true,
    onReceptionFrameReset: () => {
      ready.value = false
      timedOut.value = false
      receptionFrameKey.value += 1
      window.clearTimeout(readyTimer)
      readyTimer = window.setTimeout(() => { timedOut.value = true }, 18_000)
    },
    openVisitorDesk: () => { void router.push({ name: 'v2-internal-chat-visitors' }) },
    openLegacyVisitorConversation: (id) => {
      void router.push({ name: 'v2-internal-chat-visitors', query: { wide: '1', conversation: String(id) } })
    },
    openWideWindow: (detail) => openWideWindow(chatConversationId((detail as { conversationId?: unknown })?.conversationId)),
    onIdentityChanged: expireChat,
    onIncomingMessage: incomingMessage,
    onNavigationReady: () => {
      ready.value = true
      window.clearTimeout(readyTimer)
    },
    production: true,
  })
  receptionPanel.value = connection.receptionPanel
  readyTimer = window.setTimeout(() => { timedOut.value = true }, 18_000)
  const target = chatConversationId(route.query.conversation)
  if (target !== undefined) {
    if (route.query.scope === 'visitor') connection.openVisitorConversation(target)
    else connection.openConversation(target)
    return
  }
  try {
    const { conversations } = await chatRequest<{ conversations: { id: number }[] }>({
      url: '/api/internal-chat/conversations/', signal: requests.signal,
    })
    if (!requests.signal.aborted && !notificationOpened) connection.openConversation(chatConversationId(conversations[0]?.id))
  } catch {
    // WebK loads the list independently and owns its error state.
  }
})
onUnmounted(() => {
  requests.abort()
  unbindWideWindow?.()
  connection?.dispose()
  window.clearTimeout(readyTimer)
})

function reload() { window.location.reload() }
</script>

<template>
  <main class="internal-chat-wide">
    <div v-if="!allowed || expired" class="internal-chat-wide-status" role="alert">
      {{ expired ? '登录已失效，请重新登录。' : '当前账号暂未开启内部沟通。' }}
      <RouterLink to="/v2">返回团团通</RouterLink>
    </div>
    <div v-else-if="!ready" class="internal-chat-wide-status" role="status">
      {{ timedOut ? '聊天加载超时。' : '正在打开内部沟通…' }}
      <button v-if="timedOut" type="button" @click="reload">重新加载</button>
    </div>
    <div v-else-if="popupBlocked" class="internal-chat-wide-status internal-chat-wide-popup" role="status">
      浏览器拦截了新窗口，请允许弹出窗口后重试。
      <button type="button" @click="popupBlocked = false">关闭提示</button>
    </div>
    <div
      v-if="ready && allowed && !expired && !popupBlocked && notificationsAvailable
        && notificationStatus !== 'enabled' && !notificationPromptDismissed"
      class="internal-chat-wide-status internal-chat-wide-popup internal-chat-wide-notifications"
    >
      <span v-if="notificationStatus === 'blocked'">浏览器已阻止通知，请在网站设置中允许。</span>
      <button
        v-else type="button" class="cv2-btn cv2-btn--sm" data-enable-browser-notifications
        :disabled="permissionPending" @click="enableDesktopNotifications"
      >开启浏览器通知</button>
      <button type="button" class="cv2-btn cv2-btn--sm"
        aria-label="暂不提醒" @click="notificationPromptDismissed = true">×</button>
    </div>
    <iframe
      v-if="allowed && !expired"
      :key="receptionFrameKey"
      ref="frame"
      :class="{ 'internal-chat-wide-loading': !ready }"
      :src="src"
      title="内部沟通聊天"
    />
    <VisitorReceptionDialog v-if="allowed && !expired && receptionPanel" :panel="receptionPanel" />
  </main>
</template>

<style scoped>
.internal-chat-wide { position: fixed; inset: 0; overflow: hidden; background: white; }
.internal-chat-wide iframe { display: block; width: 100%; height: 100%; border: 0; }
.internal-chat-wide-loading { visibility: hidden; }
.internal-chat-wide-status { position: absolute; z-index: 1; top: 18px; left: 0; right: 0; padding-inline: 18px; text-align: center; }
.internal-chat-wide-popup { padding-block: 8px; background: white; }
.internal-chat-wide-notifications {
  inset-inline-start: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  padding-inline: 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  color: var(--tt2-dim);
  background: var(--tt2-panel);
}
.internal-chat-wide-status a, .internal-chat-wide-status button { margin-inline-start: 8px; }
</style>
