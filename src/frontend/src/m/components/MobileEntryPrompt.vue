<script setup lang="ts">
import type { Popup } from 'framework7/types'
import { f7Popup as F7Popup } from 'framework7-vue'
import { onBeforeUnmount, onMounted, ref, useId, useTemplateRef } from 'vue'

defineProps<{ title: string; description: string }>()
const emit = defineEmits<{ dismiss: [] }>()
const host = useTemplateRef<HTMLElement>('host')
const id = useId()
const ready = ref(false)
const ownedInert = new Set<HTMLElement>()
let popup: Popup.Popup | undefined
let previousFocus: Element | null = null
let backgroundObserver: MutationObserver | undefined

function makeInert(element: Element): void {
  if (!(element instanceof HTMLElement) || element.hasAttribute('inert')) return
  element.setAttribute('inert', '')
  ownedInert.add(element)
}

function syncBackground(): void {
  if (!popup) {
    // A chat dialog in the browser's top layer must finish before the Framework7 popup.
    ready.value = !document.querySelector('body > dialog.m-chat-screen[open]')
    return
  }
  for (const sibling of host.value?.parentElement?.children ?? []) {
    if (sibling !== host.value) makeInert(sibling)
  }
  // Back navigation can reopen the native chat dialog above this popup; it must remain operable.
  for (const bubble of document.querySelectorAll('body > .m-chat-bubble-area')) makeInert(bubble)
}

onMounted(() => {
  backgroundObserver = new MutationObserver(syncBackground)
  const root = host.value?.parentElement
  if (root) backgroundObserver.observe(root, { childList: true })
  backgroundObserver.observe(document.body, { childList: true })
  // The open flag changes before the native close event rather than removing the dialog.
  document.addEventListener('close', syncBackground, true)
  syncBackground()
})

function onOpened(instance: Popup.Popup): void {
  popup = instance
  previousFocus = document.activeElement
  instance.el.focus({ preventScroll: true })
  syncBackground()
}

function restoreBackground(): void {
  popup = undefined
  for (const element of ownedInert) element.removeAttribute('inert')
  ownedInert.clear()
  if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
    previousFocus.focus({ preventScroll: true })
  }
  previousFocus = null
}

function keepFocusInPrompt(event: KeyboardEvent): void {
  const buttons = popup?.el.querySelectorAll<HTMLElement>('a.button[href], button.button:not(:disabled)')
  const first = buttons?.[0]
  const last = buttons?.[buttons.length - 1]
  const active = document.activeElement
  if (!first || !last || (active !== popup?.el && active !== (event.shiftKey ? first : last))) return
  event.preventDefault()
  const next = event.shiftKey ? last : first
  next.focus()
}

onBeforeUnmount(() => {
  backgroundObserver?.disconnect()
  document.removeEventListener('close', syncBackground, true)
  popup?.close(false)
  restoreBackground()
})
</script>

<template>
  <div ref="host" class="m-entry-prompt-host">
    <F7Popup
      v-if="host && ready"
      class="m-entry-prompt"
      opened
      tablet-fullscreen
      backdrop
      :container-el="host"
      :animate="false"
      :close-by-backdrop-click="false"
      :close-on-escape="false"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      :aria-labelledby="`${id}-title`"
      :aria-describedby="`${id}-description`"
      @popup:opened="onOpened"
      @popup:closed="restoreBackground"
      @keydown.tab="keepFocusInPrompt"
      @keydown.esc.stop.prevent="emit('dismiss')"
    >
      <h2 :id="`${id}-title`">{{ title }}</h2>
      <p :id="`${id}-description`">{{ description }}</p>
      <div class="m-entry-prompt__actions"><slot /></div>
    </F7Popup>
  </div>
</template>

<style scoped>
.m-entry-prompt-host {
  position: relative;
  z-index: 14000;
}

.m-entry-prompt.popup {
  --f7-button-large-height: 48px;
  --f7-button-border-radius: 14px;
  position: fixed;
  inset: 0;
  width: min(392px, calc(100% - 32px));
  height: fit-content;
  max-height: calc(100dvh - 32px);
  margin: auto;
  padding: 28px 24px 20px;
  overflow-y: auto;
  border-radius: 24px;
  outline: none;
  background: var(--f7-page-bg-color, #fff);
  color: #25202a;
  text-align: center;
  box-shadow: 0 16px 56px rgb(27 16 35 / 18%);
}

.m-entry-prompt h2 {
  margin: 0 0 10px;
  font-size: 22px;
  line-height: 1.35;
}

.m-entry-prompt p {
  margin: 0 0 22px;
  color: var(--m-muted, #67636d);
  font-size: 15px;
  line-height: 1.65;
}

.m-entry-prompt__actions {
  display: grid;
  gap: 8px;
}
</style>
