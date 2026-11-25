<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import VisitorReceptionDialog from './VisitorReceptionDialog.vue'
import type { VisitorReceptionPanel } from './visitorReception'
import { bindChatWideWindow, chatConversationId, openChatWindow } from './chatWideWindow'
import { fetchMe } from '@/api/auth'
import { AuthExpiredError } from '@/api/errors'
import { useDesktopChatNotifications } from './desktopChatNotifications'
import { attachLiveChatPreview, chatWebKBase, isSameChatIdentity, liveChatPreview, useChatPreviewUnread, type ChatIdentity } from './liveChatPreview'

const props = defineProps<{
  production?: boolean
  openRequest: number
  conversationId?: number
  notificationOwner?: string | null
  identity?: ChatIdentity
  positionScope?: 'kefu'
  defaultAnchor?: HTMLElement | null
}>()
const emit = defineEmits<{ openOrder: [orderNo: string] }>()
const router = useRouter()
const receptionPanel = shallowRef<VisitorReceptionPanel>()
const receptionFrameKey = ref(0)
const popupBlocked = ref(false)
let unbindWideWindow: (() => void) | undefined

const liveEnabled = computed(() => props.production === true || liveChatPreview)
const unreadTotal = useChatPreviewUnread(() => props.production === true)

// 本地原型共三种折叠样式，URL 的 chatFold 可指定初始样式。
const variants = [
  { id: 'bubble', width: 72, height: 72 },
  { id: 'rail', width: 44, height: 124 },
  { id: 'bar', width: 164, height: 46 },
] as const
type Variant = typeof variants[number]
type Point = { left: number; top: number }
type Positions = Partial<Record<Variant['id'], Point>>

const previewUrl = computed(() => liveEnabled.value
  ? chatWebKBase(props.production === true) + '/?local-chat=1&noServiceWorker=1&list=1&live=1'
  : 'http://127.0.0.1:4179/?local-chat=1&noServiceWorker=1&list=1')
const storageKey = props.positionScope === 'kefu'
  ? 'tt.kefu.internal-chat.positions'
  : 'tt.v2.internal-chat-preview.positions'
const edgeInset = 18
const panelGap = 16
const panelWidth = 420
const panelHeight = 640
const minPanelWidth = 320
const minPanelHeight = 400
const dragThreshold = 5
const initialVariant = props.positionScope === 'kefu'
  ? 'bubble'
  : new URLSearchParams(window.location.search).get('chatFold')
const variant: Variant = variants.find(({ id }) => id === initialVariant) ?? variants[0]
const positions = ref(loadPositions())
const viewport = ref({ width: window.innerWidth, height: window.innerHeight })
const anchorRect = ref<DOMRect>()
const livePosition = ref<Point>()
const launcher = ref<HTMLButtonElement>()
const iframe = ref<HTMLIFrameElement>()
const chatPrepared = ref(false)
const isOpen = ref(false)
const sessionChanged = ref(false)
const notificationPromptDismissed = ref(false)

let pointer: { id: number; x: number; y: number; origin: Point } | undefined
let skipPointerClick = false
let cancelPreload: (() => void) | undefined

function loadPositions(): Positions {
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as Positions | null
    const valid: Positions = {}
    for (const { id } of variants) {
      const value = stored?.[id]
      if (value && Number.isFinite(value.left) && Number.isFinite(value.top)) valid[id] = value
    }
    return valid
  } catch {
    return {}
  }
}

function constrain(to: Point): Point {
  const horizontalInset = variant.id === 'rail' ? 0 : edgeInset
  return {
    left: Math.min(
      Math.max(to.left, horizontalInset),
      Math.max(horizontalInset, viewport.value.width - variant.width - horizontalInset),
    ),
    top: Math.min(
      Math.max(to.top, edgeInset),
      Math.max(edgeInset, viewport.value.height - variant.height - edgeInset),
    ),
  }
}

const launcherPosition = computed(() => {
  if (livePosition.value) return livePosition.value
  const current = variant
  const saved = positions.value[current.id]
  const anchor = anchorRect.value
  const bottomInset = viewport.value.width <= 720 ? 56 : edgeInset
  const defaultTop = current.id === 'rail'
    ? (viewport.value.height - current.height) / 2
    : (anchor ? anchor.top - edgeInset : viewport.value.height - bottomInset) - current.height
  const position = constrain(saved ?? {
    left: (anchor ? anchor.right - edgeInset : viewport.value.width) - current.width,
    top: defaultTop,
  })
  if (current.id === 'rail') {
    position.left = saved?.left === 0 ? 0 : viewport.value.width - current.width
  }
  return position
})

const launcherStyle = computed(() => ({
  left: `${launcherPosition.value.left}px`,
  top: `${launcherPosition.value.top}px`,
  width: `${variant.width}px`,
  height: `${variant.height}px`,
}))

const panelStyle = computed(() => {
  const { left, top } = launcherPosition.value
  const { width: viewportWidth, height: viewportHeight } = viewport.value
  const right = left + variant.width
  const bottom = top + variant.height
  const maxHeight = Math.max(0, Math.min(panelHeight, viewportHeight - edgeInset * 2))
  const above = top - panelGap - edgeInset
  const below = viewportHeight - bottom - panelGap - edgeInset
  const onRight = left + variant.width / 2 >= viewportWidth / 2
  const sideSpace = (onRight ? left : viewportWidth - right) - panelGap - edgeInset
  const placeBeside = sideSpace >= minPanelWidth
    && (variant.id === 'rail' || Math.max(above, below) < minPanelHeight)
  const availableWidth = placeBeside ? sideSpace : viewportWidth - edgeInset * 2
  const width = Math.max(0, Math.min(panelWidth, availableWidth))
  const height = placeBeside ? maxHeight : Math.max(0, Math.min(maxHeight, Math.max(above, below)))
  const alignedLeft = onRight ? right - width : left
  const centeredTop = top + (variant.height - height) / 2
  const panelLeft = placeBeside
    ? (onRight ? left - panelGap - width : right + panelGap)
    : Math.min(Math.max(alignedLeft, edgeInset), viewportWidth - edgeInset - width)
  const panelTop = placeBeside
    ? Math.min(Math.max(centeredTop, edgeInset), viewportHeight - edgeInset - height)
    : above >= below ? top - panelGap - height : bottom + panelGap
  return { left: `${panelLeft}px`, top: `${panelTop}px`, width: `${width}px`, height: `${height}px` }
})

const launcherLabel = computed(() => {
  if (isOpen.value) return '收起聊天窗口'
  return unreadTotal.value ? `打开内部通讯，${unreadTotal.value} 条未读消息` : '打开内部通讯'
})

function updateAnchor(): void {
  const rect = props.defaultAnchor?.getBoundingClientRect()
  anchorRect.value = rect?.width ? rect : undefined
}

watch(() => props.defaultAnchor, (anchor, _, onCleanup) => {
  updateAnchor()
  if (!anchor) return
  const observer = new ResizeObserver(updateAnchor)
  observer.observe(anchor)
  onCleanup(() => observer.disconnect())
}, { immediate: true, flush: 'post' })

function updateViewport(): void {
  viewport.value = { width: window.innerWidth, height: window.innerHeight }
  updateAnchor()
  if (livePosition.value) livePosition.value = constrain(livePosition.value)
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
    const id = window.setTimeout(prepareChat, 250)
    cancelPreload = () => window.clearTimeout(id)
  }
}

let liveConnection: ReturnType<typeof attachLiveChatPreview> | undefined

function disconnectPreviousSession() {
  sessionChanged.value = true
  isOpen.value = false
  chatPrepared.value = false
  cancelPreload?.()
  liveConnection?.dispose()
  liveConnection = undefined
}

async function verifyNotificationIdentity() {
  const expected = props.identity
  if (!expected || sessionChanged.value) return false
  try {
    const current = await fetchMe({ fresh: true })
    if (props.identity !== expected || sessionChanged.value) return false
    if (isSameChatIdentity(expected, current)) return true
    disconnectPreviousSession()
  } catch (error) {
    if (error instanceof AuthExpiredError && props.identity === expected) disconnectPreviousSession()
  }
  return false
}

const {
  supported: desktopNotificationsAvailable, status: notificationStatus,
  pending: permissionPending, checkPermission, enable: enableDesktopNotifications, incomingMessage,
} = useDesktopChatNotifications(
  () => liveEnabled.value && !sessionChanged.value ? props.notificationOwner ?? undefined : undefined,
  (id, scope) => {
    openPanel()
    if (scope === 'visitor') liveConnection?.openVisitorConversation(id)
    else liveConnection?.openConversation(id)
  },
  verifyNotificationIdentity,
)
function openWideWindow(id?: number) {
  if (!isOpen.value || !props.production || sessionChanged.value || !router) return false
  const source = liveConnection?.sourceConversation(id)
  const target = router.resolve({
    name: 'v2-internal-chat-window',
    query: source ? {
      conversation: String(source.id),
      ...(source.scope === 'visitor' ? { scope: 'visitor' } : {}),
    } : {},
  })
  const opened = openChatWindow(target.href)
  popupBlocked.value = !opened
  return opened
}

onMounted(() => {
  window.addEventListener('resize', updateViewport)
  if (props.production) unbindWideWindow = bindChatWideWindow(() => iframe.value, openWideWindow)
  if (liveEnabled.value) {
    liveConnection = attachLiveChatPreview(iframe, isOpen, (orderNo) => {
      isOpen.value = false
      emit('openOrder', orderNo)
    }, {
      retainOnClose: true,
      identity: props.identity,
      production: props.production,
      onIdentityChanged: disconnectPreviousSession,
      onIncomingMessage: incomingMessage,
      visitorReception: !!props.production && props.positionScope !== 'kefu',
      openLegacyVisitorConversation: (id) => {
        void router?.push({ name: 'v2-internal-chat-visitors', query: { wide: '1', conversation: String(id) } })
      },
      onReceptionFrameReset: () => { receptionFrameKey.value += 1 },
      openWideWindow: (detail) => openWideWindow(chatConversationId((detail as { conversationId?: unknown })?.conversationId)),
      openVisitorDesk: () => {
        if (!sessionChanged.value && props.production && props.positionScope !== 'kefu') {
          void router?.push({ name: 'v2-internal-chat-visitors' })
        }
      },
    })
    if (props.openRequest > 0) liveConnection.openConversation(props.conversationId)
    receptionPanel.value = liveConnection.receptionPanel
    schedulePreload()
  }
})
onUnmounted(() => {
  unbindWideWindow?.()
  cancelPreload?.()
  window.removeEventListener('resize', updateViewport)
  liveConnection?.dispose()
})
watch(isOpen, () => liveConnection?.reportOpen(), { flush: 'post' })

function beginDrag(event: PointerEvent): void {
  if (event.button !== 0 || pointer) return
  skipPointerClick = false
  pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, origin: launcherPosition.value }
  launcher.value?.setPointerCapture(event.pointerId)
}

function moveDrag(event: PointerEvent): void {
  if (pointer?.id !== event.pointerId) return
  const dx = event.clientX - pointer.x
  const dy = event.clientY - pointer.y
  if (!livePosition.value && Math.hypot(dx, dy) < dragThreshold) return
  livePosition.value = constrain({ left: pointer.origin.left + dx, top: pointer.origin.top + dy })
}

function finishDrag(event: PointerEvent): void {
  if (pointer?.id !== event.pointerId) return
  const position = livePosition.value
  pointer = undefined
  livePosition.value = undefined
  if (!position) return
  skipPointerClick = true
  if (event.type !== 'pointerup') return

  const { id, width } = variant
  positions.value[id] = id === 'rail'
    ? { left: position.left + width / 2 < viewport.value.width / 2 ? 0 : viewport.value.width - width, top: position.top }
    : position
  try {
    localStorage.setItem(storageKey, JSON.stringify(positions.value))
  } catch {
    // 浏览器禁用本地存储时，本次会话仍保留位置。
  }
}

function openPanel(): void {
  cancelPreload?.()
  cancelPreload = undefined
  chatPrepared.value = true
  if (!isOpen.value) {
    checkPermission()
    notificationPromptDismissed.value = false
  }
  isOpen.value = true
}

function activateLauncher(event: MouseEvent): void {
  if (skipPointerClick && event.detail > 0) {
    skipPointerClick = false
    return
  }
  if (isOpen.value) isOpen.value = false
  else openPanel()
}

watch(() => props.openRequest, request => {
  if (request <= 0) return
  openPanel()
  liveConnection?.openConversation(props.conversationId)
  void nextTick(() => launcher.value?.focus({ preventScroll: true }))
}, { immediate: true })
</script>

<template>
  <Teleport v-if="!sessionChanged" to="body">
    <div class="ttv2 cv2-chat-launcher-preview">
      <button
        ref="launcher"
        type="button"
        class="cv2-chat-launcher"
        :class="`cv2-chat-launcher--${variant.id}`"
        :data-dragging="!!livePosition || undefined"
        :data-left-edge="launcherPosition.left === 0 || undefined"
        :data-open="isOpen || undefined"
        :style="launcherStyle"
        :aria-label="launcherLabel"
        :title="launcherLabel"
        :aria-expanded="isOpen"
        aria-controls="cv2-chat-launcher-window"
        @pointerdown="beginDrag"
        @pointermove="moveDrag"
        @pointerup="finishDrag"
        @pointercancel="finishDrag"
        @lostpointercapture="finishDrag"
        @keydown.esc="isOpen = false"
        @click="activateLauncher"
      >
        <span class="cv2-chat-launcher-icon" aria-hidden="true">
          <svg class="cv2-chat-launcher-open-icon" viewBox="0 0 24 24" focusable="false">
            <path fill="currentColor" d="M12 3a9 9 0 1 0 0 18h9v-9a9 9 0 0 0-9-9Z" />
          </svg>
          <svg class="cv2-chat-launcher-close-icon" viewBox="0 0 24 24" focusable="false">
            <path d="m6 6 12 12M18 6 6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </span>
        <span v-if="variant.id !== 'bubble'" class="cv2-chat-launcher-label">
          {{ isOpen ? launcherLabel : '内部通讯' }}
        </span>
        <span v-if="!isOpen && unreadTotal" class="cv2-chat-launcher-unread" aria-hidden="true">
          {{ unreadTotal > 99 ? '99+' : unreadTotal }}
        </span>
      </button>

      <section
        id="cv2-chat-launcher-window"
        class="cv2-chat-launcher-window"
        aria-label="内部通讯"
        :data-open="isOpen || undefined"
        :inert="isOpen ? undefined : true"
        :style="panelStyle"
      >
        <div
          v-if="isOpen && liveEnabled && notificationOwner && desktopNotificationsAvailable
            && notificationStatus !== 'enabled' && !notificationPromptDismissed"
          class="cv2-chat-notification-control"
        >
          <span v-if="notificationStatus === 'blocked'">浏览器已阻止通知，请在网站设置中允许。</span>
          <template v-else>
            <span>开启浏览器通知，聊天收起后也能收到新消息提醒。</span>
            <button
              type="button"
              class="cv2-btn cv2-btn--sm"
              data-enable-browser-notifications
              :disabled="permissionPending"
              @click="enableDesktopNotifications"
            >开启浏览器通知</button>
          </template>
          <button
            type="button"
            class="cv2-chat-notification-dismiss"
            aria-label="暂不提醒"
            @click="notificationPromptDismissed = true"
          >×</button>
        </div>
        <div v-if="isOpen && popupBlocked" class="cv2-chat-notification-control" role="status">
          <span>浏览器拦截了新窗口，请允许弹出窗口后重试。</span>
          <button type="button" class="cv2-chat-notification-dismiss" aria-label="关闭提示" @click="popupBlocked = false">×</button>
        </div>
        <iframe v-if="chatPrepared" :key="receptionFrameKey" ref="iframe" :src="previewUrl" title="内部通讯聊天内容" />
        <VisitorReceptionDialog v-if="isOpen && receptionPanel" :panel="receptionPanel" />
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.cv2-chat-launcher-preview {
  color: var(--tt2-ink);
  font-family: inherit;
}

.cv2-chat-launcher {
  position: fixed;
  z-index: 1001;
  display: inline-flex;
  box-sizing: border-box;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: var(--tt2-link-blue);
  color: var(--tt2-on-primary);
  touch-action: none;
  user-select: none;
  cursor: grab;
  box-shadow: 0 6px 18px var(--tt2-flash-shadow);
}

.cv2-chat-launcher[data-dragging] {
  cursor: grabbing;
}

.cv2-chat-launcher-icon {
  display: grid;
  flex: none;
  place-items: center;
}

.cv2-chat-launcher-icon svg {
  grid-area: 1 / 1;
  display: block;
  width: 32px;
  height: 32px;
  transition: transform 300ms ease, opacity 100ms ease;
}

.cv2-chat-launcher--bubble .cv2-chat-launcher-icon svg {
  width: 38px;
  height: 38px;
}

.cv2-chat-launcher-close-icon,
.cv2-chat-launcher[data-open] .cv2-chat-launcher-open-icon {
  opacity: 0;
  transform: scale(.75) rotate(-30deg);
}

.cv2-chat-launcher[data-open] .cv2-chat-launcher-close-icon {
  opacity: 1;
  transform: none;
}

.cv2-chat-launcher-unread {
  position: absolute;
  inset: -3px -3px auto auto;
  box-sizing: border-box;
  min-width: 22px;
  min-height: 22px;
  padding: 1px 4px 0;
  border: 2px solid var(--tt2-panel);
  border-radius: 12px;
  background: var(--tt2-money-red);
  color: var(--tt2-on-primary);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 16px;
  text-align: center;
}

.cv2-chat-launcher-label {
  font-size: 13px;
  font-weight: 650;
  white-space: nowrap;
}

.cv2-chat-launcher--rail {
  flex-direction: column;
  gap: 7px;
  border-radius: 9px 0 0 9px;
  background: var(--tt2-link-blue-deep);
}

.cv2-chat-launcher--rail[data-left-edge] {
  border-radius: 0 9px 9px 0;
}

.cv2-chat-launcher--rail .cv2-chat-launcher-icon svg {
  width: 21px;
  height: 21px;
}

.cv2-chat-launcher--rail .cv2-chat-launcher-label {
  writing-mode: vertical-rl;
  letter-spacing: .13em;
}

.cv2-chat-launcher--rail .cv2-chat-launcher-unread {
  inset: 2px 2px auto auto;
}

.cv2-chat-launcher--bar {
  gap: 8px;
  justify-content: start;
  padding-inline: 8px 10px;
  border: 1px solid var(--tt2-line);
  border-radius: 9px;
  background: var(--tt2-panel);
  color: var(--tt2-ink);
}

.cv2-chat-launcher--bar .cv2-chat-launcher-icon {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--tt2-link-blue);
  color: var(--tt2-on-primary);
}

.cv2-chat-launcher--bar .cv2-chat-launcher-icon svg {
  width: 16px;
  height: 16px;
}

.cv2-chat-launcher--bar .cv2-chat-launcher-unread {
  position: static;
  min-height: 18px;
  margin-inline-start: auto;
  border: 0;
}

.cv2-chat-launcher-window {
  position: fixed;
  z-index: 1000;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 16px;
  background: var(--tt2-panel);
  box-shadow: 0 8px 32px var(--tt2-flash-shadow);
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transform: translateY(40px);
  transition: opacity 200ms linear, transform 250ms linear, visibility 0ms linear 250ms;
}

.cv2-chat-launcher-window[data-open] {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
  transform: translateY(0);
  transition-delay: 0ms;
}

.cv2-chat-launcher-window iframe {
  display: block;
  width: 100%;
  flex: 1;
  min-height: 0;
  border: 0;
}

.cv2-chat-notification-control {
  position: absolute;
  z-index: 1;
  inset: 12px 12px auto;
  display: flex;
  gap: 8px;
  align-items: center;
  margin-inline-start: auto;
  max-width: 348px;
  padding: 8px 10px;
  border: 1px solid var(--tt2-line);
  border-radius: 10px;
  background: var(--tt2-panel);
  box-shadow: 0 3px 12px var(--tt2-flash-shadow);
  color: var(--tt2-dim);
  font-size: 12px;
}

.cv2-chat-notification-dismiss {
  flex: none;
  margin-inline-start: auto;
  padding: 0 4px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 20px;
}

.cv2-chat-launcher-preview button:focus-visible {
  outline: 2px solid var(--tt2-link-blue-deep);
  outline-offset: 2px;
}

.cv2-chat-launcher--bubble:focus-visible {
  border-radius: 50%;
}

@media (prefers-reduced-motion: reduce) {
  .cv2-chat-launcher-window,
  .cv2-chat-launcher-icon svg {
    transition: none;
  }
}
</style>
