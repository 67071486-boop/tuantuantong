// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import V2XgjShopBinding from '../V2XgjShopBinding.vue'
import { fetchXgjBindings, fetchXgjShops } from '@/api/xgjBindings'

vi.mock('@/api/xgjBindings', () => ({
  fetchXgjBindings: vi.fn(), fetchXgjShops: vi.fn(), createXgjBinding: vi.fn(),
  deleteXgjBinding: vi.fn(), queryXgjOrderDetail: vi.fn(), syncXgjShops: vi.fn(),
  testXgjConnection: vi.fn(), updateXgjBinding: vi.fn(),
}))

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const writeText = vi.fn()
beforeEach(() => {
  vi.resetAllMocks()
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  vi.mocked(fetchXgjBindings).mockResolvedValue({ items: [{ id: 17, shop_name: '测试应用', app_key: 'synthetic', app_secret_set: true, order_push_url: 'https://example.test/order', product_push_url: 'https://example.test/product', remark: '' }], total: 1, page: 1, page_size: 200 })
  vi.mocked(fetchXgjShops).mockResolvedValue({ items: [] })
})
afterEach(() => {
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

describe('闲管家推送地址复制', () => {
  it('完整显示两条地址，使用订单页的复制图标，并隐藏重复的系统开关', async () => {
    const wrapper = mount(V2XgjShopBinding)
    await flushPromises()
    const urls = wrapper.findAll('.xgj-push-url').map((node) => node.text())
    expect(urls).toEqual(['https://example.test/order', 'https://example.test/product'])
    for (const kind of ['order', 'product']) {
      const button = wrapper.get(`[data-v2-copy-${kind}="17"]`)
      expect(button.classes()).toContain('cv2-copybtn')
      expect(button.find('svg').exists()).toBe(true)
    }
    expect(wrapper.find('[data-v2="switch-unpaid"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="switch-hall-sim"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2="switch-hall-preferred"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('已启用：按来源单号')
    wrapper.unmount()
  })

  it('两个按钮分别复制对应地址，并提示成功', async () => {
    writeText.mockResolvedValue(undefined)
    const wrapper = mount(V2XgjShopBinding)
    await flushPromises()
    await wrapper.get('[data-v2-copy-order="17"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith('https://example.test/order')
    expect(wrapper.text()).toContain('已复制推送地址')
    await wrapper.get('[data-v2-copy-product="17"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith('https://example.test/product')
    wrapper.unmount()
  })

  it('剪贴板失败时明确提示，不误报成功', async () => {
    writeText.mockRejectedValue(new Error('denied'))
    const wrapper = mount(V2XgjShopBinding)
    await flushPromises()
    await wrapper.get('[data-v2-copy-order="17"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('复制失败，请手动复制')
    expect(wrapper.text()).not.toContain('已复制推送地址')
    wrapper.unmount()
  })
})
