import type { AxiosRequestConfig } from 'axios'
import { reactive } from 'vue'
import { chatRequest } from '@/api/chatTransport'
import { AuthExpiredError, BizError, HttpError } from '@/api/errors'
import { request } from '@/api/http'
import {
  closeVisitorConversation, getVisitorConversation, getVisitorDesk, getVisitorDeskAvailability,
  getVisitorTransfer, setVisitorDeskAvailability, transferVisitorConversation,
  visitorPageHeaders, type VisitorHandoff,
} from '@/api/internalChatVisitors'
import { createReceptionChatTransport } from '@/api/receptionChatTransport'
import { createVisitorHistoryTransport } from '@/api/visitorHistoryTransport'
import { installVisitorReceptionMenu } from './visitorReceptionMenu'

const deskRoot = '/api/internal-chat/visitor/desk/'
const fallbackInterval = 2_000
const heartbeatInterval = 10_000
const lightweightRequestTimeout = 2_500

export type VisitorReceptionPanel = {
  transferring: boolean
  loading: boolean
  busy: boolean
  candidates: { id: number, name: string }[]
  history: VisitorHandoff[]
  target: number | ''
  error: string
  cancel: () => void
  dismissError: () => void
  chooseTarget: (id: number | '') => void
  transfer: () => Promise<void>
}

function accessLost(cause: unknown) {
  return cause instanceof AuthExpiredError || (cause instanceof HttpError && (cause.status === 403 || cause.status === 404))
}

export function createVisitorReception(
  userId: number,
  identityPlane: string,
  effects: {
    changed: () => void
    revoked: (ids: number[]) => void
    modeChanged: (previouslyActive: boolean) => void
    gatewayConnected: () => boolean
  },
) {
  const requests = new AbortController()
  const { signal } = requests
  const headers = visitorPageHeaders(userId)
  const channelName = 'tt:chat-reception:' + identityPlane + ':' + String(userId)
  let channel: BroadcastChannel | undefined
  let active = false
  let probe: Promise<void> | undefined
  let initialProbe = true
  let legacy = false
  let enabled: boolean | undefined
  let watched: number | undefined
  let assigned = false
  let transferLookup = 0
  let detailRequest: AbortController | undefined
  let menu: ReturnType<typeof installVisitorReceptionMenu> | undefined
  let availabilityRequest: AbortController | undefined
  let heartbeatTimer: number | undefined
  let fallbackTimer: number | undefined

  const panel = reactive<VisitorReceptionPanel>({
    transferring: false, loading: false, busy: false,
    candidates: [], history: [], target: '', error: '',
    cancel: dismissTransfer,
    dismissError: () => { panel.error = '' },
    chooseTarget: id => { panel.target = id },
    transfer: confirmTransfer,
  })

  async function refreshAvailability() {
    if (!active || panel.busy || availabilityRequest || signal.aborted) return
    const controller = new AbortController()
    availabilityRequest = controller
    try {
      const data = await getVisitorDeskAvailability(userId, controller.signal)
      if (!controller.signal.aborted && !signal.aborted) {
        enabled = data.enabled
        menu?.refresh()
      }
    } catch (cause) {
      if (!controller.signal.aborted && !signal.aborted && accessLost(cause)) loseAccess()
    } finally {
      if (availabilityRequest === controller) availabilityRequest = undefined
    }
  }

  function loseAccess() {
    if (!active) return
    active = false
    enabled = undefined
    assigned = false
    watched = undefined
    dismissTransfer()
    availabilityRequest?.abort()
    detailRequest?.abort()
    channel?.close()
    channel = undefined
    menu?.dispose()
    menu = undefined
    clearTimers()
    effects.modeChanged(true)
    effects.changed()
  }

  function clearTimers() {
    window.clearInterval(heartbeatTimer)
    window.clearInterval(fallbackTimer)
  }

  function notifyPeers() {
    if (signal.aborted) return
    channel?.postMessage('changed')
    effects.changed()
  }

  function syncPeers() {
    void refreshAvailability()
    void refreshConversation()
    effects.changed()
  }

  function resumeAccess() {
    active = true
    if (typeof BroadcastChannel === 'function') channel = new BroadcastChannel(channelName)
    void refreshAvailability()
    heartbeatTimer = window.setInterval(syncPeers, heartbeatInterval)
    fallbackTimer = window.setInterval(() => {
      if (!effects.gatewayConnected()) effects.changed()
    }, fallbackInterval)
    channel?.addEventListener('message', syncPeers)
    if (!initialProbe) effects.modeChanged(false)
    effects.changed()
  }

  function checkAccess() {
    if (probe) return probe
    if (signal.aborted) return Promise.resolve()
    probe = getVisitorDesk(userId, signal, lightweightRequestTimeout).then(({ seat_mode, can_receive }) => {
      if (signal.aborted) return
      const allowed = seat_mode === true && can_receive === true
      legacy = !allowed
      if (allowed && !active) resumeAccess()
      if (!allowed && active) loseAccess()
    }).catch(cause => {
      if (signal.aborted || !accessLost(cause)) return
      legacy = false
      loseAccess()
    }).finally(() => { probe = undefined })
    return probe
  }

  function restoredPage() {
    if (document.visibilityState !== 'visible') return
    void checkAccess()
    if (active) syncPeers()
  }

  const initial = checkAccess().finally(() => { initialProbe = false })
  window.addEventListener('online', restoredPage)
  document.addEventListener('visibilitychange', restoredPage)

  function receptionRequest<T>(config: AxiosRequestConfig): Promise<T> {
    const url = config.url
    if (!url?.startsWith(deskRoot)) return chatRequest<T>(config)
    const resource = url.slice(deskRoot.length).split('?', 1)[0]
    return request<T>({ ...config, headers, signal, skipUnauthorizedHandler: true }).catch(cause => {
      if (!signal.aborted && accessLost(cause) && (resource === 'sync/' || resource === 'conversations/')) loseAccess()
      throw cause
    })
  }

  function createTransport() {
    return createReceptionChatTransport(createVisitorHistoryTransport('desk', receptionRequest), { fullInternal: true })
  }

  let transport = createTransport()

  async function readUnread(): Promise<number> {
    await initial
    if (!active || signal.aborted) return 0
    try {
      const { unread_count } = await request<{ unread_count: number }>({
        url: deskRoot + 'unread/', headers, signal, skipUnauthorizedHandler: true, timeout: lightweightRequestTimeout,
      })
      return Number.isSafeInteger(unread_count) && unread_count >= 0 ? unread_count : 0
    } catch (cause) {
      if (!signal.aborted && accessLost(cause)) {
        loseAccess()
        return 0
      }
      throw cause
    }
  }

  function finishOperation() {
    panel.busy = false
    menu?.refresh()
  }

  async function toggle() {
    if (!active || signal.aborted || enabled === undefined || panel.busy) return
    panel.busy = true
    panel.error = ''
    availabilityRequest?.abort()
    try {
      const data = await setVisitorDeskAvailability(userId, !enabled)
      if (signal.aborted) return
      enabled = data.enabled
      notifyPeers()
    } catch (cause) {
      if (!signal.aborted) panel.error = cause instanceof Error ? cause.message : '设置接待状态失败，请重试。'
    } finally {
      finishOperation()
    }
  }

  async function refreshConversation() {
    const id = watched
    if (!active || signal.aborted || id === undefined || detailRequest) return
    const controller = new AbortController()
    detailRequest = controller
    try {
      const summary = await getVisitorConversation('desk', userId, id, controller.signal)
      if (!controller.signal.aborted && !signal.aborted && watched === id) {
        assigned = summary.service_status === 'assigned'
        menu?.refresh()
      }
    } catch (cause) {
      if (controller.signal.aborted || signal.aborted || watched !== id) return
      if ((cause instanceof HttpError && cause.status === 404) || (cause instanceof BizError && cause.code === 1002)) {
        effects.revoked([id])
      } else if (accessLost(cause)) loseAccess()
    } finally {
      if (detailRequest === controller) detailRequest = undefined
    }
  }

  function watchConversation(id: number | undefined) {
    if (watched === id) return
    detailRequest?.abort()
    detailRequest = undefined
    watched = id
    assigned = false
    panel.error = ''
    dismissTransfer()
    menu?.refresh()
    void refreshConversation()
  }

  function dismissTransfer() {
    transferLookup += 1
    panel.transferring = false
    panel.loading = false
    panel.candidates = []
    panel.history = []
    panel.target = ''
  }

  async function openTransfer() {
    const id = watched
    if (id === undefined || !assigned || panel.busy || signal.aborted) return
    const generation = ++transferLookup
    panel.transferring = true
    panel.loading = true
    panel.error = ''
    try {
      const data = await getVisitorTransfer(userId, id)
      if (generation !== transferLookup || watched !== id || signal.aborted) return
      panel.candidates = data.candidates
      panel.history = data.history
      panel.target = data.candidates[0]?.id ?? ''
    } catch (cause) {
      if (generation === transferLookup && watched === id && !signal.aborted) {
        panel.error = cause instanceof Error ? cause.message : '读取转接人员失败，请重试。'
      }
    } finally {
      if (generation === transferLookup && watched === id && !signal.aborted) panel.loading = false
    }
  }

  async function confirmTransfer() {
    const target = panel.target
    if (!panel.transferring || target === '') return
    await modifyConversation(async id => {
      await transferVisitorConversation(userId, id, target)
      if (signal.aborted) return
      dismissTransfer()
      if (watched === id) assigned = false
      effects.revoked([id])
    })
  }

  async function modifyConversation(action: (id: number) => Promise<void>) {
    const id = watched
    if (!assigned || id === undefined || panel.busy || signal.aborted) return
    panel.busy = true
    panel.error = ''
    try {
      await action(id)
      notifyPeers()
    } catch (cause) {
      if (watched === id && !signal.aborted) panel.error = cause instanceof Error ? cause.message : '操作失败，请重试。'
    } finally {
      finishOperation()
    }
  }

  function attachMenu(doc?: Document | null) {
    menu?.dispose()
    if (!active || signal.aborted || !doc) return
    menu = installVisitorReceptionMenu(doc, () => ({ enabled, assigned, visitor: watched !== undefined, busy: panel.busy }), {
      toggle: () => { void toggle() },
      transfer: () => { void openTransfer() },
      end: () => { void modifyConversation(async id => {
        await closeVisitorConversation(userId, id)
        if (watched === id && !signal.aborted) assigned = false
      }) },
    })
  }

  return {
    initial,
    checkAccess,
    active: () => active,
    legacy: () => legacy,
    dispatch: (input: Parameters<typeof transport>[0]) => transport(input),
    readUnread,
    watchConversation,
    refreshConversation,
    attachMenu,
    panel,
    resetFrame() {
      transport = createTransport()
      watchConversation(undefined)
      menu?.dispose()
      menu = undefined
    },
    dispose() {
      requests.abort()
      availabilityRequest?.abort()
      detailRequest?.abort()
      menu?.dispose()
      window.removeEventListener('online', restoredPage)
      document.removeEventListener('visibilitychange', restoredPage)
      channel?.close()
      clearTimers()
    },
  }
}
