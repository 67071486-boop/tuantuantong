// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【自助注册】m 端公开注册页的契约测试（spec §7；照 `LoginView.spec.ts` 的形状）。这一页是**免登录公开页**（`requiresAuth:false`，唯一先例 `m-login`），没有后端可打 ⇒ **接口全部 mock**（`vi.mock('@/api/selfRegistration')`），断言打在「页面把这些夹具渲染成了什么」与「跳转去哪」上。
 * spec 原话的落点（一条不少）：① 有效首屏渲染池（工作室名 + 游戏 checkbox + 表单三件）；② 失效态：`valid:false` ⇒ 失效文案 + 去登录入口；③ 提交成功 → 成功态含 `initial_password`（来自 mock 响应，⛔ 前端不硬编码）→ 跳转 query 带对（`/login` + `notice=self-registered`，倒计时与手动两条路都钉）；④ 补强：提交失败展示信封 msg、三条预检文案带字段名且不发请求、取数失败不冒充"链接失效"。
 */
vi.mock('@/api/selfRegistration', () => ({
  fetchSelfRegisterScreen: vi.fn(),
  submitSelfRegister: vi.fn(),
}))


const replace = vi.fn()
/** 可变：每个用例自己设 `tk`。 */
const routeQuery: { tk?: unknown } = {}
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace }),
  useRoute: () => ({ query: routeQuery }),
}))

import {
  fetchSelfRegisterScreen,
  submitSelfRegister,
} from '@/api/selfRegistration'
import SelfRegisterPage from '../SelfRegisterPage.vue'

const mockedScreen = vi.mocked(fetchSelfRegisterScreen)
const mockedSubmit = vi.mocked(submitSelfRegister)

/** 合成快照：工作室 + 游戏池（spec §3.3 的 `games: [{id, name}]` 形状）。 */
const VALID_SCREEN = {
  valid: true as const,
  tenant_name: '测试工作室',
  games: [
    { id: 1, name: '测试游戏甲' },
    { id: 2, name: '测试游戏乙' },
    { id: 3, name: '测试游戏丙' },
  ],
  expires_at: '2026-10-23T10:00:00+08:00',
}

const DONE = { username: '13800001111', display_name: 'sq20001', initial_password: '合成初始密码-123' }

beforeEach(() => {
  mockedScreen.mockReset()
  mockedSubmit.mockReset()
  replace.mockReset()
  delete routeQuery.tk
  routeQuery.tk = 'tk-spec-token'
})

afterEach(() => {
  vi.useRealTimers()
})

/** 挂页并等首屏那一跳 settle（mount → onMounted 的 GET → 分支渲染）。 */
async function mountPage(): Promise<ReturnType<typeof mount>> {
  const wrapper = mount(SelfRegisterPage)
  await flushPromises()
  return wrapper
}

/** 填完手机号 + 收款支付宝两栏并勾上游戏（预检全过，只差提交）。 */
async function fillValidForm(wrapper: Awaited<ReturnType<typeof mountPage>>): Promise<void> {
  await wrapper.find('input#m-register-mobile').setValue('13800001111')
  await wrapper.find('input#m-register-alipay-account').setValue('synth-alipay@jab')
  await wrapper.find('input#m-register-alipay-name').setValue('测试打手甲')
  const boxes = wrapper.findAll('input[type="checkbox"]')
  await boxes[0]!.setValue(true)
  await boxes[2]!.setValue(true)
}

installFramework7ForTest()

describe('首屏（spec §7-19 第 1、2 条）', () => {
  it('有效：首屏请求带 `tk`、渲染工作室名 + 游戏池 checkbox + 表单三件', async () => {
    mockedScreen.mockResolvedValue(VALID_SCREEN)
    const wrapper = await mountPage()

    // 首屏那一跳真的带了链接里的令牌（⛔ 不是页面自己编一个空 tk 打过去）。
    expect(mockedScreen).toHaveBeenCalledTimes(1)
    expect(mockedScreen).toHaveBeenCalledWith('tk-spec-token')

    expect(wrapper.text()).toContain('测试工作室')
    const labels = wrapper.findAll('.m-self-register__games label').map((label) => label.text())
    expect(labels).toEqual(['测试游戏甲', '测试游戏乙', '测试游戏丙'])
    expect(wrapper.find('input#m-register-mobile').exists()).toBe(true)
    // spec §2.1：姓名栏删了，换成收款支付宝两栏。
    expect(wrapper.find('input#m-register-name').exists()).toBe(false)
    expect(wrapper.find('input#m-register-alipay-account').exists()).toBe(true)
    expect(wrapper.find('input#m-register-alipay-name').exists()).toBe(true)
    expect(wrapper.text()).toContain('收款支付宝账号')
    expect(wrapper.text()).toContain('收款支付宝姓名')
    expect(wrapper.text()).toContain('注册')
    // 失效文案与加载占位**都不在**（三态互斥，别渲染成"既有效又失效"）。
    expect(wrapper.text()).not.toContain('链接已失效或已被使用')
  })

  it('无效（`valid:false`）⇒ 失效态文案 + 「去登录」入口，**没有**表单', async () => {
    mockedScreen.mockResolvedValue({ valid: false })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('链接已失效或已被使用，请联系分享链接的人事重新获取')
    expect(wrapper.find('input#m-register-mobile').exists()).toBe(false)
    const btn = wrapper.findAll('button').find((b) => b.text() === '去登录')!
    expect(btn).toBeDefined()
    await btn.trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledWith({ path: '/login', query: { notice: 'self-registered' } })
  })

  it('首屏取数失败 ⇒ 显示**真实原因**（⛔ 不冒充"链接已失效"）', async () => {
    mockedScreen.mockRejectedValue(new Error('尝试过于频繁，请稍后再试'))
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('尝试过于频繁，请稍后再试')
    expect(wrapper.text()).not.toContain('链接已失效或已被使用')
  })
})

describe('提交（spec §7-19 第 3 条）', () => {
  beforeEach(() => {
    mockedScreen.mockResolvedValue(VALID_SCREEN)
  })

  it('成功 ⇒ 成功态含来自 mock 响应的 `initial_password`（⛔ 前端不硬编码）+ 引导文案', async () => {
    mockedSubmit.mockResolvedValue(DONE)
    const wrapper = await mountPage()
    await fillValidForm(wrapper)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()

    // 载荷逐键对（tk 来自 query；mobile 与支付宝两栏来自输入；game_ids 来自勾选）。
    // ⛔ 恰好五键：`display_name`（姓名）已删，载荷里不许再有它（spec §2.1）。
    expect(mockedSubmit).toHaveBeenCalledTimes(1)
    expect(mockedSubmit).toHaveBeenCalledWith({
      tk: 'tk-spec-token',
      mobile: '13800001111',
      alipay_account: 'synth-alipay@jab',
      alipay_name: '测试打手甲',
      game_ids: [1, 3],
    })
    // 成功页三行（spec §2.1）：登录账号 / 账号名称（响应的 display_name，前端不自拼）/ 初始密码。
    expect(wrapper.text()).toContain('注册成功！')
    expect(wrapper.text()).toContain(`登录账号：${DONE.username}`)
    expect(wrapper.text()).toContain(`账号名称：${DONE.display_name}`)
    expect(wrapper.text()).toContain('初始密码：')
    expect(wrapper.text()).toContain(DONE.initial_password)
    expect(wrapper.text()).toContain('登录后可在『我的-修改密码』修改')
    // 表单已不在（成功态替换了它，⛔ 不是叠在表单下面）。
    expect(wrapper.find('input#m-register-mobile').exists()).toBe(false)
  })

  it('成功后约 6 秒自动跳 `/m/login?notice=self-registered`（倒计时；E-0198：假定时器、用例自收口）', async () => {
    vi.useFakeTimers()
    mockedSubmit.mockResolvedValue(DONE)
    const wrapper = await mountPage()
    await fillValidForm(wrapper)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(replace).not.toHaveBeenCalled()

    // 5 秒：还没到（判据不是"最后跳了"的恒真形态）。
    vi.advanceTimersByTime(5000)
    expect(replace).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith({ path: '/login', query: { notice: 'self-registered' } })
  })

  it('手动点「去登录」⇒ 立即跳同一目标（不等倒计时）', async () => {
    vi.useFakeTimers()
    mockedSubmit.mockResolvedValue(DONE)
    const wrapper = await mountPage()
    await fillValidForm(wrapper)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text().startsWith('去登录'))!
    expect(btn).toBeDefined()
    await btn.trigger('click')
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith({ path: '/login', query: { notice: 'self-registered' } })
  })

  it('提交失败 ⇒ 展示**信封 msg 原样**、停在表单态，可改后重试', async () => {
    mockedSubmit.mockRejectedValue(new Error('该手机号已注册，请直接登录或联系人事'))
    const wrapper = await mountPage()
    await fillValidForm(wrapper)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    const alert = wrapper.find('.m-self-register__error')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toBe('该手机号已注册，请直接登录或联系人事')
    // 停在表单态（输入还在，用户改个手机号就能重试）。
    expect(wrapper.find('input#m-register-mobile').exists()).toBe(true)
    expect((wrapper.find('input#m-register-mobile').element as HTMLInputElement).value).toBe('13800001111')
    // 成功态没有出现（⛔ 不是又弹成功又报错）。
    expect(wrapper.text()).not.toContain('注册成功！')
  })
})

describe('提交前预检（体验层；文案带字段名，E-0074）', () => {
  beforeEach(() => {
    mockedScreen.mockResolvedValue(VALID_SCREEN)
  })

  it('手机号不是 11 位 ⇒ 就地提示带「手机号」字段名，**不发请求**', async () => {
    const wrapper = await mountPage()
    await wrapper.find('input#m-register-mobile').setValue('13800')
    await wrapper.find('input#m-register-alipay-account').setValue('synth-alipay@jab')
    await wrapper.find('input#m-register-alipay-name').setValue('测试打手甲')
    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.m-self-register__error').text()).toBe('手机号需为 11 位数字（1 开头）')
    expect(mockedSubmit).not.toHaveBeenCalled()
  })

  it('收款支付宝账号空着 ⇒ 提示带「支付宝账号」字段名，不发请求', async () => {
    const wrapper = await mountPage()
    await wrapper.find('input#m-register-mobile').setValue('13800001111')
    await wrapper.find('input#m-register-alipay-name').setValue('测试打手甲')
    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.m-self-register__error').text()).toBe('请填写收款支付宝账号')
    expect(mockedSubmit).not.toHaveBeenCalled()
  })

  it('收款支付宝姓名空着 ⇒ 提示带「支付宝姓名」字段名，不发请求', async () => {
    const wrapper = await mountPage()
    await wrapper.find('input#m-register-mobile').setValue('13800001111')
    await wrapper.find('input#m-register-alipay-account').setValue('synth-alipay@jab')
    await wrapper.findAll('input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.m-self-register__error').text()).toBe('请填写收款支付宝姓名（实名）')
    expect(mockedSubmit).not.toHaveBeenCalled()
  })

  it('一款游戏都没勾 ⇒ 提示带「游戏」字段名，不发请求', async () => {
    const wrapper = await mountPage()
    await wrapper.find('input#m-register-mobile').setValue('13800001111')
    await wrapper.find('input#m-register-alipay-account').setValue('synth-alipay@jab')
    await wrapper.find('input#m-register-alipay-name').setValue('测试打手甲')
    await wrapper.find('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.m-self-register__error').text()).toBe('请至少勾选一款游戏')
    expect(mockedSubmit).not.toHaveBeenCalled()
  })
})
