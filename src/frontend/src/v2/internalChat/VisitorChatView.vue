<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRoute } from 'vue-router'
import { AuthExpiredError, BizError, HttpError } from '@/api/errors'
import {
  bootstrapVisitor, getVisitorConversation, getVisitorConversations, getVisitorDesk, getVisitorGuest,
  registerVisitor, type VisitorConversation,
} from '@/api/internalChatVisitors'
import { chatConversationId } from './chatWideWindow'
import VisitorChatFrame from './VisitorChatFrame.vue'
import VisitorDeskToolbar from './VisitorDeskToolbar.vue'

const props = defineProps<{ scope: 'guest' | 'desk' }>()
const route = useRoute()
const wide = computed(() => props.scope === 'desk' || route.query.wide === '1')
const state = ref<'loading' | 'register' | 'chat' | 'failed'>('loading')
const conversationId = ref<number>()
const internalConversationId = ref<number>()
const viewerId = ref<number>()
const active = shallowRef<VisitorConversation>()
const seatMode = ref(false)
const canReceive = ref(false)
const frameVersion = ref(0)
const frame = ref<InstanceType<typeof VisitorChatFrame>>()
const showDirectoryIntro = ref(false)
let detailRequest: AbortController | undefined
const phone = ref('')
const pending = ref(false)
const error = ref('')

const guestStatus = computed(() => {
  if (props.scope !== 'guest') return ''
  if (active.value?.service_status === 'closed') return '本次接待已结束，如需帮助可继续发送消息。'
  if (active.value?.service_status === 'waiting') return '留言已进入队列，有客服可接待时会自动分配。'
  if (active.value?.peer.presence === 'busy') return '客服繁忙，可以先留言。'
  return ''
})

const introAttemptKey = 'tt:visitor-seats:registration-in-flight'
const introSeatKey = (id: number) => `tt:visitor-seats:intro:${id}`

function recordRegistrationAttempt() {
  try { sessionStorage.setItem(introAttemptKey, '1') } catch { /* Storage can be disabled. */ }
}

function prepareGuestIntro(id: number, newlyRegistered = false) {
  showDirectoryIntro.value = seatMode.value && newlyRegistered
  if (!seatMode.value) return
  try {
    const previous = localStorage.getItem(introSeatKey(id))
    const submitted = newlyRegistered || sessionStorage.getItem(introAttemptKey) === '1'
    showDirectoryIntro.value = previous !== 'seen' && (submitted || previous === 'pending')
    if (showDirectoryIntro.value) localStorage.setItem(introSeatKey(id), 'pending')
    sessionStorage.removeItem(introAttemptKey)
  } catch { /* The introduction is still available when storage is disabled. */ }
}

function recordDirectoryOpened() {
  if (!showDirectoryIntro.value || viewerId.value === undefined) return
  try { localStorage.setItem(introSeatKey(viewerId.value), 'seen') } catch { /* Storage can be disabled. */ }
}

async function enterChat(id: number, fallback?: number | null) {
  conversationId.value = undefined
  if (wide.value && !showDirectoryIntro.value) {
    conversationId.value = chatConversationId(route.query.conversation) ?? chatConversationId(fallback)
    if (conversationId.value === undefined) {
      const { conversations } = await getVisitorConversations(props.scope, id)
      conversationId.value = chatConversationId(conversations[0]?.id)
    }
  }
  viewerId.value = id
  state.value = 'chat'
}

function removeRevoked(ids: number[]) {
  if (conversationId.value !== undefined && ids.includes(conversationId.value)) {
    detailRequest?.abort()
    detailRequest = undefined
    conversationId.value = undefined
    active.value = undefined
  }
  frameVersion.value += 1
}

async function refreshConversation(id: number) {
  const viewer = viewerId.value
  if (!seatMode.value || viewer === undefined || id !== conversationId.value || detailRequest) return
  const controller = new AbortController()
  detailRequest = controller
  try {
    const reply = await getVisitorConversation(props.scope, viewer, id, controller.signal)
    if (controller.signal.aborted || id !== conversationId.value) return
    active.value = reply
    if (props.scope === 'guest') {
      const presence = reply.peer.presence
      const online = presence === 'online' || presence === 'offline' ? presence === 'online' : null
      frame.value?.updatePresence(id, online)
    }
  } catch (cause) {
    if (controller.signal.aborted || id !== conversationId.value) return
    if (cause instanceof AuthExpiredError || (cause instanceof HttpError && cause.status === 403)) endSession()
    else if (props.scope === 'desk' && ((cause instanceof BizError && cause.code === 1002) || (cause instanceof HttpError && cause.status === 404))) {
      removeRevoked([id])
    }
  } finally {
    if (detailRequest === controller) detailRequest = undefined
  }
}

function watchConversation(id: number | undefined) {
  if (id !== conversationId.value) {
    detailRequest?.abort()
    detailRequest = undefined
    conversationId.value = id
    active.value = undefined
  }
  if (id !== undefined) {
    showDirectoryIntro.value = false
    void refreshConversation(id)
  }
}

function markConversationClosed(conversation: VisitorConversation) {
  if (conversation.id !== conversationId.value) return
  detailRequest?.abort()
  detailRequest = undefined
  active.value = conversation
}

function updateDeskReception(seats: boolean, eligible: boolean) {
  seatMode.value = seats
  canReceive.value = eligible
}

function endSession() {
  detailRequest?.abort()
  detailRequest = undefined
  viewerId.value = undefined
  active.value = undefined
  internalConversationId.value = undefined
  state.value = 'failed'
  error.value = '访客沟通身份已变更，请重新连接。'
}

async function load() {
  error.value = ''
  state.value = 'loading'
  try {
    if (props.scope === 'desk') {
      const { user, seat_mode, can_receive } = await getVisitorDesk()
      seatMode.value = seat_mode === true
      canReceive.value = can_receive === true
      await enterChat(user.id)
    } else {
      const { registered, seat_mode } = await bootstrapVisitor()
      seatMode.value = seat_mode === true
      if (registered) {
        const { user } = await getVisitorGuest()
        prepareGuestIntro(user.id)
        await enterChat(user.id)
      } else state.value = 'register'
    }
  } catch (cause) {
    state.value = 'failed'
    if (cause instanceof AuthExpiredError) error.value = '登录已失效，请重新登录后进入访客接待。'
    else if (cause instanceof HttpError && cause.status === 403) error.value = '当前账号没有访客接待权限。'
    else if (cause instanceof HttpError && cause.status === 404) error.value = '当前工作室暂未开放访客接待。'
    else error.value = '暂时无法打开访客沟通，请重试。'
  }
}

async function register() {
  if (pending.value) return
  const number = phone.value.trim()
  if (!/^1[3-9]\d{9}$/.test(number)) {
    error.value = '请输入正确的 11 位手机号。'
    return
  }
  pending.value = true
  error.value = ''
  if (seatMode.value) recordRegistrationAttempt()
  try {
    const { user, conversation_id, seat_mode } = await registerVisitor(number)
    seatMode.value = seat_mode === true
    prepareGuestIntro(user.id, true)
    phone.value = ''
    await enterChat(user.id, conversation_id)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '注册失败，请重试。'
  } finally {
    pending.value = false
  }
}

onMounted(() => {
  document.title = props.scope === 'desk' ? '访客接待 · 团团通' : '访客注册 · 团团通'
  void load()
})
onBeforeUnmount(() => detailRequest?.abort())
</script>

<template>
  <section v-if="state === 'chat' && viewerId !== undefined" class="ttv2 visitor-chat-host">
    <VisitorDeskToolbar v-if="scope === 'desk'" :viewer-id="viewerId" :seat-mode="seatMode" :can-receive="canReceive" :conversation="active"
      @reception-changed="updateDeskReception" @session-expired="endSession"
      @transferred="id => removeRevoked([id])" @closed="markConversationClosed" />
    <aside v-if="showDirectoryIntro || guestStatus" class="visitor-host-notice">
      <template v-if="showDirectoryIntro">
        <p role="status">请选择人事客服或订单客服，发送消息后由当班人员接待。</p>
        <button type="button" class="cv2-btn" @click="frame?.openDirectory()">打开通讯录</button>
        <button type="button" class="cv2-btn" @click="showDirectoryIntro = false">关闭提示</button>
      </template>
      <p v-else role="status">{{ guestStatus }}</p>
    </aside>
    <VisitorChatFrame
      :key="frameVersion"
      ref="frame"
      :scope="scope"
      :viewer-id="viewerId"
      :wide="wide"
      :conversation-id="conversationId"
      :internal-conversation-id="internalConversationId"
      :observe-conversation="seatMode"
      :auto-open-directory="!seatMode || showDirectoryIntro"
      @session-expired="endSession"
      @navigation-ready="recordDirectoryOpened"
      @watch-conversation="watchConversation"
      @watch-internal-conversation="id => internalConversationId = id"
      @refresh-conversation="refreshConversation"
      @revoked="removeRevoked"
    />
  </section>
  <div v-else class="visitor-registration-page">
    <header class="visitor-header">
      <span class="visitor-brand-mark" aria-hidden="true" />
      <span>团团通</span>
    </header>
    <main class="visitor-registration">
      <h1>{{ scope === 'desk' ? '访客接待' : '访客注册' }}</h1>
      <form v-if="state === 'register'" @submit.prevent="register">
        <label for="visitor-phone">手机号</label>
        <div class="visitor-phone-control">
          <span aria-hidden="true">+86</span>
          <input
            id="visitor-phone"
            v-model="phone"
            type="tel"
            inputmode="numeric"
            autocomplete="tel-national"
            maxlength="11"
            placeholder="请输入 11 位手机号"
            aria-describedby="visitor-phone-hint"
            :aria-invalid="!!error"
            :disabled="pending"
            required
          >
        </div>
        <p id="visitor-phone-hint" class="visitor-hint">请填写你的手机号，用于登记来访信息。</p>
        <p v-if="error" role="alert" class="visitor-error">{{ error }}</p>
        <button type="submit" class="visitor-primary" :disabled="pending">
          {{ pending ? '注册中…' : '注册并进入通讯录' }}
        </button>
      </form>
      <p v-else-if="state === 'loading'" role="status">正在连接…</p>
      <template v-else>
        <p role="alert" class="visitor-error">{{ error }}</p>
        <button type="button" class="visitor-primary" @click="load">重新连接</button>
        <RouterLink v-if="scope === 'desk'" class="visitor-return" to="/v2">返回团团通</RouterLink>
      </template>
    </main>
  </div>
</template>

<style scoped>
.visitor-chat-host { position: fixed; inset: 0; display: flex; flex-direction: column; }
.visitor-host-notice {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 6px 12px;
  padding: 8px 12px;
  color: var(--tt2-primary-deep);
  background: var(--tt2-hover-wash);
}
.visitor-host-notice p { margin: 0; }
.visitor-registration-page {
  position: fixed;
  inset: 0;
  overflow: auto;
  color: #152c2b;
  background: #f2f5f3;
}
.visitor-header {
  height: 76px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding-inline: max(24px, calc((100vw - 960px) / 2));
  font-size: 22px;
  font-weight: 700;
  border-bottom: 1px solid #d4e1dc;
}
.visitor-brand-mark { position: relative; width: 40px; height: 40px; border-radius: 12px; background: #17453e; }
.visitor-brand-mark::before, .visitor-brand-mark::after {
  content: '';
  position: absolute;
  width: 16px;
  height: 12px;
  border: 1.5px solid #d7e9df;
  border-radius: 6px;
  background: #17453e;
}
.visitor-brand-mark::before { top: 10px; left: 8px; }
.visitor-brand-mark::after { top: 17px; left: 14px; }
.visitor-registration {
  width: min(400px, calc(100% - 72px));
  margin: clamp(24px, 10vh, 96px) auto 24px;
  padding: clamp(18px, 5vw, 32px);
  border: 1px solid #d4e1dc;
  border-radius: 16px;
  background: #fff;
}
.visitor-registration h1 { margin: 0 0 24px; font-size: 26px; }
.visitor-registration label { display: block; margin-block-end: 8px; font-weight: 600; }
.visitor-phone-control {
  display: flex;
  gap: 12px;
  align-items: center;
  padding-inline: 12px;
  border: 1px solid #cadad4;
  border-radius: 8px;
  color: #42635c;
}
.visitor-phone-control:focus-within { outline: 2px solid #258277; outline-offset: 1px; }
.visitor-phone-control input {
  flex: 1;
  min-width: 0;
  min-height: 46px;
  border: 0;
  outline: 0;
  background: transparent;
  color: #152c2b;
  font: inherit;
  font-size: 16px;
}
.visitor-hint { color: #4e625d; font-size: 14px; }
.visitor-error { color: #a32622; }
.visitor-primary {
  width: 100%;
  min-height: 44px;
  border: 0;
  border-radius: 8px;
  background: #146359;
  color: #fff;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.visitor-primary:disabled { opacity: .65; cursor: wait; }
.visitor-return { display: block; margin-top: 16px; color: #146359; text-align: center; }
</style>
