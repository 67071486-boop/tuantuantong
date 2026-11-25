// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { BizError, HttpError, NetworkError } from '@/api/errors'
import { openInternalChatPreview } from '../internalChat/launcherPreview'
import V2OrderChatPane from '../V2OrderChatPane.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
const orderNo = '20261120000000000021'
const base = `/api/internal-chat/orders/${orderNo}/`
const empty = { has_conversation: false, id: null, order_no: orderNo, local_order_no: orderNo,
  title: '合成旧订单', is_member: false, can_join: false, can_send: true,
  can_reopen: false, archived: false, self_member_id: null, members: [] }
const conversation = { ...empty, has_conversation: true, id: 1000000031,
  is_member: true, can_send: true, self_member_id: 11 }
const accepted = { conversation, messages: [{ id: 1000000032, seq: 1, conversation_id: conversation.id,
  sender_id: 11, text: '首条合成消息', created_at: '2026-11-20T10:00:00Z', revoked: false, reply_to_id: null }] }
const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6vSAAAAAASUVORK5CYII='), value => value.charCodeAt(0))
const mounted: Array<{ unmount(): void }> = []

function mockImageDecode(width = 1, height = 1) {
  const decode = vi.fn(async () => {})
  const createObjectURL = vi.fn((_image: Blob) => 'blob:test-image')
  const revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL
    static revokeObjectURL = revokeObjectURL
  })
  vi.stubGlobal('Image', class {
    naturalWidth = width
    naturalHeight = height
    decode = decode
  })
  return { decode, createObjectURL, revokeObjectURL }
}

function setup(state = empty) {
  request.mockImplementation(async config => {
    if (config.method === 'GET' && config.url === base) return state
    throw new Error('不应有额外请求')
  })
  const open = vi.fn()
  const wrapper = mount(V2OrderChatPane, { attachTo: document.body, props: { orderNo, production: true },
    global: { provide: { [openInternalChatPreview as symbol]: open } } })
  mounted.push(wrapper)
  return { wrapper, open }
}
beforeEach(() => { request.mockReset() })
afterEach(() => { mounted.splice(0).forEach(value => value.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it.each([true, false])('无群旧订单只读加载不建群、不加入，有发送权%s才显示首次发送', async canSend => {
  const { wrapper } = setup({ ...empty, can_send: canSend })
  await flushPromises()
  expect(wrapper.find('iframe').exists()).toBe(false)
  expect(wrapper.find('textarea').exists()).toBe(canSend)
  expect(wrapper.text()).not.toContain('加入会话失败')
  expect(request.mock.calls.every(([value]) => value.method === 'GET')).toBe(true)
})

it('首条发送确认丢失后保留正文和client_id，重复点击不并发重发，成功打开真实原群', async () => {
  const { wrapper, open } = setup()
  await flushPromises()
  await wrapper.get('textarea').setValue('首条合成消息')
  let fail!: (reason: Error) => void
  request.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
  const send = wrapper.get('[data-order-chat-first-send]')
  await send.trigger('click')
  await send.trigger('click')
  expect(request.mock.calls.filter(([value]) => value.method === 'POST')).toHaveLength(1)
  const first = request.mock.calls.slice(-1)[0]![0]
  expect(first.url).toBe(base + 'messages/')
  expect(first.data).toMatchObject({ text: '首条合成消息', client_id: expect.any(String) })
  fail(new NetworkError('发送超时'))
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('首条合成消息')
  expect(wrapper.text()).toContain('内部沟通暂时不可用')
  request.mockResolvedValueOnce(accepted)
  await send.trigger('click')
  await flushPromises()
  expect(request.mock.calls.filter(([value]) => value.method === 'POST').slice(-1)[0]![0].data).toEqual(first.data)
  expect(open).toHaveBeenCalledExactlyOnceWith(conversation.id)
  expect(request.mock.calls.some(([value]) => value.url.endsWith('/join/'))).toBe(false)
})

it('修改发送内容后换用新的client_id，避免原幂等键套到不同内容', async () => {
  const { wrapper } = setup()
  await flushPromises()
  await wrapper.get('textarea').setValue('第一次内容')
  request.mockRejectedValueOnce(new HttpError(503, '内部沟通暂时不可用'))
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  const first = request.mock.calls.slice(-1)[0]![0].data.client_id
  await wrapper.get('textarea').setValue('修改后的内容')
  request.mockRejectedValueOnce(new HttpError(503, '内部沟通暂时不可用'))
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  expect(request.mock.calls.slice(-1)[0]![0].data.client_id).not.toBe(first)
})

it('首次只发错标图片也按 PNG 内容发送，发送前不创建会话', async () => {
  const { createObjectURL, revokeObjectURL } = mockImageDecode()
  const { wrapper, open } = setup()
  await flushPromises()
  const input = wrapper.get<HTMLInputElement>('input[type=file]')
  expect(input.attributes('accept')).toBeUndefined()
  Object.defineProperty(input.element, 'files', { value: [new File([png], '合成图片.txt', { type: 'text/plain' })] })
  await input.trigger('change')
  await vi.waitFor(() => expect(wrapper.find('img[alt="待发送的聊天图片"]').exists()).toBe(true))
  expect(createObjectURL.mock.calls[0]?.[0].type).toBe('image/png')
  expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:test-image')
  expect(request.mock.calls.every(([value]) => value.method === 'GET')).toBe(true)
  request.mockResolvedValueOnce(accepted)
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  const payload = request.mock.calls.filter(([value]) => value.method === 'POST')[0]![0].data
  expect(payload).toMatchObject({ text: '', image: { mime: 'image/png', data: btoa(String.fromCharCode(...png)) } })
  expect(open).toHaveBeenCalledWith(conversation.id)
})

it('粘贴超过 3 MiB 的图片先压缩，普通文字粘贴保留浏览器默认处理', async () => {
  mockImageDecode(1500, 1500)
  const drawImage = vi.fn()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => {
    callback(new Blob(['compressed'], { type: 'image/webp' }))
  })
  const { wrapper } = setup()
  await flushPromises()
  const textarea = wrapper.get<HTMLTextAreaElement>('textarea').element
  const text = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(text, 'clipboardData', { value: { files: [], items: [{ kind: 'string' }] } })
  expect(textarea.dispatchEvent(text)).toBe(true)

  const large = new File([png, new Uint8Array(3 * 1024 * 1024)], '截图.bin')
  const image = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(image, 'clipboardData', { value: { files: [], items: [{ kind: 'file', getAsFile: () => large }] } })
  expect(textarea.dispatchEvent(image)).toBe(false)
  await vi.waitFor(() => expect(wrapper.get('img').attributes('src')).toBe(`data:image/webp;base64,${btoa('compressed')}`))
  expect(drawImage).toHaveBeenCalledTimes(1)
  request.mockResolvedValueOnce(accepted)
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  const payload = request.mock.calls.find(([value]) => value.method === 'POST')?.[0].data
  expect(payload).toMatchObject({ image: { mime: 'image/webp', data: btoa('compressed') } })
})

it.each([
  [new File(['x'], '非图片.txt', { type: 'text/plain' }), '无法读取这张图片', 2],
  [new File([new Uint8Array(10 * 1024 * 1024 + 1)], '过大图片.png', { type: 'image/png' }), '原图片不能超过 10 MiB', 0],
] as const)('非法图片不触发建群或发送 %s', async (file, message, calls) => {
  const { decode } = mockImageDecode()
  decode.mockRejectedValue(new Error('无法解码'))
  const { wrapper } = setup()
  await flushPromises()
  const input = wrapper.get<HTMLInputElement>('input[type=file]')
  Object.defineProperty(input.element, 'files', { value: [file] })
  await input.trigger('change')
  await vi.waitFor(() => expect(wrapper.get('[role=alert]').text()).toContain(message))
  expect(decode).toHaveBeenCalledTimes(calls)
  expect(wrapper.get('[data-order-chat-first-send]').attributes('disabled')).toBeDefined()
  expect(request.mock.calls.every(([value]) => value.method === 'GET')).toBe(true)
})

it('超过 1600 万像素的图片无法进入预览', async () => {
  mockImageDecode(4001, 4000)
  const { wrapper } = setup()
  await flushPromises()
  const input = wrapper.get<HTMLInputElement>('input[type=file]')
  Object.defineProperty(input.element, 'files', { value: [new File([png], '截图.png')] })
  await input.trigger('change')
  await vi.waitFor(() => expect(wrapper.get('[role=alert]').text()).toBe('图片分辨率过大'))
  expect(wrapper.find('img').exists()).toBe(false)
})

it('聊天临时停机再恢复时，保留失败发送草稿与原client_id', async () => {
  vi.useFakeTimers()
  const { wrapper } = setup()
  try {
    await flushPromises()
    await wrapper.get('textarea').setValue('待确认消息')
    request.mockRejectedValueOnce(new HttpError(503, '内部沟通暂时不可用'))
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    await flushPromises()
    const original = request.mock.calls.filter(([value]) => value.method === 'POST')[0]![0].data
    request.mockRejectedValueOnce(new HttpError(503, '内部沟通暂时不可用'))
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.text()).toContain('内部沟通暂时不可用')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('待确认消息')
    request.mockResolvedValueOnce(accepted)
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    await flushPromises()
    expect(request.mock.calls.filter(([value]) => value.method === 'POST').slice(-1)[0]![0].data).toEqual(original)
  } finally { vi.useRealTimers() }
})

it('另一个窗口并发建好原群时仍保留本窗口未发草稿，随后发送打开同一群', async () => {
  vi.useFakeTimers()
  const { wrapper, open } = setup()
  try {
    await flushPromises()
    await wrapper.get('textarea').setValue('本窗口草稿')
    request.mockResolvedValueOnce({ ...conversation, is_member: false, can_send: false, can_join: true })
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.find('iframe').exists()).toBe(true)
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('本窗口草稿')
    expect(request.mock.calls.some(([value]) => value.url.endsWith('/join/'))).toBe(false)
    request.mockResolvedValueOnce({ ...conversation, is_member: false, can_send: false, can_join: true })
    request.mockResolvedValueOnce(conversation)
    request.mockResolvedValueOnce(accepted)
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    await flushPromises()
    expect(open).toHaveBeenCalledExactlyOnceWith(conversation.id)
    expect(request.mock.calls.filter(([value]) => value.url.endsWith('/join/'))).toHaveLength(1)
  } finally { vi.useRealTimers() }
})

it('只改首尾空白而实际发送payload不变时仍复用原client_id', async () => {
  const { wrapper } = setup()
  await flushPromises()
  await wrapper.get('textarea').setValue('同一条消息')
  request.mockRejectedValueOnce(new NetworkError('发送超时'))
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  const original = request.mock.calls.filter(([value]) => value.method === 'POST')[0]![0].data
  await wrapper.get('textarea').setValue(' 同一条消息 ')
  request.mockRejectedValueOnce(new NetworkError('发送超时'))
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  expect(request.mock.calls.filter(([value]) => value.method === 'POST').slice(-1)[0]![0].data).toEqual(original)
})

it('空摘要后另一窗口抢先建群时，仅在当前发送动作中明确加入，再用原client_id重试一次', async () => {
  const { wrapper, open } = setup()
  await flushPromises()
  await wrapper.get('textarea').setValue('并发首发消息')
  request.mockRejectedValueOnce(new HttpError(409, '请先加入会话'))
  request.mockResolvedValueOnce({ ...conversation, is_member: false, can_send: false, can_join: true })
  request.mockResolvedValueOnce(conversation)
  request.mockResolvedValueOnce(accepted)
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  expect(request.mock.calls.map(([value]) => [value.method, value.url])).toEqual([
    ['GET', base], ['POST', base + 'messages/'], ['GET', base], ['POST', base + 'join/'], ['POST', base + 'messages/'],
  ])
  const posts = request.mock.calls.filter(([value]) => value.url === base + 'messages/')
  expect(posts[0]![0].data).toEqual(posts[1]![0].data)
  expect(open).toHaveBeenCalledExactlyOnceWith(conversation.id)
})

it.each([
  new HttpError(403, '订单权限已撤销'), new HttpError(503, '请先加入会话'),
  new HttpError(409, '其他冲突'), new BizError(409, '请先加入会话'),
])('其他失败不会触发自动加入或重复发送 %s', async reason => {
  const { wrapper } = setup()
  await flushPromises()
  await wrapper.get('textarea').setValue('合成失败消息')
  request.mockRejectedValueOnce(reason)
  await wrapper.get('[data-order-chat-first-send]').trigger('click')
  await flushPromises()
  expect(request.mock.calls.filter(([value]) => value.method === 'POST')).toHaveLength(1)
  expect(request.mock.calls.some(([value]) => value.url.endsWith('/join/'))).toBe(false)
})

it('旧消息确认在重开入口后迟到时不会重复打开或清掉新入口状态', async () => {
  vi.useFakeTimers()
  const { wrapper, open } = setup()
  try {
    await flushPromises()
    await wrapper.get('textarea').setValue('等待原确认')
    let oldAck!: (value: typeof accepted) => void
    request.mockImplementationOnce(() => new Promise(resolve => { oldAck = resolve }))
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    const first = request.mock.calls.filter(([value]) => value.method === 'POST')[0]![0].data
    request.mockRejectedValueOnce(new HttpError(503, '内部沟通暂时不可用'))
    await vi.advanceTimersByTimeAsync(3000)
    await wrapper.get('button').trigger('click')
    await flushPromises()
    request.mockResolvedValueOnce(accepted)
    await wrapper.get('[data-order-chat-first-send]').trigger('click')
    await flushPromises()
    expect(request.mock.calls.filter(([value]) => value.method === 'POST').slice(-1)[0]![0].data).toEqual(first)
    expect(open).toHaveBeenCalledTimes(1)
    oldAck(accepted)
    await flushPromises()
    expect(open).toHaveBeenCalledExactlyOnceWith(conversation.id)
  } finally { vi.useRealTimers() }
})
