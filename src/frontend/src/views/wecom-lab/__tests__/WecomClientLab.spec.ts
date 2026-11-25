// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import WecomClientLab from '../WecomClientLab.vue'
import { findLabGroups, getLabSignature } from '@/api/wecomClientLab'

vi.mock('@/api/wecomClientLab', () => ({ findLabGroups: vi.fn(), getLabSignature: vi.fn() }))

const group = { name: 'test-group', chat_id: 'test-chat-id' }
let ready: () => void
const invoke = vi.fn()
const open = vi.fn()

beforeEach(() => {
  sessionStorage.clear()
  Object.defineProperty(navigator, 'userAgent', { configurable: true, value: 'wxwork' })
  vi.mocked(findLabGroups).mockResolvedValue({ groups: [group], too_many: false })
  vi.mocked(getLabSignature).mockResolvedValue({ corp_id: 'corp', agent_id: 1, timestamp: 1, nonce: 'nonce', corp_signature: 'corp-sign', agent_signature: 'agent-sign' })
  window.wx = {
    ready: (callback) => { ready = callback }, error: vi.fn(),
    config: () => ready(), agentConfig: (params) => (params.success as () => void)(),
    invoke, openEnterpriseChat: open,
  }
  invoke.mockClear()
  open.mockClear()
})
afterEach(() => { vi.restoreAllMocks(); delete window.wx; sessionStorage.clear() })

async function prepare() {
  const wrapper = mount(WecomClientLab)
  await flushPromises()
  await wrapper.get('[data-test="group-name"]').setValue('test-group')
  await wrapper.findAll('button')[0]!.trigger('click')
  await flushPromises()
  await wrapper.get('input[type="radio"]').setValue()
  return wrapper
}

describe('企微客户端测试页', () => {
  it('只在手动开启后下次加载尝试打开，并且只执行一次', async () => {
    const wrapper = await prepare()
    expect(open).not.toHaveBeenCalled()
    await wrapper.findAll('button')[2]!.trigger('click')
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
    const next = mount(WecomClientLab)
    await flushPromises()
    expect(open).toHaveBeenCalledOnce()
    expect(open.mock.calls[0]![0].chatId).toBe('test-chat-id')
    expect(invoke).not.toHaveBeenCalled()
    next.unmount()
    const third = mount(WecomClientLab)
    await flushPromises()
    expect(open).toHaveBeenCalledOnce()
    third.unmount()
  })

  it('取消确认时不会发送或修改成员', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    await wrapper.find('textarea').setValue(JSON.stringify({ chatId: 'test-chat-id', userIds: 'test-user' }))
    await wrapper.findAll('button')[3]!.trigger('click')
    await wrapper.findAll('button')[4]!.trigger('click')
    await wrapper.findAll('button')[5]!.trigger('click')
    expect(invoke).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('不允许用参数指定另一个群', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await wrapper.find('textarea').setValue(JSON.stringify({ chatId: 'other-chat-id', userIds: 'test-user' }))
    await wrapper.findAll('button')[3]!.trigger('click')
    expect(invoke).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('参数须包含当前测试群')
    wrapper.unmount()
  })

  it('手动打开会话携带所选群和成员，并显示回调', async () => {
    const wrapper = await prepare()
    await wrapper.findAll('input')[2]!.setValue('test-user')
    open.mockImplementationOnce((params) => params.success({ err_msg: 'openEnterpriseChat:ok' }))
    await wrapper.findAll('button')[1]!.trigger('click')
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ chatId: 'test-chat-id', userIds: 'test-user' }))
    expect(wrapper.text()).toContain('openEnterpriseChat:ok')
    expect(wrapper.text()).toContain('请在企微中核对实际操作')
    wrapper.unmount()
  })

  it('确认后尝试变更成员并展示失败回调', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    invoke.mockImplementationOnce((_name, _params, callback) => callback({ err_msg: 'updateEnterpriseChat:fail' }))
    await wrapper.find('textarea').setValue(JSON.stringify({ chatId: 'test-chat-id', userIds: 'test-user' }))
    await wrapper.findAll('button')[3]!.trigger('click')
    expect(invoke).toHaveBeenCalledWith('updateEnterpriseChat', { chatId: 'test-chat-id', userIds: 'test-user' }, expect.any(Function))
    expect(wrapper.text()).toContain('updateEnterpriseChat:fail')
    wrapper.unmount()
  })

  it('无效成员 JSON 不调用客户端', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    for (const invalid of ['{', '[]', '{}', '{"chatId":"test-chat-id"}']) {
      await wrapper.find('textarea').setValue(invalid)
      await wrapper.findAll('button')[3]!.trigger('click')
    }
    expect(invoke).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('确认后按企微接口格式向所选群发送链接消息', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await wrapper.get('[data-test="link-title"]').setValue('test-title')
    await wrapper.get('[data-test="link-desc"]').setValue('test-description')
    await wrapper.get('[data-test="link-url"]').setValue('https://example.com/test')
    await wrapper.findAll('button')[4]!.trigger('click')
    expect(invoke).toHaveBeenCalledWith('openExistedChatWithMsg', {
      chatId: 'test-chat-id', msg: { msgtype: 'link', link: { title: 'test-title', desc: 'test-description', url: 'https://example.com/test' } },
    }, expect.any(Function))
    wrapper.unmount()
  })

  it('确认后打开群发并说明无法限制企微选择器', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await wrapper.findAll('textarea')[1]!.setValue('test-message')
    await wrapper.findAll('button')[5]!.trigger('click')
    expect(invoke).toHaveBeenCalledWith('shareToExternalChat', { text: { content: 'test-message' } }, expect.any(Function))
    expect(wrapper.text()).toContain('企微群发选择器不受此页面限制')
    expect(wrapper.text()).toContain('只选择指定的测试群')
    wrapper.unmount()
  })

  it('无效链接及空群发消息不会调用客户端', async () => {
    const wrapper = await prepare()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await wrapper.findAll('textarea')[1]!.setValue('   ')
    for (const invalid of ['', 'javascript:alert(1)', 'not a url']) {
      await wrapper.get('[data-test="link-url"]').setValue(invalid)
      await wrapper.findAll('button')[4]!.trigger('click')
    }
    await wrapper.findAll('button')[5]!.trigger('click')
    expect(invoke).not.toHaveBeenCalled()
    wrapper.unmount()
  })

})
