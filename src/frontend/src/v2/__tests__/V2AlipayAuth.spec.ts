// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import V2AlipayAuth from '../V2AlipayAuth.vue'
import * as api from '@/api/alipayAuthorization'

vi.mock('@/api/alipayAuthorization', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/alipayAuthorization')>()),
  listAlipayAuthorizations: vi.fn(), createAlipayAuthorization: vi.fn(),
  authorizeAlipay: vi.fn(), refreshAlipayAuthorization: vi.fn(), identityAlipayAuthorization: vi.fn(), refreshAlipayIdentity: vi.fn(), revokeAlipayAuthorization: vi.fn(),
  newAlipayRequestId: vi.fn(() => 'synthetic-request'),
}))
const pending: api.AlipayAuthorization = {
  id: 12, request_id: 'server-request', account_masked: '138****0000', real_name: '测试用户', status: 'pending',
  created_at: '2026-01-01', authorized_at: null, last_error: null,
  authorization_url: null, qr_png_base64: 'cG5n',
}
const click = async (wrapper: ReturnType<typeof mount>, label: string) => {
  const button = wrapper.findAll('button').find((item) => item.text() === label)
  expect(button).toBeDefined()
  await button!.trigger('click'); await flushPromises()
}

describe('付款支付宝授权', () => {
  afterEach(() => { vi.useRealTimers() })
  beforeEach(() => { localStorage.clear(); vi.resetAllMocks(); vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([pending]); vi.mocked(api.newAlipayRequestId).mockReturnValue('synthetic-request') })
  it('签约后获取付款身份二维码并刷新结果', async () => {
    const signed = { ...pending, status: 'signed', next_action: 'identity', qr_png_base64: null }
    vi.mocked(api.authorizeAlipay).mockResolvedValue(pending)
    vi.mocked(api.refreshAlipayAuthorization).mockResolvedValue(signed)
    vi.mocked(api.identityAlipayAuthorization).mockResolvedValue({ ...signed, qr_png_base64: 'aWRlbnRpdHk=' })
    vi.mocked(api.refreshAlipayIdentity).mockResolvedValue({ ...signed, status: 'authorized', next_action: null })
    const wrapper = mount(V2AlipayAuth); await flushPromises(); await click(wrapper, '继续授权')
    const dialog = wrapper.find('[aria-label="支付宝授权"]')
    await dialog.findAll('button').find((b) => b.text() === '刷新授权结果')!.trigger('click'); await flushPromises()
    expect(api.identityAlipayAuthorization).toHaveBeenCalledWith(12)
    expect(dialog.text()).toContain('请使用付款支付宝扫码'); expect(dialog.find('img').attributes('src')).toContain('aWRlbnRpdHk=')
    await dialog.findAll('button').find((b) => b.text() === '刷新授权结果')!.trigger('click'); await flushPromises()
    expect(api.refreshAlipayIdentity).toHaveBeenCalledWith(12); expect(dialog.text()).toContain('授权成功'); wrapper.unmount()
  })
  it('关闭弹窗后在途签约刷新不会继续发起付款身份授权', async () => {
    let complete!: (value: api.AlipayAuthorization) => void
    vi.mocked(api.authorizeAlipay).mockResolvedValue(pending)
    vi.mocked(api.refreshAlipayAuthorization).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const wrapper = mount(V2AlipayAuth); await flushPromises(); await click(wrapper, '继续授权')
    const dialog = wrapper.find('[aria-label="支付宝授权"]')
    await dialog.findAll('button').find((b) => b.text() === '刷新授权结果')!.trigger('click')
    await click(wrapper, '关闭'); complete({ ...pending, status: 'signed', next_action: 'identity' }); await flushPromises()
    expect(api.identityAlipayAuthorization).not.toHaveBeenCalled(); expect(wrapper.find('[aria-label="支付宝授权"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('取消授权必须二次确认，保留已取消记录并允许重新授权', async () => {
    const authorized = { ...pending, status: 'authorized' }; const revoked = { ...pending, status: 'revoked' }
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([authorized]); vi.mocked(api.revokeAlipayAuthorization).mockResolvedValue(revoked)
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    await click(wrapper, '取消授权'); expect(api.revokeAlipayAuthorization).not.toHaveBeenCalled()
    await click(wrapper, '返回'); expect(api.revokeAlipayAuthorization).not.toHaveBeenCalled()
    await click(wrapper, '取消授权'); vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([revoked]); await click(wrapper, '确认取消授权')
    expect(api.revokeAlipayAuthorization).toHaveBeenCalledExactlyOnceWith(12)
    expect(wrapper.text()).toContain('已取消授权'); expect(wrapper.text()).toContain('重新授权'); wrapper.unmount()
  })
  it('验证码失败自动重试，出现二维码后停止', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockResolvedValueOnce(captcha).mockResolvedValueOnce(pending)
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    await click(wrapper, '继续授权')
    expect(wrapper.text()).toContain('正在自动尝试获取授权二维码')
    await vi.advanceTimersByTimeAsync(2999); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1); await flushPromises(); expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(3000); await flushPromises(); expect(api.authorizeAlipay).toHaveBeenCalledTimes(3)
    expect(wrapper.find('img[alt="支付宝授权二维码"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })
  it('手动刷新失败后自动尝试停止，不显示正在尝试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let fail!: (reason: Error) => void
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha)
    vi.mocked(api.refreshAlipayAuthorization).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    await click(wrapper, '继续授权')
    const dialog = wrapper.find('[aria-label="支付宝授权"]')
    await dialog.findAll('button').find((item) => item.text() === '刷新授权结果')!.trigger('click')
    await vi.advanceTimersByTimeAsync(3000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).not.toContain('正在自动尝试获取授权二维码')
    fail(new Error('刷新失败')); await flushPromises()
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('新增付款账号遇到验证码失败时自动沿用记录重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.createAlipayAuthorization).mockResolvedValueOnce(captcha)
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(pending)
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    await click(wrapper, '增加一个用于付款的支付宝账号')
    const inputs = wrapper.findAll('input'); await inputs[0]!.setValue('13800000000'); await inputs[1]!.setValue('测试用户')
    await click(wrapper, '确定'); expect(wrapper.text()).toContain('正在自动尝试获取授权二维码')
    await vi.advanceTimersByTimeAsync(3000); await flushPromises()
    expect(api.createAlipayAuthorization).toHaveBeenCalledTimes(1)
    expect(api.authorizeAlipay).toHaveBeenCalledExactlyOnceWith(12)
    expect(wrapper.find('img[alt="支付宝授权二维码"]').exists()).toBe(true)
    wrapper.unmount()
  })
  it('关闭弹窗停止重试，待核对状态不重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha)
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    await click(wrapper, '继续授权'); await click(wrapper, '关闭')
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce({ ...captcha, requires_platform_review: true })
    await click(wrapper, '继续授权'); await vi.advanceTimersByTimeAsync(6000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
  it('关闭弹窗时在途重试返回也不会重新打开', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let complete!: (value: api.AlipayAuthorization) => void
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    await click(wrapper, '继续授权'); await vi.advanceTimersByTimeAsync(3000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    await click(wrapper, '关闭'); complete(pending); await flushPromises()
    expect(wrapper.find('[aria-label="支付宝授权"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
  it('首次授权请求尚未返回时离开页面，不启动自动重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let complete!: (value: api.AlipayAuthorization) => void
    vi.mocked(api.authorizeAlipay).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const wrapper = mount(V2AlipayAuth); await flushPromises(); vi.useFakeTimers()
    const button = wrapper.findAll('button').find((item) => item.text() === '继续授权')!
    await button.trigger('click'); wrapper.unmount(); complete(captcha)
    await flushPromises(); await vi.advanceTimersByTimeAsync(6000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
  })
  it('恢复待授权记录并刷新为授权成功', async () => {
    vi.mocked(api.refreshAlipayAuthorization).mockResolvedValue({ ...pending, status: 'authorized' })
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    expect(wrapper.text()).toContain('待授权')
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([{ ...pending, status: 'authorized' }])
    await click(wrapper, '刷新授权结果')
    expect(api.refreshAlipayAuthorization).toHaveBeenCalledWith(12)
    expect(wrapper.text()).toContain('授权成功')
    expect(wrapper.text()).not.toContain('继续授权')
  })
  it('提交失败后保留申请编号并允许重试，不误报成功', async () => {
    vi.mocked(api.createAlipayAuthorization).mockRejectedValueOnce(new Error('服务暂不可用')).mockResolvedValueOnce(pending)
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    await click(wrapper, '增加一个用于付款的支付宝账号')
    const inputs = wrapper.findAll('input'); await inputs[0]!.setValue('13800000000'); await inputs[1]!.setValue('测试用户')
    await click(wrapper, '确定')
    expect(wrapper.text()).toContain('服务暂不可用'); expect(wrapper.text()).not.toContain('授权成功')
    await click(wrapper, '确定')
    expect(api.createAlipayAuthorization).toHaveBeenNthCalledWith(1, { account: '13800000000', real_name: '测试用户', request_id: 'synthetic-request' })
    expect(api.createAlipayAuthorization).toHaveBeenNthCalledWith(2, { account: '13800000000', real_name: '测试用户', request_id: 'synthetic-request' })
    expect(wrapper.find('img[alt="支付宝授权二维码"]').attributes('src')).toBe('data:image/png;base64,cG5n')
  })
  it('未知状态不显示成功', async () => {
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([{ ...pending, status: 'unexpected' }])
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    expect(wrapper.text()).toContain('状态待确认'); expect(wrapper.text()).not.toContain('授权成功')
  })
  it('身份信息需平台核对时禁止继续授权，但允许刷新', async () => {
    const waiting = { ...pending, requires_platform_review: true, last_error: '付款身份信息不完整，请联系平台处理' }
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([waiting])
    vi.mocked(api.refreshAlipayAuthorization).mockResolvedValue(waiting)
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    expect(wrapper.text()).toContain('付款身份信息不完整，请联系平台处理')
    const button = wrapper.findAll('button').find((item) => item.text() === '继续授权')!
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click'); expect(api.authorizeAlipay).not.toHaveBeenCalled()
    await click(wrapper, '刷新授权结果')
    expect(api.refreshAlipayAuthorization).toHaveBeenCalledWith(12)
    expect(wrapper.text()).not.toContain('扫码完成身份授权')
  })
  it('弹窗进入平台核对状态时不展示二维码或允许重复授权', async () => {
    const waiting = { ...pending, requires_platform_review: true, last_error: '无法确认付款身份是否匹配，请联系平台处理' }
    vi.mocked(api.createAlipayAuthorization).mockResolvedValue(waiting)
    const wrapper = mount(V2AlipayAuth); await flushPromises()
    await click(wrapper, '增加一个用于付款的支付宝账号')
    const inputs = wrapper.findAll('input'); await inputs[0]!.setValue('13800000000'); await inputs[1]!.setValue('测试用户')
    await click(wrapper, '确定')
    const dialog = wrapper.find('[aria-label="支付宝授权"]')
    expect(dialog.text()).toContain('无法确认付款身份是否匹配，请联系平台处理')
    expect(dialog.find('img').exists()).toBe(false)
    expect(dialog.findAll('button').find((item) => item.text() === '继续授权')!.attributes('disabled')).toBeDefined()
  })
  it('请求超时后重新进入仍复用请求编号，直到列表确认同一记录', async () => {
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([])
    vi.mocked(api.createAlipayAuthorization).mockRejectedValueOnce(new Error('请求超时'))
    let wrapper = mount(V2AlipayAuth); await flushPromises()
    await click(wrapper, '增加一个用于付款的支付宝账号')
    let inputs = wrapper.findAll('input'); await inputs[0]!.setValue('13800000000'); await inputs[1]!.setValue('测试用户')
    await click(wrapper, '确定')
    expect(localStorage.getItem('alipay-authorization-pending-request')).toBe('synthetic-request')
    expect(JSON.stringify(localStorage)).not.toContain('13800000000')
    expect(JSON.stringify(localStorage)).not.toContain('测试用户')
    wrapper.unmount()
    wrapper = mount(V2AlipayAuth); await flushPromises()
    expect(wrapper.text()).toContain('上次申请结果尚未确认')
    await click(wrapper, '增加一个用于付款的支付宝账号')
    inputs = wrapper.findAll('input'); await inputs[0]!.setValue('13800000000'); await inputs[1]!.setValue('测试用户')
    vi.mocked(api.createAlipayAuthorization).mockResolvedValueOnce({ ...pending, request_id: 'synthetic-request' })
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValueOnce([{ ...pending, request_id: 'another-request' }])
    await click(wrapper, '确定')
    expect(api.createAlipayAuthorization).toHaveBeenLastCalledWith(expect.objectContaining({ request_id: 'synthetic-request' }))
    expect(localStorage.getItem('alipay-authorization-pending-request')).toBe('synthetic-request')
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValueOnce([{ ...pending, request_id: 'synthetic-request' }])
    await click(wrapper, '刷新列表')
    expect(localStorage.getItem('alipay-authorization-pending-request')).toBeNull()
  })
})
