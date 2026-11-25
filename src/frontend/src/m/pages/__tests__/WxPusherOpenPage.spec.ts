// @vitest-environment jsdom
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import WxPusherOpenPage from '../WxPusherOpenPage.vue'

const { query } = vi.hoisted(() => ({ query: {} as Record<string, string> }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query }) }))
enableAutoUnmount(afterEach)

const originalLocation = window.location
const assign = vi.fn()
const androidUa = 'Mozilla/5.0 Android/WxPusher-Android'
const appUrl = 'daidaitong://suqing.suqing.chat/m/wxpusher/open'

function setDevice(origin: string, userAgent: string): void {
  Object.defineProperty(window, 'location', { configurable: true, value: { origin, assign } })
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(userAgent)
}

beforeEach(() => {
  query.entry = 'wxpusher'
  delete query.continue
  assign.mockReset()
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
  vi.restoreAllMocks()
})

describe('WxPusher 扩展功能', () => {
  it('安卓扩展功能只保留可点的入口', () => {
    setDevice('https://suqing.suqing.chat', androidUa)
    assign.mockImplementation(() => { throw new Error('未安装') })

    const links = mount(WxPusherOpenPage).findAll('a')
    expect(assign).not.toHaveBeenCalled()
    expect(links.map((link) => link.attributes('href'))).toEqual([appUrl, '/m/?entry=wxpusher'])
  })

  it('确认回退走固定的后端地址，App 链接不附带确认信息', () => {
    setDevice('https://suqing.suqing.chat', androidUa)
    query.continue = 'confirm'
    const links = mount(WxPusherOpenPage).findAll('a')

    expect(assign).not.toHaveBeenCalled()
    expect(links.map((link) => link.attributes('href'))).toEqual([appUrl, '/api/auth/wxpusher/login'])
  })

  it.each([
    ['https://suqing.suqing.chat', 'iPhone/WxPusher-iOS', 'wxpusher'],
    ['https://other.example.invalid', androidUa, 'wxpusher'],
    ['https://suqing.suqing.chat', androidUa, 'external'],
  ])('普通浏览器、其它工作室和普通访问只使用网页（%s, %s, %s）', (origin, ua, entry) => {
    setDevice(origin, ua)
    query.entry = entry
    query.continue = 'https://external.example.invalid'
    const links = mount(WxPusherOpenPage).findAll('a')

    expect(assign).not.toHaveBeenCalled()
    expect(links.map((link) => link.attributes('href'))).toEqual(['/m/?entry=wxpusher'])
  })
})
