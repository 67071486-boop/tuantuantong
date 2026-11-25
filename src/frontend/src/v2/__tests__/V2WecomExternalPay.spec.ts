// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getStatus = vi.fn()
const getIssues = vi.fn()
const getInvalid = vi.fn()
vi.mock('@/api/wecomExternalPay', () => ({
  getExternalPayStatus: (...args: unknown[]) => getStatus(...args),
  listExternalPayIssues: (...args: unknown[]) => getIssues(...args),
  listExternalPayInvalid: (...args: unknown[]) => getInvalid(...args),
}))
import V2WecomExternalPay from '../V2WecomExternalPay.vue'
import { HttpError } from '@/api/errors'

beforeEach(() => {
  getStatus.mockReset().mockResolvedValue({
    configured: true, enabled: true, last_run_at: null, last_success_at: null, last_error: null,
  })
  getIssues.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 2 })
  getInvalid.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 2 })
})

describe('企微对外收款同步', () => {
  it('显示授权错误及不同的待处理原因和候选金额，分页仅查询记录', async () => {
    getStatus.mockResolvedValue({
      configured: true, enabled: true, last_run_at: null, last_success_at: null,
      last_error: '企微错误 48002：应用未授权',
    })
    getIssues.mockResolvedValueOnce({
      items: [
        { id: 1, bill_type: 0, transaction_id: 'tx-synthetic-1', payment_amount: '80.00', pay_time: null,
          reason: '金额不一致', candidate_count: 1,
          candidates: [{ order_no: 'order-1', order_amount: '100.00' }] },
        { id: 2, bill_type: 0, transaction_id: 'tx-synthetic-2', payment_amount: '50.00', pay_time: null,
          reason: '匹配到多个候选订单', candidate_count: 2,
          candidates: [{ order_no: 'order-2', order_amount: '50.00' }, { order_no: 'order-3', order_amount: '50.00' }] },
      ], total: 3, page: 1, page_size: 2,
    }).mockResolvedValueOnce({
      items: [{ id: 3, bill_type: 0, transaction_id: 'tx-synthetic-3', payment_amount: '10.00', pay_time: null,
        reason: '金额不一致', candidate_count: 1, candidates: [{ order_no: 'order-4', order_amount: '20.00' }] }],
      total: 3, page: 2, page_size: 2,
    })
    const wrapper = mount(V2WecomExternalPay)
    await flushPromises()
    expect(wrapper.find('[data-v2="external-pay-last-error"]').text()).toContain('48002')
    expect(wrapper.find('[data-v2-issue-row="1"]').text()).toContain('80.00')
    expect(wrapper.find('[data-v2-issue-row="1"]').text()).toContain('order-1（100.00 元）')
    expect(wrapper.find('[data-v2-issue-row="2"]').text()).toContain('匹配到多个候选订单')
    expect(wrapper.find('[data-v2-issue-row="2"]').text()).toContain('共 2 张候选订单')
    await wrapper.find('[data-v2="external-pay-next"]').trigger('click')
    await flushPromises()
    expect(getIssues).toHaveBeenLastCalledWith(2)
    expect(wrapper.find('[data-v2-issue-row="3"]').text()).toContain('order-4')
    expect((wrapper.find('[data-v2="external-pay-next"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('状态或列表请求失败时显示错误，刷新后可恢复', async () => {
    getStatus.mockRejectedValueOnce(new HttpError(403, '没有权限查看企微设置'))
    getIssues.mockRejectedValueOnce(new HttpError(403, '没有权限查看企微设置'))
    const wrapper = mount(V2WecomExternalPay)
    await flushPromises()
    expect(wrapper.find('[data-v2="external-pay-status-error"]').text()).toContain('没有权限查看企微设置')
    expect(wrapper.find('[data-v2="external-pay-issues-error"]').text()).toContain('没有权限查看企微设置')
    await wrapper.find('[data-v2="external-pay-refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="external-pay-status"]').text()).toContain('已启用')
    expect(wrapper.find('[data-v2="external-pay-issues-empty"]').text()).toContain('暂无待处理记录')
  })

  it('显示历史退款追溯的进度和需要人工核对的原因', async () => {
    getIssues.mockResolvedValueOnce({
      items: [
        { id: 10, bill_type: 1, transaction_id: 'refund-tx-1', payment_amount: '10.00', pay_time: null,
          reason: '正在追溯原付款', candidate_count: 0, candidates: [] },
        { id: 11, bill_type: 1, transaction_id: 'refund-tx-2', payment_amount: '12.00', pay_time: null,
          reason: '未找到原付款，请人工核对', candidate_count: 0, candidates: [] },
      ], total: 2, page: 1, page_size: 20,
    })
    const wrapper = mount(V2WecomExternalPay)
    await flushPromises()
    expect(wrapper.find('[data-v2-issue-row="10"]').text()).toContain('正在追溯原付款')
    expect(wrapper.find('[data-v2-issue-row="10"]').text()).toContain('退款')
    expect(wrapper.find('[data-v2-issue-row="11"]').text()).toContain('未找到原付款，请人工核对')
  })

  it('显示异常账单重查状态并独立翻页', async () => {
    getInvalid.mockResolvedValueOnce({
      items: [{ id: 1, reason: '账单金额不合法', window_start: '2026-01-01T00:00:00Z',
        window_end: '2026-01-02T00:00:00Z', last_retry_at: null, identifiable: true }],
      total: 2, page: 1, page_size: 1,
    }).mockResolvedValueOnce({
      items: [{ id: 2, reason: '交易单号不合法', window_start: null,
        window_end: null, last_retry_at: null, identifiable: false }],
      total: 2, page: 2, page_size: 1,
    })
    const wrapper = mount(V2WecomExternalPay)
    await flushPromises()
    expect(wrapper.find('[data-v2-invalid-row="1"]').text()).toContain('账单金额不合法')
    expect(wrapper.find('[data-v2-invalid-row="1"]').text()).toContain('系统自动重查')
    await wrapper.find('[data-v2="external-pay-invalid-next"]').trigger('click')
    await flushPromises()
    expect(getInvalid).toHaveBeenLastCalledWith(2)
    expect(wrapper.find('[data-v2-invalid-row="2"]').text()).toContain('请人工核对')
    expect(getIssues).toHaveBeenCalledTimes(1)
  })

  it('异常账单读取被拒绝时显示错误，不显示空列表', async () => {
    getInvalid.mockRejectedValueOnce(new HttpError(403, '没有权限'))
    const wrapper = mount(V2WecomExternalPay)
    await flushPromises()
    expect(wrapper.find('[data-v2="external-pay-invalid-error"]').text()).toContain('没有权限')
    expect(wrapper.find('[data-v2="external-pay-invalid-empty"]').exists()).toBe(false)
  })
})
