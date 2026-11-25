// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const request = vi.fn()
vi.mock('@/api/http', () => ({ request: (...args: unknown[]) => request(...args) }))
vi.mock('@/api/tbSendLogs', () => ({ TB_SEND_LOGS_PATH: '/api/business/tb-send-logs/', retrySendLogs: vi.fn() }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: () => true }) }))
import V2OrderShipLog from '../V2OrderShipLog.vue'

beforeEach(() => {
  request.mockReset()
  request.mockImplementation(async ({ params }) => ({
    items: params.exclude_missing ? [{ id: 2, order_no: '系统单乙', tb_order_no: '淘宝单乙', send_params: '', store_name: '', result: 0, sent_at: '2026-11-13T10:00:00', operator: '系统', remark: '上游失败' }] : [
      { id: 1, order_no: '系统单甲', tb_order_no: '淘宝单甲', send_params: '', store_name: '', result: 1, sent_at: '2026-11-13T10:00:00', operator: '系统', remark: '' },
      { id: 2, order_no: '系统单乙', tb_order_no: '淘宝单乙', send_params: '', store_name: '', result: 0, sent_at: '2026-11-13T10:00:00', operator: '系统', remark: '上游失败' },
    ], total: params.exclude_missing ? 1 : 2, page: params.page, page_size: 50,
  }))
})

it('按钮与统计同行，状态带圈，失败面板使用独立过滤和分页', async () => {
  const wrapper = mount(V2OrderShipLog)
  try {
    await flushPromises()
    expect(wrapper.get('[data-v2-filter="key"]').attributes('placeholder')).toBe('系统单号或淘宝单号')
    expect(wrapper.get('[data-v2="toolbar"]').get('[data-v2="total"]').text()).toContain('2条')
    expect(wrapper.get('[data-v2-result="1"]').classes()).toContain('cv2-st--mark')
    expect(wrapper.get('[data-v2-result="0"]').classes()).toContain('cv2-st--error')
    expect(wrapper.get('[data-v2="failure-panel"]').text()).toContain('上游失败')
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ params: expect.objectContaining({ result: '0', exclude_missing: true, page: 1 }) }))
    await wrapper.get('[data-v2-filter="key"]').setValue('测试单')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ params: expect.objectContaining({ key: '测试单', result: '0', exclude_missing: true }) }))
    const mainCount = request.mock.calls.filter(([config]) => !config.params.exclude_missing).length
    wrapper.findAllComponents({ name: 'ElPagination' })[1]!.vm.$emit('current-change', 2)
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ params: expect.objectContaining({ page: 2, exclude_missing: true }) }))
    expect(request.mock.calls.filter(([config]) => !config.params.exclude_missing)).toHaveLength(mainCount)
  } finally { wrapper.unmount() }
})
