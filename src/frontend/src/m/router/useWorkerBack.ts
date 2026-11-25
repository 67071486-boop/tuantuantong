import { useRouter, type RouteLocationRaw } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { canReadWorkerOrders, canReadWorkerPerformance, firstPermittedTab } from '../tabs'
import { mSitePath, sanitizeMRedirect } from '../utils/redirect'

export const SCREENSHOT_RETURN_ORDER = 'workerScreenshotReturnOrder'

export function useWorkerBack(parent: () => RouteLocationRaw): () => void {
  const router = useRouter()
  const session = useSessionStore()

  function canReturnTo(nodes: readonly string[]): boolean {
    const includesOrders = nodes.includes('my_orders')
    const includesPerformance = nodes.includes('my_performance')
    if (includesOrders || includesPerformance) {
      return (includesOrders && canReadWorkerOrders(session.canNode))
        || (includesPerformance && canReadWorkerPerformance(session.canNode))
    }
    return session.canNode(nodes)
  }

  return () => {
    const target = parent()
    const resolved = router.resolve(target)
    if (!canReturnTo(resolved.meta.permissions ?? [])) {
      void router.replace({ name: firstPermittedTab(session.canNode)?.routeName ?? 'm-empty' })
      return
    }

    const previous = router.options.history.state.back
    if (typeof previous === 'string' && previous.startsWith('/')
      && sanitizeMRedirect(mSitePath(previous)) && router.resolve(previous).path === resolved.path) {
      router.back()
    } else {
      void router.replace(target)
    }
  }
}
