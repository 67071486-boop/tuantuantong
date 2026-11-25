import { request } from './http'

const ROOT = '/api/business/online-payroll/'

export const PAYROLL_CLOSE_HINT = '付款单已保存，可前往「在线结算明细」继续付款。'
export const PAYROLL_CREATING_HINT = '正在制单，请稍候。付款单保存后将继续准备付款二维码。'
export const PAYROLL_WAITING_HINT = '付款单已保存，正在准备支付宝付款二维码。准备好后会自动显示。'
export const PAYROLL_AUTO_QR_ATTEMPTS = 3
export const PAYROLL_BROWSER_BUTTON = '跳转至浏览器付款'
export const PAYROLL_BROWSER_BLOCKED = '浏览器未打开付款页，请再次点击「跳转至浏览器付款」。'
export type PayrollCategory = 'taker' | 'customer_service'

export interface PayrollSelection {
  performance_ids: number[]
  category: PayrollCategory
}

export interface PayrollItem {
  performance_id: number
  order_no: string
  bonus: string
  deduction: string
  adjustment: string
  amount: string
}

export interface PayrollPreviewTransfer {
  recipient_name: string
  account_masked: string
  amount: string
  items: PayrollItem[]
}

export function payrollRecipientTotals(transfer: PayrollPreviewTransfer) {
  const sum = (field: 'bonus' | 'deduction' | 'adjustment') =>
    (transfer.items.reduce((cents, item) => cents + Math.round(Number(item[field]) * 100), 0) / 100).toFixed(2)
  return {
    recipient_name: transfer.recipient_name,
    account_masked: transfer.account_masked,
    bonus: sum('bonus'),
    deduction: sum('deduction'),
    adjustment: sum('adjustment'),
    amount: transfer.amount,
  }
}

export interface PayrollTransfer extends PayrollPreviewTransfer {
  id: number | string
  status: string
  channel_order_no: string | null
  transfer_id: string | null
  success_time: string | null
  last_error?: string
  batch_id?: string
  finalized_at?: string | null
}

export interface PayrollPreview {
  fingerprint: string
  payers: { id: number; account_masked: string; real_name: string }[]
  total: string
  transfers: PayrollPreviewTransfer[]
}

export interface PayrollGatewayBatch {
  id: number | string
  status: string
  payment_url?: string | null
  payment_pc_url?: string | null
  payment_qr_png_base64?: string | null
  can_prepare_payment?: boolean
  last_error?: string
  transfers: PayrollTransfer[]
}

export interface PayrollBatchSummary {
  id: string
  category: PayrollCategory
  status: string
  total: string
  created_at: string
  payer_masked: string
}

export interface PayrollBatch extends PayrollBatchSummary {
  can_cancel?: boolean
  active: boolean
  gateway_batches: PayrollGatewayBatch[]
}

interface PayrollPage<T> {
  items: T[]
  count: number
}

export function previewPayroll(selection: PayrollSelection): Promise<PayrollPreview | { existing_batch_id: string }> {
  return request({ url: ROOT + 'preview/', method: 'post', data: selection })
}

export function createPayroll(data: PayrollSelection & {
  payer_id: number
  fingerprint: string
  request_id: string
}): Promise<PayrollBatch> {
  return request({ url: ROOT + 'batches/', method: 'post', data })
}

export function listPayroll(page: number): Promise<PayrollPage<PayrollBatchSummary>> {
  return request({ url: ROOT + 'batches/', params: { page } })
}

export function listMyPayroll(page: number): Promise<PayrollPage<PayrollTransfer>> {
  return request({ url: ROOT + 'mine/', params: { page } })
}

export function readPayroll(id: string): Promise<PayrollBatch> {
  return request({ url: ROOT + `batches/${encodeURIComponent(id)}/` })
}

export function preparePayrollPayment(
  id: string, gatewayBatchId: string | number, refreshLink = false, desktop = false,
): Promise<PayrollBatch> {
  return request({
    url: ROOT + `batches/${encodeURIComponent(id)}/payment/`,
    method: 'post',
    data: { gateway_batch_id: gatewayBatchId, refresh_link: refreshLink, ...(desktop ? { desktop } : {}) },
  })
}

export async function openPayrollBrowserPayment(
  requestLink: () => Promise<PayrollBatch>, gatewayBatchId: PayrollGatewayBatch['id'], isOpen: () => boolean,
): Promise<{ batch: PayrollBatch; blocked: boolean }> {
  // A new tab must be created while the browser still has the click gesture.
  const tab = window.open('', '_blank')
  if (tab) tab.opener = null
  let opened = false
  try {
    const batch = await requestLink()
    const part = batch.gateway_batches.find((item) => item.id === gatewayBatchId)
    const url = batch.active && part?.can_prepare_payment ? part.payment_pc_url : undefined
    if (isOpen() && url?.startsWith('https://') && tab && !tab.closed) {
      try {
        tab.location.replace(url)
        opened = true
      } catch {
        // The button can use the returned URL on a second click.
      }
    }
    return { batch, blocked: Boolean(url) && !opened }
  } finally {
    if (tab && !opened) tab.close()
  }
}

export function keepPayrollPaymentCredentials(current: PayrollBatch, updated: PayrollBatch): PayrollBatch {
  return {
    ...updated,
    gateway_batches: updated.gateway_batches.map((part) => {
      const previous = current.gateway_batches.find((item) => item.id === part.id)
      if (!part.can_prepare_payment || previous?.status !== part.status) return part
      return {
        ...part,
        payment_qr_png_base64: part.payment_qr_png_base64 ?? previous.payment_qr_png_base64,
        payment_pc_url: part.payment_pc_url ?? previous.payment_pc_url,
      }
    }),
  }
}

export function payrollQrRefreshStopped(part: PayrollGatewayBatch | undefined): boolean {
  return !part || ['succeeded', 'partial', 'failed', 'closed'].includes(part.status)
    || (part.status === 'unknown' && Boolean(part.last_error))
}

export function cancelPayroll(id: string): Promise<PayrollBatch> {
  return request({ url: ROOT + `batches/${encodeURIComponent(id)}/cancel/`, method: 'post', data: {} })
}

const PAYROLL_STATUS_LABELS: Record<string, string> = {
  preparing: '制单中',
  ready: '待付款',
  processing: '处理中',
  needs_review: '待核对',
  partial: '部分成功',
  succeeded: '打款成功',
  closed: '已关闭',
  staged: '等待制单',
  sealing: '正在生成付款码',
  payable: '待扫码付款',
  submitting: '正在制单',
  created: '已制单',
  failed: '打款失败',
  unknown: '待核对',
}

export function payrollStatusLabel(status: string): string {
  return PAYROLL_STATUS_LABELS[status] ?? '待核对'
}

export function payrollCategoryLabel(category: PayrollCategory): string {
  return category === 'taker' ? '打手报酬' : '客服报酬'
}
