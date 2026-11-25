<script setup lang="ts">
import { orderStatusClass } from '@/v2/orderStatusAppearance'
import { computed, ref, watch } from 'vue'
import { BizError, HttpError } from '@/api/errors'
import {
  createOrderNote,
  fetchOrder,
  fetchOrderAudit,
  type AuditEntry,
  type Order,
} from '@/api/orders'
import { V2_ORDER_STATUS_LABELS } from '@/config/orderStatus'
import { useSessionStore } from '@/stores/session'
import V2DetailFace from '@/v2/V2DetailFace.vue'
import V2OwnOrderDetail from '@/v2/V2OwnOrderDetail.vue'
import { orderChatPreviewEnabled } from '@/v2/internalChat/orderChatPreview'

const props = defineProps<{
  orderNo: string
  viewNo?: string
  gameName?: string
  projectText?: string
  status?: string
  takerName?: string
  allowOwnOrderDetail?: boolean
  allowOwnOrderFallback?: boolean
}>()

const session = useSessionStore()
const restrictedByPermission = computed(() =>
  props.allowOwnOrderDetail === true && !session.canNode(['order.view_detail', 'order.view_content']),
)
const useOwnOrder = ref(false)
const restricted = computed(() => restrictedByPermission.value || useOwnOrder.value)
const order = ref<Order | null>(null)
const logs = ref<AuditEntry[]>([])
const logsLoading = ref(true)
const orderFailed = ref(false)
const logsFailed = ref(false)
const noteText = ref('')
const noteAdding = ref(false)
const detailTab = ref<'note' | 'action'>('note')
const canAddLog = computed(() => session.canNode(['order.add_log']))

function textOf(value: string | null | undefined): string {
  return value === null || value === undefined ? '' : value.trim()
}

function logTime(iso: string): string {
  const d = new Date(iso)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

const statusCode = computed(() => textOf(order.value?.status) || textOf(props.status))
const statusLabel = computed(() => {
  const code = statusCode.value
  return (V2_ORDER_STATUS_LABELS as Record<string, string>)[code] ?? code
})

function isUnavailableOrder(error: unknown): boolean {
  return (error instanceof HttpError && (error.status === 403 || error.status === 404))
    || (error instanceof BizError && error.code === 1002)
}

watch([() => props.orderNo, restrictedByPermission], async ([orderNo, ownOnly], _, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  useOwnOrder.value = false
  order.value = null
  logs.value = []
  logsLoading.value = true
  orderFailed.value = false
  logsFailed.value = false
  if (ownOnly) return
  const [loaded, audit] = await Promise.allSettled([
    fetchOrder(orderNo),
    fetchOrderAudit(orderNo),
  ])
  if (!current) return
  if (loaded.status === 'fulfilled') order.value = loaded.value
  else if (props.allowOwnOrderFallback && isUnavailableOrder(loaded.reason)) useOwnOrder.value = true
  else orderFailed.value = true
  if (audit.status === 'fulfilled') logs.value = audit.value
  else logsFailed.value = true
  logsLoading.value = false
}, { immediate: true })

async function onAddNote(): Promise<void> {
  if (noteText.value.trim() === '') return
  noteAdding.value = true
  try {
    const row = await createOrderNote(props.orderNo, noteText.value)
    logs.value = [row, ...logs.value]
    noteText.value = ''
  } finally {
    noteAdding.value = false
  }
}
</script>

<template>
  <V2OwnOrderDetail v-if="restricted && session.user?.internal_chat_enabled !== true && !orderChatPreviewEnabled()" :order-no="orderNo" />
  <V2DetailFace v-else :production-order-chat="session.user?.internal_chat_enabled === true" :order-no="orderNo" :order-chat-only="restricted" :source-order-no="order?.source_order_no" :has-dailiantong-history="order?.has_dailiantong_history">
  <V2OwnOrderDetail v-if="restricted" :order-no="orderNo" />
  <div v-else class="dtl-wrap" data-v2="order-detail-pane">
    <div class="dtl-left">
      <p v-if="orderFailed" class="dtl-logs-empty" role="alert">无法读取订单详情</p>
      <div class="dtl-fields">
        <div class="dtl-row">
          <span class="dtl-label">订单号</span>
          <span class="cv2-num">{{ viewNo || order?.source_order_no || orderNo }}</span>
        </div>
        <div class="dtl-row">
          <span class="dtl-label">系统单号</span>
          <span class="cv2-num">{{ orderNo }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': textOf(gameName) === '' }">
          <span class="dtl-label">游戏名称</span>
          <span>{{ gameName }}</span>
        </div>
        <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': textOf(projectText || order?.project_text) === '' }">
          <span class="dtl-label">代练项目</span>
          <span>{{ projectText || order?.project_text }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': statusCode === '' }">
          <span class="dtl-label">订单状态</span>
          <span class="cv2-st cv2-st--mark cv2-order-status" :class="orderStatusClass(statusCode)">
            <i class="cv2-dot" aria-hidden="true"></i>{{ statusLabel }}
          </span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': textOf(takerName) === '' }">
          <span class="dtl-label">接手人</span>
          <span>{{ takerName }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': textOf(order?.game_account) === '' }">
          <span class="dtl-label">游戏账号</span>
          <span>{{ order?.game_account }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': textOf(order?.game_sub_account) === '' }">
          <span class="dtl-label">子账号</span>
          <span>{{ order?.game_sub_account }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': textOf(order?.customer_phone) === '' }">
          <span class="dtl-label">联系电话</span>
          <span>{{ order?.customer_phone }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': order === null }">
          <span class="dtl-label">接单价</span>
          <span class="cv2-money">{{ order?.total_price }}</span>
        </div>
        <div class="dtl-row" :class="{ 'dtl-skip': order === null }">
          <span class="dtl-label">退款</span>
          <span class="cv2-num">{{ order?.refund_fee }}</span>
        </div>
        <div class="dtl-row dtl-row--wide" :class="{ 'dtl-skip': textOf(order?.content) === '' }">
          <span class="dtl-label">订单内容</span>
          <span>{{ order?.content }}</span>
        </div>
      </div>
    </div>
    <div class="dtl-right">
      <div class="dtl-tabs">
        <button type="button" class="dtl-tab" :class="{ 'dtl-tab--on': detailTab === 'note' }" @click="detailTab = 'note'">订单日志</button>
        <button type="button" class="dtl-tab" :class="{ 'dtl-tab--on': detailTab === 'action' }" @click="detailTab = 'action'">操作日志</button>
      </div>
      <div v-if="canAddLog" class="dtl-add">
        <el-input v-model="noteText" :maxlength="255" placeholder="你可以在这里输入订单日志内容" :disabled="detailTab !== 'note'" />
        <button v-if="detailTab === 'note'" type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :disabled="noteAdding" @click="void onAddNote()">添加</button>
      </div>
      <div class="dtl-logs">
        <p v-if="logsLoading" class="dtl-logs-empty">正在加载日志…</p>
        <p v-else-if="logsFailed" class="dtl-logs-empty" role="alert">无法读取订单日志</p>
        <template v-else>
          <p v-for="row in logs" :key="row.id" class="dtl-log">
            <span class="dtl-log-time">【{{ logTime(row.created_at) }}】</span>{{ row.summary }}
          </p>
          <p v-if="logs.length === 0" class="dtl-logs-empty">暂无日志记录</p>
        </template>
      </div>
    </div>
  </div>
  </V2DetailFace>
</template>
