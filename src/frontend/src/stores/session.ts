import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { fetchMe, login as apiLogin, logout as apiLogout, type User } from '@/api/auth'
import { AuthExpiredError } from '@/api/errors'
import { endBrowserChatSession } from '@/api/chatSession'
import { invalidateOrderFinanceSession } from '@/api/orderFinanceDirect'
import { clearMobileRedirectSuppressed } from '@/mobileRedirect'

/** 会话接缝的**真实现**（P5 前端）。骨架里的空壳已被本实现替换（原注释见 git 历史）。不变量（spec §4.1，实现不得改写）：1. `clear()` 的终态只能是 `guest`；`unknown` 只存在于首次 `load()` 之前；2. `load()` 单飞（并发共享同一个 promise）；3. 会话代数（epoch）：`login`/`logout`/`clear` 各 bump 一次，在途 `load` 的结果落地前必须核对代数；4. `logout()` 在**发请求前**就把本地终结为 `guest`；5. `me` 的非 401 失败（网络/超时/5xx）也落 `guest`，并把错误留给 `consumeLoadError()` 提示一次。 */
export type SessionStatus = 'unknown' | 'loading' | 'ready' | 'guest'

/** 「打手」职位名（**前端唯一出处**，D-A1）。与后端常量 `src/apps/business/roles.py::WORKER`（`WORKER = "代练员"`）**同值同源口径**：「打手」= 持「代练员」职位，与已落地的 D-0228 是**同一套判据**（⛔ 不要再造第二套）。⚠️ 这里是**前端自己写的一份字面量**，不是从后端拉的常量（`/api/auth/me` 的 `roles`只给职位**名**）⇒ 两处改名必须**同一次**改（`roles.py::WORKER` 与这里）。`src/stores/__tests__/session.spec.ts` 有一条钉子把本常量与 `roles` 的判定关系钉住。 */
export const WORKER_ROLE = '代练员'

export const useSessionStore = defineStore('session', () => {
  const status = ref<SessionStatus>('unknown')
  const user = ref<User | null>(null)
  const loadError = ref<unknown>(null)

  let epoch = 0
  let inflight: Promise<void> | null = null
  /** `refresh()` 的在途 promise（与 `load()` 的 `inflight` **分开**：两者的终态契约不同）。 */
  let refreshInflight: Promise<void> | null = null

  const identityPlane = computed(() => user.value?.identity_plane ?? null)
  const isTenantAdmin = computed(() => user.value?.is_tenant_admin ?? false)
  /** 账号管理员（账号线 spec §5.4 为前端显式加在 `/api/auth/me` 上的判据）：`apps/accounts/permissions.py::is_account_admin` —— 租户平面上认「系统管理员」Group，`public` 平面上恒 `False`；Django 会话层另会拒绝停用账号。⚠️ 与 `isTenantAdmin` **不是**同义，但分叉只在 `public` 平面（本 SPA 走不到，注释要说全）：那里本判据恒 `False`（`apps/accounts/permissions.py::is_account_admin` 的平面守卫，裁决 R14），而 `is_tenant_admin` 仍看 `is_superuser`（`apps/core/permissions.py::is_tenant_admin`）——即该平面上它可以为 `True`。租户平面上两者同源（都认那个 Group）。⛔ **不按行号引用服务端**（E-0088）。
   * 系统人员菜单/路由用它，而不是 `permissions`——后端那条准入是 `IsAccountAdmin`权限类（`apps/accounts/permissions.py`），**不是一个 codename**。前端隐藏只是体验层：真正的拒绝在 `/api/accounts/*`（非管理员 403）。 */
  const isAccountAdmin = computed(() => user.value?.is_account_admin ?? false)
  /** 是不是「打手」（D-A1）：`/api/auth/me` 的 `roles`（Group 名数组）里**含**「代练员」→ `true`。手机端 `/m/` 的准入判据，由 `m/router/guards.ts` 消费：会话就绪后**不是**打手 ⇒ 弹回桌面根；打手 ⇒ 放行（⛔ 不动 `/m/` 的节点门与 tab 表——那两条判据各管各的）。⚠️ 三条口径，别自行改写：- **按职位，不看节点**：客服 / 客服主管 / 系统管理员在 `/m/` 里也持节点，按本判据一律被请出去（业务方拍板：**只有打手用手机**）。- **不短路管理员**：与 `canNode` 的 `isTenantAdmin || isAccountAdmin` 短路**不同**——系统管理员手机上也看不到 `/m/`（spec §1.5 的代价，如实登记）。- `roles` 缺席 ⇒ 空数组（fail-closed：判不了职位就不算打手，多跳一次好过放错人）。 */
  const isWorker = computed(() => (user.value?.roles ?? []).includes(WORKER_ROLE))
  /** Django codename 集合（**legacy**，D3 冻结）：保留给排障与既有载荷，**不再是任何判据**。 */
  const permissions = computed(() => new Set(user.value?.permissions ?? []))
  /** RBAC 节点 code 集合（`/api/auth/me` 的 `menu_permissions`）。**在「节点 code」这条判据轴上**是唯一来源（spec §5.4 / D-0020 结案）——别的可见性判据各有自己的输入：组的 `hidden` 与 `accountAdminOnly`、路由的 `requiresAccountAdmin`、以及「未声明 `permissions` = 登录即可见」这条默认。缺席按空集（fail-closed）。 */
  const menuPermissions = computed(() => new Set(user.value?.menu_permissions ?? []))
  /** 当前账号有权访问的游戏主键（`/api/auth/me` 的 `game_ids`；升序）。⚠️ 与 `api/config.ts` 的 `fetchGames()` **同源不同路**：那个函数自己再读一次`/api/auth/me`（layui island 是独立文档、没有本 store），这里是给 Vue 壳层用的会话快照。两处都按**缺席 = 空数组**（fail-closed）。 */
  const gameIds = computed(() => user.value?.game_ids ?? [])

  /** 节点判据（路由/菜单/页面按钮共用）。⚠️ **必须短路 `isTenantAdmin` 与 `isAccountAdmin` 两者**：`menu_permissions`对 superuser **没有短路**，而部署空窗期（`seed_rbac_catalog` 与`migrate_rbac_roles --apply` 之间）`is_tenant_admin` 的它**确实是 `[]`**——只读它会让管理员菜单全空。空数组 = 未声明 = 通过（与 `useMenu.canSeeItem` 同一口径）。 */
  function canNode(codes: readonly string[]): boolean {
    if (isTenantAdmin.value || isAccountAdmin.value) return true
    if (codes.length === 0) return true
    return codes.some((c) => menuPermissions.value.has(c))
  }

  function clear(): void {
    endBrowserChatSession()
    invalidateOrderFinanceSession()
    epoch += 1
    // ⚠️ **在途的 `refresh()` 必须一起出清**：不清的话，登出 / 登录
    // 之后紧接着的 `refresh()` 会复用那条**过期** promise（单飞去重），而它的代数已变 ⇒ 结果被
    // 丢弃 ⇒ 语义上「刷新被吞掉」（fail-safe，但静默）。
    // ⚠️ 这一句是**承重**的，⛔ 不能指望 `refresh()` 的 `finally` 顺带完成它（2026-09-25 复评
    // Important 订正）：原注释写「`finally` 里把 null 写回仍然安全」是**假的**——`finally` 现在
    // 只清**自己那一条**，一条陈旧的在途 refresh 落地时**不会**碰这里新起的那条引用。
    // 删掉本行 ⇒ `session.spec.ts` 的「clear() 出清在途 refresh」与「陈旧 refresh 落地」两条用例
    // 变红（已实测：前者只发 1 次、后者的夹具自证看到 #2 根本没发出去）。
    refreshInflight = null
    status.value = 'guest'
    user.value = null
    loadError.value = null
    // ⚠️ **会话终结必须连「手机端抑制标记」一起出清**。
    // 该标记的语义是「**当前这个人**不要再被跳进 /m/」（`mobileRedirect.ts` D-A3），
    // 会话一终结它就不再代表任何人。不清的后果：同一标签页里换人登录后会带着上一个人的判定
    // （非打手留下的标记会把后来的**打手**挡在桌面版外）。
    // 落点选这里：`clear()` 是**唯一**会话终结点（`logout()` 调它，桌面 / 手机端 401 处理器也调它），
    // 所以三条终结路径一次覆盖（E-0027 的「回退 / 清理路径」自检）。
    // 判据：`session.spec.ts` 的「clear() / logout() 清掉抑制标记」两条 + 桌面入口那条真实出口。
    clearMobileRedirectSuppressed()
  }

  function load(): Promise<void> {
    if (status.value === 'ready' || status.value === 'guest') return Promise.resolve()
    if (inflight) return inflight
    const mine = ++epoch
    status.value = 'loading'
    inflight = (async () => {
      try {
        const me = await fetchMe()
        if (mine !== epoch) return
        user.value = me
        status.value = 'ready'
      } catch (e) {
        if (mine !== epoch) return
        user.value = null
        status.value = 'guest'
        if (!(e instanceof AuthExpiredError)) loadError.value = e
      } finally {
        inflight = null
      }
    })()
    return inflight
  }

  /** 取走"引导失败"的错误（只给一次，避免每次导航都弹）。 */
  function consumeLoadError(): unknown {
    const e = loadError.value
    loadError.value = null
    return e
  }

  async function login(username: string, password: string): Promise<void> {
    const me = await apiLogin({ username, password })
    epoch += 1
    // ⚠️ 同 `clear()`：把在途 `refresh()` 出清——否则它那条**过期**
    // promise 会被紧接着的 `refresh()` 复用成静默 no-op。
    refreshInflight = null
    user.value = me
    status.value = 'ready'
    loadError.value = null
  }

  /** 重新取一次当前用户（顶栏头像同步线）。用途：已经在 `ready` 之后「用户资料在别处变了」——目前唯一调用点是壳收到 island 的`AVATAR_UPDATED_MESSAGE_TYPE`（「我的资料」页传完新头像），重取后右上角随即换新头像。⚠️ **不能复用 `load()`**：`load()` 到达 `ready`/`guest` 后**短路**（单飞 + 终态契约），而本函数的用途恰恰是「已经是 ready、但要再取一次」——走 `load()` 会变成静默 no-op。取数仍走**同一个** `fetchMe()`（⛔ 不新拼 URL、不新开取数通道）。⚠️ **失败不动已有会话**：一次刷新失败（网络抖动 / 5xx）不该把已登录的人踢成 `guest`——顶栏保留旧头像，比「刷新一下人就没了」好。代数（`epoch`）照旧：在途结果落地前核对，期间若 `clear()` / `login()` 发生则丢弃（不变量 3）。⚠️ **在途去重**：正常路径只发一次，但同源脚本可以连续
   * `postMessage`（壳每收到一条「头像已更新」就调一次 `refresh()`）⇒ 不去重的话**每一条都发一个** `/api/auth/me`。所以同一轮里第二次调用**复用**在途那条 promise（也**不再** bump 代数——否则第一条的结果会被自己的第二跳判成过期）。⚠️ **失败语义不变**：那个 `catch` 依旧只保留旧会话；`finally` 只把**自己那一条**从在途里清掉（复评 Important：⛔ 不能无条件清——见下），所以「失败之后下一次刷新**真的**再发一次」这条既有语义也保持不变。 */
  function refresh(): Promise<void> {
    if (refreshInflight) return refreshInflight
    const mine = ++epoch
    // ⚠️ **`finally` 只清自己那一条**：把本次 promise 存成局部变量，
    // 落地时比对身份再清。若**无条件** `refreshInflight = null`，一条**陈旧**的在途 refresh
    // （代数已过期、结果注定被丢弃）落地时，会把**更新**的那条在途引用也清掉 ⇒ 紧接着的
    // `refresh()` 误以为没有在途，又发一条 `/api/auth/me`。复评实测序列：refresh#1 在途 →
    // `clear()` → refresh#2（计数 2）→ 让 #1 落地 → refresh#3 ⇒ **计数 3、两条同时在途**；
    // 修复后 #3 复用 #2 ⇒ 计数停在 2。判据见 `session.spec.ts` 的「陈旧 refresh 落地」那条。
    let promise: Promise<void> | null = null
    promise = (async () => {
      try {
        const me = await fetchMe()
        if (mine !== epoch) return
        user.value = me
        status.value = 'ready'
      } catch {
        // 刻意留空：刷新失败只保留旧会话（见 docstring），下一次交互/导航会再取。
      } finally {
        if (refreshInflight === promise) refreshInflight = null
      }
    })()
    refreshInflight = promise
    return promise
  }

  async function logout(): Promise<void> {
    clear()
    try {
      await apiLogout()
    } catch {
      // 服务端幂等；本地已经终结，不回滚（spec §4.1-4）
    }
  }

  return {
    status,
    user,
    identityPlane,
    isTenantAdmin,
    isAccountAdmin,
    isWorker,
    permissions,
    menuPermissions,
    gameIds,
    canNode,
    clear,
    load,
    consumeLoadError,
    login,
    logout,
    refresh,
  }
})
