// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { mountLegacyRecords } from '../../onlinePayroll'

const api = vi.hoisted(() => ({ listPayroll: vi.fn(), readPayroll: vi.fn(), preparePayrollPayment: vi.fn() }))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

it('在线结算明细通过原付款单继续获取付款码，展示部分成功的逐笔状态', async () => {
  const part = { id: 1, status: 'payable', can_prepare_payment: true, transfers: [
    { id: 7, recipient_name: '打手甲', amount: '25.00', status: 'succeeded', finalized_at: '2026-11-15', items: [] },
    { id: 8, recipient_name: '打手乙', amount: '15.00', status: 'processing', items: [] },
  ] }
  const batch = { id: '原付款单', category: 'taker', status: 'partial', total: '40.00', active: true, gateway_batches: [part] }
  api.listPayroll.mockResolvedValue({ count: 1, items: [batch] })
  api.readPayroll.mockResolvedValue(batch)
  api.preparePayrollPayment.mockResolvedValue({ ...batch, gateway_batches: [{ ...part, payment_qr_png_base64: 'cXI=' }] })
  const host = document.createElement('div')
  const side = document.createElement('div')
  document.body.append(host, side)
  const stop = mountLegacyRecords(host, side, false, batch.id)
  await flushPromises()
  expect(api.readPayroll).toHaveBeenCalledWith(batch.id)
  expect(side.textContent).toContain('部分成功')
  expect(side.textContent).toContain('打款成功')
  expect(side.textContent).toContain('处理中')
  expect(api.preparePayrollPayment).toHaveBeenCalledExactlyOnceWith(batch.id, part.id)
  expect(side.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,cXI=')
  stop()
  host.remove()
  side.remove()
})

it('旧版列表隐藏付款单号，点击整行或文字操作查看同一份明细', async () => {
  api.readPayroll.mockClear()
  const batch = { id: '列表隐藏的付款单号', category: 'taker', status: 'failed', total: '25.00', active: false, gateway_batches: [] }
  api.listPayroll.mockResolvedValue({ count: 1, items: [batch] })
  api.readPayroll.mockResolvedValue(batch)
  const host = document.createElement('div')
  const side = document.createElement('div')
  const stop = mountLegacyRecords(host, side, false)
  try {
    await flushPromises()
    expect(host.textContent).not.toMatch(/付款单号|列表隐藏的/)
    expect(host.querySelector('tbody tr .lableb')?.textContent).toBe('打款失败')
    const row = host.querySelector('tbody tr')!
    const view = row.querySelector('button')!
    expect(view.textContent).toBe('查看付款单')
    expect(view.classList.contains('layui-btn')).toBe(false)
    row.querySelector('td')!.click()
    await flushPromises()
    expect(side.textContent).toContain('打款失败 · 25.00 元')
    expect(api.readPayroll).toHaveBeenCalledExactlyOnceWith(batch.id)
    view.click()
    await flushPromises()
    expect(api.readPayroll).toHaveBeenCalledTimes(2)
  } finally {
    stop()
  }
})
