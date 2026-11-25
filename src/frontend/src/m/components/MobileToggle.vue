<script setup lang="ts">
import { f7Toggle as F7Toggle } from 'framework7-vue'
import { useTemplateRef, watchPostEffect } from 'vue'

const props = defineProps<{ label: string; checked: boolean; disabled: boolean; immediate?: boolean }>()
const emit = defineEmits<{ toggle: []; 'update:checked': [checked: boolean] }>()
const control = useTemplateRef<InstanceType<typeof F7Toggle>>('control')

// Framework7 renders the checkbox inside its label and has no input-attribute slot.
watchPostEffect(() => {
  const root = control.value?.$el as HTMLElement | undefined
  const input = root?.querySelector('input')
  input?.setAttribute('role', 'switch')
  input?.setAttribute('aria-label', props.label)
  input?.setAttribute('aria-checked', String(props.checked))
})

// Server-backed switches display the confirmed value. Drafts follow the native change event.
function onClick(event: MouseEvent): void {
  if (props.immediate) return
  event.preventDefault()
  if (!props.disabled) emit('toggle')
}
</script>

<template>
  <F7Toggle
    ref="control"
    class="m-toggle-control"
    :init="immediate"
    :checked="checked"
    :disabled="disabled"
    @click="onClick"
    @update:checked="emit('update:checked', $event)"
  />
</template>

<style scoped>
.m-toggle-control {
  flex: none;
  align-self: center;
}

.m-toggle-control :deep(input) {
  position: absolute;
  z-index: 2;
  display: block;
  inset: -8px 0;
  width: 100%;
  height: 44px;
  margin: 0;
  opacity: 0;
  cursor: pointer;
}

.m-toggle-control:has(:focus-visible) {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: 2px;
}
</style>
