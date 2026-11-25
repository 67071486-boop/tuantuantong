// @vitest-environment jsdom
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import V2Messages from '../V2Messages.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

enableAutoUnmount(afterEach)
beforeEach(() => vi.resetAllMocks())
afterEach(() => vi.useRealTimers())

const baseUrl = '/api/auth/wxpusher/staff'
const defaults = { enabled: false, events: { approval_assigned: true, refund_assigned: true } }

it('从替换后的消息通知页设置员工提醒，并只保存改过的偏好', async () => {
  vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (url === `${baseUrl}/guide`) return { binding_enabled: false, bound: false, pending_binding: null }
    if (url === `${baseUrl}/preferences`) {
      return method === 'PATCH' ? { enabled: true, events: { ...defaults.events, refund_assigned: false } } : defaults
    }
    throw new Error(`unexpected ${url}`)
  })
  const wrapper = mount(V2Messages)
  await flushPromises()

  expect(wrapper.get('h1').text()).toBe('消息通知')
  expect(wrapper.text()).toContain('下载 WxPusher')
  expect(wrapper.text()).not.toContain('全部已读')
  expect(wrapper.get('#staff-notice-enabled').attributes('aria-checked')).toBe('false')
  await wrapper.get('#staff-notice-enabled').trigger('click')
  await wrapper.get('#staff-notice-refund_assigned').trigger('click')
  await wrapper.get('form').trigger('submit')
  await flushPromises()

  expect(request).toHaveBeenLastCalledWith({
    url: `${baseUrl}/preferences`, method: 'PATCH', data: { enabled: true, events: { refund_assigned: false } },
  })
  expect(wrapper.text()).toContain('已保存')
  expect(wrapper.get('#staff-notice-approval_assigned').attributes('aria-checked')).toBe('true')
})

it('群聊提醒在员工待办关闭时仍显示已开启', async () => {
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url === `${baseUrl}/guide`) return { binding_enabled: false, bound: false }
    return { ...defaults, group_chat_enabled: true }
  })
  const wrapper = mount(V2Messages)
  await flushPromises()

  expect(wrapper.text()).toContain('已开启')
  expect(wrapper.find('#staff-notice-group-chat').exists()).toBe(false)
  expect(wrapper.get('#staff-notice-enabled').attributes('aria-checked')).toBe('false')
})

it('员工扫码后自动确认、可发送测试消息并解绑自己的账号', async () => {
  vi.useFakeTimers()
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url === `${baseUrl}/guide`) return { binding_enabled: true, bound: false, pending_binding: null }
    if (url === `${baseUrl}/preferences`) return defaults
    if (url === `${baseUrl}/binding/start`) return {
      qr_url: 'https://wxpusher.zjiecode.com/api/qrcode/fictional.jpg',
      expires_at: new Date(Date.now() + 120_000).toISOString(),
    }
    if (url === `${baseUrl}/binding/progress`) return { bound: true }
    if (url === `${baseUrl}/binding/test`) return { accepted: true }
    if (url === `${baseUrl}/binding`) return { bound: false }
    throw new Error(`unexpected ${url}`)
  })
  const wrapper = mount(V2Messages)
  await flushPromises()

  await wrapper.get('button.cv2-btn--primary').trigger('click')
  await flushPromises()
  expect(wrapper.get('img').attributes('alt')).toBe('WxPusher 个人绑定二维码')
  await vi.advanceTimersByTimeAsync(10_000)
  await flushPromises()
  expect(wrapper.find('img').exists()).toBe(false)

  const clickButton = async (text: string) => {
    const button = wrapper.findAll('button').find((candidate) => candidate.text() === text)
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
  }
  await clickButton('发送测试消息')
  expect(wrapper.text()).toContain('WxPusher 已受理')
  await clickButton('解绑')
  await clickButton('确认解绑')
  expect(request).toHaveBeenLastCalledWith({ url: `${baseUrl}/binding`, method: 'DELETE' })
  expect(wrapper.text()).toContain('未绑定')
})
