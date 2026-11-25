// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { request } from '@/api/http'
import { useSessionStore, WORKER_ROLE } from '@/stores/session'
import WorkerApp from '../../App.vue'
import OrderDetailView from '../../pages/OrderDetailView.vue'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'

vi.mock('@/api/http', () => ({ request: vi.fn() }))
const chatModule = vi.hoisted(() => ({ pending: undefined as Promise<void> | undefined }))
vi.mock('../MobileInternalChat.vue', async (original) => {
  await chatModule.pending
  return original()
})

installFramework7ForTest()

it('从订单直达内部群时，慢加载的聊天模块就绪后仍打开本单会话', async () => {
  const showModal = function (this: HTMLDialogElement) { this.open = true }
  const close = function (this: HTMLDialogElement) { this.open = false }
  Object.assign(HTMLDialogElement.prototype, { showModal, close })
  let releaseChatModule!: () => void
  chatModule.pending = new Promise((resolve) => { releaseChatModule = resolve })
  const orderNo = '10000000000000000012'
  const summaryUrl = `/api/internal-chat/orders/${orderNo}/`
  vi.mocked(request).mockImplementation(async ({ url }) => {
    if (url === `/api/business/my-orders/${orderNo}/detail/`) {
      return { order_no: orderNo, source_order_no: '', status: 'IN_PROGRESS',
        game_name: '测试游戏', project_text: '合成项目', temp_amount: '0.00' }
    }
    if (url === summaryUrl) return { id: 81, title: '合成订单', order_no: orderNo,
      local_order_no: orderNo, is_member: true, can_join: true, members: [] }
    return { unread_count: 0 }
  })
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  session.status = 'ready'
  session.user = {
    id: 7, username: 'test-worker', display_name: '测试打手', identity_plane: 'tenant_a', roles: [WORKER_ROLE],
    permissions: [], menu_permissions: [], is_tenant_admin: false, internal_chat_enabled: true,
  }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/orders/:orderNo', name: 'm-order-detail', component: OrderDetailView }],
  })
  await router.push(`/orders/${orderNo}`)
  const wrapper = mount(WorkerApp, { attachTo: document.body, global: { plugins: [pinia, router] } })
  const openChat = '[data-testid="detail-open-internal-chat"]'
  try {
    await flushPromises()
    await wrapper.get(openChat).trigger('click')
    await vi.waitFor(() => expect(wrapper.get(openChat).text()).toBe('打开内部沟通'))
    expect(request).toHaveBeenCalledWith({ method: 'GET', url: summaryUrl })
    expect(document.querySelector('dialog.m-chat-screen')).toBeNull()

    releaseChatModule()
    const frame = await vi.waitFor(() => {
      const openFrame = document.querySelector<HTMLIFrameElement>('dialog[open] iframe')
      expect(openFrame?.contentWindow).toBeTruthy()
      return openFrame!
    })
    const post = vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation(() => {})
    for (const type of ['tt:chat.ready', 'tt:chat.navigationReady']) {
      window.dispatchEvent(new MessageEvent('message', { origin: location.origin, source: frame.contentWindow, data: { type } }))
    }
    expect(post).toHaveBeenCalledWith({ type: 'tt:chat.openConversation', detail: 81 }, location.origin)
  } finally {
    releaseChatModule()
    wrapper.unmount()
    document.querySelector('script[src="/socket.io/socket.io.js"]')?.remove()
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'close')
  }
})
