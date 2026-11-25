import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { installMGuards } from './router/guards'
import { MINE_PERFORMANCE_HASH, MINE_PERMISSIONS } from './tabs'

/**
 * 手机端路由表（spec §4.1 那张表的逐条落地）。base 用 `${import.meta.env.BASE_URL}m/`：与桌面同一份 Vite base（默认 '/'，挂子路径时自动跟随），手机端固定在它下面的 `m/`。
 * ⚠️ vue-router 的 `fullPath` **不含** base（`createCurrentLocation` 会 `stripBase`）⇒「站内 `/m/` 路径」的换算一律走 `utils/redirect.ts::mSitePath / mRoutePathOf`，别在这里手拼。dev 访问形态 `http://<vite 地址>/m/index.html`（dev 不做目录 index 改写；prod 由 nginx `location /m/` 兜住——差异登记在 spec §10，别当 bug 排）。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'm-login',
    component: () => import('./pages/LoginView.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/wxpusher/chat/:conversationId([1-9][0-9]{0,18})',
    name: 'm-wxpusher-chat',
    component: () => import('./pages/WxPusherChatPage.vue'),
  },
  {
    path: '/wxpusher/open',
    name: 'm-wxpusher-open',
    component: () => import('./pages/WxPusherOpenPage.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/wxpusher/hall-entry',
    name: 'm-wxpusher-hall-entry',
    component: () => import('./pages/WxPusherHallEntryView.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/wxpusher/return',
    name: 'm-wxpusher-return',
    component: () => import('./pages/WxPusherReturnPage.vue'),
    meta: { requiresAuth: false },
  },
  {
    // 自助注册公开页（spec §5，`docs/specs/2026-10-16-self-registration-links-design.md`）。⛔ **免登录**：注册的人还没有账号，
    // 守卫默认的 fail-closed「漏声明 = 要登录」会把他拦去登录页（业务目标等于没交付）。令牌从 query `?tk=` 读（页面自己读，路由不做校验——有效性判据在服务端）；注册成功跳 `/m/login?notice=self-registered`（横幅见 `LoginView.vue`）。
    // ⚠️ 守卫的「非打手弹回桌面」门在 `session.status === 'ready'` 才判：匿名访客照常进（与 `m-login` 同一处境）；已登录的**非打手**会被那道门带走——全 m 端的既有口径，本页不另开特例。
    path: '/register',
    name: 'm-self-register',
    component: () => import('./pages/SelfRegisterPage.vue'),
    meta: { requiresAuth: false },
  },
  {
    // 壳路由：顶栏 + 底部 Tabbar（spec §5.2）。hall / mine 是它的子路由。
    path: '/',
    component: () => import('./layouts/MobileTabLayout.vue'),
    children: [
      {
        // m-home 是**虚拟落地页**：守卫在进入前就重定向到第一个有权限的 tab（都无 → m-empty），
        // 见 `router/guards.ts`。组件只为满足 vue-router 的记录形状，永远不渲染。
        path: '',
        name: 'm-home',
        component: { name: 'MHomePlaceholder', render: () => null },
      },
      {
        path: 'hall',
        name: 'm-hall',
        component: () => import('./pages/HallPage.vue'),
        meta: { permissions: ['grab_hall.take_order'] },
      },
      {
        path: 'mine',
        name: 'm-mine',
        component: () => import('./pages/MinePage.vue'),
        meta: { permissions: MINE_PERMISSIONS },
      },
      {
        path: 'performance',
        name: 'm-performance',
        redirect: ({ query }) => ({ name: 'm-mine', query, hash: MINE_PERFORMANCE_HASH }),
      },
      {
        path: 'channel',
        name: 'm-channel',
        component: () => import('./pages/ChannelPage.vue'),
      },
      {
        // M3（spec §1）：「我的」聚合页（tabbar 第四格）。
        path: 'me',
        name: 'm-me',
        component: () => import('./pages/MePage.vue'),
        meta: { permissions: ['my_profile'] },
      },
    ],
  },
  {
    path: '/channel/articles/:articleId(\\d+)',
    name: 'm-channel-article',
    component: () => import('./pages/ChannelArticlePage.vue'),
  },
  {
    // M3（spec §2）：我的资料。后端 `/api/auth/profile` 只认登录（IsAuthenticated），
    // 路由门照桌面 `routePermissions` 的 `accounts-profile` = `my_profile` 同一颗。
    path: '/me/profile',
    name: 'm-me-profile',
    component: () => import('./pages/MeProfilePage.vue'),
    meta: { permissions: ['my_profile'] },
  },
  {
    // M3（spec §3）：修改密码（`POST /api/auth/password` 只认登录；门照桌面同页 = `my_profile`）。
    path: '/me/password',
    name: 'm-me-password',
    component: () => import('./pages/MePasswordPage.vue'),
    meta: { permissions: ['my_profile'] },
  },
  {
    path: '/me/notifications',
    name: 'm-me-notifications',
    component: () => import('./pages/MeWxPusherPage.vue'),
    meta: { permissions: ['my_profile'] },
  },
  {
    path: '/me/notifications/records',
    name: 'm-me-notification-inbox',
    component: () => import('./pages/MeNotificationInboxPage.vue'),
    meta: { permissions: ['my_profile'] },
  },
  {
    path: '/me/notifications/order',
    name: 'm-me-notifications-order',
    component: () => import('./pages/MeWxPusherPage.vue'),
    props: { orderDetails: true },
    meta: { permissions: ['my_profile'] },
  },
  {
    // M3（spec §4）：问题反馈。后端三端点都是「登录 + 节点 `home.feedback`」
    // （`feedback_views.py`，与桌面 `routePermissions['feedback']` 同一颗）。
    path: '/me/feedback',
    name: 'm-me-feedback',
    component: () => import('./pages/MeFeedbackPage.vue'),
    meta: { permissions: ['home.feedback'] },
  },
  {
    // 20 位形状闸（先例：桌面 `/orders/:order_no(\d{20})`）；页面内还有第二道
    // `m/utils/orderNoShape.ts` 的形状闸（不符 → 404 态页、零请求）。
    path: '/orders/:orderNo(\\d{20})',
    alias: '/wxpusher/orders/:orderNo(\\d{20})',
    name: 'm-order-detail',
    component: () => import('./pages/OrderDetailView.vue'),
    meta: { permissions: ['my_orders'] },
  },
  {
    // 订单截图（列表 / 上传 / 删除，一页承载）。门 = 桌面截图两颗入口按钮的**同一对节点**（`canNode` 是 any-of 语义：持任一即进）；页内再按各自动作节点 fail-closed 收窄——上传区 `order.upload_screenshot`、删除 `order.screenshot_manage`，没权限的那块不渲染，⛔ 不置灰。
    path: '/orders/:orderNo(\\d{20})/screenshots',
    name: 'm-order-screenshots',
    component: () => import('./pages/OrderScreenshotsPage.vue'),
    meta: { permissions: ['order.view_screenshot', 'order.upload_screenshot'] },
  },
  {
    path: '/orders/:orderNo(\\d{20})/screenshots/browser',
    name: 'm-order-screenshot-browser',
    component: () => import('./pages/OrderScreenshotBrowserPage.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/empty',
    name: 'm-empty',
    component: () => import('./pages/EmptyView.vue'),
  },
  {
    // 兜底：未知路径一律去空态页（不渲染桌面版 404）。
    path: '/:pathMatch(.*)*',
    name: 'm-not-found',
    redirect: { name: 'm-empty' },
  },
]

const mRouter = createRouter({
  history: createWebHistory(`${import.meta.env.BASE_URL}m/`),
  routes,
})

installMGuards(mRouter)
export default mRouter
