import type { Router } from 'vue-router'
import { setUnauthorizedHandler } from '@/api/http'
import { useSessionStore } from '@/stores/session'

/**
 * 401 的唯一归属：**清会话 + 跳登录**（spec §5.4 的四条不变量）。`api/http.ts` 只负责在 401 时回调注入的处理器——它不 import store/router。
 * 返回处理器本体是为了**可测**：装配用的是同一个函数（`installUnauthorizedHandler`），测试直接调它，就能覆盖「并发只导航一次」「在 /login 上不导航」这两条今天测不到的规则。
 */
export function createUnauthorizedHandler(router: Router): () => void {
  let redirecting = false
  return () => {
    const session = useSessionStore()
    session.clear()
    const current = router.currentRoute.value
    if (current.name === 'login') return // 已在登录页：不重复导航（防死循环）
    if (redirecting) return // 并发 401 只导航一次
    redirecting = true
    const target = current.fullPath
    const query = isLoginOwnPath(target) ? {} : { redirect: target }
    void router
      .replace({ name: 'login', query })
      // ⚠️ 这里**丢弃全部拒绝**（不只是重定向超 30 次那一种）。三种形态要分清：真实成因是 vue-router 在重定向链超 30 次时 reject（防死循环机制）；
      // `{ name: 'login' }` 解析不到的 reject 也被吞——那属于**编程错误**，靠 `unauthorized.spec.ts` 的「跳转真的发生」断言兜（改名会被它抓到），不靠这里报错。不用 `void …finally(...)`：`void` 只吃掉返回值、**不**吃拒绝，会留下 Unhandled Rejection。
      .catch(() => {})
      .finally(() => {
        redirecting = false
      })
  }
}

/**
 * `redirect` 不得是 `/login` 自身的 fullPath（否则登录后又回登录页）。判据**恰为三种形态**：`/login` 本身、`/login?…`、`/login/…`。
 * `startsWith('/login')` 会把 `/login2`、`/logins` 这类无关路径也一并剥掉 `redirect`（收紧过的判据；有 `/login2` 用例的反例钉住）。
 */
function isLoginOwnPath(target: string): boolean {
  return target === '/login' || target.startsWith('/login?') || target.startsWith('/login/')
}

export function installUnauthorizedHandler(router: Router): void {
  setUnauthorizedHandler(createUnauthorizedHandler(router))
}
