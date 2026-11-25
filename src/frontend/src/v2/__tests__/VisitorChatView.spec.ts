// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import VisitorChatView from '../internalChat/VisitorChatView.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ user: { internal_chat_enabled: true } }) }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ resolve: () => ({ href: '/visitor-chat' }) }),
}))

const send = vi.mocked(request)
const base = '/api/internal-chat/visitor/'
const user = { id: 71, display_name: '客服甲' }
const conversationId = 9
const visitorFrameConversationId = conversationId * 2
const seat = { id: conversationId, peer: { id: 32, name: '访客 139****0000' }, seat_kind: 'hr', service_status: 'assigned' }
const unmounts: (() => void)[] = []

function mountView(scope: 'guest' | 'desk') {
  const wrapper = mount(VisitorChatView, {
    attachTo: document.body,
    props: { scope },
    global: { stubs: {
      RouterLink: true,
      ElDialog: { props: ['modelValue', 'title'], template: `<section v-if="modelValue" role="dialog" :aria-label="title"><slot/><slot name="footer"/></section>` },
    } },
  })
  unmounts.push(() => wrapper.unmount())
  return wrapper
}

function message(source: Window, type: string, detail?: unknown, extra = {}, origin = window.location.origin) {
  window.dispatchEvent(new MessageEvent('message', { source, origin, data: { type, detail, ...extra } }))
}

function staffRequests() {
  let enabled = false
  send.mockImplementation(async config => {
    switch (config.url) {
      case `${base}desk/me/`: return { user, seat_mode: true, can_receive: true }
      case `${base}desk/availability/`:
        if (config.method === 'post') enabled = (config.data as { enabled: boolean }).enabled
        return { enabled }
      case `${base}desk/conversations/`: return { cursor: 30, conversations: [seat] }
      case '/api/internal-chat/conversations/': return { cursor: 7, conversations: [] }
      case '/api/internal-chat/sync/?after=7': return { cursor: 7, changes: [], has_more: false }
      case `${base}desk/conversations/9/`: return seat
      case `${base}desk/conversations/9/transfer/`:
        return config.method === 'post' ? seat : { candidates: [{ id: 23, name: '客服乙' }], history: [] }
      case `${base}desk/conversations/9/close/`: return { ...seat, service_status: 'closed' }
      case `${base}desk/sync/?after=30`: return { changes: [], revoked_conversation_ids: [8], cursor: 41 }
      default: throw new Error(`unexpected ${config.method ?? 'get'} ${config.url}`)
    }
  })
}

beforeEach(() => {
  send.mockReset()
  localStorage.clear()
  sessionStorage.clear()
})
afterEach(() => { unmounts.splice(0).forEach(unmount => unmount()); vi.restoreAllMocks() })

it('首次注册在团团通页面提示，沿用原通讯录及聊天标题的在线状态消息', async () => {
  let busy = false
  let closed = false
  send.mockImplementation(async config => {
    if (config.url === `${base}guest/bootstrap/`) return { registered: false, seat_mode: true }
    if (config.url === `${base}guest/register/`) return { user, conversation_id: null, seat_mode: true }
    if (config.url === `${base}guest/conversations/9/`) return {
      ...seat, peer: { ...seat.peer, presence: busy ? 'busy' : 'offline' }, service_status: closed ? 'closed' : busy ? 'new' : 'waiting',
    }
    throw new Error(`unexpected ${config.url}`)
  })
  const page = mountView('guest')
  await flushPromises()
  await page.get('input[type="tel"]').setValue('13900000001')
  await page.get('form').trigger('submit')
  await flushPromises()
  expect(page.get('aside').text()).toContain('请选择人事客服或订单客服')
  const source = page.get<HTMLIFrameElement>('iframe').element.contentWindow!
  const post = vi.spyOn(source, 'postMessage')
  message(source, 'tt:chat.navigationReady')
  expect(post).toHaveBeenCalledWith({ type: 'tt:chat.openDirectory', detail: undefined }, window.location.origin)
  message(source, 'tt:chat.watchPresence', conversationId, {}, 'https://example.test')
  expect(send).not.toHaveBeenCalledWith(expect.objectContaining({ url: `${base}guest/conversations/9/` }))
  message(source, 'tt:chat.watchPresence', conversationId)
  await flushPromises()
  expect(page.get('aside').text()).toContain('留言已进入队列')
  expect(post).toHaveBeenCalledWith({ type: 'tt:chat.presence', detail: { conversationId, online: false } }, window.location.origin)

  busy = true
  message(source, 'tt:chat.watchPresence', conversationId)
  await flushPromises()
  expect(page.get('aside').text()).toContain('客服繁忙')
  expect(post).toHaveBeenLastCalledWith({ type: 'tt:chat.presence', detail: { conversationId, online: null } }, window.location.origin)
  closed = true
  message(source, 'tt:chat.watchPresence', conversationId)
  await flushPromises()
  expect(page.get('aside').text()).toBe('本次接待已结束，如需帮助可继续发送消息。')
  expect(page.get('iframe').element.contentWindow).toBe(source)
})

it('在团团通工具栏开始与停止接待、转接会话并卸载原接待窗口', async () => {
  staffRequests()
  const page = mountView('desk')
  await flushPromises()
  const iframe = page.get<HTMLIFrameElement>('iframe').element
  const source = iframe.contentWindow!
  message(source, 'tt:chat.navigationReady')
  message(source, 'tt:chat.watchPresence', visitorFrameConversationId)
  await flushPromises()
  const button = (label: string) => page.findAll('button').find(item => item.text() === label)!
  await button('开始接待').trigger('click')
  await flushPromises()
  await button('停止接待').trigger('click')
  await flushPromises()
  expect(send).toHaveBeenCalledWith(expect.objectContaining({ url: `${base}desk/availability/`, method: 'post', data: { enabled: false } }))
  await button('转接会话').trigger('click')
  await flushPromises()
  expect(page.get('[role="dialog"]').text()).toContain('客服乙')
  await button('确认转接').trigger('click')
  await flushPromises()
  expect(send).toHaveBeenCalledWith(expect.objectContaining({ url: `${base}desk/conversations/9/transfer/`, method: 'post', data: { target_id: 23 } }))
  expect(page.get('iframe').element).not.toBe(iframe)
  expect(page.get('[aria-label="访客接待操作"]').text()).not.toContain('访客 139****0000')
})

it('保留当前仍有权访问的会话，清理旧窗口历史并通过团团通按钮结束接待', async () => {
  staffRequests()
  const page = mountView('desk')
  await flushPromises()
  const iframe = page.get<HTMLIFrameElement>('iframe').element
  const source = iframe.contentWindow!
  message(source, 'tt:chat.navigationReady')
  message(source, 'tt:chat.watchPresence', visitorFrameConversationId)
  message(source, 'tt:chat.request', undefined, { id: 11, method: 'get', path: '/api/internal-chat/conversations/' })
  await flushPromises()
  message(source, 'tt:chat.request', undefined, { id: 12, method: 'get', path: '/api/internal-chat/sync/?after=1' })
  await flushPromises()
  const fresh = page.get<HTMLIFrameElement>('iframe').element.contentWindow!
  expect(fresh).not.toBe(source)
  const post = vi.spyOn(fresh, 'postMessage')
  message(fresh, 'tt:chat.navigationReady')
  expect(post).toHaveBeenCalledWith({ type: 'tt:chat.openConversation', detail: visitorFrameConversationId }, window.location.origin)
  message(fresh, 'tt:chat.watchPresence', visitorFrameConversationId)
  await flushPromises()
  const button = (label: string) => page.findAll('button').find(item => item.text() === label)!
  await button('结束会话').trigger('click')
  await flushPromises()
  expect(send).toHaveBeenCalledWith(expect.objectContaining({ url: `${base}desk/conversations/9/close/`, method: 'post' }))
  expect(page.find('[role="dialog"]').exists()).toBe(false)
  expect(button('结束会话').attributes('disabled')).toBeDefined()
  expect(page.get('[aria-label="访客接待操作"]').text()).toContain('已结束')
})

it('清除已转接访客的旧窗口时，重新打开客服正在使用的普通单聊', async () => {
  staffRequests()
  const page = mountView('desk')
  await flushPromises()
  const source = page.get<HTMLIFrameElement>('iframe').element.contentWindow!
  const directId = visitorFrameConversationId + 1
  message(source, 'tt:chat.navigationReady')
  message(source, 'tt:chat.watchPresence', visitorFrameConversationId)
  await flushPromises()
  expect(page.get('[aria-label="访客接待操作"]').text()).toContain('访客 139****0000')

  message(source, 'tt:chat.watchPresence', directId)
  message(source, 'tt:chat.request', undefined, { id: 11, method: 'get', path: '/api/internal-chat/conversations/' })
  await flushPromises()
  expect(page.get('[aria-label="访客接待操作"]').text()).not.toContain('访客 139****0000')
  expect(page.findAll('button').find(item => item.text() === '转接会话')?.attributes('disabled')).toBeDefined()

  message(source, 'tt:chat.request', undefined, { id: 12, method: 'get', path: '/api/internal-chat/sync/?after=1' })
  await flushPromises()
  const fresh = page.get<HTMLIFrameElement>('iframe').element.contentWindow!
  expect(fresh).not.toBe(source)
  const post = vi.spyOn(fresh, 'postMessage')
  message(fresh, 'tt:chat.navigationReady')
  expect(post).toHaveBeenCalledWith({ type: 'tt:chat.openConversation', detail: directId }, window.location.origin)
})
