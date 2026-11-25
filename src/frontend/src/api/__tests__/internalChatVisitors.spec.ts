import { describe, expect, it } from 'vitest'
import { visitorFrameRequest } from '../internalChatVisitors'

describe('visitor chat iframe request routes', () => {
  it('maps auth, conversation history and reads only to the selected visitor scope', () => {
    expect(visitorFrameRequest('guest', { method: 'get', path: '/api/auth/me' }))
      .toEqual({ method: 'get', url: '/api/internal-chat/visitor/guest/me/' })
    expect(visitorFrameRequest('desk', { method: 'get', path: '/api/internal-chat/conversations/8/messages/?limit=5&before=2' }))
      .toEqual({ method: 'get', url: '/api/internal-chat/visitor/desk/conversations/8/messages/?limit=5&before=2', data: undefined })
    const body = { seq: 2 }
    expect(visitorFrameRequest('guest', { method: 'post', path: '/api/internal-chat/conversations/8/read/', body }))
      .toEqual({ method: 'post', url: '/api/internal-chat/visitor/guest/conversations/8/read/', data: body })
  })

  it.each([
    ['post', '/api/auth/me'],
    ['get', 'https://example.com/api/auth/me'],
    ['get', '/api/internal-chat/settings/'],
    ['get', '/api/internal-chat/../auth/me'],
    ['get', '/api/internal-chat/visitor/desk/contacts/'],
    ['get', '/api/internal-chat/conversations/0/messages/17/image/'],
    ['get', '/api/internal-chat/conversations/4/messages/0/image/'],
    ['get', '/api/internal-chat/conversations/4/messages/17/image/?scope=desk'],
    ['post', '/api/internal-chat/conversations/4/messages/17/image/'],
    ['get', '/api/internal-chat/sync/#5'],
    ['post', '/api/internal-chat/conversations/8/messages/?bypass=1'],
  ])('rejects %s %s', (method, path) => {
    expect(visitorFrameRequest('guest', { method, path })).toBeUndefined()
  })

  it.each(['guest', 'desk'] as const)('loads a chat image through the current %s session', (scope) => {
    const image = { mime: 'image/png', data: 'test-image' }
    const path = '/api/internal-chat/conversations/4/messages/'
    expect(visitorFrameRequest(scope, { method: 'get', path: `${path}17/image/` }))
      .toEqual({ method: 'get', url: `/api/internal-chat/visitor/${scope}/conversations/4/messages/17/image/`, data: undefined })
    expect(visitorFrameRequest(scope, { method: 'post', path, body: { client_id: 'test', image } }))
      .toEqual({ method: 'post', url: `/api/internal-chat/visitor/${scope}/conversations/4/messages/`, data: { client_id: 'test', image } })
  })

  it.each([
    ['get', '/api/internal-chat/conversations/8/'],
    ['post', '/api/internal-chat/availability/'],
    ['post', '/api/internal-chat/conversations/8/transfer/'],
    ['post', '/api/internal-chat/conversations/8/close/'],
  ])('keeps host-only %s %s outside both iframe scopes', (method, path) => {
    expect(visitorFrameRequest('desk', { method, path })).toBeUndefined()
    expect(visitorFrameRequest('guest', { method, path })).toBeUndefined()
  })
})
