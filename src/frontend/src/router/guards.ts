import type { Router } from 'vue-router'
import { notifyError } from '@/api/notify'
import { BizError } from '@/api/errors'
import { useSessionStore } from '@/stores/session'

/** 先加载会话，再核对页面权限。已登录但无权进入时回到该版本首页。 */
export function installGuards(router: Router): void {
  router.beforeEach(async (to) => {
    if (to.name === 'visitor-chat') return true
    const session = useSessionStore()
    await session.load()
    const bootError = session.consumeLoadError()
    if (bootError) notifyError(bootError) // 一次性：非 401 的引导失败

    const requiresAuth = to.meta.requiresAuth ?? true
    if (!requiresAuth) {
      if (to.name === 'login' && session.status === 'ready') return { name: 'home' }
      return true
    }

    if (session.status !== 'ready') {
      return { name: 'login', query: { redirect: to.fullPath } }
    }

    const home = to.matched.some((record) => record.name === 'v2') ? 'v2-home' : 'home'
    if (!session.canNode(to.meta.permissions ?? [])) {
      notifyError(new BizError(403, '无权限访问该页面'))
      return { name: home }
    }
    // 账号管理员判据（`meta.requiresAccountAdmin`）。与上面的节点 code 判据是**两条独立**判据：后端某些端点的准入是 `IsAccountAdmin` 权限类、不是一个节点 code，不能塞进 `permissions`（那会让 `canNode([])` 语义与 `useMenu` 的 `canSeeItem` 都被牵连）；判据值来自 `/api/auth/me` 的 `is_account_admin`。
    // ⚠️ **它不再挂在【系统人员】那 7 条上**：后端账号端点的准入已放宽到 `IsAccountManager`（系统管理员 **或** 持任一账号管理节点），「人事」正是持 `system_user.*` 节点的**非**管理员 ⇒ 留着这道门会让它看不到入口、直敲 URL 也被兜回首页。今天用它的只有 6 条**自有功能**路由；本判据的机制与它的合成路由用例（`__tests__/guards.spec.ts`）保持不变。
    if (to.meta.requiresAccountAdmin === true && !session.isAccountAdmin) {
      notifyError(new BizError(403, '无权限访问该页面'))
      return { name: home }
    }
    return true
  })
}
