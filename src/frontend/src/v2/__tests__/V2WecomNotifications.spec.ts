// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getSettings = vi.fn()
const saveSettings = vi.fn()
const listRecords = vi.fn()
vi.mock('@/api/wecomNotifications', () => ({
  getWecomNotificationSettings: (...args: unknown[]) => getSettings(...args),
  saveWecomNotificationSettings: (...args: unknown[]) => saveSettings(...args),
  listWecomNotificationRecords: (...args: unknown[]) => listRecords(...args),
}))
import V2WecomNotifications from '../V2WecomNotifications.vue'

const defaults = { group_sync_enabled: true, group_qr_enabled: true, external_pay_enabled: true, payment_mismatch_enabled: false, order_status_enabled: false }
beforeEach(() => {
  getSettings.mockReset().mockResolvedValue({ ...defaults })
  saveSettings.mockReset().mockImplementation((value: unknown) => Promise.resolve(value))
  listRecords.mockReset().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
})

describe('企微功能设置', () => {
  it('展示五个开关并将用户选择统一保存', async () => {
    const wrapper = mount(V2WecomNotifications)
    await flushPromises()
    expect((wrapper.find('[data-v2="feature-group-sync"]').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.find('[data-v2="notify-payment"]').element as HTMLInputElement).checked).toBe(false)
    await wrapper.find('[data-v2="feature-group-qr"]').setValue(false)
    await wrapper.find('[data-v2="notify-payment"]').setValue(true)
    await wrapper.find('[data-v2="notification-save"]').trigger('click')
    await flushPromises()
    expect(saveSettings).toHaveBeenCalledWith({ ...defaults, group_qr_enabled: false, payment_mismatch_enabled: true })
    expect(wrapper.find('[data-v2="notification-settings-message"]').text()).toContain('已保存')
    expect(wrapper.text()).toContain('到期清理')
  })

  it('设置读取失败时禁止保存，重新加载可恢复', async () => {
    getSettings.mockRejectedValueOnce(new Error('无权读取'))
    const wrapper = mount(V2WecomNotifications)
    await flushPromises()
    expect(wrapper.find('[data-v2="notification-settings-message"]').text()).toContain('无权读取')
    expect((wrapper.find('[data-v2="notification-save"]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('[data-v2="notification-refresh"]').trigger('click')
    await flushPromises()
    expect((wrapper.find('[data-v2="notification-save"]').element as HTMLButtonElement).disabled).toBe(false)
  })

  it('显示未发送原因并能分页查看', async () => {
    listRecords.mockResolvedValueOnce({ items: [{ id: 1, kind: 'order_status', order_no: 'order-synthetic', detail: '问题单', status: 'failed', reason: '部分人员发送失败', recipients: [{ userid: 'person-a', status: 'sent' }, { userid: 'person-b', status: 'failed' }], created_at: null }], total: 2, page: 1, page_size: 1 })
      .mockResolvedValueOnce({ items: [{ id: 2, kind: 'payment_mismatch', order_no: 'order-two', detail: '', status: 'pending', reason: '', created_at: null }], total: 2, page: 2, page_size: 1 })
    const wrapper = mount(V2WecomNotifications)
    await flushPromises()
    expect(wrapper.find('[data-v2-notification-row="1"]').text()).toContain('部分人员发送失败')
    expect(wrapper.find('[data-v2-notification-row="1"]').text()).toContain('未全部发送')
    expect(wrapper.find('[data-v2-notification-row="1"]').text()).toContain('person-b：未发送')
    await wrapper.find('[data-v2="notification-next"]').trigger('click')
    await flushPromises()
    expect(listRecords).toHaveBeenLastCalledWith(2)
    expect(wrapper.find('[data-v2-notification-row="2"]').text()).toContain('待发送')
  })

  it('销售客服无人匹配时显示原因', async () => {
    listRecords.mockResolvedValueOnce({ items: [{ id: 3, kind: 'order_status', order_no: 'order-three', detail: '已暂停', status: 'failed', reason: '未找到销售客服', recipients: [], created_at: null }], total: 1, page: 1, page_size: 20 })
    const wrapper = mount(V2WecomNotifications)
    await flushPromises()
    expect(wrapper.find('[data-v2-notification-row="3"]').text()).toContain('未找到销售客服')
  })

  it('通知记录的 UTC 时间按北京时间显示', async () => {
    listRecords.mockResolvedValueOnce({ items: [{ id: 4, kind: 'order_status', order_no: 'order-four', detail: '', status: 'sent', reason: '', created_at: '2026-11-14T02:15:00Z' }], total: 1, page: 1, page_size: 20 })
    const wrapper = mount(V2WecomNotifications)
    await flushPromises()
    expect(wrapper.find('[data-v2-notification-row="4"]').text()).toContain('2026-11-14 10:15:00')
  })
})
