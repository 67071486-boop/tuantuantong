// @vitest-environment jsdom
import { createPinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import WxPusherChatPage from '../WxPusherChatPage.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
enableAutoUnmount(afterEach)
afterEach(() => vi.resetAllMocks())

const conversationUrl = '/api/internal-chat/notification-conversations/32/'
const messagesUrl = `${conversationUrl}messages/`
const path = '/wxpusher/chat/32?message=81&entry=wxpusher'
const nextPath = '/wxpusher/chat/33?message=91&entry=wxpusher'
const target = { id: 81, seq: 4, sender_id: 55, text: '请核对新要求', revoked: false }
const nextTarget = { ...target, id: 91, seq: 6, text: '请确认下一个群' }
const history = {
  order_no: '0086', remark: '', self_member_id: 51, can_reply: true,
  senders: [{ id: 55, name: '客服小林', studio_name: '晨风工作室' }],
  messages: [target], has_newer: false,
}

async function openChat(destination = path) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/wxpusher/chat/:conversationId', component: WxPusherChatPage }],
  })
  await router.push(destination)
  const wrapper = mount(RouterView, { global: { plugins: [createPinia(), router] } })
  await flushPromises()
  return { router, wrapper }
}

it('从同一个通知入口打开单聊，展示对方姓名并写回这条消息的已读', async () => {
  const directHistory = {
    ...history, kind: 'direct', peer_name: '客服小林', order_no: '',
    messages: [target],
    senders: [{ id: 55, name: '客服小林', studio_name: '' }],
  }
  const fetch = vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (url === `${conversationUrl}read/`) return { read_seq: 4 }
    if (method === 'POST') return { ...target, id: 82, sender_id: 51, text: '收到' }
    return directHistory
  })
  const { wrapper } = await openChat()
  expect(wrapper.get('h1').text()).toBe('客服小林')
  expect(wrapper.get('.push-chat__target').text()).toContain('请核对新要求')
  expect(fetch).toHaveBeenLastCalledWith({
    url: '/api/internal-chat/notification-conversations/32/read/', method: 'POST', data: { seq: 4 },
  })
  await wrapper.get('textarea').setValue('收到')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.findAll('.push-chat__author').map((row) => row.text())).toEqual(['客服小林', '我'])
})

it('一次读取专用群接口取得消息上下文，标记到目标位置，并允许引用回复', async () => {
  const fetch = vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (url === messagesUrl) return method === 'POST' ? { ...target, id: 82, text: '已确认' } : history
  })
  const { wrapper } = await openChat()
  expect(fetch).toHaveBeenNthCalledWith(1, { url: messagesUrl, params: { around: '81' } })
  expect(fetch).toHaveBeenNthCalledWith(2, { url: `${conversationUrl}read/`, method: 'POST', data: { seq: 4 } })
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(wrapper.get('.push-chat__target').text()).toContain('客服小林 · 晨风工作室')
  expect(wrapper.get('.push-chat__target').text()).toContain('请核对新要求')
  await wrapper.get('.push-chat__target button').trigger('click')
  await wrapper.get('textarea').setValue('已确认')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(fetch).toHaveBeenCalledWith(expect.objectContaining({ url: messagesUrl, method: 'POST',
    data: expect.objectContaining({ reply_to_id: 81, text: '已确认' }),
  }))
  expect(wrapper.text()).toContain('已确认')
})

it('已结束的群聊仍显示上下文，但只有读取行为', async () => {
  const fetch = vi.mocked(request).mockResolvedValueOnce({ ...history, can_reply: false })
  const { wrapper } = await openChat()
  expect(wrapper.get('[role="status"]').text()).toContain('群聊已结束')
  expect(wrapper.get('.push-chat__target').text()).toContain('请核对新要求')
  expect(wrapper.find('form').exists()).toBe(false)
  expect(wrapper.find('.push-chat__target button').exists()).toBe(false)
  expect(fetch).toHaveBeenLastCalledWith({ url: `${conversationUrl}read/`, method: 'POST', data: { seq: 4 } })
})

it('无权访问的链接不会显示消息或发送表单', async () => {
  const fetch = vi.mocked(request).mockRejectedValueOnce(new Error('未找到'))
  const { wrapper } = await openChat()
  expect(wrapper.get('[role="alert"]').text()).toContain('确认当前账号可以访问该会话')
  expect(wrapper.find('form').exists()).toBe(false)
  expect(fetch).toHaveBeenCalledExactlyOnceWith({ url: messagesUrl, params: { around: '81' } })
})

it.each([
  '/wxpusher/chat/32?entry=wxpusher',
  '/wxpusher/chat/32?message=81%0A&entry=wxpusher',
  '/wxpusher/chat/32?message=81&message=82&entry=wxpusher',
  '/wxpusher/chat/0?message=81&entry=wxpusher',
  '/wxpusher/chat/9007199254740992?message=81&entry=wxpusher',
  '/wxpusher/chat/32?message=9007199254740992&entry=wxpusher',
])('消息或群编号未通过校验时不发请求（%s）', async (destination) => {
  const { wrapper } = await openChat(destination)
  expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法打开')
  expect(request).not.toHaveBeenCalled()
})

it('切换通知链接清空上个群的草稿，旧回复完成后不写入新群', async () => {
  let resolveSend: (message: typeof target) => void = () => {}
  const pending = new Promise<typeof target>((resolve) => { resolveSend = resolve })
  vi.mocked(request).mockImplementation(async ({ url, method }) => {
    if (url === messagesUrl) return method === 'POST' ? pending : history
    if (url === '/api/internal-chat/notification-conversations/33/messages/') return {
      ...history, order_no: '0096', messages: [nextTarget],
    }
  })
  const { router, wrapper } = await openChat()
  await wrapper.get('.push-chat__target button').trigger('click')
  await wrapper.get('textarea').setValue('上一群的回复')
  await wrapper.get('form').trigger('submit')
  expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
  await router.push(nextPath)
  await flushPromises()
  const nextInput = wrapper.get<HTMLTextAreaElement>('textarea')
  expect(nextInput.element.value).toBe('')
  expect(nextInput.attributes('disabled')).toBeUndefined()
  expect(wrapper.find('.push-chat__reply').exists()).toBe(false)
  await nextInput.setValue('新群的草稿')
  resolveSend({ ...target, text: '上一群的回复' })
  await flushPromises()
  expect(wrapper.get('.push-chat__target').text()).toContain('请确认下一个群')
  expect(wrapper.text()).not.toContain('上一群的回复')
  expect(nextInput.element.value).toBe('新群的草稿')
})
