// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { HttpError } from '@/api/errors'
import { openInternalChatPreview } from '../internalChat/launcherPreview'
import V2OrderChatPane from '../V2OrderChatPane.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
const orderNo = '20261120000000000001'
const base = `/api/internal-chat/orders/${orderNo}/`
const group = { id: 31, title: '合成订单群', order_no: orderNo, local_order_no: orderNo,
  is_member: true, can_join: true, self_member_id: 11, members: [],
  archived: false, can_reopen: false, can_send: true }

beforeEach(() => { vi.useFakeTimers(); request.mockReset() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it.each([[403, '订单权限已撤销'], [503, '暂时无法确认订单权限，请稍后重试']])(
  '已打开订单群轮询得到%s后卸载旧内容，并在恢复后重新核验', async (status, message) => {
    request.mockResolvedValueOnce(group)
    const wrapper = mount(V2OrderChatPane, { attachTo: document.body, props: { orderNo, production: true } })
    try {
      await flushPromises()
      const old = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
      request.mockRejectedValueOnce(new HttpError(Number(status), String(message)))
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(wrapper.find('iframe').exists()).toBe(false)
      expect(wrapper.text()).toContain(message)
      window.dispatchEvent(new MessageEvent('message', {
        source: old, origin: location.origin, data: { type: 'tt:order-history.ready' },
      }))
      expect(wrapper.find('iframe').exists()).toBe(false)
      request.mockResolvedValueOnce(group)
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow).not.toBe(old)
      expect(request).toHaveBeenLastCalledWith({ method: 'GET', url: base })
    } finally { wrapper.unmount() }
  },
)

it('历史或附件返回撤权后立即清空窗口，迟到的成功结果不能重新显示', async () => {
  request.mockResolvedValueOnce(group)
  const wrapper = mount(V2OrderChatPane, { attachTo: document.body, props: { orderNo, production: true } })
  try {
    await flushPromises()
    const frame = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
    const respond = vi.spyOn(frame, 'postMessage').mockImplementation(() => {})
    let finish!: (value: object) => void
    request.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    request.mockRejectedValueOnce(new HttpError(403, '订单权限已撤销'))
    const send = (id: number, suffix: string) => window.dispatchEvent(new MessageEvent('message', {
      source: frame, origin: location.origin,
      data: { type: 'tt:chat.request', id, method: 'get', path: base + suffix },
    }))
    send(1, 'messages/')
    send(2, 'messages/19/image/')
    await flushPromises()
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.text()).toContain('订单权限已撤销')
    finish({ messages: [{ text: '不应继续显示的旧内容' }] })
    await flushPromises()
    expect(respond.mock.calls.some(([value]) => value.id === 1 && value.result)).toBe(false)
  } finally { wrapper.unmount() }
})

it.each([false, true])('已归档群只有can_reopen=%s时显示重开，并使用原群返回值', async canReopen => {
  request.mockResolvedValueOnce({ ...group, archived: true, can_reopen: canReopen, can_send: false })
  const open = vi.fn()
  const wrapper = mount(V2OrderChatPane, {
    attachTo: document.body, props: { orderNo, production: true }, global: { provide: { [openInternalChatPreview as symbol]: open } },
  })
  try {
    await flushPromises()
    expect(wrapper.text()).toContain('已归档')
    const button = wrapper.find('[data-order-chat-reopen]')
    expect(button.exists()).toBe(canReopen)
    expect(wrapper.text()).not.toContain('主动归档')
    if (!canReopen) return
    request.mockResolvedValueOnce({ ...group, archived: false, can_reopen: true })
    await button.trigger('click')
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({ method: 'POST', url: base + 'reopen/' })
    expect(wrapper.text()).not.toContain('已归档')
    expect(open).not.toHaveBeenCalled()
  } finally { wrapper.unmount() }
})

it('页面隐藏时暂停权限轮询，卸载后取消轮询', async () => {
  request.mockResolvedValue(group)
  const wrapper = mount(V2OrderChatPane, { attachTo: document.body, props: { orderNo, production: true } })
  await flushPromises()
  const visible = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  await vi.advanceTimersByTimeAsync(6000)
  expect(request).toHaveBeenCalledTimes(1)
  visible.mockReturnValue('visible')
  await vi.advanceTimersByTimeAsync(3000)
  expect(request).toHaveBeenCalledTimes(2)
  wrapper.unmount()
  await vi.advanceTimersByTimeAsync(6000)
  expect(request).toHaveBeenCalledTimes(2)
})

it.each([
  { member: true, archived: false }, { member: true, archived: true },
  { member: false, archived: false }, { member: false, archived: true },
])('参与者可以打开原群，can_join仅控制尚未加入者：%j', async ({ member, archived }) => {
  const opened = { ...group, is_member: true, can_join: false, archived, can_send: !archived }
  request.mockResolvedValueOnce({ ...opened, is_member: member, can_join: !member, self_member_id: member ? 11 : null })
  if (!member) request.mockResolvedValueOnce(opened)
  const open = vi.fn()
  const wrapper = mount(V2OrderChatPane, {
    attachTo: document.body, props: { orderNo, production: true },
    global: { provide: { [openInternalChatPreview as symbol]: open } },
  })
  try {
    await flushPromises()
    const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
    const send = (type: string) => window.dispatchEvent(new MessageEvent('message', {
      source, origin: location.origin, data: { type },
    }))
    send('tt:order-history.ready')
    send('tt:order-history.activate')
    await flushPromises()
    expect(open).toHaveBeenCalledExactlyOnceWith(group.id)
    const joins = request.mock.calls.filter(([value]) => value.url === base + 'join/')
    expect(joins).toHaveLength(member ? 0 : 1)
  } finally { wrapper.unmount() }
})
