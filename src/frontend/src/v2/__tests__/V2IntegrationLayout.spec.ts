// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import V2QqbotSettings from '../V2QqbotSettings.vue'
import V2WecomGroupQr from '../V2WecomGroupQr.vue'
import { updateQQBotBinding } from '@/api/qqbot'

vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ isAccountAdmin: true, canNode: () => true }) }))
vi.mock('@/api/qqbot', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/qqbot')>()),
  getQQBotBinding: vi.fn().mockResolvedValue({ app_id: '', app_secret_set: false, auth_verified: false, enabled: false }),
  updateQQBotBinding: vi.fn(),
}))
vi.mock('@/api/wecomGroupQr', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/wecomGroupQr')>()),
  getWecomQrApp: vi.fn().mockResolvedValue({ app_key: 'test', app_secret: 'test', state: 'enabled', state_text: '启用', callback_url: '', callback_state: 'disabled', ip_allowlist: '' }),
}))
vi.mock('@/api/orderQrFetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/orderQrFetch')>()),
  getOrderQrConfig: vi.fn().mockRejectedValue(new Error('未配置')),
}))

describe('对接页面简化', () => {
  it('群码应用保留接入配置，移除能力和调用示例面板', async () => {
    const wrapper = mount(V2WecomGroupQr)
    await flushPromises()
    expect(wrapper.find('[data-v2="app-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2="capability-top"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('这把密钥能做的事')
    expect(wrapper.find('[data-v2="capability-panel"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="ticket-example"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('QQ 密钥标签与输入框同组，保留留空不修改提示', async () => {
    const wrapper = mount(V2QqbotSettings)
    await flushPromises()
    const field = wrapper.get('[data-v2="app-secret"]').element.closest('.qb-credential-field')
    expect(field?.classList.contains('qb-credential-field')).toBe(true)
    expect(field?.querySelector('.cv2-field-label')?.textContent).toBe('AppSecret')
    expect(wrapper.get('[data-v2="app-secret"]').attributes('placeholder')).toBe('留空表示不修改')
    expect(wrapper.find('[data-v2="cred-warn"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('修改密钥保存后提示重启，仅保存原有配置时不提示', async () => {
    vi.mocked(updateQQBotBinding).mockResolvedValue({ app_id: '', app_secret_set: true, auth_verified: false, enabled: false } as never)
    const wrapper = mount(V2QqbotSettings)
    await flushPromises()
    await wrapper.get('[data-v2="binding-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="binding-result"]').text()).not.toContain('重启')
    const secret = wrapper.get('[data-v2="app-secret"]')
    await secret.setValue('new-secret')
    await wrapper.get('[data-v2="binding-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-v2="binding-result"]').text()).toContain('重启 QQ 机器人服务')
    wrapper.unmount()
  })

})
