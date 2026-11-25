<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { chatRequest as request } from '@/api/chatTransport'
import { writeMobileRedirectSuppressed } from '@/mobileRedirect'
import { useSessionStore } from '@/stores/session'
import { wxpusherChatAppLink, wxpusherChatSitePath } from '../utils/wxpusherOrderLink'

interface ChatHistory {
  kind?: 'direct'
  peer_name?: string
  order_no: string
  remark: string
  senders: { id: number; name: string; studio_name: string }[]
  self_member_id: number
  can_reply: boolean
  messages: Message[]
  has_newer: boolean
}

interface Message {
  id: number
  seq: number
  sender_id: number
  text: string
  revoked: boolean
  image?: { mime: string }
}

const route = useRoute()
const session = useSessionStore()
const chat = ref<ChatHistory | null>(null)
const messages = ref<Message[]>([])
const loading = ref(false)
const hasNewer = ref(false)
const failed = ref(false)
const actionError = ref('')
const draft = ref('')
const sending = ref(false)
const replyTo = ref<Message | null>(null)
const highlighted = ref<HTMLLIElement | null>(null)
const imagePreview = ref<{ id: number; src: string } | null>(null)
let pendingSend: { text: string; reply_to_id: number | null; client_id: string } | null = null
let generation = 0

const chatPath = computed(() => wxpusherChatSitePath(route.params.conversationId, route.query.message, route.query.open))
const conversationUrl = computed(() => chatPath.value === null ? null
  : `/api/internal-chat/notification-conversations/${route.params.conversationId}/`)
const chatTitle = computed(() => {
  const conversation = chat.value
  if (!conversation) return '内部沟通'
  return conversation.remark || (conversation.kind === 'direct'
    ? conversation.peer_name || '单聊' : `订单-${conversation.order_no}`)
})
const appLink = computed(() => chatPath.value === null || !session.isWorker
  ? undefined : wxpusherChatAppLink(chatPath.value.replace(/&open=web$/, ''), route.query.entry))

function author(message: Message): string {
  if (chat.value?.kind === 'direct' && message.sender_id === chat.value.self_member_id) return '我'
  const sender = chat.value?.senders.find((member) => member.id === message.sender_id)
  if (!sender) return chat.value?.kind === 'direct' ? '聊天成员' : '群成员'
  return sender.studio_name ? `${sender.name} · ${sender.studio_name}` : sender.name
}

function isTarget(message: Message): boolean {
  return String(message.id) === route.query.message
}

async function markRead(message: Message, url: string): Promise<void> {
  try {
    await request({ url: `${url}read/`, method: 'POST', data: { seq: message.seq } })
  } catch {
    // 已读回写失败时保留服务端未读状态，打开内容仍可用。
  }
}

async function load(latest = false): Promise<void> {
  const url = conversationUrl.value
  if (url === null) {
    loading.value = false
    failed.value = true
    return
  }
  const current = ++generation
  loading.value = true
  failed.value = false
  actionError.value = ''
  imagePreview.value = null
  try {
    const history = await request<ChatHistory>({
      url: `${url}messages/`, params: latest ? undefined : { around: route.query.message },
    })
    if (current !== generation) return
    const target = latest ? history.messages[history.messages.length - 1] : history.messages.find(isTarget)
    if (!history.self_member_id || (!latest && !target)) throw new Error('没有可显示的消息')
    chat.value = history
    messages.value = history.messages
    hasNewer.value = !latest && history.has_newer
    loading.value = false
    await nextTick()
    if (current !== generation) return
    highlighted.value?.scrollIntoView?.({ block: 'center' })
    if (target) void markRead(target, url)
  } catch {
    if (current !== generation) return
    chat.value = null
    messages.value = []
    failed.value = true
  } finally {
    if (current === generation) loading.value = false
  }
}

async function send(): Promise<void> {
  const text = draft.value.trim()
  const url = conversationUrl.value
  if (url === null || !text || sending.value || !chat.value?.can_reply || loading.value) return
  const current = generation
  const reply_to_id = replyTo.value?.id ?? null
  pendingSend = pendingSend?.text === text && pendingSend.reply_to_id === reply_to_id ? pendingSend
    : { text, reply_to_id, client_id: crypto.randomUUID() }
  sending.value = true
  actionError.value = ''
  try {
    const message = await request<Message>({ url: `${url}messages/`, method: 'POST', data: pendingSend })
    if (current !== generation) return
    if (!messages.value.some((item) => item.id === message.id)) messages.value.push(message)
    pendingSend = null
    draft.value = ''
    replyTo.value = null
  } catch (error) {
    if (current === generation) actionError.value = error instanceof Error ? error.message : '暂时无法发送消息'
  } finally {
    if (current === generation) sending.value = false
  }
}

async function showImage(message: Message): Promise<void> {
  const url = conversationUrl.value
  if (url === null) return
  const current = generation
  actionError.value = ''
  try {
    const image = await request<{ mime: string; data: string }>({ url: `${url}messages/${message.id}/image/` })
    if (current !== generation) return
    if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif'].includes(image.mime)) {
      actionError.value = '无法显示此图片格式'
      return
    }
    imagePreview.value = { id: message.id, src: `data:${image.mime};base64,${image.data}` }
  } catch {
    if (current === generation) actionError.value = '暂时无法查看图片'
  }
}

function rememberStaffDesktop(): void {
  writeMobileRedirectSuppressed()
}

watch(() => route.fullPath, () => {
  generation++
  chat.value = null
  messages.value = []
  hasNewer.value = false
  actionError.value = ''
  imagePreview.value = null
  draft.value = ''
  replyTo.value = null
  pendingSend = null
  sending.value = false
  void load()
}, { immediate: true, flush: 'sync' })
</script>

<template>
  <main class="push-chat">
    <header class="push-chat__header">
      <div>
        <h1>{{ chatTitle }}</h1>
        <span v-if="chat?.remark && chat.order_no">订单-{{ chat.order_no }}</span>
      </div>
      <a v-if="appLink" :href="appLink">在代代通打开</a>
    </header>
    <p v-if="loading" role="status">正在加载聊天…</p>
    <div v-else-if="failed" role="alert">
      <p>暂时无法打开这条消息，请确认当前账号可以访问该会话。</p>
      <button v-if="chatPath" type="button" @click="load()">重试</button>
    </div>
    <template v-else-if="chat">
      <ol class="push-chat__messages" aria-label="聊天消息">
        <li v-for="message in messages" :key="message.id"
          :ref="(element) => { if (isTarget(message)) highlighted = element as HTMLLIElement | null }"
          :class="{ 'push-chat__target': isTarget(message) }">
          <span class="push-chat__author">{{ author(message) }}</span>
          <p v-if="message.revoked || message.text">{{ message.revoked ? '消息已撤回' : message.text }}</p>
          <template v-if="!message.revoked">
            <img v-if="imagePreview?.id === message.id" :src="imagePreview.src" alt="聊天图片">
            <button v-else-if="message.image" type="button" @click="showImage(message)">查看图片</button>
            <button v-if="chat.can_reply" type="button" :disabled="sending" @click="replyTo = message">回复</button>
          </template>
        </li>
      </ol>
      <button v-if="hasNewer" type="button" :disabled="sending" @click="load(true)">查看最新消息</button>
      <form v-if="chat.can_reply" class="push-chat__compose" @submit.prevent="send">
        <div v-if="replyTo" class="push-chat__reply">
          <span>回复 {{ author(replyTo) }}</span>
          <button type="button" :disabled="sending" @click="replyTo = null">取消引用</button>
        </div>
        <textarea v-model="draft" aria-label="聊天消息" maxlength="4000" rows="2" placeholder="输入消息" :disabled="sending" />
        <button type="submit" :disabled="sending || !draft.trim()">{{ sending ? '正在发送…' : '发送' }}</button>
      </form>
      <p v-else role="status">群聊已结束，可以查看以前的消息。</p>
    </template>
    <p v-if="actionError" role="alert">{{ actionError }}</p>
    <footer>
      <RouterLink v-if="session.isWorker" :to="{ name: 'm-home' }">返回团团通</RouterLink>
      <a v-else href="/v2/messages" @click="rememberStaffDesktop">返回消息通知</a>
    </footer>
  </main>
</template>

<style scoped>
.push-chat { display: grid; gap: 12px; max-width: 40rem; margin: 0 auto; padding: 16px; min-height: 100dvh; align-content: start; background: #fff; color: #27313a; }
.push-chat__header, .push-chat__reply { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.push-chat h1 { font-size: 18px; margin: 0; }
.push-chat__messages { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.push-chat__messages li { background: #f3f5f7; border-radius: 8px; padding: 10px 12px; }
.push-chat__messages li.push-chat__target { background: #e8f0ff; }
.push-chat__author { font-size: 13px; color: #536476; }
.push-chat__messages p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0 6px; }
.push-chat img { display: block; max-width: 100%; margin-bottom: 8px; }
.push-chat__compose { display: grid; gap: 8px; }
.push-chat textarea { padding: 8px; border: 1px solid #aeb8c2; border-radius: 6px; resize: vertical; }
.push-chat button, .push-chat a { min-height: 34px; color: #21498d; }
.push-chat button { background: transparent; border: 1px solid #b4c2d0; border-radius: 6px; padding: 4px 8px; }
.push-chat li button + button { margin-inline-start: 6px; }
.push-chat button:disabled { opacity: .6; }
</style>
