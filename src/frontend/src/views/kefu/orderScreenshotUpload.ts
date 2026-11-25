import { BizError } from '@/api/errors'
import { claimScreenshot, fetchScreenshots, requestScreenshotTicket, type ScreenshotTicket } from '@/api/screenshots'
import { ErrorCode } from '@/config/errorCodes'
import { putFileToStorage } from '@/layui-island/directUpload'

export type ScreenshotUpload = {
  file: File
  ticket: ScreenshotTicket | null
  ticketExpiresAt: number
  uploaded: boolean
  canReplace: boolean
}

const ORDER_SCREENSHOT = 0
const SCREENSHOT_UPLOAD_TIMEOUT_MS = 45_000
const REJECTED_IMAGE_MESSAGES = new Set([
  '图片无效或为空',
  '图片超过 18 MB',
  '只支持 jpg / jpeg / png',
])

export function newScreenshotUpload(file: File): ScreenshotUpload {
  return { file, ticket: null, ticketExpiresAt: 0, uploaded: false, canReplace: false }
}

async function uploadWithDeadline(uploadUrl: string, file: File): Promise<void> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), SCREENSHOT_UPLOAD_TIMEOUT_MS)
  try {
    await putFileToStorage(uploadUrl, file, controller.signal)
  } catch (error) {
    if (controller.signal.aborted) throw new Error('截图上传超时，请重试')
    throw error
  } finally {
    clearTimeout(timer)
  }
}

export async function attachKefuOrderScreenshot(
  orderNo: string,
  state: ScreenshotUpload,
  canView: boolean,
): Promise<void> {
  state.canReplace = false
  const { file } = state
  if (!state.ticket || (!state.uploaded && state.ticketExpiresAt <= Date.now())) {
    const requestedAt = Date.now()
    const extension = file.type === 'image/png' ? 'png' : 'jpg'
    state.ticket = await requestScreenshotTicket(orderNo, extension, ORDER_SCREENSHOT)
    state.ticketExpiresAt = requestedAt + state.ticket.expires_in * 1000
  }

  const ticket = state.ticket
  if (!state.uploaded) {
    await uploadWithDeadline(ticket.upload_url, file)
    state.uploaded = true
  }

  try {
    await claimScreenshot(orderNo, {
      screenshot_id: ticket.screenshot_id,
      original_name: file.name,
      remark: '',
    })
  } catch (error) {
    const message = error instanceof BizError && error.code === ErrorCode.VALIDATION ? error.message : ''
    if (message === '文件还没上传成功') {
      state.uploaded = false
      throw error
    }
    const rejectedImage = REJECTED_IMAGE_MESSAGES.has(message)
    if (rejectedImage || message === '单个订单最多上传20张截图，请先删除后再上传') {
      state.ticket = null
      state.uploaded = false
      state.canReplace = rejectedImage
      throw error
    }

    // 丢失回执和待认领记录被清理返回同一文案，先查原票据是否已经就绪。
    const screenshots = canView ? await fetchScreenshots(orderNo).catch(() => null) : null
    if (screenshots?.items.some((item) => item.id === ticket.screenshot_id)) return
    if (screenshots && message === '没有待认领的上传记录') {
      state.ticket = null
      state.uploaded = false
    }
    throw error
  }
}
