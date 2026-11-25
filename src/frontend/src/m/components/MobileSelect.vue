<script setup lang="ts" generic="T extends string | number">
import { computed, onBeforeUnmount, ref, useId, useTemplateRef } from 'vue'
import {
  f7Button as F7Button, f7Icon as F7Icon, f7List as F7List, f7ListItem as F7ListItem,
  f7Popover as F7Popover, f7Sheet as F7Sheet, f7,
} from 'framework7-vue'
import type { Popover, Sheet } from 'framework7/types'

defineOptions({ inheritAttrs: false })

const props = defineProps<{
  label: string
  options: readonly { value: T; label: string }[]
  modelValue: T | null
  triggerLabel?: string
  presentation?: 'sheet' | 'popover'
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const dialogId = useId()
const host = useTemplateRef<HTMLElement>('host')
const trigger = useTemplateRef<InstanceType<typeof F7Button>>('trigger')
const panel = useTemplateRef<HTMLElement>('panel')
const opened = ref(false)
const usePopover = computed(() => props.presentation === 'popover')
const selectedLabel = computed(() => props.options.find(({ value }) => value === props.modelValue)?.label)
let sheet: Sheet.Sheet | undefined
let popover: Popover.Popover | undefined
let restorePopoverFocus = true
let unmounting = false
let openAtPointerDown = false

function onTriggerPointerDown(): void {
  openAtPointerDown = opened.value
}

function openSelect(event: MouseEvent): void {
  if (!usePopover.value) {
    opened.value = true
    return
  }
  // Framework7 can close on the trigger's click before Vue handles it.
  const toggleOff = opened.value || (event.detail > 0 && openAtPointerDown)
  openAtPointerDown = false
  if (toggleOff) {
    closeSelect()
    return
  }
  const element = panel.value?.closest<HTMLElement>('.popover')
  const anchor = trigger.value?.$el
  if (element && anchor) f7.popover.open(element, anchor, false)
}

function onPopoverOpen(instance: Popover.Popover): void {
  popover = instance
  restorePopoverFocus = true
  window.addEventListener('scroll', onOutsideScroll, true)
  // Vue’s wrapper does not pass the animation option to Framework7.
  instance.params.animate = false
}

function onPopoverOpened(): void {
  requestAnimationFrame(() => {
    if (opened.value && !unmounting) focusChoice()
  })
}

function closeSelect(restoreFocus = true): void {
  if (!usePopover.value) {
    opened.value = false
    return
  }
  if (!popover?.opened) return
  restorePopoverFocus = restoreFocus
  const element = popover.el
  // iOS removes this class until the next frame; close() needs it for cleanup.
  if (!element.classList.contains('modal-in')) {
    element.classList.add('modal-in')
    requestAnimationFrame(() => {
      if (!opened.value || unmounting) element.classList.remove('modal-in')
    })
  }
  popover.close(false)
}

function onOutsideScroll(event: Event): void {
  if (event.target instanceof Node && host.value?.contains(event.target)) return
  closeSelect()
}

function onFocusOut(event: FocusEvent): void {
  if (!usePopover.value || !(event.relatedTarget instanceof Node)) return
  if (!host.value?.contains(event.relatedTarget)) closeSelect(false)
}

function onEscape(event: KeyboardEvent): void {
  if (!usePopover.value || !opened.value) return
  event.stopPropagation()
  event.preventDefault()
  closeSelect()
}

function choose(value: T): void {
  if (value !== props.modelValue) emit('update:modelValue', value)
  closeSelect()
}

function focusChoice(): void {
  const target = panel.value?.querySelector<HTMLElement>('[aria-pressed="true"]')
    ?? panel.value?.querySelector<HTMLElement>('.m-select__close')
  target?.focus()
}

function trapFocus(event: KeyboardEvent): void {
  if (usePopover.value) return
  const buttons = panel.value?.querySelectorAll<HTMLButtonElement>('button')
  if (!buttons?.length) return
  const first = buttons[0]
  const last = buttons[buttons.length - 1]
  const from = event.shiftKey ? first : last
  if (document.activeElement !== from) return
  event.preventDefault()
  const target = event.shiftKey ? last : first
  target?.focus()
}

function onClosed(): void {
  sheet = undefined
  popover = undefined
  window.removeEventListener('scroll', onOutsideScroll, true)
  if (unmounting) return
  const focus = document.activeElement
  if (!usePopover.value || (restorePopoverFocus && (focus === document.body || panel.value?.contains(focus)))) {
    trigger.value?.$el.focus({ preventScroll: true })
  }
}

onBeforeUnmount(() => {
  unmounting = true
  if (usePopover.value) closeSelect()
  else sheet?.close(false)
})
</script>

<template>
  <div
    ref="host"
    class="m-select"
    :class="{ 'm-select--fixed-label': triggerLabel }"
    @focusout="onFocusOut"
  >
    <F7Button
      ref="trigger"
      v-bind="$attrs"
      type="button"
      outline
      class="m-select__trigger"
      :disabled.prop="options.length === 0"
      :aria-label="selectedLabel ? `${label}：${selectedLabel}` : label"
      :aria-expanded="opened"
      :aria-controls="dialogId"
      aria-haspopup="dialog"
      @pointerdown="onTriggerPointerDown"
      @click="openSelect"
      @keydown.esc="onEscape"
    >
      <span class="m-select__value">{{ triggerLabel ?? selectedLabel ?? label }}</span>
      <F7Icon v-if="!triggerLabel" f7="chevron_down" :size="11" aria-hidden="true" />
    </F7Button>

    <component
      :is="usePopover ? F7Popover : F7Sheet"
      v-if="host"
      v-bind="usePopover
        ? { backdrop: false, closeByOutsideClick: true }
        : { opened, animate: false, backdrop: true, closeByBackdropClick: true }"
      :class="usePopover ? 'm-select__popover' : 'm-select__sheet'"
      :container-el="host"
      close-on-escape
      @update:opened="opened = $event"
      @popover:open="onPopoverOpen"
      @popover:opened="onPopoverOpened"
      @popover:closed="onClosed"
      @sheet:open="sheet = $event"
      @sheet:opened="focusChoice"
      @sheet:closed="onClosed"
    >
      <div
        :id="dialogId"
        ref="panel"
        class="m-select__panel"
        role="dialog"
        :aria-modal="usePopover ? undefined : true"
        :aria-label="label"
        @keydown.esc="onEscape"
        @keydown.tab="trapFocus"
      >
        <header>
          <h2>{{ label }}</h2>
          <F7Button type="button" class="m-select__close" @click="closeSelect()">取消</F7Button>
        </header>
        <F7List simple-list class="m-select__options">
          <F7ListItem v-for="option in options" :key="option.value">
            <F7Button
              type="button"
              class="m-select__option"
              :data-option-value="option.value"
              :aria-pressed="option.value === modelValue"
              @click="choose(option.value)"
            >
              <span>{{ option.label }}</span>
              <F7Icon v-if="option.value === modelValue" f7="checkmark" :size="16" aria-hidden="true" />
            </F7Button>
          </F7ListItem>
        </F7List>
      </div>
    </component>
  </div>
</template>

<style scoped>
.m-select {
  min-width: 0;
  max-width: var(--m-select-max-width, 134px);
}

.m-select .m-select__trigger {
  --f7-button-height: 40px;
  --f7-button-border-radius: 12px;
  --f7-button-border-width: 1px;
  --f7-button-border-color: var(--m-border);
  --f7-button-text-color: var(--f7-text-color);
  --f7-button-bg-color: var(--f7-page-bg-color);
  --f7-button-font-size: 13px;
  justify-content: space-between;
  gap: 7px;
  width: 100%;
  padding-inline: 9px;
}

.m-select--fixed-label .m-select__trigger {
  justify-content: center;
}

.m-select__value {
  overflow: hidden;
  text-overflow: ellipsis;
}

.m-select__sheet.sheet-modal {
  --f7-sheet-height: auto;
  --f7-sheet-border-radius: 24px;
  --f7-sheet-bg-color: var(--f7-page-bg-color);
  inset-inline: 0;
  width: min(100%, 540px);
  margin-inline: auto;
}

.m-select__popover.popover {
  --f7-popover-width: min(260px, calc(100vw - 20px));
  position: fixed;
}

.m-select__popover .m-select__panel {
  max-height: min(52dvh, 320px);
  padding: 0 6px 6px;
}

.m-select__panel {
  display: flex;
  flex-direction: column;
  max-height: 80dvh;
  padding: 8px 16px max(10px, env(safe-area-inset-bottom));
}

.m-select__panel header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-inline: 10px;
  border-bottom: 1px solid var(--m-border);
}

.m-select__panel h2 {
  margin: 0;
  font-size: 17px;
}

.m-select .m-select__close {
  width: auto;
  min-height: 44px;
}

.m-select .m-select__options {
  margin: 0;
  overflow: auto;
  overscroll-behavior: contain;
}

.m-select__options :deep(li) {
  padding: 0;
}

.m-select .m-select__option {
  --f7-button-height: 44px;
  --f7-button-font-size: 16px;
  --f7-button-text-color: var(--f7-text-color);
  justify-content: space-between;
  gap: 10px;
  height: auto;
  min-height: var(--f7-button-height);
  padding-block: 6px;
  text-align: start;
  white-space: normal;
  overflow-wrap: anywhere;
}

.m-select .m-select__option[aria-pressed="true"] {
  --f7-button-text-color: var(--f7-theme-color);
}
</style>
