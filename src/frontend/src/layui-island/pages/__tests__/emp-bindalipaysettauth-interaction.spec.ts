// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import page from '../emp-bindalipaysettauth'
import * as api from '@/api/alipayAuthorization'

const env = vi.hoisted(() => ({ handlers: {} as Record<string, (arg: { event: string; data: { id: number } }) => void>, reload: vi.fn(), opened: null as HTMLElement | null, notices: [] as string[], ends: {} as Record<number, () => void>, nextId: 0, confirm: null as ((id: number) => void) | null }))
vi.mock('@/api/alipayAuthorization', async (original) => ({
  ...(await original<typeof import('@/api/alipayAuthorization')>()),
  listAlipayAuthorizations: vi.fn(), createAlipayAuthorization: vi.fn(), authorizeAlipay: vi.fn(), refreshAlipayAuthorization: vi.fn(), identityAlipayAuthorization: vi.fn(), refreshAlipayIdentity: vi.fn(), revokeAlipayAuthorization: vi.fn(),
  newAlipayRequestId: vi.fn(() => 'synthetic-request'),
}))
vi.mock('@/layui/artifactPage', () => ({ fetchArtifactHtml: vi.fn(async () => `<form><div class="newBindDiv" style="display:none"><div class="layui-form-item"><input id="newPayAlipayAcc"></div><div class="layui-form-item"><input id="reNewPayAlipayAcc"></div><div class="layui-form-item"><input id="newPayAlipayName"></div><div class="layui-form-item"><input name="channel"></div><div class="layui-form-item"><input id="remark"></div><button type="button" lay-filter="savebtn">开始签约</button></div><table id="tableList"></table><script id="baroperating" type="text/html"></script></form>`) }))
vi.mock('@/layui/island', () => ({
  bootstrapIsland: async () => ({
    $: (el: HTMLElement) => el,
    form: { render: vi.fn() },
    table: { render: () => ({ reload: env.reload }), on: (name: string, cb: typeof env.handlers[string]) => { env.handlers[name] = cb } },
    layer: { confirm: (_text: string, _options: unknown, callback: (id: number) => void) => { env.confirm = callback }, open: ({ content, end, beforeEnd }: { content: HTMLElement; end?: () => void; beforeEnd?: () => void }) => { env.opened = content; const id = ++env.nextId; const fn = beforeEnd ?? end; if (fn) env.ends[id] = fn; return id }, close: vi.fn() },
  }),
  initIslandElements: vi.fn(), islandMsg: (_layer: unknown, text: string) => env.notices.push(text),
}))
const pending: api.AlipayAuthorization = { id: 12, request_id: 'server-request', account_masked: '138****0000', real_name: '测试用户', status: 'pending', created_at: null, authorized_at: null, last_error: null, authorization_url: null, qr_png_base64: 'cG5n' }
const flush = async () => { await new Promise((resolve) => setTimeout(resolve, 0)) }

describe('旧版付款支付宝授权', () => {
  afterEach(() => { vi.useRealTimers() })
  beforeEach(() => { localStorage.clear(); vi.resetAllMocks(); env.handlers = {}; env.opened = null; env.notices = []; env.ends = {}; env.nextId = 0; env.confirm = null; vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([pending]); vi.mocked(api.newAlipayRequestId).mockReturnValue('synthetic-request') })
  it('签约后付款身份扫码并刷新身份结果', async () => {
    const signed = { ...pending, status: 'signed', next_action: 'identity', qr_png_base64: null }
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([signed]); vi.mocked(api.identityAlipayAuthorization).mockResolvedValue({ ...signed, qr_png_base64: 'aWRlbnRpdHk=' })
    vi.mocked(api.refreshAlipayIdentity).mockResolvedValue({ ...signed, status: 'authorized', next_action: null })
    const root = document.createElement('div'); await page.mount(root, {} as never)
    env.handlers['tool(tableList)']!({ event: 'identity', data: { id: 12 } }); await flush()
    expect(env.opened?.textContent).toContain('请使用付款支付宝扫码'); expect(env.opened?.querySelector('img')?.src).toContain('aWRlbnRpdHk=')
    env.opened!.querySelector<HTMLButtonElement>('button')!.click(); await flush()
    expect(api.refreshAlipayIdentity).toHaveBeenCalledWith(12); expect(env.opened?.textContent).toContain('授权成功')
  })
  it('取消授权需要确认并保留已取消记录', async () => {
    const authorized = { ...pending, status: 'authorized' }; const revoked = { ...pending, status: 'revoked' }
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([authorized]); vi.mocked(api.revokeAlipayAuthorization).mockResolvedValue(revoked)
    const root = document.createElement('div'); await page.mount(root, {} as never)
    env.handlers['tool(tableList)']!({ event: 'revoke', data: { id: 12 } }); expect(api.revokeAlipayAuthorization).not.toHaveBeenCalled(); expect(env.confirm).not.toBeNull()
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([revoked]); env.confirm!(99); await flush()
    expect(api.revokeAlipayAuthorization).toHaveBeenCalledExactlyOnceWith(12)
    expect(env.reload).toHaveBeenLastCalledWith(expect.objectContaining({ data: [expect.objectContaining({ str2: '已取消授权', status: 'revoked' })] }))
    expect(root.querySelector('#baroperating')?.textContent).toContain('重新授权')
  })
  it('旧弹窗延迟关闭不能中断新弹窗的自动重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockResolvedValueOnce(captcha).mockResolvedValueOnce(pending)
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(3000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    env.ends[1]!()
    await vi.advanceTimersByTimeAsync(3000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(3)
    expect(env.opened?.querySelector('img')?.alt).toBe('支付宝授权二维码')
    if (typeof unmount === 'function') unmount()
  })
  it('验证码失败自动重试，出现二维码后停止', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockResolvedValueOnce(pending)
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0)
    expect(env.opened?.textContent).toContain('正在自动尝试获取授权二维码')
    await vi.advanceTimersByTimeAsync(2999); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1); expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    expect(env.opened?.querySelector('img')?.alt).toBe('支付宝授权二维码')
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    if (typeof unmount === 'function') unmount()
  })
  it('手动刷新跨过重试时间并失败时明确暂停自动尝试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let fail!: (reason: Error) => void
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha)
    vi.mocked(api.refreshAlipayAuthorization).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0)
    env.opened!.querySelector<HTMLButtonElement>('button')!.click()
    expect(env.opened?.textContent).toContain('自动尝试已暂停')
    await vi.advanceTimersByTimeAsync(3000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    fail(new Error('刷新失败')); await vi.advanceTimersByTimeAsync(0)
    expect(env.notices).toContain('刷新失败')
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    if (typeof unmount === 'function') unmount()
  })
  it('自动请求网络失败后明确暂停，不再发送请求', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockRejectedValueOnce(new Error('网络异常'))
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(3000)
    expect(env.opened?.textContent).toContain('自动尝试已暂停')
    expect(env.opened?.textContent).not.toContain('正在自动尝试')
    expect(env.notices).toContain('网络异常')
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    if (typeof unmount === 'function') unmount()
  })
  it('卸载页面后停止重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValue(captcha)
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); if (typeof unmount === 'function') unmount()
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
  })
  it('关闭当前弹窗后停止重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    vi.mocked(api.authorizeAlipay).mockResolvedValue(captcha)
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); env.ends[1]!()
    await vi.advanceTimersByTimeAsync(6000); expect(api.authorizeAlipay).toHaveBeenCalledTimes(1)
    if (typeof unmount === 'function') unmount()
  })
  it('用户关闭时在途重试即使返回二维码也不重新打开弹窗', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let complete!: (value: api.AlipayAuthorization) => void
    vi.mocked(api.authorizeAlipay).mockResolvedValueOnce(captcha).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(3000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(2)
    const original = env.opened; env.ends[1]!(); complete(pending)
    await vi.advanceTimersByTimeAsync(0); expect(env.opened).toBe(original)
    expect(env.opened?.querySelector('img')).toBeNull()
    if (typeof unmount === 'function') unmount()
  })
  it('首次授权请求尚未返回时离开页面，不打开弹窗或重试', async () => {
    const captcha = { ...pending, qr_png_base64: null, last_error: '验证码识别失败，请稍后重试', retryable_login: true }
    let complete!: (value: api.AlipayAuthorization) => void
    vi.mocked(api.authorizeAlipay).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const root = document.createElement('div'); const unmount = await page.mount(root, {} as never)
    vi.useFakeTimers(); env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } })
    await vi.advanceTimersByTimeAsync(0); if (typeof unmount === 'function') unmount(); complete(captcha)
    await vi.advanceTimersByTimeAsync(6000)
    expect(api.authorizeAlipay).toHaveBeenCalledTimes(1); expect(env.opened).toBeNull()
  })
  it('加载待授权记录，并通过行内操作继续授权', async () => {
    vi.mocked(api.authorizeAlipay).mockResolvedValue(pending)
    const root = document.createElement('div'); await page.mount(root, {} as never)
    expect(env.reload).toHaveBeenCalledWith(expect.objectContaining({ data: [expect.objectContaining({ id: 12, str2: '待授权' })] }))
    env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } }); await flush()
    expect(api.authorizeAlipay).toHaveBeenCalledWith(12)
    expect(env.opened?.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,cG5n')
  })
  it('身份信息需平台核对时禁止继续授权，保留刷新', async () => {
    const waiting = { ...pending, requires_platform_review: true, last_error: '付款身份信息不完整，请联系平台处理' }
    vi.mocked(api.listAlipayAuthorizations).mockResolvedValue([waiting])
    vi.mocked(api.refreshAlipayAuthorization).mockResolvedValue(waiting)
    const root = document.createElement('div'); await page.mount(root, {} as never)
    expect(env.reload).toHaveBeenCalledWith(expect.objectContaining({ data: [expect.objectContaining({ none: '付款身份信息不完整，请联系平台处理', requires_platform_review: true })] }))
    env.handlers['tool(tableList)']!({ event: 'authorize', data: { id: 12 } }); await flush()
    expect(api.authorizeAlipay).not.toHaveBeenCalled()
    env.handlers['tool(tableList)']!({ event: 'refreshAuth', data: { id: 12 } }); await flush()
    expect(api.refreshAlipayAuthorization).toHaveBeenCalledWith(12)
  })
  it('创建后需平台处理时弹窗不展示二维码并可刷新', async () => {
    const waiting = { ...pending, requires_platform_review: true, last_error: '无法确认付款身份是否匹配，请联系平台处理' }
    vi.mocked(api.createAlipayAuthorization).mockResolvedValue(waiting)
    const root = document.createElement('div'); await page.mount(root, {} as never)
    env.handlers['toolbar(tableList)']!({ event: 'addBookBtn', data: { id: 0 } })
    const box = env.opened!; (box.querySelector('#newPayAlipayAcc') as HTMLInputElement).value = '13800000000'; (box.querySelector('#newPayAlipayName') as HTMLInputElement).value = '测试用户'
    box.querySelector<HTMLButtonElement>('[lay-filter="savebtn"]')!.click(); await flush()
    expect(env.opened?.textContent).toContain('无法确认付款身份是否匹配，请联系平台处理')
    expect(env.opened?.querySelector('img')).toBeNull()
    expect(env.opened?.textContent).toContain('刷新授权结果')
  })
  it('提交失败时保留输入和请求编号，重试后显示二维码', async () => {
    vi.mocked(api.createAlipayAuthorization).mockRejectedValueOnce(new Error('服务暂不可用')).mockResolvedValueOnce(pending)
    const root = document.createElement('div'); await page.mount(root, {} as never)
    env.handlers['toolbar(tableList)']!({ event: 'addBookBtn', data: { id: 0 } })
    const box = env.opened!; (box.querySelector('#newPayAlipayAcc') as HTMLInputElement).value = '13800000000'; (box.querySelector('#newPayAlipayName') as HTMLInputElement).value = '测试用户'
    const button = box.querySelector<HTMLButtonElement>('[lay-filter="savebtn"]')!; button.click(); await flush()
    expect(env.notices).toContain('服务暂不可用'); expect(env.opened).toBe(box)
    button.click(); await flush()
    expect(api.createAlipayAuthorization).toHaveBeenNthCalledWith(1, expect.objectContaining({ request_id: 'synthetic-request' }))
    expect(api.createAlipayAuthorization).toHaveBeenNthCalledWith(2, expect.objectContaining({ request_id: 'synthetic-request' }))
    expect(env.opened?.querySelector('img')?.alt).toBe('支付宝授权二维码')
  })
})
