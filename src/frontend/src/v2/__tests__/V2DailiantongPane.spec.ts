// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError, NetworkError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import V2DetailFace from '../V2DetailFace.vue'

const { fetchPushes, fetchMessages, sendMessage } = vi.hoisted(() => ({
  fetchPushes: vi.fn(),
  fetchMessages: vi.fn(),
  sendMessage: vi.fn(),
}))

vi.mock('@/api/dailiantongPush', async importOriginal => ({
  ...(await importOriginal<typeof import('@/api/dailiantongPush')>()),
  fetchOrderPlatformPushes: fetchPushes,
  fetchOrderPlatformPushMessages: fetchMessages,
  sendOrderPlatformPushMessage: sendMessage,
}))

const ORDER = '11111111111111111111'
const push = {
  id: 10, platform: 10, serial_no: '平台合成单', state: 'sent', price: '20.00', ensure1: '0.00', ensure2: '0.00',
  time_limit_hours: 24, remote_status: 12, remote_cancel_status: 10, last_synced_at: null, last_error: '',
}
const history = {
  messages: [
    { sender: '接单者', text: '已开始\n稍后更新', time: '2026-11-15 09:00', is_system: false },
    { sender: '系统', text: '系统记录', time: '', is_system: true },
  ],
  images: [{ caption: '订单进度', url: 'https://example.invalid/progress.png', time: '', img: '' }],
  error: '',
}

function mountDetail(canWrite = true) {
  const pinia = createPinia()
  useSessionStore(pinia).user = {
    menu_permissions: canWrite ? ['order.list', 'order.platform_write'] : ['order.list'],
  } as never
  return mount(V2DetailFace, {
    props: { orderNo: ORDER, hasDailiantongHistory: true }, global: { plugins: [pinia] }, slots: { default: '订单信息' },
  })
}

async function openDlt(wrapper: ReturnType<typeof mountDetail>) {
  await wrapper.get('[data-v2="tab-dlt"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  fetchPushes.mockReset().mockResolvedValue([push])
  fetchMessages.mockReset().mockResolvedValue(history)
  sendMessage.mockReset().mockResolvedValue(push)
})

describe('订单详情代练通留言', () => {
  it('切到代练通才读历史，返回时保留草稿，提交后更新平台留言', async () => {
    const wrapper = mountDetail()
    expect(fetchPushes).not.toHaveBeenCalled()
    await openDlt(wrapper)
    const list = wrapper.get('section[aria-label="平台留言"]')
    const chat = wrapper.get('section[aria-label="与打手对话"]')
    expect(wrapper.text()).toContain('平台合成单')
    expect(list.text()).toContain('接单者')
    expect(list.get('.dlt-chat-body').element.textContent).toBe('已开始\n稍后更新')
    expect(list.get('.dlt-chat-system').text()).toBe('系统')
    expect(list.get('a').attributes('href')).toBe('https://example.invalid/progress.png')
    await chat.get('textarea').setValue('  合成回复  ')
    await wrapper.get('[data-v2="tab-local"]').trigger('click')
    expect(chat.isVisible()).toBe(false)
    await openDlt(wrapper)
    expect(chat.get('textarea').element.value).toBe('  合成回复  ')
    expect(fetchMessages).toHaveBeenCalledExactlyOnceWith(ORDER)

    fetchMessages.mockResolvedValueOnce({
      ...history, messages: [...history.messages, { sender: '发单者', text: '合成回复', time: '', is_system: false }],
    })
    await chat.get('form').trigger('submit')
    await flushPromises()
    expect(sendMessage).toHaveBeenCalledExactlyOnceWith(ORDER, '合成回复')
    expect(chat.get('textarea').element.value).toBe('')
    expect(chat.get('[role="status"]').text()).toBe('留言已提交')
    expect(list.text()).toContain('合成回复')
    wrapper.unmount()
  })

  it('历史接口失败有明确提示，只有读权限时可以重取历史', async () => {
    fetchMessages.mockResolvedValueOnce({ messages: [], error: '平台暂时无法读取' })
    const wrapper = mountDetail(false)
    await openDlt(wrapper)
    expect(wrapper.get('[role="alert"]').text()).toContain('平台暂时无法读取')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.get('section[aria-label="与打手对话"]').text()).toContain('仅可查看')
    await wrapper.get('section[aria-label="平台留言"] button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已开始')
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('发送业务失败保留原文和草稿，网络未知提示核对且不会重复发送', async () => {
    const wrapper = mountDetail()
    await openDlt(wrapper)
    const chat = wrapper.get('section[aria-label="与打手对话"]')
    const input = chat.get('textarea')
    const form = chat.get('form')
    await form.trigger('submit')
    expect(sendMessage).not.toHaveBeenCalled()
    expect(chat.get('[role="alert"]').text()).toBe('留言内容不能为空')
    await input.setValue('合成草稿')
    sendMessage.mockRejectedValueOnce(new BizError(1002, '平台未接受留言'))
    await form.trigger('submit')
    await flushPromises()
    expect(chat.get('[role="alert"]').text()).toBe('平台未接受留言')
    let rejectRequest: (error: NetworkError) => void = () => undefined
    sendMessage.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRequest = reject }))
    await form.trigger('submit')
    await form.trigger('submit')
    rejectRequest(new NetworkError('请求超时'))
    await flushPromises()
    expect(sendMessage).toHaveBeenCalledTimes(2)
    expect(input.element.value).toBe('合成草稿')
    expect(chat.get('[role="alert"]').text()).toContain('请刷新平台留言核对')
    wrapper.unmount()
  })

  it('订单结束后仍可读留言，未发到代练通的订单不会查留言', async () => {
    fetchPushes.mockResolvedValueOnce([{ ...push, remote_status: 17 }]).mockResolvedValueOnce([])
    const ended = mountDetail()
    await openDlt(ended)
    expect(ended.text()).toContain('代练通订单已结束，留言仅供查看')
    expect(ended.find('textarea').exists()).toBe(false)
    expect(fetchMessages).toHaveBeenCalledExactlyOnceWith(ORDER)
    ended.unmount()

    const local = mountDetail()
    await openDlt(local)
    expect(local.text()).toContain('本单尚未发到交易平台')
    expect(fetchMessages).toHaveBeenCalledExactlyOnceWith(ORDER)
    local.unmount()
  })

  it('推送读取失败或未分配单号都可以刷新，成功后刷新仍保留草稿', async () => {
    fetchPushes.mockRejectedValueOnce(new NetworkError('读取超时'))
      .mockResolvedValueOnce([{ ...push, serial_no: null, state: 'pending' }])
    const wrapper = mountDetail()
    await openDlt(wrapper)
    expect(wrapper.text()).toContain('读取超时')
    await wrapper.get('.dtl-dlt > button').trigger('click')
    await flushPromises()
    expect(fetchMessages).not.toHaveBeenCalled()
    await wrapper.get('.dtl-dlt > button').trigger('click')
    await flushPromises()
    expect(fetchMessages).toHaveBeenCalledExactlyOnceWith(ORDER)
    expect(wrapper.text()).toContain('已开始')
    await wrapper.get('textarea').setValue('合成草稿')
    await wrapper.get('section[aria-label="平台留言"] button').trigger('click')
    await flushPromises()
    expect(wrapper.get('textarea').element.value).toBe('合成草稿')
    expect(fetchMessages).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('读最大 ID 的代练通记录，写入目标是另一平台时只展示代练通历史', async () => {
    fetchPushes.mockResolvedValueOnce([
      { ...push, id: 9, state: 'closed', serial_no: '旧平台单' },
      push,
      { ...push, id: 11, platform: 20, serial_no: '另一平台单' },
    ])
    const wrapper = mountDetail()
    await openDlt(wrapper)
    expect(wrapper.text()).toContain('平台合成单')
    expect(wrapper.text()).toContain('当前有效订单已发往其他平台')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(fetchMessages).toHaveBeenCalledExactlyOnceWith(ORDER)
    expect(sendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
