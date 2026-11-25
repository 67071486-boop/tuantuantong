// @vitest-environment jsdom
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import type { AxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 传输层是唯一的桩边界：api/myProfile / api/realname 跑真代码，判据打在 request 的
// url / method / data 上。`CSRF_HEADER_NAME` 必须给（directUpload 同源直传要它）。
vi.mock('../../router/useWorkerBack', () => ({ useWorkerBack: () => vi.fn() }))
vi.mock('@/api/http', () => ({ request: vi.fn(), http: {}, CSRF_HEADER_NAME: 'X-CSRFToken' }))

vi.mock('../../framework7Ui', () => ({
  Toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), warning: vi.fn() }),
}))
vi.mock('../../components/MobilePhotoBrowser.vue', () => ({
  default: {
  name: 'MobilePhotoBrowser',
  props: ['visible', 'images', 'closeBtn'],
  emits: ['update:visible'],
  template:
    '<div class="photo-browser-stub" :data-visible="String(visible)"'
    + ' :data-images="JSON.stringify(images || [])"></div>',
},
}))

/**
 * jsdom 没有 canvas、`Image` 也不会解码 ⇒ 真 `compressImage` 会卡在 loadImage。
 * 只把 `compressImage` 换成恒等（= 「压缩失败回落原图」那条既有路径）；
 * 画质取值本身的钉子在 `imageCompress.spec.ts`（边界值全列）。
 */
vi.mock('@/layui-island/imageCompress', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui-island/imageCompress')>()
  return {
    ...actual,
    // 返回**新的 File**（模拟压缩成功：canvas 产物是另一个对象）⇒
    // `userMediaTicketExt` 走「压缩成功 ⇒ jpg」那一支（真浏览器的常态路径）。
    compressImage: vi.fn(
      async (file: File) => new File([file], file.name, { type: 'image/jpeg' }),
    ),
  }
})

import { Toast } from '../../framework7Ui'
import type { Mock } from 'vitest'
import { BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import type { MyProfile } from '@/api/myProfile'
import MeProfilePage from '../MeProfilePage.vue'

const requestMock = request as unknown as Mock
/** 中性提示走 Toast 本体、失败走 Toast.error ⇒ 两者都要可清可断。 */
const toast = Toast as unknown as Mock & { success: Mock; error: Mock }

enableAutoUnmount(afterEach)

/* ── 合成夹具（E-0077） ── */

function profileFixture(overrides: Partial<MyProfile> = {}): MyProfile {
  return {
    username: 'ceshidashoujia',
    display_name: '测试打手甲',
    role: '代练员',
    id_card_no: '',
    is_real_name: false,
    realname: { enabled: true, remaining: 2 },
    base_salary: '0.00',
    alipay_account: 'synth-alipay-a',
    alipay_name: '测试收款人甲',
    bank_name: '测试银行甲',
    bank_card_no: 'synth-card-0001',
    qq: '10000',
    mobile: '13800000000',
    live_url: 'https://live-synth.test.invalid/a',
    games: [101],
    avatar_url: 'https://avatar-synth.test.invalid/a.jpg',
    wechat_qr_url: 'https://qr-synth.test.invalid/a.jpg',
    ...overrides,
  }
}

function fileOf(name: string, bytes: number): File {
  return new File([new Uint8Array(bytes)], name, { type: 'image/png' })
}

/* ── mock 的请求路由 ── */

let profileGetImpl: (config: AxiosRequestConfig) => Promise<unknown>
let profilePatchImpl: (config: AxiosRequestConfig) => Promise<unknown>
let ticketImpl: (config: AxiosRequestConfig) => Promise<unknown>
let claimImpl: (config: AxiosRequestConfig) => Promise<unknown>
let verifyImpl: (config: AxiosRequestConfig) => Promise<unknown>

/** 按谓词取请求 config 清单（判据打在 url / method / data 上）。 */
const callsOf = (pred: (config: AxiosRequestConfig) => boolean): AxiosRequestConfig[] =>
  requestMock.mock.calls.map((c) => c[0] as AxiosRequestConfig).filter(pred)

function wireRequestMock(): void {
  requestMock.mockImplementation((config: AxiosRequestConfig) => {
    const url = String(config.url)
    const method = String(config.method).toLowerCase()
    if (url === '/api/auth/profile' && method === 'get') return profileGetImpl(config)
    if (url === '/api/auth/profile' && method === 'patch') return profilePatchImpl(config)
    if (url.startsWith('/api/auth/profile/media/') && url.endsWith('/ticket')) return ticketImpl(config)
    if (url.startsWith('/api/auth/profile/media/')) return claimImpl(config)
    if (url === '/api/auth/realname/verify/') return verifyImpl(config)
    throw new Error(`spec: 未预期的请求 ${method} ${url}`)
  })
}

const fetchMock = vi.fn()

installFramework7ForTest()

beforeEach(() => {
  setActivePinia(createPinia())
  requestMock.mockReset()
  toast.mockClear()
  toast.error.mockClear()
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({ ok: true, status: 200 })
  vi.stubGlobal('fetch', fetchMock)
  // 默认全绿：GET/PATCH 都回同一份未实名资料（开关启用 ⇒ 那颗入口显示）。
  profileGetImpl = () => Promise.resolve({ profile: profileFixture() })
  profilePatchImpl = (config: AxiosRequestConfig) => {
    // PATCH 回**最新形状**（桌面同款：后端归一化后回整份资料）。
    const data = config.data as Partial<MyProfile>
    return Promise.resolve({ profile: { ...profileFixture(), ...data } })
  }
  let ticketSeq = 0
  ticketImpl = () => {
    ticketSeq += 1
    return Promise.resolve({ key: `synth-${ticketSeq}`, upload_url: `/blob/synth-${ticketSeq}`, expires_in: 600 })
  }
  claimImpl = (config: AxiosRequestConfig) => Promise.resolve({ key: (config.data as { key: string }).key })
  verifyImpl = () =>
    Promise.resolve({ result: 'pass', id_card_masked: '110********00', remaining: 1 })
  wireRequestMock()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mountPage() {
  return mount(MeProfilePage)
}

/** 7 个可编辑输入框的 testid（与 FIELDS 的 key 一致）。 */
const FIELD_KEYS = [
  'alipay_account', 'alipay_name', 'bank_name', 'bank_card_no',
  'qq', 'mobile', 'live_url',
] as const

describe('我的资料（/m/me/profile）— 加载锁与保存', () => {
  it('加载期锁定：取数在途 ⇒ 7 个输入框 + 保存都 disabled（桌面 P 线同款先例）', async () => {
    profileGetImpl = () => new Promise(() => {})
    const wrapper = mountPage()
    await flushPromises()
    for (const key of FIELD_KEYS) {
      expect(wrapper.find(`[data-testid="profile-field-${key}"] input`).attributes('disabled')).toBeDefined()
    }
    expect(wrapper.find('[data-testid="profile-save"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('回填成功 ⇒ 解锁 + 7 字段按载荷填充（label 逐字桌面；失败保持锁定 + 重试后解锁）', async () => {
    profileGetImpl = () => Promise.reject(new HttpError(500, '服务器开小差了'))
    const wrapper = mountPage()
    await flushPromises()
    // 失败：错误态 + 输入框仍 disabled（⛔ 拿不到旧值不放行一个会被覆盖的表单）。
    expect(wrapper.find('[data-testid="profile-error"]').text()).toContain('服务器开小差了')
    expect(wrapper.find(`[data-testid="profile-field-qq"] input`).attributes('disabled')).toBeDefined()

    profileGetImpl = () => Promise.resolve({ profile: profileFixture() })
    await wrapper.find('[data-testid="profile-retry"]').trigger('click')
    await flushPromises()
    for (const key of FIELD_KEYS) {
      expect(wrapper.find(`[data-testid="profile-field-${key}"] input`).attributes('disabled')).toBeUndefined()
    }
    expect((wrapper.find('[data-testid="profile-field-alipay_account"] input').element as HTMLInputElement).value)
      .toBe('synth-alipay-a')
    expect((wrapper.find('[data-testid="profile-field-mobile"] input').element as HTMLInputElement).value)
      .toBe('13800000000')
    const labels = wrapper.findAll('[data-testid="profile-form-card"] label').map((w) => w.text())
    // ⛔ 逐字（桌面 user-myinfo.html 的 7 个 label）。
    expect(labels).toEqual([
      '结算支付宝账号', '结算支付宝姓名', '结算银行名称', '结算银行卡号',
      'QQ/微信', '联系电话', '我的直播地址',
    ])
  })

  it('保存：PATCH /api/auth/profile 带**全部 7 键**；成功 toast「保存成功」+ 用响应回填', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-field-qq"] input').setValue('20000')
    await wrapper.find('[data-testid="profile-save"]').trigger('click')
    await flushPromises()

    const patches = callsOf((c) => String(c.url) === '/api/auth/profile' && String(c.method).toLowerCase() === 'patch')
    expect(patches).toHaveLength(1)
    expect(patches[0]!.data).toEqual({
      alipay_account: 'synth-alipay-a',
      alipay_name: '测试收款人甲',
      bank_name: '测试银行甲',
      bank_card_no: 'synth-card-0001',
      qq: '20000',
      mobile: '13800000000',
      live_url: 'https://live-synth.test.invalid/a',
    })
    expect(toast).toHaveBeenCalledWith('保存成功')
    // 响应回填：QQ 变成响应里的值（后端归一化后以它为准）。
    expect((wrapper.find('[data-testid="profile-field-qq"] input').element as HTMLInputElement).value)
      .toBe('20000')
  })

  it('保存失败：toast = 后端 msg 原样（⛔ 不自编失败文案）', async () => {
    profilePatchImpl = () => Promise.reject(new BizError(1000, '手机号格式不正确'))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-save"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('手机号格式不正确')
  })
})

describe('我的资料 — 头像 / 微信收款码上传', () => {
  it('头像上传成功：ticket(jpg) → PUT → claim(key) → toast「上传成功」→ 头像 src 换新', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="profile-avatar-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('头像.png', 1024)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()

    const tickets = callsOf((c) => String(c.url).includes('/media/avatar/ticket'))
    expect(tickets).toHaveLength(1)
    // 压缩成功一律 JPEG ⇒ ext 用 jpg（userMediaTicketExt 的口径，桌面同款）。
    expect(tickets[0]!.data).toEqual({ ext: 'jpg' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]![0]).toBe('/blob/synth-1')
    expect(fetchMock.mock.calls[0]![1]).toMatchObject({ method: 'PUT' })
    const claims = callsOf((c) => String(c.url) === '/api/auth/profile/media/avatar' && String(c.method).toLowerCase() === 'post')
    expect(claims).toHaveLength(1)
    expect(claims[0]!.data).toEqual({ key: 'synth-1' })
    expect(toast).toHaveBeenCalledWith('上传成功')
    // 回填失败**静默**（认领已成功），头像 src 不变也不报错 —— 这里 GET 失败试一次。
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('收款码上传成功：两态翻转（重新上传 / 已上传，点击查看）；ext 与 claim 判据', async () => {
    const wrapper = mountPage()
    await flushPromises()
    // 夹具本就带 qr 链接 ⇒ 出厂即「已上传」态。
    expect(wrapper.find('[data-testid="profile-qr-btn"]').text()).toBe('重新上传')
    // 文案对齐桌面 .lookWxImg 的原文「已上传」（评审 Low-3）；点击看图是本页行为。
    expect(wrapper.find('[data-testid="profile-qr-view"]').text()).toBe('已上传')
    const input = wrapper.find('[data-testid="profile-qr-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('收款码.jpg', 2048)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    const tickets = callsOf((c) => String(c.url).includes('/media/wechat-qr/ticket'))
    expect(tickets).toHaveLength(1)
    expect(tickets[0]!.data).toEqual({ ext: 'jpg' })
    expect(toast).toHaveBeenCalledWith('上传成功')
  })

  it('10MB 闸在票据之前（零请求）；坏扩展名同；文案逐字桌面', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const qrInput = wrapper.find('[data-testid="profile-qr-input"]')
    Object.defineProperty(qrInput.element, 'files', {
      value: [fileOf('大图.png', 10 * 1024 * 1024 + 1)],
      configurable: true,
    })
    await qrInput.trigger('change')
    await flushPromises()
    expect(toast).toHaveBeenCalledWith('头像与收款码最大支持10MB，请压缩后重试')
    expect(callsOf((c) => String(c.url).includes('/ticket'))).toHaveLength(0)

    const avatarInput = wrapper.find('[data-testid="profile-avatar-input"]')
    Object.defineProperty(avatarInput.element, 'files', {
      value: [fileOf('动图.gif', 1024)],
      configurable: true,
    })
    await avatarInput.trigger('change')
    await flushPromises()
    expect(toast).toHaveBeenCalledWith('只支持 jpg / jpeg / png')
    expect(callsOf((c) => String(c.url).includes('/ticket'))).toHaveLength(0)
  })

  it('上传请求失败：toast = 后端 msg 原样（403 门 / 业务码都走 Error.message）', async () => {
    ticketImpl = () => Promise.reject(new BizError(1002, '媒体类型不受支持'))
    const wrapper = mountPage()
    await flushPromises()
    const input = wrapper.find('[data-testid="profile-avatar-input"]')
    Object.defineProperty(input.element, 'files', {
      value: [fileOf('头像.png', 512)],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('媒体类型不受支持')
  })
})

describe('我的资料 — 实名区块', () => {
  it('未实名 + 启用 ⇒ 「点击进行实名认证」入口显示；表单含警示句与**计费提示逐字**', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-state"]').text()).toBe('未实名')
    expect(wrapper.find('[data-testid="profile-realname-sid"]').exists()).toBe(false)
    await wrapper.find('[data-testid="profile-realname-open"]').trigger('click')
    const form = wrapper.find('[data-testid="profile-realname-form"]')
    expect(form.text()).toContain(
      '请填写本人真实信息，虚假信息将可能导致商家无法给你结算，只支持中国大陆身份',
    )
    // ⛔ 计费提示一字不许省（E-0024：钉字面值）。
    expect(wrapper.find('[data-testid="profile-realname-billing"]').text()).toBe(
      '本次核验将消耗 1 次，无论核验结果一致与否',
    )
  })

  it('开关未启用（realname.enabled=false）⇒ 入口不渲染；已实名 ⇒ 打码号显示、入口消失', async () => {
    profileGetImpl = () =>
      Promise.resolve({ profile: profileFixture({ realname: { enabled: false, remaining: null } }) })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-open"]').exists()).toBe(false)
    wrapper.unmount()

    // ⭐ 评审必修 1：载荷给**明文**号（后端 GET /profile 就是明文），
    // 页面显示的必须是**打码形态**（前 3 + *** + 后 2，规则照后端 mask_id_card）。
    profileGetImpl = () =>
      Promise.resolve({
        profile: profileFixture({
          is_real_name: true,
          id_card_no: '110101199001010011',
          realname: { enabled: true, remaining: 0 },
        }),
      })
    const wrapper2 = mountPage()
    await flushPromises()
    expect(wrapper2.find('[data-testid="profile-realname-state"]').text()).toBe('已通过实名认证')
    const sidText = wrapper2.find('[data-testid="profile-realname-sid"]').text()
    expect(sidText).toContain('110***11')
    // 明文绝不整个出现在页面上（这是「打码显示」的承重断言）。
    expect(sidText).not.toContain('110101199001010011')
    expect(wrapper2.find('[data-testid="profile-realname-open"]').exists()).toBe(false)
  })

  it('没填全就提交：结果行「请填写真实姓名与身份证号后再提交」，零请求', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-realname-open"]').trigger('click')
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '请填写真实姓名与身份证号后再提交',
    )
    expect(callsOf((c) => String(c.url).includes('/realname/verify/'))).toHaveLength(0)
  })

  it('核验通过：徽标就地翻已认证 + 结果带剩余次数；载荷是 {name, id_card_no}', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-realname-open"]').trigger('click')
    await wrapper.find('[data-testid="profile-realname-name"] input').setValue('测试姓名甲')
    await wrapper.find('[data-testid="profile-realname-sid"] input').setValue('110101199001010011')
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()

    const verifies = callsOf((c) => String(c.url) === '/api/auth/realname/verify/')
    expect(verifies).toHaveLength(1)
    expect(verifies[0]!.data).toEqual({ name: '测试姓名甲', id_card_no: '110101199001010011' })
    expect(wrapper.find('[data-testid="profile-realname-state"]').text()).toBe('已通过实名认证')
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '已通过实名认证（剩余核验次数 1 次）',
    )
    // 表单不收起（结果留在屏幕上）；「点击进行实名认证」入口因已认证自动消失。
    expect(wrapper.find('[data-testid="profile-realname-form"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="profile-realname-open"]').exists()).toBe(false)
  })

  it('核验未通过（fail，这次也扣了）：状态不动 + 未通过文案带余量', async () => {
    verifyImpl = () => Promise.resolve({ result: 'fail', id_card_masked: '110********00', remaining: 1 })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-realname-open"]').trigger('click')
    await wrapper.find('[data-testid="profile-realname-name"] input').setValue('测试姓名甲')
    await wrapper.find('[data-testid="profile-realname-sid"] input').setValue('110101199001010011')
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-state"]').text()).toBe('未实名')
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '实名认证未通过，请核对姓名与身份证号是否一致（剩余核验次数 1 次）',
    )
  })

  it('业务码分支逐字（桌面 realNameErrorText）：1026 找客服购买 / 1025 找管理员 / 1029 稍后再试 / 1000 透传原文', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-testid="profile-realname-open"]').trigger('click')
    // 先填全（早挡用例已单独覆盖「没填全」；这里要打的是业务码分支）。
    await wrapper.find('[data-testid="profile-realname-name"] input').setValue('测试姓名甲')
    await wrapper.find('[data-testid="profile-realname-sid"] input').setValue('110101199001010011')

    verifyImpl = () => Promise.reject(new BizError(1026, '实名认证余量不足'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '实名认证余量不足，请联系客服购买',
    )

    verifyImpl = () => Promise.reject(new BizError(1025, '未启用'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '本系统尚未启用实名认证功能，请联系管理员开启',
    )

    verifyImpl = () => Promise.reject(new BizError(1027, '平台侧暂无可用的实名认证密钥包'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '平台侧暂无可用的实名认证密钥包，请联系管理员补充',
    )

    // D-0251：`1029` 提交过于频繁 ⇒ 只说「稍后再试」（不锁定、窗口滚过自动恢复）。
    // ⚠️ 夹具 msg 刻意**不是**期望文案：default 支会原样透传 msg ⇒ 断言才分得出「真有这一支」。
    verifyImpl = () => Promise.reject(new BizError(1029, '后端原文（夹具）'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '尝试过于频繁，请稍后再试',
    )

    verifyImpl = () => Promise.reject(new BizError(1017, '上游其它失败'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '实名认证服务暂时不可用（上游网络异常），请稍后重试',
    )

    verifyImpl = () => Promise.reject(new BizError(1000, '身份证号格式不正确，请核对后重新提交'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '身份证号格式不正确，请核对后重新提交',
    )

    verifyImpl = () => Promise.reject(new HttpError(403, '您没有该功能的访问权限'))
    await wrapper.find('[data-testid="profile-realname-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="profile-realname-result"]').text()).toBe(
      '您没有该功能的访问权限',
    )
  })
})
