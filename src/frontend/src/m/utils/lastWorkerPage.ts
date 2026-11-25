import type { RouteLocationNormalized, Router } from 'vue-router'
import type { User } from '@/api/auth'
import { MINE_PERFORMANCE_HASH, M_TABS } from '../tabs'
import { mSitePath, sanitizeMRedirect } from './redirect'

export const WXPUSHER_ENTRY = 'wxpusher'

const resumableRoutes = new Set([
  ...M_TABS.map((tab) => tab.routeName),
  'm-order-detail',
  'm-me-notifications',
  'm-me-notifications-order',
])

function storageKey(user: User | null): string | null {
  if (!user?.identity_plane || !Number.isSafeInteger(user.id) || user.id < 1) return null
  return `ttt:worker-page:${user.identity_plane}:${user.id}`
}

function canResume(route: Pick<RouteLocationNormalized, 'meta'> & { name?: unknown }): boolean {
  return typeof route.name === 'string' && resumableRoutes.has(route.name) && route.meta.requiresAuth !== false
}

export function rememberWorkerPage(user: User | null, route: RouteLocationNormalized): void {
  const key = storageKey(user)
  if (!key || !canResume(route)) return
  try {
    const inPerformance = route.name === 'm-mine' && route.hash === MINE_PERFORMANCE_HASH
    window.localStorage.setItem(key, route.path + (inPerformance ? MINE_PERFORMANCE_HASH : ''))
  } catch {
    // WebView 禁用存储时继续正常导航。
  }
}

export function recallWorkerPage(user: User | null, router: Router) {
  const key = storageKey(user)
  if (!key) return null
  try {
    const path = window.localStorage.getItem(key)
    if (!path?.startsWith('/') || !sanitizeMRedirect(mSitePath(path))) return null
    const target = path === '/performance'
      ? { name: 'm-mine', hash: MINE_PERFORMANCE_HASH }
      : path
    const route = router.resolve(target)
    const inPerformance = route.name === 'm-mine' && route.hash === MINE_PERFORMANCE_HASH
    const canonical = route.path + (inPerformance ? MINE_PERFORMANCE_HASH : '')
    return route.fullPath === canonical && canResume(route) ? route : null
  } catch {
    return null
  }
}
