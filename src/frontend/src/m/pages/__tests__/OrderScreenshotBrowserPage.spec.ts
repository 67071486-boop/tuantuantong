// @vitest-environment jsdom

import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { HttpError, NetworkError } from '@/api/errors'
import { request } from '@/api/http'
import { installMGuards } from '../../router/guards'
import OrderScreenshotBrowserPage from '../OrderScreenshotBrowserPage.vue'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {}, CSRF_HEADER_NAME: 'X-CSRFToken' }))
vi.mock('@/layui-island/imageCompress', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/layui-island/imageCompress')>(),
  compressImage: vi.fn(async (file: File) => file),
}))

const ORDER = '10000000000000000031'
const PATH = `/orders/${ORDER}/screenshots/browser`
const CHALLENGE = 'a'.repeat(43)

installFramework7ForTest()
enableAutoUnmount(afterEach)
afterEach(() => {
  vi.unstubAllGlobals()
  vi.mocked(request).mockReset()
})

async function openPage(url = `${PATH}#${CHALLENGE}`) {
  const router = createRouter({
    history: createMemoryHistory('/m/'),
    routes: [{
      path: '/orders/:orderNo(\\d{20})/screenshots/browser',
      name: 'm-order-screenshot-browser',
      component: OrderScreenshotBrowserPage,
      meta: { requiresAuth: false },
    }],
  })
  installMGuards(router)
  const pinia = createPinia()
  // 首次导航必须能在没有登录会话的条件下进入专用页面。
  setActivePinia(pinia)
  await router.push(url)
  const wrapper = mount({ template: '<RouterView />' }, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { router, wrapper }
}

describe('外部浏览器订单截图页', () => {
  it('在 WxPusher 保留原单号和链接片段，等待系统菜单打开', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0/WxPusher-Android' })
    const { wrapper, router } = await openPage()
    expect(wrapper.get('[data-testid="shot-browser-instructions"]').text()).toContain('在浏览器中打开')
    expect(wrapper.find('[data-testid="shot-album-btn"]').exists()).toBe(false)
    expect(router.currentRoute.value.fullPath).toBe(`${PATH}#${CHALLENGE}`)
    expect(request).not.toHaveBeenCalled()
  })

  it('不用网页登录即可兑换，清除链接秘密后为本单上传', async () => {
    const storage = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', storage)
    vi.mocked(request).mockImplementation(async ({ url }) => {
      if (url === '/api/business/screenshot-browser/exchange') return null
      if (url === '/api/business/screenshot-browser/ticket') {
        return { screenshot_id: 9, upload_url: '/blob/test.jpg' }
      }
      if (url === '/api/business/screenshot-browser/claim') return { id: 9 }
      throw new Error(`未预期的请求 ${url}`)
    })
    const { wrapper, router } = await openPage()
    expect(router.currentRoute.value.fullPath).toBe(PATH)
    expect(wrapper.find('[data-testid="shot-camera-btn"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="shot-grid"]').exists()).toBe(false)
    const album = wrapper.get<HTMLInputElement>('[data-testid="shot-album-input"]')
    Object.defineProperty(album.element, 'files', {
      value: [new File(['test'], '测试订单.jpg', { type: 'image/jpeg' })],
    })
    await album.trigger('change')
    await flushPromises()
    expect(wrapper.get('[data-testid="shot-browser-success"]').text()).toBe('已上传 1 张截图')
    expect(storage).toHaveBeenCalledWith('/blob/test.jpg', expect.objectContaining({ method: 'PUT' }))
    expect(vi.mocked(request).mock.calls.map(([config]) => [config.url, config.data])).toEqual([
      ['/api/business/screenshot-browser/exchange', { order_no: ORDER, challenge: CHALLENGE }],
      ['/api/business/screenshot-browser/ticket', { order_no: ORDER, ext: 'jpg' }],
      ['/api/business/screenshot-browser/claim', { order_no: ORDER, screenshot_id: 9, original_name: '测试订单.jpg' }],
    ])
  })

  it('响应丢失后可重试，先恢复浏览器里已经兑换成功的授权', async () => {
    vi.mocked(request).mockRejectedValueOnce(new NetworkError('网络异常，请检查连接'))
      .mockResolvedValueOnce(null)
    const { wrapper, router } = await openPage()
    expect(router.currentRoute.value.fullPath).toBe(PATH)
    expect(wrapper.text()).not.toContain(CHALLENGE)
    await wrapper.get('[data-testid="shot-browser-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-album-btn"]').exists()).toBe(true)
    expect(vi.mocked(request).mock.calls.map(([config]) => config.url)).toEqual([
      '/api/business/screenshot-browser/exchange',
      '/api/business/screenshot-browser/status',
    ])
    expect(vi.mocked(request).mock.calls[1]?.[0]).toMatchObject({
      headers: { 'X-Screenshot-Challenge': CHALLENGE },
    })
  })

  it('会话校验刷新后可重试，未兑换时用原链接继续', async () => {
    const refreshError = new HttpError(403, '会话校验已刷新，请重试')
    refreshError.csrfRefreshed = true
    vi.mocked(request).mockRejectedValueOnce(refreshError)
      .mockRejectedValueOnce(new HttpError(403, '上传链接已失效，请回到 WxPusher 重新生成'))
      .mockResolvedValueOnce(null)
    const { wrapper, router } = await openPage()
    expect(router.currentRoute.value.fullPath).toBe(PATH)
    await wrapper.get('[data-testid="shot-browser-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="shot-album-btn"]').exists()).toBe(true)
    expect(vi.mocked(request).mock.calls[2]?.[0]).toMatchObject({
      url: '/api/business/screenshot-browser/exchange',
      data: { order_no: ORDER, challenge: CHALLENGE },
    })
  })

  it('失效链接清除片段，提示回 WxPusher 生成新链接', async () => {
    vi.mocked(request).mockRejectedValue(new HttpError(403, '上传链接已失效，请回到 WxPusher 重新生成'))
    const { wrapper, router } = await openPage()
    expect(router.currentRoute.value.fullPath).toBe(PATH)
    expect(wrapper.get('[role="alert"]').text()).toContain('回到 WxPusher 重新生成')
    expect(wrapper.find('[data-testid="shot-browser-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="shot-album-btn"]').exists()).toBe(false)
  })
})
