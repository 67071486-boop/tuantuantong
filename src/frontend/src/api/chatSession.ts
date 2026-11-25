import { readCsrfCookie } from './csrf-cookie'
import { AuthExpiredError } from './errors'

let session = new AbortController()
let channel: BroadcastChannel | undefined
let pageCsrf: string | undefined
let pageUserId: number | undefined
let pageLocked = false

function reset(): void {
  const previous = session
  session = new AbortController()
  previous.abort()
}

function sessionChannel(): BroadcastChannel | undefined {
  if (!channel && import.meta.env.VITE_CHAT_DIRECT_ORIGIN
    && typeof window !== 'undefined' && typeof window.BroadcastChannel === 'function') {
    channel = new window.BroadcastChannel('ttt:chat-session')
    channel.addEventListener('message', (event) => {
      if (event.data === 'reset' && pageUserId !== undefined) lockBrowserChatPage()
    })
  }
  return channel
}

export function chatSessionSignal(): AbortSignal {
  sessionChannel()
  return session.signal
}

export function invalidateChatSession(): void {
  reset()
}

export function browserChatCsrf(): string | null | undefined {
  if (import.meta.env.VITE_CHAT_DIRECT_ORIGIN && typeof document !== 'undefined') return readCsrfCookie()
}

export function bindBrowserChatSession(userId: number, csrf: string | null | undefined): void {
  if (csrf === undefined || pageLocked || !Number.isSafeInteger(userId) || userId <= 0) return
  if (pageUserId !== undefined && pageUserId !== userId) {
    lockBrowserChatPage()
    return
  }
  pageUserId = userId
  if (csrf && pageCsrf === undefined) pageCsrf = csrf
  sessionChannel()
}

export function confirmBrowserChatLogin(userId: number): void {
  pageCsrf = undefined
  pageUserId = undefined
  pageLocked = false
  reset()
  bindBrowserChatSession(userId, browserChatCsrf())
  sessionChannel()?.postMessage('reset')
}

export function endBrowserChatSession(broadcast = false): void {
  if (browserChatCsrf() !== undefined) lockBrowserChatPage()
  else reset()
  if (broadcast) sessionChannel()?.postMessage('reset')
}

function lockBrowserChatPage(): void {
  if (pageLocked) return
  pageLocked = true
  reset()
}

export function browserChatPageLocked(): boolean {
  // Django rotates this cookie when another tab changes the login cookie.
  if (!pageLocked && pageCsrf !== undefined && pageCsrf !== browserChatCsrf()) lockBrowserChatPage()
  return pageLocked
}

export function requireBrowserChatSession(ticketUserId?: number): number | undefined {
  if (browserChatCsrf() === undefined) return
  if (ticketUserId !== undefined && ticketUserId !== pageUserId) lockBrowserChatPage()
  if (browserChatPageLocked() || !pageCsrf || pageUserId === undefined) {
    throw new AuthExpiredError('登录状态已变更，请刷新页面后再打开聊天')
  }
  return pageUserId
}
