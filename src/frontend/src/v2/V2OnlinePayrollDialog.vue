<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
import { computed, onMounted, ref } from 'vue'
import {
  createPayroll,
  PAYROLL_CREATING_HINT,
  payrollCategoryLabel,
  payrollRecipientTotals,
  previewPayroll,
  readPayroll,
  type PayrollBatch,
  type PayrollPreview,
  type PayrollSelection,
} from '@/api/onlinePayroll'
import PayrollBatchPanel from './PayrollBatchPanel.vue'

const props = defineProps<{ selection: PayrollSelection }>()
const emit = defineEmits<{ close: [batch: PayrollBatch | null]; changed: [batch: PayrollBatch] }>()
const preview = ref<PayrollPreview | null>(null)
const batch = ref<PayrollBatch | null>(null)
const payer = ref<number | null>(null)
const loading = ref(false)
const sending = ref(false)
const panelBusy = ref(false)
const error = ref('')
const requestId = ref('')
const busy = computed(() => sending.value || panelBusy.value)
const recipients = computed(() => preview.value?.transfers.map(payrollRecipientTotals) ?? [])

async function loadPreview(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const quote = await previewPayroll(props.selection)
    if ('existing_batch_id' in quote) {
      applyBatch(await readPayroll(quote.existing_batch_id))
    } else {
      preview.value = quote
      payer.value = quote.payers.length === 1 ? quote.payers[0]!.id : null
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取结算预览失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => { void loadPreview() })

function applyBatch(data: PayrollBatch): void {
  batch.value = data
  emit('changed', data)
}

function close(): void {
  if (!busy.value) emit('close', batch.value)
}

async function submit(): Promise<void> {
  if (sending.value || batch.value || preview.value === null || payer.value === null) return
  sending.value = true
  error.value = ''
  try {
    if (!requestId.value) requestId.value = crypto.randomUUID()
    applyBatch(await createPayroll({
      ...props.selection,
      payer_id: payer.value,
      fingerprint: preview.value.fingerprint,
      request_id: requestId.value,
    }))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '制单失败，请用本次选择重试或查看在线结算明细'
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="true"
    title="结算并直接打款"
    width="min(960px, 94vw)"
    class="cv2-dialog"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    @update:model-value="close"
  >
    <p v-if="loading" role="status">正在核对待结报酬…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="sending" class="cv2-note" role="status">{{ PAYROLL_CREATING_HINT }}</p>
    <PayrollBatchPanel
      v-if="batch"
      :batch="batch"
      auto-show-payment
      @updated="applyBatch"
      @busy="panelBusy = $event"
    />
    <template v-else-if="preview && !sending">
      <div class="cv2-toolbar">
        <strong>{{ payrollCategoryLabel(selection.category) }} · 实付合计 {{ preview.total }} 元</strong>
        <label>
          付款支付宝
          <select v-model="payer" :disabled="sending || Boolean(requestId)" data-v2="payroll-payer">
            <option :value="null" disabled>请选择</option>
            <option v-for="option in preview.payers" :key="option.id" :value="option.id">
              {{ option.real_name }}（{{ option.account_masked }}）
            </option>
          </select>
        </label>
      </div>
      <p v-if="!preview.payers.length" class="cv2-note">没有已授权且可用的付款支付宝。</p>
      <div v-if="recipients.length" class="payroll-preview">
        <table class="cv2-table" aria-label="按收款人汇总">
          <thead><tr><th>收款人</th><th>收款支付宝</th><th>生效绩效</th><th>扣点</th><th>未结调整</th><th>实付金额</th></tr></thead>
          <tbody>
            <tr v-for="(recipient, index) in recipients" :key="index">
              <td>{{ recipient.recipient_name }}</td>
              <td>{{ recipient.account_masked }}</td>
              <td>{{ recipient.bonus }}</td>
              <td>{{ recipient.deduction }}</td>
              <td>{{ recipient.adjustment }}</td>
              <td>{{ recipient.amount }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <template #footer>
      <button type="button" class="cv2-btn" :disabled="busy" @click="close">关闭</button>
      <button
        v-if="preview && !batch"
        type="button"
        class="cv2-btn cv2-btn--primary"
        :disabled="payer === null || sending || !preview.transfers.length"
        data-v2="payroll-submit"
        @click="submit"
      >{{ sending ? '正在制单…' : '确认制单' }}</button>
    </template>
  </el-dialog>
</template>

<style scoped>
.payroll-preview { margin-top: 12px; max-height: 50vh; overflow: auto; }
</style>
