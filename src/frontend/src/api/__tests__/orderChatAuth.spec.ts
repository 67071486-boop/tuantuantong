import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AuthExpiredError } from '../errors'
import { fetchOrderChat, joinOrderChat, reopenOrderChat, sendOrderChatMessage } from '../orderChat'

const app = vi.hoisted(() => ({ hosted: false, grant: 'synthetic-chat-grant' as string | undefined, expire: vi.fn() }))
const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
vi.mock('@/m/components/appChatWebView', () => ({
  get appChatHosted() { return app.hosted },
  get appChatGrant() { return app.grant },
  expireAppChatGrant: app.expire,
}))
const no = '20261120000000000021'
beforeEach(() => { app.hosted = false; app.grant = 'synthetic-chat-grant'; app.expire.mockClear(); request.mockReset().mockResolvedValue({ id: 31, members: [] }) })
afterEach(() => { app.hosted = false })

it('普通H5摘要沿用会话认证，不添加App grant', async () => {
  await fetchOrderChat(no)
  expect(request).toHaveBeenCalledExactlyOnceWith({ method: 'GET', url: `/api/internal-chat/orders/${no}/` })
})

it('App壳摘要、显式加入、重开和首次消息共用Chat grant', async () => {
  app.hosted = true
  const payload = { text: '合成首条消息', client_id: 'synthetic-request-1' }
  await fetchOrderChat(no)
  await joinOrderChat(no)
  await reopenOrderChat(no)
  await sendOrderChatMessage(no, payload)
  expect(request).toHaveBeenCalledTimes(4)
  for (const [config] of request.mock.calls) {
    expect(config).toMatchObject({ baseURL: '', headers: { Authorization: 'Chat synthetic-chat-grant' }, skipUnauthorizedHandler: true })
  }
  expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ data: payload }))
})

it('Chat grant已失效时停止请求，不退回普通网页登录认证', async () => {
  app.hosted = true
  request.mockRejectedValueOnce(new AuthExpiredError('聊天登录已过期'))
  await expect(fetchOrderChat(no)).rejects.toThrow(AuthExpiredError)
  expect(app.expire).toHaveBeenCalledTimes(1)
  app.grant = undefined
  await expect(sendOrderChatMessage(no, { text: '合成消息', client_id: 'synthetic-request-2' })).rejects.toThrow(AuthExpiredError)
  expect(request).toHaveBeenCalledTimes(1)
})

it('只有明确的无会话摘要可作为首次发送入口，坏响应不能伪装空群', async () => {
  request.mockResolvedValueOnce({})
  await expect(fetchOrderChat(no)).rejects.toThrow('聊天摘要返回异常')
})
