<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, useTemplateRef, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import {
  f7Button as F7Button,
  f7Icon as F7Icon,
  f7Navbar as F7Navbar,
  f7Page as F7Page,
  f7Popup as F7Popup,
  f7Toolbar as F7Toolbar,
  f7Preloader as F7Preloader,
  f7ToolbarPane as F7ToolbarPane,
  f7Link as F7Link,
  f7ready,
} from 'framework7-vue'
import type { Popup } from 'framework7/types'
import { fetchWxPusherGuide } from '@/composables/useWxPusherBinding'
import { useSessionStore } from '@/stores/session'
import { HALL_SUBSCRIPTION_HASH, permittedTabs } from '../tabs'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()
const tabs = computed(() => session.status === 'ready' && session.user
  ? permittedTabs((codes) => session.canNode(codes))
  : [],
)
const activeTab = computed(() => tabs.value.find((tab) => tab.routeName === route.name))
const isHall = computed(() => route.name === 'm-hall')
const isMe = computed(() => route.name === 'm-me')
const hasCenteredTitle = computed(() =>
  isHall.value || route.name === 'm-mine' || route.name === 'm-channel' || isMe.value,
)
const tenantName = computed(() => session.user?.tenant_name || '团团通')
const toolbarHost = useTemplateRef<HTMLElement>('toolbarHost')
const scroll = useTemplateRef<HTMLElement>('scroll')
const shell = useTemplateRef<InstanceType<typeof F7Page>>('shell')
const hallHelpTrigger = useTemplateRef<InstanceType<typeof F7Button>>('hallHelpTrigger')
const showHallHelp = ref(false)
const hallHelpRequest = ref<symbol>()
const bindingUnavailable = ref(false)
const hallHelpId = useId()
const canBindNotifications = computed(() =>
  session.status === 'ready' && session.isWorker && session.canNode(['my_profile']),
)
const canOpenHallSubscription = computed(() => canBindNotifications.value && session.canNode(['grab_hall.take_order']))
let hallHelpPopup: Popup.Popup | undefined
const tabIcons: Record<string, string> = {
  'm-hall': 'square_grid_2x2',
  'm-mine': 'doc_text',
  'm-channel': 'bubble_left_bubble_right',
  'm-me': 'person_crop_circle',
}

watch(() => route.name, () => {
  if (scroll.value) scroll.value.scrollTop = 0
})

watch([
  () => route.fullPath, () => session.identityPlane, () => session.user?.id,
  canBindNotifications, canOpenHallSubscription,
], () => {
  hallHelpRequest.value = undefined
  hallHelpPopup?.close(false)
  showHallHelp.value = false
}, { flush: 'sync' })

function onHelpOpened(popup: Popup.Popup): void {
  hallHelpPopup = popup
  popup.el.querySelector<HTMLButtonElement>('button')?.focus()
}

function onHelpClosed(): void {
  hallHelpPopup = undefined
  if (isHall.value) hallHelpTrigger.value?.$el.focus()
}

function trapHelpFocus(event: KeyboardEvent): void {
  if (!(event.currentTarget instanceof HTMLElement)) return
  const buttons = event.currentTarget.querySelectorAll('button')
  const first = buttons[0]
  const last = buttons[buttons.length - 1]
  if (!first || !last || document.activeElement !== (event.shiftKey ? first : last)) return
  event.preventDefault()
  const target = event.shiftKey ? last : first
  target.focus()
}

async function openHallHelp(): Promise<void> {
  if (!isHall.value || showHallHelp.value || hallHelpRequest.value) return
  bindingUnavailable.value = false
  if (!canOpenHallSubscription.value) {
    showHallHelp.value = true
    return
  }
  const request = Symbol()
  hallHelpRequest.value = request
  try {
    const guide = await fetchWxPusherGuide('/api/auth/wxpusher', true).catch(() => null)
    if (hallHelpRequest.value !== request) return
    bindingUnavailable.value = guide?.binding_enabled === false
    if (guide?.binding_enabled === true && guide.bound === true) {
      await router.push({ name: 'm-me-notifications', hash: HALL_SUBSCRIPTION_HASH })
    } else {
      showHallHelp.value = true
    }
  } finally {
    if (hallHelpRequest.value === request) hallHelpRequest.value = undefined
  }
}

function goToNotifications(): void {
  if (!canBindNotifications.value || bindingUnavailable.value) return
  showHallHelp.value = false
  void router.push({ name: 'm-me-notifications' })
}

type TabbarElement = HTMLElement & { f7ToolbarOnPointer?: EventListener }
type IosToolbar = {
  init: (element: HTMLElement) => void
  destroy: (element: HTMLElement) => void
}
const toolbar = () => toolbarHost.value?.querySelector<TabbarElement>('.toolbar')
const dragEvents = ['pointerdown', 'pointermove', 'pointerup', 'pointercancel'] as const

let tabDrag: { id: number; x: number; y: number; routeName: typeof route.name; routeOnRelease: boolean } | undefined

function syncTabHighlight(): void {
  const element = toolbar()
  if (element) f7ready((f7) => f7.toolbar.setHighlight(element))
}

function insideToolbar(target: EventTarget | null): boolean {
  return target instanceof Node && toolbar()?.contains(target) === true
}

function beginTabDrag(event: PointerEvent): void {
  if (event.pointerType !== 'touch' || !event.isPrimary || !insideToolbar(event.target)) return
  tabDrag = { id: event.pointerId, x: event.clientX, y: event.clientY, routeName: route.name, routeOnRelease: false }
}

function trackTabDrag(event: PointerEvent): void {
  if (event.pointerType !== 'touch') return
  if (!event.isPrimary) {
    // 只拦底栏的其他触点，底栏外的事件继续交给页面。
    if (insideToolbar(event.target)) {
      // 第二指的 touchstart 也会改变 Framework7 用来校验 click 的目标。
      if (tabDrag) tabDrag.routeOnRelease = true
      if (tabDrag || event.type === 'pointerdown') event.stopPropagation()
    } else {
      cancelTabDrag()
    }
    return
  }
  if (tabDrag?.id !== event.pointerId || tabDrag.routeOnRelease) return
  tabDrag.routeOnRelease = event.clientX !== tabDrag.x || event.clientY !== tabDrag.y
}

function tabAt(x: number, y: number): string | undefined {
  const links = toolbar()?.querySelectorAll<HTMLElement>('.tab-link') ?? []
  return Array.from(links).find((link) => {
    const { left, right, top, bottom } = link.getBoundingClientRect()
    return x >= left && x < right && y >= top && y < bottom
  })?.dataset.tabRoute
}

async function goToTab(routeName: string): Promise<void> {
  try {
    await router.replace({ name: routeName })
  } finally {
    syncTabHighlight()
  }
}

function clickTab(event: MouseEvent, routeName: string): Promise<void> | undefined {
  // Framework7 会在抬手时合成 click，拖动切页由下面的触摸处理统一提交。
  if (!event.isTrusted && tabDrag?.routeOnRelease) return
  return goToTab(routeName)
}

function endTabDrag(event: PointerEvent): void {
  const drag = tabDrag
  if (drag?.id !== event.pointerId) return
  tabDrag = undefined
  if (!drag.routeOnRelease) return
  const target = event.type === 'pointerup' && drag.routeName === route.name
    ? tabAt(event.clientX, event.clientY)
    : undefined
  if (target && target !== route.name && tabs.value.some((tab) => tab.routeName === target)) {
    void goToTab(target)
  } else {
    syncTabHighlight()
  }
}

function cancelTabDrag(event?: FocusEvent | PointerEvent): void {
  if (!tabDrag || (event && 'pointerId' in event && event.pointerId !== tabDrag.id)) return
  const id = tabDrag.id
  tabDrag = undefined
  toolbar()?.f7ToolbarOnPointer?.(new PointerEvent('pointercancel', { pointerType: 'touch', pointerId: id }))
  syncTabHighlight()
}

onMounted(() => f7ready((f7) => {
  const element = toolbar()
  if (!element) return
  // Framework7 已给初次挂载的底栏绑定手势。Vue Router 回到布局时才需要重新绑定。
  if (!element.f7ToolbarOnPointer) {
    const iosToolbar = f7.toolbar as typeof f7.toolbar & IosToolbar
    iosToolbar.init(element)
  }
  // 抬手先让 Framework7 结束玻璃动画，再同步 Vue Router。
  for (const event of dragEvents) document.addEventListener(event, trackTabDrag, true)
  document.addEventListener('pointerup', endTabDrag)
  document.addEventListener('pointercancel', endTabDrag)
  window.addEventListener('blur', cancelTabDrag)
}))
onBeforeUnmount(() => {
  hallHelpRequest.value = undefined
  hallHelpPopup?.close(false)
  window.removeEventListener('blur', cancelTabDrag)
  for (const event of dragEvents) document.removeEventListener(event, trackTabDrag, true)
  document.removeEventListener('pointerup', endTabDrag)
  document.removeEventListener('pointercancel', endTabDrag)
  f7ready((f7) => {
    const element = toolbar()
    if (element) {
      const iosToolbar = f7.toolbar as typeof f7.toolbar & IosToolbar
      iosToolbar.destroy(element)
    }
  })
})
watch([activeTab, () => tabs.value.length], syncTabHighlight, { flush: 'post' })

async function onLogout(): Promise<void> {
  await session.logout()
  void router.replace({ name: 'm-login' })
}
</script>

<template>
  <F7Page ref="shell" class="m-shell" :page-content="false">
    <header>
      <F7Navbar
        :key="hasCenteredTitle ? 'centered' : 'tabs'"
        class="m-navbar"
        :class="{ 'm-navbar--centered': hasCenteredTitle }"
        :title="hasCenteredTitle ? undefined : tenantName"
        :sliding="false"
      >
        <template v-if="hasCenteredTitle" #left>
          <span class="m-navbar__tenant" :title="tenantName">{{ tenantName }}</span>
        </template>
        <template v-if="hasCenteredTitle" #title>
          <h1 class="m-navbar__title">{{ activeTab?.label }}</h1>
        </template>
        <template v-if="!isMe" #right>
          <F7Button
            v-if="isHall"
            ref="hallHelpTrigger"
            type="button"
            class="m-navbar__hall-help"
            preloader
            :preloader-size="16"
            :loading="hallHelpRequest !== undefined"
            aria-haspopup="dialog"
            :aria-expanded="showHallHelp"
            :aria-controls="hallHelpId"
            :aria-busy="hallHelpRequest !== undefined"
            @click="openHallHelp"
          >如何快速抢单</F7Button>
          <template v-else>
            <span v-if="session.user?.display_name" class="m-navbar__account">{{ session.user.display_name }}</span>
            <F7Button type="button" small class="m-navbar__exit" @click="onLogout">退出</F7Button>
          </template>
        </template>
      </F7Navbar>
    </header>

    <main ref="scroll" class="m-shell__scroll">
      <div class="ptr-preloader" aria-hidden="true"><F7Preloader /></div>
      <div class="m-shell__body">
        <h1 v-if="!hasCenteredTitle" class="m-shell__title">{{ activeTab?.label }}</h1>
        <RouterView />
      </div>
    </main>

    <F7Popup
      v-if="isHall && shell"
      v-model:opened="showHallHelp"
      class="m-hall-help"
      :container-el="shell.$el"
      :animate="false"
      tablet-fullscreen
      backdrop
      close-by-backdrop-click
      close-on-escape
      @popup:opened="onHelpOpened"
      @popup:closed="onHelpClosed"
    >
      <section
        :id="hallHelpId"
        class="m-hall-help__panel"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="`${hallHelpId}-title`"
        :aria-describedby="`${hallHelpId}-description`"
        @keydown.tab="trapHelpFocus"
        @keydown.esc.stop.prevent="showHallHelp = false"
      >
        <h2 :id="`${hallHelpId}-title`">如何快速抢单</h2>
        <p :id="`${hallHelpId}-description`">绑定 WxPusher 后，在「消息通知」的「抢单订阅」里开启实时新单通知，选好要订阅的游戏。按需要设置价格区间和关键词，再点「保存订阅」。收到通知后可直接点开抢单。</p>
        <p v-if="bindingUnavailable">工作室暂未开放绑定。</p>
        <footer class="m-hall-help__actions">
          <F7Button type="button" tonal round @click="showHallHelp = false">关闭</F7Button>
          <F7Button v-if="canBindNotifications && !bindingUnavailable" type="button" fill round @click="goToNotifications">去绑定</F7Button>
        </footer>
      </section>
    </F7Popup>

    <nav ref="toolbarHost" aria-label="打手端导航" @pointerdown="beginTabDrag" @lostpointercapture="cancelTabDrag">
      <F7Toolbar class="m-tabbar" bottom tabbar icons>
        <F7ToolbarPane>
          <F7Link
            v-for="tab in tabs"
            :key="tab.routeName"
            class="external"
            :href="router.resolve({ name: tab.routeName }).href"
            :data-tab-route="tab.routeName"
            tab-link
            :tab-link-active="activeTab === tab"
            :aria-current="activeTab === tab ? 'page' : undefined"
            @click.prevent="clickTab($event, tab.routeName)"
          >
            <F7Icon :f7="tabIcons[tab.routeName]" :size="23" class="m-tabbar__icon" aria-hidden="true" />
            <span class="tabbar-label">{{ tab.label }}</span>
          </F7Link>
        </F7ToolbarPane>
      </F7Toolbar>
    </nav>
  </F7Page>
</template>

<style scoped>
.m-tabbar__icon,
.tabbar-label {
  /* Framework7 在抬手时点击链接本身，触摸也需命中同一元素。 */
  pointer-events: none;
}

.m-navbar__exit {
  flex: none;
  width: auto;
}

.m-navbar__hall-help {
  flex: none;
  width: auto;
  min-height: 44px;
  padding-inline: 6px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.m-navbar--centered :deep(.navbar-inner) {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
}

.m-navbar--centered :deep(.title) {
  position: static;
  font-size: 17px;
}

.m-navbar--centered :deep(.navbar-inner > :is(.left, .right)) {
  backdrop-filter: none;
  background-color: transparent;
  box-shadow: none;
}

.m-navbar--centered :deep(.left) {
  justify-self: start;
  max-width: 100%;
}

.m-navbar--centered :deep(.right) {
  justify-self: end;
  max-width: 100%;
}

.m-navbar__tenant {
  min-width: 0;
  overflow: hidden;
  padding-inline: 8px;
  color: var(--m-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.m-navbar__title {
  margin: 0;
  font: inherit;
}

.m-hall-help.popup {
  position: fixed;
  inset: 0;
  width: min(calc(100% - 32px), 344px);
  height: fit-content;
  max-height: calc(100dvh - 32px);
  margin: auto;
  overflow: auto;
  border-radius: 24px;
}

.m-hall-help__panel {
  padding: 20px;
}

.m-hall-help__panel h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.m-hall-help__panel p {
  margin: 0;
  color: var(--m-muted);
  font-size: 14px;
  line-height: 1.6;
}

.m-hall-help__actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.m-hall-help__actions .button {
  flex: 1;
  min-height: 44px;
}
</style>
