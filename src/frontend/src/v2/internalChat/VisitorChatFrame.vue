<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { AuthExpiredError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import { createReceptionChatTransport, receptionFrameId, receptionSourceId } from '@/api/receptionChatTransport'
import { useSessionStore } from '@/stores/session'
import { type VisitorChatScope, visitorFrameRequest, visitorPageHeaders } from '@/api/internalChatVisitors'
import { createVisitorHistoryTransport } from '@/api/visitorHistoryTransport'
import { bindChatWideWindow, chatConversationId, openChatWindow } from './chatWideWindow'
import { chatWebKBase } from './liveChatPreview'

const props = defineProps<{
  scope: VisitorChatScope
  viewerId: number
  wide: boolean
  conversationId?: number
  internalConversationId?: number
  observeConversation?: boolean
  autoOpenDirectory?: boolean
}>()
const emit = defineEmits<{
  sessionExpired: []
  navigationReady: []
  watchConversation: [id: number | undefined]
  watchInternalConversation: [id: number | undefined]
  refreshConversation: [id: number]
  revoked: [ids: number[]]
}>()
const router = useRouter()
const frame = ref<HTMLIFrameElement>()
const ready = ref(false)
const timedOut = ref(false)
const blocked = ref(false)
const origin = window.location.origin
const webkUrl = chatWebKBase(true) + '/?local-chat=1&noServiceWorker=1&live=1&visitor=' + props.scope +
  (props.wide ? '' : '&h5=1&list=1')
const requests = new AbortController()
const requestFromHost = createVisitorHistoryTransport(props.scope, config => request({
  ...config, headers: visitorPageHeaders(props.viewerId), signal: requests.signal, skipUnauthorizedHandler: true,
}))
const hasInternalChat = props.scope === 'desk' && useSessionStore().user?.internal_chat_enabled === true
const receptionTransport = hasInternalChat ? createReceptionChatTransport(requestFromHost) : undefined
let releaseWideWindow: (() => void) | undefined
let refreshTimer: number | undefined
let readyTimer: number | undefined
let watchedConversation: number | undefined

function send(type: string, detail?: unknown) {
  frame.value?.contentWindow?.postMessage({ type, detail }, origin)
}

function refresh() {
  if (!ready.value || document.visibilityState !== 'visible') return
  send('tt:chat.changed')
  if (props.observeConversation && watchedConversation !== undefined) emit('refreshConversation', watchedConversation)
}

function reportVisibility() {
  send('tt:chat.visible', document.visibilityState === 'visible')
  refresh()
}

function openWideWindow(conversationId?: number) {
  const source = receptionTransport && conversationId !== undefined ? receptionSourceId(conversationId) : undefined
  const id = receptionTransport ? source?.id : conversationId
  const target = router.resolve({
    name: source?.scope === 'internal' ? 'v2-internal-chat-window'
      : props.scope === 'guest' ? 'visitor-chat' : 'v2-internal-chat-visitors',
    query: {
      ...(source?.scope === 'internal' ? {} : { wide: '1' }),
      ...(id === undefined ? {} : { conversation: String(id) }),
    },
  })
  const opened = openChatWindow(target.href)
  blocked.value = !opened
  return opened
}

function dispatchFrameRequest(data: Parameters<typeof visitorFrameRequest>[1]) {
  if (receptionTransport) return receptionTransport(data)
  const config = visitorFrameRequest(props.scope, data)
  if (!config) throw new Error('访客会话暂不支持此操作')
  return requestFromHost(config)
}

async function acceptMessage(event: MessageEvent) {
  const source = frame.value?.contentWindow
  if (!source || event.source !== source || event.origin !== origin) return
  const data = event.data as {
    type?: unknown, id?: unknown, detail?: unknown,
    path?: unknown, method?: unknown, body?: unknown,
  } | undefined
  if (data?.type === 'tt:chat.ready') {
    send('tt:chat.visible', document.visibilityState === 'visible')
    return
  }
  if (data?.type === 'tt:chat.navigationReady') {
    if (ready.value) return
    if (receptionTransport && props.internalConversationId !== undefined) {
      send('tt:chat.openConversation', receptionFrameId('internal', props.internalConversationId))
    } else if (props.conversationId !== undefined) {
      send('tt:chat.openConversation', receptionTransport ? receptionFrameId('visitor', props.conversationId) : props.conversationId)
    } else if (props.scope === 'guest' && props.autoOpenDirectory !== false) openDirectory()
    ready.value = true
    timedOut.value = false
    window.clearTimeout(readyTimer)
    emit('navigationReady')
    return reportVisibility()
  }
  if (data?.type === 'tt:chat.openWideWindow') {
    const detail = data.detail as { conversationId?: unknown } | undefined
    openWideWindow(chatConversationId(detail?.conversationId))
    return
  }
  if (data?.type === 'tt:chat.watchPresence' && (props.observeConversation || receptionTransport)) {
    const id = typeof data.detail === 'number' ? chatConversationId(data.detail) : undefined
    if (data.detail !== null && id === undefined) return
    const source = receptionTransport && id !== undefined ? receptionSourceId(id) : undefined
    watchedConversation = receptionTransport ? source?.scope === 'visitor' ? source.id : undefined : id
    emit('watchConversation', watchedConversation)
    if (receptionTransport) emit('watchInternalConversation', source?.scope === 'internal' ? source.id : undefined)
    return
  }
  if (data?.type !== 'tt:chat.request' || typeof data.id !== 'number' || !Number.isSafeInteger(data.id)) return
  try {
    const result = await dispatchFrameRequest(data)
    if (requests.signal.aborted) return
    source.postMessage({ type: 'tt:chat.response', id: data.id, result }, origin)
    if (props.scope === 'desk' && typeof data.path === 'string' && data.path.startsWith('/api/internal-chat/sync/?')) {
      const { revoked_conversation_ids: revoked } = result as { revoked_conversation_ids?: unknown }
      if (Array.isArray(revoked)) {
        const ids = revoked.map(chatConversationId).filter((id): id is number => id !== undefined)
        if (ids.length) emit('revoked', ids)
      }
    }
  } catch (cause) {
    if (requests.signal.aborted) return
    const status = cause instanceof AuthExpiredError ? 401 : cause instanceof HttpError ? cause.status : undefined
    const error = cause instanceof Error ? cause.message : '聊天请求失败'
    source.postMessage({ type: 'tt:chat.response', id: data.id, error, errorStatus: status }, origin)
    const sessionCheck = typeof data.path === 'string' &&
      (data.path === '/api/auth/me' || data.path.startsWith('/api/internal-chat/sync/?'))
    if (status === 401 || (sessionCheck && (status === 403 || status === 404))) {
      requests.abort()
      emit('sessionExpired')
    }
  }
}

onMounted(() => {
  window.addEventListener('message', acceptMessage)
  releaseWideWindow = bindChatWideWindow(() => frame.value, openWideWindow)
  document.addEventListener('visibilitychange', reportVisibility)
  refreshTimer = window.setInterval(refresh, 2_000)
  readyTimer = window.setTimeout(() => { timedOut.value = true }, 18_000)
})

onBeforeUnmount(() => {
  requests.abort()
  releaseWideWindow?.()
  window.removeEventListener('message', acceptMessage)
  document.removeEventListener('visibilitychange', reportVisibility)
  window.clearInterval(refreshTimer)
  window.clearTimeout(readyTimer)
})

function reload() { window.location.reload() }
function openDirectory() { send('tt:chat.openDirectory') }
function updatePresence(id: number, online: boolean | null) {
  if (id === watchedConversation) send('tt:chat.presence', { conversationId: id, online })
}

defineExpose({ updatePresence, openDirectory })
</script>

<template>
  <main class="visitor-chat-frame">
    <div v-if="!ready || blocked" class="visitor-chat-feedback" role="status">
      <template v-if="timedOut">
        聊天加载超时。
        <button type="button" @click="reload">重新加载</button>
      </template>
      <template v-else-if="blocked">
        浏览器拦截了新窗口，请允许弹出窗口后重试。
        <button type="button" @click="blocked = false">关闭提示</button>
      </template>
      <template v-else>正在打开{{ scope === 'guest' && conversationId === undefined ? '通讯录' : '会话' }}…</template>
    </div>
    <iframe
      ref="frame"
      :class="{ 'visitor-chat-loading': !ready }"
      :src="webkUrl"
      :title="scope === 'guest' ? '访客通讯录和聊天' : '访客接待会话'"
    />
  </main>
</template>

<style scoped>
.visitor-chat-frame { position: relative; flex: 1; min-height: 0; overflow: hidden; background: white; }
.visitor-chat-frame iframe { display: block; width: 100%; height: 100%; border: 0; }
.visitor-chat-loading { visibility: hidden; }
.visitor-chat-feedback {
  position: absolute;
  z-index: 1;
  top: 18px;
  left: 50%;
  width: max-content;
  max-width: calc(100% - 28px);
  transform: translateX(-50%);
  text-align: center;
  background: white;
  color: #29453f;
  padding: 8px 12px;
  border-radius: 8px;
}
.visitor-chat-feedback button { margin-inline-start: 8px; }
</style>
