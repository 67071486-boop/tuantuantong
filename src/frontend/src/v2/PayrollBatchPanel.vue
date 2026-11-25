<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import {
  cancelPayroll,
  keepPayrollPaymentCredentials,
  openPayrollBrowserPayment,
  PAYROLL_AUTO_QR_ATTEMPTS,
  PAYROLL_BROWSER_BLOCKED,
  PAYROLL_BROWSER_BUTTON,
  PAYROLL_WAITING_HINT,
  payrollQrRefreshStopped,
  payrollStatusLabel,
  preparePayrollPayment,
  readPayroll,
  type PayrollBatch,
  type PayrollGatewayBatch,
} from '@/api/onlinePayroll'
import { isMobileUserAgent } from '@/mobileRedirect'
import PayrollTransferTable from './PayrollTransferTable.vue'

const props = defineProps<{ batch: PayrollBatch; autoShowPayment?: boolean }>()
const emit = defineEmits<{ updated: [batch: PayrollBatch]; busy: [busy: boolean] }>()
const busy = ref(false)
const error = ref('')
const confirmCancel = ref(false)
const paymentProgress = ref<{ id: PayrollGatewayBatch['id']; label: string }>()
const autoQrId = ref<PayrollGatewayBatch['id']>()
const desktop = !isMobileUserAgent(navigator.userAgent)
let disposed = false
let timer: ReturnType<typeof setTimeout> | undefined
let pollSequence = 0
let autoQrReady = false
let allowAutoPrepare = true
const autoQrAttempts = new Map<PayrollGatewayBatch['id'], number>()
const awaitingQr = ref<PayrollGatewayBatch['id']>()

function scheduleRefresh(): void {
  clearTimeout(timer)
  if (disposed || busy.value || autoQrId.value !== undefined || !props.batch.active) return
  timer = setTimeout(() => { void pollStatus() }, 3000)
}

async function pollStatus(): Promise<void> {
  const sequence = ++pollSequence
  const id = props.batch.id
  try {
    const updated = await readPayroll(id)
    if (!disposed && sequence === pollSequence && id === props.batch.id) {
      allowAutoPrepare = true
      emit('updated', keepPayrollPaymentCredentials(props.batch, updated))
    }
  } catch {
    // The next poll retries transient status errors.
  } finally {
    if (sequence === pollSequence) scheduleRefresh()
  }
}

watch(() => props.batch, (batch) => {
  scheduleRefresh()
  if (busy.value || autoQrId.value !== undefined) return
  if (batch.gateway_batches.some((part) => part.payment_qr_png_base64)) autoQrReady = true
  const refreshing = batch.gateway_batches.find((part) => part.id === awaitingQr.value)
  if (refreshing?.can_prepare_payment) {
    awaitingQr.value = undefined
    void prepareAutomaticQr(refreshing)
    return
  }
  if (!batch.active || payrollQrRefreshStopped(refreshing)) awaitingQr.value = undefined
  if (!props.autoShowPayment || autoQrReady || !allowAutoPrepare) return
  allowAutoPrepare = false
  const first = batch.gateway_batches.find((part) =>
    part.can_prepare_payment && (autoQrAttempts.get(part.id) ?? 0) < PAYROLL_AUTO_QR_ATTEMPTS,
  )
  if (first) {
    autoQrAttempts.set(first.id, (autoQrAttempts.get(first.id) ?? 0) + 1)
    void prepareAutomaticQr(first)
  }
}, { immediate: true, flush: 'post' })

onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})

async function perform(
  action: () => Promise<PayrollBatch>, progress?: typeof paymentProgress.value,
): Promise<void> {
  if (busy.value || disposed) return
  // Ignore status reads that started before this action.
  ++pollSequence
  clearTimeout(timer)
  autoQrId.value = undefined
  busy.value = true
  paymentProgress.value = progress
  emit('busy', true)
  error.value = ''
  try {
    const updated = await action()
    if (!disposed) {
      confirmCancel.value = false
      busy.value = false
      emit('updated', keepPayrollPaymentCredentials(props.batch, updated))
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '操作失败，请查看原付款单状态。'
  } finally {
    busy.value = false
    paymentProgress.value = undefined
    emit('busy', false)
    scheduleRefresh()
  }
}

function prepare(batch: PayrollGatewayBatch): void {
  void perform(() => preparePayrollPayment(props.batch.id, batch.id), {
    id: batch.id, label: '正在获取付款二维码，请稍候…',
  })
}

async function prepareAutomaticQr(part: PayrollGatewayBatch): Promise<void> {
  const sequence = ++pollSequence
  const id = props.batch.id
  autoQrId.value = part.id
  error.value = ''
  try {
    const updated = await preparePayrollPayment(id, part.id)
    if (!disposed && sequence === pollSequence && id === props.batch.id) {
      autoQrId.value = undefined
      emit('updated', keepPayrollPaymentCredentials(props.batch, updated))
    }
  } catch (cause) {
    if (sequence === pollSequence) error.value = cause instanceof Error ? cause.message : '获取付款码失败'
  } finally {
    if (sequence === pollSequence) {
      autoQrId.value = undefined
      scheduleRefresh()
    }
  }
}

function prepareDesktop(batch: PayrollGatewayBatch): void {
  const id = props.batch.id
  void perform(async () => {
    const result = await openPayrollBrowserPayment(
      () => preparePayrollPayment(id, batch.id, false, true),
      batch.id, () => !disposed && id === props.batch.id,
    )
    if (result.blocked) error.value = PAYROLL_BROWSER_BLOCKED
    return result.batch
  }, {
    id: batch.id, label: '正在获取电脑付款链接，请稍候…',
  })
}

function refreshLink(batch: PayrollGatewayBatch): void {
  void perform(async () => {
    const result = await preparePayrollPayment(props.batch.id, batch.id, true)
    awaitingQr.value = result.active ? batch.id : undefined
    return result
  }, { id: batch.id, label: '正在重新生成付款二维码，请稍候…' })
}

function refresh(): Promise<void> {
  return perform(async () => {
    const result = await readPayroll(props.batch.id)
    allowAutoPrepare = true
    return result
  })
}

function cancel(): void {
  void perform(() => cancelPayroll(props.batch.id))
}
</script>

<template>
  <div class="payroll-batch">
    <div class="cv2-toolbar">
      <strong>{{ payrollStatusLabel(batch.status) }} · {{ batch.total }} 元</strong>
      <span>付款支付宝 {{ batch.payer_masked || '—' }}</span>
      <button v-if="batch.active" type="button" class="cv2-btn" :disabled="busy" @click="refresh()">
        刷新打款结果
      </button>
      <button
        v-if="batch.can_cancel && !confirmCancel"
        type="button"
        class="cv2-btn"
        :disabled="busy"
        @click="confirmCancel = true"
      >取消本次付款</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="autoShowPayment && batch.status === 'preparing'" class="cv2-note" role="status">
      {{ PAYROLL_WAITING_HINT }}
    </p>
    <div v-if="batch.can_cancel && confirmCancel" class="cv2-note">
      只会取消尚未向计全付制单的明细，已提交明细仍需核对。
      <button type="button" class="cv2-btn" :disabled="busy" @click="confirmCancel = false">返回</button>
      <button type="button" class="cv2-btn" :disabled="busy" @click="cancel">确认取消</button>
    </div>
    <section v-for="(part, index) in batch.gateway_batches" :key="part.id" class="payroll-part">
      <h3>批次 {{ index + 1 }} · {{ payrollStatusLabel(part.status) }}</h3>
      <p v-if="part.last_error">{{ part.last_error }}</p>
      <p v-if="paymentProgress?.id === part.id" role="status">{{ paymentProgress.label }}</p>
      <p v-else-if="awaitingQr === part.id" role="status">正在重新生成付款二维码，请稍候…</p>
      <div v-if="part.can_prepare_payment" class="payroll-methods">
        <section class="payroll-method" aria-label="手机支付宝扫码">
          <h4>手机支付宝扫码</h4>
          <p>用选定的付款支付宝扫码，核对金额后确认。</p>
          <img
            v-if="part.payment_qr_png_base64"
            :src="`data:image/png;base64,${part.payment_qr_png_base64}`"
            alt="支付宝付款二维码"
            width="200"
            height="200"
          >
          <p v-else-if="autoQrId === part.id" role="status">正在获取付款二维码，请稍候…</p>
          <button
            type="button"
            class="cv2-btn"
            :disabled="busy || autoQrId === part.id"
            @click="part.payment_qr_png_base64 ? refreshLink(part) : prepare(part)"
          >{{ part.payment_qr_png_base64 ? '重新获取付款码' : '获取付款码' }}</button>
        </section>
        <section v-if="desktop" class="payroll-method" aria-label="电脑浏览器付款">
          <h4>电脑浏览器付款</h4>
          <p>在浏览器打开支付宝付款页，核对金额后确认。</p>
          <a
            v-if="part.payment_pc_url"
            class="cv2-btn cv2-btn--primary"
            :href="part.payment_pc_url"
            target="_blank"
            rel="noopener noreferrer"
          >{{ PAYROLL_BROWSER_BUTTON }}</a>
          <button v-else type="button" class="cv2-btn cv2-btn--primary" :disabled="busy" @click="prepareDesktop(part)">
            {{ PAYROLL_BROWSER_BUTTON }}
          </button>
        </section>
      </div>
      <PayrollTransferTable :transfers="part.transfers" />
    </section>
  </div>
</template>

<style scoped>
.payroll-batch { display: grid; gap: 12px; }
.payroll-part { display: grid; gap: 8px; }
.payroll-methods { display: flex; flex-wrap: wrap; gap: 12px; }
.payroll-method { flex: 1 1 240px; display: grid; align-content: start; justify-items: start; gap: 8px; }
</style>
