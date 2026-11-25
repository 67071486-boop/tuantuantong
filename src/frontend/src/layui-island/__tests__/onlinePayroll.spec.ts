// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import type { PayrollBatch, PayrollPreview } from '@/api/onlinePayroll'
import { mountLegacyBatch, mountLegacyPreview } from '../onlinePayroll'

const api = vi.hoisted(() => ({
  previewPayroll: vi.fn(), createPayroll: vi.fn(), readPayroll: vi.fn(), preparePayrollPayment: vi.fn(),
}))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

it('从确认制单显示等待提示，自动取付款码，失败后在原位置显示结果', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const preview: PayrollPreview = {
    fingerprint: 'quote', total: '10.00',
    payers: [{ id: 1, account_masked: '***101', real_name: '付款甲' }],
    transfers: [{
      recipient_name: '收款甲', account_masked: '***201', amount: '10.00', items: [
        { performance_id: 3, order_no: '订单甲', bonus: '7.11', deduction: '2.22', adjustment: '-0.01', amount: '4.88' },
        { performance_id: 4, order_no: '订单乙', bonus: '4.89', deduction: '0.00', adjustment: '0.23', amount: '5.12' },
      ],
    }],
  }
  const gateway = { id: 32, status: 'sealing', can_prepare_payment: false, transfers: [] }
  const saved: PayrollBatch = {
    id: '原付款单', category: 'taker', created_at: '', status: 'preparing',
    total: '10.00', payer_masked: '***101', active: true, gateway_batches: [gateway],
  }
  const payable = { ...gateway, status: 'payable', can_prepare_payment: true }
  api.previewPayroll.mockResolvedValue(preview)
  let save!: (batch: PayrollBatch) => void
  api.createPayroll.mockReturnValueOnce(new Promise((resolve) => { save = resolve }))
  api.readPayroll.mockResolvedValueOnce({ ...saved, status: 'ready', gateway_batches: [payable] })
  api.preparePayrollPayment.mockResolvedValue({
    ...saved, status: 'ready', gateway_batches: [{ ...payable, payment_qr_png_base64: 'cXI=' }],
  })
  api.readPayroll.mockResolvedValueOnce({
    ...saved, status: 'failed', active: false, gateway_batches: [{ ...gateway, status: 'failed' }],
  })
  const host = document.createElement('div')
  document.body.append(host)
  const cleanup = mountLegacyPreview(host, { category: 'taker', performance_ids: [3, 4] }, vi.fn())
  try {
    await flushPromises()
    expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
    expect(Array.from(host.querySelectorAll('tbody td'), (cell) => cell.textContent)).toEqual([
      '收款甲', '***201', '12.00', '2.22', '0.22', '10.00',
    ])
    expect(host.textContent).not.toMatch(/关联订单|订单甲|订单乙/)
    host.querySelector('button')!.click()
    expect(host.querySelector('[role="status"]:not([hidden])')?.textContent).toContain('正在制单，请稍候')
    save(saved)
    await flushPromises()
    expect(host.querySelector('[role="status"]')?.textContent).toContain('付款单已保存')
    await vi.advanceTimersByTimeAsync(3000)
    expect(host.querySelector('img[alt="支付宝付款二维码"]')).toBeTruthy()
    await vi.advanceTimersByTimeAsync(3000)
    expect(host.querySelector('h3')?.textContent).toContain('打款失败')
    expect(host.querySelector('img')).toBeNull()
  } finally {
    cleanup()
    host.remove()
    vi.useRealTimers()
  }
})

it('自动查询期间可获取电脑付款链接，迟到的查询不会覆盖链接', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('desktop')
  const tab = { opener: window, closed: false, location: { replace: vi.fn() }, close: vi.fn() }
  const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
  api.readPayroll.mockReset()
  api.preparePayrollPayment.mockReset()
  const gateway = { id: 32, status: 'payable', can_prepare_payment: true, transfers: [] }
  const payable: PayrollBatch = {
    id: '原付款单', category: 'taker', created_at: '', status: 'ready',
    total: '10.00', payer_masked: '***101', active: true,
    gateway_batches: [{ ...gateway, payment_qr_png_base64: 'cXI=' }],
  }
  let resolvePoll!: (result: PayrollBatch) => void
  api.readPayroll.mockReturnValueOnce(new Promise((resolve) => { resolvePoll = resolve }))
  api.preparePayrollPayment.mockResolvedValue({
    ...payable,
    gateway_batches: [{ ...gateway, payment_pc_url: 'https://example.test/desktop-pay' }],
  })
  const host = document.createElement('div')
  document.body.append(host)
  const cleanup = mountLegacyBatch(host, payable, vi.fn(), true)
  try {
    await vi.advanceTimersByTimeAsync(3000)
    expect(api.readPayroll).toHaveBeenCalledExactlyOnceWith(payable.id)
    const desktopButton = host.querySelector<HTMLButtonElement>('[aria-label="电脑浏览器付款"] button')!
    expect(desktopButton.textContent).toBe('跳转至浏览器付款')
    expect(desktopButton.disabled).toBe(false)
    desktopButton.click()
    await flushPromises()
    expect(api.preparePayrollPayment).toHaveBeenCalledExactlyOnceWith(payable.id, 32, false, true)
    expect(open).toHaveBeenCalledExactlyOnceWith('', '_blank')
    expect(tab.opener).toBeNull()
    expect(tab.location.replace).toHaveBeenCalledExactlyOnceWith('https://example.test/desktop-pay')
    expect(tab.close).not.toHaveBeenCalled()
    resolvePoll({
      ...payable, status: 'preparing', gateway_batches: [{ ...gateway, status: 'sealing', can_prepare_payment: false }],
    })
    await flushPromises()
    expect(host.querySelector('a')?.href).toBe('https://example.test/desktop-pay')
    expect(host.querySelector('img[alt="支付宝付款二维码"]')).toBeTruthy()
  } finally {
    cleanup()
    host.remove()
    ua.mockRestore()
    open.mockRestore()
    vi.useRealTimers()
  }
})
