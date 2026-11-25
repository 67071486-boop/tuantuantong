import type { AxiosRequestConfig } from 'axios'
import { describe, expect, it, vi } from 'vitest'
import { createVisitorHistoryTransport } from '../visitorHistoryTransport'

const api = '/api/internal-chat/'
const old = { id: 32, conversation_id: 7, seq: 8 }
const latest = { id: 40, conversation_id: 7, seq: 9 }
const fresh = { id: 52, conversation_id: 7, seq: 10 }
const earlier = { id: 25, conversation_id: 7, seq: 7 }
const backendHistory = { messages: [old, latest, fresh], has_more: true, has_newer: false }

describe('visitor history and realtime delivery', () => {
  it.each(['guest', 'desk'] as const)('lets sync publish a new %s message before history includes it', async (scope) => {
    const base = api + 'visitor/' + scope + '/'
    const history = base + 'conversations/7/messages/'
    let summary = latest
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      switch (config.url) {
        case base + 'conversations/?limit=1&offset=0': return { conversations: [{ id: 1 }] }
        case base + 'conversations/?limit=1&offset=1': return { conversations: [{ id: 7, last_message: summary }] }
        case base + 'sync/?after=31':
          return { cursor: 32, changes: [{ message: fresh }] }
        case base + 'sync/?after=32': return { cursor: 32, changes: [] }
        case history + '?before=8': return { messages: [earlier], has_more: false, has_newer: true }
        case history + '?before=10': return { ...backendHistory, messages: [old, latest], has_newer: true }
        case history + '?before=11': return backendHistory
        default: return backendHistory
      }
    })
    const send = createVisitorHistoryTransport(scope, request as unknown as Parameters<typeof createVisitorHistoryTransport>[1])
    const get = (url: string) => send({ method: 'get', url })

    await get(base + 'conversations/?limit=1&offset=0')
    await get(base + 'conversations/?limit=1&offset=1')
    expect(await get(history)).toMatchObject({ messages: [old, latest], has_more: true, has_newer: false })
    expect(await get(base + 'sync/?after=31')).toMatchObject({ changes: [{ message: fresh }] })
    summary = fresh
    await get(base + 'conversations/?limit=1&offset=1')
    await get(base + 'sync/?after=31')
    expect(await get(history)).toMatchObject({ messages: [old, latest], has_newer: false })
    expect(await get(history + '?around=32')).toMatchObject({ messages: [old, latest], has_newer: false })
    expect(await get(history + '?around=52')).toEqual(backendHistory)
    expect(await get(history + '?before=8')).toEqual({ messages: [earlier], has_more: false, has_newer: true })
    expect(await get(history + '?before=50')).toMatchObject({ messages: [old, latest], has_newer: false })
    expect(await get(api + 'conversations/7/messages/')).toEqual(backendHistory)

    await get(base + 'sync/?after=32')
    expect(await get(history)).toEqual(backendHistory)
  })

  it('seeds an empty guest conversation as soon as the guest selects a seat', async () => {
    const base = api + 'visitor/guest/'
    const history = base + 'conversations/7/messages/'
    const first = { ...old, seq: 1 }
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      switch (config.url) {
        case base + 'conversations/': return { id: 7, last_message: null }
        case base + 'sync/?after=0': return { cursor: 1, changes: [{ message: first }] }
        case base + 'sync/?after=1': return { cursor: 1, changes: [] }
        case history + '?before=1': return { messages: [], has_newer: true }
        case history + '?before=2': return { messages: [first], has_newer: false }
        default: throw new Error(`unexpected URL ${config.url}`)
      }
    })
    const send = createVisitorHistoryTransport('guest', request as unknown as Parameters<typeof createVisitorHistoryTransport>[1])
    await send({ method: 'post', url: base + 'conversations/', data: { peer_id: 1 } })
    await send({ method: 'get', url: base + 'sync/?after=0' })
    expect(await send({ method: 'get', url: history })).toEqual({ messages: [], has_newer: false })
    await send({ method: 'get', url: base + 'sync/?after=1' })
    expect(await send({ method: 'get', url: history })).toEqual({ messages: [first], has_newer: false })
  })

  it('opens a newly transferred room with its older history and holds its pending realtime event', async () => {
    const base = api + 'visitor/desk/'
    const history = base + 'conversations/7/messages/'
    let transferred = false
    const request = vi.fn(async (config: AxiosRequestConfig) => {
      switch (config.url) {
        case base + 'conversations/':
          return { conversations: transferred ? [{ id: 7, last_message: fresh }] : [] }
        case base + 'sync/?after=31': return { cursor: 32, changes: [{ message: fresh }] }
        case history + '?before=10': return { ...backendHistory, messages: [old, latest], has_newer: true }
        default: return backendHistory
      }
    })
    const send = createVisitorHistoryTransport('desk', request as unknown as Parameters<typeof createVisitorHistoryTransport>[1])
    const get = (url: string) => send({ method: 'get', url })

    await get(base + 'conversations/')
    await get(base + 'sync/?after=31')
    transferred = true
    await get(base + 'conversations/')
    expect(await get(history)).toMatchObject({ messages: [old, latest], has_newer: false })
  })
})
