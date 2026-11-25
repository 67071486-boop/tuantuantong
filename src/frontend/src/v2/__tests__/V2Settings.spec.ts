// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionStore } from '@/stores/session'
import V2Settings from '../V2Settings.vue'

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), save: vi.fn() }))
vi.mock('@/api/systemSwitches', () => ({ fetchSystemSwitches: mocks.fetch, updateSystemSwitch: mocks.save }))

const rows = [
  { key: 'forbid_duplicate_source_order_no', value: 'N' },
  { key: 'reject_unpaid_xgj_order', value: 'Y' },
  { key: 'hall_simulated_orders', value: 'N' },
  { key: 'hall_preferred_orders', value: 'Y' },
  { key: 'auto_refund_platform_order', value: 'Y' },
  { key: 'WITHDRAW_AUTO_PAYOUT', value: 'Y' },
]
async function mountPage(edit = true) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = { id: 7, username: 'test_admin', display_name: '测试账号', identity_plane: 'tenant', is_tenant_admin: false, is_account_admin: false, roles: [], role: '', permissions: [], menu_permissions: edit ? ['system.settings', 'system.edit_settings'] : ['system.settings'], tenant_name: '示例' }
  session.status = 'ready'
  const wrapper = mount(V2Settings, { global: { plugins: [pinia] } })
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  mocks.fetch.mockReset().mockResolvedValue({ items: rows })
  mocks.save.mockReset().mockResolvedValue({})
})
describe('订单业务设置', () => {
  it('只列五项并回填服务器值，不加载后自动保存', async () => {
    const wrapper = await mountPage()
    expect(wrapper.findAll('[data-v2-setting]')).toHaveLength(5)
    expect(wrapper.text()).toContain('1. 禁止录入相同的来源单号')
    expect(wrapper.text()).toContain('4. 接单大厅优选订单')
    expect(wrapper.text()).toContain('5. 平台退款后自动退单')
    expect(wrapper.text()).not.toContain('提现')
    expect((wrapper.get('[data-v2-setting="reject_unpaid_xgj_order"] input[value="Y"]').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.get('[data-v2-setting="hall_simulated_orders"] input[value="N"]').element as HTMLInputElement).checked).toBe(true)
    expect(mocks.save).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('关闭平台退款自动退单时只保存该项', async () => {
    const wrapper = await mountPage()
    const row = wrapper.get('[data-v2-setting="auto_refund_platform_order"]')
    await row.get('input[value="N"]').setValue()
    await row.get('button').trigger('click')
    await flushPromises()
    expect(mocks.save).toHaveBeenCalledExactlyOnceWith('auto_refund_platform_order', 'N')
    wrapper.unmount()
  })
  it('只保存明确更改的一项，其他设置不写入', async () => {
    const wrapper = await mountPage()
    const row = wrapper.get('[data-v2-setting="hall_simulated_orders"]')
    await row.get('input[value="Y"]').setValue()
    await row.get('button').trigger('click')
    await flushPromises()
    expect(mocks.save).toHaveBeenCalledTimes(1)
    expect(mocks.save).toHaveBeenCalledWith('hall_simulated_orders', 'Y')
    expect(row.get('button').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('保存失败后显示重新读取到的真实值，查看权限不能修改', async () => {
    mocks.save.mockRejectedValueOnce(new Error('保存失败'))
    mocks.fetch.mockResolvedValueOnce({ items: rows }).mockResolvedValueOnce({ items: rows })
    const wrapper = await mountPage()
    const row = wrapper.get('[data-v2-setting="hall_preferred_orders"]')
    await row.get('input[value="N"]').setValue()
    await row.get('button').trigger('click')
    await flushPromises()
    expect((row.get('input[value="Y"]').element as HTMLInputElement).checked).toBe(true)
    expect((row.get('input[value="N"]').element as HTMLInputElement).checked).toBe(false)
    expect(mocks.fetch).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[role="status"]').text()).toBe('保存失败，已重新读取当前设置')
    expect(row.get('button').attributes('disabled')).toBeDefined()
    wrapper.unmount()
    const readonly = await mountPage(false)
    expect(readonly.findAll('[data-v2-setting] button')).toHaveLength(0)
    expect(readonly.findAll('[data-v2-setting] input').every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    readonly.unmount()
  })
  it('保存和重读均失败时持续显示未知，重新加载后才能修改', async () => {
    mocks.save.mockRejectedValueOnce(new Error('网络错误'))
    mocks.fetch.mockResolvedValueOnce({ items: rows }).mockRejectedValueOnce(new Error('网络错误'))
    const wrapper = await mountPage()
    vi.useFakeTimers()
    const row = wrapper.get('[data-v2-setting="hall_preferred_orders"]')
    const pendingRow = wrapper.get('[data-v2-setting="hall_simulated_orders"]')
    await pendingRow.get('input[value="Y"]').setValue()
    await row.get('input[value="N"]').setValue()
    await row.get('button').trigger('click')
    await flushPromises()
    expect(row.get('[role="status"]').text()).toContain('状态未知')
    expect(row.findAll('input').every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    expect(row.findAll('input').every((input) => !(input.element as HTMLInputElement).checked)).toBe(true)
    expect(row.get('button').attributes('disabled')).toBeDefined()
    vi.advanceTimersByTime(6100)
    await wrapper.vm.$nextTick()
    expect(row.get('[role="status"]').text()).toContain('状态未知')
    await wrapper.findAll('button').find((button) => button.text() === '重新加载')!.trigger('click')
    await flushPromises()
    expect(row.find('[role="status"]').exists()).toBe(false)
    expect((row.get('input[value="Y"]').element as HTMLInputElement).checked).toBe(true)
    expect((row.get('input[value="N"]').element as HTMLInputElement).disabled).toBe(false)
    expect((pendingRow.get('input[value="Y"]').element as HTMLInputElement).checked).toBe(true)
    expect(pendingRow.get('button').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
    vi.useRealTimers()
  })
  it('列表缺项时禁用修改并可重新加载，不写默认值', async () => {
    mocks.fetch.mockResolvedValueOnce({ items: rows.slice(0, 2) })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('订单设置不完整')
    expect(wrapper.findAll('[data-v2-setting] input').every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    expect(mocks.save).not.toHaveBeenCalled()
    await wrapper.get('button.cv2-btn:not([disabled])').trigger('click')
    await flushPromises()
    expect(mocks.fetch).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
