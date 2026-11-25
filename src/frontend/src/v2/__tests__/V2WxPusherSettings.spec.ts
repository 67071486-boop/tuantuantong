// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import V2WxPusherSettings from '../V2WxPusherSettings.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

const endpoint = '/api/business/wxpusher-config/'
const systemUrl = 'https://workspace.example.test/api/auth/wxpusher/login'
const exampleUrl = 'https://workspace.example.test/m/orders/mine'
const existing = {
  configured: true,
  source: 'environment',
  app_id: '200123',
  subscribe_url: '',
  system_url: systemUrl,
  has_app_token: true,
  has_login_signing_key: true,
  push_enabled: true,
  login_enabled: true,
  binding_prompt_enabled: false,
  order_enabled: true,
  settlement_enabled: true,
  order_events: { assigned: true, unassigned: true, review_rejected: true, completed: true, paused_resumed: true, issue: true, refunded: true },
}
const pushControls = '[data-v2="push-controls"]'
const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset().mockImplementation(async ({ url, method, data }) => {
    if (url === endpoint) return method === 'PATCH'
      ? { ...existing, ...data, order_events: { ...existing.order_events, ...data.order_events }, source: 'tenant' }
      : existing
    if (url === `${endpoint}recipients/`) return { items: [{ id: 12, name: '测试打手' }] }
    if (url === `${endpoint}history/`) return { items: [{ id: 5, kind: 'test', title: '团团通测试消息', recipient: '测试打手', status: 'sent', created_at: '2026-11-16T01:30:47Z', attempted_at: null }] }
    if (url === `${endpoint}test/`) return { accepted: true }
    if (url === `${endpoint}preview/`) return { items: [{ kind: 'assigned', title: '收到新的订单指派', html: `演示订单<br><a href="${exampleUrl}">请前往查看</a>`, url: exampleUrl }] }
    throw new Error(`Unexpected request ${url}`)
  })
})

function openPage() {
  return mount(V2WxPusherSettings, {
    global: { stubs: { OrderTabDialog: { props: ['modelValue', 'title'], template: '<aside v-if="modelValue"><h2>{{ title }}</h2><slot/><slot name="footer"/></aside>' } } },
  })
}

describe('消息推送订阅', () => {
  it('沿用现有凭据，独立保存开关和订单子项', async () => {
    const wrapper = openPage()
    await flushPromises()
    expect(wrapper.text()).toContain('沿用现有配置')
    expect(wrapper.get('[aria-label="系统入口地址"]').element).toHaveProperty('value', systemUrl)
    expect(wrapper.get('#wxpusher-app-token').element).toHaveProperty('value', '')
    expect(wrapper.get('#wxpusher-login-key').element).toHaveProperty('value', '')
    await wrapper.get(`${pushControls} input[name="push_enabled"]`).trigger('click')
    await wrapper.get(`${pushControls} input[name="review_rejected"]`).trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const save = mockedRequest.mock.calls.find(([options]) => options.method === 'PATCH')?.[0]
    expect(save?.data).toEqual({ expected_app_id: '200123', push_enabled: false, order_events: { review_rejected: false } })
    expect(wrapper.text()).toContain('设置已保存')
    expect(wrapper.get(`${pushControls} input[name="login_enabled"]`).element).toHaveProperty('checked', true)
  })

  it('独立保存登录绑定提醒，关闭后仍可以使用原有的推送配置', async () => {
    const wrapper = openPage()
    await flushPromises()
    const prompt = wrapper.get(`${pushControls} input[name="binding_prompt_enabled"]`)
    expect(prompt.element).toHaveProperty('checked', false)
    expect(wrapper.get(`${pushControls} input[name="push_enabled"]`).element).toHaveProperty('checked', true)

    await prompt.trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const saves = () => mockedRequest.mock.calls.filter(([options]) => options.method === 'PATCH').map(([options]) => options.data)
    expect(saves()).toEqual([{ expected_app_id: '200123', binding_prompt_enabled: true }])
    expect(prompt.element).toHaveProperty('checked', true)

    await prompt.trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(saves()).toEqual([
      { expected_app_id: '200123', binding_prompt_enabled: true },
      { expected_app_id: '200123', binding_prompt_enabled: false },
    ])
    expect(prompt.element).toHaveProperty('checked', false)
    expect(wrapper.get(`${pushControls} input[name="push_enabled"]`).element).toHaveProperty('checked', true)
  })

  it('切换应用时刷新打手，忽略旧应用尚未返回的打手列表', async () => {
    const standardRequest = mockedRequest.getMockImplementation()!
    let finishOldRequest: (result: { items: { id: number; name: string }[] }) => void
    const oldRequest = new Promise<{ items: { id: number; name: string }[] }>((resolve) => { finishOldRequest = resolve })
    let requested = 0
    mockedRequest.mockImplementation(async (options) => {
      if (options.url === `${endpoint}recipients/`) {
        requested += 1
        return requested === 1 ? oldRequest : { items: [{ id: 21, name: '新应用打手' }] }
      }
      return standardRequest(options)
    })

    const wrapper = openPage()
    await flushPromises()
    await wrapper.get('input[inputmode="numeric"]').setValue('200124')
    await wrapper.get('#wxpusher-app-token').setValue('AT_synthetic_replacement')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    finishOldRequest!({ items: [{ id: 12, name: '旧应用打手' }] })
    await flushPromises()

    expect(wrapper.get('[aria-label="接收测试消息的打手"]').text()).toContain('新应用打手')
    expect(wrapper.text()).not.toContain('旧应用打手')
  })

  it('给选定打手发送测试消息，并区分平台受理与手机收件', async () => {
    const wrapper = openPage()
    await flushPromises()
    await wrapper.get('[aria-label="接收测试消息的打手"]').setValue('12')
    await wrapper.get('[data-v2="wxpusher-send-test"]').trigger('click')
    await flushPromises()
    expect(mockedRequest).toHaveBeenCalledWith({ url: `${endpoint}test/`, method: 'POST', data: { user_id: 12, expected_app_id: '200123' } })
    expect(wrapper.text()).toContain('WxPusher 已受理，请让打手确认手机是否收到。')
    expect(wrapper.get('tbody').text()).toContain('WxPusher 已受理')
    expect(wrapper.get('tbody').text()).toContain('2026-11-16 09:30:47')
  })

  it.each([
    ['assigned', 'not_bound', '接收人未绑定 WxPusher'],
    ['hall_new_order', 'hall_unavailable', '订单已离开大厅或接收人无抢单资格'],
    ['hall_new_order', 'hall_rule_changed', '订单不再符合订阅筛选'],
  ])('按订单号查询，显示 %s 的未发送原因 %s', async (kind, skipReason, expected) => {
    const original = mockedRequest.getMockImplementation()!
    mockedRequest.mockImplementation(async (options) => {
      if (options.url === `${endpoint}history/` && options.params?.order_no) {
        return { total: 1, items: [{
          id: 6, kind, title: '收到新的订单指派', order_no: 'ORD0086',
          recipient: '待绑定打手', status: 'skipped', skip_reason: skipReason,
          created_at: '2026-11-16T01:30:47Z', attempted_at: null,
        }] }
      }
      return original(options)
    })
    const wrapper = openPage()
    await flushPromises()
    await wrapper.get('[aria-label="订单号"]').setValue('ORD0086')
    await wrapper.get('[data-v2="wxpusher-history-filters"]').trigger('submit')
    await flushPromises()
    expect(wrapper.get('tbody').text()).toContain('ORD0086')
    expect(wrapper.get('tbody').text()).toContain('待绑定打手')
    expect(wrapper.get('tbody').text()).toContain(expected)
    expect(wrapper.get('tbody').text()).not.toContain('团团通测试消息')
  })

  it('按需加载模拟数据预览，并隔离通知正文', async () => {
    const wrapper = openPage()
    await flushPromises()
    expect(mockedRequest.mock.calls.some(([{ url }]) => url === `${endpoint}preview/`)).toBe(false)
    await wrapper.get('[data-v2="wxpusher-preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('收到新的订单指派')
    expect(wrapper.text()).toContain(exampleUrl)
    expect(wrapper.get('iframe').attributes('sandbox')).toBe('')
    expect(wrapper.get('iframe').attributes('srcdoc')).toContain('请前往查看')
  })
})
