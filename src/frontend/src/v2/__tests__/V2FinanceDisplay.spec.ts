// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const { fetchSummary, fetchDaily, fetchAll, fetchSources, exportFile } = vi.hoisted(() => ({
  fetchSummary: vi.fn().mockResolvedValue({ items: [], totals: { shop_count: 0, count: 0, amount: '0.00', missing_count: 0 } }),
  fetchDaily: vi.fn().mockResolvedValue({ items: [{ date: '2026-11-13', order_count: 2, total_amount: '30.00' }], total: 1 }),
  fetchSources: vi.fn().mockResolvedValue({ items: [] }),
  exportFile: vi.fn().mockRejectedValue(new Error('测试导出')),
  fetchAll: vi.fn().mockResolvedValue([
    { id: 1, order_no: '20261113000000000001', source_order_no: '', username: 'test_a', emp_type: '打单工资', temp_amount: '10.00', settled_amount: '10.00', payable_net: '10.00', project: '', settled_at: null, settled_by: '' },
    { id: 2, order_no: '20261113000000000002', source_order_no: '', username: 'test_b', emp_type: '销售提成', temp_amount: '20.00', settled_amount: '20.00', payable_net: '20.00', project: '', settled_at: null, settled_by: '' },
  ]),
}))
vi.mock('@/api/xgjReceipts', () => ({ XGJ_RECEIPT_NODE: 'finance.xgj_receipt_detail', fetchReceiptSummary: fetchSummary, fetchReceiptList: vi.fn() }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: () => true }) }))
vi.mock('@/api/orderSources', () => ({ fetchOrderSourceGroups: vi.fn().mockResolvedValue({ items: [] }) }))
vi.mock('@/api/settlements', () => ({ fetchSettlementDailySummary: fetchDaily, fetchAllSettlements: fetchAll, fetchSettlementDailySources: fetchSources }))
vi.mock('@/api/performances', () => ({ downloadExport: exportFile }))
vi.mock('@/api/financeExportJobs', () => ({ queueFinanceExport: vi.fn().mockResolvedValue(null) }))
vi.mock('@/v2/charts', () => ({ buildDonutData: () => null, createDonut: vi.fn(), loadECharts: vi.fn() }))

import V2BillSummary from '../V2BillSummary.vue'
import V2XgjReceipts from '../V2XgjReceipts.vue'

describe('财务页面展示', () => {
  it('闲管家收款明细默认查询本地当天', async () => {
    const today = new Date()
    const expected = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
    const wrapper = mount(V2XgjReceipts)
    await flushPromises()
    expect(fetchSummary).toHaveBeenCalledWith({ date_from: expected, date_to: expected })
    expect((wrapper.get('[data-v2-filter="date-from"]').element as HTMLInputElement).value).toBe(expected)
    expect((wrapper.get('[data-v2-filter="date-to"]').element as HTMLInputElement).value).toBe(expected)
    wrapper.unmount()
  })

  it('结算记录按类型切换汇总、来源和明细，切换时清空旧明细', async () => {
    const wrapper = mount(V2BillSummary)
    await flushPromises()
    expect(fetchDaily).toHaveBeenLastCalledWith(expect.objectContaining({ emp_type: '打单工资', page: 1 }))
    await wrapper.get('[data-v2-view-detail="2026-11-13"]').trigger('click')
    await flushPromises()
    expect(fetchAll).toHaveBeenLastCalledWith(expect.objectContaining({ emp_type: '打单工资' }))
    await wrapper.get('[data-v2-settlement-type="销售提成"]').trigger('click')
    await flushPromises()
    expect(fetchDaily).toHaveBeenLastCalledWith(expect.objectContaining({ emp_type: '销售提成', page: 1 }))
    await wrapper.get('[data-v2-amount-toggle="2026-11-13"]').trigger('click')
    await flushPromises()
    expect(fetchSources).toHaveBeenLastCalledWith('2026-11-13', '销售提成')
    expect(wrapper.find('[data-v2-detail-row="1"]').exists()).toBe(false)
    await wrapper.get('[data-v2-view-detail="2026-11-13"]').trigger('click')
    await flushPromises()
    expect(fetchAll).toHaveBeenLastCalledWith(expect.objectContaining({ emp_type: '销售提成' }))
    await wrapper.get('[data-v2="export"]').trigger('click')
    await flushPromises()
    expect(exportFile).toHaveBeenLastCalledWith('/api/business/settlements/export', expect.objectContaining({ emp_type: '销售提成', date_from: '2026-11-13', date_to: '2026-11-13' }))
    await wrapper.get('[data-v2="page-size"]').setValue('50')
    await flushPromises()
    expect(fetchDaily).toHaveBeenLastCalledWith(expect.objectContaining({ emp_type: '销售提成', page: 1, page_size: 50 }))
    wrapper.unmount()
  })

  it('切换标签后丢弃迟到的明细，空列表显示暂无数据', async () => {
    let resolveDetail!: (rows: unknown[]) => void
    fetchAll.mockImplementationOnce(() => new Promise((resolve) => { resolveDetail = resolve }))
    const wrapper = mount(V2BillSummary)
    await flushPromises()
    await wrapper.get('[data-v2-view-detail="2026-11-13"]').trigger('click')
    fetchDaily.mockResolvedValueOnce({ items: [], total: 0 })
    await wrapper.get('[data-v2-settlement-type="销售提成"]').trigger('click')
    await flushPromises()
    resolveDetail([{ id: 99 }])
    await flushPromises()
    expect(wrapper.find('[data-v2-detail-row="99"]').exists()).toBe(false)
    expect(wrapper.get('[data-v2="empty"]').text()).toContain('暂无数据')
    wrapper.unmount()
  })

  it('查询或切换标签不会取消导出，并明确提示原日期和类型', async () => {
    let resolveExport!: (result: { blob: Blob; filename: string }) => void
    exportFile.mockImplementationOnce(() => new Promise((resolve) => { resolveExport = resolve }))
    const createUrl = vi.fn().mockReturnValue('blob:test')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createUrl })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mount(V2BillSummary)
    await flushPromises()
    await wrapper.get('[data-v2-view-detail="2026-11-13"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="export"]').trigger('click')
    await wrapper.get('[data-v2="detail-search"]').trigger('click')
    await wrapper.get('[data-v2-settlement-type="销售提成"]').trigger('click')
    resolveExport({ blob: new Blob(), filename: 'test.csv' })
    await flushPromises()
    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('已导出 2026-11-13 打单工资 结算记录')
    wrapper.unmount()
    click.mockRestore()
    Reflect.deleteProperty(URL, 'createObjectURL')
  })

  it('导出带上已查询的来源和订单号', async () => {
    fetchSources.mockResolvedValueOnce({ items: [{ source_id: 8, source_name: '店铺甲', order_count: 1, total_amount: '10.00' }] })
    const wrapper = mount(V2BillSummary)
    await flushPromises()
    await wrapper.get('[data-v2-amount-toggle="2026-11-13"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2-view-source="8"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="detail-order-no"]').setValue('订单甲')
    await wrapper.get('[data-v2="detail-search"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="export"]').trigger('click')
    await flushPromises()
    expect(exportFile).toHaveBeenLastCalledWith('/api/business/settlements/export', expect.objectContaining({ order_source: 8, order_no: '订单甲', emp_type: '打单工资' }))
    wrapper.unmount()
  })

  it('切换标签后导出失败提示仍标明发起时的日期和类型', async () => {
    let rejectExport!: (error: Error) => void
    exportFile.mockImplementationOnce(() => new Promise((_, reject) => { rejectExport = reject }))
    const wrapper = mount(V2BillSummary)
    await flushPromises()
    await wrapper.get('[data-v2-view-detail="2026-11-13"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="export"]').trigger('click')
    await wrapper.get('[data-v2-settlement-type="销售提成"]').trigger('click')
    rejectExport(new Error('网络异常'))
    await flushPromises()
    expect(wrapper.text()).toContain('导出 2026-11-13 打单工资 失败：网络异常')
    wrapper.unmount()
  })

})
