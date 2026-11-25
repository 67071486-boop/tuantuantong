// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「企微自建应用」v2 页（`V2WecomSettings`，D-0379）的判据：
 * mock API → **钉值渲染**（配置回填不含 secret 原文、回调信息卡三样、验证时刻占位）+
 * **真交互**（保存组装 PUT 载荷：secret 族为空**不发键**＝留空不改；自检逐项渲染；
 * 测试发消息取输入框值）+ **权限门**（非账号管理员 fail-closed 白屏）+
 * **业务码分支**（未配置 1039 显示人话，远端 1040 原样显示）。
 * 全部夹具都是**合成值**（PII 纪律）；后端契约见 `api/wecom.ts` 头注。
 */
const getWecomConfig = vi.fn()
const putWecomConfig = vi.fn()
const runWecomSelfCheck = vi.fn()
const sendWecomTestMessage = vi.fn()

vi.mock('@/api/wecom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/wecom')>()),
  getWecomConfig: (...args: unknown[]) => getWecomConfig(...args),
  putWecomConfig: (...args: unknown[]) => putWecomConfig(...args),
  runWecomSelfCheck: (...args: unknown[]) => runWecomSelfCheck(...args),
  sendWecomTestMessage: (...args: unknown[]) => sendWecomTestMessage(...args),
}))

import V2WecomSettings from '../V2WecomSettings.vue'
import { useSessionStore } from '@/stores/session'
import { BizError } from '@/api/errors'

/** 合成配置行（形状 = `WecomConfig`；值全部合成）。 */
const CONFIG_ROW = {
  corp_id: 'wwspec0000synthetic',
  agent_id: 1000042,
  secret_masked: 'weco****cdef',
  contacts_secret_masked: 'weco****9abc',
  callback_token: 'spec-callback-token-synthetic',
  encoding_aes_key: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ',
  enabled: true,
  ready: true,
  contacts_ready: true,
  verified_at: null,
  callback_url: 'https://spec-tenant.example.chat/api/wecom/callback',
}

async function mountPage(): Promise<ReturnType<typeof mount>> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  session.user = {
    id: 42,
    username: 'wecom_admin_spec',
    display_name: '管理员甲',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: true,
    roles: [],
    role: '',
    permissions: [],
    menu_permissions: ['system.wecom_settings'],
    tenant_name: '示例工作室甲',
  }
  session.status = 'ready'
  const wrapper = mount(V2WecomSettings, { global: { plugins: [pinia], stubs: { V2WecomAftersales: true, V2WecomExternalPay: true } } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  getWecomConfig.mockReset().mockResolvedValue({ ...CONFIG_ROW })
  putWecomConfig.mockReset().mockResolvedValue({ ready: true })
  runWecomSelfCheck.mockReset()
  sendWecomTestMessage.mockReset().mockResolvedValue({ sent: true })
})

describe('V2WecomSettings', () => {
  it('Secret 和 EncodingAESKey 没有占据第二行的说明，保留输入与复制功能', async () => {
    const wrapper = await mountPage()
    const secret = wrapper.find('[data-v2="secret"]')
    const aes = wrapper.find('[data-v2="aes-key"]')
    const secretField = secret.element.closest('label')!
    const aesField = aes.element.closest('label')!
    expect(secretField.children).toHaveLength(2)
    expect(aesField.children).toHaveLength(2)
    expect(secret.attributes('placeholder')).toContain('留空')
    expect(wrapper.find('[data-v2="copy-aeskey"]').exists()).toBe(true)
  })

  it('配置回填：corp_id / agent_id / token / aes_key 逐字上屏；secret 族恒空（只写不读）', async () => {
    const wrapper = await mountPage()
    expect(getWecomConfig).toHaveBeenCalledTimes(1)
    expect(wrapper.find('v2-wecom-external-pay-stub').exists()).toBe(true)
    const corp = wrapper.find('[data-v2="corp-id"]').element as HTMLInputElement
    const agent = wrapper.find('[data-v2="agent-id"]').element as HTMLInputElement
    expect(corp.value).toBe(CONFIG_ROW.corp_id)
    expect(agent.value).toBe(String(CONFIG_ROW.agent_id))
    const token = wrapper.find('[data-v2="callback-token"]').element as HTMLInputElement
    expect(token.value).toBe(CONFIG_ROW.callback_token)
    const secret = wrapper.find('[data-v2="secret"]').element as HTMLInputElement
    expect(secret.value).toBe('') // ⛔ 原文不可能上屏（后端只回掩码、前端回填恒空）
    expect(wrapper.find('[data-v2="callback-url"]').text()).toBe(CONFIG_ROW.callback_url)
    expect(wrapper.find('[data-v2="verified-at"]').text()).toContain('还没验证过')
  })

  it('保存组装 PUT 载荷：凭据族为空就不发键（留空 = 不改）、填了才发且值逐字=输入框值', async () => {
    const wrapper = await mountPage()
    await wrapper.find('[data-v2="config-save"]').trigger('click')
    await flushPromises()
    expect(putWecomConfig).toHaveBeenCalledTimes(1)
    const payload = putWecomConfig.mock.calls[0]![0] as Record<string, unknown>
    expect(payload.corp_id).toBe(CONFIG_ROW.corp_id)
    expect(payload.agent_id).toBe(CONFIG_ROW.agent_id)
    expect(payload.enabled).toBe(true)
    // 回显的回调 Token / AESKey 原样随载荷回存（只读回填，非空 ⇒ 发键）。
    expect(payload.callback_token).toBe(CONFIG_ROW.callback_token)
    expect(payload.encoding_aes_key).toBe(CONFIG_ROW.encoding_aes_key)
    // secret 族只写不读 ⇒ 输入框恒空 ⇒ 键不存在（⛔ 空串不当「不改」：键不存在才是不改的可读判据）。
    expect('secret' in payload).toBe(false)
    expect('contacts_secret' in payload).toBe(false)
    expect(wrapper.find('[data-v2="config-result"]').text()).toContain('已保存')

    // 填了 secret ⇒ 键出现，值逐字 = 输入框值。
    await wrapper.find('[data-v2="secret"]').setValue('spec-new-secret-value')
    await wrapper.find('[data-v2="config-save"]').trigger('click')
    await flushPromises()
    const payload2 = putWecomConfig.mock.calls[1]![0] as Record<string, unknown>
    expect(payload2.secret).toBe('spec-new-secret-value')

    // ⭐ MF-1（评审必修）：callback_token / encoding_aes_key 是回调功能的唯一门槛——
    // 清空输入框 ⇒ 键不出现（后端非空=写入、空=不改正好对齐）；重新填写 ⇒ 键出现且逐字。
    await wrapper.find('[data-v2="callback-token"]').setValue('')
    await wrapper.find('[data-v2="aes-key"]').setValue('')
    await wrapper.find('[data-v2="config-save"]').trigger('click')
    await flushPromises()
    const payload3 = putWecomConfig.mock.calls[2]![0] as Record<string, unknown>
    expect('callback_token' in payload3).toBe(false)
    expect('encoding_aes_key' in payload3).toBe(false)

    await wrapper.find('[data-v2="callback-token"]').setValue('spec-token-refilled')
    await wrapper.find('[data-v2="aes-key"]').setValue(
      'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ',
    )
    await wrapper.find('[data-v2="config-save"]').trigger('click')
    await flushPromises()
    const payload4 = putWecomConfig.mock.calls[3]![0] as Record<string, unknown>
    expect(payload4.callback_token).toBe('spec-token-refilled')
    expect(payload4.encoding_aes_key).toBe('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ')
  })

  it('自检：逐项渲染 ok / 失败（人话原样）；自检失败信封走 toast 不白屏', async () => {
    const wrapper = await mountPage()
    runWecomSelfCheck.mockResolvedValue({
      main: { ok: true, skipped: false, message: 'token 获取成功' },
      contacts: { ok: false, skipped: false, message: '企微错误 60020：企业可信 IP 未配置' },
    })
    await wrapper.find('[data-v2="selfcheck-run"]').trigger('click')
    await flushPromises()
    expect(runWecomSelfCheck).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-v2="selfcheck-main"]').text()).toContain('通过：token 获取成功')
    expect(wrapper.find('[data-v2="selfcheck-contacts"]').text()).toContain('失败：企微错误 60020')
    expect(wrapper.find('[data-v2="selfcheck-contacts"]').classes()).toContain('wc-bad')
  })

  it('测试发消息：工具栏按钮开门后取输入框的值（E-0176 调用方载荷），成功与 1040 两个分支分开', async () => {
    const wrapper = await mountPage()
    // 视觉重做后输入框住在「测试发消息」弹窗里：先点工具栏按钮开门（EP 弹窗懒渲染）。
    await wrapper.find('[data-v2="test-send-open"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-v2="test-to-user"]').setValue('spec-userid-0001')
    await wrapper.find('[data-v2="test-content"]').setValue('测试内容甲')
    await wrapper.find('[data-v2="test-send"]').trigger('click')
    await flushPromises()
    expect(sendWecomTestMessage).toHaveBeenCalledWith('spec-userid-0001', '测试内容甲')
    expect(wrapper.find('[data-v2="test-result"]').text()).toContain('已发出')

    sendWecomTestMessage.mockRejectedValue(
      new BizError(1040, '企微错误 81013：核对接收人账号'),
    )
    await wrapper.find('[data-v2="test-send"]').trigger('click')
    await flushPromises()
    const result = wrapper.find('[data-v2="test-result"]')
    expect(result.classes()).toContain('wc-bad')
    expect(result.text()).toContain('81013')
  })

  it('未配置（1039）读配置失败 ⇒ 页面不炸（toast 说出人话）', async () => {
    getWecomConfig.mockRejectedValue(
      new BizError(1039, '企微应用还没配置：先填好凭据'),
    )
    const wrapper = await mountPage()
    await flushPromises()
    const toast = wrapper.find('[data-v2="toast"]')
    expect(toast.exists()).toBe(true)
    expect(toast.text()).toContain('先填好凭据')
  })

  it('非账号管理员 fail-closed：只有「无权限」一行，不发任何请求', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const session = useSessionStore()
    session.user = {
      id: 43,
      username: 'wecom_plain_spec',
      display_name: '客服乙',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      role: '',
      permissions: [],
      menu_permissions: [],
      tenant_name: '示例工作室甲',
    }
    session.status = 'ready'
    const wrapper = mount(V2WecomSettings, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.find('[data-v2="no-permission"]').text()).toBe('无权限管理企微自建应用')
    expect(getWecomConfig).not.toHaveBeenCalled()
  })
})
