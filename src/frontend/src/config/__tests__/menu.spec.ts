// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { isClickable } from '@/composables/useMenu'
import router from '@/router'
import { BOTTOM_ITEMS, MENU_GROUPS, TOP_LEVEL_ITEMS } from '../menu'

/**
 * **本文件里「菜单条目」的统一口径**。
 *
 * ⚠️ 与 `router/__tests__/route-declarations.spec.ts` 的 `MENU_ITEMS` **同一个口径**：
 * 冻结 IA 的条目分三处声明（`TOP_LEVEL_ITEMS` / `MENU_GROUPS[].items` / `BOTTOM_ITEMS`），
 * 凡是问「菜单里有哪些入口」的判据都要**一起看**这三处。本文件里**不适用的**是那些
 * **按分组**数的判据（`MENU_GROUPS` 的单元数 / 组内条目数）——底部条目不属于任何分组。
 */
const MENU_ITEMS = [...TOP_LEVEL_ITEMS, ...MENU_GROUPS.flatMap((g) => g.items), ...BOTTOM_ITEMS]

/** 转录计数断言：把「人工逐字转录」这个最大人为错误源变成会红的测试。
 *  数字出处：docs/FRONTEND-LAYOUT.md §4 各单元标题 + §4.1 处置表；分组计数是**写死字面量**。
 *  按业务裁定删掉的入口与整组（商城、短信）不在本表里——不是漏写。 */
const EXPECTED_ITEM_COUNTS: Record<string, number> = {
  我的相关: 4,
  // 「第三方平台订单管理」是复刻页，本表只管组内条数。
  订单管理: 7,
  游戏排团: 5,
  游戏号库: 5,
  系统人员: 7,
  // 「打手钱包」是自有页（原「打手提现审核」并入），不进 replicaRoutes；节点 `finance.taker_withdraw_review`。
  财务相关: 8,
  // 2026-11-10 第二批：「会员余额明细」「会员等级设置」两条删除（业务方拍板）⇒ 组内 3 条。
  客户维护: 3,
  游戏维护: 2,
  发平台设置: 7,
  系统维护: 11,
  // ⭐ 企微自建应用（D-0379）：5 → **6**（v2-only 页；旧壳无路由 ⇒ 旧侧栏置灰，
  // 由「可点」那条判据的 `V2_ONLY_GREY` 承载）。
  第三方平台: 6,
  开放平台: 3,
  // 【APP与小程序】是自有功能组（对标物里没有）：「数据概览」（复用 `finance.support_statistics`，
  // 见 `docs/plans/2026-10-23-app-mp-stats.md` §8）与「发布文章作品」（自有节点 `system.article_manage`，
  // 见 `docs/plans/2026-10-23-channel-articles.md` §1）。
  APP与小程序: 2,
}

/**
 * 菜单里**有 `routeName`** 的条目（按 `menu.ts` 的冻结顺序逐条列出 label → routeName）。
 * 来源：`config/replicaRoutes.ts` 的 `benchmarkMenuLabel` 匹配 + `router/index.ts` 的
 * `...replicaRoutes` 生成规则（见 `menu.ts` 文件头「接线机制」段）+ 自有功能页的手写条目。
 * ⭐ 置灰形态自 D-0379 起有两种：**无 routeName**（历史，当前 0 条）与
 * **v2-only**（`wecom-settings`，routeName 只在 v2 子树解析，见「可点」那条的 `V2_ONLY_GREY`）。
 */
const EXPECTED_ENTRY_POINTS: Array<[label: string, routeName: string]> = [
  ['后台首页', 'home'],
  ['指派给我的订单', 'orders-mine'],
  ['我参与的排团', 'groups-mine'],
  ['内部抢单大厅', 'orders-inside-hall'],
  ['我的订单收入', 'agents-my-income'],
  ['在线结算记录', 'agents-my-settlement'],
  ['我的资料', 'accounts-profile'],
  ['消息通知', 'accounts-messages'],
  ['录入订单', 'order-new'],
  ['管理订单', 'orders'],
  // 对标「交易平台订单处理」的复刻页，位置照对标菜单序（管理订单之后）；
  // 路由由 `replicaRoutes.ts` 自动生成。
  ['第三方平台订单管理', 'orders-platform-core'],
  ['找回删除的订单', 'orders-recycle'],
  ['交易订单操作记录', 'orders-platform-action-log'],
  ['订单发货记录', 'orders-taobao-ship'],
  ['上家订单改价记录', 'orders-source-price-log'],
  ['新建排团', 'groups-new'],
  ['旧版排团', 'groups-legacy'],
  ['2021版排团', 'groups-v2021'],
  ['2025版排团', 'groups-v2025'],
  ['看公开的排团坑位', 'groups-public-tasks'],
  ['新增游戏号', 'accounts-game-new'],
  ['导入内部游戏号', 'accounts-game-import'],
  ['管理内部账号', 'accounts-game'],
  ['售卖账号预览板', 'accounts-game-sell-preview'],
  ['指定给我的账号', 'accounts-game-mine'],
  ['新开系统账号', 'system-user-new'],
  // 自有功能页（对标物里没有这一页）：与「新开系统账号」同组同节点（`system_user.add_edit`）；
  // 路由 name 与 island key 同名，都是 `self-registration`，与另几条自有功能页同一约定。
  ['自助注册', 'self-registration'],
  ['人员管理', 'system-users'],
  ['登录限制设置', 'system-login-restrict'],
  ['人员黑名单', 'system-blacklist'],
  ['人员实名记录与配置', 'system-real-name'],
  ['员工电子合同管理', 'system-contract'],
  ['打手与客服报酬', 'agents-performance'],
  ['结算记录', 'agents-settlement'],
  ['在线结算明细', 'agents-online-pay-detail'],
  ['客服接发单统计', 'agents-salesman-stats'],
  // 对标 `/Emp/dayoperation` 的自有实现页；权限节点与上面那条**同一个** `finance.support_statistics`
  // （⛔ 不新增权限点）。
  ['经营报表', 'agents-day-report'],
  // 显示名「鸡蛋铺资金明细与提现」有意改「团团通资金明细与提现」（只改显示层，routeName /
  // 权限点 / 路由都不动；对标原文照旧，偏离登记在 `replicaRoutes.ts` 该条 note 与页面模块）。
  ['团团通资金明细与提现', 'agents-fund-detail'],
  // 「打手钱包」由原「打手提现审核」+「打手钱包管理」合并而来（组内 **8 条不变**，
  // 上面的 `财务相关: 8` **不动**）；两个既有权限节点都保留，页签按权限渲染。
  ['打手钱包', 'agents-taker-wallet'],
  // 自有功能页（对标物没有这一页）：路由是 `router/index.ts` 的手写条目
  // `/finance/xgj-receipts`（`meta.islandPage` = `xgj-receipts`）；
  // 权限点是自有节点 `finance.xgj_receipt_detail`。
  ['闲管家收款明细', 'xgj-receipts'],
  // 显示名 2026-11-10 第二批改「客户管理 / 客户分析报表」（纯显示层，路由 / 权限节点零改动）；
  // 「会员余额明细」「会员等级设置」两条已删除（REPLICA_SKIP 留痕）。
  ['客户管理', 'customers'],
  ['客户分析报表', 'customers-analysis'],
  ['黑名单管理', 'customers-blacklist'],
  ['游戏管理', 'game-config-games'],
  // 原「订单项目分类」页 +「常用项目」页两条合并成这一条。
  ['订单项目分类与常用项目', 'game-config-projects'],
  ['交易平台对接设置', 'system-platform-integration'],
  ['平台快速发单设置', 'system-platform-push-order'],
  ['交易平台自动回复', 'system-platform-auto-reply'],
  ['交易单自动调价配置', 'system-auto-price-adjust'],
  ['自动发消息到微信群设置', 'system-wechat-group-push'],
  // 自有功能页（对标物里没有这两页）；权限点**复用** `order.list`（后端两个读面的端点门就是它）。
  ['代练通按日统计', 'dailiantong-stats'],
  ['代练通订单要求模板', 'dailiantong-order-templates'],
  ['角色职位权限', 'system-role-authority'],
  ['角色职位分组', 'system-role-group'],
  ['录单识别设置', 'system-order-recognition'],
  ['订单来源管理', 'system-order-sources'],
  ['绑定上家系统设置', 'system-source-system'],
  ['在线结算支付宝授权', 'agents-alipay-auth'],
  ['其它平台店铺对接', 'system-pdd-ship'],
  ['系统内部公告管理', 'system-notices'],
  ['系统操作日志管理', 'system-operation-log'],
  // 与【发平台设置】组那条同名条目指向同一路由。
  ['自动发消息到微信群设置', 'system-wechat-group-push'],
  ['系统各项设置管理', 'system-settings'],
  // 自有功能页（对标物里没有这一页）
  ['闲管家店铺对接', 'thirdparty-xgj-shop'],
  // 鸡蛋铺系统同步入口已下架，页文件和前端 api 模块也已删除，别去找它们。
  // 自有功能页（对标物里没有这一页）：自动上传企微群码到订单截图；
  // 路由 name 与 island key 同名，都是 `wecom-group-qr`，与另几条自有功能页同一约定。
  ['自动上传企微群码到订单截图', 'wecom-group-qr'],
  // 自有功能页（对标物里没有这一页）：微信公众号设置。
  ['微信公众号设置', 'system-wechatmp-settings'],
  // 自有功能页（对标物里没有这一页）：QQ机器人设置（路由 name 与 island key 同名，
  // 都是 `qqbot-settings`；权限点是自有节点 `system.qqbot_settings`）。
  ['QQ机器人设置', 'qqbot-settings'],
  // 自有功能页（对标物里没有这一页）：马上来查码对接；权限点**复用** `system.pinduoduo_settings`
  // （同微信公众号设置口径，⛔ 不新增 RBAC 节点）。
  ['马上来查码对接', 'system-order-qr-fetch'],
  // ⭐ 企微自建应用（D-0379）：**v2-only**——routeName `wecom-settings` 在旧路由表里
  // **不存在**（v2 侧才有 `v2-wecom-settings`，经 `v2Menu.V2_ROUTE_OVERRIDES` 指过去）。
  // 它进本清单（配置数据钉死），但「可点」判据把它作为**已知的旧壳置灰项**单独登记
  // （见「恰好这 75 条可点」那条的专项处理）。
  ['企微自建应用', 'wecom-settings'],
  // 自有功能页（对标物里没有这一页）：性能分析。
  ['性能分析', 'system-perfanalysis'],
  // 自有功能页（对标物里没有这一页）：应用管理（路由 name 与 island key 同名，都是 `open-apps`）。
  ['应用管理', 'open-apps'],
  // 自有功能页（对标物里没有这一页）：打手对接。权限点是自有节点 `system.allow_taker_open_api`
  // （⛔ 不是复用别人的点）。
  ['打手对接', 'taker-apps'],
  // 自建 island 页 `layui-island/pages/app-stats.ts`；权限点**复用** `finance.support_statistics`
  // （⛔ 不新增节点）。
  ['数据概览', 'app-stats'],
  // 自有功能页（对标物里没有这一页）：发布文章作品（路由 name 与 island key 同名，都是
  // `app-mp-articles`）；权限点是自有节点 `system.article_manage`（⛔ 不是复用别人的点）。
  ['发布文章作品', 'app-mp-articles'],
  // 自有功能页（对标物里没有这一页）：问题反馈（**侧栏最底部**那一条，声明在 `BOTTOM_ITEMS`、
  // **不属于任何分组**）。权限点是**精确命中**的 `home.feedback`（对标 112 号点），不是复用别的点。
  ['问题反馈', 'feedback'],
]

/**
 * ⭐ 2026-11-11 企微自建应用线（D-0379）：置灰判据从 `STILL_GREY` 换成「可点」那条测试里的
 * **`V2_ONLY_GREY`**（v2-only 条目带 `routeName`、旧壳刻意无路由——与旧「无 routeName 置灰」
 * 是两种形态，混在一个数组里会把语义搅浑）。旧的空数组删掉了。
 */

describe('菜单骨架（冻结 IA）', () => {
  it('单元数：顶部直挂 1 + 分组 13 = 14', () => {
    // 当前口径：顶部直挂 1 + 13 个分组 = 14 个单元；分组名与顺序由下面的逐字断言钉死
    // （自有功能组按「追加在末尾」排在最后）。
    expect(TOP_LEVEL_ITEMS).toHaveLength(4)
    expect(MENU_GROUPS).toHaveLength(13)
  })

  it('整组隐藏 2 组，且就是移除商城组与短信组、放出【发平台设置】与【客户维护】后剩下的那 2 组', () => {
    // 【发平台设置】已改成组可见、条目级隐藏；【客户维护】2026-11-10 业务方拍板放出（客户线）。
    const hidden = MENU_GROUPS.filter((g) => g.hidden).map((g) => g.name)
    expect(hidden.sort()).toEqual(
      ['游戏排团', '游戏号库'].sort(),
    )
  })

  it('显示单元 = 12（顶部直挂 + 11 个可见分组）', () => {
    const visibleGroups = MENU_GROUPS.filter((g) => !g.hidden)
    expect(visibleGroups).toHaveLength(11)
    expect(1 + visibleGroups.length).toBe(12)
  })

  it('【发平台设置】组可见，组内可见「交易平台对接设置 + 交易平台自动回复」两条', () => {
    // ⛔ 断言钉的是**恰好集合**（条目清单 + 可见/隐藏两份标签各一份），不是 `toContain`：
    //    将来谁把组内别的条目也放出来、或把已交付的又藏回去，这里当场红。
    const group = MENU_GROUPS.find((g) => g.name === '发平台设置')
    expect(group, '菜单里没有「发平台设置」分组').toBeDefined()
    expect(group!.hidden ?? false).toBe(false)
    expect(group!.items.map((i) => [i.label, i.routeName])).toEqual([
      ['交易平台对接设置', 'system-platform-integration'],
      ['平台快速发单设置', 'system-platform-push-order'],
      ['交易平台自动回复', 'system-platform-auto-reply'],
      ['交易单自动调价配置', 'system-auto-price-adjust'],
      ['自动发消息到微信群设置', 'system-wechat-group-push'],
      ['代练通按日统计', 'dailiantong-stats'],
      ['代练通订单要求模板', 'dailiantong-order-templates'],
    ])
    // 其余 3 条仍是条目级 `hidden`：**或已有其它入口**（「自动发消息到微信群设置」在【系统维护】可见）、
    // **或页面尚未接后端** ⇒ 不重复放第二处入口。⛔ 这不是「3 条都没做完」。
    expect(group!.items.filter((i) => !i.hidden).map((i) => i.label)).toEqual([
      '交易平台对接设置',
      '交易平台自动回复',
      '代练通按日统计',
      '代练通订单要求模板',
    ])
    expect(group!.items.filter((i) => i.hidden).map((i) => i.label)).toEqual([
      '平台快速发单设置',
      '交易单自动调价配置',
      '自动发消息到微信群设置',
    ])
  })

  it('每个分组的条目数与 §4 标题一致', () => {
    // 断言写法说明：`toHaveLength(EXPECTED_ITEM_COUNTS[g.name])` 在
    // noUncheckedIndexedAccess（tsconfig.app.json）下是 number | undefined，过不了 vue-tsc。
    // 改成对 .length 用 toBe（参数类型是 unknown），语义不变：
    // 计数表里漏掉某个分组时得到 undefined，`undefined !== 条目数` 照样会红。
    for (const g of MENU_GROUPS) {
      expect(g.items.length, `分组「${g.name}」条目数不符`).toBe(EXPECTED_ITEM_COUNTS[g.name])
    }
  })

  it('条目名不重复（同一分组内）', () => {
    for (const g of MENU_GROUPS) {
      const labels = g.items.map((i) => i.label)
      expect(new Set(labels).size, `分组「${g.name}」有重复条目`).toBe(labels.length)
    }
  })

  it('分组名与顺序逐字对齐 §4（商城组与短信组按业务裁定移除）', () => {
    // 顺序也是冻结项。出处：docs/FRONTEND-LAYOUT.md §4 的各单元标题顺序；
    // §3 把「分组名与分组数量」「条目顺序」都列为冻结。
    // 相对布局文档少了【鸡蛋铺商铺】和【短信业务】两组（业务裁定整组删除）。
    expect(MENU_GROUPS.map((g) => g.name)).toEqual([
      '我的相关',
      '订单管理',
      '游戏排团',
      '游戏号库',
      '系统人员',
      '财务相关',
      '客户维护',
      '游戏维护',
      '发平台设置',
      '系统维护',
      '第三方平台',
      // 自有功能组（对标物里没有这一组），按「自有功能追加在末尾」的既有做法排在最后。
      '开放平台',
      // 自有功能组（对标物里没有这一组），同样追加在末尾；组内 2 条（数据概览 / 发布文章作品）。
      'APP与小程序',
    ])
  })

  it('带 routeName 的条目恰好这 76 条（label → routeName 逐条钉死；含 `home`；置灰项见「可点」那条）', () => {
    // 为什么值得钉死：可点判定是「有 routeName **且** 路由表里真有该 name」，
    // 而 `not-found` **真的在路由表里**（src/router/index.ts）。给任何未实现条目随手加一个
    // `routeName: 'not-found'`（或将来任何真实存在的 name）会**静默变成可点**，
    // 单测不会红、页面多出一个死链入口。这里把「菜单数据里哪些条目是入口」钉死，
    // 连 label → routeName 的对应关系与冻结顺序一起钉（不是只钉「有没有」）。
    const withRouteName = MENU_ITEMS.filter(
      (i) => i.routeName !== undefined,
    )
    expect(withRouteName.map((i) => [i.label, i.routeName])).toEqual(EXPECTED_ENTRY_POINTS)
  })

  it('恰好这 76 条带 routeName 的条目里，除 v2-only 1 条外都可点（用真路由表判定）；置灰项 = 企微自建应用 1 条（v2-only，旧壳刻意无路由）', () => {
    // 用**真路由表**判定（不是造一个 hasRoute）：菜单里写的 name 必须在 src/router/index.ts 里
    // 真的存在，否则就是「菜单说能点、点了 404」的死链。硬要求出处：docs/FRONTEND-LAYOUT.md §8
    //「置灰项必须不可点击」。判据面 = **三处声明全收**（`MENU_ITEMS`，含 `BOTTOM_ITEMS`）——
    // 底部那条一样要过「路由表里真有这个 name」这一关（`isClickable` 同时查两者）。
    // ⭐ 企微自建应用（D-0379）是**v2-only** 的既有例外：routeName `wecom-settings` 只在
    // `/v2` 子树有真路由（`v2-wecom-settings`，经 `v2Menu.V2_ROUTE_OVERRIDES` 指过去），
    // 旧路由表里**刻意没有它**（D-0344：旧前端一条不改）⇒ 本网（**旧壳口径**）把它按
    // **已知置灰项**逐字登记（`V2_ONLY_GREY`），⛔ 不是漏接线。
    // ⭐ E-0325 之后的口径：**置灰 = 本网自己的真值表（旧路由表）里目标真不存在**——
    // 这条对旧壳成立；**v2 侧栏按另一张真值表（解析后的目标）判**，`wecom-settings` 在
    // v2 可点、直达 `v2-wecom-settings`——由 `v2/__tests__/CarbonShell.spec.ts` 的
    // 「企微自建应用在 v2 侧栏可点」回归判据钉住（不靠本条）。
    const V2_ONLY_GREY: Array<[group: string, label: string]> = [
      ['第三方平台', '企微自建应用'],
    ]
    const hasRoute = (name: string) => router.hasRoute(name)
    const allItems = MENU_ITEMS

    const clickable = allItems
      .filter((i) => isClickable(i, hasRoute))
      .filter((i) => !V2_ONLY_GREY.some(([, label]) => label === i.label))
      .map((i) => i.label)
    expect(clickable).toEqual(EXPECTED_ENTRY_POINTS.filter(([label]) =>
      !V2_ONLY_GREY.some(([, greyLabel]) => greyLabel === label),
    ).map(([label]) => label))

    // 仍不可点的条目逐条枚举（按分组消歧同名条目）；同组内相邻的两条、【游戏维护】里
    // 已全部接通等情形都由这份集合等式承载，不再靠抽样。
    const menuGrey = MENU_GROUPS.flatMap((g) =>
      g.items.filter((i) => !isClickable(i, hasRoute)).map((i) => [g.name, i.label] as [string, string]),
    )
    const topGrey = TOP_LEVEL_ITEMS.filter((i) => !isClickable(i, hasRoute)).map(
      (i) => ['(顶层)', i.label] as [string, string],
    )
    expect([...topGrey, ...menuGrey]).toEqual(V2_ONLY_GREY)
  })

  it('【系统人员】**没有**组级 accountAdminOnly（可见性下移到条目级节点码）', () => {
    // 组级门不能留：「人事」持 `system_user.*` 但**非**管理员，后端准入是 `IsAccountManager`
    // （系统管理员**或**持任一账号管理节点）；留着 `accountAdminOnly: true` 人事整组都看不见，
    // 「人事能开子账户」等于没交付。可见性**下移到条目级**（每条自带 `permissions`，
    // 由 `useMenu.ts::keepItem` 逐条判；空组不渲染 ⇒ 零节点会话仍看不到这一组，fail-closed）。
    // ⛔ **断言没有放宽**：本条断言「**没有**组级门 **且** 条目各自声明了非空节点码（恰好这 7 条）」；
    // 三种 viewer 下的真实可见性由 `composables/__tests__/useMenu.systemUsers.spec.ts` 逐条驱动
    // （⛔ 不能靠"没人测"；「自助注册」与「新开系统账号」同一个节点，阳性对照的点集合不因它变大）。
    const group = MENU_GROUPS.find((g) => g.name === '系统人员')
    expect(group, '菜单里没有「系统人员」分组').toBeDefined()
    expect(group!.accountAdminOnly ?? false).toBe(false)
    expect(group!.items.map((i) => [i.label, i.routeName])).toEqual([
      ['新开系统账号', 'system-user-new'],
      ['自助注册', 'self-registration'],
      ['人员管理', 'system-users'],
      ['登录限制设置', 'system-login-restrict'],
      ['人员黑名单', 'system-blacklist'],
      ['人员实名记录与配置', 'system-real-name'],
      ['员工电子合同管理', 'system-contract'],
    ])
    // 「下移到条目级」这句话的**承重前提**：每一条都真的声明了节点码（否则那一条对
    // 非管理员就是「登录即可见」——门就开没了）。列成字面值，⛔ 不从 routePermissions 派生。
    expect(group!.items.map((i) => i.permissions)).toEqual([
      ['system_user.add_edit'],
      ['system_user.add_edit'],
      ['system_user.list'],
      ['system_user.login_restriction'],
      ['system_user.blacklist'],
      ['system_user.realname_records'],
      ['system_user.contract_management'],
    ])
  })

  it('【开放平台】**没有**组级 `accountAdminOnly`，组内条目就是「性能分析 + 应用管理 + 打手对接」三条', () => {
    // 组级门不能留：本组要放**打手**进来（新条目 `taker-apps`），而打手**不是**账号管理员，
    // 留着组级门打手连这一组都看不见。可见性**下移到条目级**（三条各自声明 `permissions`，
    // 由 `useMenu.ts::keepItem` 逐条判）。⚠️ `canSeeItem` 对 **`isTenantAdmin` 短路放行** ⇒
    // **租户管理员（非账号管理员的那种）**会看到「性能分析」与「应用管理」——这是有意变更不是副作用，
    // 三条可见性由 `composables/__tests__/useMenu.openPlatform.spec.ts` **逐条**钉住（⛔ 不能靠"没人测"）。
    // ⚠️ 路由级那一道门**没动**：「性能分析」与「应用管理」仍带 `requiresAccountAdmin: true`
    // ⇒ 非账号管理员直接敲 URL 照样进不去（「打手对接」刻意**不挂**它）。
    // ⚠️ **本组不许静默长大**：将来往【开放平台】里加页面时，要么确认新页面同样只对
    // 相关节点开放（那就同一次改这里），要么换一个组。
    const group = MENU_GROUPS.find((g) => g.name === '开放平台')
    expect(group, '菜单里没有「开放平台」分组').toBeDefined()
    expect(group!.accountAdminOnly ?? false).toBe(false)
    expect(group!.items.map((i) => [i.label, i.routeName])).toEqual([
      ['性能分析', 'system-perfanalysis'],
      ['应用管理', 'open-apps'],
      ['打手对接', 'taker-apps'],
    ])
    // 阳性对照：本组**不是**靠"整组都声明了节点码"混过上面那条的——三条的点各不相同。
    expect(new Set(group!.items.flatMap((i) => i.permissions ?? [])).size).toBe(3)
  })
})

describe('条目级判据（节点 code）', () => {
  it('有 routeName 的条目（除 `home`）都声明了 permissions，且形态是节点 code', () => {
    const NODE_CODE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$/
    const items = MENU_ITEMS.filter(
      (i) => i.routeName !== undefined && i.routeName !== 'home',
    )
    // 本条数「有 routeName 且不是 home」。入口清单数的是全部入口（含 home）。两个数不要混着改。
    // 2026-11-10 第二批客户维护删两条目 ⇒ 75 → 73；企微自建应用（D-0379）+1 ⇒ 74。
    expect(items.length).toBe(74)
    for (const i of items) {
      expect(i.permissions, `${i.label} 没有声明 permissions`).toBeDefined()
      expect((i.permissions ?? []).length).toBeGreaterThan(0)
      for (const c of i.permissions ?? []) expect(c).toMatch(NODE_CODE)
    }
  })
})
