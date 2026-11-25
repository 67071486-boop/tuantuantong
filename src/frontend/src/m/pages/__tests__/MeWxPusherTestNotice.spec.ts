// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import MeWxPusherPage from '../MeWxPusherPage.vue'

vi.mock('../../router/useWorkerBack', () => ({ useWorkerBack: () => vi.fn() }))
vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('../../components/WxPusherPreferences.vue', () => ({ default: { template: '<div />' } }))
vi.mock('vue-router', () => ({ useRoute: () => ({ hash: '' }), useRouter: () => ({}) }))

enableAutoUnmount(afterEach)
installFramework7ForTest()

beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
})

describe('打手自助测试手机通知', () => {
  const sendButton = '[data-testid="wxpusher-send-test"]'

  it('仅已绑定的账号可以测试，且成功提示不把受理当成送达', async () => {
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false })
      .mockResolvedValueOnce({ binding_enabled: true, bound: true })
      .mockResolvedValueOnce({ accepted: true })
    const unbound = mount(MeWxPusherPage)
    await flushPromises()
    expect(unbound.find(sendButton).exists()).toBe(false)

    const bound = mount(MeWxPusherPage)
    await flushPromises()
    await bound.get(sendButton).trigger('click')
    await flushPromises()

    expect(request).toHaveBeenCalledWith({ url: '/api/auth/wxpusher/binding/test', method: 'POST' })
    expect(bound.text()).toContain('已交给 WxPusher，请在手机上查看')
  })

  it('发送结果不确定时显示错误，并允许稍后重试', async () => {
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: true })
      .mockRejectedValueOnce(new Error('暂时无法确认测试通知是否发送，请稍后再试。'))
      .mockResolvedValueOnce({ accepted: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await wrapper.get(sendButton).trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法确认')
    expect(wrapper.text()).not.toContain('已交给 WxPusher')
    await wrapper.get(sendButton).trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('已交给 WxPusher，请在手机上查看')
  })
})
