import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { request } from '@/api/http'

export interface NotificationGuide {
  workspace_enabled?: boolean
  binding_enabled: boolean
  binding_link_supported?: boolean
  binding_prompt_enabled?: boolean
  bound: boolean
  pending_binding: BindingAttempt | null
}

interface BindingQr {
  mode?: 'qr'
  qr_url: string
  expires_at: string
}

interface BindingLink {
  mode: 'link'
  attempt_id: string
  subscribe_url: string
  expires_at: string
  qr_url?: never
}

type BindingAttempt = BindingQr | BindingLink
type BindingAction = 'start' | 'cancel' | 'send-link-code' | 'confirm-link-code' | 'test' | 'unbind'
type BindingProgress = { bound: boolean }
type LinkCodeReceipt = { uid: string; deliveryUnconfirmed: boolean }

const PROGRESS_INTERVAL = 10_000

export function fetchWxPusherGuide(baseUrl: string, background = false): Promise<NotificationGuide> {
  return request<NotificationGuide>({
    url: `${baseUrl}/guide`,
    method: 'GET',
    ...(background ? { skipUnauthorizedHandler: true, timeout: 5000 } : {}),
  })
}

export function useWxPusherBinding(baseUrl: string, isVisible = () => true, mode: 'qr' | 'link' = 'qr') {
  const bindingUrl = `${baseUrl}/binding`
  const guide = ref<NotificationGuide | null>(null)
  const bindingMode = computed(() => guide.value?.binding_link_supported === false ? 'qr' : mode)
  const loading = ref(true)
  const failed = ref(false)
  const challenge = ref<BindingAttempt | null>(null)
  const linkChallenge = computed(() => challenge.value?.mode === 'link' ? challenge.value : null)
  const expired = ref(false)
  const pendingAction = ref<BindingAction | null>(null)
  const busy = computed(() => pendingAction.value !== null)
  const actionError = ref('')
  const progressError = ref('')
  const bindingError = computed({
    get: () => actionError.value || progressError.value,
    set: (message: string) => {
      actionError.value = message
      progressError.value = ''
    },
  })
  const bindingCompleted = ref('')
  const linkCodeReceipt = ref<LinkCodeReceipt | null>(null)
  const confirmingUnbind = ref(false)
  const testNoticeAccepted = ref(false)
  let pollTimer: number | undefined
  let nextProgressAt = 0
  let progressInFlight = false
  let unmounted = false

  function clearPollTimer(): void {
    window.clearTimeout(pollTimer)
    pollTimer = undefined
  }

  function scheduleProgress(): void {
    clearPollTimer()
    const current = challenge.value
    if (unmounted || !isVisible() || !current) return
    if (!(Date.parse(current.expires_at) > Date.now())) {
      expired.value = true
      return
    }
    if (document.hidden || progressInFlight || busy.value) return
    pollTimer = window.setTimeout(() => void checkProgress(current), Math.max(0, nextProgressAt - Date.now()))
  }

  function requestProgress(current: BindingAttempt): Promise<BindingProgress> {
    return request<BindingProgress>({
      url: `${bindingUrl}/progress`, method: 'POST', timeout: 25_000,
      ...(current.mode === 'link' ? { data: { mode: 'link', attempt_id: current.attempt_id } } : {}),
    })
  }

  function applyProgress(current: BindingAttempt, result: BindingProgress): void {
    if (unmounted || challenge.value !== current) return
    progressError.value = ''
    if (result.bound && guide.value) {
      actionError.value = ''
      if (current.mode === 'link') {
        bindingCompleted.value = guide.value.bound ? '已换绑 WxPusher' : '已绑定 WxPusher'
        testNoticeAccepted.value = false
      }
      guide.value.bound = true
      challenge.value = null
    }
  }

  async function checkProgress(current: BindingAttempt): Promise<void> {
    if (unmounted || !isVisible() || document.hidden || progressInFlight || busy.value
      || challenge.value !== current || expired.value) return
    if (!(Date.parse(current.expires_at) > Date.now())) {
      expired.value = true
      return
    }
    progressInFlight = true
    nextProgressAt = Date.now() + PROGRESS_INTERVAL
    try {
      applyProgress(current, await requestProgress(current))
    } catch (error) {
      if (!unmounted && challenge.value === current && !busy.value) {
        const replaced = current.mode === 'link' && await reconcileLink(current)
        if (!replaced && !unmounted && challenge.value === current && !expired.value && !busy.value) {
          progressError.value = error instanceof Error && error.message ? error.message : '暂时无法读取绑定状态，稍后会自动重试。'
        }
      }
    } finally {
      progressInFlight = false
      scheduleProgress()
    }
  }

  watch(challenge, (current, _previous, onCleanup) => {
    expired.value = false
    linkCodeReceipt.value = null
    if (!current) return
    nextProgressAt = Date.now() + PROGRESS_INTERVAL
    const timer = window.setTimeout(scheduleProgress, Math.max(0, Date.parse(current.expires_at) - Date.now()))
    onCleanup(() => {
      window.clearTimeout(timer)
      clearPollTimer()
    })
    scheduleProgress()
  }, { flush: 'sync' })

  watch(isVisible, scheduleProgress)

  function applyGuide(status: NotificationGuide): void {
    guide.value = status
    const pending = status.pending_binding
    const canResume = pending?.mode === 'link' ? mode === 'link' : !status.bound
    challenge.value = status.binding_enabled && canResume ? pending ?? null : null
  }

  async function reconcileLink(current: BindingLink): Promise<boolean> {
    const status = await fetchWxPusherGuide(baseUrl).catch(() => null)
    if (!status || unmounted || challenge.value !== current) return false
    const pending = status.pending_binding
    if (status.binding_enabled && pending?.mode === 'link' && pending.attempt_id === current.attempt_id) {
      guide.value = status
      return false
    }
    applyGuide(status)
    bindingError.value = ''
    return true
  }

  async function loadGuide(): Promise<void> {
    loading.value = true
    failed.value = false
    try {
      const status = await fetchWxPusherGuide(baseUrl)
      if (!unmounted) applyGuide(status)
    } catch {
      failed.value = true
    } finally {
      loading.value = false
    }
  }

  async function changeBinding<T>(kind: BindingAction, action: () => Promise<T>, fallback: string): Promise<T | undefined> {
    if (busy.value) return
    pendingAction.value = kind
    bindingError.value = ''
    try {
      return await action()
    } catch (error) {
      bindingError.value = error instanceof Error && error.message ? error.message : fallback
    } finally {
      pendingAction.value = null
      scheduleProgress()
    }
  }

  function startBinding(): Promise<BindingLink | undefined> {
    const requestedMode = bindingMode.value
    return changeBinding('start', async () => {
      const wasBound = guide.value?.bound === true
      bindingCompleted.value = ''
      try {
        const setup = await request<BindingAttempt>({
          url: `${bindingUrl}/start`, method: 'POST',
          ...(requestedMode === 'link' ? { data: { mode: requestedMode } } : {}),
        })
        if (unmounted) return
        if (setup.mode === 'link' || !guide.value?.bound) challenge.value = setup
        if (setup.mode === 'link') return setup
      } catch (error) {
        // 重新读取服务端当前的绑定尝试，避免显示已被替换的链接或二维码。
        const status = await fetchWxPusherGuide(baseUrl).catch(() => null)
        if (unmounted) return
        if (status && (requestedMode === 'link' || !guide.value?.bound)) applyGuide(status)
        if (wasBound || !guide.value?.bound) throw error
      }
    }, requestedMode === 'link' ? '获取订阅链接失败，请重试。' : '获取绑定二维码失败，请重试。')
  }

  async function cancelBinding(): Promise<void> {
    const current = linkChallenge.value
    if (!current) return
    await changeBinding('cancel', async () => {
      try {
        const result = await request<{ canceled: boolean }>({
          url: `${bindingUrl}/cancel`, method: 'POST', data: { mode: 'link', attempt_id: current.attempt_id },
        })
        if (!result.canceled) throw new Error('绑定状态已更新，请查看当前状态。')
        if (!unmounted && challenge.value === current) challenge.value = null
      } catch (error) {
        const status = await fetchWxPusherGuide(baseUrl).catch(() => null)
        if (status && !unmounted && (challenge.value === current || !challenge.value)) applyGuide(status)
        throw error
      }
    }, '未能取消绑定，请稍后重试。')
  }

  async function sendLinkCode(uid: string): Promise<void> {
    const current = linkChallenge.value
    if (!current || expired.value) return
    await changeBinding('send-link-code', async () => {
      const result = await request<{ sent: boolean; delivery_unconfirmed?: boolean }>({
        url: `${bindingUrl}/link-code`, method: 'POST', data: { mode: 'link', attempt_id: current.attempt_id, uid },
      })
      if (unmounted || challenge.value !== current) return
      if (!result.sent && !result.delivery_unconfirmed) throw new Error('暂未发出验证码，请稍后再试。')
      linkCodeReceipt.value = { uid, deliveryUnconfirmed: !result.sent }
    }, '未能发送验证码，请稍后重试。')
  }

  async function confirmLinkCode(code: string): Promise<void> {
    const current = linkChallenge.value
    if (!current || expired.value) return
    await changeBinding('confirm-link-code', async () => {
      try {
        const result = await request<BindingProgress>({
          url: `${bindingUrl}/link-confirm`, method: 'POST', data: { mode: 'link', attempt_id: current.attempt_id, code },
        })
        if (!result.bound) throw new Error('暂未完成绑定，请检查验证码后再试。')
        applyProgress(current, result)
      } catch (error) {
        if (!(await reconcileLink(current))) throw error
      }
    }, '未能确认绑定，请稍后重试。')
  }

  function sendTestNotice(): Promise<void> {
    return changeBinding('test', async () => {
      testNoticeAccepted.value = false
      await request({ url: `${bindingUrl}/test`, method: 'POST' })
      testNoticeAccepted.value = true
    }, '未能发送测试消息，请稍后重试。')
  }

  function unbind(): Promise<void> {
    return changeBinding('unbind', async () => {
      const result = await request<{ bound: false }>({ url: bindingUrl, method: 'DELETE' })
      if (guide.value) guide.value.bound = result.bound
      challenge.value = null
      confirmingUnbind.value = false
      bindingCompleted.value = ''
      testNoticeAccepted.value = false
    }, '解绑失败，请重试。')
  }

  onMounted(() => {
    document.addEventListener('visibilitychange', scheduleProgress)
    void loadGuide()
  })

  onBeforeUnmount(() => {
    unmounted = true
    document.removeEventListener('visibilitychange', scheduleProgress)
    clearPollTimer()
  })

  return {
    guide, bindingMode, loading, failed, challenge, linkChallenge, expired, pendingAction, busy, bindingError,
    bindingCompleted, linkCodeReceipt, confirmingUnbind, testNoticeAccepted,
    loadGuide, startBinding, cancelBinding, sendLinkCode, confirmLinkCode, sendTestNotice, unbind,
  }
}
