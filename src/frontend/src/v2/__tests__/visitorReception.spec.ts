// @vitest-environment jsdom
import { flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { HttpError } from '@/api/errors'
import { request } from '@/api/http'
import { createVisitorReception } from '../internalChat/visitorReception'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

it('聊天重开时识别新分配的客服席位，也能在权限移除后切回普通聊天', async () => {
  let allowed = false
  vi.mocked(request).mockImplementation(async ({ url }) => {
    switch (url) {
      case '/api/internal-chat/visitor/desk/me/':
        if (!allowed) throw new HttpError(403, '目前未配置访客席位')
        return { seat_mode: true, can_receive: true }
      case '/api/internal-chat/visitor/desk/availability/':
        return { enabled: false }
      case '/api/internal-chat/visitor/desk/unread/':
        return { unread_count: 3 }
      default:
        throw new Error('未匹配的聊天请求')
    }
  })
  const modeChanged = vi.fn()
  const reception = createVisitorReception(20, 'demo', {
    changed: vi.fn(), revoked: vi.fn(), gatewayConnected: () => true, modeChanged,
  })
  try {
    await reception.initial
    expect(reception.active()).toBe(false)
    expect(reception.legacy()).toBe(false)
    expect(await reception.readUnread()).toBe(0)
    expect(modeChanged).not.toHaveBeenCalled()

    allowed = true
    await reception.checkAccess()
    expect(reception.active()).toBe(true)
    expect(await reception.readUnread()).toBe(3)
    expect(modeChanged).toHaveBeenCalledExactlyOnceWith(false)

    allowed = false
    await reception.checkAccess()
    expect(reception.active()).toBe(false)
    expect(await reception.readUnread()).toBe(0)
    expect(modeChanged.mock.calls).toEqual([[false], [true]])
  } finally {
    reception.dispose()
  }
})

it('仍给只在旧接待名单上的员工保留原入口', async () => {
  vi.mocked(request).mockResolvedValue({ seat_mode: true, can_receive: false })
  const reception = createVisitorReception(20, 'demo', {
    changed: vi.fn(), revoked: vi.fn(), gatewayConnected: () => true, modeChanged: vi.fn(),
  })
  try {
    await reception.initial
    await flushPromises()
    expect(reception.active()).toBe(false)
    expect(reception.legacy()).toBe(true)
  } finally {
    reception.dispose()
  }
})
