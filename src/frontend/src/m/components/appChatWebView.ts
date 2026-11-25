import { readonly, ref } from 'vue'
import { invalidateChatSession } from '@/api/chatSession'

type AppChatBridge = {
  back: (requestId: number) => void
  setVisible: (visible: boolean) => void
  openTarget: (conversationId: number, messageId: number) => void
}

declare global {
  interface Window {
    __ttAppChatVisible?: boolean
    ttAppChat?: AppChatBridge
  }
}

const signedGrant = /^[a-zA-Z0-9_:-]{1,512}$/
const previewStorageKey = 'tt:android-chat-preview'

function isAppChatPreview(): boolean {
  if (!import.meta.env.DEV || typeof window === 'undefined' || window.top !== window
    || window.location.origin !== 'http://127.0.0.1:5184' || !window.location.pathname.startsWith('/m/')
    || !/(?:^|\s)DaidaitongChatPreview(?:\s|$)/.test(navigator.userAgent)) return false

  const optedIn = new URLSearchParams(window.location.search).get('appChatPreview') === '1'
  try {
    if (optedIn) window.sessionStorage.setItem(previewStorageKey, '1')
    return window.sessionStorage.getItem(previewStorageKey) === '1'
  } catch {
    return optedIn
  }
}

export const appChatPreview = isAppChatPreview()

export const appChatHosted = typeof window !== 'undefined' && window.top === window && location.protocol === 'https:'
  && location.pathname === '/m/' && new URLSearchParams(location.search).get('appChat') === '1'
export const appChatCloseEnabled = appChatHosted && new URLSearchParams(location.search).get('appChatClose') === '1'
export const appChatRetainEnabled = appChatCloseEnabled && new URLSearchParams(location.search).get('appChatRetain') === '1'

function captureGrant(): string | undefined {
  if (!appChatHosted) return
  const grant = new URLSearchParams(location.hash.slice(1)).get('chatGrant')
  if (!grant || !signedGrant.test(grant)) return
  window.history.replaceState(window.history.state, '', location.pathname + location.search)
  return grant
}

// The native bearer stays in the app; the WebView holds only a chat grant in memory.
export let appChatGrant = captureGrant()
const expired = ref(appChatHosted && !appChatGrant)
export const appChatExpired = readonly(expired)

export function replaceAppChatGrant(grant: string): void {
  if (!expired.value && grant !== appChatGrant && signedGrant.test(grant)) {
    appChatGrant = grant
    invalidateChatSession()
  }
}

export function expireAppChatGrant(): void {
  if (!appChatHosted || expired.value) return
  appChatGrant = undefined
  expired.value = true
  invalidateChatSession()
  if (appChatRetainEnabled) notifyAppChat('expired')
}

export function appChatTarget(): { conversationId: number; messageId: number } | undefined {
  if (!appChatHosted) return
  const query = new URLSearchParams(location.search)
  const conversation = query.get('conversation') ?? ''
  const message = query.get('message') ?? ''
  if (!/^[1-9][0-9]{0,15}$/.test(conversation) || !/^[1-9][0-9]{0,15}$/.test(message)) return
  const conversationId = Number(conversation)
  const messageId = Number(message)
  if (Number.isSafeInteger(conversationId) && Number.isSafeInteger(messageId)) return { conversationId, messageId }
}

export function notifyAppChat(action: 'ready' | 'close' | 'back-empty' | 'back-handled' | 'expired', requestId?: number): void {
  const query = requestId !== undefined ? `?id=${requestId}` : ''
  if (appChatPreview || appChatRetainEnabled) {
    window.location.assign(`ttt-internal-chat://bridge/${action}${query}`)
  } else if (action === 'close' && appChatCloseEnabled) {
    window.location.replace('/m/?appChat=1&appChatClose=1&close=1')
  }
}

export function notifyAppChatUnread(count: number): void {
  if (appChatPreview && Number.isSafeInteger(count) && count >= 0) {
    console.info(`ttt-internal-chat-unread:${Math.min(count, 100)}`)
  }
}
