/** 冻结 IA 的声明式骨架。出处：docs/FRONTEND-LAYOUT.md §4 / §4.1。
 *
 * 硬要求（不要为了省事改写）：
 * - 条目名与组内顺序**逐字对齐** §4（§3 冻结清单要求）。
 * - 口径：单位是**单元**，不是图标位。14 个单元 = 顶部直挂 **1 个单元**（该单元实际有 **4 条**，
 * 其中 `hidden: true` 的 1 条不渲染 → 可见 **3 条**）+ 分组 **13 个**；整组隐藏 2 → 显示 **12 个单元**
 * （【发平台设置】组可见、条目级 `hidden`；【客户维护】2026-11-10 业务方拍板放出）。
 * **图标位按条目算**：可见顶部 3 条 + 可见分组标题 11 个 =
 * **14 个图标位**（见 `menuIcons.ts` 与 docs/FRONTEND-THEME.md §5.2）。「7 个单元」曾被当成
 * 「7 个图标位」用过，别再这么读。
 * - 可点判定不看本文件的 `soon` 字段，而看路由表是否真有该 name（见 useMenu.ts）。
 * `soon` 是**保留字段，当前没有任何代码读它**：置灰与「即将开放」都由 `disabled` 驱动，
 * 而 `disabled = !isClickable(...)`（即「路由表里没有该 name」）。保留它是给将来
 * 「路由已存在、页面还没就绪」留表达力；**不得**拿它当可点判定，否则会造出
 * 「菜单说能点、点了 404」的死链。置灰条目也一律带 `soon: true`，那是全量转录时的统一
 * 填充，**无行为含义**——不要从它推断排期。今天**没有置灰条目**
 * （`menu.spec.ts` 的 `STILL_GREY` 是空数组）。
 *
 * ⚠️ 三条现状，别写反（核对代码与 spec §5.5 / §8）：
 * 1. **前端判据已落地**：可见性判据是 `composables/useMenu.ts` 的 `canSeeItem`
 * （管理员全可见；未声明 / `[]` 视为"登录即可见"；否则要求与用户 `/api/auth/me` 的 `menu_permissions` —— 即 RBAC **节点 code** —— 有交集）。
 * 所以本文件的条目可以按需声明 `permissions` 来收窄可见性。
 * 2. **它是体验层，不是访问控制**：菜单不可见 ≠ 数据不可访问——隐藏只是 DOM 里少一个节点。
 * 3. **多数业务端点仍只挂 `IsAuthenticated`**；少数动作已接细粒度权限
 * （`apps/business/permissions.py::has_biz_permission`）。本文件里的 `permissions` 是
 * **前端可见性**的输入，不是"服务端会拦下这个请求"的证明。
 *
 * ## 接线机制（layui island 1:1 复刻）
 *
 * 本文件**只加 `routeName`、并去掉已接通条目的 `soon`**：已接线条目按对标菜单中文逐条匹配
 * `config/replicaRoutes.ts` 的 `benchmarkMenuLabel`，取其 `routeName`；路由表由
 * `router/index.ts` 的 `...replicaRoutes`（`!accessDenied && !REPLICA_SKIP.has(key)`）
 * 加上显式 island 路由程序化生成（详见该文件头）。所以「加一个 `routeName`」就能点亮菜单，
 * **不需要别的开关**（判定见 `useMenu.ts` 的 `isClickable`）。
 * 做法纪律：条目从本文件改即可；**不要手改生成物** `config/replicaRoutes.ts`（它对标 89 条
 * 逐条转录，`replicaRoutes.spec.ts` 会红）——「对标有、我们不做」的对标 `key` 加进
 * `router/index.ts` 的 `REPLICA_SKIP`（见该文件「业务方裁定不做」段）。
 *
 * ## 已整组删除 / 不做的组（均为业务方裁定，别再按「漏接线」处理）
 *
 * - **商城（店铺管理）不做**：不是「置灰等以后」，是**不做**。原【鸡蛋铺商铺】4 条已整组删除；
 * 五个对标 `key`（`order-jdpshoporderlist` / `jdpshop-shopindex` / `jdpshop-shoptype` /
 * `jdpshop-addshopitem` / `jdpshop-shopmange`）登记进 `router/index.ts` 的 `REPLICA_SKIP`
 * （那 5 条对标页本身也 `accessDenied`，见 `replicaRoutes.ts` 的 `accessDenied` 字段）。
 * 无 island 页、无证据件（本来就没有）。
 * - **短信业务整块下线**：隐藏组【短信业务】已整组删除，island 页与证据件一并删除，4 个对标 `key`
 * （`sms-smslogslist` / `sms-smstemplatelist` / `sms-smsbulkhair` / `system-smssetting`）
 * 登记进 `router/index.ts` 的 `REPLICA_SKIP`；`system.sms_settings` 与 `order.send_sms`
 * 两个节点已从 RBAC 目录删除（数据迁移
 * `apps/rbac/migrations/0002_remove_pruned_nodes_and_rename_submit.py`）——前端只摘声明。
 * 裁剪历史明细见 `docs/DECISIONS.md`（D-0036 / D-0038 / D-0086）。
 *
 * 另有对标页**已由既有 EP 页承担**的条目（订单 / 游戏维护 / 系统人员 / 首页），它们都已写 `routeName`。
 */
export type MenuItem = {
  label: string
  routeName?: string
  soon?: boolean
  hidden?: boolean
  accountAdminOnly?: boolean
  /**
 * 可见性判据：**RBAC 节点 code** 数组（D-0020 结案）。`[]` ≡ 未声明 = 登录即可见。
 * 取值必须与 `config/routePermissions.ts` 里**同一 `routeName`** 的值一致
 * （由 `router/__tests__/route-declarations.spec.ts` 的 (c) 逐条比对）。
 * 形态：栏目级 `"order"` / 功能点 `"order.list"`。
 */
  permissions?: string[]
}
export type MenuGroup = {
  name: string
  items: MenuItem[]
  hidden?: boolean
  /**
 * 整组只对**账号管理员**可见（`is_superuser` 或持「系统管理员」Group）。
 * 判据由 `useMenu.ts` 读 `/api/auth/me` 的 `is_account_admin` 执行；与条目级
 * `permissions`（节点 code 交集）是两条独立判据。前端隐藏只是体验层。
 */
  accountAdminOnly?: boolean
}

export const TOP_LEVEL_ITEMS: MenuItem[] = [
  { label: '后台首页', routeName: 'home' },
  // 两条对标顶层项 + 一条隐藏的排团项；`我参与的排团` 保持 `hidden: true`——
  // 接线只加 routeName，不改冻结 IA 的可见性。
  { label: '指派给我的订单', routeName: 'orders-mine', permissions: ['my_orders'] },
  { label: '我参与的排团', routeName: 'groups-mine', permissions: ['my_group'], hidden: true },
  { label: '内部抢单大厅', routeName: 'orders-inside-hall', permissions: ['grab_hall'] },
]

export const MENU_GROUPS: MenuGroup[] = [
  {
    name: '我的相关',
    // 组内 4 条全部接线。原「异常消失订单」是整页删除（不是隐藏）：对标 key
    // `order-exceptionorderlog` 已登记进 `router/index.ts` 的 `REPLICA_SKIP`。
    items: [
      { label: '我的订单收入', routeName: 'agents-my-income', permissions: ['my_performance'] },
      { label: '在线结算记录', routeName: 'agents-my-settlement', permissions: ['finance', 'my_performance'] },
      { label: '我的资料', routeName: 'accounts-profile', permissions: ['my_profile'] },
      { label: '消息通知', routeName: 'accounts-messages', permissions: ['my_profile'] },
    ],
  },
  {
    name: '订单管理',
    items: [
      // 「第三方平台订单管理」的位置照对标菜单序（录入订单 → 管理订单 → 交易平台订单处理 → …）。
      // ⚠️ 权限码**复用** `order.list`：对标 1236 号点 `order.platform_order_center` 已从 RBAC
      // 目录裁剪（`catalog.py::PRUNED_BENCHMARK_IDS`），该页已接真后端但目录无独立点——
      // 代价是不能单独授权本页（持 `order.list` 的人都能看）。取值与理由同
      // `config/routePermissions.ts` 的「已知近似」段，两处必须同源（`route-declarations.spec.ts` 的 (c) 钉住）。
      { label: '录入订单', routeName: 'order-new', permissions: ['order.add_edit'] },
      { label: '管理订单', routeName: 'orders', permissions: ['order.list'] },
      { label: '第三方平台订单管理', routeName: 'orders-platform-core', permissions: ['order.list'] },
      { label: '找回删除的订单', routeName: 'orders-recycle', permissions: ['order.recycle'] },
      { label: '交易订单操作记录', routeName: 'orders-platform-action-log', permissions: ['order.platform_action_logs'] },
      { label: '订单发货记录', routeName: 'orders-taobao-ship', permissions: ['order.taobao_ship_records'] },
      { label: '上家订单改价记录', routeName: 'orders-source-price-log', permissions: ['order.upstream_price_change_logs'] },
    ],
  },
  {
    name: '游戏排团',
    hidden: true,
    // 整组隐藏（冻结 IA）；5 条都有对标页，接线只加 routeName、不动 `hidden`。
    items: [
      { label: '新建排团', routeName: 'groups-new', permissions: ['my_group'] },
      { label: '旧版排团', routeName: 'groups-legacy', permissions: ['my_group'] },
      { label: '2021版排团', routeName: 'groups-v2021', permissions: ['my_group'] },
      { label: '2025版排团', routeName: 'groups-v2025', permissions: ['my_group'] },
      { label: '看公开的排团坑位', routeName: 'groups-public-tasks', permissions: ['my_group'] },
    ],
  },
  {
    name: '游戏号库',
    hidden: true,
    // 整组隐藏（冻结 IA），5 条全部接线。
    items: [
      { label: '新增游戏号', routeName: 'accounts-game-new', permissions: ['my_account'] },
      { label: '导入内部游戏号', routeName: 'accounts-game-import', permissions: ['my_account'] },
      { label: '管理内部账号', routeName: 'accounts-game', permissions: ['my_account'] },
      { label: '售卖账号预览板', routeName: 'accounts-game-sell-preview', permissions: ['my_account'] },
      { label: '指定给我的账号', routeName: 'accounts-game-mine', permissions: ['my_account'] },
    ],
  },
  {
    name: '系统人员',
    // 本组没有组级门，可见性下移到条目级 `permissions`：「人事」持 `system_user.*` 但**非**管理员，
    // 留组级门会让人事整组都看不见（「人事能开子账户」的业务目标等于没交付）；
    // `canSeeItem` 对管理员短路、一条都看不见的组由「空组不渲染」丢掉 ⇒ 零节点会话看不到这一组（fail-closed）。
    // ⚠️ `accountAdminOnly` **机制本身没删**（`useMenu.canSeeGroup` 仍在判）——只是今天没有哪个组用它；⛔ 别当它已废除。
    items: [
      // 7 条全部接线：3 条功能页 + 4 条结构复刻页（后端未实现，页面按对标结构空数据渲染）。
      { label: '新开系统账号', routeName: 'system-user-new', permissions: ['system_user.add_edit'] },
      // 自有功能页（对标物里没有这一页）：一次性注册链接的配置 / 生成 / 记录页。
      // 节点与「新开系统账号」同一个 `system_user.add_edit`（⛔ 不新增节点）；
      // 逐条理由与代价见 `config/routePermissions.ts` 里那条登记。
      { label: '自助注册', routeName: 'self-registration', permissions: ['system_user.add_edit'] },
      { label: '人员管理', routeName: 'system-users', permissions: ['system_user.list'] },
      { label: '登录限制设置', routeName: 'system-login-restrict', permissions: ['system_user.login_restriction'] },
      { label: '人员黑名单', routeName: 'system-blacklist', permissions: ['system_user.blacklist'] },
      { label: '人员实名记录与配置', routeName: 'system-real-name', permissions: ['system_user.realname_records'] },
      { label: '员工电子合同管理', routeName: 'system-contract', permissions: ['system_user.contract_management'] },
    ],
  },
  {
    name: '财务相关',
    // 「经营报表」**复用** `finance.support_statistics` 节点（⛔ 不新增权限点、不动冻结目录）。
    items: [
      // `order.list` 与 `finance.payroll` **任一即见**（`canSeeItem` 是 some 语义）：有订单管理权限
      // 的人就该能进绩效列表、改打手绩效；`finance.payroll` 保留、既有可见面不收紧。
      // 与路由的 `meta.permissions` 同源（两份由 `route-declarations.spec.ts` (c) 逐条比对，只改一边会红）。
      { label: '打手与客服报酬', routeName: 'agents-performance', permissions: ['finance.payroll', 'order.list'] },
      { label: '结算记录', routeName: 'agents-settlement', permissions: ['finance.settlement_records'] },
      { label: '在线结算明细', routeName: 'agents-online-pay-detail', permissions: ['finance.online_settlement_details'] },
      { label: '客服接发单统计', routeName: 'agents-salesman-stats', permissions: ['finance.support_statistics'] },
      // 对标 `/Emp/dayoperation`（`replicaRoutes.ts` 的 `emp-dayoperation`）。与上面那条**同一个节点**：
      // 两页共用一个权限点 ⇒ 拆开是独立架构决定。
      { label: '经营报表', routeName: 'agents-day-report', permissions: ['finance.support_statistics'] },
      // 显示名有意偏离对标原文「鸡蛋铺资金明细与提现」——**只改显示层**（菜单文案 + 页面标题），
      // 路由 / 权限节点零改动；偏离登记在 `config/replicaRoutes.ts` 该条 note 与页面模块
      // `emp-funddetailslist.ts` 文件头（三处同一次改）。
      { label: '团团通资金明细与提现', routeName: 'agents-fund-detail', permissions: ['finance.platform_fund_details'] },
      // 「打手钱包」由原「打手提现审核」+「打手钱包管理」两条合并而来（组内 **8 条不变**，
      // `menu.spec.ts:39` 钉死的 8 **不动**）；两个页签按权限渲染（没权限不画，fail-closed），
      // **两个既有权限节点都保留** ⇒ 已配的角色授权零影响。
      // ⚠️ 必须单行同时含 `routeName:` 与 `permissions:`（后端
      // `tests/test_rbac_wiring.py::_menu_permissions` 逐行解析）。
      { label: '打手钱包', routeName: 'agents-taker-wallet', permissions: ['finance.taker_withdraw_review', 'finance.taker_wallet_adjust'] },
      // 组内追加的自有功能页（对标物没有这一页）⇒ 不进 `replicaRoutes.ts`；权限点是自有节点
      // `finance.xgj_receipt_detail`（`benchmark_id = None`）⇒ 能单独授权。⚠️ 与路由的
      // `meta.permissions` 同一次改齐（两份由 `route-declarations.spec.ts` (c) 逐条比对）。
      { label: '闲管家收款明细', routeName: 'xgj-receipts', permissions: ['finance.xgj_receipt_detail'] },
    ],
  },
  {
    name: '客户维护',
    // ⭐ 2026-11-10 业务方拍板放出（客户线）：整组可见。「客户管理」已接真后端
    // （v2 真页 `V2Customers.vue`；旧前端复刻壳页不动）；其余 2 条照旧是壳页（挂账）。
    // ⭐ 2026-11-10 第二批：「会员余额明细」「会员等级设置」两条删除（业务方拍板暂时不做），
    // 对标 key 登记进 `router/index.ts` 的 `REPLICA_SKIP`；「会员管理/会员分析报表」显示名
    // 改「客户管理/客户分析报表」（纯显示层，路由 / 权限节点零改动）。
    // ⭐ 2026-11-11 第五批：「客户管理」的权限 = member_list ∪ member_list_self_only
    // （自有限制节点，任一即见；只持限制点的用户行集由服务端收口）。
    items: [
      { label: '客户管理', routeName: 'customers', permissions: ['customer.member_list', 'customer.member_list_self_only'] },
      { label: '客户分析报表', routeName: 'customers-analysis', permissions: ['customer.member_analysis_report'] },
      { label: '黑名单管理', routeName: 'customers-blacklist', permissions: ['customer.blacklist'] },
    ],
  },
  {
    name: '游戏维护',
    items: [
      // 「订单项目分类与常用项目」是左分类 / 右项目的主从页（`pages/game-projectsetting.ts`），
      // 由原「订单项目分类管理」+「常用代练项目管理」两条合并而来。
      // ⚠️ 权限声明**两个节点任一即可**（`canSeeItem` 是 some 语义）：只持分类权限的人
      // 仍进得来，页内的写按钮再按各自节点分别收。
      { label: '游戏管理', routeName: 'game-config-games', permissions: ['game.server'] },
      { label: '订单项目分类与常用项目', routeName: 'game-config-projects', permissions: ['game.order_type_categories', 'game.common_projects'] },
    ],
  },
  {
    name: '发平台设置',
    // 组可见、条目级隐藏：组头会出现在侧栏，里面挂 4 条可见条目。⚠️ 条目级 `hidden` 由
    // `useMenu.ts::keepItem` 收口（与组级 `hidden` 是两条独立判据）；空组不渲染 ⇒
    // 没有 `system.platform_integration_settings` 的账号整组看不见（fail-closed）。
    // ⚠️ 「自动发消息到微信群设置」在【系统维护】与本组各有一条同名条目、指向同一路由，
    // **实际可见入口只走【系统维护】那条**（同一功能只留一处可见入口；对标本来也是两处同名，
    // 只是它把系统维护那条置灰了——方向相反、「只留一处」一致）；⛔ 别再写「两处都能进」。
    // 其余 3 条条目级 `hidden` 的真实原因：**或已有其它入口、或页面尚未接后端** ⇒ 不重复放第二处入口；
    // ⛔ 别概括成「3 条都没做完」。
    items: [
      { label: '交易平台对接设置', routeName: 'system-platform-integration', permissions: ['system.platform_integration_settings'] },
      { label: '平台快速发单设置', routeName: 'system-platform-push-order', permissions: ['system.platform_push_order_config'], hidden: true },
      { label: '交易平台自动回复', routeName: 'system-platform-auto-reply', permissions: ['system.platform_auto_reply'] },
      { label: '交易单自动调价配置', routeName: 'system-auto-price-adjust', permissions: ['system.auto_price_adjust_config'], hidden: true },
      { label: '自动发消息到微信群设置', routeName: 'system-wechat-group-push', permissions: ['system.order_push_group_settings'], hidden: true },
      // 代练通两条是**自有功能页**（对标物里没有）⇒ 路由是 `router/index.ts` 的手写条目、
      // ⛔ 不进 `replicaRoutes.ts`（那张表只收对标 89 条）；权限**复用** `order.list`
      // （后端这两个读面的 `permission_classes` 就是它，⛔ 不新增 RBAC 节点）。
      // ⚠️ 「订单要求模板」是**代练通侧**的 `OrderRequireTemp*`，不是本仓的「代练要求模板」。
      // ⚠️ 连带后果：本组从此**也**会对「只持 `order.list`」的账号可见（这两条对他们是可见的）
      // ——⛔ 不是把整组放开：其余条目的可见性判据一个字没动。
      { label: '代练通按日统计', routeName: 'dailiantong-stats', permissions: ['order.list'] },
      { label: '代练通订单要求模板', routeName: 'dailiantong-order-templates', permissions: ['order.list'] },
    ],
  },
  {
    name: '系统维护',
    // 11 条全部接线。
    items: [
      { label: '角色职位权限', routeName: 'system-role-authority', permissions: ['system.role_authority'] },
      { label: '角色职位分组', routeName: 'system-role-group', permissions: ['system.position_groups'] },
      { label: '录单识别设置', routeName: 'system-order-recognition', permissions: ['system.auto_discern_settings'] },
      { label: '订单来源管理', routeName: 'system-order-sources', permissions: ['system.sources'] },
      { label: '绑定上家系统设置', routeName: 'system-source-system', permissions: ['system.bind_upstream_system'] },
      { label: '在线结算支付宝授权', routeName: 'agents-alipay-auth', permissions: ['finance.alipay_authorization'] },
      { label: '其它平台店铺对接', routeName: 'system-pdd-ship', permissions: ['system.pinduoduo_settings'] },
      { label: '系统内部公告管理', routeName: 'system-notices', permissions: ['system.publish_notice'] },
      { label: '系统操作日志管理', routeName: 'system-operation-log', permissions: ['system.logs'] },
      // 渠道支持企微机器人 + 自定义 Webhook。与【发平台设置】组那条同名条目**指向同一个路由**，
      // 实际可见入口只有本组这一条（那边按条目级 `hidden` 不渲染，见组头注释）。
      { label: '自动发消息到微信群设置', routeName: 'system-wechat-group-push', permissions: ['system.order_push_group_settings'] },
      { label: '系统各项设置管理', routeName: 'system-settings', permissions: ['system.settings'] },
    ],
  },
  {
    // ⚠️ **这一组是对标物里没有的**（冻结 IA §4 的偏离）：它与「对标有、我们不做」是两件相反的事，
    // 所以**不进** `replicaRoutes.ts`、也**不需要** `REPLICA_SKIP`（那两处的登记对象都是**对标条目**；
    // 本组没有对标条目）。权限**复用**既有节点（`system.pinduoduo_settings`，「其它店商平台对接」那一个点）：
    // RBAC 目录有一条**逐行对拍对标清单文档**的护栏（`test_rbac_structure.py::test_b5`），
    // 逐条理由与代价见 `config/routePermissions.ts` 的「已知近似」，
    // 以及 `docs/specs/2026-09-22-xgj-shop-integration-design.md` §3-D3 / §12-3。
    name: '第三方平台',
    items: [
      { label: '闲管家店铺对接', routeName: 'thirdparty-xgj-shop', permissions: ['system.pinduoduo_settings'] },
      // 「鸡蛋铺系统同步」入口已整条下架：菜单条目、`/thirdparty/jidanpu-sync` 路由、
      // island 页文件（`thirdparty-jidanpu-sync.ts`）与前端 api 模块（`jidanpuSync.ts`）
      // 都已删除——⛔ 别去找它们（后端与表暂留）。
      // 页面见 `layui-island/pages/wecom-group-qr.ts`；权限**复用** `system.pinduoduo_settings`
      // （设计文档 D9：⛔ **不新增 RBAC 节点**）；本组**不能**加组级 `accountAdminOnly`
      // （会连累同组的「闲管家店铺对接」），AppSecret 的可见面靠**页面自检**收窄
      // （设计文档 D10，落地见那个页文件的门禁）。
      { label: '自动上传企微群码到订单截图', routeName: 'wecom-group-qr', permissions: ['system.pinduoduo_settings'] },
      // 自开发：设置页 / 回调 / 预设菜单 / OAuth 绑定 / 模板消息推送。权限**复用**
      // `system.pinduoduo_settings`（⛔ 不新增节点）；第二道门 = `requiresAccountAdmin: true`
      // + 页内自检（组级 `accountAdminOnly` 照旧不加，会连累全组）。
      { label: '微信公众号设置', routeName: 'system-wechatmp-settings', permissions: ['system.pinduoduo_settings'] },
      // 权限用**自有节点** `system.qqbot_settings`（`benchmark_id = None`；⛔ 不是复用别页的点——
      // 与 `wecom-group-qr` 那条不同，这一条**能**单独授权）。⚠️ 本条目必须**单行**同时含
      // `routeName:` 与 `permissions:`（单行约束同本文件 `BOTTOM_ITEMS` 上方说明）。
      // 第二道门（凭据）：路由级 `requiresAccountAdmin: true` + 页内自检；组级 `accountAdminOnly` 照旧不加。
      { label: 'QQ机器人设置', routeName: 'qqbot-settings', permissions: ['system.qqbot_settings'] },
      // 对接「马上来 self-app」的查群二维码接口：订单缺群码时在自然触发点自动拉一次。
      // 权限**复用** `system.pinduoduo_settings`（同微信公众号设置口径，⛔ 不新增节点）。
      // 本条目必须**单行**同时含 `routeName:` 与 `permissions:`（同 `BOTTOM_ITEMS` 上方说明）。
      // 第二道门（api_key 是凭据）：路由级 `requiresAccountAdmin: true` + 页内自检；组级门照旧不加。
      { label: '马上来查码对接', routeName: 'system-order-qr-fetch', permissions: ['system.pinduoduo_settings'] },
      // 企微自建应用（D-0379）：**v2-only 页**（炭火 `/v2/wecom-settings`；旧壳无此页 ⇒
      // 本条目在旧侧栏渲染为置灰「即将开放」，v2 侧由 `v2/v2Menu.ts` 的映射指向真页）。
      // 权限用**自有节点** `system.wecom_settings`（`benchmark_id = None`，能单独授权）。
      // ⚠️ 本条目必须**单行**同时含 `routeName:` 与 `permissions:`（单行约束同本文件上方说明）。
      // 第二道门（凭据）：路由级 `requiresAccountAdmin: true` + 页内自检；组级门照旧不加。
      { label: '企微自建应用', routeName: 'wecom-settings', permissions: ['system.wecom_settings'] },
    ],
  },
  {
    // ⚠️ **这一组也是对标物里没有的**（自有功能组，冻结的是**对标那部分**，自有功能按需新增）：
    // **不进** `config/replicaRoutes.ts`（那张表是对标 89 条的逐条转录）、也**不需要** `REPLICA_SKIP`
    // （island 页登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`）。
    // ⚠️ 位置追加在末尾（自有功能的组排在对标各组之后），顺序是 `menu.spec.ts` 的冻结项，
    // 动它要同一次改那张清单。
    // 可见性**逐条判**（`useMenu.ts::keepItem`，fail-closed；空组不渲染）。⚠️ `canSeeItem` 对
    // **`isTenantAdmin` 短路放行** ⇒ 租户管理员（非账号管理员的那种）也会看到「性能分析」「应用管理」；
    // 三条的真实可见性由 `composables/__tests__/useMenu.openPlatform.spec.ts` 逐条钉住（⛔ 不能靠"没人测"）。
    // ⚠️ 路由级那一道门**没动**：「性能分析」与「应用管理」仍带 `requiresAccountAdmin: true`
    // （非账号管理员直接敲 URL 照样进不去），「打手对接」刻意**不挂**它（打手不是账号管理员）。
    // ⚠️ **本组不许静默长大**：往本组加页面时，要么确认新页面同样只对相关节点开放（同一次改这里与那张
    // spec 清单），要么换一个组。
    name: '开放平台',
    items: [
      // 性能分析：**自有功能页**（对标物里没有这一页），⛔ 只读（spec §8 第 3 条：
      // 不提供导出、不提供跨租户汇总）。权限**复用** `system.logs`：不写节点码就得同时放宽
      // `routePermissions.spec.ts` 与 `route-declarations.spec.ts` (d) 两条承重网，复用语义最近的点
      // 是既有先例；代价（不能单独授权"谁能看性能分析"）登记在 `config/routePermissions.ts` 的「已知近似」。
      { label: '性能分析', routeName: 'system-perfanalysis', permissions: ['system.logs'] },
      // 应用管理：**自有功能页**（对标物里没有这一页）——给第三方发 AppKey / AppSecret，
      // 让它按签名只读查单。权限**复用** `system.pinduoduo_settings`（⛔ 不新增 RBAC 节点）；
      // 逐条理由与代价见 `config/routePermissions.ts` 的「已知近似」。
      { label: '应用管理', routeName: 'open-apps', permissions: ['system.pinduoduo_settings'] },
      // 打手对接：**自有功能页**（对标物里没有这一页）。⚠️⛔ **必须写成单行、同一行里同时出现
      // `routeName:` 与 `permissions:`**：执行者是后端 `src/tests/test_rbac_wiring.py::_menu_permissions`
      // （**逐行**解析，拆行会被它**静默漏掉**、且不报错）；`test_rbac_menu_alignment.py` 里的
      // `_menu_declared_codes()` 是**跨行**正则、不管单行，别混用这两个文件名。
      // ⚠️ 权限点是**自有节点** `system.allow_taker_open_api`（`benchmark_id = None`，⛔ 不是复用）；
      // 登记表见 `apps/rbac/catalog.py` 模块 docstring（目录从此**不再等于对标全表**）。
      // ⚠️ 本条目对应的路由**不带** `requiresAccountAdmin`——打手不是账号管理员，挂上它就进不来。
      { label: '打手对接', routeName: 'taker-apps', permissions: ['system.allow_taker_open_api'] },
    ],
  },
  {
    // 【APP与小程序】（**自有功能**：对标物里没有这一组、不是复刻）。**不进** `config/replicaRoutes.ts`
    // （那张表只收对标 89 条渲染菜单），也**不需要** `REPLICA_SKIP`；两个 island 页登记在
    // `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
    // ·「数据概览」：复用 `finance.support_statistics`，⛔ 不新增节点、⛔ 不建表
    // （见 `docs/plans/2026-10-23-app-mp-stats.md` §7/§8）；
    // ·「发布文章作品」：自有节点 `system.article_manage`（`benchmark_id = None`），可单独授权
    // （见 `docs/plans/2026-10-23-channel-articles.md` §1）。组名恰好集合由 `menu.spec.ts` 钉住。
    // ⚠️ 条目**必须单行**同时含 `routeName:` 与 `permissions:`（单行约束同 `BOTTOM_ITEMS` 上方说明）。
    name: 'APP与小程序',
    items: [
      { label: '数据概览', routeName: 'app-stats', permissions: ['finance.support_statistics'] },
      { label: '发布文章作品', routeName: 'app-mp-articles', permissions: ['system.article_manage'] },
    ],
  },
]

/**
 * **侧栏最底部的直挂条目**（与 `TOP_LEVEL_ITEMS` / `MENU_GROUPS` 并列的第三条声明）：
 * 定成一个**直接可点的条目**、⛔ 不新开分组（docs/specs/2026-10-08-feedback-design.md §6.1）。
 * 渲染位置：`layouts/AppSidebar.vue` 里 `</el-sub-menu>` 之后、`</el-menu>` 之前。
 *
 * ⚠️ **必须是单行对象、且同一行里同时有 `routeName:` 与 `permissions:`**：
 * 后端有跨前后端的结构网 `src/tests/test_rbac_wiring.py::_menu_permissions` 会**逐行**
 * 解析本文件，只有同一行里两者都在才算一条声明（`test_t2b_experience_sites_match_menu_declaration`
 * 用它校验 `home.feedback` 的体验层位点）。⛔ 拆成多行会让那条网**静默漏掉**这一条，
 * 并在另一侧报「位点指向不存在的路由名」。
 *
 * ⚠️ 权限点**不是新造的**：`home.feedback`（对标 112 号点「问题反馈及建议」）本来就在
 * `apps/rbac/catalog.py` 的 `NODES` 里；本页是**精确命中**、不是借用（逐条理由见
 * `config/routePermissions.ts` 的「已知近似」）。本条**不进** `config/replicaRoutes.ts`、
 * 也**不需要** `REPLICA_SKIP`；island key 登记在 `replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 */
export const BOTTOM_ITEMS: MenuItem[] = [
  { label: '问题反馈', routeName: 'feedback', permissions: ['home.feedback'] },
]
