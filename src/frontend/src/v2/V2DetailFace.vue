<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { routeLocationKey } from 'vue-router'
import V2DailiantongPane from '@/v2/V2DailiantongPane.vue'
import V2OrderChatPane from '@/v2/V2OrderChatPane.vue'
import V2WecomGroupPane from '@/v2/V2WecomGroupPane.vue'
import { internalChatProduction } from '@/v2/internalChat/launcherPreview'
import { orderChatPreviewEnabled } from '@/v2/internalChat/orderChatPreview'

const props = defineProps<{
  orderNo: string
  sourceOrderNo?: string | undefined
  orderChatOnly?: boolean
  productionOrderChat?: boolean
  hasDailiantongHistory?: boolean | undefined
}>()

type DetailFace = 'local' | 'dlt' | 'wecom' | 'order-chat'
const face = ref<DetailFace>('local')
const dltVisited = ref(false)
const workspaceChat = inject(internalChatProduction, null)
const productionOrderChat = computed(() => props.productionOrderChat === true || workspaceChat?.value === true)
const hasOrderChat = computed(() => productionOrderChat.value || orderChatPreviewEnabled())
const route = inject(routeLocationKey, null)

watch(() => props.orderNo, () => {
  face.value = 'local'
  dltVisited.value = false
})

watch(() => hasOrderChat.value && route?.params.order_no === props.orderNo && route.query.orderChat === '1', fromChat => {
  if (fromChat) face.value = 'order-chat'
}, { immediate: true })

function selectFace(next: DetailFace): void {
  if (next === 'dlt') dltVisited.value = true
  face.value = next
}

function focusOrderChat(event: Event): void {
  if (hasOrderChat.value && event instanceof CustomEvent && event.detail?.orderNo === props.orderNo && route?.params.order_no === props.orderNo) {
    face.value = 'order-chat'
  }
}

onMounted(() => {
  window.addEventListener('tt:order-chat-focus', focusOrderChat)
})
onUnmounted(() => {
  window.removeEventListener('tt:order-chat-focus', focusOrderChat)
})
</script>

<template>
  <div class="dtl-face">
    <div class="dtl-tabs" role="tablist" aria-label="订单详情">
      <button type="button" role="tab" :aria-selected="face === 'local'" class="dtl-tab" :class="{ 'dtl-tab--on': face === 'local' }" data-v2="tab-local" @click="selectFace('local')">本系统订单</button>
      <button v-if="!orderChatOnly" type="button" role="tab" :aria-selected="face === 'wecom'" class="dtl-tab" :class="{ 'dtl-tab--on': face === 'wecom' }" data-v2="tab-wecom" @click="selectFace('wecom')">企微群聊</button>
      <button v-if="!orderChatOnly && hasDailiantongHistory" type="button" role="tab" :aria-selected="face === 'dlt'" class="dtl-tab" :class="{ 'dtl-tab--on': face === 'dlt' }" data-v2="tab-dlt" @click="selectFace('dlt')">代练通</button>
      <button v-if="hasOrderChat" type="button" role="tab" :aria-selected="face === 'order-chat'" class="dtl-tab" :class="{ 'dtl-tab--on': face === 'order-chat' }" data-v2="tab-order-chat" @click="selectFace('order-chat')">订单群聊</button>
    </div>
    <div v-show="face === 'local'" class="dtl-face-content">
      <slot />
    </div>
    <div v-if="hasDailiantongHistory && dltVisited" v-show="face === 'dlt'" class="dtl-face-content"><V2DailiantongPane :key="orderNo" :order-no="orderNo" /></div>
    <div v-if="face === 'wecom'" class="dtl-face-content"><V2WecomGroupPane :order-no="orderNo" :source-order-no="sourceOrderNo" /></div>
    <div v-if="face === 'order-chat'" class="dtl-face-content" role="tabpanel" aria-label="订单群聊">
      <V2OrderChatPane :production="productionOrderChat" :key="orderNo" :order-no="orderNo" />
    </div>
  </div>
</template>
