import { appChatHosted } from '../components/appChatWebView'
import { START_LOCATION, type RouteLocationResolved, type Router } from 'vue-router'
import { Toast } from '../framework7Ui'
import { useSessionStore } from '@/stores/session'
import {
  clearMobileRedirectSuppressed,
  readMobileRedirectSuppressed,
  writeMobileRedirectSuppressed,
} from '@/mobileRedirect'
import { canEnterMine, canReadWorkerPerformance, firstPermittedTab, MINE_PERFORMANCE_HASH } from '../tabs'
import { recallWorkerPage, rememberWorkerPage, WXPUSHER_ENTRY } from '../utils/lastWorkerPage'
import { isOrderNoShape } from '../utils/orderNoShape'
import { mRoutePathOf, mSitePath, sanitizeMRedirect } from '../utils/redirect'
import {
  forgetWxPusherHallContinuation,
  forgetWxPusherContinuation,
  readWxPusherHallContinuation,
  readWxPusherContinuation,
  rememberWxPusherHallContinuation,
  rememberWxPusherContinuation,
  requestWxPusherHallContinueUrl,
  requestWxPusherSharedHall,
  requestWxPusherContinueUrl,
  returnToWxPusherMessageDetail,
  type SharedHallTarget,
  shouldContinueWxPusherChatOnIos,
  waitForDaidaitongHandoff,
  WXPUSHER_ORDER_PASSWORD_NOTICE,
  wxpusherChatAppLink,
  wxpusherChatPathFromRedirect,
  wxpusherChatSitePath,
  wxpusherHallAppLink,
  wxpusherHallResumeUrl,
  wxpusherHallTicketFromHash,
  wxpusherSharedHallClaimPath,
  wxpusherSharedHallQuery,
  wxpusherSharedHallTarget,
  wxpusherOrderAppLink,
  wxpusherOrderNoFromRedirect,
} from '../utils/wxpusherOrderLink'

function orderPasswordRoute(orderNo: string) {
  return {
    name: 'm-login',
    query: {
      entry: WXPUSHER_ENTRY,
      redirect: `/m/orders/${orderNo}`,
      notice: WXPUSHER_ORDER_PASSWORD_NOTICE,
    },
  }
}

function wxpusherPasswordRoute(path: string) {
  return { name: 'm-login', query: { entry: WXPUSHER_ENTRY, redirect: path } }
}

function sharedHallRetryRoute(target: SharedHallTarget) {
  return { name: 'm-wxpusher-hall-entry', query: { ...wxpusherSharedHallQuery(target), retry: '1' }, replace: true }
}

/**
 * 手机端鉴权守卫（语义照桌面 `router/guards.ts`）：① 每次导航先 `await session.load()`（守卫是 `load()` 的唯一所有者，`main.ts` 不自己调 `me`）；② `meta.requiresAuth` **默认 true**（fail-closed：漏声明 = 要登录，`m-login` 显式 false）③ 未认证 → `m-login?redirect=<站内 /m/ 路径>`，已认证但 `meta.permissions` 不满足 → toast 后回 `m-home` ④ `m-home` 是**虚拟落地页**：进入即重定向到第一个有权限的 tab、都无 → `m-empty`——⚠️ 不能写成路由记录的 `redirect` 字段：那会在守卫（`session.load()`）**之前**求值，首导航时 session 还是 `unknown`，会把已登录的人送去 `m-empty` ⑤ 身份门：会话就绪后不是打手 ⇒ 写抑制标记 + 自证写成功 + 整页回桌面根（写不进去则不弹）；是打手 ⇒ 清标记；未登录（非 ready）**不弹**、照旧走 `m-login`。
 * 与桌面的两处**有意差异**：权限不满足的提示用 Framework7 `Toast`（⛔ 手机端不 import element-plus，桌面 `notifyError` 会牵出 EP）；`redirect` 存**站内 `/m/` 路径**（`mSitePath(to.fullPath)`）不是 `fullPath` 原文——fullPath 不含 base（读源码实证见 `utils/redirect.ts`），存原文会被 `sanitizeMRedirect` 的 `/m/` 前缀判据一律拒掉，登录后永远回不到原页。
 * ⚠️ 只许返回路由目标（与桌面同一条纪律），不要 fire-and-forget 地 `void router.replace(...)`。
 */
export function installMGuards(router: Router): void {
  let navigationId = 0
  let appHandoff: AbortController | undefined
  router.beforeEach(async (to, from) => {
    if (appChatHosted) {
      await useSessionStore().load()
      return true
    }
    const currentNavigation = ++navigationId
    appHandoff?.abort()
    appHandoff = undefined
    const canContinueWxPusherLogin = () =>
      currentNavigation === navigationId && document.visibilityState !== 'hidden'
    const isAppBridge = to.name === 'm-wxpusher-open'
    const isWxPusherLoginReturn = from === START_LOCATION && to.query.entry === WXPUSHER_ENTRY
      && to.query.redirect === undefined && (isAppBridge || to.name === 'm-home' || to.name === 'm-login')
    const willConfirm = isAppBridge && to.query.continue === 'confirm'
    const pendingHall = isWxPusherLoginReturn && !isAppBridge ? readWxPusherHallContinuation() : null
    if (pendingHall) {
      const session = useSessionStore()
      await session.load()
      if (currentNavigation !== navigationId) return false
      if (session.status === 'ready' && session.isWorker) {
        window.location.replace(wxpusherHallResumeUrl(pendingHall))
        return false
      }
      return {
        name: 'm-wxpusher-hall-entry',
        query: { entry: WXPUSHER_ENTRY, retry: '1' },
        hash: '#hall=' + pendingHall,
        replace: true,
      }
    }
    const pending = isWxPusherLoginReturn ? readWxPusherContinuation(!willConfirm) : null
    if (pending) {
      const password = 'orderNo' in pending ? orderPasswordRoute(pending.orderNo)
        : wxpusherPasswordRoute('chatPath' in pending ? pending.chatPath : wxpusherSharedHallClaimPath(pending.sharedHall))
      if (willConfirm) {
        try {
          window.location.replace('/api/auth/wxpusher/login')
          return false
        } catch {
          forgetWxPusherContinuation()
          return 'sharedHall' in pending ? sharedHallRetryRoute(pending.sharedHall) : password
        }
      }
      const session = useSessionStore()
      await session.load()
      if (currentNavigation !== navigationId) return false
      if ('sharedHall' in pending) {
        if (session.status !== 'ready') return sharedHallRetryRoute(pending.sharedHall)
        return session.isWorker ? mRoutePathOf(wxpusherSharedHallClaimPath(pending.sharedHall)) : password
      }
      if ('chatPath' in pending) {
        if (session.status !== 'ready') return password
        const target = router.resolve(mRoutePathOf(pending.chatPath))
        return { name: 'm-wxpusher-chat', params: target.params, query: { ...target.query, open: 'web' }, replace: true }
      }
      return session.status === 'ready' && session.isWorker
        ? { name: 'm-order-detail', params: { orderNo: pending.orderNo }, replace: true }
        : password
    }
    if (isAppBridge || to.name === 'm-wxpusher-return' || to.name === 'm-order-screenshot-browser') return true
    const hallEntry = to.name === 'm-wxpusher-hall-entry'
    const sharedEntry = hallEntry && to.query.shared !== undefined
    const sharedHall = sharedEntry ? wxpusherSharedHallTarget(to.query, to.hash) : null
    const hallTicket = hallEntry && !sharedEntry ? wxpusherHallTicketFromHash(to.hash, to.query.entry) : null
    if (hallEntry) {
      forgetWxPusherContinuation()
      forgetWxPusherHallContinuation()
      if (to.query.retry === '1' || (sharedHall === null && hallTicket === null)) return true
    }
    const isChat = to.name === 'm-wxpusher-chat'
    const chatPath = isChat ? wxpusherChatSitePath(to.params.conversationId, to.query.message, to.query.open) : null
    const chatReturn = to.name === 'm-login' ? wxpusherChatPathFromRedirect(to.query.redirect) : null
    const orderNo = to.params.orderNo
    const orderLogin = to.name === 'm-order-detail' && to.path.startsWith('/wxpusher/orders/')
      && to.query.entry === WXPUSHER_ENTRY && typeof orderNo === 'string' && isOrderNoShape(orderNo)
      ? { name: 'm-login', query: { entry: WXPUSHER_ENTRY, redirect: `/m/orders/${orderNo}` } }
      : null
    const orderAppLink = orderLogin ? wxpusherOrderAppLink(orderNo, to.query.entry) : undefined
    const hallAppLink = hallTicket || (sharedHall && to.query.open !== 'web')
      ? wxpusherHallAppLink(to.query.source, to.query.ref, to.query.entry) : undefined
    const chatAppLink = chatPath === null ? undefined : wxpusherChatAppLink(chatPath, to.query.entry)
    let appLink = from === START_LOCATION ? orderAppLink ?? hallAppLink ?? chatAppLink : undefined
    const loadSharedRouting = sharedHall
      ? () => requestWxPusherSharedHall(sharedHall).catch(() => ({ url: null, openInApp: false }))
      : null
    const sharedRouting = appLink && loadSharedRouting ? await loadSharedRouting() : null
    if (currentNavigation !== navigationId) return false
    if (sharedRouting && !sharedRouting.openInApp) appLink = undefined
    if (from === START_LOCATION && (orderLogin || chatPath !== null)) {
      forgetWxPusherContinuation()
      forgetWxPusherHallContinuation()
    }

    let autoContinueChat = from === START_LOCATION && shouldContinueWxPusherChatOnIos(chatPath, to.query.entry)
    if (appLink || hallTicket || sharedHall) {
      const controller = new AbortController()
      appHandoff = controller
      const handoff = await waitForDaidaitongHandoff(appLink, controller.signal)
      if (handoff === 'cancelled' || currentNavigation !== navigationId) return false
      appHandoff = undefined
      if (chatPath !== null) autoContinueChat = handoff === 'foreground'
      if (handoff === 'returned' && chatPath === null) {
        returnToWxPusherMessageDetail()
        return { name: 'm-wxpusher-return', replace: true }
      }
    }

    if (hallTicket && !canContinueWxPusherLogin()) return false
    const session = useSessionStore()
    await session.load()
    if (sharedHall && loadSharedRouting) {
      if (!canContinueWxPusherLogin()) return false
      const path = wxpusherSharedHallClaimPath(sharedHall)
      if (session.status === 'ready') {
        return session.isWorker ? mRoutePathOf(path) : wxpusherPasswordRoute(path)
      }
      const routing = sharedRouting ?? await loadSharedRouting()
      if (!canContinueWxPusherLogin()) return false
      if (!routing.url) return sharedHallRetryRoute(sharedHall)
      if (!rememberWxPusherContinuation({ sharedHall })) return wxpusherPasswordRoute(path)
      try {
        window.location.replace(routing.url)
        return false
      } catch {
        forgetWxPusherContinuation()
        return sharedHallRetryRoute(sharedHall)
      }
    }
    if (hallTicket) {
      if (!canContinueWxPusherLogin()) return false
      if (session.status !== 'ready' || !session.isWorker) {
        try {
          const target = await requestWxPusherHallContinueUrl(hallTicket)
          if (!canContinueWxPusherLogin()) return false
          if (rememberWxPusherHallContinuation(hallTicket)) {
            window.location.replace(target)
            return false
          }
        } catch {
          if (!canContinueWxPusherLogin()) return false
          forgetWxPusherHallContinuation()
        }
        return true
      }
      window.location.replace(wxpusherHallResumeUrl(hallTicket))
      return false
    }
    const continuation = chatPath !== null && autoContinueChat && session.status !== 'ready' ? { chatPath }
      : orderLogin && orderAppLink && from === START_LOCATION && typeof orderNo === 'string'
        && (session.status !== 'ready' || !session.isWorker) ? { orderNo } : null
    if (continuation && canContinueWxPusherLogin()) {
      try {
        const target = await requestWxPusherContinueUrl(continuation)
        if (canContinueWxPusherLogin() && rememberWxPusherContinuation(continuation)) {
          window.location.replace(target)
          return false
        }
      } catch {
        if (currentNavigation === navigationId) forgetWxPusherContinuation()
      }
    }
    const fromWxPusher = to.query.entry === WXPUSHER_ENTRY && (to.name === 'm-home' || to.name === 'm-login')
    const bootError = session.consumeLoadError()
    // 一次性：非 401 的引导失败。桌面用 notifyError；手机端用 Toast 显示同一句 msg。
    if (bootError) Toast.error(bootError instanceof Error ? bootError.message : '会话加载失败')

    // ── 身份门：手机端只服务「打手」（持「代练员」职位）。⚠️ **顺序承重，别挪**：
    // 必须在 `await session.load()` **之后**判（身份来自 `/api/auth/me` 的 `roles`）；⛔ 不能拿「未登录」当理由弹——业务方选的是「先跳 /m/，登录后非打手弹回」⇒ 未登录必须照旧落到下面的 `m-login`（`status !== 'ready'` 时 `isWorker` 恒 false），否则打手连登录页都看不到。
    // 所以在 `status === 'ready'` 这一支里判：非打手 ⇒ 写抑制标记（自证成功才跳）+ 回桌面根；打手 ⇒ 清标记。
    if (session.status === 'ready') {
      if (!session.isWorker && !isChat && chatReturn === null) {
        if (orderLogin) return orderLogin
        if (fromWxPusher) {
          // 明确进入打手端时保留当前桌面会话，等用户主动提交打手凭据。
          return to.name === 'm-login' ? true : { name: 'm-login', query: { entry: WXPUSHER_ENTRY }, replace: true }
        }
        // ⚠️ 先写标记、**再自证写进去了**，两步都成立才跳（D-A3）。
        writeMobileRedirectSuppressed()
        // ⚠️ 自证这一步是**承重**的：存储写不进去（隐私模式 / 配额）时标记永远不在 ⇒ 桌面入口每次整页加载都跳回 `/m/` ⇒ 这里又 `replace('/')` ⇒ **来回跳**，是真死循环、不是「多跳一次」。写不进去就**不弹**：退回旧行为（非打手留在 `/m/`，按节点门照常走），宁可少管一次也不能成环。
        if (readMobileRedirectSuppressed()) {
          // 桌面根不是本 SPA 的路由 ⇒ 只能整页换（`location.replace` 不留历史，返回可回退）。
          // 这与模块 docstring 的「只许返回路由目标」不冲突：那条管的是**本 SPA 内部**导航，
          // 这里是**离开 `/m/`**；⛔ 不要写成 `router.replace('/')`（m 端路由表里没有 '/')。
          window.location.replace('/')
          // 导航中止；文档正在被桌面壳替换（hooks 里没法 await 整页跳转）。
          return false
        }
      } else if (session.isWorker) {
        clearMobileRedirectSuppressed()
      }
    }

    function canVisit(target: Pick<RouteLocationResolved, 'name' | 'meta'>): boolean {
      return target.name === 'm-mine'
        ? canEnterMine(session.canNode)
        : session.canNode(target.meta.permissions ?? [])
    }

    const requiresAuth = to.meta.requiresAuth ?? true
    if (!requiresAuth) {
      if (to.name === 'm-login' && session.status === 'ready') {
        if (chatReturn !== null) return mRoutePathOf(chatReturn)
        const returnOrder = wxpusherOrderNoFromRedirect(to.query.redirect, to.query.entry)
        if (returnOrder) return { name: 'm-order-detail', params: { orderNo: returnOrder } }
        const target = fromWxPusher ? sanitizeMRedirect(to.query.redirect) : null
        if (target !== null) {
          const path = mRoutePathOf(target)
          if (router.resolve(path).name === 'm-hall') return path
        }
        return { name: 'm-home', query: fromWxPusher ? { entry: WXPUSHER_ENTRY } : undefined }
      }
      return true
    }

    if (session.status !== 'ready') {
      if (chatPath !== null) return wxpusherPasswordRoute(chatPath)
      return orderLogin ?? { name: 'm-login', query: { redirect: mSitePath(to.fullPath) } }
    }

    if (!canVisit(to)) {
      Toast('无权限访问该页面')
      return { name: 'm-home' }
    }

    if (to.name === 'm-mine' && to.hash === MINE_PERFORMANCE_HASH && !canReadWorkerPerformance(session.canNode)) {
      return { name: 'm-mine', query: to.query, replace: true }
    }

    if (to.name === 'm-home') {
      if (fromWxPusher) {
        const previous = recallWorkerPage(session.user, router)
        if (previous && canVisit(previous)) {
          return { path: previous.path, hash: previous.hash, replace: true }
        }
      }
      const first = firstPermittedTab((codes) => session.canNode(codes))
      return { name: first?.routeName ?? 'm-empty', replace: fromWxPusher }
    }
    return true
  })
  router.afterEach((to, _from, failure) => {
    if (failure || appChatHosted) return
    if (to.name === 'm-order-detail' && to.path.startsWith('/orders/')) forgetWxPusherContinuation()
    const session = useSessionStore()
    if (session.status === 'ready' && session.isWorker) rememberWorkerPage(session.user, to)
  })
}
