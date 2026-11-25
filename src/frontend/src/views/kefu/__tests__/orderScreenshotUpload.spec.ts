import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import { claimScreenshot, fetchScreenshots, requestScreenshotTicket } from '@/api/screenshots'
import { putFileToStorage } from '@/layui-island/directUpload'
import { attachKefuOrderScreenshot, newScreenshotUpload } from '../orderScreenshotUpload'

vi.mock('@/api/screenshots', () => ({
  claimScreenshot: vi.fn(),
  fetchScreenshots: vi.fn(),
  requestScreenshotTicket: vi.fn(),
}))
vi.mock('@/layui-island/directUpload', () => ({ putFileToStorage: vi.fn() }))

const orderNo = '20260319000000000001'
const firstTicket = { screenshot_id: 71, upload_url: '/upload/71', key: 'test/71.png', expires_in: 900 }
const nextTicket = { screenshot_id: 72, upload_url: '/upload/72', key: 'test/72.png', expires_in: 900 }
const file = new File(['test'], '测试订单截图.png', { type: 'image/png' })
const missingRecord = new BizError(1000, '没有待认领的上传记录')

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(requestScreenshotTicket).mockResolvedValueOnce(firstTicket).mockResolvedValue(nextTicket)
  vi.mocked(putFileToStorage).mockResolvedValue(undefined)
  vi.mocked(claimScreenshot).mockResolvedValue({ id: firstTicket.screenshot_id })
})

describe('已录订单的截图补传', () => {
  it('存储确认缺失时重传同一张图，再认领原票据', async () => {
    const state = newScreenshotUpload(file)
    const missing = new BizError(1000, '文件还没上传成功')
    vi.mocked(claimScreenshot).mockRejectedValueOnce(missing)

    await expect(attachKefuOrderScreenshot(orderNo, state, true)).rejects.toBe(missing)
    await attachKefuOrderScreenshot(orderNo, state, true)

    expect(requestScreenshotTicket).toHaveBeenCalledExactlyOnceWith(orderNo, 'png', 0)
    expect(putFileToStorage).toHaveBeenCalledTimes(2)
    expect(putFileToStorage).toHaveBeenNthCalledWith(2, '/upload/71', file, expect.any(AbortSignal))
    expect(claimScreenshot).toHaveBeenLastCalledWith(orderNo, expect.objectContaining({ screenshot_id: 71 }))
    expect(fetchScreenshots).not.toHaveBeenCalled()
  })

  it('直传卡住时提示超时，重试继续使用原订单的票据', async () => {
    vi.useFakeTimers()
    try {
      const state = newScreenshotUpload(file)
      vi.mocked(putFileToStorage).mockImplementationOnce((_url, _file, signal) => new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
      }))

      const firstUpload = attachKefuOrderScreenshot(orderNo, state, true)
        .then(() => '', (error: unknown) => error)
      await vi.advanceTimersByTimeAsync(45_000)
      await expect(firstUpload).resolves.toMatchObject({ message: '截图上传超时，请重试' })
      expect(claimScreenshot).not.toHaveBeenCalled()

      await attachKefuOrderScreenshot(orderNo, state, true)

      expect(requestScreenshotTicket).toHaveBeenCalledExactlyOnceWith(orderNo, 'png', 0)
      expect(putFileToStorage).toHaveBeenNthCalledWith(2, '/upload/71', file, expect.any(AbortSignal))
      expect(claimScreenshot).toHaveBeenCalledExactlyOnceWith(orderNo, expect.objectContaining({ screenshot_id: 71 }))
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('回执丢失时通过原截图已就绪确认成功，避免再签发一份', async () => {
    const state = newScreenshotUpload(file)
    vi.mocked(claimScreenshot).mockRejectedValueOnce(new Error('测试网络中断'))
    vi.mocked(fetchScreenshots).mockResolvedValue({
      items: [{
        id: 71, img_type: 0, url: '/test/71.png', original_name: file.name,
        size_bytes: file.size, remark: '', uploaded_by: 2, created_at: '2026-03-19T00:00:00Z',
      }],
      total: 1,
    })

    await attachKefuOrderScreenshot(orderNo, state, true)

    expect(fetchScreenshots).toHaveBeenCalledExactlyOnceWith(orderNo)
    expect(requestScreenshotTicket).toHaveBeenCalledOnce()
    expect(putFileToStorage).toHaveBeenCalledOnce()
    expect(claimScreenshot).toHaveBeenCalledOnce()
  })

  it('待认领的记录已失效且没有就绪图时，为同一张订单换票重传', async () => {
    const state = newScreenshotUpload(file)
    vi.mocked(claimScreenshot).mockRejectedValueOnce(missingRecord)
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: [], total: 0 })

    await expect(attachKefuOrderScreenshot(orderNo, state, true)).rejects.toBe(missingRecord)
    await attachKefuOrderScreenshot(orderNo, state, true)

    expect(requestScreenshotTicket).toHaveBeenCalledTimes(2)
    expect(putFileToStorage).toHaveBeenNthCalledWith(2, '/upload/72', file, expect.any(AbortSignal))
    expect(claimScreenshot).toHaveBeenLastCalledWith(orderNo, expect.objectContaining({ screenshot_id: 72 }))
  })

  it('无法查看确认原图时保留原票据，不冒险重复发出', async () => {
    const state = newScreenshotUpload(file)
    vi.mocked(claimScreenshot).mockRejectedValue(missingRecord)
    vi.mocked(fetchScreenshots).mockRejectedValue(new Error('测试无访问权限'))

    await expect(attachKefuOrderScreenshot(orderNo, state, true)).rejects.toBe(missingRecord)
    await expect(attachKefuOrderScreenshot(orderNo, state, false)).rejects.toBe(missingRecord)

    expect(requestScreenshotTicket).toHaveBeenCalledOnce()
    expect(putFileToStorage).toHaveBeenCalledOnce()
    expect(claimScreenshot).toHaveBeenCalledTimes(2)
    expect(fetchScreenshots).toHaveBeenCalledExactlyOnceWith(orderNo)
  })

  it('后台已拒收并清理损坏的图片时允许更换附件', async () => {
    const state = newScreenshotUpload(file)
    const rejected = new BizError(1000, '图片无效或为空')
    vi.mocked(claimScreenshot).mockRejectedValueOnce(rejected)

    await expect(attachKefuOrderScreenshot(orderNo, state, true)).rejects.toBe(rejected)

    expect(state).toMatchObject({ canReplace: true, uploaded: false, ticket: null })
    expect(fetchScreenshots).not.toHaveBeenCalled()
  })
})
