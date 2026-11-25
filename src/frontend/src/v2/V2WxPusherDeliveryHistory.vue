<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { BizError } from '@/api/errors'
import { listWxPusherHistory, type WxPusherHistoryQuery, type WxPusherRecord } from '@/api/wxpusherConfig'

const props = defineProps<{ refreshKey: number }>()
const PAGE_SIZE = 50
const RECORD_STATUS: Record<WxPusherRecord['status'], { text: string; tone: string }> = {
  pending: { text: '待发送', tone: 'unsettled' },
  attempted: { text: '已尝试发送', tone: 'doing' },
  sent: { text: 'WxPusher 已受理', tone: 'settled' },
  skipped: { text: '已跳过', tone: 'unassigned' },
  unconfirmed: { text: '受理未确认', tone: 'error' },
}
const SKIP_REASONS: Readonly<Record<string, string>> = {
  app_unconfigured: '工作室未配置 WxPusher',
  workspace_disabled: '工作室关闭了此类推送',
  personal_disabled: '接收人关闭了此类推送',
  not_bound: '接收人未绑定 WxPusher',
  binding_changed: '接收人的 WxPusher 绑定已变更',
  recipient_unavailable: '接收人资格或待办归属已变更',
  hall_unavailable: '订单已离开大厅或接收人无抢单资格',
  hall_rule_changed: '订单不再符合订阅筛选',
  group_unavailable: '会话不可用或消息已撤回',
  group_already_read: '接收人已读消息',
}
const dateTime = new Intl.DateTimeFormat('sv-SE', {
  dateStyle: 'short', timeStyle: 'medium', timeZone: 'Asia/Shanghai',
})
const emptyFilters = { order_no: '', recipient: '', status: '' as WxPusherRecord['status'] | '', date_from: '', date_to: '' }
const filters = reactive({ ...emptyFilters })
const applied = ref<WxPusherHistoryQuery>({})
const page = ref(1)
const records = ref<WxPusherRecord[]>([])
const total = ref(0)
const pages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const loading = ref(false)
const error = ref('')
let requestNumber = 0

function timeText(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '—' : dateTime.format(parsed)
}

function reasonText(record: WxPusherRecord): string {
  if (record.status !== 'skipped') return '—'
  return SKIP_REASONS[record.skip_reason] ?? '未记录原因'
}

async function loadHistory(): Promise<void> {
  const current = ++requestNumber
  loading.value = true
  error.value = ''
  try {
    const result = await listWxPusherHistory({ ...applied.value, page: page.value })
    if (current !== requestNumber) return
    total.value = result.total
    if (page.value > pages.value) {
      page.value = pages.value
      await loadHistory()
      return
    }
    records.value = result.items
  } catch (cause) {
    if (current === requestNumber) error.value = cause instanceof BizError ? cause.message : '读取推送记录失败，请重试。'
  } finally {
    if (current === requestNumber) loading.value = false
  }
}

function search(): void {
  applied.value = {
    order_no: filters.order_no.trim() || undefined,
    recipient: filters.recipient.trim() || undefined,
    status: filters.status || undefined,
    date_from: filters.date_from || undefined,
    date_to: filters.date_to || undefined,
  }
  page.value = 1
  void loadHistory()
}

function reset(): void {
  Object.assign(filters, emptyFilters)
  search()
}

function goToPage(next: number): void {
  page.value = next
  void loadHistory()
}

watch(() => props.refreshKey, () => { void loadHistory() }, { immediate: true })
</script>

<template>
  <section class="cv2-panel" aria-labelledby="wxpusher-history-title">
    <header class="cv2-panel-head history-head">
      <h3 id="wxpusher-history-title" class="cv2-panel-title">推送记录与排查</h3>
      <button type="button" class="cv2-btn" :disabled="loading" @click="loadHistory">刷新记录</button>
    </header>
    <form class="history-filters" data-v2="wxpusher-history-filters" @submit.prevent="search">
      <label>订单号
        <input v-model="filters.order_no" aria-label="订单号" class="cv2-input" type="search" placeholder="系统订单号或展示订单号" />
      </label>
      <label>接收人
        <input v-model="filters.recipient" aria-label="接收人" class="cv2-input" type="search" placeholder="名称或账号" />
      </label>
      <label>状态
        <select v-model="filters.status" class="cv2-input">
          <option value="">全部</option>
          <option v-for="(status, key) in RECORD_STATUS" :key="key" :value="key">{{ status.text }}</option>
        </select>
      </label>
      <label>开始日期<input v-model="filters.date_from" class="cv2-input" type="date" :max="filters.date_to || undefined" /></label>
      <label>结束日期<input v-model="filters.date_to" class="cv2-input" type="date" :min="filters.date_from || undefined" /></label>
      <button type="submit" class="cv2-btn cv2-btn--primary">查询</button>
      <button type="button" class="cv2-btn" @click="reset">重置</button>
    </form>
    <p v-if="loading" class="cv2-field-hint" role="status">正在读取推送记录…</p>
    <p v-else-if="error" class="history-error" role="alert">{{ error }}</p>
    <p v-else-if="!records.length" class="cv2-field-hint">没有符合条件的推送记录。</p>
    <div v-else class="cv2-table-panel history-table">
      <table class="cv2-table">
        <thead>
          <tr>
            <th scope="col">订单号</th><th scope="col">消息</th><th scope="col">接收人</th><th scope="col">状态</th>
            <th scope="col">未发送原因</th><th scope="col">创建时间</th><th scope="col">尝试发送时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in records" :key="record.id">
            <td>{{ record.order_no || '—' }}</td><td>{{ record.title }}</td><td>{{ record.recipient }}</td>
            <td><span class="cv2-st" :class="'cv2-st--' + (RECORD_STATUS[record.status]?.tone ?? 'unassigned')">
              <i class="cv2-dot" aria-hidden="true"></i>{{ RECORD_STATUS[record.status]?.text ?? '状态未知' }}
            </span></td>
            <td>{{ reasonText(record) }}</td>
            <td>{{ timeText(record.created_at) }}</td><td>{{ timeText(record.attempted_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="!error && total > PAGE_SIZE" class="history-pagination">
      <span>共 {{ total }} 条</span>
      <button type="button" class="cv2-btn" :disabled="loading || page <= 1" @click="goToPage(page - 1)">上一页</button>
      <span>{{ page }} / {{ pages }}</span>
      <button type="button" class="cv2-btn" :disabled="loading || page >= pages" @click="goToPage(page + 1)">下一页</button>
    </div>
  </section>
</template>

<style scoped>
.history-head,
.history-filters,
.history-pagination { display: flex; align-items: end; flex-wrap: wrap; gap: 8px 12px; }
.history-head { align-items: center; justify-content: space-between; }
.history-filters { margin: 12px 0; }
.history-filters label { display: flex; flex-direction: column; gap: 4px; }
.history-filters .cv2-input { min-width: 124px; }
.history-table { overflow: auto; }
.history-table td { white-space: nowrap; }
.history-pagination { align-items: center; justify-content: flex-end; margin-top: 12px; }
.history-error { color: var(--tt2-st-error); }
</style>
