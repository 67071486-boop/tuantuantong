// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { MENU_GROUPS } from '@/config/menu'
import { nodeCodesFor } from '@/config/routePermissions'
import { canSeeItem } from '@/composables/useMenu'
import V2Settlement from '../V2Settlement.vue'

const api = vi.hoisted(() => ({ listMyPayroll: vi.fn(), listPayroll: vi.fn() }))
vi.mock('@/api/onlinePayroll', async (original) => ({
  ...await original<typeof import('@/api/onlinePayroll')>(),
  ...api,
}))

it('只拥有我的绩效权限的打手仍能看到自己的在线结算入口', () => {
  const item = MENU_GROUPS.flatMap((group) => group.items).find((row) => row.routeName === 'agents-my-settlement')!
  expect(canSeeItem(item, { isTenantAdmin: false, isAccountAdmin: false, nodePermissions: new Set(['my_performance']) })).toBe(true)
  expect(nodeCodesFor('agents-my-settlement')).toContain('my_performance')
})

it('只请求自己的转账记录并能展开对应业务订单和流水号', async () => {
  api.listMyPayroll.mockResolvedValue({
    count: 1,
    items: [{
      id: 21,
      status: 'succeeded',
      recipient_name: '打手甲',
      amount: '18.50',
      account_masked: '***245',
      transfer_id: '计全付单号甲',
      channel_order_no: '支付宝流水甲',
      success_time: '2026-11-15T12:34:56+08:00',
      finalized_at: '2026-11-15T12:35:00+08:00',
      items: [{ performance_id: 7, order_no: '业务订单甲', bonus: '20.00', deduction: '1.50', adjustment: '0.00', amount: '18.50' }],
    }],
  })
  const wrapper = mount(V2Settlement)
  await flushPromises()
  expect(api.listMyPayroll).toHaveBeenCalledWith(1)
  expect(api.listPayroll).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('支付宝流水甲')
  expect(wrapper.text()).toContain('2026-11-15 12:34:56')
  await wrapper.findAll('button').find((button) => button.text() === '查看关联订单')!.trigger('click')
  expect(wrapper.text()).toContain('业务订单甲')
  expect(wrapper.text()).toContain('18.50')
  wrapper.unmount()
})

it('接口返回空数组时显示空态，失败时显示失败原因', async () => {
  api.listMyPayroll.mockResolvedValueOnce({ count: 0, items: [] })
  api.listMyPayroll.mockRejectedValueOnce(new Error('读取失败'))
  const wrapper = mount(V2Settlement)
  await flushPromises()
  expect(wrapper.text()).toContain('暂无结算打款记录')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toBe('读取失败')
  wrapper.unmount()
})
