// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routeLocationKey } from 'vue-router'
import { BizError, HttpError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'
import V2DetailFace from '../V2DetailFace.vue'
import V2OrderDetailPane from '../V2OrderDetailPane.vue'
import { internalChatProduction, openInternalChatPreview } from '../internalChat/launcherPreview'
import { webKLiveOrigin as WEB_K } from '../internalChat/liveChatPreview'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
vi.mock('@/v2/V2DailiantongPane.vue', () => ({ default: { template: '<div>代练通内容</div>' } }))

const ORDER = '20261113000000000001'
const BASE = `/api/internal-chat/orders/${ORDER}/`
const group = {
  id: 423,
  kind: 'order',
  title: '订单-群聊测试',
  order_no: '20261113000000000002',
  local_order_no: ORDER,
  is_member: false,
  can_join: true,
  self_member_id: null,
  members: [
    { id: 81, name: '合成发单人', studio_name: '发单工作室', order_role: 'publisher', left_at: null },
    { id: 82, name: '合成打手', studio_name: '接单工作室', order_role: 'worker', left_at: '2026-11-16T08:00:00Z' },
  ],
}
function mountDetail(options: { reverseLink?: boolean; ownOrder?: boolean; production?: boolean } = {}) {
  const open = vi.fn()
  const wrapper = mount(V2DetailFace, {
    attachTo: document.body,
    props: { orderNo: ORDER, orderChatOnly: options.ownOrder === true },
    slots: { default: '订单内容' },
    global: { provide: {
      [openInternalChatPreview as symbol]: open,
      [internalChatProduction as symbol]: ref(options.production === true),
      [routeLocationKey as symbol]: { params: { order_no: ORDER }, query: options.reverseLink ? { orderChat: '1' } : {} },
    } },
  })
  return { wrapper, open }
}

function sendFromHistory(source: Window, type: string, origin = WEB_K, detail?: object) {
  window.dispatchEvent(new MessageEvent('message', { data: { type, detail }, source, origin }))
}

describe('订单详情里的订单群聊', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_INTERNAL_CHAT_PREVIEW', '1')
    vi.stubEnv('VITE_INTERNAL_CHAT_LIVE', '1')
    request.mockReset()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
  })

  it('没有预览开关时，只在已开启的工作室显示订单详情里的正式群聊', async () => {
    vi.stubEnv('VITE_INTERNAL_CHAT_PREVIEW', '0')
    const hidden = mountDetail()
    const pilot = mountDetail({ production: true })
    try {
      expect(hidden.wrapper.find('[data-v2="tab-order-chat"]').exists()).toBe(false)
      request.mockResolvedValueOnce(group)
      await pilot.wrapper.get('[data-v2="tab-order-chat"]').trigger('click')
      await flushPromises()
      const src = new URL(pilot.wrapper.get<HTMLIFrameElement>('iframe').element.src)
      expect(src.origin).toBe(location.origin)
      expect(src.pathname).toBe('/internal-chat-webk/')
      expect(src.searchParams.get('order-history')).toBe(ORDER)
    } finally {
      hidden.wrapper.unmount()
      pilot.wrapper.unmount()
    }
  })

  it('只在订单群聊页签显示原版记录，点击加入后进入对应会话', async () => {
    request.mockImplementation(({ url }: { url: string }) => {
      if (url === BASE) return Promise.resolve(group)
      if (url === `${BASE}join/`) return Promise.resolve({ ...group, is_member: true, self_member_id: 93 })
      throw new Error(`意外的请求 ${url}`)
    })
    const { wrapper, open } = mountDetail()
    expect(request).not.toHaveBeenCalled()
    expect(wrapper.find('iframe').exists()).toBe(false)
    await wrapper.get('[data-v2="tab-order-chat"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('aside, footer').exists()).toBe(false)
    const src = new URL(wrapper.get<HTMLIFrameElement>('iframe').element.src)
    expect(src.origin).toBe(WEB_K)
    expect(src.searchParams.get('order-history')).toBe(ORDER)
    expect(request).toHaveBeenCalledExactlyOnceWith({ method: 'GET', url: BASE })
    expect(open).not.toHaveBeenCalled()

    const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
    sendFromHistory(source, 'tt:order-history.ready')
    sendFromHistory(source, 'tt:order-history.activate', WEB_K, { conversationId: 999 })
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith({ method: 'POST', url: `${BASE}join/` })
    expect(open).toHaveBeenCalledExactlyOnceWith(423)
    wrapper.unmount()
  })

  it('只有当前已加载的消息区可触发入群，失败时可以重试', async () => {
    request.mockResolvedValueOnce(group)
    const { wrapper, open } = mountDetail({ reverseLink: true })
    try {
      await flushPromises()
      const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
      const respond = vi.spyOn(source, 'postMessage').mockImplementation(() => {})
      sendFromHistory(source, 'tt:order-history.activate')
      sendFromHistory(window, 'tt:order-history.ready')
      sendFromHistory(source, 'tt:order-history.activate')
      sendFromHistory(source, 'tt:order-history.ready')
      sendFromHistory(source, 'tt:order-history.activate', 'http://127.0.0.1:4182')
      sendFromHistory(window, 'tt:order-history.activate')
      expect(request).toHaveBeenCalledExactlyOnceWith({ method: 'GET', url: BASE })
      expect(respond).not.toHaveBeenCalled()

      request.mockRejectedValueOnce(new Error('暂时无法入群'))
      sendFromHistory(source, 'tt:order-history.activate')
      sendFromHistory(source, 'tt:order-history.activate')
      expect(respond).toHaveBeenCalledExactlyOnceWith(
        { type: 'tt:order-history.action-state', detail: { busy: true } }, WEB_K,
      )
      await flushPromises()
      expect(request).toHaveBeenCalledTimes(2)
      expect(respond).toHaveBeenLastCalledWith(
        { type: 'tt:order-history.action-state', detail: { busy: false, error: '暂时无法入群' } }, WEB_K,
      )
      expect(open).not.toHaveBeenCalled()

      request.mockResolvedValueOnce({ ...group, id: 999, is_member: true })
      sendFromHistory(source, 'tt:order-history.activate')
      await flushPromises()
      expect(open).not.toHaveBeenCalled()

      request.mockResolvedValueOnce({ ...group, is_member: true, self_member_id: 93 })
      sendFromHistory(source, 'tt:order-history.activate')
      await flushPromises()
      expect(open).toHaveBeenCalledExactlyOnceWith(group.id)
    } finally {
      wrapper.unmount()
    }
  })

  it('入群请求期间切走页签，旧会话不会弹出', async () => {
    request.mockResolvedValueOnce(group)
    let finishJoin!: (value: typeof group & { is_member: boolean }) => void
    request.mockImplementationOnce(() => new Promise(resolve => { finishJoin = resolve }))
    const { wrapper, open } = mountDetail({ reverseLink: true })
    try {
      await flushPromises()
      const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
      sendFromHistory(source, 'tt:order-history.ready')
      sendFromHistory(source, 'tt:order-history.activate')
      await wrapper.get('[data-v2="tab-local"]').trigger('click')
      expect(wrapper.find('iframe').exists()).toBe(false)
      finishJoin({ ...group, is_member: true })
      await flushPromises()
      expect(open).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('消息区只能读取当前订单的记录和图片，查看不触发已读', async () => {
    const page = { messages: [], has_more: false, has_newer: false }
    request.mockImplementation(({ url }: { url: string }) => Promise.resolve(url === BASE ? group : page))
    const { wrapper } = mountDetail({ reverseLink: true })
    try {
      await flushPromises()
      const iframe = wrapper.get<HTMLIFrameElement>('iframe').element
      const source = iframe.contentWindow!
      const respond = vi.spyOn(source, 'postMessage').mockImplementation((message) => {
        structuredClone(message)
      })
      const send = (data: unknown, eventSource = source, origin = WEB_K) => {
        window.dispatchEvent(new MessageEvent('message', { data, origin, source: eventSource }))
      }
      const bridgeRequest = (id: number, path: string, method = 'get') => ({ type: 'tt:chat.request', id, path, method })

      send({ type: 'tt:order-history.ready' }, window)
      expect(wrapper.text()).toContain('正在加载聊天记录')
      send({ type: 'tt:order-history.ready' })
      await flushPromises()
      expect(wrapper.text()).not.toContain('正在加载聊天记录')

      send(bridgeRequest(1, `${BASE}messages/?before=9`))
      send(bridgeRequest(2, `${BASE}messages/?around=201`))
      send(bridgeRequest(3, `${BASE}messages/201/image/`))
      send(bridgeRequest(4, BASE))
      send(bridgeRequest(5, `${BASE}messages/`), window)
      send(bridgeRequest(6, `${BASE}messages/`), source, 'http://127.0.0.1:4182')
      send(bridgeRequest(7, `${BASE}messages/?before=9&around=201`))
      send(bridgeRequest(8, '/api/internal-chat/conversations/423/read/', 'post'))
      send(bridgeRequest(9, `${BASE}join/`, 'post'))
      send(bridgeRequest(10, '/api/internal-chat/orders/20261113000000000002/messages/'))
      send(bridgeRequest(11, '/api/internal-chat/conversations/423/pin/', 'post'))
      await flushPromises()

      expect(request.mock.calls).toEqual([
        [{ method: 'GET', url: BASE }],
        [{ method: 'GET', url: `${BASE}messages/`, params: { before: 9 } }],
        [{ method: 'GET', url: `${BASE}messages/`, params: { around: 201 } }],
        [{ method: 'GET', url: `${BASE}messages/201/image/` }],
      ])
      for (const id of [1, 2, 3]) {
        expect(respond).toHaveBeenCalledWith({ type: 'tt:chat.response', id, result: page }, WEB_K)
      }
      expect(respond).toHaveBeenCalledWith({ type: 'tt:chat.response', id: 4, result: group }, WEB_K)
      for (const id of [7, 8, 9, 10, 11]) {
        expect(respond).toHaveBeenCalledWith({ type: 'tt:chat.response', id, error: '不支持该聊天请求' }, WEB_K)
      }
      expect(respond).toHaveBeenCalledTimes(9)
    } finally {
      wrapper.unmount()
    }
  })

  it('直达精简订单详情时选中群聊，切换页签会卸载只读消息区', async () => {
    const { default: router } = await import('@/router')
    expect(router.resolve(`/v2/orders/${ORDER}?orderChat=1`).name).toBe('v2-order-detail-preview')
    expect(router.resolve(`/orders/${ORDER}`).name).toBe('order-detail')
    request.mockResolvedValue({ ...group, is_member: true, self_member_id: 82 })
    const { wrapper, open } = mountDetail({ reverseLink: true, ownOrder: true })
    await flushPromises()
    expect(wrapper.find('[data-v2="tab-wecom"]').exists()).toBe(false)
    expect(wrapper.get('[data-v2="tab-order-chat"]').attributes('aria-selected')).toBe('true')
    expect(wrapper.find('iframe').exists()).toBe(true)
    const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
    sendFromHistory(source, 'tt:order-history.ready')
    sendFromHistory(source, 'tt:order-history.activate')
    await flushPromises()
    expect(open).toHaveBeenCalledWith(423)
    await wrapper.get('[data-v2="tab-local"]').trigger('click')
    expect(wrapper.find('iframe').exists()).toBe(false)
    window.dispatchEvent(new CustomEvent('tt:order-chat-focus', { detail: { orderNo: ORDER } }))
    await flushPromises()
    expect(wrapper.get('[data-v2="tab-order-chat"]').attributes('aria-selected')).toBe('true')
    expect(wrapper.find('iframe').exists()).toBe(true)
    expect(request.mock.calls.every(([config]) => config.method === 'GET')).toBe(true)
    wrapper.unmount()
  })

  it('只读消息区加载失败可以重试，旧消息区不能接收新会话的结果', async () => {
    request.mockResolvedValue(group)
    const { wrapper } = mountDetail({ reverseLink: true })
    try {
      await flushPromises()
      const original = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'tt:order-history.error' }, origin: WEB_K, source: original,
      }))
      await flushPromises()
      expect(wrapper.text()).toContain('聊天记录加载失败')
      await wrapper.get('.history-status button').trigger('click')
      const replacement = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
      expect(replacement).not.toBe(original)
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'tt:order-history.ready' }, origin: WEB_K, source: original,
      }))
      await flushPromises()
      expect(wrapper.text()).toContain('正在加载聊天记录')
    } finally {
      wrapper.unmount()
    }
  })

  it('已结束的群保留历史，按钮不可加入', async () => {
    request.mockResolvedValue({ ...group, can_join: false })
    const { wrapper, open } = mountDetail({ reverseLink: true })
    await flushPromises()
    expect(wrapper.find('iframe').exists()).toBe(true)
    const source = wrapper.get<HTMLIFrameElement>('iframe').element.contentWindow!
    sendFromHistory(source, 'tt:order-history.ready')
    sendFromHistory(source, 'tt:order-history.activate')
    await flushPromises()
    expect(open).not.toHaveBeenCalled()
    expect(request.mock.calls.every(([config]) => config.method === 'GET')).toBe(true)
    wrapper.unmount()
  })

  it.each([new HttpError(403, '无权查看'), new BizError(1002, '记录不存在或已被删除')])(
    '跨角色的预览详情用已接订单详情回退 %s',
    async denied => {
      const ownedOrder = `/api/business/my-orders/${ORDER}/detail/`
      request.mockImplementation(({ url }: { url: string }) => {
        if (url === `/api/business/orders/${ORDER}/`) return Promise.reject(denied)
        if (url === ownedOrder) return Promise.resolve({ order_no: ORDER, content: '合成的已接订单内容' })
        return Promise.resolve([])
      })
      const pinia = createPinia()
      useSessionStore(pinia).user = {
        id: 11,
        username: 'mixed',
        display_name: '混合角色',
        identity_plane: 'tenant',
        is_tenant_admin: false,
        roles: ['客服', '代练员'],
        permissions: [],
        menu_permissions: ['order.view_detail', 'my_orders.send_to_my_system'],
      }
      const wrapper = mount(V2OrderDetailPane, {
        props: { orderNo: ORDER, allowOwnOrderDetail: true, allowOwnOrderFallback: true },
        global: { plugins: [pinia] },
      })
      await flushPromises()
      expect(wrapper.text()).toContain('合成的已接订单内容')
      expect(wrapper.find('[data-v2="tab-wecom"]').exists()).toBe(false)
      expect(request).toHaveBeenCalledWith({ url: ownedOrder, method: 'get' })
      wrapper.unmount()
    },
  )
})
