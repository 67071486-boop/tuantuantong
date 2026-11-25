<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  listPayroll,
  payrollCategoryLabel,
  payrollStatusLabel,
  readPayroll,
  type PayrollBatch,
  type PayrollBatchSummary,
} from '@/api/onlinePayroll'
import PayrollBatchPanel from './PayrollBatchPanel.vue'

const route = useRoute()
const rows = ref<PayrollBatchSummary[]>([])
const detail = ref<PayrollBatch | null>(null)
const detailId = ref<string>()
const page = ref(1)
const count = ref(0)
const loading = ref(false)
const detailLoading = ref(false)
const detailBusy = ref(false)
const listError = ref('')
const detailError = ref('')
let detailRequest = 0
const statusClasses: Record<string, string> = {
  preparing: 'cv2-st--unassigned',
  ready: 'cv2-st--pending',
  processing: 'cv2-st--doing',
  needs_review: 'cv2-st--accept',
  partial: 'cv2-st--accept',
  succeeded: 'cv2-st--done',
  failed: 'cv2-st--error',
  closed: 'cv2-st--unassigned',
}

async function loadList(next = page.value): Promise<void> {
  if (loading.value) return
  loading.value = true
  listError.value = ''
  try {
    const result = await listPayroll(next)
    rows.value = result.items
    count.value = result.count
    page.value = next
  } catch (cause) {
    listError.value = cause instanceof Error ? cause.message : '读取付款单失败'
  } finally {
    loading.value = false
  }
}

async function loadDetail(id: string | undefined): Promise<void> {
  const request = ++detailRequest
  detail.value = null
  detailError.value = ''
  detailLoading.value = Boolean(id)
  if (!id) return
  try {
    const result = await readPayroll(id)
    if (request === detailRequest) detail.value = result
  } catch (cause) {
    if (request === detailRequest) {
      detailError.value = cause instanceof Error ? cause.message : '读取付款单明细失败'
    }
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

watch(() => route.query.batch, (id) => { openDetail(typeof id === 'string' ? id : undefined) }, { immediate: true })
void loadList()

function openDetail(id?: string): void {
  if (detailBusy.value || id === detailId.value) return
  detailId.value = id
  void loadDetail(id)
}

function updateDetail(updated: PayrollBatch): void {
  detail.value = updated
  rows.value = rows.value.map((row) => row.id === updated.id ? updated : row)
}
</script>

<template>
  <div class="cv2-page" data-v2-page="online-pay-detail">
    <div class="cv2-toolbar">
      <button type="button" class="cv2-btn" :disabled="loading" @click="loadList()">刷新付款单</button>
    </div>
    <p v-if="listError" role="alert">{{ listError }}</p>
    <section class="cv2-panel cv2-table-panel" aria-label="在线结算付款单">
      <table class="cv2-table">
        <thead>
          <tr><th>报酬类别</th><th>付款支付宝</th><th>实付合计</th><th>付款状态</th><th>制单时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="payroll-row" @click="openDetail(row.id)">
            <td>{{ payrollCategoryLabel(row.category) }}</td>
            <td>{{ row.payer_masked || '—' }}</td>
            <td class="cv2-td-num">{{ row.total }} 元</td>
            <td>
              <span class="cv2-st cv2-st--mark" :class="statusClasses[row.status] ?? 'cv2-st--accept'">
                <i class="cv2-dot" aria-hidden="true"></i>{{ payrollStatusLabel(row.status) }}
              </span>
            </td>
            <td>{{ row.created_at.replace('T', ' ').slice(0, 19) }}</td>
            <td><button type="button" class="cv2-actlink" @click.stop="openDetail(row.id)">查看付款单</button></td>
          </tr>
          <tr v-if="!rows.length && !listError"><td colspan="6" class="cv2-empty">{{ loading ? '读取中…' : '暂无付款单' }}</td></tr>
        </tbody>
      </table>
    </section>
    <div v-if="count > 25" class="cv2-toolbar" aria-label="付款单分页">
      <button type="button" class="cv2-btn" :disabled="loading || page === 1" @click="loadList(page - 1)">上一页</button>
      <span>第 {{ page }} 页，共 {{ count }} 条</span>
      <button type="button" class="cv2-btn" :disabled="loading || page * 25 >= count" @click="loadList(page + 1)">下一页</button>
    </div>
    <el-dialog
      v-if="detailId"
      :model-value="true"
      title="在线结算付款单"
      width="min(1080px, 94vw)"
      class="cv2-dialog"
      :close-on-click-modal="false"
      :close-on-press-escape="!detailBusy"
      :show-close="!detailBusy"
      @update:model-value="openDetail()"
    >
      <p v-if="detailLoading" role="status">正在读取付款单…</p>
      <p v-if="detailError" role="alert">{{ detailError }}</p>
      <PayrollBatchPanel
        v-if="detail"
        :key="detail.id"
        :batch="detail"
        auto-show-payment
        @updated="updateDetail"
        @busy="detailBusy = $event"
      />
      <template #footer>
        <button type="button" class="cv2-btn" :disabled="detailBusy" @click="openDetail()">关闭</button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.payroll-row {
  cursor: pointer;
}
</style>
