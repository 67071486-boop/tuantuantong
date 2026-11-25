// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchPage = vi.fn()
vi.mock('@/v2/orderRecycleApi', () => ({ fetchRecycleBinPage: (...args: unknown[]) => fetchPage(...args) }))
vi.mock('@/api/orders', () => ({ restoreOrder: vi.fn() }))
vi.mock('@/api/config', () => ({ fetchAllGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }))
vi.mock('@/api/orderSources', () => ({ fetchOrderSources: vi.fn(async () => ({ items: [] })) }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => ({ canNode: () => true }) }))

import V2OrderRecycle from '../V2OrderRecycle.vue'

beforeEach(() => {
  localStorage.clear()
  fetchPage.mockReset()
  fetchPage.mockResolvedValue({ items: [{
    order_no: '20261113000000000001', source_order_no: '', status: 'COMPLETED', game: 1,
    server: null, project_text: '示例项目', game_role_name: '角色甲', game_account: '',
    customer_ww: '', source: '示例来源', remark: '示例备注', deleted_at: '2026-11-13T12:00:00',
    deleted_by: '测试删除人', is_urgent: false, tag: '',
  }], total: 1, page: 1, page_size: 30 })
})

describe('找回删除订单', () => {
  it('搜索提示准确，关键词仍发送到查询且不再发送游戏账号', async () => {
    const wrapper = mount(V2OrderRecycle)
    await flushPromises()
    const keyword = wrapper.get('[data-v2-filter="key1"]')
    expect(keyword.attributes('placeholder')).toBe('订单号/角色名/手机号')
    expect(wrapper.find('[data-v2-filter="game-acc"]').exists()).toBe(false)
    await keyword.setValue('测试角色')
    await wrapper.get('[data-v2="search"]').trigger('click')
    await flushPromises()
    expect(fetchPage).toHaveBeenLastCalledWith(expect.objectContaining({ keyword: '测试角色' }))
    expect(fetchPage.mock.lastCall?.[0]).not.toHaveProperty('game_account')
    wrapper.unmount()
  })

  it('统计与按钮同排，删除时间保持原列号且备注紧邻删除人左侧', async () => {
    const wrapper = mount(V2OrderRecycle)
    await flushPromises()
    expect(wrapper.get('[data-v2="toolbar"]').find('[data-v2="total"]').text()).toContain('共1笔')
    expect(wrapper.get('[data-v2="toolbar"]').find('[data-v2="refresh"]').exists()).toBe(true)
    expect(wrapper.get('[data-v2="toolbar"]').find('[data-v2="restore-checked"]').exists()).toBe(true)
    const heads = wrapper.findAll('thead th').map((th) => th.text())
    const time = heads.indexOf('删除时间')
    expect(time).toBe(9)
    expect(heads.slice(time - 2, time + 2)).toEqual(['重要备注', '删除人', '删除时间', '旺旺/来源'])
    const cells = wrapper.get('[data-v2-row="20261113000000000001"]').findAll('td').map((td) => td.text())
    expect(cells.slice(time - 2, time + 2)).toEqual(['示例备注', '测试删除人', '2026-11-13 12:00', '示例来源'])
    wrapper.unmount()
  })
})
