import { computed, onUnmounted, ref, watch } from 'vue'
import { browserChatPageLocked, chatSessionSignal } from '@/api/chatSession'

const storagePrefix = 'tt.internal-chat.desktop-notifications.'
const rememberedMessages = 128

export type DesktopChatScope = 'internal' | 'visitor'

export function desktopChatOwner(user: { tenant_name?: string; identity_plane?: string | null; id: number }) {
  return `${user.tenant_name}:${user.identity_plane ?? null}:${user.id}`
}

function sharedMessageRecord(owner: string, message: string, record = false) {
  const key = storagePrefix + 'recent.' + owner
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(key) ?? '[]')
    const recent: unknown[] = Array.isArray(stored) ? stored : []
    if (recent.includes(message)) return true
    if (record) localStorage.setItem(key, JSON.stringify([...recent.slice(1 - rememberedMessages), message]))
  } catch {
    // The per-window record still prevents repeats when storage is unavailable.
  }
  return false
}

function accountPreference(owner: string, value?: boolean): boolean {
  try {
    if (value !== undefined) localStorage.setItem(storagePrefix + owner, value ? '1' : '0')
    return localStorage.getItem(storagePrefix + owner) === '1'
  } catch {
    return value ?? false
  }
}

export function useDesktopChatNotifications(
  owner: () => string | undefined,
  openChat: (conversationId: number, scope: DesktopChatScope) => void,
  verifyIdentity: () => Promise<boolean>,
) {
  const supported = window.isSecureContext && typeof Notification === 'function'
  const permission = ref<NotificationPermission>(supported ? Notification.permission : 'denied')
  const optedIn = ref(false)
  const pending = ref(false)
  const seen = new Set<string>()
  let current: Notification | undefined
  let generation = 0

  const status = computed(() => {
    if (permission.value === 'denied') return 'blocked'
    return optedIn.value && permission.value === 'granted' ? 'enabled' : 'disabled'
  })

  function closeNotice() {
    if (!current) return
    current.onclick = null
    current.close()
    current = undefined
  }

  function readPermission() {
    if (!supported) return
    permission.value = Notification.permission
    if (permission.value !== 'granted') closeNotice()
  }

  function syncPreference(event: StorageEvent) {
    const active = owner()
    if (!active || (event.key !== null && event.key !== storagePrefix + active)) return
    optedIn.value = accountPreference(active)
    if (!optedIn.value) closeNotice()
  }

  window.addEventListener('focus', readPermission)
  window.addEventListener('storage', syncPreference)
  watch(owner, (active) => {
    generation += 1
    pending.value = false
    closeNotice()
    seen.clear()
    optedIn.value = active !== undefined && accountPreference(active)
    readPermission()
  }, { immediate: true, flush: 'sync' })

  onUnmounted(() => {
    generation += 1
    closeNotice()
    window.removeEventListener('focus', readPermission)
    window.removeEventListener('storage', syncPreference)
  })

  async function enable() {
    const active = owner()
    if (!active || !supported || pending.value) return
    readPermission()
    if (permission.value === 'denied' || status.value === 'enabled') return

    const requestGeneration = generation
    pending.value = true
    try {
      if (permission.value !== 'granted') await Notification.requestPermission()
      if (generation !== requestGeneration) return
      readPermission()
      if (Notification.permission === 'granted') optedIn.value = accountPreference(active, true)
    } catch {
      // 浏览器拒绝或未能显示权限请求时保持关闭。
    } finally {
      if (generation === requestGeneration) pending.value = false
    }
  }

  async function incomingMessage(value: unknown) {
    const active = owner()
    if (!supported || !active || typeof value !== 'object' || value === null) return
    const { id, conversationId, scope: rawScope } = value as Record<string, unknown>
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1
      || typeof conversationId !== 'number' || !Number.isSafeInteger(conversationId) || conversationId < 1
      || (rawScope !== undefined && rawScope !== 'internal' && rawScope !== 'visitor')) return
    const scope = rawScope ?? 'internal'
    const key = `${scope}:${id}`
    if (seen.has(key) || sharedMessageRecord(active, key)) return

    readPermission()
    if (status.value !== 'enabled') return
    const activeGeneration = generation
    const session = chatSessionSignal()
    const isCurrent = () => activeGeneration === generation && !session.aborted && !browserChatPageLocked()
    if (!isCurrent() || !await verifyIdentity() || !isCurrent()) return

    const display = () => {
      if (!isCurrent() || seen.has(key) || sharedMessageRecord(active, key)) return
      readPermission()
      if (status.value !== 'enabled') return
      closeNotice()
      try {
        current = new Notification('团团通 · 内部沟通', {
          body: '你有一条新的内部沟通消息',
          tag: 'tuantuantong-internal-chat',
        })
        sharedMessageRecord(active, key, true)
        seen.add(key)
        if (seen.size > rememberedMessages) {
          const oldest = seen.values().next()
          if (!oldest.done) seen.delete(oldest.value)
        }
        current.onclick = (event) => {
          event.preventDefault()
          if (!isCurrent()) return
          window.focus()
          closeNotice()
          void verifyIdentity().then((valid) => {
            if (valid && isCurrent()) openChat(conversationId, scope)
          })
        }
      } catch {
        // 不支持页面级系统通知时，页面未读数仍照常更新。
      }
    }
    try {
      if (navigator.locks) await navigator.locks.request(storagePrefix + active, display)
      else display()
    } catch {
      display()
    }
  }

  return { supported, status, pending, checkPermission: readPermission, enable, incomingMessage }
}
