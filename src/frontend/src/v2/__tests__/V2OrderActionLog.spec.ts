// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { ElPagination } from 'element-plus'
import { BizError } from '@/api/errors'
import type { OrderTimeline } from '@/api/auditTimeline'
import { createMemoryHistory, createRouter } from 'vue-router'
import V2OrderActionLog from '../V2OrderActionLog.vue'

const request = vi.hoisted(() => vi.fn())
const fetchOrder = vi.hoisted(() => vi.fn())
const fetchOrderAudit = vi.hoisted(() => vi.fn())
const fetchOrderTimeline = vi.hoisted(() => vi.fn<(key: string) => Promise<OrderTimeline>>())
const canNode = vi.hoisted(() => vi.fn(() => true))
vi.mock('@/api/http', () => ({ request }))
vi.mock('@/api/auditTimeline', () => ({ fetchOrderTimeline }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode }) }))
vi.mock('@/api/orders', async (original) => ({
  ...await original<typeof import('@/api/orders')>(), fetchOrder, fetchOrderAudit,
}))

async function makeRouter(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/v2/order-action-log', name: 'v2-order-action-log', component: V2OrderActionLog }],
  })
  await router.push('/v2/order-action-log' + query)
  return router
}

it('交易单号打开系统订单详情，统计独立翻页', async () => {
  request.mockReset()
  canNode.mockReturnValue(true)
  const orderNo = '20261113000000000001'
  request.mockResolvedValue({
    items: [{ order_no: orderNo, source_order_no: 'TRADE-123', game_name: '测试游戏', from_value: '', to_value: '', action_label: '修改', actor: 'a', actor_display: '甲', created_at: '2026-11-13T10:00:00Z' }],
    total: 1, page: 1, summary: Array.from({ length: 11 }, (_, i) => ({ action: `a${i}`, action_label: `动作${i}`, actor: 'a', actor_display: '甲', count: i + 1 })),
  })
  fetchOrder.mockResolvedValue({ order_no: orderNo, status: 'COMPLETED' })
  fetchOrderAudit.mockResolvedValue([])
  const router = await makeRouter()
  const wrapper = mount(V2OrderActionLog, { attachTo: document.body, global: { plugins: [router] } })
  try {
    await flushPromises()
    expect(wrapper.find('input[placeholder="系统单号或交易单号"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-v2-summary]')).toHaveLength(10)
    await wrapper.get('[data-v2="summary-pager"] button:last-child').trigger('click')
    expect(wrapper.findAll('[data-v2-summary]')).toHaveLength(1)
    expect(wrapper.text()).toContain('动作10')
    expect(request).toHaveBeenCalledTimes(1)
    wrapper.getComponent(ElPagination).vm.$emit('current-change', 2)
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ params: expect.objectContaining({ page: 2 }) }))
    expect(wrapper.text()).toContain('动作10')
    expect(wrapper.get('[data-v2="summary-pager"]').text()).toContain('2 / 2')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(3)
    expect(wrapper.findAll('[data-v2-summary]')).toHaveLength(10)
    expect(wrapper.get('[data-v2="summary-pager"]').text()).toContain('1 / 2')
    await wrapper.get(`[data-v2-source-detail="${orderNo}"]`).trigger('click')
    await flushPromises()
    expect(fetchOrder).toHaveBeenCalledWith(orderNo)
    expect(fetchOrderAudit).toHaveBeenCalledWith(orderNo)
  } finally { wrapper.unmount() }
})

it('无权限时不加载操作记录', async () => {
  request.mockReset()
  canNode.mockReturnValue(false)
  fetchOrderTimeline.mockClear()
  const router = await makeRouter('?order_no=20261113000000000001')
  const wrapper = mount(V2OrderActionLog, { global: { plugins: [router] } })
  try {
    await flushPromises()
    expect(request).not.toHaveBeenCalled()
    expect(fetchOrderTimeline).not.toHaveBeenCalled()
    expect(wrapper.find('[data-v2="summary-pager"]').exists()).toBe(false)
  } finally { wrapper.unmount(); canNode.mockReturnValue(true) }
})

it('按时间线传来的单号首屏取数，重置后恢复默认查询', async () => {
  request.mockReset()
  request.mockResolvedValue({ items: [], total: 0, page: 1, summary: [] })
  const firstOrderNo = '20261113000000000001'
  const secondOrderNo = '20251001000000000001'
  fetchOrderTimeline.mockReset().mockImplementation(async (key) => timelineFor(key, 'SOURCE-EX-1'))
  const router = await makeRouter(`?order_no=${firstOrderNo}`)
  const wrapper = mount(V2OrderActionLog, { global: { plugins: [router] } })
  try {
    await flushPromises()
    const search = wrapper.get<HTMLInputElement>('input[placeholder="系统单号或交易单号"]')
    expect(search.element.value).toBe(firstOrderNo)
    expect(wrapper.get('[data-v2="order-no"]').text()).toBe(firstOrderNo)
    expect(request.mock.lastCall?.[0].params).toEqual({ key1: firstOrderNo, page: 1, page_size: 50 })

    await router.push({ query: { order_no: secondOrderNo } })
    await flushPromises()
    expect(search.element.value).toBe(secondOrderNo)
    expect(wrapper.get('[data-v2="order-no"]').text()).toBe(secondOrderNo)
    expect(request.mock.lastCall?.[0].params).toEqual({ key1: secondOrderNo, page: 1, page_size: 50 })

    await wrapper.get('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(search.element.value).toBe('')
    expect(wrapper.find('[data-v2="order-card"]').exists()).toBe(false)
    expect(request.mock.lastCall?.[0].params).toEqual({
      page: 1, page_size: 50, stime: expect.any(String), etime: expect.any(String),
    })
  } finally {
    wrapper.unmount()
  }
})

function timelineFor(orderNo: string, sourceOrderNo: string): OrderTimeline {
  return {
    order: {
      order_no: orderNo, source_order_no: sourceOrderNo, status: 'IN_PROGRESS', status_label: '代练中',
      game_name: '', project_text: '', is_deleted: false, deleted_at: null, created_at: null,
      updated_at: null, taker_display: null,
    },
    related: { peer_links: [], split_group: null },
    bindings_health: [],
    sections: [{
      key: 'operation_log', label: '操作留痕', truncated: false,
      items: [{
        at: '2026-11-13T10:00:00Z', kind: 'order.transition', kind_label: '改订单状态',
        status: '', summary: '待抢单改为代练中',
      }],
    }],
  }
}

async function mountTimelineSearch(query = '') {
  request.mockReset()
  request.mockResolvedValue({ items: [], total: 0, page: 1, summary: [] })
  fetchOrderTimeline.mockReset()
  const router = await makeRouter(query)
  const wrapper = mount(V2OrderActionLog, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

it('提交来源单号后在本页显示时间线，日志翻页沿用同一条时间线', async () => {
  const wrapper = await mountTimelineSearch()
  try {
    const search = wrapper.get<HTMLInputElement>('input[placeholder="系统单号或交易单号"]')
    await search.setValue('  SOURCE-EX-1  ')
    expect(fetchOrderTimeline).not.toHaveBeenCalled()
    fetchOrderTimeline.mockResolvedValue(timelineFor('20261113000000000001', 'SOURCE-EX-1'))
    await search.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(fetchOrderTimeline).toHaveBeenCalledWith('SOURCE-EX-1')
    const panel = wrapper.get('[data-v2="searched-order-timeline"]')
    expect(panel.text()).toContain('订单时间线')
    expect(panel.get('[data-v2="order-no"]').text()).toBe('20261113000000000001')
    expect(panel.get('[data-v2="source-order-no"]').text()).toBe('SOURCE-EX-1')
    expect(panel.get('[data-v2-section="operation_log"]').text()).toContain('待抢单改为代练中')
    expect(wrapper.find('[aria-label="操作明细"]').exists()).toBe(true)

    wrapper.getComponent(ElPagination).vm.$emit('current-change', 2)
    await flushPromises()
    expect(fetchOrderTimeline).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="searched-order-timeline"]').exists()).toBe(true)
  } finally { wrapper.unmount() }
})

it('较慢的上一次查询不会覆盖后发的时间线，重置能清空结果', async () => {
  const wrapper = await mountTimelineSearch()
  try {
    let finishFirst!: (value: OrderTimeline) => void
    fetchOrderTimeline.mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve }))
    const search = wrapper.get<HTMLInputElement>('input[placeholder="系统单号或交易单号"]')
    await search.setValue('SOURCE-EX-1')
    await wrapper.get('[data-v2="search"]').trigger('click')
    expect(wrapper.find('[data-v2="timeline-loading"]').exists()).toBe(true)

    fetchOrderTimeline.mockResolvedValueOnce(timelineFor('20261113000000000002', 'SOURCE-EX-2'))
    await search.setValue('SOURCE-EX-2')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    finishFirst(timelineFor('20261113000000000001', 'SOURCE-EX-1'))
    await flushPromises()
    expect(wrapper.get('[data-v2="source-order-no"]').text()).toBe('SOURCE-EX-2')

    await wrapper.get('[data-v2="reset"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="searched-order-timeline"]').exists()).toBe(false)
    expect(fetchOrderTimeline).toHaveBeenCalledTimes(2)
  } finally { wrapper.unmount() }
})

it('未匹配时间线时提示核对完整单号，仍显示关键词过滤后的操作表', async () => {
  const wrapper = await mountTimelineSearch()
  try {
    const search = wrapper.get<HTMLInputElement>('input[placeholder="系统单号或交易单号"]')
    fetchOrderTimeline.mockRejectedValueOnce(new BizError(1002, '未找到匹配的订单'))
    await search.setValue('EX-')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="timeline-error"]').text()).toContain('完整单号')
    expect(wrapper.find('[data-v2="order-card"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="操作明细"]').exists()).toBe(true)
    expect(request.mock.lastCall?.[0].params.key1).toBe('EX-')

    fetchOrderTimeline.mockRejectedValueOnce(new Error('Connection unavailable'))
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="timeline-error"]').text()).toContain('查询失败')
  } finally { wrapper.unmount() }
})
