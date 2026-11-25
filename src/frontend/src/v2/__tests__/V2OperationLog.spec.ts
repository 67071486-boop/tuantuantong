// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { fetchAuditLogs, type AuditLogRow } from '@/api/auditLogs'
import V2OperationLog from '../V2OperationLog.vue'

vi.mock('@/api/auditLogs', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/auditLogs')>(),
  fetchAuditActors: vi.fn(async () => []),
  fetchAuditLogs: vi.fn(),
}))

vi.mock('@/api/config', () => ({
  fetchGames: vi.fn(async () => []),
}))

it('在 v2 系统操作日志中可以打开并看到完整删除原因', async () => {
  const reason = '测试删除原因'.repeat(33) + '末尾'
  const row: AuditLogRow = {
    target_id: '1',
    log_type: 10,
    order_no: '20261113000000000001',
    source_order_no: '',
    game_role_name: '',
    game_name: '',
    summary: '删除订单（原因已截断）',
    actor: '测试客服',
    created_at: '2026-11-13T09:00:00+08:00',
    action: 'order.delete',
    delete_reason: reason,
  }
  vi.mocked(fetchAuditLogs).mockResolvedValue({
    items: [
      row,
      { ...row, target_id: '2', action: 'order.transition', summary: '更改订单状态', delete_reason: '' },
    ],
    page: 1,
    page_size: 30,
    total: 2,
  })
  const wrapper = mount(V2OperationLog, { attachTo: document.body })
  try {
    await flushPromises()
    expect(fetchAuditLogs).toHaveBeenCalledWith(expect.objectContaining({ include_delete_reason: true }))
    const transition = wrapper.get('[data-v2-row="1"]')
    expect(transition.text()).toContain('更改订单状态')
    expect(transition.find('button').exists()).toBe(false)
    const button = wrapper.get('[data-v2-row="0"] button')
    expect(button.text()).toBe('查看原因')
    await button.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"] .lg-reason-text').text()).toBe(reason)
  } finally {
    wrapper.unmount()
  }
})
