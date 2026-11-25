import type { AxiosRequestConfig } from 'axios'
import { request } from './http'
import { AuthExpiredError, HttpError, mapHttpStatus, NetworkError } from './errors'
import { isEnvelope, unwrap } from './unwrap'
import { readCsrfCookie } from './csrf-cookie'
import { chatSessionSignal, requireBrowserChatSession } from './chatSession'
import { appChatGrant, appChatHosted, expireAppChatGrant } from '@/m/components/appChatWebView'

const ticketUrl = '/api/internal-chat/direct-ticket/'
const chatPrefix = '/api/internal-chat/'
const directRead = /^(conversations\/|unread\/|sync\/|conversations\/[1-9]\d*\/messages\/(?:search\/|[1-9]\d*\/image\/)?|orders\/\d{20}\/(?:messages\/(?:[1-9]\d*\/image\/)?)?|(?:notification-conversations|order-groups|app\/notification-conversations|app\/order-groups)\/[1-9]\d*\/messages\/(?:[1-9]\d*\/image\/)?)$/
const directWrite = /^(conversations\/[1-9]\d*\/(?:messages\/(?:[1-9]\d*\/revoke\/)?|read\/|preferences\/|pin\/)|orders\/\d{20}\/(?:messages|join|reopen)\/|(?:notification-conversations|order-groups|app\/notification-conversations|app\/order-groups)\/[1-9]\d*\/(?:messages|read)\/)$/

type Scope = { signal: AbortSignal; csrf: string | null; grant: string | undefined }
type Ticket = { scope: Scope; expiresAt: number; token: string | null }
let cached: Ticket | undefined
let pending: { scope: Scope; promise: Promise<Ticket> } | undefined
let observedSession: AbortSignal | undefined

function directOrigin(): string | undefined {
  const value: unknown = import.meta.env.VITE_CHAT_DIRECT_ORIGIN
  if (!value) return
  if (typeof value !== 'string' || !/^https:\/\/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.suqing\.chat\/?$/.test(value)) {
    throw new NetworkError('聊天直连地址配置无效')
  }
  return new URL(value).origin
}

function directPath(config: AxiosRequestConfig): string | undefined {
  const raw = config.url
  if (!raw?.startsWith(chatPrefix) || raw.includes('#')) return
  const path = raw.slice(chatPrefix.length).split('?', 1)[0] ?? ''
  const method = (config.method ?? 'get').toLowerCase()
  if (method === 'get' ? directRead.test(path) : method === 'post' && directWrite.test(path)) {
    return raw.slice(chatPrefix.length)
  }
}

async function tenantRequest<T>(config: AxiosRequestConfig): Promise<T> {
  if (!appChatHosted) {
    const userId = requireBrowserChatSession()
    const url = config.url
    if (userId === undefined || !url?.startsWith(chatPrefix)
      || !new URL(url, window.location.origin).pathname.startsWith(chatPrefix)) return request<T>(config)
    return request<T>({
      ...config, baseURL: '', headers: { ...config.headers, 'X-Chat-Page-User': String(userId) },
    })
  }
  const grant = appChatGrant
  if (!grant) throw new AuthExpiredError('聊天登录已过期')
  try {
    return await request<T>({
      ...config, baseURL: '', headers: { Authorization: `Chat ${grant}` }, skipUnauthorizedHandler: true,
    })
  } catch (error) {
    if (error instanceof AuthExpiredError && appChatGrant === grant) expireAppChatGrant()
    throw error
  }
}

function currentScope(): Scope {
  const signal = chatSessionSignal()
  if (signal !== observedSession) {
    observedSession = signal
    signal.addEventListener('abort', () => {
      if (cached?.scope.signal === signal) cached = undefined
      if (pending?.scope.signal === signal) pending = undefined
    }, { once: true })
  }
  return { signal, grant: appChatHosted ? appChatGrant : undefined, csrf: appChatHosted ? null : readCsrfCookie() }
}

function matchesScope(expected: Scope, actual: Scope, allowInitialCsrf = false): boolean {
  return !expected.signal.aborted && expected.signal === actual.signal && expected.grant === actual.grant
    && (expected.csrf === actual.csrf || allowInitialCsrf && expected.csrf === null)
}

function requireCurrent(scope: Scope): void {
  if (!appChatHosted) requireBrowserChatSession()
  if (!matchesScope(scope, currentScope())) throw new AuthExpiredError('登录状态已变更，请重试')
}

function acquireTicket(): Promise<Ticket> {
  const scope = currentScope()
  if (cached && cached.expiresAt > performance.now() + 5_000 && matchesScope(cached.scope, scope)) return Promise.resolve(cached)
  if (pending && matchesScope(pending.scope, scope, true)) return pending.promise
  cached = undefined
  const requestedAt = performance.now()
  const promise = tenantRequest<{ enabled: boolean; token?: string; ttl_seconds?: number; user_id?: number }>({
    url: ticketUrl, method: 'POST', signal: scope.signal,
  }).then((value) => {
    if (!appChatHosted) requireBrowserChatSession()
    const current = currentScope()
    if (!matchesScope(scope, current, true)) throw new AuthExpiredError('登录状态已变更，请重试')
    if (value?.enabled === false) {
      cached = { scope: current, expiresAt: performance.now() + 30_000, token: null }
    } else {
      const { ttl_seconds: ttl, user_id: userId } = value ?? {}
      if (value?.enabled !== true || typeof value.token !== 'string' || !/^[A-Za-z0-9_.-]{1,4096}$/.test(value.token)
        || typeof ttl !== 'number' || !Number.isSafeInteger(ttl) || ttl <= 0 || ttl > 120
        || typeof userId !== 'number' || !Number.isSafeInteger(userId) || userId <= 0) {
        throw new NetworkError('聊天凭证返回异常')
      }
      if (!appChatHosted) requireBrowserChatSession(userId)
      const expiresAt = requestedAt + ttl * 1000
      if (expiresAt <= performance.now()) throw new NetworkError('聊天凭证已过期，请重试')
      cached = { scope: current, expiresAt, token: value.token }
    }
    return cached
  }).finally(() => {
    if (pending?.promise === promise) pending = undefined
  })
  pending = { scope, promise }
  return promise
}

export function chatRealtimeOrigin(): Promise<string | undefined> | undefined {
  const origin = directOrigin()
  if (!origin) return
  if (!appChatHosted) requireBrowserChatSession()
  return acquireTicket().then((ticket) => {
    requireCurrent(ticket.scope)
    return ticket.token ? origin : undefined
  })
}

function appendParams(url: URL, params: unknown): void {
  if (params instanceof URLSearchParams) {
    params.forEach((value, name) => url.searchParams.append(name, value))
  } else if (typeof params === 'object' && params !== null) {
    for (const [name, value] of Object.entries(params)) {
      if (value == null) continue
      if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
        throw new NetworkError('聊天查询参数无效')
      }
      url.searchParams.append(name, String(value))
    }
  }
}

async function requestDirect<T>(url: URL, config: AxiosRequestConfig, ticket: Ticket, retry: boolean): Promise<T> {
  requireCurrent(ticket.scope)
  if (!ticket.token) throw new HttpError(503, '聊天连接配置已变更，请重试')
  const method = (config.method ?? 'GET').toUpperCase()
  const body = method === 'POST' ? JSON.stringify(config.data ?? {}) : undefined
  const controller = new AbortController()
  const abort = () => controller.abort()
  const timeout = setTimeout(abort, config.timeout ?? 15_000)
  ticket.scope.signal.addEventListener('abort', abort, { once: true })
  config.signal?.addEventListener?.('abort', abort)
  if (config.signal?.aborted) abort()
  let response: Response
  let result: unknown
  try {
    response = await fetch(url, {
      method, body, signal: controller.signal, credentials: 'omit', redirect: 'error', mode: 'cors', cache: 'no-store',
      headers: { Authorization: `Bearer ${ticket.token}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    })
    result = await response.json().catch(() => undefined)
  } catch {
    requireCurrent(ticket.scope)
    throw new NetworkError('无法连接聊天服务，请稍后重试')
  } finally {
    clearTimeout(timeout)
    ticket.scope.signal.removeEventListener('abort', abort)
    config.signal?.removeEventListener?.('abort', abort)
  }
  requireCurrent(ticket.scope)
  if (response.status === 401) {
    if (cached === ticket) cached = undefined
    if (method === 'GET' && retry && !config.signal?.aborted) {
      const refreshed = await acquireTicket()
      requireCurrent(ticket.scope)
      return requestDirect<T>(url, config, refreshed, false)
    }
  }
  if (!response.ok) throw mapHttpStatus(response.status, isEnvelope(result) && typeof result.msg === 'string' ? result.msg : '')
  if (!isEnvelope(result) || !Number.isSafeInteger(result.code) || typeof result.msg !== 'string') {
    throw new NetworkError('聊天服务返回异常')
  }
  return unwrap({ ...result, data: result.data as T })
}

export async function chatRequest<T>(config: AxiosRequestConfig): Promise<T> {
  if (!appChatHosted) requireBrowserChatSession()
  const suffix = directPath(config)
  const origin = suffix && directOrigin()
  if (origin) {
    const ticket = await acquireTicket()
    requireCurrent(ticket.scope)
    if (ticket.token) {
      const url = new URL(`/v1/browser/${suffix}`, origin)
      appendParams(url, config.params)
      return requestDirect<T>(url, config, ticket, true)
    }
  }
  return tenantRequest<T>(config)
}
