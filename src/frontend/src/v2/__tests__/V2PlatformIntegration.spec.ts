// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「交易平台对接设置」v2（V2PlatformIntegration）的保真网：mock API → 钉值渲染（13 列恰好集合 /
 * 「暂未开通」徽标 / 支付密码两态）、行内开关 PATCH 单字段 + 成功也整表重取（默认开关降级同平台
 * 其他行）、字段随平台变形照 island platformFieldConfig（L171-212）、凭据红线（AppSecret/支付密码
 * 框永远留空，只在填了值时随载荷发）、9 组设置（Y/N 布尔 4 组 + 0/1 整数 5 组；只有 SK145/SK158
 * 「已生效」；保存钮读到真值前禁用）、确认框与校验文案逐字。文案出处 = island 复刻页
 * `system-tripartiteplatformsetting.ts` 与证据件 HTML（组件常量表有行号）；全部夹具都是**合成值**。
 */
const fetchPlatformIntegrations = vi.fn()
const createPlatformIntegration = vi.fn()
const updatePlatformIntegration = vi.fn()
const deletePlatformIntegration = vi.fn()
const fetchPlatformPushSettings = vi.fn()
const savePlatformPushSettings = vi.fn()

vi.mock('@/api/platformIntegrations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/platformIntegrations')>()),
  fetchPlatformIntegrations: (...args: unknown[]) => fetchPlatformIntegrations(...args),
  createPlatformIntegration: (...args: unknown[]) => createPlatformIntegration(...args),
  updatePlatformIntegration: (...args: unknown[]) => updatePlatformIntegration(...args),
  deletePlatformIntegration: (...args: unknown[]) => deletePlatformIntegration(...args),
  fetchPlatformPushSettings: (...args: unknown[]) => fetchPlatformPushSettings(...args),
  savePlatformPushSettings: (...args: unknown[]) => savePlatformPushSettings(...args),
}))

import V2PlatformIntegration from '../V2PlatformIntegration.vue'
import { useSessionStore } from '@/stores/session'
import type { PlatformIntegrationRow, PlatformPushSettings } from '@/api/platformIntegrations'

/* ── 合成夹具 ─────────────────────────────────────────────────────────────────── */

const ROW_JDT: PlatformIntegrationRow = {
  id: 11,
  platform: 10,
  platform_label: '代练通',
  account: 'syn-jdt-acc',
  app_id: 'syn-appid-001',
  has_secret: true,
  has_password: true,
  is_enabled: true,
  is_default: true,
  sync_accept: false,
  sms_notify: true,
  include_push: false,
  contact_qq: '10001',
  contact_phone: '13800000001',
  remark: '合成备注一',
  is_available: true,
  created_at: '2026-10-01T08:00:00+08:00',
  updated_at: '2026-10-01T08:00:00+08:00',
}

const ROW_MAMA: PlatformIntegrationRow = {
  ...ROW_JDT,
  id: 12,
  platform: 30,
  platform_label: '代练妈妈',
  account: 'syn-mama-acc',
  app_id: 'syn-mama-appid',
  has_secret: false,
  has_password: false,
  is_enabled: false,
  is_default: false,
  sync_accept: true,
  sms_notify: false,
  include_push: true,
  contact_qq: '',
  contact_phone: '',
  remark: '',
  is_available: false,
}

const SETTINGS: PlatformPushSettings = {
  quick_push_enabled: true,
  legacy_simple_mode: 0,
  check_game_region: 1,
  forbid_push_when_double_gold_zero: false,
  duplicate_push_check: 0,
  select_jidanpu_center_by_default: false,
  auto_zero_refund_on_conflict: true,
  block_push_completed_orders: 0,
  auto_send_group_qr_after_accept: 1,
  default_power_level: 1,
  default_limit_accept: null,
  require_template: '',
}

function makeRouter(): Router {
  const Blank = { template: '<div />' }
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', name: 'v2', component: Blank, children: [{ path: '', name: 'v2-home', component: Blank }] },
      { path: '/legacy-integration', name: 'system-platform-integration', component: Blank },
    ],
  })
}

function makeSession(permissions: string[]): void {
  const session = useSessionStore()
  session.user = {
    id: 9,
    username: 'tester_a',
    display_name: '测试甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: permissions,
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
}

async function mountPage(permissions: string[] = ['system.platform_integration_settings']) {
  const pinia = createPinia()
  setActivePinia(pinia)
  makeSession(permissions)
  const router = makeRouter()
  await router.push('/v2')
  await router.isReady()
  const wrapper = mount(V2PlatformIntegration, { global: { plugins: [router, pinia] } })
  await flushPromises()
  return { wrapper, router }
}

function toastOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']): string {
  const el = wrapper.find('[data-v2="toast"]')
  return el.exists() ? el.text() : ''
}

function rowOf(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], id: number) {
  return wrapper.find(`[data-v2-row="${id}"]`)
}

/** 开新增/编辑弹层里的平台 radio（value = 平台 id）。 */
async function choosePlatform(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], value: number) {
  await wrapper.find(`[data-v2-platform-opt="${value}"] input`).setValue(true)
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchPlatformIntegrations.mockResolvedValue([ROW_JDT, ROW_MAMA])
  fetchPlatformPushSettings.mockResolvedValue(SETTINGS)
})

describe('V2PlatformIntegration：权限门（fail-closed）', () => {
  it('无 system.platform_integration_settings ⇒ 「无权限查看本页」空态，不发任何请求', async () => {
    const { wrapper } = await mountPage([])
    expect(wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限查看本页')
    expect(fetchPlatformIntegrations).not.toHaveBeenCalled()
    expect(fetchPlatformPushSettings).not.toHaveBeenCalled()
  })
})

describe('V2PlatformIntegration：钉值渲染', () => {
  it('13 列表头恰好集合；两行渲染；计数句；页头引文块逐字', async () => {
    const { wrapper } = await mountPage()
    const headers = wrapper.findAll('.cv2-table thead th').map((t) => t.text().trim())
    expect(headers).toEqual([
      '平台名称', '平台账号/ID', 'AppId', '支付密码', '是否启用', '默认账号', '同步验收',
      '接单短信通知', '发单包含', '发单联系QQ', '发单联系电话', '备注说明', '操作',
    ])
    expect(wrapper.findAll('[data-v2-row]')).toHaveLength(2)
    expect(wrapper.find('[data-v2="count"]').text()).toBe('共 2 个对接账号')
    const quote = wrapper.find('[data-v2="page-quote"]').text()
    expect(quote).toContain('设置了以下各项，您就可以很方便的在系统里一键发单到各大交易平台，系统会自动同步代练平台的交易状态和交易信息。若您不会设置，我们的技术人员很乐意协助您设置。')
    expect(quote).toContain('允许相同平台添加多个发单账号')
    expect(wrapper.find('[data-v2="toolbar"]').text()).toContain('刷新列表')
    expect(wrapper.find('[data-v2="toolbar"]').text()).toContain('新增平台对接')
  })

  it('is_available=false 的行挂「暂未开通」徽标（title 逐字）；支付密码两态（对勾 title / 点击设置）', async () => {
    const { wrapper } = await mountPage()
    const badge = rowOf(wrapper, ROW_MAMA.id).find('[data-v2-cell="avail-badge-12"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('暂未开通')
    expect(badge.attributes('title')).toBe('该平台本版暂未开通：参数可以保存，但不会用于发单')
    expect(rowOf(wrapper, ROW_JDT.id).find('[data-v2-cell="avail-badge-11"]').exists()).toBe(false)
    const setCell = rowOf(wrapper, ROW_JDT.id).find('[data-v2-paypwd="11"]')
    expect(setCell.attributes('title')).toBe('已设置平台支付密码，点击可以更改')
    const emptyCell = rowOf(wrapper, ROW_MAMA.id).find('[data-v2-paypwd="12"]')
    expect(emptyCell.text()).toBe('点击设置')
    expect(emptyCell.attributes('title')).toBe('还没有设置平台支付密码，点击快速设置')
  })
})

describe('V2PlatformIntegration：5 颗行内开关（PATCH 单字段；成功也整表重取）', () => {
  it('关掉默认账号 ⇒ PATCH {is_default:false}，且列表整表重取（fetch 第 2 次）', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-switch="is_default-11"] input').setValue(false)
    await flushPromises()
    expect(updatePlatformIntegration).toHaveBeenCalledTimes(1)
    expect(updatePlatformIntegration).toHaveBeenCalledWith(ROW_JDT.id, { is_default: false })
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
  })

  it('PATCH 失败 ⇒ toast 后端原文 + 以服务端为准重取', async () => {
    updatePlatformIntegration.mockRejectedValueOnce(new Error('合成失败：开关保存被拒'))
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-switch="is_enabled-11"] input').setValue(false)
    await flushPromises()
    expect(toastOf(wrapper)).toBe('合成失败：开关保存被拒')
    expect(wrapper.find('[data-v2="toast"]').classes()).toContain('cv2-toast--error')
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
  })
})

describe('V2PlatformIntegration：新增/修改弹层（字段随平台变形）', () => {
  it('新增标题逐字；初始（未选平台）无 AppSecret 行；各平台的 label / 显隐照 island platformFieldConfig', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-form"]')
    expect(dlg.text()).toContain('新增一个代练平台对接')
    expect(dlg.text()).toContain('请联系我们售后人员帮您对接')
    // platform 0：AppSecret 行隐藏（照证据件初始 display:none），AppId 显示。
    expect(dlg.find('[data-v2-field="app-secret"]').exists()).toBe(false)
    expect(dlg.find('[data-v2-field="app-id"]').exists()).toBe(true)
    // 10 代练通：账号 label = 代练通用户ID。
    await choosePlatform(wrapper, 10)
    expect(wrapper.find('[data-v2="dlg-form"]').text()).toContain('代练通用户ID')
    // 30/80/90：商户标识（AppId 格）/ 商户密钥。
    await choosePlatform(wrapper, 30)
    let text = wrapper.find('[data-v2="dlg-form"]').text()
    expect(text).toContain('商户标识')
    expect(text).toContain('商户密钥')
    // 50 咕噜代练：平台账号 / 对接秘钥 / AppId 整格隐藏 / 平台登录密码格出现 + 尚未开放说明。
    await choosePlatform(wrapper, 50)
    const dlg50 = wrapper.find('[data-v2="dlg-form"]')
    text = dlg50.text()
    expect(text).toContain('平台登录密码')
    expect(text).toContain('「平台登录密码」尚未开放：本版不会保存这一格的内容。')
    expect(text).toContain('对接秘钥')
    expect(dlg50.find('[data-v2-field="app-id"]').exists()).toBe(false)
    expect(dlg50.find('[data-v2-field="login-password"]').exists()).toBe(true)
    // 93 天玩代练：User_Code / 对接密钥；无登录密码格。
    await choosePlatform(wrapper, 93)
    const dlg93 = wrapper.find('[data-v2="dlg-form"]')
    expect(dlg93.text()).toContain('User_Code')
    expect(dlg93.text()).toContain('对接密钥')
    expect(dlg93.find('[data-v2-field="login-password"]').exists()).toBe(false)
  })

  it('校验逐字：未选平台 ⇒ 请选择一个平台；平台 30 无 AppId ⇒ AppId不能为空（50 豁免）', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('请选择一个平台')
    expect(createPlatformIntegration).not.toHaveBeenCalled()
    await choosePlatform(wrapper, 30)
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('AppId不能为空')
    expect(createPlatformIntegration).not.toHaveBeenCalled()
    // 50 豁免 AppId（整格都没有），直接可存。
    await choosePlatform(wrapper, 50)
    await wrapper.find('[data-v2-field="account"]').setValue('syn-gulu-acc')
    createPlatformIntegration.mockResolvedValue({ ...ROW_JDT, id: 13, platform: 50, platform_label: '咕噜代练' })
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(createPlatformIntegration).toHaveBeenCalledTimes(1)
    expect(toastOf(wrapper)).toBe('已保存')
  })

  it('凭据红线：编辑行两个密码框永远留空；填了值才随载荷发，没填连键都不带', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-edit="11"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-form"]')
    expect(dlg.text()).toContain('编辑对接')
    const accInput = dlg.find('[data-v2-field="app-secret"]')
    const payInput = dlg.find('[data-v2-field="pay-password"]')
    expect((accInput.element as HTMLInputElement).value).toBe('')
    expect((payInput.element as HTMLInputElement).value).toBe('')
    expect((payInput.element as HTMLInputElement).placeholder).toBe('若没有改过，请不要动此处')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(updatePlatformIntegration).toHaveBeenCalledTimes(1)
    const sent = updatePlatformIntegration.mock.calls[0]?.[1] as Record<string, unknown>
    expect(sent).not.toHaveProperty('app_secret')
    expect(sent).not.toHaveProperty('pay_password')
    // 填了值 ⇒ 载荷带上；支付密码 placeholder 在编辑态是「若没有改过，请不要动此处」。
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-edit="11"]').trigger('click')
    await wrapper.find('[data-v2-field="app-secret"]').setValue('syn-new-secret')
    await wrapper.find('[data-v2-field="pay-password"]').setValue('  syn-pay-9  ')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    const sent2 = updatePlatformIntegration.mock.calls[1]?.[1] as Record<string, unknown>
    expect(sent2['app_secret']).toBe('syn-new-secret')
    expect(sent2['pay_password']).toBe('syn-pay-9')
  })

  it('保存成功（暂未开通平台）⇒ 已保存 + 条件后缀；填了平台登录密码 ⇒ 追加未提交说明', async () => {
    createPlatformIntegration.mockResolvedValue({ ...ROW_MAMA, id: 14 })
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    await choosePlatform(wrapper, 30)
    await wrapper.find('[data-v2-field="account"]').setValue('syn-mama-2')
    await wrapper.find('[data-v2-field="app-id"]').setValue('syn-mama-appid-2')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('已保存；该平台暂未开通，本版只保存参数')
    // 平台 50 + 平台登录密码填了值（照渲染不保存）⇒ 追加「尚未开放，未随本次保存提交」。
    await wrapper.find('[data-v2="add"]').trigger('click')
    await choosePlatform(wrapper, 50)
    await wrapper.find('[data-v2-field="account"]').setValue('syn-gulu-2')
    await wrapper.find('[data-v2-field="login-password"]').setValue('syn-login-pwd')
    createPlatformIntegration.mockResolvedValue({ ...ROW_JDT, id: 15, platform: 50, platform_label: '咕噜代练' })
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('已保存；「平台登录密码」尚未开放，未随本次保存提交')
    const sent = createPlatformIntegration.mock.calls[1]?.[0] as Record<string, unknown>
    expect(sent).not.toHaveProperty('login_password')
  })

  it('保存失败 ⇒ 保存失败：<后端原文>，弹层不关（可改后重试）', async () => {
    createPlatformIntegration.mockRejectedValue(new Error('合成失败：账号重复'))
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="add"]').trigger('click')
    await choosePlatform(wrapper, 10)
    await wrapper.find('[data-v2-field="account"]').setValue('syn-jdt-2')
    await wrapper.find('[data-v2-field="app-id"]').setValue('syn-appid-2')
    await wrapper.find('[data-v2="form-save"]').trigger('click')
    await flushPromises()
    expect(toastOf(wrapper)).toBe('保存失败：合成失败：账号重复')
    expect(wrapper.find('[data-v2="dlg-form"]').exists()).toBe(true)
  })
})

describe('V2PlatformIntegration：支付密码弹层 / 删除确认', () => {
  it('点「点击设置」⇒ 弹层标题逐字；空值拦截；成功 PATCH {pay_password} + 支付密码已设置', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_MAMA.id).find('[data-v2-paypwd="12"]').trigger('click')
    const dlg = wrapper.find('[data-v2="dlg-paypwd"]')
    expect(dlg.text()).toContain('请填充平台的支付密码')
    await wrapper.find('[data-v2="paypwd-save"]').trigger('click')
    expect(toastOf(wrapper)).toBe('支付密码不能为空')
    expect(updatePlatformIntegration).not.toHaveBeenCalled()
    await wrapper.find('[data-v2-field="new-pay-password"]').setValue('  syn-pay-12  ')
    await wrapper.find('[data-v2="paypwd-save"]').trigger('click')
    await flushPromises()
    expect(updatePlatformIntegration).toHaveBeenCalledWith(12, { pay_password: 'syn-pay-12' })
    expect(toastOf(wrapper)).toBe('支付密码已设置')
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
  })

  it('删除确认语与按钮逐字；「点错了」零请求；「确认删除」⇒ DELETE + 已删除 + 重取', async () => {
    const { wrapper } = await mountPage()
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-delete="11"]').trigger('click')
    expect(wrapper.find('[data-v2="confirm-text"]').text()).toBe(
      '是否确认删除这个对接账号？提示：删除此对接账号不会影响之前用这个账号发过单的交易操作',
    )
    await wrapper.find('[data-v2="confirm-cancel"]').trigger('click')
    expect(deletePlatformIntegration).not.toHaveBeenCalled()
    deletePlatformIntegration.mockResolvedValue(null)
    await rowOf(wrapper, ROW_JDT.id).find('[data-v2-delete="11"]').trigger('click')
    await wrapper.find('[data-v2="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(deletePlatformIntegration).toHaveBeenCalledWith(11)
    expect(toastOf(wrapper)).toBe('已删除')
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
  })
})

describe('V2PlatformIntegration：「对所有平台生效」9 组设置', () => {
  it('9 组恰好集合；逐组徽标：只有 SK145/SK158 是绿「已生效」，其余橙「仅保存」；声明与图例逐字', async () => {
    const { wrapper } = await mountPage()
    const panel = wrapper.find('[data-v2="settings-panel"]')
    expect(panel.text()).toContain('这里的设置项会针对所有平台生效')
    expect(panel.find('[data-v2="settings-notice"]').text()).toBe(
      '本版有两条已生效：「撞单自动 0 撤」与「平台接手后自动发群二维码」；其余开关仍只保存、不影响业务',
    )
    expect(panel.find('[data-v2="settings-legend"]').text()).toBe(
      '绿色「已生效」= 发单 / 同步链路会读它；橙色「仅保存」= 只写进系统，业务行为不变。逐组见下方标注。',
    )
    const sks = wrapper.findAll('[data-v2-settings-group]').map((el) => el.attributes('data-v2-settings-group'))
    expect([...new Set(sks)]).toEqual([
      'SK132', 'SK156', 'SK157', 'SK107', 'SK159', 'SK106', 'SK145', 'SK155', 'SK158',
    ])
    const badges = wrapper.findAll('[data-v2-settings-badge]')
    expect(badges).toHaveLength(9)
    const on = badges.filter((b) => b.classes().includes('cv2-st--settled')).map((b) => b.attributes('data-v2-settings-badge'))
    const off = badges.filter((b) => b.classes().includes('cv2-st--paused')).map((b) => b.attributes('data-v2-settings-badge'))
    expect(on).toEqual(['SK145', 'SK158'])
    expect(off).toEqual(['SK132', 'SK156', 'SK157', 'SK107', 'SK159', 'SK106', 'SK155'])
    expect(badges[0]!.text()).toBe('仅保存')
    expect(badges.find((b) => b.attributes('data-v2-settings-badge') === 'SK145')!.text()).toBe('已生效')
  })

  it('选项文案逐字（spot check）；读取后按后端值回填（布尔 → Y/N，0/1 → 原值）', async () => {
    const { wrapper } = await mountPage()
    const checkedOf = (key: string): boolean =>
      (wrapper.find(`[data-v2-settings-opt="${key}"] input`).element as HTMLInputElement).checked
    expect(wrapper.find('[data-v2-settings-opt="SK159:0"]').text()).toBe('检测')
    expect(wrapper.find('[data-v2-settings-opt="SK159:1"]').text()).toBe('不做检测')
    expect(wrapper.find('[data-v2-settings-opt="SK155:0"]').text()).toBe('不限制')
    expect(wrapper.find('[data-v2-settings-opt="SK107:Y"]').text()).toBe('禁止发单')
    expect(wrapper.find('[data-v2-settings-opt="SK107:N"]').text()).toBe('不限制')
    expect(wrapper.find('[data-v2-settings-opt="SK145:Y"]').text()).toBe('0撤')
    expect(checkedOf('SK132:Y')).toBe(true)
    expect(checkedOf('SK156:0')).toBe(true)
    expect(checkedOf('SK157:1')).toBe(true)
    expect(checkedOf('SK107:N')).toBe(true)
    expect(checkedOf('SK106:N')).toBe(true)
    expect(checkedOf('SK145:Y')).toBe(true)
    expect(checkedOf('SK158:1')).toBe(true)
    expect(checkedOf('SK155:0')).toBe(true)
    expect(checkedOf('SK159:0')).toBe(true)
  })

  it('读取完成前保存钮禁用 + 常驻提示逐字；读取完成后解禁', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    makeSession(['system.platform_integration_settings'])
    const router = makeRouter()
    await router.push('/v2')
    await router.isReady()
    const wrapper = mount(V2PlatformIntegration, { global: { plugins: [router, pinia] } })
    expect(wrapper.find('[data-v2="settings-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-v2="settings-hint"]').text()).toBe('设置还没读取完成，请稍后再试（或刷新页面）')
    await flushPromises()
    expect(wrapper.find('[data-v2="settings-save"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-v2="settings-hint"]').exists()).toBe(false)
  })

  it('保存载荷：Y/N 布尔 4 组 + 0/1 整数 5 组（不归一），9 键恰好；成功回填后端返回值 + 已保存', async () => {
    const { wrapper } = await mountPage()
    // 改两颗：SK132 关（Y→N）、SK158 停发（1→0）。
    await wrapper.find('[data-v2-settings-opt="SK132:N"] input').setValue(true)
    await wrapper.find('[data-v2-settings-opt="SK158:0"] input').setValue(true)
    const saved: PlatformPushSettings = { ...SETTINGS, quick_push_enabled: false, auto_send_group_qr_after_accept: 0 }
    savePlatformPushSettings.mockResolvedValue(saved)
    await wrapper.find('[data-v2="settings-save"]').trigger('click')
    await flushPromises()
    expect(savePlatformPushSettings).toHaveBeenCalledTimes(1)
    expect(savePlatformPushSettings).toHaveBeenCalledWith({
      quick_push_enabled: false,
      legacy_simple_mode: 0,
      check_game_region: 1,
      forbid_push_when_double_gold_zero: false,
      duplicate_push_check: 0,
      select_jidanpu_center_by_default: false,
      auto_zero_refund_on_conflict: true,
      block_push_completed_orders: 0,
      auto_send_group_qr_after_accept: 0,
    })
    expect(toastOf(wrapper)).toBe('已保存')
    // 回填**后端返回的那份**（不是刚发出去的本地值——两边恰好一致也照常成立）。
    expect((wrapper.find('[data-v2-settings-opt="SK132:N"] input').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.find('[data-v2-settings-opt="SK158:0"] input').element as HTMLInputElement).checked).toBe(true)
  })

  it('读设置失败 ⇒ 全清（9 组无任何选中）+ 错误 toast；保存钮保持禁用', async () => {
    fetchPlatformPushSettings.mockRejectedValue(new Error('合成失败：设置读取被拒'))
    const { wrapper } = await mountPage()
    expect(toastOf(wrapper)).toBe('合成失败：设置读取被拒')
    expect(wrapper.findAll('[data-v2-settings-opt] input:checked')).toHaveLength(0)
    expect(wrapper.find('[data-v2="settings-save"]').attributes('disabled')).toBeDefined()
  })
})

describe('V2PlatformIntegration：工具条', () => {
  it('刷新列表 ⇒ 重取 + 已刷新；取数失败态带 重试', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(fetchPlatformIntegrations).toHaveBeenCalledTimes(2)
    expect(toastOf(wrapper)).toBe('已刷新')
    fetchPlatformIntegrations.mockRejectedValue(new Error('合成失败：列表拉取被拒'))
    await wrapper.find('[data-v2="refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-v2="empty"]').text()).toContain('加载失败：合成失败：列表拉取被拒')
    expect(wrapper.find('[data-v2="retry"]').text()).toBe('重试')
  })
})
