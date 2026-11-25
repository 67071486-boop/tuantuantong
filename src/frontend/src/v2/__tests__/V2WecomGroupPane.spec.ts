// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
vi.mock('@/v2/V2DailiantongPane.vue', () => ({ default: { template: '<div>代练通内容</div>' } }))
vi.mock('@/v2/wecomArchive', () => ({
  archiveGroups: vi.fn().mockResolvedValue({ enabled: false, groups: [], complete: true }),
}))

import V2DetailFace from '../V2DetailFace.vue'

const ORDER = '20261113000000000001'
const groups = [
  { chat_id: 'group-a', name: '合成群 123', owner: '客服甲', members: [{ name: '成员甲' }] },
  { chat_id: 'group-b', name: '另一群 123', owner: '', members: [] },
]

describe('订单详情企微群聊', () => {
  beforeEach(() => { request.mockReset() })
  afterEach(() => vi.useRealTimers())

  it('只有切换企微标签才获取群并分别请求群码', async () => {
    request.mockImplementation((config: { method: string; data?: { chat_id: string } }) => {
      if (config.method === 'GET') return Promise.resolve({ groups })
      return Promise.resolve({ status: 'ready', qr_url: `https://example.test/${config.data?.chat_id}.png`, message: '' })
    })
    const wrapper = mount(V2DetailFace, {
      props: { orderNo: ORDER, hasDailiantongHistory: true },
      slots: { default: '订单内容' },
    })
    expect(wrapper.findAll('.dtl-tabs button').map((tab) => tab.text())).toEqual(['本系统订单', '企微群聊', '代练通'])
    expect(wrapper.text()).toContain('订单内容')
    expect(request).not.toHaveBeenCalled()
    await wrapper.get('[data-v2="tab-dlt"]').trigger('click')
    expect(request).not.toHaveBeenCalled()
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith({ url: `/api/wecom/orders/${ORDER}/groups/`, method: 'GET' })
    expect(request).toHaveBeenCalledWith({ url: `/api/wecom/orders/${ORDER}/group-qr/`, method: 'POST', data: { chat_id: 'group-a' } })
    expect(request).toHaveBeenCalledWith({ url: `/api/wecom/orders/${ORDER}/group-qr/`, method: 'POST', data: { chat_id: 'group-b' } })
    expect(wrapper.text()).toContain('匹配到多个群')
    expect(wrapper.text()).toContain('成员甲')
    const images = wrapper.findAll('[data-v2="group-qr"] img')
    expect(images.map((image) => image.attributes('src'))).toEqual(['https://example.test/group-a.png', 'https://example.test/group-b.png'])
    await wrapper.get('[data-v2="tab-dlt"]').trigger('click')
    expect(wrapper.text()).toContain('代练通内容')
    expect(wrapper.find('[data-v2="wecom-groups"]').exists()).toBe(false)
  })

  it('显示未匹配和群列表读取失败，不请求生成群码', async () => {
    request.mockResolvedValueOnce({ groups: [] }).mockRejectedValueOnce(new Error('network'))
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    expect(wrapper.findAll('.dtl-tabs button').map((tab) => tab.text())).toEqual(['本系统订单', '企微群聊'])
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('暂未找到群资料和二维码')
    expect(request).toHaveBeenCalledTimes(1)
    await wrapper.get('[data-v2="tab-local"]').trigger('click')
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('企微群资料加载失败')
    expect(request).toHaveBeenCalledTimes(2)
  })

  it.each([[5, 5], [6, 0]])('匹配 %i 个群时请求 %i 个群码', async (count, expectedPosts) => {
    const matched = Array.from({ length: count }, (_, index) => ({ chat_id: `group-${index}`, name: `合成群${index}`, owner: '', members: [] }))
    request.mockImplementation((config: { method: string }) => config.method === 'GET'
      ? Promise.resolve({ groups: matched.slice(0, 5), too_many: count > 5, next_after: count > 5 ? 5 : null })
      : Promise.resolve({ status: 'ready', qr_url: 'https://example.test/qr.png', message: '' }))
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(request.mock.calls.filter(([config]) => config.method === 'POST')).toHaveLength(expectedPosts)
    expect(wrapper.text().includes('超过 5 个')).toBe(count === 6)
    expect(wrapper.text().includes('暂未找到群资料和二维码')).toBe(false)
    expect(wrapper.findAll('[data-v2="group-qr"]')).toHaveLength(expectedPosts)
  })

  it('异常情况下返回六条候选时也不会请求群码', async () => {
    const matched = Array.from({ length: 6 }, (_, index) => ({ chat_id: `group-${index}`, name: `合成群${index}`, owner: '', members: [] }))
    request.mockResolvedValue({ groups: matched, too_many: false })
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('超过 5 个')
    expect(wrapper.text()).toContain('合成群0')
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('超限时可以翻页查看所有匹配群，不触发任何群码请求', async () => {
    const matched = Array.from({ length: 6 }, (_, index) => ({ chat_id: `group-${index}`, name: `合成群${index}`, owner: '', members: [] }))
    request.mockImplementation((config: { params?: { after: number } }) => Promise.resolve(config.params?.after === 5
      ? { groups: matched.slice(5), too_many: true, next_after: null }
      : { groups: matched.slice(0, 5), too_many: true, next_after: 5 }))
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成群0')
    expect(wrapper.text()).not.toContain('合成群5')
    expect(wrapper.text()).toContain('已暂停生成群码')
    const pageButtons = () => wrapper.get('nav[aria-label="客户群分页"]').findAll('button')
    expect(pageButtons()[0]?.attributes('disabled')).toBeDefined()
    await pageButtons()[1]?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成群5')
    expect(wrapper.text()).not.toContain('合成群0')
    expect(pageButtons()[1]?.attributes('disabled')).toBeDefined()
    expect(request).toHaveBeenLastCalledWith({ url: `/api/wecom/orders/${ORDER}/groups/`, method: 'GET', params: { after: 5 } })
    await pageButtons()[0]?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成群0')
    expect(request).toHaveBeenCalledTimes(3)
    expect(request.mock.calls.every(([config]) => config.method === 'GET')).toBe(true)
  })

  it('翻页期间匹配数从六个减少到五个时提示回首页，不按旧页生成群码', async () => {
    const matched = Array.from({ length: 5 }, (_, index) => ({ chat_id: `group-${index}`, name: `合成群${index}`, owner: '', members: [] }))
    request.mockResolvedValueOnce({ groups: matched, too_many: true, next_after: 5 })
      .mockResolvedValueOnce({ groups: [], too_many: false, next_after: null })
      .mockResolvedValueOnce({ groups: matched, too_many: false, next_after: null })
      .mockResolvedValue({ status: 'ready', qr_url: 'https://example.test/qr.png', message: '' })
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    await wrapper.get('nav[aria-label="客户群分页"]').findAll('button')[1]?.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('群列表已变化，请返回首页刷新')
    expect(wrapper.text()).not.toContain('匹配到的群超过 5 个')
    expect(request).toHaveBeenCalledTimes(2)
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成群0')
    expect(request.mock.calls.filter(([config]) => config.method === 'POST')).toHaveLength(5)
  })

  it('处理中会重试，离开标签后停止重试', async () => {
    vi.useFakeTimers()
    request.mockResolvedValueOnce({ groups: [groups[0]] }).mockResolvedValue({ status: 'pending', qr_url: '', message: '正在处理' })
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await Promise.resolve()
    await Promise.resolve()
    expect(request).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(3000)
    expect(request).toHaveBeenCalledTimes(3)
    await wrapper.get('[data-v2="tab-local"]').trigger('click')
    await vi.advanceTimersByTimeAsync(6000)
    expect(request).toHaveBeenCalledTimes(3)
  })

  it('生成结果不确定时不自动重试，图片失败时可手动重取', async () => {
    request.mockResolvedValueOnce({ groups }).mockResolvedValueOnce({ status: 'uncertain', qr_url: '', message: '创建结果待核实' }).mockResolvedValueOnce({ status: 'ready', qr_url: 'https://example.test/qr.png', message: '' }).mockResolvedValueOnce({ status: 'ready', qr_url: 'https://example.test/new.png', message: '' })
    const wrapper = mount(V2DetailFace, { props: { orderNo: ORDER } })
    await wrapper.get('[data-v2="tab-wecom"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('创建结果待核实')
    expect(wrapper.findAll('[data-v2="group-qr"]')[0]?.find('button').exists()).toBe(false)
    await wrapper.get('img').trigger('error')
    expect(wrapper.text()).toContain('群码图片加载失败')
    await wrapper.get('[data-v2="group-qr"] button').trigger('click')
    await flushPromises()
    expect(wrapper.get('img').attributes('src')).toBe('https://example.test/new.png')
  })
})
