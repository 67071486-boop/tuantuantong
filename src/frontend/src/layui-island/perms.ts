/**
 * 岛页（iframe）里的**权限节点判据**共享模块。
 *
 * 为什么单独抽一份：`system-roleauthority.ts` 原先自带一份 `viewerCanNode`，本轮的首页
 * 「快捷操作」权限门（`pages/index-console.ts`）也要同一判据——两份复制会各漂各的
 * （E-0011 的「同一事实两套判据」形状）。本模块把实现收成一份，两处都从这里取。
 *
 * 语义与出处：
 * - 壳层（Vue SPA）侧对应实现是 `src/stores/session.ts::canNode`（管理员短路 ∨ 节点 ∈
 *   `menu_permissions`）——岛页跑在 iframe 里，**不引 Pinia**，所以判据要能独立成纯函数。
 * - ⚠️ 管理员两条都要短路：`menu_permissions` 对 superuser **没有短路**，而部署空窗期
 *   （`seed_rbac_catalog` 与 `migrate_rbac_roles --apply` 之间）租户管理员的它**确实是 `[]`**。
 */
export type NodeViewerInput = {
  is_tenant_admin?: boolean
  is_account_admin?: boolean
  menu_permissions?: string[]
}

/** 与 `stores/session.canNode` **同口径**的纯函数（iframe 页不引 Pinia，判据要能单测）。 */
export function canNode(me: NodeViewerInput, code: string): boolean {
  if (me.is_tenant_admin === true || me.is_account_admin === true) return true
  return (me.menu_permissions ?? []).includes(code)
}

/* ── ⭐ 2026-10-28：「交易平台写动作」权限节点（`order.platform_write`）──────────────
 *
 * 业务方口径：**平台写按钮**（撤回 / 改价 / 补时 / 补款 / 加价 / 协商撤销 / 同意撤销 /
 * 取消撤销 / 申请仲裁 / 锁定 / 解锁 / 确认完单 / 编辑平台单 / 重发 / 截图上传 / 拉黑）
 * 的渲染要挂这一个节点——**没权限就不画**（fail-closed，与 D-0154/`index-console` 同一原则：
 * 点了会 403 的入口就不该出现）。覆盖三处：第三方平台订单管理页（`order-coreordercenter`）、
 * 订单详情页的平台页签（`order-orderview`）、管理订单页的行内平台入口（`order-orderlist`）。
 *
 * ## ⚠️ 降级：后端**还没有**这个节点（业务方 2026-10-28 明确要求先按旧行为跑）
 *
 * 后端另一条线才加这个节点（今天 `grep -r platform_write src/apps` 为空，
 * `apps/rbac/catalog.py` 的节点目录里也没有）。前端**拿不到「目录里有没有某个 code」**：
 * `/api/auth/me` 的 `menu_permissions` 只回**本账号持有的**节点；目录全表那条
 * `GET /api/permission-nodes/` 要 `system.role_authority`（客服没有这个点，会 403）
 * ⇒ 前端**无法自动分辨**「后端还没上线这个节点」与「这个账号没被授权」两件事。
 * ⇒ 用一颗**显式开关** `PLATFORM_WRITE_NODE_SHIPPED` 做降级（唯一出处，⛔ 页面不各写一份）：
 *
 * | 开关 | 判据 |
 * |---|---|
 * | `false`（**已过期**，仅页测试用于驱动降级分支） | **退回旧行为**：按 `order.list`（这批写端点今天在后台的门就是它）。身份取不到（`fetchMe` 失败）⇒ 与升级前一致，照画 |
 * | `true`（后端上线节点的那一次同批改 true） | 严格按 `order.platform_write`；**身份取不到也不画**（fail-closed） |
 *
 * 上线动作（后端那次）：把开关改成 `true` + 给「客服」角色默认勾上该节点 +
 * `migrate_rbac_roles --apply`（口径与其它节点一致，见 `docs/RUNBOOK.md`）。
 */
export const PLATFORM_WRITE_NODE = 'order.platform_write'

/**
 * 后端是否**已经上线** `order.platform_write`（唯一开关的初值；见上面那张表）。
 *
 * ⚠️ 这是一个**可变**的模块状态（`let` + 下面两个出口），理由只有一条：页测试要能分别
 * 驱动「降级档」与「上线后 fail-closed 档」两个分支。业务侧唯一的上线动作 = 把这里的
 * 初值改成 `true`（后端上线节点的那一次同批）。
 */
let platformWriteNodeShipped = true  // 后端节点已上线（含存量角色自动补授）

/** 后端本次是否已上线该节点（读）。 */
export function isPlatformWriteNodeShipped(): boolean {
  return platformWriteNodeShipped
}

/**
 * 打开 / 关闭「后端已上线该节点」这一档（**唯一开关**）。
 *
 * ⚠️ 业务代码里 ⛔ 不要调它（上线是**改初值**的同一次提交，不是运行期拨开关）；
 * 页测试用它驱动两个分支。
 */
export function setPlatformWriteNodeShipped(value: boolean): void {
  platformWriteNodeShipped = value
}

/**
 * 「交易平台写动作」能不能画（纯函数）。
 *
 * `viewer === undefined` = 身份没取到（`/api/auth/me` 失败 / 401 / 网络）：
 * 降级档照画（与升级前同一行为，后端仍会 403 兜底）；上线后 fail-closed 不画。
 */
export function canPlatformWrite(viewer: NodeViewerInput | undefined): boolean {
  if (!platformWriteNodeShipped) {
    // 降级：退回旧行为——按 `order.list` 判；身份取不到 ⇒ 与升级前一致（照画）。
    return viewer === undefined || canNode(viewer, 'order.list')
  }
  return viewer !== undefined && canNode(viewer, PLATFORM_WRITE_NODE)
}

/**
 * 本页当前是否允许画平台写按钮（**渲染期读它**）。
 *
 * 默认 `true` = 升级前的行为；`applyPlatformWritePermission(me)` 在页面挂载期用
 * `/api/auth/me` 的载荷覆盖一次（`canPlatformWrite` 的产物）。页面在**首次渲染之前**调用它。
 */
let platformWriteAllowed = true

/** 用 `/api/auth/me` 的载荷判定并缓存本页的「平台写按钮可画」开关（返回判定结果）。 */
export function applyPlatformWritePermission(
  viewer: NodeViewerInput | undefined,
): boolean {
  platformWriteAllowed = canPlatformWrite(viewer)
  return platformWriteAllowed
}

/** 渲染期读的开关（见 `applyPlatformWritePermission`）。 */
export function platformWriteAllowedNow(): boolean {
  return platformWriteAllowed
}

/**
 * 复位成 **`true`（升级前行为）**。
 *
 * ⚠️ 只给页测试用（模块级状态跨用例共享，同 `setPushJdpAvailable` 的先例）：
 * 业务代码里 ⛔ 不要调它——业务侧的写入点只有 `applyPlatformWritePermission`。
 */
export function resetPlatformWriteAllowedForTests(): void {
  platformWriteAllowed = true
}

/* ── ⭐ 2026-10-29（fix/taker-web-order-detail）：订单详情页的「全量 / 受限」模式判据 ──────
 *
 * 业务方报障：打手（代练员）在「指派给我的订单」/「我的收入」页点单号蓝链，开出来的
 * 订单详情**整页空白**。根因：蓝链开的是 `order-orderview` 岛页（iframe，**不经过**
 * `/orders/:order_no` 的路由守卫），该页取数打**全量**端点 `GET /api/business/orders/<单号>/`，
 * 门 = `order.view_detail` ∨ `order.view_content`（后端 `ORDER_PERMISSIONS["retrieve"]`）
 * ——「代练员」默认角色两颗都没有 ⇒ 403 ⇒ 页面只剩证据件的静态空骨架。
 *
 * 修法（D-0222 桌面侧补全）：岛页挂载期自判——持任一节点 ⇒ 全量模式（原行为一字不动）；
 * 都没有 ⇒ **受限模式**：改调打手受限端点 `GET /api/business/my-orders/<单号>/detail/`
 * （`api/myOrders.ts::fetchMyOrderDetail`，手机端 H5 详情页同一条），渲染脱敏字段集。
 * ⛔ **不给代练员补 `order.view_detail`/`order.view_content`**：全量 `OrderSerializer`
 * 含客户联系方式（R1 裁定不给）。
 */

/** 「全量订单详情」的节点门（any 元组，与后端 `ORDER_PERMISSIONS["retrieve"]` 同一颗）。 */
export const ORDER_FULL_DETAIL_NODES: readonly string[] = [
  'order.view_detail',
  'order.view_content',
]

/** 本账号能不能开**全量**订单详情（纯函数；与 `canPlatformWrite` 同一条 `canNode` 通道）。 */
export function canViewFullOrderDetail(viewer: NodeViewerInput): boolean {
  return ORDER_FULL_DETAIL_NODES.some((code) => canNode(viewer, code))
}

/**
 * 挂载期判「本账号能不能开全量订单详情」：探一次 `/api/auth/me`（**裸 `fetch`**，
 * 与 `resolvePlatformWritePermission` 同一条通道——理由同它：页测试把 `request()` 当
 * 响应队列，挂载期多发一发会抢走排好的响应 / 挂死在永不 resolve 的桩上）。
 *
 * 返回值三态：
 *  - `true` = 持 `order.view_detail` 或 `order.view_content` ⇒ **全量模式**（原行为）；
 *  - `false` = 两颗都没有 ⇒ **受限模式**（改调 `my-orders` 受限端点）；
 *  - `undefined` = 身份探不到（网络 / 401 / 形状不合）⇒ 调用方按**全量模式**降级
 *    ——与判据存在之前的行为一致（后端 403 + 页面弹 msg 的兜底仍在），不把
 *    「探针失败」放大成「把有权限的人也踢进受限模式」。
 */
export async function resolveFullDetailPermission(): Promise<boolean | undefined> {
  const viewer = await probeViewer()
  if (viewer === undefined) return undefined
  return canViewFullOrderDetail(viewer)
}

/** 探针超时（毫秒）。与 `push-availability` 同一量级：拿不到就按 fail-closed 处理，不拖挂载。 */
export const PLATFORM_WRITE_PROBE_TIMEOUT_MS = 3000

/**
 * 挂载期判定本页能不能画平台写按钮（**页面只调这一个**；首次渲染之前 await 它）。
 *
 * 两条路径：
 * - **降级档**（`platformWriteNodeShipped === false`，今天）：**不发请求**、直接置 `true`
 *   ——与升级前的行为逐字一致（多打一发 `/auth/me` 会让本来不需要它的两个页各多一次往返）。
 * - **上线后**（`true`）：探一次 `/api/auth/me`（**裸 `fetch`**，理由见下），按
 *   `order.platform_write` 判；探针失败 / 超时 / 形状不合 ⇒ **fail-closed**（不画）。
 *
 * ## 为什么探针用裸 `fetch` 而不是 `request()`
 *
 * 与订单列表页 `fetchPushAvailability` 同一个理由（那里记过完整现场）：两页的页测试把
 * `request()` 当**响应队列**——很多用例在挂载前 `mockResolvedValueOnce` 排好给真实动作的响应，
 * 挂载期多发一发会**抢走**那一响应；更狠的是「在途闸 / 超时」类用例把 `request()` 桩成
 * **永不 resolve** 的 promise（故意测卡死）⇒ 任何 await `request()` 的挂载期代码都会挂死。
 * 这个探针只需要一个布尔，不值得改变全页测试的契约。
 *
 * ## 失败面
 *
 * `200 + code === 0 + data.user` ⇒ 用 `data.user` 判；其余（401 / 403 / 网络 / 超时 / 非信封 /
 * 形状不合）⇒ `undefined` ⇒ `canPlatformWrite` 按上线档 fail-closed（不画）。
 * ⛔ 不弹提示：这是渲染门，不是用户动作（缺入口是 fail-closed 的正确表现，排障看控制台）。
 */
export async function resolvePlatformWritePermission(): Promise<boolean> {
  if (!platformWriteNodeShipped) {
    platformWriteAllowed = true
    return true
  }
  const viewer = await probeViewer()
  if (viewer === undefined) {
    console.info(
      '[perms] 平台写按钮未渲染：/api/auth/me 探针失败或形状不合（fail-closed）',
    )
  }
  return applyPlatformWritePermission(viewer)
}

/** Resolve the detail-page permissions from one identity read. */
export async function resolveOrderViewPermissions(): Promise<{
  fullDetail: boolean | undefined
  changeTakerPrice: boolean
  addEditOrder: boolean
}> {
  const viewer = await probeViewer()
  return {
    fullDetail: viewer === undefined ? undefined : canViewFullOrderDetail(viewer),
    changeTakerPrice: viewer !== undefined && (
      canNode(viewer, 'order.submit_payout_price') || canNode(viewer, 'order.edit_payout_price')
    ),
    addEditOrder: viewer !== undefined && canNode(viewer, 'order.add_edit'),
  }
}

/** 裸 `fetch` 探 `/api/auth/me` 的 `data.user`；任何异常 ⇒ `undefined`（调用方 fail-closed）。 */
async function probeViewer(): Promise<NodeViewerInput | undefined> {
  const base = import.meta.env.VITE_API_BASE ?? ''
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PLATFORM_WRITE_PROBE_TIMEOUT_MS)
  try {
    const res = await fetch(`${base}/api/auth/me`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
    if (!res.ok) return undefined
    const body = (await res.json()) as {
      code?: number
      data?: { user?: NodeViewerInput }
    }
    if (body?.code !== 0) return undefined
    const user = body.data?.user
    return user !== null && typeof user === 'object' ? user : undefined
  } catch {
    return undefined
  } finally {
    clearTimeout(timer)
  }
}
