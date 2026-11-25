<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId } from 'vue'
import { fetchOrderChat, hasOrderChatConversation, joinOrderChat, sendOrderChatMessage, type OrderChatSummary } from '@/api/orderChat'
import { AuthExpiredError, HttpError } from '@/api/errors'
import { encodeFirstOrderImage, type FirstOrderMessageDraft } from './internalChat/firstOrderMessage'

const props = defineProps<{ orderNo: string; canSend: boolean; archived?: boolean; hasConversation?: boolean; existingConversation?: OrderChatSummary | null }>()
const draft = defineModel<FirstOrderMessageDraft>({ required: true })
const emit = defineEmits<{ sent: [conversation: OrderChatSummary] }>()
const messageId = useId()
const sending = ref(false)
const readingImage = ref(false)
const error = ref('')
const enabled = computed(() => props.canSend && !sending.value && !readingImage.value
  && Boolean(draft.value.text.trim() || draft.value.image))
let retired = false

function updateText(event: Event): void {
  draft.value = { ...draft.value, text: (event.target as HTMLTextAreaElement).value }
  error.value = ''
}

async function readImage(file: File): Promise<void> {
  if (!props.canSend || sending.value || readingImage.value) return
  readingImage.value = true
  error.value = ''
  try {
    const image = await encodeFirstOrderImage(file)
    if (!retired) draft.value = { ...draft.value, image, imageName: file.name }
  } catch (reason) {
    if (!retired) error.value = reason instanceof Error ? reason.message : '读取图片失败'
  } finally { if (!retired) readingImage.value = false }
}

function chooseImage(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) void readImage(file)
}

function pasteImage(event: ClipboardEvent): void {
  const clipboard = event.clipboardData
  const file = clipboard?.files[0] ?? Array.from(clipboard?.items ?? [])
    .find(item => item.kind === 'file')?.getAsFile()
  if (!file) return
  event.preventDefault()
  void readImage(file)
}

function removeImage(): void {
  draft.value = { ...draft.value, image: undefined, imageName: undefined }
}

class FirstMessageStateError extends Error {}

async function prepareExisting(orderNo: string): Promise<void> {
  const current = await fetchOrderChat(orderNo)
  if (retired || props.orderNo !== orderNo) return
  if (!hasOrderChatConversation(current)) throw new FirstMessageStateError('会话状态已更新，请重试')
  if (current.archived) throw new FirstMessageStateError('群聊已归档，仅可查看历史消息')
  if (current.is_member) {
    if (current.can_send === false) throw new FirstMessageStateError('当前仅可查看聊天记录')
    return
  }
  if (!current.can_join) throw new FirstMessageStateError('当前仅可查看聊天记录')
  const joined = await joinOrderChat(orderNo)
  if (retired || props.orderNo !== orderNo) return
  if (joined.id !== current.id || !joined.is_member || joined.archived || joined.can_send === false) {
    throw new FirstMessageStateError('会话状态已更新，请重试')
  }
}

async function send(): Promise<void> {
  if (!enabled.value) return
  const orderNo = props.orderNo
  const previous = draft.value.attempt
  const unchanged = previous?.text === draft.value.text.trim() && previous?.image?.mime === draft.value.image?.mime
    && previous?.image?.data === draft.value.image?.data
  const attempt = unchanged && previous ? previous : {
    text: draft.value.text.trim(), client_id: crypto.randomUUID(),
    ...(draft.value.image && { image: draft.value.image }),
  }
  draft.value = { ...draft.value, attempt }
  sending.value = true
  error.value = ''
  try {
    if (props.existingConversation) await prepareExisting(orderNo)
    if (retired || props.orderNo !== orderNo) return
    let result
    try {
      result = await sendOrderChatMessage(orderNo, attempt)
    } catch (reason) {
      if (!(reason instanceof HttpError) || reason.status !== 409 || reason.message !== '请先加入会话') throw reason
      await prepareExisting(orderNo)
      if (retired || props.orderNo !== orderNo) return
      result = await sendOrderChatMessage(orderNo, attempt)
    }
    if (retired || props.orderNo !== orderNo) return
    if (!result?.conversation || !hasOrderChatConversation(result.conversation)
      || !result.conversation.is_member || !Array.isArray(result.messages) || !result.messages.length
      || result.messages.some(message => !Number.isSafeInteger(message.id) || message.id <= 0
        || message.conversation_id !== result.conversation.id)) throw new Error('消息接收结果不完整')
    draft.value = { text: '' }
    emit('sent', result.conversation)
  } catch (reason) {
    if (retired || props.orderNo !== orderNo) return
    error.value = reason instanceof FirstMessageStateError || reason instanceof AuthExpiredError ? reason.message : reason instanceof HttpError && reason.status === 403 ? reason.message
      : reason instanceof HttpError && reason.status === 503 && reason.message === '暂时无法确认订单权限，请稍后重试'
        ? reason.message : reason instanceof HttpError && (reason.status === 400 || reason.status === 409) ? reason.message : '内部沟通暂时不可用'
  } finally { if (!retired) sending.value = false }
}

onBeforeUnmount(() => { retired = true })
</script>

<template>
  <div class="order-chat-start" data-order-chat-start>
    <p class="order-chat-start-state">{{ hasConversation ? '可进入已有会话继续沟通' : archived ? '已归档 · 暂无聊天消息' : '暂无聊天消息' }}</p>
    <template v-if="canSend">
      <label :for="messageId">发送消息后进入会话</label>
      <textarea :id="messageId" :value="draft.text" rows="3" maxlength="4000"
        placeholder="输入消息" :disabled="sending" @input="updateText" @paste="pasteImage" />
      <div v-if="draft.image" class="order-chat-start-image">
        <img :src="`data:${draft.image.mime};base64,${draft.image.data}`" alt="待发送的聊天图片">
        <span>{{ draft.imageName }}</span>
        <button type="button" :disabled="sending" @click="removeImage">移除图片</button>
      </div>
      <div class="order-chat-start-actions">
        <label class="order-chat-start-file">
          {{ readingImage ? '正在读取图片…' : '添加图片' }}
          <input type="file" aria-label="选择聊天图片"
            :disabled="sending || readingImage" @change="chooseImage">
        </label>
        <button type="button" class="cv2-btn cv2-btn--sm" data-order-chat-first-send
          :disabled="!enabled" @click="send">{{ sending ? '正在发送…' : '发送并加入会话' }}</button>
      </div>
    </template>
    <p v-else-if="!archived" class="order-chat-start-state">当前仅可查看聊天记录</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.order-chat-start { display: grid; gap: 10px; padding: 18px; color: var(--tt2-ink, var(--f7-text-color)); }
.order-chat-start p { margin: 0; }
.order-chat-start-state { color: var(--tt2-dim, var(--f7-list-item-subtitle-text-color)); }
.order-chat-start textarea { width: 100%; box-sizing: border-box; resize: vertical; padding: 10px; border: 1px solid var(--tt2-line, var(--f7-list-item-border-color)); border-radius: 4px; font: inherit; color: inherit; background: var(--tt2-panel, var(--f7-page-bg-color)); }
.order-chat-start-actions, .order-chat-start-image { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.order-chat-start-actions { justify-content: space-between; }
.order-chat-start-file { position: relative; cursor: pointer; color: var(--tt2-primary, var(--f7-theme-color)); }
.order-chat-start-file input { position: absolute; inset: 0; opacity: 0; width: 100%; cursor: pointer; }
.order-chat-start-image img { width: 64px; height: 64px; object-fit: contain; }
.order-chat-start-image button { border: 0; background: transparent; color: var(--tt2-primary, var(--f7-theme-color)); cursor: pointer; }
.order-chat-start [role='alert'] { color: var(--tt2-st-error, var(--f7-color-red)); }
</style>
