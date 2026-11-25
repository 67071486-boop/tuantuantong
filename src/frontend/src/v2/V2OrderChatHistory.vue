<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, useTemplateRef } from 'vue'
import { fetchOrderChatImage, fetchOrderChatMessages, type OrderChatSummary } from '@/api/orderChat'
import { HttpError } from '@/api/errors'
import { chatWebKBase } from '@/v2/internalChat/liveChatPreview'

const props = defineProps<{
  orderNo: string
  production?: boolean
  group: OrderChatSummary
  activate: () => Promise<string | undefined>
}>()
const emit = defineEmits<{ unavailable: [message: string] }>()
const iframe = useTemplateRef('iframe')
const webKBase = computed(() => chatWebKBase(props.production === true))
const frameOrigin = computed(() => new URL(webKBase.value).origin)
const attempt = ref(0)
const status = ref<'loading' | 'ready' | 'error'>('loading')
let timer: ReturnType<typeof setTimeout> | undefined
let activeFrame: Window | undefined
let retired = false

function restart(): void {
  status.value = 'loading'
  attempt.value++
  clearTimeout(timer)
  timer = setTimeout(() => { status.value = 'error' }, 30_000)
}

function fetchHistory(path: unknown, method: unknown): Promise<unknown> {
  if (method !== 'get' || typeof path !== 'string') throw new Error('不支持该聊天请求')
  const base = `/api/internal-chat/orders/${props.orderNo}/`
  if (path === base) return Promise.resolve(props.group)
  if (!path.startsWith(`${base}messages/`)) throw new Error('不支持该聊天请求')
  const suffix = path.slice(`${base}messages/`.length)
  if (suffix === '') return fetchOrderChatMessages(props.orderNo)
  const cursor = /^\?(before|around)=([1-9]\d{0,15})$/.exec(suffix)
  if (cursor && Number.isSafeInteger(Number(cursor[2]))) {
    const id = Number(cursor[2])
    return fetchOrderChatMessages(props.orderNo, cursor[1] === 'before' ? { before: id } : { around: id })
  }
  const image = /^([1-9]\d{0,15})\/image\/$/.exec(suffix)
  if (image && Number.isSafeInteger(Number(image[1]))) return fetchOrderChatImage(props.orderNo, Number(image[1]))
  throw new Error('不支持该聊天请求')
}

async function handleActivation(frame: Window): Promise<void> {
  if (status.value !== 'ready' || activeFrame === frame) return
  activeFrame = frame
  frame.postMessage({ type: 'tt:order-history.action-state', detail: { busy: true } }, frameOrigin.value)
  let error: string | undefined
  try {
    error = await props.activate()
  } catch {
    error = '打开会话失败，请重试'
  } finally {
    if (activeFrame === frame) activeFrame = undefined
  }
  if (iframe.value?.contentWindow === frame) {
    frame.postMessage({ type: 'tt:order-history.action-state', detail: { busy: false, error } }, frameOrigin.value)
  }
}

async function onMessage(event: MessageEvent): Promise<void> {
  const frame = iframe.value?.contentWindow
  if (retired || !frame || event.source !== frame || event.origin !== frameOrigin.value) return
  const data = event.data as { type?: unknown; id?: unknown; path?: unknown; method?: unknown } | null
  if (data?.type === 'tt:order-history.ready' || data?.type === 'tt:order-history.error') {
    status.value = data.type === 'tt:order-history.ready' ? 'ready' : 'error'
    clearTimeout(timer)
    return
  }
  if (data?.type === 'tt:order-history.activate') {
    await handleActivation(frame)
    return
  }
  if (data?.type !== 'tt:chat.request' || typeof data.id !== 'number') return
  const response = { type: 'tt:chat.response', id: data.id }
  try {
    const result = await fetchHistory(data.path, data.method)
    if (!retired && iframe.value?.contentWindow === frame) frame.postMessage({ ...response, result }, frameOrigin.value)
  } catch (error) {
    if (error instanceof HttpError && (error.status === 403 || error.status === 503)) {
      retired = true
      emit('unavailable', error.status === 403 ? '订单权限已撤销' : '暂时无法确认订单权限，请稍后重试')
      return
    }
    if (iframe.value?.contentWindow === frame) {
      frame.postMessage({ ...response, error: error instanceof Error ? error.message : '读取聊天记录失败' }, frameOrigin.value)
    }
  }
}

onMounted(() => {
  window.addEventListener('message', onMessage)
  restart()
})

onUnmounted(() => {
  retired = true
  window.removeEventListener('message', onMessage)
  clearTimeout(timer)
})
</script>

<template>
  <div class="order-chat-history">
    <iframe
      v-if="attempt > 0"
      :key="attempt"
      ref="iframe"
      :src="webKBase + '/?local-chat=1&noServiceWorker=1&list=1&live=1&order-history=' + encodeURIComponent(orderNo)"
      title="订单群聊天记录"
      :aria-hidden="status !== 'ready' ? true : undefined"
      :tabindex="status === 'ready' ? undefined : -1"
    />
    <div v-if="status !== 'ready'" class="history-status">
      <p v-if="status === 'loading'" role="status">正在加载聊天记录…</p>
      <template v-else>
        <p role="alert">聊天记录加载失败</p>
        <button type="button" class="cv2-btn cv2-btn--sm" @click="restart">重试</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.order-chat-history { position: relative; height: 100%; min-height: 0; }
.order-chat-history iframe { display: block; width: 100%; height: 100%; border: 0; }
.history-status {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 6px;
  color: var(--tt2-dim);
  background: var(--tt2-panel);
}
.history-status p { margin: 0; }
</style>
