// @vitest-environment jsdom
import { readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import router, { REPLICA_SKIP } from '../index'

/**
 * 路由表是 的接线产物，本文件只钉**接线契约**：path ↔ name ↔ param 名 ↔ 组件。视图模块桩掉（真组件会牵出 Element Plus，而**本文件**不需要它——挂载组件的行为测试各自在别处）。
 * 桩掉之后仍然能验证「哪个 path 落到哪个视图」：把路由的 `component` 当惰性导入函数调用、取 `default` 的身份即可；视图名互换（例如把岛页接到 `/orders/new`）会被这里抓到。
 */
vi.mock('@/views/NotFoundView.vue', () => ({ default: { __view: 'NotFoundView' } }))
// 订单三页 2026-09-20 起改为 1:1 复刻页（LayuiIslandHost + meta.islandPage）；
// 其中 `/orders/:order_no` 是 2026-09-22（订单详情线 T4）接上的——旧的 EP 详情页
// `views/orders/OrderDetailView.vue` 已删除（业务方裁定不留两套订单详情）。
vi.mock('@/components/LayuiIslandHost.vue', () => ({ default: { __view: 'LayuiIslandHost' } }))

/** 20 位纯数字，与后端 `lookup_value_regex = r"\d{20}"` 同形状。 */
const ORDER_NO = '12345678901234567890'

/** 取某个 path 命中的**最内层**路由记录的视图身份（惰性导入函数调用后的 default）。 */
async function viewOf(path: string): Promise<unknown> {
  // 不用 `matched.at(-1)`：本工程的 TS lib 目标没有 `Array.prototype.at`（实测 TS2550）。
  const matched = router.resolve(path).matched
  const record = matched[matched.length - 1]
  const component = record?.components?.default
  expect(typeof component, `${path} 的 component 应当是惰性导入函数`).toBe('function')
  const mod = await (component as () => Promise<{ default: unknown }>)()
  return mod.default
}

describe('订单页路由接线', () => {
  it('三条路由都在表里：orders / order-new / order-detail', () => {
    // 名字来源：`OrderNewView.vue:266` push `{ name: 'orders' }`、
    // `OrderListView.vue:95` push `{ name: 'order-detail', params: { order_no } }`。
    // `order-new` 此前无人引用，按 `order-detail` 的同构命名补上。
    expect(router.hasRoute('orders')).toBe(true)
    expect(router.hasRoute('order-new')).toBe(true)
    expect(router.hasRoute('order-detail')).toBe(true)
  })

  it('/orders → orders → island 宿主（key=order-orderlist）', async () => {
    // 原断言是 `OrderListView`；该页 2026-09-20 按业务方指派重做为 1:1 复刻（外观按对标、
    // 功能仍接我们的订单 API），所以这里升级成「宿主 + island key」——**意图不变**：
    // 仍然是钉「哪个 path 落到哪个组件」，而且钉得更死（多一个 key）。
    const resolved = router.resolve('/orders')
    expect(resolved.name).toBe('orders')
    expect(resolved.meta.islandPage).toBe('order-orderlist')
    expect(await viewOf('/orders')).toEqual({ __view: 'LayuiIslandHost' })
  })

  it('/orders/new → order-new → island 宿主（不能被 /orders/:order_no 抢走）', async () => {
    const resolved = router.resolve('/orders/new')
    expect(resolved.name).toBe('order-new')
    expect(resolved.params).toEqual({})
    expect(resolved.meta.islandPage).toBe('order-addorder')
    expect(await viewOf('/orders/new')).toEqual({ __view: 'LayuiIslandHost' })
  })

  it('/orders/<20 位数字> → order-detail → island 宿主（key=order-orderview），参数名是 order_no', async () => {
    const resolved = router.resolve(`/orders/${ORDER_NO}`)
    expect(resolved.name).toBe('order-detail')
    // 参数名**必须是 `order_no`**：订单列表等 island 页用 `onNavigate('order-detail', { order_no })` 发跳转请求（`order-orderlist.ts` / `order-myorderlist.ts` / `emp-myemp.ts` 都是这个名字）。
    // 宿主（`LayuiIslandHost.vue::islandSrc`）会把 `route.params` 逐个拼进 iframe query ⇒ island 页从 `window.location.search` 读到的就是 `order_no`（见 `order-orderview.spec.ts`）。
    expect(resolved.params).toEqual({ order_no: ORDER_NO })
    // 组件是 island 宿主 + `meta.islandPage`（不是旧 EP 的组件名判定）——
    // **判定更死**（多一个 key）：钉「哪个 path 落到哪个组件」。
    expect(resolved.meta.islandPage).toBe('order-orderview')
    expect(await viewOf(`/orders/${ORDER_NO}`)).toEqual({ __view: 'LayuiIslandHost' })
  })

  it('20 位数字才进详情：非 20 位的 `/orders/*` 落到 not-found（D-0036 的参数约束）', () => {
    // 出处：后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"` ⇒ 前端这条动态路由必须同样只吃 20 位数字。业务裁剪删掉 3 条 `/orders/*` 页后，**没有这个约束**它们会被 `:order_no` 吞掉、渲染「订单号格式不正确（应为 20 位数字）」的详情页而不是 404——一条既不存在的页面、又不报错的死链（`replicaRoutes.ts` 文件头记过这个动态冲突）。
    // ⚠️ `/orders/platform-core` **不是 404**（那一页已启用，路由由映射表自动生成，见下面的 describe）；剩下 2 条仍落 404。
    expect(router.resolve(`/orders/${ORDER_NO}`).name).toBe('order-detail')
    expect(router.resolve('/orders/client-form').name).toBe('not-found')
    expect(router.resolve('/orders/customer-chat-log').name).toBe('not-found')
    // 长度对但非数字、以及数字但长度不对，都不是订单号。
    expect(router.resolve('/orders/abcdefghijklmnopqrst').name).toBe('not-found')
    expect(router.resolve('/orders/1234567890123456789').name).toBe('not-found')
    // 静态段仍胜过参数段（这条约束不影响它）。
    expect(router.resolve('/orders/new').name).toBe('order-new')
  })

  it('/orders/platform-core → orders-platform-core → island 宿主（第三方平台订单管理，T7）', async () => {
    //（业务方要求把对标的「交易平台订单处理」做回来、菜单改名「第三方平台订单管理」）：`order-coreordercenter` 从 `REPLICA_SKIP` 摘掉后，路由由 `...replicaRoutes` 自动生成。
    // 「自动生成」只保证「有一条路由」，**不保证** `meta.islandPage` 指向对的 key（指错了宿主照常起另一页、不报错）⇒ 按 E-0024 用**字面值**钉住 path ↔ name ↔ key ↔ 标题。
    const resolved = router.resolve('/orders/platform-core')
    expect(resolved.name).toBe('orders-platform-core')
    expect(resolved.params).toEqual({})
    expect(resolved.meta.islandPage).toBe('order-coreordercenter')
    expect(resolved.meta.title).toBe('第三方平台订单管理')
    // 权限码 = 既有的 `order.list`（对标第 1236 号点已被目录裁剪删掉；理由与代价见
    // `config/routePermissions.ts` 的「已知近似」段）。菜单侧同一条（route-declarations 的 (c) 钉住）。
    expect(resolved.meta.permissions).toEqual(['order.list'])
    expect(await viewOf('/orders/platform-core')).toEqual({ __view: 'LayuiIslandHost' })
    // 「重新启用」的**反向**判据：它必须不再被跳过（否则上面那条 resolve 也不会是这个名字）。
    expect(REPLICA_SKIP.has('order-coreordercenter')).toBe(false)
  })

  it('列表页的 push 契约能落到正确 URL（反向钉住参数名）', () => {
    // 这条是 `OrderListView.vue:95` 的调用形状：name + params.order_no。
    // 若参数名改成 `orderNo`，vue-router 会因缺必填参数而 reject，这里也会立刻红。
    expect(router.resolve({ name: 'order-detail', params: { order_no: ORDER_NO } }).path).toBe(
      `/orders/${ORDER_NO}`,
    )
    expect(router.resolve({ name: 'orders' }).path).toBe('/orders')
    expect(router.resolve({ name: 'order-new' }).path).toBe('/orders/new')
  })

  it('原有两条路由没被挤掉：/ → home，未知路径 → not-found', async () => {
    expect(router.resolve('/').name).toBe('home')
    // 后台首页是 1:1 复刻 island 页（不是 EP 骨架页 `views/HomeView.vue`，那个已删）。页签文案仍是「后台首页」——`meta.title` 是页签文案的来源；缺它时 `AppShell.vue` 的 `resolveTitle()` 会回落到 `route.name`。
    // ⚠️ brief 原句写的是 `matched.at(-1)`，改成本文件 `viewOf()` 同款的取下标写法：`tsconfig.vitest.json` 的 `lib: []` 下没有 `Array.prototype.at`，`vue-tsc` 实测 TS2550。
    const homeMatched = router.resolve('/').matched
    const homeMeta = homeMatched[homeMatched.length - 1]?.meta
    expect(await viewOf('/')).toEqual({ __view: 'LayuiIslandHost' })
    expect(homeMeta?.islandPage).toBe('index-console')
    expect(homeMeta?.title).toBe('后台首页')
    // `/orders` 不是通配路由：未知路径仍落到 catch-all
    expect(router.resolve('/nope/nope').name).toBe('not-found')
    expect(await viewOf('/nope/nope')).toEqual({ __view: 'NotFoundView' })
  })
})

/**
 * 【第三方平台】的**自有功能**路由（对标物里没有这两页 ⇒ 不进 `REPLICA_ROUTES`，也没有 `REPLICA_SKIP` 条目）。写法与上面的订单三页同形：path ↔ name ↔ island key ↔ 组件。为什么必须**逐路由钉字面值**（而不只靠结构网）：独立评审实测过一条变异——把 `meta.islandPage` 改成另一个**存在**的 key 时全库用例**全绿（SURVIVED）** ⇒ 「路由指错页」是**静默**的：宿主照常加载 iframe、起的是**另一个**页面，没有异常、没有红；结构网只验「key 有页面文件」，抓不到「指向另一个**存在**的 key」⇒ 两条判据缺一不可。
 * ⛔ 其中第二条 `/thirdparty/jidanpu-sync` 已按业务方裁定**下架**——菜单条目与路由同一次删掉；「island 页文件 / 前端 api 模块保留、阶段二再删」的说法也已不成立（阶段二已删除 `thirdparty-jidanpu-sync.ts` 与 `api/jidanpuSync.ts`，连同它们的三条测试）⇒ 本节今天**只剩 1 条**（`/thirdparty/xgj-shops`），⛔ 别去找那两个文件。
 * ⚠️ 本节判据面**不受影响**；那条变异记录（指错成另一个存在的 key 是**静默**故障）**仍然成立**，由 `xgj-shops` 自己的字面值钉子 + 下面那条**反向判据**一起承载。
 */
describe('【第三方平台】自有功能路由接线', () => {
  it('/thirdparty/xgj-shops → thirdparty-xgj-shop → island 宿主（key=thirdparty-xgjshop）', async () => {
    const resolved = router.resolve('/thirdparty/xgj-shops')
    expect(resolved.name).toBe('thirdparty-xgj-shop')
    // 字面值，不引 `PAGE_KEY` 常量：引用生产常量会让断言在常量被改坏时**不红**（E-0024）。
    expect(resolved.meta.islandPage).toBe('thirdparty-xgjshop')
    expect(await viewOf('/thirdparty/xgj-shops')).toEqual({ __view: 'LayuiIslandHost' })
  })

  it('反向判据：/thirdparty/jidanpu-sync 已下架，直接输网址不再落到那个页面', async () => {
    // ⭐ 这是业务方「**连直接输网址也要挡住**」那条要求的判据本体（原正向用例已随路由一起删掉）。
    // ⚠️ 断言形状按**实测**写，⛔ 不是猜（AGENTS.md §5-E / E-0003）：删掉路由后实测 `router.resolve('/thirdparty/jidanpu-sync')` ⇒ `name === 'not-found'`（不是 `undefined`）、`params === { pathMatch: ['thirdparty', 'jidanpu-sync'] }`、`meta.title === '页面不存在'`、`matched` 末条的 `path === '/:pathMatch(.*)*'`（catch-all 在 `router/index.ts` 末尾）⇒ 它落 **catch-all**，不再是那个 island 页。
    expect(router.hasRoute('thirdparty-jidanpu-sync'), '路由 name 还在表里').toBe(false)
    const resolved = router.resolve('/thirdparty/jidanpu-sync')
    expect(resolved.name).toBe('not-found')
    // 上面注释声称过的 `params` 形状，这里**真的钉住**（评审建议：注释声称了却没断言 = 假保证）。
    // 形状是 2026-10-08 实测的原文，⛔ 不是猜：catch-all 的 `:pathMatch(.*)*` 吃到的是
    // **按 `/` 切开的数组**（不是那个 URL 原文串），所以这里用 `toEqual` 逐元素钉死。
    expect(resolved.params).toEqual({ pathMatch: ['thirdparty', 'jidanpu-sync'] })
    // ⛔ 关键：**没有** `islandPage` —— 有它就说明还落在某个 island 宿主上（页面换了个 key 也算没过）。
    expect(resolved.meta.islandPage).toBeUndefined()
    expect(await viewOf('/thirdparty/jidanpu-sync')).toEqual({ __view: 'NotFoundView' })
  })
})

/**
 * 【开放平台】的**自有功能**路由：`/system/performance` ↔ `system-perfanalysis` ↔ island key ↔ 宿主组件。与上面【第三方平台】那两条同形：逐条钉字面值（理由同上：`meta.islandPage` 指错成**另一个存在的 key** 是静默故障，结构网抓不到）。
 * ⚠️ 它还多钉一条**第二道门**：`meta.requiresAccountAdmin === true`——业务方要的是「只给超管看」（spec §1 第 2 条）。把它删掉、只留 `permissions`（复用的 `system.logs`）会让**持该节点的非账号管理员**也能进这一页（后端随后 403、页面报错），而**没有任何别的网会红**。
 */
describe('【开放平台】自有功能路由接线', () => {
  it('/system/performance → system-perfanalysis → island 宿主（key=system-perfanalysis）', async () => {
    const resolved = router.resolve('/system/performance')
    expect(resolved.name).toBe('system-perfanalysis')
    expect(resolved.params).toEqual({})
    expect(resolved.meta.islandPage).toBe('system-perfanalysis')
    expect(resolved.meta.title).toBe('性能分析')
    // 第二道门：账号管理员（判据值 = `/api/auth/me` 的 `is_account_admin`）。
    expect(resolved.meta.requiresAccountAdmin).toBe(true)
    expect(await viewOf('/system/performance')).toEqual({ __view: 'LayuiIslandHost' })
  })

  /**
   * 「应用管理」同样必须**逐条钉字面值**（不能只靠下面的结构网）：结构网只验「key 有页面文件」，抓不到「`meta.islandPage` 指向了**另一个存在**的 key」那种静默故障——与本文件【第三方平台】那一节的实测 SURVIVED 先例同一个理由（那条先例的路由与页已删除，⛔ 别去找那两个文件；本条判据面照旧生效）。
   */
  it('/open-platform/apps → open-apps → island 宿主（key=open-apps）', async () => {
    const resolved = router.resolve('/open-platform/apps')
    expect(resolved.name).toBe('open-apps')
    expect(resolved.params).toEqual({})
    expect(resolved.meta.islandPage).toBe('open-apps')
    expect(resolved.meta.title).toBe('应用管理')
    // 第二道门（与菜单整组的 `accountAdminOnly` 同源）：账号管理员。
    expect(resolved.meta.requiresAccountAdmin).toBe(true)
    // 第一道门：复用的既有节点（spec D9，⛔ 不新增 RBAC 节点）——用**字面值**钉，
    // 不引 `nodeCodesFor` / 生产常量（E-0024：引常量会让断言在常量被改坏时不红）。
    expect(resolved.meta.permissions).toEqual(['system.pinduoduo_settings'])
    expect(await viewOf('/open-platform/apps')).toEqual({ __view: 'LayuiIslandHost' })
  })
})

/**
 * 【第三方平台】的**自有功能**路由：`/thirdparty/wecom-group-qr` ↔ `wecom-group-qr` ↔ island key ↔ 宿主组件。与上面几条**同形**、必须**逐条钉字面值**（结构网抓不到「`meta.islandPage` 指向了**另一个存在**的 key」那种静默故障——宿主照常加载 iframe、起的是**另一个**页面，没有异常没有红；先例见本文件【第三方平台】那一节的注释）。
 * ⚠️ 它还多钉一条**第二道门**（spec D10）：`meta.requiresAccountAdmin === true`——本组**不能**加组级 `accountAdminOnly`（会连累同组的「闲管家店铺对接」），而 AppSecret 是明文可取的凭据 ⇒ 删掉它、只留 `permissions`（复用的 `system.pinduoduo_settings`）会让**持该节点的非账号管理员**也能进这一页（后端随后 403、页面报错），而**没有任何别的网会红**。
 */
describe('【第三方平台】自动上传企微群码到订单截图：路由接线', () => {
  it('/thirdparty/wecom-group-qr → wecom-group-qr → island 宿主（key=wecom-group-qr）', async () => {
    const resolved = router.resolve('/thirdparty/wecom-group-qr')
    expect(resolved.name).toBe('wecom-group-qr')
    expect(resolved.params).toEqual({})
    // 字面值，不引 `PAGE_KEY` 常量：引用生产常量会让断言在常量被改坏时**不红**（E-0024）。
    expect(resolved.meta.islandPage).toBe('wecom-group-qr')
    expect(resolved.meta.title).toBe('自动上传企微群码到订单截图')
    // 第一道门：复用的既有节点（设计文档 D9，⛔ 不新增 RBAC 节点）——同样用**字面值**钉。
    expect(resolved.meta.permissions).toEqual(['system.pinduoduo_settings'])
    // 第二道门（设计文档 D10）：账号管理员（判据值 = `/api/auth/me` 的 `is_account_admin`）。
    expect(resolved.meta.requiresAccountAdmin).toBe(true)
    expect(await viewOf('/thirdparty/wecom-group-qr')).toEqual({ __view: 'LayuiIslandHost' })
  })
})

/**
 * 【第三方平台】的**自有功能**路由：`/thirdparty/qqbot-settings` ↔ `qqbot-settings` ↔ island key ↔ 宿主组件。与上面几条**同形**、必须**逐条钉字面值**（结构网抓不到「指向**另一个存在**的 key」的静默故障，先例见本文件【第三方平台】那一节的注释）。
 * ⚠️ 它也带**第二道门**（与 wecom-group-qr 的 D10 同款）：AppSecret 是凭据 ⇒ 路由级 `requiresAccountAdmin: true`；删掉它、只留 `permissions`（自有节点 `system.qqbot_settings`）会让**持该节点的非账号管理员**也能进这一页，而**没有任何别的网会红**。
 */
describe('【第三方平台】QQ机器人设置：路由接线', () => {
  it('/thirdparty/qqbot-settings → qqbot-settings → island 宿主（key=qqbot-settings）', async () => {
    const resolved = router.resolve('/thirdparty/qqbot-settings')
    expect(resolved.name).toBe('qqbot-settings')
    expect(resolved.params).toEqual({})
    // 字面值，不引 `PAGE_KEY` 常量：引用生产常量会让断言在常量被改坏时**不红**（E-0024）。
    expect(resolved.meta.islandPage).toBe('qqbot-settings')
    expect(resolved.meta.title).toBe('QQ机器人设置')
    // 第一道门：**自有**节点（能单独授权）——用**字面值**钉。
    expect(resolved.meta.permissions).toEqual(['system.qqbot_settings'])
    // 第二道门（与 wecom-group-qr 的 D10 同款）：账号管理员。
    expect(resolved.meta.requiresAccountAdmin).toBe(true)
    expect(await viewOf('/thirdparty/qqbot-settings')).toEqual({ __view: 'LayuiIslandHost' })
  })
})

/**
 * 【财务相关】经营报表：`/agents/day-report` ↔ `agents-day-report` ↔ island key ↔ 宿主组件。这条路由**不是手写条目**：由 `...replicaRoutes` 的映射表自动生成（`emp-dayoperation` 同一次从 `REPLICA_SKIP` 摘掉）。
 * 「自动生成」只保证「有一条路由」，**不保证**「`meta.islandPage` 指向的是对的那一页」——指错成另一个**存在**的 key 时宿主照常加载 iframe、起的是**另一个**页面，没有异常、没有红（先例见本文件【第三方平台】那一节）⇒ 按 E-0024 用**字面值**把它钉死。
 */
describe('【财务相关】经营报表：路由接线', () => {
  it('/agents/day-report → agents-day-report → island 宿主（key=emp-dayoperation）', async () => {
    const resolved = router.resolve('/agents/day-report')
    expect(resolved.name).toBe('agents-day-report')
    expect(resolved.params).toEqual({})
    expect(resolved.meta.islandPage).toBe('emp-dayoperation')
    expect(resolved.meta.title).toBe('经营报表')
    // 第一道门：与「客服接发单统计」**同一个** RBAC 节点（spec §0 第 3 条：⛔ 不新增权限点）。
    expect(resolved.meta.permissions).toEqual(['finance.support_statistics'])
    expect(await viewOf('/agents/day-report')).toEqual({ __view: 'LayuiIslandHost' })
  })
})

// 以下来自 feat/frontend-shell-replica（壳复刻 ）：页签条的标题直接来自 `meta.title`。
// 两边是**同名不同用途**的测试（main 钉订单页接线、本块钉两个原有路由的标题），合并时都保留。
describe('路由标题', () => {
  it('首页与 404 都带 meta.title', () => {
    const byName = (n: string) => router.getRoutes().find((r) => r.name === n)
    expect(byName('home')?.meta.title).toBe('后台首页')
    expect(byName('not-found')?.meta.title).toBe('页面不存在')
  })
})

// 「对标有、我们不做」的**唯一**登记点是 `router/index.ts` 的 `REPLICA_SKIP`（AGENTS.md §5）。为什么必须直接断言集合内容：这 5 条 jdpshop 同时在 `replicaRoutes.ts` 里标了 `accessDenied`，过滤器先按 `accessDenied` 排除它们 ⇒ **漏登记不会改变生成的路由数**、也没有任何既有断言会因此变红（评审实测：从集合里删掉一条，加本条断言之前的 1117 条测试全绿——「1117」是那次实测当时的基线，不是当前总数）。
// 所以这里把「登记」本身钉住，并且按 E-0024 用**字面值**列这 5 个 key（不引用生产常量）。
const JIDPSHOP_SKIP_KEYS = [
  'order-jdpshoporderlist',
  'jdpshop-shopindex',
  'jdpshop-shoptype',
  'jdpshop-addshopitem',
  'jdpshop-shopmange',
]

describe('商城（jdpshop）不做：REPLICA_SKIP 登记', () => {
  it('5 个 jdpshop key 都在 REPLICA_SKIP 里', () => {
    const missing = JIDPSHOP_SKIP_KEYS.filter((k) => !REPLICA_SKIP.has(k))
    expect(missing, `REPLICA_SKIP 里缺少：${missing.join(', ')}`).toEqual([])
  })
})

/**
 * **结构网**：`meta.islandPage` ↔ `src/layui-island/pages/<key>.ts` 的对拍。上面那些逐路由钉子只覆盖**手写**的少数几条；这张网覆盖**全部**带 `meta.islandPage` 的路由。注释里的计数（63 / 62 / 69 等）都是**写下时**的读数、只是描述**不是判据**（本网断的是「每条都有页面文件」，⛔ 别把它们当权威读数用）。
 * 它守的是另一种静默故障：key 指向一个**不存在**的页面文件时，宿主 `LayuiIslandHost.vue::islandSrc` 会照常渲染 iframe、加载 `/layui-island/page.html?page=<key>`，而 `page.ts` 的注册表里没有这个 key ⇒ iframe 里是**空白页**，没有异常、没有红（「指向另一个**存在**的 key」这张网抓不到，那一类靠上面的逐路由字面钉子）。**实测**枚举 `router.getRoutes()` 与 `pages/*.ts` 对拍 `missing = 0` ⇒ 无需收窄、按全量写；将来 `replicaRoutes` 新增一条没建页面的条目时这张网会红——那正是它该做的（先建页面、或登记进 `REPLICA_SKIP`）。
 * ⚠️ 反方向**不管**：`pages/` 下有 3 个页面文件没有对应路由（`system-roleauthority-edit` / `system-roleauthority-view` / `system-setgames`——它们是**弹层页**，由别的页面 `layer.open` 打开、本来就没有路由）。那条不变量在 `config/__tests__/replicaRoutes.spec.ts` 里按 `LAYER_ONLY_PAGE_KEYS` 显式豁免着管，不在这里重复（AGENTS.md §4「不重复同一事实」）。
 */
describe('islandPage ↔ 页面文件结构网（全量）', () => {
  /** `src/layui-island/pages/*.ts` 的文件名（去扩展名）= 合法 island key 的**唯一真实出处**。 */
  const PAGES_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../layui-island/pages')
  const pageKeys = new Set(
    readdirSync(PAGES_DIR, { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith('.ts'))
      .map((e) => e.name.replace(/\.ts$/, '')),
  )
  /** 路由表里带 `meta.islandPage` 的记录（`getRoutes()` 已把 `...replicaRoutes` 展开）。 */
  const routesWithIslandPage = router.getRoutes().filter((r) => r.meta.islandPage !== undefined)

  it('阳性对照：枚举面非空，且含本任务的自有功能 key（防「空集恒真」）', () => {
    // AGENTS.md §5-G：结构网自己的断言也必须能失败。若 `PAGES_DIR` 解析错、或 `pages/` 空了，「每条路由都有页面文件」会以一种**看起来像业务错**的方式全红；这一条把「枚举面本身坏了」单独钉出来。key 按 E-0024 用**字面值**（不引页面模块导出的 `PAGE_KEY` 常量）。
    // ⛔ 改准：这里钉的是**剩下这一个**自有功能 key `thirdparty-xgjshop`（另一条 `thirdparty-jidanpu-sync` 的路由已按业务方裁定下架，它的 island 页文件也已删除 ⇒ `pageKeys` 里没有那个串——这不是异常，本条判据面一字未动）。
    expect(pageKeys.size, `layui-island/pages 下没有任何 .ts 页面文件（${PAGES_DIR}）`).toBeGreaterThan(0)
    expect([...pageKeys]).toEqual(expect.arrayContaining(['thirdparty-xgjshop']))
    // 路由面同样要非空：路由表若被清空，下面那条的 `missing` 恒为空、判据恒真。
    expect(routesWithIslandPage.length, '路由表里没有任何带 meta.islandPage 的路由').toBeGreaterThan(0)
  })

  it('每条带 meta.islandPage 的路由，其 key 都有页面文件（不存在 ⇒ 红）', () => {
    const missing = routesWithIslandPage
      .filter((r) => !pageKeys.has(String(r.meta.islandPage)))
      .map((r) => `${String(r.name)} (${r.path}) → ${String(r.meta.islandPage)}`)
    expect(missing, `以下路由的 meta.islandPage 没有对应页面文件：\n${missing.join('\n')}`).toEqual([])
  })
})
