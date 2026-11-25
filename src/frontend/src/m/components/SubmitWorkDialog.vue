<script setup lang="ts">
import { onBeforeUnmount, ref, useTemplateRef, watch } from 'vue'
import type { Sheet } from 'framework7/types'
import { f7Button as F7Button, f7Sheet as F7Sheet, f7Radio as F7Radio } from 'framework7-vue'
import { Toast } from '../framework7Ui'
import type { Order } from '@/api/orders'
import { ISSUE_REASON_MAX_LENGTH, ISSUE_REASON_REQUIRED_TEXT, reportIssueOrder, submitMyOrder } from '@/api/myOrders'
import { orderStatusLabel, type OrderStatus } from '@/config/orderStatus'

/** 我的订单与订单详情共用，成功时用返回的订单刷新页面状态。 */
const props = defineProps<{
  visible: boolean
  /** 要操作的单号（20 位）；空串时确认**不发请求**（fail-closed）。 */
  orderNo: string
  /** 打开弹层时默认勾中的档（「提交完成」入口给 `PENDING_REVIEW`、「报问题」入口给 `ISSUE`）。 */
  initialTarget?: OrderStatus
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'success', order: Order): void
}>()

/** 桌面 `order-myorderlist.ts:470` 的 `CHANGE_STATUS_DIALOG_TITLE`（逐字）。 */
const DIALOG_TITLE = '更改订单状态'
/** 桌面 `:476` 的 `CHANGE_STATUS_TARGETS`（顺序 = radio 顺序，默认勾第一档）。 */
const TARGETS: readonly OrderStatus[] = ['PENDING_REVIEW', 'ISSUE']
/** 桌面 `:478` 的 `CHANGE_STATUS_CONFIRM_BTN`（逐字）。 */
const CONFIRM_TEXT = '确定'
/** 桌面 `:479` 的 `CHANGE_STATUS_CANCEL_BTN`（逐字）。 */
const CANCEL_TEXT = '取消'
/** spec §4.5-5 白名单句：提交完单的成功提示。 */
const SUBMIT_SUCCESS_TEXT = '提交成功'
/** spec §4.5-5 白名单句：报问题单的成功提示。 */
const REPORT_ISSUE_SUCCESS_TEXT = '已报问题单'

// 初值直接吃 `initialTarget`（组件以 `visible: true` 直接挂载时 watch 不触发，契约要一致）；
// 之后的每次打开由下面的 watch 重置。
const selected = ref<OrderStatus>(props.initialTarget ?? 'PENDING_REVIEW')
const reason = ref('')
const reasonInput = useTemplateRef<HTMLTextAreaElement>('reasonInput')
/** 在途闸：确认 / 取消 / 遮罩关在请求落地前全部禁用。 */
const submitting = ref(false)
const sheetHost = useTemplateRef<HTMLElement>('sheetHost')
// Framework7 must mount the sheet before toggling it open so the backdrop is initialized.
const submitSheet = useTemplateRef<InstanceType<typeof F7Sheet>>('submitSheet')
let openSheet: Sheet.Sheet | undefined

// Framework7 reads dismissal options again when removing its listeners. Keep those options
// static and apply the submitting guard in handlers owned by this component.
function onSheetOpen(sheet: Sheet.Sheet): void {
  openSheet = sheet
  document.addEventListener('keydown', onSheetKeyDown)
}

function onSheetClosed(): void {
  document.removeEventListener('keydown', onSheetKeyDown)
  openSheet = undefined
}

function onSheetKeyDown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && !event.defaultPrevented) onCancel()
}

function onHostClick(event: MouseEvent): void {
  if (event.target === openSheet?.backdropEl) onCancel()
}

onBeforeUnmount(() => {
  openSheet?.close(false)
  onSheetClosed()
})

watch(
  () => props.visible,
  (visible) => {
    if (!visible) return
    selected.value = props.initialTarget ?? 'PENDING_REVIEW'
    reason.value = ''
  },
)

function close(): void {
  emit('update:visible', false)
}

function onCancel(): void {
  if (submitting.value) return
  close()
}

/** 失败一律原样显示错误文本（`request()` 抛出的 message 就是后端 `msg` 原文）。 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

async function onConfirm(): Promise<void> {
  if (submitting.value) return
  // 空单号 fail-closed：一条注定打不中的请求都不发（渲染入口正常给不出空串，DOM 被人为改时兜底）。
  if (props.orderNo === '') return
  const isIssue = selected.value === 'ISSUE'
  const issueReason = reason.value.trim()
  if (isIssue && issueReason === '') {
    Toast.error(ISSUE_REASON_REQUIRED_TEXT)
    reasonInput.value?.focus()
    return
  }
  submitting.value = true
  try {
    const order =
      isIssue
        ? await reportIssueOrder(props.orderNo, issueReason)
        : await submitMyOrder(props.orderNo)
    Toast.success(isIssue ? REPORT_ISSUE_SUCCESS_TEXT : SUBMIT_SUCCESS_TEXT)
    emit('success', order)
    close()
  } catch (e) {
    Toast.error(errText(e))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div ref="sheetHost" @click="onHostClick">
    <F7Sheet
      v-if="sheetHost"
      ref="submitSheet"
      class="m-submit-sheet"
      backdrop
      role="dialog"
      aria-modal="true"
      :aria-label="DIALOG_TITLE"
      :opened="Boolean(submitSheet) && visible"
      :container-el="sheetHost"
      :close-by-backdrop-click="false"
      :close-on-escape="false"
      :animate="false"
      @sheet:open="onSheetOpen"
      @sheet:closed="onSheetClosed"
    >
      <div class="m-submit-dialog" data-testid="submit-work-dialog">
        <h3 class="m-submit-dialog__title">{{ DIALOG_TITLE }}</h3>

        <div class="m-submit-dialog__targets" data-testid="submit-targets">
          <F7Radio
            v-for="code in TARGETS"
            :key="code"
            class="m-submit-dialog__option"
            name="m-submit-target"
            :checked="selected === code"
            :value="code"
            :disabled="submitting"
            @change="selected = code"
          >
            <span>{{ orderStatusLabel(code) }}</span>
          </F7Radio>
        </div>

        <label v-if="selected === 'ISSUE'" class="m-submit-dialog__reason">
          <span>说明原因</span>
          <textarea
            ref="reasonInput"
            v-model="reason"
            aria-label="说明原因"
            :placeholder="ISSUE_REASON_REQUIRED_TEXT"
            :maxlength="ISSUE_REASON_MAX_LENGTH"
            :disabled="submitting"
            required
          />
        </label>

        <footer class="m-submit-dialog__foot">
          <F7Button type="button" class="m-submit-dialog__cancel" round outline :disabled.prop="submitting" @click="onCancel">
            {{ CANCEL_TEXT }}
          </F7Button>
          <F7Button type="button" class="m-submit-dialog__confirm" round fill :disabled.prop="submitting" @click="onConfirm">
            {{ CONFIRM_TEXT }}
          </F7Button>
        </footer>
      </div>
    </F7Sheet>
  </div>
</template>

<style scoped>
.m-submit-sheet {
  --f7-sheet-height: auto;
}

.m-submit-dialog {
  padding: 20px 16px calc(20px + env(safe-area-inset-bottom));
  background: #fff;
  border-radius: 12px 12px 0 0;
}

.m-submit-dialog__title {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 600;
  color: #1f2329;
  text-align: center;
}

.m-submit-dialog__targets {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}

.m-submit-dialog__option {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  font-size: 14px;
  color: #1f2329;
  background: #f3f3f3;
  border-radius: 8px;
}

.m-submit-dialog__foot {
  display: flex;
  gap: 12px;
}

.m-submit-dialog__reason {
  display: grid;
  gap: 8px;
  margin-bottom: 16px;
  font-size: 14px;
}

.m-submit-dialog__reason textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 72px;
  padding: 10px 12px;
  border: 1px solid #d9dce2;
  border-radius: 8px;
  font-size: 16px;
}

.m-submit-dialog__cancel,
.m-submit-dialog__confirm {
  flex: 1;
}
</style>
