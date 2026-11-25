// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({ fetchMe: vi.fn(), login: vi.fn(), logout: vi.fn() }))
vi.mock('@/api/site', () => ({ fetchSiteName: vi.fn() }))
// 手机端 UI 走 TDesign（⛔ element-plus）。测试只桩**组件层**：真组件的 es 构建会牵出 `.css` 导入（vitest 的外部化依赖交给 Node 加载，不认 .css），而共享的 vitest.config.ts 不为单页测试改动，故在此桩掉。
vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))

const replace = vi.fn()
/** 每个用例自行设置回跳来源。 */
const routeQuery: { redirect?: unknown; notice?: unknown; entry?: unknown } = {}
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace }),
  useRoute: () => ({ query: routeQuery }),
}))

import { login } from '@/api/auth'
import { fetchSiteName } from '@/api/site'
import { Toast } from '../../framework7Ui'
import { BizError, HttpError } from '@/api/errors'
import { http } from '@/api/http'
import LoginView from '../LoginView.vue'

const apiLogin = vi.mocked(login)
type ToastMock = ReturnType<typeof vi.fn> & { error: ReturnType<typeof vi.fn> }
const toast = Toast as unknown as ToastMock

const booster = {
  id: 7,
  username: 'ceshidashoujia',
  display_name: '测试打手甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [],
  permissions: [],
}

beforeEach(() => {
  setActivePinia(createPinia())
  apiLogin.mockReset()
  vi.mocked(fetchSiteName).mockReset().mockResolvedValue('')
  toast.error.mockClear()
  replace.mockReset()
  delete routeQuery.redirect
  delete routeQuery.notice
  delete routeQuery.entry
})

async function fillAndSubmit(wrapper: ReturnType<typeof mount>) {
  await wrapper.find('input#m-login-username').setValue('测试打手甲')
  await wrapper.find('input#m-login-password').setValue('mima-000000')
  await wrapper.find('button').trigger('click')
}

installFramework7ForTest()

describe('m 端登录页', () => {
  it('登录前使用站点返回的工作室名作为主标题', async () => {
    vi.mocked(fetchSiteName).mockResolvedValue('测试星河工作室')
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('测试星河工作室')
    expect(apiLogin).not.toHaveBeenCalled()
  })

  it('工作室名未取到时仍显示通用登录标题', async () => {
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('打手登录')
    expect(wrapper.find('#m-login-username').exists()).toBe(true)
  })

  it('成功：按 redirect（合法 /m/ 路径）落回原页', async () => {
    apiLogin.mockResolvedValue(booster)
    routeQuery.redirect = '/m/hall?game=3'
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(apiLogin).toHaveBeenCalledWith({ username: '测试打手甲', password: 'mima-000000' })
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith('/hall?game=3')
  })

  it('成功：无 redirect → m-home', async () => {
    apiLogin.mockResolvedValue(booster)
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'm-home' })
  })

  it('WxPusher 回退页登录成功后仍按 WxPusher 的打手端入口打开', async () => {
    apiLogin.mockResolvedValue(booster)
    routeQuery.entry = 'wxpusher'
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'm-home', query: { entry: 'wxpusher' } })
  })

  it('从新单推送登录后回到该单的抢单确认入口', async () => {
    apiLogin.mockResolvedValue(booster)
    routeQuery.entry = 'wxpusher'
    routeQuery.redirect = '/m/hall?claimSource=peer&claimRef=42'
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()

    expect(replace).toHaveBeenCalledWith('/hall?claimSource=peer&claimRef=42')
  })

  it('成功：外站 redirect 被清洗拒掉 → 回 m-home（不放行站外目标）', async () => {
    apiLogin.mockResolvedValue(booster)
    routeQuery.redirect = 'https://evil.example'
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'm-home' })
  })

  it('成功：桌面路径 redirect 被清洗拒掉 → 回 m-home', async () => {
    apiLogin.mockResolvedValue(booster)
    routeQuery.redirect = '/orders'
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ name: 'm-home' })
  })

  it('失败：toast 显示后端 msg 原样（BizError.message），不跳转', async () => {
    apiLogin.mockRejectedValue(new BizError(401, '用户名或密码错误'))
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(toast.error).toHaveBeenCalledWith('用户名或密码错误')
    expect(replace).not.toHaveBeenCalled()
  })

  it('失败：HttpError 的 message 同样原样显示', async () => {
    apiLogin.mockRejectedValue(new HttpError(403, '该单不是你接的，不能提交'))
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('该单不是你接的，不能提交')
  })

  it('提交期间按钮禁用；结束后复位', async () => {
    let resolveLogin: (u: typeof booster) => void = () => {}
    apiLogin.mockImplementation(
      () => new Promise((res) => { resolveLogin = res }),
    )
    const wrapper = mount(LoginView)
    await wrapper.find('input#m-login-username').setValue('测试打手甲')
    await wrapper.find('input#m-login-password').setValue('mima-000000')
    const btn = wrapper.find('button')
    expect(btn.attributes('disabled')).toBeUndefined() // 两格填完 → 可点

    await btn.trigger('click')
    expect(apiLogin).toHaveBeenCalledTimes(1)
    expect(btn.attributes('disabled')).toBeDefined() // 提交中禁用

    resolveLogin(booster)
    await flushPromises()
    expect(btn.attributes('disabled')).toBeUndefined() // 结束后复位
  })

  it('没填完不发电（canSubmit 闸）', async () => {
    const wrapper = mount(LoginView)
    await wrapper.find('input#m-login-username').setValue('测试打手甲')
    const btn = wrapper.find('button')
    expect(btn.attributes('disabled')).toBeDefined()
    await btn.trigger('click') // disabled 的按钮仍触发 DOM click，靠 submit() 早返回
    expect(apiLogin).not.toHaveBeenCalled()
  })
})

describe('WxPusher 订单登录', () => {
  const orderNo = '10000000000000000002'
  const followUrl = 'https://wxpusher.zjiecode.com/app/#/follow?type=1&id=141&jump=auto'
  const continueButton = '[data-testid="wxpusher-continue-order"]'
  const pendingOrderKey = 'ttt-wxpusher-order-continuation'
  const location = window.location
  const assign = vi.fn()

  beforeEach(() => {
    routeQuery.entry = 'wxpusher'
    routeQuery.redirect = `/m/orders/${orderNo}`
    Object.defineProperty(window, 'location', { configurable: true, value: { ...location, assign } })
    document.cookie = 'csrftoken=csrf-fixture; path=/'
    window.sessionStorage.removeItem(pendingOrderKey)
  })

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: location })
    document.cookie = 'csrftoken=; path=/; max-age=0'
    assign.mockReset()
    vi.restoreAllMocks()
  })

  it('官方地址有效后，先在当前标签保留订单，再打开 WxPusher 官方验证页面', async () => {
    const send = vi.spyOn(http, 'request').mockResolvedValue({ data: { code: 0, msg: '', data: { url: followUrl } } })
    assign.mockImplementation(() => {
      const stored = JSON.parse(window.sessionStorage.getItem(pendingOrderKey) ?? 'null')
      expect(stored).toEqual({ orderNo, expiresAt: expect.any(Number) })
      expect(stored.expiresAt).toBeGreaterThan(Date.now())
    })
    const wrapper = mount(LoginView)
    const button = wrapper.get(continueButton)
    expect(button.text()).toBe('通过 WxPusher 继续')
    await button.trigger('click')
    await flushPromises()
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      method: 'POST', url: `/api/auth/wxpusher/orders/${orderNo}/continue`,
      headers: expect.objectContaining({ 'X-CSRFToken': 'csrf-fixture' }),
    }))
    expect(assign).toHaveBeenCalledExactlyOnceWith(followUrl)
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('接口回复业务失败时留在本页，仍可用密码登录并返回订单', async () => {
    vi.spyOn(http, 'request').mockResolvedValue({ data: { code: 1002, msg: '暂时无法使用 WxPusher 登录', data: null } })
    apiLogin.mockResolvedValue(booster)
    const wrapper = mount(LoginView)
    await wrapper.get(continueButton).trigger('click')
    await flushPromises()
    expect(assign).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('暂时无法使用 WxPusher 登录')
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(replace).toHaveBeenCalledExactlyOnceWith(`/orders/${orderNo}`)
  })

  it('普通网页的订单登录不显示通过 WxPusher 继续', () => {
    delete routeQuery.entry
    expect(mount(LoginView).find(continueButton).exists()).toBe(false)
  })

  it('成功回包带非官方地址时不会跳转，仍可留在登录页', async () => {
    vi.spyOn(http, 'request').mockResolvedValue({
      data: { code: 0, msg: '', data: { url: 'https://example.invalid/app/#/follow?type=1&id=141&jump=auto' } },
    })
    const wrapper = mount(LoginView)
    await wrapper.get(continueButton).trigger('click')
    await flushPromises()
    expect(assign).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledExactlyOnceWith('暂时无法通过 WxPusher 继续，请用账号密码登录')
  })

  it('当前浏览器不能保存订单时保留密码登录并说明原因', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('不允许存储') })
    vi.spyOn(http, 'request').mockResolvedValue({ data: { code: 0, msg: '', data: { url: followUrl } } })
    const wrapper = mount(LoginView)
    await wrapper.get(continueButton).trigger('click')
    await flushPromises()
    expect(assign).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('当前网页无法记住这张订单，请用账号密码登录')
    expect(wrapper.find('input#m-login-password').attributes('disabled')).toBeUndefined()
  })

  it('已回到当前订单但没有网页会话时提示用账号密码登录', () => {
    routeQuery.notice = 'wxpusher-order-password'
    const wrapper = mount(LoginView)
    expect(wrapper.get('[role="status"]').text()).toBe('请先用团团通账号密码登录，登录后会打开这张订单。')
  })

  it('改用密码登录时放弃当前标签之前的 WxPusher 回跳', async () => {
    window.sessionStorage.setItem(pendingOrderKey, JSON.stringify({ orderNo, expiresAt: Date.now() + 60_000 }))
    apiLogin.mockResolvedValue(booster)
    const wrapper = mount(LoginView)
    await fillAndSubmit(wrapper)
    await flushPromises()
    expect(window.sessionStorage.getItem(pendingOrderKey)).toBeNull()
    expect(replace).toHaveBeenCalledWith(`/orders/${orderNo}`)
  })
})

/**
 * 自助注册回流横幅（spec 判据；`docs/specs/2026-10-16-self-registration-links-design.md`）：`/m/register` 注册成功跳 `/m/login?notice=self-registered` ⇒ 顶部横幅。
 * ⚠️ 判据打在**横幅文本**上（E-0009：不是"没崩"）；反向用例钉「带别的 query 值**不**出」（键在在与值真假是两件事，E-0021 同族）。
 */
describe('m 端登录页 · 自助注册回流横幅（spec §7-20）', () => {
  it('`?notice=self-registered` ⇒ 渲染横幅，文案逐字', async () => {
    routeQuery.notice = 'self-registered'
    const wrapper = mount(LoginView)
    await flushPromises()
    const banner = wrapper.find('.m-login__notice')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toBe('注册成功！请用手机号登录（密码见注册完成页）')
  })

  it('无 notice query ⇒ **不**渲染横幅', async () => {
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.find('.m-login__notice').exists()).toBe(false)
  })

  it('notice 是别的值 ⇒ 也不渲染（钉的是「只认 self-registered 这一个值」）', async () => {
    routeQuery.notice = 'something-else'
    const wrapper = mount(LoginView)
    await flushPromises()
    expect(wrapper.find('.m-login__notice').exists()).toBe(false)
  })
})
