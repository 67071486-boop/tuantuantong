// @vitest-environment jsdom
import { flushPromises, shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const fetchGroups = vi.fn().mockResolvedValue({
  items: [{
    order_no: '20261113000000000001', source_order_no: '', order_status: 'IN_PROGRESS',
    game: '测试游戏', server: '一区', pushes: [{
      platform: 10, serial_no: 'P123', state: 'success', remote_status: 1,
      remote_cancel_status: null, price: '10.00', accept_price: '25.00',
    }],
  }],
  total: 1,
})
const fetchCounts = vi.fn().mockResolvedValue({ total: 1, counts: {} })
vi.mock('@/api/dailiantongPush', async (original) => ({
  ...(await original<typeof import('@/api/dailiantongPush')>()),
  fetchPlatformPushGroups: (...args: unknown[]) => fetchGroups(...args),
  fetchPlatformPushCounts: (...args: unknown[]) => fetchCounts(...args),
}))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: () => true }) }))

import V2PlatformOrders from '../V2PlatformOrders.vue'

describe('第三方平台订单工具条与金额', () => {
  it('保留批量操作并隐藏指定入口，刷新仍重新读取列表', async () => {
    const wrapper = shallowMount(V2PlatformOrders)
    await flushPromises()
    const actions = wrapper.findAll('[data-v2-action]').map((button) => button.attributes('data-v2-action'))
    expect(actions).toEqual(['withdraw', 'accept', 'arbitrate', 'sync'])
    expect(wrapper.get('[data-v2="refresh"]').classes()).toContain('cv2-btn--primary')
    expect(wrapper.find('[data-v2="toolbar-strip"] [data-v2="refresh"]').exists()).toBe(false)
    const before = fetchGroups.mock.calls.length
    await wrapper.get('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(fetchGroups.mock.calls.length).toBeGreaterThan(before)
    wrapper.unmount()
  })

  it('订单号沿用平台订单颜色，三个金额显示统一样式', async () => {
    const wrapper = shallowMount(V2PlatformOrders)
    await flushPromises()
    const row = wrapper.get('[data-v2-row="20261113000000000001"]')
    expect(row.get('[data-v2-detail]').classes()).toContain('cv2-platform-order-link')
    expect(row.findAll('.cv2-money').map((value) => value.text())).toEqual(['10.00', '25.00', '15.00'])
    wrapper.unmount()
  })
})
