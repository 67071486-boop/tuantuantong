// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import V2UpstreamPriceLog from '../V2UpstreamPriceLog.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: () => true }) }))

it('提示订单号和来源单号，查询沿用关键词并与刷新同行显示统计', async () => {
  request.mockResolvedValue({ items: [], total: 3, page: 1, page_size: 50 })
  const wrapper = mount(V2UpstreamPriceLog)
  try {
    await flushPromises()
    const keyword = wrapper.get('input[placeholder="订单号 / 来源单号"]')
    await keyword.setValue('SOURCE-123')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ params: expect.objectContaining({ key: 'SOURCE-123', page: 1 }) }))
    const toolbar = wrapper.get('[data-v2="toolbar"]')
    expect(toolbar.find('[data-v2="refresh"]').exists()).toBe(true)
    expect(toolbar.get('[data-v2="total"]').text()).toBe('本次搜索统计共3条记录')
  } finally {
    wrapper.unmount()
  }
})
