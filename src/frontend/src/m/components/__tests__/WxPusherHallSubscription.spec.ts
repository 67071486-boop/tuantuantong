// @vitest-environment jsdom
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import WxPusherHallSubscription from '../WxPusherHallSubscription.vue'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

const URL = '/api/auth/wxpusher/hall-subscription'
const configRow = { sort_order: 1, is_active: true, created_at: '2026-01-01T00:00:00Z' }
const mockRequest = vi.mocked(request)
const enabledSwitch = 'input[role="switch"][aria-label="新单推送"]'
const gameOneCheckbox = 'input[name="hall-push-game-101"]'
const gameTwoCheckbox = 'input[name="hall-push-game-102"]'

enableAutoUnmount(afterEach)
installFramework7ForTest()

beforeEach(() => {
  vi.resetAllMocks()
  mockRequest.mockImplementation(async (config) => {
    if (config.url === '/api/auth/me') return { user: { game_ids: [101, 102, 104] } }
    if (config.url === '/api/business/games/') return { items: [
      { ...configRow, id: 101, name: '测试游戏甲' },
      { ...configRow, id: 102, name: '测试游戏乙' },
      { ...configRow, id: 103, name: '无游戏权限' },
      { ...configRow, id: 104, name: '已停用游戏', is_active: false },
    ] }
    if (config.url === URL && config.method === 'GET') return { enabled: false, games: [] }
    if (config.url === URL && config.method === 'PATCH') return config.data
    throw new Error(`未预期的请求 ${config.url}`)
  })
})

describe('打手抢单订阅', () => {
  it('默认关闭，选择有权限的游戏，可分别保存精确规则或订阅全部新单', async () => {
    const page = mount(WxPusherHallSubscription, { attachTo: document.body })
    await flushPromises()

    expect(page.get(enabledSwitch).element).toHaveProperty('checked', false)
    expect(page.text()).not.toContain('无游戏权限')
    expect(page.text()).not.toContain('已停用游戏')
    expect(page.text()).not.toContain('区服')
    expect(page.text()).not.toContain('优选')
    expect(page.text()).not.toContain('来源')

    await page.get(enabledSwitch).setValue(true)
    expect(page.get(gameOneCheckbox).element).toHaveProperty('checked', false)
    expect(page.get(gameTwoCheckbox).element).toHaveProperty('checked', false)
    expect(page.text()).toContain('请先选择要订阅的游戏')
    await page.get('button[type="submit"]').trigger('click')
    await flushPromises()
    expect(mockRequest).toHaveBeenLastCalledWith({ url: URL, method: 'PATCH', data: { enabled: true, games: [] } })

    await page.get(gameOneCheckbox).setValue(true)
    expect(page.get('#hall-push-game-101-filter').attributes('aria-expanded')).toBe('true')
    expect(page.text()).toContain('消息过多容易错过内部沟通消息')
    await page.get('#hall-push-price-min-101').setValue('0')
    await page.get('#hall-push-price-max-101').setValue('120.50')
    await page.get('#hall-push-include-101').setValue('排位，铂金 排位')
    await page.get('#hall-push-exclude-101').setValue('急单')
    await page.get(gameTwoCheckbox).setValue(true)
    expect(page.get('#hall-push-game-101-filter').attributes('aria-expanded')).toBe('false')
    expect(page.get('#hall-push-game-102-filter').attributes('aria-expanded')).toBe('true')
    await page.get('button[type="submit"]').trigger('click')
    await flushPromises()

    expect(mockRequest).toHaveBeenCalledWith({ url: URL, method: 'PATCH', data: {
      enabled: true,
      games: [
        { game_id: 101, price_min: '0', price_max: '120.50', include_keywords: ['排位', '铂金'], exclude_keywords: ['急单'] },
        { game_id: 102, price_min: null, price_max: null, include_keywords: [], exclude_keywords: [] },
      ],
    } })
    expect(page.text()).toContain('订阅设置已保存')
    expect(page.text()).toContain('全部新单')
  })

  it('价格区间无效时保留编辑内容并给出明确提示', async () => {
    const page = mount(WxPusherHallSubscription)
    await flushPromises()
    await page.get(gameOneCheckbox).setValue(true)
    await page.get('#hall-push-price-min-101').setValue('100')
    await page.get('#hall-push-price-max-101').setValue('99.99')

    await page.get('form').trigger('submit')

    expect(page.get('[role="alert"]').text()).toContain('测试游戏甲的最低价不能高于最高价')
    expect(page.get('#hall-push-price-min-101').element).toHaveProperty('value', '100')
    expect(mockRequest.mock.calls.some(([config]) => config.method === 'PATCH')).toBe(false)
  })

  it('关闭总开关保留选中的游戏和筛选规则，取消游戏时只移除该游戏', async () => {
    const baseMock = mockRequest.getMockImplementation()!
    const first = { game_id: 101, price_min: '88.00', price_max: null, include_keywords: ['排位'], exclude_keywords: [] }
    const second = { game_id: 102, price_min: null, price_max: null, include_keywords: [], exclude_keywords: [] }
    mockRequest.mockImplementation(async (config) => {
      if (config.url === URL && config.method === 'GET') return { enabled: true, games: [first, second] }
      return baseMock(config)
    })
    const page = mount(WxPusherHallSubscription)
    await flushPromises()

    await page.get(enabledSwitch).setValue(false)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(mockRequest).toHaveBeenLastCalledWith({ url: URL, method: 'PATCH', data: { enabled: false, games: [first, second] } })

    await page.get(gameTwoCheckbox).setValue(false)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(mockRequest).toHaveBeenLastCalledWith({ url: URL, method: 'PATCH', data: { enabled: false, games: [first] } })
  })

  it('正在保存时禁用订阅控件，失败后保留草稿以便重试', async () => {
    const baseMock = mockRequest.getMockImplementation()!
    let rejectSave!: (error: Error) => void
    const pending = new Promise<never>((_, reject) => { rejectSave = reject })
    mockRequest.mockImplementation(async (config) => {
      if (config.url === URL && config.method === 'PATCH') return pending
      return baseMock(config)
    })
    const page = mount(WxPusherHallSubscription)
    await flushPromises()
    await page.get(gameOneCheckbox).setValue(true)
    await page.get('#hall-push-price-min-101').setValue(' 0 ')

    await page.get('form').trigger('submit')
    for (const selector of [enabledSwitch, gameOneCheckbox, '#hall-push-game-101-filter', '#hall-push-price-min-101', 'button[type="submit"]']) {
      expect(page.get(selector).element).toHaveProperty('disabled', true)
    }
    await page.get('form').trigger('submit')
    expect(mockRequest.mock.calls.filter(([config]) => config.method === 'PATCH')).toHaveLength(1)
    rejectSave(new Error('保存失败'))
    await flushPromises()
    expect(page.get('[role="alert"]').text()).toBe('保存失败')
    expect(page.get('#hall-push-price-min-101').element).toHaveProperty('value', '0')

    mockRequest.mockImplementation(baseMock)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(page.get('[role="status"]').text()).toBe('订阅设置已保存。')
    await page.get(enabledSwitch).setValue(true)
    expect(page.find('[role="status"]').exists()).toBe(false)
  })
})
