import { computed, ref, type Ref } from 'vue'
import { chatRealtimeOrigin, chatRequest } from '@/api/chatTransport'
import { browserChatPageLocked, chatSessionSignal, requireBrowserChatSession } from '@/api/chatSession'
import { fetchMe } from '@/api/auth'
import { AuthExpiredError, HttpError } from '@/api/errors'
import { appChatExpired, appChatHosted, replaceAppChatGrant } from '@/m/components/appChatWebView'
import { receptionFrameId, receptionSourceId } from '@/api/receptionChatTransport'
import { useDemoChat } from './demoChat'
import { createVisitorReception } from './visitorReception'

export const liveChatPreview = import.meta.env.DEV && import.meta.env.VITE_INTERNAL_CHAT_LIVE === '1'
export const webKLiveOrigin = import.meta.env.VITE_INTERNAL_CHAT_WEBK_ORIGIN || 'http://127.0.0.1:4180'
export function chatWebKBase(production: boolean): string {
  return production ? `${location.origin}/internal-chat-webk` : webKLiveOrigin
}
export type ChatIdentity = { id: number; identity_plane: string }

export function isSameChatIdentity(expected: ChatIdentity, actual: ChatIdentity): boolean {
  return expected.id === actual.id && expected.identity_plane === actual.identity_plane
}

const base = '/api/internal-chat/'
const liveUnread = ref(0)

export function useChatPreviewUnread(production = () => false) {
  const demo = useDemoChat()
  return computed(() => production() || liveChatPreview ? liveUnread.value : demo.unreadTotal)
}

const refreshInterval = 10 * 60 * 1000
type ChatTarget = number | { conversationId: number; messageId: number }
type ReceptionTarget = { scope: 'internal' | 'visitor', id: number }
type RetainedChatTarget = { conversationId: number; messageId: number; requestId: number }

type Gateway = {
  on(event: string, handler: (data?: unknown) => void): Gateway
  emit(event: string, data: string | null): Gateway
  connect(): Gateway
  disconnect(): void
}
type GatewayOptions = {
  auth: (done: (auth: { token: string }) => void) => void
  autoConnect: boolean
  transports: ['websocket']
}
type GatewayFactory = {
  (options: GatewayOptions): Gateway
  (origin: string, options: GatewayOptions): Gateway
}
const gatewayLoads = new Map<string | undefined, Promise<GatewayFactory>>()
// SHA-384 of client-dist/socket.io.js from the locked socket.io 4.8.4 npm package.
const gatewayScriptIntegrity = 'sha384-Reot/vo8FFwN0x9+TUlfgfqhwfzvBM0YeCDPf977fZzLqc55oTKZGNv8BZA0LR4J'

function gatewayFactory(origin?: string): Promise<GatewayFactory> {
  const existing = gatewayLoads.get(origin)
  if (existing) return existing
  const pending = new Promise<GatewayFactory>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = origin ? `${origin}/socket.io/socket.io.js` : '/socket.io/socket.io.js'
    if (origin) {
      script.crossOrigin = 'anonymous'
      script.integrity = gatewayScriptIntegrity
    }
    script.onload = () => {
      const factory = (window as Window & { io?: GatewayFactory }).io
      if (factory) resolve(factory)
      else reject(new Error('实时连接不可用'))
      script.remove()
    }
    script.onerror = () => {
      script.remove()
      reject(new Error('实时连接不可用'))
    }
    document.head.append(script)
  }).catch((error: unknown) => {
    gatewayLoads.delete(origin)
    throw error
  })
  gatewayLoads.set(origin, pending)
  return pending
}

const chatReadPath = /^\/api\/internal-chat\/(contacts\/|conversations\/|conversations\/[1-9]\d*\/messages\/(?:search\/|[1-9]\d*\/image\/)?|sync\/)$/
const chatWritePath = /^\/api\/internal-chat\/conversations\/(?:[1-9]\d*\/(?:messages\/|read\/|preferences\/|messages\/[1-9]\d*\/revoke\/))?$/
const chatPinPath = /^\/api\/internal-chat\/conversations\/[1-9]\d*\/pin\/$/
const chatOrderStatePath = /^\/api\/internal-chat\/orders\/\d{20}\/$/
const chatOrderReopenPath = /^\/api\/internal-chat\/orders\/\d{20}\/reopen\/$/

function approvedRequest(value: unknown): { method: 'get' | 'post', url: string, data: unknown } | undefined {
  if (typeof value !== 'object' || value === null) return
  const { method, path, body } = value as Record<string, unknown>
  if ((method !== 'get' && method !== 'post') || typeof path !== 'string' || path.length > 1500) return
  const url = new URL(path, location.origin)
  if (url.origin !== location.origin || url.hash || url.username || url.password) return
  if (url.pathname === '/api/auth/me') {
    return method === 'get' && !url.search ? { method, url: url.pathname, data: undefined } : undefined
  }
  if (chatPinPath.test(url.pathname)) {
    return method === 'post' && !url.search ? { method, url: url.pathname, data: body } : undefined
  }
  if (chatOrderStatePath.test(url.pathname) || chatOrderReopenPath.test(url.pathname)) {
    const expected = chatOrderReopenPath.test(url.pathname) ? 'post' : 'get'
    return method === expected && !url.search ? { method, url: url.pathname, data: body } : undefined
  }
  if (method === 'get' ? !chatReadPath.test(url.pathname) : !chatWritePath.test(url.pathname)) return
  return { method, url: url.pathname + url.search, data: method === 'post' ? body : undefined }
}

export function attachLiveChatPreview(
  iframe: Ref<HTMLIFrameElement | undefined>,
  isOpen: Ref<boolean>,
  openOrder: (orderNo: string) => void,
  { retainOnClose = false, closeFromList, canClose = () => isOpen.value, onListControlsReady, onBackResult, onRetainedNavigationDone,
    onIncomingMessage, identity, onIdentityChanged, onNavigationReady,
    openWideWindow, openVisitorDesk, openLegacyVisitorConversation, visitorReception = false, onReceptionFrameReset, production = false }: {
    production?: boolean
    retainOnClose?: boolean
    closeFromList?: () => void
    canClose?: () => boolean
    onListControlsReady?: () => void
    onNavigationReady?: () => void
    openWideWindow?: (detail: unknown) => void
    openVisitorDesk?: () => void
    openLegacyVisitorConversation?: (id: number) => void
    visitorReception?: boolean
    onReceptionFrameReset?: () => void
    onBackResult?: (id: number, handled: boolean) => void
    onRetainedNavigationDone?: (id: number, settled: boolean) => void
    onIncomingMessage?: (data: unknown) => void
    identity?: ChatIdentity
    onIdentityChanged?: () => void
  } = {},
) {
  const frameOrigin = new URL(chatWebKBase(production)).origin
  let disposed = false
  let frameReady = false
  let navigationReady = false
  let requestedConversation: ChatTarget | undefined
  let requestedVisitor: number | undefined
  let currentRoom: ReceptionTarget | undefined
  let pendingRetainedConversation: RetainedChatTarget | undefined
  let switchingRetainedConversation: number | undefined
  let retiredFrame: HTMLIFrameElement | undefined
  let gateway: Gateway | undefined
  let gatewayOrigin: string | undefined
  let gatewaySession: AbortSignal | undefined
  let gatewayConnected = false
  let presenceWatching = false
  let presenceConversation: number | undefined
  let presenceRequest = 0
  let retryGateway: number | undefined
  let unreadInFlight = false
  let lastVisitorUnread = 0
  let pendingUnreadSession: AbortSignal | undefined
  let refreshInProgress = false
  let refreshDueAt = Date.now() + refreshInterval
  let refreshTimer: number | undefined
  liveUnread.value = 0
  const visitor = production && visitorReception && identity
    ? createVisitorReception(identity.id, identity.identity_plane, {
      changed,
      revoked: ids => resetReceptionFrame(ids),
      modeChanged: wasActive => resetReceptionFrame(undefined, wasActive),
      gatewayConnected: () => gatewayConnected,
    })
    : undefined

  function notifyFrame(type: string, detail?: unknown) {
    if (frameReady) iframe.value?.contentWindow?.postMessage({ type, detail }, frameOrigin)
  }

  async function refreshUnread() {
    if (disposed || appChatExpired.value) return
    pendingUnreadSession = chatSessionSignal()
    if (unreadInFlight) return
    unreadInFlight = true
    try {
      while (pendingUnreadSession && !disposed && !appChatExpired.value) {
        const signal = pendingUnreadSession
        pendingUnreadSession = undefined
        if (signal.aborted) continue
        try {
          const [{ unread_count }, visitorUnread] = await Promise.all([
            chatRequest<{ unread_count: number }>({ url: `${base}unread/`, signal }),
            visitor?.readUnread().catch(() => undefined),
          ])
          if (disposed || signal.aborted || appChatExpired.value) continue
          if (!Number.isSafeInteger(unread_count) || unread_count < 0) continue
          if (visitorUnread !== undefined || !visitor?.active()) lastVisitorUnread = visitorUnread ?? 0
          liveUnread.value = unread_count + lastVisitorUnread
          window.dispatchEvent(new CustomEvent('tt:chat-unread', { detail: liveUnread.value }))
        } catch {
          // Keep the last count while the browser is offline.
        }
      }
    } finally {
      unreadInFlight = false
    }
  }

  function changed() {
    if (disposed || appChatExpired.value) return
    notifyFrame('tt:chat.changed')
    void visitor?.refreshConversation()
    void refreshUnread()
  }

  function refreshPresenceWatch() {
    const serial = ++presenceRequest
    const socket = gateway
    const conversationId = presenceConversation
    if (disposed || !socket || !gatewayConnected) return
    if (presenceWatching) {
      socket.emit('chat:presence.watch', null)
      presenceWatching = false
    }
    if (!isOpen.value || conversationId === undefined) return
    void chatRequest<{ token: string }>({
      url: `${base}conversations/${conversationId}/presence-ticket/`, method: 'post',
    }).then(({ token }) => {
      if (disposed || !isOpen.value || serial !== presenceRequest || appChatExpired.value) return
      presenceWatching = true
      socket.emit('chat:presence.watch', token)
    }).catch(() => {
      // Keep status unknown if the conversation is unavailable.
    })
  }

  function receivePresence(value: unknown) {
    if (disposed || !isOpen.value || !presenceWatching || typeof value !== 'object' || value === null) return
    const { conversationId, online } = value as Record<string, unknown>
    if (typeof conversationId === 'number' && conversationId === presenceConversation && typeof online === 'boolean') {
      notifyFrame('tt:chat.presence', {
        conversationId: visitor?.active() ? receptionFrameId('internal', conversationId) : conversationId, online,
      })
    }
  }

  function reportOpen() {
    notifyFrame('tt:chat.visible', isOpen.value)
    refreshPresenceWatch()
  }

  function openRequestedConversation() {
    if (!frameReady || !navigationReady || !isOpen.value) return
    if (requestedVisitor !== undefined) {
      if (!visitor?.active()) return
      notifyFrame('tt:chat.openConversation', receptionFrameId('visitor', requestedVisitor))
      requestedVisitor = undefined
      requestedConversation = undefined
      return
    }
    const target = requestedConversation
    if (target === undefined) return
    let framed = target
    if (visitor?.active()) {
      framed = typeof target === 'number' ? receptionFrameId('internal', target) : {
        conversationId: receptionFrameId('internal', target.conversationId),
        messageId: receptionFrameId('internal', target.messageId),
      }
    }
    notifyFrame('tt:chat.openConversation', framed)
    requestedConversation = undefined
  }

  function resetFrame() {
    retiredFrame = iframe.value
    frameReady = false
    navigationReady = false
    pendingRetainedConversation = undefined
    switchingRetainedConversation = undefined
    presenceConversation = undefined
    visitor?.resetFrame()
  }

  // Order groups are not sent through the native direct-chat presence hook.
  function activeOrder(previouslyCombined: boolean): ReceptionTarget | undefined {
    const activePeer = iframe.value?.contentDocument?.querySelector('.chatlist-chat.active[data-peer-id]')
    const framedId = -Number(activePeer?.getAttribute('data-peer-id'))
    if (!Number.isSafeInteger(framedId) || framedId <= 0) return
    const source = previouslyCombined ? receptionSourceId(framedId) : { scope: 'internal' as const, id: framedId }
    if (source?.scope === 'internal') return source
  }

  function resetReceptionFrame(revoked?: number[], previouslyCombined = visitor?.active() === true) {
    if (disposed || !frameReady || !onReceptionFrameReset) return
    const selected = currentRoom ?? activeOrder(previouslyCombined)
    if (requestedVisitor === undefined && requestedConversation === undefined && selected?.scope === 'internal') {
      requestedConversation = selected.id
    }
    if (requestedVisitor === undefined && requestedConversation === undefined
      && selected?.scope === 'visitor' && visitor?.active() && !revoked?.includes(selected.id)) {
      requestedVisitor = selected.id
    }
    currentRoom = undefined
    resetFrame()
    onReceptionFrameReset()
  }

  function sourceConversation(id?: number): ReceptionTarget | undefined {
    if (id === undefined) return currentRoom
    return visitor?.active() ? receptionSourceId(id) : { scope: 'internal', id }
  }

  function openRequestedRetainedConversation() {
    if (!frameReady || !navigationReady || switchingRetainedConversation !== undefined || !pendingRetainedConversation) return
    switchingRetainedConversation = pendingRetainedConversation.requestId
    notifyFrame('tt:chat.openRetainedConversation', pendingRetainedConversation)
    pendingRetainedConversation = undefined
  }

  async function requestChatData(config: NonNullable<ReturnType<typeof approvedRequest>>) {
    if (visitor) {
      await visitor.initial
      if (visitor.active()) return visitor.dispatch({ path: config.url, method: config.method, body: config.data })
    }
    return chatRequest(config)
  }

  async function acceptRequest(event: MessageEvent) {
    const frame = iframe.value
    const frameWindow = frame?.contentWindow
    if (!frameWindow || frame === retiredFrame || event.source !== frameWindow || event.origin !== frameOrigin) return
    const data = event.data as { type?: unknown, id?: unknown, detail?: unknown }
    if (data?.type === 'tt:chat.ready') {
      retiredFrame = undefined
      presenceConversation = undefined
      frameReady = true
      reportOpen()
      openRequestedRetainedConversation()
      return openRequestedConversation()
    }
    if (data?.type === 'tt:chat.navigationReady') {
      navigationReady = true
      if (visitor) {
        await visitor.initial
        if (disposed || iframe.value !== frame || frame === retiredFrame) return
        visitor.attachMenu(frame.contentDocument)
        notifyFrame('tt:chat.visitorDeskAvailable', visitor.legacy())
      }
      onNavigationReady?.()
      openRequestedRetainedConversation()
      return openRequestedConversation()
    }
    if (data?.type === 'tt:chat.retainedNavigationDone' && typeof data.id === 'number' &&
      typeof data.detail === 'boolean' && data.id === switchingRetainedConversation) {
      switchingRetainedConversation = undefined
      onRetainedNavigationDone?.(data.id, data.detail)
      openRequestedRetainedConversation()
      return
    }
    if (data?.type === 'tt:chat.watchPresence') {
      if (!isOpen.value) return
      const id = data.detail
      if (id !== null && (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= 0)) return
      const watched = id === null ? undefined : sourceConversation(id)
      currentRoom = watched
      visitor?.watchConversation(watched?.scope === 'visitor' ? watched.id : undefined)
      const conversationId = watched?.scope === 'internal' ? watched.id : undefined
      if (presenceConversation === conversationId) return
      presenceConversation = conversationId
      return refreshPresenceWatch()
    }
    if (data?.type === 'tt:chat.listControlsReady') return onListControlsReady?.()
    if (data?.type === 'tt:chat.backResult' && typeof data.id === 'number' && typeof data.detail === 'boolean') {
      return onBackResult?.(data.id, data.detail)
    }
    if (data?.type === 'tt:chat.closeFromList') {
      if (canClose()) closeFromList?.()
      return
    }
    if (data?.type === 'tt:chat.openWideWindow') {
      if (isOpen.value) openWideWindow?.(data.detail)
      return
    }
    if (data?.type === 'tt:chat.openVisitorDesk') {
      if (isOpen.value) openVisitorDesk?.()
      return
    }
    if (data?.type === 'tt:chat.openOrder') {
      if (isOpen.value && typeof data.detail === 'string' && /^\d{20}$/.test(data.detail)) {
        openOrder(data.detail)
      }
      return
    }
    if (data?.type !== 'tt:chat.request' || typeof data.id !== 'number') return
    try {
      const config = approvedRequest(data)
      if (!config) throw new Error('不支持该聊天请求')
      if (!isOpen.value && chatPinPath.test(config.url)) throw new Error('请打开聊天后再操作置顶')
      if (!isOpen.value && chatOrderReopenPath.test(config.url)) throw new Error('请打开聊天后再重开群聊')
      if (retainOnClose && !isOpen.value && config.method === 'post' && /\/read\/(?:\?|$)/.test(config.url)) {
        frameWindow.postMessage({ type: 'tt:chat.response', id: data.id }, frameOrigin)
        return
      }
      if (config.url === '/api/auth/me' && !appChatHosted && browserChatPageLocked()) requireBrowserChatSession()
      const result = config.url === '/api/auth/me'
        ? await fetchMe().then(({ id, display_name }) => {
          if (!appChatHosted) {
            requireBrowserChatSession()
            if (!gateway && import.meta.env.VITE_CHAT_DIRECT_ORIGIN) scheduleReconnect()
          }
          return { user: { id, display_name } }
        })
        : await requestChatData(config)
      if (disposed || iframe.value !== frame || frame === retiredFrame) return
      if (visitor?.active() && config.url.startsWith(`${base}sync/?`)) {
        const revoked = (result as { revoked_conversation_ids?: number[] }).revoked_conversation_ids
        if (revoked?.length) return resetReceptionFrame(revoked)
      }
      frameWindow.postMessage({ type: 'tt:chat.response', id: data.id, result }, frameOrigin)
      if (config.method === 'post') changed()
    } catch (error) {
      const message = error instanceof Error ? error.message : '聊天请求失败'
      const errorStatus = error instanceof HttpError ? error.status : undefined
      if (!disposed && iframe.value === frame && frame !== retiredFrame) {
        frameWindow.postMessage({ type: 'tt:chat.response', id: data.id, error: message, errorStatus }, frameOrigin)
      }
    }
  }

  async function connectGateway() {
    try {
      const session = chatSessionSignal()
      const lookup = chatRealtimeOrigin()
      const origin = lookup ? await lookup : undefined
      if (lookup && session.aborted) return scheduleReconnect()
      if (disposed) return
      if (gateway && gatewayOrigin === origin) {
        gateway.connect()
        return
      }
      const factory = await gatewayFactory(origin)
      if (lookup && session.aborted) return scheduleReconnect()
      if (disposed) return
      if (gateway && gatewayOrigin === origin) return
      disconnectGateway()
      gatewayOrigin = origin
      const options: GatewayOptions = {
        autoConnect: false,
        transports: ['websocket'],
        auth: (done) => {
          void chatRequest<{ token: string; user: ChatIdentity }>({ url: `${base}realtime-ticket/`, method: 'post' })
            .then(({ token, user }) => {
              if (!isCurrent()) return
              if (identity && (!user || !isSameChatIdentity(identity, user))) {
                onIdentityChanged?.()
                dispose()
                return
              }
              done({ token })
            }, () => {
              if (isCurrent()) done({ token: '' })
            })
        },
      }
      const socket = origin ? factory(origin, options) : factory(options)
      const isCurrent = () => !disposed && gateway === socket
        && (!lookup || (!session.aborted && (appChatHosted || !browserChatPageLocked())))
      gateway = socket
      if (lookup) {
        gatewaySession = session
        session.addEventListener('abort', onGatewaySessionChanged, { once: true })
      }
      if (onIncomingMessage) {
        socket.on('chat:message', (data) => {
          if (isCurrent()) onIncomingMessage(data)
        }).on('chat:visitor-message', (data) => {
          if (isCurrent() && typeof data === 'object' && data !== null
            && 'scope' in data && data.scope === 'visitor') onIncomingMessage(data)
        })
      }
      socket.on('chat:changed', () => { if (isCurrent()) changed() })
        .on('chat:presence', (data) => { if (isCurrent()) receivePresence(data) })
        .on('connect', () => {
          if (!isCurrent()) return
          gatewayConnected = true
          changed()
          refreshPresenceWatch()
        })
        .on('connect_error', () => { if (isCurrent()) scheduleReconnect() })
        .on('disconnect', (reason) => {
          if (gateway !== socket) return
          gatewayConnected = false
          presenceWatching = false
          presenceRequest += 1
          if (!disposed && presenceConversation !== undefined) {
            const conversationId = visitor?.active() ? receptionFrameId('internal', presenceConversation) : presenceConversation
            notifyFrame('tt:chat.presence', { conversationId, online: null })
          }
          if (reason === 'io server disconnect') scheduleReconnect()
        })
        .connect()
    } catch (error) {
      if (!(error instanceof AuthExpiredError)) scheduleReconnect()
    }
  }

  function disconnectGateway() {
    gatewaySession?.removeEventListener('abort', onGatewaySessionChanged)
    gatewaySession = undefined
    gateway?.disconnect()
    gateway = undefined
  }

  function onGatewaySessionChanged() {
    disconnectGateway()
    pendingUnreadSession = undefined
    lastVisitorUnread = 0
    liveUnread.value = 0
    if (!appChatExpired.value && (appChatHosted || !browserChatPageLocked())) scheduleReconnect()
  }

  function scheduleReconnect() {
    if (disposed || retryGateway !== undefined) return
    retryGateway = window.setTimeout(() => {
      retryGateway = undefined
      void connectGateway()
    }, 1500)
  }

  async function refreshAppGrant() {
    if (disposed || appChatExpired.value || refreshInProgress || !appChatHosted
      || document.visibilityState === 'hidden' || Date.now() < refreshDueAt) return
    refreshInProgress = true
    try {
      const { grant } = await chatRequest<{ grant: string }>({ url: `${base}app/refresh/`, method: 'post' })
      if (!disposed) replaceAppChatGrant(grant)
      refreshDueAt = Date.now() + refreshInterval
    } catch {
      // Keep the current grant through outages; invalid grants are handled by chatRequest.
    } finally {
      refreshInProgress = false
    }
  }

  function dispose() {
    if (disposed) return
    disposed = true
    pendingUnreadSession = undefined
    window.removeEventListener('message', acceptRequest)
    window.removeEventListener('online', changed)
    document.removeEventListener('visibilitychange', refreshAppGrant)
    window.clearInterval(refreshTimer)
    window.clearTimeout(retryGateway)
    disconnectGateway()
    visitor?.dispose()
    liveUnread.value = 0
  }

  window.addEventListener('message', acceptRequest)
  window.addEventListener('online', changed)
  if (appChatHosted) {
    document.addEventListener('visibilitychange', refreshAppGrant)
    refreshTimer = window.setInterval(refreshAppGrant, 60_000)
  }
  void connectGateway()
  void refreshUnread()
  return {
    requestBack(id: number) {
      if (!frameReady || !onBackResult) return false
      iframe.value?.contentWindow?.postMessage({ type: 'tt:chat.back', id }, frameOrigin)
      return true
    },
    receptionPanel: visitor?.panel,
    sourceConversation,
    reportOpen() {
      reportOpen()
      openRequestedConversation()
      if (isOpen.value && visitor) void visitor.checkAccess().then(() => {
        notifyFrame('tt:chat.visitorDeskAvailable', visitor.legacy())
      })
    },
    openConversation(target?: ChatTarget) {
      if (typeof target === 'number' && (!Number.isSafeInteger(target) || target < 1)) return
      requestedVisitor = undefined
      requestedConversation = target
      openRequestedConversation()
    },
    openVisitorConversation(id: number) {
      if (!visitor || !Number.isSafeInteger(id) || id < 1) return
      requestedConversation = undefined
      requestedVisitor = id
      if (visitor.active()) return openRequestedConversation()
      void visitor.checkAccess().then(() => {
        if (disposed || requestedVisitor !== id) return
        if (visitor.active()) return openRequestedConversation()
        requestedVisitor = undefined
        if (visitor.legacy()) openLegacyVisitorConversation?.(id)
      })
    },
    openRetainedConversation(target: RetainedChatTarget) {
      pendingRetainedConversation = target
      openRequestedRetainedConversation()
    },
    resetFrame,
    dispose,
  }
}
