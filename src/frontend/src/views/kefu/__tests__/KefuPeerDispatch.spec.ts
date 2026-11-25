// @vitest-environment jsdom
import { createPinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { fetchPeerDirectAssignment, fetchPeerDraftStudios, searchPeerTakers, type KefuPeerSelection } from '@/api/peerDirectAssignment'
import KefuPeerDispatch from '../KefuPeerDispatch.vue'
import { pickSelect, provideFramework7 } from './dialogFixture'

vi.mock('@/api/peerDirectAssignment', () => ({
  fetchPeerDirectAssignment: vi.fn(),
  fetchPeerDraftStudios: vi.fn(),
  searchPeerTakers: vi.fn(),
}))

provideFramework7()
enableAutoUnmount(afterEach)
beforeEach(() => vi.resetAllMocks())

it('建单前可按账号片段找打手，选中后才交给表单', async () => {
  vi.mocked(fetchPeerDraftStudios).mockResolvedValue({ studios: [{ id: 3, name: '测试下家' }] })
  vi.mocked(searchPeerTakers).mockResolvedValue({ items: [
    { account_name: 'demo48263', match_token: 'preview-match-first' },
    { account_name: 'sample48263', match_token: 'preview-match-second' },
  ] })
  const update = vi.fn<(value: KefuPeerSelection | null) => void>()
  const wrapper = mount(KefuPeerDispatch, {
    props: { gameId: 2, modelValue: null, 'onUpdate:modelValue': update },
    attachTo: document.body,
    global: { plugins: [createPinia()] },
  })
  await flushPromises()
  await pickSelect(wrapper, '.kefu-picker-button', 3)
  const input = wrapper.get('input[id$="-account"]')
  await input.setValue('48263')
  await input.trigger('keydown.enter')
  await flushPromises()
  expect(searchPeerTakers).toHaveBeenCalledExactlyOnceWith(undefined, { game_id: 2, studio_id: 3, query: '48263' })
  const candidates = wrapper.findAll('.kefu-peer-results button')
  expect(candidates.map((candidate) => candidate.text())).toEqual(['demo48263', 'sample48263'])
  expect(update).not.toHaveBeenCalledWith(expect.objectContaining({ match_token: expect.any(String) }))
  await candidates[1]!.trigger('click')
  expect(update).toHaveBeenLastCalledWith({ studio_id: 3, account_name: 'sample48263', match_token: 'preview-match-second', expected_price: null })
})

it('打开已有待确认的订单时只提供原指派目标和金额', async () => {
  const orderNo = '20261118000000002100'
  vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
    studios: [], assignee: null, price: '60.00',
    pending: { studio_id: 3, studio_name: '测试下家', account_name: 'demo48263', price: '75.00' },
  })
  const update = vi.fn<(value: KefuPeerSelection | null) => void>()
  const wrapper = mount(KefuPeerDispatch, {
    props: { gameId: 2, orderNo, modelValue: null, 'onUpdate:modelValue': update },
    global: { plugins: [createPinia()] },
  })
  await flushPromises()
  expect(wrapper.get('[role=status]').text()).toContain('75.00 元')
  expect(wrapper.find('input').exists()).toBe(false)
  expect(update).toHaveBeenLastCalledWith({ studio_id: 3, account_name: 'demo48263', expected_price: '75.00' })
})

it('编辑时改选游戏，按新游戏加载工作室并搜索打手，沿用原订单价格', async () => {
  const orderNo = '20261118000000002101'
  vi.mocked(fetchPeerDirectAssignment).mockResolvedValue({
    studios: [{ id: 4, name: '新游戏下家' }], assignee: null, price: '60.00', pending: null,
  })
  vi.mocked(searchPeerTakers).mockResolvedValue({
    items: [{ account_name: 'demo48263', match_token: 'preview-new-game' }],
  })
  const update = vi.fn<(value: KefuPeerSelection | null) => void>()
  const wrapper = mount(KefuPeerDispatch, {
    props: { gameId: 3, originalGameId: 2, orderNo, modelValue: null, 'onUpdate:modelValue': update },
    attachTo: document.body,
    global: { plugins: [createPinia()] },
  })
  await flushPromises()
  expect(fetchPeerDirectAssignment).toHaveBeenCalledExactlyOnceWith(orderNo, 3)
  await pickSelect(wrapper, '.kefu-picker-button', 4)
  const input = wrapper.get('input[id$="-account"]')
  await input.setValue('48263')
  await input.trigger('keydown.enter')
  await flushPromises()
  expect(searchPeerTakers).toHaveBeenCalledExactlyOnceWith(undefined, { game_id: 3, studio_id: 4, query: '48263' })
  await wrapper.get('.kefu-peer-results button').trigger('click')
  expect(update).toHaveBeenLastCalledWith({
    studio_id: 4, account_name: 'demo48263', match_token: 'preview-new-game', expected_price: '60.00',
  })
})
