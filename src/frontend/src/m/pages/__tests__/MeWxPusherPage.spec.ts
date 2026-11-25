// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import { openWxPusherSubscription } from '../../utils/wxpusherSubscription'
import MeWxPusherPage from '../MeWxPusherPage.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('../../components/WxPusherPreferences.vue', () => ({ default: { template: '<div><slot name="entries" /></div>' } }))
vi.mock('../../utils/wxpusherSubscription', () => ({ openWxPusherSubscription: vi.fn() }))
const { back, push, replace, state } = vi.hoisted(() => ({
  back: vi.fn(), push: vi.fn(), replace: vi.fn(), state: { back: null as string | null },
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ hash: '' }),
  useRouter: () => ({ back, push, replace, options: { history: { state } }, resolve: () => ({ path: '/me', meta: {} }) }),
}))

enableAutoUnmount(afterEach)

const subscribeUrl = 'https://wxpusher.zjiecode.com/app/#/follow?type=1&id=900000001'
const personalSubscribeUrl = 'https://wxpusher.zjiecode.com/api/s/review-link'
const qrUrl = 'https://wxpusher.zjiecode.com/api/qrcode/TestGuideQrOnly.jpg'

function linkAttempt(attemptId = 'test-link-attempt', url = personalSubscribeUrl) {
  return { mode: 'link', attempt_id: attemptId, subscribe_url: url, expires_at: new Date(Date.now() + 300_000).toISOString() }
}

installFramework7ForTest()

beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
  state.back = null
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('打手消息通知引导', () => {
  it('首绑使用个人订阅链接，不展示下载区或公共订阅入口', async () => {
    vi.mocked(request).mockResolvedValue({ subscribe_url: subscribeUrl, qr_url: qrUrl, binding_enabled: true, bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    expect(request).toHaveBeenCalledWith({ url: '/api/auth/wxpusher/guide', method: 'GET' })
    expect(wrapper.find('[data-testid="download-wxpusher-android"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="download-wxpusher-ios"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('下载 WxPusher')
    expect(wrapper.find(`img[src="${qrUrl}"]`).exists()).toBe(false)
    expect(wrapper.find(`a[href="${subscribeUrl}"]`).exists()).toBe(false)
    expect(wrapper.get('[data-testid="wxpusher-bind"]').text()).toBe('获取订阅链接')
    await wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
    expect(replace).toHaveBeenCalledWith({ name: 'm-me' })
    expect(back).not.toHaveBeenCalled()
  })

  it('点击获取订阅链接，生成后直接在当前页打开，订阅后自动完成绑定', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: null })
      .mockResolvedValueOnce(linkAttempt())
      .mockResolvedValueOnce({ bound: false })
      .mockResolvedValueOnce({ bound: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    await wrapper.get('[data-testid="wxpusher-bind"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/start', method: 'POST', data: { mode: 'link' },
    })
    const link = wrapper.get('a[data-testid="wxpusher-subscribe-link"]')
    expect(openWxPusherSubscription).toHaveBeenCalledExactlyOnceWith(personalSubscribeUrl)
    expect(link.attributes('href')).toBe(personalSubscribeUrl)
    expect(link.attributes('target')).toBeUndefined()
    expect(wrapper.find('img[alt="WxPusher 个人绑定二维码"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('再点一次原链接')

    await vi.advanceTimersByTimeAsync(10_000)
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/progress', method: 'POST', timeout: 25_000,
      data: { mode: 'link', attempt_id: 'test-link-attempt' },
    })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('未绑定')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.find('a[data-testid="wxpusher-subscribe-link"]').exists()).toBe(false)
  })

  it('已绑定可直接发起换绑，直到新绑定确认成功前旧账号仍可接收通知', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: null })
      .mockResolvedValueOnce(linkAttempt())
      .mockResolvedValueOnce({ bound: false })
      .mockResolvedValueOnce({ bound: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    await wrapper.get('[data-testid="wxpusher-rebind"]').trigger('click')
    await flushPromises()
    const link = wrapper.get('a[data-testid="wxpusher-subscribe-link"]')
    expect(openWxPusherSubscription).toHaveBeenCalledExactlyOnceWith(personalSubscribeUrl)
    expect(link.attributes('href')).toBe(personalSubscribeUrl)
    expect(wrapper.text()).toContain('旧绑定仍可接收通知')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.find('[data-testid="wxpusher-send-test"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('已换绑 WxPusher')

    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.text()).toContain('已换绑 WxPusher')
    expect(wrapper.find('[data-testid="wxpusher-subscribe-link"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="wxpusher-rebind"]').exists()).toBe(true)
    expect(request).not.toHaveBeenCalledWith({ url: '/api/auth/wxpusher/binding', method: 'DELETE' })
  })

  it('换绑链接生成失败时仍保留旧绑定并提示重试', async () => {
    const guide = { binding_enabled: true, bound: true, pending_binding: null }
    vi.mocked(request)
      .mockResolvedValueOnce(guide)
      .mockRejectedValueOnce(new Error('暂时无法生成订阅链接，请重试。'))
      .mockResolvedValueOnce(guide)
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    await wrapper.get('[data-testid="wxpusher-rebind"]').trigger('click')
    await flushPromises()
    expect(openWxPusherSubscription).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法生成订阅链接')
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.find('[data-testid="wxpusher-send-test"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="wxpusher-rebind"]').attributes('disabled')).toBeUndefined()
  })

  it.each(['订单推送设置', '离开本页'])('生成链接时切到%s，迟到结果不跳转', async (destination) => {
    let finishStart!: (result: ReturnType<typeof linkAttempt>) => void
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: null })
      .mockReturnValueOnce(new Promise<ReturnType<typeof linkAttempt>>((resolve) => { finishStart = resolve }))
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    const button = wrapper.get('[data-testid="wxpusher-bind"]')
    await button.trigger('click')
    await button.trigger('click')
    expect(request).toHaveBeenCalledTimes(2)

    if (destination === '订单推送设置') await wrapper.setProps({ orderDetails: true })
    else wrapper.unmount()
    finishStart(linkAttempt())
    await flushPromises()
    expect(openWxPusherSubscription).not.toHaveBeenCalled()
  })

  it('恢复待换绑链接，可取消新尝试并保留已关联账号', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: linkAttempt() })
      .mockResolvedValueOnce({ canceled: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    expect(openWxPusherSubscription).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="wxpusher-subscribe-link"]').attributes('href')).toBe(personalSubscribeUrl)
    await wrapper.get('[data-testid="wxpusher-cancel-link"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/cancel', method: 'POST', data: { mode: 'link', attempt_id: 'test-link-attempt' },
    })
    expect(wrapper.find('[data-testid="wxpusher-subscribe-link"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('旧标签页只取消它正在展示的尝试，失败后重新读取当前绑定状态', async () => {
    const freshUrl = 'https://wxpusher.zjiecode.com/api/s/fresh-link'
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: linkAttempt('old-link') })
      .mockRejectedValueOnce(new Error('此绑定尝试已更新，请重新查看。'))
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: linkAttempt('fresh-link', freshUrl) })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="wxpusher-subscribe-link"]').attributes('href')).toBe(personalSubscribeUrl)
    await wrapper.get('[data-testid="wxpusher-cancel-link"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenNthCalledWith(2, {
      url: '/api/auth/wxpusher/binding/cancel', method: 'POST', data: { mode: 'link', attempt_id: 'old-link' },
    })
    expect(request).toHaveBeenLastCalledWith({ url: '/api/auth/wxpusher/guide', method: 'GET' })
    expect(wrapper.get('[data-testid="wxpusher-subscribe-link"]').attributes('href')).toBe(freshUrl)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.get('[role="alert"]').text()).toContain('此绑定尝试已更新')
  })

  it.each([true, false])('已有订阅可用 App 收到的验证码换绑（确认发送受理=%s）', async (sent) => {
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: linkAttempt() })
      .mockResolvedValueOnce({ sent, delivery_unconfirmed: !sent, expires_at: new Date(Date.now() + 240_000).toISOString() })
      .mockResolvedValueOnce({ bound: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    const uidField = 'input#wxpusher-link-uid'
    const codeField = 'input#wxpusher-link-code'
    expect(wrapper.find(uidField).exists()).toBe(false)

    await wrapper.get('[data-testid="wxpusher-link-recovery"]').trigger('click')
    expect(wrapper.text()).toContain('WxPusher App 的「我的」')
    expect(wrapper.find(codeField).exists()).toBe(true)
    expect(wrapper.get('[data-testid="wxpusher-confirm-link-code"]').element).toHaveProperty('disabled', true)
    await wrapper.get(uidField).setValue('UID_local_worker')
    await wrapper.get('[data-testid="wxpusher-send-link-code"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/link-code', method: 'POST',
      data: { mode: 'link', attempt_id: 'test-link-attempt', uid: 'UID_local_worker' },
    })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.text()).not.toContain('已换绑 WxPusher')
    expect(wrapper.text()).toContain(sent ? '发送请求已受理' : '暂未确认验证码是否送达')

    await wrapper.get(codeField).setValue('042900')
    await wrapper.get('[data-testid="wxpusher-confirm-link-code"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/link-confirm', method: 'POST',
      data: { mode: 'link', attempt_id: 'test-link-attempt', code: '042900' },
    })
    expect(wrapper.text()).toContain('已换绑 WxPusher')
    expect(wrapper.find(uidField).exists()).toBe(false)
  })

  it('重新打开待绑定页面后可直接验证已收到的验证码，无需重发', async () => {
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: linkAttempt() })
      .mockResolvedValueOnce({ bound: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    await wrapper.get('[data-testid="wxpusher-link-recovery"]').trigger('click')
    await wrapper.get('input#wxpusher-link-code').setValue('031021')
    await wrapper.get('[data-testid="wxpusher-confirm-link-code"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/link-confirm', method: 'POST',
      data: { mode: 'link', attempt_id: 'test-link-attempt', code: '031021' },
    })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
  })

  it('仍待绑定的后台查询保留验证码失败提示', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    const guide = { binding_enabled: true, bound: true, pending_binding: linkAttempt() }
    vi.mocked(request)
      .mockResolvedValueOnce(guide)
      .mockRejectedValueOnce(new Error('验证码不正确，请重试。'))
      .mockResolvedValueOnce(guide)
      .mockResolvedValueOnce({ bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    await wrapper.get('[data-testid="wxpusher-link-recovery"]').trigger('click')
    await wrapper.get('input#wxpusher-link-code').setValue('031021')
    await wrapper.get('[data-testid="wxpusher-confirm-link-code"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('验证码不正确，请重试。')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ url: '/api/auth/wxpusher/binding/progress' }))
    expect(wrapper.get('[role="alert"]').text()).toBe('验证码不正确，请重试。')
  })

  it('绑定检查的回复丢失时读取最终状态并退出旧链接', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: linkAttempt() })
      .mockRejectedValueOnce(new Error('请求超时'))
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: null })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    expect(wrapper.find('[data-testid="wxpusher-subscribe-link"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(request).toHaveBeenLastCalledWith({ url: '/api/auth/wxpusher/guide', method: 'GET' })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.find('[data-testid="wxpusher-subscribe-link"]').exists()).toBe(false)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(request).toHaveBeenCalledTimes(3)
  })

  it('查询的尝试已更新时继续展示服务端返回的另一条待绑定链接', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    const freshUrl = 'https://wxpusher.zjiecode.com/api/s/other-attempt'
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: linkAttempt('old-attempt') })
      .mockRejectedValueOnce(new Error('绑定尝试已失效'))
      .mockResolvedValueOnce({ binding_enabled: true, bound: true, pending_binding: linkAttempt('current-attempt', freshUrl) })
      .mockResolvedValueOnce({ bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.get('[data-testid="wxpusher-subscribe-link"]').attributes('href')).toBe(freshUrl)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/progress', method: 'POST', timeout: 25_000,
      data: { mode: 'link', attempt_id: 'current-attempt' },
    })
  })

  it('从「我的」进入时点击返回复用原来的历史记录', async () => {
    state.back = '/me'
    vi.mocked(request).mockResolvedValue({ subscribe_url: subscribeUrl, qr_url: null })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await wrapper.get('[data-testid="wxpusher-guide-back"]').trigger('click')
    expect(back).toHaveBeenCalledOnce()
    expect(replace).not.toHaveBeenCalled()
  })

  it('未开放绑定时仍能打开通知记录', async () => {
    vi.mocked(request).mockResolvedValue({ binding_enabled: false, bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    expect(wrapper.find('[data-testid="wxpusher-bind"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('绑定暂未开放')
    await wrapper.get('[data-testid="wxpusher-notification-inbox"] a').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'm-me-notification-inbox' })
  })

  it('未绑定也能在消息通知页设置抢单订阅，折叠后保留未保存的选择', async () => {
    vi.mocked(request).mockImplementation(async ({ url }) => {
      if (url === '/api/auth/wxpusher/guide') return { binding_enabled: false, bound: false }
      if (url === '/api/auth/wxpusher/hall-subscription') return { enabled: false, games: [] }
      if (url === '/api/business/games/') return { items: [{ id: 101, name: '测试游戏甲', is_active: true }] }
      if (url === '/api/auth/me') return { user: { game_ids: [101] } }
      throw new Error(`未预期的请求 ${url}`)
    })
    vi.spyOn(useSessionStore(), 'canNode').mockImplementation((nodes) => nodes.includes('grab_hall.take_order'))
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    const toggle = wrapper.get('[data-testid="wxpusher-hall-subscription"]')
    const enabledSwitch = 'input[role="switch"][aria-label="新单推送"]'
    const gameCheckbox = 'input[name="hall-push-game-101"]'

    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find(enabledSwitch).exists()).toBe(false)
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('消息通知')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await wrapper.get(enabledSwitch).setValue(true)
    await wrapper.get(gameCheckbox).setValue(true)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')

    expect(wrapper.get(enabledSwitch).element).toHaveProperty('checked', true)
    expect(wrapper.get(gameCheckbox).element).toHaveProperty('checked', true)
    expect(vi.mocked(request).mock.calls.filter(([{ url }]) => url === '/api/auth/wxpusher/hall-subscription')).toHaveLength(1)
    expect(push).not.toHaveBeenCalled()
  })

  it('没有抢单权限时不显示抢单订阅', async () => {
    vi.mocked(request).mockResolvedValue({ binding_enabled: true, bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="wxpusher-hall-subscription"]').exists()).toBe(false)
  })

  it('读取绑定状态失败时可在第二步重试', async () => {
    vi.mocked(request).mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({ binding_enabled: true, bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    expect(wrapper.find('[data-testid="wxpusher-bind"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('绑定状态加载失败')
    await wrapper.get('[data-testid="retry-wxpusher-guide"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="wxpusher-bind"]').exists()).toBe(true)
  })

  it('扫个人码后自动绑定，无需验证码或按钮确认，并能确认解绑', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    const personalQr = 'https://wxpusher.zjiecode.com/api/qrcode/ExamplePersonalCode.jpg'
    const expiresAt = new Date(Date.now() + 300_000).toISOString()
    vi.mocked(request)
      .mockResolvedValueOnce({ subscribe_url: null, qr_url: qrUrl, binding_enabled: true, binding_link_supported: false, bound: false })
      .mockResolvedValueOnce({ qr_url: personalQr, expires_at: expiresAt })
      .mockResolvedValueOnce({ bound: false })
      .mockResolvedValueOnce({ bound: true })
      .mockResolvedValueOnce({ bound: false })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="wxpusher-bind"]').text()).toBe('获取绑定二维码')
    await wrapper.get('[data-testid="wxpusher-bind"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(personalQr)
    expect(wrapper.text()).toContain('扫完回到本页，团团通会自动绑定')
    expect(wrapper.text()).not.toContain('验证码')
    expect(wrapper.find('[data-testid="wxpusher-scanned"]').exists()).toBe(false)
    expect(request).toHaveBeenCalledWith({ url: '/api/auth/wxpusher/binding/start', method: 'POST' })
    await vi.advanceTimersByTimeAsync(9_999)
    expect(request).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(request).toHaveBeenLastCalledWith({
      url: '/api/auth/wxpusher/binding/progress', method: 'POST', timeout: 25_000,
    })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('未绑定')
    await vi.advanceTimersByTimeAsync(9_999)
    expect(request).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
    expect(wrapper.get('[data-testid="wxpusher-send-test"]').text()).toBe('发送测试消息')
    expect(wrapper.find('[data-testid="wxpusher-rebind"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(request).toHaveBeenCalledTimes(4)

    await wrapper.get('[data-testid="wxpusher-unbind"]').trigger('click')
    expect(wrapper.text()).toContain('WxPusher 里的订阅会保留')
    expect(request).not.toHaveBeenCalledWith({ url: '/api/auth/wxpusher/binding', method: 'DELETE' })
    await wrapper.get('[data-testid="wxpusher-unbind-confirm"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: '/api/auth/wxpusher/binding', method: 'DELETE' })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('未绑定')
  })

  it('恢复上次的个人码；切到别的 App 时暂停，回来后继续查绑定', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    vi.mocked(request)
      .mockResolvedValueOnce({
        binding_enabled: true, bound: false,
        pending_binding: { qr_url: qrUrl, expires_at: new Date(Date.now() + 300_000).toISOString() },
      })
      .mockResolvedValueOnce({ bound: true })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(qrUrl)

    hidden.mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(20_000)
    expect(request).toHaveBeenCalledOnce()

    hidden.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(request).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('已绑定')
  })

  it('换码时忽略旧查询迟到的结果；离开页面后不再查询', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    let completeOldProgress!: (result: { bound: boolean }) => void
    const oldProgress = new Promise<{ bound: boolean }>((resolve) => { completeOldProgress = resolve })
    const freshQr = 'https://wxpusher.zjiecode.com/api/qrcode/FreshPersonalCode.jpg'
    const expiresAt = new Date(Date.now() + 300_000).toISOString()
    vi.mocked(request)
      .mockResolvedValueOnce({
        binding_enabled: true, bound: false, pending_binding: { qr_url: qrUrl, expires_at: expiresAt },
      })
      .mockReturnValueOnce(oldProgress)
      .mockResolvedValueOnce({ qr_url: freshQr, expires_at: expiresAt })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(request).toHaveBeenCalledTimes(2)

    const replaceQr = wrapper.findAll('button').find((button) => button.text() === '换一个个人绑定码')
    await replaceQr!.trigger('click')
    await flushPromises()
    completeOldProgress({ bound: true })
    await flushPromises()
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe('未绑定')
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(freshQr)

    wrapper.unmount()
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(20_000)
    expect(request).toHaveBeenCalledTimes(3)
  })

  it('换码失败且旧码已失效时，保留获取新码入口', async () => {
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false })
      .mockResolvedValueOnce({ qr_url: qrUrl, expires_at: new Date(Date.now() + 300_000).toISOString() })
      .mockRejectedValueOnce(new Error('暂时无法生成二维码，请稍后重试。'))
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: null })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await wrapper.get('[data-testid="wxpusher-bind"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(qrUrl)

    const replaceQr = wrapper.findAll('button').find((button) => button.text() === '换一个个人绑定码')
    expect(replaceQr).toBeDefined()
    await replaceQr!.trigger('click')
    await flushPromises()
    expect(wrapper.find('img[alt="WxPusher 个人绑定二维码"]').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法生成二维码')
    expect(wrapper.get('[data-testid="wxpusher-bind"]').attributes('disabled')).toBeUndefined()
  })

  it.each([false, true])('换码请求被拒绝时恢复服务端状态（已绑定=%s）', async (bound) => {
    const pendingQr = { qr_url: qrUrl, expires_at: new Date(Date.now() + 300_000).toISOString() }
    vi.mocked(request)
      .mockResolvedValueOnce({ binding_enabled: true, bound: false, pending_binding: pendingQr })
      .mockRejectedValueOnce(new Error('暂时无法换码'))
      .mockResolvedValueOnce({ binding_enabled: true, bound, pending_binding: bound ? null : pendingQr })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()

    const replaceQr = wrapper.findAll('button').find((button) => button.text() === '换一个个人绑定码')
    await replaceQr!.trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({ url: '/api/auth/wxpusher/guide', method: 'GET' })
    expect(wrapper.get('[data-testid="wxpusher-binding-status"]').text()).toBe(bound ? '已绑定' : '未绑定')
    expect(wrapper.find('img[alt="WxPusher 个人绑定二维码"]').exists()).toBe(!bound)
    expect(wrapper.find('[role="alert"]').exists()).toBe(!bound)
  })

  it('个人码过期后隐藏二维码、停止查询且可申请新码', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    const freshQr = 'https://wxpusher.zjiecode.com/api/qrcode/FreshPersonalCode.jpg'
    vi.mocked(request)
      .mockResolvedValueOnce({ subscribe_url: null, qr_url: qrUrl, binding_enabled: true, bound: false })
      .mockResolvedValueOnce({ qr_url: qrUrl, expires_at: new Date(Date.now() + 1000).toISOString() })
      .mockResolvedValueOnce({ qr_url: freshQr, expires_at: new Date(Date.now() + 300_000).toISOString() })
    const wrapper = mount(MeWxPusherPage)
    await flushPromises()
    await wrapper.get('[data-testid="wxpusher-bind"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(qrUrl)

    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.find('img[alt="WxPusher 个人绑定二维码"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('绑定二维码已过期')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(request).toHaveBeenCalledTimes(2)
    await wrapper.get('[data-testid="wxpusher-bind"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('img[alt="WxPusher 个人绑定二维码"]').attributes('src')).toBe(freshQr)
  })
})
