import { request } from './http'
import type { ScreenshotTicket } from './screenshots'

const base = '/api/business/screenshot-browser'

export function startScreenshotBrowser(orderNo: string): Promise<{ challenge: string }> {
  return request({
    url: `${base}/start`,
    method: 'POST',
    data: { order_no: orderNo },
  })
}

export function exchangeScreenshotBrowser(orderNo: string, challenge: string): Promise<null> {
  return request({
    url: `${base}/exchange`,
    method: 'POST',
    data: { order_no: orderNo, challenge },
    skipUnauthorizedHandler: true,
  })
}

export function checkScreenshotBrowser(orderNo: string, challenge?: string): Promise<null> {
  return request({
    url: `${base}/status`,
    params: { order_no: orderNo },
    headers: challenge ? { 'X-Screenshot-Challenge': challenge } : undefined,
    skipUnauthorizedHandler: true,
  })
}

export function requestBrowserScreenshotTicket(orderNo: string, ext: string): Promise<ScreenshotTicket> {
  return request({
    url: `${base}/ticket`,
    method: 'POST',
    data: { order_no: orderNo, ext },
    skipUnauthorizedHandler: true,
  })
}

export function claimBrowserScreenshot(
  orderNo: string, screenshotId: number, originalName: string,
): Promise<{ id: number }> {
  return request({
    url: `${base}/claim`,
    method: 'POST',
    data: { order_no: orderNo, screenshot_id: screenshotId, original_name: originalName },
    skipUnauthorizedHandler: true,
  })
}
