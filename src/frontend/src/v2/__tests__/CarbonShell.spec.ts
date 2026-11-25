// @vitest-environment jsdom
// ↑ 挂载组件需要 DOM（本仓 vitest 默认 environment 是 node，只在本文件覆盖）。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, onMounted, ref, watch } from 'vue'
import { createMemoryHistory, createRouter, useRoute, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 炭火外壳（CarbonShell）侧栏菜单的判据（2026-10-30「菜单照抄旧侧边栏」线）：**「照抄」的机器判据**：v2 渲染出的结构与「`config/menu.ts` 经 `useMenu` 同一条管道」算出的结果**恰好相等**——组标题集合与顺序 = `visibleGroups(MENU_GROUPS)` 的输出；每组子条目 = `visibleItems(组条目)` 的逐字输出；CarbonShell 与旧侧边栏吃的是同一份数据，这里钉的是「渲染层没有抄错 / 漏抄 / 改序」。再抽 5 个真实条目名钉字面值防「两端一起漂」。其余：折叠真点击（unique-opened，同旧壳 el-menu）、「后台首页」特例 push `v2-home`、抽样旧条目 push 旧路由 name、非管理员按权限不渲染、`isClickable=false` 置灰不可点。会话用**合成值**（PII 纪律：`src/**` 不进真值）；子路由用空组件替身——本文件只测壳，首页内容的行为判据在 `V2Dashboard.spec.ts`。
 * 炭火外壳（CarbonShell）侧栏菜单的判据（2026-10-30「菜单照抄旧侧边栏」线）：**「照抄」的机器判据**：v2 渲染出的结构与「`config/menu.ts` 经 `useMenu` 同一条管道」算出的结果**恰好相等**——组标题集合与顺序 = `visibleGroups(MENU_GROUPS)` 的输出；每组子条目 = `visibleItems(组条目)` 的逐字输出；CarbonShell 与旧侧边栏吃的是同一份数据，这里钉的是「渲染层没有抄错 / 漏抄 / 改序」。再抽 5 个真实条目名钉字面值防「两端一起漂」。其余：折叠真点击（unique-opened，同旧壳 el-menu）、「后台首页」特例 push `v2-home`、抽样旧条目 push 旧路由 name、非管理员按权限不渲染、`isClickable=false` 置灰不可点。会话用**合成值**（PII 纪律：`src/**` 不进真值）；子路由用空组件替身——本文件只测壳，首页内容的行为判据在 `V2Dashboard.spec.ts`。 */
import type { User } from '@/api/auth'
import { avatarDataUri } from '@/config/avatar'
import { BOTTOM_ITEMS, MENU_GROUPS, TOP_LEVEL_ITEMS } from '@/config/menu'
import { visibleGroups, visibleItems } from '@/composables/useMenu'
import { GROUP_ICONS } from '@/config/menuIcons'

/** v2 侧栏渲染层对期望侧的同构变换：条目级隐藏（`V2_HIDDEN_ROUTE_NAMES`；组内重排
 * `moveGroupTailToLast` 是从 v2Menu import 的**同一个函数**，不在这里复刻）。
 * 「照抄」网比较的是渲染侧的输出，渲染侧已经换过 ⇒ 期望侧必须吃同一份输入（否则
 * 两端不对等，网恒红）。 */
function v2Visible<T extends { routeName?: string }>(items: readonly T[]): T[] {
  return items.filter((i) => !V2_HIDDEN_ROUTE_NAMES.has(i.routeName ?? ''))
}

/** 期望侧的组名同名变换：渲染层组名改名（`V2_GROUP_LABEL_OVERRIDES`）套用——
 * 与 `useV2Menu()` import 同一张表，⛔ 手抄第二份（2026-11-06 第三方平台批）。 */
function v2GroupName(name: string): string {
  return V2_GROUP_LABEL_OVERRIDES[name] ?? name
}

/** 期望侧的条目名同名变换：渲染层条目名改名（`V2_ITEM_LABEL_OVERRIDES`，键 = routeName）。 */
function v2ItemLabel(item: { routeName?: string; label: string }): string {
  return V2_ITEM_LABEL_OVERRIDES[item.routeName ?? ''] ?? item.label
}

/** 管理员会话（合成值；形状照 `/api/auth/me`）：`useMenu.canSeeItem` 对管理员短路 ⇒ 全菜单可见。 */
const ME_ADMIN: User = {
  id: 7,
  username: 'tester_admin',
  display_name: '测试管理员',
  identity_plane: 'tenant',
  is_tenant_admin: true,
  is_account_admin: true,
  roles: ['系统管理员'],
  role: '系统管理员',
  permissions: [],
  menu_permissions: [],
  tenant_name: '示例工作室甲',
}

/** 非管理员会话：只持三个节点。预期可见面（由 useMenu 语义算出，断言里逐条钉）：顶部仅「后台首页」（无 permissions = 登录即可见），my_orders / grab_hall 都不可见；【订单管理】只剩「录入订单」(order.add_edit)、「系统人员」只剩「人员管理」(system_user.list)；【财务相关】对「打手与客服报酬」是 some 语义但本会话两者皆无 ⇒ 整组空 ⇒ 不渲染组头；底部「问题反馈」(home.feedback) 可见。 */
const ME_STAFF: User = {
  id: 8,
  username: 'tester_staff',
  display_name: '测试客服',
  identity_plane: 'tenant',
  is_tenant_admin: false,
  is_account_admin: false,
  roles: ['客服'],
  role: '客服',
  permissions: [],
  menu_permissions: ['order.add_edit', 'system_user.list', 'home.feedback'],
  tenant_name: '示例工作室甲',
}

/** 本文件可变的会话载荷：fetchMe 被 mock 成返回它，测试开头换值即换会话。 */
let mePayload: User

const apiLogout = vi.fn(async () => null)

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  // ⚠️ 工厂被提升到文件顶：这里只能**惰性**引用 `apiLogout`（直接写 `logout: apiLogout`
  // 会在 const 初始化前执行 = TDZ ReferenceError，实测）。fetchMe 那条同理。
  return {
    ...actual,
    fetchMe: vi.fn(async () => mePayload),
    login: vi.fn(async () => mePayload),
    logout: () => apiLogout(),
  }
})

import CarbonShell from '../CarbonShell.vue'
import { useSessionStore } from '@/stores/session'
import {
  moveGroupTailToLast,
  targetNameOf,
  V2_GROUP_LABEL_OVERRIDES,
  V2_HIDDEN_ROUTE_NAMES,
  V2_ITEM_LABEL_OVERRIDES,
  V2_ROUTE_OVERRIDES,
} from '../v2Menu'

const Blank = { template: '<div />' }
const OrderForm = defineComponent({
  setup: () => ({ value: ref('') }),
  template: '<input v-model="value" data-v2-test="order-draft" />',
})
const OtherForm = defineComponent({
  setup: () => ({ value: ref('') }),
  template: '<input v-model="value" data-v2-test="other-form" />',
})
const loadOrderList = vi.fn()
const OrderList = defineComponent({
  setup() {
    const route = useRoute()
    const filter = ref(String(route.query.sample ?? ''))
    const checked = ref(false)
    onMounted(() => loadOrderList(route.fullPath))
    watch(() => route.query, () => { filter.value = String(route.query.sample ?? '') })
    return { route, filter, checked }
  },
  template: `<div :data-test-order-list="route.fullPath">
    <input v-model="filter" data-test-order-filter />
    <input v-model="checked" type="checkbox" data-test-order-selection />
  </div>`,
})

/** 测试路由表：/v2（CarbonShell + v2-home + 一条测试用 v2 子路由）+ config/menu.ts 里出现过的 routeName 里**生产旧路由表真实存在的那些**，注册成真路由 ⇒ `isClickable`（= router.hasRoute）与生产旧表等价（全集见 v2Routes.spec 的 LEGACY_ROUTES_SNAPSHOT）。
 * ⭐ E-0325：v2-only 条目（生产旧表**没有**的名字，首例 `wecom-settings`）⛔ **不进** legacy 注册——
 * 注册了会让「v2 侧栏可点性」的回归判据恒真（本线真 bug 曾被这层夹具遮住：置灰被继承、
 * 侧栏点不动，而 spec 全绿）。**新增 v2-only 页必须来这里把名字加进 `V2_ONLY_MENU_NAMES`**，
 * 真值以生产 `router/index.ts` 旧表（= LEGACY_ROUTES_SNAPSHOT）为准。
 * ⚠️ `v2-my-orders`（测试替身组件 + 合成标题）：标签页条的「累积开签 / 切换 / 关闭」需要第二条 v2 路由才能驱动（见 v2Routes.spec）。 */
const V2_ONLY_MENU_NAMES = new Set<string>(['wecom-settings'])
function makeRouter(): Router {
  const names = new Set<string>()
  for (const item of TOP_LEVEL_ITEMS) if (item.routeName) names.add(item.routeName)
  for (const group of MENU_GROUPS)
    for (const item of group.items) if (item.routeName) names.add(item.routeName)
  for (const item of BOTTOM_ITEMS) if (item.routeName) names.add(item.routeName)
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/v2',
        name: 'v2',
        component: CarbonShell,
        meta: { title: '后台首页 v2' },
        children: [
          { path: '', name: 'v2-home', component: Blank, meta: { title: '后台首页 v2' } },
          { path: 'my-orders', name: 'v2-my-orders', component: OtherForm, meta: { title: '指派给我的订单' } },
          // ⭐ D-0352：第三条 v2 子路由（「内部抢单大厅」）；生产路由表见 v2Routes.spec。
          { path: 'hall', name: 'v2-hall', component: Blank, meta: { title: '内部抢单大厅' } },
          // ⭐（D-0353）「我的相关」组 4 页 v2 化——特例映射的落点必须真实存在，
          // 否则下面「映射防死链」那条对拍（`router.hasRoute(targetNameOf(...))`）会红。
          { path: 'income', name: 'v2-income', component: Blank, meta: { title: '我的订单收入' } },
          { path: 'settlement', name: 'v2-settlement', component: Blank, meta: { title: '在线结算记录' } },
          { path: 'profile', name: 'v2-profile', component: Blank, meta: { title: '我的资料' } },
          { path: 'messages', name: 'v2-messages', component: Blank, meta: { title: '消息通知' } },
          { path: 'approvals', name: 'v2-approvals', component: Blank, meta: { title: '审批中心' } },
          { path: 'wxpusher-settings', name: 'v2-wxpusher-settings', component: Blank, meta: { title: '消息推送订阅' } },
          { path: 'internal-chat-settings', name: 'v2-internal-chat-settings', component: Blank, meta: { title: '内部沟通配置' } },
          // ⭐（D-0355）「录入订单」v2 页——特例映射第八条的落点（防死链对拍）。
          { path: 'order-new', name: 'v2-order-new', component: OrderForm, meta: { title: '录入订单', permissions: ['order.add_edit'] } },
          // ⭐（2026-11-05 设计线）三条 v2 页——特例映射 orders / orders-platform-core /
          // orders-recycle 的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'order-manage', name: 'v2-order-manage', component: OrderList, meta: { title: '管理订单', permissions: ['order.list'] } },
          { path: 'platform-orders', name: 'v2-platform-orders', component: Blank, meta: { title: '第三方平台订单管理' } },
          { path: 'order-recycle', name: 'v2-order-recycle', component: Blank, meta: { title: '找回删除的订单' } },
          // ⭐（2026-11-05）「新开系统账号」v2 页——特例映射 `system-user-new` 的落点
          // （防死链对拍 `router.hasRoute(targetNameOf('system-user-new'))`）；生产路由见 v2Routes.spec。
          { path: 'user-new', name: 'v2-user-new', component: Blank, meta: { title: '新开系统账号' } },
          // ⭐（2026-11-05 设计线）三张记录页——特例映射 orders-platform-action-log /
          // orders-taobao-ship / orders-source-price-log 的落点；生产路由见 v2Routes.spec。
          { path: 'order-action-log', name: 'v2-order-action-log', component: Blank, meta: { title: '交易订单操作记录' } },
          { path: 'ship-log', name: 'v2-ship-log', component: Blank, meta: { title: '订单发货记录' } },
          { path: 'upstream-price-log', name: 'v2-upstream-price-log', component: Blank, meta: { title: '上家订单改价记录' } },
          // ⭐（2026-11-05 系统人员批次）五页——特例映射 system-users / system-real-name /
          // system-login-restrict / system-blacklist / system-contract 的落点（防死链对拍）。
          { path: 'user-list', name: 'v2-user-list', component: Blank, meta: { title: '人员管理' } },
          { path: 'real-name', name: 'v2-real-name', component: Blank, meta: { title: '人员实名记录与配置' } },
          { path: 'login-restrict', name: 'v2-login-restrict', component: Blank, meta: { title: '登录限制设置' } },
          { path: 'blacklist', name: 'v2-blacklist', component: Blank, meta: { title: '人员黑名单' } },
          { path: 'contract', name: 'v2-contract', component: Blank, meta: { title: '员工电子合同管理' } },
          // 闲管家收款明细的路由落点。
          { path: 'xgj-receipts', name: 'v2-xgj-receipts', component: Blank, meta: { title: '闲管家收款明细' } },
          // ⭐（2026-11-05 本分支）「游戏维护」「发平台设置」两组 6 条——特例映射的落点（防死链对拍）。
          { path: 'game-manage', name: 'v2-game-manage', component: Blank, meta: { title: '游戏管理' } },
          { path: 'game-projects', name: 'v2-game-projects', component: Blank, meta: { title: '订单项目分类与常用项目' } },
          { path: 'platform-integration', name: 'v2-platform-integration', component: Blank, meta: { title: '交易平台对接设置' } },
          { path: 'platform-auto-reply', name: 'v2-platform-auto-reply', component: Blank, meta: { title: '交易平台自动回复' } },
          { path: 'dailiantong-stats', name: 'v2-dailiantong-stats', component: Blank, meta: { title: '代练通按日统计' } },
          { path: 'dailiantong-templates', name: 'v2-dailiantong-templates', component: Blank, meta: { title: '代练通订单要求模板' } },
          // ⭐（2026-11-06 财务族第二批）五页——特例映射 agents-performance / agents-settlement /
          // agents-day-report / agents-online-pay-detail / agents-fund-detail 的落点（防死链对拍）。
          { path: 'performance', name: 'v2-performance', component: Blank, meta: { title: '打手与客服报酬' } },
          { path: 'bill-summary', name: 'v2-bill-summary', component: Blank, meta: { title: '结算记录' } },
          { path: 'day-report', name: 'v2-day-report', component: Blank, meta: { title: '经营报表' } },
          { path: 'online-pay-detail', name: 'v2-online-pay-detail', component: Blank, meta: { title: '在线结算明细' } },
          { path: 'fund-detail', name: 'v2-fund-detail', component: Blank, meta: { title: '团团通资金明细与提现' } },
          // ⭐（2026-11-06 系统维护批次）11 页——特例映射 system-role-authority / system-role-group /
          // system-order-recognition / system-order-sources / system-source-system /
          // agents-alipay-auth / system-pdd-ship / system-notices / system-operation-log /
          // system-wechat-group-push / system-settings 的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'role-authority', name: 'v2-role-authority', component: Blank, meta: { title: '角色职位权限' } },
          { path: 'role-group', name: 'v2-role-group', component: Blank, meta: { title: '角色职位分组' } },
          { path: 'order-recognition', name: 'v2-order-recognition', component: Blank, meta: { title: '录单识别设置' } },
          { path: 'order-sources', name: 'v2-order-sources', component: Blank, meta: { title: '订单来源管理' } },
          { path: 'source-system', name: 'v2-source-system', component: Blank, meta: { title: '绑定上家系统设置' } },
          { path: 'alipay-auth', name: 'v2-alipay-auth', component: Blank, meta: { title: '在线结算支付宝授权' } },
          { path: 'pdd-ship', name: 'v2-pdd-ship', component: Blank, meta: { title: '其它平台店铺对接' } },
          { path: 'notices', name: 'v2-notices', component: Blank, meta: { title: '系统内部公告管理' } },
          { path: 'operation-log', name: 'v2-operation-log', component: Blank, meta: { title: '系统操作日志管理' } },
          { path: 'wechat-group-push', name: 'v2-wechat-group-push', component: Blank, meta: { title: '自动发消息到微信群设置' } },
          { path: 'settings', name: 'v2-settings', component: Blank, meta: { title: '系统各项设置管理' } },
          // ⭐（2026-11-06 APP与小程序+问题反馈批）3 条——特例映射 app-stats / feedback /
          // app-mp-articles 的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'app-stats', name: 'v2-app-stats', component: Blank, meta: { title: '数据概览' } },
          { path: 'feedback', name: 'v2-feedback', component: Blank, meta: { title: '问题反馈' } },
          { path: 'articles', name: 'v2-articles', component: Blank, meta: { title: '发布文章作品' } },
          // ⭐（2026-11-06 开放平台批）3 条——特例映射 system-perfanalysis / open-apps /
          // taker-apps 的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'perfanalysis', name: 'v2-perfanalysis', component: Blank, meta: { title: '性能分析' } },
          { path: 'open-apps', name: 'v2-open-apps', component: Blank, meta: { title: '应用管理' } },
          { path: 'taker-apps', name: 'v2-taker-apps', component: Blank, meta: { title: '打手对接' } },
          // ⭐（2026-11-06 第三方平台批）4 条——特例映射 thirdparty-xgj-shop / wecom-group-qr /
          // system-wechatmp-settings / qqbot-settings 的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'xgj-shop', name: 'v2-xgj-shop', component: Blank, meta: { title: '闲管家店铺对接' } },
          { path: 'wecom-group-qr', name: 'v2-wecom-group-qr', component: Blank, meta: { title: '自动上传群码' } },
          { path: 'wechatmp-settings', name: 'v2-wechatmp-settings', component: Blank, meta: { title: '微信公众号设置' } },
          { path: 'qqbot-settings', name: 'v2-qqbot-settings', component: Blank, meta: { title: 'QQ机器人设置' } },
          // ⭐（D-0379）「企微自建应用」v2-only——特例映射 `wecom-settings` 的落点
          // （防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'wecom-settings', name: 'v2-wecom-settings', component: Blank, meta: { title: '企微自建应用' } },
          // ⭐（2026-11-10 客户线）2 条——特例映射 customers（会员管理）与
          // customers-analysis（客户分析报表，第三批）的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'customers', name: 'v2-customers', component: Blank, meta: { title: '客户管理' } },
          { path: 'customer-analysis', name: 'v2-customer-analysis', component: Blank, meta: { title: '客户分析报表' } },
          // ⭐（2026-11-11 客户线第七批）1 条——特例映射 customers-blacklist（黑名单管理
          // v2 占位，不再跳 v1 旧壳）的落点（防死链对拍）；生产路由见 v2Routes.spec。
          { path: 'customer-blacklist', name: 'v2-customer-blacklist', component: Blank, meta: { title: '黑名单管理' } },
        ],
      },
      ...[...names]
        .filter((name) => !V2_ONLY_MENU_NAMES.has(name))
        .map((name) => ({ path: `/legacy/${name}`, name, component: Blank })),
      { path: '/login', name: 'login', component: Blank },
    ],
  })
}

/** 挂载前的公共装配：单条 pinia 链（setActivePinia 与 mount 必须同一个）+ 会话就绪。 */
async function mountShell(router: Router, attachTo?: HTMLElement) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  await session.load() // 守卫在生产里做这件事；测试里显式做（会话就绪后壳才读得到用户）
  // 经 `<RouterView/>` 宿主挂载：/v2 的 component 就是 CarbonShell 本尊，直接 mount 会把
  // 壳内部的 RouterView 又渲染一层 matched[0]（= 壳自己）⇒ 全页 ×2。
  return mount({ template: '<RouterView />' }, { attachTo, global: { plugins: [router, pinia] } })
}

/** B3 专用：当前路由停在某个旧路由上时直接挂壳（matched[0] 是替身组件，不会翻倍）。 */
async function mountShellAt(router: Router) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const session = useSessionStore()
  await session.load()
  return mount(CarbonShell, { global: { plugins: [router, pinia] } })
}

async function mountAdminShell(router: Router) {
  await router.push('/v2')
  await router.isReady()
  return mountShell(router)
}

/** 渲染出的组标题（按 DOM 顺序）。 */
function renderedGroupNames(wrapper: Awaited<ReturnType<typeof mountShell>>): string[] {
  return wrapper.findAll('[data-v2-group]').map((el) => el.attributes('data-v2-group') ?? '')
}

/** 某个组渲染出的子条目文字（按 DOM 顺序）。 */
function renderedSubLabels(
  wrapper: Awaited<ReturnType<typeof mountShell>>,
  group: string,
): string[] {
  return wrapper
    .findAll(`[data-v2-sublist="${group}"] .cv2-sub-link`)
    .map((a) => a.text())
}

let router: Router

beforeEach(() => {
  mePayload = ME_ADMIN
  router = makeRouter()
})

it('loads the development chat from the top bar and follows live unread counts', async () => {
  router.addRoute('v2', {
    path: 'internal-chat',
    name: 'v2-internal-chat',
    component: () => import('../V2InternalChat.vue'),
    meta: { title: '内部通讯' },
  })
  await router.push('/v2')
  const wrapper = await mountShell(router, document.body)
  try {
    await vi.dynamicImportSettled()
    await flushPromises()
    const entry = wrapper.get('.cv2-topbar a[aria-label="内部沟通，5 条未读消息"]')
    expect(entry.attributes('href')).toBe('/v2/internal-chat')
    await entry.trigger('click')
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('v2-internal-chat')
    expect(entry.attributes('aria-current')).toBe('page')

    await wrapper.get('.cv2-chat-row[aria-label*="3 条未读消息"]').trigger('click')
    expect(entry.attributes('aria-label')).toBe('内部沟通，2 条未读消息')
    await wrapper.get('.cv2-chat-row[aria-label*="2 条未读消息"]').trigger('click')
    expect(entry.attributes('aria-label')).toBe('内部沟通')
    expect(entry.find('.cv2-chat-count').exists()).toBe(false)

    const draft = '仅当前演示会话可见的草稿'
    await wrapper.get('textarea[aria-label="输入消息"]').setValue(draft)
    await router.push('/v2')
    await router.push('/v2/internal-chat')
    expect(wrapper.get('textarea').element.value).toBe(draft)

    mePayload = { ...ME_ADMIN, identity_plane: 'tenant_second', tenant_name: '示例工作室乙' }
    await useSessionStore().refresh()
    await flushPromises()
    expect(wrapper.get('textarea').element.value).toBe('')
    expect(entry.attributes('aria-label')).toBe('内部沟通，5 条未读消息')
  } finally {
    wrapper.unmount()
  }
})

describe('CarbonShell：菜单与 config/menu.ts 同源（管理员会话）', () => {
  it('组标题集合与顺序 = visibleGroups(MENU_GROUPS) 的输出（「照抄」的机器判据；期望侧套 V2_HIDDEN_ROUTE_NAMES 过滤与组名改名覆盖表）', async () => {
    const wrapper = await mountAdminShell(router)
    const expected = visibleGroups(
      MENU_GROUPS.map((g) => ({ ...g, items: v2Visible(g.items) })),
      () => true,
    ).map((g) => v2GroupName(g.name))
    // 阳性对照：非空网——expected 空（≥5）时上面断言恒真，等于没测。
    expect(expected.length).toBeGreaterThanOrEqual(5)
    expect(renderedGroupNames(wrapper)).toEqual(expected)
    // 改名的钉值（防「两端共用同一张表」把网变成恒真）：组标题在 v2 侧栏真的是「功能维护」。
    expect(renderedGroupNames(wrapper)).toContain('功能维护')
    expect(renderedGroupNames(wrapper)).not.toContain('第三方平台')
  })

  it('每组子条目 = visibleItems 的逐字输出（期望侧套 V2_HIDDEN_ROUTE_NAMES 过滤 + 组名/条目名改名覆盖表 + 与渲染侧共用的 moveGroupTailToLast 组尾重排），组内顺序也照抄', async () => {
    const wrapper = await mountAdminShell(router)
    for (const group of visibleGroups(MENU_GROUPS, () => true)) {
      // 组名要先过改名覆盖表再去 DOM 查（改名组的 data-v2-sublist 是新名）。
      const renderedName = v2GroupName(group.name)
      const expected = moveGroupTailToLast(v2Visible(visibleItems(group.items, () => true))).map(v2ItemLabel)
      if (group.name === '我的相关') expected.unshift('审批中心')
      if (group.name === '第三方平台') expected.unshift('消息推送订阅', '内部沟通配置')
      expect(renderedSubLabels(wrapper, renderedName), `组「${renderedName}」的子条目`).toEqual(expected)
    }
    // 条目改名的钉值：群码条目在 v2 侧栏显示新名、旧名不再出现。
    expect(wrapper.find('[data-v2-menu="自动上传群码"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-menu="自动上传企微群码到订单截图"]').exists()).toBe(false)
    // 组内重排的钉值（防「两端共用同一函数」把网变成恒真）：资金明细必须真在【财务相关】末项。
    const financeLabels = renderedSubLabels(wrapper, '财务相关')
    expect(financeLabels.length).toBeGreaterThan(0)
    expect(financeLabels[financeLabels.length - 1]).toBe('团团通资金明细与提现')
  })

  it('钉值：抽 4 个真实条目名逐字在 DOM；隐藏条目「我参与的排团」「打手钱包」不渲染', async () => {
    const wrapper = await mountAdminShell(router)
    for (const label of ['录入订单', '交易平台对接设置', 'QQ机器人设置', '问题反馈']) {
      const el = wrapper.find(`[data-v2-menu="${label}"]`)
      expect(el.exists(), `条目「${label}」应渲染`).toBe(true)
      expect(el.text()).toBe(label)
    }
    expect(wrapper.find('[data-v2-menu="我参与的排团"]').exists()).toBe(false)
    // 「打手钱包」2026-11-06 业务方拍板 v2 侧栏隐藏（V2_HIDDEN_ROUTE_NAMES；旧壳照旧）。
    expect(wrapper.find('[data-v2-menu="打手钱包"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-menu="客服接发单统计"]').exists()).toBe(false)
  })

  it('顶部直挂在前、底部条目殿后（同旧侧边栏 DOM 顺序；bottom 区只有「问题反馈」，资金明细在【财务相关】组内）', async () => {
    const wrapper = await mountAdminShell(router)
    // 组 li 的直接子级是组头按钮与子列表 ⇒ 直挂条目（顶部+底部）不含子条目。
    const rendered = wrapper.findAll('.cv2-nav-list > li > .cv2-nav-link').map((a) => a.text())
    const expected = [
      ...visibleItems(TOP_LEVEL_ITEMS, () => true),
      ...visibleItems(BOTTOM_ITEMS, () => true),
    ].map((i) => i.label)
    expect(rendered).toEqual(expected)
    expect(rendered[0]).toBe('后台首页')
    expect(rendered[rendered.length - 1]).toBe('问题反馈')
  })

  it('图标沿用旧侧边栏同一张表（config/menuIcons 的 GROUP_ICONS / TOP_LEVEL_ICONS / BOTTOM_ICONS）', async () => {
    const wrapper = await mountAdminShell(router)
    for (const name of renderedGroupNames(wrapper)) {
      const head = wrapper.find(`[data-v2-group-head="${name}"]`)
      expect(head.find('.cv2-nav-icon').classes()).toContain(`layui-icon-${GROUP_ICONS[name]}`)
    }
    const expectedIcons =
      visibleItems(TOP_LEVEL_ITEMS, () => true).length +
      visibleGroups(MENU_GROUPS, () => true).length +
      visibleItems(BOTTOM_ITEMS, () => true).length
    expect(wrapper.findAll('.cv2-nav-icon')).toHaveLength(expectedIcons)
  })
})

/** 分组的收起状态 = 开合类（`cv2-nav-group--open`）的出现/消失。⚠️ 动画线起子列表**常驻 DOM**（收起 = grid 0fr + visibility:hidden 的滑动动画层），旧 v-show 的「内联 display:none」判据不再成立；开合类是该状态的唯一落点，`aria-expanded` 与它同源（下面两条一起钉，防单边漂移）。 */
/** VTU 查找结果里本判据用到的最小形状（DOMWrapper 的结构子集，可递归 find）。 */
type FinderLike = {
  classes(): string[]
  attributes(): Record<string, unknown>
  find(selector: string): FinderLike
}

function groupIsOpen(wrapper: FinderLike, group: string): boolean {
  const li = wrapper.find(`[data-v2-group="${group}"]`)
  const byClass = li.classes().includes('cv2-nav-group--open')
  const byAria = li.find(`[data-v2-group-head="${group}"]`).attributes()['aria-expanded'] === 'true'
  // 两条判据必须一致：不一致 = 实现把状态存了两份（类与 aria 各判各的）——当场红。
  expect(byClass, `组「${group}」的开合类与 aria-expanded 必须同源`).toBe(byAria)
  return byClass
}

describe('CarbonShell：折叠交互（unique-opened，同旧壳 el-menu）', () => {
  it('默认全收起；点组头展开 ⇒ 开合类与 aria-expanded 翻转，再点收起（子列表常驻 DOM）', async () => {
    const wrapper = await mountAdminShell(router)
    const sub = wrapper.find('[data-v2-sublist="订单管理"]')
    expect(sub.exists()).toBe(true) // 动画层：收起也在 DOM 里（行高压到 0fr），不是 v-show 卸载
    expect(groupIsOpen(wrapper, '订单管理')).toBe(false)
    await wrapper.find('[data-v2-group-head="订单管理"]').trigger('click')
    expect(groupIsOpen(wrapper, '订单管理')).toBe(true)
    await wrapper.find('[data-v2-group-head="订单管理"]').trigger('click')
    expect(groupIsOpen(wrapper, '订单管理')).toBe(false)
  })

  it('同时只展开一个：展开「订单管理」后再点「财务相关」，前者自动收起', async () => {
    const wrapper = await mountAdminShell(router)
    await wrapper.find('[data-v2-group-head="订单管理"]').trigger('click')
    expect(groupIsOpen(wrapper, '订单管理')).toBe(true)
    await wrapper.find('[data-v2-group-head="财务相关"]').trigger('click')
    expect(groupIsOpen(wrapper, '财务相关')).toBe(true)
    expect(groupIsOpen(wrapper, '订单管理')).toBe(false)
  })

  it('当前路由落在某组内时，该组默认展开（同旧壳 default-active 的行为）', async () => {
    // ⭐ push 的是「管理订单」的**解析目标**（2026-11-04 起映射 v2-order-manage）：
    // isActive 比较 route.name === target，push 旧 name 'orders' 永远匹配不上（在途线欠账，
    // 本线顺手改准并登记 STATUS）。
    await router.push({ name: 'v2-order-manage' }) // 「管理订单」在【订单管理】组内
    await router.isReady()
    const wrapper = await mountShellAt(router)
    expect(groupIsOpen(wrapper, '订单管理')).toBe(true)
    expect(groupIsOpen(wrapper, '财务相关')).toBe(false)
    expect(
      wrapper.find('[data-v2-menu="管理订单"]').classes(),
      '组内当前页挂子条目 active 态',
    ).toContain('cv2-sub-link--active')
  })
})

describe('CarbonShell：点击行为', () => {
  it('内部沟通配置是「功能维护」第二项，管理员可打开', async () => {
    const wrapper = await mountAdminShell(router)
    await wrapper.get('[data-v2-group-head="功能维护"]').trigger('click')
    expect(renderedSubLabels(wrapper, '功能维护').slice(0, 2)).toEqual(['消息推送订阅', '内部沟通配置'])
    await wrapper.get('[data-v2-sublist="功能维护"] [data-v2-menu="内部沟通配置"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/v2/internal-chat-settings')
  })

  it('工作室管理员从「功能维护」打开展示消息推送订阅的页面', async () => {
    const wrapper = await mountAdminShell(router)
    await wrapper.get('[data-v2-group-head="功能维护"]').trigger('click')
    await wrapper.get('[data-v2-sublist="功能维护"] [data-v2-menu="消息推送订阅"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/v2/wxpusher-settings')
  })

  it('导航等待时立即高亮目标，未跳转时恢复原选中项', async () => {
    const wrapper = await mountAdminShell(router)
    let finish!: () => void
    vi.spyOn(router, 'push').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve }))
    await wrapper.find('[data-v2-menu="管理订单"]').trigger('click')
    expect(wrapper.find('[data-v2-menu="管理订单"]').classes()).toContain('cv2-sub-link--active')
    expect(wrapper.find('[data-v2-menu="后台首页"]').classes()).not.toContain('cv2-nav-link--active')
    finish()
    await flushPromises()
    expect(wrapper.find('[data-v2-menu="管理订单"]').classes()).not.toContain('cv2-sub-link--active')
    expect(wrapper.find('[data-v2-menu="后台首页"]').classes()).toContain('cv2-nav-link--active')
  })

  it('「后台首页」点击 push v2-home，落点确实是 v2-home', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push')
    await wrapper.find('[data-v2-menu="后台首页"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-home' })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('v2-home')
  })

  it('抽样三条条目点击 push 各自 target：「管理订单」→ v2-order-manage、「结算记录」→ v2-bill-summary、底部「问题反馈」→ v2-feedback（2026-11-06 APP与小程序+问题反馈批起三条均映射 v2）', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="管理订单"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-order-manage' })
    await wrapper.find('[data-v2-menu="结算记录"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-bill-summary' })
    await wrapper.find('[data-v2-menu="问题反馈"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-feedback' })
  })

  it('映射防死链：配置里每个 routeName 的 target 都真实存在，特例恰好五十七条（home/orders-mine/orders-inside-hall + 「我的相关」4 条 + order-new/orders + platform-core/recycle + system-user-new + 三张记录页 + 系统人员组 5 页 + 财务族 2 条 + 游戏维护/发平台设置 6 条 + 财务族第二批 5 条 + 系统维护组 11 条 + APP与小程序/问题反馈/发布文章 3 条 + 开放平台 3 条 + 第三方平台 5 条 + 会员管理/客户分析报表 2 条[2026-11-10 客户线] + 企微自建应用 1 条[D-0379 v2-only]）', () => {
    // 特例映射的恰好集合（v2Menu.V2_ROUTE_OVERRIDES 的对拍；加第五十一条要显式来这里登记）。
    expect(targetNameOf('home')).toBe('v2-home')
    expect(targetNameOf('orders-mine')).toBe('v2-my-orders')
    expect(targetNameOf('orders-inside-hall')).toBe('v2-hall')
    expect(targetNameOf('agents-my-income')).toBe('v2-income')
    expect(targetNameOf('agents-my-settlement')).toBe('v2-settlement')
    expect(targetNameOf('accounts-profile')).toBe('v2-profile')
    expect(targetNameOf('accounts-messages')).toBe('v2-messages')
    expect(targetNameOf('order-new')).toBe('v2-order-new')
    expect(targetNameOf('orders')).toBe('v2-order-manage')
    expect(targetNameOf('orders-platform-core')).toBe('v2-platform-orders')
    expect(targetNameOf('orders-recycle')).toBe('v2-order-recycle')
    expect(targetNameOf('system-user-new')).toBe('v2-user-new')
    expect(targetNameOf('orders-platform-action-log')).toBe('v2-order-action-log')
    expect(targetNameOf('orders-taobao-ship')).toBe('v2-ship-log')
    expect(targetNameOf('orders-source-price-log')).toBe('v2-upstream-price-log')
    expect(targetNameOf('system-users')).toBe('v2-user-list')
    expect(targetNameOf('system-real-name')).toBe('v2-real-name')
    expect(targetNameOf('system-login-restrict')).toBe('v2-login-restrict')
    expect(targetNameOf('system-blacklist')).toBe('v2-blacklist')
    expect(targetNameOf('system-contract')).toBe('v2-contract')
    expect(targetNameOf('xgj-receipts')).toBe('v2-xgj-receipts')
    expect(targetNameOf('game-config-games')).toBe('v2-game-manage')
    expect(targetNameOf('game-config-projects')).toBe('v2-game-projects')
    expect(targetNameOf('system-platform-integration')).toBe('v2-platform-integration')
    expect(targetNameOf('system-platform-auto-reply')).toBe('v2-platform-auto-reply')
    expect(targetNameOf('dailiantong-stats')).toBe('v2-dailiantong-stats')
    expect(targetNameOf('dailiantong-order-templates')).toBe('v2-dailiantong-templates')
    // 财务族第二批（2026-11-06）：剩余五页。
    expect(targetNameOf('agents-performance')).toBe('v2-performance')
    expect(targetNameOf('agents-settlement')).toBe('v2-bill-summary')
    expect(targetNameOf('agents-day-report')).toBe('v2-day-report')
    expect(targetNameOf('agents-online-pay-detail')).toBe('v2-online-pay-detail')
    expect(targetNameOf('agents-fund-detail')).toBe('v2-fund-detail')
    // 系统维护组 11 页（2026-11-06 系统维护批次）。
    expect(targetNameOf('system-role-authority')).toBe('v2-role-authority')
    expect(targetNameOf('system-role-group')).toBe('v2-role-group')
    expect(targetNameOf('system-order-recognition')).toBe('v2-order-recognition')
    expect(targetNameOf('system-order-sources')).toBe('v2-order-sources')
    expect(targetNameOf('system-source-system')).toBe('v2-source-system')
    expect(targetNameOf('agents-alipay-auth')).toBe('v2-alipay-auth')
    expect(targetNameOf('system-pdd-ship')).toBe('v2-pdd-ship')
    expect(targetNameOf('system-notices')).toBe('v2-notices')
    expect(targetNameOf('system-operation-log')).toBe('v2-operation-log')
    expect(targetNameOf('system-wechat-group-push')).toBe('v2-wechat-group-push')
    expect(targetNameOf('system-settings')).toBe('v2-settings')
    // APP与小程序组 + 底部「问题反馈」v2（2026-11-06 APP与小程序+问题反馈批）。
    expect(targetNameOf('app-stats')).toBe('v2-app-stats')
    expect(targetNameOf('feedback')).toBe('v2-feedback')
    expect(targetNameOf('app-mp-articles')).toBe('v2-articles')
    // 开放平台组 3 页 v2（2026-11-06 开放平台批）。
    expect(targetNameOf('system-perfanalysis')).toBe('v2-perfanalysis')
    expect(targetNameOf('open-apps')).toBe('v2-open-apps')
    expect(targetNameOf('taker-apps')).toBe('v2-taker-apps')
    // 第三方平台组 4 页 v2（2026-11-06 第三方平台批）。
    expect(targetNameOf('thirdparty-xgj-shop')).toBe('v2-xgj-shop')
    expect(targetNameOf('wecom-group-qr')).toBe('v2-wecom-group-qr')
    expect(targetNameOf('system-wechatmp-settings')).toBe('v2-wechatmp-settings')
    expect(targetNameOf('qqbot-settings')).toBe('v2-qqbot-settings')
    // 企微自建应用（D-0379，v2-only）：旧壳条目置灰、v2 有真页。
    expect(targetNameOf('wecom-settings')).toBe('v2-wecom-settings')
    // 「会员管理」「客户分析报表」v2（2026-11-10 客户线）：旧菜单两条（复刻壳页）指向 v2 真页。
    expect(targetNameOf('customers')).toBe('v2-customers')
    expect(targetNameOf('customers-analysis')).toBe('v2-customer-analysis')
    // 「黑名单管理」（客户黑名单）v2 占位（2026-11-11 第七批）：点击不再跳 v1 旧壳。
    expect(targetNameOf('customers-blacklist')).toBe('v2-customer-blacklist')
    // 键集合恰等（评审 P1：只钉阳性断言时，注入新条真实映射不红——2026-10-31 实锤）。
    expect(Object.keys(V2_ROUTE_OVERRIDES).sort()).toEqual(
      [
        'accounts-messages',
        'accounts-profile',
        'customers-analysis',
        'customers-blacklist',
        'agents-alipay-auth',
        'agents-day-report',
        'agents-fund-detail',
        'agents-my-income',
        'agents-my-settlement',
        'agents-online-pay-detail',
        'agents-performance',
        'agents-settlement',
        'app-mp-articles',
        'app-stats',
        'customers',
        'dailiantong-order-templates',
        'dailiantong-stats',
        'feedback',
        'game-config-games',
        'game-config-projects',
        'home',
        'open-apps',
        'order-new',
        'orders',
        'orders-inside-hall',
        'orders-mine',
        'orders-platform-action-log',
        'orders-platform-core',
        'orders-recycle',
        'orders-source-price-log',
        'orders-taobao-ship',
        'qqbot-settings',
        'system-blacklist',
        'system-contract',
        'system-login-restrict',
        'system-notices',
        'system-operation-log',
        'system-order-recognition',
        'system-order-sources',
        'system-pdd-ship',
        'system-perfanalysis',
        'system-platform-auto-reply',
        'system-platform-integration',
        'system-real-name',
        'system-role-authority',
        'system-role-group',
        'system-settings',
        'system-source-system',
        'system-user-new',
        'system-users',
        'system-wechat-group-push',
        'system-wechatmp-settings',
        'taker-apps',
        'thirdparty-xgj-shop',
        'wecom-group-qr',
        'wecom-settings',
        'xgj-receipts',
      ].sort(),
    )
    const all = [...TOP_LEVEL_ITEMS, ...BOTTOM_ITEMS, ...MENU_GROUPS.flatMap((g) => g.items)]
    for (const item of all) {
      if (item.routeName === undefined) continue
      expect(
        router.hasRoute(targetNameOf(item.routeName)),
        `「${item.label}」的 target ${targetNameOf(item.routeName)} 不在路由表`,
      ).toBe(true)
    }
  })

  it('「指派给我的订单」点击 push v2 路由（特例映射②；落点在 /v2 子树内）', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="指派给我的订单"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-my-orders' })
  })

  it('「内部抢单大厅」点击 push v2 路由（特例映射③ D-0352；落点在 /v2 子树内）', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="内部抢单大厅"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-hall' })
  })

  it('「我的相关」组 4 条点击 push 各自的 v2 路由（D-0353 特例映射④–⑦；落点都在 /v2 子树内）', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="我的订单收入"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-income' })
    await wrapper.find('[data-v2-menu="在线结算记录"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-settlement' })
    await wrapper.find('[data-v2-menu="我的资料"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-profile' })
    await wrapper.find('[data-v2-menu="消息通知"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-messages' })
  })

  it('「游戏维护」「发平台设置」两组抽样点击 push 各自的 v2 路由（2026-11-05 特例映射；落点都在 /v2 子树内）', async () => {
    const wrapper = await mountAdminShell(router)
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="游戏管理"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-game-manage' })
    await wrapper.find('[data-v2-menu="订单项目分类与常用项目"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-game-projects' })
    await wrapper.find('[data-v2-menu="交易平台对接设置"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-platform-integration' })
    await wrapper.find('[data-v2-menu="交易平台自动回复"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-platform-auto-reply' })
    await wrapper.find('[data-v2-menu="代练通按日统计"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-dailiantong-stats' })
    await wrapper.find('[data-v2-menu="代练通订单要求模板"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-dailiantong-templates' })
  })
})

describe('CarbonShell：按权限显隐（非管理员会话）', () => {
  beforeEach(() => {
    mePayload = ME_STAFF
  })

  it('无权限的条目不渲染', async () => {
    const wrapper = await mountAdminShell(router)
    for (const label of ['指派给我的订单', '内部抢单大厅', '管理订单', '我的订单收入']) {
      expect(wrapper.find(`[data-v2-menu="${label}"]`).exists(), `「${label}」不应渲染`).toBe(false)
    }
  })

  it('有其他功能维护权限的员工看不到消息推送订阅', async () => {
    mePayload = { ...ME_STAFF, menu_permissions: ['system.pinduoduo_settings'] }
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('[data-v2-group="功能维护"]').exists()).toBe(true)
    expect(wrapper.find('[data-v2-menu="消息推送订阅"]').exists()).toBe(false)
    expect(wrapper.find('[data-v2-menu="内部沟通配置"]').exists()).toBe(false)
  })

  it('整组不可见 ⇒ 不渲染空组头（fail-closed，同 useMenu 的空组不渲染；【第三方平台】按 v2 改名后的组名「功能维护」判）', async () => {
    const wrapper = await mountAdminShell(router)
    for (const name of ['财务相关', '系统维护', '功能维护']) {
      expect(wrapper.find(`[data-v2-group="${name}"]`).exists(), `组「${name}」不应渲染`).toBe(
        false,
      )
    }
  })

  it('只有抄送记录的账号也能从「我的相关」进入审批中心', async () => {
    const wrapper = await mountAdminShell(router)
    expect(renderedSubLabels(wrapper, '我的相关')).toEqual(['审批中心'])
    const push = vi.spyOn(router, 'push')
    await wrapper.get('[data-v2-menu="审批中心"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-approvals' })
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('v2-approvals')
  })

  it('有权组照常渲染，且子条目恰好是权限过滤后的可见子集', async () => {
    const wrapper = await mountAdminShell(router)
    expect(renderedSubLabels(wrapper, '订单管理')).toEqual(['录入订单'])
    expect(renderedSubLabels(wrapper, '系统人员')).toEqual(['人员管理'])
    expect(wrapper.find('[data-v2-menu="问题反馈"]').exists()).toBe(true)
    expect(renderedGroupNames(wrapper)).toEqual(['我的相关', '订单管理', '系统人员'])
  })
})

describe('CarbonShell：置灰（可点判据）与壳级结构', () => {
  // ⭐ E-0325 回归断言（修复前必红）：v2-only 条目 `wecom-settings` 旧表无路由，
  // `disabled` 曾被 useMenu 的旧表判据原样继承 ⇒ v2 侧栏点了没反应（页面从侧栏不可达）。
  // 修法 = `useV2Menu.withTarget` 按**解析后的目标**重判可点 ⇒ 本条钉「可点 + push 对目标」。
  it('「企微自建应用」在 v2 侧栏可点且 push v2-wecom-settings（v2-only 条目按目标表判，E-0325）', async () => {
    const wrapper = await mountAdminShell(router)
    const link = wrapper.find('[data-v2-menu="企微自建应用"]')
    expect(link.exists()).toBe(true)
    expect(link.classes()).not.toContain('cv2-sub-link--disabled')
    expect(link.attributes('aria-disabled')).toBeUndefined()
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await link.trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'v2-wecom-settings' })
  })

  it('目标真不存在（removeRoute 掉**目标** v2-settings）⇒ 置灰且点击不跳（置灰=目标表判不出来才灰，E-0325 口径）', async () => {
    // ⚠️ 按**目标**删（`system-settings` 的 target 是 `v2-settings`）：新口径下可点性跟目标表走。
    router.removeRoute('v2-settings') // 目标表里没有 ⇒ withTarget 判 disabled=true
    const wrapper = await mountAdminShell(router)
    const link = wrapper.find('[data-v2-menu="系统各项设置管理"]')
    expect(link.exists()).toBe(true) // 置灰 = 仍渲染，只是不可点（不是隐藏）
    expect(link.classes()).toContain('cv2-sub-link--disabled')
    expect(link.attributes('aria-disabled')).toBe('true')
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await link.trigger('click')
    expect(push).not.toHaveBeenCalled()
  })

  it('品牌块渲染「团团通」+ 会话里的租户名（合成值）；顶部用户块 = 旧壳同款「名称[职位]」（逐字拼接，无职位不加方括号），且夹在品牌块与菜单之间', async () => {
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('.cv2-brand-name').text()).toBe('团团通')
    expect(wrapper.find('.cv2-brand-tenant').text()).toBe('示例工作室甲')
    // 旧壳 AppAccountBlock 同款拼接（v2 旧形态「职位 · 名称」已废）；钉整串值。
    expect(wrapper.find('.cv2-user-line').text()).toBe('测试管理员[系统管理员]')
    // 位置钉（业务方拍板挪顶部）：aside 直接子级顺序 = 品牌 → 用户块 → 菜单（底部块已废）。
    const sideChildren = Array.from(wrapper.find('.cv2-side').element.children).map(
      (el) => el.classList[0],
    )
    expect(sideChildren).toEqual(['cv2-brand', 'cv2-side-user', 'cv2-nav'])
    // 头像：ME_ADMIN 无 avatar_url ⇒ 回落按名字生成的色板头像（config/avatar.ts 同一生成器，同旧壳）。
    const img = wrapper.find('.cv2-side-user .cv2-avatar').element as HTMLImageElement
    expect(img.src).toBe(avatarDataUri('测试管理员', 60))
  })

  it('顶部用户块：会话带 avatar_url ⇒ 头像直接用会话真值（同旧壳 AppSidebar 的取数，合成 URL）', async () => {
    mePayload = { ...ME_ADMIN, avatar_url: 'https://example.com/avatar-synthetic.png' }
    const wrapper = await mountAdminShell(router)
    const img = wrapper.find('.cv2-side-user .cv2-avatar').element as HTMLImageElement
    expect(img.src).toBe('https://example.com/avatar-synthetic.png')
    expect(wrapper.find('.cv2-user-line').text()).toBe('测试管理员[系统管理员]')
  })

  it('顶部用户块：role 为空串 ⇒ 只显示名称、不加方括号（旧壳逐字语义）', async () => {
    mePayload = { ...ME_ADMIN, role: '' }
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('.cv2-user-line').text()).toBe('测试管理员')
  })

  it('当前项（后台首页）挂 --active 类与 aria-current="page"；无子条目 active', async () => {
    const wrapper = await mountAdminShell(router)
    const active = wrapper.findAll('.cv2-nav-link--active')
    expect(active).toHaveLength(1)
    expect(active[0]!.text()).toBe('后台首页')
    expect(active[0]!.attributes('aria-current')).toBe('page')
    expect(wrapper.findAll('.cv2-sub-link--active')).toHaveLength(0)
  })
})

/* ── 外壳补全（业务方 五项指令，D-0346）：尺寸 / 动画 / 顶栏 / 标签页 ────────── */

// CSS 形状钉读的是 carbon.css 源文本（与 carbonTokens.spec 同一手法）：jsdom 不算 cascade，
// 尺寸与过渡的「值」只能对着源码钉；组件类的落点（哪个类用了哪个 token）也在同一份文本里。
const CARBON_CSS = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../carbon.css'),
  'utf8',
)

describe('CarbonShell 外壳：侧栏宽度（D-0346 放大 → D-0351 收窄）', () => {
  it('宽度 token = 220px（业务方 2026-11-03 两轮定档：240「拉窄一点」→200→实测「太窄了」回调 220，对标 layui 后台侧栏宽档），且 .cv2-side / 顶栏 / 标签页条 / 内容区全部走 var(--tt2-side-w)', () => {
    expect(CARBON_CSS).toMatch(/--tt2-side-w:\s*220px/)
    expect(CARBON_CSS).not.toMatch(/--tt2-side-w:\s*200px/)
    for (const cls of ['.cv2-side', '.cv2-topbar', '.cv2-tabs']) {
      const rule = CARBON_CSS.match(new RegExp(`${cls.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')}\\s*\\{[^}]*\\}`))
      expect(rule, `carbon.css 里应有 ${cls} 规则`).not.toBeNull()
      expect(rule![0]).toContain('var(--tt2-side-w)')
    }
    expect(CARBON_CSS).toMatch(/\.cv2-main\s*\{[^}]*margin-left:\s*var\(--tt2-side-w\)/)
  })

  it('菜单主级条目高 44px token；条目行与组头行都用它（旧壳 --tt-shell-menu-item-h 的比例）', () => {
    expect(CARBON_CSS).toMatch(/--tt2-menu-item-h:\s*44px/)
    expect(CARBON_CSS).toMatch(/\.cv2-nav-link\s*\{[^}]*height:\s*var\(--tt2-menu-item-h\)/)
    expect(CARBON_CSS).toMatch(/\.cv2-nav-head\s*\{[^}]*height:\s*var\(--tt2-menu-item-h\)/)
  })

  it('子条目行高 34px token（业务方 D-0351「展开的子菜单之间隔太远了 要隔近一点的」；比旧壳 38px 再紧一档），且主级不受影响', () => {
    expect(CARBON_CSS).toMatch(/--tt2-sub-item-h:\s*34px/)
    expect(CARBON_CSS).toMatch(/\.cv2-sub-link\s*\{[^}]*min-height:\s*var\(--tt2-sub-item-h\)/)
    // 主级条目仍走 44px（收窄只动子条目）。
    expect(CARBON_CSS).toMatch(/\.cv2-nav-link\s*\{[^}]*height:\s*var\(--tt2-menu-item-h\)/)
  })

  it('顶栏与标签页条的高度走 token（顶栏 50px 旧壳比例；签条 ⭐ D-0354 加大到 42px），标签页在顶栏正下方', () => {
    expect(CARBON_CSS).toMatch(/--tt2-topbar-h:\s*50px/)
    expect(CARBON_CSS).toMatch(/--tt2-tabs-h:\s*42px/)
    expect(CARBON_CSS).not.toMatch(/--tt2-tabs-h:\s*40px/)
    expect(CARBON_CSS).toMatch(/\.cv2-topbar\s*\{[^}]*height:\s*var\(--tt2-topbar-h\)/)
    expect(CARBON_CSS).toMatch(/\.cv2-tabs\s*\{[^}]*top:\s*var\(--tt2-topbar-h\)/)
    expect(CARBON_CSS).toMatch(/\.cv2-tabs\s*\{[^}]*height:\s*var\(--tt2-tabs-h\)/)
  })

  it('⭐ D-0354 标签页加大 + 色层：签 14px 字 / 左右 20px 内边距、清空按钮同步加大；当前签 = 主色实底白字（白 80% 关闭 ×，悬停全白）、非当前悬停 = 青绿浅底主色深字（CSS 形状钉）', () => {
    // 签面尺寸与字号（业务方「签偏小」：36px/13px → 42px/14px、内边距 20px）。
    expect(CARBON_CSS).toMatch(/\.cv2-tab\s*\{[^}]*padding:\s*0 20px/)
    expect(CARBON_CSS).toMatch(/\.cv2-tab\s*\{[^}]*font-size:\s*14px/)
    expect(CARBON_CSS).toMatch(/\.cv2-tabs-clear\s*\{[^}]*font-size:\s*14px/)
    // 当前签 = 实底青绿整块 + 白字（对标版式、色取本系统主题）。
    expect(CARBON_CSS).toMatch(
      /\.cv2-tab--active\s*\{[^}]*background:\s*var\(--tt2-primary\)[^}]*color:\s*var\(--tt2-panel\)/,
    )
    // 当前签不再走「白底 + 主色下条」旧皮。
    expect(CARBON_CSS).not.toMatch(/\.cv2-tab--active\s*\{[^}]*border-bottom-color/)
    // 非当前悬停 = 浅青绿底 + 主色深字；:not 排除当前签（实底不换皮）。
    expect(CARBON_CSS).toMatch(
      /\.cv2-tab:hover:not\(\.cv2-tab--active\)\s*\{[^}]*background:\s*var\(--tt2-tool-bg\)[^}]*color:\s*var\(--tt2-primary-deep\)/,
    )
    // 当前签上的关闭 ×：白 80% 透明（token），悬停全白。
    expect(CARBON_CSS).toMatch(/--tt2-tab-close:\s*rgba\(255, 255, 255, 0.8\)/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-tab--active \.cv2-tab-close\s*\{[^}]*color:\s*var\(--tt2-tab-close\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-tab--active \.cv2-tab-close:hover\s*\{[^}]*color:\s*var\(--tt2-panel\)/,
    )
  })
})

describe('CarbonShell 外壳：展开/收起滑动动画（业务方「原版是滑下去展开的」）', () => {
  it('动画 = grid 0fr↔1fr 行高过渡 + 200ms token；箭头旋转同 token 过渡', () => {
    expect(CARBON_CSS).toMatch(/--tt2-motion:\s*200ms/)
    expect(CARBON_CSS).toMatch(/\.cv2-sub-wrap\s*\{[^}]*grid-template-rows:\s*0fr/)
    expect(CARBON_CSS).toMatch(/\.cv2-nav-group--open \.cv2-sub-wrap\s*\{[^}]*grid-template-rows:\s*1fr/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-sub-wrap\s*\{[^}]*transition:\s*grid-template-rows var\(--tt2-motion\)/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-nav-arrow\s*\{[^}]*transition:\s*transform var\(--tt2-motion\)/,
    )
  })

  it('prefers-reduced-motion 下直接切换（全局兜底块仍在，动画 token 不绕过它）', () => {
    expect(CARBON_CSS).toContain('@media (prefers-reduced-motion: reduce)')
    const media = CARBON_CSS.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*$/)
    expect(media, 'reduced-motion 块应存在且能取出').not.toBeNull()
    expect(media![0]).toContain('transition: none')
  })
})

describe('CarbonShell 外壳：顶部用户块（业务方拍板挪顶部，竖排居中）', () => {
  it('宽档竖排：容器纵向居中、头像 60px 圆、名称行居中 13px、下缘分隔线隔开菜单（CSS 形状钉）', () => {
    expect(CARBON_CSS).toMatch(
      /\.cv2-side-user\s*\{[^}]*flex-direction:\s*column[^}]*align-items:\s*center/,
    )
    expect(CARBON_CSS).toMatch(
      /\.cv2-avatar\s*\{[^}]*width:\s*60px[^}]*height:\s*60px[^}]*border-radius:\s*50%/,
    )
    expect(CARBON_CSS).toMatch(/\.cv2-user-line\s*\{[^}]*text-align:\s*center/)
    expect(CARBON_CSS).toMatch(
      /\.cv2-side-user\s*\{[^}]*border-bottom:\s*1px solid var\(--tt2-side-line\)/,
    )
  })
})

describe('CarbonShell 外壳：顶栏（左页题 / 右客服工具+账号+退出）', () => {
  it('页题 = 当前路由 meta.title；导航到 v2 子路由跟着换', async () => {
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('.cv2-topbar-title').text()).toBe('后台首页 v2')
    await router.push('/v2/my-orders')
    await flushPromises()
    expect(wrapper.find('.cv2-topbar-title').text()).toBe('指派给我的订单')
  })

  it('「客服工具」链接在顶栏右区容器内、账号区之前（右上角正确位置；href/target/rel 钉值）', async () => {
    const wrapper = await mountAdminShell(router)
    const link = wrapper.find('[data-v2="kefu-link"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toBe('客服工具')
    expect(link.attributes('href')).toBe('/kefu-tools/')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener')
    // 位置钉：右区容器内，且排在账号信息之前（顶到右、靠左一点 = 旧壳语义）。
    const right = wrapper.find('.cv2-topbar-right')
    expect(right.find('[data-v2="kefu-link"]').exists()).toBe(true)
    const user = wrapper.find('.cv2-topbar-user')
    const pos = link.element.compareDocumentPosition(user.element)
    expect(pos & Node.DOCUMENT_POSITION_FOLLOWING, '客服工具必须在账号信息之前').toBeTruthy()
    // 结构上不存在旧壳那个「布局洞」：链接不在顶栏首个子元素的位置。
    // ⭐ 响应式窄档：首子元素 = ☰（≤720 抽屉档开关，宽档 CSS display:none），
    // 页题紧随其后；「客服工具」仍在右区容器、不在顶栏头部。
    const topbarChildren = Array.from(wrapper.find('.cv2-topbar').element.children).map((el) =>
      el.classList[0],
    )
    expect(topbarChildren[0]).toBe('cv2-burger')
    expect(topbarChildren).toContain('cv2-topbar-title')
    expect(topbarChildren[0]).not.toBe('cv2-kefu-link')
  })

  it('账号区渲染「职位 · 显示名」（会话快照，合成值）', async () => {
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('.cv2-topbar-user').text()).toBe('系统管理员 · 测试管理员')
  })

  it('退出：先终结本地会话、再发登出请求，最后 replace 到登录页（旧顶栏同一条序列）', async () => {
    const wrapper = await mountAdminShell(router)
    const store = useSessionStore()
    await wrapper.find('[data-v2="logout"]').trigger('click')
    // ⚠️ 对顺序敏感：不 flush 就断言——session.logout() 在发请求**之前**就终结本地。
    expect(store.status).toBe('guest')
    expect(store.user).toBeNull()
    await flushPromises()
    expect(apiLogout).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('CarbonShell 外壳：标签页条（复用 usePageTabs，炭火签面）', () => {
  it('挂载即开首页签（/v2，激活态）；导航累积开签、去重键 = fullPath', async () => {
    const wrapper = await mountAdminShell(router)
    let tabs = wrapper.findAll('[data-v2-tab]')
    expect(tabs).toHaveLength(1)
    expect(tabs[0]!.attributes('data-v2-tab')).toBe('/v2')
    expect(tabs[0]!.classes()).toContain('cv2-tab--active')
    expect(tabs[0]!.text()).toContain('后台首页 v2')

    await router.push('/v2/my-orders')
    await flushPromises()
    tabs = wrapper.findAll('[data-v2-tab]')
    expect(tabs).toHaveLength(2)
    expect(tabs[1]!.attributes('data-v2-tab')).toBe('/v2/my-orders')
    expect(tabs[1]!.classes()).toContain('cv2-tab--active')
    expect(tabs[1]!.text()).toContain('指派给我的订单')

    // 再回到已在列的地址 = 只激活、不追加（composable 的去重语义）。
    await router.push('/v2')
    await flushPromises()
    expect(wrapper.findAll('[data-v2-tab]')).toHaveLength(2)
    expect(wrapper.find('[data-v2-tab="/v2"]').classes()).toContain('cv2-tab--active')
  })

  it('点签切换 = push 那条 fullPath；首签无关闭按钮，其余签可关', async () => {
    const wrapper = await mountAdminShell(router)
    await router.push('/v2/my-orders')
    await flushPromises()
    const home = wrapper.find('[data-v2-tab="/v2"]')
    const other = wrapper.find('[data-v2-tab="/v2/my-orders"]')
    expect(home.find('.cv2-tab-close').exists()).toBe(false)
    expect(other.find('.cv2-tab-close').exists()).toBe(true)

    const push = vi.spyOn(router, 'push')
    await home.trigger('click')
    expect(push).toHaveBeenCalledWith('/v2')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/v2')
  })

  it('关掉激活签 → 邻居接管（先右后左在 composable；这里钉「关掉右签回左签」）', async () => {
    const wrapper = await mountAdminShell(router)
    await router.push('/v2/my-orders')
    await flushPromises()
    await wrapper.find('[data-v2-tab="/v2/my-orders"] .cv2-tab-close').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/v2')
    expect(wrapper.findAll('[data-v2-tab]')).toHaveLength(1)
  })

  it('清空标签页 → 只剩首页签并停在 /v2（在首页签上清空也不落零页签）', async () => {
    const wrapper = await mountAdminShell(router)
    await router.push('/v2/my-orders')
    await flushPromises()
    await wrapper.find('[data-v2="tabs-clear"]').trigger('click')
    await flushPromises()
    const tabs = wrapper.findAll('[data-v2-tab]')
    expect(tabs).toHaveLength(1)
    expect(tabs[0]!.attributes('data-v2-tab')).toBe('/v2')
    expect(tabs[0]!.classes()).toContain('cv2-tab--active')
    expect(router.currentRoute.value.path).toBe('/v2')

    // 在首页签自己上清空（唯一容易漏的边界）：仍剩首签、仍是激活。
    await wrapper.find('[data-v2="tabs-clear"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[data-v2-tab]')).toHaveLength(1)
    expect(wrapper.find('[data-v2-tab="/v2"]').classes()).toContain('cv2-tab--active')
  })

  it('录单和普通页面都保留输入，刷新当前签不清掉其它签', async () => {
    const wrapper = await mountAdminShell(router)
    const draft = () => wrapper.get<HTMLInputElement>('[data-v2-test="order-draft"]:not([inert] *)')
    const other = () => wrapper.get<HTMLInputElement>('[data-v2-test="other-form"]')
    await router.push('/v2/order-new')
    await draft().setValue('一号草稿')
    await router.push('/v2/my-orders')
    await other().setValue('其他页面')
    await wrapper.get('[data-v2-tab="/v2/order-new"]').trigger('click')
    await flushPromises()
    expect(draft().element.value).toBe('一号草稿')
    await wrapper.get('[data-v2-tab="/v2/my-orders"]').trigger('click')
    await flushPromises()
    expect(other().element.value).toBe('其他页面')
    await other().setValue('其他页面刷新')
    await wrapper.get('[data-v2="tabs-refresh"]').trigger('click')
    expect(other().element.value).toBe('')

    await router.push('/v2/order-new?sample=2')
    expect(draft().element.value).toBe('')
    await draft().setValue('二号草稿')
    await wrapper.get('[data-v2-tab="/v2/order-new"]').trigger('click')
    await flushPromises()
    expect(draft().element.value).toBe('一号草稿')
    await wrapper.get('[data-v2="tabs-refresh"]').trigger('click')
    expect(draft().element.value).toBe('')
    await wrapper.get('[data-v2-tab="/v2/order-new?sample=2"]').trigger('click')
    await flushPromises()
    expect(draft().element.value).toBe('二号草稿')
  })

  it('管理订单切签保留各自筛选和勾选，不重查列表；只刷新当前签', async () => {
    loadOrderList.mockClear()
    const wrapper = await mountAdminShell(router)
    const first = '/v2/order-manage?sample=first'
    const second = '/v2/order-manage?sample=second'
    const list = (key: string) => wrapper.get(`[data-test-order-list="${key}"]`)
    const filter = (key: string) => list(key).get<HTMLInputElement>('[data-test-order-filter]')
    const selection = (key: string) => list(key).get<HTMLInputElement>('[data-test-order-selection]')

    await router.push(first)
    await filter(first).setValue('筛过的订单')
    await selection(first).setValue(true)
    await wrapper.get('[data-v2-tab="/v2"]').trigger('click')
    await flushPromises()
    expect(list(first).element.closest('[inert]')).not.toBeNull()
    expect(list(first).element.parentElement?.style.display).toBe('none')

    await router.push(second)
    expect(filter(second).element.value).toBe('second')
    expect(selection(second).element.checked).toBe(false)
    await wrapper.get(`[data-v2-tab="${first}"]`).trigger('click')
    await flushPromises()
    expect(list(first).element.closest('[inert]')).toBeNull()
    expect(list(first).element.parentElement?.style.display).toBe('')
    expect(list(second).element.parentElement?.style.display).toBe('none')
    expect(filter(first).element.value).toBe('筛过的订单')
    expect(selection(first).element.checked).toBe(true)
    expect(loadOrderList.mock.calls).toEqual([[first], [second]])

    await wrapper.get(`[data-v2-tab="${second}"]`).trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2="tabs-refresh"]').trigger('click')
    expect(loadOrderList.mock.calls).toEqual([[first], [second], [second]])
    expect(selection(first).element.checked).toBe(true)
    expect(filter(first).element.value).toBe('筛过的订单')

    await wrapper.get(`[data-v2-tab="${first}"] .cv2-tab-close`).trigger('click')
    expect(wrapper.find(`[data-test-order-list="${first}"]`).exists()).toBe(false)
    await router.push(first)
    expect(selection(first).element.checked).toBe(false)
    expect(filter(first).element.value).toBe('first')
  })

  it('管理订单清空标签或收窄会话的数据范围时销毁旧列表', async () => {
    loadOrderList.mockClear()
    const wrapper = await mountAdminShell(router)
    const store = useSessionStore()
    const url = '/v2/order-manage'
    const list = () => wrapper.find(`[data-test-order-list="${url}"]`)
    await router.push(url)
    await wrapper.get('[data-v2="tabs-clear"]').trigger('click')
    await flushPromises()
    expect(list().exists()).toBe(false)
    await router.push(url)
    expect(loadOrderList.mock.calls).toEqual([[url], [url]])

    const staff = { ...ME_STAFF, menu_permissions: ['order.list'], game_ids: [1, 2] }
    mePayload = staff
    await store.refresh()
    await flushPromises()
    expect(loadOrderList.mock.calls).toEqual([[url], [url], [url]])
    await router.push('/v2')
    mePayload = { ...staff, game_ids: [1] }
    await store.refresh()
    await flushPromises()
    expect(list().exists()).toBe(false)
    await router.push(url)
    expect(loadOrderList.mock.calls).toEqual([[url], [url], [url], [url]])

    mePayload = { ...staff, menu_permissions: [] }
    await store.refresh()
    await flushPromises()
    expect(list().exists()).toBe(false)
  })

  it('关闭未激活的录单签、清空标签、退出后，重新打开录单页都是空表单', async () => {
    const wrapper = await mountAdminShell(router)
    const draft = () => wrapper.get<HTMLInputElement>('[data-v2-test="order-draft"]:not([inert] *)')
    await router.push('/v2/order-new')
    await draft().setValue('待关闭的草稿')
    await wrapper.get('[data-v2-tab="/v2"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-v2-tab="/v2/order-new"] .cv2-tab-close').trigger('click')
    await router.push('/v2/order-new')
    expect(draft().element.value).toBe('')

    await draft().setValue('待清空的草稿')
    await wrapper.get('[data-v2="tabs-clear"]').trigger('click')
    await flushPromises()
    await router.push('/v2/order-new')
    expect(draft().element.value).toBe('')

    await draft().setValue('待退出的草稿')
    await wrapper.get('[data-v2="logout"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
    await useSessionStore().login('tester_admin', 'test-password')
    await router.push('/v2/order-new')
    expect(draft().element.value).toBe('')
  })

  it('刷新会话时只为仍有录单权限的同一账号保留草稿', async () => {
    const wrapper = await mountAdminShell(router)
    const store = useSessionStore()
    const draft = () => wrapper.get<HTMLInputElement>('[data-v2-test="order-draft"]:not([inert] *)')
    await router.push('/v2/order-new')
    await draft().setValue('管理员草稿')
    await router.push('/v2')
    mePayload = { ...ME_ADMIN, display_name: '更新后管理员' }
    await store.refresh()
    await router.push('/v2/order-new')
    expect(draft().element.value).toBe('管理员草稿')

    await router.push('/v2')
    mePayload = ME_STAFF
    await store.refresh()
    await router.push('/v2/order-new')
    expect(draft().element.value).toBe('')

    await draft().setValue('客服草稿')
    await router.push('/v2')
    mePayload = { ...ME_STAFF, menu_permissions: [] }
    await store.refresh()
    await router.push('/v2/order-new')
    expect(wrapper.find('[data-v2-test="order-draft"]:not([inert] *)').exists()).toBe(false)

    mePayload = ME_STAFF
    await store.refresh()
    await flushPromises()
    expect(draft().element.value).toBe('')
  })
})

/* ── 响应式三档（⭐ 2026-11-01 响应式专包；断点口径 = carbon.css 文件头注释）─────────────
 * 240（默认）→ 72 图标栏（≤1100）→ 顶部抽屉（≤720）。CSS 形状钉读 carbon.css 源文本
 * （@media 块用括号配对取出，与 carbonTokens.spec 同一手法）；抽屉开合与选完自动收是
 * 组件行为，用挂载真点（断点判定在 CSS，jsdom 里类翻转照常可断）。 */

/** 取 `@media (query)` 的整块（括号配对；取不到 = 响亮失败）。 */
function mediaBlock(source: string, query: string): string {
  const marker = source.indexOf(`@media (${query})`)
  if (marker < 0) throw new Error(`carbon.css 里找不到 @media (${query})——响应式块丢了`)
  const open = source.indexOf('{', marker)
  let depth = 0
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1
    if (source[i] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(open + 1, i)
    }
  }
  throw new Error('@media 块括号不配对')
}

describe('CarbonShell 外壳：响应式三档（响应式专包 2026-11-01）', () => {
  it('断点口径恰为四个字面量（1100/900/720/560），与 carbon.css 文件头注释一致', () => {
    expect(CARBON_CSS.match(/@media \(max-width: \d+px\)/g)).toEqual([
      '@media (max-width: 1100px)',
      '@media (max-width: 900px)',
      '@media (max-width: 720px)',
      '@media (max-width: 560px)',
    ])
  })

  it('≤1100 图标栏档：72px token；侧栏/内容区/顶栏/签条随档联动；条目只显图标、组子菜单改右浮层', () => {
    expect(CARBON_CSS).toMatch(/--tt2-side-w-rail:\s*72px/)
    const rail = mediaBlock(CARBON_CSS, 'max-width: 1100px')
    expect(rail).toMatch(/\.cv2-side\s*\{[^}]*width:\s*var\(--tt2-side-w-rail\)/)
    expect(rail).toMatch(/\.cv2-main\s*\{[^}]*margin-left:\s*var\(--tt2-side-w-rail\)/)
    expect(rail).toMatch(/\.cv2-topbar\s*\{[^}]*left:\s*var\(--tt2-side-w-rail\)/)
    expect(rail).toMatch(/\.cv2-tabs\s*\{[^}]*left:\s*var\(--tt2-side-w-rail\)/)
    // 条目只显图标：文字/箭头藏、组子菜单浮层（absolute 溢出 72px 栏，overflow 放行）。
    expect(rail).toMatch(/\.cv2-nav-link span,\s*\n\s*\.cv2-nav-head span,\s*\n\s*\.cv2-nav-head \.cv2-nav-arrow\s*\{[^}]*display:\s*none/)
    expect(rail).toMatch(/\.cv2-sub-list\s*\{[^}]*position:\s*absolute/)
    expect(rail).toMatch(/\.cv2-nav\s*\{[^}]*overflow:\s*visible/)
    // 浮层是深炭底（token），不是裸色。
    expect(rail).toMatch(/\.cv2-sub-list\s*\{[^}]*background:\s*var\(--tt2-side\)/)
    // 顶部用户块：图标栏档退化成居中小头像（40px），名称文字藏掉。
    expect(rail).toMatch(/\.cv2-avatar\s*\{[^}]*width:\s*40px[^}]*height:\s*40px/)
    expect(rail).toMatch(/\.cv2-user-line\s*\{[^}]*display:\s*none/)
  })

  it('≤720 抽屉档：☰ 宽档默认不渲染、抽屉档才出现；侧栏 transform 滑入 + 遮罩盖内容；margin 联动归 0', () => {
    expect(CARBON_CSS).toMatch(/\.cv2-burger\s*\{[^}]*display:\s*none/)
    const drawer = mediaBlock(CARBON_CSS, 'max-width: 720px')
    expect(drawer).toMatch(/\.cv2-burger\s*\{[^}]*display:\s*inline-flex/)
    expect(drawer).toMatch(/\.cv2-main\s*\{[^}]*margin-left:\s*0/)
    expect(drawer).toMatch(/\.cv2-topbar\s*\{[^}]*left:\s*0/)
    expect(drawer).toMatch(/\.cv2-tabs\s*\{[^}]*left:\s*0/)
    // 抽屉 = 恢复 240 全量菜单、平时收在屏左外、开着才滑入（transform 由根类驱动）。
    expect(drawer).toMatch(/\.cv2-side\s*\{[^}]*width:\s*var\(--tt2-side-w\)/)
    expect(drawer).toMatch(/\.cv2-side\s*\{[^}]*transform:\s*translateX\(-100%\)/)
    expect(drawer).toMatch(
      /\.cv2-shell--drawer-open \.cv2-side\s*\{[^}]*transform:\s*translateX\(0\)/,
    )
    // 遮罩：开着才显示、盖在内容上（z 低于抽屉本体）、色走 token。
    expect(drawer).toMatch(
      /\.cv2-shell--drawer-open \.cv2-scrim\s*\{[^}]*display:\s*block[^}]*background:\s*var\(--tt2-scrim\)/,
    )
    expect(drawer).toMatch(/\.cv2-shell--drawer-open \.cv2-scrim\s*\{[^}]*z-index:\s*25/)
    expect(CARBON_CSS).toMatch(/\.cv2-side\s*\{[^}]*z-index:\s*30/)
    // 图标栏档的收起在抽屉里全部还原（文字块/条目文字/箭头/子列表回内联文档流）。
    expect(drawer).toMatch(/\.cv2-nav-link span,\s*\n\s*\.cv2-nav-head span\s*\{[^}]*display:\s*inline/)
    expect(drawer).toMatch(/\.cv2-sub-list\s*\{[^}]*position:\s*static/)
    // 两列网格堆叠单列（包 C 同口径：资料表单 / 消息两栏）。
    expect(drawer).toMatch(/\.cv2-form-grid\s*\{[^}]*grid-template-columns:\s*1fr/)
    expect(drawer).toMatch(/\.cv2-msg-layout\s*\{[^}]*flex-direction:\s*column/)
    // 顶部用户块在抽屉里还原宽档竖排（60px 头像 + 名称文字恢复显示）。
    expect(drawer).toMatch(/\.cv2-avatar\s*\{[^}]*width:\s*60px[^}]*height:\s*60px/)
    expect(drawer).toMatch(/\.cv2-user-line\s*\{[^}]*display:\s*block/)
  })

  it('≤900/≤560 状态卡片：4 张/行 → 2 张/行（grid 定列数；560 块排在 900 后覆盖）', () => {
    const four = mediaBlock(CARBON_CSS, 'max-width: 900px')
    expect(four).toMatch(/\.cv2-scards\s*\{[^}]*grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/)
    expect(four).toMatch(/\.cv2-scard\s*\{[^}]*width:\s*auto/)
    const two = mediaBlock(CARBON_CSS, 'max-width: 560px')
    expect(two).toMatch(/\.cv2-scards\s*\{[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/)
  })

  it('☰ 开关抽屉：点击翻转根类 cv2-shell--drawer-open；选完菜单自动收（go 统一收口）', async () => {
    const wrapper = await mountAdminShell(router)
    const root = wrapper.find('.ttv2')
    expect(root.classes()).not.toContain('cv2-shell--drawer-open')
    const burger = wrapper.find('[data-v2="nav-burger"]')
    expect(burger.exists()).toBe(true)
    expect(burger.attributes('aria-controls')).toBe('v2-side-nav')
    await burger.trigger('click')
    expect(root.classes()).toContain('cv2-shell--drawer-open')
    expect(burger.attributes('aria-expanded')).toBe('true')
    // 遮罩点了也收。
    await wrapper.find('[data-v2="nav-scrim"]').trigger('click')
    expect(root.classes()).not.toContain('cv2-shell--drawer-open')
    // 选完菜单自动收：开着抽屉点一条导航，类被 go() 摘掉。
    await burger.trigger('click')
    expect(root.classes()).toContain('cv2-shell--drawer-open')
    const push = vi.spyOn(router, 'push').mockResolvedValue(undefined)
    await wrapper.find('[data-v2-menu="后台首页"]').trigger('click')
    expect(root.classes()).not.toContain('cv2-shell--drawer-open')
    expect(push).toHaveBeenCalledWith({ name: 'v2-home' })
  })

  it('图标栏档的 tooltip = 条目/组头上的原生 title（直挂条目与组头都有；子条目在浮层里带全文不需要）', async () => {
    const wrapper = await mountAdminShell(router)
    expect(wrapper.find('[data-v2-menu="后台首页"]').attributes('title')).toBe('后台首页')
    expect(wrapper.find('[data-v2-menu="问题反馈"]').attributes('title')).toBe('问题反馈')
    expect(wrapper.find('[data-v2-group-head="订单管理"]').attributes('title')).toBe('订单管理')
  })
})
