<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  DAILIANTONG_PLATFORM,
  PLATFORM_ACTIVE_PUSH_STATES,
  fetchOrderPlatformPushMessages,
  fetchOrderPlatformPushes,
  sendOrderPlatformPushMessage,
  type DailiantongPushRow,
  type PlatformPushMessage,
  type PlatformPushProgressItem,
} from '@/api/dailiantongPush'
import { formatMoney } from '@/config/money'
import { classifyPushError } from '@/layui-island/platformPush'
import { useSessionStore } from '@/stores/session'

/** 综合状态中文，与第三方平台订单页同一张表（后端下发优先）。 */
const DISPLAY_STATUS_LABELS: Readonly<Record<string, string>> = {
  temp_order: '临时订单',
  unaccepted: '未接手',
  in_progress: '正在代练',
  waiting_review: '等待验收',
  abnormal: '订单异常',
  locked: '已锁定',
  cancelling: '撤销中',
  settled: '已结算',
  revoked: '已撤销',
  force_revoked: '已强制撤销',
  arbitrating: '客服介入中',
  off_shelf: '已下架',
}

const TOKEN_BY_REMOTE_STATUS: Readonly<Record<number, string>> = {
  10: 'temp_order',
  11: 'unaccepted',
  12: 'in_progress',
  13: 'waiting_review',
  14: 'abnormal',
  15: 'locked',
  16: 'cancelling',
  17: 'settled',
  18: 'off_shelf',
}

/** 撤销状态码表，与 `platformPush.ts` 的 `REMOTE_CANCEL_STATUS_LABELS` 同值。 */
const CANCEL_LABELS: Readonly<Record<number, string>> = {
  10: '未撤销',
  11: '协商撤销中',
  12: '协商已处理',
  13: '撤销已取消',
  14: '客服介入中',
  15: '客服已处理',
  16: '客服强制撤销',
}

const props = defineProps<{ orderNo: string }>()
const session = useSessionStore()

const loading = ref(true)
const failed = ref('')
const pushes = ref<DailiantongPushRow[]>([])
const dltPushes = computed(() => pushes.value.filter(push => push.platform === DAILIANTONG_PLATFORM))
const historyPush = computed(() => latestPush(dltPushes.value.filter(push => textOf(push.serial_no) !== '')))
const row = computed(() => historyPush.value ?? latestPush(dltPushes.value))
const messages = ref<PlatformPushMessage[]>([])
const images = ref<PlatformPushProgressItem[]>([])
const messagesError = ref('')
const imagesError = ref('')
const historyLoading = ref(false)
let historyRequest = 0
const draft = ref('')
const sending = ref(false)
const sendError = ref('')
const sendNotice = ref('')
const canReadHistory = computed(() => session.canNode(['order.list']) && historyPush.value !== null)
const canWrite = computed(() => session.canNode(['order.platform_write']))
const activePush = computed(() => latestPush(pushes.value.filter(push => PLATFORM_ACTIVE_PUSH_STATES.includes(push.state))))
const sendBlocked = computed(() => {
  const push = activePush.value
  if (failed.value !== '') return '请先刷新代练通订单信息'
  if (push === null) return '当前没有有效的代练通订单，留言仅供查看'
  if (push.platform !== DAILIANTONG_PLATFORM) return '当前有效订单已发往其他平台，代练通留言仅供查看'
  if (push.remote_status === 17 || [12, 15, 16].some(status => status === push.remote_cancel_status)) {
    return '代练通订单已结束，留言仅供查看'
  }
  if (textOf(push.serial_no) === '') return '当前订单没有可留言的代练通单号'
  return push.serial_no === historyPush.value?.serial_no ? '' : '当前代练通单号与留言记录不一致，留言仅供查看'
})

function latestPush(rows: DailiantongPushRow[]): DailiantongPushRow | null {
  return rows.reduce<DailiantongPushRow | null>((latest, push) =>
    latest === null || push.id > latest.id ? push : latest, null)
}

function textOf(value: string | null | undefined): string {
  return value === null || value === undefined ? '' : value.trim()
}

function statusLabel(push: DailiantongPushRow): string {
  if (push.state === 'closed') return '已关闭'
  const token = textOf(push.display_status)
  if (token !== '') {
    const backend = textOf(push.display_status_label)
    return backend !== '' ? backend : (DISPLAY_STATUS_LABELS[token] ?? token)
  }
  const cancel = push.remote_cancel_status
  if (push.remote_status === 17) return '已结算'
  if (cancel === 12 || cancel === 15) return '已撤销'
  if (cancel === 16) return '已强制撤销'
  if (cancel === 11) return '撤销中'
  if (cancel === 14) return '客服介入中'
  const mapped = push.remote_status === null ? undefined : TOKEN_BY_REMOTE_STATUS[push.remote_status]
  return mapped === undefined ? '' : (DISPLAY_STATUS_LABELS[mapped] ?? mapped)
}

function cancelLabel(status: number | null): string {
  if (status === null) return ''
  return CANCEL_LABELS[status] ?? ''
}

function moneyShown(value: string | null | undefined): boolean {
  return textOf(value) !== ''
}

function syncedText(iso: string | null): string {
  if (iso === null || iso.trim() === '') return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function platformName(platform: number): string {
  return platform === DAILIANTONG_PLATFORM ? '代练通' : String(platform)
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message !== '' ? error.message : '请求失败'
}

async function loadHistory(): Promise<void> {
  if (!canReadHistory.value) return
  const request = ++historyRequest
  historyLoading.value = true
  try {
    const result = await fetchOrderPlatformPushMessages(props.orderNo)
    if (request !== historyRequest) return
    messages.value = result.messages
    images.value = result.images ?? []
    messagesError.value = textOf(result.error)
    imagesError.value = textOf(result.images_error)
  } catch (error) {
    if (request === historyRequest) messagesError.value = errorText(error)
  } finally {
    if (request === historyRequest) historyLoading.value = false
  }
}

async function sendMessage(): Promise<void> {
  if (!canWrite.value || sendBlocked.value !== '' || sending.value || loading.value) return
  const message = draft.value.trim()
  sendError.value = ''
  sendNotice.value = ''
  if (message === '' || Array.from(message).length > 512) {
    sendError.value = message === '' ? '留言内容不能为空' : '留言内容不能超过 512 个字符'
    return
  }
  sending.value = true
  try {
    await sendOrderPlatformPushMessage(props.orderNo, message)
    draft.value = ''
    sendNotice.value = '留言已提交'
    await loadHistory()
  } catch (error) {
    sendError.value = classifyPushError(error) === 'failed'
      ? errorText(error)
      : '发送结果未确认，请刷新平台留言核对'
  } finally {
    sending.value = false
  }
}

async function refreshPlatform(): Promise<void> {
  loading.value = true
  failed.value = ''
  try {
    pushes.value = await fetchOrderPlatformPushes(props.orderNo)
    await loadHistory()
  } catch (error) {
    failed.value = errorText(error)
  } finally {
    loading.value = false
  }
}

onMounted(() => { void refreshPlatform() })
</script>

<template>
  <div class="dtl-dlt" :class="{ 'dlt-split': canReadHistory }" data-v2="dlt-pane">
    <p v-if="loading && row === null" class="dtl-dlt-empty">正在读取代练通…</p>
    <p v-else-if="failed !== ''" class="dtl-dlt-empty">加载失败：{{ failed }}</p>
    <p v-else-if="row === null" class="dtl-dlt-empty">本单尚未发到交易平台</p>
    <div v-else class="dtl-fields">
      <div class="dtl-row" :class="{ 'dtl-skip': textOf(row.serial_no) === '' }">
        <span class="dtl-label">交易平台单号</span>
        <span class="cv2-num">{{ row.serial_no }}</span>
      </div>
      <div class="dtl-row">
        <span class="dtl-label">交易平台</span>
        <span>{{ platformName(row.platform) }}</span>
      </div>
      <div class="dtl-row">
        <span class="dtl-label">订单价格</span>
        <span class="cv2-money">{{ formatMoney(row.price) }}</span>
      </div>
      <div class="dtl-row">
        <span class="dtl-label">安全保证金</span>
        <span class="cv2-money">{{ formatMoney(row.ensure1) }}</span>
      </div>
      <div class="dtl-row">
        <span class="dtl-label">效率保证金</span>
        <span class="cv2-money">{{ formatMoney(row.ensure2) }}</span>
      </div>
      <div class="dtl-row" :class="{ 'dtl-skip': !moneyShown(row.settled_pay_level_bal) }">
        <span class="dtl-label">已结代练费</span>
        <span class="cv2-money">{{ formatMoney(row.settled_pay_level_bal) }}</span>
      </div>
      <div class="dtl-row" :class="{ 'dtl-skip': !moneyShown(row.settled_rep_ensure_bal) }">
        <span class="dtl-label">已赔保证金</span>
        <span class="cv2-money">{{ formatMoney(row.settled_rep_ensure_bal) }}</span>
      </div>
      <div class="dtl-row">
        <span class="dtl-label">要求时限</span>
        <span>{{ row.time_limit_hours }} 小时</span>
      </div>
      <div class="dtl-row" :class="{ 'dtl-skip': statusLabel(row) === '' }">
        <span class="dtl-label">交易状态</span>
        <span>{{ statusLabel(row) }}</span>
      </div>
      <div class="dtl-row" :class="{ 'dtl-skip': cancelLabel(row.remote_cancel_status) === '' }">
        <span class="dtl-label">撤销状态</span>
        <span>{{ cancelLabel(row.remote_cancel_status) }}</span>
      </div>
      <div class="dtl-row" :class="{ 'dtl-skip': syncedText(row.last_synced_at) === '' }">
        <span class="dtl-label">最近同步</span>
        <span class="cv2-num">{{ syncedText(row.last_synced_at) }}</span>
      </div>
      <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': textOf(row.last_error) === '' }">
        <span class="dtl-label">失败原因</span>
        <span>{{ row.last_error }}</span>
      </div>
    </div>
    <button v-if="!canReadHistory && !loading" type="button" class="cv2-btn cv2-btn--quiet" @click="void refreshPlatform()">刷新代练通</button>
    <div v-if="canReadHistory" class="dlt-chat-panel">
      <section aria-label="平台留言">
        <header class="dlt-chat-heading">
          <h3>平台留言</h3>
          <button type="button" class="cv2-btn cv2-btn--quiet" :disabled="historyLoading || loading || sending" @click="void refreshPlatform()">刷新代练通</button>
        </header>
        <div class="dlt-chat-history" :aria-busy="historyLoading">
          <p v-if="historyLoading && messages.length === 0" class="dlt-chat-note">正在加载留言…</p>
          <p v-else-if="messagesError" class="dlt-chat-error" role="alert">留言加载失败：{{ messagesError }}</p>
          <p v-else-if="messages.length === 0" class="dlt-chat-note">暂无留言</p>
          <ol v-if="messages.length" class="dlt-chat-messages">
            <li v-for="(message, index) in messages" :key="index">
              <div class="dlt-chat-meta">
                <strong v-if="message.sender && !(message.is_system && message.sender === '系统')">{{ message.sender }}</strong>
                <span v-if="message.is_system" class="dlt-chat-system">系统</span>
                <span>{{ message.time }}</span>
              </div>
              <p class="dlt-chat-body">{{ message.text }}</p>
            </li>
          </ol>
          <p v-if="imagesError && !messagesError" class="dlt-chat-error" role="alert">进度图加载失败：{{ imagesError }}</p>
          <div v-if="images.length" class="dlt-chat-images" aria-label="订单进度图" role="group">
            <figure v-for="(image, index) in images" :key="index">
              <a v-if="image.url" :href="image.url" target="_blank" rel="noopener noreferrer">
                <img :src="image.url" :alt="image.caption || '订单进度图'" loading="lazy" referrerpolicy="no-referrer">
              </a>
              <span v-else>无预览</span>
              <figcaption v-if="image.caption || image.time">{{ image.caption }} {{ image.time }}</figcaption>
            </figure>
          </div>
        </div>
      </section>
      <section class="dlt-chat-reply" aria-label="与打手对话">
        <form v-if="canWrite && !sendBlocked" class="dlt-chat-composer" @submit.prevent="void sendMessage()">
          <p class="dlt-chat-note">发送后会显示在代练通订单的留言里。</p>
          <textarea v-model="draft" class="cv2-input dlt-chat-input" rows="4" aria-label="留言内容" placeholder="输入留言" :disabled="sending" />
          <div class="dlt-chat-send">
            <p v-if="sendError" class="dlt-chat-error" role="alert">{{ sendError }}</p>
            <p v-else-if="sendNotice" class="dlt-chat-note" role="status">{{ sendNotice }}</p>
            <button type="submit" class="cv2-btn cv2-btn--primary cv2-btn--fill" :disabled="sending || loading">{{ sending ? '发送中…' : '发送' }}</button>
          </div>
        </form>
        <p v-else class="dlt-chat-note dlt-chat-readonly">{{ canWrite ? sendBlocked : '当前账号仅可查看平台留言' }}</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dlt-split {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
  align-items: start;
  gap: 12px;
}

.dtl-dlt.dlt-split > .dtl-fields { grid-template-columns: 108px minmax(0, 1fr); }

.dlt-chat-panel {
  min-width: 0;
  border: 1px solid var(--tt2-line);
  border-radius: 6px;
  background: var(--tt2-panel);
}

.dlt-chat-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 42px;
  padding: 4px 12px;
  border-bottom: 1px solid var(--tt2-line);
}

.dlt-chat-heading h3 { margin: 0; font-size: 14px; }
.dlt-chat-history { max-height: 320px; overflow: auto; padding: 8px 12px; }
.dlt-chat-messages { margin: 0; padding: 0; list-style: none; }
.dlt-chat-messages li { padding: 8px 0; }
.dlt-chat-messages li + li { border-top: 1px solid var(--tt2-line); }

.dlt-chat-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--tt2-dim);
  font-size: 12px;
}

.dlt-chat-system {
  padding: 0 5px;
  border: 1px solid currentColor;
  border-radius: 4px;
  color: var(--tt2-btn-solid-push);
}

.dlt-chat-body { margin: 3px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.dlt-chat-note, .dlt-chat-error { margin: 0; font-size: 13px; overflow-wrap: anywhere; }
.dlt-chat-note { color: var(--tt2-dim); }
.dlt-chat-error { color: var(--tt2-btn-danger); }
.dlt-chat-images { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 0; }
.dlt-chat-images figure { width: 96px; margin: 0; overflow-wrap: anywhere; font-size: 12px; }
.dlt-chat-images img { display: block; width: 100%; height: 70px; object-fit: contain; }
.dlt-chat-reply { border-top: 1px solid var(--tt2-line); }
.dlt-chat-readonly { padding: 12px; }
.dlt-chat-composer { display: grid; gap: 8px; padding: 12px; }
.dlt-chat-composer .dlt-chat-input { box-sizing: border-box; width: 100%; min-height: 96px; resize: vertical; }
.dlt-chat-send { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.dlt-chat-send button { flex: none; }

@media (max-width: 680px) {
  .dlt-split { grid-template-columns: minmax(0, 1fr); }
}
</style>
