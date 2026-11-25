/** `/v2` 炭火外壳的侧栏菜单 —— **薄封装**，本文件不持有任何菜单数据。业务方 原话「直接照抄 先把框架和菜单搞好」：炭火只换视觉，菜单的**名称、顺序、分组结构、按权限显隐、可点判定**必须与旧侧边栏完全一致。「照抄」的唯一可靠实现 = **直接消费同一份配置**（与 `layouts/AppSidebar.vue` 同一条数据流）：- 骨架（条目 / 分组 / hidden / permissions）：`config/menu.ts`；- 可见性与可点判定（`canSeeItem` / `canSeeGroup` / `isClickable`）：`composables/useMenu.ts`。⛔ 不在这里手抄条目名——手抄第二份必然漂移（本文件的前身「手写 8 项数组」就是反例，已删）。**特例映射（D-0352/D-0353/D-0355 起，2026-11-04 增「管理订单」）**：v2 已建页面的旧菜单条目指向 v2 路由——- 旧「后台首页」（routeName `home`）→ `v2-home`：/v2 就是后台首页的新壳，若照抄跳旧
 * `home`，点「后台首页」会离开 v2、菜单里反而没有 v2 自己；- 旧「指派给我的订单」（routeName `orders-mine`）→ `v2-my-orders`（2026-10-31 业务方指令④：该页已有 v2 版），同一条理由；- 旧「内部抢单大厅」（routeName `orders-inside-hall`）→ `v2-hall`（2026-10-31 D-0352：该页已有 v2 版），同一条理由。- 「我的相关」组 4 条（D-0353）：我的订单收入 / 在线结算记录 / 我的资料 /我的消息各有 v2 版（`v2-income` / `v2-settlement` / `v2-profile` / `v2-messages`）。- 「录入订单」（D-0355）：routeName `order-new` → `v2-order-new`（该页已有 v2 版）。- 「管理订单」（2026-11-04）：routeName `orders` → `v2-order-manage`。- 「第三方平台订单管理」「找回删除的订单」（2026-11-05 设计线）：`orders-platform-core` → `v2-platform-orders`、`orders-recycle` → `v2-order-recycle`。- 「系统人员」组其余 5 页（2026-11-05 系统人员批次）：`system-users` / `system-real-name` / `system-login-restrict` / `system-blacklist` / `system-contract` → 各自 v2 页。- 「游戏维护」「发平台设置」两组 6 条（2026-11-05 设计线，分支 feat/v2-game-platform）：`game-config-games` → `v2-game-manage`、`game-config-projects` → `v2-game-projects`、`system-platform-integration` → `v2-platform-integration`、`system-platform-auto-reply` → `v2-platform-auto-reply`、`dailiantong-stats` → `v2-dailiantong-stats`、`dailiantong-order-templates` → `v2-dailiantong-templates`。- 「系统维护」组 11 条（2026-11-06 系统维护批次）：`system-role-authority` / `system-role-group` / `system-order-recognition` / `system-order-sources` / `system-source-system` / `agents-alipay-auth` / `system-pdd-ship` / `system-notices` / `system-operation-log` / `system-wechat-group-push` / `system-settings` → 各自 v2 页。「数据概览」「问题反馈」（bottom 区）「发布文章作品」3 条
 * （2026-11-06 APP与小程序+问题反馈批）：`app-stats` / `feedback` / `app-mp-articles` → 各自 v2 页。- 「开放平台」组 3 页（2026-11-06 开放平台批）：`system-perfanalysis` / `open-apps` / `taker-apps` → 各自 v2 页。其余条目 target = 原旧路由 name，点击 = `router.push({ name })`（同一 SPA、同一登录态，跳旧页由旧壳接管渲染）。财务族第一批（2026-11-06）：`xgj-receipts` → v2 页；「打手钱包」进 `V2_HIDDEN_ROUTE_NAMES`、「团团通资金明细与提现」组内重排到【财务相关】组末项（`moveGroupTailToLast`，渲染层重排、`config/menu.ts` 零改动）。- 「第三方平台」组（2026-11-06 第三方平台批）：`thirdparty-xgj-shop` / `wecom-group-qr` / `system-wechatmp-settings` / `qqbot-settings` → 各自 v2 页；组名渲染层改名「第三方平台」→「功能维护」、条目改名「自动上传企微群码到订单截图」→「自动上传群码」（`V2_GROUP_LABEL_OVERRIDES` / `V2_ITEM_LABEL_OVERRIDES`）、「马上来查码对接」（`system-order-qr-fetch`）进 `V2_HIDDEN_ROUTE_NAMES`（并入群码页）。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useMenu } from '@/composables/useMenu'
import type { MenuItem } from '@/config/menu'

/** v2 自己的路由 name（`router/index.ts` 的 /v2 子树）。 */
export const V2_HOME = 'v2-home'
export const V2_MY_ORDERS = 'v2-my-orders'
export const V2_HALL = 'v2-hall'
export const V2_INCOME = 'v2-income'
export const V2_SETTLEMENT = 'v2-settlement'
export const V2_PROFILE = 'v2-profile'
export const V2_MESSAGES = 'v2-messages'
export const V2_APPROVALS = 'v2-approvals'
export const V2_ORDER_NEW = 'v2-order-new'
export const V2_ORDER_MANAGE = 'v2-order-manage'
export const V2_PLATFORM_ORDERS = 'v2-platform-orders'
export const V2_ORDER_RECYCLE = 'v2-order-recycle'
// 「游戏维护」「发平台设置」两组 6 页（2026-11-05 设计线）。
export const V2_GAME_MANAGE = 'v2-game-manage'
export const V2_GAME_PROJECTS = 'v2-game-projects'
export const V2_PLATFORM_INTEGRATION = 'v2-platform-integration'
export const V2_PLATFORM_AUTO_REPLY = 'v2-platform-auto-reply'
export const V2_DAILIANTONG_STATS = 'v2-dailiantong-stats'
export const V2_DAILIANTONG_TEMPLATES = 'v2-dailiantong-templates'
export const V2_USER_NEW = 'v2-user-new'
export const V2_ORDER_ACTION_LOG = 'v2-order-action-log'
export const V2_SHIP_LOG = 'v2-ship-log'
export const V2_UPSTREAM_PRICE_LOG = 'v2-upstream-price-log'
// 「系统人员」组其余 5 页（2026-11-05 系统人员批次）：人员管理 / 人员实名记录与配置 /
// 登录限制设置 / 人员黑名单 / 员工电子合同管理。
export const V2_USER_LIST = 'v2-user-list'
export const V2_REAL_NAME = 'v2-real-name'
export const V2_LOGIN_RESTRICT = 'v2-login-restrict'
export const V2_BLACKLIST = 'v2-blacklist'
export const V2_CONTRACT = 'v2-contract'
// 「闲管家收款明细」v2。
export const V2_XGJ_RECEIPTS = 'v2-xgj-receipts'
// 财务族第二批（2026-11-06）：打手与客服报酬 / 结算记录 / 经营报表 / 在线结算明细 /
// 团团通资金明细与提现（后者在 v2 侧栏排【财务相关】组末项，override 后点击进 v2 页）。
export const V2_PERFORMANCE = 'v2-performance'
export const V2_BILL_SUMMARY = 'v2-bill-summary'
export const V2_DAY_REPORT = 'v2-day-report'
export const V2_ONLINE_PAY_DETAIL = 'v2-online-pay-detail'
export const V2_FUND_DETAIL = 'v2-fund-detail'
// 「系统维护」组 11 页 v2（2026-11-06 系统维护批次）：角色职位权限 / 角色职位分组 /
// 录单识别设置 / 订单来源管理 / 绑定上家系统设置 / 在线结算支付宝授权 / 其它平台店铺对接 /
// 系统内部公告管理 / 系统操作日志管理 / 自动发消息到微信群设置 / 系统各项设置管理。
export const V2_ROLE_AUTHORITY = 'v2-role-authority'
export const V2_ROLE_GROUP = 'v2-role-group'
export const V2_ORDER_RECOGNITION = 'v2-order-recognition'
export const V2_ORDER_SOURCES = 'v2-order-sources'
export const V2_SOURCE_SYSTEM = 'v2-source-system'
export const V2_ALIPAY_AUTH = 'v2-alipay-auth'
export const V2_PDD_SHIP = 'v2-pdd-ship'
export const V2_NOTICES = 'v2-notices'
export const V2_OPERATION_LOG = 'v2-operation-log'
export const V2_WECHAT_GROUP_PUSH = 'v2-wechat-group-push'
export const V2_SETTINGS = 'v2-settings'
// 「数据概览」「问题反馈」（bottom 区）「发布文章作品」v2（2026-11-06 APP与小程序+问题反馈批）。
export const V2_APP_STATS = 'v2-app-stats'
export const V2_FEEDBACK = 'v2-feedback'
export const V2_ARTICLES = 'v2-articles'
// 「开放平台」组 3 页 v2（2026-11-06 开放平台批）：性能分析 / 应用管理 / 打手对接。
export const V2_PERFANALYSIS = 'v2-perfanalysis'
export const V2_OPEN_APPS = 'v2-open-apps'
export const V2_TAKER_APPS = 'v2-taker-apps'
// 「第三方平台」组 4 页 v2（2026-11-06 第三方平台批）：闲管家店铺对接 / 自动上传群码
// （= 企微群码 + 马上来查码两页合一页）/ 微信公众号设置 / QQ机器人设置。
export const V2_XGJ_SHOP = 'v2-xgj-shop'
export const V2_WECOM_GROUP_QR = 'v2-wecom-group-qr'
export const V2_WECHATMP_SETTINGS = 'v2-wechatmp-settings'
export const V2_QQBOT_SETTINGS = 'v2-qqbot-settings'
// 「企微自建应用」v2（D-0379，2026-11-11）：**v2-only 页**——只有 v2 有真页。
// 两张真值表各自判各的（E-0325）：旧壳按旧表判 ⇒ 条目置灰（D-0344 旧前端不动）；
// v2 侧栏按**解析后的目标**判（`useV2Menu.withTarget`）⇒ 可点、直达本页。
export const V2_WECOM_SETTINGS = 'v2-wecom-settings'
// 「会员管理」v2（2026-11-10 客户线）：炭火真页，后端同线新增两个只读端点。
export const V2_CUSTOMERS = 'v2-customers'
// 「客户分析报表」v2（2026-11-10 客户线第三批）：炭火真页，后端同线新增 analysis 聚合端点。
export const V2_CUSTOMER_ANALYSIS = 'v2-customer-analysis'
// 「黑名单管理」（客户黑名单）v2 占位（2026-11-11 客户线第七批）：点击不再跳 v1 旧壳。
export const V2_CUSTOMER_BLACKLIST = 'v2-customer-blacklist'

/** 旧 routeName → v2 路由 name 的特例（⛔ 新 v2 页必须在这里登记映射，
 * 同一次改 `CarbonShell.spec` / `v2Routes.spec` 的对拍网——「特例恰好 N 条」由键集合恰等断言钉住（评审 P1：只钉阳性断言时，注入新映射不红）。 */
export const V2_ROUTE_OVERRIDES: Readonly<Record<string, string>> = {
  home: V2_HOME,
  'orders-mine': V2_MY_ORDERS,
  'orders-inside-hall': V2_HALL,
  'agents-my-income': V2_INCOME,
  'agents-my-settlement': V2_SETTLEMENT,
  'accounts-profile': V2_PROFILE,
  'accounts-messages': V2_MESSAGES,
  'order-new': V2_ORDER_NEW,
  // 「管理订单」v2（2026-11-04 业务方授权本地迭代线）：该页已有 v2 版。
  orders: V2_ORDER_MANAGE,
  // 「第三方平台订单管理」「找回删除的订单」v2（2026-11-05 设计线）：两页各有 v2 版。
  'orders-platform-core': V2_PLATFORM_ORDERS,
  'orders-recycle': V2_ORDER_RECYCLE,
  // 「新开系统账号」v2（2026-11-05）：与「自助注册」合并成一个 v2 页（页内两页签）。
  'system-user-new': V2_USER_NEW,
  // 「交易订单操作记录」「订单发货记录」（对标名淘宝发货记录，2026-11-05 业务方改名）
  // 「上家订单改价记录」v2（2026-11-05 设计线）：三页各有 v2 版。
  'orders-platform-action-log': V2_ORDER_ACTION_LOG,
  'orders-taobao-ship': V2_SHIP_LOG,
  'orders-source-price-log': V2_UPSTREAM_PRICE_LOG,
  // 「系统人员」组其余 5 页 v2（2026-11-05 系统人员批次）：实名页全功能；
  // 三个空壳对标页在 v2 落成诚实空态（页面恒空表，旧版按钮点了才提示「尚未开放」）。
  'system-users': V2_USER_LIST,
  'system-real-name': V2_REAL_NAME,
  'system-login-restrict': V2_LOGIN_RESTRICT,
  'system-blacklist': V2_BLACKLIST,
  'system-contract': V2_CONTRACT,
  // 「闲管家收款明细」v2。
  'xgj-receipts': V2_XGJ_RECEIPTS,
  // 「游戏维护」「发平台设置」两组 6 页 v2（2026-11-05 设计线，feat/v2-game-platform）。
  'game-config-games': V2_GAME_MANAGE,
  'game-config-projects': V2_GAME_PROJECTS,
  'system-platform-integration': V2_PLATFORM_INTEGRATION,
  'system-platform-auto-reply': V2_PLATFORM_AUTO_REPLY,
  'dailiantong-stats': V2_DAILIANTONG_STATS,
  'dailiantong-order-templates': V2_DAILIANTONG_TEMPLATES,
  // 财务族第二批（2026-11-06）：剩余五页 v2 化（收尾批）——每页各有 v2 版。
  'agents-performance': V2_PERFORMANCE,
  'agents-settlement': V2_BILL_SUMMARY,
  'agents-day-report': V2_DAY_REPORT,
  'agents-online-pay-detail': V2_ONLINE_PAY_DETAIL,
  'agents-fund-detail': V2_FUND_DETAIL,
  // 「系统维护」组 11 页 v2（2026-11-06 系统维护批次）：全组接线——「在线结算支付宝授权」
  // 挂在【系统维护】组（routeName `agents-alipay-auth`，同【财务相关】无此条目）。
  'system-role-authority': V2_ROLE_AUTHORITY,
  'system-role-group': V2_ROLE_GROUP,
  'system-order-recognition': V2_ORDER_RECOGNITION,
  'system-order-sources': V2_ORDER_SOURCES,
  'system-source-system': V2_SOURCE_SYSTEM,
  'agents-alipay-auth': V2_ALIPAY_AUTH,
  'system-pdd-ship': V2_PDD_SHIP,
  'system-notices': V2_NOTICES,
  'system-operation-log': V2_OPERATION_LOG,
  'system-wechat-group-push': V2_WECHAT_GROUP_PUSH,
  'system-settings': V2_SETTINGS,
  // 「数据概览」「问题反馈（bottom 区）」「发布文章作品」v2（2026-11-06 APP与小程序+问题反馈批）。
  'app-stats': V2_APP_STATS,
  'feedback': V2_FEEDBACK,
  'app-mp-articles': V2_ARTICLES,
  // 「开放平台」组 3 页 v2（2026-11-06 开放平台批）：性能分析 / 应用管理 / 打手对接。
  // 前两页的路由还带 requiresAccountAdmin（v2 子树首例，router/index.ts 同一次落）。
  'system-perfanalysis': V2_PERFANALYSIS,
  'open-apps': V2_OPEN_APPS,
  'taker-apps': V2_TAKER_APPS,
  // 「第三方平台」组 4 页 v2（2026-11-06 第三方平台批）：前三页复用 `system.pinduoduo_settings`、
  // QQ机器人用自有节点（与旧路由同门，router/index.ts 同一次落）。
  'thirdparty-xgj-shop': V2_XGJ_SHOP,
  'wecom-group-qr': V2_WECOM_GROUP_QR,
  'system-wechatmp-settings': V2_WECHATMP_SETTINGS,
  'qqbot-settings': V2_QQBOT_SETTINGS,
  // 「企微自建应用」（D-0379，v2-only）：旧壳无此路由（置灰）、v2 侧栏经本映射可点直达
  // （可点判据见 `useV2Menu.withTarget`，E-0325）。
  'wecom-settings': V2_WECOM_SETTINGS,
  // 「会员管理」「客户分析报表」v2（2026-11-10 客户线）：旧菜单两条（复刻壳页）指向 v2 真页。
  customers: V2_CUSTOMERS,
  'customers-analysis': V2_CUSTOMER_ANALYSIS,
  // 「黑名单管理」（客户黑名单）v2 占位（2026-11-11 第七批）：旧菜单条目（复刻壳页）
  // 指向 v2 占位页，点击不再跳 v1 旧壳。
  'customers-blacklist': V2_CUSTOMER_BLACKLIST,
}

/** 仅 **v2 侧栏**隐藏的旧菜单条目（routeName 集合）。⛔ `config/menu.ts` 一字不动：旧壳走
 * `useMenu()` 照旧显示；v2 把「自助注册」并进「新开系统账号」页第二页签（`v2-user-new`）、
 * 把「打手钱包」整条隐藏（2026-11-06 业务方拍板「不需要了」；路由与页面保留）、
 * 把「马上来查码对接」隐藏并入「自动上传群码」页的独立区块（2026-11-06 第三方平台批，
 * `v2-wecom-group-qr`；旧路由与旧壳直达照旧）。过滤打在 `useV2Menu()` 组装 groups 的
 * **条目级**；topLevel / bottom 不含这些条目。 */
export const V2_HIDDEN_ROUTE_NAMES = new Set<string>([
  'self-registration',
  'agents-taker-wallet',
  'system-order-qr-fetch',
  'agents-salesman-stats',
])

/** 仅 **v2 侧栏**把条目重排到所在组**末项**的旧条目（routeName 集合；2026-11-06 业务方澄清：
 * 「团团通资金明细与提现」的「放最底部」=【财务相关】组**最后一项**，不是侧栏 bottom 区——
 * bottom 区照 `useMenu().bottom` 原样、只有「问题反馈」）。重排打在 `useMenu()` 过滤结果
 * **之后**（fail-closed：无 `finance.platform_fund_details` 权限的人本来就不渲染该条目），
 * ⛔ 不从原始 MENU_GROUPS 摘。 */
const V2_GROUP_TAIL_ROUTE_NAMES = new Set<string>(['agents-fund-detail'])

/** 组内重排：`V2_GROUP_TAIL_ROUTE_NAMES` 里的条目挪到组尾，其余保持 `useMenu()` 顺序。
 * 渲染侧（`useV2Menu`）与对拍网期望侧（CarbonShell.spec）**共用这一个函数**，⛔ 手抄第二份。 */
export function moveGroupTailToLast<T extends { routeName?: string }>(items: readonly T[]): T[] {
  const tail = items.filter((item) => V2_GROUP_TAIL_ROUTE_NAMES.has(item.routeName ?? ''))
  const rest = items.filter((item) => !V2_GROUP_TAIL_ROUTE_NAMES.has(item.routeName ?? ''))
  return [...rest, ...tail]
}

/** 仅 **v2 侧栏**生效的组名 / 条目名渲染层改名（2026-11-06 第三方平台批，业务方两项改名）。
 * ⛔ `config/menu.ts` 一字不动：旧壳照旧显示「第三方平台」「自动上传企微群码到订单截图」，
 * v2 显示「功能维护」「自动上传群码」。键 = `config/menu.ts` 的组名 / 条目 `routeName`；
 * 期望侧（`CarbonShell.spec` 的「照抄」网）import 这两张表套用，⛔ 手抄第二份。 */
export const V2_GROUP_LABEL_OVERRIDES: Readonly<Record<string, string>> = {
  第三方平台: '功能维护',
}
export const V2_ITEM_LABEL_OVERRIDES: Readonly<Record<string, string>> = {
  'wecom-group-qr': '自动上传群码',
}

/** v2 的导航目标（vue-router 的 location name）：`V2_ROUTE_OVERRIDES` 里的条目走 v2 路由，
 * 其余原样透传。`routeName` 缺席 = 配置错误，**响亮抛**而不是静默渲染死链接
 * （前身 `targetNameOf` 的同一纪律）。 */
export function targetNameOf(routeName: string | undefined): string {
  if (routeName === undefined) {
    throw new Error('v2 菜单条目缺 routeName——请核 config/menu.ts，不许渲染无目标条目')
  }
  return V2_ROUTE_OVERRIDES[routeName] ?? routeName
}

/** 一条可渲染条目 = `useMenu()` 判定后的条目（label/routeName）+ 解析好的导航目标
 * + **按目标表重判**的可点性。 */
export type V2MenuEntry = MenuItem & { disabled: boolean; target: string }

/** 一个可渲染分组（`useMenu()` 过滤后的组；可见性判定已在其内部收口）。 */
export type V2MenuGroup = { name: string; items: V2MenuEntry[] }

/** 炭火侧栏的数据出口。返回值与 `useMenu()` 同形（topLevel / groups / bottom），
 * 只给每条补一个解析好的 `target`。分组结构、顺序、显隐从 `useMenu()` 原样继承
 * （⛔ 不判第二遍）；**`disabled` 是这里的例外、必须重判**：`useMenu.isClickable`
 * 查的是**旧路由表**，而 v2 的导航真值是**解析后的目标**（`V2_ROUTE_OVERRIDES` 指到
 * v2 子树）——v2-only 条目（首例 `wecom-settings`，旧表无此名）在旧表判必灰、在 v2
 * 却真实可达（现场 = E-0325）。这不是同一事实判两遍，是**两张真值表各自判各的**：
 * 旧壳照旧判旧表（置灰，D-0344 不动），v2 按目标表判（`router.hasRoute(target)`）。
 * 渲染层例外共五处：条目级隐藏（`V2_HIDDEN_ROUTE_NAMES`）、组内重排（`moveGroupTailToLast`）、
 * 组名与条目名的渲染层改名（`V2_GROUP_LABEL_OVERRIDES` / `V2_ITEM_LABEL_OVERRIDES`）、
 * 可点性按目标表重判（本段）。 */
export function useV2Menu() {
  const { groups, topLevel, bottom } = useMenu({
    我的相关: [{ label: '审批中心', routeName: V2_APPROVALS }],
    第三方平台: [
      { label: '消息推送订阅', routeName: 'v2-wxpusher-settings', accountAdminOnly: true },
      { label: '内部沟通配置', routeName: 'v2-internal-chat-settings', accountAdminOnly: true },
    ],
  })
  const router = useRouter()

  const withTarget = (item: MenuItem & { disabled: boolean }): V2MenuEntry => {
    const target = targetNameOf(item.routeName)
    return {
      ...item,
      label: V2_ITEM_LABEL_OVERRIDES[item.routeName ?? ''] ?? item.label,
      target,
      // v2 侧栏的可点判据跟**目标**走（E-0325）：目标路由存在就可点，与旧表无关。
      disabled: !router.hasRoute(target),
    }
  }

  return {
    topLevel: computed<V2MenuEntry[]>(() => topLevel.value.map(withTarget)),
    groups: computed<V2MenuGroup[]>(() =>
      groups.value.map((group) => ({
        name: V2_GROUP_LABEL_OVERRIDES[group.name] ?? group.name,
        items: moveGroupTailToLast(
          group.items
            // 条目级隐藏（V2_HIDDEN_ROUTE_NAMES）只影响 v2 侧栏的渲染面。
            .filter((item) => !V2_HIDDEN_ROUTE_NAMES.has(item.routeName ?? ''))
            .map(withTarget),
        ),
      })),
    ),
    bottom: computed<V2MenuEntry[]>(() => bottom.value.map(withTarget)),
  }
}
