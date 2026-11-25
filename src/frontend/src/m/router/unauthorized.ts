import type { Router } from 'vue-router'
import { setUnauthorizedHandler } from '@/api/http'
import { useSessionStore } from '@/stores/session'
import { mSitePath } from '../utils/redirect'

/**
 * 401 的唯一归属：**清会话 + 跳登录**（形状照桌面 `router/unauthorized.ts`，spec §5.4 四条不变量）。
 * `api/http.ts` 只负责在 401 时回调注入的处理器——它不 import store/router。与桌面的差异只有两处：目标路由是 `m-login`；`redirect` 存**站内 `/m/` 路径**（理由见 `router/guards.ts` 与 `utils/redirect.ts`）。返回处理器本体是为了**可测**（与桌面同一条理由）。
 */
export function createMUnauthorizedHandler(router: Router): () => void {
  let redirecting = false
  return () => {
    const session = useSessionStore()
    session.clear()
    const current = router.currentRoute.value
    if (current.name === 'm-login') return // 已在登录页：不重复导航（防死循环）
    if (redirecting) return // 并发 401 只导航一次
    redirecting = true
    const query = isMLoginOwnPath(current.fullPath) ? {} : { redirect: mSitePath(current.fullPath) }
    void router
      .replace({ name: 'm-login', query })
      // 与桌面同口径：丢弃全部拒绝（重定向链超限 / 编程错误都靠测试断言兜，不靠这里报错）。
      .catch(() => {})
      .finally(() => {
        redirecting = false
      })
  }
}

/** `redirect` 不得是 `/login` 自身的路径（否则登录后又回登录页）。三种形态，与桌面同款。 */
function isMLoginOwnPath(routePath: string): boolean {
  return routePath === '/login' || routePath.startsWith('/login?') || routePath.startsWith('/login/')
}

export function installMUnauthorizedHandler(router: Router): void {
  setUnauthorizedHandler(createMUnauthorizedHandler(router))
}
