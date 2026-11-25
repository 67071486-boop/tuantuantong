// @vitest-environment jsdom
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchAllGames } from '@/api/config'
import { request } from '@/api/http'
import { createUpstreamBindingRow, getUpstreamBindingRow, updateUpstreamBindingRow, type JidanpuUpstreamBindingRow } from '@/api/jidanpuUpstream'
import { fetchOrderSources } from '@/api/orderSources'
import { getPeerBinding, updatePeerBindingHallSettings, type PeerBindingRow } from '@/api/peerBindings'
import V2SourceSystem from '../V2SourceSystem.vue'

vi.mock('@/api/config', () => ({ fetchAllGames: vi.fn() }))
vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/api/jidanpuUpstream', () => ({
  createUpstreamBindingRow: vi.fn(), deleteUpstreamBinding: vi.fn(),
  getUpstreamBindingRow: vi.fn(), updateUpstreamBindingRow: vi.fn(),
}))
vi.mock('@/api/orderSources', () => ({ fetchOrderSources: vi.fn() }))
vi.mock('@/api/peerBindings', () => ({
  createPeerBinding: vi.fn(), deletePeerBinding: vi.fn(),
  getPeerBinding: vi.fn(), updatePeerBindingHallSettings: vi.fn(),
}))

const upstream = { name: '合成上家', account: 'synthetic', jidanpu_base_url: 'https://example.test' }
const jidanpu = {
  ...upstream, id: 17, enabled: true, order_source: null, hall_price_percent: 80, game_rules: null,
} as JidanpuUpstreamBindingRow
const peer = {
  id: 24, peer_name: '合成团团通', peer_base_url: 'https://peer.example.test',
  peer_account: 'synthetic', hall_sync_enabled: true, hall_price_percent: 75,
  direct_taker_assignment_enabled: false,
  game_rules: [{ game_id: 1, hall_price_percent: 61 }, { game_id: 2, hall_price_percent: null }],
} as PeerBindingRow

function game(id: number, name: string) {
  return { id, name, sort_order: id, is_active: true, created_at: '2026-10-01T00:00:00Z' }
}

function row(id: number, systemType: string) {
  return {
    id, system_type: systemType, system_name: '合成上家', base_url: upstream.jidanpu_base_url,
    account: 'synthetic', bound_by_name: '测试客服', bound_at: '2026-10-01T00:00:00Z',
    last_ok_at: null, last_error: '', last_error_at: null, consecutive_failures: 0,
  }
}

const save = '[data-v2="bind-save"]'
const all = 'input[data-v2="sync-all-games"]'
const restricted = 'input[data-v2="sync-selected-games"]'

async function openEdit(id: number) {
  const wrapper = mount(V2SourceSystem, { attachTo: document.body })
  await flushPromises()
  await wrapper.get(`[data-v2-edit-btn="${id}"]`).trigger('click')
  await flushPromises()
  return wrapper
}

async function selectGame(wrapper: VueWrapper, name: string) {
  const selector = wrapper.get('[data-v2="sync-game-picker"] .el-select__wrapper')
  if (selector.attributes('aria-expanded') !== 'true' && !selector.classes().includes('is-focused')) {
    await selector.trigger('click')
    await flushPromises()
  }
  const option = wrapper.findAll('[role="option"]').find((entry) => entry.text() === name)
  if (option === undefined) throw new Error(`找不到游戏选项 ${name}`)
  await option.trigger('click')
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(request).mockResolvedValue({ items: [row(17, 'jidanpu'), row(24, 'tuantuantong')] })
  vi.mocked(fetchAllGames).mockResolvedValue([game(1, '测试游戏甲'), game(2, '测试游戏乙')])
  vi.mocked(fetchOrderSources).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 })
  vi.mocked(getUpstreamBindingRow).mockResolvedValue(jidanpu)
  vi.mocked(getPeerBinding).mockResolvedValue(peer)
  vi.mocked(updateUpstreamBindingRow).mockResolvedValue(jidanpu)
  vi.mocked(updatePeerBindingHallSettings).mockResolvedValue(peer)
  vi.mocked(createUpstreamBindingRow).mockResolvedValue(jidanpu)
})

describe('上家绑定按游戏配置新订单同步', () => {
  it('鸡蛋铺选择游戏后才提交，比例留空继承默认', async () => {
    const wrapper = await openEdit(17)
    try {
      expect(wrapper.get<HTMLInputElement>(all).element.checked).toBe(true)
      await wrapper.get(restricted).setValue(true)
      await flushPromises()
      expect(wrapper.text()).toContain('已同步的订单保留原价')
      await wrapper.get(save).trigger('click')
      expect(wrapper.text()).toContain('请至少选择一个同步游戏')
      expect(updateUpstreamBindingRow).not.toHaveBeenCalled()
      await selectGame(wrapper, '测试游戏甲')
      await selectGame(wrapper, '测试游戏乙')
      const price = wrapper.get('input[data-v2-game-percent="1"]')
      await price.setValue('70.5')
      await wrapper.get(save).trigger('click')
      expect(updateUpstreamBindingRow).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('测试游戏甲的大厅价比例需要是 1 到 100 之间的整数')
      await price.setValue('65')
      await wrapper.get(save).trigger('click')
      await flushPromises()
      expect(updateUpstreamBindingRow).toHaveBeenCalledExactlyOnceWith(17, expect.objectContaining({
        password: '', enabled: true, hall_price_percent: 80,
        game_rules: [{ game_id: 1, hall_price_percent: 65 }, { game_id: 2, hall_price_percent: null }],
      }))
    } finally {
      wrapper.unmount()
    }
  })

  it('新增鸡蛋铺绑定时一次保存所选游戏及比例', async () => {
    const wrapper = mount(V2SourceSystem, { attachTo: document.body })
    try {
      await flushPromises()
      await wrapper.get('[data-v2="add"]').trigger('click')
      await wrapper.get('input[data-v2="url-input"]').setValue(upstream.jidanpu_base_url)
      await wrapper.get('input[data-v2="acc-input"]').setValue(upstream.account)
      await wrapper.get('input[data-v2="pwd-input"]').setValue('合成口令')
      await wrapper.get(restricted).setValue(true)
      await flushPromises()
      await selectGame(wrapper, '测试游戏乙')
      await wrapper.get('input[data-v2-game-percent="2"]').setValue('60')
      await wrapper.get(save).trigger('click')
      await flushPromises()
      expect(createUpstreamBindingRow).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
        game_rules: [{ game_id: 2, hall_price_percent: 60 }],
      }))
    } finally {
      wrapper.unmount()
    }
  })

  it('团团通回填两条游戏规则，切回全部时清除专属比例', async () => {
    const wrapper = await openEdit(24)
    try {
      expect(wrapper.get<HTMLInputElement>(restricted).element.checked).toBe(true)
      expect(wrapper.get<HTMLInputElement>('input[data-v2-game-percent="1"]').element.value).toBe('61')
      expect(wrapper.get<HTMLInputElement>('input[data-v2-game-percent="2"]').element.value).toBe('')
      await wrapper.get(all).setValue(true)
      expect(wrapper.find('[data-v2="sync-game-picker"]').exists()).toBe(false)
      await wrapper.get(save).trigger('click')
      await flushPromises()
      expect(updatePeerBindingHallSettings).toHaveBeenCalledExactlyOnceWith(24, {
        hall_sync_enabled: true, hall_price_percent: 75, game_rules: null,
      })
      expect(updateUpstreamBindingRow).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('未改游戏规则的既有绑定继续提交原有大厅设置', async () => {
    const wrapper = await openEdit(24)
    try {
      await wrapper.get(save).trigger('click')
      await flushPromises()
      expect(updatePeerBindingHallSettings).toHaveBeenCalledExactlyOnceWith(24, {
        hall_sync_enabled: true, hall_price_percent: 75,
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('只给选中的团团通上家绑定单独打开直派打手授权', async () => {
    const wrapper = await openEdit(24)
    try {
      const toggle = wrapper.get('[data-v2="peer-direct-assignment-switch"] [role="switch"]')
      expect(toggle.attributes('aria-checked')).toBe('false')
      await toggle.trigger('click')
      await wrapper.get(save).trigger('click')
      await flushPromises()
      expect(updatePeerBindingHallSettings).toHaveBeenCalledExactlyOnceWith(24, {
        direct_taker_assignment_enabled: true,
      })
      await wrapper.get('[data-v2-edit-btn="17"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-v2="peer-direct-assignment-switch"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })
})
