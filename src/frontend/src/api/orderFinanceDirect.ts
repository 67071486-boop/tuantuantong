import type { AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { appChatHosted } from '@/m/components/appChatWebView'
import { fetchMe } from './auth'
import { readCsrfCookie } from './csrf-cookie'
import { AuthExpiredError, NetworkError } from './errors'
import { request } from './http'

const servicePaths = new Set([
  '/api/app/income',
  '/api/app/wallet',
  ...[
    'alipay-authorizations', 'approvals', 'business-report', 'deposit-rules', 'hall', 'hall-claim-target',
    'my-orders', 'online-payroll', 'order-stats', 'settlements', 'support-stats',
    'takers', 'xgj-receipts', 'orders', 'performances', 'wallet-transactions', 'wallets', 'withdrawals',
  ].map(group => '/api/business/' + group),
])
const formatPath = /^\/api\/business\/(orders|performances|wallet-transactions|wallets|withdrawals)\.[a-z0-9]+\/?$/
const webException = /^\/api\/business\/(orders\/xgj-receipt-sync(?:\.[a-z0-9]+)?|approvals\/settings)\/?$/
const ticketPath = '/api/auth/order-finance-ticket/'
const allowedOrigin = /^https:\/\/(?:order-finance|ceshi-order-finance|order-finance-ceshi)\.suqing\.chat\/?$/
const safeHeaders = ['Accept', 'Content-Type']

type Scope = { generation: string; userId: number; signal: AbortSignal }
type Grant = { scope: Scope; expires: number; authorization?: string }
type IssuedTicket = { enabled: boolean; token?: string; ttl_seconds?: number; user_id?: number }
type Target = { url: string; authorization: string; scope: Scope }

let generation: string | undefined
let expectedUser: number | undefined
let locked = false
let controller = new AbortController()
let channel: BroadcastChannel | undefined
let recovery: Promise<void> | undefined
let grant: Grant | undefined
let pending: { scope: Scope; ticket: Promise<Grant> } | undefined
const directRequests = new WeakMap<AxiosRequestConfig, Scope>()

export function orderFinanceDirectConfigured(): boolean {
  return typeof document !== 'undefined' && !appChatHosted && Boolean(import.meta.env.VITE_ORDER_FINANCE_DIRECT_ORIGIN)
}

export function bindOrderFinancePage(userId: number, cookie = orderFinancePageCookie()): void {
  if (!orderFinanceDirectConfigured() || locked || !Number.isSafeInteger(userId) || userId <= 0) return
  if (expectedUser !== undefined && expectedUser !== userId) {
    invalidateOrderFinanceSession()
    return
  }
  if (cookie) generation ??= cookie
  expectedUser = userId
  if (!channel && typeof window.BroadcastChannel === 'function') {
    channel = new window.BroadcastChannel('ttt:order-finance-session')
    channel.onmessage = event => {
      if (event.data === 'changed' && expectedUser !== undefined) invalidateOrderFinanceSession()
    }
  }
}

export function orderFinancePageCookie(): string | null | undefined {
  return orderFinanceDirectConfigured() ? readCsrfCookie() : undefined
}

function reset(): void {
  controller.abort()
  controller = new AbortController()
  grant = undefined
  pending = undefined
  recovery = undefined
}

export function confirmOrderFinanceLogin(userId: number): void {
  if (!orderFinanceDirectConfigured()) return
  reset()
  locked = false
  generation = undefined
  expectedUser = undefined
  bindOrderFinancePage(userId)
  channel?.postMessage('changed')
}

export function invalidateOrderFinanceSession(broadcast = false): void {
  if (!orderFinanceDirectConfigured()) return
  reset()
  locked = true
  if (broadcast) channel?.postMessage('changed')
}

function currentScope(): Scope {
  const cookie = orderFinancePageCookie()
  if (generation && generation !== cookie) invalidateOrderFinanceSession()
  if (locked || !generation || expectedUser === undefined) throw new AuthExpiredError('登录状态已变更，请刷新页面后再操作订单')
  return { generation, userId: expectedUser, signal: controller.signal }
}

function requireScope(scope: Scope): void {
  if (scope.signal.aborted || currentScope().signal !== scope.signal) {
    throw new AuthExpiredError('登录状态已变更，请刷新页面后再操作订单')
  }
}

export function orderFinancePath(url: string, baseURL?: string): string | undefined {
  if (!orderFinanceDirectConfigured() || !url || url.startsWith('//')) return
  let parsed: URL
  try {
    const absolute = /^https?:\/\//i.test(url)
    if (!absolute && !url.startsWith('/')) return
    const original = absolute || !baseURL ? url : baseURL.replace(/\/$/, '') + url
    parsed = new URL(original, window.location.origin)
  } catch {
    return
  }
  const path = parsed.pathname
  if (parsed.origin !== window.location.origin || parsed.hash || parsed.username || parsed.password
    || path.includes('%') || webException.test(path)) return
  if (servicePaths.has(path.split('/').slice(0, 4).join('/')) || formatPath.test(path)) {
    return path + parsed.search
  }
}

async function issueGrant(scope: Scope): Promise<Grant> {
  const started = performance.now()
  const issued = await request<IssuedTicket>({
    url: ticketPath, baseURL: '', method: 'post', skipUnauthorizedHandler: true, signal: scope.signal,
  })
  requireScope(scope)
  if (issued?.enabled === false) return { scope, expires: started + 30_000 }
  const ttl = issued?.ttl_seconds
  if (issued?.enabled !== true || typeof ttl !== 'number' || !Number.isInteger(ttl) || ttl <= 0 || ttl > 120
    || typeof issued.token !== 'string' || issued.token.length > 8192 || !/^[a-zA-Z0-9_-]+={0,2}$/.test(issued.token)) {
    throw new NetworkError('订单财务凭证返回异常')
  }
  if (issued.user_id !== scope.userId) {
    invalidateOrderFinanceSession()
    throw new AuthExpiredError('登录状态已变更，请刷新页面后再操作订单')
  }
  const expires = started + ttl * 1000
  if (expires <= performance.now()) throw new NetworkError('订单财务凭证已过期，请重试')
  return { scope, expires, authorization: 'OrderFinance ' + issued.token }
}

function orderFinanceGrant(scope: Scope): Promise<Grant> {
  if (grant?.scope.signal === scope.signal && grant.expires > performance.now() + 3000) return Promise.resolve(grant)
  if (pending?.scope.signal === scope.signal) return pending.ticket
  const ticket = issueGrant(scope).then(result => {
    grant = result
    return result
  }).finally(() => {
    if (pending?.ticket === ticket) pending = undefined
  })
  pending = { scope, ticket }
  return ticket
}

async function orderFinanceTarget(path: string): Promise<Target | undefined> {
  const configured = import.meta.env.VITE_ORDER_FINANCE_DIRECT_ORIGIN
  if (!configured || !allowedOrigin.test(configured)) throw new NetworkError('订单财务地址配置无效')
  if (!locked && expectedUser !== undefined && !generation) {
    const signal = controller.signal
    const refresh = recovery ??= fetchMe({ fresh: true, signal }).then(() => undefined)
    try {
      await refresh
    } finally {
      if (recovery === refresh) recovery = undefined
    }
    if (signal.aborted) throw new AuthExpiredError('登录状态已变更，请刷新页面后再操作订单')
  }
  const scope = currentScope()
  const issued = await orderFinanceGrant(scope)
  requireScope(scope)
  if (issued.authorization) return { url: new URL(path, configured).href, authorization: issued.authorization, scope }
}

const METHODS = new Set(['get', 'head', 'post', 'put', 'patch', 'delete'])

export async function prepareOrderFinanceRequest(config: InternalAxiosRequestConfig): Promise<InternalAxiosRequestConfig> {
  const path = orderFinancePath(config.url ?? '', config.baseURL)
  if (!path || !METHODS.has(config.method?.toLowerCase() ?? 'get') || config.auth || config.headers.has('Authorization')) {
    return config
  }
  const target = await orderFinanceTarget(path)
  if (!target) return config
  for (const header of Object.keys(config.headers.toJSON())) {
    if (!safeHeaders.some(allowed => allowed.toLowerCase() === header.toLowerCase())) config.headers.delete(header)
  }
  config.headers.set('Authorization', target.authorization)
  config.url = target.url
  config.baseURL = ''
  config.allowAbsoluteUrls = true
  config.withCredentials = false
  config.withXSRFToken = false
  config.adapter = 'fetch'
  config.fetchOptions = { redirect: 'error', cache: 'no-store' }
  config.signal ??= target.scope.signal
  directRequests.set(config, target.scope)
  return config
}

export function wasOrderFinanceDirect(config: AxiosRequestConfig | undefined): boolean {
  return config !== undefined && directRequests.has(config)
}

export function finishOrderFinanceRequest(config: AxiosRequestConfig | undefined, status: number | undefined): void {
  const scope = config && directRequests.get(config)
  if (scope) {
    requireScope(scope)
    if (status === 401) grant = undefined
  }
}

// Legacy table reads still receive the original response status and JSON.
export async function fetchOrderFinanceTable(url: string, options: RequestInit): Promise<{
  status: number; statusText: string; headers: Headers; text: string
}> {
  const path = orderFinancePath(url)
  if (!path) throw new NetworkError('订单财务请求地址无效')
  const target = await orderFinanceTarget(path)
  const headers = new Headers(options.headers)
  let response: Response
  if (target) {
    const directHeaders = new Headers({ Authorization: target.authorization })
    const accept = headers.get('Accept')
    if (accept) directHeaders.set('Accept', accept)
    response = await fetch(target.url, {
      signal: options.signal, headers: directHeaders, credentials: 'omit', redirect: 'error', cache: 'no-store',
    })
  } else {
    if (!headers.has('X-Requested-With')) headers.set('X-Requested-With', 'XMLHttpRequest')
    response = await fetch(url, { ...options, headers, credentials: 'same-origin' })
  }
  const text = await response.text()
  if (target) {
    requireScope(target.scope)
    if (response.status === 401) grant = undefined
  }
  return { status: response.status, statusText: response.statusText, headers: response.headers, text }
}
