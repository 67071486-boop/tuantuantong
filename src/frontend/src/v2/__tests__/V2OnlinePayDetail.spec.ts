// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import type { PayrollBatch } from '@/api/onlinePayroll'
import V2OnlinePayDetail from '../V2OnlinePayDetail.vue'

const api = vi.hoisted(() => ({ listPayroll: vi.fn(), readPayroll: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

it('隐藏付款单号，点击整行或文字操作打开付款明细', async () => {
  const batches: [PayrollBatch, PayrollBatch] = [{
    id: '列表隐藏的付款单号甲', category: 'taker', created_at: '2026-11-15T10:00:00',
    status: 'failed', total: '25.00', payer_masked: '***101', active: false, gateway_batches: [],
  }, {
    id: '列表隐藏的付款单号乙', category: 'customer_service', created_at: '2026-11-15T11:00:00',
    status: 'succeeded', total: '15.00', payer_masked: '***102', active: false, gateway_batches: [],
  }]
  api.listPayroll.mockResolvedValue({ count: batches.length, items: batches })
  api.readPayroll.mockImplementation((id: string) => Promise.resolve(batches.find((batch) => batch.id === id)))
  const wrapper = mount(V2OnlinePayDetail, {
    global: { stubs: { 'el-dialog': { template: '<div><slot /><slot name="footer" /></div>' } } },
  })
  try {
    await flushPromises()
    const table = wrapper.get('table')
    expect(table.text()).not.toMatch(/付款单号|列表隐藏的/)
    expect(table.get('.cv2-st--mark.cv2-st--error').text()).toBe('打款失败')
    expect(table.get('.cv2-st--mark.cv2-st--done').text()).toBe('打款成功')
    await table.get('tbody tr:first-child td').trigger('click')
    await flushPromises()
    expect(api.readPayroll).toHaveBeenCalledExactlyOnceWith(batches[0].id)
    expect(wrapper.get('.payroll-batch').text()).toContain('打款失败 · 25.00 元')
    await table.get('tbody tr:nth-child(2) button.cv2-actlink').trigger('click')
    await flushPromises()
    expect(api.readPayroll.mock.calls).toEqual(batches.map((batch) => [batch.id]))
    expect(wrapper.get('.payroll-batch').text()).toContain('打款成功 · 15.00 元')
  } finally {
    wrapper.unmount()
  }
})
