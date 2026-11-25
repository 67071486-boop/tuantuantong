import type { HallClaimSource } from '@/api/hall'
import { request } from '@/api/http'
import { isHallClaimLink } from './hallDisplay'
import { WXPUSHER_ENTRY } from './lastWorkerPage'
import { isOrderNoShape } from './orderNoShape'

const WXPUSHER_ORIGIN = 'https://wxpusher.zjiecode.com'
const DAIDAITONG_HOST = 'suqing.suqing.chat'
const DAIDAITONG_SCHEME = `daidaitong://${DAIDAITONG_HOST}`
const APP_HANDOFF_WAIT_MS = 1000
const ORDER_CONTINUATION_KEY = 'ttt-wxpusher-order-continuation'
const HALL_CONTINUATION_KEY = 'ttt-wxpusher-hall-continuation'
const CONTINUATION_WINDOW_MS = 10 * 60 * 1000
export const WXPUSHER_ORDER_PASSWORD_NOTICE = 'wxpusher-order-password'

function isDaidaitongSite(): boolean {
  return window.location.origin === `https://${DAIDAITONG_HOST}`
}

export function isWxPusherAndroid(): boolean {
  return navigator.userAgent.endsWith('/WxPusher-Android')
}

function canOpenDaidaitong(entry: unknown): boolean {
  return entry === WXPUSHER_ENTRY && isDaidaitongSite() && isWxPusherAndroid()
}

export function wxpusherHomeAppLink(entry: unknown): string | undefined {
  return canOpenDaidaitong(entry) ? DAIDAITONG_SCHEME + '/m/wxpusher/open' : undefined
}

export function wxpusherOrderAppLink(orderNo: unknown, entry: unknown): string | undefined {
  if (!canOpenDaidaitong(entry) || typeof orderNo !== 'string' || !isOrderNoShape(orderNo)) return
  return `${DAIDAITONG_SCHEME}/m/orders/${orderNo}`
}

export function wxpusherHallAppLink(source: unknown, ref: unknown, entry: unknown): string | undefined {
  if (!canOpenDaidaitong(entry) || typeof ref !== 'string' || !isHallClaimLink(source, ref)) return
  return `${DAIDAITONG_SCHEME}/m/hall?source=${source}&ref=${ref}`
}

export function returnToWxPusherMessageDetail(): void {
  if (window.history.length <= 1) {
    if (window.opener) window.close()
    return
  }
  try {
    // WxPusher 可以省略来源地址。明确来自其他站点时交给用户关闭网页。
    if (!document.referrer || new URL(document.referrer).origin === WXPUSHER_ORIGIN) {
      window.history.back()
    }
  } catch {
    // 无法后退时，由落地页提示使用 WxPusher 的关闭按钮。
  }
}

type AppHandoff = 'foreground' | 'returned' | 'cancelled'

export function waitForDaidaitongHandoff(appLink: string | undefined, signal: AbortSignal): Promise<AppHandoff> {
  if (signal.aborted) return Promise.resolve('cancelled')
  return new Promise((resolve) => {
    let attempted = false
    let leftPage = false
    let pageShown = true
    let timer: number | undefined

    function finish(result: AppHandoff): void {
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('pagehide', onPageHide)
      window.removeEventListener('pageshow', onPageShow)
      signal.removeEventListener('abort', onAbort)
      resolve(result)
    }
    function markPageLeft(): void {
      if (!attempted) return
      leftPage = true
      window.clearTimeout(timer)
    }
    function onVisibilityChange(): void {
      if (document.visibilityState === 'hidden') {
        markPageLeft()
      } else if (pageShown) {
        if (leftPage) finish('returned')
        else if (!attempted) attemptHandoff()
      }
    }
    function onPageHide(): void {
      pageShown = false
      markPageLeft()
    }
    function onPageShow(): void {
      pageShown = true
      onVisibilityChange()
    }
    function onAbort(): void { finish('cancelled') }

    // 只把实际尝试唤起后离开的网页当作原生回流。预加载的隐藏页先等到前台。
    function attemptHandoff(): void {
      if (!appLink) {
        finish('foreground')
        return
      }
      attempted = true
      timer = window.setTimeout(() => {
        if (document.visibilityState === 'hidden') markPageLeft()
        else finish('foreground')
      }, APP_HANDOFF_WAIT_MS)
      try {
        window.location.assign(appLink)
      } catch {
        if (!leftPage) finish('foreground')
      }
    }

    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('pageshow', onPageShow)
    signal.addEventListener('abort', onAbort)
    onVisibilityChange()
  })
}

export function wxpusherOrderNoFromRedirect(redirect: unknown, entry: unknown): string | null {
  const prefix = '/m/orders/'
  if (entry !== WXPUSHER_ENTRY || typeof redirect !== 'string' || !redirect.startsWith(prefix)) return null
  const orderNo = redirect.slice(prefix.length)
  return isOrderNoShape(orderNo) ? orderNo : null
}

function wxpusherContinueUrl(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  try {
    const url = new URL(raw)
    return url.origin === WXPUSHER_ORIGIN && url.pathname === '/app/'
      && !url.username && !url.password && !url.search && url.hash.startsWith('#/follow?')
      && new URLSearchParams(url.hash.slice('#/follow?'.length)).get('jump') === 'auto' ? url.href : null
  } catch {
    return null
  }
}

function safeChatId(value: unknown): value is string {
  if (typeof value !== 'string') return false
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 && String(id) === value
}

export function wxpusherChatSitePath(conversation: unknown, message: unknown, open: unknown): string | null {
  if (!safeChatId(conversation) || !safeChatId(message)) return null
  return `/m/wxpusher/chat/${conversation}?message=${message}&entry=${WXPUSHER_ENTRY}${open === 'web' ? '&open=web' : ''}`
}

export function wxpusherChatPathFromRedirect(redirect: unknown): string | null {
  if (typeof redirect !== 'string') return null
  const match = /^\/m\/wxpusher\/chat\/([0-9]+)\?message=([0-9]+)&entry=wxpusher(?:&open=(web))?$/.exec(redirect)
  return match && match[0] === redirect ? wxpusherChatSitePath(match[1], match[2], match[3]) : null
}

export function wxpusherChatAppLink(chatPath: string, entry: unknown): string | undefined {
  if (canOpenDaidaitong(entry) && !chatPath.endsWith('&open=web')) {
    return `${DAIDAITONG_SCHEME}${chatPath.split('&')[0]}`
  }
}

export function shouldContinueWxPusherChatOnIos(chatPath: string | null, entry: unknown): boolean {
  return chatPath !== null && !chatPath.endsWith('&open=web') && entry === WXPUSHER_ENTRY && isWxPusherIos()
}

export function isWxPusherIos(): boolean {
  // WxPusher iOS 的内置页面注册了这条 WebKit bridge，普通 Safari 没有。
  const webkit = (window as Window & { webkit?: { messageHandlers?: { wxpusher?: { postMessage?: unknown } } } }).webkit
  return typeof webkit?.messageHandlers?.wxpusher?.postMessage === 'function'
}

export type SharedHallTarget = { source: HallClaimSource, ref: string }

function sharedHallTarget(source: unknown, ref: unknown): SharedHallTarget | null {
  return typeof ref === 'string' && isHallClaimLink(source, ref) ? { source, ref } : null
}

export function wxpusherSharedHallTarget(query: Record<string, unknown>, hash: string): SharedHallTarget | null {
  if (query.shared !== '1' || query.entry !== WXPUSHER_ENTRY || hash || query.hall !== undefined) return null
  return sharedHallTarget(query.source, query.ref)
}

export function wxpusherSharedHallQuery(target: SharedHallTarget) {
  return { entry: WXPUSHER_ENTRY, shared: '1', ...target }
}

export function wxpusherSharedHallClaimPath({ source, ref }: SharedHallTarget): string {
  return `/m/hall?claimSource=${source}&claimRef=${ref}`
}

export type WxPusherContinuation = { orderNo: string } | { chatPath: string } | { sharedHall: SharedHallTarget }

function requiredWxPusherContinueUrl(raw: unknown): string {
  const target = wxpusherContinueUrl(raw)
  if (target === null) throw new Error('暂时无法通过 WxPusher 继续，请用账号密码登录')
  return target
}

export async function requestWxPusherOrderContinueUrl(orderNo: string): Promise<string> {
  const result = await request<{ url: string } | null>({
    url: `/api/auth/wxpusher/orders/${orderNo}/continue`, method: 'POST', skipUnauthorizedHandler: true,
  })
  return requiredWxPusherContinueUrl(result?.url)
}

export async function requestWxPusherSharedHall(target: SharedHallTarget): Promise<{ url: string | null, openInApp: boolean }> {
  const result = await request<{ url: string | null, open_in_app: boolean } | null>({
    url: '/api/auth/wxpusher/hall/continue', method: 'GET', params: target, skipUnauthorizedHandler: true,
  })
  return { url: wxpusherContinueUrl(result?.url), openInApp: result?.open_in_app === true }
}

export async function requestWxPusherContinueUrl(continuation: Exclude<WxPusherContinuation, { sharedHall: SharedHallTarget }>): Promise<string> {
  if ('orderNo' in continuation) return requestWxPusherOrderContinueUrl(continuation.orderNo)
  const result = await request<{ url: string } | null>({
    url: '/api/auth/wxpusher/chat/continue', method: 'GET', skipUnauthorizedHandler: true,
  })
  return requiredWxPusherContinueUrl(result?.url)
}

export async function requestWxPusherHallContinueUrl(ticket: string): Promise<string> {
  const result = await request<{ url: string } | null>({
    url: '/api/auth/wxpusher/login', method: 'GET', params: { hallStart: ticket }, skipUnauthorizedHandler: true,
  })
  return requiredWxPusherContinueUrl(result?.url)
}

function isWxPusherHallTicket(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 1024 && /^[.A-Za-z0-9_:-]+$/.test(value)
}

export function wxpusherHallTicketFromHash(hash: string, entry: unknown): string | null {
  if (entry !== WXPUSHER_ENTRY || !isDaidaitongSite() || !hash.startsWith('#')) return null
  const tickets = new URLSearchParams(hash.slice(1)).getAll('hall')
  return tickets.length === 1 && isWxPusherHallTicket(tickets[0]) ? tickets[0] : null
}

export function wxpusherHallResumeUrl(ticket: string): string {
  return '/api/auth/wxpusher/login?hallEntry=' + encodeURIComponent(ticket)
}

function forgetContinuation(key: string): void {
  try { window.sessionStorage.removeItem(key) } catch { /* 仍可直接用密码登录 */ }
}

function rememberContinuation(key: string, data: WxPusherContinuation | { hall: string }): boolean {
  try {
    const value = JSON.stringify({ ...data, expiresAt: Date.now() + CONTINUATION_WINDOW_MS })
    window.sessionStorage.setItem(key, value)
    if (window.sessionStorage.getItem(key) === value) return true
  } catch { /* 存储不可用时保留密码入口 */ }
  forgetContinuation(key)
  return false
}

function readContinuation<T>(
  key: string, decode: (pending: Record<string, unknown>) => T | null, consume: boolean,
): T | null {
  try {
    const value = window.sessionStorage.getItem(key)
    if (value === null) return null
    const pending = JSON.parse(value)
    const now = Date.now()
    const target = pending && typeof pending === 'object' ? decode(pending) : null
    if (target === null || typeof pending.expiresAt !== 'number'
      || pending.expiresAt <= now || pending.expiresAt > now + CONTINUATION_WINDOW_MS) {
      forgetContinuation(key)
      return null
    }
    if (consume) window.sessionStorage.removeItem(key)
    return target
  } catch {
    forgetContinuation(key)
    return null
  }
}

export function forgetWxPusherContinuation(): void {
  forgetContinuation(ORDER_CONTINUATION_KEY)
}

export function rememberWxPusherContinuation(continuation: WxPusherContinuation): boolean {
  if (decodeWxPusherContinuation(continuation) === null) return false
  forgetWxPusherHallContinuation()
  return rememberContinuation(ORDER_CONTINUATION_KEY, continuation)
}

function decodeWxPusherContinuation(pending: Record<string, unknown>): WxPusherContinuation | null {
  const { orderNo, sharedHall } = pending
  if (typeof orderNo === 'string' && isOrderNoShape(orderNo)) return { orderNo }
  const chatPath = wxpusherChatPathFromRedirect(pending.chatPath)
  if (chatPath !== null) return { chatPath }
  if (sharedHall && typeof sharedHall === 'object' && 'source' in sharedHall && 'ref' in sharedHall) {
    const target = sharedHallTarget(sharedHall.source, sharedHall.ref)
    if (target !== null) return { sharedHall: target }
  }
  return null
}

export function readWxPusherContinuation(consume: boolean): WxPusherContinuation | null {
  return readContinuation(ORDER_CONTINUATION_KEY, decodeWxPusherContinuation, consume)
}

export function forgetWxPusherHallContinuation(): void {
  forgetContinuation(HALL_CONTINUATION_KEY)
}

export function rememberWxPusherHallContinuation(ticket: string): boolean {
  if (!isWxPusherHallTicket(ticket)) return false
  forgetWxPusherContinuation()
  return rememberContinuation(HALL_CONTINUATION_KEY, { hall: ticket })
}

export function readWxPusherHallContinuation(): string | null {
  return isDaidaitongSite()
    ? readContinuation(HALL_CONTINUATION_KEY, ({ hall }) => isWxPusherHallTicket(hall) ? hall : null, true)
    : null
}
