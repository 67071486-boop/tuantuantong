// @vitest-environment jsdom
// ↑ 真路由表用 `createWebHistory(import.meta.env.BASE_URL)`（内部解构 `window`），node 环境下 import 即抛。
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** `/v2` 接线的**机器判据**（实施计划 §3/§5.1，`docs/plans/2026-10-30-frontend-v2-shell-dashboard.md`）：1. **旧路由保护网**：下面的 `LEGACY_ROUTES_SNAPSHOT` 是 `/v2` 落地**前一刻**的全量`name → path` 快照（2026-10-30 在 `057b4339` 上实测导出，91 条）。本用例把「现路由表 − v2 子树」与快照做**恰好集合**对拍——本线承诺「老的前端一行不改」，任何人再动旧路由（加 / 删 / 改 path / 改 name）都会在这里红。⚠️ 这张快照**只增不改**口径不适用：它的语义就是「冻结」，将来给旧前端加路由时**有意**更新快照并说明（与 `replicaRoutes.spec.ts` 的恰好集合同一维护方式）。2. `/v2` 存在且形状正确（父 `v2` → CarbonShell，子 `''` → `v2-home` → V2Dashboard）。
 * 3. **认证同旧口径**：`/v2` 不挂 `meta.permissions` / `requiresAccountAdmin`——守卫的`requiresAuth` 默认 true = 登录即可，与白名单里的 `home` 同一档。子页有自己的业务权限。 */
vi.mock('@/components/LayuiIslandHost.vue', () => ({ default: { __view: 'LayuiIslandHost' } }))
vi.mock('@/views/kefu/KefuConsoleView.vue', () => ({ default: { __view: 'KefuConsoleView' } }))
vi.mock('@/v2/CarbonShell.vue', () => ({ default: { __view: 'CarbonShell' } }))
vi.mock('@/v2/V2Dashboard.vue', () => ({ default: { __view: 'V2Dashboard' } }))
vi.mock('@/v2/V2MyOrders.vue', () => ({ default: { __view: 'V2MyOrders' } }))
vi.mock('@/v2/V2Hall.vue', () => ({ default: { __view: 'V2Hall' } }))
vi.mock('@/v2/V2Income.vue', () => ({ default: { __view: 'V2Income' } }))
vi.mock('@/v2/V2Settlement.vue', () => ({ default: { __view: 'V2Settlement' } }))
vi.mock('@/v2/V2Profile.vue', () => ({ default: { __view: 'V2Profile' } }))
vi.mock('@/v2/V2Messages.vue', () => ({ default: { __view: 'V2Messages' } }))
vi.mock('@/v2/V2InternalChat.vue', () => ({ default: { __view: 'V2InternalChat' } }))
vi.mock('@/v2/V2WecomArchiveSearch.vue', () => ({ default: { __view: 'V2WecomArchiveSearch' } }))
vi.mock('@/v2/V2OrderAudit.vue', () => ({ default: { __view: 'V2OrderAudit' } }))
vi.mock('@/v2/V2OrderNew.vue', () => ({ default: { __view: 'V2OrderNew' } }))
vi.mock('@/v2/V2OrderManage.vue', () => ({ default: { __view: 'V2OrderManage' } }))
vi.mock('@/v2/V2PlatformOrders.vue', () => ({ default: { __view: 'V2PlatformOrders' } }))
vi.mock('@/v2/V2OrderRecycle.vue', () => ({ default: { __view: 'V2OrderRecycle' } }))
vi.mock('@/v2/V2UserNew.vue', () => ({ default: { __view: 'V2UserNew' } }))
vi.mock('@/v2/V2OrderActionLog.vue', () => ({ default: { __view: 'V2OrderActionLog' } }))
vi.mock('@/v2/V2OrderShipLog.vue', () => ({ default: { __view: 'V2OrderShipLog' } }))
vi.mock('@/v2/V2UpstreamPriceLog.vue', () => ({ default: { __view: 'V2UpstreamPriceLog' } }))
vi.mock('@/v2/V2UserList.vue', () => ({ default: { __view: 'V2UserList' } }))
vi.mock('@/v2/V2RealName.vue', () => ({ default: { __view: 'V2RealName' } }))
vi.mock('@/v2/V2LoginRestrict.vue', () => ({ default: { __view: 'V2LoginRestrict' } }))
vi.mock('@/v2/V2Blacklist.vue', () => ({ default: { __view: 'V2Blacklist' } }))
vi.mock('@/v2/V2Contract.vue', () => ({ default: { __view: 'V2Contract' } }))
vi.mock('@/v2/V2XgjReceipts.vue', () => ({ default: { __view: 'V2XgjReceipts' } }))
vi.mock('@/v2/V2Customers.vue', () => ({ default: { __view: 'V2Customers' } }))
vi.mock('@/v2/V2CustomerAnalysis.vue', () => ({ default: { __view: 'V2CustomerAnalysis' } }))
vi.mock('@/v2/V2CustomerBlacklist.vue', () => ({ default: { __view: 'V2CustomerBlacklist' } }))
vi.mock('@/v2/V2GameManage.vue', () => ({ default: { __view: 'V2GameManage' } }))
vi.mock('@/v2/V2GameProjects.vue', () => ({ default: { __view: 'V2GameProjects' } }))
vi.mock('@/v2/V2PlatformIntegration.vue', () => ({ default: { __view: 'V2PlatformIntegration' } }))
vi.mock('@/v2/V2PlatformAutoReply.vue', () => ({ default: { __view: 'V2PlatformAutoReply' } }))
vi.mock('@/v2/V2DailiantongStats.vue', () => ({ default: { __view: 'V2DailiantongStats' } }))
vi.mock('@/v2/V2DailiantongTemplates.vue', () => ({ default: { __view: 'V2DailiantongTemplates' } }))
vi.mock('@/v2/V2Performance.vue', () => ({ default: { __view: 'V2Performance' } }))
vi.mock('@/v2/V2BillSummary.vue', () => ({ default: { __view: 'V2BillSummary' } }))
vi.mock('@/v2/V2DayReport.vue', () => ({ default: { __view: 'V2DayReport' } }))
vi.mock('@/v2/V2OnlinePayDetail.vue', () => ({ default: { __view: 'V2OnlinePayDetail' } }))
vi.mock('@/v2/V2FundDetail.vue', () => ({ default: { __view: 'V2FundDetail' } }))
vi.mock('@/v2/V2RoleAuthority.vue', () => ({ default: { __view: 'V2RoleAuthority' } }))
vi.mock('@/v2/V2RoleGroup.vue', () => ({ default: { __view: 'V2RoleGroup' } }))
vi.mock('@/v2/V2OrderRecognition.vue', () => ({ default: { __view: 'V2OrderRecognition' } }))
vi.mock('@/v2/V2OrderSources.vue', () => ({ default: { __view: 'V2OrderSources' } }))
vi.mock('@/v2/V2SourceSystem.vue', () => ({ default: { __view: 'V2SourceSystem' } }))
vi.mock('@/v2/V2AlipayAuth.vue', () => ({ default: { __view: 'V2AlipayAuth' } }))
vi.mock('@/v2/V2PddShip.vue', () => ({ default: { __view: 'V2PddShip' } }))
vi.mock('@/v2/V2Notices.vue', () => ({ default: { __view: 'V2Notices' } }))
vi.mock('@/v2/V2OperationLog.vue', () => ({ default: { __view: 'V2OperationLog' } }))
vi.mock('@/v2/V2WechatGroupPush.vue', () => ({ default: { __view: 'V2WechatGroupPush' } }))
vi.mock('@/v2/V2Settings.vue', () => ({ default: { __view: 'V2Settings' } }))
// APP与小程序+问题反馈批（2026-11-06）+3：每条接线都有一条组件 mock
// （缺了 A 波 spec 就依赖 B 波的 V2Articles.vue 真文件）。
vi.mock('@/v2/V2AppStats.vue', () => ({ default: { __view: 'V2AppStats' } }))
vi.mock('@/v2/V2Feedback.vue', () => ({ default: { __view: 'V2Feedback' } }))
vi.mock('@/v2/V2Articles.vue', () => ({ default: { __view: 'V2Articles' } }))
// 开放平台批（2026-11-06）+3：每条接线都有一条组件 mock（与上面同一纪律）。
vi.mock('@/v2/V2PerfAnalysis.vue', () => ({ default: { __view: 'V2PerfAnalysis' } }))
vi.mock('@/v2/V2OpenApps.vue', () => ({ default: { __view: 'V2OpenApps' } }))
vi.mock('@/v2/V2TakerApps.vue', () => ({ default: { __view: 'V2TakerApps' } }))
// 第三方平台批（2026-11-06）+4：同上（A 波只落占位壳，B 波填实真页时本文件不依赖真文件）。
vi.mock('@/v2/V2XgjShopBinding.vue', () => ({ default: { __view: 'V2XgjShopBinding' } }))
vi.mock('@/v2/V2WecomGroupQr.vue', () => ({ default: { __view: 'V2WecomGroupQr' } }))
vi.mock('@/v2/V2WechatmpSettings.vue', () => ({ default: { __view: 'V2WechatmpSettings' } }))
vi.mock('@/v2/V2QqbotSettings.vue', () => ({ default: { __view: 'V2QqbotSettings' } }))
vi.mock('@/v2/V2WecomSettings.vue', () => ({ default: { __view: 'V2WecomSettings' } }))

// 真守卫用例要驱动 `installGuards`（守卫读会话 store + `fetchMe` + `notifyError`）⇒ 按 guards.spec
// 同款 mock；其余用例只 `router.resolve`、不触发守卫，不受影响。
vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn(), login: vi.fn() }
})
vi.mock('@/api/notify', () => ({ notifyError: vi.fn() }))
vi.mock('@/api/site', () => ({ fetchSiteName: vi.fn().mockResolvedValue('示例工作室甲') }))

import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { fetchMe, login } from '@/api/auth'
import { AuthExpiredError } from '@/api/errors'
import { notifyError } from '@/api/notify'
import type { User } from '@/api/auth'
import router from '@/router'
import LoginView from '@/views/LoginView.vue'
import { V2_ROUTE_OVERRIDES } from '../v2Menu'

/** 取某个 path 命中的**最内层**路由记录的视图身份（惰性导入函数调用后的 default）。 */
async function viewOf(path: string): Promise<unknown> {
  const matched = router.resolve(path).matched
  const record = matched[matched.length - 1]
  const component = record?.components?.default
  expect(typeof component, `${path} 的 component 应当是惰性导入函数`).toBe('function')
  const mod = await (component as () => Promise<{ default: unknown }>)()
  return mod.default
}

/** `/v2` 落地**前一刻**的旧路由全量快照（`name → path`，按 name 排序；初版 /v2 落地前导出 91 条，
 * 现状 80 条——2026-10-31 马上来查码线 D-0348 有意 +1；2026-11-10 第二批客户维护删两页
 * 有意 −2（见快照内 ⭐ 注）；导出方式见文件头）。 */
const LEGACY_ROUTES_SNAPSHOT: Record<string, string> = {
  'accounts-game': '/accounts/game',
  'accounts-game-import': '/accounts/game/import',
  'accounts-game-mine': '/accounts/game/mine',
  'accounts-game-new': '/accounts/game/new',
  'accounts-game-sell-preview': '/accounts/game/sell-preview',
  'accounts-messages': '/accounts/messages',
  'accounts-profile': '/accounts/profile',
  'agents-alipay-auth': '/agents/alipay-auth',
  'agents-day-report': '/agents/day-report',
  'agents-fund-detail': '/agents/fund-detail',
  'agents-my-income': '/agents/my-income',
  'agents-my-settlement': '/agents/my-settlement',
  'agents-online-pay-detail': '/agents/online-pay-detail',
  'agents-performance': '/agents/performance',
  'agents-salesman-stats': '/agents/salesman-stats',
  'agents-settlement': '/agents/settlement',
  'agents-taker-wallet': '/agents/taker-wallet',
  'agents-wallet-adjust': '/agents/wallet-adjust',
  'agents-withdraw-review': '/agents/withdraw-review',
  'app-mp-articles': '/app-mp/articles',
  'app-stats': '/business/app-stats',
  customers: '/customers',
  'customers-analysis': '/customers/analysis',
  'customers-blacklist': '/customers/blacklist',
  // ⭐（2026-11-10 第二批）`customers-balance-log` / `customers-levels` 两行删除：
  // 业务方裁定两页整个去掉（REPLICA_SKIP 留痕），路由不再生成——
  // 快照的「冻结」语义允许**有意**删除时同一次更新并说明（见文件头）。
  'dailiantong-order-templates': '/system/dailiantong-order-templates',
  'dailiantong-stats': '/system/dailiantong-stats',
  feedback: '/feedback',
  'game-config-games': '/game-config/games',
  'game-config-projects': '/game-config/projects',
  'groups-legacy': '/groups/legacy',
  'groups-mine': '/groups/mine',
  'groups-new': '/groups/new',
  'groups-public-tasks': '/groups/public-tasks',
  'groups-v2021': '/groups/v2021',
  'groups-v2025': '/groups/v2025',
  home: '/',
  'kefu-console': '/kefu',
  login: '/login',
  'not-found': '/:pathMatch(.*)*',
  'open-apps': '/open-platform/apps',
  'order-detail': '/orders/:order_no(\\d{20})',
  'order-new': '/orders/new',
  orders: '/orders',
  'orders-inside-hall': '/orders/inside-hall',
  'orders-mine': '/orders/mine',
  'orders-platform-action-log': '/orders/platform-action-log',
  'orders-platform-core': '/orders/platform-core',
  'orders-recycle': '/orders/recycle',
  'orders-source-price-log': '/orders/source-price-log',
  'orders-taobao-ship': '/orders/taobao-ship',
  'qqbot-settings': '/thirdparty/qqbot-settings',
  'self-registration': '/self-registration',
  'system-auto-price-adjust': '/system/auto-price-adjust',
  'system-blacklist': '/system-users/blacklist',
  'system-contract': '/system-users/contract',
  'system-login-restrict': '/system-users/login-restrict',
  'system-notices': '/system/notices',
  'system-operation-log': '/system/operation-log',
  // ⭐（D-0348）【第三方平台】的「马上来查码对接」
  // （自有功能自建路由 `router/index.ts`，D-0348 有意动旧路由表）⇒ 快照**有意** +1（81 → 82，
  // 维护方式见文件头第 1 条「有意更新快照并说明」）。
  'system-order-qr-fetch': '/thirdparty/order-qr-fetch',
  'system-order-recognition': '/system/order-recognition',
  'system-order-sources': '/system/order-sources',
  'system-pdd-ship': '/system/pdd-ship',
  'system-perfanalysis': '/system/performance',
  'system-platform-auto-reply': '/system/platform-auto-reply',
  'system-platform-integration': '/system/platform-integration',
  'system-platform-push-order': '/system/platform-push-order',
  'system-real-name': '/system-users/real-name',
  'system-role-authority': '/system/role-authority',
  'system-role-group': '/system/role-group',
  'system-settings': '/system/settings',
  'system-source-system': '/system/source-system',
  'system-user-edit': '/system-users/:id/edit',
  'system-user-new': '/system-users/new',
  'system-users': '/system-users',
  'system-wechat-group-push': '/system/wechat-group-push',
  'system-wechatmp-settings': '/thirdparty/wechatmp-settings',
  'taker-apps': '/open-platform/taker-apps',
  'thirdparty-xgj-shop': '/thirdparty/xgj-shops',
  'wecom-client-lab': '/wecom-client-lab',
  'wecom-group-qr': '/thirdparty/wecom-group-qr',
  'xgj-receipts': '/finance/xgj-receipts',
}

/** 本线新增的路由 name（对拍旧快照时排除；超出这个集合 = v2 子树被塞了新路由，也要红）。
 * ⭐（D-0352）+1：「内部抢单大厅」。⭐（D-0353）+4：「我的相关」组 4 页 v2 化。
 * ⭐（D-0360）+1：「订单时间线」（URL 直达不进菜单）。⭐（D-0355）+1：「录入订单」。
 * ⭐（2026-11-04/05 并行线）+3：「管理订单」「第三方平台订单管理」「找回删除的订单」。
 * ⭐（2026-11-05）+1：「新开系统账号」。
 * ⭐（2026-11-05 系统人员批次）+5：「人员管理」「人员实名记录与配置」「登录限制设置」
 * 「人员黑名单」「员工电子合同管理」。
 * ⭐（2026-11-05 本分支）+6：「游戏维护」「发平台设置」两组 6 页。
 * 财务族包含「闲管家收款明细」。
 * ⭐（2026-11-06 财务族第二批）+5：打手与客服报酬 / 结算记录 / 经营报表 / 在线结算明细 /
 * 团团通资金明细与提现（财务相关组 8 页全部 v2 化收尾）。
 * ⭐（2026-11-06 系统维护批次）+11：「系统维护」组 11 页全接线（角色职位权限 / 角色职位分组 /
 * 录单识别设置 / 订单来源管理 / 绑定上家系统设置 / 在线结算支付宝授权 / 其它平台店铺对接 /
 * 系统内部公告管理 / 系统操作日志管理 / 自动发消息到微信群设置 / 系统各项设置管理）。
 * ⭐（同批代登记在途线欠账）+3：并行线已落 router 但未登集合的三张记录页
 * 「交易订单操作记录」「订单发货记录」「上家订单改价记录」（STATUS 有代账登记）。
 * ⭐（2026-11-06 APP与小程序+问题反馈批）+3：「数据概览」「问题反馈」「发布文章作品」。
 * ⭐（2026-11-06 开放平台批）+3：「性能分析」「应用管理」「打手对接」。
 * ⭐（2026-11-06 第三方平台批）+4：「闲管家店铺对接」「自动上传群码」（合并页）
 * 「微信公众号设置」「QQ机器人设置」（「马上来查码对接」v2 隐藏、不设路由）。
 * ⭐（2026-11-10 客户线第三批）+1：「客户分析报表」。
 * ⭐（2026-11-11 客户线第七批）+1：「黑名单管理」v2 占位（不再跳 v1 旧壳）。 */
const V2_ROUTE_NAMES = new Set([
  'v2',
  'v2-home',
  'v2-my-orders',
  'v2-hall',
  'v2-income',
  'v2-settlement',
  'v2-profile',
  'v2-messages',
  'v2-internal-chat',
  'v2-order-detail-preview',
  'v2-approvals',
  'v2-wecom-archive',
  'v2-order-audit',
  'v2-order-new',
  'v2-order-manage',
  'v2-platform-orders',
  'v2-order-recycle',
  'v2-user-new',
  'v2-order-action-log',
  'v2-ship-log',
  'v2-upstream-price-log',
  'v2-user-list',
  'v2-real-name',
  'v2-login-restrict',
  'v2-blacklist',
  'v2-contract',
  'v2-xgj-receipts',
  // ⭐ 2026-11-10 客户线：会员管理（炭火真页）。
  'v2-customers',
  // ⭐ 2026-11-10 客户线第三批：客户分析报表（炭火真页）。
  'v2-customer-analysis',
  // ⭐ 2026-11-11 客户线第七批：黑名单管理（炭火占位页，不再跳 v1 旧壳）。
  'v2-customer-blacklist',
  'v2-game-manage',
  'v2-game-projects',
  'v2-platform-integration',
  'v2-platform-auto-reply',
  'v2-dailiantong-stats',
  'v2-dailiantong-templates',
  'v2-performance',
  'v2-bill-summary',
  'v2-day-report',
  'v2-online-pay-detail',
  'v2-fund-detail',
  'v2-role-authority',
  'v2-role-group',
  'v2-order-recognition',
  'v2-order-sources',
  'v2-source-system',
  'v2-alipay-auth',
  'v2-pdd-ship',
  'v2-notices',
  'v2-operation-log',
  'v2-wechat-group-push',
  'v2-settings',
  'v2-app-stats',
  'v2-feedback',
  'v2-articles',
  'v2-perfanalysis',
  'v2-open-apps',
  'v2-taker-apps',
  'v2-xgj-shop',
  'v2-wecom-group-qr',
  'v2-wxpusher-settings',
  'v2-internal-chat-settings',
  'v2-internal-chat-visitors',
  'v2-internal-chat-window',
  'v2-wechatmp-settings',
  'v2-qqbot-settings',
  'v2-wecom-settings',
])

describe('/v2 接线：旧路由保护网（「老的前端不动」的机器判据）', () => {
  it('现路由表（去掉 v2 子树）与落地前快照恰好集合相等', () => {
    const current: Record<string, string> = {}
    for (const record of router.getRoutes()) {
      const name = String(record.name ?? '')
      if (V2_ROUTE_NAMES.has(name) || name === 'visitor-chat') continue
      current[name] = record.path
    }
    expect(
      current,
      '旧路由表与 /v2 落地前快照不一致——本线承诺旧前端一行不改；改了旧路由就要在这里说清',
    ).toEqual(LEGACY_ROUTES_SNAPSHOT)
  })

  it('阳性对照：快照非空且 v2 路由确实不在快照里（防空集恒真 / 防 v2 混入旧集合）', () => {
    expect(Object.keys(LEGACY_ROUTES_SNAPSHOT).length).toBeGreaterThan(0)
    expect(LEGACY_ROUTES_SNAPSHOT['v2']).toBeUndefined()
    expect(LEGACY_ROUTES_SNAPSHOT['v2-home']).toBeUndefined()
    expect(LEGACY_ROUTES_SNAPSHOT['v2-my-orders']).toBeUndefined()
    expect(LEGACY_ROUTES_SNAPSHOT['v2-hall']).toBeUndefined()
    expect(router.hasRoute('home'), '旧首页路由必须还在').toBe(true)
  })
})

describe('/v2 接线：路由形状', () => {
  it('/v2 → v2-home → V2Dashboard，父记录是 CarbonShell', async () => {
    const resolved = router.resolve('/v2')
    expect(resolved.name).toBe('v2-home')
    expect(resolved.meta.title).toBe('后台首页 v2')
    // 嵌套：matched[0] = 父（CarbonShell），最内层 = 子（V2Dashboard）。
    expect(await viewOf('/v2')).toEqual({ __view: 'V2Dashboard' })
    const parent = router.resolve('/v2').matched[0]
    const parentComponent = parent?.components?.default
    expect(typeof parentComponent).toBe('function')
    const parentMod = await (parentComponent as () => Promise<{ default: unknown }>)()
    expect(parentMod.default).toEqual({ __view: 'CarbonShell' })
  })

  it('/v2/my-orders → v2-my-orders → V2MyOrders（D-0346 指令④「指派给我的订单」v2 页）', async () => {
    const resolved = router.resolve('/v2/my-orders')
    expect(resolved.name).toBe('v2-my-orders')
    expect(resolved.meta.title).toBe('指派给我的订单')
    expect(await viewOf('/v2/my-orders')).toEqual({ __view: 'V2MyOrders' })
  })

  it('/v2/order-audit → v2-order-audit → V2OrderAudit（D-0360「订单时间线」，URL 直达不进菜单）', async () => {
    const resolved = router.resolve('/v2/order-audit')
    expect(resolved.name).toBe('v2-order-audit')
    expect(resolved.meta.title).toBe('订单时间线')
    expect(await viewOf('/v2/order-audit')).toEqual({ __view: 'V2OrderAudit' })
  })

  it('/v2/hall → v2-hall → V2Hall（D-0352「内部抢单大厅」v2 页）', async () => {
    const resolved = router.resolve('/v2/hall')
    expect(resolved.name).toBe('v2-hall')
    expect(resolved.meta.title).toBe('内部抢单大厅')
    expect(await viewOf('/v2/hall')).toEqual({ __view: 'V2Hall' })
  })

  it.each([
    ['/v2/income', 'v2-income', '我的订单收入', 'V2Income'],
    ['/v2/settlement', 'v2-settlement', '在线结算记录', 'V2Settlement'],
    ['/v2/profile', 'v2-profile', '我的资料', 'V2Profile'],
    ['/v2/messages', 'v2-messages', '消息通知', 'V2Messages'],
    ['/v2/order-new', 'v2-order-new', '录入订单', 'V2OrderNew'],
    ['/v2/order-manage', 'v2-order-manage', '管理订单', 'V2OrderManage'],
    ['/v2/platform-orders', 'v2-platform-orders', '第三方平台订单管理', 'V2PlatformOrders'],
    ['/v2/order-recycle', 'v2-order-recycle', '找回删除的订单', 'V2OrderRecycle'],
    ['/v2/game-manage', 'v2-game-manage', '游戏管理', 'V2GameManage'],
    ['/v2/game-projects', 'v2-game-projects', '订单项目分类与常用项目', 'V2GameProjects'],
    ['/v2/platform-integration', 'v2-platform-integration', '交易平台对接设置', 'V2PlatformIntegration'],
    ['/v2/platform-auto-reply', 'v2-platform-auto-reply', '交易平台自动回复', 'V2PlatformAutoReply'],
    ['/v2/dailiantong-stats', 'v2-dailiantong-stats', '代练通按日统计', 'V2DailiantongStats'],
    ['/v2/dailiantong-templates', 'v2-dailiantong-templates', '代练通订单要求模板', 'V2DailiantongTemplates'],
  ] as const)(
    '%s → %s → %s（「我的相关」4 页 + 录入订单 + 管理订单族 3 页 + 游戏维护/发平台设置 6 页）',
    async (path, name, title, view) => {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.title).toBe(title)
      expect(await viewOf(path)).toEqual({ __view: view })
    },
  )

  it.each([
    ['/v2/xgj-receipts', 'v2-xgj-receipts', '闲管家收款明细', 'V2XgjReceipts', ['finance.xgj_receipt_detail']],
    ['/v2/performance', 'v2-performance', '打手与客服报酬', 'V2Performance', ['finance.payroll', 'order.list']],
    ['/v2/bill-summary', 'v2-bill-summary', '结算记录', 'V2BillSummary', ['finance.settlement_records']],
    ['/v2/day-report', 'v2-day-report', '经营报表', 'V2DayReport', ['finance.support_statistics']],
    ['/v2/online-pay-detail', 'v2-online-pay-detail', '在线结算明细', 'V2OnlinePayDetail', ['finance.online_settlement_details']],
    ['/v2/fund-detail', 'v2-fund-detail', '团团通资金明细与提现', 'V2FundDetail', ['finance.platform_fund_details']],
    ['/v2/role-authority', 'v2-role-authority', '角色职位权限', 'V2RoleAuthority', ['system.role_authority']],
    ['/v2/role-group', 'v2-role-group', '角色职位分组', 'V2RoleGroup', ['system.position_groups']],
    ['/v2/order-recognition', 'v2-order-recognition', '录单识别设置', 'V2OrderRecognition', ['system.auto_discern_settings']],
    ['/v2/order-sources', 'v2-order-sources', '订单来源管理', 'V2OrderSources', ['system.sources']],
    ['/v2/source-system', 'v2-source-system', '绑定上家系统设置', 'V2SourceSystem', ['system.bind_upstream_system']],
    ['/v2/alipay-auth', 'v2-alipay-auth', '在线结算支付宝授权', 'V2AlipayAuth', ['finance.alipay_authorization']],
    ['/v2/pdd-ship', 'v2-pdd-ship', '其它平台店铺对接', 'V2PddShip', ['system.pinduoduo_settings']],
    ['/v2/notices', 'v2-notices', '系统内部公告管理', 'V2Notices', ['system.publish_notice']],
    ['/v2/operation-log', 'v2-operation-log', '系统操作日志管理', 'V2OperationLog', ['system.logs']],
    ['/v2/wechat-group-push', 'v2-wechat-group-push', '自动发消息到微信群设置', 'V2WechatGroupPush', ['system.order_push_group_settings']],
    ['/v2/settings', 'v2-settings', '系统各项设置管理', 'V2Settings', ['system.settings']],
    ['/v2/app-stats', 'v2-app-stats', '数据概览', 'V2AppStats', ['finance.support_statistics']],
    ['/v2/feedback', 'v2-feedback', '问题反馈', 'V2Feedback', ['home.feedback']],
    ['/v2/articles', 'v2-articles', '发布文章作品', 'V2Articles', ['system.article_manage']],
    // 2026-11-10 客户线：会员管理（炭火真页；后端同线新增两个只读端点）。
    // ⭐ 2026-11-11 第五批：两节点任一即进（自有限制节点 member_list_self_only）。
    ['/v2/customers', 'v2-customers', '客户管理', 'V2Customers', ['customer.member_list', 'customer.member_list_self_only']],
    // 2026-11-10 客户线第三批：客户分析报表（同节点门 = 旧路由 customers-analysis 的声明；
    // 后端 analysis 端点的闸是 customer.member_list——两点都在冻结目录，实际只有管理员能过）。
    ['/v2/customer-analysis', 'v2-customer-analysis', '客户分析报表', 'V2CustomerAnalysis', ['customer.member_analysis_report']],
    // 2026-11-11 客户线第七批：黑名单管理 v2 占位（同节点门 = 旧路由 customers-blacklist）。
    ['/v2/customer-blacklist', 'v2-customer-blacklist', '黑名单管理', 'V2CustomerBlacklist', ['customer.blacklist']],
  ] as const)(
    '%s → %s → %s（财务族两批 + 系统维护组 11 页 + APP与小程序/问题反馈批 3 页 + 客户线 1 页；挂旧路由同节点权限门，⛔ 不进「不挂 permissions」名单）',
    async (path, name, title, view, permissions) => {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.title).toBe(title)
      expect(await viewOf(path)).toEqual({ __view: view })
      const child = router.getRoutes().find((r) => r.name === name)
      expect(child).toBeDefined()
      expect(child!.meta.permissions).toEqual(permissions)
      expect(child!.meta.requiresAccountAdmin).toBeUndefined()
    },
  )

  // 开放平台批（2026-11-06）另起新块：现有块共享 `requiresAccountAdmin toBeUndefined`
  // 断言，v2 子树首例带此门的两条塞进去必红 ⇒ 形态照 router/__tests__/routes.spec.ts:160-206
  // 的字面钉法另立一块（permissions 字面值 + 逐行钉管理员门）。
  it.each([
    ['/v2/perfanalysis', 'v2-perfanalysis', '性能分析', 'V2PerfAnalysis', ['system.logs'], true],
    ['/v2/open-apps', 'v2-open-apps', '应用管理', 'V2OpenApps', ['system.pinduoduo_settings'], true],
    ['/v2/taker-apps', 'v2-taker-apps', '打手对接', 'V2TakerApps', ['system.allow_taker_open_api'], undefined],
  ] as const)(
    '%s → %s → %s（开放平台批 3 页；挂旧路由同节点权限门；前两条 requiresAccountAdmin=true 是 v2 子树首例，第三条照旧路由裁定不挂——挂了打手会被自己那道门挡住）',
    async (path, name, title, view, permissions, adminRequired) => {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.title).toBe(title)
      expect(await viewOf(path)).toEqual({ __view: view })
      const child = router.getRoutes().find((r) => r.name === name)
      expect(child).toBeDefined()
      expect(child!.meta.permissions).toEqual(permissions)
      // 无条件断言（vitest/no-conditional-expect）：undefined 行钉「不挂」，true 行钉「挂上」。
      expect(child!.meta.requiresAccountAdmin).toBe(adminRequired === undefined ? undefined : true)
    },
  )

  // 第三方平台批（2026-11-06）另起新块：现有块共享 `requiresAccountAdmin toBeUndefined`
  // 断言，本批三条带 true 不能进现有块（开放平台批同一条理由）；闲管家照旧路由裁定**不带**
  // 管理员门（持节点的非管理员要能进），断言形态照 router/__tests__/routes.spec.ts:160-206
  // 的字面钉法。群码页 meta.title 取合并后的新名「自动上传群码」（顶栏页题读 meta.title）。
  it.each([
    ['/v2/xgj-shop', 'v2-xgj-shop', '闲管家店铺对接', 'V2XgjShopBinding', ['system.pinduoduo_settings'], undefined],
    ['/v2/wecom-group-qr', 'v2-wecom-group-qr', '自动上传群码', 'V2WecomGroupQr', ['system.pinduoduo_settings'], true],
    ['/v2/wechatmp-settings', 'v2-wechatmp-settings', '微信公众号设置', 'V2WechatmpSettings', ['system.pinduoduo_settings'], true],
    ['/v2/qqbot-settings', 'v2-qqbot-settings', 'QQ机器人设置', 'V2QqbotSettings', ['system.qqbot_settings'], true],
  ] as const)(
    '%s → %s → %s（第三方平台批 4 页；挂旧路由同节点权限门；后三条 requiresAccountAdmin=true 照旧路由，闲管家不带）',
    async (path, name, title, view, permissions, adminRequired) => {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.title).toBe(title)
      expect(await viewOf(path)).toEqual({ __view: view })
      const child = router.getRoutes().find((r) => r.name === name)
      expect(child).toBeDefined()
      expect(child!.meta.permissions).toEqual(permissions)
      // 无条件断言（vitest/no-conditional-expect）：undefined 行钉「不挂」，true 行钉「挂上」。
      expect(child!.meta.requiresAccountAdmin).toBe(adminRequired === undefined ? undefined : true)
    },
  )

  // ⭐ 企微自建应用（D-0379，v2-only）：独立 it.each 块——两道门齐挂（自有节点 + 管理员门）。
  it.each([
    ['/v2/wecom-settings', 'v2-wecom-settings', '企微自建应用', 'V2WecomSettings', ['system.wecom_settings'], true],
  ] as const)(
    '%s → %s → %s（企微自建应用线；自有节点 system.wecom_settings + requiresAccountAdmin=true）',
    async (path, name, title, view, permissions, adminRequired) => {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.title).toBe(title)
      expect(await viewOf(path)).toEqual({ __view: view })
      const child = router.getRoutes().find((r) => r.name === name)
      expect(child).toBeDefined()
      expect(child!.meta.permissions).toEqual(permissions)
      expect(child!.meta.requiresAccountAdmin).toBe(adminRequired === undefined ? undefined : true)
    },
  )

  it('独立企微聊天搜索挂在 v2 下，具体访问权限交给服务端核对', async () => {
    const path = '/v2/wecom-archive'
    const matched = router.resolve(path)
    expect(matched.name).toBe('v2-wecom-archive')
    expect(matched.meta.requiresAuth).not.toBe(false)
    expect(await viewOf(path)).toEqual({ __view: 'V2WecomArchiveSearch' })
  })

  it('父记录 meta.layout = blank（App.vue 的壳层开关读它；漏声明炭火壳会被旧 AppShell 包住——2026-10-30 合并后 e2e 视觉验证实锤的那次）', () => {
    // `route.meta` 是 matched 全链的浅合并：父带 layout、子不带 ⇒ 合并结果仍是 blank。
    // ⚠️ v2 子页（my-orders / hall 与「我的相关」4 页）同样必须落 blank——E-0308 钉的就是「子页被旧壳包住」这个洞。
    expect(router.resolve('/v2').meta.layout).toBe('blank')
    for (const path of ['/v2/my-orders', '/v2/hall', '/v2/income', '/v2/settlement', '/v2/profile', '/v2/messages', '/v2/approvals', '/v2/wecom-archive', '/v2/order-audit', '/v2/order-new', '/v2/order-manage', '/v2/platform-orders', '/v2/order-recycle', '/v2/user-new', '/v2/order-action-log', '/v2/ship-log', '/v2/upstream-price-log', '/v2/user-list', '/v2/real-name', '/v2/login-restrict', '/v2/blacklist', '/v2/contract', '/v2/xgj-receipts', '/v2/customers', '/v2/customer-analysis', '/v2/customer-blacklist', '/v2/game-manage', '/v2/game-projects', '/v2/platform-integration', '/v2/platform-auto-reply', '/v2/dailiantong-stats', '/v2/dailiantong-templates', '/v2/performance', '/v2/bill-summary', '/v2/day-report', '/v2/online-pay-detail', '/v2/fund-detail', '/v2/role-authority', '/v2/role-group', '/v2/order-recognition', '/v2/order-sources', '/v2/source-system', '/v2/alipay-auth', '/v2/pdd-ship', '/v2/notices', '/v2/operation-log', '/v2/wechat-group-push', '/v2/settings', '/v2/app-stats', '/v2/feedback', '/v2/articles', '/v2/perfanalysis', '/v2/open-apps', '/v2/taker-apps', '/v2/xgj-shop', '/v2/wecom-group-qr', '/v2/wechatmp-settings', '/v2/qqbot-settings', '/v2/wecom-settings']) {
      expect(router.resolve(path).meta.layout, `${path} 应继承 blank`).toBe('blank')
    }
  })

  it('子路由恰好六十四条（含开发环境的内部通讯）', () => {
    const parent = router.getRoutes().find((r) => r.name === 'v2')
    expect(parent).toBeDefined()
    expect(parent!.children).toHaveLength(64)
    expect(parent!.children.map((c) => c.name)).toEqual([
      'v2-home',
      'v2-my-orders',
      'v2-hall',
      'v2-income',
      'v2-settlement',
      'v2-profile',
      'v2-messages',
      'v2-internal-chat',
      'v2-order-detail-preview',
      'v2-approvals',
      'v2-wecom-archive',
      'v2-order-audit',
      'v2-order-new',
      'v2-order-manage',
      'v2-platform-orders',
      'v2-order-recycle',
      'v2-user-new',
      'v2-order-action-log',
      'v2-ship-log',
      'v2-upstream-price-log',
      'v2-user-list',
      'v2-real-name',
      'v2-login-restrict',
      'v2-blacklist',
      'v2-contract',
      'v2-xgj-receipts',
      'v2-customers',
      'v2-customer-analysis',
      'v2-customer-blacklist',
      'v2-game-manage',
      'v2-game-projects',
      'v2-platform-integration',
      'v2-platform-auto-reply',
      'v2-dailiantong-stats',
      'v2-dailiantong-templates',
      'v2-performance',
      'v2-bill-summary',
      'v2-day-report',
      'v2-online-pay-detail',
      'v2-fund-detail',
      'v2-role-authority',
      'v2-role-group',
      'v2-order-recognition',
      'v2-order-sources',
      'v2-source-system',
      'v2-alipay-auth',
      'v2-pdd-ship',
      'v2-notices',
      'v2-operation-log',
      'v2-wechat-group-push',
      'v2-settings',
      'v2-app-stats',
      'v2-feedback',
      'v2-articles',
      'v2-perfanalysis',
      'v2-open-apps',
      'v2-taker-apps',
      'v2-xgj-shop',
      'v2-wecom-group-qr',
      'v2-wxpusher-settings',
      'v2-internal-chat-settings',
      'v2-wechatmp-settings',
      'v2-qqbot-settings',
      'v2-wecom-settings',
    ])
  })

  it('/v2 不会吃掉任何旧 URL（静态段 /v2 与旧路由零前缀冲突；旧 /orders/mine 原样可达）', () => {
    expect(router.resolve('/v2/nope').name).toBe('not-found')
    expect(router.resolve('/v2/salesman-stats').name).toBe('not-found')
    expect(router.resolve('/agents/salesman-stats').name).toBe('agents-salesman-stats')
    expect(router.resolve('/orders').name).toBe('orders')
    expect(router.resolve('/orders/mine').name).toBe('orders-mine')
  })
})

describe('/v2 切换后沿用旧页面的入口权限', () => {
  it('内部通讯沿用 v2 外壳与登录校验', async () => {
    const path = '/v2/internal-chat'
    const resolved = router.resolve(path)
    expect(resolved.name).toBe('v2-internal-chat')
    expect(resolved.meta.layout).toBe('blank')
    expect(resolved.meta.title).toBe('内部通讯')
    expect(await viewOf(path)).toEqual({ __view: 'V2InternalChat' })

    setActivePinia(createPinia())
    vi.mocked(fetchMe).mockRejectedValue(new AuthExpiredError('未认证'))
    await router.push(path)
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe(path)
  })

  it('首页要求登录，且可以作为登录后的落点', () => {
    expect(router.resolve('/v2').meta.requiresAuth).toBeUndefined()
    expect(router.resolve('/v2').meta.permissions).toBeUndefined()
  })

  it.each([
    ['v2-my-orders', 'orders-mine'],
    ['v2-hall', 'orders-inside-hall'],
    ['v2-income', 'agents-my-income'],
    ['v2-settlement', 'agents-my-settlement'],
    ['v2-profile', 'accounts-profile'],
    ['v2-messages', 'accounts-messages'],
    ['v2-order-audit', 'orders-platform-action-log'],
    ['v2-order-new', 'order-new'],
    ['v2-order-manage', 'orders'],
    ['v2-platform-orders', 'orders-platform-core'],
    ['v2-order-recycle', 'orders-recycle'],
    ['v2-order-action-log', 'orders-platform-action-log'],
    ['v2-ship-log', 'orders-taobao-ship'],
    ['v2-upstream-price-log', 'orders-source-price-log'],
    ['v2-game-manage', 'game-config-games'],
    ['v2-game-projects', 'game-config-projects'],
    ['v2-platform-integration', 'system-platform-integration'],
    ['v2-platform-auto-reply', 'system-platform-auto-reply'],
    ['v2-dailiantong-stats', 'dailiantong-stats'],
    ['v2-dailiantong-templates', 'dailiantong-order-templates'],
  ])('%s 沿用 %s 的权限', (newName, existingName) => {
    const expected = router.resolve({ name: existingName }).meta.permissions
    expect(expected?.length).toBeGreaterThan(0)
    expect(router.resolve({ name: newName }).meta.permissions).toEqual(expected)
  })
})

/**
 * `requiresAccountAdmin` 的**真守卫**用例（开放平台批 2026-11-06；v2 子树**首例**带此门）。
 * 形状照 `router/__tests__/guards.spec.ts:115`：合成会话 + 真 `installGuards` + 真导航，
 * 两个方向都测（只测拒绝会放过「把所有路由都拒了」，只测放行会放过「没接线」）。
 * ⚠️ 会话用**合成值**（PII 纪律）；非管理员那一条**持着节点**也进不去——这正是第二道门
 * 独立于节点门的那句话（守卫里两条判据各自独立执行）。
 */
describe('/v2 接线：requiresAccountAdmin 真守卫（开放平台批，v2 子树首例）', () => {
  /** 合成会话（形状照 `/api/auth/me`；PII 纪律：`src/**` 不进真值）。 */
  const staff: User = {
    id: 21,
    username: 'tester_staff',
    display_name: '测试客服',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: ['客服'],
    role: '客服',
    permissions: [],
    menu_permissions: [],
    tenant_name: '示例工作室甲',
  }

  beforeEach(() => {
    // mock 是模块级的：不 reset 会把上一条的调用计数带进下一条（notifyError 那条就栽在这）。
    vi.mocked(fetchMe).mockReset()
    vi.mocked(notifyError).mockReset()
  })

  it('只读订单账号可以进管理订单，但无权直达录单页', async () => {
    setActivePinia(createPinia())
    vi.mocked(fetchMe).mockResolvedValue({ ...staff, menu_permissions: ['order.list'] })
    await router.push('/v2/order-manage')
    expect(router.currentRoute.value.name).toBe('v2-order-manage')

    await router.push('/v2/order-new')
    expect(router.currentRoute.value.name).toBe('v2-home')
    expect(notifyError).toHaveBeenCalledOnce()
  })

  it('/v2/perfanalysis：非账号管理员（持 system.logs 节点）→ 弹回新版首页 + 就地提示（守卫第二道门）', async () => {
    setActivePinia(createPinia())
    vi.mocked(fetchMe).mockResolvedValue({ ...staff, menu_permissions: ['system.logs'] })
    await router.push('/v2/perfanalysis')
    expect(router.currentRoute.value.name).toBe('v2-home')
    expect(notifyError).toHaveBeenCalled()
  })

  it('/v2/perfanalysis：账号管理员 → 放行（且不要求节点）', async () => {
    setActivePinia(createPinia())
    vi.mocked(fetchMe).mockResolvedValue({ ...staff, is_account_admin: true, menu_permissions: [] })
    await router.push('/v2/perfanalysis')
    expect(router.currentRoute.value.name).toBe('v2-perfanalysis')
    expect(notifyError).not.toHaveBeenCalled()
  })
})


describe('生产后台改用新版入口', () => {
  const user: User = {
    id: 2,
    username: 'cutover_tester',
    display_name: '验收账号',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    permissions: [],
    menu_permissions: ['order.list', 'order.view_detail'],
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(fetchMe).mockReset()
    vi.mocked(fetchMe).mockResolvedValue(user)
    vi.mocked(login).mockReset()
    vi.mocked(notifyError).mockClear()
  })

  it('企微自建应用只有新版入口', () => {
    expect(router.hasRoute('wecom-settings')).toBe(false)
    expect(router.resolve({ name: V2_ROUTE_OVERRIDES['wecom-settings']! }).path).toBe('/v2/wecom-settings')
  })

  const legacyMappings = Object.entries(V2_ROUTE_OVERRIDES).filter(([name]) => name !== 'wecom-settings')
  it.each(legacyMappings)('%s → %s 保持登录与两道权限门', (legacy, name) => {
    const source = router.resolve({ name: legacy })
    const target = router.resolve({ name })
    expect(target.path).toMatch(/^\/v2(?:\/|$)/)
    expect([...(target.meta.permissions ?? [])].sort()).toEqual([...(source.meta.permissions ?? [])].sort())
    expect(target.meta.requiresAuth ?? true).toBe(source.meta.requiresAuth ?? true)
    expect(target.meta.requiresAccountAdmin === true).toBe(source.meta.requiresAccountAdmin === true)
  })

  it('根路径和常用旧列表进新版，保留查询和定位片段', async () => {
    await router.push('/')
    expect(router.currentRoute.value.fullPath).toBe('/v2')
    await router.push('/orders?status=IN_PROGRESS#list')
    expect(router.currentRoute.value.fullPath).toBe('/v2/order-manage?status=IN_PROGRESS#list')
    await router.push('/orders/new')
    expect(router.currentRoute.value.fullPath).toBe('/v2')
    expect(notifyError).toHaveBeenCalledOnce()
  })

  it('打手旧钱包和无直接等价的新旧工具地址保留原路由', async () => {
    vi.mocked(fetchMe).mockResolvedValue({ ...user, is_tenant_admin: true, is_account_admin: true })
    for (const path of ['/agents/taker-wallet', '/system-users/42/edit', '/thirdparty/order-qr-fetch', '/self-registration', '/kefu']) {
      await router.push(path)
      expect(router.currentRoute.value.path).toBe(path)
    }
  })

  it('财务回执和密码页签的旧链接改去新版，并保留深链参数', async () => {
    vi.mocked(fetchMe).mockResolvedValue({ ...user, is_tenant_admin: true, is_account_admin: true })
    await router.push('/agents/online-pay-detail?batch=demo')
    expect(router.currentRoute.value.fullPath).toBe('/v2/online-pay-detail?batch=demo')
    await router.push('/accounts/profile?tab=password')
    expect(router.currentRoute.value.fullPath).toBe('/v2/profile?tab=password')
  })

  it.each([
    {
      path: '/login', redirect: undefined, landing: '/v2', routeName: 'v2-home',
      orderNo: undefined, islandPage: undefined,
    },
    {
      path: '/orders/20260101000100000001?tab=details#summary',
      redirect: '/orders/20260101000100000001?tab=details#summary',
      landing: '/orders/20260101000100000001?tab=details#summary', routeName: 'order-detail',
      orderNo: '20260101000100000001', islandPage: 'order-orderview',
    },
  ])('从 $path 登录后回到对应首页或旧订单详情', async ({ path, redirect, landing, routeName, orderNo, islandPage }) => {
    vi.mocked(fetchMe).mockRejectedValue(new AuthExpiredError('未认证'))
    vi.mocked(login).mockResolvedValue(user)
    await router.push(path)
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe(redirect)
    const wrapper = mount(LoginView, { global: { plugins: [getActivePinia()!, router] } })
    try {
      await wrapper.get('input[name="username"]').setValue(user.username)
      await wrapper.get('input[name="password"]').setValue('synthetic-pass')
      await wrapper.get('form').trigger('submit')
      await flushPromises()
      const current = router.currentRoute.value
      expect(current.fullPath).toBe(landing)
      expect(current.name).toBe(routeName)
      expect(current.params.order_no).toBe(orderNo)
      expect(current.meta.islandPage).toBe(islandPage)
    } finally {
      wrapper.unmount()
    }
  })

  it('无订单查看权限不能打开旧详情', async () => {
    vi.mocked(fetchMe).mockResolvedValue({ ...user, menu_permissions: [] })
    await router.push('/orders/20260101000100000001')
    expect(router.currentRoute.value.fullPath).toBe('/v2')
    expect(notifyError).toHaveBeenCalledOnce()
  })
})
