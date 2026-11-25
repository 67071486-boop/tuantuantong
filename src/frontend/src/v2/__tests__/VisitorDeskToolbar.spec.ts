// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import VisitorDeskToolbar from '../internalChat/VisitorDeskToolbar.vue'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

it('同步值班资格，接待时切换标签页仍保持值班状态', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const send = vi.mocked(request)
  let eligible = true
  let enabled = false
  let pendingRead: Promise<{ enabled: boolean }> | undefined
  let settleRead: ((status: { enabled: boolean }) => void) | undefined
  let readSignal: AbortSignal | undefined
  send.mockImplementation(async config => {
    expect(config.headers).toEqual({ 'X-Chat-Page-User': '71' })
    if (config.url?.endsWith('/desk/me/')) return { user: { id: 71 }, seat_mode: true, can_receive: eligible }
    if (config.method === 'post') enabled = (config.data as { enabled: boolean }).enabled
    else if (pendingRead) {
      readSignal = config.signal as AbortSignal
      return pendingRead
    }
    return { enabled }
  })
  const page = mount(VisitorDeskToolbar, {
    props: {
      viewerId: 71,
      seatMode: true,
      canReceive: true,
      conversation: { id: 9, peer: { id: 32, name: '访客 13900000001' }, seat_kind: 'hr', service_status: 'assigned' },
    },
    global: { stubs: { ElDialog: true } },
  })
  const button = (label: string) => page.findAll('button').find(item => item.text() === label)
  try {
    await flushPromises()
    expect(button('开始接待')?.classes()).toContain('cv2-btn--primary')
    expect(page.get('[role="status"]').text()).toBe('已停止接待')
    const pausedIcon = page.get('[role="status"] svg').html()
    expect(page.text()).toContain('访客 13900000001')
    expect(page.findAll('a')).toHaveLength(0)

    visibility.mockReturnValue('hidden')
    const pausedRequests = send.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(send).toHaveBeenCalledTimes(pausedRequests)
    visibility.mockReturnValue('visible')
    pendingRead = new Promise(resolve => { settleRead = resolve })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(readSignal?.aborted).toBe(false)
    await button('开始接待')!.trigger('click')
    await flushPromises()
    expect(readSignal?.aborted).toBe(true)
    settleRead?.({ enabled: false })
    await flushPromises()
    pendingRead = undefined
    expect(button('停止接待')?.classes()).toContain('cv2-btn--tint-amber')
    expect(page.get('[role="status"]').text()).toBe('接待中')
    expect(page.get('[role="status"] svg').html()).not.toBe(pausedIcon)

    visibility.mockReturnValue('hidden')
    const previousRequests = send.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(send.mock.calls.slice(previousRequests).map(([config]) => config.url)).toEqual([
      '/api/internal-chat/visitor/desk/me/',
      '/api/internal-chat/visitor/desk/availability/',
    ])
    enabled = false
    visibility.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(button('开始接待')).toBeDefined()

    eligible = false
    await vi.advanceTimersByTimeAsync(10_000)
    expect(page.emitted('receptionChanged')?.slice(-1)).toEqual([[true, false]])
  } finally {
    page.unmount()
    send.mockReset()
    visibility.mockRestore()
    vi.useRealTimers()
  }
})
