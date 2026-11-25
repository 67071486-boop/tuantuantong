import { createRouter, createWebHistory } from 'vue-router'
import { REPLICA_ROUTES } from '@/config/replicaRoutes'
import { nodeCodesFor } from '@/config/routePermissions'
import { useSessionStore } from '@/stores/session'
import { orderChatPreviewEnabled } from '@/v2/internalChat/orderChatPreview'
import { V2_ROUTE_OVERRIDES } from '@/v2/v2Menu'
import { installGuards } from './guards'

// 只有旧版 Playwright 审计服务器会注入此常量。
declare const __LEGACY_ROUTE_AUDIT__: boolean | undefined

/**
 * **不生成 island 路由**的对标页。分类与出处：① 已由 EP 实现承担、后业务方指派重做为 1:1 复刻的 6 条——path/name 沿用既有 URL（不用映射表的别名路由），仍要跳过（否则映射表会在别名路径上再生成重复路由）；② `index-console` 已由 `/` 承担（同一菜单位置不留两条路由）；③ `system-user-list` 与 4 条 `/system-users/*` 已在下面**显式声明**（path/name 是既有 URL，换掉会动菜单 `routeName` 与既有测试，只把组件换成 island 宿主）；④ `home-blacklistplan`（避坑板）：对标页是 **Vue + Element** 实现（读码：引了 `vue.min.js` 与 `elementindex.js`），且是对标顶层导航项、**不在冻结 IA 里**（`docs/FRONTEND-LAYOUT.md` §4）→ 等业务方指派，留着它会指向不存在的页面模块；⑤ 各业务方裁定不做的整组（D-0036 16 条 / 商城 5 条 / 短信整块 D-0038·D-0086 / 4 个系统设置页 / D-0339 异常消失订单）见下面各段清单——菜单入口、island 页、证据件、页面测试与路由同一次删除，URL 直达落 `not-found`。
 * ⚠️ 旧 `/orders/*` 跳过项里 `/orders/client-form`、`/orders/customer-chat-log` 之所以真能落 404，靠的是 `/orders/:order_no(\d{20})` 的**参数约束**（没有它，动态段会把任何单段路径吞成订单详情）；`/orders/platform-core` 已从本集合摘掉——映射表会自动生成它。条数以本集合字面量为准。
 * ⚠️ `export` 出来是给 `router/__tests__/routes.spec.ts` 直接断言登记内容用：那 5 条 jdpshop 同时也 `accessDenied`，漏登记**不会**改变生成的路由数（`accessDenied` 先过滤掉了）⇒ 没有别的东西能抓到漏登记（评审实测：加断言之前删掉一条，1117 条测试全绿）。
 */
export const REPLICA_SKIP = new Set([

  'index-console',
  'home-blacklistplan',
  'system-user-list',
  'user-userloginrestrict',
  'user-backuserlist',
  'user-rnamecertlogandsetting',
  'user-usercontractsigning',
  // 下面 6 条：对标页**已由 EP 实现承担**、业务方指派重做为 1:1 复刻——现在由显式条目渲染 island（path/name 是既有 URL，不改用映射表的别名），
  // 所以这里**仍要跳过**：否则映射表会在 `/orders/create`、`/game-config/game-servers` 这类别名路径上再生成一套重复路由。
  'order-addorder',
  'order-orderlist',
  'game-gameserver',
  'game-ordertypssetting',
  'game-projectsetting',
  'user-adduser',
  // ── 业务方裁定不做（D-0036）─────────────────────────────
  // 菜单入口、island 页（`src/layui-island/pages/<key>.ts`）、证据件（`public/replica/<key>.html`）与路由都已删除 ⇒ 这些 URL 直达落 `not-found`。
  // ⚠️ 不要因为「`replicaRoutes.ts` 里还有这一条」就把它们放回来：那张表是对标 89 条菜单的**逐条转录**（`replicaRoutes.spec.ts` 断言恰好 89 条、且只增不减），「对标有、我们不做」登记的**唯一**位置就是本集合。经营报表和第三方平台订单管理已从本集合摘掉，映射表会生成它们；条数以字面量为准。
  'order-clientordergenerate',
  'order-reccustomerchatlog',
  'user-applyonboardingmange',
  'user-checkinlog',
  'user-checkinstatic',
  'emp-booklist',
  'emp-learminformationlist',
  'emp-privatephonecalllog',
  'emp-qacenter',
  'game-gameitemlist',
  'game-pricecalculator',
  'system-customerphoneprivatesetting',
  'system-wechatappmange',
  'system-onlineconsultationset',
  // ── 业务方裁定「商城（店铺管理）」不做：5 条 ──────────────────
  // ① 业务方裁定不做（不再按「置灰等以后」处理），菜单入口已从 `config/menu.ts` 删除；② 对标这 5 条页本身也进不去：4 个 `/jdpshop/*` 与 `/Order/JdpShopOrderList` 实测 302 → `/ErrorPage/nopermission`（`replicaRoutes.ts` 里 `accessDenied: true`，由 `replicaRoutes.spec.ts` 的集合断言钉住），没有可复刻的页面。
  // 这 5 条**同时也是** `accessDenied` ⇒ 它们本来就不生成路由（`/apps/*`、`/orders/platform-shop` 直达落 `not-found`）；登记进本集合是把「业务方不做」这个决定记在 AGENTS.md §5 指定的**唯一**登记点上（与上一组同款先例）。
  'order-jdpshoporderlist',
  'jdpshop-shopindex',
  'jdpshop-shoptype',
  'jdpshop-addshopitem',
  'jdpshop-shopmange',
  // ── 短信业务整块不做（D-0038 整栏剔除；业务方原话「我们不需要短信功能」，D-0086）──
  // 此前 4 个页面靠**借用** `system.sms_settings` 一个点活着；下线的是**页面这一半**——`system.sms_settings` 与 `order.send_sms` 两个节点连同接线位点已在目录裁剪任务里删除（本组 4 个对标 key 只作 `REPLICA_SKIP` 留痕，不再有节点可判）。
  'sms-smslogslist',
  'sms-smstemplatelist',
  'sms-smsbulkhair',
  'system-smssetting',
  // ── 业务方裁定下线 4 个系统设置页（原话：录单修正设置、订单标签维护、微信公众号页面、淘宝店对接设置都删掉不需要了）──
  // 这 4 条**后端从来没有模型与端点**（`apps/rbac/wiring.py` 的 PENDING_REASON 逐条写着「前端 1:1 渲染页，零 api 调用」）⇒ 下线的是前端这一半：菜单条目、island 页、证据件、页面测试同一次删掉，4 个 RBAC 权限点一并删除。
  // ⚠️ `config/replicaRoutes.ts` 里这 4 条**保留**（那张表是对标 89 条菜单的逐条转录，只增不减）。邻接物是淘宝发货记录和改标签动作 `order.update_tags`（catalog 里 bid 1258），不是这两个设置页。
  'system-verifiaddorderkey',
  'system-orderlabellist',
  'system-wechartsetting',
  'system-taobaoshipsetting',
  // ── 业务方裁定删除「异常消失订单」（D-0339）：1 条 ────────────────────
  // 业务方拍板：菜单和功能整个去掉（不需要了）——**不是隐藏，是删除**。菜单条目（config/menu.ts）、island 页（`order-exceptionorderlog.ts`）、证据件、页测试与权限映射都在同一次删除；本集合登记后映射表不再生成路由 ⇒ `/orders/exception-log` 直达落 not-found。
  // ⚠️ `config/replicaRoutes.ts` 里这一条**保留**（对标 89 条菜单的**逐条转录**，只增不减）；「对标有、我们不做」的**唯一**登记点就是本集合。⚠️ 本段注释⛔ 不用单引号包任何词：`e2e/fixtures/routes.ts` 用正则抽本块里**所有**单引号串当 skip key（多抽的 key 不会命中任何路由，但 REPLICA_SKIP.size 的钉数会被撑大）。
  'order-exceptionorderlog',
  // ── 业务方裁定删除客户维护两项（2026-11-10 第二批）：2 条 ──────────────────
  // 业务方拍板：会员余额明细与会员等级设置整个去掉（暂时不需要）——**不是隐藏，是删除**。
  // 菜单条目（config/menu.ts）、island 页（`customer-hyfundetailslist.ts` / `customer-hylevelsetting.ts`）、
  // 证据件、页测试与权限映射都在同一次删除；本集合登记后映射表不再生成路由 ⇒
  // `/customers/balance-log` 与 `/customers/levels` 直达落 not-found。
  // ⚠️ `config/replicaRoutes.ts` 里这两条**保留**（对标 89 条菜单的**逐条转录**，只增不减）；
  // 对标有、我们不做的**唯一**登记点就是本集合。⚠️ 本段注释⛔ 不用单引号包任何词：
  // `e2e/fixtures/routes.ts` 用正则抽本块里**所有**单引号串当 skip key（多抽的 key 不会命中
  // 任何路由，但 REPLICA_SKIP.size 的钉数会被撑大）。
  'customer-hyfundetailslist',
  'customer-hylevelsetting',
])

/**
 * 生成的复刻路由条数 = `REPLICA_ROUTES` 条数 − 5 条 accessDenied − 只被本集合排除的条数（`REPLICA_SKIP` 里另有 5 条店铺页与 accessDenied 重叠，不重复扣）。别按加一减一推算，用 `REPLICA_ROUTES` 和本集合的字面量数。
 * 组件一律是 `LayuiIslandHost`：按 `meta.islandPage` 在 iframe 里加载 `/layui-island/page.html?page=<key>`（页面模块见 `src/layui-island/pages/<key>.ts`）。`meta.title` 取**对标菜单中文**（`benchmarkMenuLabel`）而不是对标 `<title>`：页签条是我们自己的壳（`docs/FRONTEND-LAYOUT.md` §4 的冻结 IA），要对齐菜单；不一致的 53 条已记在 `replicaRoutes.ts` 的 `note` 备查。
 * 每条都挂 `meta.permissions`（**RBAC 节点 code**，取值表见 `config/routePermissions.ts`；D-0020 已结案，白名单只剩 `login` / `home` / `not-found`）。被对标按权限拒绝的 5 条（`accessDenied`）不生成路由，只做占位页。
 */
const replicaRoutes = REPLICA_ROUTES.filter(
  (r) => !r.accessDenied && !REPLICA_SKIP.has(r.key),
).map((r) => ({
  path: r.path,
  name: r.routeName,
  component: () => import('@/components/LayuiIslandHost.vue'),
  meta: {
    title: r.benchmarkMenuLabel ?? r.title,
    islandPage: r.key,
    // D-0020 结案（spec §5.4）：判据是**RBAC 节点 code**，取值表在 `config/routePermissions.ts`。
    // 白名单外的路由**必须**非空——`router/__tests__/route-declarations.spec.ts` 的承重判据钉住这一点。
    permissions: nodeCodesFor(r.routeName),
  },
}))

const router = createRouter({
  // 用 Vite 注入的 base（= 构建时的 `base` 配置，见 vite.config.ts 的 `base`）。
  // 写死 `createWebHistory()` 会让产物只能挂在域名根路径：dist 里的资源已经是 `/assets/…`，
  // 而路由若不带同一前缀，挂到子路径（如 `/console/`）后刷新就 404——D9/§11 的「中性产物」也就成了空话。
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      // 1:1 复刻页（业务方 2026-09-23 指派）：外观按对标 `/Index/console`，功能接我们自己的 API。
      // 原 `views/HomeView.vue`（EP 写的「连通性自检」骨架页）随之删除——同一个菜单位置不留两条实现。
      // 页签文案「后台首页」保持不变（`meta.title`，`AppShell.vue` 读它）。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { title: '后台首页', islandPage: 'index-console' },
    },
    // 订单三页的接线。三个 name 的出处：`orders` = `OrderNewView.vue` 的「去列表」push；`order-detail` = `OrderListView.vue` 的「详情」push；`order-new` 按 `order-detail` 的同构命名取、菜单的「录入订单」指向它。
    // 菜单条目的可点判定走 `useMenu.ts` 的 `isClickable`（`hasRoute` 就是 `router.hasRoute`）：name 不在这张表里，对应条目就保持置灰 + 不可点。
    {
      path: '/orders',
      name: 'orders',
      // 1:1 复刻页（业务方指派重做）：外观按对标，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { title: '管理订单', islandPage: 'order-orderlist', permissions: nodeCodesFor('orders') },
    },
    {
      // 静态段写在 `:order_no` 之前：`/orders/new` 与 `/orders/:order_no` 都能匹配它。
      // vue-router 的匹配器按 score 排序（静态段分数高于参数段），顺序只影响可读性。
      path: '/orders/new',
      name: 'order-new',
      // 1:1 复刻页（业务方指派重做）：外观按对标，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { title: '录入订单', islandPage: 'order-addorder', permissions: nodeCodesFor('order-new') },
    },
    {
      // 参数名是 `order_no`，不是 camelCase 的 `orderNo`：订单列表 / 我的订单等 island 页都用 `onNavigate('order-detail', { order_no })` 发跳转请求，参数名由那些调用方锁定。
      // 组件 = **island 宿主**（旧 EP 详情页 `OrderDetailView.vue` 已删——业务方裁定「不留两套长得不一样的订单详情」）：宿主把路由参数**逐个拼进 iframe query**（`components/LayuiIslandHost.vue::islandSrc`），island 页 `order-orderview.ts` 再从 `window.location.search` 读 `order_no` ⇒ 这里**不需要**在组件里读 `route.params`。旧 EP 详情页的 3 个动作（改价 / 状态流转 / 取消退单）随页移除，仍可在**订单管理页**做到（登记在本页「已知会不同」清单）。
      // ⚠️ **`(\d{20})` 约束（D-0036 加的）不要去掉**：后端 `Order.order_no` 的 `lookup_value_regex = r"\d{20}"`，20 位数字是这条路由**唯一**合法取值；不约束时任何单段路径都会被它吞成订单详情（`replicaRoutes.ts` 文件头记过这个动态冲突），D-0036 删掉的旧 `/orders/*` URL 会渲染「订单号格式不正确」的详情页而**不是** 404。探针实测（vue-router 5.3.1）：约束后 `/orders/platform-core` → `not-found`、`/orders/<20 位>` → `order-detail`、`/orders/new` 仍由静态段胜出 → `order-new`。
      path: '/orders/:order_no(\\d{20})',
      name: 'order-detail',
      // 1:1 复刻页（业务方指派）：外观按对标 `/Order/OrderView`，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      // 本条此前没有 `meta`（也没有 `meta.title`），这里只补准入判据与 island key、不改页签行为。
      meta: { islandPage: 'order-orderview', permissions: nodeCodesFor('order-detail') },
    },
    // 游戏维护三页：**三条各指向一个薄包装**（`GameListView` 等），它们再渲染同一个参数化视图 `ConfigPage.vue`——三个不同组件意味着路由之间切换**不会复用实例**；若三条直接指向同一个组件，`useConfigResource(resource, …)` 会拿着旧资源把请求打到错的端点。
    // URL 加 `game-config/` 前缀是有意的：冻结的是菜单位置与名称、不是 URL（对标物是 `/Game/...`），前缀避免与将来的「游戏号库 / 排团」撞名。判据 = 节点 code，取值表见 `config/routePermissions.ts`（D-0020 已结案）。
    {
      path: '/game-config/games',
      name: 'game-config-games',
      // 1:1 复刻页（业务方指派重做）：外观按对标，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { islandPage: 'game-gameserver',  title: '游戏管理', permissions: nodeCodesFor('game-config-games') },
    },
    // 业务方要求合并「订单项目分类管理」与「常用代练项目管理」：只剩这一条路由，island 页是左分类 / 右项目的主从页 `game-projectsetting`；旧的 `/game-config/categories` 路由已删除（直达落 404），免得两个入口各显示一半。
    {
      path: '/game-config/projects',
      name: 'game-config-projects',
      // 合并页（不是 1:1 复刻）：外观沿用 layui，功能接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { islandPage: 'game-projectsetting',  title: '项目维护', permissions: nodeCodesFor('game-config-projects') },
    },
    // 【财务相关】→「打手提现审核」。⛔ **自建路由，不是复刻**：对标物没有这一页 ⇒ **不进 `config/replicaRoutes.ts`**（那张表是对标 89 条菜单的逐条转录，`replicaRoutes.spec.ts` 断言恰好集合）。
    // 形状照上面的 `game-config-projects`：`meta.islandPage` + `title` + `nodeCodesFor(…)`。权限点 `finance.taker_withdraw_review` 是**新增**节点（计划 §4.4），取值表登记在 `config/routePermissions.ts`；菜单侧 `config/menu.ts` 的「财务相关」组是同一次改齐的另一半（两份由 `route-declarations.spec.ts` (c) 逐条比对）。
    {
      path: '/agents/withdraw-review',
      name: 'agents-withdraw-review',
      // 自建 island 页（没有对标证据件，外观沿用 layui，功能接我们自己的 API）。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'agents-withdraw-review',
        title: '打手提现审核',
        permissions: nodeCodesFor('agents-withdraw-review'),
      },
    },
    // 【财务相关】→「打手钱包管理」。⛔ **自建路由，不是复刻**：对标物没有这一页 ⇒ **不进 `config/replicaRoutes.ts`**。形状照上面的 `agents-withdraw-review`：`meta.islandPage` + `title` + `nodeCodesFor(…)`。
    // 权限点 `finance.taker_wallet_adjust` 是**新增**的自有节点，取值表登记在 `config/routePermissions.ts`；菜单侧 `config/menu.ts` 的「财务相关」组是同一次改齐的另一半。
    {
      path: '/agents/wallet-adjust',
      name: 'agents-wallet-adjust',
      // 自建 island 页（没有对标证据件，外观沿用 layui，功能接我们自己的 API）。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'agents-wallet-adjust',
        title: '打手钱包管理',
        permissions: nodeCodesFor('agents-wallet-adjust'),
      },
    },
    // 【财务相关 → 打手钱包】合并页（spec `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.2）。⛔ **自建路由，不是复刻**：对标物没有这一页 ⇒ **不进 `config/replicaRoutes.ts`**。形状照上面的 `agents-wallet-adjust`。
    // ⚠️ **最小改法**：两份既有页模块（867 / 809 行）**文件保留不动**、上面两条旧路由**照旧活着**（旧深链直接可用，不引入 redirect 机制——仓库今天没有 redirect 先例）；本合并页只是**新增一个入口**，页里两个页签**复用两份既有页模块的内核**。
    // ⚠️ 权限 = 两个既有节点任一（`canNode` 是 some 语义）；页签按权限渲染，只有一个节点的人只见对应页签（D-0154 的 fail-closed 在页内落实）。
    {
      path: '/agents/taker-wallet',
      name: 'agents-taker-wallet',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'agents-taker-wallet',
        title: '打手钱包',
        permissions: nodeCodesFor('agents-taker-wallet'),
      },
    },
    // 【财务相关 → 闲管家收款明细】（spec `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.1）。⛔ **自建路由，不是复刻**：对标物没有这一页 ⇒ **不进 `config/replicaRoutes.ts`**。形状照上面的 `agents-taker-wallet`。
    // 权限点 = **新增**的自有节点 `finance.xgj_receipt_detail`（后端 `catalog.NODES` 与数据迁移 `0012` 同一次落），取值表登记在 `config/routePermissions.ts`；菜单侧 `config/menu.ts` 的「财务相关」组同一次改齐。
    {
      path: '/finance/xgj-receipts',
      name: 'xgj-receipts',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'xgj-receipts',
        title: '闲管家收款明细',
        permissions: nodeCodesFor('xgj-receipts'),
      },
    },
    // 客服宝桌面窄窗直连 /kefu。Framework7 Vue 视图自带顶栏，省去后台外壳。
    // 路由守卫继续使用订单列表节点；页内各操作分别检查权限。
    {
      path: '/kefu',
      name: 'kefu-console',
      component: () => import('@/views/kefu/KefuConsoleView.vue'),
      meta: {
        layout: 'blank',
        title: '客服工作台',
        permissions: nodeCodesFor('kefu-console'),
      },
    },
    {
      path: '/wecom-client-lab',
      name: 'wecom-client-lab',
      component: () => import('@/views/wecom-lab/WecomClientLab.vue'),
      meta: { layout: 'blank', title: '企微客户端测试', permissions: nodeCodesFor('wecom-client-lab'), requiresAccountAdmin: true },
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { requiresAuth: false, layout: 'blank' },
    },
    {
      path: '/visitor-chat',
      name: 'visitor-chat',
      component: () => import('@/v2/internalChat/VisitorChatView.vue'),
      props: { scope: 'guest' },
      meta: { title: '访客注册', requiresAuth: false, layout: 'blank' },
    },
    {
      path: '/v2/internal-chat-visitors',
      name: 'v2-internal-chat-visitors',
      component: () => import('@/v2/internalChat/VisitorChatView.vue'),
      props: { scope: 'desk' },
      meta: { title: '访客接待', layout: 'blank' },
    },
    {
      path: '/v2/internal-chat-window',
      name: 'v2-internal-chat-window',
      component: () => import('@/v2/internalChat/InternalChatWideView.vue'),
      meta: { title: '内部沟通', layout: 'blank' },
    },
    // 【系统人员】6 条菜单 + 1 条编辑页。准入判据**只有一条**：`meta.permissions`（**RBAC 节点 code**，由 `guards.ts` 的 `session.canNode` 执行，取值表见 `config/routePermissions.ts`，D-0020 已结案）——**fail-closed**：没声明节点码或没有交集的会话进不去（白名单 3 条之外每条路由都必须声明，由 `route-declarations.spec.ts` 的 (a) 守）。
    // ⛔ **`requiresAccountAdmin` 已从这 7 条上删除**（原因，不是顺手改）：后端账号端点的准入已从「持『系统管理员』职位」放宽到 `IsAccountManager`（= 系统管理员 **或** 持任一账号管理节点），而「人事」正是持 `system_user.*` 节点的非管理员 ⇒ 留着这道门会让**人事看不到入口、直敲 URL 也被兜回首页**，业务目标等于没交付。⚠️ 判据**没有开没**：节点门照旧 + 后端那一层照旧；⚠️ 该机制**本身没删**——仍挂在另外 6 条自有功能路由上（见下面各自的注释）。`meta.title` = 页签条标题（`AppShell` 读它）。
    // 人员管理接后端 `/api/accounts/`（功能页）；其余 4 条曾是 `PlaceholderView.vue` 的**结构复刻占位页**，现已按 D-0026 换成 layui island 复刻页。path/name 保持不变：换 URL 会牵动菜单 `routeName` 与既有测试，而那 4 条没有任何 bookmark 价值。业务方裁定删除 3 条（邀请大神注册管理 / 打卡记录 / 打卡分析，D-0036）：显式路由、island 页、证据件与菜单条目一并删除。
    {
      path: '/system-users',
      name: 'system-users',
      // 1:1 复刻页：用通用 island 宿主（页面 key 在 `meta.islandPage`，注册表见 `src/layui-island/page.ts`）。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '人员管理',
        islandPage: 'system-user-list',
        permissions: nodeCodesFor('system-users'),
      },
    },
    {
      // 静态段 `new` 写在 `:id/edit` 之前，与订单三页同一条可读性约定（匹配靠 score，不靠顺序）。
      path: '/system-users/new',
      name: 'system-user-new',
      // 1:1 复刻页（业务方指派重做）：外观按对标，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'user-adduser',
        title: '新开系统账号',
        permissions: nodeCodesFor('system-user-new'),
      },
    },
    {
      path: '/system-users/:id/edit',
      name: 'system-user-edit',
      // 1:1 复刻页（业务方指派重做）：外观按对标，功能仍接我们自己的 API。
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        islandPage: 'user-adduser',
        title: '编辑系统账号',
        permissions: nodeCodesFor('system-user-edit'),
      },
    },
    {
      path: '/system-users/login-restrict',
      name: 'system-login-restrict',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '登录限制设置',
        islandPage: 'user-userloginrestrict',
        permissions: nodeCodesFor('system-login-restrict'),
      },
    },
    {
      path: '/system-users/blacklist',
      name: 'system-blacklist',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '人员黑名单',
        islandPage: 'user-backuserlist',
        permissions: nodeCodesFor('system-blacklist'),
      },
    },
    {
      path: '/system-users/real-name',
      name: 'system-real-name',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '人员实名记录与配置',
        islandPage: 'user-rnamecertlogandsetting',
        permissions: nodeCodesFor('system-real-name'),
      },
    },
    {
      path: '/system-users/contract',
      name: 'system-contract',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '员工电子合同管理',
        islandPage: 'user-usercontractsigning',
        permissions: nodeCodesFor('system-contract'),
      },
    },
    // 【第三方平台】→「闲管家店铺对接」（**自有功能，不是复刻**：对标物里没有这一页）。页面是 `pages/thirdparty-xgjshop.ts`（自建标记，没有证据件，不走 `mountConfigLike` 那条复刻链路）。
    // ⚠️ 它**不进** `REPLICA_ROUTES`，也**没有** `REPLICA_SKIP` 条目——那两处的登记对象都是**对标条目**（89 条转录里的 key），本页不在那份转录里。⚠️ `permissions` 取的是**复用**的既有节点（`system.pinduoduo_settings`），逐条理由与代价见 `config/routePermissions.ts` 的「已知近似」。
    // 畸形入参覆盖：本页只调已存在的端点（读单端点由 `tests/test_read_platform_order.py` 与 `tests/test_xgj_shop_binding.py` 自己钉），这里没有新端点的入参面。
    {
      path: '/thirdparty/xgj-shops',
      name: 'thirdparty-xgj-shop',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '闲管家店铺对接',
        islandPage: 'thirdparty-xgjshop',
        permissions: nodeCodesFor('thirdparty-xgj-shop'),
      },
    },
    // 【第三方平台】→「鸡蛋铺系统同步」（**自有功能，不是复刻**：对标物里没有这一页）。页面曾是 `pages/thirdparty-jidanpu-sync.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES`、也没有 `REPLICA_SKIP` 条目，island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`；`permissions` 取的是**复用**的既有节点 `system.pinduoduo_settings`。
    // ⛔ 业务方裁定废弃本功能 ⇒ 那条路由对象已下架（与 `config/menu.ts` 的菜单条目同一次改完，否则 `e2e/fixtures/routes.ts::HIDDEN_MENU_PATHS` 会在模块加载期抛错）；前端两个文件（`thirdparty-jidanpu-sync.ts` / `api/jidanpuSync.ts`）、它们的三条测试、`routePermissions.ts` 的登记、`OWN_FEATURE_PAGE_KEYS` 那一项**已同一次移除**。
    // ⛔ 别把这条路由或那两个文件「修」回来：它们今天**不存在**（`AGENTS.md` §5-C：注释按实现写）。

    // 【开放平台】→「性能分析」（**自有功能，不是复刻**：对标物里没有这一页）。页面是 `pages/system-perfanalysis.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（与菜单侧的两条判据一一对应，都保留）**：① `permissions`（**复用**的既有节点 `system.logs`，取值表见 `config/routePermissions.ts` 的「已知近似」）——它是 `routePermissions.spec.ts` 的**恰好集合**断言与 `route-declarations.spec.ts` (d) 所要求的（⛔ 不写它要同时放宽两条网）；② `requiresAccountAdmin: true`——业务方要的是「只对账号管理员可见」，判据值来自 `/api/auth/me` 的 `is_account_admin`，与菜单整组的 `accountAdminOnly` 同源。两条判据在 `guards.ts` 里各自独立执行 ⇒ 非账号管理员即使持有 `system.logs` 也进不来。
    {
      path: '/system/performance',
      name: 'system-perfanalysis',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '性能分析',
        islandPage: 'system-perfanalysis',
        permissions: nodeCodesFor('system-perfanalysis'),
        requiresAccountAdmin: true,
      },
    },
    // 【开放平台】→「应用管理」（**自有功能，不是复刻**：对标物里没有这一页）。页面是 `pages/open-apps.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（与菜单侧的两条判据一一对应，缺一不可）**：① `permissions`（**复用**的既有节点 `system.pinduoduo_settings`，spec D9 直接裁定「⛔ 不新增 RBAC 节点」；取值表见 `routePermissions.ts` 的「已知近似」）——恰好集合断言与 `route-declarations.spec.ts` (d) 所要求（⛔ 不写它要同时放宽两条网）；② `requiresAccountAdmin: true`——「只对账号管理员可见」，判据值来自 `/api/auth/me` 的 `is_account_admin`，与菜单整组同源。两条判据在 `guards.ts` 里各自独立执行 ⇒ 非账号管理员即使持那个节点也进不来。
    {
      path: '/open-platform/apps',
      name: 'open-apps',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '应用管理',
        islandPage: 'open-apps',
        permissions: nodeCodesFor('open-apps'),
        requiresAccountAdmin: true,
      },
    },
    // 【APP与小程序】→「发布文章作品」（**自有功能，不是复刻**：对标物里没有这一页）。业务方口径：移动端「频道」只读，编辑 / 发布 / 草稿列表都放在网页端（计划 `docs/plans/2026-10-23-channel-articles.md` §1）；页面是 `layui-island/pages/app-mp-articles.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **只有一道门**：`permissions` = 本批新增的自有节点 `system.article_manage`（后端同批在 `catalog.NODES` 登记 + 数据迁移落到存量租户）。⛔ 不挂 `requiresAccountAdmin`：这一页交给内容运营，不是账号管理员专属（与【开放平台】那几页不同——那几页管的是凭据）。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`e2e/fixtures/routes.ts` 的 `readStaticIslandRoutes()` 只认这一种形状，否则它**响亮失败**（不是静默少测）。
    {
      path: '/app-mp/articles',
      name: 'app-mp-articles',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '发布文章作品',
        islandPage: 'app-mp-articles',
        permissions: nodeCodesFor('app-mp-articles'),
      },
    },
    // 【第三方平台】→「自动上传企微群码到订单截图」（**自有功能，不是复刻**：对标物里没有这一页；业务方原话点名新增这一格）。页面是 `pages/wecom-group-qr.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（缺一不可）**：① `permissions`（**复用**的既有节点 `system.pinduoduo_settings`，设计文档 D9 直接裁定「⛔ 不新增 RBAC 节点」；取值表见 `routePermissions.ts` 的「已知近似」）——恰好集合断言与 `route-declarations.spec.ts` (d) 所要求（⛔ 不写它要同时放宽两条网）；② `requiresAccountAdmin: true`——设计文档 D10：这一组**不能**加组级 `accountAdminOnly`（会连累同组的「闲管家店铺对接」），而 AppSecret 是明文可取的凭据 ⇒ 页面内容收窄到账号管理员（判据值 = `/api/auth/me` 的 `is_account_admin`）。两条判据在 `guards.ts` 里各自独立执行。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则它**响亮失败**（不是静默少测）。
    {
      path: '/thirdparty/wecom-group-qr',
      name: 'wecom-group-qr',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '自动上传企微群码到订单截图',
        islandPage: 'wecom-group-qr',
        permissions: nodeCodesFor('wecom-group-qr'),
        requiresAccountAdmin: true,
      },
    },
    // 【第三方平台】→「微信公众号设置」（**自有功能，不是复刻**：对标物里没有这一页；D-0268 自开发）。页面是 `pages/system-wechatmp-settings.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（缺一不可，照企微群码那页的形状）**：① `permissions`（**复用**既有节点 `system.pinduoduo_settings`，第三方平台组现行口径 ⛔ 不新增节点；取值表见 `routePermissions.ts` 的「已知近似」）；② `requiresAccountAdmin: true`——token / aes_key 是要复制进公众平台的原文（AppSecret 只回掩码），内容收窄到账号管理员（企微群码 D10 的同款做法）。深链打开时还有页内自检（同一个节点 + 账号管理员，fail-closed）。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则它**响亮失败**（不是静默少测）。
    {
      path: '/thirdparty/wechatmp-settings',
      name: 'system-wechatmp-settings',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '微信公众号设置',
        islandPage: 'system-wechatmp-settings',
        permissions: nodeCodesFor('system-wechatmp-settings'),
        requiresAccountAdmin: true,
      },
    },

    // 【第三方平台】→「马上来查码对接」（**自有功能，不是复刻**：对标物里没有这一页；D-0348 自开发）。页面是 `pages/system-order-qr-fetch.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（缺一不可，照微信公众号设置那页的形状）**：① `permissions`（**复用**既有节点 `system.pinduoduo_settings`，D-0128 冻结 ⛔ 不新增节点）；② `requiresAccountAdmin: true`——api_key 是凭据（后端只回掩码），内容收窄到账号管理员（企微群码 D10 的同款做法）。深链打开时还有页内自检（同一个节点 + 账号管理员，fail-closed）。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则它**响亮失败**（不是静默少测）。
    {
      path: '/thirdparty/order-qr-fetch',
      name: 'system-order-qr-fetch',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '马上来查码对接',
        islandPage: 'system-order-qr-fetch',
        permissions: nodeCodesFor('system-order-qr-fetch'),
        requiresAccountAdmin: true,
      },
    },

    // 【第三方平台】→「QQ机器人设置」（**自有功能，不是复刻**：对标物里没有这一页；已接入，后端端点挂 `api/qqbot/` 前缀）。页面是 `pages/qqbot-settings.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **两道门（缺一不可）**：① `permissions`——**自有节点** `system.qqbot_settings`（`benchmark_id = None`；与 `wecom-group-qr` 复用别页的点不同，这一条**能单独授权**，登记见 `routePermissions.ts`）；② `requiresAccountAdmin: true`——这一页管 AppSecret 凭据（`wecom-group-qr` 的 D10 同款：判据值 = `/api/auth/me` 的 `is_account_admin`）；组级 `accountAdminOnly` 照旧不加（会连累同组「闲管家店铺对接」）。两条判据在 `guards.ts` 里各自独立执行。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则**响亮失败**；同一次必须登记进那个文件的 `EXPLICIT_ROUTES` **与** `EXPLICIT_ROUTES_BY_NAME`（两份清单是一件事的两处写法，漏一处会在**模块加载期**当场抛）。
    {
      path: '/thirdparty/qqbot-settings',
      name: 'qqbot-settings',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: 'QQ机器人设置',
        islandPage: 'qqbot-settings',
        permissions: nodeCodesFor('qqbot-settings'),
        requiresAccountAdmin: true,
      },
    },
    // ⭐ 【开放平台】→「打手对接」（**自有功能，不是复刻**：对标物里没有这一页；spec 裁定「页面放【开放平台】组、与「应用管理」并排」＋「新增一个权限点」）。页面是 `pages/taker-apps.ts`（自建标记、没有证据件 ⇒ 不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **只有一道门**（节点码）：`system.allow_taker_open_api`——⛔ **不许**挂 `requiresAccountAdmin: true`（「应用管理」那两条挂的是它）：**打手不是账号管理员**，挂上它打手就被自己那道门挡在外面（页面白做）；「谁是管理员」那条分支判据**在服务端**（`apps/takerapi/api/app_views.py::TakerAppAdminView._is_system_admin`），页面只按它决定「显示全部 + 选绑谁」还是「只看自己 + 锁死为自己」（spec §6）。本页**不**挂 `requiresAccountAdmin` ⇒ 「非账号管理员直接敲 URL」这一格由**节点码**挡（`guards.ts`）。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则**响亮失败**；同一次必须登记进 `EXPLICIT_ROUTES` **与** `EXPLICIT_ROUTES_BY_NAME`（漏一处会在**模块加载期**当场抛）。
    {
      path: '/open-platform/taker-apps',
      name: 'taker-apps',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '打手对接',
        islandPage: 'taker-apps',
        permissions: nodeCodesFor('taker-apps'),
      },
    },
    // 【侧栏最底部】→「问题反馈」（**自有功能，不是复刻**：对标物侧栏最底部没有这一条；业务方原话「直接排到最下面的菜单」，spec §6.1 / §6.2）。页面是 `pages/feedback.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ `permissions` 取 `home.feedback`（对标 112 号点「问题反馈及建议」）——这是**精确命中**而不是像 `system-perfanalysis` / `open-apps` 那样复用别的点；逐条理由见 `config/routePermissions.ts` 里那段「一条不是近似的登记」。⚠️ **只有一道门**（节点码）：业务方要的是「谁都能提交、只有系统管理员看得到全部」，而「谁是管理员」那条判据（`is_tenant_admin`）**在服务端**（列表 queryset 收窄 + PATCH 403），所以**不挂** `requiresAccountAdmin`——挂了会把非管理员的**提交**入口也一起挡掉。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则它**响亮失败**（不是静默少测）。
    {
      path: '/feedback',
      name: 'feedback',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: { title: '问题反馈', islandPage: 'feedback', permissions: nodeCodesFor('feedback') },
    },
    // 【系统人员】组 →「自助注册」（**自有功能，不是复刻**：对标物的人员管理组没有这一页；业务方需求：生成一次性注册链接给打手自助开户，spec `docs/specs/2026-10-16-self-registration-links-design.md` §4 / §6）。页面是 `pages/self-registration.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`。
    // ⚠️ **只有一道门**（节点码）：`system_user.add_edit`——与「新开系统账号 / 编辑系统账号」同一个点（spec 直接裁定，⛔ 不新增 RBAC 节点）；「页内不再二次收窄」，也不挂 `requiresAccountAdmin`（持该节点的**人事**要能生成链接，挂上就把业务目标挡死了）。
    // ⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则**响亮失败**；同一次必须登记进 `EXPLICIT_ROUTES` **与** `EXPLICIT_ROUTES_BY_NAME`（漏一处会在**模块加载期**当场抛）。
    {
      path: '/self-registration',
      name: 'self-registration',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '自助注册',
        islandPage: 'self-registration',
        permissions: nodeCodesFor('self-registration'),
      },
    },
    // ⭐ 【APP与小程序 → 数据概览】（**自有功能，不是复刻**：对标物里没有这一组）。计划 `docs/plans/2026-10-23-app-mp-stats.md` §7/§8；页面是 `pages/app-stats.ts`（自建标记，没有证据件，不走 `mountConfigLike`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`，页面说明在 `docs/FRONTEND-LAYOUT.md` §4。
    // ⚠️ **只有一道门**（节点码）：复用 `finance.support_statistics`（⛔ 不新增 RBAC 节点）；路由守卫（`guards.ts`）按节点挡，页内再自检一次。⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状，否则**响亮失败**；同一次必须登记进 `EXPLICIT_ROUTES` **与** `EXPLICIT_ROUTES_BY_NAME`。
    {
      path: '/business/app-stats',
      name: 'app-stats',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '数据概览',
        islandPage: 'app-stats',
        permissions: nodeCodesFor('app-stats'),
      },
    },
    // ⭐ 【发平台设置】组两条**自有功能**页（对标物里没有这两页）——「代练通按日统计」与「代练通订单要求模板」。页面是 `layui-island/pages/dailiantong-stats.ts` / `dailiantong-order-templates.ts`（自建标记，没有证据件，不走 `mountConfigLike` / `artifactPage`）；不进 `REPLICA_ROUTES` 也没有 `REPLICA_SKIP` 条目，island key 登记在 `OWN_FEATURE_PAGE_KEYS`，页面说明在 `docs/FRONTEND-LAYOUT.md` §4。
    // ⚠️ **只有一道门**（节点码）：两页的后端 `permission_classes` 都是 `order.list`（⛔ 不新增 RBAC 节点）；路由守卫按它挡，页内再用同一节点自检一次（iframe 深链）。⚠️ `path` 写成**单引号字面量、行首（除空白）就是 `path:`**：`readStaticIslandRoutes()` 只认这一种形状；同一次必须登记进 `EXPLICIT_ROUTES` **与** `EXPLICIT_ROUTES_BY_NAME`。
    {
      path: '/system/dailiantong-stats',
      name: 'dailiantong-stats',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '代练通按日统计',
        islandPage: 'dailiantong-stats',
        permissions: nodeCodesFor('dailiantong-stats'),
      },
    },
    {
      path: '/system/dailiantong-order-templates',
      name: 'dailiantong-order-templates',
      component: () => import('@/components/LayuiIslandHost.vue'),
      meta: {
        title: '代练通订单要求模板',
        islandPage: 'dailiantong-order-templates',
        permissions: nodeCodesFor('dailiantong-order-templates'),
      },
    },
    // ⭐【前端 v2 试验线】（D-0344）：`/v2` 独立路由子树——炭火外壳 + 新首页，旧路由与旧壳一行不改（业务方原话「老的前端我需要保留 怕效果不好」）。实施计划 `docs/plans/2026-10-30-frontend-v2-shell-dashboard.md`；视觉标准 `docs/FRONTEND-V2-DRAFT.md`（token 唯一出处，样式在 `src/v2/carbon.css`，全部挂在 `.ttv2` 作用域下、经 main.ts 引入——对旧页面是惰性的）。
    // **认证同旧口径**：不挂 `meta.permissions`（守卫 `requiresAuth` 默认 true = 登录即可，与白名单里的 `home` 同一档，判据见 `route-declarations.spec.ts` 的 WHITELIST；该白名单与 `e2e/fixtures/routes.ts` 的 NON_ISLAND_PATHS 需把 `v2` / `v2-home` / `/v2` 登记进去）。父记录命名 `v2`：嵌套父记录也会进 `getRoutes()` 的审计面，匿名记录会以 `''` 混进「无声明路由集合」，命名后该集合里是两个可读的 name。
    {
      path: '/v2',
      name: 'v2',
      // ⚠️ `layout: 'blank'` 是承重的：`App.vue` 的壳层开关按 `route.meta.layout ?? 'shell'`
      //    走（只有 `/login` 之外的「裸路由」才逃出旧 AppShell）——漏了它，炭火壳会被旧壳
      //    整个包住（2026-10-30 合并后 e2e 视觉验证实锤；钉子在 v2Routes.spec 的 layout 针）。
      component: () => import('@/v2/CarbonShell.vue'),
      meta: { title: '后台首页 v2', layout: 'blank' },
      children: [
        {
          path: '',
          name: 'v2-home',
          component: () => import('@/v2/V2Dashboard.vue'),
          meta: { title: '后台首页 v2' },
        },
        // 「指派给我的订单」v2（D-0346，2026-10-31 业务方指令④）：功能与布局照旧 island 页
        // `order-myorderlist`，炭火重建（`src/v2/V2MyOrders.vue`）；数据零新增端点。
        // meta 会从父记录继承 `layout:'blank'`（E-0308 的钉在 v2Routes.spec 看着）。
        {
          path: 'my-orders',
          name: 'v2-my-orders',
          component: () => import('@/v2/V2MyOrders.vue'),
          meta: { title: '指派给我的订单', permissions: nodeCodesFor('v2-my-orders') },
        },
        // 「内部抢单大厅」v2（D-0352）：功能照旧 island 页
        // `order-insiderecorderlist`，炭火重建（`src/v2/V2Hall.vue`）；数据零新增端点。
        // meta 同样从父记录继承 `layout:'blank'`（钉在 v2Routes.spec）。
        {
          path: 'hall',
          name: 'v2-hall',
          component: () => import('@/v2/V2Hall.vue'),
          meta: { title: '内部抢单大厅', permissions: nodeCodesFor('v2-hall') },
        },
        // 「我的相关」组 4 页 v2 化（D-0353）：功能照旧、视觉全炭火；数据零新增
        // 端点。旧 island 页（emp-myemp / emp-mybillsummary / user-myinfo / user-mymessage）
        // 一行不改；v2Menu 的特例映射 +4（与 D-0352 合计恰好七条）在 `v2/v2Menu.ts`。
        {
          path: 'income',
          name: 'v2-income',
          component: () => import('@/v2/V2Income.vue'),
          meta: { title: '我的订单收入', permissions: nodeCodesFor('v2-income') },
        },
        {
          path: 'settlement',
          name: 'v2-settlement',
          component: () => import('@/v2/V2Settlement.vue'),
          meta: { title: '在线结算记录', permissions: nodeCodesFor('v2-settlement') },
        },
        {
          path: 'profile',
          name: 'v2-profile',
          component: () => import('@/v2/V2Profile.vue'),
          meta: { title: '我的资料', permissions: nodeCodesFor('v2-profile') },
        },
        {
          path: 'messages',
          name: 'v2-messages',
          component: () => import('@/v2/V2Messages.vue'),
          meta: { title: '消息通知', permissions: nodeCodesFor('v2-messages') },
        },
        ...(import.meta.env.DEV ? [{
          path: 'internal-chat',
          name: 'v2-internal-chat',
          component: () => import('@/v2/V2InternalChat.vue'),
          meta: { title: '内部通讯' },
        }] : []),
        {
          path: '/v2/orders/:order_no(\\d{20})',
          name: 'v2-order-detail-preview',
          beforeEnter: (to) => orderChatPreviewEnabled() || useSessionStore().user?.internal_chat_enabled === true
            || { name: 'order-detail', params: { order_no: to.params.order_no } },
          component: () => import('@/v2/V2OrderDetailPane.vue'),
          props: (route: { params: Record<string, unknown> }) => ({
            orderNo: String(route.params.order_no),
            allowOwnOrderDetail: true,
            allowOwnOrderFallback: true,
          }),
          meta: { title: '订单详情' },
        },
        {
          path: 'approvals',
          name: 'v2-approvals',
          component: () => import('@/v2/V2Approvals.vue'),
          meta: { title: '审批中心' },
        },
        {
          path: 'wecom-archive',
          name: 'v2-wecom-archive',
          component: () => import('@/v2/V2WecomArchiveSearch.vue'),
          meta: { title: '查找聊天记录' },
        },
        // 「订单时间线」（D-0360）：按单号聚合全量留痕的自建 v2 页。⭐ **URL 直达、不进侧边菜单**（拍板②）——入口只有本路由与接诊手册（TRIAGE §2.F），⛔ 不进 `config/menu.ts` / `v2Menu.ts`。
        {
          path: 'order-audit',
          name: 'v2-order-audit',
          component: () => import('@/v2/V2OrderAudit.vue'),
          meta: { title: '订单时间线', layout: 'blank', permissions: nodeCodesFor('v2-order-audit') },
        },
        // 「录入订单」v2（D-0355）：功能照现役 island 复刻页 `order-addorder`（任务书所写 `OrderNewView.vue` 已删），炭火重建（`src/v2/V2OrderNew.vue`）；表单模型/载荷 = 共享模块 `views/orders/orderFormFields.ts`。
        {
          path: 'order-new',
          name: 'v2-order-new',
          component: () => import('@/v2/V2OrderNew.vue'),
          meta: { title: '录入订单', permissions: nodeCodesFor('v2-order-new') },
        },

        // 「管理订单」v2（2026-11-04 业务方授权本地迭代线）：功能照 island 复刻页
        // `order-orderlist`（路由 `orders`），炭火重建（`src/v2/V2OrderManage.vue`）；
        // 数据零新增端点（列表 + summary 走 `v2/orderManageApi.ts` 薄封装）。
        {
          path: 'order-manage',
          name: 'v2-order-manage',
          component: () => import('@/v2/V2OrderManage.vue'),
          meta: { title: '管理订单', permissions: nodeCodesFor('v2-order-manage') },
        },
        // 「第三方平台订单管理」v2（2026-11-05 设计线）：照 island 复刻页 `order-coreordercenter`
        // （路由 `orders-platform-core`），炭火重建（`src/v2/V2PlatformOrders.vue`）；
        // 数据零新增端点（列表/聚合/页签计数/批量与行内动作全走 `api/dailiantongPush.ts`）。
        {
          path: 'platform-orders',
          name: 'v2-platform-orders',
          component: () => import('@/v2/V2PlatformOrders.vue'),
          meta: { title: '第三方平台订单管理', permissions: nodeCodesFor('v2-platform-orders') },
        },
        // 「找回删除的订单」v2（同线）：照 island 页 `order-orderrecycle`（路由 `orders-recycle`），
        // 炭火重建（`src/v2/V2OrderRecycle.vue`；列表走 `v2/orderRecycleApi.ts` 薄封装、还原复用 `restoreOrder`）。
        {
          path: 'order-recycle',
          name: 'v2-order-recycle',
          component: () => import('@/v2/V2OrderRecycle.vue'),
          meta: { title: '找回删除的订单', permissions: nodeCodesFor('v2-order-recycle') },
        },
        // 「新开系统账号」v2（2026-11-05）：island `user-adduser` + 自有页「自助注册」合并为
        // 一页两 tab（`src/v2/V2UserNew.vue`）；菜单映射与 v2 侧栏隐藏「自助注册」见 `v2Menu.ts`。
        // 本页挂权限门（`system_user.add_edit`，与旧路由同节点）；layout 从父记录继承 blank。
        {
          path: 'user-new',
          name: 'v2-user-new',
          component: () => import('@/v2/V2UserNew.vue'),
          meta: { title: '新开系统账号', permissions: ['system_user.add_edit'] },
        },
        // 「交易订单操作记录」v2（2026-11-05 设计线）：照 island 页 `order-tradingorderactionloglist`
        // （路由 `orders-platform-action-log`），炭火重建（`src/v2/V2OrderActionLog.vue`；
        // 数据走共享 `api/orderActionLogs.ts`，零新增端点）。
        {
          path: 'order-action-log',
          name: 'v2-order-action-log',
          component: () => import('@/v2/V2OrderActionLog.vue'),
          meta: { title: '交易订单操作记录', permissions: nodeCodesFor('v2-order-action-log') },
        },
        // 「订单发货记录」v2（同线；对标名「淘宝发货记录」，业务方 2026-11-05 改名）：照 island 页
        // `order-tbsendrecordlist`（路由 `orders-taobao-ship`），炭火重建（`src/v2/V2OrderShipLog.vue`；
        // 数据走共享 `api/tbSendLogs.ts`）。
        {
          path: 'ship-log',
          name: 'v2-ship-log',
          component: () => import('@/v2/V2OrderShipLog.vue'),
          meta: { title: '订单发货记录', permissions: nodeCodesFor('v2-ship-log') },
        },
        // 「上家订单改价记录」v2（同线）：照 island 页 `order-soureorderchangepricelog`
        // （路由 `orders-source-price-log`），炭火重建（`src/v2/V2UpstreamPriceLog.vue`）。
        {
          path: 'upstream-price-log',
          name: 'v2-upstream-price-log',
          component: () => import('@/v2/V2UpstreamPriceLog.vue'),
          meta: { title: '上家订单改价记录', permissions: nodeCodesFor('v2-upstream-price-log') },
        },
        // 「系统人员」组其余 5 页 v2（2026-11-05 系统人员批次）：人员管理（本批先落如实占位，内容
        // 随后填实）、人员实名记录与配置（全功能）、登录限制设置 / 人员黑名单 / 员工电子合同管理
        // （三个空壳对标页 → 诚实空态）。5 页都挂权限门（与旧路由同节点）；layout 从父记录继承 blank。
        {
          path: 'user-list',
          name: 'v2-user-list',
          component: () => import('@/v2/V2UserList.vue'),
          meta: { title: '人员管理', permissions: ['system_user.list'] },
        },
        {
          path: 'real-name',
          name: 'v2-real-name',
          component: () => import('@/v2/V2RealName.vue'),
          meta: { title: '人员实名记录与配置', permissions: ['system_user.realname_records'] },
        },
        {
          path: 'login-restrict',
          name: 'v2-login-restrict',
          component: () => import('@/v2/V2LoginRestrict.vue'),
          meta: { title: '登录限制设置', permissions: ['system_user.login_restriction'] },
        },
        {
          path: 'blacklist',
          name: 'v2-blacklist',
          component: () => import('@/v2/V2Blacklist.vue'),
          meta: { title: '人员黑名单', permissions: ['system_user.blacklist'] },
        },
        {
          path: 'contract',
          name: 'v2-contract',
          component: () => import('@/v2/V2Contract.vue'),
          meta: { title: '员工电子合同管理', permissions: ['system_user.contract_management'] },
        },
        // 「闲管家收款明细」v2（同批）：旧页同款骨架（改选即查 + 主从明细），炭火重建
        // （`src/v2/V2XgjReceipts.vue`）；数据走共享 `api/xgjReceipts.ts`，零新增端点。
        {
          path: 'xgj-receipts',
          name: 'v2-xgj-receipts',
          component: () => import('@/v2/V2XgjReceipts.vue'),
          meta: { title: '闲管家收款明细', permissions: ['finance.xgj_receipt_detail'] },
        },
        // ⭐「会员管理」v2（2026-11-10 客户线）：炭火真页（`src/v2/V2Customers.vue`）；
        // 数据走 `api/customers.ts`（后端同线新增两个只读端点）。权限门与旧路由
        // `customers`（复刻壳页）同一节点（v2 侧钉在 `v2Routes.spec` 的 it.each 行 +
        // `routePermissions.spec` 的表恰等；`route-declarations.spec` (c) 比的是旧路由 vs 菜单）；
        // layout 从父记录继承 blank。
        {
          path: 'customers',
          name: 'v2-customers',
          component: () => import('@/v2/V2Customers.vue'),
          meta: { title: '客户管理', permissions: ['customer.member_list', 'customer.member_list_self_only'] },
        },
        // ⭐「客户分析报表」v2（2026-11-10 客户线第三批）：炭火真页（`V2CustomerAnalysis.vue`，
        // range 三档一次拉 KPI/来源分布/消费分层/客户增长）。权限门与旧路由
        // `customers-analysis`（复刻壳页）同一节点 `customer.member_analysis_report`；
        // 后端 analysis 端点闸 `customer.member_list`（管理员短路）——两者都是冻结目录点，
        // 实际只有管理员能过，v2 与旧侧一致沿用菜单/路由的声明（v2Routes.spec it.each 钉）。
        {
          path: 'customer-analysis',
          name: 'v2-customer-analysis',
          component: () => import('@/v2/V2CustomerAnalysis.vue'),
          meta: { title: '客户分析报表', permissions: ['customer.member_analysis_report'] },
        },
        // ⭐「黑名单管理」（客户黑名单）v2 占位（2026-11-11 客户线第七批 §1.4）：炭火
        // 「功能建设中」空态页（`V2CustomerBlacklist.vue`），点击不再跳 v1 旧壳。权限门与
        // 旧路由 `customers-blacklist` 同节点 `customer.blacklist`；真功能另行拍板（挂账）。
        {
          path: 'customer-blacklist',
          name: 'v2-customer-blacklist',
          component: () => import('@/v2/V2CustomerBlacklist.vue'),
          meta: { title: '黑名单管理', permissions: ['customer.blacklist'] },
        },
        // 「游戏维护」「发平台设置」两组 6 页 v2（2026-11-05 设计线，分支 feat/v2-game-platform）：
        // 各页照旧 island 页炭火重建，数据零新增端点；规格与有意差异见
        // `docs/plans/2026-11-05-v2-game-and-platform-pages.md`。
        {
          path: 'game-manage',
          name: 'v2-game-manage',
          component: () => import('@/v2/V2GameManage.vue'),
          meta: { title: '游戏管理', permissions: nodeCodesFor('v2-game-manage') },
        },
        {
          path: 'game-projects',
          name: 'v2-game-projects',
          component: () => import('@/v2/V2GameProjects.vue'),
          meta: { title: '订单项目分类与常用项目', permissions: nodeCodesFor('v2-game-projects') },
        },
        {
          path: 'platform-integration',
          name: 'v2-platform-integration',
          component: () => import('@/v2/V2PlatformIntegration.vue'),
          meta: { title: '交易平台对接设置', permissions: nodeCodesFor('v2-platform-integration') },
        },
        {
          path: 'platform-auto-reply',
          name: 'v2-platform-auto-reply',
          component: () => import('@/v2/V2PlatformAutoReply.vue'),
          meta: { title: '交易平台自动回复', permissions: nodeCodesFor('v2-platform-auto-reply') },
        },
        {
          path: 'dailiantong-stats',
          name: 'v2-dailiantong-stats',
          component: () => import('@/v2/V2DailiantongStats.vue'),
          meta: { title: '代练通按日统计', permissions: nodeCodesFor('v2-dailiantong-stats') },
        },
        {
          path: 'dailiantong-templates',
          name: 'v2-dailiantong-templates',
          component: () => import('@/v2/V2DailiantongTemplates.vue'),
          meta: { title: '代练通订单要求模板', permissions: nodeCodesFor('v2-dailiantong-templates') },
        },
        // 「打手与客服报酬」v2（2026-11-06 财务族第二批）：照 island 页 `emp-emplist` 炭火重建
        // （`src/v2/V2Performance.vue`）；挂旧路由同节点权限门（some 语义，照 menu.ts 原形态）。
        {
          path: 'performance',
          name: 'v2-performance',
          component: () => import('@/v2/V2Performance.vue'),
          meta: { title: '打手与客服报酬', permissions: ['finance.payroll', 'order.list'] },
        },
        // 「结算记录」v2（同批）：照 island 自建页 `emp-billsummary`（按天聚合 + 来源分解）重建
        // （`src/v2/V2BillSummary.vue`）；零新增端点。
        {
          path: 'bill-summary',
          name: 'v2-bill-summary',
          component: () => import('@/v2/V2BillSummary.vue'),
          meta: { title: '结算记录', permissions: ['finance.settlement_records'] },
        },
        // 「经营报表」v2（同批）：照 island 页 `emp-dayoperation` 炭火化重建（§7 拍板）
        // （`src/v2/V2DayReport.vue`）；零新增端点。
        {
          path: 'day-report',
          name: 'v2-day-report',
          component: () => import('@/v2/V2DayReport.vue'),
          meta: { title: '经营报表', permissions: ['finance.support_statistics'] },
        },
        // 「在线结算明细」v2（同批）：恒空复刻页 → 诚实空态（`src/v2/V2OnlinePayDetail.vue`）。
        {
          path: 'online-pay-detail',
          name: 'v2-online-pay-detail',
          component: () => import('@/v2/V2OnlinePayDetail.vue'),
          meta: { title: '在线结算明细', permissions: ['finance.online_settlement_details'] },
        },
        // 「团团通资金明细与提现」v2（同批）：恒空复刻页 → 诚实空态（`src/v2/V2FundDetail.vue`）；
        // 菜单条目已在 v2 侧栏底部区，override 后点击进本页。
        {
          path: 'fund-detail',
          name: 'v2-fund-detail',
          component: () => import('@/v2/V2FundDetail.vue'),
          meta: { title: '团团通资金明细与提现', permissions: ['finance.platform_fund_details'] },
        },
        // 「系统维护」组 11 页 v2（2026-11-06 系统维护批次）：11 条全部接线，各页照旧 island 页
        // 炭火重建（规格与逐页口径见 `docs/plans/2026-11-06-system-maintenance-batch.md`）。
        // 11 页都挂旧路由同节点权限门（`config/routePermissions.ts` 的 v2 段同源）；layout 从
        // 父记录继承 blank。
        {
          path: 'role-authority',
          name: 'v2-role-authority',
          component: () => import('@/v2/V2RoleAuthority.vue'),
          meta: { title: '角色职位权限', permissions: ['system.role_authority'] },
        },
        {
          path: 'role-group',
          name: 'v2-role-group',
          component: () => import('@/v2/V2RoleGroup.vue'),
          meta: { title: '角色职位分组', permissions: ['system.position_groups'] },
        },
        {
          path: 'order-recognition',
          name: 'v2-order-recognition',
          component: () => import('@/v2/V2OrderRecognition.vue'),
          meta: { title: '录单识别设置', permissions: ['system.auto_discern_settings'] },
        },
        {
          path: 'order-sources',
          name: 'v2-order-sources',
          component: () => import('@/v2/V2OrderSources.vue'),
          meta: { title: '订单来源管理', permissions: ['system.sources'] },
        },
        {
          path: 'source-system',
          name: 'v2-source-system',
          component: () => import('@/v2/V2SourceSystem.vue'),
          meta: { title: '绑定上家系统设置', permissions: ['system.bind_upstream_system'] },
        },
        {
          path: 'alipay-auth',
          name: 'v2-alipay-auth',
          component: () => import('@/v2/V2AlipayAuth.vue'),
          meta: { title: '在线结算支付宝授权', permissions: ['finance.alipay_authorization'] },
        },
        {
          path: 'pdd-ship',
          name: 'v2-pdd-ship',
          component: () => import('@/v2/V2PddShip.vue'),
          meta: { title: '其它平台店铺对接', permissions: ['system.pinduoduo_settings'] },
        },
        {
          path: 'notices',
          name: 'v2-notices',
          component: () => import('@/v2/V2Notices.vue'),
          meta: { title: '系统内部公告管理', permissions: ['system.publish_notice'] },
        },
        {
          path: 'operation-log',
          name: 'v2-operation-log',
          component: () => import('@/v2/V2OperationLog.vue'),
          meta: { title: '系统操作日志管理', permissions: ['system.logs'] },
        },
        {
          path: 'wechat-group-push',
          name: 'v2-wechat-group-push',
          component: () => import('@/v2/V2WechatGroupPush.vue'),
          meta: { title: '自动发消息到微信群设置', permissions: ['system.order_push_group_settings'] },
        },
        {
          path: 'settings',
          name: 'v2-settings',
          component: () => import('@/v2/V2Settings.vue'),
          meta: { title: '系统各项设置管理', permissions: ['system.settings'] },
        },
        // 「数据概览」v2（2026-11-06 APP与小程序+问题反馈批）：照 island 自建页 `app-stats`
        // 炭火重建（`src/v2/V2AppStats.vue`）；挂旧路由同节点权限门；layout 从父记录继承 blank。
        {
          path: 'app-stats',
          name: 'v2-app-stats',
          component: () => import('@/v2/V2AppStats.vue'),
          meta: { title: '数据概览', permissions: ['finance.support_statistics'] },
        },
        // 「问题反馈」v2（同批；bottom 区条目）：提交区 + 列表区全功能（`src/v2/V2Feedback.vue`）。
        // ⚠️ 照旧路由口径**只挂一道门**（`home.feedback`）、⛔ 不挂 requiresAccountAdmin——
        // 挂了会把非管理员的提交入口也挡掉（旧路由同一条注释的口径）。
        {
          path: 'feedback',
          name: 'v2-feedback',
          component: () => import('@/v2/V2Feedback.vue'),
          meta: { title: '问题反馈', permissions: ['home.feedback'] },
        },
        // 「发布文章作品」v2（同批）：列表态 + 编辑态 + 手机预览（`src/v2/V2Articles.vue`）。
        {
          path: 'articles',
          name: 'v2-articles',
          component: () => import('@/v2/V2Articles.vue'),
          meta: { title: '发布文章作品', permissions: ['system.article_manage'] },
        },
        // 「开放平台」组 3 页 v2（2026-11-06 开放平台批）：性能分析 / 应用管理 / 打手对接，
        // 各页照旧 island 页炭火重建（计划 `docs/plans/2026-11-06-open-platform-batch.md`）。
        // 「性能分析」「应用管理」照旧路由**两道门**都挂（`requiresAccountAdmin` 在 v2 子树是
        // 首例，判据值 = `is_account_admin`，v2Routes.spec 有真守卫用例）；「打手对接」照旧路由
        // 裁定**只挂节点门**（挂管理员门会把打手挡在自己那页外面）。
        {
          path: 'perfanalysis',
          name: 'v2-perfanalysis',
          component: () => import('@/v2/V2PerfAnalysis.vue'),
          meta: { title: '性能分析', permissions: ['system.logs'], requiresAccountAdmin: true },
        },
        {
          path: 'open-apps',
          name: 'v2-open-apps',
          component: () => import('@/v2/V2OpenApps.vue'),
          meta: { title: '应用管理', permissions: ['system.pinduoduo_settings'], requiresAccountAdmin: true },
        },
        {
          path: 'taker-apps',
          name: 'v2-taker-apps',
          component: () => import('@/v2/V2TakerApps.vue'),
          meta: { title: '打手对接', permissions: ['system.allow_taker_open_api'] },
        },
        // 「第三方平台」组 4 页 v2（2026-11-06 第三方平台批）：各页照旧 island 页炭火重建
        // （计划 `docs/plans/2026-11-06-thirdparty-batch.md`）。权限照旧路由逐字（取值表见
        // `config/routePermissions.ts`）；「马上来查码对接」不设 v2 路由——v2 侧栏隐藏该条目、
        // 功能并入「自动上传群码」页的独立区块（`v2Menu.V2_HIDDEN_ROUTE_NAMES`）。
        // 「自动上传群码」v2 页 = 企微群码 + 马上来查码两页合一页，meta.title 取新名（顶栏页题读它）。
        {
          path: 'xgj-shop',
          name: 'v2-xgj-shop',
          component: () => import('@/v2/V2XgjShopBinding.vue'),
          meta: { title: '闲管家店铺对接', permissions: ['system.pinduoduo_settings'] },
        },
        {
          path: 'wecom-group-qr',
          name: 'v2-wecom-group-qr',
          component: () => import('@/v2/V2WecomGroupQr.vue'),
          meta: {
            title: '自动上传群码',
            permissions: ['system.pinduoduo_settings'],
            requiresAccountAdmin: true,
          },
        },
        {
          path: 'wxpusher-settings',
          name: 'v2-wxpusher-settings',
          component: () => import('@/v2/V2WxPusherSettings.vue'),
          meta: { title: '消息推送订阅', requiresAccountAdmin: true },
        },
        {
          path: 'internal-chat-settings',
          name: 'v2-internal-chat-settings',
          component: () => import('@/v2/V2InternalChatSettings.vue'),
          meta: { title: '内部沟通配置', requiresAccountAdmin: true },
        },
        {
          path: 'wechatmp-settings',
          name: 'v2-wechatmp-settings',
          component: () => import('@/v2/V2WechatmpSettings.vue'),
          meta: {
            title: '微信公众号设置',
            permissions: ['system.pinduoduo_settings'],
            requiresAccountAdmin: true,
          },
        },
        {
          path: 'qqbot-settings',
          name: 'v2-qqbot-settings',
          component: () => import('@/v2/V2QqbotSettings.vue'),
          meta: {
            title: 'QQ机器人设置',
            permissions: ['system.qqbot_settings'],
            requiresAccountAdmin: true,
          },
        },
        {
          // 「企微自建应用」v2（D-0379，2026-11-11）：炭火真页（`V2WecomSettings.vue`）。
          // **v2-only**：旧壳没有这条路由 ⇒ 菜单条目 `wecom-settings` 在**旧**侧栏置灰
          // （D-0344：旧前端不动）；**v2 侧栏可点**——`useV2Menu.withTarget` 按**解析后的
          // 目标**（本 name）判可点，与旧表无关（两张真值表各自判各的，E-0325）。
          // 两道门照 QQ 机器人页：① 自有节点 `system.wecom_settings`（能单独授权）；
          // ② `requiresAccountAdmin`（凭据页）。
          path: 'wecom-settings',
          name: 'v2-wecom-settings',
          component: () => import('@/v2/V2WecomSettings.vue'),
          meta: {
            title: '企微自建应用',
            permissions: ['system.wecom_settings'],
            requiresAccountAdmin: true,
          },
        },
      ],
    },
    // 映射表生成的 1:1 复刻路由（47 条 = 89 − 5 accessDenied − 37 条只被 REPLICA_SKIP 排除的；
    // 集合 42 条里另有 5 条与 accessDenied 重叠），跳过规则见文件头的 `REPLICA_SKIP`。
    ...replicaRoutes,
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

// 兼容旧入口。目标页面由下一个守卫做登录和权限检查。
router.beforeEach((to) => {
  const { name, query, hash } = to
  if (typeof __LEGACY_ROUTE_AUDIT__ !== 'undefined' && __LEGACY_ROUTE_AUDIT__) return
  if (typeof name !== 'string') return
  const target = V2_ROUTE_OVERRIDES[name]
  if (typeof target === 'string') return { name: target, query, hash }
})

installGuards(router)
export default router
