<script setup lang="ts">
import { computed, inject, onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue'
import {
  fetchOrderChat,
  hasOrderChatConversation,
  joinOrderChat,
  reopenOrderChat,
  type OrderChatSummary,
  type OrderChatState,
} from '@/api/orderChat'
import { AuthExpiredError, BizError, HttpError } from '@/api/errors'
import V2OrderChatHistory from '@/v2/V2OrderChatHistory.vue'
import V2OrderChatStart from '@/v2/V2OrderChatStart.vue'
import type { FirstOrderMessageDraft } from './internalChat/firstOrderMessage'
import { openInternalChatPreview } from '@/v2/internalChat/launcherPreview'

const props = defineProps<{ orderNo: string; production?: boolean; enterOnLoad?: boolean }>()
const emit = defineEmits<{ enter: [orderNo: string, conversationId: number] }>()
const openConversation = inject(openInternalChatPreview, null)
const group = shallowRef<OrderChatState | null>(null)
const firstDraft = shallowRef<FirstOrderMessageDraft>({ text: '' })
const startedFromEmpty = ref(false)
const conversation = computed(() => group.value && hasOrderChatConversation(group.value) ? group.value : null)
const showStart = computed(() => group.value && (!conversation.value || (startedFromEmpty.value
  && Boolean(firstDraft.value.text || firstDraft.value.image || firstDraft.value.attempt))))
const canStartSend = computed(() => group.value?.can_send === true
  || Boolean(conversation.value?.can_join && !conversation.value.archived))
const loadingGroup = ref(false)
const joining = ref(false)
const reopening = ref(false)
const groupError = ref('')
const actionError = ref('')
const groupMissing = ref(false)
const historyKey = computed(() => JSON.stringify([group.value?.self_member_id, group.value?.archived,
  group.value?.members.map(member => [member.id, member.has_permission])]))
let generation = 0
let refreshing = false

function unavailable(message: string): void {
  generation++
  group.value = null
  groupError.value = message
  loadingGroup.value = false
}

function failureMessage(error: unknown): string {
  if (error instanceof HttpError && error.status === 403) return '订单权限已撤销'
  if (error instanceof HttpError && error.status === 503) return error.message === '暂时无法确认订单权限，请稍后重试'
    ? error.message : '内部沟通暂时不可用'
  return '订单群聊加载失败'
}

function isOrderChatMissing(error: unknown): boolean {
  return (error instanceof BizError && error.code === 1002) || (error instanceof HttpError && error.status === 404)
}

function unavailableAfterJoin(error: unknown): string | undefined {
  if (error instanceof AuthExpiredError) return error.message
  if (isOrderChatMissing(error)) return '订单群聊已不可用'
  if (!(error instanceof HttpError)) return
  if (error.status === 403 && [
    '订单权限已撤销',
    '没有聊天权限',
    '登录状态已变更，请刷新页面后再打开聊天',
  ].includes(error.message)) return error.message
  if (error.status === 503 && error.message === '暂时无法确认订单权限，请稍后重试') return error.message
}

async function fetchGroup(): Promise<OrderChatState> {
  const orderNo = props.orderNo
  const loaded = await fetchOrderChat(orderNo)
  if (props.enterOnLoad && (loaded.local_order_no || loaded.order_no) !== orderNo) {
    throw new Error('订单群聊加载失败')
  }
  return loaded
}

async function refreshGroup(): Promise<void> {
  if (!group.value || loadingGroup.value || refreshing || document.visibilityState === 'hidden') return
  const current = generation
  refreshing = true
  try {
    const loaded = await fetchGroup()
    if (current === generation) group.value = loaded
  } catch (error) {
    if (current === generation) unavailable(failureMessage(error))
  } finally { refreshing = false }
}

async function loadGroup(): Promise<void> {
  const current = ++generation
  group.value = null
  groupError.value = ''
  groupMissing.value = false
  loadingGroup.value = true
  actionError.value = ''
  joining.value = false
  try {
    const loaded = await fetchGroup()
    if (current !== generation) return
    group.value = loaded
    if (!hasOrderChatConversation(loaded)) startedFromEmpty.value = true
    else if (props.enterOnLoad) {
      const error = await openOrJoin()
      if (current === generation && error) actionError.value = error
    }
  } catch (error) {
    if (current !== generation) return
    groupMissing.value = isOrderChatMissing(error)
    groupError.value = failureMessage(error)
  } finally {
    if (current === generation) loadingGroup.value = false
  }
}

async function reopen(): Promise<void> {
  const target = conversation.value
  if (!target?.archived || !target.can_reopen || reopening.value) return
  const current = generation
  reopening.value = true
  actionError.value = ''
  try {
    const updated = await reopenOrderChat(props.orderNo)
    if (current !== generation) return
    if (updated.id !== target.id || updated.archived !== false) throw new Error('重开群聊失败，请重试')
    group.value = updated
  } catch (error) {
    if (current === generation) {
      if (error instanceof HttpError && error.message === '订单权限已撤销') unavailable(error.message)
      else actionError.value = error instanceof Error ? error.message : '重开群聊失败，请重试'
    }
  } finally { reopening.value = false }
}

async function openOrJoin(): Promise<string | undefined> {
  const target = conversation.value
  if (!target || (!props.enterOnLoad && !openConversation) || joining.value) return
  if (target.is_member) {
    enterConversation(target.id)
    return
  }
  if (target.can_join === false) return
  const current = generation
  joining.value = true
  try {
    const joined = await joinOrderChat(props.orderNo)
    if (current !== generation) return
    if (joined.id !== target.id || !joined.is_member) {
      return '加入会话失败，请重试'
    }
    group.value = joined
    enterConversation(target.id)
  } catch (error) {
    if (current !== generation) return
    const message = props.enterOnLoad ? unavailableAfterJoin(error) : undefined
    if (message) {
      unavailable(message)
      return
    }
    return (error instanceof Error && error.message) || '加入会话失败，请重试'
  } finally {
    if (current === generation) joining.value = false
  }
}

function enterConversation(conversationId: number): void {
  if (props.enterOnLoad) emit('enter', props.orderNo, conversationId)
  else openConversation?.(conversationId)
}

function firstSent(value: OrderChatSummary): void {
  if ((value.local_order_no || value.order_no) !== props.orderNo) return
  group.value = value
  startedFromEmpty.value = false
  enterConversation(value.id)
}

watch(() => props.orderNo, () => {
  firstDraft.value = { text: '' }
  startedFromEmpty.value = false
  void loadGroup()
}, { immediate: true })
let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => { refreshTimer = setInterval(() => { void refreshGroup() }, 3000) })
onScopeDispose(() => { generation++; clearInterval(refreshTimer) })
</script>

<template>
  <section class="order-chat" data-v2="order-chat-pane">
    <p v-if="loadingGroup" class="order-chat-state" role="status">正在加载订单群聊…</p>
    <p v-else-if="groupMissing" class="order-chat-state">暂无订单群聊</p>
    <div v-else-if="groupError" class="order-chat-state" role="alert">
      <p>{{ groupError }}</p>
      <button type="button" class="cv2-btn cv2-btn--sm" @click="loadGroup">重试</button>
    </div>
    <div v-else-if="group" class="order-chat-content">
      <div v-if="group.archived" class="order-chat-archive" role="status">
        <span>已归档 · 可查看历史消息</span>
        <button v-if="group.can_reopen" type="button" class="cv2-btn cv2-btn--sm"
          data-order-chat-reopen :disabled="reopening" @click="reopen">{{ reopening ? '正在重开…' : '重开群聊' }}</button>
      </div>
      <p v-if="actionError" class="order-chat-state" role="alert">{{ actionError }}</p>
      <V2OrderChatStart v-if="showStart" :key="orderNo" v-model="firstDraft" :order-no="orderNo"
        :can-send="canStartSend" :archived="group.archived" :has-conversation="conversation !== null" :existing-conversation="conversation" @sent="firstSent" />
      <V2OrderChatHistory
        v-if="conversation"
        :production="production"
        :key="historyKey"
        :order-no="orderNo"
        :group="conversation"
        :activate="openOrJoin"
        @unavailable="unavailable"
      />
    </div>
  </section>
</template>

<style scoped>
.order-chat {
  height: 100%;
  color: var(--tt2-ink);
}
.order-chat-state { margin: 18px; color: var(--tt2-dim); }
.order-chat-state p { margin: 0 0 8px; }
.order-chat-content { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.order-chat-content :deep(.order-chat-history) { flex: 1; }
.order-chat-archive { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 14px; color: var(--tt2-dim); border-bottom: 1px solid var(--tt2-line); }
</style>
