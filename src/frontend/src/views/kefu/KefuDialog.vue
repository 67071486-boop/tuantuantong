<script setup lang="ts">
import { nextTick, useId } from 'vue'
import { f7Popup as F7Popup } from 'framework7-vue'

const open = defineModel<boolean>('open', { required: true })
defineProps<{ title: string }>()

const titleId = useId()
let trigger: HTMLElement | null = null

function focusPopup(): void {
  trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
  void nextTick(() => document.getElementById(titleId)?.parentElement
    ?.querySelector<HTMLElement>('[autofocus], button, input')?.focus())
}

function restoreFocus(): void {
  trigger?.focus()
  trigger = null
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return
  const focusable = Array.from((event.currentTarget as HTMLElement)
    .querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), a[href]'))
  const last = focusable[focusable.length - 1]
  const boundary = event.shiftKey ? focusable[0] : last
  if (document.activeElement !== boundary) return
  event.preventDefault()
  const next = event.shiftKey ? last : focusable[0]
  next?.focus()
}
</script>

<template>
  <F7Popup
    v-model:opened="open"
    class="kefu-dialog"
    :animate="false"
    backdrop
    close-by-backdrop-click
    close-on-escape
    role="dialog"
    aria-modal="true"
    :aria-labelledby="titleId"
    @popup:open="focusPopup"
    @popup:closed="restoreFocus"
    @keydown="onKeydown"
  >
    <div class="kefu-dialog-inner">
      <h2 :id="titleId">{{ title }}</h2>
      <slot v-if="open" />
    </div>
  </F7Popup>
</template>

<style scoped>
.kefu-dialog.popup {
  top: 50%;
  left: 50%;
  width: min(calc(100% - 24px), 396px);
  height: auto;
  max-height: calc(100dvh - 40px);
  margin: 0;
  transform: translate(-50%, -50%);
  overflow: auto;
  border-radius: 18px;
}
.kefu-dialog-inner { padding: 14px; }
h2 { margin: 0 0 12px; font-size: 17px; }
</style>
