// @vitest-environment jsdom
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchScreenshots } from '@/api/screenshots'
import KefuOrderScreenshot from '../KefuOrderScreenshot.vue'

vi.mock('@/api/screenshots', () => ({ fetchScreenshots: vi.fn() }))

const PNG = Uint8Array.from(
  atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aSRsAAAAASUVORK5CYII='),
  (byte) => byte.charCodeAt(0),
)

class ImageWithoutDecoder {
  onerror: (() => void) | null = null
  set src(_src: string) {
    queueMicrotask(() => this.onerror?.())
  }

  decode() {
    return decodeImage()
  }
}

const decodeImage = vi.fn<() => Promise<void>>()

function screenshot(name: string, type = 'image/png'): File {
  return new File([PNG], name, { type })
}

function pasteScreenshot(element: Element, ...files: File[]): Event {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', {
    value: { items: files.map((file) => ({ kind: 'file', type: file.type, getAsFile: () => file })) },
  })
  element.dispatchEvent(event)
  return event
}

function mountField(props: { orderNo?: string; canViewExisting?: boolean } = {}) {
  const wrapper = mount(KefuOrderScreenshot, {
    props: {
      ...props,
      modelValue: [],
      'onUpdate:modelValue': (files: File[]) => void wrapper.setProps({ modelValue: files }),
      'onUpdate:busy': (busy: boolean) => void wrapper.setProps({ busy }),
    },
  })
  return wrapper
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  vi.mocked(fetchScreenshots).mockReset().mockResolvedValue({ items: [], total: 0 })
  decodeImage.mockReset().mockResolvedValue(undefined)
  vi.stubGlobal('Image', ImageWithoutDecoder)
  let nextUrlId = 0
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => `blob:order-screenshot-${++nextUrlId}`)
    static revokeObjectURL = vi.fn()
  })
})

afterEach(() => vi.unstubAllGlobals())

describe('快捷录单上传订单截图', () => {
  it('粘贴无扩展名图片就显示缩略图，移除后不再提交截图', async () => {
    const wrapper = mountField()
    const pasted = pasteScreenshot(wrapper.get('[aria-label="粘贴订单截图"]').element, screenshot('截图'))

    expect(pasted.defaultPrevented).toBe(true)
    await vi.waitFor(() => {
      expect(wrapper.get('img[alt="订单截图预览"]').attributes('src')).toMatch(/^blob:/)
    })
    expect(wrapper.props('modelValue')).toMatchObject([{ name: '截图.png', type: 'image/png' }])

    const previewUrl = wrapper.get('img[alt="订单截图预览"]').attributes('src')
    await wrapper.get('button[aria-label="移除订单截图"]').trigger('click')

    expect(wrapper.props('modelValue')).toEqual([])
    expect(wrapper.find('img').exists()).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(previewUrl)
  })

  it('可以点选图片，换到无效文件时保留原图并提示原因', async () => {
    const wrapper = mountField()
    const input = wrapper.get<HTMLInputElement>('input[type="file"]')
    const openPicker = vi.spyOn(input.element, 'click')
    await wrapper.get('button[aria-label="选择订单截图"]').trigger('click')
    expect(openPicker).toHaveBeenCalledOnce()

    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [screenshot('测试图片.png', 'application/octet-stream')],
    })
    await input.trigger('change')
    await vi.waitFor(() => {
      expect(wrapper.get('img[alt="订单截图预览"]').isVisible()).toBe(true)
    })

    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['不是图片'], '测试无效.png', { type: 'image/png' })],
    })
    await input.trigger('change')
    await vi.waitFor(() => expect(wrapper.get('[role="status"]').text()).toContain('仅支持 JPG 和 PNG'))

    expect(wrapper.props('modelValue')).toMatchObject([{ name: '测试图片.png', type: 'image/png' }])
    expect(wrapper.get('img[alt="订单截图预览"]').isVisible()).toBe(true)
    const previewUrl = wrapper.get('img[alt="订单截图预览"]').attributes('src')
    wrapper.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(previewUrl)
  })

  it('图片只有 PNG 文件头且无法解码时保留上一张有效截图', async () => {
    const wrapper = mountField()
    const pasteInput = wrapper.get('[aria-label="粘贴订单截图"]').element
    pasteScreenshot(pasteInput, screenshot('测试原图.png'))
    await vi.waitFor(() => expect(wrapper.props('modelValue')[0]?.name).toBe('测试原图.png'))

    decodeImage.mockRejectedValueOnce(new Error('图片解码失败'))
    const corrupt = new File([PNG.slice(0, 8)], '测试损坏.png', { type: 'image/png' })
    pasteScreenshot(pasteInput, corrupt)

    await vi.waitFor(() => expect(wrapper.get('[role="status"]').text()).toContain('无法读取截图'))
    expect(wrapper.props('modelValue')[0]?.name).toBe('测试原图.png')
    expect(wrapper.get('img[alt="订单截图预览"]').isVisible()).toBe(true)
  })
  it('一次选择和粘贴都能追加多张图，缩略图在上传行下方且可以单独移除', async () => {
    const wrapper = mountField()
    const input = wrapper.get<HTMLInputElement>('input[type="file"]')
    expect(input.element.multiple).toBe(true)
    Object.defineProperty(input.element, 'files', { value: [screenshot('测试图片甲.png'), screenshot('测试图片乙.png')] })
    await input.trigger('change')
    await vi.waitFor(() => expect(wrapper.findAll('img')).toHaveLength(2))

    pasteScreenshot(wrapper.get('[aria-label="粘贴订单截图"]').element, screenshot('测试图片丙.png'), screenshot('测试图片丁.png'))
    await vi.waitFor(() => expect(wrapper.findAll('img')).toHaveLength(4))
    expect(wrapper.get('.kefu-shot-row').find('img').exists()).toBe(false)
    expect(wrapper.get('.kefu-shot-row').element.nextElementSibling?.querySelectorAll('img')).toHaveLength(4)

    const previews = wrapper.findAll('img').map((img) => img.attributes('src'))
    vi.mocked(URL.revokeObjectURL).mockClear()
    await wrapper.findAll('button[aria-label="移除订单截图"]')[1]!.trigger('click')
    expect(wrapper.props('modelValue').map((file: File) => file.name)).toEqual(['测试图片甲.png', '测试图片丙.png', '测试图片丁.png'])
    expect(wrapper.findAll('img').map((img) => img.attributes('src'))).toEqual([previews[0], previews[2], previews[3]])
    expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith(previews[1])
  })

  it('编辑订单展示已保存截图并把它们计入每单上限', async () => {
    const items = Array.from({ length: 19 }, (_, i) => ({
      id: i + 1, img_type: 0, url: `/测试图片${i}.png`, original_name: '测试截图.png',
      uploaded_by: 1, created_at: '', remark: '', size_bytes: 100,
    }))
    items.push({ ...items[0]!, id: 20, img_type: 1 })
    vi.mocked(fetchScreenshots).mockResolvedValue({ items, total: items.length })
    const wrapper = mountField({ orderNo: '20261118000000000001', canViewExisting: true })
    await vi.waitFor(() => expect(wrapper.findAll('img')).toHaveLength(19))
    expect(fetchScreenshots).toHaveBeenCalledExactlyOnceWith('20261118000000000001')

    pasteScreenshot(wrapper.get('[aria-label="粘贴订单截图"]').element, screenshot('测试图片甲.png'), screenshot('测试图片乙.png'))
    await vi.waitFor(() => expect(wrapper.findAll('img')).toHaveLength(20))
    expect(wrapper.props('modelValue')).toMatchObject([{ name: '测试图片甲.png' }])
    expect(wrapper.get('[role="status"]').text()).toContain('20 张截图')
  })

  it('只有上传权限时不会请求已保存截图', async () => {
    const wrapper = mountField({ orderNo: '20261118000000000001' })
    await wrapper.vm.$nextTick()
    expect(fetchScreenshots).not.toHaveBeenCalled()
  })

})
