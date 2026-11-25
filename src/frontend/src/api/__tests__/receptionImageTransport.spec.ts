import type { AxiosRequestConfig } from 'axios'
import { expect, it, vi } from 'vitest'
import { createReceptionChatTransport } from '../receptionChatTransport'

const root = '/api/internal-chat/'
const data = { mime: 'image/png', data: 'example' }

it('in both reception hosts, reads visitor and internal images using their original scoped IDs', async () => {
  const request = vi.fn(async ({ url }: AxiosRequestConfig) => {
    if (url === root + 'visitor/desk/conversations/') return { cursor: 1, conversations: [] }
    if (url === root + 'conversations/') return { cursor: 2, conversations: [{ id: 7, last_message: null }] }
    if (url?.endsWith('/image/')) return data
    throw new Error(`unexpected URL ${url}`)
  })

  for (const fullInternal of [false, true]) {
    request.mockClear()
    const send = createReceptionChatTransport(request as unknown as Parameters<typeof createReceptionChatTransport>[0], { fullInternal })
    const get = (path: string) => send({ path: root + path, method: 'get' })
    await get('conversations/')
    expect(await get('conversations/14/messages/6/image/')).toEqual(data)
    expect(await get('conversations/15/messages/7/image/')).toEqual(data)
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: root + 'visitor/desk/conversations/7/messages/3/image/' }))
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: root + 'conversations/7/messages/3/image/' }))
    const calls = request.mock.calls.length
    await expect(get('conversations/14/messages/7/image/')).rejects.toThrow('聊天图片不存在')
    await expect(get('conversations/15/messages/6/image/')).rejects.toThrow('聊天图片不存在')
    expect(request).toHaveBeenCalledTimes(calls)
  }
})
