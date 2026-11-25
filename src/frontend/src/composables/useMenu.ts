import { computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  BOTTOM_ITEMS,
  MENU_GROUPS,
  TOP_LEVEL_ITEMS,
  type MenuGroup,
  type MenuItem,
} from '@/config/menu'
import { useSessionStore } from '@/stores/session'

/** 可点判定只有一条规则：有 routeName 且路由表里真有该 name。 */
export function isClickable(item: MenuItem, hasRoute: (name: string) => boolean): boolean {
  return item.routeName !== undefined && hasRoute(item.routeName)
}

/** 只负责「隐藏的单条不渲染」。可点与否由 isClickable 单独判定——两个概念不要混。本文件里 `useMenu()` 已改为在 `keepItem` 一处同时收口「`hidden`」与「权限」（见下），所以这两个函数不再是 `useMenu()` 的路径；保留导出是为了不让既有测试失效。`_hasRoute` 参数在本函数里没有用到：前缀下划线是给 lint 的信号（见 eslint.config.ts 的 `argsIgnorePattern: '^_'`），本工程的 tsconfig **没有**开`noUnusedParameters`。 */
export function visibleItems(items: MenuItem[], _hasRoute: (name: string) => boolean): MenuItem[] {
  return items.filter((i) => !i.hidden)
}

export function visibleGroups(groups: MenuGroup[], _hasRoute: (name: string) => boolean): MenuGroup[] {
  return groups.filter((g) => !g.hidden)
}

export type MenuViewer = {
  /** 租户管理员。判据在 **`apps/core/permissions.py::is_tenant_admin`** 一处定义——租户平面上 = 持「系统管理员」Group；`public` 平面仍看 `is_superuser`。⚠️ 与下面的 `isAccountAdmin` **在租户平面上同源**（两者都认那个 Group，`is_account_admin` 另有一条预取缓存的安全网）；分叉只在 `public` 平面（那里 `isAccountAdmin` 恒 `False`、本判据看 `is_superuser`）——本 SPA 走不到，但注释要说全。⛔ **不要再按行号引用服务端**（E-0088）：判据会移位，符号名不会。 */
  isTenantAdmin: boolean
  /** 账号管理员。判据在 **`apps/accounts/permissions.py::is_account_admin`** 一处定义——租户平面上 = 持「系统管理员」Group（委托 `is_tenant_admin`，另有一条预取缓存的安全网）；`public` 平面一律 `False`。⚠️ 与上面的 `isTenantAdmin` **在租户平面上同源**（两者都认那个 Group）；分叉只在 `public`平面（那里本判据恒 `False`、`isTenantAdmin` 看 `is_superuser`）——本 SPA 走不到，但注释要说全。⛔ **不要再按行号引用服务端**（E-0088）：判据会移位，符号名不会。 */
  isAccountAdmin: boolean
  /** **RBAC 节点 code** 集合（`/api/auth/me` 的 `menu_permissions`）。 */
  nodePermissions: ReadonlySet<string>
}

/** 菜单条目的可见性判据（spec §5.4 / D-0020 结案）。规则：**`is_tenant_admin` 或 `is_account_admin` 全可见**；未声明或 `[]` 视为"登录即可见"；否则要求条目声明的**节点 code** 与用户的 `menu_permissions` 有交集。⚠️ 两条短路都要（不是 `|| isTenantAdmin` 一条）：`menu_permissions` 对 superuser**没有短路**，而部署空窗期里 `is_tenant_admin` 的它确实是 `[]`。这是体验层，不是访问控制：隐藏只是 DOM 里没有节点。真正的拒绝在 `/api/**`。没看到菜单不等于服务端会拦下这个请求。 */
export function canSeeItem(item: MenuItem, viewer: MenuViewer): boolean {
  if (item.accountAdminOnly && !viewer.isAccountAdmin) return false
  if (viewer.isTenantAdmin || viewer.isAccountAdmin) return true
  const needed = item.permissions ?? []
  if (needed.length === 0) return true
  return needed.some((c) => viewer.nodePermissions.has(c))
}

/** 分组的可见性判据。两条独立规则：`hidden`（冻结 IA 的表达）与 `accountAdminOnly`（组级「只对账号管理员可见」，判据值 = `/api/auth/me` 的 `is_account_admin`）。今天没有任何组把 `accountAdminOnly` 设为真：【系统人员】的可见性在条目级节点码上。机制仍要留着，它和路由级 `requiresAccountAdmin`（`router/guards.ts`）同形，后者还挂在自有功能路由上。删掉它会连 `__tests__/useMenu.spec.ts` 的 `canSeeGroup` 一起空掉。抽成纯函数与 `canSeeItem` 同理——`useMenu()` 内部要用 `useRouter()`，纯函数测试拿不到。 */
export function canSeeGroup(group: MenuGroup, viewer: MenuViewer): boolean {
  if (group.hidden) return false
  if (group.accountAdminOnly && !viewer.isAccountAdmin) return false
  return true
}

/** 菜单的出口：可见性判据来自本文件的 `canSeeItem`（条目按需声明 `permissions`），调用方（`AppSidebar`）只消费 `topLevel` / `groups` / `bottom`，不需要知道判据。⚠️ `bottom`（侧栏最底部那一条，2026-10-08「问题反馈」）**必须走同一个 `keepItem`**：另写一套可见性判据就是「同一事实两套判据」（E-0021），而两套的漂移只表现为**某个条目该显示时没显示**——没有任何网会报。承重判据见`__tests__/useMenu.bottom.spec.ts`（它按 `canSeeItem` 的语义逐态钉住 `bottom`）。 */
export function useMenu(leadingItems: Readonly<Record<string, readonly MenuItem[]>> = {}) {
  const router = useRouter()
  const session = useSessionStore()
  const viewer = computed<MenuViewer>(() => ({
    isTenantAdmin: session.isTenantAdmin,
    isAccountAdmin: session.isAccountAdmin,
    nodePermissions: session.menuPermissions,
  }))
  const hasRoute = (name: string) => router.hasRoute(name)

  // 判据只这一处：`hidden`（冻结 IA 的表达）与权限**都**在 `keepItem` 里收口。
  // 组一级今天有两条判据：`g.hidden`，以及 `g.accountAdminOnly`
  // （`accountAdminOnly` 今天没有组在用；机制保留的理由见 `canSeeGroup`）。
  const keepItem = (i: MenuItem) => !i.hidden && canSeeItem(i, viewer.value)
  const keepGroup = (g: MenuGroup) => canSeeGroup(g, viewer.value)

  /** 空组不渲染。为什么非有不可：`keepGroup` 只判**组级**的门，而条目级那一层在下面按 `keepItem` 过滤 ⇒一个"组级门过了、但组内一条都看不见"的账号会看到一个**空的组头**（点开什么都没有）——那比看不见更糟（用户会以为页面坏了）。⚠️ 这不是理论情形：本线把【开放平台】的**组级门去掉了**（可见性下移到条目级），于是"没有 `system.logs` / `system.pinduoduo_settings`/ `system.allow_taker_open_api` 里任何一个节点的普通账号"第一次真的会出现这种形状。⚠️ 判据落在**过滤之后的 `items`** 上（⛔ 不是"原始声明为空"）：空组是**按 viewer 算出来的**，同一个组对不同的人结论不同。⚠️ 只作用于 `groups`：`topLevel` / `bottom` 是**条目**，不存在"空组"这一档。 */
  const groups = computed(() =>
    MENU_GROUPS.filter(keepGroup)
      .map((g) => ({
        ...g,
        items: [...(leadingItems[g.name] ?? []), ...g.items]
          .filter(keepItem).map((i) => ({ ...i, disabled: !isClickable(i, hasRoute) })),
      }))
      .filter((g) => g.items.length > 0),
  )

  const topLevel = computed(() =>
    TOP_LEVEL_ITEMS.filter(keepItem).map((i) => ({ ...i, disabled: !isClickable(i, hasRoute) })),
  )

  // 侧栏最底部那一条（`config/menu.ts` 的 `BOTTOM_ITEMS`）：**同一个 `keepItem`**，见上面的注释。
  const bottom = computed(() =>
    BOTTOM_ITEMS.filter(keepItem).map((i) => ({ ...i, disabled: !isClickable(i, hasRoute) })),
  )

  return { groups, topLevel, bottom }
}
