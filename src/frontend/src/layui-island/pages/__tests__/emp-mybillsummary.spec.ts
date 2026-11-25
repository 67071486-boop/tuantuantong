// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { mountLegacyRecords } from '../../onlinePayroll'

const api = vi.hoisted(() => ({ listMyPayroll: vi.fn(), listPayroll: vi.fn(), readPayroll: vi.fn() }))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

it('旧版个人结算记录只读本人记录，流水关联到业务订单', async () => {
  api.listMyPayroll.mockResolvedValue({ count: 1, items: [{
    id: 7, recipient_name: '打手甲', amount: '25.00', status: 'succeeded', finalized_at: '2026-11-15',
    account_masked: '***222', channel_order_no: '流水甲',
    items: [{ performance_id: 2, order_no: '订单甲', bonus: '25.00', deduction: '0.00', adjustment: '0.00', amount: '25.00' }],
  }] })
  const host = document.createElement('div')
  const stop = mountLegacyRecords(host, document.createElement('div'), true, '其他人的付款单')
  await flushPromises()
  expect(api.listMyPayroll).toHaveBeenCalledWith(1)
  expect(api.listPayroll).not.toHaveBeenCalled()
  expect(api.readPayroll).not.toHaveBeenCalled()
  expect(host.querySelector('details')?.textContent).toContain('订单甲')
  expect(host.querySelector('details')?.textContent).toContain('流水甲')
  stop()
})
