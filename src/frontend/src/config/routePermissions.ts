/**
 * **路由 name → RBAC 节点 code** 的声明表（D-0020 结案；spec §5.4）。
 *
 * 路由侧「谁看得见哪个页面」的声明。`router/index.ts` 用它填 `meta.permissions`，
 * `guards.ts` 用 `session.canNode()` 执行。`menu.ts` 另外独立再声明一份，
 * 两份由路由声明测试逐条比对；都从这张表派生的话，那条比对会变成同义反复。
 *
 * ## 取值规则（四类，逐条可核；出处 = `src/apps/rbac/catalog.py` 的 `MODULES` / `NODES`）
 * 1. **对标页在目录里有同名 URL 的功能点** ⇒ 用那个点（多数）；
 * 2. **对标页本身就是栏目入口**（对标栏目的 URL 就是该页面，如 `/Order/MyOrderList` 是
 *    「我的订单」栏目）⇒ 用**栏目码**（`level=0`，**无点号**）；
 * 3. **对标页所属栏目被业务方指令剔除**（`docs/JIDANPU-PERMISSION-CATALOG.md` §0：
 *    排团业务 6 / 游戏号库 7）⇒ 用**最近的栏目码**：
 *    排团 → `my_group`（[13] 我的排团）、游戏号库 → `my_account`（[129] 我的账号）。
 *    （短信业务 1151 整栏同样被剔除，但它那 4 个页面已于 2026-09-23 整块下线、本表不再有条目，
 *    所以这里不再列它——见 `router/index.ts` 的 `REPLICA_SKIP` 第 7 类。）
 * 4. **对标页既不是栏目入口、栏目也没被剔除，但目录里就是没有它那个功能点** ⇒ 用**它所属栏目的码**，
 *    并**在下面「已知近似」里逐条登记理由与代价**（`agents-my-settlement`）。
 *
 * ## 已知近似（**必须逐条登记，不许当成精准**）
 * - `accounts-messages`（我的消息）：对标 `/User/MyMessage` **不是权限树里的节点**
 *   （[3] 我的资料 栏目只有一个 `my_profile.edit` API 点）。按同 URL 目录 `'/User/'` 归入
 *   `my_profile`（[3] 我的资料 的栏目码）。**理由**：同目录、同"我的"语义。**代价**：粒度是栏目级。
 * - `agents-my-settlement` 保留 `finance`，同时允许有 `my_performance` 的打手查本人打款。
 *   个人页面与财务「在线结算明细」分开，服务端始终按登录人取数。
 * - ⭐ 2026-10-30（菜单两改线，D-0339）：`orders-exception-log`（异常消失订单，原登记
 *   `→ order` 栏目码，规则 4 的另一例）**整条移出本表**——业务方裁定删除该页（菜单 / island 页 /
 *   证据件 / 页测试同批删，对标 key `order-exceptionorderlog` 登记 `REPLICA_SKIP` 第 9 类），
 *   路由不再生成 ⇒ 本表的「没有多余条目」判据要求映射同一次删掉。原登记的近似语义
 *   （`→ order` 栏目码、栏目级粒度）随条目一并作废，原文见 git 历史。
 * - **排团整组**（本表 **6 条** `groups-*` 路由；复核：
 *   `grep -c "^  'groups-[a-z0-9-]*': 'my_group',$" config/routePermissions.ts` ⇒ 6）→ `my_group`
 *   （[13] 我的排团 栏目码）：对标【排团业务】整栏被业务方指令剔除（`JIDANPU-PERMISSION-CATALOG.md` §0），
 *   目录里没有这些页面的节点，只能按规则 3 取最近的栏目码。
 *   **理由**：「能进排团页」与「持有『我的排团』栏目」**语义并不等同**：`my_group` 的字面是
 *   「我参与的排团」那一页，而这 6 条里只有 `groups-mine` 是它，其余是新增 / 旧版 / 2021 版 /
 *   2025 版 / 公开坑位。
 *   **代价**：栏目级粒度，6 页共用一个点；且**路由门按它收紧是实打实的**——不持 `my_group`
 *   的职位连这些页的路由都进不去（菜单整组 `hidden` 是前端 IA，不构成放行）。
 * - **游戏号库整组**（本表 **5 条** `accounts-game-*` 路由；复核：
 *   `grep -c "^  'accounts-game[a-z0-9-]*': 'my_account',$" config/routePermissions.ts` ⇒ 5）
 *   → `my_account`（[129] 我的账号 栏目码）：对标【游戏号库】整栏同样被剔除。
 *   **理由**：语义不等同——`my_account` 字面是「我的账号」，而这 5 页是内部账号的
 *   新增 / 导入 / 管理 / 售卖预览 / 指定给我的，只有最后一项贴「我的」。
 *   **代价**：栏目级粒度，5 页共用一个点；路由门同样按它收紧。
 * - `thirdparty-xgj-shop`（闲管家店铺对接）→ `system.pinduoduo_settings`（「其它店商平台对接」那一个点）。
 *  **理由**：这是**对标物里没有的自有功能**（业务方指令新增），目录里**不可能**有它的点。
 *   四条取值规则都不适用（规则 1/2 要求对标页存在；规则 3 是「栏目被剔除」；规则 4 要求「栏目未被剔除
 *   但目录里没有那个功能点」——本页**不属任何对标栏目**）。所以在既有 163 点（150 功能点 + 13 栏目；
 *   2026-09-23 本线裁剪前是 210，本线再裁 4 个已下线设置页点前是 167）里取**语义最近**的一个：
 *   两页做的是同一件事（第三方店铺对接 → 录单读单），只是平台不同。
 *   **代价**：两页共用一个点——**不能**单独授权「谁能看闲管家页」；且不持这个点的职位两页都进不去。
 *   要独立授权就得往 RBAC 目录里加自有节点，而那会踩 `test_rbac_structure.py::test_b5`
 *   （目录逐行对拍对标清单文档）⇒ 那是一次独立的架构决策，不在本线范围内
 *   （`docs/specs/2026-09-22-xgj-shop-integration-design.md` §3-D3 / §12-3）。
 *   鸡蛋铺系统同步已下架，别再登记 `thirdparty-jidanpu-sync`，也别去找那个页面。
 *   这个点还被哪些路由共用，以下面 `ROUTE_NODE_CODES` 为准，注释里不另抄计数。
 * - `system-perfanalysis`（【开放平台 → 性能分析】，业务方指令新增）→ `system.logs`
 *   （「系统操作日志管理」那一个点）。**理由**：这是**自有功能页**（对标物里没有这一页，见
 *   `docs/plans/2026-10-04-request-metrics.md` §0），目录里没有它的点。四条取值规则同样都不适用
 *   （它不属任何对标栏目），所以在既有 163 点里取**语义最近**的一个：两页都属于"系统可观测"
 *   ——一笔一笔的系统操作日志 vs 按接口/按人汇总的请求耗时。
 *   **代价**：与「系统操作日志管理」共用一个点 ⇒ **不能**单独授权"谁能看性能分析"（要独立授权
 *   就得往 RBAC 目录里加自有节点，那会踩 `test_rbac_structure.py::test_b5`，属独立架构决策）。
 *   ⚠️ **可见面没有因此放大**：这两页的真实可见性由**第二道门**决定——路由带
 *   `requiresAccountAdmin: true`（判据值 = `/api/auth/me` 的 `is_account_admin`）
 *   ⇒ 不持这个点的**账号管理员**照常能看（`session.canNode` 对管理员短路为真），
 *   而持这个点的**非账号管理员**看不到入口、直连 URL 也会被兜回首页。
 *   ⛔ **2026-10-12 改准**（线 D 修复轮 P2 顺手）：原文还写了「菜单整组标了
 *  `accountAdminOnly: true`」——**那句从 T4起就不成立**：为了让打手看得见
 *   「打手对接」，【开放平台】的**组级门已被去掉**（可见性下移到条目级，见
 *   `config/__tests__/menu.spec.ts` 与 `composables/__tests__/useMenu.openPlatform.spec.ts`）。
 *   ⇒ 这两页今天只剩**路由级**那一道 `requiresAccountAdmin`。
 *   ⚠️ **为什么不干脆不写节点码**（任务书 §6 Task 3 的原话是"`permissions` 不写节点码，靠整组
 *   `accountAdminOnly`"）：`routePermissions.spec.ts` 有一条**恰好集合**断言
 *   （`Object.keys(ROUTE_NODE_CODES)` 必须等于白名单外的**每条**路由、没有多余条目），
 *   而 `route-declarations.spec.ts` 的 (d) 要求每个能查到路由的菜单条目都声明**非空**
 *   `permissions` ⇒ 不写就得**同时放宽那两条承重网**。复用一个语义最近的点是【第三方平台】
 *   两页的既有先例 ⇒ 两条网一个字不放宽；这一条就是那次取舍的登记处。
 * - ⭐ `open-apps`（【开放平台 → 应用管理】，业务方指令新增）→
 *   `system.pinduoduo_settings`（「其它店商平台对接」那一个点）。**理由**：spec **D9 直接裁定**
 *   复用这一点——「⛔ **不新增 RBAC 节点**」，因为 `catalog.NODES` 是**恰好 150 个点**的冻结清单
 *   （`test_rbac_wiring` T1 的等号判据），塞自有节点会让"目录 = 对标全表"那句话变成假话；
 *   且**已有先例**（【第三方平台】那两页就复用了同一个点）。
 *   **代价**：不能单独授权「谁能管开放平台应用」。鸡蛋铺系统同步已下架，不再共用这个点。
 *   还和谁共用，以 `ROUTE_NODE_CODES` 为准。要独立授权就得加自有节点，那是另一次架构决策。
 *   ⚠️ **可见面没有因此放大**：与 `system-perfanalysis` 同款——只剩**路由级**
 *   `requiresAccountAdmin: true`（判据值 = `/api/auth/me` 的 `is_account_admin`；
 *   ⛔ 组级 `accountAdminOnly` 自 T4 2026-10-08 起已不在本组，见上一条的改准块）
 *   ⇒ 持这个点的**非**账号管理员看不到入口、直连 URL 也被兜回首页。
 *   📌 **另一侧的门不同**（**已知缺口**，spec 的已知缺口清单里登记的那条）：本页的**写端点**
 *   判据是 `system.pinduoduo_settings`，而「权限设置」弹层要用的树
 *   `GET /api/permission-nodes/` 的判据是 `system.role_authority`（`apps/rbac/views.py` 的
 *   `DEFAULT_NODE`）——**两个门不一致**。真正会撞上的只剩**裸超管**那一窄类
 *   （账号管理员里持「系统管理员」职位的走 `nodes_of` 短路 ⇒ 树不 403；而裸超管连应用管理
 *   端点本身也会 403，`has_node_now` 对裸超管**不短路**）⇒ 页面对 403 **如实显示后端文案**、
 *   ⛔ 不静默空树、⛔ 不在前端新造第二套权限判据。
 *
 * ⭐ **2026-10-26（第三方平台订单管理线 T7）新增一条近似**：`orders-platform-core`
 * （第三方平台订单管理，对标 `/Order/CoreOrderCenter`）→ **`order.list`**（14「订单管理列表」）。
 *   **理由**：对标这一页在权限目录里**有自己那一个点**（第 1236 号 `交易平台订单处理`，
 *   `docs/JIDANPU-PERMISSION-CATALOG.md` 的 `/Order/CoreOrderCenter` 行），
 *   但本仓 2026-09-23 的权限目录裁剪把 `order.platform_order_center` 删掉了
 *   （`apps/rbac/catalog.py` 的 `PRUNED_BENCHMARK_IDS` 里记着 1236）。该页 2026-10-27 起
 *   **已接真后端**（`GET /api/business/platform-pushes/`），但权限目录里仍没有独立点
 *   ⇒ 不新增节点（新增会踩 `test_rbac_structure.py::test_b5`，属一次独立的架构决策），
 *   在既有节点里取**语义最近**的一个：本页做的正是「查这一批订单」。
 *   **代价（如实）**：**不能**单独授权「谁能看第三方平台订单管理」——持 `order.list` 的人
 *   都能看；将来抛单链路落地、要独立授权时，需要回头把第 1236 号点补进目录（同一次改
 *   `catalog.py` + 接线表 + 数据迁移）。菜单侧的同一个决定写在 `config/menu.ts` 那条注释里，
 *   两处必须同源（`router/__tests__/route-declarations.spec.ts` 的 (c) 钉住）。
 *
 * ## 白名单（**不**在此表内）
 * `login` / `home` / `not-found`：`login` 未认证可访问、`home` 是登录后落地页、
 * `not-found` 是未知路径落点。⭐ 2026-10-30（D-0344）追加 `v2` / `v2-home`（登录即可，
 * 与 `home` 同档）。见 `PUBLIC_ROUTE_NAMES`。
 *
 *   ⚠️ **形态**：栏目码**没有点号**（`order` / `my_orders` / `finance`），
 * 功能点码**恰好一个点号**（`order.list`）。测试钉住这一点。
 *
 * ⭐ **D-0219起本表支持「多节点任一」**：值可以是**数组**（`canNode` /
 * `canSeeItem` 本来就是 `some` 语义，数组形态只是把既有 any 判据显式表达出来）。
 * 第一条就是 `agents-performance`（打手绩效列表）——后端列表门同批改绑，见那条的注释。
 */

/**
 * ⭐ **一条不是近似的登记**（写在这里、⛔ 不写进上面那个「已知近似」段落，是为了让人别把它
 * 按段落位置误读成"又一个借用的点"）：`feedback`（【侧栏最底部 → 问题反馈】，
 * 业务方指令新增）→ `home.feedback`（对标 112 号点「问题反馈及建议」）。
 *
 * **为什么是精确命中而不是近似**：对标物的 `/Index/QuestionBack` 在权限目录里**有自己那一个点**
 * （`docs/JIDANPU-PERMISSION-CATALOG.md` 第 112 行、`apps/rbac/catalog.py` 的 `NODES` 是同一行），
 * 而本页做的正是「问题反馈及建议」这件事 ⇒ 它落在上面「取值规则」的**第 1 类**
 * （对标页在目录里有同名 URL 的功能点 ⇒ 用那个点），**不是**第 3 / 4 类那种"栏目码兜底"。
 *
 * ⚠️ **代价的差别（这才是本条值得单独写的原因）**：上面每一条近似都背着
 * 「与别的页共用一个点 ⇒ 不能单独授权其中一页」；本页**没有**这个代价——
 * 它独占 `home.feedback`，将来要「只给一部分人看反馈页」直接勾这个点即可
 * （对标备注原文「建议全员开放权限」；存量自定义角色不自动补授这条边界见 spec §1.2）。
 * ⚠️ 与 `system-perfanalysis` / `open-apps` 那两条**性质不同**：那两页是对标物里**没有**的自有功能。
 */
export const ROUTE_NODE_CODES: Readonly<Record<string, string | readonly string[]>> = {
  'wecom-client-lab': 'system.wecom_settings',
  'accounts-game': 'my_account',
  'accounts-game-import': 'my_account',
  'accounts-game-mine': 'my_account',
  'accounts-game-new': 'my_account',
  'accounts-game-sell-preview': 'my_account',
  'accounts-messages': 'my_profile',
  'accounts-profile': 'my_profile',
  'agents-alipay-auth': 'finance.alipay_authorization',
  'agents-fund-detail': 'finance.platform_fund_details',
  'agents-my-income': 'my_performance',
  'agents-my-settlement': ['finance', 'my_performance'],
  'agents-online-pay-detail': 'finance.online_settlement_details',
  // ⭐ **D-0219：本表第一条「多节点任一」**（数组 = `canNode`/`canSeeItem`
  //    的 `some` 语义，任一命中即进）。业务方裁定原话：不勾「允许代练员或下家导出自己的绩效
  //    记录」（`my_performance.export`，1163）时**客服**进不了绩效列表页、改不了打手绩效——
  //    「有订单管理（修改）权限的人就该能进、能改」⇒ 加 `order.list`（14「订单管理列表」，
  //    与后端数据范围收口 `PERFORMANCE_MANAGEMENT_NODE` 同一个节点），原 `finance.payroll`
  //    保留（既有可见面不收紧）。
  //    ⚠️ **先核实过路由归属（⛔ 不按名字猜）**：消费 `GET /api/business/performances/`、
  //    行内改接单价走 `/adjust/` 的打手绩效列表是 `emp-emplist` 这页（`/agents/performance`，
  //    菜单【打手与客服报酬】）；`agents-my-income` 是**另一页**「我的收入」（`emp-myemp`，
  //    对标 `/Emp/MyEmp`，代练员自己看），本条不涉及它。后端同批改绑：
  //    `PerformanceViewSet.PERFORMANCE_PERMISSIONS["list"]` = 1163 ∪ `order.list`（任一即进；
  //    1163 降为「自助查看门」，管理侧入口 = `order.list`）。`/adjust/` 仍只认 1162
  //    （`order.submit_payout_price`），进得了页面不等于改得动钱。
  'agents-performance': ['finance.payroll', 'order.list'],
  'agents-salesman-stats': 'finance.support_statistics',
  // 经营报表**复用**「客服接发单统计」那一个节点
  // （spec §0 第 3 条裁定：⛔ 不新增 RBAC 节点、不动 150 条冻结清单）。
  // ⚠️ 这是**规则 4 之外的第五种情形**：对标页 `/Emp/dayoperation` 有同名 URL 的功能点
  // （1192 `finance.support_statistics`），而两页今天共用它 ⇒ 无法单独授权「只看经营报表」
  // （代价如实登记在 spec §9 第 8 条）。要拆是独立架构决定。
  'agents-day-report': 'finance.support_statistics',
  // ⭐ 【APP与小程序 → 数据概览】
  // （自建 island 页 `/business/app-stats`）。计划
  // `docs/plans/2026-10-23-app-mp-stats.md` §8 第一条：**复用**同一个节点
  // `finance.support_statistics`（⛔ 不新增节点、⛔ 不加迁移）。
  'app-stats': 'finance.support_statistics',
  // ⭐ 【发平台设置】组的两条自有功能页
  // （自建 island 页 `/system/dailiantong-stats` 与 `/system/dailiantong-order-templates`）。
  // **复用** `order.list`：后端这两个读面的 `permission_classes` 就是
  // `has_node("order.list")`（`dailiantong_extra_views.py` 的 `EXTRA_ACTION_NODE`），
  // ⛔ 不新增 RBAC 节点。**代价**：不能单独授权「谁能看代练通统计 / 模板」——
  // 持 `order.list`（订单管理列表）的人都能看；这两页本来就是订单页的运营视图。
  'dailiantong-stats': 'order.list',
  'dailiantong-order-templates': 'order.list',
  'agents-settlement': 'finance.settlement_records',
  // ⭐（计划
  // `docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §4.4 / §5）：
  // 【财务相关 → 打手提现审核】（`agents-withdraw-review`，自建 island 页 `/agents/withdraw-review`）。
  // ⭐ 与上面那几条「复用语义最近的点」**性质不同**：本页用的是**本批新增的自有节点**
  // `finance.taker_withdraw_review`（"打手提现审核"，编号取 `catalog.NODES` 的下一个空号）
  // ⇒ 本页**能**单独授权，没有「与别页共用一个点」的代价（同 `taker-apps` / `feedback` 的性质）。
  // ⚠️ 该节点由**后端同一次**在 `src/apps/rbac/catalog.py` 的 `NODES` 里登记（连同冻结计数
  // 那几条网）——本表这一条与该登记**必须同值**，否则 `routePermissions.spec.ts` 的
  // 「表里的每个 code 都真实存在于后端目录」会当场点名（打错一个字母也一样）。
  // ⚠️ 本页**没有对标证据件** ⇒ ⛔ 不进 `config/replicaRoutes.ts`（那张表只收对标 89 条菜单）。
  // 📌 **财务页合并**：路由 name 不变（旧深链照旧活着），但它的
  // **路由级判据并入合并页**——独立路由的权限声明保留（合并页路由是另一条 name）。
  'agents-withdraw-review': 'finance.taker_withdraw_review',
  // ⭐（客服后台打手钱包管理）：
  // 【财务相关 → 打手钱包管理】（`agents-wallet-adjust`，自建 island 页 `/agents/wallet-adjust`）。
  // 权限点 = **自有**节点 `finance.taker_wallet_adjust`
  // （后端 `apps/rbac/catalog.py` 的 `NODES` 同一次登记；`routePermissions.spec.ts` 的
  // 「表里的每个 code 都真实存在于后端目录」会当场点名打错字）。
  // ⚠️ 本页**没有对标证据件** ⇒ ⛔ 不进 `config/replicaRoutes.ts`（那张表只收对标 89 条菜单）。
  'agents-wallet-adjust': 'finance.taker_wallet_adjust',
  // ⭐（spec
  // `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.2）：
  // 【财务相关 → 打手钱包】合并页（两页签：提现审核 / 钱包管理）。**任一节点即进**
  // （`canNode` 是 some 语义，先例 = `agents-performance` / `game-config-projects`）；
  // 两个既有节点都保留、人事已配的角色授权零影响；页签按权限渲染（没权限不画）。
  'agents-taker-wallet': ['finance.taker_withdraw_review', 'finance.taker_wallet_adjust'],
  // ⭐ 【财务相关 → 闲管家收款明细】（`xgj-receipts`，
  // 自建 island 页 `/finance/xgj-receipts`）。权限点 = **自有**节点
  // `finance.xgj_receipt_detail`（后端 `catalog.NODES` 与数据迁移 `0012` 同一次落）
  // ⇒ 本页能单独授权（与上面两条自有页同一性质）。
  'xgj-receipts': 'finance.xgj_receipt_detail',
  'customers': ['customer.member_list', 'customer.member_list_self_only'],
  'customers-analysis': 'customer.member_analysis_report',
  'customers-blacklist': 'customer.blacklist',
  // ⭐（2026-11-10 第二批）原 `customers-balance-log` / `customers-levels` 两行的映射已删：
  // 业务方裁定两页整个去掉（REPLICA_SKIP 留痕），路由不再生成 ⇒
  // 本表的「没有多余条目」判据要求映射同一次删掉（与 orders-exception-log 同款先例）；
  // 两个 RBAC 节点本身留在冻结目录未动（`PENDING` 登记照旧）。
  // ⭐ 分类页与常用项目页**合并**成一页 ⇒ 路由判据 = 两个节点任一
  // （`canNode` 是 some 语义）。页内的写按钮再按各自节点分别收。
  // 旧的 `game-config-categories` 路由 / 菜单条目已删除，这里不再保留它的映射。
  'game-config-games': 'game.server',
  'game-config-projects': ['game.order_type_categories', 'game.common_projects'],
  'groups-legacy': 'my_group',
  'groups-mine': 'my_group',
  'groups-new': 'my_group',
  'groups-public-tasks': 'my_group',
  'groups-v2021': 'my_group',
  'groups-v2025': 'my_group',
  'order-detail': 'order.view_detail',
  'order-new': 'order.add_edit',
  'orders': 'order.list',
  // ⭐（客服精简工作台）：
  // 【自建路由】`/kefu`（`kefu-console`，spec `docs/specs/2026-10-20-kefu-console-page.md` §2）。
  // 它**不进后台菜单**（`config/menu.ts` 不动，入口 = 直接访问 URL / 将来的桌面小窗），
  // 所以本表里这一条**没有菜单侧的对偶声明**——`route-declarations.spec.ts` 的 (c) 只比对
  // 「菜单里有 `routeName` 的条目」，因此不会因为缺对偶而红；(a)/(d) 只看非白名单路由有没有
  // 非空 `permissions`，本条满足。
  // 取值 = `order.list`（14「订单管理列表」）：客服角色持有 `order` 栏目就会拿到它 ⇒ 能进。
  // 本页只做「查单（订单号搜索）/ 录单 / 看详情」：编辑、指派、抛大厅、
  // 改状态、复制已从本页移除（计划 2026-10-27-kefu-narrow-window-revamp §2）。
  // ⚠️ 页内按钮另按各自的节点 fail-closed 收，本表只管「谁进得来」。
  'kefu-console': 'order.list',
  //（`orders-exception-log` 已随「异常消失订单」下线整条移出，见文件头「已知近似」。）
  'orders-inside-hall': 'grab_hall',
  'orders-mine': 'my_orders',
  'orders-platform-action-log': 'order.platform_action_logs',
  // ⭐ 对标 `/Order/CoreOrderCenter`。
  // 对标的第 1236 号点已被目录裁剪删掉（该页 2026-10-27 起已接真后端，但目录仍无独立点）
  // ⇒ 取语义最近的 `order.list`；
  // 理由与代价（不能单独授权这一页）见上面「已知近似」段的那一条。
  'orders-platform-core': 'order.list',
  'orders-recycle': 'order.recycle',
  'orders-source-price-log': 'order.upstream_price_change_logs',
  'orders-taobao-ship': 'order.taobao_ship_records',
  'system-auto-price-adjust': 'system.auto_price_adjust_config',
  'system-blacklist': 'system_user.blacklist',
  'system-contract': 'system_user.contract_management',
  'system-login-restrict': 'system_user.login_restriction',
  'system-notices': 'system.publish_notice',
  'system-operation-log': 'system.logs',
  'system-order-recognition': 'system.auto_discern_settings',
  'system-order-sources': 'system.sources',
  'system-pdd-ship': 'system.pinduoduo_settings',
  'system-platform-auto-reply': 'system.platform_auto_reply',
  'system-platform-integration': 'system.platform_integration_settings',
  'system-platform-push-order': 'system.platform_push_order_config',
  'system-real-name': 'system_user.realname_records',
  'system-role-authority': 'system.role_authority',
  'system-role-group': 'system.position_groups',
  'system-settings': 'system.settings',
  'system-source-system': 'system.bind_upstream_system',
  'system-user-edit': 'system_user.add_edit',
  'system-user-new': 'system_user.add_edit',
  'system-users': 'system_user.list',
  'system-wechat-group-push': 'system.order_push_group_settings',
  // 自有功能（对标物里没有这一页）：取语义最近的「其它店商平台对接」点，理由与代价见上面的「已知近似」。
  'thirdparty-xgj-shop': 'system.pinduoduo_settings',
  // 鸡蛋铺系统同步的路由已删。别加回 `thirdparty-jidanpu-sync`：恰好集合会红，页面文件也不在了。
  // 自有功能（对标物里没有这一页）：取语义最近的「系统操作日志管理」点，
  // 理由与代价（含"为什么不写节点码"）见上面的「已知近似」。
  'system-perfanalysis': 'system.logs',
  // 自有功能：复用「其它店商平台对接」，不能单独授权。共用名单以本表为准。
  'open-apps': 'system.pinduoduo_settings',
  // 【第三方平台】→「自动上传企微群码到订单截图」（业务方指令新增；
  // 设计文档 `docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §8.1 / D9）。
  // ⛔ **不新增 RBAC 节点**（D9：`catalog.NODES` 是恰好 150 点的冻结清单）⇒ 取语义最近的
  // 「其它店商平台对接」那一个点，与同组那两页同一个点。
  // ⚠️ 代价（与 `open-apps` 同款，如实登记）：**不能**单独授权「谁能管这把企微群码密钥」——
  // 这一个点今天管的是 `system-pdd-ship` / `thirdparty-xgj-shop` / `open-apps` / 本页
  // （前三条见上面两条登记；本页是**第 4 个**共用它的路由）。
  // ⚠️ **可见面没有因此放大**：本页另有第二道门 `requiresAccountAdmin: true`（设计文档 D10，
  // 判据值 = `/api/auth/me` 的 `is_account_admin`）⇒ 持这个点的**非**账号管理员看不到入口、
  // 直连 URL 也被兜回首页，页面被深链打开时还有页内自检（同一个节点 + 账号管理员，fail-closed）。
  'wecom-group-qr': 'system.pinduoduo_settings',
  // 【第三方平台】→「微信公众号设置」（D-0268，2026-10-19 自开发）。
  // ⛔ **不新增 RBAC 节点**（计划 H7 / 第三方平台组现行口径：`catalog.NODES` 是恰好 150 点
  // 的冻结清单）⇒ 取语义最近的「其它店商平台对接」那一个点，与同组各页同一个点。
  // ⚠️ 代价（与 `open-apps` / `wecom-group-qr` 同款，如实登记）：**不能**单独授权
  // 「谁能管公众号配置」——这一个点今天管的是 `system-pdd-ship` / `thirdparty-xgj-shop` /
  // `open-apps` / `wecom-group-qr` / 本页（**第 5 个**共用它的路由）。
  // ⚠️ **可见面没有因此放大**：本页另有第二道门 `requiresAccountAdmin: true`（照
  // 企微群码 D10 的同款做法——AppSecret 虽只回掩码，token / aes_key 是原文）⇒
  // 持这个点的**非**账号管理员看不到入口、直连 URL 也被兜回首页，深链还有页内自检。
  'system-wechatmp-settings': 'system.pinduoduo_settings',
  // 【第三方平台】→「马上来查码对接」（D-0348，2026-10-31；api/orderQrFetch.ts 同值声明）。
  // ⭐ 复用 `system.pinduoduo_settings`（微信公众号设置同款：第三方平台组现行口径，
  // D-0128 冻结 ⛔ 不新增节点）——本页是「第 6 个」共用它的路由。
  // ⚠️ **可见面没有因此放大**：本页另有第二道门 `requiresAccountAdmin: true`
  //（api_key 是凭据，后端只回掩码）⇒ 持这个点的**非**账号管理员看不到入口、
  // 直连 URL 也被兜回首页，深链还有页内自检。
  'system-order-qr-fetch': 'system.pinduoduo_settings',
  // 【第三方平台】→「QQ机器人设置」。
  // ⭐ 与上面那几条「复用语义最近的现成点」**性质不同**：本页用的是**本仓第三条自有节点**
  //    `system.qqbot_settings`（`benchmark_id = None`；存量租户靠数据迁移
  //    `0008_add_qqbot_settings_node`）⇒ 本页**能**单独授权，没有「与别页共用一个点」的代价
  //    （同 `taker-apps` / `feedback` 那两条自有节点的性质）。
  // ⚠️ **第二道门**：路由带 `requiresAccountAdmin: true`（这一页管 AppSecret 凭据，
  //    与 `wecom-group-qr` 的 D10 同款）；岛页被深链打开时还有页内自检（同一个节点 + 账号管理员，
  //    fail-closed）。节点码常量住在 `api/qqbot.ts` 的 `QQBOT_SETTINGS_NODE`（岛页扫描只扫 pages/）。
  'qqbot-settings': 'system.qqbot_settings',
  // 侧栏最底部那一条（`config/menu.ts` 的 `BOTTOM_ITEMS`，业务方指令）。
  // ⭐ **精确命中，不是近似**：对标 112 号点 `home.feedback` 的原文就是「问题反馈及建议」
  //    （`docs/JIDANPU-PERMISSION-CATALOG.md` 第 112 行；`apps/rbac/catalog.py` 的 `NODES` 里
  //    也是同一条），本页做的正是这件事 ⇒ 上面那四条取值规则里的**第 1 类**（对标页在目录里
  //    有同名 URL 的功能点 ⇒ 用那个点），⛔ 不需要在「已知近似」里登记代价。
  //    ⚠️ 与 `system-perfanalysis` / `open-apps` 那两条**性质不同**：那两页是对标物里没有的
  //    自有功能、只能复用语义最近的点；本页的对标页 `/Index/QuestionBack` 本来就有自己的点，
  //    所以这里**没有**「与别页共用一个点、不能单独授权」那类代价。
  //    ⚠️ 本线把 `catalog.py` 里这一条的 `available` 从 `False` 翻成 `True` 并补了 `INTERCEPT`
  //    位点（spec §5）——那是**后端**侧的同批改动，不在本表。
  'feedback': 'home.feedback',
  // 【APP与小程序】→「发布文章作品」（业务方 2026-10-23 口径；计划
  // `docs/plans/2026-10-23-channel-articles.md` §1）。
  // ⭐ 与上面那几条「复用语义最近的现成点」**性质不同**：本批**新增了自有节点**
  //    `system.article_manage`（`benchmark_id = None`）⇒ 本页用的是**自己的**点，
  //    **没有**「与别页共用一个点、不能单独授权」那类代价（同 `taker-apps` / `feedback` 的性质）。
  //    ⚠️ 那个点由**后端同批**登记（`apps/rbac/catalog.py` 的 `NODES` + `EXPECTED_NODE_COUNT`
  //    + 数据迁移）；前端这里只声明「本页用哪个点」，两件事分别在两侧。
  //    ⛔ 若后端那一半没落，`routePermissions.spec.ts` 的「表里每个 code 都真实存在于后端目录」
  //    会红——那是**正确的**跨分支依赖信号，⛔ 不要为了让本表变绿去改后端目录或删这一条。
  'app-mp-articles': 'system.article_manage',
  // ⭐ **T4（2026-10-08，打手侧开放接口）**：【开放平台】→「打手对接」
  //    （**自有功能页**，对标物里没有这一页）。⚠️ 与上面那几条「复用语义最近的现成点」
  //    **性质完全不同**：本线按业务方裁定（spec §7.1 的落点 ③）**新增了本仓第一条自有节点**
  //    `system.allow_taker_open_api`（`benchmark_id = None`）⇒ 本页用的是**自己的**点，
  //    **没有**「与别页共用一个点、不能单独授权」那类代价（同 `feedback` 那条的性质）。
  //    ⚠️ 那个点靠数据迁移 `apps/rbac/migrations/0006_add_taker_open_api_node.py` 落到存量租户；
  //    ⛔ 不改这里的取值不会让目录多出点来——两件事分别在前后端两侧。
  'taker-apps': 'system.allow_taker_open_api',
  // 【系统人员】组 →「自助注册」（业务方 2026-10-16 需求，spec
  // `docs/specs/2026-10-16-self-registration-links-design.md` §6）。
  // ⛔ **不新增 RBAC 节点**：取「新开系统账号 / 编辑系统账号」的**同一个点**
  // `system_user.add_edit`（"开账号"这一件事本来就是这个点管的，自助开户是它的另一种入口）。
  // ⚠️ 值写成**数组**是照 spec §6 的字面（D-0219 起的「多节点任一」形态；单元素数组的
  //    `some` 语义与单串等价，`nodeCodesFor` 两种都展开）。
  // ⚠️ 代价（与既有共用点的口径一致，如实登记）：**不能**单独授权「谁能用自助注册页」——
  //    持这个点的职位同时能开/编辑系统账号。要独立授权就得往 RBAC 目录里加自有节点，
  //    那会踩 `test_rbac_structure.py::test_b5`，属独立架构决策（本期 spec 明确不做）。
  'self-registration': ['system_user.add_edit'],
  // ⭐ v2 子树里**挂了权限门**的 34 条（`router/index.ts` 的 meta.permissions 逐条同源）；
  //   无权限门的 v2 子路由走 `PUBLIC_ROUTE_NAMES` 白名单（见上），不进本表。
  //   ⭐（2026-11-05 并集收口补登记：六页线合并时随路由并集带进，值照各自路由 meta 抄）。
  //   ⭐（2026-11-06 系统维护批次）+11：系统维护组 11 页全接线，值照各自路由 meta 抄。
  //   ⭐（2026-11-06 APP与小程序+问题反馈批）+3：数据概览 / 问题反馈 / 发布文章作品，
  //   值照各自路由 meta 抄（与旧路由 app-stats / feedback / app-mp-articles 同节点）。
  //   ⭐（2026-11-06 开放平台批）+3：性能分析 / 应用管理 / 打手对接，值照各自路由 meta 抄
  //   （与旧路由 system-perfanalysis / open-apps / taker-apps 同节点）。
  //   ⭐（2026-11-06 第三方平台批）+4：闲管家店铺对接 / 自动上传群码（合并页）/
  //   微信公众号设置 / QQ机器人设置，值照各自路由 meta 抄（前三条与旧路由同用
  //   `system.pinduoduo_settings`；QQ机器人用自有节点 `system.qqbot_settings`）。
  'v2-user-new': 'system_user.add_edit',
  'v2-user-list': 'system_user.list',
  'v2-real-name': 'system_user.realname_records',
  'v2-login-restrict': 'system_user.login_restriction',
  'v2-blacklist': 'system_user.blacklist',
  'v2-contract': 'system_user.contract_management',
  'v2-xgj-receipts': 'finance.xgj_receipt_detail',
  // ⭐ 2026-11-10 客户线：与旧路由 `customers` 同一节点（两份由 route-declarations.spec (c) 比对）。
  // ⭐ 2026-11-11 第五批：两节点任一即进（自有限制节点 `customer.member_list_self_only`）。
  'v2-customers': ['customer.member_list', 'customer.member_list_self_only'],
  // ⭐（2026-11-10 第三批）与旧路由 `customers-analysis` 同一节点；后端 analysis 端点的闸是
  // `customer.member_list`（管理员短路）——两点都在冻结目录（available=False），实际只有管理员能过。
  'v2-customer-analysis': 'customer.member_analysis_report',
  // ⭐（2026-11-11 第七批）「黑名单管理」v2 占位页：与旧路由 `customers-blacklist` 同节点
  // `customer.blacklist`（目录 138 号点，冻结未翻真 available=False，实际只有管理员能过）。
  'v2-customer-blacklist': 'customer.blacklist',
  // 切换时沿用对应旧页面的入口权限。
  'v2-my-orders': 'my_orders',
  'v2-hall': 'grab_hall',
  'v2-income': 'my_performance',
  'v2-settlement': ['finance', 'my_performance'],
  'v2-profile': 'my_profile',
  'v2-messages': 'my_profile',
  'v2-order-audit': 'order.platform_action_logs',
  'v2-order-new': 'order.add_edit',
  'v2-order-manage': 'order.list',
  'v2-platform-orders': 'order.list',
  'v2-order-recycle': 'order.recycle',
  'v2-order-action-log': 'order.platform_action_logs',
  'v2-ship-log': 'order.taobao_ship_records',
  'v2-upstream-price-log': 'order.upstream_price_change_logs',
  'v2-game-manage': 'game.server',
  'v2-game-projects': ['game.order_type_categories', 'game.common_projects'],
  'v2-platform-integration': 'system.platform_integration_settings',
  'v2-platform-auto-reply': 'system.platform_auto_reply',
  'v2-dailiantong-stats': 'order.list',
  'v2-dailiantong-templates': 'order.list',
  'v2-performance': ['finance.payroll', 'order.list'],
  'v2-bill-summary': 'finance.settlement_records',
  'v2-day-report': 'finance.support_statistics',
  'v2-online-pay-detail': 'finance.online_settlement_details',
  'v2-fund-detail': 'finance.platform_fund_details',
  'v2-role-authority': 'system.role_authority',
  'v2-role-group': 'system.position_groups',
  'v2-order-recognition': 'system.auto_discern_settings',
  'v2-order-sources': 'system.sources',
  'v2-source-system': 'system.bind_upstream_system',
  'v2-alipay-auth': 'finance.alipay_authorization',
  'v2-pdd-ship': 'system.pinduoduo_settings',
  'v2-notices': 'system.publish_notice',
  'v2-operation-log': 'system.logs',
  'v2-wechat-group-push': 'system.order_push_group_settings',
  'v2-settings': 'system.settings',
  // ⭐（2026-11-06 APP与小程序+问题反馈批）+3：与旧路由 app-stats / feedback / app-mp-articles
  //   同节点（各自路由 meta 抄）。
  'v2-app-stats': 'finance.support_statistics',
  'v2-feedback': 'home.feedback',
  'v2-articles': 'system.article_manage',
  // ⭐（2026-11-06 开放平台批）+3：与旧路由 system-perfanalysis / open-apps / taker-apps
  //   同节点（各自路由 meta 抄）。
  'v2-perfanalysis': 'system.logs',
  'v2-open-apps': 'system.pinduoduo_settings',
  'v2-taker-apps': 'system.allow_taker_open_api',
  // ⭐（2026-11-06 第三方平台批）+4：与旧路由 thirdparty-xgj-shop / wecom-group-qr /
  //   system-wechatmp-settings / qqbot-settings 同节点（各自路由 meta 抄）。
  //   「马上来查码对接」不设 v2 路由（功能并入 v2 群码页），旧路由的登记照旧。
  'v2-xgj-shop': 'system.pinduoduo_settings',
  'v2-wecom-group-qr': 'system.pinduoduo_settings',
  'v2-wechatmp-settings': 'system.pinduoduo_settings',
  'v2-qqbot-settings': 'system.qqbot_settings',
  // ⭐（2026-11-11 企微自建应用线，D-0379）与 v2 路由同节点（自有节点，`benchmark_id=None`；
  //   后端 `apps/wecom/views.py::WecomSettingsGateView` 的类体装配）。旧壳没有 `wecom-settings`
  //   路由（v2-only）；独立客户端测试页另复用同一权限节点。
  'v2-wecom-settings': 'system.wecom_settings',
}

/** 无需路由业务节点。审批中心和企微会话检索在登录后分别核对服务端权限。 */
export const PUBLIC_ROUTE_NAMES: ReadonlySet<string> = new Set([
  'login',
  'visitor-chat',
  'home',
  'v2-internal-chat-visitors', // 登录后由访客接待 API 校验接待资格。
  'v2-internal-chat-window', // 登录后由内部沟通 API 校验访问范围。
  'not-found',
  'v2',
  'v2-home',
  'v2-approvals',
  'v2-order-detail-preview', // 登录后由订单详情与群聊 API 分别校验访问范围。
  'v2-wecom-archive',
  'v2-internal-chat-settings', // 由 requiresAccountAdmin 限制入口。
  'v2-wxpusher-settings', // 由 requiresAccountAdmin 限制入口。
])

/** 该路由要声明的准入判据；未命中回 `[]`（**不抛**——守卫与路由生成都要能吃空值）。
 *  值为数组（D-0219 起的「多节点任一」形态）时原样展开；消费方（`canNode` / `canSeeItem`）
 *  本来就是 `some` 语义，数组不需要额外标记。 */
export function nodeCodesFor(routeName: string): string[] {
  const code = ROUTE_NODE_CODES[routeName]
  if (code === undefined) return []
  // `typeof` 收窄（`Array.isArray` 收不了 `readonly string[]` 的只读性那一侧）。
  if (typeof code === 'string') return [code]
  return [...code]
}
