<script setup lang="ts">
import { inject, onDeactivated, provide, watch } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import OrderTabDialog from './OrderTabDialog.vue'
import { openInternalChatPreview } from './internalChat/launcherPreview'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; chatHandoff: [] }>()

const openChat = inject(openInternalChatPreview, null)
const route = inject(routeLocationKey, undefined)
const router = inject(routerKey, undefined)
let pendingConversation: number | undefined

function ownsActiveTab(): boolean {
  return !route || !router || route.fullPath === router.currentRoute.value.fullPath
}

if (openChat) provide(openInternalChatPreview, conversationId => {
  if (!props.modelValue || !ownsActiveTab() || conversationId === undefined || pendingConversation !== undefined) return
  pendingConversation = conversationId
  emit('chatHandoff')
  emit('update:modelValue', false)
})

watch(() => props.modelValue, visible => {
  if (visible) pendingConversation = undefined
}, { flush: 'sync' })

watch(ownsActiveTab, active => {
  if (!active) pendingConversation = undefined
}, { flush: 'sync' })

onDeactivated(() => { pendingConversation = undefined })

function afterClosed(): void {
  const conversationId = pendingConversation
  pendingConversation = undefined
  if (conversationId !== undefined && ownsActiveTab()) openChat?.(conversationId)
}
</script>

<template>
  <OrderTabDialog
    v-bind="$attrs"
    :model-value="modelValue"
    @update:model-value="(value: boolean) => { if (value !== modelValue) emit('update:modelValue', value) }"
    @closed="afterClosed"
  >
    <slot />
  </OrderTabDialog>
</template>
