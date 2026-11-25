<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CheckCircleIcon, PauseIcon } from 'tdesign-icons-vue-next'
import { AuthExpiredError, HttpError } from '@/api/errors'
import {
  closeVisitorConversation, getVisitorDesk, getVisitorDeskAvailability, getVisitorTransfer, setVisitorDeskAvailability,
  transferVisitorConversation, type VisitorConversation, type VisitorHandoff,
} from '@/api/internalChatVisitors'

const props = defineProps<{ viewerId: number, seatMode: boolean, canReceive: boolean, conversation?: VisitorConversation }>()
const emit = defineEmits<{
  receptionChanged: [seatMode: boolean, canReceive: boolean]
  sessionExpired: []
  transferred: [id: number]
  closed: [conversation: VisitorConversation]
}>()

const enabled = ref<boolean>()
const settingAvailability = ref(false)
const availabilityError = ref('')
const operation = ref<'transfer'>()
const lookup = ref(false)
const acting = ref(false)
const operationError = ref('')
const candidates = ref<{ id: number, name: string }[]>([])
const history = ref<VisitorHandoff[]>([])
const target = ref<number | ''>('')
let lookupGeneration = 0
let receptionRequest: AbortController | undefined
let refreshTimer: number | undefined

const assigned = computed(() => props.conversation?.service_status === 'assigned')
const seatName = computed(() => {
  if (props.conversation?.seat_kind === 'hr') return '人事客服'
  if (props.conversation?.seat_kind === 'orders') return '订单客服'
  return ''
})

function isForbidden(cause: unknown) {
  return cause instanceof HttpError && cause.status === 403
}

async function refreshReception() {
  if (receptionRequest || settingAvailability.value) return
  const controller = new AbortController()
  receptionRequest = controller
  try {
    const { seat_mode, can_receive } = await getVisitorDesk(props.viewerId, controller.signal)
    if (controller.signal.aborted) return
    emit('receptionChanged', seat_mode === true, can_receive === true)
    if (can_receive !== true) {
      enabled.value = undefined
      availabilityError.value = ''
      return
    }
    try {
      const data = await getVisitorDeskAvailability(props.viewerId, controller.signal)
      if (controller.signal.aborted) return
      enabled.value = data.enabled
      availabilityError.value = ''
    } catch (cause) {
      if (controller.signal.aborted) return
      if (isForbidden(cause)) {
        enabled.value = undefined
        emit('receptionChanged', seat_mode === true, false)
      } else throw cause
    }
  } catch (cause) {
    if (controller.signal.aborted) return
    if (cause instanceof AuthExpiredError || isForbidden(cause)) emit('sessionExpired')
    else if (enabled.value === undefined) availabilityError.value = '读取接待状态失败，请重试。'
  } finally {
    if (receptionRequest === controller) receptionRequest = undefined
  }
}

function refreshReceptionIfNeeded() {
  if (document.visibilityState === 'visible' || enabled.value === true) void refreshReception()
}

async function toggleAvailability() {
  if (!props.canReceive || settingAvailability.value) return
  if (enabled.value === undefined) return refreshReception()
  receptionRequest?.abort()
  receptionRequest = undefined
  settingAvailability.value = true
  availabilityError.value = ''
  try {
    const data = await setVisitorDeskAvailability(props.viewerId, !enabled.value)
    enabled.value = data.enabled
  } catch (cause) {
    if (cause instanceof AuthExpiredError) emit('sessionExpired')
    else if (isForbidden(cause)) {
      enabled.value = undefined
      emit('receptionChanged', props.seatMode, false)
    } else availabilityError.value = cause instanceof Error ? cause.message : '设置接待状态失败，请重试。'
  } finally {
    settingAvailability.value = false
  }
}

function dismissOperation() {
  lookupGeneration += 1
  operation.value = undefined
  operationError.value = ''
  lookup.value = false
}

function updateDialog(open: boolean) {
  if (!open && !acting.value) dismissOperation()
}

async function openTransfer() {
  const id = props.conversation?.id
  if (id === undefined || !props.canReceive || !assigned.value || acting.value) return
  const generation = ++lookupGeneration
  candidates.value = []
  history.value = []
  target.value = ''
  operationError.value = ''
  operation.value = 'transfer'
  lookup.value = true
  try {
    const reply = await getVisitorTransfer(props.viewerId, id)
    if (generation !== lookupGeneration) return
    candidates.value = reply.candidates
    history.value = reply.history
    target.value = reply.candidates[0]?.id ?? ''
  } catch (cause) {
    if (generation === lookupGeneration) operationError.value = cause instanceof Error ? cause.message : '读取转接人员失败，请重试。'
  } finally {
    if (generation === lookupGeneration) lookup.value = false
  }
}

async function confirm(action: 'transfer' | 'close') {
  const id = props.conversation?.id
  const recipient = target.value
  if (id === undefined || !props.canReceive || !assigned.value || acting.value || (action === 'transfer' && recipient === '')) return
  acting.value = true
  operationError.value = ''
  try {
    if (action === 'transfer' && recipient !== '') {
      await transferVisitorConversation(props.viewerId, id, recipient)
      dismissOperation()
      emit('transferred', id)
    } else if (action === 'close') {
      const conversation = await closeVisitorConversation(props.viewerId, id)
      dismissOperation()
      emit('closed', conversation)
    }
  } catch (cause) {
    operationError.value = cause instanceof Error ? cause.message : '操作失败，请重试。'
  } finally {
    acting.value = false
  }
}

watch([() => props.conversation?.id, () => props.canReceive], dismissOperation)
onMounted(() => {
  void refreshReception()
  refreshTimer = window.setInterval(refreshReceptionIfNeeded, 10_000)
  document.addEventListener('visibilitychange', refreshReceptionIfNeeded)
})
onBeforeUnmount(() => {
  receptionRequest?.abort()
  window.clearInterval(refreshTimer)
  document.removeEventListener('visibilitychange', refreshReceptionIfNeeded)
})
</script>

<template>
  <header v-if="seatMode" class="visitor-desk-toolbar" aria-label="访客接待操作" role="region">
    <div class="visitor-desk-context">
      <strong>访客接待</strong>
      <span v-if="conversation" class="visitor-desk-selection">
        <template v-if="seatName">{{ seatName }} · </template>{{ conversation.peer.name }}<template v-if="conversation.service_status === 'closed'"> · 已结束</template>
      </span>
    </div>
    <div v-if="canReceive" class="visitor-desk-actions">
      <span v-if="enabled !== undefined" class="visitor-desk-availability" :data-enabled="enabled" role="status">
        <CheckCircleIcon v-if="enabled" aria-hidden="true" class="visitor-desk-availability-icon" />
        <PauseIcon v-else aria-hidden="true" class="visitor-desk-availability-icon" />
        {{ enabled ? '接待中' : '已停止接待' }}
      </span>
      <button class="cv2-btn cv2-btn--sm" :class="{ 'cv2-btn--primary': enabled === false, 'cv2-btn--tint-amber': enabled === true }"
        type="button" :disabled="settingAvailability"
        @click="toggleAvailability">
        {{ settingAvailability ? '更新中…' : enabled === undefined ? '重新读取' : enabled ? '停止接待' : '开始接待' }}
      </button>
      <button class="cv2-btn cv2-btn--sm" type="button" :disabled="!assigned || acting" @click="openTransfer">转接会话</button>
      <button class="cv2-btn cv2-btn--sm" type="button" :disabled="!assigned || acting" @click="confirm('close')">结束会话</button>
    </div>
    <p v-if="availabilityError || (!operation && operationError)" class="visitor-desk-error" role="alert">
      {{ availabilityError || operationError }}
    </p>
  </header>

  <ElDialog
    :model-value="operation !== undefined"
    title="转接会话"
    class="cv2-dialog cv2-dialog--sm"
    :close-on-click-modal="!acting"
    :close-on-press-escape="!acting"
    :show-close="!acting"
    :append-to-body="false"
    @update:model-value="updateDialog"
  >
    <template v-if="operation === 'transfer'">
      <p v-if="lookup" role="status">正在读取同席位可接待人员…</p>
      <template v-else-if="candidates.length">
        <label for="visitor-transfer-recipient">转接给</label>
        <select id="visitor-transfer-recipient" v-model="target" class="cv2-input visitor-desk-recipient" :disabled="acting">
          <option v-for="candidate in candidates" :key="candidate.id" :value="candidate.id">{{ candidate.name }}</option>
        </select>
      </template>
      <p v-else-if="!operationError">当前没有同席位的可接待人员。</p>
      <template v-if="history.length">
        <p class="visitor-desk-handoff-label">交接记录</p>
        <ul class="visitor-desk-handoff">
          <li v-for="entry in history" :key="entry.transferred_at">
            {{ entry.from_name ?? entry.from_id }} → {{ entry.to_name ?? entry.to_id }}
          </li>
        </ul>
      </template>
    </template>
    <p v-if="operationError" class="visitor-desk-error" role="alert">{{ operationError }}</p>
    <template #footer>
      <button class="cv2-btn" type="button" :disabled="acting" @click="dismissOperation">取消</button>
      <button class="cv2-btn cv2-btn--primary" type="button"
        :disabled="acting || lookup || target === ''" @click="confirm('transfer')">确认转接</button>
    </template>
  </ElDialog>
</template>

<style scoped>
.visitor-desk-toolbar {
  position: relative;
  box-sizing: border-box;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 4px 12px;
  min-height: 36px;
  padding: 3px clamp(8px, 1.5vw, 16px);
  background: var(--tt2-panel);
  color: var(--tt2-ink);
  border-bottom: 1px solid var(--tt2-line);
}
.visitor-desk-context, .visitor-desk-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 3px 6px; }
.visitor-desk-context { min-width: 0; }
.visitor-desk-context strong { font-size: 14px; white-space: nowrap; }
.visitor-desk-selection, .visitor-desk-handoff-label { color: var(--tt2-dim); }
.visitor-desk-selection { min-width: 0; overflow-wrap: anywhere; font-size: 13px; font-variant-numeric: tabular-nums; }
.visitor-desk-actions > .cv2-btn { box-sizing: border-box; padding-inline: 8px; }
.visitor-desk-availability { display: inline-flex; align-items: center; gap: 3px; color: var(--tt2-st-paused); font-size: 12px; white-space: nowrap; }
.visitor-desk-availability[data-enabled='true'] { color: var(--tt2-st-doing); }
.visitor-desk-availability-icon { flex: none; font-size: 14px; }
.visitor-desk-recipient { display: block; width: 100%; margin-block-start: 6px; }
.visitor-desk-handoff-label { margin-block: 12px 4px; }
.visitor-desk-handoff { margin: 0; padding-inline-start: 18px; }
.visitor-desk-error { flex-basis: 100%; margin: 0; color: var(--tt2-st-error); }
</style>
