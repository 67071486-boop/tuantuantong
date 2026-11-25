<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import { writeMobileRedirectSuppressed } from '@/mobileRedirect'
import { attachLiveChatPreview, chatWebKBase, useChatPreviewUnread } from '@/v2/internalChat/liveChatPreview'
import { appChatCloseEnabled, appChatRetainEnabled, appChatTarget, notifyAppChat, notifyAppChatUnread } from './appChatWebView'

const props = defineProps<{
  appPreview?: boolean
  embeddedApp?: boolean
  production?: boolean
  notificationEntry?: { conversationId?: number; staff: boolean; appLink?: string }
}>()
const emit = defineEmits<{ closeNotification: [] }>()

const router = useRouter()
const bubble = ref<HTMLButtonElement>()
const dragArea = ref<HTMLDivElement>()
const screen = ref<HTMLDialogElement>()
const iframe = ref<HTMLIFrameElement>()
const isOpen = ref(false)
const chatPrepared = ref(false)
const appVisible = ref(window.__ttAppChatVisible === true)
const nativeHost = computed(() => props.appPreview || (props.embeddedApp && appChatRetainEnabled))
const retainedNavigation = ref<number>()
const displayed = computed(() => isOpen.value && (!nativeHost.value || appVisible.value))
const isVisible = computed(() => displayed.value && retainedNavigation.value === undefined)
const viewport = window.visualViewport
const viewportStyle = ref<{ height: string, top: string }>()
const listControlsReady = ref(false)
const preopenChat = computed(() => props.appPreview || props.embeddedApp || props.notificationEntry !== undefined)
const frameVersion = ref(0)
const loadTimedOut = ref(false)
const unread = useChatPreviewUnread(() => props.production === true)
const bubbleLabel = computed(() => unread.value > 0 ? `打开内部沟通，${unread.value} 条未读消息` : '打开内部沟通')
const webKBase = chatWebKBase(props.production === true)
const frameOrigin = new URL(webKBase).origin
const previewUrl = `${webKBase}/?local-chat=1&noServiceWorker=1&list=1&live=1&h5=1${props.appPreview || props.embeddedApp ? '&app=1' : ''}${props.embeddedApp && !appChatCloseEnabled ? '&nativeHost=1' : ''}${props.embeddedApp && appChatRetainEnabled ? '&retainedApp=1' : ''}`
const bubblePositionKey = 'tuantuantong:h5-chat-bubble-position'
const dragThreshold = 6
type Point = { x: number, y: number }
const savedPosition = ref(readPosition())
const dragPosition = ref<Point>()
const bubbleStyle = computed(() => {
  const position = dragPosition.value ?? savedPosition.value
  return position && { '--m-chat-x': `${position.x}px`, '--m-chat-y': `${position.y}px` }
})
let dragStart: { id: number, pointer: Point, origin: Point, width: number, height: number } | undefined
let suppressPointerClick = false
let pendingBack: { id: number, timer: number } | undefined
let loadTimer: number | undefined
let cancelPreload: (() => void) | undefined
let connection: ReturnType<typeof attachLiveChatPreview> | undefined
let launchedFromOrder: string | undefined
const orderVisit = shallowRef<{ origin: string; detail: string; orderNo: string; pending: boolean; returning: boolean }>()
const isVisitingOrder = computed(() => orderVisit.value !== undefined)
let restoredOrderPath: string | undefined
let nextRetainedNavigation = 0
let retainedNavigationTimer: number | undefined
let recoveringRetainedNavigation = false

function readPosition(): Point | undefined {
  try {
    const position: Partial<Point> | null = JSON.parse(window.localStorage.getItem(bubblePositionKey) ?? 'null')
    if (position && typeof position.x === 'number' && Number.isFinite(position.x)
      && typeof position.y === 'number' && Number.isFinite(position.y)) {
      return { x: position.x, y: position.y }
    }
  } catch {
    // 存储不可用时沿用右下角默认位置。
  }
}

function startDrag(event: PointerEvent): void {
  if (event.button !== 0 || dragStart || !bubble.value || !dragArea.value) return
  const bounds = bubble.value.getBoundingClientRect()
  const area = dragArea.value.getBoundingClientRect()
  dragStart = {
    id: event.pointerId,
    pointer: { x: event.clientX, y: event.clientY },
    origin: { x: bounds.left - area.left, y: bounds.top - area.top },
    width: bounds.width,
    height: bounds.height,
  }
  suppressPointerClick = false
  bubble.value.setPointerCapture(event.pointerId)
}

function moveDrag(event: PointerEvent): void {
  if (!dragStart || dragStart.id !== event.pointerId || !dragArea.value) return
  const dx = event.clientX - dragStart.pointer.x
  const dy = event.clientY - dragStart.pointer.y
  if (!dragPosition.value && Math.hypot(dx, dy) < dragThreshold) return
  const area = dragArea.value.getBoundingClientRect()
  dragPosition.value = {
    x: Math.max(0, Math.min(dragStart.origin.x + dx, area.width - dragStart.width)),
    y: Math.max(0, Math.min(dragStart.origin.y + dy, area.height - dragStart.height)),
  }
}

function finishDrag(event: PointerEvent): void {
  if (!dragStart || dragStart.id !== event.pointerId) return
  if (event.type === 'pointerup') moveDrag(event)
  const position = dragPosition.value
  dragStart = undefined
  dragPosition.value = undefined
  if (!position) return
  suppressPointerClick = true
  if (event.type !== 'pointerup') return
  savedPosition.value = position
  try {
    window.localStorage.setItem(bubblePositionKey, JSON.stringify(position))
  } catch {
    // 存储不可用时，本次打开的页面仍记住位置。
  }
}

function updateViewport(): void {
  viewportStyle.value = viewport && viewport.height > 0
    ? { height: `${viewport.height}px`, top: `${viewport.offsetTop}px` }
    : undefined
}

function schedulePreload(): void {
  if (chatPrepared.value) return
  if (document.hidden) {
    document.addEventListener('visibilitychange', schedulePreload, { once: true })
    cancelPreload = () => document.removeEventListener('visibilitychange', schedulePreload)
    return
  }
  const prepareChat = () => {
    cancelPreload = undefined
    if (document.hidden) schedulePreload()
    else chatPrepared.value = true
  }
  if (typeof window.requestIdleCallback === 'function') {
    const id = window.requestIdleCallback(prepareChat, { timeout: 1200 })
    cancelPreload = () => window.cancelIdleCallback(id)
  } else {
    const id = window.setTimeout(prepareChat, 400)
    cancelPreload = () => window.clearTimeout(id)
  }
}

function finishAppBack(id: number, handled: boolean): void {
  if (id !== pendingBack?.id) return
  window.clearTimeout(pendingBack.timer)
  pendingBack = undefined
  notifyAppChat(handled ? 'back-handled' : 'back-empty', id)
}

function backFromApp(id: number): void {
  if (!appVisible.value || !Number.isSafeInteger(id) || id <= 0) return
  if (pendingBack) window.clearTimeout(pendingBack.timer)
  pendingBack = undefined
  if (returnFromOrder()) {
    notifyAppChat('back-handled', id)
    return
  }
  if (orderVisit.value && router.currentRoute.value.name === 'm-order-screenshots') {
    router.back()
    notifyAppChat('back-handled', id)
    return
  }
  if (!listControlsReady.value || !connection?.requestBack(id)) {
    notifyAppChat('back-empty', id)
    return
  }
  pendingBack = { id, timer: window.setTimeout(() => finishAppBack(id, true), 550) }
}

function waitForChatControls(): void {
  window.clearTimeout(loadTimer)
  loadTimedOut.value = false
  loadTimer = window.setTimeout(() => { loadTimedOut.value = true }, 15000)
}

function onListControlsReady(): void {
  const wasReady = listControlsReady.value
  listControlsReady.value = true
  window.clearTimeout(loadTimer)
  loadTimedOut.value = false
  if (recoveringRetainedNavigation) {
    recoveringRetainedNavigation = false
    retainedNavigation.value = undefined
  }
  if (!wasReady && nativeHost.value) notifyAppChat('ready')
}

function retryChat(): void {
  if (recoveringRetainedNavigation) connection?.resetFrame()
  frameVersion.value++
  waitForChatControls()
}

function recoverRetainedNavigation(): void {
  window.clearTimeout(retainedNavigationTimer)
  recoveringRetainedNavigation = true
  listControlsReady.value = false
  retryChat()
}

watch(unread, (count) => { if (props.appPreview) notifyAppChatUnread(count) }, { immediate: true })

watch(isVisible, () => connection?.reportOpen(), { flush: 'sync' })
watch(() => props.notificationEntry, (entry) => {
  const restoredFromOrder = restoredOrderPath === router.currentRoute.value.fullPath
  restoredOrderPath = undefined
  if (entry && connection && !restoredFromOrder) openNotification(entry)
}, { flush: 'post' })

function openNotification(entry: NonNullable<typeof props.notificationEntry>): void {
  showChat()
  if (!listControlsReady.value) waitForChatControls()
  // 通知里那条消息可能已失效，交给聊天窗口按当前未读位置打开会话。
  connection?.openConversation(entry.conversationId)
}

onMounted(() => {
  updateViewport()
  viewport?.addEventListener('resize', updateViewport)
  viewport?.addEventListener('scroll', updateViewport)
  connection = attachLiveChatPreview(iframe, isVisible, (orderNo) => {
    void showOrder(orderNo)
  }, {
    retainOnClose: true,
    production: props.production,
    closeFromList: closeChat,
    canClose: () => displayed.value,
    onListControlsReady,
    onBackResult: nativeHost.value ? finishAppBack : undefined,
    onRetainedNavigationDone(id, settled) {
      if (retainedNavigation.value !== id) return
      if (!settled) {
        recoverRetainedNavigation()
        return
      }
      window.clearTimeout(retainedNavigationTimer)
      retainedNavigation.value = undefined
    },
  })
  if (nativeHost.value) {
    window.ttAppChat = {
      back: backFromApp,
      setVisible(visible) {
        appVisible.value = visible
        if (!visible && pendingBack) finishAppBack(pendingBack.id, true)
      },
      openTarget(conversationId, messageId) {
        if (!Number.isSafeInteger(conversationId) || conversationId < 1 ||
          !Number.isSafeInteger(messageId) || messageId < 1) return
        if (props.embeddedApp && appChatRetainEnabled) {
          const requestId = ++nextRetainedNavigation
          window.clearTimeout(retainedNavigationTimer)
          recoveringRetainedNavigation = false
          retainedNavigation.value = requestId
          retainedNavigationTimer = window.setTimeout(recoverRetainedNavigation, 12_000)
          connection?.openRetainedConversation({ conversationId, messageId, requestId })
        } else {
          connection?.openConversation({ conversationId, messageId })
        }
      },
    }
  }
  if (props.appPreview || props.embeddedApp) {
    showChat()
    waitForChatControls()
    if (props.embeddedApp) connection?.openConversation(appChatTarget())
  } else if (props.notificationEntry) {
    openNotification(props.notificationEntry)
  } else if (props.production) {
    schedulePreload()
  }
})

const removeAfterEach = router.afterEach((to, from, failure) => {
  restoredOrderPath = undefined
  if (!failure && launchedFromOrder !== undefined &&
    (to.name !== 'm-order-detail' || to.params.orderNo !== launchedFromOrder)) {
    launchedFromOrder = undefined
    connection?.openConversation()
    hideChat()
  }
  const visit = orderVisit.value
  if (!visit || visit.pending) return
  if (failure) {
    visit.returning = false
    return
  }
  if (to.fullPath === visit.detail) return
  if (from.fullPath === visit.detail && to.name === 'm-order-screenshots' && to.params.orderNo === visit.orderNo) return
  orderVisit.value = undefined
  if (from.fullPath === visit.detail && to.fullPath === visit.origin) {
    restoredOrderPath = to.fullPath
    showChat()
  }
})
onUnmounted(() => {
  cancelPreload?.()
  viewport?.removeEventListener('resize', updateViewport)
  viewport?.removeEventListener('scroll', updateViewport)
  if (props.appPreview) notifyAppChatUnread(0)
  if (nativeHost.value) {
    delete window.ttAppChat
    window.clearTimeout(pendingBack?.timer)
  }
  window.clearTimeout(loadTimer)
  window.clearTimeout(retainedNavigationTimer)
  orderVisit.value = undefined
  removeAfterEach()
  connection?.dispose()
})

async function showOrder(orderNo: string): Promise<void> {
  if (props.embeddedApp) {
    window.location.assign(`daidaitong://${location.host}/m/orders/${orderNo}`)
    return
  }
  if (props.notificationEntry?.staff) {
    writeMobileRedirectSuppressed()
    window.location.assign(`/orders/${orderNo}`)
    return
  }
  const current = router.currentRoute.value
  const sameOrder = current.name === 'm-order-detail' && current.params.orderNo === orderNo
  const target = { name: 'm-order-detail', params: { orderNo } }
  const visit = {
    origin: current.fullPath,
    detail: sameOrder ? current.fullPath : router.resolve(target).fullPath,
    orderNo,
    pending: !sameOrder,
    returning: false,
  }
  orderVisit.value = visit
  hideChat()
  if (sameOrder) return

  try {
    const failure = await router.push(target)
    if (!failure && router.currentRoute.value.fullPath === visit.detail) {
      visit.pending = false
      return
    }
  } catch {
    // A failed route load leaves the current chat available for retry.
  }
  if (orderVisit.value !== visit) return
  orderVisit.value = undefined
  if (router.currentRoute.value.fullPath === visit.origin) showChat()
}

function returnFromOrder(): boolean {
  const visit = orderVisit.value
  if (!visit || router.currentRoute.value.fullPath !== visit.detail) return false
  if (visit.pending || visit.returning) return true
  if (visit.origin === visit.detail) {
    orderVisit.value = undefined
    showChat()
  } else {
    visit.returning = true
    if (router.options.history.state.back === visit.origin) router.back()
    else void router.replace(visit.origin).catch(() => { visit.returning = false })
  }
  return true
}

function openConversation(id?: number): void {
  if (id !== undefined && (!Number.isSafeInteger(id) || id <= 0)) return
  const current = router.currentRoute.value
  if (!preopenChat.value && id !== undefined && current.name === 'm-order-detail' && typeof current.params.orderNo === 'string') {
    launchedFromOrder = current.params.orderNo
  }
  showChat()
  connection?.openConversation(id)
}

defineExpose({ returnFromOrder, openConversation, isVisitingOrder })

function openChat(event: MouseEvent): void {
  if (event.detail !== 0 && suppressPointerClick) {
    suppressPointerClick = false
    return
  }
  if (orderVisit.value?.pending || returnFromOrder()) return
  orderVisit.value = undefined
  showChat()
}

function showChat(): void {
  if (isOpen.value || !screen.value) return
  cancelPreload?.()
  cancelPreload = undefined
  updateViewport()
  screen.value.showModal()
  chatPrepared.value = true
  isOpen.value = true
}

function hideChat(): void {
  if (!isOpen.value) return
  isOpen.value = false
  screen.value?.close()
}

function closeChat(): void {
  if (nativeHost.value || (props.embeddedApp && appChatCloseEnabled)) {
    if (nativeHost.value) appVisible.value = false
    notifyAppChat('close')
    return
  }
  if (props.embeddedApp) return
  // The next bubble tap should not retry a direct open the user has closed.
  launchedFromOrder = undefined
  connection?.openConversation()
  hideChat()
  if (props.notificationEntry) {
    emit('closeNotification')
    return
  }
  void nextTick(() => bubble.value?.focus({ preventScroll: true }))
}

function refreshListControls(): void {
  listControlsReady.value = false
  if (preopenChat.value && !loadTimedOut.value) waitForChatControls()
  iframe.value?.contentWindow?.postMessage({ type: 'tt:chat.requestListControlsReady' }, frameOrigin)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="!preopenChat" ref="dragArea" class="m-chat-bubble-area">
      <button
        v-show="!isOpen"
        ref="bubble"
        type="button"
        class="m-chat-bubble"
        :style="bubbleStyle"
        :data-dragging="dragPosition ? '' : undefined"
        :aria-label="bubbleLabel"
        aria-haspopup="dialog"
        @click="openChat"
        @pointerdown="startDrag"
        @pointermove="moveDrag"
        @pointerup="finishDrag"
        @pointercancel="finishDrag"
        @lostpointercapture="finishDrag"
        @contextmenu.prevent
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path fill="currentColor" d="M12 3a9 9 0 1 0 0 18h9v-9a9 9 0 0 0-9-9Z" />
        </svg>
        <span v-if="unread > 0" class="m-chat-unread" aria-hidden="true">{{ unread > 99 ? '99+' : unread }}</span>
      </button>
    </div>

    <dialog
      ref="screen"
      class="m-chat-screen"
      :style="viewportStyle"
      aria-label="内部沟通"
      @cancel.prevent="!listControlsReady && closeChat()"
    >
      <a v-if="notificationEntry?.appLink" :href="notificationEntry.appLink" class="m-chat-app-link external">
        在代代通打开
      </a>
      <iframe
        v-if="chatPrepared"
        :key="frameVersion"
        ref="iframe"
        :src="previewUrl"
        title="内部沟通聊天内容"
        @load="refreshListControls"
      />
      <div v-if="preopenChat && !listControlsReady" class="m-chat-load-state">
        <p role="status">{{ loadTimedOut ? '暂时打不开内部沟通' : '正在加载内部沟通…' }}</p>
        <button v-if="loadTimedOut" type="button" @click="retryChat">重试</button>
      </div>
      <button
        v-if="isOpen && (!embeddedApp || appChatCloseEnabled) && !listControlsReady"
        type="button"
        class="m-chat-loading-close"
        aria-label="关闭内部沟通"
        @click="closeChat"
      >
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
          <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="1.75" />
        </svg>
      </button>
    </dialog>
  </Teleport>
</template>

<style scoped>
.m-chat-bubble-area {
  position: fixed;
  /* 位于大厅订单详情遮罩下方，避开页眉和底部操作栏。 */
  z-index: 598;
  top: calc(56px + env(safe-area-inset-top));
  right: max(16px, env(safe-area-inset-right));
  bottom: calc(88px + env(safe-area-inset-bottom));
  left: max(16px, env(safe-area-inset-left));
  pointer-events: none;
}

/* 下拉刷新停稳时的零位移会隔开按钮和气泡的层级。保留下拉时的动画。 */
:global(body:has(> .m-chat-bubble-area) .t-pull-down-refresh__track:is([style*="translate3d(0px, 0px, 0px)"], [style*="translate3d(0, 0px, 0)"]):has(.m-mine-card__actions, .m-hall-card__claim)) {
  transform: none !important;
}

:global(body:has(> .m-chat-bubble-area) :is(.m-mine-card__submit, .m-mine-card__report, .m-hall-card__claim)) {
  position: relative;
  z-index: 599;
}

.m-chat-bubble,
.m-chat-loading-close {
  --m-chat-bubble-size: 56px;
  position: absolute;
  display: grid;
  width: var(--m-chat-bubble-size);
  height: var(--m-chat-bubble-size);
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: #1677ff;
  color: #fff;
  box-shadow: 0 6px 18px rgb(43 42 40 / 28%);
  pointer-events: auto;
}

.m-chat-bubble {
  left: clamp(0px, var(--m-chat-x, 100%), max(0px, 100% - var(--m-chat-bubble-size)));
  top: clamp(0px, var(--m-chat-y, 100%), max(0px, 100% - var(--m-chat-bubble-size)));
  cursor: grab;
  touch-action: none;
  user-select: none;
}

.m-chat-loading-close {
  right: calc(16px + env(safe-area-inset-right));
  bottom: calc(16px + env(safe-area-inset-bottom));
  cursor: pointer;
}

.m-chat-bubble[data-dragging] {
  cursor: grabbing;
}

.m-chat-bubble svg,
.m-chat-loading-close svg {
  width: 30px;
  height: 30px;
}

.m-chat-unread {
  position: absolute;
  top: -3px;
  right: -3px;
  display: grid;
  box-sizing: border-box;
  min-width: 22px;
  min-height: 22px;
  place-items: center;
  padding: 0 4px;
  border: 2px solid #fff;
  border-radius: 12px;
  background: #db3434;
  color: #fff;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  line-height: 16px;
}

.m-chat-screen {
  position: fixed;
  inset: 0 0 auto;
  box-sizing: border-box;
  width: 100%;
  height: 100dvh;
  max-width: none;
  max-height: none;
  margin: 0;
  padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  overflow: hidden;
  border: 0;
  background: #fff;
  color: #17191b;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}

.m-chat-app-link {
  z-index: 1;
  justify-self: end;
  padding: 8px 14px;
  color: #0960bf;
  font-size: 13px;
}

.m-chat-screen::backdrop {
  /* The keyboard can show the layout viewport outside the resized chat. */
  background: #fff;
}

.m-chat-screen:not([open]) {
  /* Keep the iframe viewport sized so hiding the dialog does not reset chat scroll. */
  visibility: hidden;
  pointer-events: none;
}

.m-chat-load-state {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  text-align: center;
  background: #fff;
}

.m-chat-load-state button {
  padding: 8px 16px;
  border: 0;
  border-radius: 8px;
  color: #1677ff;
  background: #f0f6ff;
}

.m-chat-screen iframe {
  display: block;
  grid-row: 2;
  width: 100%;
  height: 100%;
  border: 0;
}
</style>
