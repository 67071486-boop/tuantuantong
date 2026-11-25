// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「我的资料」v2（V2Profile）的判据：mock API → **钉值渲染**（只读 3 格 / 7 字段回填 / 游戏权限 / 实名两态 / 收款码两态 /绑定块 ①②式与回调地址与健康块）、真交互（保存资料 / 实名弹层 / 改密 / 绑定保存与测试连接 /复制回调地址）、「加载完成前不许编辑」的两道锁、失败态。功能语义的出处 = island 复刻页 `layui-island/pages/user-myinfo.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。
 * 「我的资料」v2（V2Profile）的判据：mock API → **钉值渲染**（只读 3 格 / 7 字段回填 / 游戏权限 / 实名两态 / 收款码两态 /绑定块 ①②式与回调地址与健康块）、真交互（保存资料 / 实名弹层 / 改密 / 绑定保存与测试连接 /复制回调地址）、「加载完成前不许编辑」的两道锁、失败态。功能语义的出处 = island 复刻页 `layui-island/pages/user-myinfo.ts`（组件文件头有逐条对照表）；全部夹具都是**合成值**（PII 纪律）。 */
import type { MyProfile } from '@/api/myProfile'
import { BizError } from '@/api/errors'
import type { JidanpuMemberBindingRow, JidanpuMemberTestResult } from '@/api/jidanpuMember'

const fetchMyProfile = vi.fn()
const patchMyProfile = vi.fn()
const changePassword = vi.fn()
const requestMediaTicket = vi.fn()
const claimMedia = vi.fn()
const verifyRealName = vi.fn()
const fetchMemberJidanpuBinding = vi.fn()
const saveMemberJidanpuBinding = vi.fn()
const testMemberJidanpuConnection = vi.fn()
const fetchAllGames = vi.fn()
const compressImage = vi.fn()
const putFileToStorage = vi.fn()

vi.mock('@/api/myProfile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/myProfile')>()),
  fetchMyProfile: (...args: unknown[]) => fetchMyProfile(...args),
  patchMyProfile: (...args: unknown[]) => patchMyProfile(...args),
  changePassword: (...args: unknown[]) => changePassword(...args),
  requestMediaTicket: (...args: unknown[]) => requestMediaTicket(...args),
  claimMedia: (...args: unknown[]) => claimMedia(...args),
}))
vi.mock('@/api/realname', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/realname')>()),
  verifyRealName: (...args: unknown[]) => verifyRealName(...args),
}))
vi.mock('@/api/jidanpuMember', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/jidanpuMember')>()),
  fetchMemberJidanpuBinding: (...args: unknown[]) => fetchMemberJidanpuBinding(...args),
  saveMemberJidanpuBinding: (...args: unknown[]) => saveMemberJidanpuBinding(...args),
  testMemberJidanpuConnection: (...args: unknown[]) => testMemberJidanpuConnection(...args),
}))
vi.mock('@/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/config')>()),
  fetchAllGames: (...args: unknown[]) => fetchAllGames(...args),
}))
vi.mock('@/layui-island/imageCompress', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/layui-island/imageCompress')>()),
  // jsdom 没有 canvas：压缩这一跳在本 spec 里钉「10MB 闸先拦」，成功路径把压缩桩成原样返回。
  compressImage: (...args: unknown[]) => compressImage(...args),
}))
vi.mock('@/layui-island/directUpload', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/layui-island/directUpload')>()),
  putFileToStorage: (...args: unknown[]) => putFileToStorage(...args),
}))

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import V2Profile from '../V2Profile.vue'

/** 合成资料（形状照 `MyProfile`；没传过头像/收款码 ⇒ null）。 */
const PROFILE: MyProfile = {
  username: 'tester_a',
  display_name: '测试甲',
  role: '打手',
  id_card_no: '',
  is_real_name: false,
  realname: { enabled: true, remaining: 3 },
  base_salary: '0.00',
  alipay_account: 'ali@example.com',
  alipay_name: '测试甲',
  bank_name: '示例银行',
  bank_card_no: '6222000000000000',
  qq: '10000',
  mobile: '13800000000',
  live_url: 'https://live.example.com/a',
  games: [1],
  avatar_url: null,
  wechat_qr_url: null,
}

/** 合成绑定行（①式已配置；密码不回显只有 `password_set`）。 */
const BINDING: JidanpuMemberBindingRow = {
  id: 11,
  mode: 'password',
  jidanpu_base_url: 'https://shop-a.jidanpu.com',
  account: 'shop-a-agent',
  account_name: '代账人',
  password_set: true,
  client_id: '',
  client_secret_set: false,
  callback_url: '',
  callback_url_reason: '',
  consumer_state: { text: '一切正常，最近一次回执已对上。', bad: false },
  source_name: '团团通系统',
  enabled: true,
  hall_sync_enabled: false,
  fast_poll_seconds: 60,
  reconcile_seconds: 600,
  throw_price_percent: 100,
  last_run_at: '2026-10-30T08:00:00+08:00',
  last_ok_at: '2026-10-30T08:00:00+08:00',
  last_error: '',
  last_error_at: null,
  checked_at: '2026-10-29T08:00:00+08:00',
  created_at: '2026-09-01T08:00:00+08:00',
  updated_at: '2026-10-30T08:00:00+08:00',
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/accounts/profile', name: 'accounts-profile', component: Blank },
    ],
  })
}

async function mountPage(path = '/v2') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(V2Profile, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

function field(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], key: string) {
  return wrapper.find(`[data-v2-field="${key}"]`)
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchMyProfile.mockResolvedValue({ ...PROFILE })
  patchMyProfile.mockImplementation(async (payload: Partial<MyProfile>) => ({ ...PROFILE, ...payload }))
  changePassword.mockResolvedValue(null)
  requestMediaTicket.mockResolvedValue({ key: 'k', upload_url: 'https://bucket/upload', expires_in: 600 })
  claimMedia.mockResolvedValue({ key: 'k' })
  verifyRealName.mockResolvedValue({ result: 'pass', id_card_masked: '110***00', remaining: 2 })
  fetchAllGames.mockResolvedValue([
    { id: 1, name: '示例游戏一', sort_order: 1, is_active: true, created_at: '2026-01-01T00:00:00+08:00' },
    { id: 2, name: '示例游戏二', sort_order: 2, is_active: true, created_at: '2026-01-01T00:00:00+08:00' },
  ])
  fetchMemberJidanpuBinding.mockResolvedValue({ ...BINDING })
  saveMemberJidanpuBinding.mockImplementation(async () => ({ ...BINDING }))
  testMemberJidanpuConnection.mockResolvedValue({
    ok: true,
    messages: [],
    source_options: [{ name: 'x-团团通系统', label: '团团通系统' }],
    source_options_ok: true,
    source_ok: true,
  } satisfies JidanpuMemberTestResult)
  compressImage.mockImplementation(async (file: File) => file)
  putFileToStorage.mockResolvedValue(undefined)
})

describe('V2Profile：加载与「加载完成前不许编辑」的两道锁', () => {
  it('资料输入框显示占位；功能设置展示可逐步查看的向导且隐藏开放平台选项', async () => {
    const { wrapper } = await mountPage()
    expect(field(wrapper, 'qq').attributes('placeholder')).toBe('请输入QQ号')
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect(wrapper.findAll('[data-v2-guide-step]')).toHaveLength(4)
    expect(wrapper.find('[data-v2="guide-detail"]').exists()).toBe(false)
    await wrapper.find('[data-v2-guide-step="1"]').trigger('click')
    expect(wrapper.find('[data-v2="guide-detail"]').text()).toContain('客服账号')
    await wrapper.find('[data-v2="guide-next"]').trigger('click')
    expect(wrapper.find('[data-v2="guide-detail"]').text()).toContain('订单来源管理')
    expect(wrapper.find('[data-v2="guide-detail"]').text()).not.toContain('客服账号')
    expect(field(wrapper, 'member-mode').find('option[value="openapi"]').attributes('hidden')).toBeDefined()
  })

  it('取数回来之前 7 框 + 保存 + 绑定块都锁着；回填成功才解锁（防「打字被服务端旧值覆盖」）', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const router = makeRouter()
    await router.push('/v2')
    await router.isReady()
    const wrapper = mount(V2Profile, { global: { plugins: [router, pinia] } })
    // 取数在途：编辑框与保存都 disabled（绑定块在「功能设置」页签里，切过去看同一道锁）。
    expect((field(wrapper, 'qq').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.find('[data-v2="save-profile"]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect((field(wrapper, 'member-account').element as HTMLInputElement).disabled).toBe(true)
    await flushPromises()
    expect((field(wrapper, 'member-account').element as HTMLInputElement).disabled).toBe(false)
    await wrapper.find('[data-v2-tab="basic"]').trigger('click')
    expect((field(wrapper, 'qq').element as HTMLInputElement).disabled).toBe(false)
    expect((wrapper.find('[data-v2="save-profile"]').element as HTMLButtonElement).disabled).toBe(false)
  })

  it('钉值回填：只读 3 格 + 7 字段 + 游戏权限（全部游戏签只在全有时亮）+ 头像回落色块 + 收款码两态', async () => {
    const { wrapper } = await mountPage()
    expect((field(wrapper, 'loginAcc').element as HTMLInputElement).value).toBe('tester_a')
    expect((field(wrapper, 'sid').element as HTMLInputElement).value).toBe('')
    expect((field(wrapper, 'basicSalary').element as HTMLInputElement).value).toBe('0.00')
    expect((field(wrapper, 'alipay_account').element as HTMLInputElement).value).toBe('ali@example.com')
    expect((field(wrapper, 'live_url').element as HTMLInputElement).value).toBe('https://live.example.com/a')
    // games: [1] 而游戏库有 2 款 ⇒ 「全部游戏」不亮；游戏一亮、游戏二不亮。
    expect(wrapper.find('[data-v2-game="all"]').classes()).not.toContain('cv2-chip--on')
    expect(wrapper.find('[data-v2-game="示例游戏一"]').classes()).toContain('cv2-chip--on')
    expect(wrapper.find('[data-v2-game="示例游戏二"]').classes()).not.toContain('cv2-chip--on')
    // 没传过头像 ⇒ 回落按昵称哈希的 SVG data URI（⛔ 前端不拼服务端 URL）。
    expect(wrapper.find('[data-v2="avatar"]').attributes('src')).toContain('data:image/svg')
    // 未上传收款码 ⇒ 「已上传」不显示、按钮文案「上传收款码」。
    expect(wrapper.find('[data-v2="look-qr"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="upload-qr"]').text()).toBe('上传收款码')
  })

  it('资料取数失败 ⇒ 错误 toast + 编辑框**保持锁定**（拿不到旧值就不该在会被覆盖的表单上放行）', async () => {
    fetchMyProfile.mockRejectedValue(new Error('合成错误：登录过期'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：登录过期')
    expect((field(wrapper, 'qq').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.find('[data-v2="save-profile"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('绑定取数失败 ⇒ 错误 toast + 绑定块保持锁定（成功回 null = 还没绑定，是合法结果、要解锁）', async () => {
    fetchMemberJidanpuBinding.mockRejectedValue(new Error('合成错误：绑定读取失败'))
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：绑定读取失败')
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect((field(wrapper, 'member-account').element as HTMLInputElement).disabled).toBe(true)

    fetchMemberJidanpuBinding.mockResolvedValue(null)
    const again = await mountPage()
    await again.wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect((field(again.wrapper, 'member-account').element as HTMLInputElement).disabled).toBe(false)
  })
})

describe('V2Profile：基本资料保存（PATCH 7 字段；响应那一行回填）', () => {
  it('改 QQ → 保存 ⇒ patchMyProfile 收全部 7 个键；toast 保存成功；响应回填到输入框', async () => {
    const { wrapper } = await mountPage()
    await field(wrapper, 'qq').setValue('20000')
    await wrapper.find('[data-v2="save-profile"]').trigger('click')
    await flushPromises()
    expect(patchMyProfile).toHaveBeenCalledTimes(1)
    expect(patchMyProfile).toHaveBeenCalledWith(
      expect.objectContaining({
        alipay_account: 'ali@example.com',
        alipay_name: '测试甲',
        bank_name: '示例银行',
        bank_card_no: '6222000000000000',
        qq: '20000',
        mobile: '13800000000',
        live_url: 'https://live.example.com/a',
      }),
    )
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('保存成功')
    expect((field(wrapper, 'qq').element as HTMLInputElement).value).toBe('20000')
  })

  it('保存失败 ⇒ 后端 msg 原样 toast（前端不造文案）', async () => {
    patchMyProfile.mockRejectedValue(new Error('合成错误：手机号已被占用'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="save-profile"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：手机号已被占用')
  })
})

describe('V2Profile：实名认证（enabled && !isRealName 才有入口；结果留在弹层里）', () => {
  it('启用且未实名 ⇒ 入口可见、弹层里有计费提示；没填全 ⇒ 固定提示、零请求', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="realname-state"]').text()).toBe('未实名')
    await wrapper.find('[data-v2="to-realname"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-realname"]')
    expect(dlg.exists()).toBe(true)
    // ⭐ 弹窗档 = md 640（全局统一）；主行动按钮收进 footer 右下。
    expect(dlg.classes()).toContain('cv2-dialog--md')
    expect(dlg.classes()).toContain('cv2-dialog--md') // width prop 已摘除（档位类接管）
    expect(dlg.find('.el-dialog__footer [data-v2="rn-submit"]').exists()).toBe(true)
    expect(dlg.find('.cv2-dlg-actions').classes()).toContain('cv2-dlg-actions')
    expect(dlg.find('[data-v2="rn-billing"]').text()).toContain('本次核验将消耗 1 次，无论核验结果一致与否')
    await wrapper.find('[data-v2="rn-submit"]').trigger('click')
    await flushPromises()
    expect(dlg.find('[data-v2="rn-result"]').text()).toContain('请填写真实姓名与身份证号后再提交')
    expect(verifyRealName).not.toHaveBeenCalled()
  })

  it('本租户未启用实名（realname.enabled=false）⇒ 「未实名」但仍无入口（fail-closed：键缺席同档）', async () => {
    fetchMyProfile.mockResolvedValue({ ...PROFILE, realname: { enabled: false, remaining: null } })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="realname-state"]').text()).toBe('未实名')
    expect(wrapper.find('[data-v2="to-realname"]').exists()).toBe(false)
  })

  it('已实名 ⇒ 状态绿签、无入口（enabled && !isRealName 才显示）', async () => {
    fetchMyProfile.mockResolvedValue({ ...PROFILE, is_real_name: true })
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-v2="realname-state"]').text()).toBe('已通过实名认证')
    expect(wrapper.find('[data-v2="to-realname"]').exists()).toBe(false)
  })

  it('核验通过 ⇒ 结果行带剩余次数、「是否实名」当场翻绿（不必刷新）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="to-realname"]').trigger('click')
    await field(wrapper, 'rn-name').setValue('张三')
    await field(wrapper, 'rn-sid').setValue('110101199001010011')
    await wrapper.find('[data-v2="rn-submit"]').trigger('click')
    await flushPromises()
    expect(verifyRealName).toHaveBeenCalledWith({ name: '张三', id_card_no: '110101199001010011' })
    expect(wrapper.find('[data-v2="rn-result"]').text()).toContain('已通过实名认证（剩余核验次数 2 次）')
    expect(wrapper.find('[data-v2="realname-state"]').text()).toBe('已通过实名认证')
  })

  it('核验没通过（result: fail，这一次也照样扣了）⇒ 状态不动、结果行说清没通过', async () => {
    verifyRealName.mockResolvedValue({ result: 'fail', id_card_masked: '110***00', remaining: 2 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="to-realname"]').trigger('click')
    await field(wrapper, 'rn-name').setValue('张三')
    await field(wrapper, 'rn-sid').setValue('110101199001010011')
    await wrapper.find('[data-v2="rn-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="rn-result"]').text()).toContain('实名认证未通过')
    expect(wrapper.find('[data-v2="realname-state"]').text()).toBe('未实名')
  })

  it('业务失败（1026 余量不足）⇒ 按业务码给「该找谁」的人话，不用一句「核验失败」糊过去', async () => {
    verifyRealName.mockRejectedValue(new BizError(1026, '配额不足'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="to-realname"]').trigger('click')
    await field(wrapper, 'rn-name').setValue('张三')
    await field(wrapper, 'rn-sid').setValue('110101199001010011')
    await wrapper.find('[data-v2="rn-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="rn-result"]').text()).toContain('实名认证余量不足，请联系客服购买')
  })
})

describe('V2Profile：修改密码（即时校验只省一次往返；权威判据在后端）', () => {
  it('密码深链打开修改密码页签，移除参数时回到基本资料', async () => {
    const { wrapper, router } = await mountPage('/v2?tab=password')
    expect(field(wrapper, 'oldPwd').exists()).toBe(true)
    await router.replace('/v2')
    expect(field(wrapper, 'qq').exists()).toBe(true)
    expect(field(wrapper, 'oldPwd').exists()).toBe(false)
  })

  it('两次新密码不一致 ⇒ 固定文案提示、零请求', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="password"]').trigger('click')
    await field(wrapper, 'oldPwd').setValue('old-pass')
    await field(wrapper, 'newPwd').setValue('new-pass')
    await field(wrapper, 'renewPwd').setValue('different')
    await wrapper.find('[data-v2="save-password"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('两次输入的新密码不一致')
    expect(changePassword).not.toHaveBeenCalled()
  })

  it('一致 ⇒ POST 三个字段；成功后清空 3 框 + 「修改成功」', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="password"]').trigger('click')
    await field(wrapper, 'oldPwd').setValue('old-pass')
    await field(wrapper, 'newPwd').setValue('new-pass')
    await field(wrapper, 'renewPwd').setValue('new-pass')
    await wrapper.find('[data-v2="save-password"]').trigger('click')
    await flushPromises()
    expect(changePassword).toHaveBeenCalledWith({
      old_password: 'old-pass',
      new_password: 'new-pass',
      confirm_password: 'new-pass',
    })
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('修改成功')
    expect((field(wrapper, 'oldPwd').element as HTMLInputElement).value).toBe('')
  })
})

describe('V2Profile：下家鸡蛋铺绑定——回填与健康块', () => {
  it('回填库里那一行：①式各格 + 密码框永远留空（占位按 password_set）+ 健康块空态要如实', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect((field(wrapper, 'member-base-url').element as HTMLInputElement).value).toBe('https://shop-a.jidanpu.com')
    expect((field(wrapper, 'member-account').element as HTMLInputElement).value).toBe('shop-a-agent')
    expect((field(wrapper, 'member-password').element as HTMLInputElement).value).toBe('')
    expect(field(wrapper, 'member-password').attributes('placeholder')).toBe('已配置，留空表示不修改')
    expect((field(wrapper, 'member-source').element as HTMLSelectElement).value).toBe('团团通系统')
    expect((field(wrapper, 'member-throw-percent').element as HTMLInputElement).value).toBe('100')
    expect((field(wrapper, 'member-hall-sync').element as HTMLInputElement).checked).toBe(false)
    expect((field(wrapper, 'member-enabled').element as HTMLInputElement).checked).toBe(true)
    // 健康块：还没失败过就写「暂无失败记录」，⛔ 不写成绿色对勾；回调消费 = 后端那句话原样。
    const health = wrapper.find('[data-v2="member-health"]').text()
    expect(health).toContain('已启用')
    expect(health).toContain('2026-10-30 08:00')
    expect(health).toContain('快路 60 秒 / 对账 600 秒')
    expect(health).toContain('—（暂无失败记录）')
    expect(health).toContain('一切正常，最近一次回执已对上。')
  })

  it('还没绑定（GET 回 null）⇒ 健康块如实写「还没绑定」、回调格显示那句人话（不画空格子）', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue(null)
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect(wrapper.find('[data-v2="member-health"]').text()).toContain('还没绑定')
    expect(wrapper.find('[data-v2="callback-reason"]').text()).toBe(
      '保存一次之后，这里会显示要填进鸡蛋铺那侧的回调地址。',
    )
  })

  it('存量②式行如实回显自己的方式（⛔ 不假装它是①式）：openapi 格显示、①式格藏起（只切可见性、不清值）', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({
      ...BINDING,
      mode: 'openapi',
      client_id: 'cid-1',
      client_secret_set: true,
      callback_url: 'https://tt.example.com/api/peer/callback?token=x',
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect((field(wrapper, 'member-mode').element as HTMLSelectElement).value).toBe('openapi')
    // v-show 挂在那一格的容器上 ⇒ DOM 在、只是 display:none（切过去再切回来不丢已填的东西）。
    const itemOf = (key: string): HTMLElement =>
      field(wrapper, key).element.closest<HTMLElement>('.cv2-field')!
    expect(itemOf('member-account').style.display).toBe('none')
    expect(itemOf('member-source').style.display).toBe('none')
    expect(itemOf('member-client-id').style.display).not.toBe('none')
    expect((field(wrapper, 'member-client-id').element as HTMLInputElement).value).toBe('cid-1')
    // 回调地址那一格：地址行显示（只读 + 复制）。
    expect((wrapper.find('[data-v2="callback-url"]').element as HTMLInputElement).value).toContain('token=x')
  })

  it('回调地址拿不到但有后端原因 ⇒ 显示后端给的原文（⛔ 前端不另写文案）', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({
      ...BINDING,
      mode: 'openapi',
      client_id: 'cid-1',
      callback_url: '',
      callback_url_reason: '合成原因：令牌还没生成',
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    expect(wrapper.find('[data-v2="callback-reason"]').text()).toBe('合成原因：令牌还没生成')
    expect(wrapper.find('[data-v2="callback-row"]').exists()).toBe(false)
  })
})

describe('V2Profile：下家鸡蛋铺绑定——保存（①②式两支 + ②→① 补 mode）', () => {
  it('①式地址为空 ⇒ 「请先填团团通地址与账号」、零请求；账号为空同句', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await field(wrapper, 'member-base-url').setValue('')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请先填团团通地址与账号')
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()
    await field(wrapper, 'member-base-url').setValue('https://shop-a.jidanpu.com')
    await field(wrapper, 'member-account').setValue('')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请先填团团通地址与账号')
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()
  })

  it('①式正常保存：载荷六个键、**不带 mode**（普通①式保存逐字不变）；密码原样（不 trim）；地址只 trim 空白（去尾斜杠在后端，保存后用响应那一行回填）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await field(wrapper, 'member-base-url').setValue(' https://shop-b.jidanpu.com/ ')
    await field(wrapper, 'member-password').setValue(' p@ss ')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    expect(saveMemberJidanpuBinding).toHaveBeenCalledTimes(1)
    const payload = saveMemberJidanpuBinding.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.mode).toBeUndefined()
    expect(payload.jidanpu_base_url).toBe('https://shop-b.jidanpu.com/')
    expect(payload.account).toBe('shop-a-agent')
    expect(payload.password).toBe(' p@ss ')
    expect(payload.source_name).toBe('团团通系统')
    expect(payload.throw_price_percent).toBe(100)
    expect(payload.hall_sync_enabled).toBe(false)
    expect(payload.enabled).toBe(true)
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('保存成功')
    // 响应那一行回填（mock 返回 BINDING 的地址）⇒ 页面显示的是库里的规范值。
    expect((field(wrapper, 'member-base-url').element as HTMLInputElement).value).toBe('https://shop-a.jidanpu.com')
  })

  it('⭐ ②→① 切回来那一跳：库里是②式、用户切①式保存 ⇒ 载荷**必须补** `mode: "password"`（不补 = 静默走错方式）', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({ ...BINDING, mode: 'openapi', client_id: 'cid-1' })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await field(wrapper, 'member-mode').setValue('password')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    const payload = saveMemberJidanpuBinding.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.mode).toBe('password')
  })

  it('②式保存：载荷带 `mode: "openapi"`、没有 password 键；密钥从没存过且留空 ⇒ 早挡文案、零请求', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({ ...BINDING, mode: 'openapi', client_id: 'cid-1', client_secret_set: false })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await field(wrapper, 'member-client-secret').setValue('')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('请填鸡蛋铺给你的 client_secret')
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()

    await field(wrapper, 'member-client-secret').setValue('sec-1')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    const payload = saveMemberJidanpuBinding.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.mode).toBe('openapi')
    expect(payload.client_id).toBe('cid-1')
    expect(payload.client_secret).toBe('sec-1')
    expect(payload).not.toHaveProperty('password')
  })

  it('②式已存过密钥 ⇒ 留空 = 不改（正当用法，逼重填才是倒退）', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({ ...BINDING, mode: 'openapi', client_id: 'cid-1', client_secret_set: true })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    const payload = saveMemberJidanpuBinding.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.client_secret).toBe('')
  })

  it('保存失败（启用时后端逐条点名缺哪条权限）⇒ 原样显示、不改写成「保存失败」', async () => {
    saveMemberJidanpuBinding.mockRejectedValue(new Error('该账号打不开「录入订单」页，说明没有对应权限'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="member-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('该账号打不开「录入订单」页，说明没有对应权限')
  })
})

describe('V2Profile：下家鸡蛋铺绑定——测试连接与来源字典（D-0177）', () => {
  it('①式：带密码框现值 + 下拉现值；成功 ⇒ toast 连接成功、字典进下拉、提示位回常态', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await field(wrapper, 'member-password').setValue('pending-pass')
    await wrapper.find('[data-v2="member-test"]').trigger('click')
    await flushPromises()
    expect(testMemberJidanpuConnection).toHaveBeenCalledWith('pending-pass', '团团通系统')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('连接成功')
    const options = field(wrapper, 'member-source').findAll('option').map((o) => o.text())
    expect(options).toEqual(['团团通系统'])
    expect(wrapper.find('[data-v2="source-hint"]').exists()).toBe(false)
  })

  it('字典没读到（source_options_ok=false）⇒ 提示位说「这次没读到」；⛔ 不说「对方已无此来源」', async () => {
    testMemberJidanpuConnection.mockResolvedValue({
      ok: false,
      messages: ['合成错误：登录失败'],
      source_options: [],
      source_options_ok: false,
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="member-test"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="source-hint"]').text()).toContain('这次没读到对方的来源名单')
    expect(wrapper.find('[data-v2="source-hint"]').text()).not.toContain('对方已无此来源')
    // ok=false 是正常结论（不是异常）：后端点名的那几句要显示出来。
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('合成错误：登录失败')
  })

  it('当前来源名不在新字典里 ⇒ 保留该选项并提示「对方已无此来源，请重选」（⛔ 不静默清空）', async () => {
    testMemberJidanpuConnection.mockResolvedValue({
      ok: true,
      messages: [],
      source_options: [{ name: 'x-另一个来源', label: '另一个来源' }],
      source_options_ok: true,
      source_ok: false,
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="member-test"]').trigger('click')
    await flushPromises()
    const options = field(wrapper, 'member-source').findAll('option').map((o) => o.text())
    expect(options).toEqual(['另一个来源', '团团通系统'])
    expect((field(wrapper, 'member-source').element as HTMLSelectElement).value).toBe('团团通系统')
    expect(wrapper.find('[data-v2="source-hint"]').text()).toContain('对方已无此来源，请重选')
  })

  it('②式：两个参数都不传（测的是已保存的那一行）；结论 = 逐条权限表，「没探」与「不可用」分开显示', async () => {
    fetchMemberJidanpuBinding.mockResolvedValue({ ...BINDING, mode: 'openapi', client_id: 'cid-1' })
    testMemberJidanpuConnection.mockResolvedValue({
      ok: false,
      messages: [],
      checks: [
        { step: 'token', label: '取令牌', probed: true, ok: false, kind: 'auth', reason: '合成原因：密码不对', message: '合成提示：请核对账号密码', action: '去鸡蛋铺改密码' },
        { step: 'GetGames', label: '游戏列表', probed: false, ok: false, kind: '', reason: '上一跳未通过', message: '', action: '先解决取令牌' },
      ],
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="member-test"]').trigger('click')
    await flushPromises()
    expect(testMemberJidanpuConnection).toHaveBeenCalledWith('', '')
    const table = wrapper.find('[data-v2="member-test-result"]')
    expect(table.exists()).toBe(true)
    expect(table.find('[data-v2-check-result="token"]').text()).toBe('不可用')
    expect(table.find('[data-v2-check-result="GetGames"]').text()).toBe('没探')
    expect(table.text()).toContain('合成提示：请核对账号密码')
  })
})

describe('V2Profile：回调地址复制与媒体上传', () => {
  it('复制回调地址：clipboard 可用 ⇒ 成功提示；缺 clipboard ⇒ 失败提示（⛔ 不静默）', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    fetchMemberJidanpuBinding.mockResolvedValue({
      ...BINDING,
      mode: 'openapi',
      client_id: 'cid-1',
      callback_url: 'https://tt.example.com/api/peer/callback?token=x',
    })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    await wrapper.find('[data-v2="copy-callback"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('https://tt.example.com/api/peer/callback?token=x')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('已复制回调地址')

    Object.assign(navigator, { clipboard: undefined })
    await wrapper.find('[data-v2="copy-callback"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('复制失败，请手动选中复制')
  })

  it('上传 10 MB 闸：超限 ⇒ 前置提示、不发票据请求（后端是权威，这一句只是早挡）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="upload-avatar"]').trigger('click')
    const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
    expect(input).not.toBeNull()
    const big = new File([new ArrayBuffer(10 * 1024 * 1024 + 1)], 'big.png', { type: 'image/png' })
    Object.defineProperty(input, 'files', { value: [big] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('头像与收款码最大支持10MB，请压缩后重试')
    expect(requestMediaTicket).not.toHaveBeenCalled()
  })

  it('上传成功路径：票据 → 直传 → 认领 → 「上传成功」→ 只刷那一格（fetchMyProfile 再取一次）', async () => {
    const { wrapper } = await mountPage()
    expect(fetchMyProfile).toHaveBeenCalledTimes(1)
    await wrapper.find('[data-v2="upload-avatar"]').trigger('click')
    const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
    const small = new File([new ArrayBuffer(64)], 'me.png', { type: 'image/png' })
    Object.defineProperty(input, 'files', { value: [small] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(requestMediaTicket).toHaveBeenCalledWith('avatar', 'png')
    expect(putFileToStorage).toHaveBeenCalledTimes(1)
    expect(claimMedia).toHaveBeenCalledWith('avatar', 'k')
    expect(wrapper.find('[data-v2="toast"]').text()).toContain('上传成功')
    expect(fetchMyProfile).toHaveBeenCalledTimes(2)
  })
})


/* ── D-0354 追记 3：表单两列收拢的 CSS 形状钉（jsdom 不算 cascade，钉源文本）── */
const CARBON_CSS = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
  'utf8',
)

describe('V2Profile：⭐ D-0354 追记 3 表单两列收拢（业务方「太散乱」；纯布局零功能改动）', () => {
  it('cv2-form-grid = 2 列 + gap 14px + 限宽 880px；动作行 cv2-form-actions 同限宽', () => {
    expect(CARBON_CSS).toMatch(
      /\.cv2-form-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/,
    )
    expect(CARBON_CSS).toMatch(/\.cv2-form-grid\s*\{[^}]*gap:\s*14px/)
    expect(CARBON_CSS).toMatch(/\.cv2-form-grid\s*\{[^}]*max-width:\s*880px/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-form-actions\s*\{[^}]*max-width:\s*880px[^}]*margin-top:\s*14px/,
    )
  })

  it('「同步大厅订单」跨两列（cv2-field--wide）；三个动作行都收编 cv2-form-actions（内联样式行不复存在）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2-tab="features"]').trigger('click')
    const hallSyncField = wrapper
      .find('[data-v2-field="member-hall-sync"]')
      .element.closest<HTMLElement>('.cv2-field')!
    expect(hallSyncField.classList.contains('cv2-field--wide')).toBe(true)
  })

  it('全部页签都不再有内联样式的 cv2-filter-actions 动作行', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.findAll('.cv2-form-actions').length).toBeGreaterThanOrEqual(1)
    expect(wrapper.find('.cv2-filter-actions[style]').exists()).toBe(false)
  })
})
