<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getExternalPayStatus,
  listExternalPayInvalid,
  listExternalPayIssues,
  type ExternalPayInvalid,
  type ExternalPayIssue,
  type ExternalPayStatus,
} from '@/api/wecomExternalPay'

const status = ref<ExternalPayStatus | null>(null)
const statusError = ref('')
const issuesError = ref('')
const loadingStatus = ref(true)
const loadingIssues = ref(true)
const items = ref<ExternalPayIssue[]>([])
const page = ref(1)
const total = ref(0)
const pageSize = ref(20)
const hasNext = computed(() => page.value * pageSize.value < total.value)
const invalidItems = ref<ExternalPayInvalid[]>([])
const invalidError = ref('')
const loadingInvalid = ref(true)
const invalidPage = ref(1)
const invalidTotal = ref(0)
const invalidPageSize = ref(20)
const hasNextInvalid = computed(() => invalidPage.value * invalidPageSize.value < invalidTotal.value)

function timeText(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { hour12: false })
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}

async function loadStatus(): Promise<void> {
  loadingStatus.value = true
  try {
    status.value = await getExternalPayStatus()
    statusError.value = ''
  } catch (e) {
    status.value = null
    statusError.value = safeError(e)
  } finally {
    loadingStatus.value = false
  }
}

async function loadIssues(next: number): Promise<void> {
  loadingIssues.value = true
  try {
    const result = await listExternalPayIssues(next)
    items.value = result.items
    page.value = result.page
    pageSize.value = result.page_size
    total.value = result.total
    issuesError.value = ''
  } catch (e) {
    items.value = []
    issuesError.value = safeError(e)
  } finally {
    loadingIssues.value = false
  }
}

async function loadInvalid(next: number): Promise<void> {
  loadingInvalid.value = true
  try {
    const result = await listExternalPayInvalid(next)
    invalidItems.value = result.items
    invalidPage.value = result.page
    invalidPageSize.value = result.page_size
    invalidTotal.value = result.total
    invalidError.value = ''
  } catch (e) {
    invalidItems.value = []
    invalidError.value = safeError(e)
  } finally {
    loadingInvalid.value = false
  }
}

function refresh(): void {
  void loadStatus()
  void loadInvalid(1)
  void loadIssues(1)
}

onMounted(refresh)
</script>

<template>
  <section class="cv2-panel" data-v2="block-external-pay">
    <div class="cv2-panel-head">
      <h3 class="cv2-panel-title">对外收款同步</h3>
      <button type="button" class="cv2-btn" data-v2="external-pay-refresh" @click="refresh">刷新</button>
    </div>
    <p v-if="loadingStatus" class="cv2-field-hint" data-v2="external-pay-status-loading">正在读取同步状态…</p>
    <p v-else-if="statusError" class="ep-error" data-v2="external-pay-status-error">同步状态读取失败：{{ statusError }}</p>
    <template v-else-if="status">
      <div class="cv2-info-list" data-v2="external-pay-status">
        <div class="cv2-info-row"><span class="cv2-info-label">同步状态</span><span class="cv2-info-value">{{ !status.configured ? '企微应用未配置' : status.enabled ? '已启用' : '未启用' }}</span></div>
        <div class="cv2-info-row"><span class="cv2-info-label">最近尝试</span><span class="cv2-info-value">{{ timeText(status.last_run_at) }}</span></div>
        <div class="cv2-info-row"><span class="cv2-info-label">最近成功</span><span class="cv2-info-value">{{ timeText(status.last_success_at) }}</span></div>
      </div>
      <p v-if="status.last_error" class="ep-error" data-v2="external-pay-last-error">最近同步失败：{{ status.last_error }}</p>
    </template>

    <h4 class="ep-subtitle">无法处理的企微账单</h4>
    <p class="cv2-field-hint">可根据时间范围和原因在企微核对。</p>
    <p v-if="loadingInvalid" class="cv2-field-hint" data-v2="external-pay-invalid-loading">正在读取异常账单…</p>
    <p v-else-if="invalidError" class="ep-error" data-v2="external-pay-invalid-error">异常账单读取失败：{{ invalidError }}</p>
    <p v-else-if="invalidItems.length === 0" class="cv2-field-hint" data-v2="external-pay-invalid-empty">暂无异常账单</p>
    <div v-else class="cv2-table-panel" data-v2="external-pay-invalid">
      <div class="ep-scroll">
        <table class="cv2-table">
          <thead><tr><th>账单查询开始时间</th><th>账单查询结束时间</th><th>异常原因</th><th>处理方式</th><th>最近重查</th></tr></thead>
          <tbody><tr v-for="item in invalidItems" :key="item.id" :data-v2-invalid-row="item.id">
            <td>{{ timeText(item.window_start) }}</td><td>{{ timeText(item.window_end) }}</td>
            <td>{{ item.reason }}</td><td>{{ item.identifiable ? '系统自动重查' : '请人工核对' }}</td><td>{{ timeText(item.last_retry_at) }}</td>
          </tr></tbody>
        </table>
      </div>
      <div class="cv2-pager">
        <span class="cv2-pager-total">第 {{ invalidPage }} 页 · 共 {{ invalidTotal }} 条</span>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="invalidPage <= 1" data-v2="external-pay-invalid-prev" @click="void loadInvalid(invalidPage - 1)">上一页</button>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="!hasNextInvalid" data-v2="external-pay-invalid-next" @click="void loadInvalid(invalidPage + 1)">下一页</button>
      </div>
    </div>

    <h4 class="ep-subtitle">收款同步待处理记录</h4>
    <p v-if="loadingIssues" class="cv2-field-hint" data-v2="external-pay-issues-loading">正在读取待处理记录…</p>
    <p v-else-if="issuesError" class="ep-error" data-v2="external-pay-issues-error">待处理记录读取失败：{{ issuesError }}</p>
    <p v-else-if="items.length === 0" class="cv2-field-hint" data-v2="external-pay-issues-empty">暂无待处理记录</p>
    <div v-else class="cv2-table-panel" data-v2="external-pay-issues">
      <div class="ep-scroll">
        <table class="cv2-table">
          <thead><tr><th>账单类型</th><th>交易单号</th><th>账单金额</th><th>账单时间</th><th>待处理原因</th><th>候选订单</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.id" :data-v2-issue-row="item.id">
              <td>{{ item.bill_type === 1 ? '退款' : '付款' }}</td>
              <td class="cv2-num">{{ item.transaction_id }}</td>
              <td class="cv2-num">{{ item.payment_amount }} 元</td>
              <td class="cv2-num">{{ timeText(item.pay_time) }}</td>
              <td>{{ item.reason }}</td>
              <td><span v-if="item.candidate_count">共 {{ item.candidate_count }} 张候选订单</span><div v-for="candidate in item.candidates" :key="candidate.order_no">{{ candidate.order_no }}（{{ candidate.order_amount }} 元）</div><span v-if="item.candidate_count === 0">—</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="cv2-pager" data-v2="external-pay-pager">
        <span class="cv2-pager-total">第 {{ page }} 页 · 共 {{ total }} 条</span>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="page <= 1" data-v2="external-pay-prev" @click="void loadIssues(page - 1)">上一页</button>
        <button type="button" class="cv2-btn cv2-btn--sm" :disabled="!hasNext" data-v2="external-pay-next" @click="void loadIssues(page + 1)">下一页</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ep-error { color: var(--tt2-st-error); line-height: 1.6; }
.ep-subtitle { margin: 16px 0 10px; }
.ep-scroll { overflow-x: auto; }
.ep-scroll table { min-width: 900px; }
</style>
